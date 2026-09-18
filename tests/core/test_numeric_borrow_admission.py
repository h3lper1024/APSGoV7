"""FB2 control tests with explicit task/plan/evaluator/budget doubles.

The admission adapters, FB1 compiled guard, publication function and state commit
are real. These tests do not run candidate enumeration, real scoring or audits.
"""

from collections import namedtuple
from dataclasses import dataclass
from types import SimpleNamespace as NS

import numpy as np
import pytest

from apsgo_scheduler.core import _numeric_borrow_admission as admission
from apsgo_scheduler.core import _numeric_search as search
from apsgo_scheduler.core._numeric_borrow_readiness import borrow_readiness_step
from apsgo_scheduler.core._numeric_state import NumericChainView, NumericSearchAction, DESCRIPTOR_FIELDS
from apsgo_scheduler.core._numeric_rules import NumericRuleKind
from apsgo_scheduler.core._numeric_units import NumericValueError
from apsgo_scheduler.core.contracts import SearchStopReason


def array(values, dtype=np.int64):
    value = np.array(values, dtype=dtype)
    value.setflags(write=False)
    return value


NodeColumns = namedtuple("NodeColumns", (
    "width thickness min_temperature max_temperature present weight duration_ms role source "
    "source_period hot_roll_grade soft_hard_class split_group piece_index piece_count "
    "purpose accepted_sequence"
))
Groups = namedtuple("Groups", "parent_row target_period accepted_sequence")
Derived = namedtuple("Derived", "priority narrow_matches surface_matches same_spec_groups")


def nodes(durations, sources, periods, *, group=None, piece=None, count=None, roles=None):
    n = len(durations)
    zero = array([0] * n)
    return NodeColumns(zero, zero, zero, zero, array([[True] * 4] * n, np.bool_).reshape(n, 4),
        array([1] * n), array(durations), array(roles if roles is not None else [0] * n),
        array(sources), array(periods), zero, zero,
        array(group if group is not None else [-1] * n),
        array(piece if piece is not None else [-1] * n),
        array(count if count is not None else [-1] * n), zero, array([1] * n))


def derived(n):
    return Derived(*(array(np.zeros((0, n), np.int64)) for _ in range(4)))


@dataclass(eq=False)
class Task:
    fingerprint: str
    nodes: object
    originals: object
    split_groups: object
    period_ids: tuple = ("P0", "P1", "P2", "P3")
    ancestor_fingerprints: tuple = ()

    def __post_init__(self):
        self.source_ids = tuple(f"source-{i}" for i in range(len(self.originals.weight)))
        self.node_ids = tuple(f"node-{i}" for i in range(len(self.nodes.weight)))
        self.units = NS(width=1, thickness=1, temperature=1, weight=100)
        for name, value in zip(Derived._fields, derived(len(self.nodes.weight))):
            setattr(self, name, value)


@dataclass(frozen=True)
class Rule:
    index: int
    rule_id: str
    kind: NumericRuleKind


@dataclass
class Program:
    task_fingerprint: str
    fingerprint: str
    rules: tuple
    rule_set_fingerprint: str = "same-rules"

    def for_kind(self, kind):
        return tuple(r for r in self.rules if r.kind == kind)


@dataclass
class Quality:
    task_fingerprint: str
    rule_program_fingerprint: str
    fingerprint: str
    objectives: tuple = tuple(range(9))


class Plan:
    @classmethod
    def build(cls, task, node_rows, offsets, ids, periods, *, generation=0):
        plan = cls()
        plan.node_rows, plan.chain_offsets = array(node_rows), array(offsets)
        plan.chain_ids, plan.chain_periods = array(ids), array(periods)
        row_chain = np.full(len(task.nodes.weight), -1, np.int64)
        row_position = row_chain.copy()
        for chain, (a, b) in enumerate(zip(offsets, offsets[1:])):
            for position, row in enumerate(node_rows[a:b]):
                row_chain[row], row_position[row] = chain, position
        plan.row_to_chain, plan.row_to_position = array(row_chain), array(row_position)
        plan.generation, plan.task_fingerprint = generation, task.fingerprint
        plan.fingerprint = repr((task.fingerprint, tuple(node_rows), tuple(offsets), tuple(ids), tuple(periods)))
        return plan


