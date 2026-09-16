"""Numeric candidate edits and the static parts of first local search."""

from dataclasses import dataclass
from math import isfinite
from time import perf_counter

import numpy as np
from numba import njit

from ._numeric_construction import NumericInitialSolution
from ._numeric_evaluation import (
    NumericPlanEvaluation, NumericQualityProgram, materialize_numeric_evaluation,
    preview_numeric_chain_order_quality,
)
from ._numeric_resources import materialize_private_resources
from . import _numeric_candidate_kernel as common_candidate
from ._numeric_candidate_kernel import NumericDeferredCandidateFailure, capture_candidate_result
from ._numeric_chain_ops import chain_rows
from ._numeric_kernel import (
    task_columns, rule_tables, flat_chain_view, evaluate_chain_kernel,
    edge_node, all_edges_allowed_values, _add,
)
from ._numeric_rules import (
    NumericMetricKind, NumericRuleKind, NumericRuleProgram, evaluate_numeric_split,
)
from ._numeric_state import (
    NumericPlan, NumericTask, NumericSearchAction, NumericCandidateWorkspace,
    NumericCandidateDescriptors, DESCRIPTOR_FIELDS, readonly,
    split_target_periods_match, OK, CANCELLED, CAPACITY,
)
from ._numeric_units import NumericValueError, checked_sum, int64
from .budget import SolveRuntimeBudget
from .contracts import SearchStopReason, fingerprint
from .model import MaterialRole

_GENERATED_VIRTUAL = tuple(MaterialRole).index(MaterialRole.GENERATED_VIRTUAL)


