"""Replay a bound diagnostic request through the public solver, without a database."""

from __future__ import annotations

import argparse
import cProfile
import json
import platform
import pstats
import sys
from contextlib import ExitStack
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from time import perf_counter, process_time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from apsgo_scheduler.api.request import (  # noqa: E402
    OrderInput,
    PeriodInput,
    QualityCriterionSpec,
    RuleDefinitionSpec,
    RuleSetSpec,
    SchedulingRequest,
    VirtualPrototypeInput,
    fingerprint_public_request,
)
from apsgo_scheduler.app.rule_set_loader import fingerprint_rule_set_spec  # noqa: E402
from apsgo_scheduler.app.service import solve_request  # noqa: E402
from apsgo_scheduler.core import neighborhoods, solver  # noqa: E402
from apsgo_scheduler.core.contracts import RuleScope, SolverPolicy, fingerprint  # noqa: E402
from apsgo_scheduler.core.model import MaterialRole  # noqa: E402
from apsgo_scheduler.core.delivery_timing import DeliveryTimingInput, OrderTimingInput  # noqa: E402
from apsgo_v7_service.diagnostics import _json_values  # noqa: E402
from tools.verify_solver_diagnostics import _code_identity, _sha256, _write_json  # noqa: E402


def load_request(source: Path | bytes):
    """Rebuild exact public carriers; verify identities before any policy override."""
    payload = source if isinstance(source, bytes) else source.read_bytes()
    data = json.loads(payload.decode("utf-8"), parse_float=Decimal)
    raw = data["request"].copy()
    raw["orders"] = tuple(
        OrderInput(**{**item, "material_role": MaterialRole(item["material_role"])})
        for item in raw["orders"]
    )
    raw["periods"] = tuple(PeriodInput(**item) for item in raw["periods"])
    raw["virtual_prototypes"] = tuple(
        VirtualPrototypeInput(**item) for item in raw["virtual_prototypes"]
    )
    spec = raw["rule_set_spec"].copy()
    spec["rules"] = tuple(
        RuleDefinitionSpec(**{**item, "scope": RuleScope(item["scope"])})
        for item in spec["rules"]
    )
    spec["quality_spec"] = tuple(QualityCriterionSpec(**item) for item in spec["quality_spec"])
    raw["rule_set_spec"] = RuleSetSpec(**spec)
    raw["policy"] = SolverPolicy(**raw["policy"])
    if raw.get("delivery_timing") is not None:
        timing = raw["delivery_timing"]
        raw["delivery_timing"] = DeliveryTimingInput(**{
            **timing,
            "orders": tuple(OrderTimingInput(**{**item, "duration_hours": Decimal(item["duration_hours"])}) for item in timing["orders"]),
            "virtual_hours_per_tonne": {key: Decimal(value) for key, value in timing["virtual_hours_per_tonne"].items()},
        })
    request = SchedulingRequest(**raw)
    if fingerprint_public_request(request) != data["request_fingerprint"]:
        raise ValueError("bound request fingerprint mismatch")
    if not (
        fingerprint_rule_set_spec(request.rule_set_spec)
        == request.rule_set_spec.fingerprint == data["rule_set_fingerprint"]
    ):
        raise ValueError("bound rule set fingerprint mismatch")
    return request


def _counters(state, context):
    return {
        "candidate_check_count": context.factory.budget.candidate_check_count,
        "complete_candidate_evaluation_count": context.complete_candidate_evaluation_count,
        "accepted_move_count": state.accepted_move_count,
    }


def _snapshot(state, context):
    cache = context.factory.cache
    stop = context.factory.budget.stop_reason
    return _json_values({
        **_counters(state, context),
        "virtual_sequence": state.virtual_sequence,
        "problem_fingerprint": cache.problem.input_fingerprint,
        "plan": state.current_plan,
        "plan_fingerprint": fingerprint(state.current_plan),
        "quality": state.current_evaluation.quality_key,
        "evaluation": state.current_evaluation,
        "evaluation_fingerprint": fingerprint(state.current_evaluation),
        "trace": context.accepted_move_traces,
        "trace_fingerprint": fingerprint(context.accepted_move_traces),
        "stop_reason": None if stop is None else stop.value,
        "edge_cache": {
            "hits": cache.hit_count, "misses": cache.miss_count, "entries": cache.entry_count,
        },
        "numeric_layout": {
            "view_build_count": context.numeric_view_build_count,
            "prepare_seconds": Decimal(str(context.numeric_prepare_seconds)),
            "peak_row_count": context.numeric_peak_row_count,
        },
    })


class FirstSearchComplete(BaseException):
    """Diagnostic escape before split/replay/audits; never a production result."""