class Evaluation:
    def __init__(self, task, program, quality, plan, ends, key):
        self.task_fingerprint = task.fingerprint
        self.rule_program_fingerprint, self.quality_program_fingerprint = program.fingerprint, quality.fingerprint
        self.plan_fingerprint, self.plan_generation = plan.fingerprint, plan.generation
        self.delivery = NS(node_end_ms=array(ends))
        self.quality_key = array(key)
        self.kernel_result = NS(hits=array(np.zeros((len(plan.chain_ids) + 1, len(program.rules)), np.int64)),
                                ends=self.delivery.node_end_ms, quality=self.quality_key, status=array([0] * 5))


class Budget:
    """A controllable SAME budget double, not an alternate production budget."""
    def __init__(self):
        self.candidate_check_count = 1
        self.stop_reason = None
        self.checks = 0
        self.cancel_at = None
        self.reason = SearchStopReason.USER_CANCELLED

    def allows_search(self):
        self.checks += 1
        if self.cancel_at is not None and self.checks >= self.cancel_at:
            self.stop_reason = self.reason
        return self.stop_reason is None

    def consume_candidate_check(self):
        if not self.allows_search():
            return False
        self.candidate_check_count += 1
        return True


class Workspace:
    def require_current(self, task, plan):
        if task is not self.task or plan is not self.plan:
            raise NumericValueError("test.workspace", "stale task/plan")

    def require_view(self, view):
        if (view.epoch != self.epoch or view.count != self.chain_count
                or view.base_rows is not self.plan.node_rows or view.changed_rows is not self.changed_rows
                or view.ids is not self.ids or view.starts is not self.starts
                or view.stops is not self.stops or view.periods is not self.periods
                or view.private is not self.private):
            raise NumericValueError("test.workspace", "stale view")


