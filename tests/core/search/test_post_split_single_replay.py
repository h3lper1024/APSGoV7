"""The split phase alone owns natural continuation and one conditional replay."""

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apsgo_scheduler.app.rule_set_loader import fingerprint_rule_set_spec, load_rule_set
from apsgo_scheduler.core import controlled_split, neighborhoods
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import ControlledSplitMode, SearchStopReason, fingerprint
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import MaterialRole, SearchState, VirtualPurpose
from apsgo_scheduler.core.neighborhoods import SearchContext
from apsgo_scheduler.core.rules.rule_set import ProcessRuleSet
from apsgo_scheduler.core.virtual_material import VirtualFactory
from tests.app.test_input_normalizer import gqga4_six_level_spec
from tests.core.graph.test_bipartite_matching import budget
from tests.core.search.test_controlled_order_split import parent, split_case
from tests.core.search.test_virtual_fill_reference_trace import fill_stage, fill_start

D = Decimal
COMPLETE = SearchStopReason.LOCAL_SEARCH_COMPLETE
SAME = ControlledSplitMode.SAME_PERIOD_SPLIT
FUTURE = ControlledSplitMode.FUTURE_BORROW_RETURN

# Original solver, identical current-rule starting plan, changing only its
# cancelled reverse-width carrier filter to the existing {'*'} option.
# check, action, underweight count, gap, chain count, ordered physical signature
FUTURE_ONLY_REFERENCE = (
    (
        86414,
        "controlled_order_split",
        3,
        "404.12",
        23,
        "632fcb3771bd75605e890b8844b8e53c1205f1800c3ba4f9cc553d7c99165a23",
    ),
    (
        86692,
        "whole_chain_insertion",
        2,
        "274.42",
        22,
        "7896d76137a278b43cfea117207b74ff5d0e4ef7cfc0f0cf7b2c8c75d4b7bf43",
    ),
    (
        91573,
        "real_node_relocation",
        2,
        "254.42",
        22,
        "7ebe9408a54504bf3ff65669850f69146bb5b6ab20373e1286ada8bb339964de",
    ),
    (
        94671,
        "real_node_relocation",
        2,
        "230.42",
        22,
        "04c080823121c93d95a082470df7972f4b0a6724865ae6617a02ba8be168cba5",
    ),
    (
        97898,
        "real_node_relocation",
        2,
        "204.42",
        22,
        "26bd8a27ac1dab79b3a49540a2c443ffdd3c88e19fc264ab6660581802638231",
    ),
)


@pytest.mark.parametrize(
    "reason", tuple(reason for reason in SearchStopReason if reason is not COMPLETE)
)
def test_actual_stop_is_never_cleared_or_followed_by_a_replay(reason, monkeypatch):
    state, context = split_case()
    runtime = context.factory.budget
    runtime.candidate_check_count = 19
    runtime.stop_reason = reason
    before = fingerprint(state)
    monkeypatch.setattr(
        controlled_split, "run_local_search", lambda *_: pytest.fail("unexpected replay")
    )
    assert controlled_split.run_controlled_order_split(state, context) is state
    assert runtime.stop_reason is reason and runtime.candidate_check_count == 19
    assert fingerprint(state) == before


@pytest.mark.parametrize(
    "changes",
    (
        {"enabled": False},
        {"rule_changes": {"allowed_modes": []}},
        {"parents": (parent(weight="50"),)},
        {"narrow_enabled": False},
    ),
)
def test_no_accepted_split_finishes_without_replay(changes, monkeypatch):
    state, context = split_case(**changes)
    runtime = context.factory.budget
    runtime.stop_reason = COMPLETE
    before = fingerprint(state)
    monkeypatch.setattr(
        controlled_split, "run_local_search", lambda *_: pytest.fail("unexpected replay")
    )
    assert controlled_split.run_controlled_order_split(state, context) is state
    assert runtime.stop_reason is COMPLETE
    assert fingerprint(state) == before
    assert context.accepted_move_traces == ()


