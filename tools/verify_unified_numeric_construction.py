"""Run only construction from a selected checkout; never enter local search."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter, process_time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--prepared-request", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--through-initial", action="store_true")
    args = parser.parse_args()
    root = args.source_root.resolve()
    sys.path[:0] = [str(root / "src"), str(root)]
    from apsgo_scheduler.app.input_normalizer import normalize_input
    from apsgo_scheduler.app.rule_set_loader import load_rule_set
    from apsgo_scheduler.core._numeric_state import NumericTask
    from apsgo_scheduler.core._numeric_rules import NumericRuleProgram
    from apsgo_scheduler.core._numeric_evaluation import NumericQualityProgram
    from apsgo_scheduler.core._numeric_construction import (
        build_numeric_construction_graph, numeric_minimum_path_cover, construct_numeric_initial_plan,
    )
    from apsgo_scheduler.core.budget import SolveRuntimeBudget
    from tools.profile_numeric_solver import derive_numeric_request
    from tools.profile_solver_search import load_request
    from tools.verify_solver_diagnostics import _write_json

    args.output_dir.mkdir(parents=True, exist_ok=False)
    request = derive_numeric_request(load_request(args.prepared_request))
    rule_set = load_rule_set(request.rule_set_spec)
    task = NumericTask.build(normalize_input(request, rule_set), rule_set, request.delivery_timing)
    program = NumericRuleProgram.compile(task, rule_set)
    quality = NumericQualityProgram.compile(task, program, rule_set)
    runtime = SolveRuntimeBudget.from_policy(request.policy, perf_counter(), clock=perf_counter)
    started, cpu = perf_counter(), process_time()
    graph = build_numeric_construction_graph(task, program, runtime, seed=request.policy.seed)
    if not graph.complete:
        raise AssertionError(("graph", graph.stop_reason))
    cover = numeric_minimum_path_cover(graph, runtime)
    if not cover.complete:
        raise AssertionError(("cover", cover.stop_reason))
    data = dict(task=task.fingerprint, program=program.fingerprint, quality_program=quality.fingerprint,
        graph=dict(ordered_rows=graph.ordered_rows.tolist(), offsets=graph.adjacency_offsets.tolist(),
            rows=graph.adjacency_rows.tolist(), checked=graph.checked_edge_count, allowed=graph.allowed_edge_count,
            direction_rejected=graph.direction_rejected_edge_count, fingerprint=graph.fingerprint),
        cover=dict(matching=cover.matching_successor.tolist(), offsets=cover.path_offsets.tolist(),
            rows=cover.path_rows.tolist(), fingerprint=cover.fingerprint))
    if args.through_initial:
        initial = construct_numeric_initial_plan(task, program, quality, graph, cover, runtime)
        if not initial.complete:
            raise AssertionError(("initial", initial.stop_reason))
        plan = initial.plan
        data["initial"] = dict(rows=plan.node_rows.tolist(), offsets=plan.chain_offsets.tolist(),
            ids=plan.chain_ids.tolist(), periods=plan.chain_periods.tolist(), fingerprint=initial.fingerprint,
            plan_fingerprint=plan.fingerprint, quality=initial.evaluation.quality_key.tolist())
    if runtime.candidate_check_count != 0:
        raise AssertionError("construction consumed candidate budget")
    _write_json(args.output_dir / "construction.json", data)
    _write_json(args.output_dir / "identity.json", dict(source_root=str(root),
        input_sha256=hashlib.sha256(args.prepared_request.read_bytes()).hexdigest(),
        sources={str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted((root / "src/apsgo_scheduler/core").glob("_numeric*.py"))},
        wall_seconds=str(perf_counter() - started), cpu_seconds=str(process_time() - cpu),
        mode="cold_diagnostic_not_performance", candidate_checks=runtime.candidate_check_count))
    print(json.dumps(dict(checked=graph.checked_edge_count, allowed=graph.allowed_edge_count,
        paths=cover.path_count, initial=args.through_initial)))


if __name__ == "__main__":
    main()