@pytest.fixture
def make_case(monkeypatch):
    for name, cls in (("NumericTask", Task), ("NumericPlan", Plan), ("NumericRuleProgram", Program),
                      ("NumericQualityProgram", Quality), ("NumericPlanEvaluation", Evaluation),
                      ("SolveRuntimeBudget", Budget)):
        monkeypatch.setattr(search, name, cls)
    monkeypatch.setattr(admission, "SolveRuntimeBudget", Budget)
    monkeypatch.setattr(search, "split_target_periods_match", lambda *args: True)

    def make(*, lower=15, enabled=True, old_period=2, new_period=0, split=False,
             old_rows=(0, 1), new_rows=None, duration=(10, 10), source_period=(0, 2)):
        node = nodes(duration, (0, 1), source_period)
        originals = NS(earliest_start_ms=array([0, lower]), has_earliest_start=array([True, True], np.bool_),
                       weight=array([1, 1]), due_ms=array([100, 100]), old_backlog=array([False, False], np.bool_))
        task = Task("base-task", node, originals, Groups(array([]), array([]), array([])))
        rules = (Rule(0, "other", NumericRuleKind.WIDTH),)
        if enabled:
            rules += (Rule(1, "not-a-fixed-earliest-id", NumericRuleKind.EARLIEST_START),)
        program, quality = Program(task.fingerprint, "base-program", rules), Quality(task.fingerprint, "base-program", "base-quality")
        old = Plan.build(task, list(old_rows), [0, 1, 2], [10, 20], [0, old_period])
        old_ends = np.cumsum([duration[r] for r in old_rows]).tolist()
        before = Evaluation(task, program, quality, old, old_ends, [2, 100, 0, 0, 0, 0, 0, 0, 2])
        state = search.NumericSearchState(task, program, quality, old, before)
        workspace = Workspace()
        workspace.task, workspace.plan, workspace.epoch = task, old, 0
        workspace.chain_count, workspace.group_count, workspace.node_count = 1, int(split), 2 if split else 0
        rows = list(new_rows if new_rows is not None else ((0, 2, 3) if split else (0, 1)))
        # Inactive capacity deliberately contains invalid values. Adapters must slice it off.
        if split:
            workspace.nodes = nodes([5, 5, -77], [1, 1, -77], [2, 2, -77],
                group=[0, 0, -77], piece=[1, 2, -77], count=[2, 2, -77])
            workspace.split_groups = Groups(array([1, -77]), array([new_period, -77]), array([1, -77]))
            materialized_nodes = NodeColumns(*(array(np.concatenate((getattr(node, name), getattr(workspace.nodes, name)[:2])))
                                                  for name in NodeColumns._fields))
            final_task = Task("extended-task", materialized_nodes, originals,
                             Groups(array([1]), array([new_period]), array([1])), ancestor_fingerprints=(task.fingerprint,))
            final_program, final_quality = Program(final_task.fingerprint, "extended-program", rules), Quality(final_task.fingerprint, "extended-program", "extended-quality")
        else:
            workspace.nodes = nodes([-77], [-77], [-77])
            workspace.split_groups = Groups(array([-77]), array([-77]), array([-77]))
            final_task, final_program, final_quality = task, program, quality
        workspace.derived = derived(len(workspace.nodes.weight))
        workspace.changed_count = len(rows)
        workspace.changed_rows = array(rows + [-77, -77])
        workspace.starts, workspace.stops = array([0]), array([len(rows)])
        workspace.private, workspace.ids, workspace.periods = array([True], np.bool_), array([10]), array([new_period])
        view = NumericChainView(old.node_rows, workspace.changed_rows, workspace.starts, workspace.stops,
                                workspace.private, workspace.ids, workspace.periods, 1, 0)
        final = Plan.build(final_task, rows, [0, len(rows)], [10], [new_period], generation=1)
        ends = np.cumsum([final_task.nodes.duration_ms[row] for row in rows]).tolist()
        after = Evaluation(final_task, final_program, final_quality, final, ends, [1, 50, 0, 0, 0, 0, 0, 0, 1])
        descriptor = np.full(len(DESCRIPTOR_FIELDS), -1, np.int64)
        descriptor[search.common_candidate.ACTION] = search.common_candidate.APPEND
        descriptor[search.common_candidate.SOURCE], descriptor[search.common_candidate.TARGET] = 20, 10
        descriptor[search.common_candidate.POSITION] = 1
        descriptor[search.common_candidate.REVERSE_SOURCE] = descriptor[search.common_candidate.REVERSE_TARGET] = 0
        descriptor[search.common_candidate.VARIANT] = 0
        result = NS(view=view, summary=after.kernel_result, program=program, quality_program=quality,
                    status=0, prepared=True, admissible=True, descriptor=array(descriptor),
                    affected_rows=array([]), virtual_sequence=0, split_sequence=int(split))
        budget = Budget()
        case = NS(state=state, budget=budget, workspace=workspace, result=result,
                  task=final_task, program=final_program, quality=final_quality, plan=final, evaluation=after,
                  calls=[], after_resources=None, after_plan=None, after_evaluation=None)

        def materialize_resources(*args):
            case.calls.append("resources")
            if case.after_resources:
                case.after_resources()
            return NS(task=case.task, program=case.program, quality=case.quality)

        original_build = search._build_plan
        def build(*args, **kwargs):
            case.calls.append("plan")
            plan = original_build(*args, **kwargs)
            if case.after_plan:
                case.after_plan()
            return plan

        def evaluate(task_, program_, quality_, plan_, summary):
            case.calls.append("evaluation")
            value = Evaluation(task_, program_, quality_, plan_, case.evaluation.delivery.node_end_ms,
                               case.evaluation.quality_key)
            if case.after_evaluation:
                case.after_evaluation()
            return value

        monkeypatch.setattr(search, "materialize_private_resources", materialize_resources)
        monkeypatch.setattr(search, "_build_plan", build)
        monkeypatch.setattr(search, "materialize_numeric_evaluation", evaluate)
        return case
    return make


def accepted_state(state):
    return (state.task, state.program, state.quality, state.plan, state.evaluation,
            state.accepted_move_count, state.accepted_moves, state.virtual_sequence, state.split_sequence)


def consume(case):
    return search.consume_candidate_result(case.state, case.budget, case.workspace, case.result)


def direct_commit(case, **kwargs):
    edit = search._common_candidate_edit(case.state, case.result.descriptor, 1)
    return case.state.commit(case.task, case.program, case.quality, case.plan, case.evaluation, edit, (), **kwargs)


@pytest.mark.parametrize("old_period,new_period,lower,expected", [
    (2, 0, 15, False), (2, 0, 10, True), (2, 0, 11, False), (2, 0, -1, True),
    (1, 0, 15, False), (0, 0, 15, True), (0, 1, 15, True), (0, 2, 15, True),
])
def test_admission_matrix(make_case, old_period, new_period, lower, expected):
    case = make_case(old_period=old_period, new_period=new_period, lower=lower)
    before = accepted_state(case.state)
    assert consume(case) is expected
    stats = case.state.borrow_diagnostics
    assert stats.checked == 1 and stats.passed == int(expected)
    assert case.state.complete_candidate_evaluation_count == 1 and case.budget.candidate_check_count == 1
    if expected:
        assert case.state.accepted_move_count == 1 and case.state.plan.generation == 1
    else:
        assert accepted_state(case.state) == before and not case.calls
        assert stats.rejected_new_or_deeper_borrow == 1


