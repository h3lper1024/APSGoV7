"""Core orchestration reuses each completed stage and never skips the split owner."""

from dataclasses import fields, replace
from decimal import Decimal
from unittest.mock import patch

import pytest

from apsgo_scheduler.core import controlled_split, initial_solution, solver
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.contracts import (
    CoreAuditStatus,
    DiagnosticPhase,
    SearchStopReason,
    SolveStatus,
    fingerprint,
)
from apsgo_scheduler.core.model import SchedulingProblem
from tests.core.graph.test_construction_order import node, rules
from tests.core.rules.test_inter_chain_width_gap import rule as width_gap_rule
from tests.core.rules.test_inter_chain_width_gap import with_gap
from tests.core.search.test_controlled_order_split import split_case
from tests.core.search.test_single_real_node_relocation import Stop
from tests.core.search.test_virtual_material_factory import prototype
from tests.core.test_solver_policy import policy as make_policy

D = Decimal


def resign_problem(problem, **changes):
    problem = replace(problem, **changes)
    values = {
        item.name: getattr(problem, item.name)
        for item in fields(problem)
        if item.name not in {"problem_id", "input_fingerprint"}
    }
    return replace(problem, input_fingerprint=fingerprint(values))


def solver_case(
    *,
    nodes=None,
    rule_items=(),
    rule_set=None,
    prototypes=(),
    period_order=("period",),
    limit=1000,
    policy_changes=None,
    clock=lambda: 1.0,
    cancellation=None,
    started_at=0.0,
):
    rule_set = (
        replace(rules(), rules=tuple(rule_items), fingerprint=fingerprint(tuple(rule_items)))
        if rule_set is None
        else rule_set
    )
    problem = resign_problem(
        SchedulingProblem(
            "solver-case",
            rule_set.product_line_code,
            rule_set.process_code,
            rule_set.scenario,
            (node("real"),) if nodes is None else tuple(nodes),
            tuple(period_order),
            tuple(prototypes),
            "pending",
        )
    )
    policy = make_policy(**({"candidate_check_limit": limit} | (policy_changes or {})))
    runtime = SolveRuntimeBudget.from_policy(policy, started_at, cancellation, clock=clock)
    return problem, rule_set, policy, runtime


def test_real_pipeline_reuses_initial_evaluation_and_one_cache_budget_context(monkeypatch):
    values = solver_case(nodes=(node("a"), node("b")), limit=0)
    problem, rule_set, policy, runtime = values
    calls, captured = [], {}
    graph = solver.build_construction_dag
    cover = solver.minimum_path_cover
    construct = solver.construct_initial_plan
    local = solver.run_local_search
    split = solver.run_controlled_order_split
    audit = solver.audit_core_without_search_cache

    def observed_graph(active_problem, cache, budget, *, seed):
        calls.append("graph")
        assert active_problem is problem and budget is runtime and seed == policy.seed
        assert cache.problem is problem and cache.rule_set is rule_set
        captured["cache"] = cache
        captured["graph"] = graph(active_problem, cache, budget, seed=seed)
        return captured["graph"]

    def observed_cover(dag, budget):
        calls.append("path_cover")
        assert dag is captured["graph"] and budget is runtime
        captured["cover"] = cover(dag, budget)
        return captured["cover"]

    def observed_initial(active_problem, dag, path_cover, cache, budget):
        calls.append("initial")
        assert active_problem is problem and dag is captured["graph"]
        assert path_cover is captured["cover"] and cache is captured["cache"] and budget is runtime
        captured["initial"] = construct(active_problem, dag, path_cover, cache, budget)
        return captured["initial"]

    def observed_local(state, context):
        calls.append("local_search")
        assert state.current_plan is captured["initial"].candidate.plan
        assert state.current_evaluation is captured["initial"].candidate.search_evaluation
        assert context.factory.cache is captured["cache"] and context.factory.budget is runtime
        assert context.policy is policy
        captured["state"], captured["context"] = state, context
        return local(state, context)

    def observed_split(state, context):
        calls.append("controlled_split")
        assert state is captured["state"] and context is captured["context"]
        assert runtime.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE
        return split(state, context)

    def observed_audit(candidate, active_problem, active_rules, budget):
        calls.append("audit")
        assert candidate.plan is captured["state"].current_plan
        assert candidate.search_evaluation is captured["state"].current_evaluation
        assert active_problem is problem and active_rules is rule_set and budget is runtime
        captured["audit"] = audit(candidate, active_problem, active_rules, budget)
        return captured["audit"]

    for name, function in (
        ("build_construction_dag", observed_graph),
        ("minimum_path_cover", observed_cover),
        ("construct_initial_plan", observed_initial),
        ("run_local_search", observed_local),
        ("run_controlled_order_split", observed_split),
        ("audit_core_without_search_cache", observed_audit),
    ):
        monkeypatch.setattr(solver, name, function)
    with patch.object(
        initial_solution, "evaluate_plan", wraps=initial_solution.evaluate_plan
    ) as evaluated:
        result = solver.solve(*values)
    assert calls == ["graph", "path_cover", "initial", "local_search", "controlled_split", "audit"]
    evaluated.assert_called_once()
    assert result.status is SolveStatus.SUCCESS
    assert result.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE
    assert result.release.canonical_plan is captured["state"].current_plan
    assert result.release.resource_facts is captured["audit"].resource_facts
    assert result.metrics.graph_edge_check_count == 1
    assert result.metrics.matching_edge_count == result.metrics.path_count == 1
    assert result.metrics.initial_chain_count == result.metrics.final_chain_count == 1
    assert result.metrics.candidate_check_count == runtime.candidate_check_count == 0
    assert result.metrics.complete_candidate_evaluation_count == 0
    assert result.trace == ()


