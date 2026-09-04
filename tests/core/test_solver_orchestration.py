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
    RuleScope,
    SearchStopReason,
    SolveStatus,
    fingerprint,
)
from apsgo_scheduler.core.model import SchedulingProblem
from tests.core.graph.test_construction_order import node, rules
from tests.core.rules.test_inter_chain_width_gap import rule as width_gap_rule
from tests.core.rules.test_inter_chain_width_gap import with_gap
from tests.core.search.test_chain_order_integration import add_gap
from tests.core.search.test_controlled_order_split import split_case
from tests.core.search.test_single_real_node_relocation import Stop
from tests.core.search.test_virtual_material_factory import prototype
from tests.core.test_quality_key import SeverityRule, ruleset
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


def width_split_solver_case(*, accepted_split=True, **changes):
    _, previous = add_gap(*split_case(enabled=accepted_split, narrow_enabled=accepted_split))
    cache = previous.factory.cache
    return solver_case(
        nodes=cache.problem.nodes,
        rule_set=cache.rule_set,
        prototypes=cache.problem.virtual_prototypes,
        period_order=cache.problem.period_order,
        **changes,
    )


@pytest.mark.parametrize("accepted_split", (False, True))
@pytest.mark.parametrize("entry_stop", (None, SearchStopReason.LOCAL_SEARCH_COMPLETE))
def test_width_phase_runs_once_after_split_and_optional_old_replay_with_shared_state(
    accepted_split, entry_stop, monkeypatch
):
    values = width_split_solver_case(accepted_split=accepted_split)
    problem, active, policy, runtime = values
    calls, captured = [], {}
    initial = solver.construct_initial_plan
    local = solver.run_local_search
    split = solver.run_controlled_order_split
    replay = controlled_split.run_local_search
    width = solver.run_width_optimization
    audit = solver.audit_core_without_search_cache
    expected = ["old_search", "controlled_split"] + (["old_replay"] if accepted_split else [])
    budget_identity = (
        runtime.started_at_monotonic,
        runtime.search_deadline_monotonic,
        runtime.final_deadline_monotonic,
        runtime.candidate_check_limit,
        runtime.clock,
        runtime.cancellation,
    )

    def observed_initial(current, graph, cover, cache, budget):
        assert current is problem and budget is runtime
        captured["cache"] = cache
        captured["initial"] = initial(current, graph, cover, cache, budget)
        return captured["initial"]

    def observed_local(state, context):
        calls.append("old_search")
        assert state.current_plan is captured["initial"].candidate.plan
        assert state.current_evaluation is captured["initial"].candidate.search_evaluation
        assert context.factory.cache is captured["cache"]
        assert context.factory.budget is runtime and context.policy is policy
        captured["state"], captured["context"] = state, context
        captured["factory"] = context.factory
        return local(state, context)

    def observed_split(state, context):
        calls.append("controlled_split")
        assert state is captured["state"] and context is captured["context"]
        result = split(state, context)
        assert result is state and runtime.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE
        assert not state.current_evaluation.violations
        captured["before_width"] = fingerprint(state)
        captured["checks"] = runtime.candidate_check_count
        captured["evaluations"] = context.complete_candidate_evaluation_count
        captured["trace"] = context.accepted_move_traces
        runtime.stop_reason = entry_stop
        return result

    def observed_replay(state, context):
        assert calls == ["old_search", "controlled_split"]
        calls.append("old_replay")
        assert state is captured["state"] and context is captured["context"]
        assert state.split_sequence == 1
        return replay(state, context)

    def observed_width(state, context):
        assert calls == expected
        calls.append("width_optimization")
        assert state is captured["state"] and context is captured["context"]
        assert context.factory is captured["factory"]
        assert context.factory.cache is captured["cache"]
        assert context.factory.budget is runtime and runtime.stop_reason is entry_stop
        assert context.accepted_move_traces is captured["trace"]
        assert fingerprint(state) == captured["before_width"]
        assert runtime.candidate_check_count == captured["checks"]
        assert context.complete_candidate_evaluation_count == captured["evaluations"]
        result = width(state, context)
        assert result is state and fingerprint(state) == captured["before_width"]
        assert runtime.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE
        assert (
            runtime.started_at_monotonic,
            runtime.search_deadline_monotonic,
            runtime.final_deadline_monotonic,
            runtime.candidate_check_limit,
            runtime.clock,
            runtime.cancellation,
        ) == budget_identity
        return result

    def observed_audit(candidate, current, rules, budget):
        assert calls == expected + ["width_optimization"]
        calls.append("audit")
        assert candidate.plan is captured["state"].current_plan
        assert candidate.search_evaluation is captured["state"].current_evaluation
        assert current is problem and rules is active and budget is runtime
        return audit(candidate, current, rules, budget)

    for name, function in (
        ("construct_initial_plan", observed_initial),
        ("run_local_search", observed_local),
        ("run_controlled_order_split", observed_split),
        ("run_width_optimization", observed_width),
        ("audit_core_without_search_cache", observed_audit),
    ):
        monkeypatch.setattr(solver, name, function)
    monkeypatch.setattr(controlled_split, "run_local_search", observed_replay)
    result = solver.solve(*values)
    assert calls == expected + ["width_optimization", "audit"]
    assert result.status is SolveStatus.SUCCESS and result.core_audit.passed
    assert result.release.canonical_plan is captured["state"].current_plan
    assert result.trace == captured["trace"]
    assert result.metrics.accepted_move_count == int(accepted_split)
    assert result.metrics.accepted_split_count == int(accepted_split)
    assert result.metrics.accepted_same_period_split_count == int(accepted_split)
    assert result.metrics.accepted_future_borrow_return_count == 0
    assert captured["state"].virtual_sequence == 2 * int(accepted_split)
    assert result.metrics.complete_candidate_evaluation_count == captured["evaluations"]
    assert result.metrics.candidate_check_count == runtime.candidate_check_count
    assert (runtime.candidate_check_count > captured["checks"]) is accepted_split
    phases = tuple(result.metrics.stage_duration_seconds)
    assert phases[-3:] == ("controlled_split_and_replay", "width_optimization", "core_audit")
    assert result.metrics.stage_duration_seconds["width_optimization"] >= 0