def measure(request, *, scope="first", profile=None, result_observer=None):
    if scope not in {"first", "full"}:
        raise ValueError("scope must be first or full")
    report, functions, observed = {}, [], []
    original_search = solver.run_local_search
    original_refinement = solver.run_width_optimization

    def refinement(state, context):
        report["pre_refinement"] = _snapshot(state, context)
        return original_refinement(state, context)

    def timed(name, original):
        def wrapped(state, context):
            before = _counters(state, context)
            started, cpu_started = perf_counter(), process_time()
            try:
                return original(state, context)
            finally:
                functions.append({
                    "function": name,
                    "wall_seconds": Decimal(str(perf_counter() - started)),
                    "cpu_seconds": Decimal(str(process_time() - cpu_started)),
                    **{key: value - before[key] for key, value in _counters(state, context).items()},
                })
        return wrapped

    def first_search(state, context):
        # Snapshot construction is outside the measured first-search interval.
        initial = _snapshot(state, context)
        observed.append((state, context))
        started, cpu_started = perf_counter(), process_time()
        if profile is not None:
            profile.enable()
        try:
            with ExitStack() as stack:
                for name in (
                    "improve_whole_chain", "improve_real_node_relocation",
                    "improve_virtual_weight_fill", "improve_chain_order",
                ):
                    stack.enter_context(patch.object(
                        neighborhoods, name, timed(name, getattr(neighborhoods, name)),
                    ))
                original_search(state, context)
        finally:
            if profile is not None:
                profile.disable()
            report["first_search_timing"] = {
                "wall_seconds": Decimal(str(perf_counter() - started)),
                "cpu_seconds": Decimal(str(process_time() - cpu_started)),
                "functions": functions,
            }
        report["first_search"] = {"initial": initial, "final": _snapshot(state, context)}
        if scope == "first":
            raise FirstSearchComplete

    started, cpu_started = perf_counter(), process_time()
    with patch.object(solver, "run_local_search", first_search), \
         patch.object(solver, "run_width_optimization", refinement):
        try:
            result = solve_request(request)
        except FirstSearchComplete:
            if scope != "first":
                raise
        else:
            report["result"] = _json_values(result)
            if scope == "first":
                raise RuntimeError(f"first local search not reached: {result.status.value}")
            if observed:
                report["final_search"] = _snapshot(*observed[0])
    report["public_call_wall_seconds"] = Decimal(str(perf_counter() - started))
    report["public_call_cpu_seconds"] = Decimal(str(process_time() - cpu_started))
    if result_observer is not None and "result" in report:
        result_observer(result)
    report["measurement_scope"] = (
        "first_local_search_only_no_release" if scope == "first" else "full_public_solve_with_observation"
    )
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-request", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--scope", choices=("check", "first", "full"), default="first")
    parser.add_argument("--profile", action="store_true", help="Profile first local search only")
    parser.add_argument("--total-time-limit-seconds", type=Decimal)
    args = parser.parse_args(argv)
    prepared_bytes = args.prepared_request.read_bytes()
    request = load_request(prepared_bytes)
    original_fingerprint = fingerprint_public_request(request)
    original_policy = request.policy
    if args.total_time_limit_seconds is not None:
        request = replace(request, policy=replace(
            request.policy, total_time_limit_seconds=args.total_time_limit_seconds,
        ))
    # Fail rather than overwrite any previous or unrelated run.
    args.output_dir.mkdir(parents=True, exist_ok=False)
    with (args.output_dir / "prepared_request.json").open("xb") as stream:
        stream.write(prepared_bytes)
    identity = {
        "source": _code_identity(ROOT),
        "tool_sha256": _sha256(Path(__file__)),
        "prepared_request_sha256": _sha256(args.output_dir / "prepared_request.json"),
        "original_request_fingerprint": original_fingerprint,
        "measured_request_fingerprint": fingerprint_public_request(request),
        "original_policy": _json_values(original_policy),
        "measured_policy": _json_values(request.policy),
        "platform": platform.platform(), "python": platform.python_version(),
        "profile_enabled": args.profile,
        "requested_scope": args.scope,
    }
    _write_json(args.output_dir / "input_identity.json", identity)
    if args.scope == "check":
        return 0
    profile = cProfile.Profile() if args.profile else None
    try:
        report = measure(request, scope=args.scope, profile=profile)
        _write_json(args.output_dir / "measurement.json", report)
    except Exception as error:
        _write_json(args.output_dir / "error.json", {
            "type": type(error).__name__, "message": str(error), "status": "failed_measurement",
        })
        raise
    finally:
        if profile is not None:
            profile.dump_stats(str(args.output_dir / "first_search.pstats"))
            with (args.output_dir / "profile_functions.txt").open("x", encoding="utf-8") as stream:
                stats = pstats.Stats(profile, stream=stream)
                stats.sort_stats("cumulative").print_stats(60)
                stats.sort_stats("tottime").print_stats(40)
    print(f"Completed {report['measurement_scope']}: {args.output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
