"""SB1 functional tests only: real budget/scopes and scalar contract definitions.

Private package loading avoids executing the solver's package initializer. Only
clock/cancellation and candidate identities are synthetic; no solver, scoring,
Numba kernel, integration suite or production scheduling is run here.
"""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
import importlib.util
from pathlib import Path
import random
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "src/apsgo_scheduler/core"
PREFIX = "_apsgo_sb1_functional.core"


def _load_modules():
    for name in ("_apsgo_sb1_functional", PREFIX):
        package = ModuleType(name)
        package.__path__ = [str(CORE)]
        sys.modules[name] = package
    path = CORE / "contracts.py"
    source = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = {"require_text", "require_int", "require_decimal", "require_enum",
             "sum_decimals", "SearchStopReason", "SolverPolicy", "contract_values"}
    constants = {"CONSTRUCTION_ORDER_KEY", "NUMERIC_SEMANTICS_KEY", "INTEGER_NUMERIC_SEMANTICS_KEY"}
    selected = [node for node in source.body
                if isinstance(node, (ast.Import, ast.ImportFrom))
                or isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names
                or isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets)]
    assert {node.name for node in selected if isinstance(node, (ast.FunctionDef, ast.ClassDef))} == names
    contracts = ModuleType(PREFIX + ".contracts")
    contracts.__file__ = str(path)
    sys.modules[contracts.__name__] = contracts
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), contracts.__dict__)
    modules = []
    for part in ("budget", "_search_phase_budget"):
        spec = importlib.util.spec_from_file_location(PREFIX + "." + part, CORE / (part + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[module.__name__] = module
        spec.loader.exec_module(module)
        modules.append(module)
    return contracts, *modules


contracts, budget_module, phases = _load_modules()
Budget = budget_module.SolveRuntimeBudget
Exit = phases.SearchPhaseExit
Stop = contracts.SearchStopReason
Identity = phases.SearchAttemptIdentity


class Clock:
    def __init__(self, now=10.0):
        self.now = now
        self.error = None

    def __call__(self):
        if self.error is not None:
            raise self.error
        return self.now


class Cancellation:
    cancelled = False

    def is_cancelled(self):
        return self.cancelled


def make_budget(limit=20, used=0):
    clock, cancel = Clock(), Cancellation()
    budget = Budget(0.0, 100.0, 110.0, limit, used, cancel, clock=clock)
    return budget, clock, cancel


def identity(sequence=1):
    return Identity("task", "plan", 3, sequence)


@pytest.mark.parametrize("used,limit", [(0, 0), (0, 1), (3, 8), (8, 8)])
def test_unscoped_permits_and_last_candidate_keep_original_behavior(used, limit):
    budget, _, _ = make_budget(limit, used)
    assert budget.allows_search()
    for _ in range(limit - used):
        assert budget.consume_candidate_check()
    assert budget.allows_search()
    assert budget.candidate_check_count == limit
    assert not budget.consume_candidate_check()
    assert budget.stop_reason is Stop.CANDIDATE_LIMIT_REACHED
    assert budget.allows_finalization()


def test_from_policy_and_inactive_projection_unchanged():
    policy = contracts.SolverPolicy(1, Decimal("110"), Decimal("10"), 7,
        contracts.CONSTRUCTION_ORDER_KEY, contracts.INTEGER_NUMERIC_SEMANTICS_KEY, Decimal("40"))
    budget = Budget.from_policy(policy, 0.0, clock=Clock())
    assert (budget.search_deadline_monotonic, budget.final_deadline_monotonic) == (100, 110)
    assert "_active_phase" not in contracts.contract_values(budget)
    before = contracts.contract_values(budget)
    scope = budget.phase_scope("not_entered", 1, 1)
    assert budget._active_phase is None and not scope._entered
    assert contracts.contract_values(budget) == before


@pytest.mark.parametrize("count", [-1, True, 1.0, "1", None])
def test_invalid_global_charge_is_not_counted(count):
    budget, _, _ = make_budget()
    with pytest.raises(ValueError):
        budget.permit(count)
    assert budget.candidate_check_count == 0 and budget.stop_reason is None


def test_nonzero_entry_count_and_successor_phase():
    budget, _, _ = make_budget(12, 5)
    with budget.phase_scope("basic", 2, 20) as phase:
        assert phase.count_at_entry == 5
        assert budget.permit(2)
        assert not budget.consume_candidate_check()
        assert not budget.must_stop
        assert budget.allows_search()  # Deliberately a GLOBAL continuation poll.
    assert phase.result.reason is Exit.CANDIDATE_SLICE_EXHAUSTED
    with budget.phase_scope("refinement", 3, 20) as repair:
        assert budget.permit(3)
        repair.finish()
    assert budget.candidate_check_count == 10
    assert phase.result.count_at_exit == 7
    assert repair.result.reason is Exit.SCAN_COMPLETE


def test_parent_children_and_atomic_multi_unit_permit():
    budget, _, _ = make_budget(50, 7)
    with budget.phase_scope("parent", 5, 20) as parent:
        assert budget.permit(1)
        with budget.phase_scope("first", 2, 40) as first:
            assert first.soft_deadline == parent.soft_deadline
            assert not budget.permit(3)
            assert budget.candidate_check_count == 8
        assert first.result.reason is Exit.CANDIDATE_SLICE_EXHAUSTED
        with budget.phase_scope("second", 99, 40) as second:
            assert second.candidate_limit == 4
            assert budget.permit(4)
        assert not budget.consume_candidate_check()
    assert parent.result.used_candidate_checks == 5
    assert second.result.used_candidate_checks == 4
    assert budget.candidate_check_count == 12 and budget.stop_reason is None


def test_global_exhaustion_has_priority_over_phase_exhaustion():
    budget, _, _ = make_budget(1)
    with budget.phase_scope("one", 1, 10) as phase:
        with phase.candidate_attempt(identity()) as attempt:
            assert attempt.granted and attempt.allows_continue(identity())
            assert budget.stop_reason is None
        assert not phase.allows_new_work()
    assert budget.candidate_check_count == 1
    assert phase.result.reason is Exit.GLOBAL_STOP
    assert budget.stop_reason is Stop.CANDIDATE_LIMIT_REACHED


def test_global_exhaustion_on_poll_does_not_create_fake_charge():
    budget, _, _ = make_budget(0)
    with budget.phase_scope("none", 10, 10) as phase:
        assert phase.candidate_limit == 0
        assert not phase.allows_new_work()
    assert budget.candidate_check_count == 0
    assert budget.stop_reason is Stop.CANDIDATE_LIMIT_REACHED


def test_zero_phase_does_not_exhaust_global_budget():
    budget, _, _ = make_budget()
    with budget.phase_scope("zero", 0, 10) as phase:
        assert not phase.allows_new_work()
    assert phase.result.reason is Exit.INSUFFICIENT_BUDGET
    assert budget.stop_reason is None and budget.candidate_check_count == 0


@pytest.mark.parametrize("limit", [1, 2, 3, 7, 101])
def test_clipped_allocations_and_one_time_releases_preserve_total(limit):
    budget, _, _ = make_budget(limit + 5, 5)
    used = 0
    results = []
    for index, grant in enumerate((limit * 20 // 100, limit * 10 // 100,
                                   limit * 60 // 100, limit)):
        with budget.phase_scope(str(index), grant, 10) as phase:
            spend = min(phase.candidate_limit, 1)
            if spend:
                assert budget.permit(spend)
            phase.finish()
        released = phase.release_unused()
        assert released.unused_candidate_checks == phase.candidate_limit - spend
        with pytest.raises(ValueError, match="already released"):
            phase.release_unused()
        used += spend
        results.append(phase.result)
    assert budget.candidate_check_count == 5 + used <= limit + 5
    assert sum(r.used_candidate_checks for r in results) == used


def test_releases_do_not_refund_rejected_or_failed_attempts():
    budget, clock, _ = make_budget()
    with pytest.raises(RuntimeError, match="candidate error"):
        with budget.phase_scope("error", 5, 10) as phase:
            with phase.candidate_attempt(identity()) as attempt:
                assert attempt.granted
                clock.now += 1
                raise RuntimeError("candidate error")
    assert not attempt.allows_continue(identity())
    result = phase.release_unused()
    assert result.used_candidate_checks == 1 and result.unused_candidate_checks == 4
    assert result.failed and result.reason is Exit.INCOMPLETE
    assert result.unused_time_seconds == 0
    assert budget.candidate_check_count == 1 and budget.stop_reason is None


def test_child_release_is_parent_detail_not_extra_global_allowance():
    budget, _, _ = make_budget()
    with budget.phase_scope("p", 6, 10) as parent:
        with budget.phase_scope("c", 4, 10) as child:
            assert budget.permit(2)
        child.release_unused()
        assert parent.remaining_candidate_checks == 4
        assert budget.permit(4)
    assert parent.release_unused().unused_candidate_checks == 0
    assert parent.result.used_candidate_checks == 6
    assert child.result.used_candidate_checks == 2  # Included in six, not added to it.
    assert budget.candidate_check_count == 6


def test_last_phase_candidate_can_continue_after_soft_deadline():
    budget, clock, _ = make_budget()
    with budget.phase_scope("last", 1, 2) as phase:
        with phase.candidate_attempt(identity()) as attempt:
            assert attempt.granted and budget.candidate_check_count == 1
            clock.now = 14
            assert attempt.allows_continue(identity())
        assert not phase.allows_new_work()
    assert phase.result.reason is Exit.CANDIDATE_SLICE_EXHAUSTED
    assert phase.result.soft_overrun_seconds == 2
    assert budget.stop_reason is None


def test_soft_time_prevents_new_work_even_when_no_candidate_is_generated():
    budget, clock, _ = make_budget()
    with budget.phase_scope("enumeration", 10, 2) as phase:
        assert phase.allows_new_work()
        clock.now = 12
        assert not phase.allows_new_work()
        assert not budget.consume_candidate_check()
    assert phase.result.reason is Exit.TIME_SLICE_EXHAUSTED
    assert phase.result.used_candidate_checks == 0 and budget.stop_reason is None
    with budget.phase_scope("next", 1, 1) as next_phase:
        assert budget.consume_candidate_check()
    assert next_phase.result.used_candidate_checks == 1


def test_child_time_and_global_deadlines_are_clipped_and_never_extended():
    budget, clock, _ = make_budget()
    limits = budget.search_deadline_monotonic, budget.final_deadline_monotonic
    with budget.phase_scope("p", 8, 5) as parent:
        clock.now = 13
        with budget.phase_scope("c", 3, 999) as child:
            assert child.soft_deadline == parent.soft_deadline == 15
            clock.now = 15
            assert not child.allows_new_work()
        assert not parent.allows_new_work()
    assert child.result.reason is parent.result.reason is Exit.TIME_SLICE_EXHAUSTED
    assert (budget.search_deadline_monotonic, budget.final_deadline_monotonic) == limits
    with budget.phase_scope("global_clip", 1, 999) as clipped:
        assert clipped.soft_deadline == 100


@pytest.mark.parametrize("stop_kind", ["cancel", "time", "system"])
def test_real_stop_blocks_inflight_and_cannot_be_cleared_by_next_phase(stop_kind):
    budget, clock, cancel = make_budget()
    with budget.phase_scope("first", 1, 2) as phase:
        with phase.candidate_attempt(identity()) as attempt:
            assert attempt.granted
            if stop_kind == "cancel":
                cancel.cancelled = True
                expected = Stop.USER_CANCELLED
            elif stop_kind == "time":
                clock.now = 100
                expected = Stop.SEARCH_TIME_LIMIT_REACHED
            else:
                budget.stop_reason = Stop.SYSTEM_ERROR
                expected = Stop.SYSTEM_ERROR
            assert not attempt.allows_continue(identity())
    with budget.phase_scope("next", 10, 1) as next_phase:
        assert not next_phase.allows_new_work()
    assert phase.result.reason is next_phase.result.reason is Exit.GLOBAL_STOP
    assert budget.stop_reason is expected and budget.candidate_check_count == 1


def test_cancellation_takes_precedence_after_soft_exit():
    budget, _, cancel = make_budget()
    with budget.phase_scope("first", 0, 0) as phase:
        assert not phase.allows_new_work()
        cancel.cancelled = True
        assert not budget.consume_candidate_check()
    assert phase.result.reason is Exit.GLOBAL_STOP
    assert budget.stop_reason is Stop.USER_CANCELLED


def test_finalization_window_is_not_lent_to_phase_search():
    budget, clock, _ = make_budget()
    with budget.phase_scope("search", 5, 999) as phase:
        clock.now = 100
        assert not phase.allows_new_work()
    assert budget.allows_finalization()
    clock.now = 110
    assert not budget.allows_finalization()
    assert budget.stop_reason is Stop.FINALIZATION_TIME_LIMIT_REACHED


@pytest.mark.parametrize("bad", [replace(identity(), task_fingerprint="other"),
    replace(identity(), plan_fingerprint="other"), replace(identity(), generation=4), identity(2), None])
def test_attempt_rejects_changed_identity_or_generation(bad):
    budget, _, _ = make_budget()
    with budget.phase_scope("x", 3, 10) as phase:
        with phase.candidate_attempt(identity()) as attempt:
            with pytest.raises(ValueError, match="identity or generation"):
                attempt.allows_continue(bad)
            assert attempt.allows_continue(identity())


def test_attempt_cannot_leak_to_next_candidate_or_phase():
    budget, _, _ = make_budget()
    with budget.phase_scope("x", 2, 10) as phase:
        with phase.candidate_attempt(identity()) as first:
            assert first.granted
        with phase.candidate_attempt(identity(2)) as second:
            assert not first.allows_continue(identity())
            assert second.allows_continue(identity(2))
    with budget.phase_scope("y", 1, 10) as next_phase:
        assert not second.allows_continue(identity(2))
    assert not first.allows_continue(identity()) and budget.candidate_check_count == 2


def test_denied_attempt_cannot_continue_or_charge_again():
    budget, _, _ = make_budget()
    with budget.phase_scope("none", 0, 10) as phase:
        with phase.candidate_attempt(identity()) as attempt:
            assert not attempt.granted and not attempt.allows_continue(identity())
        with pytest.raises(ValueError, match="reentered"):
            attempt.__enter__()
    assert budget.candidate_check_count == 0


@pytest.mark.parametrize("operation", ["nested_attempt", "charge", "child", "new_poll", "finish"])
def test_active_attempt_forbids_new_or_unrelated_work(operation):
    budget, _, _ = make_budget()
    with budget.phase_scope("x", 3, 10) as phase:
        with phase.candidate_attempt(identity()) as attempt:
            with pytest.raises(ValueError):
                if operation == "nested_attempt":
                    with phase.candidate_attempt(identity(2)):
                        pass
                elif operation == "charge":
                    budget.consume_candidate_check()
                elif operation == "child":
                    with budget.phase_scope("child", 1, 1):
                        pass
                elif operation == "new_poll":
                    phase.allows_new_work()
                else:
                    phase.finish()
            assert attempt.allows_continue(identity())
    assert budget.candidate_check_count == 1


def test_extra_bridge_variant_requires_new_charge():
    budget, _, _ = make_budget()
    with budget.phase_scope("variants", 1, 10) as phase:
        with phase.candidate_attempt(identity()) as direct:
            assert direct.granted
        with phase.candidate_attempt(identity(2)) as bridge:
            assert not bridge.granted
    assert budget.candidate_check_count == 1 and budget.stop_reason is None


def test_exception_unwinds_child_and_restores_parent():
    budget, _, _ = make_budget()
    with budget.phase_scope("p", 5, 10) as parent:
        with pytest.raises(RuntimeError):
            with budget.phase_scope("c", 2, 5) as child:
                assert budget.consume_candidate_check()
                raise RuntimeError("failed")
        assert budget._active_phase is parent
        assert budget.consume_candidate_check()
    assert budget._active_phase is None
    assert child.result.failed and child.result.reason is Exit.INCOMPLETE
    assert parent.result.used_candidate_checks == 2


def test_leaked_attempt_is_invalidated_even_on_bad_scope_use():
    budget, _, _ = make_budget()
    phase = budget.phase_scope("x", 2, 10)
    phase.__enter__()
    attempt = phase.candidate_attempt(identity())
    attempt.__enter__()
    with pytest.raises(ValueError, match="must close"):
        phase.__exit__(None, None, None)
    assert budget._active_phase is None and not attempt.allows_continue(identity())
    assert phase.result.failed


def test_lifo_misuse_does_not_corrupt_stack():
    budget, _, _ = make_budget()
    with budget.phase_scope("p", 5, 10) as parent:
        with budget.phase_scope("c", 2, 5) as child:
            with pytest.raises(ValueError, match="current active"):
                parent.__exit__(None, None, None)
            assert budget._active_phase is child
        assert budget._active_phase is parent


def test_cleanup_clock_error_restores_parent_without_masking_original_error():
    budget, clock, _ = make_budget()
    with budget.phase_scope("p", 5, 10) as parent:
        with pytest.raises(RuntimeError, match="original"):
            with budget.phase_scope("c", 2, 5) as child:
                clock.error = ValueError("bad clock")
                raise RuntimeError("original")
        clock.error = None
        assert budget._active_phase is parent
        assert child.result.ended_at is None and child.result.unused_time_seconds == 0
    assert budget._active_phase is None


def test_cleanup_failure_without_original_error_propagates():
    budget, clock, _ = make_budget()
    with pytest.raises(ValueError, match="bad clock"):
        with budget.phase_scope("c", 2, 5) as phase:
            clock.error = ValueError("bad clock")
    assert budget._active_phase is None and phase.result.failed
    assert phase.result.reason is Exit.INCOMPLETE


def test_backwards_exit_clock_is_error_not_time_credit():
    budget, clock, _ = make_budget()
    with pytest.raises(ValueError, match="backwards"):
        with budget.phase_scope("x", 2, 5) as phase:
            clock.now = 9
    assert phase.result.ended_at is None and phase.result.unused_time_seconds == 0


@pytest.mark.parametrize("reason", [Exit.SCAN_COMPLETE, Exit.NOT_APPLICABLE, Exit.INSUFFICIENT_BUDGET])
def test_explicit_internal_results_do_not_set_global_stop(reason):
    budget, _, _ = make_budget()
    with budget.phase_scope("x", 2, 5) as phase:
        phase.finish(reason)
        assert not budget.consume_candidate_check()
    assert phase.result.reason is reason and budget.stop_reason is None


def test_implicit_scope_exit_is_not_scan_completion():
    budget, _, _ = make_budget()
    with budget.phase_scope("x", 2, 5) as phase:
        pass
    assert phase.result.reason is Exit.INCOMPLETE


@pytest.mark.parametrize("reason", [Exit.CANDIDATE_SLICE_EXHAUSTED, Exit.TIME_SLICE_EXHAUSTED,
                                    Exit.GLOBAL_STOP, "scan_complete"])
def test_caller_cannot_forge_budget_exit(reason):
    budget, _, _ = make_budget()
    with budget.phase_scope("x", 2, 5) as phase:
        with pytest.raises(ValueError):
            phase.finish(reason)


def test_cannot_relabel_exhausted_phase_as_complete():
    budget, _, _ = make_budget()
    with budget.phase_scope("x", 1, 5) as phase:
        assert budget.consume_candidate_check()
        assert not budget.consume_candidate_check()
        with pytest.raises(ValueError):
            phase.finish()
    assert phase.result.reason is Exit.CANDIDATE_SLICE_EXHAUSTED


def test_completed_scope_and_result_are_not_reusable():
    budget, _, _ = make_budget()
    phase = budget.phase_scope("x", 2, 5)
    with pytest.raises(ValueError):
        phase.release_unused()
    with phase:
        with pytest.raises(ValueError):
            phase.__enter__()
        phase.finish()
    with pytest.raises(ValueError):
        phase.allows_new_work()
    with pytest.raises(FrozenInstanceError):
        phase.result.count_at_exit = 90


@pytest.mark.parametrize("name,count,seconds", [("", 1, 1), ("  ", 1, 1), (None, 1, 1),
    ("x", -1, 1), ("x", True, 1), ("x", 1.0, 1), ("x", 1, -1),
    ("x", 1, float("nan")), ("x", 1, float("inf")), ("x", 1, True)])
def test_invalid_phase_parameters_do_not_change_global_budget(name, count, seconds):
    budget, _, _ = make_budget()
    with pytest.raises(ValueError):
        budget.phase_scope(name, count, seconds)
    assert budget.candidate_check_count == 0 and budget._active_phase is None


@pytest.mark.parametrize("values", [("", "p", 0, 1), ("t", "", 0, 1),
    ("t", "p", -1, 1), ("t", "p", True, 1), ("t", "p", 0, 0), ("t", "p", 0, True)])
def test_invalid_attempt_identity(values):
    with pytest.raises(ValueError):
        Identity(*values)


def test_fixed_seed_nested_ledger_against_integer_reference():
    rng = random.Random(590531)
    for case in range(80):
        budget, _, _ = make_budget(1000, rng.randrange(20))
        initial = budget.candidate_check_count
        parent_limit = rng.randrange(1, 30)
        reference_used = 0
        with budget.phase_scope(str(case), parent_limit, 20) as parent:
            for index in range(6):
                grant = rng.randrange(10)
                expected_cap = min(grant, parent_limit - reference_used)
                with budget.phase_scope(str(index), grant, 5) as child:
                    assert child.candidate_limit == expected_cap
                    child_used = 0
                    while child_used < expected_cap:
                        assert budget.consume_candidate_check()
                        child_used += 1
                        reference_used += 1
                    assert not budget.consume_candidate_check()
                assert child.result.used_candidate_checks == child_used
                child.release_unused()
        assert budget.stop_reason is None
        assert parent.result.used_candidate_checks == reference_used <= parent_limit
        assert budget.candidate_check_count == initial + reference_used


def test_global_deadline_crossing_during_poll_is_not_reported_as_soft_timeout():
    budget, _, _ = make_budget()
    values = iter((10.0, 99.9, 100.0))
    budget.clock = lambda: next(values, 100.0)
    with budget.phase_scope("crossing", 1, 999) as phase:
        assert not phase.allows_new_work()
        assert budget.stop_reason is Stop.SEARCH_TIME_LIMIT_REACHED
    assert phase.result.reason is Exit.GLOBAL_STOP


def test_zero_time_slice_never_starts_candidate():
    budget, _, _ = make_budget()
    with budget.phase_scope("zero_time", 10, 0) as phase:
        with phase.candidate_attempt(identity()) as attempt:
            assert not attempt.granted
    assert phase.result.reason is Exit.TIME_SLICE_EXHAUSTED
    assert budget.candidate_check_count == 0 and budget.stop_reason is None


def test_large_deadline_sum_is_rejected_without_replacing_parent():
    budget, clock, _ = make_budget()
    with budget.phase_scope("parent", 4, 1) as parent:
        clock.now = 1e308
        with pytest.raises(ValueError, match="phase_deadline"):
            with budget.phase_scope("overflow", 1, 1e308):
                pass
        assert budget._active_phase is parent
        clock.now = 10


def test_successful_last_attempt_and_scan_completion_do_not_fake_exhaustion():
    budget, _, _ = make_budget(1)
    with budget.phase_scope("complete", 1, 5) as phase:
        with phase.candidate_attempt(identity()) as attempt:
            assert attempt.granted and attempt.allows_continue(identity())
        phase.finish()
    assert phase.result.reason is Exit.SCAN_COMPLETE and budget.stop_reason is None
    assert budget.candidate_check_count == 1
    # The orchestrator, not scope exit, decides whole-search completion/stop.


def test_scope_without_global_budget_is_rejected():
    with pytest.raises(ValueError, match="shared"):
        phases.SearchPhaseBudget(object(), "x", 1, 1)


def test_attempt_without_identity_is_rejected_without_charging():
    budget, _, _ = make_budget()
    with budget.phase_scope("x", 1, 1) as phase:
        with pytest.raises(ValueError, match="SearchAttemptIdentity"):
            phase.candidate_attempt(object())
    assert budget.candidate_check_count == 0


def test_charged_scope_cannot_be_reported_as_skipped():
    budget, _, _ = make_budget()
    with budget.phase_scope("x", 2, 1) as phase:
        assert budget.consume_candidate_check()
        with pytest.raises(ValueError, match="skipped"):
            phase.finish(Exit.NOT_APPLICABLE)


def test_uncharged_peek_never_creates_candidate_authorization():
    budget, _, _ = make_budget()
    with budget.phase_scope("x", 2, 1) as phase:
        assert phase.allows_new_work()
        attempt = phase.candidate_attempt(identity())
        assert not attempt.allows_continue(identity())
        assert budget.candidate_check_count == 0
