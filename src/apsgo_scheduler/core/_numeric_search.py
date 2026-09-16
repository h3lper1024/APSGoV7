"""Numeric candidate edits and the static parts of first local search."""

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from time import perf_counter

from ._numeric_construction import NumericInitialSolution
from ._numeric_evaluation import (
    NumericPlanEvaluation,
    NumericQualityProgram,
    summarize_numeric_candidate,
    materialize_numeric_evaluation,
    preview_numeric_chain_order_quality,
)
from ._numeric_resources import (
    NumericResourceExtension,
    choose_split_separator,
    choose_virtual_bridge,
    extend_resource_workspace,
    split_group,
    split_piece_node,
    virtual_node,
)
from ._numeric_rules import (
    NumericMetricKind,
    NumericRuleKind,
    NumericRuleProgram,
    numeric_rows_prohibited_profile,
    evaluate_numeric_split,
    numeric_edge_allowed,
)
from ._numeric_state import NumericPlan, NumericTask, split_target_periods_match
from ._numeric_units import (
    NumericValueError,
    allocate_piece_milliseconds,
    checked_sum,
    int64,
)
from .budget import SolveRuntimeBudget
from .contracts import SearchStopReason, fingerprint
from .model import MaterialRole, VirtualPurpose

_GENERATED_VIRTUAL = tuple(MaterialRole).index(MaterialRole.GENERATED_VIRTUAL)


class NumericSearchAction(str, Enum):
    WHOLE_CHAIN_APPEND = "whole_chain_append"
    WHOLE_CHAIN_PREPEND = "whole_chain_prepend"
    WHOLE_CHAIN_INSERTION = "whole_chain_insertion"
    REAL_NODE_RELOCATION = "real_node_relocation"
    CHAIN_ORDER_RELOCATION = "chain_order_relocation"
    VIRTUAL_WEIGHT_FILL = "virtual_weight_fill"
    CONTROLLED_ORDER_SPLIT = "controlled_order_split"
    DELIVERY_INTRA_MOVE = "delivery_intra_move"
    NODE_MOVE = "width_node_move"
    NODE_EXCHANGE = "width_node_exchange"
    BLOCK_MOVE = "width_block_move"
    BLOCK_EXCHANGE = "width_block_exchange"
    CHAIN_CUT = "width_chain_cut"
    BRIDGE_RECLAMATION = "width_bridge_reclamation"


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


def _chain_rows(plan, chain_index):
    start = int(plan.chain_offsets[chain_index])
    stop = int(plan.chain_offsets[chain_index + 1])
    return tuple(int(value) for value in plan.node_rows[start:stop])


def _chain_index(plan, chain_id):
    for index, value in enumerate(plan.chain_ids):
        if int(value) == chain_id:
            return index
    raise NumericValueError("candidate", "stable chain identity is absent from current plan")


def _layout(plan):
    return (
        [_chain_rows(plan, index) for index in range(plan.chain_ids.size)],
        [int(value) for value in plan.chain_ids],
        [int(value) for value in plan.chain_periods],
    )


def _assigned_period(task, rows):
    periods = [
        int(task.nodes.source_period[row])
        for row in rows
        if int(task.nodes.role[row]) != _GENERATED_VIRTUAL
    ]
    if not periods:
        raise NumericValueError("candidate", "chain must retain real material")
    return min(periods)


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


def _joined_rows(edit, source, target):
    source = tuple(reversed(source)) if edit.source_reversed else source
    target = tuple(reversed(target)) if edit.target_reversed else target
    if edit.action is NumericSearchAction.WHOLE_CHAIN_APPEND:
        return target + source
    if edit.action is NumericSearchAction.WHOLE_CHAIN_PREPEND:
        return source + target
    if edit.action is NumericSearchAction.WHOLE_CHAIN_INSERTION:
        if edit.target_position > len(target):
            raise NumericValueError("candidate", "whole-chain insertion is outside target")
        return target[: edit.target_position] + source + target[edit.target_position :]
    raise NumericValueError("candidate", "whole-chain action required")


