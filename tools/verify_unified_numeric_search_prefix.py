"""Compare search prefixes and an optional bounded refinement window."""

import argparse
import cProfile
from dataclasses import fields
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import pstats
import sys
from time import perf_counter, process_time
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--prepared-request", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--through-split", action="store_true")
    parser.add_argument("--refinement-checks", type=int, default=0)
    parser.add_argument("--refinement-diagnostics", action="store_true")
    parser.add_argument("--profile-refinement", action="store_true")
    args = parser.parse_args()
    if args.refinement_checks < 0 or (args.refinement_checks and not args.through_split):
        parser.error("nonnegative refinement window requires --through-split")
    if args.profile_refinement and not args.refinement_checks:
        parser.error("--profile-refinement requires a positive refinement window")
    root = args.source_root.resolve()
    sys.path[:0] = [str(root / "src"), str(root)]
    from apsgo_scheduler.app.input_normalizer import normalize_input
    from apsgo_scheduler.app.rule_set_loader import load_rule_set
    from apsgo_scheduler.core._numeric_state import NumericTask
    from apsgo_scheduler.core._numeric_rules import NumericRuleProgram
    from apsgo_scheduler.core._numeric_evaluation import NumericQualityProgram
    from apsgo_scheduler.core._numeric_units import to_ticks
    from apsgo_scheduler.core._numeric_construction import (
        build_numeric_construction_graph, numeric_minimum_path_cover, construct_numeric_initial_plan,
    )
    from apsgo_scheduler.core import _numeric_search as search
    from apsgo_scheduler.core.budget import SolveRuntimeBudget
    from apsgo_scheduler.core.contracts import SearchStopReason
    from apsgo_v7_service.diagnostics import _json_values
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
    cover = numeric_minimum_path_cover(graph, runtime)
    initial = construct_numeric_initial_plan(task, program, quality, graph, cover, runtime)
    state = search.NumericSearchState.start(task, program, quality, initial)
    data = dict(initial=initial.fingerprint, phases=[], accepted=[])
    digest, quota_count = hashlib.sha256(), 0
    phase = "first_search"
    original_permit, original_commit = SolveRuntimeBudget.permit, search.NumericSearchState.commit

    def permit(budget, count=1):
        nonlocal quota_count
        result = original_permit(budget, count)
        if count:
            quota_count += 1
            record = (phase, budget.candidate_check_count, count, result,
                      None if budget.stop_reason is None else budget.stop_reason.value)
            digest.update(json.dumps(record, separators=(",", ":")).encode() + b"\n")
            if budget.candidate_check_count % 25000 == 0:
                print(json.dumps(dict(phase=phase, checks=budget.candidate_check_count)), flush=True)
        return result

    def commit(current, *values, **options):
        result = original_commit(current, *values, **options)
        move = current.accepted_moves[-1]
        data["accepted"].append(dict(phase=phase, task=current.task.fingerprint,
            plan=current.plan.fingerprint, move=_json_values({f.name: getattr(move, f.name) for f in fields(move)})))
        return result

    def snapshot():
        return dict(phase=phase, task=state.task.fingerprint, program=state.program.fingerprint,
            quality_program=state.quality.fingerprint, plan=state.plan.fingerprint,
            quality=state.evaluation.quality_key.tolist(), candidate_checks=runtime.candidate_check_count,
            complete_evaluations=state.complete_candidate_evaluation_count, accepted=state.accepted_move_count,
            bridge_required=state.bridge_required_candidate_count, virtual_sequence=state.virtual_sequence,
            split_sequence=state.split_sequence, replay_count=state.replay_count,
            stop_reason=None if runtime.stop_reason is None else runtime.stop_reason.value)

    slack = to_ticks(request.policy.whole_chain_pair_scan_slack_weight, task.units.weight, "slack")
    with patch.object(SolveRuntimeBudget, "permit", permit), patch.object(search.NumericSearchState, "commit", commit):
        for name in ("improve_numeric_whole_chain", "improve_numeric_real_node_relocation",
                     "improve_numeric_virtual_weight_fill", "improve_numeric_chain_order"):
            phase = name
            if not runtime.allows_search():
                raise AssertionError(("unexpected prefix stop", snapshot()))
            options = dict(pair_scan_slack_weight=slack,
                maximum_virtual_bridge_nodes=request.policy.maximum_virtual_bridge_nodes) if name.endswith("whole_chain") else {}
            getattr(search, name)(state.task, state.program, state.quality, state, runtime, **options)
            data["phases"].append(snapshot())
            print(json.dumps(snapshot()), flush=True)
        if runtime.allows_search():
            runtime.stop_reason = SearchStopReason.LOCAL_SEARCH_COMPLETE
        if args.through_split:
            phase = "split_and_unique_replay"
            search.improve_numeric_controlled_split(state, runtime, pair_scan_slack_weight=slack,
                maximum_virtual_bridge_nodes=request.policy.maximum_virtual_bridge_nodes)
            data["phases"].append(snapshot())
        if args.refinement_checks:
            from apsgo_scheduler.core import _numeric_refinement as refinement
            import numpy as np
            phase = "refinement_window"
            runtime.candidate_check_limit = min(runtime.candidate_check_limit,
                runtime.candidate_check_count + args.refinement_checks)
            generate_digest, generate_count = hashlib.sha256(), 0
            original_scan = refinement._scan_family
            def scan(current, budget, recipes, *a, **kw):
                def observed():
                    nonlocal generate_count
                    for item in recipes:
                        owner, value = item if kw.get("cursor") is not None else (None, item)
                        if isinstance(value, np.ndarray):
                            value = (tuple(search.NumericSearchAction)[value[0]],
                                *(int(value[i]) for i in (1, 2, 5, 6, 7, 8)))
                        record = (kw.get("diagnostic_key"), owner, value[0].value, *value[1:])
                        generate_digest.update(json.dumps(record, separators=(",", ":")).encode() + b"\n")
                        generate_count += 1
                        yield item
                return original_scan(current, budget, observed(), *a, **kw)
            with patch.object(refinement, "_scan_family", scan):
                diagnostics = refinement.NumericRefinementDiagnostics() if args.refinement_diagnostics else None
                profiler = cProfile.Profile() if args.profile_refinement else None
                if profiler is not None:
                    profiler.enable()
                try:
                    refinement.improve_numeric_refinement(state, runtime,
                        maximum_virtual_bridge_nodes=request.policy.maximum_virtual_bridge_nodes,
                        diagnostics=diagnostics)
                finally:
                    if profiler is not None:
                        profiler.disable()
                        profiler.dump_stats(str(args.output_dir / "refinement.pstats"))
                        with (args.output_dir / "refinement_profile.txt").open("x", encoding="utf-8") as stream:
                            stats = pstats.Stats(profiler, stream=stream)
                            stats.sort_stats("cumulative").print_stats(60)
                            stats.sort_stats("tottime").print_stats(40)
            if diagnostics is not None:
                _write_json(args.output_dir / "refinement_diagnostics.json",
                    json.loads(json.dumps(diagnostics.snapshot()), parse_float=Decimal))
            data["phases"].append(snapshot())
            data.update(refinement_descriptions=generate_count,
                refinement_description_sha256=generate_digest.hexdigest())
    data.update(quota_count=quota_count, quota_sha256=digest.hexdigest(), final=snapshot(),
        node_ids=state.task.node_ids, ancestors=state.task.ancestor_fingerprints,
        rows=state.plan.node_rows.tolist(), offsets=state.plan.chain_offsets.tolist(),
        ids=state.plan.chain_ids.tolist(), periods=state.plan.chain_periods.tolist())
    _write_json(args.output_dir / "search_prefix.json", data)
    _write_json(args.output_dir / "identity.json", dict(source_root=str(root),
        input_sha256=hashlib.sha256(args.prepared_request.read_bytes()).hexdigest(),
        sources={str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted((root / "src/apsgo_scheduler/core").glob("_numeric*.py"))},
        tool_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        refinement_profile=args.profile_refinement,
        wall_seconds=str(perf_counter() - started), cpu_seconds=str(process_time() - cpu),
        mode=("profiled_refinement_diagnostic_not_performance" if args.profile_refinement
              else "cold_prefix_diagnostic_not_performance")))


if __name__ == "__main__":
    main()
