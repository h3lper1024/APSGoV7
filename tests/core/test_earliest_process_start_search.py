"""Release-time repair uses existing candidates, quota and atomic publication."""
from dataclasses import replace
from decimal import Decimal

import numpy as np
import pytest

from apsgo_scheduler.core import _numeric_refinement as refinement
from apsgo_scheduler.core import _numeric_refinement_scan as scan
from apsgo_scheduler.core import _numeric_search as search
from apsgo_scheduler.core import _numeric_candidate_kernel as candidate
from apsgo_scheduler.core._numeric_kernel import task_columns
from apsgo_scheduler.core._numeric_rules import NumericReason
from apsgo_scheduler.core._numeric_state import NumericPlan
from apsgo_scheduler.core._numeric_evaluation import evaluate_numeric_plan
from apsgo_scheduler.core._numeric_audit import audit_numeric_core_without_search_cache
from tests.core.test_earliest_process_start_rule import early_request
from tests.core.test_numeric_boundary_audit import numeric_state, audit_budget
from tests.core.test_numeric_construction import budget


def repair_state(*, lower="2026-06-01T03:00:00+08:00", enabled=True):
    request = early_request(lower=lower, enabled=enabled)
    request = replace(request, orders=tuple(replace(order, width=Decimal(1000)) for order in request.orders))
    problem, rules, state = numeric_state(request)
    return request, problem, rules, state


@pytest.mark.parametrize("size", (1, 8))
def test_existing_refinement_repairs_via_valid_intermediate_early_candidates(size):
    request, problem, rules, state = repair_state()
    runtime = budget(candidate_limit=500)
    refinement.improve_numeric_refinement(state, runtime, maximum_virtual_bridge_nodes=0, _batch_size=size)
    assert not any(v.prohibited for v in state.evaluation.violations)
    assert state.accepted_move_count >= 2
    assert state.accepted_moves[0].quality_after[0] == 1
    assert state.virtual_sequence == 0
    assert state.evaluation.delivery.node_end_ms[-1] == 7 * 3600000
    assert audit_numeric_core_without_search_cache(state, problem, rules, request.delivery_timing, audit_budget()).report.passed


def test_other_hard_problem_still_blocks_refinement():
    _, _, state = numeric_state(early_request())
    plan = NumericPlan.build(state.task, (2, 0, 1, 3, 4, 5, 6), (0, 7), (10,), (0,))
    state.plan = plan
    state.evaluation = evaluate_numeric_plan(state.task, state.program, state.quality, plan)
    assert any(v.prohibited and v.reason is not NumericReason.EARLY_START for v in state.evaluation.violations)
    runtime = budget(candidate_limit=100)
    refinement.improve_numeric_refinement(state, runtime)
    assert runtime.candidate_check_count == state.accepted_move_count == 0


def test_new_reverse_width_violation_is_rejected_even_if_early_score_improves():
    request = early_request(lower="2026-06-01T03:00:00+08:00")
    request = replace(request, delivery_timing=replace(request.delivery_timing, orders=tuple(
        replace(o, earliest_start_at="2026-06-01T03:00:00+08:00") if i < 2 else
        replace(o, duration_hours=Decimal(4)) if i == 3 else o
        for i, o in enumerate(request.delivery_timing.orders))))
    _, _, state = numeric_state(request)
    before = state.plan
    # Bringing node 3 forward reduces two early starts but adds reverse-width violations.
    description = scan.description(scan.INTRA, 10, 10, 3, 4, 0)
    accepted, _ = refinement._scan_family(state, budget(candidate_limit=10), iter((description,)), maximum_virtual_bridge_nodes=0)
    assert not accepted and state.plan is before


def test_early_focus_is_stable_and_each_intra_move_has_one_lane():
    _, _, _, state = repair_state()
    view = scan.build_scan(state, task_columns(state.task))
    assert view.sources[0] == 0
    regular, critical = [], []
    for owner in view.sources:
        for lane, target in ((False, regular), (True, critical)):
            target.extend(tuple(map(int, d)) for d in scan.intra(view, int(owner), lane) if d[0] >= 0)
    assert not set(regular) & set(critical)
    assert len(regular + critical) == len(set(regular + critical)) == 21
    # Ready node 1 moving before unready node 0 belongs to the focus lane.
    assert tuple(map(int, scan.description(scan.INTRA, 10, 10, 1, 2, 0))) in critical


def test_budget_exhaustion_keeps_early_result_without_waiting():
    _, _, _, state = repair_state()
    before = state.plan
    runtime = budget(candidate_limit=0)
    refinement.improve_numeric_refinement(state, runtime)
    assert state.plan is before and runtime.candidate_check_count == 0
    assert any(v.reason is NumericReason.EARLY_START for v in state.evaluation.violations)
    assert state.evaluation.delivery.node_end_ms[0] == 3600000


def test_disabled_rule_does_not_add_focus():
    _, _, _, state = repair_state(enabled=False)
    view = scan.build_scan(state, task_columns(state.task))
    assert not view.early.any()