@dataclass(frozen=True, slots=True)
class NumericCandidateEdit:
    task_fingerprint: str
    plan_fingerprint: str
    generation: int
    sequence: int
    action: NumericSearchAction
    source_chain_id: int
    target_chain_id: int
    node_row: int = -1
    target_position: int = -1
    source_reversed: bool = False
    target_reversed: bool = False
    source_start: int = -1
    source_stop: int = -1
    target_start: int = -1
    target_stop: int = -1

    def __post_init__(self):
        for name in ("task_fingerprint", "plan_fingerprint"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise NumericValueError("candidate", f"nonempty {name} required")
        for name in (
            "generation",
            "sequence",
            "source_chain_id",
            "target_chain_id",
            "node_row",
            "target_position",
            "source_start",
            "source_stop",
            "target_start",
            "target_stop",
        ):
            int64(getattr(self, name), name)
        if self.generation < 0 or self.sequence <= 0:
            raise NumericValueError(
                "candidate", "nonnegative generation and positive sequence required"
            )
        if not isinstance(self.action, NumericSearchAction):
            raise NumericValueError("candidate", "known numeric search action required")
        same_chain_actions = {
            NumericSearchAction.VIRTUAL_WEIGHT_FILL,
            NumericSearchAction.DELIVERY_INTRA_MOVE,
            NumericSearchAction.BRIDGE_RECLAMATION,
        }
        if self.source_chain_id == self.target_chain_id and self.action not in same_chain_actions:
            raise NumericValueError("candidate", "source and target chains must differ")
        if type(self.source_reversed) is not bool or type(self.target_reversed) is not bool:
            raise NumericValueError("candidate", "reversal flags must be boolean")
        if self.action is NumericSearchAction.REAL_NODE_RELOCATION:
            if (
                self.node_row < 0
                or self.target_position < 0
                or self.source_reversed
                or self.target_reversed
            ):
                raise NumericValueError("candidate", "invalid real-node relocation description")
        elif self.action is NumericSearchAction.CHAIN_ORDER_RELOCATION:
            if (
                self.node_row != -1
                or self.target_position < 0
                or self.source_reversed
                or self.target_reversed
            ):
                raise NumericValueError("candidate", "invalid chain-order description")
        elif self.action is NumericSearchAction.CONTROLLED_ORDER_SPLIT:
            if (
                self.node_row < 0
                or self.target_position != -1
                or self.source_reversed
                or self.target_reversed
            ):
                raise NumericValueError("candidate", "invalid controlled-split description")
        elif self.action is NumericSearchAction.VIRTUAL_WEIGHT_FILL:
            if (
                self.node_row != -1
                or self.target_position < 0
                or self.source_reversed
                or self.target_reversed
            ):
                raise NumericValueError("candidate", "invalid virtual-fill description")
        elif self.action is NumericSearchAction.DELIVERY_INTRA_MOVE:
            if not (
                self.node_row == -1
                and self.target_position >= 0
                and self.source_start >= 0
                and self.source_stop == self.source_start + 1
                and self.target_position < self.source_start
                and self.target_start == self.target_stop == -1
                and not self.source_reversed
                and not self.target_reversed
            ):
                raise NumericValueError("candidate", "invalid intra-chain move description")
        elif self.action in {NumericSearchAction.NODE_MOVE, NumericSearchAction.BLOCK_MOVE}:
            expected = 1 if self.action is NumericSearchAction.NODE_MOVE else 2
            if not (
                self.node_row == -1
                and self.target_position >= 0
                and self.source_start >= 0
                and self.source_stop - self.source_start >= expected
                and self.target_start == self.target_stop == -1
                and not self.source_reversed
                and not self.target_reversed
            ):
                raise NumericValueError("candidate", "invalid inter-chain move description")
        elif self.action in {NumericSearchAction.NODE_EXCHANGE, NumericSearchAction.BLOCK_EXCHANGE}:
            minimum = 1 if self.action is NumericSearchAction.NODE_EXCHANGE else 2
            if not (
                self.node_row == self.target_position == -1
                and self.source_start >= 0
                and self.source_stop - self.source_start >= minimum
                and self.target_start >= 0
                and self.target_stop > self.target_start
                and not self.source_reversed
                and not self.target_reversed
            ):
                raise NumericValueError("candidate", "invalid inter-chain exchange description")
        elif self.action is NumericSearchAction.CHAIN_CUT:
            if not (
                self.node_row == self.target_position == -1
                and self.source_start > 0
                and self.source_stop == -1
                and self.target_start >= 0
                and self.target_stop >= 0
                and self.target_start != self.target_stop
                and not self.source_reversed
                and not self.target_reversed
            ):
                raise NumericValueError("candidate", "invalid chain-cut description")
        elif self.action is NumericSearchAction.BRIDGE_RECLAMATION:
            if not (
                self.node_row == self.target_position == -1
                and self.source_start >= 0
                and self.source_stop > self.source_start
                and self.target_start == self.target_stop == -1
                and not self.source_reversed
                and not self.target_reversed
            ):
                raise NumericValueError("candidate", "invalid bridge-reclamation description")
        elif self.node_row != -1 or self.target_position < 0:
            raise NumericValueError("candidate", "invalid whole-chain description")


@dataclass(frozen=True, slots=True)
class NumericAcceptedMove:
    sequence: int
    action: NumericSearchAction
    affected_chain_ids: tuple[int, ...]
    affected_rows: tuple[int, ...]
    affected_sources: tuple[int, ...]
    quality_before: tuple[int, ...]
    quality_after: tuple[int, ...]

    def __post_init__(self):
        int64(self.sequence, "accepted.sequence")
        if self.sequence <= 0 or not isinstance(self.action, NumericSearchAction):
            raise NumericValueError("accepted", "positive sequence and known action required")
        if (
            not self.affected_chain_ids
            or len(set(self.affected_chain_ids)) != len(self.affected_chain_ids)
            or any(type(value) is not int or value < 0 for value in self.affected_chain_ids)
            or any(type(value) is not int or value < 0 for value in self.affected_rows)
            or not self.affected_sources
            or len(set(self.affected_sources)) != len(self.affected_sources)
            or any(type(value) is not int or value < 0 for value in self.affected_sources)
            or len(self.quality_before) != len(self.quality_after)
            or any(type(value) is not int for value in (*self.quality_before, *self.quality_after))
            or not self.quality_after < self.quality_before
        ):
            raise NumericValueError("accepted", "complete strictly improving move trace required")


@dataclass(slots=True)
class NumericSearchState:
    task: NumericTask
    program: NumericRuleProgram
    quality: NumericQualityProgram
    plan: NumericPlan
    evaluation: NumericPlanEvaluation
    accepted_move_count: int = 0
    complete_candidate_evaluation_count: int = 0
    bridge_required_candidate_count: int = 0
    virtual_sequence: int = 0
    split_sequence: int = 0
    replay_count: int = 0
    accepted_moves: tuple[NumericAcceptedMove, ...] = ()

    def __post_init__(self):
        if (
            not isinstance(self.task, NumericTask)
            or not isinstance(self.program, NumericRuleProgram)
            or not isinstance(self.quality, NumericQualityProgram)
            or not isinstance(self.plan, NumericPlan)
            or not isinstance(self.evaluation, NumericPlanEvaluation)
            or self.program.task_fingerprint != self.task.fingerprint
            or self.quality.task_fingerprint != self.task.fingerprint
            or self.quality.rule_program_fingerprint != self.program.fingerprint
            or self.plan.task_fingerprint != self.task.fingerprint
            or self.evaluation.task_fingerprint != self.task.fingerprint
            or self.evaluation.rule_program_fingerprint != self.program.fingerprint
            or self.evaluation.quality_program_fingerprint != self.quality.fingerprint
            or self.evaluation.plan_fingerprint != self.plan.fingerprint
            or self.evaluation.plan_generation != self.plan.generation
        ):
            raise NumericValueError("search_state", "matching numeric plan and evaluation required")
        for name in (
            "accepted_move_count",
            "complete_candidate_evaluation_count",
            "bridge_required_candidate_count",
            "virtual_sequence",
            "split_sequence",
            "replay_count",
        ):
            if int64(getattr(self, name), name) < 0:
                raise NumericValueError(name, "nonnegative search counter required")
        if (
            not isinstance(self.accepted_moves, tuple)
            or any(not isinstance(value, NumericAcceptedMove) for value in self.accepted_moves)
            or len(self.accepted_moves) != self.accepted_move_count
        ):
            raise NumericValueError("accepted_moves", "accepted trace count mismatch")

    @classmethod
    def start(cls, task, program, quality, initial):
        if (
            not isinstance(task, NumericTask)
            or not isinstance(program, NumericRuleProgram)
            or not isinstance(quality, NumericQualityProgram)
            or not isinstance(initial, NumericInitialSolution)
            or not initial.complete
            or initial.plan is None
            or initial.evaluation is None
        ):
            raise NumericValueError("search_state", "complete numeric initial solution required")
        return cls(task, program, quality, initial.plan, initial.evaluation)

    def commit(
        self,
        candidate_task,
        candidate_program,
        candidate_quality,
        candidate,
        evaluation,
        edit,
        affected_rows,
        *,
        virtual_sequence=None,
        split_sequence=None,
    ):
        if (
            not isinstance(candidate_task, NumericTask)
            or not isinstance(candidate_program, NumericRuleProgram)
            or not isinstance(candidate_quality, NumericQualityProgram)
            or not isinstance(candidate, NumericPlan)
            or not isinstance(evaluation, NumericPlanEvaluation)
            or not isinstance(edit, NumericCandidateEdit)
            or candidate_program.task_fingerprint != candidate_task.fingerprint
            or candidate_quality.task_fingerprint != candidate_task.fingerprint
            or candidate_quality.rule_program_fingerprint != candidate_program.fingerprint
            or candidate.task_fingerprint != candidate_task.fingerprint
            or candidate.generation != self.plan.generation + 1
            or evaluation.plan_fingerprint != candidate.fingerprint
            or evaluation.plan_generation != candidate.generation
            or evaluation.task_fingerprint != candidate_task.fingerprint
            or evaluation.rule_program_fingerprint != candidate_program.fingerprint
            or evaluation.quality_program_fingerprint != candidate_quality.fingerprint
            or edit.task_fingerprint != self.task.fingerprint
            or edit.plan_fingerprint != self.plan.fingerprint
            or edit.generation != self.plan.generation
        ):
            raise NumericValueError("candidate", "complete next-generation candidate required")
        before = tuple(int(value) for value in self.evaluation.quality_key)
        after = tuple(int(value) for value in evaluation.quality_key)
        affected = tuple(
            dict.fromkeys(
                (edit.source_chain_id,)
                if edit.action
                in {
                    NumericSearchAction.CHAIN_ORDER_RELOCATION,
                    NumericSearchAction.VIRTUAL_WEIGHT_FILL,
                    NumericSearchAction.DELIVERY_INTRA_MOVE,
                    NumericSearchAction.BRIDGE_RECLAMATION,
                }
                else (edit.source_chain_id, edit.target_chain_id)
            )
        )
        affected_sources = tuple(
            dict.fromkeys(
                int(candidate_task.nodes.source[int(row)])
                for chain_id in affected
                for chain_index in range(candidate.chain_ids.size)
                if int(candidate.chain_ids[chain_index]) == chain_id
                for row in candidate.node_rows[
                    int(candidate.chain_offsets[chain_index]) :
                    int(candidate.chain_offsets[chain_index + 1])
                ]
                if int(candidate_task.nodes.source[int(row)]) >= 0
            )
        )
        move = NumericAcceptedMove(
            edit.sequence,
            edit.action,
            affected,
            tuple(dict.fromkeys(affected_rows)),
            affected_sources,
            before,
            after,
        )
        next_virtual_sequence = (
            self.virtual_sequence if virtual_sequence is None else virtual_sequence
        )
        next_split_sequence = self.split_sequence if split_sequence is None else split_sequence
        if (
            int64(next_virtual_sequence, "virtual_sequence") < self.virtual_sequence
            or int64(next_split_sequence, "split_sequence") < self.split_sequence
        ):
            raise NumericValueError("accepted_sequence", "accepted sequence cannot go backwards")
        self.task = candidate_task
        self.program = candidate_program
        self.quality = candidate_quality
        self.plan = candidate
        self.evaluation = evaluation
        self.accepted_move_count += 1
        self.accepted_moves += (move,)
        self.virtual_sequence = next_virtual_sequence
        self.split_sequence = next_split_sequence


@dataclass(frozen=True, slots=True)
class NumericSearchCheckpoint:
    task_fingerprint: str
    plan_fingerprint: str
    plan_generation: int
    quality_key: tuple[int, ...]
    candidate_check_count: int
    complete_candidate_evaluation_count: int
    accepted_move_count: int
    bridge_required_candidate_count: int
    virtual_sequence: int
    split_sequence: int
    replay_count: int
    stop_reason: SearchStopReason | None
    elapsed_seconds: float
    fingerprint: str

    def __post_init__(self):
        for name in ("task_fingerprint", "plan_fingerprint", "fingerprint"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise NumericValueError("checkpoint", f"nonempty {name} required")
        for name in (
            "plan_generation",
            "candidate_check_count",
            "complete_candidate_evaluation_count",
            "accepted_move_count",
            "bridge_required_candidate_count",
            "virtual_sequence",
            "split_sequence",
            "replay_count",
        ):
            if int64(getattr(self, name), name) < 0:
                raise NumericValueError(name, "nonnegative checkpoint counter required")
        if (
            len(self.quality_key) != 9
            or any(type(value) is not int for value in self.quality_key)
            or (self.stop_reason is not None and not isinstance(self.stop_reason, SearchStopReason))
            or type(self.elapsed_seconds) not in (int, float)
            or not isfinite(self.elapsed_seconds)
            or self.elapsed_seconds < 0
        ):
            raise NumericValueError("checkpoint", "invalid numeric search checkpoint")

    @classmethod
    def capture(cls, task, state, budget, started):
        if not isinstance(task, NumericTask) or not isinstance(state, NumericSearchState):
            raise NumericValueError("checkpoint", "numeric task and search state required")
        elapsed = perf_counter() - started
        values = dict(
            task=task.fingerprint,
            plan=state.plan.fingerprint,
            generation=state.plan.generation,
            quality=tuple(int(value) for value in state.evaluation.quality_key),
            checked=budget.candidate_check_count,
            evaluated=state.complete_candidate_evaluation_count,
            accepted=state.accepted_move_count,
            bridge_required=state.bridge_required_candidate_count,
            virtual_sequence=state.virtual_sequence,
            split_sequence=state.split_sequence,
            replay_count=state.replay_count,
            moves=tuple(
                (
                    move.sequence,
                    move.action.value,
                    move.affected_chain_ids,
                    move.affected_rows,
                    move.quality_before,
                    move.quality_after,
                )
                for move in state.accepted_moves
            ),
            stop=None if budget.stop_reason is None else budget.stop_reason.value,
        )
        return cls(
            task.fingerprint,
            state.plan.fingerprint,
            state.plan.generation,
            values["quality"],
            values["checked"],
            values["evaluated"],
            values["accepted"],
            values["bridge_required"],
            values["virtual_sequence"],
            values["split_sequence"],
            values["replay_count"],
            budget.stop_reason,
            elapsed,
            fingerprint(values),
        )


def _build_plan(task, current, chains, chain_ids, periods, *, group_periods=True):
    if not chains or not (len(chains) == len(chain_ids) == len(periods)):
        raise NumericValueError("candidate", "nonempty consistent candidate chains required")
    if group_periods:
        order = sorted(range(len(chains)), key=periods.__getitem__)
        chains = [chains[index] for index in order]
        chain_ids = [chain_ids[index] for index in order]
        periods = [periods[index] for index in order]
    rows = [row for chain in chains for row in chain]
    offsets = [0]
    for chain in chains:
        offsets.append(offsets[-1] + len(chain))
    return NumericPlan.build(
        task,
        rows,
        offsets,
        chain_ids,
        periods,
        generation=current.generation + 1,
    )


def _quality(evaluation):
    return tuple(int(value) for value in evaluation.quality_key)


def _common_candidate_edit(state, descriptor, sequence):
    action = tuple(NumericSearchAction)[int(descriptor[common_candidate.ACTION])]
    values = dict(task_fingerprint=state.task.fingerprint, plan_fingerprint=state.plan.fingerprint,
        generation=state.plan.generation, sequence=sequence, action=action,
        source_chain_id=int(descriptor[common_candidate.SOURCE]),
        target_chain_id=int(descriptor[common_candidate.TARGET]))
    if action in {NumericSearchAction.WHOLE_CHAIN_APPEND, NumericSearchAction.WHOLE_CHAIN_PREPEND,
                  NumericSearchAction.WHOLE_CHAIN_INSERTION}:
        values.update(target_position=int(descriptor[common_candidate.POSITION]),
            source_reversed=bool(descriptor[common_candidate.REVERSE_SOURCE]),
            target_reversed=bool(descriptor[common_candidate.REVERSE_TARGET]))
    elif action in {NumericSearchAction.REAL_NODE_RELOCATION, NumericSearchAction.CONTROLLED_ORDER_SPLIT}:
        values.update(node_row=int(descriptor[common_candidate.NODE]))
        if action is NumericSearchAction.REAL_NODE_RELOCATION:
            values.update(target_position=int(descriptor[common_candidate.POSITION]))
    elif action in {NumericSearchAction.CHAIN_ORDER_RELOCATION, NumericSearchAction.VIRTUAL_WEIGHT_FILL}:
        values.update(target_position=int(descriptor[common_candidate.POSITION]))
    elif action in {NumericSearchAction.DELIVERY_INTRA_MOVE, NumericSearchAction.NODE_MOVE, NumericSearchAction.BLOCK_MOVE}:
        values.update(target_position=int(descriptor[common_candidate.POSITION]),
            source_start=int(descriptor[common_candidate.START]), source_stop=int(descriptor[common_candidate.STOP]))
    elif action in {NumericSearchAction.NODE_EXCHANGE, NumericSearchAction.BLOCK_EXCHANGE, NumericSearchAction.CHAIN_CUT}:
        values.update(source_start=int(descriptor[common_candidate.START]),
            source_stop=int(descriptor[common_candidate.STOP]),
            target_start=int(descriptor[common_candidate.OTHER_START]), target_stop=int(descriptor[common_candidate.OTHER_STOP]))
    else:
        values.update(source_start=int(descriptor[common_candidate.START]), source_stop=int(descriptor[common_candidate.STOP]))
    return NumericCandidateEdit(**values)


def publish_accepted_candidate(state, workspace, result, edit):
    """Materialize and verify locally; commit is the only mutation of live state."""
    workspace.require_current(state.task, state.plan)
    workspace.require_view(result.view)
    if result.program is not state.program or result.quality_program is not state.quality:
        raise NumericValueError("candidate.publish", "candidate rules changed before publication")
    if not result.prepared or not result.admissible or not tuple(result.summary.quality) < _quality(state.evaluation):
        raise NumericValueError("candidate.publish", "admissible strict improvement required")
    virtual_count = sum(int(role) == _GENERATED_VIRTUAL for role in workspace.nodes.role[:workspace.node_count])
    if (result.virtual_sequence != state.virtual_sequence + virtual_count
            or result.split_sequence != state.split_sequence + workspace.group_count):
        raise NumericValueError("candidate.publish", "resource sequences do not extend the current state")
    virtual_sequence = state.virtual_sequence
    for row in range(workspace.node_count):
        if int(workspace.nodes.role[row]) == _GENERATED_VIRTUAL:
            virtual_sequence += 1
            if int(workspace.nodes.accepted_sequence[row]) != virtual_sequence:
                raise NumericValueError("candidate.publish", "virtual resource order changed")
    for group in range(workspace.group_count):
        if int(workspace.split_groups.accepted_sequence[group]) != state.split_sequence + group + 1:
            raise NumericValueError("candidate.publish", "split resource order changed")
    extension = materialize_private_resources(workspace, state.program, state.quality)
    chains = tuple(chain_rows(result.view, i) for i in range(result.view.count))
    candidate = _build_plan(extension.task, state.plan, chains,
        workspace.ids[:workspace.chain_count], workspace.periods[:workspace.chain_count], group_periods=False)
    if not split_target_periods_match(extension.task, chains, candidate.chain_periods):
        raise NumericValueError("candidate.publish", "split target period changed before publication")
    evaluation = materialize_numeric_evaluation(extension.task, extension.program,
        extension.quality, candidate, result.summary)
    state.commit(extension.task, extension.program, extension.quality, candidate, evaluation,
        edit, tuple(map(int, result.affected_rows)), virtual_sequence=result.virtual_sequence,
        split_sequence=result.split_sequence)


def consume_candidate_result(state, budget, workspace, result):
    """Consume one already charged attempt; do not pull or charge a later one."""
    workspace.require_current(state.task, state.plan)
    workspace.require_view(result.view)
    if isinstance(result, NumericDeferredCandidateFailure):
        raise result.error
    if result.program is not state.program or result.quality_program is not state.quality:
        raise NumericValueError("candidate.consume", "candidate rules changed before consumption")
    if result.status == CANCELLED:
        return False
    if result.status != OK:
        raise NumericValueError("candidate.compute", f"numeric candidate status {result.status}; capacity must retry before consumption")
    if not result.prepared:
        return False
    if result.summary is None:
        raise NumericValueError("candidate.consume", "unfinished preparation cannot be consumed")
    if budget.candidate_check_count <= 0:
        raise NumericValueError("candidate.consume", "candidate must consume its original logical check first")
    state.complete_candidate_evaluation_count += 1
    if not result.admissible or not budget.allows_search() or not tuple(result.summary.quality) < _quality(state.evaluation):
        return False
    edit = _common_candidate_edit(state, result.descriptor, budget.candidate_check_count)
    publish_accepted_candidate(state, workspace, result, edit)
    return True


def consume_candidate_attempts(state, budget, attempts):
    """First check belongs to the caller; each subsequent repair keeps one check.

    Pulling happens only after rejection. This preserves the original preparation
    before second-check point while preventing unused suffix errors from firing.
    """
    position = 0
    for workspace, result in attempts:
        if position and not budget.consume_candidate_check():
            return False
        position += 1
        if consume_candidate_result(state, budget, workspace, result):
            return True
    return False


def _underweight_indices(evaluation):
    return [
        index
        for index, result in enumerate(evaluation.chain_results)
        if any(
            metric.kind is NumericMetricKind.UNDERWEIGHT_CHAIN_COUNT and metric.numerator
            for metric in result.metrics
        )
    ]


def _validate_search_inputs(task, program, quality, state, budget):
    if (
        not isinstance(task, NumericTask)
        or not isinstance(program, NumericRuleProgram)
        or not isinstance(quality, NumericQualityProgram)
        or not isinstance(state, NumericSearchState)
        or not isinstance(budget, SolveRuntimeBudget)
        or state.task is not task
        or state.program is not program
        or state.quality is not quality
        or program.task_fingerprint != task.fingerprint
        or quality.task_fingerprint != task.fingerprint
        or quality.rule_program_fingerprint != program.fingerprint
        or state.plan.task_fingerprint != task.fingerprint
        or state.evaluation.task_fingerprint != task.fingerprint
        or state.evaluation.rule_program_fingerprint != program.fingerprint
        or state.evaluation.quality_program_fingerprint != quality.fingerprint
        or state.evaluation.plan_fingerprint != state.plan.fingerprint
    ):
        raise NumericValueError(
            "search", "matching numeric task, programs, state and budget required"
        )


@njit
def _first_chain_metadata(columns, view):
    weights, due = np.zeros(view.count, np.int64), np.zeros(view.count, np.int64)
    status = np.zeros(5, np.int64)
    for chain in range(view.count):
        first = True
        for row in chain_rows(view, chain):
            weights[chain] = _add(weights[chain], columns.weight[row], status)
            owner = columns.source[row]
            if owner >= 0:
                date = max(0, columns.due[owner])
                if first or date < due[chain]:
                    due[chain] = date
                first = False
    return weights, due, status


@njit
def _first_reverse_allowed(columns, rules, rows):
    if rows.size < 2:
        return False, np.zeros(5, np.int64)
    reverse = evaluate_chain_kernel(columns, rules, rows[::-1])
    if reverse.status[0] != OK:
        return False, reverse.status
    original = evaluate_chain_kernel(columns, rules, rows)
    a, b = reverse.scores[0, 0], original.scores[0, 0]
    return a < b or (a == b and reverse.scores[0, 1] <= original.scores[0, 1]), original.status


@njit
def _first_join_direct(columns, rules, source, target, position, source_reverse, target_reverse):
    status = np.zeros(5, np.int64)
    source = source[::-1] if source_reverse else source
    target = target[::-1] if target_reverse else target
    if position and not all_edges_allowed_values(
            edge_node(columns, target[position - 1]), edge_node(columns, source[0]), rules, status):
        return False, status
    if position < target.size and not all_edges_allowed_values(
            edge_node(columns, source[-1]), edge_node(columns, target[position]), rules, status):
        return False, status
    return True, status


@njit
def _first_move_eligible(columns, rules, donor, position, donor_weight, target_weight, minimum, maximum):
    status = np.zeros(5, np.int64)
    row = donor[position]
    if columns.role[row] == _GENERATED_VIRTUAL or donor.size < 2:
        return False, status
    if donor_weight - columns.weight[row] < minimum:
        return False, status
    if _add(target_weight, columns.weight[row], status) > maximum or status[0] != OK:
        return False, status
    if 0 < position < donor.size - 1:
        if not all_edges_allowed_values(
                edge_node(columns, donor[position - 1]), edge_node(columns, donor[position + 1]), rules, status):
            return False, status
    return True, status


def _check_search_status(status):
    if status[0] != OK:
        raise NumericValueError("first_search", f"numeric precheck failed: {status.tolist()}")


def _first_workspace(state):
    plan = state.plan
    view = flat_chain_view(plan.node_rows, plan.chain_offsets, plan.chain_periods, plan.chain_ids)
    columns, rules = task_columns(state.task), rule_tables(state.program.rules)
    weights, due, status = _first_chain_metadata(columns, view)
    _check_search_status(status)
    workspace = NumericCandidateWorkspace.allocate(state.task, plan,
        changed_capacity=max(64, 2 * plan.node_rows.size + 16), chain_capacity=plan.chain_ids.size + 1,
        node_capacity=8, group_capacity=0, event_capacity=8)
    return workspace, view, columns, rules, weights, due


def _first_description(workspace, action, source, target, *, row=-1, position=-1,
                       source_reversed=False, target_reversed=False):
    values = np.full((1, len(DESCRIPTOR_FIELDS)), -1, np.int64)
    values[0, common_candidate.ACTION] = action
    values[0, common_candidate.SOURCE] = source
    values[0, common_candidate.TARGET] = target
    values[0, common_candidate.NODE] = row
    values[0, common_candidate.POSITION] = position
    values[0, common_candidate.REVERSE_SOURCE] = int(source_reversed)
    values[0, common_candidate.REVERSE_TARGET] = int(target_reversed)
    values[0, common_candidate.VARIANT] = 0
    return NumericCandidateDescriptors(workspace.task, workspace.plan, readonly(values, np.int64))


def _prepare_current_description(state, budget, workspace, description, policy, *, split_decision=None):
    """Return private geometry before split's existing logical charge boundary."""
    while True:
        result = common_candidate.prepare_candidate_attempt(workspace, state.program, state.quality,
            description, 0, policy, virtual_sequence=state.virtual_sequence,
            split_sequence=state.split_sequence, split_decision=split_decision,
            allows_continue=budget.allows_search)
        if result.status != CAPACITY:
            return result
        _grow_candidate_workspace(workspace)


def _grow_candidate_workspace(workspace):
    workspace.grow()


def _try_first_description(state, budget, workspace, description, policy):
    while True:
        result = capture_candidate_result(workspace, state.program, state.quality, description, 0,
            policy, virtual_sequence=state.virtual_sequence, split_sequence=state.split_sequence,
            previous_evaluation=state.evaluation, allows_continue=budget.allows_search)
        if isinstance(result, NumericDeferredCandidateFailure) or result.status != CAPACITY:
            return consume_candidate_result(state, budget, workspace, result)
        _grow_candidate_workspace(workspace)


def improve_numeric_whole_chain(
    task,
    program,
    quality,
    state,
    budget,
    *,
    pair_scan_slack_weight,
    maximum_virtual_bridge_nodes=2,
):
    _validate_search_inputs(task, program, quality, state, budget)
    if type(pair_scan_slack_weight) is not int or pair_scan_slack_weight < 0:
        raise NumericValueError("pair_scan_slack_weight", "nonnegative integer weight required")
    if type(maximum_virtual_bridge_nodes) is not int or not 0 <= maximum_virtual_bridge_nodes <= 2:
        raise NumericValueError(
            "maximum_virtual_bridge_nodes", "zero, one or two bridge nodes required"
        )
    weight_rules = program.for_kind(NumericRuleKind.CHAIN_WEIGHT)
    maximum = weight_rules[0].values[1] if weight_rules else None
    pair_maximum = (
        None
        if maximum is None
        else checked_sum((maximum, pair_scan_slack_weight), "pair_scan_maximum")
    )
    policy = common_candidate.CandidateCheckPolicy(
        maximum_virtual_bridge_nodes, -1 if maximum is None else maximum)
    while budget.allows_search():
        task, program, quality = state.task, state.program, state.quality
        plan = state.plan
        workspace, view, columns, rules, weights, _ = _first_workspace(state)
        chain_ids = plan.chain_ids
        underweight = _underweight_indices(state.evaluation)
        mask = np.zeros(view.count, np.bool_)
        mask[underweight] = True
        donor_order = np.concatenate((np.flatnonzero(mask), np.flatnonzero(~mask)))
        reverse_allowed = np.full(view.count, -1, np.int64)
        accepted = False
        for donor_index in donor_order:
            source = chain_rows(view, donor_index)
            for target_index in range(view.count):
                if not budget.allows_search():
                    return state
                if donor_index == target_index:
                    continue
                target = chain_rows(view, target_index)
                total = checked_sum(
                    (int(weights[donor_index]), int(weights[target_index])), "pair_weight"
                )
                if pair_maximum is not None and total > pair_maximum:
                    continue
                for index, rows in ((donor_index, source), (target_index, target)):
                    if reverse_allowed[index] < 0:
                        allowed, status = _first_reverse_allowed(columns, rules, rows)
                        _check_search_status(status)
                        reverse_allowed[index] = int(allowed)
                for source_reversed in range(int(reverse_allowed[donor_index]) + 1):
                    for target_reversed in range(int(reverse_allowed[target_index]) + 1):
                        for action in (common_candidate.APPEND, common_candidate.PREPEND, common_candidate.INSERT):
                            positions = (target.size,) if action == common_candidate.APPEND else (
                                (0,) if action == common_candidate.PREPEND else range(target.size + 1))
                            for position in positions:
                                if not budget.consume_candidate_check():
                                    return state
                                direct, status = _first_join_direct(columns, rules, source, target,
                                    position, bool(source_reversed), bool(target_reversed))
                                _check_search_status(status)
                                if not direct:
                                    state.bridge_required_candidate_count += 1
                                elif maximum is not None and total > maximum:
                                    continue
                                description = _first_description(workspace, action,
                                    chain_ids[donor_index], chain_ids[target_index], position=position,
                                    source_reversed=bool(source_reversed), target_reversed=bool(target_reversed))
                                if _try_first_description(state, budget, workspace, description, policy):
                                    accepted = True
                                    break
                            if accepted:
                                break
                        if accepted:
                            break
                    if accepted:
                        break
                if accepted:
                    break
            if accepted:
                break
        if not accepted:
            return state
    return state


def improve_numeric_real_node_relocation(task, program, quality, state, budget):
    _validate_search_inputs(task, program, quality, state, budget)
    weight_rules = program.for_kind(NumericRuleKind.CHAIN_WEIGHT)
    if not weight_rules:
        return state
    minimum, maximum, _ = weight_rules[0].values
    policy = common_candidate.CandidateCheckPolicy(0)
    while budget.allows_search():
        plan = state.plan
        workspace, view, columns, rules, weights, _ = _first_workspace(state)
        chain_ids = plan.chain_ids
        accepted = False
        for target_index in _underweight_indices(state.evaluation):
            target = chain_rows(view, target_index)
            for donor_index in range(view.count):
                if not budget.allows_search():
                    return state
                if donor_index == target_index or weights[donor_index] <= minimum:
                    continue
                donor = chain_rows(view, donor_index)
                for node_position, row in enumerate(donor):
                    eligible, status = _first_move_eligible(columns, rules, donor, node_position,
                        weights[donor_index], weights[target_index], minimum, maximum)
                    _check_search_status(status)
                    if not eligible:
                        continue
                    for position in range(len(target) + 1):
                        if not budget.consume_candidate_check():
                            return state
                        direct, status = _first_join_direct(columns, rules, donor[node_position:node_position + 1],
                            target, position, False, False)
                        _check_search_status(status)
                        if not direct:
                            continue
                        description = _first_description(workspace, common_candidate.REAL_MOVE,
                            chain_ids[donor_index], chain_ids[target_index], row=row, position=position)
                        if _try_first_description(state, budget, workspace, description, policy):
                            accepted = True
                            break
                    if accepted:
                        break
                if accepted:
                    break
            if accepted:
                break
        if not accepted:
            return state
    return state


def improve_numeric_virtual_weight_fill(task, program, quality, state, budget):
    """Insert one generated unit at each ordered boundary and restart on improvement."""
    _validate_search_inputs(task, program, quality, state, budget)
    while budget.allows_search():
        task, program, quality = state.task, state.program, state.quality
        weight_rules = program.for_kind(NumericRuleKind.CHAIN_WEIGHT)
        if not weight_rules or not task.prototype_ids:
            return state
        maximum = weight_rules[0].values[1]
        plan = state.plan
        workspace, view, _, _, weights, _ = _first_workspace(state)
        chain_ids = plan.chain_ids
        policy = common_candidate.CandidateCheckPolicy(0, maximum, (NumericRuleKind.VIRTUAL_RATIO,))
        accepted = False
        for target_index in _underweight_indices(state.evaluation):
            target = chain_rows(view, target_index)
            if weights[target_index] >= maximum:
                continue
            for position in range(len(target) + 1):
                for prototype in range(len(task.prototype_ids)):
                    if not budget.consume_candidate_check():
                        return state
                    description = _first_description(workspace, common_candidate.FILL,
                        chain_ids[target_index], chain_ids[target_index], row=prototype, position=position)
                    if _try_first_description(state, budget, workspace, description, policy):
                        accepted = True
                        break
                if accepted:
                    break
            if accepted:
                break
        if not accepted:
            return state
    return state


def improve_numeric_controlled_split(
    state,
    budget,
    *,
    pair_scan_slack_weight,
    maximum_virtual_bridge_nodes=2,
):
    """Accept complete authorized partitions, then replay local search exactly once."""
    _validate_search_inputs(state.task, state.program, state.quality, state, budget)
    if budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE:
        budget.stop_reason = None
    if not budget.allows_search():
        return state
    starting_split_sequence = state.split_sequence
    while budget.allows_search():
        task, program = state.task, state.program
        workspace, view, _, _, _, _ = _first_workspace(state)
        chain_ids, periods = state.plan.chain_ids, state.plan.chain_periods
        policy = common_candidate.CandidateCheckPolicy(maximum_virtual_bridge_nodes,
            reject_prohibited_kinds=(NumericRuleKind.VIRTUAL_RATIO,))
        accepted = False
        for donor_index in range(view.count):
            donor = chain_rows(view, donor_index)
            for parent_row in donor:
                decision = evaluate_numeric_split(
                    task,
                    program,
                    int(parent_row),
                    int(periods[donor_index]),
                    state.split_sequence,
                )
                if not decision.eligible:
                    continue
                new_chain_id = checked_sum((int(chain_ids.max()), 1), "candidate.chain_id")
                description = _first_description(workspace, common_candidate.SPLIT,
                    chain_ids[donor_index], new_chain_id, row=parent_row)
                prepared = _prepare_current_description(state, budget, workspace, description, policy,
                    split_decision=decision)
                if prepared.status == CANCELLED:
                    return state
                if prepared.status != OK:
                    raise NumericValueError("split.prepare", f"numeric preparation failed: {prepared.status}")
                if not prepared.prepared:
                    continue
                if not budget.consume_candidate_check():
                    return state
                result = common_candidate.compute_candidate_attempt(workspace, state.program, state.quality,
                    description, 0, policy, preparation=prepared, previous_evaluation=state.evaluation,
                    allows_continue=budget.allows_search)
                if consume_candidate_result(state, budget, workspace, result):
                    accepted = True
                    break
            if accepted or budget.must_stop:
                break
        if not accepted:
            break
    if budget.allows_search() and state.split_sequence > starting_split_sequence:
        state.replay_count += 1
        _run_numeric_local_search(
            state,
            budget,
            pair_scan_slack_weight=pair_scan_slack_weight,
            maximum_virtual_bridge_nodes=maximum_virtual_bridge_nodes,
        )
    elif budget.allows_search():
        budget.stop_reason = SearchStopReason.LOCAL_SEARCH_COMPLETE
    return state


def improve_numeric_chain_order(task, program, quality, state, budget):
    _validate_search_inputs(task, program, quality, state, budget)
    if not (
        program.for_kind(NumericRuleKind.DELIVERY)
        or program.for_kind(NumericRuleKind.INTER_CHAIN_WIDTH)
    ):
        return state
    while budget.allows_search():
        plan = state.plan
        workspace, view, _, _, _, due = _first_workspace(state)
        chain_ids, periods = plan.chain_ids, plan.chain_periods
        policy = common_candidate.CandidateCheckPolicy(same_period_order=True, record_order_rows=False)
        accepted = False
        for source_index in np.argsort(due, kind="stable"):
            positions = np.flatnonzero((periods == periods[source_index]) & (np.arange(view.count) != source_index))
            for position in positions:
                if not budget.consume_candidate_check():
                    return state
                order = np.arange(view.count, dtype=np.int64)
                if source_index < position:
                    order[source_index:position] = order[source_index + 1:position + 1]
                else:
                    order[position + 1:source_index + 1] = order[position:source_index]
                order[position] = source_index
                if not preview_numeric_chain_order_quality(
                    task, program, quality, plan, state.evaluation, order
                ) < _quality(state.evaluation):
                    continue
                description = _first_description(workspace, common_candidate.ORDER,
                    chain_ids[source_index], chain_ids[position], position=position)
                if _try_first_description(state, budget, workspace, description, policy):
                    accepted = True
                    break
            if accepted:
                break
        if not accepted:
            return state
    return state


def _run_numeric_local_search(
    state,
    budget,
    *,
    pair_scan_slack_weight,
    maximum_virtual_bridge_nodes,
):
    if budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE:
        budget.stop_reason = None
    improve_numeric_whole_chain(
        state.task,
        state.program,
        state.quality,
        state,
        budget,
        pair_scan_slack_weight=pair_scan_slack_weight,
        maximum_virtual_bridge_nodes=maximum_virtual_bridge_nodes,
    )
    if budget.allows_search():
        improve_numeric_real_node_relocation(
            state.task, state.program, state.quality, state, budget
        )
    if budget.allows_search():
        improve_numeric_virtual_weight_fill(state.task, state.program, state.quality, state, budget)
    if budget.allows_search():
        improve_numeric_chain_order(state.task, state.program, state.quality, state, budget)
    if budget.allows_search():
        budget.stop_reason = SearchStopReason.LOCAL_SEARCH_COMPLETE
    return state


def run_numeric_search_with_split_replay(
    task,
    program,
    quality,
    initial,
    budget,
    *,
    pair_scan_slack_weight,
    maximum_virtual_bridge_nodes=2,
):
    started = perf_counter()
    state = NumericSearchState.start(task, program, quality, initial)
    _validate_search_inputs(task, program, quality, state, budget)
    _run_numeric_local_search(
        state,
        budget,
        pair_scan_slack_weight=pair_scan_slack_weight,
        maximum_virtual_bridge_nodes=maximum_virtual_bridge_nodes,
    )
    if budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE:
        improve_numeric_controlled_split(
            state,
            budget,
            pair_scan_slack_weight=pair_scan_slack_weight,
            maximum_virtual_bridge_nodes=maximum_virtual_bridge_nodes,
        )
    return state, NumericSearchCheckpoint.capture(state.task, state, budget, started)


def run_numeric_first_search_prefix(
    task,
    program,
    quality,
    initial,
    budget,
    *,
    pair_scan_slack_weight,
):
    started = perf_counter()
    state = NumericSearchState.start(task, program, quality, initial)
    _validate_search_inputs(task, program, quality, state, budget)
    improve_numeric_whole_chain(
        task,
        program,
        quality,
        state,
        budget,
        pair_scan_slack_weight=pair_scan_slack_weight,
    )
    if budget.allows_search():
        improve_numeric_real_node_relocation(task, program, quality, state, budget)
    return state, NumericSearchCheckpoint.capture(task, state, budget, started)