def test_existing_borrow_worsening_is_not_paid_for_by_better_total_score(make_case):
    case = make_case(old_period=0, lower=5, new_rows=(1, 0))
    before = accepted_state(case.state)
    assert not consume(case)
    assert accepted_state(case.state) == before
    assert case.state.borrow_diagnostics.rejected_existing_borrow_worsened == 1


def test_existing_borrow_can_improve_without_reaching_zero(make_case):
    case = make_case(old_period=0, lower=15, old_rows=(1, 0))
    assert consume(case)
    assert case.state.borrow_diagnostics.passed == 1
    assert case.state.evaluation.delivery.node_end_ms.tolist() == [10, 20]


@pytest.mark.parametrize("enabled", [False, True])
def test_rule_kind_not_configurable_name_controls_gate(make_case, enabled):
    case = make_case(enabled=enabled)
    assert consume(case) is (not enabled)
    assert case.state.borrow_diagnostics.checked == int(enabled)


@pytest.mark.parametrize("failure", ["equal", "worse", "inadmissible", "other", "unprepared", "cancelled"])
def test_preexisting_rejections_do_not_count_as_borrow_rejections(make_case, failure):
    case = make_case(lower=10)
    if failure == "equal":
        case.result.summary.quality = case.state.evaluation.quality_key
    elif failure == "worse":
        case.result.summary.quality = array([3] + [0] * 8)
    elif failure == "other":
        case.result.summary.hits = array([[1, 0], [0, 0]])
    elif failure == "unprepared":
        case.result.prepared = False
    elif failure == "cancelled":
        case.result.status = search.CANCELLED
    else:
        case.result.admissible = False
    assert not consume(case)
    assert case.state.borrow_diagnostics.checked == 0 and not case.calls


@pytest.mark.parametrize("split,lower,expected", [(True, 10, True), (True, 11, False), (False, 10, True)])
def test_private_and_materialized_guards_share_real_function(make_case, monkeypatch, split, lower, expected):
    case = make_case(split=split, lower=lower)
    native = admission.borrow_readiness_step
    shapes = []
    def spy(*args):
        shapes.append(isinstance(args[1].source, tuple))
        return native(*args)
    monkeypatch.setattr(admission, "borrow_readiness_step", spy)
    assert consume(case) is expected
    assert shapes == ([True, False] if expected else [True])
    assert case.state.borrow_diagnostics.checked == 1  # commit does not count again
    if split and not expected:
        sample = case.state.borrow_diagnostics.samples[0]
        assert not sample["has_old"] and sample["old_start_ms"] is None
    if split and expected:
        assert case.state.split_sequence == 1 and case.state.virtual_sequence == 0


def test_direct_commit_cannot_bypass_time_guard(make_case):
    case = make_case()
    before = accepted_state(case.state)
    with pytest.raises(NumericValueError, match="future_borrow_before_earliest"):
        direct_commit(case, budget=case.budget)
    assert accepted_state(case.state) == before
    assert case.state.borrow_diagnostics.checked == 0


def test_enabled_direct_commit_requires_shared_budget(make_case):
    case = make_case(lower=10)
    before = accepted_state(case.state)
    with pytest.raises(NumericValueError, match="shared runtime budget"):
        direct_commit(case)
    assert accepted_state(case.state) == before


def test_disabled_direct_commit_keeps_optional_budget(make_case):
    case = make_case(enabled=False)
    assert direct_commit(case)
    assert case.state.borrow_diagnostics.checked == 0


@pytest.mark.parametrize("damage", ["generation", "rules", "ancestors", "quality", "evaluation"])
def test_formal_binding_errors_cannot_publish(make_case, damage):
    case = make_case(lower=10, split=True)
    if damage == "generation": case.plan.generation = 2
    elif damage == "rules": case.program.rules = case.program.rules[:1]
    elif damage == "ancestors": case.task.ancestor_fingerprints = ()
    elif damage == "quality": case.quality.objectives = tuple(reversed(range(9)))
    else: case.evaluation.plan_fingerprint = "foreign"
    before = accepted_state(case.state)
    with pytest.raises(NumericValueError): direct_commit(case, budget=case.budget)
    assert accepted_state(case.state) == before


