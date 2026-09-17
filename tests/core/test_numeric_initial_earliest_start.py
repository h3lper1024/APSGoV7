"""A2 function tests, not a whole-solver or production-data regression.

Use real compiled readiness/selection/chain primitives. Mock only the task
boundary, plan builder and complete evaluator when testing orchestration; those
existing components and the full solver are verified separately by the user.
"""

from collections import namedtuple
from types import SimpleNamespace

import numpy as np
import pytest

from apsgo_scheduler.core import _numeric_initial_layout as layout
from apsgo_scheduler.core._numeric_rules import NumericReason, NumericRuleKind
from apsgo_scheduler.core._numeric_state import CANCELLED, INVALID, MORE_WORK, OK
from apsgo_scheduler.core._numeric_units import NumericValueError

HOUR = 3_600_000
Columns = namedtuple("Columns", "source duration earliest_start has_earliest_start")


def array(values, dtype=np.int64):
    result = np.array(values, dtype=dtype)
    result.setflags(write=False)
    return result


class Budget:
    def __init__(self, checks=None, reason="user_cancelled"):
        self.checks, self.reason, self.calls = checks, reason, 0
        self.stop_reason = None
        self.candidate_check_count = 0

    def allows_search(self):
        self.calls += 1
        if self.stop_reason is not None:
            return False
        if self.checks is not None and self.calls > self.checks:
            self.stop_reason = self.reason
            return False
        return True

    def consume_candidate_check(self):
        raise AssertionError("construction cannot charge a search candidate")


@pytest.mark.parametrize("releases,durations,periods,expected,starts,blocked", [
    ([2, 0], [2, 2], [0, 0], [1, 0], [0, 2], [False, False]),
    ([10, 0, 0], [5, 10, 1], [0, 0, 0], [1, 0, 2], [0, 10, 15], [False] * 3),
    ([2, 2, 2], [1, 1, 1], [0] * 3, [0, 1, 2], [0, 1, 2], [True, True, False]),
    ([10, 0], [3, 1], [0, 1], [0, 1], [0, 3], [True, False]),
    ([0, 2], [3, 1], [0, 4], [0, 1], [0, 3], [False, False]),
    ([-1, -10, 0], [0, 0, 1], [0] * 3, [0, 1, 2], [0, 0, 0], [False] * 3),
    ([0, 1001, 0], [1000, 1, 1], [0] * 3, [0, 2, 1], [0, 1000, 1001], [False] * 3),
])
def test_ready_order_is_stable_dynamic_same_period_and_has_no_wait(
    releases, durations, periods, expected, starts, blocked
):
    inputs = tuple(array(a) for a in (releases, durations, periods))
    before = tuple(a.copy() for a in inputs)
    budget = Budget()
    actual = layout.ready_chain_order(*inputs, budget)
    assert [value.tolist() for value in actual[:3]] == [expected, starts, blocked]
    assert budget.candidate_check_count == 0
    assert budget.stop_reason is None
    for old, new in zip(before, inputs):
        np.testing.assert_array_equal(old, new)
    assert layout._select_ready_chain_step.nopython_signatures


@pytest.mark.parametrize("work_limit", [1, 7, 256, 1024])
def test_selection_resumes_without_exposing_a_partial_choice(work_limit):
    releases, pending = array([1] * 599 + [0]), np.ones(600, np.bool_)
    cursor, status = np.array([0, -1], np.int64), np.zeros(5, np.int64)
    while True:
        before = int(cursor[0])
        code, selected, blocked = layout._select_ready_chain_step(
            releases, pending, 0, 600, 0, cursor, status, work_limit)
        assert int(cursor[0]) - before <= work_limit
        if code == MORE_WORK:
            assert selected == -1 and not blocked and status[0] == OK
        else:
            break
    assert code == OK and selected == 599 and not blocked
    assert pending.all()


@pytest.mark.parametrize("kind", ["empty", "negative_duration", "negative_period", "unsorted", "float"])
def test_invalid_summary_inputs_raise_instead_of_fallback(kind):
    values = ([0, 0], [1, 1], [0, 0])
    inputs = [array(v) for v in values]
    if kind == "empty":
        inputs = [array([])] * 3
    elif kind == "negative_duration":
        inputs[1] = array([1, -1])
    elif kind == "negative_period":
        inputs[2] = array([-1, 0])
    elif kind == "unsorted":
        inputs[2] = array([1, 0])
    else:
        inputs[0] = array([0, 0], np.float64)
    with pytest.raises(NumericValueError):
        layout.ready_chain_order(*inputs, Budget())