@pytest.mark.parametrize("mode", ("absent", "disabled", "diagnostic", "underweight", "prohibited"))
def test_ineligible_width_phase_is_not_called_or_reported_and_keeps_the_split_result(
    mode, monkeypatch
):
    base = ruleset()
    member = node("real", weight="500" if mode == "underweight" else "800")
    if mode == "prohibited":
        base = ruleset((SeverityRule("severity", "severity", RuleScope.CHAIN, True, "1", {}),))
        member = replace(member, rule_attributes={"severity": D(1)})
    if mode in {"underweight", "prohibited", "diagnostic"}:
        active = with_gap(base)
        if mode == "diagnostic":
            active = replace(active, quality_spec=base.quality_spec)
    else:
        active = (
            replace(base, rules=base.rules + (width_gap_rule(enabled=False),))
            if (mode == "disabled")
            else base
        )
    values = solver_case(nodes=(member,), rule_set=active)
    captured, calls = {}, []
    split, audit = solver.run_controlled_order_split, solver.audit_core_without_search_cache

    def observed_split(state, context):
        calls.append("controlled_split")
        split(state, context)
        captured["state"], captured["context"] = state, context
        captured["before"] = fingerprint(state)
        captured["checks"] = context.factory.budget.candidate_check_count
        assert bool(state.current_evaluation.violations) is (mode in {"underweight", "prohibited"})
        return state

    def observed_audit(candidate, *args):
        calls.append("audit")
        assert fingerprint(captured["state"]) == captured["before"]
        assert candidate.plan is captured["state"].current_plan
        assert candidate.search_evaluation is captured["state"].current_evaluation
        assert captured["context"].factory.budget.candidate_check_count == captured["checks"]
        return audit(candidate, *args)

    monkeypatch.setattr(solver, "run_controlled_order_split", observed_split)
    monkeypatch.setattr(solver, "audit_core_without_search_cache", observed_audit)
    monkeypatch.setattr(
        solver,
        "run_width_optimization",
        lambda *_: pytest.fail("an ineligible phase must not be called"),
    )
    result = solver.solve(*values)
    assert calls == ["controlled_split", "audit"]
    assert "width_optimization" not in result.metrics.stage_duration_seconds
    assert result.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE
    assert result.core_audit.status is CoreAuditStatus.COMPLETED
    assert result.status is (
        SolveStatus.PUBLISHABLE_WITH_ALLOWED_DEVIATION
        if mode == "underweight"
        else SolveStatus.COMPLETE_NOT_PUBLISHABLE
        if mode == "prohibited"
        else SolveStatus.SUCCESS
    )


