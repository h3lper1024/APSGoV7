"""Freeze independent critical-delivery and ordinary-bridge witnesses, not a solve."""

import argparse
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from apsgo_scheduler.core.contracts import CoreCandidateSnapshot, fingerprint
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.final_audit import audit_core_without_search_cache
from apsgo_scheduler.core.model import MaterialRole, SchedulePlan, VirtualPurpose
from apsgo_scheduler.core.neighborhoods import try_complete_candidate
from apsgo_scheduler.core import width_optimization
from apsgo_v7_service.diagnostics import _json_values
from tools.replay_backlog_search_witnesses import load_case, report_with_positions
from tools.verify_solver_diagnostics import _code_identity, _sha256, _write_json as save_json


def _write_json(path, value):
    save_json(path, _json_values(value))


def ordinary_bridge(node):
    return (node.material_role is MaterialRole.GENERATED_VIRTUAL
            and node.virtual_lineage.purpose is VirtualPurpose.EDGE_BRIDGE
            and node.virtual_lineage.related_partition_id is None)


def move_recipe(plan):
    source = next(i for i, chain in enumerate(plan.chains) if chain.chain_id == "initial-000014")
    target = next(i for i, chain in enumerate(plan.chains) if chain.chain_id == "initial-000020")
    start = next(i for i, node in enumerate(plan.chains[source].nodes)
                 if node.node_id == "0002002009-000020")
    slot = len(plan.chains[target].nodes)
    recipe = ("width_node_move", source, target, start, start + 1, slot, slot)
    if recipe != ("width_node_move", 16, 7, 14, 15, 16, 16):
        raise ValueError("frozen witness positions changed")
    return recipe


def audited(plan, evaluation, context):
    cache = context.factory.cache
    return audit_core_without_search_cache(CoreCandidateSnapshot(plan, evaluation),
        cache.problem, cache.rule_set, context.factory.budget)


def probe(run, output, *, expect_reclamation=False):
    output.mkdir(parents=True, exist_ok=False)
    state, context = load_case(run)
    original = fingerprint(state.current_plan)
    recipe = move_recipe(state.current_plan)
    before_sequence = state.virtual_sequence
    assert context.factory.budget.consume_candidate_check()
    assert width_optimization._try_segment_edit(state, context, recipe)
    # Legacy snapshots lack the high watermark: only a no-new-virtual witness is valid.
    assert state.virtual_sequence == before_sequence
    audit = audited(state.current_plan, state.current_evaluation, context)
    assert audit.report.passed
    _write_json(output / "move.json", dict(recipe=recipe, core_audit=audit,
        candidate=CoreCandidateSnapshot(state.current_plan, state.current_evaluation),
        report=report_with_positions(state.current_plan, context.factory.cache.context.delivery_timing,
                                    include_backlog_clearance=True, second_precision=True)))
    state, context = load_case(run)
    bridge_ids = [node.node_id for chain in state.current_plan.chains for node in chain.nodes if ordinary_bridge(node)]
    deletions = []
    for identity in [*bridge_ids, "virtual-000025"]:
        state, context = load_case(run)
        index = next(i for i, chain in enumerate(state.current_plan.chains)
                     if any(node.node_id == identity for node in chain.nodes))
        old = state.current_plan.chains[index]
        removed = next(node for node in old.nodes if node.node_id == identity)
        chains = list(state.current_plan.chains)
        chains[index] = replace(old, nodes=tuple(node for node in old.nodes if node.node_id != identity))
        plan = SchedulePlan(tuple(chains))
        cache = context.factory.cache
        evaluation = evaluate_plan(plan, cache.rule_set, cache.context)
        audit = audited(plan, evaluation, context)
        improves = evaluation.quality_key < state.current_evaluation.quality_key
        eligible = ordinary_bridge(removed)
        assert context.factory.budget.consume_candidate_check()
        options = {"removed_bridge_ids": (identity,)} if expect_reclamation else {}
        accepted = try_complete_candidate(state, context, tuple(chains),
            affected_chain_ids=(old.chain_id,), virtual_sequence=state.virtual_sequence,
            action_name="width_bridge_reclamation", width_optimization_only=True, **options)
        assert accepted == (expect_reclamation and eligible and improves and audit.report.passed)
        if not accepted:
            assert fingerprint(state.current_plan) == original
        row = dict(node_id=identity, chain_id=old.chain_id, purpose=removed.virtual_lineage.purpose,
                   ordinary=eligible, weight=removed.weight, quality=evaluation.quality_key,
                   improves=improves, core_audit_passed=audit.report.passed, accepted=accepted,
                   candidate_checks=context.factory.budget.candidate_check_count,
                   complete_evaluations=context.complete_candidate_evaluation_count)
        deletions.append(row)
        if identity in ("virtual-000059", "virtual-000025"):
            _write_json(output / (identity + ".json"), dict(summary=row, core_audit=audit,
                candidate=CoreCandidateSnapshot(plan, evaluation),
                report=report_with_positions(plan, cache.context.delivery_timing,
                                            include_backlog_clearance=True, second_precision=True)))
    valid = [row["node_id"] for row in deletions if row["ordinary"] and row["improves"] and row["core_audit_passed"]]
    assert len(bridge_ids) == 45 and len(valid) == 14 and "virtual-000059" in valid
    assert not deletions[-1]["ordinary"] and not deletions[-1]["core_audit_passed"]
    state, context = load_case(run)
    context.factory.budget.candidate_check_limit = 400000
    context.policy = replace(context.policy, candidate_check_limit=400000)
    found, counts = {}, {}

    def observe(current, bound, candidate):
        counts[candidate[0]] = counts.get(candidate[0], 0) + 1
        if candidate == recipe:
            found.setdefault("at_candidate_check", bound.factory.budget.candidate_check_count)
        return False

    with patch.object(width_optimization, "_try_width_recipe", observe):
        width_optimization.run_width_optimization(state, context)
    assert fingerprint(state.current_plan) == original
    result = dict(scope="independent_witnesses_core_audit_only_not_full_solve_or_application_release",
        code=_code_identity(ROOT), source=str(run),
        hashes={name: _sha256(run / name) for name in ("prepared_request.json", "measurement.json", "delivery_report.json")},
        ordinary_bridge_count=len(bridge_ids), independently_valid_deletions=valid,
        deletion_observations=deletions, move_accepted_and_core_audited=True,
        enumeration=dict(found=found, counts=counts, checked=context.factory.budget.candidate_check_count,
                         stop_reason=context.factory.budget.stop_reason))
    _write_json(output / "summary.json", result)
    print(dict(valid_deletions=len(valid), move_found=found, expected_reclamation=expect_reclamation))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expect-reclamation", action="store_true")
    args = parser.parse_args()
    probe(args.run, args.output_dir, expect_reclamation=args.expect_reclamation)