def test_total_clock_overflow_is_an_error_not_a_wrapped_time():
    with pytest.raises(NumericValueError, match="ready_clock"):
        layout.ready_chain_order(array([0, 0]), array([(1 << 63) - 1, 1]), array([0, 0]), Budget())


@pytest.mark.parametrize("reason", ["user_cancelled", "search_time_limit_reached"])
def test_stop_during_scan_does_not_return_partial_order(reason):
    budget = Budget(3, reason)
    assert layout.ready_chain_order(array([100] * 1000), array([0] * 1000), array([0] * 1000), budget) is None
    assert budget.stop_reason == reason and budget.candidate_check_count == 0


@pytest.mark.parametrize("damage", ["no_pending", "bad_cursor", "work_limit", "terminal"])
def test_selector_guards(damage):
    pending, cursor, status = np.ones(2, np.bool_), np.array([0, -1], np.int64), np.zeros(5, np.int64)
    limit = 256
    if damage == "no_pending":
        pending[:] = False
    elif damage == "bad_cursor":
        cursor[1] = 0
    elif damage == "work_limit":
        limit = 0
    else:
        status[0] = CANCELLED
    code, selected, ready = layout._select_ready_chain_step(array([1, 1]), pending, 0, 2, 0, cursor, status, limit)
    assert code == (CANCELLED if damage == "terminal" else INVALID)
    assert selected == -1 and not ready


def test_fixed_seed_orders_agree_with_independent_simple_selector():
    rng = np.random.default_rng(1729)
    for _ in range(100):
        count = int(rng.integers(1, 45))
        releases = array(rng.integers(-20, 200, count))
        durations = array(rng.integers(0, 30, count))
        periods = array(np.sort(rng.integers(0, 4, count)))
        remaining, order, starts, flags, clock = list(range(count)), [], [], [], 0
        while remaining:
            group = [i for i in remaining if periods[i] == periods[remaining[0]]]
            ready = [i for i in group if releases[i] <= clock]
            selected = (ready or group)[0]
            order.append(selected)
            starts.append(clock)
            flags.append(not ready)
            clock += int(durations[selected])
            remaining.remove(selected)
        actual = layout.ready_chain_order(releases, durations, periods, Budget())
        assert [a.tolist() for a in actual[:3]] == [order, starts, flags]
        assert actual[3] <= count * count


def build_plan(task, rows, offsets, ids, periods, *, generation=0):
    # A boundary test double. It deliberately does not run production validation.
    return SimpleNamespace(task_fingerprint=task.fingerprint, node_rows=array(rows),
        chain_offsets=array(offsets), chain_ids=array(ids), chain_periods=array(periods),
        generation=generation, fingerprint=f"{tuple(rows)}:{tuple(ids)}:{tuple(periods)}:{generation}")


def time_only_evaluation(task, program, quality, plan):
    """Time-only test double, not an implementation of the production evaluator."""
    clock, ends, violations = 0, [], []
    hits = np.zeros((plan.chain_ids.size + 1, len(program.rules)), np.int64)
    for c in range(plan.chain_ids.size):
        for position, row in enumerate(plan.node_rows[plan.chain_offsets[c]:plan.chain_offsets[c+1]]):
            source = int(task.nodes.source[row])
            if source >= 0 and clock < task.originals.earliest_start_ms[source]:
                severity = (int(task.originals.earliest_start_ms[source]) - clock) * 1000
                violations.append(SimpleNamespace(reason=NumericReason.EARLY_START,
                    chain_index=c, start_position=position, severity=severity))
                hits[-1, -1] += 1
            clock += int(task.nodes.duration_ms[row])
            ends.append(clock)
    return SimpleNamespace(plan_fingerprint=plan.fingerprint, plan_generation=plan.generation,
        task_fingerprint=task.fingerprint, rule_program_fingerprint=program.fingerprint,
        quality_program_fingerprint=quality.fingerprint,
        quality_key=array([len(violations), sum(v.severity for v in violations), 0, 0, 0, 0, 0, 0, plan.chain_ids.size]),
        kernel_result=SimpleNamespace(hits=hits), violations=tuple(violations),
        delivery=SimpleNamespace(node_end_ms=array(ends)))


