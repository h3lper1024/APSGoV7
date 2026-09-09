"""Independent final replay rejects contaminated structure, identity and evaluation."""

from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

import pytest

from apsgo_scheduler.core import controlled_split, evaluation, final_audit, neighborhoods
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import (
    CoreAuditStatus,
    CoreCandidateSnapshot,
    DiagnosticPhase,
    DiagnosticSeverity,
    RuleScope,
    SearchStopReason,
    fingerprint,
)
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import (
    Chain,
    MaterialRole,
    SchedulePlan,
    SchedulingProblem,
    VirtualPurpose,
)
from apsgo_scheduler.core.rules.base import RuleEvaluationContext
from apsgo_scheduler.core.rules.concrete import WidthTransitionRule
from apsgo_scheduler.core.rules.rule_set import ProcessRuleSet
from apsgo_scheduler.core.virtual_material import VirtualFactory
from tests.core.graph.test_bipartite_matching import budget
from tests.core.graph.test_construction_order import node, rules
from tests.core.search.test_controlled_order_split import (
    parent,
    split_case,
    suppress_replay,
)
from tests.core.search.test_single_real_node_relocation import Stop
from tests.core.search.test_virtual_material_factory import prototype

D = Decimal


def evaluation_context(problem):
    return RuleEvaluationContext(
        problem.period_order,
        {period: index for index, period in enumerate(problem.period_order)},
        tuple(item.prototype_id for item in problem.virtual_prototypes),
    )


def audit_case(
    *,
    nodes=None,
    chains=None,
    rule_items=(),
    prototypes=(),
    period_order=("period",),
    runtime=None,
    rule_set=None,
):
    nodes = (node("real"),) if nodes is None else tuple(nodes)
    rule_set = (
        replace(rules(), rules=tuple(rule_items), fingerprint=fingerprint(tuple(rule_items)))
        if rule_set is None
        else rule_set
    )
    problem = SchedulingProblem(
        "audit-problem",
        rule_set.product_line_code,
        rule_set.process_code,
        rule_set.scenario,
        nodes,
        tuple(period_order),
        tuple(prototypes),
        fingerprint((nodes, period_order, prototypes)),
    )
    if chains is None:
        assigned = min((item.source_period for item in nodes), key=problem.period_order.index)
        chains = (Chain("audit-chain", nodes, assigned),)
    plan = SchedulePlan(tuple(chains))
    candidate = CoreCandidateSnapshot(
        plan, evaluate_plan(plan, rule_set, evaluation_context(problem))
    )
    return candidate, problem, rule_set, budget() if runtime is None else runtime


def accepted_split_case(monkeypatch, **kwargs):
    state, context = split_case(**kwargs)
    with monkeypatch.context() as scope:
        suppress_replay(scope)
        controlled_split.run_controlled_order_split(state, context)
    assert state.split_sequence > 0
    return (
        CoreCandidateSnapshot(state.current_plan, state.current_evaluation),
        context.factory.cache.problem,
        context.factory.cache.rule_set,
        context.factory.budget,
    )


def replace_plan(candidate, problem, rule_set, chains):
    plan = SchedulePlan(tuple(chains))
    return CoreCandidateSnapshot(plan, evaluate_plan(plan, rule_set, evaluation_context(problem)))


def assert_rejected(outcome):
    assert not outcome.report.passed
    assert outcome.issues
    assert all(issue.phase is DiagnosticPhase.CORE_AUDIT for issue in outcome.issues)
    assert any(issue.severity is DiagnosticSeverity.ERROR for issue in outcome.issues)


def assert_unfinished(outcome, status):
    assert_rejected(outcome)
    assert outcome.report.status is status
    assert outcome.audited_evaluation is None and outcome.resource_facts is None
    assert outcome.report.audited_evaluation_fingerprint is None
    assert outcome.report.derived_resource_fingerprint is None
    assert outcome.report.audited_split_count is None
    assert outcome.report.audited_same_period_split_count is None
    assert outcome.report.audited_future_borrow_return_count is None
    assert outcome.report.search_evaluation_matches is None