def apply_numeric_candidate(task, plan, edit):
    if (
        not isinstance(task, NumericTask)
        or not isinstance(plan, NumericPlan)
        or not isinstance(edit, NumericCandidateEdit)
        or plan.task_fingerprint != task.fingerprint
        or edit.task_fingerprint != task.fingerprint
        or edit.plan_fingerprint != plan.fingerprint
        or edit.generation != plan.generation
    ):
        raise NumericValueError("candidate", "candidate identity or generation is stale")
    source_index = _chain_index(plan, edit.source_chain_id)
    target_index = _chain_index(plan, edit.target_chain_id)
    chains, chain_ids, periods = _layout(plan)
    source, target = chains[source_index], chains[target_index]
    if edit.action in {
        NumericSearchAction.WHOLE_CHAIN_APPEND,
        NumericSearchAction.WHOLE_CHAIN_PREPEND,
        NumericSearchAction.WHOLE_CHAIN_INSERTION,
    }:
        merged = _joined_rows(edit, source, target)
        retained = [
            index for index in range(len(chains)) if index not in (source_index, target_index)
        ]
        return _build_plan(
            task,
            plan,
            [*(chains[index] for index in retained), merged],
            [*(chain_ids[index] for index in retained), edit.target_chain_id],
            [*(periods[index] for index in retained), _assigned_period(task, merged)],
        )
    if edit.action is NumericSearchAction.REAL_NODE_RELOCATION:
        if edit.node_row not in source or edit.target_position > len(target):
            raise NumericValueError("candidate", "relocated node or insertion position is stale")
        if int(task.nodes.role[edit.node_row]) == _GENERATED_VIRTUAL:
            raise NumericValueError(
                "candidate", "real-node relocation cannot move generated material"
            )
        donor = tuple(row for row in source if row != edit.node_row)
        if not donor:
            raise NumericValueError("candidate", "real-node relocation cannot empty a chain")
        receiver = (
            target[: edit.target_position] + (edit.node_row,) + target[edit.target_position :]
        )
        chains[source_index], chains[target_index] = donor, receiver
        periods[source_index] = _assigned_period(task, donor)
        periods[target_index] = _assigned_period(task, receiver)
        return _build_plan(task, plan, chains, chain_ids, periods)
    if edit.action is NumericSearchAction.CHAIN_ORDER_RELOCATION:
        if (
            edit.target_position >= len(chains)
            or chain_ids[edit.target_position] != edit.target_chain_id
        ):
            raise NumericValueError("candidate", "chain-order target anchor is stale")
        if periods[source_index] != periods[target_index]:
            raise NumericValueError("candidate", "chain-order relocation changes assigned period")
        moved_chain = chains.pop(source_index)
        moved_id = chain_ids.pop(source_index)
        moved_period = periods.pop(source_index)
        chains.insert(edit.target_position, moved_chain)
        chain_ids.insert(edit.target_position, moved_id)
        periods.insert(edit.target_position, moved_period)
        return _build_plan(task, plan, chains, chain_ids, periods, group_periods=False)
    raise NumericValueError("candidate", "unsupported numeric candidate action")


def _quality(evaluation):
    return tuple(int(value) for value in evaluation.quality_key)


def _try_prepared_candidate(
    state,
    budget,
    edit,
    candidate_task,
    candidate_program,
    candidate_quality,
    candidate,
    affected_rows=(),
    *,
    virtual_sequence=None,
    split_sequence=None,
    reject_prohibited_kinds=(),
):
    if edit.sequence != budget.candidate_check_count:
        raise NumericValueError("candidate", "candidate sequence does not match consumed budget")
    chains, _, periods = _layout(candidate)
    if not split_target_periods_match(candidate_task, chains, periods):
        return False
    summary = summarize_numeric_candidate(
        candidate_task,
        candidate_program,
        candidate_quality,
        candidate,
        state.task,
        state.program,
        state.quality,
        state.plan,
        state.evaluation,
    )
    state.complete_candidate_evaluation_count += 1
    if any(
        summary.hits[:, rule.index].any()
        for rule in candidate_program.rules if rule.kind in reject_prohibited_kinds
    ):
        return False
    if not budget.allows_search() or not tuple(summary.quality) < _quality(state.evaluation):
        return False
    evaluation = materialize_numeric_evaluation(
        candidate_task, candidate_program, candidate_quality, candidate, summary
    )
    state.commit(
        candidate_task,
        candidate_program,
        candidate_quality,
        candidate,
        evaluation,
        edit,
        affected_rows,
        virtual_sequence=virtual_sequence,
        split_sequence=split_sequence,
    )
    return True


