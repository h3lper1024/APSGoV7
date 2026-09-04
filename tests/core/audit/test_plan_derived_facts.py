"""Final audit alone derives immutable, source-conserving resource facts."""

import ast
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal, Inexact, localcontext
from pathlib import Path

import pytest

from apsgo_scheduler.core import final_audit
from apsgo_scheduler.core.contracts import (
    ControlledSplitMode,
    CoreAuditStatus,
    CoreCandidateSnapshot,
    fingerprint,
    sum_weights,
)
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import Chain, MaterialRole, SchedulePlan, VirtualPurpose
from apsgo_scheduler.core.resource_facts import PlanDerivedFacts
from apsgo_scheduler.core.virtual_material import VirtualFactory
from tests.core.audit.test_final_audit_without_cache import (
    accepted_split_case,
    audit_case,
    evaluation_context,
)
from tests.core.graph.test_construction_order import node
from tests.core.search.test_controlled_order_split import parent
from tests.core.search.test_post_split_single_replay import gqga4_split_start, run_gqga4_split
from tests.core.search.test_virtual_material_factory import make_factory, prototype

D = Decimal
FUTURE = ControlledSplitMode.FUTURE_BORROW_RETURN


@pytest.fixture
def mixed_case(monkeypatch):
    return accepted_split_case(
        monkeypatch,
        parents=(
            parent("split"),
            parent(
                "borrowed-real", weight="3", source="P1", rule_attributes={"grade_class": "OTHER"}
            ),
            parent(
                "transition", weight="7", source="P1", material_role=MaterialRole.ACTUAL_TRANSITION
            ),
        ),
        origin="P9",
        anchor=True,
    )


def test_all_five_fact_collections_follow_final_positions_and_authoritative_sources(mixed_case):
    candidate, problem, _, _ = mixed_case
    result = final_audit.audit_core_without_search_cache(*mixed_case)
    facts = result.resource_facts
    assert result.report.passed and isinstance(facts, PlanDerivedFacts)
    positions = tuple(
        (chain, position, item)
        for chain in candidate.plan.chains
        for position, item in enumerate(chain.nodes)
    )
    assert len(facts.assignments) == len(positions) == 8
    for fact, (chain, position, item) in zip(facts.assignments, positions):
        assert (fact.node_id, fact.source_order_id, fact.source_resource_id) == (
            item.node_id,
            item.source_order_id,
            item.source_resource_id,
        )
        assert (fact.chain_id, fact.assigned_period, fact.position) == (
            chain.chain_id,
            chain.assigned_period,
            position,
        )
        assert fact.material_role is item.material_role and fact.weight == item.weight
    assert [
        (fact.node_id, fact.source_period, fact.assigned_period, fact.weight)
        for fact in facts.future_borrows
    ] == [("borrowed-real", "P1", "P9", D(3)), ("transition", "P1", "P9", D(7))]
    assert len(facts.actual_transitions) == 1
    transition = facts.actual_transitions[0]
    assert (transition.node_id, transition.source_order_id, transition.source_resource_id) == (
        "transition",
        "transition",
        "transition",
    )
    assert transition.assigned_period == "P9" and transition.weight == D(7)
    assert len(facts.virtual_generations) == 2 and len(facts.split_partitions) == 1
    partition = facts.split_partitions[0]
    assert partition.split_mode is FUTURE
    assert (partition.origin_assigned_period, partition.target_assigned_period) == ("P9", "P2")
    assert partition.parent_node_id == partition.parent_source_order_id == "split"
    assert partition.source_resource_id == "split" and partition.source_period == "P2"
    assert partition.piece_weights == (D(50), D(50), D(20))
    assert partition.parent_weight == D(120) and partition.accepted_source_sequence == 1
    assert partition.partition_fingerprint == fingerprint(partition.fingerprint_payload())
    assert (
        tuple(item.node_id for item in facts.virtual_generations)
        == partition.separator_virtual_node_ids
    )
    assert all(
        item.prototype_id == "p"
        and item.purpose is VirtualPurpose.SPLIT_SEPARATOR
        and item.related_partition_id == partition.partition_id
        and item.weight == D(5)
        for item in facts.virtual_generations
    )
    assert tuple(item.accepted_sequence for item in facts.virtual_generations) == (1, 2)
    assert facts.input_real_weight == facts.scheduled_real_weight == D(140)
    assert facts.generated_virtual_weight == facts.borrowed_future_weight == D(10)
    assert facts.future_pool_weight == D(130)
    assert sum_weights(chain.total_weight for chain in candidate.plan.chains) == D(150)
    for original in problem.nodes:
        assert (
            sum_weights(
                fact.weight
                for fact in facts.assignments
                if fact.source_resource_id == original.source_resource_id
            )
            == original.weight
        )
    assert (
        result.report.audited_split_count,
        result.report.audited_same_period_split_count,
        result.report.audited_future_borrow_return_count,
    ) == (1, 0, 1)


def test_partition_piece_order_is_logical_even_after_search_reorders_the_chain(mixed_case):
    candidate, problem, rules, runtime = mixed_case
    original = final_audit.audit_core_without_search_cache(
        *mixed_case
    ).resource_facts.split_partitions[0]
    chains = (
        *candidate.plan.chains[:-1],
        replace(candidate.plan.chains[-1], nodes=tuple(reversed(candidate.plan.chains[-1].nodes))),
    )
    plan = SchedulePlan(chains)
    changed = CoreCandidateSnapshot(plan, evaluate_plan(plan, rules, evaluation_context(problem)))
    outcome = final_audit.audit_core_without_search_cache(changed, problem, rules, runtime)
    assert outcome.report.passed
    fact = outcome.resource_facts.split_partitions[0]
    assert fact.piece_node_ids == original.piece_node_ids
    assert fact.piece_weights == original.piece_weights
    assert fact.separator_virtual_node_ids == original.separator_virtual_node_ids
    assert fact.partition_fingerprint == original.partition_fingerprint
    assert fact.partition_fingerprint == fingerprint(fact.fingerprint_payload())
    assert tuple(item.node_id for item in outcome.resource_facts.assignments)[-5:] == tuple(
        item.node_id for item in plan.chains[-1].nodes
    )


def test_facts_are_immutable_repeatable_and_hash_every_fact_field(mixed_case):
    first = final_audit.audit_core_without_search_cache(*mixed_case)
    second = final_audit.audit_core_without_search_cache(*mixed_case)
    facts = first.resource_facts
    assert first.report.passed and second.report.passed
    assert facts == second.resource_facts and facts is not second.resource_facts
    payload = {
        field.name: getattr(facts, field.name)
        for field in fields(facts)
        if field.name != "facts_fingerprint"
    }
    assert facts.facts_fingerprint == fingerprint(payload)
    assert first.report.derived_resource_fingerprint == facts.facts_fingerprint
    assert first.report.report_fingerprint == second.report.report_fingerprint
    assert first.report.audited_evaluation_fingerprint == fingerprint(first.audited_evaluation)
    with pytest.raises(FrozenInstanceError):
        facts.scheduled_real_weight = D(0)
    with pytest.raises(FrozenInstanceError):
        facts.assignments[0].weight = D(0)
    for key in payload:
        value = payload[key]
        modified = value + (value[0],) if isinstance(value, tuple) else value + D(1)
        assert fingerprint(payload | {key: modified}) != facts.facts_fingerprint
    assert "release_fingerprint" not in payload and "elapsed_seconds" not in payload


def test_exact_resource_totals_do_not_inherit_callers_decimal_context():
    exact = D("10000000000000000000000000000.1")
    case = audit_case(
        nodes=(node("large", weight="9999999999999999999999999999"), node("small", weight="1.1"))
    )
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        outcome = final_audit.audit_core_without_search_cache(*case)
    assert outcome.report.passed
    assert (
        outcome.resource_facts.input_real_weight
        == outcome.resource_facts.scheduled_real_weight
        == exact
    )
    assert outcome.resource_facts.generated_virtual_weight == D(0)
    assert outcome.resource_facts.split_partitions == ()
    assert (
        outcome.report.audited_split_count,
        outcome.report.audited_same_period_split_count,
        outcome.report.audited_future_borrow_return_count,
    ) == (0, 0, 0)


