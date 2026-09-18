"""SB2 functionality only: real scopes, scans, continuations and commit control.

The solver package is not imported. Contracts and selected search definitions
are compiled from repository AST, while numeric rules, candidate computation,
resource materialization and domain objects are explicit doubles below. These
checks do not assert real scheduling quality or execute an integration suite.
"""
from __future__ import annotations

import ast
import copy
import importlib.util
import sys
from dataclasses import dataclass, field
from decimal import Context, Decimal, localcontext
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "src/apsgo_scheduler/core"


class Task(NS):
    pass


class Program(NS):
    def for_kind(self, kind):
        if kind is Kind.CHAIN_WEIGHT and self.weight_enabled:
            return (NS(values=(80, 500, 0)),)
        if kind is Kind.DELIVERY and self.delivery_enabled:
            return (NS(values=()),)
        if kind is Kind.EARLIEST_START and self.earliest_enabled:
            return (NS(values=()),)
        return ()


class Quality(NS):
    pass


class Plan(NS):
    pass


class Evaluation(NS):
    pass


class State(NS):
    pass


class Kind(Enum):
    CHAIN_WEIGHT = 1
    DELIVERY = 2
    INTER_CHAIN_WIDTH = 3
    VIRTUAL_RATIO = 4
    EARLIEST_START = 5


class Metric(Enum):
    UNDERWEIGHT_CHAIN_COUNT = 1


Action = Enum("Action", {name: index for index, name in enumerate((
    "WHOLE_CHAIN_APPEND", "WHOLE_CHAIN_PREPEND", "WHOLE_CHAIN_INSERTION",
    "REAL_NODE_RELOCATION", "CHAIN_ORDER_RELOCATION", "VIRTUAL_WEIGHT_FILL",
    "CONTROLLED_ORDER_SPLIT", "DELIVERY_INTRA_MOVE", "NODE_MOVE", "NODE_EXCHANGE",
    "BLOCK_MOVE", "BLOCK_EXCHANGE", "CHAIN_CUT", "BRIDGE_RECLAMATION"))})


class Descriptions:
    def __init__(self, task, plan, values):
        self.task, self.plan, self.values = task, plan, values


class NumericError(ValueError):
    pass


class DeferredFailure:
    def __init__(self, error):
        self.error = error


class Clock:
    value = 1.0
    increment = 0.0

    def __call__(self):
        value = self.value
        self.value += self.increment
        return value


class Token:
    cancelled = False

    def is_cancelled(self):
        return self.cancelled


