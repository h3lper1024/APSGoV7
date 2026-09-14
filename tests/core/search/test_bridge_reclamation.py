"""Ordinary bridge reclamation uses real acceptance, full evaluation and immutable state."""

from dataclasses import replace
from decimal import Decimal as D

import pytest

from apsgo_scheduler.core import width_optimization as search
from apsgo_scheduler.core.contracts import CoreCandidateSnapshot, SearchStopReason, RuleScope, fingerprint
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.final_audit import audit_core_without_search_cache
from apsgo_scheduler.core.model import SchedulePlan, VirtualPurpose
from apsgo_scheduler.core.neighborhoods import is_ordinary_bridge, try_complete_candidate
from apsgo_scheduler.core.rules.concrete import HighSurfaceRunCountRule
from tests.core.test_backlog_priority_objective import tradeoff_case
from tests.core.search.test_virtual_material_factory import prototype


def case(*, minimum="700", second_precision=True, count=1, position=1, purpose=VirtualPurpose.EDGE_BRIDGE):
    _, state, context = tradeoff_case(minimum=minimum, second_precision=second_precision)
    factory = context.factory
    catalog = (prototype("bridge", width="1000"),)
    timing = replace(factory.cache.context.delivery_timing, virtual_hours_per_tonne={"bridge": D("0.01")})
    problem = replace(factory.cache.problem, virtual_prototypes=catalog, delivery_timing=timing)
    bound = replace(factory.cache.context, virtual_prototype_ids=("bridge",), delivery_timing=timing)
    context.factory = replace(factory, cache=replace(factory.cache, problem=problem, context=bound))
    context.factory.budget.candidate_check_limit = 10000
    context.policy = replace(context.policy, candidate_check_limit=10000)
    chain = state.current_plan.chains[0]
    bridges = tuple(context.factory.materialize(catalog[0], *chain.nodes, purpose=purpose, sequence=i + 9,
        **({"related_partition_id": "protected-partition"} if purpose is VirtualPurpose.SPLIT_SEPARATOR else {}))
        for i in range(count))
    state.current_plan = replace(state.current_plan, chains=(replace(chain, nodes=(
        *chain.nodes[:position], *bridges, *chain.nodes[position:])), state.current_plan.chains[1]))
    state.current_evaluation = evaluate_plan(state.current_plan, context.factory.cache.rule_set, bound)
    state.virtual_sequence = count + 8
    return state, context, bridges


def attempt(state, context, removed, **changes):
    ids = tuple(node.node_id for node in removed)
    chains = tuple(replace(chain, nodes=tuple(node for node in chain.nodes if node.node_id not in ids))
                   for chain in state.current_plan.chains)
    options = dict(affected_chain_ids=(chains[0].chain_id,), virtual_sequence=state.virtual_sequence,
                   action_name="width_bridge_reclamation", width_optimization_only=True, removed_bridge_ids=ids)
    return try_complete_candidate(state, context, chains, **(options | changes))


@pytest.mark.parametrize("position", [0, 1, 2])
def test_head_middle_tail_bridge_can_be_removed_without_rewinding_high_watermark(position):
    state, context, bridges = case(position=position)
    old = state.current_plan
    assert attempt(state, context, bridges)
    assert state.virtual_sequence == 9
    assert state.current_plan.chains[1] is old.chains[1]
    cache = context.factory.cache
    audit = audit_core_without_search_cache(CoreCandidateSnapshot(state.current_plan, state.current_evaluation),
        cache.problem, cache.rule_set, context.factory.budget)
    assert audit.report.passed
    assert all(node.node_id != bridges[0].node_id for chain in state.current_plan.chains for node in chain.nodes)


@pytest.mark.parametrize("purpose", [VirtualPurpose.WEIGHT_FILL, VirtualPurpose.SPLIT_SEPARATOR])
def test_protected_virtual_purpose_is_rejected_even_if_deletion_improves_score(purpose):
    state, context, bridges = case(purpose=purpose)
    before = fingerprint(state)
    assert not is_ordinary_bridge(bridges[0])
    assert not attempt(state, context, bridges)
    assert fingerprint(state) == before and not context.accepted_move_traces


@pytest.mark.parametrize("options", [dict(removed_bridge_ids=()), dict(removed_bridge_ids=("unknown",)),
    dict(affected_chain_ids=("last",)), dict(width_optimization_only=False), dict(chain_order_only=True)])
