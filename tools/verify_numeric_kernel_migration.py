"""Replay the real prefix, inspect its actual refinement window, never restore state."""
import argparse
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from apsgo_scheduler.app.service import solve_request
from apsgo_scheduler.core import _numeric_refinement as refinement
from apsgo_scheduler.core._numeric_evaluation import evaluate_numeric_overlay_candidate
from apsgo_scheduler.core.contracts import fingerprint
from tests.core import numeric_migration_reference_evaluation as reference
from tests.core.test_numeric_incremental_evaluation import _assert_same
from tools.profile_numeric_solver import derive_numeric_request
from tools.profile_solver_search import load_request
from tools.verify_solver_diagnostics import _write_json, _code_identity, _sha256


def compare_runs(reference_run, candidate_run):
    def read(path):
        return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    left = read(reference_run / "response.json")
    right = read(candidate_run / "response.json")
    # The complete public result includes all ordered nodes, trace identities,
    # resources and both audits. Only measured durations may differ.
    for result in (left, right):
        result["run_manifest"].pop("stage_duration_seconds")
    if left != right:
        from tools.verify_numeric_search import first_difference
        raise AssertionError(first_difference(left, right))
    for name in ("prepared_request.json", "delivery_report.json"):
        if (reference_run / name).read_bytes() != (candidate_run / name).read_bytes():
            raise AssertionError(f"{name} differs")
    return {"equal": True, "reference": str(reference_run), "candidate": str(candidate_run),
            "reference_measurement": read(reference_run / "measurement.json"),
            "candidate_measurement": read(candidate_run / "measurement.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-request", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--count", type=int, default=256)
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--reference-run", type=Path)
    parser.add_argument("--compare-run", type=Path, action="append")
    args = parser.parse_args()
    if args.reference_run or args.compare_run:
        if not args.reference_run or not args.compare_run:
            parser.error("both --reference-run and --compare-run are required")
        comparisons = [compare_runs(args.reference_run, run) for run in args.compare_run]
        args.output_dir.mkdir(parents=True, exist_ok=False)
        _write_json(args.output_dir / "comparison.json", comparisons)
        print(f"matched {len(comparisons)} complete runs, excluding measured durations only")
        return
    if args.prepared_request is None:
        parser.error("--prepared-request is required for a candidate window")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    request = derive_numeric_request(load_request(args.prepared_request))
    request = replace(request, policy=replace(request.policy, candidate_check_limit=150000))
    samples = []
    original = refinement._try_overlay_candidate

    def inspect(state, budget, edit, task, rules, quality, overlay, *a, **kw):
        if len(samples) < args.count:
            inputs = (task, rules, quality, overlay, state.task, state.program,
                      state.quality, state.plan, state.evaluation)
            expected = reference.evaluate_numeric_overlay_candidate(*inputs)
            actual = evaluate_numeric_overlay_candidate(*inputs)
            _assert_same(actual, expected)
            if args.native:
                import numpy as np
                from apsgo_scheduler.core._numeric_state import NumericPlan
                from tests.core.test_numeric_kernel_migration import assert_kernel_matches
                lengths = np.array([len(chain) for chain in overlay.chains], dtype=np.int64)
                plan = NumericPlan.build(
                    task, np.concatenate(overlay.chains), np.r_[0, np.cumsum(lengths)],
                    overlay.chain_ids, overlay.chain_periods, generation=overlay.generation,
                )
                assert_kernel_matches(task, rules, quality, plan)
            samples.append({
                "logical_candidate": budget.candidate_check_count,
                "generation": state.plan.generation,
                "task": task.fingerprint, "rules": rules.fingerprint,
                "previous_plan": state.plan.fingerprint,
                "virtual_sequence": state.virtual_sequence,
                "split_sequence": state.split_sequence,
                "edit": fingerprint(edit),
                "candidate": fingerprint(tuple(tuple(map(int, chain)) for chain in overlay.chains)),
                "quality": list(map(int, expected.quality_key)),
            })
        return original(state, budget, edit, task, rules, quality, overlay, *a, **kw)

    with patch.object(refinement, "_try_overlay_candidate", inspect):
        result = solve_request(request)
    _write_json(args.output_dir / "window.json", {
        "source": _code_identity(ROOT), "input_sha256": _sha256(args.prepared_request),
        "samples": samples, "count": len(samples),
        "result": result.result_fingerprint, "status": result.status.value,
        "stop_reason": result.stop_reason.value,
    })
    if len(samples) != args.count:
        raise RuntimeError(f"captured {len(samples)} of {args.count}; inspect run before continuing")
    print(f"matched {len(samples)} actual refinement candidates")


if __name__ == "__main__":
    main()
