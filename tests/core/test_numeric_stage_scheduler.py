"""SB3 functional tests only: actual budget/cursors/control with explicit doubles.

No solver package entry, production score, database or full scheduling run is
loaded. Legacy enumeration is extracted from the repository for ordered parity;
its edge predicate is an explicit deterministic table double. Numba cursor
functions really compile. Each synthetic batch/consumer boundary is named below.
"""
from __future__ import annotations

import ast
import importlib.util
import logging
import sys
import types
from collections import deque, namedtuple
from dataclasses import dataclass, field, replace
from enum import Enum, IntEnum
from functools import wraps
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest
from numba import njit

CORE = Path(__file__).resolve().parents[2] / "src/apsgo_scheduler/core"
PACKAGE = "_apsgo_sb3_functional"


@njit
def table_edge_node(columns, row):
    return columns.values[row]


@njit
def table_edge_allowed(left, right, rules, status):
    value = rules[left, right]
    if value < 0:
        status[0] = 99
    return value == 1


class RuleKind(IntEnum):
    EARLIEST_START = 101
    CHAIN_WEIGHT = 102
    DELIVERY = 103
    INTER_CHAIN_WIDTH = 104
    VIRTUAL_RATIO = 105


class Reason(IntEnum):
    EARLY_START = 106


class NumericError(ValueError):
    pass


@dataclass(frozen=True)
class Deferred:
    view: object
    error: Exception


@dataclass
class Clock:
    now: float = 1.0
    def __call__(self):
        return self.now


@dataclass
class Cancellation:
    cancelled: bool = False
    def is_cancelled(self):
        return self.cancelled


def module(name, values=None):
    result = types.ModuleType(PACKAGE + "." + name)
    result.__package__ = PACKAGE
    result.__file__ = str(CORE / (name + ".py"))
    result.__dict__.update(values or {})
    sys.modules[result.__name__] = result
    setattr(sys.modules[PACKAGE], name, result)
    return result


def definitions(name, selected, values):
    result = module(name, values)
    tree = ast.parse((CORE / (name + ".py")).read_text(encoding="utf-8"))
    body = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in selected]
    assert {node.name for node in body} == set(selected)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    program = ast.fix_missing_locations(ast.Module(body=[future, *body], type_ignores=[]))
    exec(compile(program, str(CORE / (name + ".py")), "exec"), result.__dict__)
    return result


def actual(name):
    spec = importlib.util.spec_from_file_location(PACKAGE + "." + name, CORE / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = result
    setattr(sys.modules[PACKAGE], name, result)
    spec.loader.exec_module(result)
    return result


@pytest.fixture(scope="module")
def env():
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(CORE)]
    sys.modules[PACKAGE] = package
    contracts = definitions("contracts", ["require_int", "require_text", "require_enum", "SearchStopReason"],
                            {"Enum": Enum})
    contracts.SolverPolicy = type("UnusedPolicyBoundary", (), {})
    contracts.sum_decimals = sum  # from_policy is deliberately outside this test.
    budget = actual("budget")
    phase = actual("_search_phase_budget")
    rules = module("_numeric_rules", {"NumericRuleKind": RuleKind, "NumericReason": Reason})
    search = module("_numeric_search", {"NumericRuleKind": RuleKind,
        "NumericDeferredCandidateFailure": Deferred, "NumericValueError": NumericError,
        "capture_candidate_result": None, "_validate_search_inputs": lambda *args: None})
    ops = definitions("_numeric_stage_operators", ["_identity", "NumericOperatorProgress",
        "NumericBasicProgress", "NumericSplitProgress", "NumericStageResult", "_shares", "_validate",
        "_basic_children", "run_basic_slice", "run_split_scan_slice", "run_replay_slice", "run_split_slice"],
        dict(dataclass=dataclass, field=field, replace=replace, SearchPhaseResult=phase.SearchPhaseResult,
             SearchPhaseExit=phase.SearchPhaseExit,
             BASIC_OPERATORS=("whole", "node", "fill", "order"),
             _ARITY={"whole": 6, "node": 4, "fill": 3, "order": 2, "split": 2},
             search=search, require_int=contracts.require_int))
    action_names = "APPEND PREPEND INSERT REAL_MOVE ORDER FILL SPLIT INTRA NODE_MOVE NODE_SWAP BLOCK_MOVE BLOCK_SWAP CUT RECLAIM".split()
    field_names = "ACTION SOURCE TARGET NODE POSITION START STOP OTHER_START OTHER_STOP REVERSE_SOURCE REVERSE_TARGET OWNER VARIANT".split()
    constants = dict(zip(action_names, range(14))) | dict(zip(field_names, range(13)))
    common = NS(**constants, CandidateCheckPolicy=lambda *args: NS(arguments=args))
    values = constants | dict(np=np, njit=njit, wraps=wraps, deque=deque, _DESCRIPTOR_SIZE=13,
        _VIRTUAL=2, _BRIDGE=0, OK=0, CONTINUE=-1, ERROR=-2, NumericValueError=NumericError,
        edge_node=table_edge_node, all_edges_allowed_values=table_edge_allowed)
    reference = definitions("_numeric_refinement_scan", ["_managed_scan", "description", "has_critical",
        "interval", "ownership", "intra", "node_moves", "node_exchanges", "blocks", "chain_candidates",
        "source_chains", "reclaim", "bounded", "alternate", "family_stream"], values)
    reference.build_scan = lambda state, columns: state.scan  # Explicit prebuilt scan boundary.
    legacy = definitions("_numeric_refinement", ["NumericRefinementDiagnostics", "_maximum_chain_weight",
        "_descriptor_settings", "run_numeric_serial_search"],
        dict(dataclass=dataclass, field=field, NumericRuleKind=RuleKind, common_candidate=common,
             SolveRuntimeBudget=budget.SolveRuntimeBudget, NumericValueError=NumericError,
             _SERIAL_BATCH_SIZE=8, perf_counter=lambda: 0.0, logger=logging.getLogger("sb3.test")))
    legacy.task_columns = lambda task: task.columns
    legacy.rule_tables = lambda program_rules: program_rules
    cursor = actual("_numeric_refinement_cursor")
    refine = actual("_numeric_refinement_stage")
    scheduler = actual("_numeric_stage_scheduler")
    yield NS(budget=budget, phase=phase, contracts=contracts, ops=ops, search=search,
             scan=reference, cursor=cursor, refine=refine, scheduler=scheduler, legacy=legacy,
             rules=rules, common=common)
    for key in tuple(sys.modules):
        if key == PACKAGE or key.startswith(PACKAGE + "."):
            del sys.modules[key]