def test_replay_uses_new_authoritative_context_and_never_search_edge_cache(monkeypatch):
    candidate, problem, rule_set, runtime = audit_case(
        nodes=(node("a"), node("b")), prototypes=(prototype("p"),)
    )
    search_context = evaluation_context(problem)
    search_cache = RuleEdgeDecisionCache(problem, rule_set, search_context)
    assert search_cache.allows(*problem.nodes)
    original = final_audit.evaluate_plan
    seen = []

    def replay(plan, active_rules, context):
        seen.append(context)
        assert context == search_context and context is not search_context
        assert plan is candidate.plan and active_rules is rule_set
        return original(plan, active_rules, context)

    monkeypatch.setattr(final_audit, "evaluate_plan", replay)
    for owner, name in (
        (evaluation, "_evaluate_candidate_plan"),
        (neighborhoods, "_evaluate_candidate_plan"),
        (evaluation, "_ChainEvaluationEntry"),
    ):
        monkeypatch.setattr(owner, name, lambda *args: pytest.fail("search reuse used by audit"))
    monkeypatch.setattr(
        RuleEdgeDecisionCache, "allows", lambda *args: pytest.fail("search cache used by audit")
    )
    outcome = final_audit.audit_core_without_search_cache(candidate, problem, rule_set, runtime)
    assert outcome.report.passed and len(seen) == 1
    assert outcome.audited_evaluation == candidate.search_evaluation
    assert outcome.audited_evaluation is not candidate.search_evaluation
    assert runtime.candidate_check_count == 0


@pytest.mark.parametrize("part", ("global_metric", "chain_metric", "summary", "quality"))
def test_comparison_covers_original_metrics_and_chain_summary_not_only_quality(part):
    candidate, problem, rule_set, runtime = audit_case()
    original = candidate.search_evaluation
    if part == "global_metric":
        changed = replace(original, metrics=dict(original.metrics) | {"chain_count": 99})
    elif part == "quality":
        changed = replace(original, quality_key=(1, 0))
    else:
        chain = original.chain_evaluations[0]
        changed_chain = (
            replace(chain, metrics={"invented_metric": D(1)})
            if part == "chain_metric"
            else replace(chain, summary=replace(chain.summary, assigned_period="other"))
        )
        changed = replace(original, chain_evaluations=(changed_chain,))
    outcome = final_audit.audit_core_without_search_cache(
        replace(candidate, search_evaluation=changed), problem, rule_set, runtime
    )
    assert_rejected(outcome)
    assert outcome.report.status is CoreAuditStatus.COMPLETED
    assert outcome.report.search_evaluation_matches is False
    assert outcome.audited_evaluation == original


def test_chain_evaluation_order_is_not_identity():
    a, b = node("a"), node("b")
    candidate, problem, rule_set, runtime = audit_case(
        nodes=(a, b), chains=(Chain("z", (a,), "period"), Chain("a", (b,), "period"))
    )
    changed = replace(
        candidate.search_evaluation,
        chain_evaluations=tuple(reversed(candidate.search_evaluation.chain_evaluations)),
    )
    outcome = final_audit.audit_core_without_search_cache(
        replace(candidate, search_evaluation=changed), problem, rule_set, runtime
    )
    assert outcome.report.passed and outcome.report.search_evaluation_matches


@pytest.mark.parametrize("change", ("missing", "extra", "duplicate_source"))
def test_authoritative_input_coverage_cannot_be_hidden_by_recomputed_evaluation(change):
    a, b = node("a"), replace(node("b"), material_role=MaterialRole.ACTUAL_TRANSITION)
    candidate, problem, rule_set, runtime = audit_case(nodes=(a, b))
    members = {
        "missing": (a,),
        "extra": (a, b, node("extra")),
        "duplicate_source": (a, b, replace(a, node_id="duplicate")),
    }[change]
    changed = replace_plan(candidate, problem, rule_set, (Chain("audit-chain", members, "period"),))
    outcome = final_audit.audit_core_without_search_cache(changed, problem, rule_set, runtime)
    assert_rejected(outcome)
    assert outcome.resource_facts is None
    assert outcome.report.invariant_failure_codes


@pytest.mark.parametrize(
    "changes",
    (
        {"weight": D("20.00000000000000000001")},
        {"source_order_id": "wrong-order"},
        {"source_resource_id": "wrong-resource"},
        {"width": D(999)},
        {"grade": "changed"},
        {"rule_attributes": {"priority": 2}},
        {"material_role": MaterialRole.ACTUAL_TRANSITION},
    ),
)
def test_unsplit_real_values_must_match_original_input(changes):
    candidate, problem, rule_set, runtime = audit_case()
    altered = replace(problem.nodes[0], **changes)
    candidate = replace_plan(
        candidate, problem, rule_set, (Chain("audit-chain", (altered,), "period"),)
    )
    outcome = final_audit.audit_core_without_search_cache(candidate, problem, rule_set, runtime)
    assert_rejected(outcome)
    assert outcome.resource_facts is None


