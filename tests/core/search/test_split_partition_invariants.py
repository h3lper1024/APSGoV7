"""The shared candidate boundary accepts only a complete, freshly authorized split."""

from dataclasses import replace
from decimal import Decimal

import pytest

from apsgo_scheduler.core import controlled_split
from apsgo_scheduler.core.contracts import ControlledSplitMode, fingerprint
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import SchedulePlan, VirtualPurpose
from apsgo_scheduler.core.neighborhoods import try_complete_candidate
from apsgo_scheduler.core.rules.rule_set import ProcessRuleSet
from tests.core.search.test_controlled_order_split import (
    capture_proposal,
    parent,
    split_case,
    suppress_replay,
)

D = Decimal


def rejected(state, context, chains, options):
    before = fingerprint(state)
    assert not try_complete_candidate(state, context, chains, **options)
    assert fingerprint(state) == before
    assert context.accepted_move_traces == ()
    assert context.complete_candidate_evaluation_count == 0


@pytest.mark.parametrize("missing", ("split_subject", "split_decision"))
def test_split_authorization_arguments_must_be_supplied_together(missing, monkeypatch):
    state, context, chains, options = capture_proposal(monkeypatch)
    options.pop(missing)
    before = fingerprint(state)
    with pytest.raises(ValueError):
        try_complete_candidate(state, context, chains, **options)
    assert fingerprint(state) == before
    assert context.complete_candidate_evaluation_count == 0


@pytest.mark.parametrize("invalid", ("split_subject", "split_decision"))
def test_split_authorization_arguments_reject_wrong_types(invalid, monkeypatch):
    state, context, chains, options = capture_proposal(monkeypatch)
    options[invalid] = object()
    with pytest.raises(ValueError):
        try_complete_candidate(state, context, chains, **options)
    assert state.split_sequence == context.complete_candidate_evaluation_count == 0


def test_ordinary_candidate_without_explicit_split_authorization_still_preserves_real_values(
    monkeypatch,
):
    state, context, chains, options = capture_proposal(monkeypatch)
    options.pop("split_subject")
    options.pop("split_decision")
    rejected(state, context, chains, options)


@pytest.mark.parametrize(
    "changed", ("count", "origin", "subject_parent", "decision_limits", "decision_fingerprint")
)
def test_stale_or_mutated_authorization_is_rejected_before_complete_evaluation(
    changed, monkeypatch
):
    state, context, chains, options = capture_proposal(monkeypatch)
    subject, decision = options["split_subject"], options["split_decision"]
    if changed == "count":
        options["split_subject"] = replace(subject, accepted_split_source_count=1)
    elif changed == "origin":
        options["split_subject"] = replace(subject, origin_assigned_period="P9")
    elif changed == "subject_parent":
        options["split_subject"] = replace(
            subject, parent_node=replace(subject.parent_node, width=D(890))
        )
    elif changed == "decision_limits":
        options["split_decision"] = replace(decision, maximum_separator_weight=D(100))
    else:
        options["split_decision"] = replace(decision, decision_fingerprint="stale")
    rejected(state, context, chains, options)


def test_same_fingerprint_does_not_replace_an_actual_rule_authorization_recheck(monkeypatch):
    state, context, chains, options = capture_proposal(monkeypatch)
    original = ProcessRuleSet.evaluate_controlled_split

    def different_decision(rule_set, subject, evaluation_context):
        decision = original(rule_set, subject, evaluation_context)
        return replace(decision, maximum_separator_weight=D(100))

    monkeypatch.setattr(ProcessRuleSet, "evaluate_controlled_split", different_decision)
    rejected(state, context, chains, options)


def test_reauthorization_exception_propagates_without_publishing_the_partition(monkeypatch):
    state, context, chains, options = capture_proposal(monkeypatch)
    before = fingerprint(state)

    def fail(*args):
        raise RuntimeError("authorization unavailable")

    monkeypatch.setattr(ProcessRuleSet, "evaluate_controlled_split", fail)
    with pytest.raises(RuntimeError, match="authorization unavailable"):
        try_complete_candidate(state, context, chains, **options)
    assert fingerprint(state) == before
    assert state.split_sequence == context.complete_candidate_evaluation_count == 0


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_piece",
        "duplicate_piece_index",
        "piece_order",
        "wrong_sum",
        "nonmaximum_prefix",
        "missing_separator",
        "extra_separator",
        "separator_purpose",
        "separator_partition",
        "separator_prototype",
        "separator_overweight",
        "parent_retained",
    ),
)
def test_incomplete_or_noncanonical_partition_cannot_enter_full_evaluation(mutation, monkeypatch):
    state, context, chains, options = capture_proposal(monkeypatch)
    nodes = list(chains[-1].nodes)
    if mutation == "missing_piece":
        nodes.pop()
    elif mutation == "duplicate_piece_index":
        nodes[2] = replace(nodes[2], split_lineage=replace(nodes[2].split_lineage, piece_index=1))
    elif mutation == "piece_order":
        nodes[0], nodes[2] = nodes[2], nodes[0]
    elif mutation == "wrong_sum":
        nodes[0] = replace(nodes[0], weight=D(49))
    elif mutation == "nonmaximum_prefix":
        nodes[0], nodes[-1] = replace(nodes[0], weight=D(49)), replace(nodes[-1], weight=D(21))
    elif mutation == "missing_separator":
        nodes.pop(1)
    elif mutation == "extra_separator":
        nodes.append(
            replace(
                nodes[1],
                node_id="extra-separator",
                virtual_lineage=replace(nodes[1].virtual_lineage, accepted_sequence=3),
            )
        )
        options["virtual_sequence"] = 3
    elif mutation == "separator_purpose":
        nodes[1] = replace(
            nodes[1],
            virtual_lineage=replace(
                nodes[1].virtual_lineage,
                purpose=VirtualPurpose.EDGE_BRIDGE,
                related_partition_id=None,
            ),
        )
    elif mutation == "separator_partition":
        nodes[1] = replace(
            nodes[1],
            virtual_lineage=replace(nodes[1].virtual_lineage, related_partition_id="foreign"),
        )
    elif mutation == "separator_prototype":
        nodes[1] = replace(
            nodes[1], virtual_lineage=replace(nodes[1].virtual_lineage, prototype_id="foreign")
        )
    elif mutation == "separator_overweight":
        nodes[1] = replace(nodes[1], weight=D(21))
    else:
        nodes.append(options["split_subject"].parent_node)
    changed = (*chains[:-1], replace(chains[-1], nodes=tuple(nodes)))
    rejected(state, context, changed, options)