@pytest.mark.parametrize("stop", ("cancel", "time"))
def test_reopening_natural_completion_immediately_rechecks_real_stops(stop, monkeypatch):
    token = SimpleNamespace(is_cancelled=lambda: stop == "cancel")
    runtime = budget(candidate_check_limit=100, cancellation=token)
    if stop == "time":
        runtime.clock = lambda: runtime.search_deadline_monotonic
    state, context = split_case(runtime=runtime)
    runtime.stop_reason = COMPLETE
    before = fingerprint(state)
    monkeypatch.setattr(
        controlled_split, "run_local_search", lambda *_: pytest.fail("unexpected replay")
    )
    controlled_split.run_controlled_order_split(state, context)
    assert runtime.stop_reason is (
        SearchStopReason.USER_CANCELLED
        if stop == "cancel"
        else SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    )
    assert fingerprint(state) == before and runtime.candidate_check_count == 0


def test_accepted_split_replays_once_with_same_allowance_and_never_rescans_afterward(monkeypatch):
    state, context = split_case()
    runtime = context.factory.budget
    runtime.candidate_check_count = 20
    context.complete_candidate_evaluation_count = 2
    runtime.stop_reason = COMPLETE
    timing = (
        runtime.started_at_monotonic,
        runtime.search_deadline_monotonic,
        runtime.final_deadline_monotonic,
    )
    replay_calls = []
    original_evaluate = ProcessRuleSet.evaluate_controlled_split
    original_replay = controlled_split.run_local_search

    def authorize(rules, subject, evaluation_context):
        assert not replay_calls, "a second split scan must not follow the replay"
        return original_evaluate(rules, subject, evaluation_context)

    def replay(current, current_context):
        assert current is state and current_context is context
        assert current_context.factory.budget is runtime
        assert runtime.candidate_check_count == 21 and runtime.stop_reason is None
        assert context.complete_candidate_evaluation_count == 3
        assert state.split_sequence == state.accepted_same_period_split_count == 1
        replay_calls.append(state.current_plan)
        return original_replay(current, current_context)

    monkeypatch.setattr(ProcessRuleSet, "evaluate_controlled_split", authorize)
    monkeypatch.setattr(controlled_split, "run_local_search", replay)
    assert controlled_split.run_controlled_order_split(state, context) is state
    assert len(replay_calls) == 1
    assert runtime.candidate_check_count == 21 and runtime.stop_reason is COMPLETE
    assert context.complete_candidate_evaluation_count == 3
    assert state.accepted_move_count == state.split_sequence == 1
    assert state.virtual_sequence == 2 and state.accepted_future_borrow_return_count == 0
    assert timing == (
        runtime.started_at_monotonic,
        runtime.search_deadline_monotonic,
        runtime.final_deadline_monotonic,
    )
    assert [item.action_name for item in context.accepted_move_traces] == ["controlled_order_split"]


def test_cancel_after_atomic_split_keeps_acceptance_but_skips_replay(monkeypatch):
    signal = SimpleNamespace(cancelled=False)
    runtime = budget(
        candidate_check_limit=100,
        cancellation=SimpleNamespace(is_cancelled=lambda: signal.cancelled),
    )
    state, context = split_case(runtime=runtime)
    runtime.stop_reason = COMPLETE
    original = SearchState.commit_accepted

    def commit(current, *args, **kwargs):
        original(current, *args, **kwargs)
        signal.cancelled = True

    monkeypatch.setattr(SearchState, "commit_accepted", commit)
    monkeypatch.setattr(
        controlled_split, "run_local_search", lambda *_: pytest.fail("unexpected replay")
    )
    controlled_split.run_controlled_order_split(state, context)
    assert runtime.stop_reason is SearchStopReason.USER_CANCELLED
    assert runtime.candidate_check_count == 1
    assert state.split_sequence == state.accepted_move_count == 1 and state.virtual_sequence == 2
    assert len(context.accepted_move_traces) == 1


