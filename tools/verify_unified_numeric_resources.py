"""Compare resource choices against a separately exported numeric baseline."""

import argparse
from dataclasses import fields
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--prepared-request", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--splits", action="store_true")
    args = parser.parse_args()
    root = args.source_root.resolve()
    sys.path[:0] = [str(root / "src"), str(root)]
    from apsgo_scheduler.app.input_normalizer import normalize_input
    from apsgo_scheduler.app.rule_set_loader import load_rule_set
    from apsgo_scheduler.core._numeric_state import NumericTask, NumericPlan, NumericNodeColumns
    from apsgo_scheduler.core._numeric_rules import NumericRuleProgram, numeric_edge_allowed
    from apsgo_scheduler.core._numeric_evaluation import NumericQualityProgram
    from apsgo_scheduler.core import _numeric_resources as resources
    from tools.profile_numeric_solver import derive_numeric_request
    from tools.profile_solver_search import load_request
    from tools.verify_solver_diagnostics import _write_json

    if not args.private and not hasattr(resources, "choose_virtual_bridge"):
        parser.error("object resource reference was retired; use --private for the current source "
                     "or --source-root pointing to the frozen pre-unification export")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    request = derive_numeric_request(load_request(args.prepared_request))
    rule_set = load_rule_set(request.rule_set_spec)
    task = NumericTask.build(normalize_input(request, rule_set), rule_set, request.delivery_timing)
    program = NumericRuleProgram.compile(task, rule_set)
    quality = NumericQualityProgram.compile(task, program, rule_set)
    count = len(task.source_ids)
    if args.private:
        from apsgo_scheduler.core._numeric_state import NumericCandidateWorkspace, OK
        # Only a base identity is needed; this is a resource test, not scheduling.
        plan = NumericPlan.build(task, tuple(range(count)), (0, count), (0,), (0,))
        workspace = NumericCandidateWorkspace.allocate(task, plan, changed_capacity=4,
            chain_capacity=2, node_capacity=32, group_capacity=1, event_capacity=32)
    if args.splits:
        from apsgo_scheduler.core._numeric_rules import evaluate_numeric_split
        if not args.private:
            from apsgo_scheduler.core._numeric_search import _split_piece_weights
        from apsgo_scheduler.core._numeric_units import allocate_piece_milliseconds
        samples = []
        for parent in range(count):
            for origin in range(int(task.nodes.source_period[parent]) + 1):
                decision = evaluate_numeric_split(task, program, parent, origin, 0)
                if not decision.eligible:
                    continue
                if args.private:
                    workspace.reset()
                    status, created, pieces = resources.prepare_private_split(workspace, parent, decision, origin, sequence=1)
                    if status != OK:
                        raise AssertionError((parent, origin, status))
                else:
                    weights = _split_piece_weights(int(task.nodes.weight[parent]), decision)
                    created, pieces = bool(weights), ()
                    result = None
                    if created:
                        durations = allocate_piece_milliseconds(int(task.nodes.weight[parent]),
                            int(task.nodes.duration_ms[parent]), weights, "split.duration")
                        group = resources.split_group(task, parent, decision, origin, 1)
                        nodes = tuple(resources.split_piece_node(task, parent, group_index=0,
                            piece_index=i, piece_count=len(weights), weight=w, duration_ms=d,
                            accepted_sequence=1) for i, (w, d) in enumerate(zip(weights, durations), 1))
                        result = resources.extend_resource_workspace(task, program, quality, nodes, split_group=group)
                        pieces = result.rows
                ends, group_ends, completed = [], [], created
                if created:
                    ends.append(len(pieces)); group_ends.append(1)
                    for sequence, (left, right) in enumerate(zip(pieces, pieces[1:]), 1):
                        if args.private:
                            status, found, selected = resources.prepare_private_separator(workspace, program,
                                int(left), int(right), sequence=sequence, group_index=0)
                            if status != OK:
                                raise AssertionError((parent, origin, sequence, status))
                        else:
                            selected_result = resources.choose_split_separator(result.task, result.program,
                                result.quality, left, right, sequence=sequence, group_index=0)
                            found = selected_result is not None
                            if found:
                                result = selected_result
                        if not found:
                            completed = False
                            break
                        ends.append(ends[-1] + 1); group_ends.append(1)
                if args.private:
                    indices = list(range(workspace.node_count))
                    columns, derived = workspace.nodes, workspace.derived
                    groups = {name: column[:workspace.group_count].tolist()
                              for name, column in zip(workspace.split_groups._fields, workspace.split_groups)}
                    assert ends == workspace.event_node_ends[:workspace.event_count].tolist()
                    assert group_ends == workspace.event_group_ends[:workspace.event_count].tolist()
                else:
                    current = task if result is None else result.task
                    indices = list(range(task.nodes.weight.size, current.nodes.weight.size))
                    columns, derived = current.nodes, current
                    groups = {f.name: getattr(current.split_groups, f.name).tolist() for f in fields(current.split_groups)}
                samples.append({"parent": parent, "origin": origin, "mode": decision.mode,
                    "created": bool(created), "completed": bool(completed), "node_ends": ends,
                    "group_ends": group_ends, "groups": groups,
                    "nodes": {f.name: getattr(columns, f.name)[indices].tolist() for f in fields(NumericNodeColumns)},
                    "derived": {name: getattr(derived, name)[:, indices].tolist()
                                for name in ("priority", "narrow_matches", "surface_matches", "same_spec_groups")}})
        _write_json(args.output_dir / "splits.json", samples)
        _write_json(args.output_dir / "identity.json", {"source_root": str(root), "private": args.private,
            "input_sha256": hashlib.sha256(args.prepared_request.read_bytes()).hexdigest(),
            "task": task.fingerprint,
            "sources": {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in sorted((root / "src/apsgo_scheduler/core").glob("_numeric*.py"))}})
        print(json.dumps({"samples": len(samples), "complete": sum(x['completed'] for x in samples),
            "same_period": sum(x['mode'] == 0 for x in samples),
            "future_return": sum(x['mode'] == 1 for x in samples)}))
        return
    samples = []
    totals = {"direct": 0, "none": 0, "single": 0, "double": 0}
    started = perf_counter()
    for left in range(count):
        for distance in (1, 7):
            right = (left + distance) % count
            for maximum in (1, 2):
                direct = numeric_edge_allowed(task, program, left, right)
                if args.private:
                    workspace.reset()
                    status, connected, rows = resources.prepare_private_bridge(workspace, program,
                        left, right, max_nodes=maximum, first_sequence=1)
                    if status != OK:
                        raise AssertionError((left, right, maximum, status))
                    selected = list(range(workspace.node_count))
                    columns, derived = workspace.nodes, workspace.derived
                else:
                    result = resources.choose_virtual_bridge(task, program, quality, left, right,
                        max_nodes=maximum, first_sequence=1)
                    connected = direct or result is not None
                    selected = [] if result is None else list(result.rows)
                    columns = task.nodes if result is None else result.task.nodes
                    derived = task if result is None else result.task
                label = "direct" if direct else "none" if not connected else "single" if len(selected) == 1 else "double"
                totals[label] += 1
                samples.append({"left": left, "right": right, "maximum": maximum,
                    "result": label,
                    "nodes": {f.name: getattr(columns, f.name)[selected].tolist()
                              for f in fields(NumericNodeColumns)},
                    "derived": {name: getattr(derived, name)[:, selected].tolist()
                                for name in ("priority", "narrow_matches", "surface_matches", "same_spec_groups")}})
    _write_json(args.output_dir / "samples.json", samples)
    summary = {"samples": len(samples), "outcomes": totals, "seconds": Decimal(str(perf_counter() - started)),
        "source_root": str(root), "private": args.private, "orders": count,
        "prototypes": len(task.prototype_ids), "task": task.fingerprint,
        "input_sha256": hashlib.sha256(args.prepared_request.read_bytes()).hexdigest(),
        "source_sha256": {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sorted((root / "src/apsgo_scheduler/core").glob("_numeric*.py"))}}
    _write_json(args.output_dir / "summary.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "source_sha256"},
                     ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
