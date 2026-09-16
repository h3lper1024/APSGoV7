"""Compare single and batched kernels after the real prefix, with a bounded window."""
import argparse
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sys
from time import perf_counter, process_time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import numpy as np
from apsgo_scheduler.core import _numeric_refinement as refinement
from apsgo_scheduler.core._numeric_evaluation import evaluate_numeric_view
from apsgo_scheduler.core._numeric_chain_ops import chain_rows
from apsgo_scheduler.core._numeric_search import NumericDeferredCandidateFailure
from apsgo_scheduler.core.contracts import fingerprint
from tools.profile_numeric_solver import derive_numeric_request, _run_once
from tools.profile_solver_search import load_request
from tools.verify_numeric_kernel_migration import compare_runs
from tools.verify_solver_diagnostics import _code_identity, _sha256, _write_json


def record_json(path, value):
    _write_json(path, json.loads(json.dumps(value), parse_float=Decimal))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-request", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--window-checks", type=int, default=20000)
    args = parser.parse_args()
    if args.window_checks <= 0:
        parser.error("positive window required")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    request = derive_numeric_request(load_request(args.prepared_request))
    request = replace(request, policy=replace(request.policy, candidate_check_limit=400000))
    improve, attempt = refinement.improve_numeric_refinement, refinement.consume_candidate_result
    windows, trace = [], []
    for batch_size in (1, 8):
        record, samples = {}, []

        def inspect(state, budget, workspace, result):
            if isinstance(result, NumericDeferredCandidateFailure) or result.summary is None:
                return attempt(state, budget, workspace, result)
            supplied = result.summary
            expected = evaluate_numeric_view(workspace, state.program, state.quality,
                previous_evaluation=state.evaluation)
            for name in expected._fields:
                np.testing.assert_array_equal(getattr(supplied, name), getattr(expected, name), err_msg=name)
            sample = {
                "logical_check": budget.candidate_check_count,
                "descriptor": result.descriptor.tolist(), "generation": state.plan.generation,
                "candidate": fingerprint(tuple(tuple(map(int, chain_rows(result.view, i)))
                                               for i in range(result.view.count))),
                "task": state.task.fingerprint, "virtual_sequence_before": state.virtual_sequence,
                "split_sequence_before": state.split_sequence,
            }
            accepted = attempt(state, budget, workspace, result)
            sample.update(accepted=accepted, plan_after=state.plan.fingerprint,
                          virtual_sequence_after=state.virtual_sequence, split_sequence_after=state.split_sequence)
            samples.append(sample)
            return accepted

        def window(state, budget, **kwargs):
            if record:
                raise AssertionError("unexpected second refinement entrance")
            entry = budget.candidate_check_count
            original_limit = budget.candidate_check_limit
            ceiling = min(original_limit, entry + args.window_checks)
            record.update(
                batch_size=batch_size, entry_check=entry, entry_plan=state.plan.fingerprint,
                original_limit=original_limit, diagnostic_window_ceiling=ceiling,
                warmup_seconds=0.0,
            )
            # Diagnostic ceiling is additive to the real prefix, never a zero reset.
            # No production restore interface, file configuration or policy rewrite.
            budget.candidate_check_limit = ceiling
            started, cpu = perf_counter(), process_time()
            from apsgo_scheduler.core._numeric_state import NumericCandidateWorkspace
            allocate = NumericCandidateWorkspace.allocate.__func__
            allocated_count, allocated_bytes = 0, 0
            def measured_allocate(cls, *args, **options):
                nonlocal allocated_count, allocated_bytes
                workspace = allocate(cls, *args, **options)
                allocated_count += 1
                allocated_bytes += workspace.allocated_bytes
                return workspace
            try:
                with patch.object(NumericCandidateWorkspace, "allocate", classmethod(measured_allocate)):
                    result = improve(state, budget, **kwargs, _batch_size=batch_size)
            finally:
                budget.candidate_check_limit = original_limit
            record.update(
                wall_seconds=perf_counter() - started, cpu_seconds=process_time() - cpu,
                exit_check=budget.candidate_check_count, exit_plan=state.plan.fingerprint,
                quality=list(map(int, state.evaluation.quality_key)),
                diagnostics=kwargs["diagnostics"].snapshot(),
                workspace_allocations=allocated_count,
                workspace_allocated_bytes_total=allocated_bytes,
            )
            if budget.candidate_check_count - entry != args.window_checks:
                raise AssertionError("window did not consume the requested logical checks")
            return result

        with patch.object(refinement, "improve_numeric_refinement", window), patch.object(
            refinement, "consume_candidate_result", inspect
        ):
            _run_once(request, args.output_dir / f"batch_{batch_size}")
        record_json(args.output_dir / f"trace_{batch_size}.json", samples)
        record_json(args.output_dir / f"window_{batch_size}.json", record)
        windows.append(record)
        trace.append(samples)
        print(f"batch={batch_size}, prefix={record['entry_check']}, window={args.window_checks}, "
              f"evaluations={len(samples)}, coordinated_batches={record['diagnostics']['numeric_batch_calls']}", flush=True)
    if trace[0] != trace[1] or windows[0]["entry_plan"] != windows[1]["entry_plan"]:
        raise AssertionError("logical trace or real refinement entrance differs")
    if windows[1]["diagnostics"]["numeric_batch_calls"] <= 0:
        raise AssertionError("bounded preparation path was not exercised")
    if not trace[0]:
        raise AssertionError("no shared candidate summaries were observed")
    comparison = compare_runs(args.output_dir / "batch_1", args.output_dir / "batch_8")
    _write_json(args.output_dir / "comparison.json", {
        "source": _code_identity(ROOT), "input_sha256": _sha256(args.prepared_request),
        "scope": "bounded correctness window; timings include per-candidate verification, not a performance benchmark",
        "windows": json.loads(json.dumps(windows), parse_float=Decimal),
        "trace_equal": True, "compared_candidates": len(trace[0]),
        "public_result": comparison,
    })
    print("all candidate summaries, ordered traces, resources, public results and audits match", flush=True)


if __name__ == "__main__":
    main()
