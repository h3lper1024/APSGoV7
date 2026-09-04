"""Production chain order is shared by construction, candidates and split replay."""

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apsgo_scheduler.core import controlled_split, initial_solution, neighborhoods
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import SearchStopReason, fingerprint
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import Chain, SchedulePlan, SchedulingProblem, SearchState
from apsgo_scheduler.core.neighborhoods import SearchContext, try_complete_candidate
from apsgo_scheduler.core.path_cover import minimum_path_cover
from apsgo_scheduler.core.rules.base import RuleEvaluationContext
from apsgo_scheduler.core.rules.rule_set import ProcessRuleSet
from apsgo_scheduler.core.virtual_material import VirtualFactory
from tests.app.test_input_normalizer import make_request
from tests.core.graph.test_bipartite_matching import budget, make_dag
from tests.core.graph.test_construction_order import node, rules
from tests.core.rules.test_inter_chain_width_gap import rule, with_gap
from tests.core.search.test_controlled_order_split import parent, split_case, suppress_replay
from tests.core.search.test_whole_chain_neighborhood import weight_rule
from tests.core.test_quality_key import criterion

D = Decimal
STAGES = (
    "improve_whole_chain",
    "improve_real_node_relocation",
    "improve_virtual_weight_fill",
    "improve_chain_order",
)


def cache_for(nodes, active, periods):
    problem = SchedulingProblem(
        "chain-order-integration",
        active.product_line_code,
        active.process_code,
        active.scenario,
        tuple(nodes),
        periods,
        (),
        fingerprint((nodes, periods)),
    )
    evaluation_context = RuleEvaluationContext(
        periods, {name: index for index, name in enumerate(periods)}, ()
    )
    return RuleEdgeDecisionCache(problem, active, evaluation_context)


def add_gap(state, context, *, include_metric=True, tail=False):
    cache = context.factory.cache
    active = with_gap(cache.rule_set)
    if not include_metric:
        active = replace(active, quality_spec=cache.rule_set.quality_spec)
    plan, problem = state.current_plan, cache.problem
    if tail:
        last = replace(node("last"), source_period="P1", rule_attributes={"grade_class": "OTHER"})
        plan = SchedulePlan(plan.chains + (Chain("last-chain", (last,), "P1"),))
        problem = replace(
            problem,
            nodes=problem.nodes + (last,),
            input_fingerprint=fingerprint((problem.nodes, last)),
        )
    new_cache = RuleEdgeDecisionCache(problem, active, cache.context)
    context = replace(context, factory=VirtualFactory(new_cache, context.factory.budget))
    return SearchState(plan, evaluate_plan(plan, active, cache.context)), context


@pytest.mark.parametrize("mode", ("objective", "diagnostic", "disabled"))
def test_initial_grouping_keeps_path_identity_ids_and_same_period_relative_order(mode):
    periods = ("P9", "empty", "P2", "P8", "P1", "P7")
    names = ("late", "early-z", "middle", "early-a", "last")
    sources = ("P1", "P9", "P2", "P9", "P7")
    nodes = tuple(replace(node(name), source_period=source) for name, source in zip(names, sources))
    active = with_gap(rules())
    if mode != "objective":
        active = replace(
            active,
            rules=(rule(enabled=mode != "disabled"),),
            quality_spec=rules().quality_spec,
        )
    cache = cache_for(nodes, active, periods)
    dag = make_dag(names, {})
    cover = minimum_path_cover(dag, budget())
    before = fingerprint((cache.problem, cover.paths, cover.matching_edges))
    runtime = budget()
    result = initial_solution.construct_initial_plan(cache.problem, dag, cover, cache, runtime)
    assert result.complete
    plan = result.candidate.plan
    indices = (0, 1, 2, 3, 4) if mode == "disabled" else (1, 3, 2, 0, 4)
    assert tuple(chain.chain_id for chain in plan.chains) == tuple(
        f"initial-{index + 1:06d}" for index in indices
    )
    assert tuple(chain.nodes[0] for chain in plan.chains) == tuple(
        nodes[index] for index in indices
    )
    assert tuple(chain.assigned_period for chain in plan.chains) == tuple(
        sources[index] for index in indices
    )
    assert tuple(
        item.chain_id for item in result.candidate.search_evaluation.chain_evaluations
    ) == (tuple(chain.chain_id for chain in plan.chains))
    assert result.plan_fingerprint == fingerprint(plan)
    assert fingerprint((cache.problem, cover.paths, cover.matching_edges)) == before
    assert runtime.candidate_check_count == 0