def _load_file(monkeypatch, name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _definitions(path, names):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    assert {node.name for node in result} == names
    return result


class Harness:
    def __init__(self, monkeypatch):
        prefix = "_apsgo_sb2_function_test"
        package = ModuleType(prefix)
        package.__path__ = [str(CORE)]
        monkeypatch.setitem(sys.modules, prefix, package)
        contract_names = {"require_text", "require_int", "require_enum", "require_decimal",
                          "sum_decimals", "SolverPolicy", "SearchStopReason"}
        contracts = ModuleType(prefix + ".contracts")
        monkeypatch.setitem(sys.modules, contracts.__name__, contracts)
        contracts.__dict__.update(dataclass=dataclass, field=field, Decimal=Decimal,
            Context=Context, localcontext=localcontext, Enum=Enum,
            CONSTRUCTION_ORDER_KEY="solverpy_stable_order_v1", NUMERIC_SEMANTICS_KEY="solverpy_float_epsilon_1e_9",
            INTEGER_NUMERIC_SEMANTICS_KEY="integer_physical_ms_half_up_v1")
        nodes = _definitions(CORE / "contracts.py", contract_names)
        future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
        exec(compile(ast.fix_missing_locations(ast.Module([future, *nodes], [])), str(CORE / "contracts.py"), "exec"), contracts.__dict__)
        self.budget_module = _load_file(monkeypatch, prefix + ".budget", CORE / "budget.py")
        self.phases = _load_file(monkeypatch, prefix + "._search_phase_budget", CORE / "_search_phase_budget.py")
        self.clock, self.token = Clock(), Token()
        self.budget = self.budget_module.SolveRuntimeBudget(0, 10000, 10100, 100000, 0,
                                                        self.token, clock=self.clock)
        self.Stop = contracts.SearchStopReason
        common = NS(APPEND=0, PREPEND=1, INSERT=2, REAL_MOVE=3, ORDER=4, FILL=5, SPLIT=6,
            ACTION=0, SOURCE=1, TARGET=2, NODE=3, POSITION=4, START=5, STOP=6,
            OTHER_START=7, OTHER_STOP=8, REVERSE_SOURCE=9, REVERSE_TARGET=10, OWNER=11, VARIANT=12)
        common.CandidateCheckPolicy = lambda *args, **kwargs: (args, tuple(sorted(kwargs.items())))
        search = ModuleType(prefix + "._numeric_search")
        monkeypatch.setitem(sys.modules, search.__name__, search)
        search.__dict__.update(dataclass=dataclass, field=field, np=np,
            NumericTask=Task, NumericRuleProgram=Program, NumericQualityProgram=Quality,
            NumericPlan=Plan, NumericPlanEvaluation=Evaluation, NumericSearchState=State,
            NumericRuleKind=Kind, NumericMetricKind=Metric, NumericSearchAction=Action,
            NumericCandidateDescriptors=Descriptions, DESCRIPTOR_FIELDS=tuple(range(13)),
            readonly=lambda values, dtype: np.array(values, dtype=dtype),
            NumericValueError=NumericError, NumericDeferredCandidateFailure=DeferredFailure,
            SolveRuntimeBudget=self.budget_module.SolveRuntimeBudget, SearchStopReason=self.Stop,
            common_candidate=common, _GENERATED_VIRTUAL=2, OK=0, CANCELLED=1, CAPACITY=2,
            int64=lambda v, name: int(v), checked_sum=lambda values, name: sum(values),
            chain_rows=lambda view, i: view.chains[int(i)])
        names = {"NumericCandidateEdit", "NumericAcceptedMove", "_quality", "_common_candidate_edit",
            "_preserves_other_hard_rules", "_validate_search_inputs", "_underweight_indices",
            "_first_description", "_prepare_current_description", "_grow_candidate_workspace",
            "_try_first_description", "consume_candidate_result", "publish_accepted_candidate",
            "improve_numeric_whole_chain", "improve_numeric_real_node_relocation",
            "improve_numeric_virtual_weight_fill", "improve_numeric_chain_order",
            "improve_numeric_controlled_split", "_run_numeric_local_search",
            "run_numeric_basic_slice", "run_numeric_split_slice"}
        nodes = _definitions(CORE / "_numeric_search.py", names)
        tree = ast.parse((CORE / "_numeric_search.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NumericSearchState")
        commit = copy.deepcopy(next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "commit"))
        commit.name = "_actual_commit"
        exec(compile(ast.fix_missing_locations(ast.Module([future, *nodes, commit], [])), str(CORE / "_numeric_search.py"), "exec"), search.__dict__)
        monkeypatch.setattr(State, "commit", search._actual_commit, raising=False)
        self.search, self.common = search, common
        self.stage = _load_file(monkeypatch, prefix + "._numeric_stage_operators", CORE / "_numeric_stage_operators.py")
        self.state = self.make_state()
        self.calls, self.prepared_counts, self.workspaces, self.joins = [], [], [], []
        self.accept_remaining, self.accept_action = 0, None
        self.direct, self.reversible, self.split_eligible, self.prepared = True, False, False, True
        self.before_capture = self.before_prepare = self.before_materialize = None
        self.before_commit_guard = None
        self.borrow_allowed = True
        search._first_workspace = self.workspace
        search._first_reverse_allowed = lambda *a: (self.reversible, np.zeros(5, np.int64))
        search._first_join_direct = self.join
        search._first_move_eligible = lambda c, r, donor, p, dw, tw, minimum, maximum: (
            len(donor) >= 2 and dw - c.weight[donor[p]] >= minimum and tw + c.weight[donor[p]] <= maximum,
            np.zeros(5, np.int64))
        search._check_search_status = lambda status: None if status[0] == 0 else (_ for _ in ()).throw(NumericError("precheck"))
        search.evaluate_numeric_split = lambda *a: NS(eligible=self.split_eligible and self.state.split_sequence == 0)
        search.preview_numeric_chain_order_quality = lambda *a: tuple(self.state.evaluation.quality_key - 1)
        search.capture_candidate_result = self.capture
        common.prepare_candidate_attempt = self.prepare
        common.compute_candidate_attempt = self.capture
        search.check_candidate_borrow = lambda *a: self.borrow_allowed
        search.check_committed_borrow = self.commit_guard
        search.materialize_private_resources = self.materialize
        search.materialize_numeric_evaluation = lambda t, p, q, plan, summary: self.make_evaluation(plan, summary.quality)
        search._build_plan = self.build_plan
        search.split_target_periods_match = lambda *a: True

    def make_state(self):
        task = Task(fingerprint="task", prototype_ids=("v",), nodes=NS(
            source=np.arange(6, dtype=np.int64), weight=np.array([20, 20, 90, 90, 20, 20]),
            role=np.zeros(6, np.int64)))
        program = Program(fingerprint="rules", task_fingerprint=task.fingerprint, rules=(),
                          weight_enabled=True, delivery_enabled=True, earliest_enabled=True)
        quality = Quality(fingerprint="quality", task_fingerprint=task.fingerprint,
                          rule_program_fingerprint=program.fingerprint)
        plan = Plan(fingerprint="plan0", task_fingerprint=task.fingerprint, generation=0,
                    node_rows=np.arange(6, dtype=np.int64), chain_offsets=np.array([0, 2, 4, 6]),
                    chain_ids=np.array([10, 20, 30]), chain_periods=np.array([0, 0, 0]))
        evaluation = self.make_evaluation(plan, np.array([10000] + [0] * 8))
        return State(task=task, program=program, quality=quality, plan=plan, evaluation=evaluation,
                     complete_candidate_evaluation_count=0, accepted_move_count=0,
                     bridge_required_candidate_count=0, virtual_sequence=0, split_sequence=0,
                     replay_count=0, accepted_moves=())

    def make_evaluation(self, plan, values):
        return Evaluation(task_fingerprint="task", rule_program_fingerprint="rules",
            quality_program_fingerprint="quality", plan_fingerprint=plan.fingerprint,
            plan_generation=plan.generation, quality_key=np.asarray(values),
            kernel_result=NS(hits=np.zeros((4, 1), np.int64)),
            chain_results=[NS(metrics=[NS(kind=Metric.UNDERWEIGHT_CHAIN_COUNT, numerator=x)]) for x in (1, 0, 1)])

    def workspace(self, state):
        class Workspace:
            def __init__(self):
                self.task, self.plan = state.task, state.plan
                self.chains = tuple(self.plan.node_rows[a:b].copy() for a, b in zip(self.plan.chain_offsets, self.plan.chain_offsets[1:]))
                self.view_value = NS(count=len(self.chains), chains=self.chains)
                self.ids, self.periods = self.plan.chain_ids.copy(), self.plan.chain_periods.copy()
                self.chain_count, self.node_count, self.group_count = len(self.chains), 0, 0
                self.nodes = NS(role=np.empty(0, np.int64), accepted_sequence=np.empty(0, np.int64))
                self.split_groups = NS(accepted_sequence=np.empty(0, np.int64))
                self.resets = self.grows = 0
            def reset(self):
                self.resets += 1
            def grow(self):
                self.grows += 1
            def require_current(self, task, plan):
                if task is not self.task or plan is not self.plan:
                    raise NumericError("stale workspace")
            def require_view(self, view):
                if view is not self.view_value:
                    raise NumericError("stale view")
        work = Workspace()
        self.workspaces.append(work)
        columns = NS(weight=state.task.nodes.weight, role=state.task.nodes.role)
        weights = np.array([columns.weight[c].sum() for c in work.chains])
        return work, work.view_value, columns, None, weights, np.array([30, 10, 20])

    def join(self, columns, rules, source, target, position, sr, tr):
        self.joins.append((tuple(source), tuple(target), int(position), bool(sr), bool(tr)))
        return self.direct, np.zeros(5, np.int64)

    def prepare(self, work, program, quality, descriptions, index, policy, **kwargs):
        self.prepared_counts.append(self.budget.candidate_check_count)
        if self.before_prepare:
            self.before_prepare(kwargs)
        if kwargs.get("allows_continue") and not kwargs["allows_continue"]():
            return NS(status=1, prepared=False)
        return NS(status=0, prepared=self.prepared)

    def capture(self, work, program, quality, descriptions, index, policy, **kwargs):
        row = descriptions.values[index].copy()
        self.calls.append((tuple(map(int, row)), self.budget.candidate_check_count, self.state.plan.generation))
        if self.before_capture:
            self.before_capture(kwargs)
        if kwargs.get("allows_continue") and not kwargs["allows_continue"]():
            return NS(status=1, prepared=False, view=work.view_value, program=program, quality_program=quality)
        action = int(row[0])
        accept = self.accept_remaining > 0 and (self.accept_action is None or action == self.accept_action)
        if accept:
            self.accept_remaining -= 1
        if action == self.common.SPLIT:
            work.group_count = 1
            work.split_groups.accepted_sequence = np.array([self.state.split_sequence + 1])
        summary = NS(quality=self.state.evaluation.quality_key.copy(), hits=np.zeros((4, 1), np.int64))
        if accept:
            summary.quality[0] -= 1
        return NS(status=0, prepared=True, admissible=True, view=work.view_value,
            program=program, quality_program=quality, descriptor=row, summary=summary,
            virtual_sequence=self.state.virtual_sequence,
            split_sequence=self.state.split_sequence + work.group_count,
            affected_rows=np.array([0]))

    def materialize(self, work, program, quality):
        if self.before_materialize:
            self.before_materialize()
        return NS(task=self.state.task, program=program, quality=quality)

    def commit_guard(self, *args):
        if self.before_commit_guard:
            self.before_commit_guard()
        return True

    def build_plan(self, task, plan, chains, ids, periods, **kwargs):
        # Deliberately no physical move: these tests isolate control, not rules.
        return Plan(**(plan.__dict__ | {"fingerprint": f"plan{plan.generation + 1}", "generation": plan.generation + 1}))

    def operator(self, kind, amount=5, progress=None, seconds=100):
        progress = progress or self.stage.NumericOperatorProgress(kind)
        result = self.stage.run_operator_slice(self.state, self.budget, progress=progress,
            candidate_checks=amount, time_slice_seconds=seconds, pair_scan_slack_weight=40)
        return result, progress


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


@pytest.mark.parametrize("kind", ("whole", "node", "fill", "order", "split"))
def test_each_operator_is_bounded_and_resumable(h, kind):
    h.split_eligible = True
    first, progress = h.operator(kind, 2)
    first_calls = tuple(h.calls)
    assert first.budget.used_candidate_checks == 2
    assert h.budget.stop_reason is None
    second, _ = h.operator(kind, 3, progress)
    assert second.budget.used_candidate_checks == 3
    assert h.budget.candidate_check_count == 5
    assert tuple(h.calls[:2]) == first_calls
    descriptors = [call[0] for call in h.calls]
    assert len(descriptors) == len(set(descriptors))
    assert all(w.resets for w in h.workspaces)
    assert not any(hasattr(progress, name) for name in ("workspace", "generator", "candidates"))


@pytest.mark.parametrize("kind", ("whole", "node", "fill", "order", "split"))
def test_sliced_enumeration_matches_one_unbroken_scan(h, kind):
    h.split_eligible = True
    _, p = h.operator(kind, 10000)
    expected = [call[0] for call in h.calls]
    expected_count = h.budget.candidate_check_count
    assert p.complete
    h.calls.clear(); h.budget.candidate_check_count = 0
    p = h.stage.NumericOperatorProgress(kind)
    for _ in range(300):
        _, p = h.operator(kind, 3, p)
        if p.complete:
            break
    assert p.complete
    assert [call[0] for call in h.calls] == expected
    assert h.budget.candidate_check_count == expected_count


@pytest.mark.parametrize("kind", ("whole", "node", "fill", "order"))
def test_staged_scan_retains_legacy_candidate_order_and_charge(h, kind):
    functions = {"whole": h.search.improve_numeric_whole_chain,
        "node": h.search.improve_numeric_real_node_relocation,
        "fill": h.search.improve_numeric_virtual_weight_fill,
        "order": h.search.improve_numeric_chain_order}
    fn = functions[kind]
    args = (h.state.task, h.state.program, h.state.quality, h.state, h.budget)
    fn(*args, **({"pair_scan_slack_weight": 40} if kind == "whole" else {}))
    expected = [call[0] for call in h.calls]
    expected_count = h.budget.candidate_check_count
    h.calls.clear(); h.budget.candidate_check_count = 0
    h.operator(kind, 10000)
    assert [call[0] for call in h.calls] == expected
    assert h.budget.candidate_check_count == expected_count


@pytest.mark.parametrize("reversible,direct", ((False, False), (True, False), (True, True)))
def test_whole_orientations_and_bridge_detection_match_legacy(h, reversible, direct):
    h.reversible, h.direct = reversible, direct
    h.search.improve_numeric_whole_chain(h.state.task, h.state.program, h.state.quality,
                                        h.state, h.budget, pair_scan_slack_weight=40)
    expected = [call[0] for call in h.calls]
    charge, bridges = h.budget.candidate_check_count, h.state.bridge_required_candidate_count
    h.calls.clear(); h.budget.candidate_check_count = 0; h.state.bridge_required_candidate_count = 0
    h.operator("whole", 10000)
    assert [call[0] for call in h.calls] == expected
    assert (h.budget.candidate_check_count, h.state.bridge_required_candidate_count) == (charge, bridges)


def test_four_siblings_keep_their_reserved_checks(h):
    p = h.stage.NumericBasicProgress()
    result = h.search.run_numeric_basic_slice(h.state, h.budget, candidate_checks=40,
        time_slice_seconds=100, progress=p, pair_scan_slack_weight=40)
    assert [c.budget.candidate_limit for c in result.children] == [20, 10, 4, 6]
    assert [c.budget.used_candidate_checks for c in result.children] == [20, 10, 4, 6]
    assert result.budget.used_candidate_checks == h.budget.candidate_check_count == 40
    assert h.budget.stop_reason is None
    assert not result.scan_complete


@pytest.mark.parametrize("amount", (0, 1, 3, 7, 19, 41))
def test_small_and_odd_parent_quotas_never_mint_checks(h, amount):
    h.budget.candidate_check_count = 9
    result = h.stage.run_basic_slice(h.state, h.budget, candidate_checks=amount,
        time_slice_seconds=100, progress=h.stage.NumericBasicProgress(), pair_scan_slack_weight=40)
    assert sum(c.budget.used_candidate_checks for c in result.children) == result.budget.used_candidate_checks
    assert h.budget.candidate_check_count - 9 <= amount
    assert sum(c.budget.candidate_limit for c in result.children) <= amount


@pytest.mark.parametrize("kind", ("whole", "node", "fill", "order", "split"))
def test_last_charged_candidate_commits_through_actual_consumption_and_commit(h, kind):
    h.split_eligible = True
    h.accept_remaining = 1
    result, p = h.operator(kind, 1)
    assert h.state.accepted_move_count == 1
    assert h.state.plan.generation == 1
    assert h.budget.candidate_check_count == 1
    assert h.budget.stop_reason is None
    assert len(h.calls) == 1
    assert not p.complete
    assert result.budget.used_candidate_checks == 1


@pytest.mark.parametrize("cutoff", ("soft", "global", "cancel"))
def test_continuation_distinguishes_phase_deadline_from_real_stop(h, cutoff):
    h.accept_remaining = 1
    def change(_):
        if cutoff == "soft": h.clock.value = 50
        elif cutoff == "global": h.clock.value = 10001
        else: h.token.cancelled = True
    h.before_capture = change
    result, _ = h.operator("whole", 1, seconds=2)
    assert h.state.accepted_move_count == int(cutoff == "soft")
    assert h.budget.candidate_check_count == 1
    if cutoff == "soft":
        assert h.budget.stop_reason is None
        assert result.budget.soft_overrun_seconds > 0
    else:
        assert result.budget.reason is h.phases.SearchPhaseExit.GLOBAL_STOP


@pytest.mark.parametrize("where", ("materialize", "commit_guard"))
@pytest.mark.parametrize("stop", ("cancel", "global"))
def test_stop_after_evaluation_prevents_official_state_change(h, where, stop):
    h.accept_remaining = 1
    old = h.state.plan
    def interrupt():
        if stop == "cancel": h.token.cancelled = True
        else: h.clock.value = 10001
    setattr(h, "before_" + where, interrupt)
    h.operator("whole", 1)
    assert h.state.plan is old
    assert h.state.accepted_move_count == 0
    assert h.budget._active_phase is None


def test_guard_still_rejects_a_better_quality(h):
    h.accept_remaining = 1
    h.borrow_allowed = False
    h.operator("whole", 1)
    assert h.state.accepted_move_count == 0
    assert h.budget.candidate_check_count == 1


def test_rule_disabled_stage_still_honors_continuation_at_commit(h):
    h.state.program.earliest_enabled = False
    h.accept_remaining = 1
    h.before_materialize = lambda: setattr(h.token, "cancelled", True)
    h.operator("whole", 1)
    assert h.state.accepted_move_count == 0


def test_exact_global_last_attempt_finishes_but_no_new_work(h):
    h.budget.candidate_check_limit = 1
    h.accept_remaining = 1
    h.operator("whole", 1)
    assert h.state.accepted_move_count == 1
    assert h.budget.candidate_check_count == 1
    h.operator("node", 5)
    assert h.budget.candidate_check_count == 1
    assert h.budget.stop_reason is h.Stop.CANDIDATE_LIMIT_REACHED


def test_nested_charge_cannot_reuse_active_lease(h):
    h.before_capture = lambda _: h.budget.consume_candidate_check()
    with pytest.raises(ValueError, match="close the current attempt"):
        h.operator("whole", 2)
    assert h.budget.candidate_check_count == 1
    assert h.budget._active_phase is None
    assert h.state.accepted_move_count == 0


def test_soft_yield_during_uncharged_split_preparation_is_not_cancel(h):
    h.split_eligible = True
    h.before_prepare = lambda _: setattr(h.clock, "value", 10)
    result, p = h.operator("split", 2, seconds=2)
    assert result.budget.reason is h.phases.SearchPhaseExit.TIME_SLICE_EXHAUSTED
    assert h.budget.candidate_check_count == 0
    assert h.budget.stop_reason is None
    assert p.cursor == (0, 0)
    h.before_prepare = None
    h.operator("split", 1, p)
    assert h.budget.candidate_check_count == 1
    assert h.prepared_counts == [0, 0]


def test_split_preparation_precedes_original_charge(h):
    h.split_eligible = True
    h.operator("split", 3)
    assert h.prepared_counts == [0, 1, 2]


def test_unprepared_split_costs_no_candidate_check(h):
    h.split_eligible, h.prepared = True, False
    result, p = h.operator("split", 3)
    assert p.complete
    assert h.prepared_counts == [0] * 6
    assert result.budget.used_candidate_checks == 0


def test_no_eligible_split_still_obeys_time_slice(h):
    h.clock.increment = .1
    result, _ = h.operator("split", 100, seconds=.8)
    assert result.budget.reason is h.phases.SearchPhaseExit.TIME_SLICE_EXHAUSTED
    assert h.budget.candidate_check_count == 0
    assert h.budget.stop_reason is None


def test_scan_and_replay_are_independently_callable(h):
    cycle = h.stage.NumericSplitProgress()
    h.split_eligible, h.accept_remaining, h.accept_action = True, 1, h.common.SPLIT
    scan = h.stage.run_split_scan_slice(h.state, h.budget, candidate_checks=1,
        time_slice_seconds=100, progress=cycle, pair_scan_slack_weight=40)
    assert cycle.pending_replay
    assert h.state.replay_count == 0
    assert scan.budget.used_candidate_checks == 1
    replay = h.stage.run_replay_slice(h.state, h.budget, candidate_checks=20,
        time_slice_seconds=100, progress=cycle, pair_scan_slack_weight=40)
    assert h.state.replay_count == 1
    h.stage.run_replay_slice(h.state, h.budget, candidate_checks=20,
        time_slice_seconds=100, progress=cycle, pair_scan_slack_weight=40)
    assert h.state.replay_count == 1
    assert all(call[0][0] != h.common.SPLIT for call in h.calls[1:])
    assert replay.budget.used_candidate_checks <= 20


def test_combined_split_halves_respect_parent_and_leave_later_budget(h):
    cycle = h.stage.NumericSplitProgress()
    h.split_eligible, h.accept_remaining, h.accept_action = True, 1, h.common.SPLIT
    result = h.search.run_numeric_split_slice(h.state, h.budget, candidate_checks=40,
        time_slice_seconds=100, progress=cycle, pair_scan_slack_weight=40)
    assert [c.budget.candidate_limit for c in result.children] == [20, 20]
    assert sum(c.budget.used_candidate_checks for c in result.children) == result.budget.used_candidate_checks <= 40
    assert h.state.split_sequence == 1
    assert h.state.replay_count == 1
    assert h.budget.stop_reason is None
    before = h.budget.candidate_check_count
    h.operator("whole", 2)
    assert h.budget.candidate_check_count == before + 2


def test_no_split_no_replay(h):
    cycle = h.stage.NumericSplitProgress()
    result = h.stage.run_split_slice(h.state, h.budget, candidate_checks=20,
        time_slice_seconds=100, progress=cycle, pair_scan_slack_weight=40)
    assert h.state.replay_count == 0
    assert result.children[1].budget.reason is h.phases.SearchPhaseExit.NOT_APPLICABLE
    assert h.budget.candidate_check_count == 0


def test_zero_replay_grant_preserves_pending_work(h):
    cycle = h.stage.NumericSplitProgress()
    h.split_eligible, h.accept_remaining, h.accept_action = True, 1, h.common.SPLIT
    result = h.stage.run_split_slice(h.state, h.budget, candidate_checks=1,
        time_slice_seconds=100, progress=cycle, pair_scan_slack_weight=40)
    assert cycle.pending_replay and h.state.replay_count == 0
    assert result.pending_replay


def test_new_generation_discards_old_completion_and_cursor(h):
    _, p = h.operator("whole", 10000)
    assert p.complete
    h.calls.clear()
    h.state.plan = h.build_plan(h.state.task, h.state.plan, (), (), ())
    h.state.evaluation = h.make_evaluation(h.state.plan, np.array([10000] + [0] * 8))
    h.operator("whole", 1, p)
    assert len(h.calls) == 1 and not p.complete
    assert p.binding[2] == 1


def test_same_generation_completed_scan_does_not_repeat(h):
    _, p = h.operator("order", 1000)
    before = h.budget.candidate_check_count
    result, _ = h.operator("order", 1000, p)
    assert result.scan_complete
    assert h.budget.candidate_check_count == before


def test_same_generation_option_change_is_not_silent(h):
    _, p = h.operator("whole", 1)
    with pytest.raises(ValueError, match="options changed"):
        h.stage.run_operator_slice(h.state, h.budget, progress=p, candidate_checks=1,
            time_slice_seconds=1, pair_scan_slack_weight=41)


def test_candidate_identity_change_cannot_commit(h):
    h.accept_remaining = 1
    def change(_):
        h.state.plan.fingerprint = "unexpected"
    h.before_capture = change
    with pytest.raises(ValueError, match="identity|generation"):
        h.operator("whole", 1)
    assert h.state.accepted_move_count == 0
    assert h.budget._active_phase is None


def test_closed_continuation_cannot_authorize_another_candidate(h):
    captured = []
    h.before_capture = lambda kwargs: captured.append(kwargs["allows_continue"])
    h.operator("whole", 1)
    assert captured[0]() is False


def test_error_does_not_refund_or_poison_sibling_scope(h):
    h.before_capture = lambda _: (_ for _ in ()).throw(RuntimeError("real error"))
    with pytest.raises(RuntimeError, match="real error"):
        h.operator("whole", 1)
    assert h.budget.candidate_check_count == 1
    assert h.budget._active_phase is None
    h.before_capture = None
    h.operator("node", 1)
    assert h.budget.candidate_check_count == 2


@pytest.mark.parametrize("kind", ("node", "fill", "order"))
def test_original_applicability_is_retained(h, kind):
    h.state.program.weight_enabled = False
    h.state.program.delivery_enabled = False
    result, p = h.operator(kind, 4)
    assert result.budget.reason is h.phases.SearchPhaseExit.NOT_APPLICABLE
    assert h.budget.candidate_check_count == 0 and p.complete


def test_deadlines_and_total_limit_not_reset(h):
    original = (h.budget.search_deadline_monotonic, h.budget.final_deadline_monotonic, h.budget.candidate_check_limit)
    h.stage.run_basic_slice(h.state, h.budget, candidate_checks=20,
        time_slice_seconds=100, progress=h.stage.NumericBasicProgress(), pair_scan_slack_weight=40)
    assert (h.budget.search_deadline_monotonic, h.budget.final_deadline_monotonic, h.budget.candidate_check_limit) == original


def test_declared_no_stage_direct_entry_uses_no_phase(h):
    h.budget.candidate_check_limit = 2
    h.search.improve_numeric_whole_chain(h.state.task, h.state.program, h.state.quality,
                                        h.state, h.budget, pair_scan_slack_weight=40)
    assert h.budget._active_phase is None
    assert h.budget.candidate_check_count == 2
    assert h.budget.stop_reason is h.Stop.CANDIDATE_LIMIT_REACHED


def test_split_scan_result_exposes_pending_replay(h):
    cycle = h.stage.NumericSplitProgress()
    h.split_eligible, h.accept_remaining, h.accept_action = True, 1, h.common.SPLIT
    result = h.stage.run_split_scan_slice(h.state, h.budget, candidate_checks=1,
        time_slice_seconds=100, progress=cycle, pair_scan_slack_weight=40)
    assert result.pending_replay


def test_split_without_acceptance_matches_legacy_without_replay(h):
    h.split_eligible = True
    h.search.improve_numeric_controlled_split(h.state, h.budget, pair_scan_slack_weight=40)
    expected, count = [x[0] for x in h.calls], h.budget.candidate_check_count
    assert h.state.replay_count == 0
    h.calls.clear(); h.budget.candidate_check_count = 0; h.budget.stop_reason = None
    h.operator("split", 100)
    assert [x[0] for x in h.calls] == expected
    assert h.budget.candidate_check_count == count
    assert h.budget.stop_reason is None


def test_capacity_retry_reuses_only_the_same_charged_attempt(h):
    original = h.search.capture_candidate_result
    count = 0
    def capacity_first(*args, **kwargs):
        nonlocal count
        count += 1
        assert kwargs["allows_continue"]()
        if count == 1:
            return NS(status=h.search.CAPACITY)
        return original(*args, **kwargs)
    h.search.capture_candidate_result = capacity_first
    h.accept_remaining = 1
    h.operator("whole", 1)
    assert count == 2 and h.workspaces[0].grows == 1
    assert h.budget.candidate_check_count == h.state.accepted_move_count == 1


def test_capacity_retry_still_stops_at_global_deadline(h):
    original = h.search.capture_candidate_result
    def capacity_first(*args, **kwargs):
        if h.workspaces[0].grows == 0:
            h.clock.value = 10001
            return NS(status=h.search.CAPACITY)
        return original(*args, **kwargs)
    h.search.capture_candidate_result = capacity_first
    h.accept_remaining = 1
    h.operator("whole", 1)
    assert h.budget.candidate_check_count == 1
    assert h.state.accepted_move_count == 0
    assert h.budget.stop_reason is h.Stop.SEARCH_TIME_LIMIT_REACHED


def test_precharge_capacity_growth_does_not_add_checks(h):
    h.split_eligible = True
    original = h.common.prepare_candidate_attempt
    def prepare(*args, **kwargs):
        if h.workspaces[0].grows == 0:
            return NS(status=h.search.CAPACITY)
        return original(*args, **kwargs)
    h.common.prepare_candidate_attempt = prepare
    h.operator("split", 1)
    assert h.workspaces[0].grows == 1
    assert h.budget.candidate_check_count == 1
    assert h.prepared_counts == [0]


def test_ineligible_node_prefix_resumes_without_repeating_eligibility(h):
    seen = []
    def ineligible(c, r, donor, position, *a):
        seen.append((tuple(donor), position))
        h.clock.value += 1
        return False, np.zeros(5, np.int64)
    h.search._first_move_eligible = ineligible
    first, p = h.operator("node", 100, seconds=1)
    assert first.budget.used_candidate_checks == 0
    h.operator("node", 100, p, seconds=100)
    # The same donor can appear for two target chains, but not twice for one.
    assert seen == [((2, 3), 0), ((2, 3), 1), ((2, 3), 0), ((2, 3), 1)]


def test_parent_scope_is_restored_after_candidate_error(h):
    h.before_capture = lambda _: (_ for _ in ()).throw(RuntimeError("failed probe"))
    with h.budget.phase_scope("parent", 50, 200) as parent:
        with pytest.raises(RuntimeError, match="failed probe"):
            h.operator("whole", 3)
        assert h.budget._active_phase is parent
        h.before_capture = None
        h.operator("node", 2)
    assert h.budget._active_phase is None
    assert parent.result.used_candidate_checks == 3


@pytest.mark.parametrize("kind", ("whole", "node", "fill", "order", "split"))
def test_random_slice_sizes_preserve_complete_candidate_sequence(h, kind):
    h.split_eligible = True
    h.operator(kind, 10000)
    expected, charge = [x[0] for x in h.calls], h.budget.candidate_check_count
    h.calls.clear(); h.budget.candidate_check_count = 0
    p = h.stage.NumericOperatorProgress(kind)
    rng = np.random.default_rng(923)
    for _ in range(200):
        h.operator(kind, int(rng.integers(1, 8)), p)
        if p.complete:
            break
    assert p.complete and [x[0] for x in h.calls] == expected
    assert h.budget.candidate_check_count == charge


def test_prior_stages_completion_is_invalid_after_a_later_acceptance(h):
    progress = h.stage.NumericBasicProgress()
    h.accept_action, h.accept_remaining = h.common.ORDER, 1
    result = h.stage.run_basic_slice(h.state, h.budget, candidate_checks=10000,
        time_slice_seconds=100, progress=progress, pair_scan_slack_weight=40)
    assert h.state.plan.generation == 1
    assert not result.scan_complete
    assert not progress.operators["whole"].complete_for(h.state)
    assert progress.operators["order"].complete_for(h.state)


def test_cancelled_split_does_not_start_replay(h):
    cycle = h.stage.NumericSplitProgress()
    h.split_eligible, h.accept_remaining = True, 1
    h.before_prepare = lambda _: setattr(h.token, "cancelled", True)
    result = h.stage.run_split_slice(h.state, h.budget, candidate_checks=40,
        time_slice_seconds=100, progress=cycle, pair_scan_slack_weight=40)
    assert h.state.replay_count == h.state.split_sequence == h.budget.candidate_check_count == 0
    assert len(result.children) == 1
    assert result.budget.reason is h.phases.SearchPhaseExit.GLOBAL_STOP


def test_remaining_replay_cannot_clear_real_cancel(h):
    cycle = h.stage.NumericSplitProgress(pending_replay=True)
    h.token.cancelled = True
    result = h.stage.run_replay_slice(h.state, h.budget, candidate_checks=40,
        time_slice_seconds=100, progress=cycle, pair_scan_slack_weight=40)
    assert h.state.replay_count == 0 and cycle.pending_replay
    assert result.budget.reason is h.phases.SearchPhaseExit.GLOBAL_STOP