def test_deletion_requires_exact_identity_affected_chain_and_explicit_mode(options):
    state, context, bridges = case()
    before = fingerprint(state)
    assert not attempt(state, context, bridges, **options)
    assert fingerprint(state) == before and context.complete_candidate_evaluation_count == 0


def test_old_mode_and_new_underweight_reject_reclamation():
    for options in (dict(second_precision=False), dict(minimum="810")):
        state, context, bridges = case(**options)
        before = fingerprint(state)
        assert not attempt(state, context, bridges)
        assert fingerprint(state) == before and not context.accepted_move_traces


def test_two_individually_deletable_bridges_are_not_assumed_jointly_deletable():
    for count, accepted in ((1, True), (2, False)):
        state, context, bridges = case(minimum="810", count=2)
        assert attempt(state, context, bridges[:count]) is accepted


def test_retained_values_and_real_nodes_cannot_be_forged():
    state, context, bridges = case()
    before = fingerprint(state)
    assert not attempt(state, context, (state.current_plan.chains[0].nodes[0],))
    a, b = state.current_plan.chains
    forged = replace(a.nodes[0], weight=a.nodes[0].weight + 1)
    assert not try_complete_candidate(state, context, (replace(a, nodes=(forged, a.nodes[-1])), b),
        affected_chain_ids=(a.chain_id,), virtual_sequence=9, action_name="width_bridge_reclamation",
        width_optimization_only=True, removed_bridge_ids=(bridges[0].node_id,))
    assert fingerprint(state) == before


def test_cleanup_proposals_whole_segment_first_then_singles_and_full_wrap():
    state, context, bridges = case(count=2)
    progress = dict(after_chain=None, after_node=None)
    first = list(search._bridge_reclamation_recipes(state, context, progress))
    assert first == [("width_bridge_reclamation", 0, 1, 3), ("width_bridge_reclamation", 0, 1, 2),
                     ("width_bridge_reclamation", 0, 2, 3)]
    assert list(search._bridge_reclamation_recipes(state, context, progress)) == first
    assert context.factory.budget.candidate_check_count == 0


def test_cleanup_before_structural_search_and_shared_cap(monkeypatch):
    state, context, bridges = case(count=2)
    context.factory.budget.candidate_check_limit = 1
    before, calls = fingerprint(state), []
    search._scan_critical_families(state, context, lambda s, c, r: calls.append(r) or False)
    assert calls == [("width_bridge_reclamation", 0, 1, 3)]
    assert context.factory.budget.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert fingerprint(state) == before


def test_cleaned_edit_then_original_fallback_pay_distinct_checks(monkeypatch):
    state, context, bridges = case()
    calls = []
    def repaired(s, c, indices, parts, action, removed):
        calls.append((removed, parts))
        return not removed
    monkeypatch.setattr(search, "_try_repaired_parts", repaired)
    recipe = ("width_node_move", 0, 1, 2, 3, 0, 0)
    assert search._scan_width_batch(state, context, iter((recipe,)), search._try_width_recipe, 1)[0]
    assert calls[0][0] == (bridges[0].node_id,) and calls[1][0] == ()
    assert context.factory.budget.candidate_check_count == 2
    assert state.virtual_sequence == 9


def test_unmodified_versions_are_not_repeated_and_cancel_does_not_fall_back(monkeypatch):
    state, context, bridges = case()
    raw = ((state.current_plan.chains[1].nodes,),)
    assert list(search._edit_variants(raw, context)) == [(raw, ())]
    def repaired(*args):
        context.factory.budget.stop_reason = SearchStopReason.USER_CANCELLED
        return False
    monkeypatch.setattr(search, "_try_repaired_parts", repaired)
    before = fingerprint(state)
    assert not search._scan_width_batch(state, context, iter((("width_node_move", 0, 1, 2, 3, 0, 0),)), search._try_width_recipe, 1)[0]
    assert fingerprint(state) == before and context.factory.budget.candidate_check_count == 1