def test_changed_donor_period_is_normalized_before_candidate_grouping():
    early = replace(node("early", width="900"), source_period="P9")
    late = replace(node("late", width="1200"), source_period="P1")
    target = replace(node("target", width="900"), source_period="P9")
    middle = replace(node("middle", width="1200"), source_period="P2")
    active = with_gap(rules())
    cache = cache_for((early, late, target, middle), active, ("P9", "P2", "P1"))
    runtime = budget(candidate_check_limit=10)
    context = SearchContext(VirtualFactory(cache, runtime), make_request().policy)
    plan = SchedulePlan(
        (
            Chain("donor", (early, late), "P9"),
            Chain("target", (target,), "P9"),
            Chain("middle", (middle,), "P2"),
        )
    )
    state = SearchState(plan, evaluate_plan(plan, active, cache.context))
    proposal = (
        replace(plan.chains[0], nodes=(late,)),
        replace(plan.chains[1], nodes=(target, early)),
        plan.chains[2],
    )
    assert runtime.consume_candidate_check()
    assert try_complete_candidate(
        state,
        context,
        proposal,
        affected_chain_ids=("donor", "target"),
        virtual_sequence=0,
        action_name="real_node_relocation",
    )
    assert tuple(chain.chain_id for chain in state.current_plan.chains) == (
        "target",
        "middle",
        "donor",
    )
    assert tuple(chain.assigned_period for chain in state.current_plan.chains) == (
        "P9",
        "P2",
        "P1",
    )
    assert state.current_plan.chains[1] is plan.chains[2]
    assert state.current_evaluation.quality_key == (0, D(0), D(300))
    assert context.complete_candidate_evaluation_count == 1
    assert state.current_evaluation == evaluate_plan(state.current_plan, active, cache.context)


@pytest.mark.parametrize("future", (False, True))
def test_split_authorization_precedes_grouping_and_returned_chain_need_not_be_last(
    future, monkeypatch
):
    state, context = add_gap(*split_case(origin="P9" if future else "P2", anchor=future), tail=True)
    replay = suppress_replay(monkeypatch)
    assert controlled_split.run_controlled_order_split(state, context) is state
    assert len(replay) == 1 and state.split_sequence == 1
    assert state.accepted_future_borrow_return_count == int(future)
    assert state.accepted_same_period_split_count == int(not future)
    partition = next(
        chain for chain in state.current_plan.chains if chain.chain_id.startswith("split-chain-")
    )
    assert partition is state.current_plan.chains[-2]
    assert partition.assigned_period == "P2"
    assert state.current_plan.chains[-1].chain_id == "last-chain"
    assert [node.weight for node in partition.nodes if node.split_lineage] == [D(50), D(50), D(20)]
    assert state.current_evaluation == evaluate_plan(
        state.current_plan, context.factory.cache.rule_set, context.factory.cache.context
    )
    assert context.complete_candidate_evaluation_count == 1
    if future:
        # Test a later ordinary edit after reopening this isolated suppressed replay boundary.
        context.factory.budget.stop_reason = None
        donor, _, last = state.current_plan.chains
        before = fingerprint(state)
        assert not try_complete_candidate(
            state,
            context,
            (last, replace(partition, nodes=donor.nodes + partition.nodes)),
            affected_chain_ids=(donor.chain_id, partition.chain_id),
            virtual_sequence=state.virtual_sequence,
            action_name="whole_chain_prepend",
        )
        assert fingerprint(state) == before
        assert context.complete_candidate_evaluation_count == 1


@pytest.mark.parametrize("include_metric", (False, True))
def test_orchestration_runs_fourth_stage_only_for_bound_objective(include_metric, monkeypatch):
    state, context = add_gap(*split_case(), include_metric=include_metric)
    calls = []
    for name in STAGES:

        def stage(current, active_context, *, stage_name=name):
            assert current is state and active_context is context
            assert context.factory.budget.stop_reason is None
            calls.append(stage_name)
            return current

        monkeypatch.setattr(neighborhoods, name, stage)
    assert neighborhoods.run_local_search(state, context) is state
    assert tuple(calls) == STAGES[: 4 if include_metric else 3]
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE


@pytest.mark.parametrize("case", ("accepted", "disabled", "ineligible", "limit"))
def test_split_replay_includes_fourth_stage_once_or_preserves_stop(case, monkeypatch):
    options = {"enabled": case != "disabled"}
    if case == "ineligible":
        options["parents"] = (parent(weight="50"),)
    if case == "limit":
        options["runtime"] = budget(candidate_check_limit=0)
    state, context = add_gap(*split_case(**options))
    calls = []
    for name in STAGES:

        def stage(current, active_context, *, stage_name=name):
            assert current is state and active_context is context
            assert context.factory.budget.stop_reason is None
            calls.append(stage_name)
            return current

        monkeypatch.setattr(neighborhoods, name, stage)
    original_authorize = ProcessRuleSet.evaluate_controlled_split

    def authorize(active, subject, evaluation_context):
        assert len(calls) == 4, "no second split scan may follow the replay"
        return original_authorize(active, subject, evaluation_context)

    monkeypatch.setattr(ProcessRuleSet, "evaluate_controlled_split", authorize)
    neighborhoods.run_local_search(state, context)
    controlled_split.run_controlled_order_split(state, context)
    assert tuple(calls) == STAGES * (2 if case == "accepted" else 1)
    assert state.split_sequence == int(case == "accepted")
    assert context.factory.budget.candidate_check_count == int(case == "accepted")
    assert context.factory.budget.stop_reason is (
        SearchStopReason.CANDIDATE_LIMIT_REACHED
        if case == "limit"
        else SearchStopReason.LOCAL_SEARCH_COMPLETE
    )


