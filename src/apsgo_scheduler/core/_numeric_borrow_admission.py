"""Search-bound adapters for the single FB1 numeric future-borrow guard.

Only consumed candidates update diagnostics. Formal commit rechecks the same
predicate without counting twice. This module never creates a plan, evaluates
rules, charges a candidate, resets a deadline, or mutates accepted state.
"""

from dataclasses import dataclass, field
import logging
from time import perf_counter

import numpy as np

from ._numeric_borrow_readiness import (
    BORROW_ALLOWED, BORROW_BEFORE_EARLIEST, BORROW_EXISTING_WORSENED,
    BORROW_CURSOR_SIZE, BorrowReadinessIndex, BorrowReadinessPieces,
    borrow_readiness_step, _VISITED,
)
from ._numeric_kernel import task_columns, private_task_columns, flat_chain_view
from ._numeric_rules import NumericRuleKind
from ._numeric_state import MORE_WORK, OK, NumericSearchAction, DESCRIPTOR_FIELDS
from ._numeric_units import NumericValueError
from .budget import SolveRuntimeBudget

logger = logging.getLogger(__name__)
_WORK_LIMIT = 256
_SAMPLE_LIMIT = 3
_ACTION = DESCRIPTOR_FIELDS.index("action")
_REASONS = {
    BORROW_BEFORE_EARLIEST: "future_borrow_before_earliest",
    BORROW_EXISTING_WORSENED: "existing_future_borrow_worsened",
}
_OWNER_FIELDS = ("task", "program", "quality", "plan", "evaluation")


@dataclass(slots=True)
class BorrowAdmissionDiagnostics:
    checked: int = 0
    passed: int = 0
    rejected_new_or_deeper_borrow: int = 0
    rejected_existing_borrow_worsened: int = 0
    interrupted: int = 0
    errors: int = 0
    scanned_nodes: int = 0
    elapsed_seconds: float = 0.0
    samples: list = field(default_factory=list)

    def snapshot(self):
        return {name: getattr(self, name) for name in (
            "checked", "passed", "rejected_new_or_deeper_borrow",
            "rejected_existing_borrow_worsened", "interrupted", "errors",
            "scanned_nodes", "elapsed_seconds",
        )} | {"first_rejection_samples": [dict(item) for item in self.samples]}


def log_borrow_admission_summary(diagnostics):
    if diagnostics.checked:
        logger.info("numeric_future_borrow_summary %s", diagnostics.snapshot())


def _require_old_binding(state, owner):
    if any(getattr(state, name) is not value for name, value in zip(_OWNER_FIELDS, owner)):
        raise NumericValueError("borrow_admission", "stale accepted generation")
    task, program, quality, plan, evaluation = owner
    if (program.task_fingerprint != task.fingerprint
            or quality.task_fingerprint != task.fingerprint
            or quality.rule_program_fingerprint != program.fingerprint
            or plan.task_fingerprint != task.fingerprint
            or evaluation.task_fingerprint != task.fingerprint
            or evaluation.rule_program_fingerprint != program.fingerprint
            or evaluation.quality_program_fingerprint != quality.fingerprint
            or evaluation.plan_fingerprint != plan.fingerprint
            or evaluation.plan_generation != plan.generation):
        raise NumericValueError("borrow_admission", "current evaluation identity mismatch")


def _scan(state, budget, columns, view, ends, pieces, require_candidate,
          diagnostics=None, descriptor=None):
    """Return (True/False/None, witness): pass/refusal/interruption.

    Numerical/identity errors raise. A private cursor never escapes this call.
    The two adapters below differ only in how they expose candidate columns.
    """
    if not isinstance(budget, SolveRuntimeBudget):
        raise NumericValueError("borrow_admission.budget", "shared runtime budget required")
    owner = tuple(getattr(state, name) for name in _OWNER_FIELDS)
    _require_old_binding(state, owner)
    require_candidate()
    if not budget.allows_search():
        return None, None
    old_task, _, _, plan, evaluation = owner
    old_columns = task_columns(old_task)
    index = BorrowReadinessIndex(plan.node_rows, plan.chain_offsets, plan.chain_periods,
        plan.row_to_chain, plan.row_to_position, evaluation.delivery.node_end_ms,
        len(old_task.period_ids))
    cursor, status = np.zeros(BORROW_CURSOR_SIZE, np.int64), np.zeros(5, np.int64)
    started = perf_counter()
    if diagnostics is not None:
        diagnostics.checked += 1
    outcome, witness, failed = None, None, False
    try:
        while True:
            _require_old_binding(state, owner)
            require_candidate()
            if not budget.allows_search():
                break
            code, passed, complete, witness = borrow_readiness_step(
                old_columns, columns, index, view, ends, pieces, cursor, status, _WORK_LIMIT)
            if code not in (OK, MORE_WORK):
                raise NumericValueError("borrow_admission.numeric",
                    f"guard failed: code={int(code)} location={status.tolist()}")
            if code == MORE_WORK:
                if complete or passed:
                    raise NumericValueError("borrow_admission.numeric", "partial guard cannot pass")
                continue
            if (not complete or passed and witness.reason != BORROW_ALLOWED
                    or not passed and witness.reason not in _REASONS):
                raise NumericValueError("borrow_admission.numeric", "invalid terminal guard result")
            _require_old_binding(state, owner)
            require_candidate()
            if budget.allows_search():
                outcome = bool(passed)
            break
    except Exception:
        failed = True
        if diagnostics is not None:
            diagnostics.errors += 1
        raise
    finally:
        if diagnostics is not None:
            diagnostics.scanned_nodes += int(cursor[_VISITED])
            diagnostics.elapsed_seconds += perf_counter() - started
            if failed:
                log_borrow_admission_summary(diagnostics)
    if diagnostics is not None:
        if outcome is None:
            diagnostics.interrupted += 1
            log_borrow_admission_summary(diagnostics)
        elif outcome:
            diagnostics.passed += 1
        else:
            name = ("rejected_new_or_deeper_borrow" if witness.reason == BORROW_BEFORE_EARLIEST
                    else "rejected_existing_borrow_worsened")
            setattr(diagnostics, name, getattr(diagnostics, name) + 1)
            if len(diagnostics.samples) < _SAMPLE_LIMIT:
                sample = {name: int(value) for name, value in witness._asdict().items()
                          if name != "has_old"}
                sample.update(reason=_REASONS[witness.reason], has_old=bool(witness.has_old),
                    action=tuple(NumericSearchAction)[int(descriptor[_ACTION])].value,
                    generation=plan.generation, chain_id=int(view.ids[witness.chain]),
                    source_order_id=old_task.source_ids[witness.source])
                if not witness.has_old:
                    for name in ("old_period", "old_start_ms", "old_early_ms"):
                        sample[name] = None
                diagnostics.samples.append(sample)
                logger.info("numeric_future_borrow_rejection %s", sample)
    return outcome, witness