def test_virtual_temperature_is_not_regenerated_from_its_final_neighbors(monkeypatch):
    left, right = node("left", temperature="600"), node("right", temperature="620")
    factory = make_factory((prototype("p"),), nodes=(left, right))
    generated = factory.materialize(
        factory.cache.problem.virtual_prototypes[0],
        left,
        right,
        purpose=VirtualPurpose.WEIGHT_FILL,
        sequence=1,
    )
    case = audit_case(
        nodes=(left, right),
        prototypes=factory.cache.problem.virtual_prototypes,
        chains=(Chain("moved", (generated, right, left), "period"),),
    )
    # The single final neighbor would produce a different lower temperature (620).
    assert generated.min_temperature == D(600) and right.min_temperature == D(620)
    before = fingerprint(case[0])
    monkeypatch.setattr(
        VirtualFactory,
        "materialize",
        lambda *a, **k: pytest.fail("audit rematerialized a virtual node"),
    )
    result = final_audit.audit_core_without_search_cache(*case)
    assert result.report.passed and fingerprint(case[0]) == before
    assert result.resource_facts.virtual_generations[0].node_id == generated.node_id
    assert not hasattr(result.resource_facts.virtual_generations[0], "min_temperature")


def test_only_final_audit_calls_the_private_fact_derivation():
    package = Path(__file__).resolve().parents[3] / "src" / "apsgo_scheduler"
    callers = set()
    for source in package.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        if any(
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Name)
            and item.func.id == "_derive_audited_resource_facts"
            for item in ast.walk(tree)
        ):
            callers.add(source.relative_to(package).as_posix())
    assert callers == {"core/final_audit.py"}


@pytest.fixture(scope="module")
def gqga4_audited():
    start = gqga4_split_start.__wrapped__()
    state, context, _, _ = run_gqga4_split(start, future_only=False)
    assert (
        fingerprint(state.current_plan)
        == "fa30b09955a46827d854e54c3df30760efc3c6784cfe77e3ed9eb009af59b498"
    )
    candidate = CoreCandidateSnapshot(state.current_plan, state.current_evaluation)
    outcome = final_audit.audit_core_without_search_cache(
        candidate,
        context.factory.cache.problem,
        context.factory.cache.rule_set,
        context.factory.budget,
    )
    return candidate, context, outcome


def test_actual_gqga4_facts_close_the_full_source_partition(gqga4_audited):
    candidate, context, outcome = gqga4_audited
    assert outcome.report.status is CoreAuditStatus.COMPLETED and outcome.report.passed
    facts = outcome.resource_facts
    assert len(facts.assignments) == 553
    assert len(facts.virtual_generations) == 20 and len(facts.actual_transitions) == 61
    assert len(facts.split_partitions) == 2
    assert facts.input_real_weight == facts.scheduled_real_weight == D("29333.91")
    assert facts.generated_virtual_weight == D(400)
    assert facts.future_pool_weight == D("23543.54")
    assert facts.borrowed_future_weight == D("22860.88")
    assert sum_weights(chain.total_weight for chain in candidate.plan.chains) == D("29733.91")
    for original in context.factory.cache.problem.nodes:
        assert (
            sum_weights(
                item.weight
                for item in facts.assignments
                if item.source_order_id == original.source_order_id
            )
            == original.weight
        )
    assert (
        outcome.report.audited_split_count,
        outcome.report.audited_same_period_split_count,
        outcome.report.audited_future_borrow_return_count,
    ) == (2, 2, 0)
    assert [item.accepted_source_sequence for item in facts.split_partitions] == [1, 2]
    assert [item.piece_weights for item in facts.split_partitions] == [
        (D(500), D("70.3")),
        (D(500), D(100)),
    ]
    assert all(
        item.partition_fingerprint == fingerprint(item.fingerprint_payload())
        for item in facts.split_partitions
    )


def test_actual_gqga4_allowed_underweight_is_not_a_zero_underweight_gate(gqga4_audited):
    _, context, outcome = gqga4_audited
    assert outcome.report.passed and outcome.report.search_evaluation_matches
    assert outcome.audited_evaluation.quality_key == (0, D(0), 2, D("204.42"), 22, D(400))
    assert context.factory.budget.candidate_check_count == 100000
    assert outcome.audited_evaluation.metrics["underweight_chain_count"] != 0
    assert not hasattr(outcome, "release")