@pytest.mark.parametrize("origin,anchor", (("P2", False), ("P9", True)))
def test_split_authorization_is_rebuilt_from_original_parent_and_fresh_context(
    origin, anchor, monkeypatch
):
    candidate, problem, rule_set, runtime = accepted_split_case(
        monkeypatch, origin=origin, anchor=anchor
    )
    original = ProcessRuleSet.evaluate_controlled_split
    observed = []

    def record(active_rules, subject, context):
        observed.append((subject, context))
        return original(active_rules, subject, context)

    monkeypatch.setattr(ProcessRuleSet, "evaluate_controlled_split", record)
    outcome = final_audit.audit_core_without_search_cache(candidate, problem, rule_set, runtime)
    assert outcome.report.passed and len(observed) == 1
    subject, context = observed[0]
    assert subject.parent_node is next(item for item in problem.nodes if item.node_id == "parent")
    assert subject.origin_assigned_period == origin
    assert subject.source_period == "P2" and subject.accepted_split_source_count == 0
    assert context == evaluation_context(problem)
    assert outcome.report.audited_split_count == 1
    assert outcome.report.audited_same_period_split_count == int(origin == "P2")
    assert outcome.report.audited_future_borrow_return_count == int(origin == "P9")


def test_multiple_partitions_reauthorize_by_acceptance_sequence_not_final_chain_order(monkeypatch):
    candidate, problem, rule_set, runtime = accepted_split_case(
        monkeypatch, parents=(parent("first"), parent("second"))
    )
    candidate = replace_plan(candidate, problem, rule_set, reversed(candidate.plan.chains))
    original = ProcessRuleSet.evaluate_controlled_split
    observed = []

    def record(active_rules, subject, context):
        observed.append((subject.parent_node.node_id, subject.accepted_split_source_count))
        return original(active_rules, subject, context)

    monkeypatch.setattr(ProcessRuleSet, "evaluate_controlled_split", record)
    outcome = final_audit.audit_core_without_search_cache(candidate, problem, rule_set, runtime)
    assert outcome.report.passed
    assert observed == [("first", 0), ("second", 1)]
    assert outcome.report.audited_split_count == outcome.report.audited_same_period_split_count == 2


def test_piece_grouping_is_global_and_does_not_require_original_alternating_chain(monkeypatch):
    candidate, problem, rule_set, runtime = accepted_split_case(monkeypatch)
    first, separator_a, second, separator_b, last = candidate.plan.chains[0].nodes
    candidate = replace_plan(
        candidate,
        problem,
        rule_set,
        (
            Chain("moved-last", (last, separator_a), "P2"),
            Chain("moved-first", (first,), "P2"),
            Chain("moved-second", (separator_b, second), "P2"),
        ),
    )
    outcome = final_audit.audit_core_without_search_cache(candidate, problem, rule_set, runtime)
    assert outcome.report.passed
    fact = outcome.resource_facts.split_partitions[0]
    assert fact.piece_node_ids == (first.node_id, second.node_id, last.node_id)
    assert fact.piece_weights == (D(50), D(50), D(20))


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_piece",
        "duplicate_index",
        "wrong_sum",
        "overweight_piece",
        "wrong_real_attribute",
        "parent_retained",
        "missing_separator",
        "wrong_separator_purpose",
        "unrelated_separator",
        "wrong_fingerprint",
        "sequence_gap",
    ),
)
def test_split_partition_corruption_is_rejected_even_after_fresh_evaluation(mutation, monkeypatch):
    candidate, problem, rule_set, runtime = accepted_split_case(monkeypatch)
    members = list(candidate.plan.chains[0].nodes)
    if mutation == "missing_piece":
        members.pop()
    elif mutation == "duplicate_index":
        members[2] = replace(
            members[2], split_lineage=replace(members[2].split_lineage, piece_index=1)
        )
    elif mutation == "wrong_sum":
        members[0] = replace(members[0], weight=D("49.999999999999999999"))
    elif mutation == "overweight_piece":
        members[0], members[-1] = (
            replace(members[0], weight=D(51)),
            replace(members[-1], weight=D(19)),
        )
    elif mutation == "wrong_real_attribute":
        members[0] = replace(members[0], grade="other")
    elif mutation == "parent_retained":
        members.append(problem.nodes[0])
    elif mutation == "missing_separator":
        members.pop(1)
    elif mutation in {"wrong_separator_purpose", "unrelated_separator"}:
        lineage = members[1].virtual_lineage
        lineage = (
            replace(lineage, purpose=VirtualPurpose.WEIGHT_FILL, related_partition_id=None)
            if mutation == "wrong_separator_purpose"
            else replace(lineage, related_partition_id="unrelated")
        )
        members[1] = replace(members[1], virtual_lineage=lineage)
    else:
        changes = (
            {"authorization_decision_fingerprint": "stale"}
            if mutation == "wrong_fingerprint"
            else {"accepted_source_sequence": 2}
        )
        members = [
            replace(item, split_lineage=replace(item.split_lineage, **changes))
            if item.split_lineage is not None
            else item
            for item in members
        ]
    changed = replace_plan(
        candidate, problem, rule_set, (Chain("partition", tuple(members), "P2"),)
    )
    outcome = final_audit.audit_core_without_search_cache(changed, problem, rule_set, runtime)
    assert_rejected(outcome)
    assert outcome.resource_facts is None


