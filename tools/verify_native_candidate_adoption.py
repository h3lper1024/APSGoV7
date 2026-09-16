"""Measure actual ordered serial coordination before adopting a native batch."""

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import platform
import statistics
import sys
from time import perf_counter, process_time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from apsgo_scheduler.core import _numeric_refinement as refinement
from tools.profile_numeric_solver import derive_numeric_request, _run_once
from tools.profile_solver_search import load_request
from tools.verify_numeric_kernel_migration import compare_runs
from tools.verify_numeric_serial_batch import record_json
from tools.verify_solver_diagnostics import _code_identity, _sha256


def run(request, destination, mode, window_checks):
    original_pool = refinement.NumericCandidateBatchWorkspace
    original_many = original_pool.prepare_many
    original_refinement = refinement.improve_numeric_refinement
    stages, calls = [], 0

    def pool(*args, **kwargs):
        kwargs["_native_executor"] = "serial" if mode == "native_serial" else None
        return original_pool(*args, **kwargs)

    def many(current, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original_many(current, *args, **kwargs)

    def refine(state, budget, *args, **kwargs):
        entry = state.plan.fingerprint
        before = budget.candidate_check_count
        original_limit = budget.candidate_check_limit
        if window_checks:
            budget.candidate_check_limit = min(budget.candidate_check_limit, before + window_checks)
        wall, cpu = perf_counter(), process_time()
        try:
            result = original_refinement(state, budget, *args, **kwargs)
        finally:
            # The artificial window must not change the policy checked by final audit.
            budget.candidate_check_limit = original_limit
        stages.append({"entry_plan": entry, "exit_plan": state.plan.fingerprint,
            "entry_checks": before, "exit_checks": budget.candidate_check_count,
            "wall_seconds": perf_counter() - wall, "cpu_seconds": process_time() - cpu})
        return result

    with patch.object(refinement, "NumericCandidateBatchWorkspace", pool), \
         patch.object(original_pool, "prepare_many", many), \
         patch.object(refinement, "improve_numeric_refinement", refine):
        measurement = _run_once(request, destination)
    if len(stages) != 1 or not measurement["core_audit"]["passed"] or not measurement["application_audit"]["passed"]:
        raise AssertionError("one actual refinement and both audits are required")
    if window_checks and stages[0]["exit_checks"] - stages[0]["entry_checks"] != window_checks:
        raise AssertionError("the requested logical window was not completed")
    if mode == "native_serial" and not calls:
        raise AssertionError("native batch was not executed")
    record = {"mode": mode, "window_checks": window_checks, "stages": stages,
        "native_batch_calls": calls, "measurement": {
            "wall_seconds": float(measurement["wall_seconds"]),
            "cpu_seconds": float(measurement["cpu_seconds"]),
            "peak_rss_bytes": measurement["peak_rss_bytes"]}}
    record_json(destination / "coordination.json", record)
    print(json.dumps({"directory": str(destination), "mode": mode,
        "wall_seconds": str(measurement["wall_seconds"]), "stage_seconds": stages[0]["wall_seconds"]}), flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-request", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--window-checks", type=int, default=20000)
    args = parser.parse_args()
    if args.window_checks < 0:
        parser.error("window checks must be nonnegative; zero means a complete solve")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    request = derive_numeric_request(load_request(args.prepared_request))
    request = replace(request, policy=replace(request.policy, candidate_check_limit=400000))
    record_json(args.output_dir / "identity.json", {"source": _code_identity(ROOT),
        "input_sha256": _sha256(args.prepared_request), "tool_sha256": _sha256(Path(__file__)),
        "environment": {"platform": platform.platform(), "python": sys.version,
            "logical_cpus": os.cpu_count(), "initial_load": os.getloadavg()},
        "scope": "public service including final audits; normal prefix before each window; no state restoration",
        "window_checks": args.window_checks, "hot_repeats": 3})
    modes = ("single", "native_serial")
    samples, reference = {mode: [] for mode in modes}, None
    for round_index in range(4):
        order = modes if round_index % 2 == 0 else modes[::-1]
        for mode in order:
            name = "warmup" if not round_index else f"hot_{round_index:02d}"
            destination = args.output_dir / f"{mode}_{name}"
            record = run(request, destination, mode, args.window_checks)
            if reference is None:
                reference = destination
            else:
                compare_runs(reference, destination)
            if round_index:
                samples[mode].append(record)
    results = {}
    for mode, values in samples.items():
        results[mode] = {"samples": values,
            "median_stage_seconds": statistics.median(v["stages"][0]["wall_seconds"] for v in values),
            "median_service_seconds": statistics.median(float(v["measurement"]["wall_seconds"]) for v in values)}
    record_json(args.output_dir / "comparison.json", {"all_complete_results_equal": True,
        "exclusion": "measured run_manifest.stage_duration_seconds only", "results": results,
        "stage_ranges_separated": max(v["stages"][0]["wall_seconds"] for v in samples["native_serial"])
            < min(v["stages"][0]["wall_seconds"] for v in samples["single"])})
    print(json.dumps({mode: {k: v for k, v in result.items() if k != "samples"}
        for mode, result in results.items()}), flush=True)


if __name__ == "__main__":
    main()