def test_real_split_owner_reopens_natural_completion_and_replays_only_once(monkeypatch):
    _, previous = split_case()
    cache = previous.factory.cache
    values = solver_case(
        nodes=cache.problem.nodes,
        rule_set=cache.rule_set,
        prototypes=cache.problem.virtual_prototypes,
        period_order=cache.problem.period_order,
    )
    initial_calls, replay_calls, contexts = [], [], []
    initial_local = solver.run_local_search
    replay_local = controlled_split.run_local_search

    def first(state, context):
        initial_calls.append(state)
        contexts.append(context)
        return initial_local(state, context)

    def replay(state, context):
        replay_calls.append(state)
        assert context is contexts[0] and context.factory.budget is values[-1]
        assert state.split_sequence == 1
        return replay_local(state, context)

    monkeypatch.setattr(solver, "run_local_search", first)
    monkeypatch.setattr(controlled_split, "run_local_search", replay)
    result = solver.solve(*values)
    assert len(initial_calls) == len(replay_calls) == 1
    assert initial_calls[0] is replay_calls[0]
    assert result.status is SolveStatus.SUCCESS
    assert (
        result.metrics.accepted_split_count == result.metrics.accepted_same_period_split_count == 1
    )
    assert result.metrics.accepted_future_borrow_return_count == 0
    assert result.metrics.accepted_move_count == 1
    assert result.metrics.complete_candidate_evaluation_count == 1
    assert tuple(move.action_name for move in result.trace) == ("controlled_order_split",)


def test_error_after_accepted_split_preserves_last_complete_state_trace_and_counters(monkeypatch):
    _, previous = split_case()
    cache = previous.factory.cache
    values = solver_case(
        nodes=cache.problem.nodes,
        rule_set=cache.rule_set,
        prototypes=cache.problem.virtual_prototypes,
        period_order=cache.problem.period_order,
    )
    original = solver.run_controlled_order_split

    def fail_after_acceptance(state, context):
        original(state, context)
        assert state.split_sequence == 1
        raise RuntimeError("fault after a complete accepted split")

    monkeypatch.setattr(solver, "run_controlled_order_split", fail_after_acceptance)
    result = solver.solve(*values)
    assert (
        result.status is SolveStatus.FAILED and result.stop_reason is SearchStopReason.SYSTEM_ERROR
    )
    assert result.release is None and result.core_audit.status is CoreAuditStatus.NOT_RUN
    pieces = tuple(
        item
        for chain in result.diagnostic_candidate.plan.chains
        for item in chain.nodes
        if item.split_lineage is not None
    )
    assert tuple(item.weight for item in pieces) == (D(50), D(50), D(20))
    assert result.metrics.accepted_split_count == result.metrics.accepted_move_count == 1
    assert result.metrics.complete_candidate_evaluation_count == 1
    assert tuple(move.action_name for move in result.trace) == ("controlled_order_split",)
    assert result.metrics.candidate_check_count == values[-1].candidate_check_count == 1