@pytest.mark.parametrize("point", ["view", "task", "summary"])
def test_stale_consumption_not_counted(make_case, point):
    case = make_case(lower=10)
    if point == "view": case.workspace.epoch += 1
    elif point == "task": case.workspace.task = object()
    else: case.result.program = object()
    with pytest.raises(NumericValueError): consume(case)
    assert case.state.borrow_diagnostics.checked == 0 and not case.calls


def test_precomputed_results_have_no_stats_until_consumed_and_old_generation_expires(make_case):
    case = make_case(lower=10)
    assert case.state.borrow_diagnostics.checked == 0
    assert consume(case)
    before = accepted_state(case.state)
    with pytest.raises(NumericValueError, match="stale"): consume(case)
    assert case.state.borrow_diagnostics.checked == 1
    assert accepted_state(case.state) == before


@pytest.mark.parametrize("stop", [SearchStopReason.USER_CANCELLED, SearchStopReason.SEARCH_TIME_LIMIT_REACHED])
@pytest.mark.parametrize("point", ["scan", "resources", "plan", "evaluation", "commit"])
def test_stop_propagates_without_any_accepted_state_mutation(make_case, monkeypatch, stop, point):
    case = make_case(lower=10)
    before = accepted_state(case.state)
    def cancel(): case.budget.stop_reason = stop
    if point == "scan":
        native = admission.borrow_readiness_step
        monkeypatch.setattr(admission, "_WORK_LIMIT", 1)
        def cancel_after_step(*args):
            value = native(*args); cancel(); return value
        monkeypatch.setattr(admission, "borrow_readiness_step", cancel_after_step)
    elif point == "resources": case.after_resources = cancel
    elif point == "plan": case.after_plan = cancel
    elif point == "evaluation": case.after_evaluation = cancel
    else:
        check = search.check_committed_borrow
        def after_check(*args):
            value = check(*args); cancel(); return value
        monkeypatch.setattr(search, "check_committed_borrow", after_check)
    assert not consume(case)
    assert case.budget.stop_reason is stop and case.budget.candidate_check_count == 1
    assert accepted_state(case.state) == before
    stats = case.state.borrow_diagnostics
    assert stats.checked == 1
    assert stats.interrupted == int(point == "scan")
    assert stats.rejected_new_or_deeper_borrow == stats.rejected_existing_borrow_worsened == 0


def test_invalid_summary_is_error_not_business_refusal(make_case):
    case = make_case(lower=10)
    case.result.summary.ends = array([10])
    before = accepted_state(case.state)
    with pytest.raises(NumericValueError, match="guard failed"): consume(case)
    assert accepted_state(case.state) == before
    stats = case.state.borrow_diagnostics
    assert stats.errors == 1 and stats.passed == 0 and stats.rejected_new_or_deeper_borrow == 0


def test_guard_identity_is_rechecked_between_bounded_steps(make_case, monkeypatch):
    case = make_case(lower=10)
    native = admission.borrow_readiness_step
    monkeypatch.setattr(admission, "_WORK_LIMIT", 1)
    def change_view(*args):
        value = native(*args); case.workspace.epoch += 1; return value
    monkeypatch.setattr(admission, "borrow_readiness_step", change_view)
    with pytest.raises(NumericValueError, match="stale"): consume(case)
    assert case.state.accepted_move_count == 0 and case.state.borrow_diagnostics.errors == 1


def test_diagnostics_bounded_and_not_added_as_scoring_violations(make_case, caplog):
    case = make_case()
    key = case.result.summary.quality.copy()
    with caplog.at_level("INFO"):
        for _ in range(5): assert not consume(case)
        admission.log_borrow_admission_summary(case.state.borrow_diagnostics)
    stats = case.state.borrow_diagnostics
    assert stats.checked == stats.rejected_new_or_deeper_borrow == 5 and len(stats.samples) == 3
    assert caplog.text.count("numeric_future_borrow_rejection") == 3
    assert "numeric_future_borrow_summary" in caplog.text
    np.testing.assert_array_equal(case.result.summary.quality, key)
    assert case.state.accepted_move_count == 0 and case.budget.candidate_check_count == 1
    snapshot = stats.snapshot(); snapshot["first_rejection_samples"][0]["row"] = 999
    assert stats.samples[0]["row"] != 999


def test_actual_numba_guard_used(make_case):
    assert consume(make_case(lower=10))
    assert borrow_readiness_step.nopython_signatures