@pytest.mark.parametrize("location", ("qualification", "replay"))
def test_exceptions_do_not_restore_a_false_natural_completion(location, monkeypatch):
    state, context = split_case()
    context.factory.budget.stop_reason = COMPLETE

    def fail(*_):
        raise RuntimeError("deliberate stage failure")

    if location == "qualification":
        monkeypatch.setattr(ProcessRuleSet, "evaluate_controlled_split", fail)
    else:
        monkeypatch.setattr(controlled_split, "run_local_search", fail)
    with pytest.raises(RuntimeError, match="deliberate stage failure"):
        controlled_split.run_controlled_order_split(state, context)
    assert context.factory.budget.stop_reason is None
    assert state.split_sequence == int(location == "replay")


def semantic_signature(plan):
    """Compare physical decisions while omitting different private node identities."""

    def key(node):
        physical = (node.width, node.thickness, node.min_temperature, node.max_temperature)
        if node.material_role is MaterialRole.GENERATED_VIRTUAL:
            return ("virtual", node.virtual_lineage.prototype_id, node.weight, *physical)
        if node.split_lineage is not None:
            lineage = node.split_lineage
            return (
                "split",
                node.source_order_id,
                node.weight,
                lineage.piece_index,
                lineage.piece_count,
                node.source_period,
                lineage.origin_assigned_period,
                lineage.split_mode.value,
                *physical,
            )
        return node.node_id

    return tuple(
        (chain.assigned_period, tuple(key(node) for node in chain.nodes)) for chain in plan.chains
    )


@pytest.fixture(scope="module")
def gqga4_split_start():
    before = fill_start.__wrapped__()
    state, context, _ = fill_stage.__wrapped__(before)
    assert (
        fingerprint(state.current_plan)
        == "cef0f1b44f54e46bab0a64871e5aecb74c489078b937c37494df989af74cec86"
    )
    assert state.accepted_move_count == 35 and state.virtual_sequence == 18
    return before[0], state.current_plan, context


def run_gqga4_split(start, *, future_only):
    _, plan, previous = start
    spec = gqga4_six_level_spec.__wrapped__()
    if future_only:
        spec = replace(
            spec,
            rules=tuple(
                replace(rule, parameters=dict(rule.parameters) | {"allowed_modes": [FUTURE.value]})
                if rule.rule_id == "controlled_order_split"
                else rule
                for rule in spec.rules
            ),
        )
        spec = replace(spec, fingerprint=fingerprint_rule_set_spec(spec))
    rules = load_rule_set(spec)
    cache = RuleEdgeDecisionCache(
        previous.factory.cache.problem, rules, previous.factory.cache.context
    )
    runtime = SolveRuntimeBudget.from_policy(previous.policy, 0, clock=lambda: 1.0)
    runtime.candidate_check_count = 86413
    runtime.stop_reason = COMPLETE
    context = SearchContext(VirtualFactory(cache, runtime), previous.policy)
    state = SearchState(
        plan, evaluate_plan(plan, rules, cache.context), accepted_move_count=35, virtual_sequence=18
    )
    records = []
    replays = []
    original_candidate = neighborhoods.try_complete_candidate
    original_replay = controlled_split.run_local_search

    def candidate(current, current_context, chains, **kwargs):
        accepted = original_candidate(current, current_context, chains, **kwargs)
        if accepted:
            records.append((context.accepted_move_traces[-1], state.current_plan))
        return accepted

    def replay(current, current_context):
        replays.append((runtime.candidate_check_count, state.split_sequence))
        return original_replay(current, current_context)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(neighborhoods, "try_complete_candidate", candidate)
        patch.setattr(controlled_split, "try_complete_candidate", candidate)
        patch.setattr(controlled_split, "run_local_search", replay)
        assert controlled_split.run_controlled_order_split(state, context) is state
    assert fingerprint(plan) == "cef0f1b44f54e46bab0a64871e5aecb74c489078b937c37494df989af74cec86"
    return state, context, tuple(records), tuple(replays)