STAGES = (
    "build_construction_dag",
    "minimum_path_cover",
    "construct_initial_plan",
    "run_local_search",
    "run_controlled_order_split",
    "audit_core_without_search_cache",
)


@pytest.mark.parametrize("stage", STAGES[:3])
@pytest.mark.parametrize("stop", ("cancel", "search_deadline"))
def test_real_construction_interruption_never_calls_downstream_or_exposes_partial_plan(
    stage, stop, monkeypatch
):
    token, now = Stop(), [1.0]
    values = solver_case(nodes=(node("a"), node("b")), cancellation=token, clock=lambda: now[0])
    runtime = values[-1]
    calls, partial = [], []

    def observing(name, original):
        def run(*args, **kwargs):
            calls.append(name)
            if name == stage:
                token.active = stop == "cancel"
                if stop == "search_deadline":
                    now[0] = runtime.search_deadline_monotonic
                result = original(*args, **kwargs)
                partial.append(result)
                return result
            assert name in STAGES[: STAGES.index(stage)]
            return original(*args, **kwargs)

        return run

    for name in STAGES:
        monkeypatch.setattr(solver, name, observing(name, getattr(solver, name)))
    before = fingerprint(values[:3])
    result = solver.solve(*values)
    assert calls == list(STAGES[: STAGES.index(stage) + 1])
    assert len(partial) == 1 and not partial[0].complete
    assert result.status is (
        SolveStatus.CANCELLED if stop == "cancel" else SolveStatus.NO_COMPLETE_PLAN
    )
    assert result.stop_reason is (
        SearchStopReason.USER_CANCELLED
        if stop == "cancel"
        else SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    )
    assert result.diagnostic_candidate is None and result.release is None
    assert result.core_audit.status is CoreAuditStatus.NOT_RUN
    assert result.metrics.candidate_check_count == runtime.candidate_check_count == 0
    assert fingerprint(values[:3]) == before


@pytest.mark.parametrize("stage", STAGES)
def test_stage_exception_ends_pipeline_without_recovery_search_or_release(stage, monkeypatch):
    values = solver_case(nodes=(node("a"), node("b")))
    calls = []

    def observing(name, original):
        def run(*args, **kwargs):
            calls.append(name)
            if name == stage:
                raise RuntimeError(f"failed at {stage}")
            assert name in STAGES[: STAGES.index(stage)]
            return original(*args, **kwargs)

        return run

    for name in STAGES:
        monkeypatch.setattr(solver, name, observing(name, getattr(solver, name)))
    before = fingerprint(values[:3])
    result = solver.solve(*values)
    assert calls == list(STAGES[: STAGES.index(stage) + 1])
    assert result.status is SolveStatus.FAILED
    assert result.stop_reason is SearchStopReason.SYSTEM_ERROR and result.release is None
    assert (result.diagnostic_candidate is not None) is (STAGES.index(stage) >= 3)
    expected_phase = (
        DiagnosticPhase.CONSTRUCTION
        if STAGES.index(stage) < 3
        else DiagnosticPhase.CORE_AUDIT
        if stage == STAGES[-1]
        else DiagnosticPhase.SEARCH
    )
    assert any(
        issue.phase is expected_phase and f"failed at {stage}" in issue.message
        for issue in result.issues
    )
    assert result.metrics.candidate_check_count == values[-1].candidate_check_count
    assert fingerprint(values[:3]) == before


