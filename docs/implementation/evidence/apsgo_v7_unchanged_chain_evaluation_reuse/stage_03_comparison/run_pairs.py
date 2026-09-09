"""Run five alternating full-solve pairs for the approved unchanged-chain reuse."""

import argparse
import copy
import os
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

OLD_COMMIT = "1bc5bf322f8cdf34c20d947af0bacb36d954c818"
OLD_SOURCE_SHA256 = "7cd98513e1f1d0b1ea15b62b59a7fc25ae1644b40ef5576ac712f6d7c511c392"
TOOL_SHA256 = "822239430ee9c06a4d0db87d5da9ea039b219b660737089fe819292ab08cc581"
PREPARED_SHA256 = "11bdd096717414a25b14e7a219b860e6e84953f17e9c02bbf8e298da9266829c"
REQUEST_FINGERPRINT = "6609ba5853a20e3bcab137e57d51b5504659c4e7c2ea9dfc333545d13ca85a3f"
CHANGED_PRODUCTION_FILES = {
    "src/apsgo_scheduler/core/evaluation.py",
    "src/apsgo_scheduler/core/neighborhoods.py",
}
TIMING_KEYS = (
    "first_wall_seconds",
    "first_cpu_seconds",
    "full_wall_seconds",
    "full_cpu_seconds",
)


def stable_measurement(measurement):
    """Ignore exact timing values only; retain their locations and all business data."""
    stable = copy.deepcopy(measurement)
    for key in ("public_call_wall_seconds", "public_call_cpu_seconds"):
        if key in stable:
            stable[key] = None
    first_timing = stable["first_search_timing"]
    for item in (first_timing, *first_timing["functions"]):
        for key in ("wall_seconds", "cpu_seconds"):
            if key in item:
                item[key] = None
    durations = stable["result"]["run_manifest"]["stage_duration_seconds"]
    for key in durations:
        durations[key] = None
    return stable