def test_disabled_gate_does_not_read_missing_lower_bounds(make_case):
    case = make_case(enabled=False)
    case.state.task.originals.has_earliest_start = array([False, False], np.bool_)
    assert consume(case)
    assert case.state.borrow_diagnostics.checked == 0


def test_after_pass_cancellation_prevents_materialization(make_case, monkeypatch):
    case = make_case(lower=10)
    gate = search.check_candidate_borrow
    def cancel_after_check(*args):
        outcome = gate(*args)
        case.budget.stop_reason = SearchStopReason.USER_CANCELLED
        return outcome
    monkeypatch.setattr(search, "check_candidate_borrow", cancel_after_check)
    assert not consume(case) and not case.calls
    assert case.state.accepted_move_count == 0 and case.state.borrow_diagnostics.passed == 1


def test_numeric_error_is_logged_separately(make_case, caplog):
    case = make_case(lower=10)
    case.result.summary.ends = array([10, 19])
    with caplog.at_level("INFO"), pytest.raises(NumericValueError):
        consume(case)
    assert "numeric_future_borrow_summary" in caplog.text
    assert "numeric_future_borrow_rejection" not in caplog.text
    assert case.state.borrow_diagnostics.errors == 1


def test_materialization_error_does_not_publish(make_case, monkeypatch):
    case = make_case(lower=10)
    before = accepted_state(case.state)
    def fail(*args): raise NumericValueError("materialization", "synthetic failure")
    monkeypatch.setattr(search, "materialize_numeric_evaluation", fail)
    with pytest.raises(NumericValueError, match="synthetic failure"): consume(case)
    assert accepted_state(case.state) == before
    assert case.state.borrow_diagnostics.checked == case.state.borrow_diagnostics.passed == 1


def test_active_private_counts_cannot_change_between_scan_steps(make_case, monkeypatch):
    case = make_case(lower=10)
    native = admission.borrow_readiness_step
    monkeypatch.setattr(admission, "_WORK_LIMIT", 1)
    def change_active_count(*args):
        value = native(*args); case.workspace.node_count += 1; return value
    monkeypatch.setattr(admission, "borrow_readiness_step", change_active_count)
    with pytest.raises(NumericValueError, match="binding changed"): consume(case)
    assert case.state.accepted_move_count == 0


def test_consumed_attempts_retain_order_and_charge_only_existing_attempts(make_case):
    case = make_case(lower=10)
    not_admissible = NS(**vars(case.result)); not_admissible.admissible = False
    def precomputed():
        yield case.workspace, not_admissible
        yield case.workspace, case.result
        raise AssertionError("accepted result must not consume later speculative attempts")
    assert search.consume_candidate_attempts(case.state, case.budget, precomputed())
    assert case.budget.candidate_check_count == 2
    assert case.state.complete_candidate_evaluation_count == 2
    assert case.state.borrow_diagnostics.checked == 1 and case.state.accepted_move_count == 1


def test_recheck_rejects_changed_materialized_period(make_case):
    case = make_case(lower=15, new_period=2)
    before = accepted_state(case.state)
    # In isolation, change finalized geometry after consumption. Actual scoring
    # is a test double; the real commit must still apply the borrow predicate.
    case.plan.chain_periods = array([0])
    with pytest.raises(NumericValueError, match="future_borrow_before_earliest"):
        direct_commit(case, budget=case.budget)
    assert accepted_state(case.state) == before


def test_guard_and_commit_receive_the_same_budget(make_case, monkeypatch):
    case = make_case(lower=10)
    original = admission._scan
    budgets = []
    def spy(state, budget, *args, **kwargs):
        budgets.append(budget); return original(state, budget, *args, **kwargs)
    monkeypatch.setattr(admission, "_scan", spy)
    assert consume(case)
    assert len(budgets) == 2 and all(b is case.budget for b in budgets)


def test_diagnostics_are_per_state_not_shared(make_case):
    first = make_case()
    assert not consume(first)
    second = make_case()
    assert second.state.borrow_diagnostics.checked == 0
    assert second.state.borrow_diagnostics.samples == []


def test_direct_cancelled_commit_does_not_count_as_consumption(make_case):
    case = make_case(lower=10)
    before = accepted_state(case.state)
    case.budget.stop_reason = SearchStopReason.USER_CANCELLED
    assert direct_commit(case, budget=case.budget) is False
    assert accepted_state(case.state) == before
    assert case.state.borrow_diagnostics.checked == 0