def _try_candidate(task, program, quality, state, budget, edit, affected_rows=()):
    candidate = apply_numeric_candidate(task, state.plan, edit)
    return _try_prepared_candidate(
        state, budget, edit, task, program, quality, candidate, affected_rows
    )


def _prohibited_profile(task, program, rows):
    return numeric_rows_prohibited_profile(task, program, rows)


def _variants(task, program, rows):
    variants = ((False, rows),)
    reversed_rows = tuple(reversed(rows))
    if reversed_rows != rows and _prohibited_profile(
        task, program, reversed_rows
    ) <= _prohibited_profile(task, program, rows):
        variants += ((True, reversed_rows),)
    return variants


def _edge_allowed(task, program, left, right):
    return numeric_edge_allowed(task, program, left, right)


def _join_is_direct(task, program, edit, source, target):
    source = tuple(reversed(source)) if edit.source_reversed else source
    target = tuple(reversed(target)) if edit.target_reversed else target
    if edit.action is NumericSearchAction.WHOLE_CHAIN_APPEND:
        boundaries = ((target[-1], source[0]),)
    elif edit.action is NumericSearchAction.WHOLE_CHAIN_PREPEND:
        boundaries = ((source[-1], target[0]),)
    else:
        position = edit.target_position
        boundaries = []
        if position:
            boundaries.append((target[position - 1], source[0]))
        if position < len(target):
            boundaries.append((source[-1], target[position]))
    return all(_edge_allowed(task, program, left, right) for left, right in boundaries)


def _join_with_bridge(workspace, left, right, max_nodes, sequence):
    if not left or not right:
        return workspace, left + right, sequence
    if _edge_allowed(workspace.task, workspace.program, left[-1], right[0]):
        return workspace, left + right, sequence
    selected = choose_virtual_bridge(
        workspace.task,
        workspace.program,
        workspace.quality,
        left[-1],
        right[0],
        max_nodes=max_nodes,
        first_sequence=sequence + 1,
    )
    if selected is None:
        return None
    return selected, left + selected.rows + right, sequence + len(selected.rows)


def _resource_whole_chain_candidate(state, edit, source, target, max_nodes):
    source = tuple(reversed(source)) if edit.source_reversed else source
    target = tuple(reversed(target)) if edit.target_reversed else target
    workspace = NumericResourceExtension(state.task, state.program, state.quality, ())
    sequence = state.virtual_sequence
    if edit.action is NumericSearchAction.WHOLE_CHAIN_APPEND:
        joined = _join_with_bridge(
            workspace, target, source, max_nodes, sequence
        )
    elif edit.action is NumericSearchAction.WHOLE_CHAIN_PREPEND:
        joined = _join_with_bridge(
            workspace, source, target, max_nodes, sequence
        )
    else:
        position = edit.target_position
        first = _join_with_bridge(
            workspace,
            target[:position],
            source,
            max_nodes,
            sequence,
        )
        if first is None:
            return None
        workspace, rows, sequence = first
        joined = _join_with_bridge(
            workspace,
            rows,
            target[position:],
            max_nodes,
            sequence,
        )
    if joined is None:
        return None
    workspace, merged, sequence = joined
    chains, chain_ids, periods = _layout(state.plan)
    source_index = _chain_index(state.plan, edit.source_chain_id)
    target_index = _chain_index(state.plan, edit.target_chain_id)
    retained = [index for index in range(len(chains)) if index not in (source_index, target_index)]
    candidate = _build_plan(
        workspace.task,
        state.plan,
        [*(chains[index] for index in retained), merged],
        [*(chain_ids[index] for index in retained), edit.target_chain_id],
        [*(periods[index] for index in retained), _assigned_period(workspace.task, merged)],
    )
    return workspace, candidate, sequence


def _chain_weight(task, rows):
    return checked_sum((int(task.nodes.weight[row]) for row in rows), "candidate_chain_weight")


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
    while budget.allows_search():
        task, program, quality = state.task, state.program, state.quality
        plan = state.plan
        chains, chain_ids, _ = _layout(plan)
        underweight = _underweight_indices(state.evaluation)
        donor_order = [
            *underweight,
            *(index for index in range(len(chains)) if index not in underweight),
        ]
        accepted = False
        for donor_index in donor_order:
            for target_index, target in enumerate(chains):
                if not budget.allows_search():
                    return state
                if donor_index == target_index:
                    continue
                source = chains[donor_index]
                total = checked_sum(
                    (_chain_weight(task, source), _chain_weight(task, target)), "pair_weight"
                )
                if pair_maximum is not None and total > pair_maximum:
                    continue
                source_variants = _variants(task, program, source)
                target_variants = _variants(task, program, target)
                for source_reversed, _ in source_variants:
                    for target_reversed, target_rows in target_variants:
                        descriptions = (
                            (NumericSearchAction.WHOLE_CHAIN_APPEND, len(target_rows)),
                            (NumericSearchAction.WHOLE_CHAIN_PREPEND, 0),
                            *(
                                (NumericSearchAction.WHOLE_CHAIN_INSERTION, position)
                                for position in range(len(target_rows) + 1)
                            ),
                        )
                        for action, position in descriptions:
                            if not budget.consume_candidate_check():
                                return state
                            edit = NumericCandidateEdit(
                                task.fingerprint,
                                plan.fingerprint,
                                plan.generation,
                                budget.candidate_check_count,
                                action,
                                chain_ids[donor_index],
                                chain_ids[target_index],
                                -1,
                                position,
                                source_reversed,
                                target_reversed,
                            )
                            if not _join_is_direct(task, program, edit, source, target):
                                state.bridge_required_candidate_count += 1
                                prepared = _resource_whole_chain_candidate(
                                    state,
                                    edit,
                                    source,
                                    target,
                                    maximum_virtual_bridge_nodes,
                                )
                                if prepared is None:
                                    continue
                                workspace, candidate, sequence = prepared
                                if maximum is not None:
                                    merged_index = _chain_index(candidate, edit.target_chain_id)
                                    if (
                                        _chain_weight(
                                            workspace.task, _chain_rows(candidate, merged_index)
                                        )
                                        > maximum
                                    ):
                                        continue
                                if _try_prepared_candidate(
                                    state,
                                    budget,
                                    edit,
                                    workspace.task,
                                    workspace.program,
                                    workspace.quality,
                                    candidate,
                                    tuple(
                                        range(
                                            state.task.nodes.weight.size,
                                            workspace.task.nodes.weight.size,
                                        )
                                    ),
                                    virtual_sequence=sequence,
                                ):
                                    accepted = True
                                    break
                                continue
                            if maximum is not None and total > maximum:
                                continue
                            if _try_candidate(task, program, quality, state, budget, edit):
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
        if not accepted:
            return state
    return state


def improve_numeric_real_node_relocation(task, program, quality, state, budget):
    _validate_search_inputs(task, program, quality, state, budget)
    weight_rules = program.for_kind(NumericRuleKind.CHAIN_WEIGHT)
    if not weight_rules:
        return state
    minimum, maximum, _ = weight_rules[0].values
    while budget.allows_search():
        plan = state.plan
        chains, chain_ids, _ = _layout(plan)
        accepted = False
        for target_index in _underweight_indices(state.evaluation):
            target = chains[target_index]
            target_weight = _chain_weight(task, target)
            for donor_index, donor in enumerate(chains):
                if not budget.allows_search():
                    return state
                if donor_index == target_index or _chain_weight(task, donor) <= minimum:
                    continue
                for node_position, row in enumerate(donor):
                    if int(task.nodes.role[row]) == _GENERATED_VIRTUAL:
                        continue
                    donor_rows = donor[:node_position] + donor[node_position + 1 :]
                    if (
                        not donor_rows
                        or _chain_weight(task, donor_rows) < minimum
                        or checked_sum(
                            (target_weight, int(task.nodes.weight[row])), "target_weight"
                        )
                        > maximum
                    ):
                        continue
                    if 0 < node_position < len(donor) - 1 and not _edge_allowed(
                        task, program, donor[node_position - 1], donor[node_position + 1]
                    ):
                        continue
                    for position in range(len(target) + 1):
                        if not budget.consume_candidate_check():
                            return state
                        if position and not _edge_allowed(task, program, target[position - 1], row):
                            continue
                        if position < len(target) and not _edge_allowed(
                            task, program, row, target[position]
                        ):
                            continue
                        edit = NumericCandidateEdit(
                            task.fingerprint,
                            plan.fingerprint,
                            plan.generation,
                            budget.candidate_check_count,
                            NumericSearchAction.REAL_NODE_RELOCATION,
                            chain_ids[donor_index],
                            chain_ids[target_index],
                            row,
                            position,
                        )
                        if _try_candidate(task, program, quality, state, budget, edit, (row,)):
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
        chains, chain_ids, periods = _layout(plan)
        accepted = False
        for target_index in _underweight_indices(state.evaluation):
            target = chains[target_index]
            target_weight = _chain_weight(task, target)
            if target_weight >= maximum:
                continue
            for position in range(len(target) + 1):
                left = target[position - 1] if position else None
                right = target[position] if position < len(target) else None
                anchor_left = left if left is not None else right
                anchor_right = right if right is not None else left
                if anchor_left is None or anchor_right is None:
                    continue
                for prototype in range(len(task.prototype_ids)):
                    if not budget.consume_candidate_check():
                        return state
                    sequence = state.virtual_sequence + 1
                    node = virtual_node(
                        task,
                        prototype,
                        anchor_left,
                        anchor_right,
                        purpose=VirtualPurpose.WEIGHT_FILL,
                        sequence=sequence,
                    )
                    workspace = extend_resource_workspace(task, program, quality, (node,))
                    row = workspace.rows[0]
                    if left is not None and not _edge_allowed(
                        workspace.task, workspace.program, left, row
                    ):
                        continue
                    if right is not None and not _edge_allowed(
                        workspace.task, workspace.program, row, right
                    ):
                        continue
                    if (
                        checked_sum(
                            (target_weight, int(workspace.task.nodes.weight[row])),
                            "target_weight",
                        )
                        > maximum
                    ):
                        continue
                    candidate_chains = list(chains)
                    candidate_chains[target_index] = target[:position] + (row,) + target[position:]
                    candidate = _build_plan(
                        workspace.task,
                        plan,
                        candidate_chains,
                        chain_ids,
                        periods,
                    )
                    edit = NumericCandidateEdit(
                        task.fingerprint,
                        plan.fingerprint,
                        plan.generation,
                        budget.candidate_check_count,
                        NumericSearchAction.VIRTUAL_WEIGHT_FILL,
                        chain_ids[target_index],
                        chain_ids[target_index],
                        target_position=position,
                    )
                    if _try_prepared_candidate(
                        state,
                        budget,
                        edit,
                        workspace.task,
                        workspace.program,
                        workspace.quality,
                        candidate,
                        (row,),
                        virtual_sequence=sequence,
                        reject_prohibited_kinds=(NumericRuleKind.VIRTUAL_RATIO,),
                    ):
                        accepted = True
                        break
                if accepted:
                    break
            if accepted:
                break
        if not accepted:
            return state
    return state


def _split_piece_weights(parent_weight, decision):
    count = (parent_weight + decision.maximum_piece_weight - 1) // decision.maximum_piece_weight
    if count < 2 or count - 1 > decision.maximum_separator_node_count:
        return ()
    weights = (decision.maximum_piece_weight,) * (count - 1) + (
        parent_weight - decision.maximum_piece_weight * (count - 1),
    )
    return (
        weights
        if all(
            decision.minimum_piece_weight <= value <= decision.maximum_piece_weight
            for value in weights
        )
        else ()
    )


def _ordinary_bridge(task, row):
    return (
        int(task.nodes.role[row]) == _GENERATED_VIRTUAL
        and int(task.nodes.purpose[row]) == tuple(VirtualPurpose).index(VirtualPurpose.EDGE_BRIDGE)
        and int(task.nodes.split_group[row]) < 0
    )


def _split_donor_rows(task, donor, parent_position):
    left, right = parent_position, parent_position + 1
    while left and _ordinary_bridge(task, donor[left - 1]):
        left -= 1
    while right < len(donor) and _ordinary_bridge(task, donor[right]):
        right += 1
    prefix, suffix = donor[:left], donor[right:]
    if (
        prefix
        and int(task.nodes.role[prefix[-1]]) == _GENERATED_VIRTUAL
        or suffix
        and int(task.nodes.role[suffix[0]]) == _GENERATED_VIRTUAL
    ):
        return None
    return prefix, suffix, donor[left:right]


def _prepare_numeric_split(state, parent_row, donor_index, decision, maximum_bridge_nodes):
    task, program, quality = state.task, state.program, state.quality
    chains, chain_ids, periods = _layout(state.plan)
    donor = chains[donor_index]
    parent_position = donor.index(parent_row)
    sides = _split_donor_rows(task, donor, parent_position)
    if sides is None:
        return None
    prefix, suffix, removed = sides
    workspace = NumericResourceExtension(task, program, quality, ())
    virtual_sequence = state.virtual_sequence
    if prefix and suffix:
        repaired = _join_with_bridge(
            workspace,
            prefix,
            suffix,
            maximum_bridge_nodes,
            virtual_sequence,
        )
        if repaired is None:
            return None
        workspace, remaining, virtual_sequence = repaired
    else:
        remaining = prefix + suffix
    added_rows = list(workspace.rows)

    weights = _split_piece_weights(int(task.nodes.weight[parent_row]), decision)
    if not weights:
        return None
    durations = allocate_piece_milliseconds(
        int(task.nodes.weight[parent_row]),
        int(task.nodes.duration_ms[parent_row]),
        weights,
        "controlled_split.duration",
    )
    split_sequence = state.split_sequence + 1
    group_index = task.split_groups.parent_row.size
    group = split_group(task, parent_row, decision, periods[donor_index], split_sequence)
    pieces = tuple(
        split_piece_node(
            task,
            parent_row,
            group_index=group_index,
            piece_index=index,
            piece_count=len(weights),
            weight=weight,
            duration_ms=duration,
            accepted_sequence=split_sequence,
        )
        for index, (weight, duration) in enumerate(zip(weights, durations), start=1)
    )
    workspace = extend_resource_workspace(
        workspace.task,
        workspace.program,
        workspace.quality,
        pieces,
        split_group=group,
    )
    piece_rows = workspace.rows
    added_rows.extend(piece_rows)
    returned = [piece_rows[0]]
    separator_weight = 0
    for left, right in zip(piece_rows, piece_rows[1:]):
        selected = choose_split_separator(
            workspace.task,
            workspace.program,
            workspace.quality,
            left,
            right,
            sequence=virtual_sequence + 1,
            group_index=group_index,
        )
        if selected is None:
            return None
        workspace = selected
        separator = selected.rows[0]
        added_rows.append(separator)
        separator_weight = checked_sum(
            (separator_weight, int(workspace.task.nodes.weight[separator])),
            "controlled_split.separator_weight",
        )
        if separator_weight > decision.maximum_separator_weight:
            return None
        virtual_sequence += 1
        returned.extend((separator, right))

    candidate_chains = []
    candidate_ids = []
    candidate_periods = []
    for index, chain in enumerate(chains):
        if index != donor_index:
            candidate_chains.append(chain)
            candidate_ids.append(chain_ids[index])
            candidate_periods.append(periods[index])
        elif remaining:
            candidate_chains.append(remaining)
            candidate_ids.append(chain_ids[index])
            candidate_periods.append(_assigned_period(workspace.task, remaining))
    new_chain_id = max(chain_ids, default=-1) + 1
    candidate_chains.append(tuple(returned))
    candidate_ids.append(new_chain_id)
    candidate_periods.append(decision.target_period)
    candidate = _build_plan(
        workspace.task,
        state.plan,
        candidate_chains,
        candidate_ids,
        candidate_periods,
    )
    return (
        workspace,
        candidate,
        new_chain_id,
        virtual_sequence,
        split_sequence,
        tuple((*removed, *added_rows)),
    )


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
        chains, chain_ids, periods = _layout(state.plan)
        accepted = False
        for donor_index, donor in enumerate(chains):
            for parent_row in donor:
                decision = evaluate_numeric_split(
                    task,
                    program,
                    parent_row,
                    periods[donor_index],
                    state.split_sequence,
                )
                if not decision.eligible:
                    continue
                prepared = _prepare_numeric_split(
                    state,
                    parent_row,
                    donor_index,
                    decision,
                    maximum_virtual_bridge_nodes,
                )
                if prepared is None:
                    continue
                (
                    workspace,
                    candidate,
                    new_chain_id,
                    virtual_sequence,
                    split_sequence,
                    affected_rows,
                ) = prepared
                if not budget.consume_candidate_check():
                    return state
                edit = NumericCandidateEdit(
                    task.fingerprint,
                    state.plan.fingerprint,
                    state.plan.generation,
                    budget.candidate_check_count,
                    NumericSearchAction.CONTROLLED_ORDER_SPLIT,
                    chain_ids[donor_index],
                    new_chain_id,
                    node_row=parent_row,
                )
                if _try_prepared_candidate(
                    state,
                    budget,
                    edit,
                    workspace.task,
                    workspace.program,
                    workspace.quality,
                    candidate,
                    affected_rows,
                    virtual_sequence=virtual_sequence,
                    split_sequence=split_sequence,
                    reject_prohibited_kinds=(NumericRuleKind.VIRTUAL_RATIO,),
                ):
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


def _delivery_chain_indices(task, plan):
    def due(index):
        rows = _chain_rows(plan, index)
        owners = (int(task.nodes.source[row]) for row in rows if int(task.nodes.source[row]) >= 0)
        return min(max(0, int(task.originals.due_ms[source])) for source in owners)

    return tuple(sorted(range(plan.chain_ids.size), key=due))


def improve_numeric_chain_order(task, program, quality, state, budget):
    _validate_search_inputs(task, program, quality, state, budget)
    if not (
        program.for_kind(NumericRuleKind.DELIVERY)
        or program.for_kind(NumericRuleKind.INTER_CHAIN_WIDTH)
    ):
        return state
    while budget.allows_search():
        plan = state.plan
        _, chain_ids, periods = _layout(plan)
        accepted = False
        for source_index in _delivery_chain_indices(task, plan):
            positions = tuple(
                index
                for index, period in enumerate(periods)
                if index != source_index and period == periods[source_index]
            )
            for position in positions:
                if not budget.consume_candidate_check():
                    return state
                order = list(range(len(chain_ids)))
                moved = order.pop(source_index)
                order.insert(position, moved)
                if not preview_numeric_chain_order_quality(
                    task, quality, plan, state.evaluation, order
                ) < _quality(state.evaluation):
                    continue
                edit = NumericCandidateEdit(
                    task.fingerprint,
                    plan.fingerprint,
                    plan.generation,
                    budget.candidate_check_count,
                    NumericSearchAction.CHAIN_ORDER_RELOCATION,
                    chain_ids[source_index],
                    chain_ids[position],
                    -1,
                    position,
                )
                if _try_candidate(task, program, quality, state, budget, edit):
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