@pytest.mark.parametrize("mutation", ("disabled", "piece_limit", "separator_limit", "source_limit"))
def test_final_authorization_uses_current_rule_instead_of_trusting_lineage(mutation, monkeypatch):
    candidate, problem, rule_set, runtime = accepted_split_case(monkeypatch)
    action = rule_set.rules[0]
    changed = (
        replace(action, enabled=False)
        if mutation == "disabled"
        else replace(
            action,
            parameters=dict(action.parameters)
            | {
                "piece_limit": {"maximum_piece_weight": D(40)},
                "separator_limit": {"maximum_separator_weight": D(9)},
                "source_limit": {"maximum_accepted_source_count": 0},
            }[mutation],
        )
    )
    rule_set = replace(rule_set, rules=(changed, *rule_set.rules[1:]), fingerprint="changed")
    outcome = final_audit.audit_core_without_search_cache(candidate, problem, rule_set, runtime)
    assert_rejected(outcome)
    assert outcome.report.action_authorization_failure_codes
    assert outcome.resource_facts is None


def test_same_split_mode_under_different_task_period_order_still_changes_authorization(monkeypatch):
    candidate, problem, rule_set, runtime = accepted_split_case(monkeypatch)
    # Source and assigned stay P2, so the mode is still SAME_PERIOD_SPLIT.
    problem = replace(problem, period_order=("P1", "P2", "P9"), input_fingerprint="reordered-task")
    outcome = final_audit.audit_core_without_search_cache(candidate, problem, rule_set, runtime)
    assert_rejected(outcome)
    assert outcome.report.action_authorization_failure_codes


@pytest.mark.parametrize("change", ("role", "grade_class"))
def test_parent_eligibility_is_read_from_authoritative_input_not_from_pieces(change, monkeypatch):
    candidate, problem, rule_set, runtime = accepted_split_case(monkeypatch)
    original = problem.nodes[0]
    altered = (
        replace(original, material_role=MaterialRole.ACTUAL_TRANSITION)
        if change == "role"
        else replace(original, rule_attributes={"grade_class": "OTHER"})
    )
    problem = replace(problem, nodes=(altered,), input_fingerprint="changed-input")
    outcome = final_audit.audit_core_without_search_cache(candidate, problem, rule_set, runtime)
    assert_rejected(outcome)
    assert outcome.report.action_authorization_failure_codes
    assert outcome.resource_facts is None


@pytest.mark.parametrize("origin", ("unknown", "P1"))
def test_forged_split_origin_is_not_used_to_infer_task_order(origin, monkeypatch):
    candidate, problem, rule_set, runtime = accepted_split_case(
        monkeypatch, origin="P9", anchor=True
    )
    partition = candidate.plan.chains[-1]
    members = tuple(
        replace(item, split_lineage=replace(item.split_lineage, origin_assigned_period=origin))
        if item.split_lineage is not None
        else item
        for item in partition.nodes
    )
    candidate = replace_plan(
        candidate,
        problem,
        rule_set,
        (*candidate.plan.chains[:-1], replace(partition, nodes=members)),
    )
    outcome = final_audit.audit_core_without_search_cache(candidate, problem, rule_set, runtime)
    assert_rejected(outcome)
    assert outcome.resource_facts is None