@pytest.fixture
def case(monkeypatch):
    task = SimpleNamespace(fingerprint="task", period_ids=("P0", "P1"),
        node_ids=("A", "B", "C", "D"), source_ids=("order-A", "order-B", "order-C", "order-D"),
        nodes=SimpleNamespace(source=array([0, 1, 2, 3]), duration_ms=array([HOUR] * 4)),
        originals=SimpleNamespace(earliest_start_ms=array([0, 3 * HOUR, 0, HOUR])))
    rules = (SimpleNamespace(index=0, rule_id="custom-width", kind=NumericRuleKind.WIDTH),
             SimpleNamespace(index=1, rule_id="custom-release", kind=NumericRuleKind.EARLIEST_START))
    program = SimpleNamespace(task_fingerprint="task", fingerprint="program", rules=rules,
                               for_kind=lambda kind: tuple(r for r in rules if r.kind is kind))
    quality = SimpleNamespace(task_fingerprint="task", rule_program_fingerprint="program", fingerprint="quality")
    plan = build_plan(task, [0, 1, 2, 3], [0, 2, 4], [10, 20], [0, 0])
    evaluation = time_only_evaluation(task, program, quality, plan)
    columns = Columns(task.nodes.source, task.nodes.duration_ms, task.originals.earliest_start_ms,
                      array([True] * 4, np.bool_))
    monkeypatch.setattr(layout, "task_columns", lambda _: columns)
    monkeypatch.setattr(layout.NumericPlan, "build", staticmethod(build_plan))
    calls = []
    def evaluate(*args):
        calls.append(args[-1])
        return time_only_evaluation(*args)
    monkeypatch.setattr(layout, "evaluate_numeric_plan", evaluate)
    return SimpleNamespace(task=task, program=program, quality=quality, plan=plan, evaluation=evaluation,
        columns=columns, calls=calls, args=(task, program, quality, plan, evaluation))


def test_manual_two_chain_case_uses_real_readiness_and_preserves_inputs(case, caplog):
    before = case.plan.node_rows.copy()
    with caplog.at_level("INFO", logger=layout.__name__):
        selected, evaluated = layout.select_ready_initial_plan(*case.args, Budget())
    assert selected.node_rows.tolist() == [2, 3, 0, 1]
    assert selected.chain_ids.tolist() == [20, 10]
    assert selected.generation == 0
    assert evaluated.quality_key[0] == 0 and case.evaluation.quality_key[0] == 1
    assert len(case.calls) == 1 and evaluated.plan_fingerprint == selected.fingerprint
    np.testing.assert_array_equal(case.plan.node_rows, before)
    details = caplog.records[-1].args
    assert details["reason"] == "strict_improvement"
    assert details["base"]["early_total_ms"] == 2 * HOUR
    assert details["selected"]["early_node_count"] == 0
    assert details["reordered_chain_count"] == 2 and details["blocked_fallback_count"] == 0


@pytest.mark.parametrize("reason", ["quality_equal", "quality_worse", "other_rule_increased"])
def test_non_improving_or_unprotected_proposal_keeps_exact_baseline(case, monkeypatch, caplog, reason):
    def evaluate(*args):
        result = time_only_evaluation(*args)
        if reason == "quality_equal":
            result.quality_key = case.evaluation.quality_key
        elif reason == "quality_worse":
            result.quality_key = array([9] * 9)
        else:
            result.kernel_result.hits[0, 0] = 1
        return result
    monkeypatch.setattr(layout, "evaluate_numeric_plan", evaluate)
    with caplog.at_level("INFO", logger=layout.__name__):
        selected = layout.select_ready_initial_plan(*case.args, Budget())
    assert selected[0] is case.plan and selected[1] is case.evaluation
    details = caplog.records[-1].args
    assert details["reason"] == reason and details["reordered_chain_count"] == 0
    assert details["proposed_reordered_chain_count"] == 2
    assert details["selected"] == details["base"]