def require_fixed_work(measurement):
    if measurement["measurement_scope"] != "full_public_solve_with_observation":
        raise ValueError("measurement is not a full observed public solve")
    for snapshot, expected, stop in (
        (measurement["first_search"]["final"], (64226, 2263, 34), "local_search_complete"),
        (measurement["final_search"], (200000, 3697, 54), "candidate_limit_reached"),
    ):
        counts = tuple(
            snapshot[key]
            for key in (
                "candidate_check_count",
                "complete_candidate_evaluation_count",
                "accepted_move_count",
            )
        )
        if counts != expected or snapshot["stop_reason"] != stop:
            raise ValueError("sample did not complete the frozen work prefix")
    result = measurement["result"]
    if not (
        result["stop_reason"] == "candidate_limit_reached"
        and result["release"] is not None
        and result["core_audit"]["passed"] is True
        and result["audit_report"]["passed"] is True
    ):
        raise ValueError("full sample did not retain a release with both audits passing")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("old-root", "new-root", "prepared-request", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    for name in ("old_root", "new_root", "prepared_request", "output"):
        setattr(args, name, getattr(args, name).absolute())
    if sys.version_info[:3] != (3, 10, 18):
        raise ValueError("run this cohort with the approved Python 3.10.18 interpreter")
    sys.path[:0] = [str(args.new_root), str(args.new_root / "src"), str(args.new_root / "tools")]
    from tools.compare_solver_stages import first_difference
    from tools.profile_solver_search import load_request
    from tools.verify_solver_diagnostics import _code_identity, _read_json, _sha256, _write_json

    if _sha256(args.prepared_request) != PREPARED_SHA256:
        raise ValueError("prepared request differs from the stage 0 frozen bytes")
    request = load_request(args.prepared_request)
    policy = request.policy
    if (
        policy.total_time_limit_seconds,
        policy.finalization_reserve_seconds,
        policy.candidate_check_limit,
        policy.seed,
    ) != (Decimal(900), Decimal(10), 200000, 590531):
        raise ValueError("frozen policy must remain 900/10/200000 with seed 590531")
    roots = {"old": args.old_root, "new": args.new_root}
    sources = {version: _code_identity(root) for version, root in roots.items()}
    if sources["old"]["production_source_sha256"] != OLD_SOURCE_SHA256:
        raise ValueError("old production source differs from the stage 0 baseline")
    old_files, new_files = (sources[version]["production_source_files"] for version in roots)
    changed = {
        name
        for name in old_files.keys() | new_files.keys()
        if old_files.get(name) != new_files.get(name)
    }
    if changed != CHANGED_PRODUCTION_FILES:
        raise ValueError("production changes must be exactly evaluation.py and neighborhoods.py")
    for root in roots.values():
        if _sha256(root / "tools/profile_solver_search.py") != TOOL_SHA256:
            raise ValueError("both versions must use the committed stage 0 measurement tool")
    args.output.mkdir(parents=True, exist_ok=False)
    _write_json(args.output / "expected_sources.json", sources)
    _write_json(
        args.output / "cohort.json",
        {
            "old_commit": OLD_COMMIT,
            "prepared_request_sha256": PREPARED_SHA256,
            "tool_sha256": TOOL_SHA256,
            "python_executable": sys.executable,
            "scope": "five full fixed-work pairs; not formal performance acceptance",
            "source_binding": "archive commit/tree identities are retained in the parent export manifest",
        },
    )
    samples, reference, reference_identity = [], None, None
    for pair in range(1, 6):
        for version in ("old", "new") if pair % 2 else ("new", "old"):
            name = f"pair_{pair:02d}_{version}"
            run = args.output / name
            command = [
                sys.executable,
                str(roots[version] / "tools/profile_solver_search.py"),
                "--prepared-request",
                str(args.prepared_request),
                "--output-dir",
                str(run),
                "--scope",
                "full",
            ]
            record = {
                "pair": pair,
                "version": version,
                "command": command,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "returncode": None,
            }
            _write_json(args.output / (name + "_started.json"), record)
            print(f"Starting {name} at {record['started_at']}", flush=True)
            try:
                with (args.output / (name + ".log")).open("x", encoding="utf-8") as stream:
                    process = subprocess.run(
                        command,
                        cwd=roots[version],
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                        check=False,
                    )
                record["returncode"] = process.returncode
                if process.returncode:
                    raise RuntimeError(f"measurement subprocess exited {process.returncode}")
                measurement = _read_json(run / "measurement.json")
                identity = _read_json(run / "input_identity.json")
                require_fixed_work(measurement)
                identity_keys = (
                    "prepared_request_sha256",
                    "original_request_fingerprint",
                    "measured_request_fingerprint",
                    "original_policy",
                    "measured_policy",
                    "platform",
                    "python",
                    "tool_sha256",
                    "profile_enabled",
                    "requested_scope",
                )
                if not (
                    identity["prepared_request_sha256"] == PREPARED_SHA256
                    and identity["original_request_fingerprint"] == REQUEST_FINGERPRINT
                    and identity["measured_request_fingerprint"] == REQUEST_FINGERPRINT
                    and identity["original_policy"] == identity["measured_policy"]
                    and identity["profile_enabled"] is False
                    and identity["requested_scope"] == "full"
                    and identity["tool_sha256"] == TOOL_SHA256
                    and identity["source"]["production_source_files"]
                    == sources[version]["production_source_files"]
                    and _code_identity(roots[version])["production_source_files"]
                    == sources[version]["production_source_files"]
                    and _sha256(roots[version] / "tools/profile_solver_search.py") == TOOL_SHA256
                    and _sha256(args.prepared_request) == PREPARED_SHA256
                ):
                    raise ValueError("sample source, tool, input or measurement mode changed")
                stable = stable_measurement(measurement)
                stable_identity = {key: identity[key] for key in identity_keys}
                if reference is None:
                    reference, reference_identity = stable, stable_identity
                record["first_difference"] = first_difference(
                    reference_identity, stable_identity, "input_identity"
                ) or first_difference(reference, stable, "measurement")
                record.update(
                    first_wall_seconds=measurement["first_search_timing"]["wall_seconds"],
                    first_cpu_seconds=measurement["first_search_timing"]["cpu_seconds"],
                    full_wall_seconds=measurement["public_call_wall_seconds"],
                    full_cpu_seconds=measurement["public_call_cpu_seconds"],
                    stage_duration_seconds=measurement["result"]["run_manifest"][
                        "stage_duration_seconds"
                    ],
                    stop_reason=measurement["result"]["stop_reason"],
                    source_sha256=identity["source"]["production_source_sha256"],
                    measurement_sha256=_sha256(run / "measurement.json"),
                    input_identity_sha256=_sha256(run / "input_identity.json"),
                )
                if record["first_difference"] is not None:
                    raise ValueError("ordered stable measurement differs from the first old sample")
                record["status"] = "pass"
            except Exception as error:
                record.update(
                    status="failed", error={"type": type(error).__name__, "message": str(error)}
                )
            finally:
                record["finished_at"] = datetime.now(timezone.utc).isoformat()
                _write_json(args.output / (name + "_summary.json"), record)
            samples.append(record)
            if record["status"] != "pass":
                raise RuntimeError(f"pair failed; all attempts retained: {name}")
            print(
                f"Completed {name}: first={record['first_wall_seconds']} full={record['full_wall_seconds']}",
                flush=True,
            )
    medians = {
        version: {
            key: statistics.median(item[key] for item in samples if item["version"] == version)
            for key in TIMING_KEYS
        }
        for version in roots
    }
    report = {
        "samples": samples,
        "medians": medians,
        "status": "pass",
        "reduction_percent": {
            key: (Decimal(1) - medians["new"][key] / medians["old"][key]) * 100
            for key in TIMING_KEYS
        },
    }
    _write_json(args.output / "summary.json", report)
    print(f"Completed all five full pairs: {args.output}", flush=True)


if __name__ == "__main__":
    main()