@pytest.fixture(scope="module")
def future_only_stage(gqga4_split_start):
    return run_gqga4_split(gqga4_split_start, future_only=True)


@pytest.fixture(scope="module")
def both_modes_stage(gqga4_split_start):
    return run_gqga4_split(gqga4_split_start, future_only=False)


def test_future_only_matches_the_same_start_original_with_only_carrier_filter_removed(
    gqga4_split_start, future_only_stage
):
    state, context, records, replays = future_only_stage
    assert len(records) == len(FUTURE_ONLY_REFERENCE) == 5
    previous_quality = evaluate_plan(
        gqga4_split_start[1], context.factory.cache.rule_set, context.factory.cache.context
    ).quality_key
    for offset, ((trace, plan), expected) in enumerate(zip(records, FUTURE_ONLY_REFERENCE), 1):
        check, action, underweight, gap, chains, signature = expected
        assert trace.sequence == 35 + offset and trace.candidate_check_count == check
        assert trace.action_name == action
        assert trace.quality_before == previous_quality
        assert trace.quality_after == (1, D("70.3"), underweight, D(gap), chains, D(380))
        assert fingerprint(semantic_signature(plan)) == signature
        previous_quality = trace.quality_after
    assert replays == ((86414, 1),)
    assert state.accepted_move_count == 40 and state.virtual_sequence == 19
    assert state.split_sequence == state.accepted_future_borrow_return_count == 1
    assert state.accepted_same_period_split_count == 0
    assert context.factory.budget.candidate_check_count == 100000
    assert context.factory.budget.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert state.current_evaluation.quality_key == (1, D("70.3"), 2, D("204.42"), 22, D(380))


def test_future_returned_partition_keeps_its_source_period_through_replay(future_only_stage):
    _, _, records, _ = future_only_stage
    split_plan = records[0][1]
    returned = split_plan.chains[-1]
    assert returned.assigned_period == "BR_00000006"
    pieces = tuple(node for node in returned.nodes if node.split_lineage is not None)
    assert tuple(node.weight for node in pieces) == (D(500), D(100))
    assert {node.source_order_id for node in pieces} == {"0002002073-000010"}
    assert all(
        node.split_lineage.split_mode is FUTURE
        and node.split_lineage.origin_assigned_period == "BR_00000001"
        for node in pieces
    )
    separator = returned.nodes[1]
    assert separator.virtual_lineage.prototype_id == "virtual_sphc:1000x0.5"
    assert separator.virtual_lineage.purpose is VirtualPurpose.SPLIT_SEPARATOR
    assert separator.weight == D(20)
    assert separator.virtual_lineage.accepted_sequence == 19
    for _, plan in records:
        assert all(
            chain.assigned_period == "BR_00000006"
            for chain in plan.chains
            for node in chain.nodes
            if node.split_lineage is not None
        )


def test_both_modes_rescan_the_normalized_donor_instead_of_freezing_its_old_period(
    both_modes_stage,
):
    state, _, records, replays = both_modes_stage
    assert replays == ((86415, 2),)
    first_trace, first_plan = records[0]
    second_trace, second_plan = records[1]
    assert first_trace.action_name == second_trace.action_name == "controlled_order_split"
    assert first_trace.affected_chain_ids == second_trace.affected_chain_ids == ("initial-000012",)
    assert fingerprint(first_plan) == (
        "83c465d5394e57545750c70512196501cd40de219a736a6adb990d138ac51aad"
    )
    assert fingerprint(second_plan) == (
        "c291c8059fbe18898a4dde17380dfd08ec4367058b12065d66541ae8d4f5b6db"
    )
    remaining_donor = next(
        chain for chain in first_plan.chains if chain.chain_id == "initial-000012"
    )
    assert remaining_donor.assigned_period == "BR_00000006"
    assert tuple(node.node_id for node in remaining_donor.nodes) == ("0002002073-000010",)
    for plan, source, period, tail, prototype, sequence in (
        (first_plan, "0002002055-000120", "BR_00000001", "70.3", "1250x0.6", 19),
        (second_plan, "0002002073-000010", "BR_00000006", "100", "1000x0.5", 20),
    ):
        returned = plan.chains[-1]
        pieces = tuple(node for node in returned.nodes if node.split_lineage is not None)
        assert returned.assigned_period == period
        assert tuple(node.weight for node in pieces) == (D(500), D(tail))
        assert {node.source_order_id for node in pieces} == {source}
        assert all(
            node.split_lineage.split_mode is SAME
            and node.split_lineage.origin_assigned_period == period
            and node.split_lineage.target_assigned_period == period
            for node in pieces
        )
        separator = returned.nodes[1]
        assert separator.virtual_lineage.prototype_id == f"virtual_sphc:{prototype}"
        assert separator.virtual_lineage.purpose is VirtualPurpose.SPLIT_SEPARATOR
        assert separator.virtual_lineage.accepted_sequence == sequence and separator.weight == D(20)
    assert state.split_sequence == state.accepted_same_period_split_count == 2
    assert state.accepted_future_borrow_return_count == 0