def test_disabled_rule_bypasses_lower_bounds_and_all_new_work(monkeypatch):
    def fail(*args):
        raise AssertionError("disabled branch must not inspect or evaluate")
    monkeypatch.setattr(layout, "task_columns", fail)
    program = SimpleNamespace(for_kind=lambda _: ())
    plan, evaluation, budget = object(), object(), Budget(0)
    result = layout.select_ready_initial_plan(None, program, None, plan, evaluation, budget)
    assert result[0] is plan and result[1] is evaluation and budget.calls == 0


def test_all_ready_retains_baseline_and_skips_second_evaluation(case, monkeypatch):
    case.task.originals.earliest_start_ms = array([-1] * 4)
    monkeypatch.setattr(layout, "task_columns", lambda _: case.columns._replace(
        earliest_start=case.task.originals.earliest_start_ms))
    evaluation = time_only_evaluation(case.task, case.program, case.quality, case.plan)
    result = layout.select_ready_initial_plan(*case.args[:4], evaluation, Budget())
    assert result[0] is case.plan and result[1] is evaluation and not case.calls


def test_all_blocked_is_finite_honest_and_diagnostic(case, monkeypatch, caplog):
    case.task.originals.earliest_start_ms = array([10 * HOUR] * 4)
    columns = case.columns._replace(earliest_start=case.task.originals.earliest_start_ms)
    monkeypatch.setattr(layout, "task_columns", lambda _: columns)
    evaluation = time_only_evaluation(case.task, case.program, case.quality, case.plan)
    with caplog.at_level("INFO", logger=layout.__name__):
        result = layout.select_ready_initial_plan(*case.args[:4], evaluation, Budget())
    assert result[0] is case.plan and result[1] is evaluation
    assert evaluation.delivery.node_end_ms.tolist() == [HOUR, 2*HOUR, 3*HOUR, 4*HOUR]
    details = caplog.records[-1].args
    assert details["blocked_fallback_count"] == 2
    assert [r["chain_start_ms"] for r in details["blocked_fallbacks"]] == [0, 2*HOUR]
    assert [r["node_id"] for r in details["blocked_fallbacks"]] == ["A", "C"]
    assert details["selected"]["early_node_count"] == 4


@pytest.mark.parametrize("field", ["plan_fingerprint", "plan_generation", "task_fingerprint", "rule_program_fingerprint", "quality_program_fingerprint"])
def test_wrong_evaluation_identity_is_not_a_quality_fallback(case, field):
    setattr(case.evaluation, field, "wrong")
    with pytest.raises(NumericValueError, match="matching plan"):
        layout.select_ready_initial_plan(*case.args, Budget())


def test_missing_lower_bound_is_not_silently_bypassed(case, monkeypatch):
    monkeypatch.setattr(layout, "task_columns", lambda _: case.columns._replace(has_earliest_start=array([False]*4, np.bool_)))
    with pytest.raises(NumericValueError, match="readiness"):
        layout.select_ready_initial_plan(*case.args, Budget())


def test_complete_evaluation_exception_propagates(case, monkeypatch):
    def fail(*args):
        raise NumericValueError("test.evaluation", "error")
    monkeypatch.setattr(layout, "evaluate_numeric_plan", fail)
    with pytest.raises(NumericValueError, match="test.evaluation"):
        layout.select_ready_initial_plan(*case.args, Budget())


@pytest.mark.parametrize("phase", ["summaries", "order", "materialize", "evaluate", "diagnostics"])
def test_budget_stop_preserves_stop_reason_and_exposes_no_baseline(case, monkeypatch, phase):
    budget = Budget()
    mapping = {"summaries": "_chain_summaries", "order": "ready_chain_order",
               "materialize": "_reordered_plan", "evaluate": "evaluate_numeric_plan",
               "diagnostics": "_blocked_details"}
    name = mapping[phase]
    old = getattr(layout, name)
    def stop(*args):
        result = old(*args)
        budget.stop_reason = "search_time_limit_reached"
        return result if phase == "evaluate" else None
    monkeypatch.setattr(layout, name, stop)
    assert layout.select_ready_initial_plan(*case.args, budget) is None
    assert budget.stop_reason == "search_time_limit_reached" and budget.candidate_check_count == 0
    assert case.plan.generation == 0 and case.plan.node_rows.tolist() == [0, 1, 2, 3]