@pytest.mark.parametrize(
    "reason",
    tuple(
        reason
        for reason in SearchStopReason
        if reason is not SearchStopReason.LOCAL_SEARCH_COMPLETE
    ),
)
def test_width_phase_does_not_reopen_a_real_stop_from_the_previous_stage(reason, monkeypatch):
    values = width_split_solver_case(accepted_split=False)
    captured, calls = {}, []
    split, audit = solver.run_controlled_order_split, solver.audit_core_without_search_cache

    def stopped_split(state, context):
        split(state, context)
        assert not state.current_evaluation.violations
        context.factory.budget.stop_reason = reason
        captured["state"], captured["before"] = state, fingerprint(state)
        return state

    def observed_audit(candidate, *args):
        calls.append("audit")
        assert values[-1].stop_reason is reason
        assert fingerprint(captured["state"]) == captured["before"]
        assert candidate.plan is captured["state"].current_plan
        return audit(candidate, *args)

    monkeypatch.setattr(solver, "run_controlled_order_split", stopped_split)
    monkeypatch.setattr(solver, "audit_core_without_search_cache", observed_audit)
    monkeypatch.setattr(
        solver,
        "run_width_optimization",
        lambda *_: pytest.fail("a real stop cannot enter the width phase"),
    )
    result = solver.solve(*values)
    assert calls == ["audit"]
    assert result.stop_reason is reason
    assert "width_optimization" not in result.metrics.stage_duration_seconds
    assert result.diagnostic_candidate.plan is captured["state"].current_plan


def test_width_phase_exception_preserves_the_completed_split_without_audit_or_release(monkeypatch):
    values = width_split_solver_case()
    captured, calls = {}, []
    replay, width = controlled_split.run_local_search, solver.run_width_optimization

    def observed_replay(state, context):
        calls.append("old_replay")
        return replay(state, context)

    def fail_width(state, context):
        calls.append("width_optimization")
        width(state, context)
        captured["state"], captured["context"] = state, context
        captured["before"] = fingerprint(state)
        raise RuntimeError("fault after width scan")

    monkeypatch.setattr(controlled_split, "run_local_search", observed_replay)
    monkeypatch.setattr(solver, "run_width_optimization", fail_width)
    monkeypatch.setattr(
        solver,
        "audit_core_without_search_cache",
        lambda *_: pytest.fail("an interrupted width phase cannot continue to audit"),
    )
    result = solver.solve(*values)
    assert calls == ["old_replay", "width_optimization"]
    assert (
        result.status is SolveStatus.FAILED and result.stop_reason is SearchStopReason.SYSTEM_ERROR
    )
    assert result.release is None and result.core_audit.status is CoreAuditStatus.NOT_RUN
    assert result.diagnostic_candidate.plan is captured["state"].current_plan
    assert result.diagnostic_candidate.search_evaluation is captured["state"].current_evaluation
    assert fingerprint(captured["state"]) == captured["before"]
    assert result.trace == captured["context"].accepted_move_traces
    assert tuple(move.action_name for move in result.trace) == ("controlled_order_split",)
    assert result.metrics.accepted_split_count == result.metrics.accepted_move_count == 1
    assert result.metrics.candidate_check_count == values[-1].candidate_check_count
    assert (
        result.metrics.complete_candidate_evaluation_count
        == captured["context"].complete_candidate_evaluation_count
    )
    assert "width_optimization" in result.metrics.stage_duration_seconds
    assert "core_audit" not in result.metrics.stage_duration_seconds
    assert any(
        issue.phase is DiagnosticPhase.SEARCH
        and "width_optimization" in issue.message
        and "fault after width scan" in issue.message
        for issue in result.issues
    )