def test_new_bridge_uses_retired_upper_bound_not_surviving_maximum():
    state, context, bridges = case()
    assert attempt(state, context, bridges)
    first, last = state.current_plan.chains
    moved = last.nodes[-1]
    new = context.factory.materialize(context.factory.cache.problem.virtual_prototypes[0], moved, first.nodes[0],
                                     purpose=VirtualPurpose.EDGE_BRIDGE, sequence=10)
    candidate = (replace(first, nodes=(moved, new, *first.nodes)), replace(last, nodes=last.nodes[:-1]))
    assert try_complete_candidate(state, context, candidate, affected_chain_ids=(first.chain_id, last.chain_id),
        virtual_sequence=10, action_name="width_node_move", width_optimization_only=True)
    assert state.virtual_sequence == 10 and new.node_id != bridges[0].node_id
    assert context.factory.materialize(context.factory.cache.problem.virtual_prototypes[0], moved, first.nodes[0],
        purpose=VirtualPurpose.EDGE_BRIDGE, sequence=state.virtual_sequence + 1).node_id == "virtual-000011"


@pytest.mark.parametrize("recipe", [
    ("delivery_intra_move", 0, 2, 0),
    ("width_node_move", 0, 1, 2, 3, 2, 2),
    ("width_node_exchange", 0, 1, 2, 3, 1, 2),
    ("width_block_move", 0, 1, 1, 3, 2, 2),
    ("width_block_exchange", 0, 1, 1, 3, 1, 2),
])
def test_all_five_structural_actions_reclaim_only_interface_bridges_and_audit(recipe):
    state, context, bridges = case()
    before = {n.node_id: n for c in state.current_plan.chains for n in c.nodes}
    assert search._scan_width_batch(state, context, iter((recipe,)), search._try_width_recipe, 1)[0]
    after = {n.node_id: n for c in state.current_plan.chains for n in c.nodes}
    assert before.keys() - after.keys() == {bridges[0].node_id}
    assert all(n == before[key] for key, n in after.items())
    assert state.virtual_sequence == 9 and context.factory.budget.candidate_check_count == 1
    cache = context.factory.cache
    assert audit_core_without_search_cache(CoreCandidateSnapshot(state.current_plan, state.current_evaluation),
        cache.problem, cache.rule_set, context.factory.budget).report.passed


def test_direct_edges_do_not_override_chain_rule_and_real_fallback_remains_accepted():
    state, context, bridges = case()
    def surface(node):
        if node.virtual_lineage:
            return node
        return replace(node, rule_attributes={**node.rule_attributes,
            "surface_grade": "FB" if node.source_order_id == "order-2" else "FC"})
    state.current_plan = replace(state.current_plan, chains=tuple(replace(c, nodes=tuple(map(surface, c.nodes)))
                                                                for c in state.current_plan.chains))
    cache = context.factory.cache
    active = replace(cache.rule_set, rules=(*cache.rule_set.rules, HighSurfaceRunCountRule(
        "surface", "表面连续", RuleScope.CHAIN, True, "1", {"surface_grades": ("FC",), "max_run_count": 1})))
    context.factory = replace(context.factory, cache=replace(cache, rule_set=active))
    state.current_evaluation = evaluate_plan(state.current_plan, active, cache.context)
    assert not state.current_evaluation.violations
    before = fingerprint(state)
    assert not attempt(state, context, bridges)
    assert fingerprint(state) == before
    assert search._scan_width_batch(state, context, iter((("width_node_exchange", 0, 1, 2, 3, 1, 2),)),
                                    search._try_width_recipe, 1)[0]
    assert any(n.node_id == bridges[0].node_id for c in state.current_plan.chains for n in c.nodes)
    assert context.factory.budget.candidate_check_count == 2 and not state.current_evaluation.violations


def test_unchanged_chain_reclamation_is_revisited_after_other_chain_acceptance(monkeypatch):
    state, context, bridges = case()
    seen, changed = [], False
    def try_recipe(current, bound, recipe):
        nonlocal changed
        if recipe[0] == "width_bridge_reclamation":
            seen.append(current.accepted_move_count)
        elif not changed:
            changed = True
            last = current.current_plan.chains[1]
            candidate = replace(current.current_plan, chains=(current.current_plan.chains[0],
                                replace(last, nodes=tuple(reversed(last.nodes)))))
            current.commit_accepted(candidate, evaluate_plan(candidate, bound.factory.cache.rule_set,
                                    bound.factory.cache.context), virtual_sequence=current.virtual_sequence)
            return True
        return False
    search._scan_critical_families(state, context, try_recipe)
    assert seen[0] == 0 and 1 in seen
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE
