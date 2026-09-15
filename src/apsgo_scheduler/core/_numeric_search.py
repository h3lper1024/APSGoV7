"""Numeric candidate edits and the static parts of first local search."""

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from time import perf_counter

from ._numeric_construction import NumericInitialSolution
from ._numeric_evaluation import (
    NumericPlanEvaluation,
    NumericQualityProgram,
    evaluate_numeric_plan,
)
from ._numeric_rules import (
    NumericMetricKind,
    NumericRuleKind,
    NumericRuleProgram,
    evaluate_numeric_edge,
    evaluate_numeric_rows,
)
from ._numeric_state import NumericPlan, NumericTask
from ._numeric_units import NumericValueError, checked_sum, int64
from .budget import SolveRuntimeBudget
from .contracts import SearchStopReason, fingerprint
from .model import MaterialRole

_GENERATED_VIRTUAL = tuple(MaterialRole).index(MaterialRole.GENERATED_VIRTUAL)


class NumericSearchAction(str, Enum):
    WHOLE_CHAIN_APPEND = "whole_chain_append"
    WHOLE_CHAIN_PREPEND = "whole_chain_prepend"
    WHOLE_CHAIN_INSERTION = "whole_chain_insertion"
    REAL_NODE_RELOCATION = "real_node_relocation"
    CHAIN_ORDER_RELOCATION = "chain_order_relocation"


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
        ):
            int64(getattr(self, name), name)
        if self.generation < 0 or self.sequence <= 0:
            raise NumericValueError("candidate", "nonnegative generation and positive sequence required")
        if not isinstance(self.action, NumericSearchAction):
            raise NumericValueError("candidate", "known numeric search action required")
        if self.source_chain_id == self.target_chain_id:
            raise NumericValueError("candidate", "source and target chains must differ")
        if type(self.source_reversed) is not bool or type(self.target_reversed) is not bool:
            raise NumericValueError("candidate", "reversal flags must be boolean")
        if self.action is NumericSearchAction.REAL_NODE_RELOCATION:
            if self.node_row < 0 or self.target_position < 0 or self.source_reversed or self.target_reversed:
                raise NumericValueError("candidate", "invalid real-node relocation description")
        elif self.action is NumericSearchAction.CHAIN_ORDER_RELOCATION:
            if self.node_row != -1 or self.target_position < 0 or self.source_reversed or self.target_reversed:
                raise NumericValueError("candidate", "invalid chain-order description")
        elif self.node_row != -1 or self.target_position < 0:
            raise NumericValueError("candidate", "invalid whole-chain description")


@dataclass(frozen=True, slots=True)
class NumericAcceptedMove:
    sequence: int
    action: NumericSearchAction
    affected_chain_ids: tuple[int, ...]
    affected_rows: tuple[int, ...]
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
            or len(self.quality_before) != len(self.quality_after)
            or any(type(value) is not int for value in (*self.quality_before, *self.quality_after))
            or not self.quality_after < self.quality_before
        ):
            raise NumericValueError("accepted", "complete strictly improving move trace required")


@dataclass(slots=True)
class NumericSearchState:
    plan: NumericPlan
    evaluation: NumericPlanEvaluation
    accepted_move_count: int = 0
    complete_candidate_evaluation_count: int = 0
    deferred_bridge_candidate_count: int = 0
    accepted_moves: tuple[NumericAcceptedMove, ...] = ()

    def __post_init__(self):
        if (
            not isinstance(self.plan, NumericPlan)
            or not isinstance(self.evaluation, NumericPlanEvaluation)
            or self.evaluation.plan_fingerprint != self.plan.fingerprint
            or self.evaluation.plan_generation != self.plan.generation
        ):
            raise NumericValueError("search_state", "matching numeric plan and evaluation required")
        for name in (
            "accepted_move_count",
            "complete_candidate_evaluation_count",
            "deferred_bridge_candidate_count",
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
    def start(cls, initial):
        if (
            not isinstance(initial, NumericInitialSolution)
            or not initial.complete
            or initial.plan is None
            or initial.evaluation is None
        ):
            raise NumericValueError("search_state", "complete numeric initial solution required")
        return cls(initial.plan, initial.evaluation)

    def commit(self, candidate, evaluation, edit, affected_rows):
        if (
            not isinstance(candidate, NumericPlan)
            or not isinstance(evaluation, NumericPlanEvaluation)
            or not isinstance(edit, NumericCandidateEdit)
            or candidate.task_fingerprint != self.plan.task_fingerprint
            or candidate.generation != self.plan.generation + 1
            or evaluation.plan_fingerprint != candidate.fingerprint
            or evaluation.plan_generation != candidate.generation
            or edit.task_fingerprint != self.plan.task_fingerprint
            or edit.plan_fingerprint != self.plan.fingerprint
            or edit.generation != self.plan.generation
        ):
            raise NumericValueError("candidate", "complete next-generation candidate required")
        before = tuple(int(value) for value in self.evaluation.quality_key)
        after = tuple(int(value) for value in evaluation.quality_key)
        affected = (
            (edit.source_chain_id,)
            if edit.action is NumericSearchAction.CHAIN_ORDER_RELOCATION
            else (edit.source_chain_id, edit.target_chain_id)
        )
        move = NumericAcceptedMove(
            edit.sequence,
            edit.action,
            affected,
            tuple(dict.fromkeys(affected_rows)),
            before,
            after,
        )
        self.plan = candidate
        self.evaluation = evaluation
        self.accepted_move_count += 1
        self.accepted_moves += (move,)


@dataclass(frozen=True, slots=True)
class NumericSearchCheckpoint:
    task_fingerprint: str
    plan_fingerprint: str
    plan_generation: int
    quality_key: tuple[int, ...]
    candidate_check_count: int
    complete_candidate_evaluation_count: int
    accepted_move_count: int
    deferred_bridge_candidate_count: int
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
            "deferred_bridge_candidate_count",
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
            deferred=state.deferred_bridge_candidate_count,
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
            values["deferred"],
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
        retained = [index for index in range(len(chains)) if index not in (source_index, target_index)]
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
            raise NumericValueError("candidate", "real-node relocation cannot move generated material")
        donor = tuple(row for row in source if row != edit.node_row)
        if not donor:
            raise NumericValueError("candidate", "real-node relocation cannot empty a chain")
        receiver = target[: edit.target_position] + (edit.node_row,) + target[edit.target_position :]
        chains[source_index], chains[target_index] = donor, receiver
        periods[source_index] = _assigned_period(task, donor)
        periods[target_index] = _assigned_period(task, receiver)
        return _build_plan(task, plan, chains, chain_ids, periods)
    if edit.action is NumericSearchAction.CHAIN_ORDER_RELOCATION:
        if edit.target_position >= len(chains) or chain_ids[edit.target_position] != edit.target_chain_id:
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


def _try_candidate(task, program, quality, state, budget, edit, affected_rows=()):
    if edit.sequence != budget.candidate_check_count:
        raise NumericValueError("candidate", "candidate sequence does not match consumed budget")
    candidate = apply_numeric_candidate(task, state.plan, edit)
    evaluation = evaluate_numeric_plan(task, program, quality, candidate)
    state.complete_candidate_evaluation_count += 1
    if not budget.allows_search() or not _quality(evaluation) < _quality(state.evaluation):
        return False
    state.commit(candidate, evaluation, edit, affected_rows)
    return True


def _prohibited_profile(task, program, rows):
    result = evaluate_numeric_rows(task, program, rows)
    prohibited = tuple(value for value in result.violations if value.prohibited)
    return len(prohibited), checked_sum(
        (value.severity for value in prohibited), "candidate_prohibited_severity"
    )


def _variants(task, program, rows):
    variants = ((False, rows),)
    reversed_rows = tuple(reversed(rows))
    if reversed_rows != rows and _prohibited_profile(task, program, reversed_rows) <= _prohibited_profile(
        task, program, rows
    ):
        variants += ((True, reversed_rows),)
    return variants


def _edge_allowed(task, program, left, right):
    return not any(value.prohibited for value in evaluate_numeric_edge(task, program, left, right).violations)


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
        or program.task_fingerprint != task.fingerprint
        or quality.task_fingerprint != task.fingerprint
        or quality.rule_program_fingerprint != program.fingerprint
        or state.plan.task_fingerprint != task.fingerprint
        or state.evaluation.task_fingerprint != task.fingerprint
        or state.evaluation.rule_program_fingerprint != program.fingerprint
        or state.evaluation.quality_program_fingerprint != quality.fingerprint
        or state.evaluation.plan_fingerprint != state.plan.fingerprint
    ):
        raise NumericValueError("search", "matching numeric task, programs, state and budget required")


def improve_numeric_whole_chain(task, program, quality, state, budget, *, pair_scan_slack_weight):
    _validate_search_inputs(task, program, quality, state, budget)
    if type(pair_scan_slack_weight) is not int or pair_scan_slack_weight < 0:
        raise NumericValueError("pair_scan_slack_weight", "nonnegative integer weight required")
    weight_rules = program.for_kind(NumericRuleKind.CHAIN_WEIGHT)
    maximum = weight_rules[0].values[1] if weight_rules else None
    pair_maximum = None if maximum is None else checked_sum(
        (maximum, pair_scan_slack_weight), "pair_scan_maximum"
    )
    while budget.allows_search():
        plan = state.plan
        chains, chain_ids, _ = _layout(plan)
        underweight = _underweight_indices(state.evaluation)
        donor_order = [*underweight, *(index for index in range(len(chains)) if index not in underweight)]
        accepted = False
        for donor_index in donor_order:
            for target_index, target in enumerate(chains):
                if not budget.allows_search():
                    return state
                if donor_index == target_index:
                    continue
                source = chains[donor_index]
                total = checked_sum((_chain_weight(task, source), _chain_weight(task, target)), "pair_weight")
                if pair_maximum is not None and total > pair_maximum:
                    continue
                source_variants = _variants(task, program, source)
                target_variants = _variants(task, program, target)
                for source_reversed, _ in source_variants:
                    for target_reversed, target_rows in target_variants:
                        descriptions = (
                            (NumericSearchAction.WHOLE_CHAIN_APPEND, len(target_rows)),
                            (NumericSearchAction.WHOLE_CHAIN_PREPEND, 0),
                            *((NumericSearchAction.WHOLE_CHAIN_INSERTION, position) for position in range(len(target_rows) + 1)),
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
                                state.deferred_bridge_candidate_count += 1
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
                        or checked_sum((target_weight, int(task.nodes.weight[row])), "target_weight") > maximum
                    ):
                        continue
                    if (
                        0 < node_position < len(donor) - 1
                        and not _edge_allowed(task, program, donor[node_position - 1], donor[node_position + 1])
                    ):
                        continue
                    for position in range(len(target) + 1):
                        if not budget.consume_candidate_check():
                            return state
                        if position and not _edge_allowed(task, program, target[position - 1], row):
                            continue
                        if position < len(target) and not _edge_allowed(task, program, row, target[position]):
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
    state = NumericSearchState.start(initial)
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