@pytest.mark.parametrize("stop", ("search_deadline", "hard_deadline", "cancel"))
def test_complete_initial_snapshot_survives_stop_for_diagnostics_and_bounded_finalization(
    stop, monkeypatch
):
    token, now = Stop(), [1.0]
    values = solver_case(nodes=(node("a"), node("b")), cancellation=token, clock=lambda: now[0])
    runtime = values[-1]
    original = solver.construct_initial_plan
    captured = []

    def finish(*args):
        result = original(*args)
        assert result.complete
        captured.append(result.candidate)
        if stop == "cancel":
            token.active = True
        else:
            now[0] = (
                runtime.search_deadline_monotonic
                if stop == "search_deadline"
                else runtime.final_deadline_monotonic
            )
        return result

    monkeypatch.setattr(solver, "construct_initial_plan", finish)
    result = solver.solve(*values)
    assert len(captured) == 1
    assert result.diagnostic_candidate.plan is captured[0].plan
    assert result.diagnostic_candidate.search_evaluation is captured[0].search_evaluation
    assert result.metrics.initial_chain_count == 1 and result.metrics.candidate_check_count == 0
    assert result.metrics.accepted_move_count == 0 and result.trace == ()
    if stop == "search_deadline":
        assert result.status is SolveStatus.SUCCESS and result.release is not None
        assert result.core_audit.passed
        assert result.stop_reason is SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    else:
        assert result.release is None
        assert result.status is (
            SolveStatus.CANCELLED if stop == "cancel" else SolveStatus.COMPLETE_NOT_PUBLISHABLE
        )
        assert result.stop_reason is (
            SearchStopReason.USER_CANCELLED
            if stop == "cancel"
            else SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED
        )


def test_final_snapshot_is_not_repaired_or_re_evaluated_to_hide_search_corruption(monkeypatch):
    original_node = replace(node("real"), source_period="first")
    values = solver_case(nodes=(original_node,), period_order=("first", "last"))
    local = solver.run_local_search
    audit = solver.audit_core_without_search_cache
    captured = []

    def corrupt(state, context):
        local(state, context)
        state.current_plan = replace(
            state.current_plan,
            chains=(replace(state.current_plan.chains[0], assigned_period="last"),),
        )
        captured.append((state.current_plan, state.current_evaluation))
        return state

    def observe(candidate, *args):
        assert candidate.plan is captured[0][0]
        assert candidate.search_evaluation is captured[0][1]
        assert candidate.plan.chains[0].assigned_period == "last"
        return audit(candidate, *args)

    monkeypatch.setattr(solver, "run_local_search", corrupt)
    monkeypatch.setattr(solver, "audit_core_without_search_cache", observe)
    result = solver.solve(*values)
    assert result.status is SolveStatus.FAILED and result.release is None
    assert result.core_audit.status is CoreAuditStatus.COMPLETED
    assert not result.core_audit.search_evaluation_matches
    assert "chain_period_not_normalized" in result.core_audit.invariant_failure_codes


@pytest.mark.parametrize("enabled", (False, True))
def test_plan_width_requirement_also_guards_direct_core_virtual_prototypes(enabled, monkeypatch):
    active = (
        with_gap(rules()) if enabled else replace(rules(), rules=(width_gap_rule(enabled=False),))
    )
    values = solver_case(
        rule_set=active, prototypes=(prototype("missing-width", width=None),), limit=0
    )
    if enabled:
        monkeypatch.setattr(
            solver,
            "build_construction_dag",
            lambda *_args, **_kwargs: pytest.fail("input failure must precede construction"),
        )
    result = solver.solve(*values)
    if enabled:
        assert result.stop_reason is SearchStopReason.INPUT_INVALID
        assert result.release is result.diagnostic_candidate is None
        assert result.core_audit.status is CoreAuditStatus.NOT_RUN
        assert [(item.code, item.subject_id, item.field_path) for item in result.issues] == [
            ("missing_required_field", "missing-width", "width")
        ]
    else:
        assert result.release is not None and result.core_audit.passed
        assert not any(item.code == "missing_required_field" for item in result.issues)