def test_split_target_lock_is_independent_of_current_rule_quality(monkeypatch):
    candidate, problem, rule_set, runtime = accepted_split_case(
        monkeypatch, origin="P9", anchor=True
    )
    original = tuple(item for chain in candidate.plan.chains for item in chain.nodes)
    candidate = replace_plan(candidate, problem, rule_set, (Chain("mixed", original, "P9"),))
    assert candidate.search_evaluation.quality_key == (0, 0)
    outcome = final_audit.audit_core_without_search_cache(candidate, problem, rule_set, runtime)
    assert_rejected(outcome)
    assert outcome.report.invariant_failure_codes


def virtual_case():
    left, right = node("left", width="1000"), node("right", width="1050")
    catalog = (prototype("p", width="1100"),)
    candidate, problem, rule_set, runtime = audit_case(nodes=(left, right), prototypes=catalog)
    factory = VirtualFactory(
        RuleEdgeDecisionCache(problem, rule_set, evaluation_context(problem)), runtime
    )
    generated = factory.materialize(
        catalog[0], left, right, purpose=VirtualPurpose.EDGE_BRIDGE, sequence=1
    )
    candidate = replace_plan(
        candidate, problem, rule_set, (Chain("audit-chain", (left, generated, right), "period"),)
    )
    return candidate, problem, rule_set, runtime


@pytest.mark.parametrize("mutation", ("prototype", "weight", "grade", "attributes", "sequence"))
def test_virtual_identity_is_verified_against_catalog_and_global_generation_sequence(mutation):
    candidate, problem, rule_set, runtime = virtual_case()
    left, virtual, right = candidate.plan.chains[0].nodes
    if mutation == "prototype":
        virtual = replace(
            virtual, virtual_lineage=replace(virtual.virtual_lineage, prototype_id="lost")
        )
    elif mutation == "weight":
        virtual = replace(virtual, weight=D(21))
    elif mutation == "grade":
        virtual = replace(virtual, grade="wrong")
    elif mutation == "attributes":
        virtual = replace(virtual, rule_attributes={"unexpected": True})
    members = (left, virtual, right)
    if mutation == "sequence":
        members = (left, virtual, replace(virtual, node_id="duplicate-generation"), right)
    candidate = replace_plan(
        candidate, problem, rule_set, (Chain("audit-chain", members, "period"),)
    )
    outcome = final_audit.audit_core_without_search_cache(candidate, problem, rule_set, runtime)
    assert_rejected(outcome)
    assert outcome.resource_facts is None


def test_corrupted_virtual_temperature_interval_is_invalid_even_without_temperature_rule():
    candidate, problem, rule_set, runtime = virtual_case()
    left, virtual, right = candidate.plan.chains[0].nodes
    damaged = replace(virtual)
    # Simulate corruption after Node's original construction, without changing the input.
    object.__setattr__(damaged, "min_temperature", D(901))
    candidate = replace_plan(
        candidate, problem, rule_set, (Chain("audit-chain", (left, damaged, right), "period"),)
    )
    assert candidate.search_evaluation.quality_key == (0, 0)
    outcome = final_audit.audit_core_without_search_cache(candidate, problem, rule_set, runtime)
    assert_rejected(outcome)
    assert "physical_value_invalid" in outcome.report.invariant_failure_codes
    assert outcome.resource_facts is None


def test_internal_cross_virtual_width_rule_is_replayed_when_both_adjacent_edges_allow():
    candidate, problem, _, runtime = virtual_case()
    width = WidthTransitionRule(
        "width",
        "width",
        RuleScope.EDGE,
        True,
        "1",
        {"max_reverse_width": D(20), "virtual_width_tolerance": D(200)},
    )
    rule_set = replace(rules(), rules=(width,), fingerprint=fingerprint(width))
    cache = RuleEdgeDecisionCache(problem, rule_set, evaluation_context(problem))
    left, virtual, right = candidate.plan.chains[0].nodes
    assert cache.allows(left, virtual) and cache.allows(virtual, right)
    # Candidate retains the old no-rules evaluation, simulating a missed chain check.
    outcome = final_audit.audit_core_without_search_cache(candidate, problem, rule_set, runtime)
    assert_rejected(outcome)
    assert outcome.report.search_evaluation_matches is False
    assert outcome.audited_evaluation.metrics["prohibited_violation_count"] == 1
    assert outcome.audited_evaluation.violations[0].scope is RuleScope.CHAIN


