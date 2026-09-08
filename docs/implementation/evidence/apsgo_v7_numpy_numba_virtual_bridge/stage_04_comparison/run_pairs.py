"""Run five cold-process full-solve pairs for the approved numeric bridge comparison."""

import argparse
import hashlib
import os
import runpy
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from importlib.metadata import version as package_version
from pathlib import Path
from time import perf_counter

REFERENCE_SCRIPT = (
    Path(__file__).resolve().parents[5]
    / "docs/implementation/evidence/apsgo_v7_unchanged_chain_evaluation_reuse"
    / "stage_03_comparison/run_pairs.py"
)
REFERENCE_SHA256 = "1f34e154f745d80b05e0dc74ab774574ee4be243aab9f6ef854fa5de683d043d"
if hashlib.sha256(REFERENCE_SCRIPT.read_bytes()).hexdigest() != REFERENCE_SHA256:
    raise ValueError("the reused comparison helper differs from its frozen source")
REFERENCE = runpy.run_path(str(REFERENCE_SCRIPT))
require_fixed_work = REFERENCE["require_fixed_work"]
PREPARED_SHA256 = REFERENCE["PREPARED_SHA256"]
REQUEST_FINGERPRINT = REFERENCE["REQUEST_FINGERPRINT"]
TOOL_SHA256 = REFERENCE["TOOL_SHA256"]
OLD_COMMIT = "7fff99791ab22725401580685598b52fc1dec205"
OLD_SOURCE_SHA256 = "e11bdea69285f3c260353eb54560a6769bbe8f1198098a13d22a038aa0f77602"
DEPENDENCY_VERSIONS = {"numpy": "2.2.6", "numba": "0.65.1", "llvmlite": "0.47.0"}
CHANGED_PRODUCTION_FILES = {
    "src/apsgo_scheduler/core/_bridge_numeric.py",
    "src/apsgo_scheduler/core/virtual_material.py",
}
TIMING_KEYS = (*REFERENCE["TIMING_KEYS"], "process_wall_seconds")
CACHE_COUNT_PATHS = tuple(
    (*snapshot, "edge_cache", key)
    for snapshot in (("first_search", "initial"), ("first_search", "final"), ("final_search",))
    for key in ("hits", "misses", "entries")
)
IDENTITY_KEYS = (
    "prepared_request_sha256", "original_request_fingerprint", "measured_request_fingerprint",
    "original_policy", "measured_policy", "platform", "python", "tool_sha256", "profile_enabled",
    "requested_scope",
)
LOGGING_LAUNCHER = "\n".join((
    "import logging, runpy, sys",
    "logging.basicConfig(level=logging.INFO, "
    "format='%(asctime)s %(levelname)s %(name)s %(message)s')",
    "sys.argv = sys.argv[1:]",
    "runpy.run_path(sys.argv[0], run_name='__main__')",
))


def stable_measurement(measurement):
    """Add only nine exact scalar cache leaves to the original timing comparison mask."""
    stable = REFERENCE["stable_measurement"](measurement)
    for path in CACHE_COUNT_PATHS:
        parent = stable
        for key in path[:-1]:
            if not isinstance(parent, dict) or key not in parent:
                break
            parent = parent[key]
        else:
            if isinstance(parent, dict) and path[-1] in parent:
                value = parent[path[-1]]
                if type(value) is not int or value < 0:
                    raise ValueError("cache statistics must remain nonnegative integer leaves")
                parent[path[-1]] = None
    return stable


def require_identity(identity, expected_source):
    if not (
        identity["prepared_request_sha256"] == PREPARED_SHA256
        and identity["original_request_fingerprint"] == REQUEST_FINGERPRINT
        and identity["measured_request_fingerprint"] == REQUEST_FINGERPRINT
        and identity["original_policy"] == identity["measured_policy"]
        and identity["profile_enabled"] is False
        and identity["requested_scope"] == "full"
        and identity["tool_sha256"] == TOOL_SHA256
        and identity["source"]["production_source_files"] == expected_source["production_source_files"]
        and identity["source"]["production_source_sha256"] == expected_source["production_source_sha256"]
    ):
        raise ValueError("sample source, tool, input or measurement mode changed")
    return {key: identity[key] for key in IDENTITY_KEYS}


def dependency_versions():
    """Inspect distribution metadata without importing numerical modules or compiling."""
    actual = {name: package_version(name) for name in DEPENDENCY_VERSIONS}
    if actual != DEPENDENCY_VERSIONS:
        raise ValueError("installed numerical dependencies differ from the frozen environment")
    return actual


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("old-root", "new-root", "prepared-request", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    for name in ("old_root", "new_root", "prepared_request", "output"):
        setattr(args, name, getattr(args, name).absolute())
    if sys.version_info[:3] != (3, 10, 18):
        raise ValueError("run this cohort with the approved Python 3.10.18 interpreter")
    dependencies = dependency_versions()
    sys.path[:0] = [str(args.new_root), str(args.new_root / "src"), str(args.new_root / "tools")]
    from tools.compare_solver_stages import first_difference
    from tools.profile_solver_search import load_request
    from tools.verify_solver_diagnostics import _code_identity, _read_json, _sha256, _write_json

    if _sha256(args.prepared_request) != PREPARED_SHA256:
        raise ValueError("prepared request differs from the frozen 900-second input")
    request = load_request(args.prepared_request)
    policy = request.policy
    if (
        policy.total_time_limit_seconds, policy.finalization_reserve_seconds,
        policy.candidate_check_limit, policy.seed,
    ) != (Decimal(900), Decimal(10), 200000, 590531):
        raise ValueError("frozen policy must remain 900/10/200000 with seed 590531")
    roots = {"old": args.old_root, "new": args.new_root}
    sources = {version: _code_identity(root) for version, root in roots.items()}
    if sources["old"]["production_source_sha256"] != OLD_SOURCE_SHA256:
        raise ValueError("old production source differs from the stage 0 baseline")
    old_files, new_files = (sources[version]["production_source_files"] for version in roots)
    if {
        name for name in old_files.keys() | new_files.keys()
        if old_files.get(name) != new_files.get(name)
    } != CHANGED_PRODUCTION_FILES:
        raise ValueError("production changes must be exactly the two numeric bridge files")
    for root in roots.values():
        if _sha256(root / "tools/profile_solver_search.py") != TOOL_SHA256:
            raise ValueError("both versions must use the frozen observation tool")
    runner_sha256 = _sha256(Path(__file__))
    args.output.mkdir(parents=True, exist_ok=False)
    _write_json(args.output / "expected_sources.json", sources)
    _write_json(args.output / "cohort.json", {
        "old_commit": OLD_COMMIT, "prepared_request_sha256": PREPARED_SHA256,
        "tool_sha256": TOOL_SHA256, "runner_sha256": runner_sha256,
        "reference_helper_sha256": REFERENCE_SHA256, "python_executable": sys.executable,
        "dependency_versions": dependencies,
        "scope": "five cold full fixed-work pairs; not formal performance acceptance",
        "source_binding": "archive commit/tree identities remain in the parent export manifest",
        "additional_mask_paths": CACHE_COUNT_PATHS, "logging_launcher": LOGGING_LAUNCHER,
    })
    samples, reference, reference_identity = [], None, None
    for pair in range(1, 6):
        pair_reference, pair_identity, pair_names = None, None, []
        for version in ("old", "new") if pair % 2 else ("new", "old"):
            name = f"pair_{pair:02d}_{version}"
            run, log = args.output / name, args.output / (name + ".log")
            command = [
                sys.executable, "-c", LOGGING_LAUNCHER,
                str(roots[version] / "tools/profile_solver_search.py"),
                "--prepared-request", str(args.prepared_request), "--output-dir", str(run),
                "--scope", "full",
            ]
            record = {
                "pair": pair, "version": version, "command": command,
                "started_at": datetime.now(timezone.utc).isoformat(), "returncode": None,
            }
            _write_json(args.output / (name + "_started.json"), record)
            print(f"Starting {name} at {record['started_at']}", flush=True)
            try:
                if not (
                    _code_identity(roots[version])["production_source_files"]
                    == sources[version]["production_source_files"]
                    and _sha256(roots[version] / "tools/profile_solver_search.py") == TOOL_SHA256
                    and _sha256(args.prepared_request) == PREPARED_SHA256
                    and _sha256(REFERENCE_SCRIPT) == REFERENCE_SHA256
                    and _sha256(Path(__file__)) == runner_sha256
                    and dependency_versions() == dependencies
                ):
                    raise ValueError("frozen source, tool or input changed before the sample")
                with log.open("x", encoding="utf-8") as stream:
                    started = perf_counter()
                    try:
                        process = subprocess.run(
                            command, cwd=roots[version], stdout=stream, stderr=subprocess.STDOUT,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, check=False,
                        )
                    finally:
                        record["process_wall_seconds"] = Decimal(str(perf_counter() - started))
                record["returncode"] = process.returncode
                if process.returncode:
                    raise RuntimeError(f"measurement subprocess exited {process.returncode}")
                measurement = _read_json(run / "measurement.json")
                identity = _read_json(run / "input_identity.json")
                require_fixed_work(measurement)
                stable_identity = require_identity(identity, sources[version])
                if not (
                    _code_identity(roots[version])["production_source_files"]
                    == sources[version]["production_source_files"]
                    and _sha256(roots[version] / "tools/profile_solver_search.py") == TOOL_SHA256
                    and _sha256(args.prepared_request) == PREPARED_SHA256
                    and _sha256(REFERENCE_SCRIPT) == REFERENCE_SHA256
                    and _sha256(Path(__file__)) == runner_sha256
                    and dependency_versions() == dependencies
                ):
                    raise ValueError("frozen source, tool or input changed during the sample")
                stable = stable_measurement(measurement)
                if reference is None:
                    reference, reference_identity = stable, stable_identity
                if pair_reference is None:
                    pair_reference, pair_identity = stable, stable_identity
                record["first_difference"] = first_difference(
                    reference_identity, stable_identity, "input_identity"
                ) or first_difference(reference, stable, "measurement")
                record["pair_first_difference"] = first_difference(
                    pair_identity, stable_identity, "input_identity"
                ) or first_difference(pair_reference, stable, "measurement")
                record.update(
                    first_wall_seconds=measurement["first_search_timing"]["wall_seconds"],
                    first_cpu_seconds=measurement["first_search_timing"]["cpu_seconds"],
                    full_wall_seconds=measurement["public_call_wall_seconds"],
                    full_cpu_seconds=measurement["public_call_cpu_seconds"],
                    stage_duration_seconds=measurement["result"]["run_manifest"]["stage_duration_seconds"],
                    stop_reason=measurement["result"]["stop_reason"],
                    source_sha256=identity["source"]["production_source_sha256"],
                    measurement_sha256=_sha256(run / "measurement.json"),
                    input_identity_sha256=_sha256(run / "input_identity.json"), log_sha256=_sha256(log),
                )
                if record["first_difference"] is not None or record["pair_first_difference"] is not None:
                    raise ValueError("ordered business data differs from the reference or paired sample")
                record["status"] = "pass"
            except Exception as error:
                record.update(status="failed", error={"type": type(error).__name__, "message": str(error)})
            finally:
                record["finished_at"] = datetime.now(timezone.utc).isoformat()
                _write_json(args.output / (name + "_summary.json"), record)
            samples.append(record)
            pair_names.append(name)
            if record["status"] != "pass":
                _write_json(args.output / "summary.json", {"status": "failed", "samples": samples})
                raise RuntimeError(f"pair failed; all attempts retained: {name}")
            print(f"Completed {name}: process={record['process_wall_seconds']} full={record['full_wall_seconds']}", flush=True)
        _write_json(args.output / f"pair_{pair:02d}_comparison.json", {
            "pair": pair, "samples": pair_names, "first_difference": None, "status": "pass",
        })
    medians = {
        version: {
            key: statistics.median(item[key] for item in samples if item["version"] == version)
            for key in TIMING_KEYS
        }
        for version in roots
    }
    _write_json(args.output / "summary.json", {
        "status": "pass", "samples": samples, "medians": medians,
        "reduction_percent": {
            key: (Decimal(1) - medians["new"][key] / medians["old"][key]) * 100
            for key in TIMING_KEYS
        },
    })
    print(f"Completed all five cold full pairs: {args.output}", flush=True)


if __name__ == "__main__":
    main()
