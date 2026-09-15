"""Run and profile the explicit integer-numeric solver on a frozen request."""

from __future__ import annotations

import argparse
import cProfile
import json
import platform
import pstats
import resource
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from time import perf_counter, process_time

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from apsgo_scheduler.api.request import fingerprint_public_request  # noqa: E402
from apsgo_scheduler.app.delivery_report import build_delivery_report  # noqa: E402
from apsgo_scheduler.app.rule_set_loader import fingerprint_rule_set_spec  # noqa: E402
from apsgo_scheduler.app.service import solve_request  # noqa: E402
from apsgo_scheduler.core.contracts import INTEGER_NUMERIC_SEMANTICS_KEY  # noqa: E402
from apsgo_v7_service.diagnostics import _json_values  # noqa: E402
from tools.profile_solver_search import load_request  # noqa: E402
from tools.verify_solver_diagnostics import _code_identity, _sha256, _write_json  # noqa: E402

_PROJECTIONS = {
    "prohibited_violation_severity": "severity_round_6_half_up_per_violation",
    "underweight_total_gap": "underweight_gap_round_2_half_up_per_chain",
    "old_backlog_last_completion_hours": "delivery_second_half_up",
    "delivery_wait_tardiness_tonne_hours": "delivery_second_half_up",
}


def derive_numeric_request(request):
    """Change only the declared numeric standard and its required projections."""
    quality = tuple(
        replace(
            criterion,
            numeric_projection=_PROJECTIONS.get(
                criterion.metric_key, "integer_exact_v1"
            ),
        )
        for criterion in request.rule_set_spec.quality_spec
    )
    spec = replace(request.rule_set_spec, quality_spec=quality)
    spec = replace(spec, fingerprint=fingerprint_rule_set_spec(spec))
    policy = replace(
        request.policy,
        numeric_semantics_key=INTEGER_NUMERIC_SEMANTICS_KEY,
    )
    return replace(request, rule_set_spec=spec, policy=policy)


def _request_envelope(request):
    return {
        "request": _json_values(request),
        "request_fingerprint": fingerprint_public_request(request),
        "rule_set_fingerprint": request.rule_set_spec.fingerprint,
    }


def _peak_rss_bytes():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _run_once(request, destination, *, profile=False):
    destination.mkdir(parents=True, exist_ok=False)
    _write_json(destination / "prepared_request.json", _request_envelope(request))
    profiler = cProfile.Profile() if profile else None
    wall_started, cpu_started = perf_counter(), process_time()
    if profiler is not None:
        profiler.enable()
    try:
        result = solve_request(request)
    finally:
        if profiler is not None:
            profiler.disable()
    wall_seconds = perf_counter() - wall_started
    cpu_seconds = process_time() - cpu_started
    report = build_delivery_report(request, result)
    candidate = result.release or result.diagnostic_candidate
    measurement = {
        "wall_seconds": Decimal(str(wall_seconds)),
        "cpu_seconds": Decimal(str(cpu_seconds)),
        "cpu_to_wall_ratio": Decimal(str(cpu_seconds / wall_seconds)),
        "peak_rss_bytes": _peak_rss_bytes(),
        "status": result.status.value,
        "stop_reason": result.stop_reason.value,
        "publishable": result.release is not None,
        "quality_key": None
        if candidate is None
        else tuple(
            (candidate.evaluation if result.release else candidate.search_evaluation).quality_key
        ),
        "run_manifest": _json_values(result.run_manifest),
        "core_audit": _json_values(result.core_audit),
        "application_audit": _json_values(result.audit_report),
        "issues": _json_values(result.issues),
        "result_fingerprint": result.result_fingerprint,
    }
    _write_json(destination / "measurement.json", measurement)
    _write_json(destination / "response.json", _json_values(result))
    if report is not None:
        _write_json(destination / "delivery_report.json", report)
    if profiler is not None:
        profiler.dump_stats(str(destination / "solver.pstats"))
        with (destination / "profile_functions.txt").open(
            "x", encoding="utf-8"
        ) as stream:
            stats = pstats.Stats(profiler, stream=stream)
            stats.sort_stats("cumulative").print_stats(80)
            stats.sort_stats("tottime").print_stats(50)
    return measurement


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-request", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--profile-first", action="store_true")
    parser.add_argument("--candidate-check-limit", type=int)
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    if args.output_dir.exists():
        raise FileExistsError(args.output_dir)

    source = load_request(args.prepared_request)
    request = derive_numeric_request(source)
    if args.candidate_check_limit is not None:
        request = replace(
            request,
            policy=replace(
                request.policy, candidate_check_limit=args.candidate_check_limit
            ),
        )
    args.output_dir.mkdir(parents=True)
    identity = {
        "source": _code_identity(ROOT),
        "tool_sha256": _sha256(Path(__file__)),
        "source_request_sha256": _sha256(args.prepared_request),
        "source_request_fingerprint": fingerprint_public_request(source),
        "numeric_request_fingerprint": fingerprint_public_request(request),
        "source_rule_set_fingerprint": source.rule_set_spec.fingerprint,
        "numeric_rule_set_fingerprint": request.rule_set_spec.fingerprint,
        "numeric_semantics_key": request.policy.numeric_semantics_key,
        "candidate_check_limit": request.policy.candidate_check_limit,
        "total_time_limit_seconds": request.policy.total_time_limit_seconds,
        "finalization_reserve_seconds": request.policy.finalization_reserve_seconds,
        "seed": request.policy.seed,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "repeat": args.repeat,
        "profile_first": args.profile_first,
    }
    _write_json(args.output_dir / "input_identity.json", identity)
    measurements = []
    for index in range(1, args.repeat + 1):
        measurement = _run_once(
            request,
            args.output_dir / f"run_{index:02d}",
            profile=args.profile_first and index == 1,
        )
        measurements.append(measurement)
        print(
            json.dumps(
                {
                    "run": index,
                    "wall_seconds": str(measurement["wall_seconds"]),
                    "cpu_seconds": str(measurement["cpu_seconds"]),
                    "status": measurement["status"],
                    "stop_reason": measurement["stop_reason"],
                    "quality_key": measurement["quality_key"],
                },
                ensure_ascii=False,
                default=str,
            ),
            flush=True,
        )
    _write_json(args.output_dir / "summary.json", {"identity": identity, "runs": measurements})
    return 0 if all(item["core_audit"]["passed"] for item in measurements) else 2


if __name__ == "__main__":
    raise SystemExit(main())