@pytest.mark.parametrize("duplicate", (False, True))
def test_violation_comparison_ignores_order_but_preserves_multiplicity(duplicate):
    candidate, problem, rule_set, runtime = audit_case(
        nodes=(node("a", width="1000"), node("b", width="1040"), node("c", width="1080")),
        rule_set=rules(maximum_increase="20"),
    )
    original = candidate.search_evaluation
    assert len(original.violations) == 2
    violations = (
        original.violations + (original.violations[0],)
        if duplicate
        else tuple(reversed(original.violations))
    )
    changed = replace(
        original,
        violations=violations,
        chain_evaluations=(replace(original.chain_evaluations[0], violations=violations),),
    )
    outcome = final_audit.audit_core_without_search_cache(
        replace(candidate, search_evaluation=changed), problem, rule_set, runtime
    )
    assert_rejected(outcome)  # Width violations remain prohibited even when replay agrees.
    assert outcome.report.search_evaluation_matches is (not duplicate)


@pytest.mark.parametrize("mutation", ("empty_chain", "duplicate_node", "unknown_period"))
def test_corrupt_internal_structure_reports_only_comparisons_it_can_complete(mutation):
    candidate, problem, rule_set, runtime = audit_case()
    broken_chain = replace(candidate.plan.chains[0])
    if mutation == "unknown_period":
        broken_chain = replace(broken_chain, assigned_period="unknown")
    else:
        # Simulate internal corruption past the immutable model's construction checks.
        members = () if mutation == "empty_chain" else broken_chain.nodes * 2
        object.__setattr__(broken_chain, "nodes", members)
    broken_plan = object.__new__(SchedulePlan)
    object.__setattr__(broken_plan, "chains", (broken_chain,))
    outcome = final_audit.audit_core_without_search_cache(
        replace(candidate, plan=broken_plan), problem, rule_set, runtime
    )
    if mutation == "duplicate_node":
        # The evaluator can finish this corrupt structure, but independent coverage rejects it.
        assert_rejected(outcome)
        assert outcome.report.status is CoreAuditStatus.COMPLETED
        assert "duplicate_node_identity" in outcome.report.invariant_failure_codes
        assert outcome.resource_facts is None
    else:
        assert_unfinished(outcome, CoreAuditStatus.ERROR)


@pytest.mark.parametrize("argument", range(4))
def test_wrong_public_argument_type_is_rejected_without_partial_audit(argument):
    values = list(audit_case())
    values[argument] = object()
    with pytest.raises(ValueError):
        final_audit.audit_core_without_search_cache(*values)


def test_rule_set_task_identity_mismatch_is_rejected():
    candidate, problem, rule_set, runtime = audit_case()
    with pytest.raises(ValueError):
        final_audit.audit_core_without_search_cache(
            candidate, replace(problem, product_line_code="another-line"), rule_set, runtime
        )


@pytest.mark.parametrize(
    "reason",
    (
        SearchStopReason.CANDIDATE_LIMIT_REACHED,
        SearchStopReason.SEARCH_TIME_LIMIT_REACHED,
        SearchStopReason.LOCAL_SEARCH_COMPLETE,
    ),
)
def test_search_stop_does_not_block_reserved_audit_time_or_consume_candidates(reason):
    runtime = budget(candidate_check_limit=1, candidate_check_count=1, clock=lambda: 105.0)
    runtime.stop_reason = reason
    values = audit_case(runtime=runtime)
    outcome = final_audit.audit_core_without_search_cache(*values)
    assert outcome.report.passed
    assert runtime.stop_reason is reason and runtime.candidate_check_count == 1


@pytest.mark.parametrize("stop", ("cancel", "deadline"))
def test_audit_stopped_at_entry_does_not_evaluate_or_create_partial_results(stop):
    token = Stop()
    token.active = stop == "cancel"
    runtime = budget(cancellation=token, clock=lambda: 110.0 if stop == "deadline" else 1.0)
    values = audit_case(runtime=runtime)
    before = fingerprint(values[:3])
    with patch.object(
        final_audit, "evaluate_plan", side_effect=AssertionError("must not evaluate")
    ):
        outcome = final_audit.audit_core_without_search_cache(*values)
    assert_unfinished(
        outcome, CoreAuditStatus.CANCELLED if stop == "cancel" else CoreAuditStatus.TIME_LIMIT
    )
    assert fingerprint(values[:3]) == before and runtime.candidate_check_count == 0