def check_candidate_borrow(state, budget, workspace, result):
    """Consumed candidate only; False never authorizes materialization/commit."""
    if not state.program.for_kind(NumericRuleKind.EARLIEST_START):
        return True
    original_view, summary = result.view, result.summary
    n, g, changed = workspace.node_count, workspace.group_count, workspace.changed_count

    def require_candidate():
        workspace.require_current(state.task, state.plan)
        workspace.require_view(original_view)
        if (result.view is not original_view or result.summary is not summary
                or result.program is not state.program or result.quality_program is not state.quality
                or summary is None or summary.status[0] != OK
                or (workspace.node_count, workspace.group_count, workspace.changed_count) != (n, g, changed)):
            raise NumericValueError("borrow_admission", "candidate summary or rule binding changed")

    require_candidate()
    if (not 0 <= n <= workspace.nodes.weight.size
            or not 0 <= g <= workspace.split_groups.parent_row.size
            or not 0 <= changed <= workspace.changed_rows.size):
        raise NumericValueError("borrow_admission", "invalid active private resource counts")
    old, groups = state.task.nodes, state.task.split_groups
    columns = private_task_columns(task_columns(state.task), workspace.nodes, workspace.derived, n)
    pieces = BorrowReadinessPieces(groups.parent_row.size, old.split_group,
        (old.split_group, workspace.nodes.split_group[:n]),
        (old.piece_index, workspace.nodes.piece_index[:n]),
        (old.piece_count, workspace.nodes.piece_count[:n]),
        (groups.parent_row, workspace.split_groups.parent_row[:g]),
        (groups.target_period, workspace.split_groups.target_period[:g]))
    active_view = original_view._replace(changed_rows=workspace.changed_rows[:changed])
    result_guard = _scan(state, budget, columns, active_view, summary.ends, pieces,
                        require_candidate, state.borrow_diagnostics, result.descriptor)
    return result_guard is not None and result_guard[0] is True


def check_committed_borrow(state, budget, task, program, quality, plan, evaluation):
    """Recheck materialized data before writing state; violations are contract errors.

    Budget is mandatory when enabled, including for a direct commit caller.
    No consumption counters or rejection examples are recorded a second time.
    """
    if not state.program.for_kind(NumericRuleKind.EARLIEST_START):
        return True

    def require_candidate():
        if (task is not state.task and state.task.fingerprint not in task.ancestor_fingerprints
                or task.period_ids != state.task.period_ids
                or program.rules != state.program.rules
                or program.rule_set_fingerprint != state.program.rule_set_fingerprint
                or quality.objectives != state.quality.objectives
                or program.task_fingerprint != task.fingerprint
                or quality.task_fingerprint != task.fingerprint
                or quality.rule_program_fingerprint != program.fingerprint
                or plan.task_fingerprint != task.fingerprint
                or plan.generation != state.plan.generation + 1
                or evaluation.task_fingerprint != task.fingerprint
                or evaluation.plan_fingerprint != plan.fingerprint
                or evaluation.plan_generation != plan.generation
                or evaluation.rule_program_fingerprint != program.fingerprint
                or evaluation.quality_program_fingerprint != quality.fingerprint):
            raise NumericValueError("borrow_admission.commit", "unbound materialized candidate")

    require_candidate()
    view = flat_chain_view(plan.node_rows, plan.chain_offsets, plan.chain_periods, plan.chain_ids)
    old, groups = state.task.nodes, state.task.split_groups
    pieces = BorrowReadinessPieces(groups.parent_row.size, old.split_group,
        task.nodes.split_group, task.nodes.piece_index, task.nodes.piece_count,
        task.split_groups.parent_row, task.split_groups.target_period)
    result_guard = _scan(state, budget, task_columns(task), view,
                        evaluation.delivery.node_end_ms, pieces, require_candidate)
    if result_guard is None or result_guard[0] is None:
        return False
    passed, witness = result_guard
    if not passed:
        raise NumericValueError("borrow_admission.commit",
            f"{_REASONS[witness.reason]}: row={int(witness.row)} source={int(witness.source)}")
    return True