Scan = namedtuple("TestScanColumns", "rows offsets ids periods piece_rows piece_offsets row_chain row_position critical ranks interval_offsets interval_ranks interval_slots ranked_chains sources early")
Columns = namedtuple("TestColumns", "values")


def scan_data(env, seed=1, sizes=(4, 3, 2), virtual=False):
    rng = np.random.default_rng(seed)
    n = sum(sizes)
    rows = rng.permutation(n).astype(np.int64)
    offsets = np.r_[0, np.cumsum(sizes)].astype(np.int64)
    ids = (np.arange(len(sizes)) * 7 + 10).astype(np.int64)
    periods = np.arange(len(sizes), dtype=np.int64) % 2
    owner = np.arange(n, dtype=np.int64) % max(1, n // 2)
    role = np.zeros(n, np.int64)
    purpose = np.zeros(n, np.int64)
    group = np.full(n, -1, np.int64)
    if virtual:
        chosen = rows[:min(3, sizes[0])]
        role[chosen] = 2
        owner[chosen] = -1
    sources = np.unique(owner[owner >= 0])
    rng.shuffle(sources)
    max_source = int(owner.max()) + 1
    pieces = np.concatenate([np.flatnonzero(owner == s) for s in range(max_source)]).astype(np.int64)
    piece_offsets = np.r_[0, np.cumsum([np.count_nonzero(owner == s) for s in range(max_source)])].astype(np.int64)
    row_chain = np.zeros(n, np.int64)
    row_position = np.zeros(n, np.int64)
    for chain, (a, b) in enumerate(zip(offsets, offsets[1:])):
        row_chain[rows[a:b]] = chain
        row_position[rows[a:b]] = np.arange(b-a)
    ranks, starts, interval_ranks, slots = env.scan.ownership(rows, offsets, owner,
        pieces, piece_offsets, row_chain, row_position, sources)
    critical = rng.integers(0, 2, n, dtype=np.int64)
    early = rng.integers(0, 2, n, dtype=np.int64)
    x = Scan(rows, offsets, ids, periods, pieces, piece_offsets, row_chain, row_position,
        np.r_[0, np.cumsum(critical)].astype(np.int64), ranks, starts, interval_ranks, slots,
        rng.permutation(len(sizes)).astype(np.int64), sources, np.r_[0, np.cumsum(early)].astype(np.int64))
    rules = rng.integers(0, 2, (n, n), dtype=np.int64)
    return x, Columns(np.arange(n, dtype=np.int64)), rules, role, purpose, group


def old_descriptions(env, stream):
    return [tuple(map(int, d)) for d in stream if int(d[env.scan.ACTION]) >= 0]


def step_descriptions(env, function, args, work):
    result, position = [], np.zeros(10, np.int64)
    for i in range(100000):
        # Serialize/restore every call: no generator/native frame may be retained.
        position = np.array(tuple(position), np.int64)
        d = function(*args, position, work)
        if int(d[env.scan.ACTION]) == env.cursor.DONE:
            return result
        assert int(d[env.scan.ACTION]) != env.cursor.ERROR
        if int(d[env.scan.ACTION]) >= 0:
            result.append(tuple(map(int, d)))
    pytest.fail("cursor failed to finish bounded enumeration")


@pytest.mark.parametrize("seed", [2, 9, 21])
@pytest.mark.parametrize("lane", [False, True])
@pytest.mark.parametrize("name", ["intra", "node_move", "node_exchange", "block"])
def test_native_cursor_order_matches_original(env, seed, lane, name):
    x, t, rules, *_ = scan_data(env, seed)
    for source in x.sources:
        source = int(source)
        if name == "intra":
            old, args = env.scan.intra, (x, source, lane)
        elif name == "node_move":
            old, args = env.scan.node_moves, (x, t, rules, source, lane)
        elif name == "node_exchange":
            old, args = env.scan.node_exchanges, (x, source, lane)
        else:
            old, args = env.scan.blocks, (x, source, lane)
        expected = old_descriptions(env, old(*args))
        function = getattr(env.cursor, name + "_step")
        assert step_descriptions(env, function, args, 1) == expected
        assert step_descriptions(env, function, args, 256) == expected
        assert function.signatures  # Compiled code, not .py_func execution.


@pytest.mark.parametrize("family", ["cut", "order", "reclaim"])
@pytest.mark.parametrize("seed", [4, 6])
def test_chain_and_reclaim_cursor_parity(env, family, seed):
    x, _, _, role, purpose, group = scan_data(env, seed, virtual=True)
    if family == "reclaim":
        expected = old_descriptions(env, env.scan.reclaim(x, role, purpose, group))
        assert step_descriptions(env, env.cursor.reclaim_step, (x, role, purpose, group), 1) == expected
    else:
        action = env.scan.CUT if family == "cut" else env.scan.ORDER
        for chain in range(len(x.ids)):
            expected = old_descriptions(env, env.scan.chain_candidates(x, action, chain))
            assert step_descriptions(env, env.cursor.chain_step, (x, action, chain), 1) == expected


@pytest.mark.parametrize("family", ["intra", "node", "block", "cut", "order"])
@pytest.mark.parametrize("lane", [False, True])
def test_family_round_robin_matches_original_and_can_rollback(env, family, lane):
    x, t, rules, role, purpose, group = scan_data(env, seed=31)
    old = old_descriptions(env, env.scan.family_stream(x, t, rules, family, None, lane,
                                                       NS(allows_search=lambda: True)))
    cursor = env.cursor.FamilyCursor(family, lane, tuple(map(int, x.sources)))
    seen = []
    for i in range(100000):
        marker = len(cursor.journal)
        value = cursor.step(x, t, rules, role, purpose, group)
        if value[env.scan.ACTION] == env.cursor.DONE:
            cursor.commit()
            break
        if value[env.scan.ACTION] >= 0:
            # Exercise rollback of a prefetched but not yet consumed description.
            after = dict(cursor.data)
            cursor.rollback(marker)
            again = cursor.step(x, t, rules, role, purpose, group)
            assert np.array_equal(value, again)
            assert cursor.data == after
            seen.append(tuple(map(int, value)))
        cursor.commit()
    else:
        pytest.fail("family cursor did not finish")
    assert seen == old
    assert not cursor.journal


class Program:
    def __init__(self, early=True, rules=None):
        self.fingerprint = "program"
        self.early = early
        self.rules = rules
    def for_kind(self, kind):
        if kind == RuleKind.EARLIEST_START:
            return [NS(index=0)] if self.early else []
        if kind == RuleKind.CHAIN_WEIGHT:
            return [NS(values=(1, 100000, 0))]
        return []


def state_data(env, seed=1, early=True, prohibited=True, virtual=False):
    x, columns, table, role, purpose, group = scan_data(env, seed, sizes=(3, 2), virtual=virtual)
    state = NS(task=NS(fingerprint="task", columns=columns,
                      nodes=NS(role=role, purpose=purpose, split_group=group)),
        plan=NS(fingerprint="plan-0", generation=0), program=Program(early, table),
        quality=NS(fingerprint="quality"), virtual_sequence=0, split_sequence=0, replay_count=0,
        complete_candidate_evaluation_count=0, accepted_move_count=0, scan=x, seen=[], pools=[])
    state.task.test_state = state
    state.evaluation = NS(quality_key=np.array([100, 0, 0, 0, 0, 0, 0, 0, 0], np.int64),
        violations=[NS(prohibited=True)] if prohibited else [],
        kernel_result=NS(violations=np.array([[0, int(Reason.EARLY_START), 0, 0, 0, 1, 0]], np.int64)
                        if early else np.empty((0, 7), np.int64), counts=np.array([int(early)], np.int64)))
    return state


def budget_data(env, limit=1000, used=0, seconds=1000):
    clock, cancellation = Clock(), Cancellation()
    budget = env.budget.SolveRuntimeBudget(0., float(seconds), float(seconds + 10), limit, used,
                                          cancellation, clock=clock)
    return budget, clock, cancellation


def install_batch_doubles(env, monkeypatch, state, budget, *, cleaned=True, on_prepare=None,
                          on_consume=None, accept=None, fail_release=False):
    """Explicit numeric evaluator, consumer and private resource boundaries."""
    state.budget = budget
    state.preparations = []
    class Pool:
        def __init__(self, task, program, quality, plan, evaluation, *, maximum_candidates, _native_executor):
            assert _native_executor == "serial"
            self.maximum_candidates = maximum_candidates
            self.plan, self.state = plan, state
            self.allocated_bytes = 100
            self.workspaces = []
            self.releases = 0
            state.pools.append(self)
        def _result(self, descriptor, variant, allows_continue):
            allows_continue()
            if variant == 1 and not cleaned:
                return None
            d = descriptor.copy()
            d[env.scan.VARIANT] = variant
            workspace = NS(valid=True, plan=self.plan)
            self.workspaces.append(workspace)
            result = NS(descriptor=d, summary=NS(quality=()), admissible=True)
            return workspace, result
        def prepare_many(self, entries, *, virtual_sequence, split_sequence, allows_continue):
            worst = sum(len(v) for _, _, v in entries)
            assert len(entries) <= self.maximum_candidates
            assert worst <= budget._active_phase.remaining_candidate_checks
            state.preparations.append((budget.candidate_check_count, worst))
            if on_prepare:
                on_prepare(self, entries)
            allows_continue()
            return [[r for v in variants if (r := self._result(d, v, allows_continue)) is not None]
                    for d, _, variants in entries]
        def attempts(self, descriptor, policy, variants, *, virtual_sequence, split_sequence, allows_continue, capture):
            try:
                for variant in variants:
                    result = self._result(descriptor, variant, allows_continue)
                    if result is not None:
                        yield result
            finally:
                state.lazy_closed = getattr(state, "lazy_closed", 0) + 1
        def release(self):
            self.releases += 1
            for workspace in self.workspaces:
                workspace.valid = False
            self.workspaces.clear()
            if fail_release:
                raise RuntimeError("private cleanup failed")
    def consume(current, shared, workspace, result, *, allows_continue):
        assert shared is budget and current is state and workspace.valid
        assert workspace.plan is state.plan  # Detect stale prefetched results.
        assert shared._active_phase._attempt is not None
        if isinstance(result, Deferred):
            raise result.error
        if on_consume:
            on_consume(current, result)
        if not allows_continue():
            return False
        state.complete_candidate_evaluation_count += 1
        state.seen.append((state.plan.generation, tuple(map(int, result.descriptor)), shared.candidate_check_count))
        accepted = bool(accept and accept(state, result))
        if accepted:
            state.accepted_move_count += 1
            generation = state.plan.generation + 1
            state.plan = NS(fingerprint=f"plan-{generation}", generation=generation)
            state.evaluation.quality_key[0] -= 1
        return accepted
    monkeypatch.setattr(env.legacy, "NumericCandidateBatchWorkspace", Pool, raising=False)
    monkeypatch.setattr(env.search, "consume_candidate_result", consume, raising=False)
    return Pool


def run_refinement(env, state, budget, progress, quota, diagnostics=None, batch=8, seconds=100):
    return env.refine.run_refinement_slice(state, budget, candidate_checks=quota,
        time_slice_seconds=seconds, progress=progress, diagnostics=diagnostics, batch_size=batch)


@pytest.mark.parametrize("cleaned", [False, True])
def test_refinement_same_generation_single_credit_resume_is_exact(env, monkeypatch, cleaned):
    full = state_data(env)
    budget, _, _ = budget_data(env, 10000)
    install_batch_doubles(env, monkeypatch, full, budget, cleaned=cleaned)
    progress = env.refine.NumericRefinementProgress()
    result = run_refinement(env, full, budget, progress, 9000)
    assert result.scan_complete and budget.stop_reason is None
    expected = full.seen
    split = state_data(env)
    sliced_budget, _, _ = budget_data(env, 10000)
    install_batch_doubles(env, monkeypatch, split, sliced_budget, cleaned=cleaned)
    progress = env.refine.NumericRefinementProgress()
    for _ in range(10000):
        result = run_refinement(env, split, sliced_budget, progress, 1)
        if result.scan_complete:
            break
        assert result.budget.reason == env.phase.SearchPhaseExit.CANDIDATE_SLICE_EXHAUSTED
    else:
        pytest.fail("one-credit refinement never completed")
    assert split.seen == expected
    assert sliced_budget.candidate_check_count == budget.candidate_check_count
    assert all(not p.workspaces for p in split.pools)
    assert progress.pending is None


@pytest.mark.parametrize("batch", [1, 3, 8])
def test_random_slices_match_unsliced_refinement(env, monkeypatch, batch):
    def run(sliced):
        state = state_data(env, seed=5, virtual=True)
        budget, _, _ = budget_data(env, 10000, used=7)
        install_batch_doubles(env, monkeypatch, state, budget)
        p = env.refine.NumericRefinementProgress()
        rng = np.random.default_rng(244)
        for _ in range(2000):
            quota = int(rng.integers(1, 14)) if sliced else 9000
            result = run_refinement(env, state, budget, p, quota, batch=batch)
            assert result.budget.used_candidate_checks <= quota
            if result.scan_complete:
                return state.seen, budget.candidate_check_count
        pytest.fail("unfinished finite refinement")
    assert run(True) == run(False)


def test_refinement_soft_precompute_yield_is_not_cancel_and_retries_lazily(env, monkeypatch):
    state = state_data(env)
    budget, clock, _ = budget_data(env)
    calls = []
    def expire(pool, entries):
        calls.append(1)
        if len(calls) == 1:
            clock.now += 2
    install_batch_doubles(env, monkeypatch, state, budget, on_prepare=expire)
    p = env.refine.NumericRefinementProgress()
    result = run_refinement(env, state, budget, p, 10, seconds=1)
    assert result.budget.reason == env.phase.SearchPhaseExit.TIME_SLICE_EXHAUSTED
    assert budget.stop_reason is None and budget.candidate_check_count == 0
    assert p.force_lazy
    result = run_refinement(env, state, budget, p, 1, seconds=1)
    assert budget.candidate_check_count == 1
    assert all(not pool.workspaces for pool in state.pools)


def test_last_paid_refinement_can_finish_past_soft_time(env, monkeypatch):
    state = state_data(env)
    budget, clock, _ = budget_data(env, limit=1)
    def pass_soft(current, result):
        clock.now += 2
    install_batch_doubles(env, monkeypatch, state, budget, cleaned=False,
                          on_consume=pass_soft, accept=lambda *args: True)
    p = env.refine.NumericRefinementProgress()
    result = run_refinement(env, state, budget, p, 1, seconds=1)
    assert state.accepted_move_count == 1 and state.plan.generation == 1
    assert budget.candidate_check_count == 1
    assert result.budget.soft_overrun_seconds >= 1
    assert budget.stop_reason == env.contracts.SearchStopReason.CANDIDATE_LIMIT_REACHED


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_real_stop_prevents_refinement_commit(env, monkeypatch, stop):
    state = state_data(env)
    budget, clock, token = budget_data(env, limit=2)
    def stop_now(current, result):
        if stop == "cancel": token.cancelled = True
        else: clock.now = budget.search_deadline_monotonic
    install_batch_doubles(env, monkeypatch, state, budget, on_consume=stop_now, accept=lambda *args: True)
    p = env.refine.NumericRefinementProgress()
    result = run_refinement(env, state, budget, p, 2)
    assert state.accepted_move_count == 0 and state.plan.generation == 0
    assert result.budget.reason == env.phase.SearchPhaseExit.GLOBAL_STOP
    assert budget._active_phase is None
    assert all(not p.workspaces for p in state.pools)


def test_accept_discards_stale_batch_and_rebinds_generation(env, monkeypatch):
    state = state_data(env)
    budget, _, _ = budget_data(env, limit=50)
    install_batch_doubles(env, monkeypatch, state, budget, accept=lambda s, r: s.accepted_move_count == 0)
    p, d = env.refine.NumericRefinementProgress(), env.legacy.NumericRefinementDiagnostics()
    run_refinement(env, state, budget, p, 20, diagnostics=d)
    assert state.accepted_move_count == 1
    assert all(g == 1 for g, _, _ in state.seen[1:])
    assert sum(d.numeric_discarded.values()) > 0
    assert p.binding == env.ops._identity(state)
    assert all(not p.workspaces for p in state.pools)


@pytest.mark.parametrize("early,prohibited,skip", [(True, True, False), (False, True, True), (False, False, False)])
def test_refinement_original_applicability(env, monkeypatch, early, prohibited, skip):
    state = state_data(env, early=early, prohibited=prohibited)
    budget, _, _ = budget_data(env)
    install_batch_doubles(env, monkeypatch, state, budget)
    p = env.refine.NumericRefinementProgress()
    result = run_refinement(env, state, budget, p, 10)
    assert p.not_applicable is skip
    assert (result.budget.used_candidate_checks == 0) is skip


def test_refinement_options_cannot_change_in_same_generation(env, monkeypatch):
    state = state_data(env)
    budget, _, _ = budget_data(env)
    install_batch_doubles(env, monkeypatch, state, budget)
    p = env.refine.NumericRefinementProgress()
    run_refinement(env, state, budget, p, 1, batch=1)
    with pytest.raises(ValueError, match="options"):
        run_refinement(env, state, budget, p, 1, batch=8)


def test_refinement_cleanup_failure_remains_error(env, monkeypatch):
    state = state_data(env)
    budget, _, _ = budget_data(env)
    install_batch_doubles(env, monkeypatch, state, budget, fail_release=True)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        run_refinement(env, state, budget, env.refine.NumericRefinementProgress(), 1)
    assert budget._active_phase is None
    assert state.accepted_move_count == 0


def install_stage_workers(env, monkeypatch, state, budget, *, basic_target=100000,
                          split_target=100000, refine_target=100000, on_check=None,
                          split_once=False, accept_refine_once=False):
    """Replace business enumeration only; real SB1 and SB2 child scopes run."""
    calls, progress_objects = [], []
    def operator(current, shared, *, progress, candidate_checks, time_slice_seconds,
                 pair_scan_slack_weight=0, maximum_virtual_bridge_nodes=2, name=None):
        assert shared is budget and current is state
        progress_objects.append(progress)
        before = state.plan.generation
        options = (pair_scan_slack_weight, maximum_virtual_bridge_nodes)
        progress.bind(state, options)
        calls.append((name, candidate_checks, shared.candidate_check_count))
        target = split_target if progress.operator == "split" else basic_target
        with shared.phase_scope(name, candidate_checks, time_slice_seconds) as phase:
            if progress.complete_for(state):
                phase.finish()
            else:
                while phase.allows_new_work():
                    if progress.cursor[0] >= target:
                        progress.complete = True
                        phase.finish()
                        break
                    sequence = shared.candidate_check_count + 1
                    identity = env.phase.SearchAttemptIdentity(state.task.fingerprint,
                        state.plan.fingerprint, state.plan.generation, sequence)
                    with phase.candidate_attempt(identity) as attempt:
                        assert attempt.granted
                        if on_check:
                            on_check(name, state, shared)
                        if not attempt.allows_continue(identity):
                            break
                        progress.cursor = (progress.cursor[0] + 1, *progress.cursor[1:])
                        if progress.operator == "split" and split_once and state.split_sequence == 0:
                            state.split_sequence += 1
                            state.accepted_move_count += 1
                            state.plan = NS(fingerprint="after-split", generation=state.plan.generation + 1)
                            progress.bind(state, options)
        return env.ops.NumericStageResult(phase.release_unused(), (), before, state.plan.generation,
                                         progress.complete_for(state))
    def refine(current, shared, *, candidate_checks, time_slice_seconds, progress, diagnostics,
               maximum_virtual_bridge_nodes=2, batch_size=8, name="refinement"):
        calls.append((name, candidate_checks, shared.candidate_check_count))
        progress.bind(state, (maximum_virtual_bridge_nodes, batch_size))
        before = state.plan.generation
        with shared.phase_scope(name, candidate_checks, time_slice_seconds) as phase:
            if progress.complete_for(state):
                phase.finish()
            else:
                while phase.allows_new_work():
                    if progress.revision >= refine_target:
                        progress.complete = True
                        phase.finish()
                        break
                    sequence = shared.candidate_check_count + 1
                    identity = env.phase.SearchAttemptIdentity(state.task.fingerprint,
                        state.plan.fingerprint, state.plan.generation, sequence)
                    with phase.candidate_attempt(identity) as attempt:
                        assert attempt.granted
                        if on_check:
                            on_check(name, state, shared)
                        if not attempt.allows_continue(identity):
                            break
                        progress.revision += 1
                        if accept_refine_once and not getattr(state, "refine_accepted", False):
                            state.refine_accepted = True
                            state.plan = NS(fingerprint="after-refine", generation=state.plan.generation + 1)
                            state.accepted_move_count += 1
                            progress.bind(state, (maximum_virtual_bridge_nodes, batch_size))
        return env.ops.NumericStageResult(phase.release_unused(), (), before, state.plan.generation,
                                         progress.complete_for(state))
    monkeypatch.setattr(env.ops, "run_operator_slice", operator, raising=False)
    monkeypatch.setattr(env.scheduler, "run_refinement_slice", refine)
    return calls, progress_objects


def run_schedule(env, state, budget):
    return env.scheduler.run_numeric_stage_schedule(state, budget, pair_scan_slack_weight=40)


@pytest.mark.parametrize("remaining", [0, 1, 2, 3, 7, 10, 19, 101, 999, 2000000])
def test_initial_integer_allocations_conserve_every_credit(env, remaining):
    allocation = env.scheduler.allocate_checks(remaining)
    assert sum(allocation) == remaining
    assert all(type(n) is int and n >= 0 for n in allocation)
    assert allocation[:3] == (remaining * 20 // 100, remaining * 10 // 100, remaining * 60 // 100)


@pytest.mark.parametrize("used", [0, 7])
def test_front_stages_exhaust_slices_but_refinement_gets_reserved_credit(env, monkeypatch, used):
    state = state_data(env)
    budget, _, _ = budget_data(env, limit=100 + used, used=used)
    deadlines = (budget.search_deadline_monotonic, budget.final_deadline_monotonic)
    calls, _ = install_stage_workers(env, monkeypatch, state, budget)
    report = run_schedule(env, state, budget)
    assert report.allocations == (20, 10, 60, 10)
    assert [o.name for o in report.observations[:3]] == ["basic", "split", "refinement"]
    assert report.observations[0].result.budget.used_candidate_checks == 20
    # No actual split accepted: scan uses its half, replay's half returns once.
    assert report.observations[1].result.budget.used_candidate_checks == 5
    assert report.observations[2].result.budget.used_candidate_checks == 60
    assert any(o.refill and o.name == "refinement" for o in report.observations)
    assert budget.candidate_check_count == 100 + used
    assert budget.stop_reason == env.contracts.SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert sum(o.result.budget.used_candidate_checks for o in report.observations) == 100
    assert (budget.search_deadline_monotonic, budget.final_deadline_monotonic) == deadlines
    assert report.unspent_checks == 0
    assert calls[0][0] == "basic:whole"


def test_unused_child_balance_is_not_credited_twice(env, monkeypatch):
    state = state_data(env)
    budget, _, _ = budget_data(env, limit=1000)
    install_stage_workers(env, monkeypatch, state, budget, basic_target=1, split_target=0, refine_target=3)
    report = run_schedule(env, state, budget)
    assert budget.candidate_check_count == 6  # Four basics + two refinement checks.
    assert report.unspent_checks == 994
    assert report.stop_reason == env.contracts.SearchStopReason.LOCAL_SEARCH_COMPLETE
    assert all(o.result.scan_complete for o in report.observations)
    assert state.replay_count == 0


@pytest.mark.parametrize("limit", [0, 1, 2, 3, 5, 9])
def test_tiny_budget_does_not_mint_stage_credits(env, monkeypatch, limit):
    state = state_data(env)
    budget, _, _ = budget_data(env, limit=limit)
    calls, _ = install_stage_workers(env, monkeypatch, state, budget)
    report = run_schedule(env, state, budget)
    assert report.count_at_exit == limit
    assert sum(report.allocations) == limit
    assert sum(o.result.budget.used_candidate_checks for o in report.observations) == limit
    assert report.unspent_checks == 0
    if limit:
        assert any(name == "refinement" and grant > 0 for name, grant, _ in calls)
    else:
        assert not calls


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_global_stop_does_not_reenter_following_stages(env, monkeypatch, stop):
    state = state_data(env)
    budget, clock, token = budget_data(env, limit=100)
    def on_check(name, state, budget):
        if stop == "cancel": token.cancelled = True
        else: clock.now = budget.search_deadline_monotonic
    calls, _ = install_stage_workers(env, monkeypatch, state, budget, on_check=on_check)
    report = run_schedule(env, state, budget)
    assert len(calls) == 1
    assert len(report.observations) == 3
    assert all(o.result.budget.reason == env.phase.SearchPhaseExit.GLOBAL_STOP for o in report.observations)
    assert budget.candidate_check_count == 1
    assert budget._active_phase is None


def test_soft_slice_does_not_consume_finalization_time(env, monkeypatch):
    state = state_data(env)
    budget, clock, _ = budget_data(env, limit=1000, seconds=101)
    def slow(name, state, budget):
        clock.now += 3
    calls, _ = install_stage_workers(env, monkeypatch, state, budget, on_check=slow)
    report = run_schedule(env, state, budget)
    assert any(name == "refinement" for name, _, _ in calls)
    assert budget.stop_reason == env.contracts.SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    assert budget.search_deadline_monotonic == 101
    assert budget.final_deadline_monotonic == 111
    assert budget.allows_finalization()
    assert any(o.budget.reason == env.phase.SearchPhaseExit.TIME_SLICE_EXHAUSTED
               for o in report.observations[0].result.children)


def test_split_replay_is_bounded_and_counted_once(env, monkeypatch):
    state = state_data(env)
    budget, _, _ = budget_data(env, limit=200)
    calls, _ = install_stage_workers(env, monkeypatch, state, budget,
        basic_target=4, split_target=10, refine_target=100000, split_once=True)
    report = run_schedule(env, state, budget)
    split = report.observations[1]
    assert split.result.budget.used_candidate_checks <= 20
    assert split.result.children[0].budget.used_candidate_checks <= 10
    assert split.result.children[1].budget.used_candidate_checks <= 10
    assert state.replay_count == 1 and state.split_sequence == 1
    assert report.observations[2].result.budget.used_candidate_checks == 120
    assert sum(o.result.budget.used_candidate_checks for o in report.observations) == 200


def test_generation_change_invalidates_old_completion_records(env, monkeypatch):
    state = state_data(env)
    budget, _, _ = budget_data(env, limit=1000)
    calls, progresses = install_stage_workers(env, monkeypatch, state, budget,
        basic_target=1, split_target=0, refine_target=5, accept_refine_once=True)
    report = run_schedule(env, state, budget)
    assert report.stop_reason == env.contracts.SearchStopReason.LOCAL_SEARCH_COMPLETE
    assert state.plan.generation == 1
    assert any(o.name == "basic:whole" and o.refill for o in report.observations)
    assert all(p.complete_for(state) for p in progresses)
    assert any(o.binding_before != o.binding_after for o in report.observations)


def test_refill_replay_children_can_receive_one_credit_each(env, monkeypatch):
    state = state_data(env)
    budget, _, _ = budget_data(env, limit=100)
    install_stage_workers(env, monkeypatch, state, budget, basic_target=1)
    progress = env.ops.NumericSplitProgress(pending_replay=True)
    for name in env.ops.BASIC_OPERATORS:
        env.scheduler._replay_operator(state, budget, progress, name, 1, 5, 40, 2, "replay:" + name)
        # Complete the boundary proof on a second visit, with no extra fee.
        env.scheduler._replay_operator(state, budget, progress, name, 1, 5, 40, 2, "replay:" + name)
    assert budget.candidate_check_count == 4
    assert state.replay_count == 1
    assert not progress.pending_replay


def test_refill_releases_are_one_time_and_checked(env):
    budget, _, _ = budget_data(env, limit=10)
    with budget.phase_scope("probe", 4, 5) as phase:
        assert budget.consume_candidate_check()
    result = env.ops.NumericStageResult(phase.release_unused(), (), 0, 0, False)
    ledger = env.scheduler._Ledger(0, {})
    ledger.settle(1, 4, result)
    assert ledger.pool == 3
    with pytest.raises(ValueError, match="already settled"):
        ledger.settle(1, 4, result)
    with pytest.raises(ValueError, match="global"):
        ledger.check(4)


def test_scheduler_rejects_nested_global_owner(env, monkeypatch):
    state = state_data(env)
    budget, _, _ = budget_data(env)
    install_stage_workers(env, monkeypatch, state, budget)
    with budget.phase_scope("existing", 10, 10):
        with pytest.raises(ValueError, match="top-level"):
            run_schedule(env, state, budget)


def test_scheduler_retains_existing_stop_reason(env, monkeypatch):
    state = state_data(env)
    budget, _, _ = budget_data(env)
    budget.stop_reason = env.contracts.SearchStopReason.SYSTEM_ERROR
    calls, _ = install_stage_workers(env, monkeypatch, state, budget)
    report = run_schedule(env, state, budget)
    assert not calls
    assert report.stop_reason == env.contracts.SearchStopReason.SYSTEM_ERROR
    assert report.count_at_exit == 0


def test_production_entry_uses_stage_scheduler_without_changing_return_contract(env, monkeypatch):
    state = state_data(env)
    budget, _, _ = budget_data(env)
    seen = []
    monkeypatch.setattr(env.legacy, "NumericSearchState", NS(start=lambda *args: state), raising=False)
    monkeypatch.setattr(env.legacy, "NumericSearchCheckpoint",
                        NS(capture=lambda *args: "checkpoint-boundary"), raising=False)
    def schedule(current, shared, **options):
        assert current is state and shared is budget
        seen.append(options)
        return NS(summary=lambda: {"test": True})
    monkeypatch.setattr(env.scheduler, "run_numeric_stage_schedule", schedule)
    result = env.legacy.run_numeric_serial_search(state.task, state.program, state.quality,
        "initial-boundary", budget, pair_scan_slack_weight=40)
    assert result == (state, "checkpoint-boundary")
    assert len(seen) == 1 and seen[0]["batch_size"] == 8
    assert seen[0]["pair_scan_slack_weight"] == 40


@pytest.mark.parametrize("invalid", [True, -1, 1.5])
def test_allocation_rejects_invalid_integer_amounts(env, invalid):
    with pytest.raises(ValueError):
        env.scheduler.allocate_checks(invalid)


def test_numeric_prefetch_accounting_includes_discarded_suffix(env, monkeypatch):
    state = state_data(env)
    budget, _, _ = budget_data(env, limit=200)
    install_batch_doubles(env, monkeypatch, state, budget,
                          accept=lambda s, r: s.accepted_move_count < 2)
    progress, diagnostics = env.refine.NumericRefinementProgress(), env.legacy.NumericRefinementDiagnostics()
    run_refinement(env, state, budget, progress, 100, diagnostics)
    assert sum(diagnostics.candidate_checks.values()) == budget.candidate_check_count
    assert sum(diagnostics.numeric_precomputed.values()) == (
        sum(diagnostics.numeric_consumed.values()) + sum(diagnostics.numeric_discarded.values()))
    assert sum(diagnostics.accepted.values()) == state.accepted_move_count == 2


def test_raw_progress_is_retained_when_time_slice_has_no_candidates(env, monkeypatch):
    state = state_data(env)
    budget, clock, _ = budget_data(env, limit=20)
    install_batch_doubles(env, monkeypatch, state, budget)
    seen = []
    original = env.cursor.FamilyCursor.step
    def slow_step(self, *args):
        seen.append((self.family, self.lane))
        result = original(self, *args)
        clock.now += 2
        return result
    monkeypatch.setattr(env.cursor.FamilyCursor, "step", slow_step)
    progress = env.refine.NumericRefinementProgress()
    first = run_refinement(env, state, budget, progress, 10, seconds=1)
    assert first.budget.used_candidate_checks == 0
    assert first.budget.reason == env.phase.SearchPhaseExit.TIME_SLICE_EXHAUSTED
    assert progress.families[(1, "reclaim")].complete
    assert not progress.first_cleanup
    run_refinement(env, state, budget, progress, 10, seconds=1)
    assert seen.count(("reclaim", False)) == 1
    assert budget.stop_reason is None


def test_pending_variant_has_only_coordinates_and_does_not_repeat_first_check(env, monkeypatch):
    state = state_data(env)
    budget, _, _ = budget_data(env, limit=100)
    install_batch_doubles(env, monkeypatch, state, budget, cleaned=True)
    progress = env.refine.NumericRefinementProgress()
    run_refinement(env, state, budget, progress, 1)
    assert progress.pending is not None
    key, coordinates, variants = progress.pending
    assert len(coordinates) == 13 and all(type(v) is int for v in coordinates)
    assert variants == (0,)
    first = state.seen[0][1]
    run_refinement(env, state, budget, progress, 1)
    second = state.seen[1][1]
    assert first[:-1] == second[:-1]
    assert first[-1] == 1 and second[-1] == 0
    assert budget.candidate_check_count == 2
    assert all(not pool.workspaces for pool in state.pools)


def test_unconsumed_deferred_failure_is_not_raised_after_accept(env, monkeypatch):
    state = state_data(env)
    budget, _, _ = budget_data(env)
    install_batch_doubles(env, monkeypatch, state, budget, accept=lambda *args: True)
    progress = env.refine.NumericRefinementProgress()
    progress.bind(state, (2, 8))
    d = env.scan.description(env.scan.INTRA, 10, 10, 1, 2, 0)
    d[env.scan.VARIANT] = 1
    result = NS(descriptor=d, summary=NS(quality=()))
    workspace = NS(valid=True, plan=state.plan)
    with budget.phase_scope("refinement", 4, 10) as phase:
        accepted, finished, begun = env.refine._consume_prepared(state, budget, phase, progress,
            (0, "intra"), d, (1, 0), [(workspace, result),
                (workspace, Deferred(None, NumericError("deferred suffix")))], False, None, "critical:intra")
    assert accepted and finished and begun
    assert budget.candidate_check_count == 1


def test_consumed_deferred_failure_is_charged_and_propagates(env, monkeypatch):
    state = state_data(env)
    budget, _, _ = budget_data(env)
    install_batch_doubles(env, monkeypatch, state, budget)
    progress = env.refine.NumericRefinementProgress()
    progress.bind(state, (2, 8))
    d = env.scan.description(env.scan.INTRA, 10, 10, 1, 2, 0)
    with pytest.raises(NumericError, match="real failure"):
        with budget.phase_scope("refinement", 1, 10) as phase:
            env.refine._consume_prepared(state, budget, phase, progress, (0, "intra"), d, (1, 0),
                [(NS(valid=True, plan=state.plan), Deferred(None, NumericError("real failure")))],
                False, None, "critical:intra")
    assert budget.candidate_check_count == 1
    assert budget._active_phase is None and state.accepted_move_count == 0


def test_wrong_precomputed_variant_cannot_mutate_state(env, monkeypatch):
    state = state_data(env)
    budget, _, _ = budget_data(env)
    install_batch_doubles(env, monkeypatch, state, budget, accept=lambda *args: True)
    progress = env.refine.NumericRefinementProgress()
    progress.bind(state, (2, 8))
    d = env.scan.description(env.scan.INTRA, 10, 10, 1, 2, 0)
    d[env.scan.VARIANT] = 9
    with pytest.raises(ValueError, match="variant"):
        with budget.phase_scope("refinement", 1, 10) as phase:
            env.refine._consume_prepared(state, budget, phase, progress, (0, "intra"), d, (1, 0),
                [(NS(valid=True, plan=state.plan), NS(descriptor=d, summary=NS(quality=())))],
                False, None, "critical:intra")
    assert budget.candidate_check_count == 1
    assert state.accepted_move_count == 0


def test_native_error_is_not_a_soft_yield(env):
    x, t, table, *_ = scan_data(env, seed=4)
    table[:] = -1
    found = False
    for owner in x.sources:
        for lane in (False, True):
            c = np.zeros(10, np.int64)
            d = env.cursor.node_move_step(x, t, table, int(owner), lane, c)
            if d[env.scan.ACTION] == env.cursor.ERROR:
                found = True
    assert found


def test_scheduler_runs_actual_refinement_control_with_only_business_boundaries_doubled(env, monkeypatch):
    state = state_data(env, seed=4)
    budget, _, _ = budget_data(env, limit=120, used=3)
    install_stage_workers(env, monkeypatch, state, budget, basic_target=100000, split_target=0)
    monkeypatch.setattr(env.scheduler, "run_refinement_slice", env.refine.run_refinement_slice)
    install_batch_doubles(env, monkeypatch, state, budget, cleaned=False)
    report = run_schedule(env, state, budget)
    assert report.observations[2].name == "refinement"
    assert report.observations[2].result.budget.used_candidate_checks > 0
    assert budget.candidate_check_count == 120
    assert sum(o.result.budget.used_candidate_checks for o in report.observations) == 117
    assert state.seen


def test_zero_progress_refill_is_not_false_convergence_or_a_busy_loop(env, monkeypatch):
    state = state_data(env)
    budget, clock, _ = budget_data(env, limit=10000)
    install_stage_workers(env, monkeypatch, state, budget, basic_target=0, split_target=0)
    def stalled(current, shared, *, candidate_checks, time_slice_seconds, progress, name, **options):
        progress.bind(state, (2, 8))
        before = state.plan.generation
        with shared.phase_scope(name, candidate_checks, time_slice_seconds) as phase:
            clock.now += .001  # Real clocks advancing must not hide zero progress.
        return env.ops.NumericStageResult(phase.release_unused(), (), before, before, False)
    monkeypatch.setattr(env.scheduler, "run_refinement_slice", stalled)
    with pytest.raises(ValueError, match="no resumable progress"):
        run_schedule(env, state, budget)
    assert budget.stop_reason is None
    assert budget.candidate_check_count == 0
    assert budget._active_phase is None