@pytest.mark.parametrize("stop", ("cancel", "deadline"))
def test_structure_scan_checks_reserved_budget_before_entering_complete_evaluation(stop):
    calls = []

    class DuringScan:
        def is_cancelled(self):
            calls.append("cancel")
            return len(calls) >= 4

    def clock():
        calls.append("clock")
        return 110.0 if len(calls) >= 4 else 1.0

    runtime = budget(cancellation=DuringScan()) if stop == "cancel" else budget(clock=clock)
    values = audit_case(nodes=tuple(node(f"order-{index}") for index in range(8)), runtime=runtime)
    with patch.object(
        final_audit, "evaluate_plan", side_effect=AssertionError("must not evaluate")
    ):
        outcome = final_audit.audit_core_without_search_cache(*values)
    assert len(calls) == 4
    assert_unfinished(
        outcome, CoreAuditStatus.CANCELLED if stop == "cancel" else CoreAuditStatus.TIME_LIMIT
    )
    assert runtime.candidate_check_count == 0


@pytest.mark.parametrize("stop", ("cancel", "deadline"))
def test_stop_after_split_authorization_prevents_full_evaluation(stop, monkeypatch):
    candidate, problem, rule_set, _ = accepted_split_case(monkeypatch)
    token, now = Stop(), [1.0]
    runtime = budget(cancellation=token, clock=lambda: now[0])
    original = ProcessRuleSet.evaluate_controlled_split
    calls = []

    def stopping(active_rules, subject, context):
        calls.append(subject)
        decision = original(active_rules, subject, context)
        token.active = stop == "cancel"
        now[0] = 110.0 if stop == "deadline" else 1.0
        return decision

    monkeypatch.setattr(ProcessRuleSet, "evaluate_controlled_split", stopping)
    with patch.object(
        final_audit, "evaluate_plan", side_effect=AssertionError("must not evaluate")
    ):
        outcome = final_audit.audit_core_without_search_cache(candidate, problem, rule_set, runtime)
    assert len(calls) == 1
    assert_unfinished(
        outcome, CoreAuditStatus.CANCELLED if stop == "cancel" else CoreAuditStatus.TIME_LIMIT
    )
    assert runtime.candidate_check_count == 0


@pytest.mark.parametrize("stop", ("cancel", "deadline"))
def test_stop_after_complete_evaluation_discards_all_audit_material(stop, monkeypatch):
    token, now = Stop(), [1.0]
    runtime = budget(cancellation=token, clock=lambda: now[0])
    values = audit_case(runtime=runtime)
    original = final_audit.evaluate_plan
    observed = []

    def stopping(*args):
        result = original(*args)
        observed.append(result)
        token.active = stop == "cancel"
        now[0] = 110.0 if stop == "deadline" else 1.0
        return result

    monkeypatch.setattr(final_audit, "evaluate_plan", stopping)
    before = fingerprint(values[:3])
    outcome = final_audit.audit_core_without_search_cache(*values)
    assert len(observed) == 1
    assert_unfinished(
        outcome, CoreAuditStatus.CANCELLED if stop == "cancel" else CoreAuditStatus.TIME_LIMIT
    )
    assert fingerprint(values[:3]) == before and runtime.candidate_check_count == 0


@pytest.mark.parametrize("stage", ("authorization", "evaluation"))
def test_audit_exception_is_an_unfinished_error_not_a_business_rejection(stage, monkeypatch):
    values = accepted_split_case(monkeypatch) if stage == "authorization" else audit_case()
    before = fingerprint(values[:3])

    def fail(*args):
        raise RuntimeError("independent audit failure")

    if stage == "authorization":
        monkeypatch.setattr(ProcessRuleSet, "evaluate_controlled_split", fail)
    else:
        monkeypatch.setattr(final_audit, "evaluate_plan", fail)
    outcome = final_audit.audit_core_without_search_cache(*values)
    assert_unfinished(outcome, CoreAuditStatus.ERROR)
    assert "independent audit failure" in " ".join(item.message for item in outcome.issues)
    assert fingerprint(values[:3]) == before