@pytest.mark.parametrize(
    "field,value",
    (
        ("width", D(890)),
        ("thickness", D(2)),
        ("min_temperature", D(690)),
        ("max_temperature", D(910)),
        ("grade", "changed"),
        ("rule_attributes", {"grade_class": "OTHER"}),
    ),
)
def test_pieces_preserve_every_original_real_physical_field(field, value, monkeypatch):
    state, context, chains, options = capture_proposal(monkeypatch)
    partition = chains[-1]
    nodes = (replace(partition.nodes[0], **{field: value}), *partition.nodes[1:])
    rejected(state, context, (*chains[:-1], replace(partition, nodes=nodes)), options)


@pytest.mark.parametrize(
    "field,value",
    (
        ("parent_node_id", "foreign"),
        ("authorization_rule_id", "foreign"),
        ("authorization_rule_version", "foreign"),
        ("authorization_decision_fingerprint", "foreign"),
        ("accepted_source_sequence", 2),
        ("partition_id", "foreign"),
        ("reason_code", "foreign"),
    ),
)
def test_all_piece_lineage_fields_must_equal_the_same_authorized_partition(
    field, value, monkeypatch
):
    state, context, chains, options = capture_proposal(monkeypatch)
    partition = chains[-1]
    piece = replace(
        partition.nodes[0],
        split_lineage=replace(partition.nodes[0].split_lineage, **{field: value}),
    )
    rejected(
        state,
        context,
        (*chains[:-1], replace(partition, nodes=(piece, *partition.nodes[1:]))),
        options,
    )


def test_changing_all_partition_modes_consistently_still_cannot_override_the_decision(monkeypatch):
    state, context, chains, options = capture_proposal(monkeypatch)
    partition = chains[-1]
    nodes = tuple(
        replace(
            item,
            split_lineage=replace(
                item.split_lineage,
                split_mode=ControlledSplitMode.FUTURE_BORROW_RETURN,
                origin_assigned_period="P9",
            ),
        )
        if item.split_lineage
        else item
        for item in partition.nodes
    )
    rejected(state, context, (*chains[:-1], replace(partition, nodes=nodes)), options)


@pytest.mark.parametrize(
    "mutation", ("remove_anchor", "change_anchor", "move_anchor_into_partition")
)
def test_authorizing_one_parent_does_not_authorize_other_real_changes(mutation, monkeypatch):
    state, context, chains, options = capture_proposal(monkeypatch, origin="P9", anchor=True)
    donor, partition = chains
    if mutation == "remove_anchor":
        changed = (partition,)
    elif mutation == "change_anchor":
        changed = (replace(donor, nodes=(replace(donor.nodes[0], weight=D(9)),)), partition)
    else:
        changed = (replace(partition, nodes=(*partition.nodes, *donor.nodes)),)
    rejected(state, context, changed, options)


def test_action_cannot_split_a_mutated_parent_even_with_fresh_authorization(monkeypatch):
    state, context = split_case()
    donor = state.current_plan.chains[0]
    changed_parent = replace(donor.nodes[0], width=D(890))
    plan = SchedulePlan((replace(donor, nodes=(changed_parent,)),))
    state = replace(
        state,
        current_plan=plan,
        current_evaluation=evaluate_plan(
            plan, context.factory.cache.rule_set, context.factory.cache.context
        ),
    )
    before = fingerprint(state)
    replay = suppress_replay(monkeypatch)
    controlled_split.run_controlled_order_split(state, context)
    assert fingerprint(state) == before and replay == []
    assert context.factory.budget.candidate_check_count == 1
    assert context.complete_candidate_evaluation_count == 0


def test_rejected_attempt_count_does_not_change_private_partition_or_node_identity(monkeypatch):
    state1, context1, chains1, options1 = capture_proposal(monkeypatch)
    state2, context2, chains2, options2 = capture_proposal(
        monkeypatch,
        runtime=replace(context1.factory.budget, candidate_check_count=7),
    )
    assert chains1 == chains2 and options1 == options2
    assert fingerprint(chains1) == fingerprint(chains2)
    assert state1.split_sequence == state2.split_sequence == 0
    assert context2.factory.budget.candidate_check_count == 8


def test_partition_identity_binds_original_fields_not_only_source_identity(monkeypatch):
    _, _, original_chains, _ = capture_proposal(monkeypatch)
    _, _, changed_chains, _ = capture_proposal(monkeypatch, parents=(parent(width=D(890)),))
    first = original_chains[-1].nodes[0]
    changed = changed_chains[-1].nodes[0]
    assert first.source_order_id == changed.source_order_id
    assert first.split_lineage.partition_id != changed.split_lineage.partition_id
    assert first.node_id != changed.node_id