@pytest.mark.parametrize("order", [[0, 0], [1], [-1, 0]])
def test_invalid_permutation_rejected(case, order):
    with pytest.raises(NumericValueError, match="permutation"):
        layout._reordered_plan(case.task, case.plan, array(order), Budget())


def test_cross_period_reordering_rejected(case):
    case.plan.chain_periods = array([0, 1])
    with pytest.raises(NumericValueError, match="same-period"):
        layout._reordered_plan(case.task, case.plan, array([1, 0]), Budget())


@pytest.mark.parametrize("damage", ["generation", "contents", "period", "identity"])
def test_materialized_plan_cannot_change_chain_semantics(case, monkeypatch, damage):
    def damaged(*args, **kwargs):
        plan = build_plan(*args, **kwargs)
        if damage == "generation":
            plan.generation += 1
        elif damage == "contents":
            plan.node_rows = array([0, 1, 2, 3])
        elif damage == "period":
            plan.chain_periods = array([0, 1])
        else:
            plan.chain_ids = array([10, 20])
        return plan
    monkeypatch.setattr(layout.NumericPlan, "build", staticmethod(damaged))
    with pytest.raises(NumericValueError):
        layout._reordered_plan(case.task, case.plan, array([1, 0]), Budget())


def test_other_rule_protection_is_per_identity_not_total(case):
    other = SimpleNamespace(index=2, kind=NumericRuleKind.NARROW_WEIGHT, rule_id="second-rule")
    program = SimpleNamespace(rules=(*case.program.rules, other))
    before = SimpleNamespace(kernel_result=SimpleNamespace(hits=np.array([[1, 0, 0]], np.int64)), quality_key=array([2]*9))
    after = SimpleNamespace(kernel_result=SimpleNamespace(hits=np.array([[0, 0, 1]], np.int64)), quality_key=array([1]*9))
    assert layout._comparison_reason(program, before, after) == "other_rule_increased"


def test_long_chain_summary_is_bounded_and_can_stop_between_steps():
    columns = Columns(array([0]*600), array([1]*600), array([600]), array([True], np.bool_))
    plan = SimpleNamespace(chain_ids=array([42]), node_rows=array(range(600)), chain_offsets=array([0, 600]))
    budget = Budget(1)
    assert layout._chain_summaries(columns, plan, budget) is None
    result = layout._chain_summaries(columns, plan, Budget())
    assert [v.tolist() for v in result] == [[600], [600]]


def test_early_diagnostics_count_nodes_and_sources_separately(case):
    case.task.nodes.source = array([0, 0, 1, 2])
    findings = tuple(SimpleNamespace(reason=NumericReason.EARLY_START, chain_index=0,
        start_position=position, severity=1000) for position in [0, 1, 1])
    evaluation = SimpleNamespace(violations=findings, quality_key=array([3, 3000, 0, 0, 0, 0, 0, 0, 2]))
    summary = layout._evaluation_summary(case.task, case.plan, evaluation)
    assert summary["early_node_count"] == 2 and summary["early_source_count"] == 1
    assert summary["early_total_ms"] == 2


def test_current_period_mixed_source_does_not_escape_earliest_check(case, monkeypatch):
    # The delayed B may be a future source in an already mixed P0 chain.
    # Its bound remains part of the chain summary; stage A cannot change periods.
    case.plan.chain_periods = array([0, 1])
    original = time_only_evaluation(case.task, case.program, case.quality, case.plan)
    result = layout.select_ready_initial_plan(*case.args[:4], original, Budget())
    assert result[0] is case.plan and result[1] is original
    assert result[1].quality_key[0] == 1 and not case.calls


def test_stop_after_logging_does_not_publish_selected_state(case, monkeypatch, caplog):
    budget = Budget()
    original_log = layout.logger.info
    def log_then_stop(*args, **kwargs):
        original_log(*args, **kwargs)
        budget.stop_reason = "user_cancelled"
    monkeypatch.setattr(layout.logger, "info", log_then_stop)
    with caplog.at_level("INFO", logger=layout.__name__):
        assert layout.select_ready_initial_plan(*case.args, budget) is None
    last = caplog.records[-1].args
    assert last["selected"] is None and "selected_chain_ids" not in last
    assert last["reason"] == "interrupted"