def test_both_mode_product_extension_retains_remaining_budget_and_one_replay(both_modes_stage):
    state, context, records, _ = both_modes_stage
    # Same-period splitting is an approved product extension, not a raw-reference equivalence.
    expected = (
        (86414, "controlled_order_split", 1, "100", 3, "404.12", 23, 380),
        (86415, "controlled_order_split", 0, "0", 3, "384.12", 23, 400),
        (86701, "whole_chain_insertion", 0, "0", 2, "274.42", 22, 400),
        (91582, "real_node_relocation", 0, "0", 2, "254.42", 22, 400),
        (94680, "real_node_relocation", 0, "0", 2, "230.42", 22, 400),
        (97907, "real_node_relocation", 0, "0", 2, "204.42", 22, 400),
    )
    assert len(records) == len(expected) == 6
    previous_quality = (1, D("670.3"), 1, D("194.42"), 22, D(360))
    for offset, ((trace, _), row) in enumerate(zip(records, expected), 1):
        check, action, prohibited, severity, underweight, gap, chains, virtual = row
        assert trace.sequence == 35 + offset and trace.candidate_check_count == check
        assert trace.action_name == action and trace.quality_before == previous_quality
        assert trace.quality_after == (
            prohibited,
            D(severity),
            underweight,
            D(gap),
            chains,
            D(virtual),
        )
        previous_quality = trace.quality_after
    assert state.accepted_move_count == 41 and state.virtual_sequence == 20
    assert context.complete_candidate_evaluation_count == 155
    assert context.factory.budget.candidate_check_count == 100000
    assert context.factory.budget.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert state.current_evaluation.quality_key == (0, D(0), 2, D("204.42"), 22, D(400))
    assert fingerprint(state.current_plan) == (
        "fa30b09955a46827d854e54c3df30760efc3c6784cfe77e3ed9eb009af59b498"
    )
    assert fingerprint(context.accepted_move_traces) == (
        "84d1f24282871222094620e12fe8f4300ae673ab562b82b7dbbd8f56a5eafa43"
    )


def test_historical_raw_split_and_replay_are_preserved_as_a_distinct_baseline(gqga4_split_start):
    frozen = gqga4_split_start[0]
    split = frozen["split"]
    assert split["start"]["candidate_checks"] == 77204 and split["accepted_count"] == 1
    accepted = split["accepted_actions"][0]
    assert accepted["candidate_checks"] == split["final"]["candidate_checks"] == 77205
    assert accepted["node"]["node_id"] == "0002002073-000010"
    assert [D(piece["weight"]) for piece in accepted["fragments"]] == [D(500), D(100)]
    assert accepted["separators"][0]["virtual_prototype_id"] == "virtual_sphc:1250x0.5"
    replay = frozen["search_rounds"][1]
    assert replay["final"]["candidate_checks"] == 94024
    assert tuple(replay["final"]["quality"]) == (1, D("70.3"), 0, D(0), 22, D(540), D("22694.88"))