def test_truncated_third_stage_does_not_start_chain_order_or_split_replay(monkeypatch):
    state, context = add_gap(*split_case())
    calls = []
    for name in STAGES:

        def stage(current, active_context, *, stage_name=name):
            calls.append(stage_name)
            if stage_name == STAGES[2]:
                active_context.factory.budget.stop_reason = SearchStopReason.USER_CANCELLED
            return current

        monkeypatch.setattr(neighborhoods, name, stage)
    before = fingerprint(state)
    neighborhoods.run_local_search(state, context)
    controlled_split.run_controlled_order_split(state, context)
    assert tuple(calls) == STAGES[:3]
    assert fingerprint(state) == before
    assert context.factory.budget.stop_reason is SearchStopReason.USER_CANCELLED


def test_mixed_source_path_uses_earliest_assignment_before_stable_grouping():
    names = ("middle", "later", "early", "peer")
    sources = ("P2", "P1", "P9", "P9")
    nodes = tuple(replace(node(name), source_period=source) for name, source in zip(names, sources))
    cache = cache_for(nodes, with_gap(rules()), ("P9", "P2", "P1"))
    dag = make_dag(names, {"later": ("early",)})
    cover = minimum_path_cover(dag, budget())
    assert cover.paths == (("middle",), ("later", "early"), ("peer",))
    result = initial_solution.construct_initial_plan(cache.problem, dag, cover, cache, budget())
    assert result.complete
    plan = result.candidate.plan
    assert tuple(chain.chain_id for chain in plan.chains) == (
        "initial-000002",
        "initial-000003",
        "initial-000001",
    )
    assert tuple(chain.assigned_period for chain in plan.chains) == ("P9", "P9", "P2")
    assert plan.chains[0].nodes == nodes[1:3]
    assert tuple(chain.nodes for chain in plan.chains[1:]) == ((nodes[3],), (nodes[0],))
    assert result.candidate.search_evaluation == evaluate_plan(plan, cache.rule_set, cache.context)
    assert result.plan_fingerprint == fingerprint(plan)


def test_real_whole_chain_merge_appends_locally_then_groups_before_future_tail():
    first = replace(node("a", weight="20"), source_period="P9")
    second = replace(node("b", weight="20"), source_period="P9")
    future = replace(node("future", weight="90"), source_period="P1")
    active = with_gap(
        replace(
            rules(),
            rules=(weight_rule(),),
            quality_spec=rules().quality_spec + (criterion("chain_count"),),
        )
    )
    cache = cache_for((first, second, future), active, ("P9", "P1"))
    runtime = budget(candidate_check_limit=10)
    policy = replace(make_request().policy, whole_chain_pair_scan_slack_weight=D(0))
    context = SearchContext(VirtualFactory(cache, runtime), policy)
    tail = Chain("future-tail", (future,), "P1")
    plan = SchedulePlan((Chain("A", (first,), "P9"), Chain("B", (second,), "P9"), tail))
    state = SearchState(plan, evaluate_plan(plan, active, cache.context))
    assert neighborhoods.improve_whole_chain(state, context) is state
    assert tuple(chain.chain_id for chain in state.current_plan.chains) == ("B", "future-tail")
    assert state.current_plan.chains[0].nodes == (second, first)
    assert state.current_plan.chains[0].assigned_period == "P9"
    assert state.current_plan.chains[1] is tail
    assert state.current_evaluation.quality_key == (0, D(0), 2, D(0))
    assert state.accepted_move_count == context.complete_candidate_evaluation_count == 1
    assert runtime.candidate_check_count == 1 and runtime.stop_reason is None
    assert context.accepted_move_traces[0].action_name == "whole_chain_append"


def test_cancel_immediately_after_split_commit_keeps_result_without_replay(monkeypatch):
    signal = SimpleNamespace(cancelled=False)
    runtime = budget(
        candidate_check_limit=10,
        cancellation=SimpleNamespace(is_cancelled=lambda: signal.cancelled),
    )
    state, context = add_gap(*split_case(runtime=runtime))
    runtime.stop_reason = SearchStopReason.LOCAL_SEARCH_COMPLETE
    original_commit = SearchState.commit_accepted

    def commit(current, *args, **kwargs):
        original_commit(current, *args, **kwargs)
        signal.cancelled = True

    monkeypatch.setattr(SearchState, "commit_accepted", commit)
    monkeypatch.setattr(
        controlled_split, "run_local_search", lambda *_: pytest.fail("unexpected split replay")
    )
    monkeypatch.setattr(
        neighborhoods, "improve_chain_order", lambda *_: pytest.fail("unexpected fourth stage")
    )
    assert controlled_split.run_controlled_order_split(state, context) is state
    assert runtime.stop_reason is SearchStopReason.USER_CANCELLED
    assert state.split_sequence == state.accepted_move_count == runtime.candidate_check_count == 1
    assert state.virtual_sequence == 2 and len(context.accepted_move_traces) == 1
    assert context.complete_candidate_evaluation_count == 1
