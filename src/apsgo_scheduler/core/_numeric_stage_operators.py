"""Opt-in bounded first-search operators; production scheduling belongs to SB3.

Only loop coordinates survive a slice. Geometry, rule decisions, evaluation and
publication still use the existing numeric search helpers. Legacy entry points
are deliberately unchanged until the production scheduler opts into slices.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field, replace

import numpy as np

from . import _numeric_search as search
from ._search_phase_budget import SearchAttemptIdentity, SearchPhaseExit, SearchPhaseResult
from .contracts import require_int

BASIC_OPERATORS = ("whole", "node", "fill", "order")
_ARITY = {"whole": 6, "node": 4, "fill": 3, "order": 2, "split": 2}


def _identity(state):
    return (state.task.fingerprint, state.plan.fingerprint, state.plan.generation,
            state.program.fingerprint, state.quality.fingerprint,
            state.virtual_sequence, state.split_sequence)


@dataclass(slots=True)
class NumericOperatorProgress:
    """Next raw loop coordinate, not a list of candidates or a live generator."""
    operator: str
    binding: tuple | None = None
    options: tuple | None = None
    cursor: tuple[int, ...] = ()
    complete: bool = False
    not_applicable: bool = False

    def __post_init__(self):
        if self.operator not in _ARITY:
            raise ValueError("unknown first-search operator")

    def bind(self, state, options):
        current = _identity(state)
        if self.binding == current and self.options != options:
            raise ValueError("same-generation scan options changed")
        if self.binding != current:
            self.binding, self.options = current, options
            self.cursor = (0,) * _ARITY[self.operator]
            self.complete = self.not_applicable = False
        if (len(self.cursor) != _ARITY[self.operator]
                or any(type(x) is not int or x < 0 for x in self.cursor)):
            raise ValueError("nonnegative operator cursor required")

    def complete_for(self, state):
        return self.complete and self.binding == _identity(state)


@dataclass(slots=True)
class NumericBasicProgress:
    operators: dict[str, NumericOperatorProgress] = field(default_factory=lambda: {
        name: NumericOperatorProgress(name) for name in BASIC_OPERATORS})

    def complete_for(self, state):
        return all(self.operators[name].complete_for(state) for name in BASIC_OPERATORS)


@dataclass(slots=True)
class NumericSplitProgress:
    """One logical split/replay cycle, even if either part needs several slices."""
    scan: NumericOperatorProgress = field(default_factory=lambda: NumericOperatorProgress("split"))
    replay: NumericBasicProgress = field(default_factory=NumericBasicProgress)
    pending_replay: bool = False
    replay_started: bool = False
    split_sequence_at_entry: int | None = None
    split_sequence_seen: int | None = None

    def observe(self, state):
        value = state.split_sequence
        if self.split_sequence_at_entry is None:
            self.split_sequence_at_entry = self.split_sequence_seen = value
        elif value < self.split_sequence_seen:
            raise ValueError("split cycle cannot resume with an older resource sequence")
        elif value > self.split_sequence_seen:
            self.pending_replay = True
            self.split_sequence_seen = value


@dataclass(frozen=True, slots=True)
class NumericStageResult:
    budget: SearchPhaseResult
    children: tuple[NumericStageResult, ...]
    generation_before: int
    generation_after: int
    scan_complete: bool
    pending_replay: bool = False


@dataclass(frozen=True, slots=True)
class _Probe:
    key: tuple[int, ...]
    after: tuple[int, ...]
    action: int
    source: int
    target: int
    position: int = -1
    row: int = -1
    source_reversed: bool = False
    target_reversed: bool = False
    total_weight: int = 0
    split_decision: object = None


class _SliceYield(Exception):
    """Only Python pre-charge work raises this; never a numeric CANCELLED code."""


def _seek(origin, prefix):
    return origin[len(prefix)] if origin[:len(prefix)] == prefix else 0


def _mark(progress, key):
    # Rebuilding current pair metadata must not rewind an inner cursor.
    progress.cursor = max(progress.cursor, key)


def _applicable(operator, state):
    kind = search.NumericRuleKind
    if operator == "node":
        return bool(state.program.for_kind(kind.CHAIN_WEIGHT))
    if operator == "fill":
        return bool(state.program.for_kind(kind.CHAIN_WEIGHT) and state.task.prototype_ids)
    if operator == "order":
        return bool(state.program.for_kind(kind.DELIVERY) or state.program.for_kind(kind.INTER_CHAIN_WIDTH))
    # Split eligibility stays with evaluate_numeric_split; do not guess rule IDs.
    return True


def _whole_probes(state, snapshot, progress, slack):
    _, view, columns, rules, weights, _ = snapshot
    origin = progress.cursor
    weight_rules = state.program.for_kind(search.NumericRuleKind.CHAIN_WEIGHT)
    maximum = weight_rules[0].values[1] if weight_rules else None
    pair_maximum = None if maximum is None else search.checked_sum((maximum, slack), "pair_scan_maximum")
    mask = np.zeros(view.count, np.bool_)
    mask[search._underweight_indices(state.evaluation)] = True
    donors = np.concatenate((np.flatnonzero(mask), np.flatnonzero(~mask)))
    reverse = np.full(view.count, -1, np.int64)
    actions = (search.common_candidate.APPEND, search.common_candidate.PREPEND, search.common_candidate.INSERT)
    for d in range(_seek(origin, ()), len(donors)):
        source_index = int(donors[d])
        source = search.chain_rows(view, source_index)
        for target_index in range(_seek(origin, (d,)), view.count):
            _mark(progress, (d, target_index, 0, 0, 0, 0))
            yield None
            if source_index == target_index:
                progress.cursor = (d, target_index + 1, 0, 0, 0, 0)
                continue
            target = search.chain_rows(view, target_index)
            total = search.checked_sum((int(weights[source_index]), int(weights[target_index])), "pair_weight")
            if pair_maximum is not None and total > pair_maximum:
                progress.cursor = (d, target_index + 1, 0, 0, 0, 0)
                continue
            for index, rows in ((source_index, source), (target_index, target)):
                if reverse[index] < 0:
                    allowed, status = search._first_reverse_allowed(columns, rules, rows)
                    search._check_search_status(status)
                    reverse[index] = int(allowed)
            for sr in range(_seek(origin, (d, target_index)), int(reverse[source_index]) + 1):
                for tr in range(_seek(origin, (d, target_index, sr)), int(reverse[target_index]) + 1):
                    for a in range(_seek(origin, (d, target_index, sr, tr)), len(actions)):
                        positions = (target.size,) if a == 0 else ((0,) if a == 1 else range(target.size + 1))
                        for p in range(_seek(origin, (d, target_index, sr, tr, a)), len(positions)):
                            key = (d, target_index, sr, tr, a, p)
                            _mark(progress, key)
                            yield _Probe(key, key[:-1] + (p + 1,), actions[a], source_index,
                                         target_index, int(positions[p]), source_reversed=bool(sr),
                                         target_reversed=bool(tr), total_weight=total)
            progress.cursor = (d, target_index + 1, 0, 0, 0, 0)


def _node_probes(state, snapshot, progress):
    _, view, columns, rules, weights, _ = snapshot
    origin = progress.cursor
    minimum, maximum, _ = state.program.for_kind(search.NumericRuleKind.CHAIN_WEIGHT)[0].values
    targets = search._underweight_indices(state.evaluation)
    for t in range(_seek(origin, ()), len(targets)):
        target_index = targets[t]
        target = search.chain_rows(view, target_index)
        for donor_index in range(_seek(origin, (t,)), view.count):
            _mark(progress, (t, donor_index, 0, 0))
            yield None
            if donor_index == target_index or weights[donor_index] <= minimum:
                progress.cursor = (t, donor_index + 1, 0, 0)
                continue
            donor = search.chain_rows(view, donor_index)
            for node_position in range(_seek(origin, (t, donor_index)), len(donor)):
                _mark(progress, (t, donor_index, node_position, 0))
                yield None
                eligible, status = search._first_move_eligible(columns, rules, donor, node_position,
                    weights[donor_index], weights[target_index], minimum, maximum)
                search._check_search_status(status)
                if not eligible:
                    progress.cursor = (t, donor_index, node_position + 1, 0)
                    continue
                for position in range(_seek(origin, (t, donor_index, node_position)), len(target) + 1):
                    key = (t, donor_index, node_position, position)
                    _mark(progress, key)
                    yield _Probe(key, key[:-1] + (position + 1,), search.common_candidate.REAL_MOVE,
                                 donor_index, target_index, position, int(donor[node_position]))
                progress.cursor = (t, donor_index, node_position + 1, 0)
            progress.cursor = (t, donor_index + 1, 0, 0)


def _fill_probes(state, snapshot, progress):
    _, view, _, _, weights, _ = snapshot
    origin = progress.cursor
    maximum = state.program.for_kind(search.NumericRuleKind.CHAIN_WEIGHT)[0].values[1]
    targets = search._underweight_indices(state.evaluation)
    for t in range(_seek(origin, ()), len(targets)):
        target_index = targets[t]
        _mark(progress, (t, 0, 0))
        yield None
        if weights[target_index] >= maximum:
            progress.cursor = (t + 1, 0, 0)
            continue
        target = search.chain_rows(view, target_index)
        for position in range(_seek(origin, (t,)), len(target) + 1):
            for prototype in range(_seek(origin, (t, position)), len(state.task.prototype_ids)):
                key = (t, position, prototype)
                _mark(progress, key)
                yield _Probe(key, key[:-1] + (prototype + 1,), search.common_candidate.FILL,
                             target_index, target_index, position, prototype)
        progress.cursor = (t + 1, 0, 0)


def _order_probes(state, snapshot, progress):
    _, view, _, _, _, due = snapshot
    origin = progress.cursor
    sources = np.argsort(due, kind="stable")
    periods = state.plan.chain_periods
    for s in range(_seek(origin, ()), len(sources)):
        source_index = int(sources[s])
        _mark(progress, (s, 0))
        yield None
        positions = np.flatnonzero((periods == periods[source_index]) & (np.arange(view.count) != source_index))
        for p in range(_seek(origin, (s,)), len(positions)):
            key = (s, p)
            _mark(progress, key)
            position = int(positions[p])
            yield _Probe(key, (s, p + 1), search.common_candidate.ORDER,
                         source_index, position, position)
        progress.cursor = (s + 1, 0)


def _split_probes(state, snapshot, progress):
    _, view, _, _, _, _ = snapshot
    origin = progress.cursor
    for d in range(_seek(origin, ()), view.count):
        donor = search.chain_rows(view, d)
        for p in range(_seek(origin, (d,)), len(donor)):
            _mark(progress, (d, p))
            yield None
            row = int(donor[p])
            decision = search.evaluate_numeric_split(state.task, state.program, row,
                int(state.plan.chain_periods[d]), state.split_sequence)
            if not decision.eligible:
                progress.cursor = (d, p + 1)
                continue
            yield _Probe((d, p), (d, p + 1), search.common_candidate.SPLIT,
                         d, -1, row=row, split_decision=decision)
        progress.cursor = (d + 1, 0)


def _policy(operator, state, bridges):
    rules = state.program.for_kind(search.NumericRuleKind.CHAIN_WEIGHT)
    maximum = rules[0].values[1] if rules else None
    policy = search.common_candidate.CandidateCheckPolicy
    if operator == "whole":
        return policy(bridges, -1 if maximum is None else maximum)
    if operator == "fill":
        return policy(0, maximum, (search.NumericRuleKind.VIRTUAL_RATIO,))
    if operator == "order":
        return policy(same_period_order=True, record_order_rows=False)
    if operator == "split":
        return policy(bridges, reject_prohibited_kinds=(search.NumericRuleKind.VIRTUAL_RATIO,))
    return policy(0)


def _description(state, workspace, probe):
    ids = state.plan.chain_ids
    target = (search.checked_sum((int(ids.max()), 1), "candidate.chain_id")
              if probe.action == search.common_candidate.SPLIT else ids[probe.target])
    return search._first_description(workspace, probe.action, ids[probe.source], target,
        row=probe.row, position=probe.position, source_reversed=probe.source_reversed,
        target_reversed=probe.target_reversed)


def _attempt_identity(state, sequence):
    return SearchAttemptIdentity(state.task.fingerprint, state.plan.fingerprint,
                                 state.plan.generation, sequence)


def _check_probe(operator, state, budget, snapshot, probe, policy, continuation,
                 description=None, preparation=None):
    workspace, view, columns, rules, _, _ = snapshot
    if not continuation():
        return False
    if operator in {"whole", "node"}:
        source = search.chain_rows(view, probe.source)
        target = search.chain_rows(view, probe.target)
        if operator == "node":
            source = np.array((probe.row,), dtype=np.int64)
        direct, status = search._first_join_direct(columns, rules, source, target, probe.position,
            probe.source_reversed, probe.target_reversed)
        search._check_search_status(status)
        if operator == "whole":
            if not direct:
                state.bridge_required_candidate_count += 1
            else:
                weight_rules = state.program.for_kind(search.NumericRuleKind.CHAIN_WEIGHT)
                if weight_rules and probe.total_weight > weight_rules[0].values[1]:
                    return False
        elif not direct:
            return False
    elif operator == "order":
        order = np.arange(view.count, dtype=np.int64)
        source, position = probe.source, probe.position
        if source < position:
            order[source:position] = order[source + 1:position + 1]
        else:
            order[position + 1:source + 1] = order[position:source]
        order[position] = source
        if not search.preview_numeric_chain_order_quality(state.task, state.program, state.quality,
                state.plan, state.evaluation, order) < search._quality(state.evaluation):
            return False
    if not continuation():
        return False
    if operator == "split":
        result = search.common_candidate.compute_candidate_attempt(workspace, state.program, state.quality,
            description, 0, policy, preparation=preparation, previous_evaluation=state.evaluation,
            allows_continue=continuation)
        return search.consume_candidate_result(state, budget, workspace, result, allows_continue=continuation)
    description = _description(state, workspace, probe)
    return search._try_first_description(state, budget, workspace, description, policy,
                                         allows_continue=continuation)


def _drive_operator(state, budget, phase, progress, slack, bridges):
    options = (slack, bridges)
    progress.bind(state, options)
    if progress.complete_for(state):
        phase.finish(SearchPhaseExit.NOT_APPLICABLE if progress.not_applicable else SearchPhaseExit.SCAN_COMPLETE)
        return
    while phase.allows_new_work():
        if not _applicable(progress.operator, state):
            progress.complete = progress.not_applicable = True
            phase.finish(SearchPhaseExit.NOT_APPLICABLE if budget.candidate_check_count == phase.count_at_entry
                         else SearchPhaseExit.SCAN_COMPLETE)
            return
        snapshot = search._first_workspace(state)
        workspace = snapshot[0]
        policy = _policy(progress.operator, state, bridges)
        factories = {"whole": lambda: _whole_probes(state, snapshot, progress, slack),
                     "node": lambda: _node_probes(state, snapshot, progress),
                     "fill": lambda: _fill_probes(state, snapshot, progress),
                     "order": lambda: _order_probes(state, snapshot, progress),
                     "split": lambda: _split_probes(state, snapshot, progress)}
        binding = _identity(state)

        def before_charge():
            if _identity(state) != binding:
                raise ValueError("preparation identity changed")
            if not phase.allows_new_work():
                raise _SliceYield
            return True

        try:
            with closing(factories[progress.operator]()) as stream:
                while phase.allows_new_work():
                    if _identity(state) != binding:
                        raise ValueError("uncommitted scan identity changed")
                    try:
                        probe = next(stream)
                    except StopIteration:
                        progress.complete = True
                        phase.finish()
                        return
                    if probe is None:
                        continue
                    description = preparation = None
                    if progress.operator == "split":
                        before_charge()
                        description = _description(state, workspace, probe)
                        preparation = search._prepare_current_description(state, budget, workspace,
                            description, policy, split_decision=probe.split_decision,
                            allows_continue=before_charge)
                        if preparation.status == search.CANCELLED and budget.must_stop:
                            return
                        if preparation.status != search.OK:
                            raise search.NumericValueError("split.prepare", f"numeric preparation failed: {preparation.status}")
                        if not preparation.prepared:
                            progress.cursor = probe.after
                            continue
                    sequence = budget.candidate_check_count + 1
                    with phase.candidate_attempt(_attempt_identity(state, sequence)) as attempt:
                        if not attempt.granted:
                            return
                        def continuation():
                            return attempt.allows_continue(_attempt_identity(state, budget.candidate_check_count))
                        accepted = _check_probe(progress.operator, state, budget, snapshot, probe,
                            policy, continuation, description, preparation)
                        progress.cursor = probe.after
                    if accepted:
                        if _identity(state) == binding:
                            raise ValueError("accepted candidate did not advance the official state")
                        progress.bind(state, options)
                        break
        except _SliceYield:
            return
        finally:
            # No private geometry, generator or candidate is kept in progress.
            workspace.reset()


def _validate(state, budget, slack, bridges):
    search._validate_search_inputs(state.task, state.program, state.quality, state, budget)
    require_int(slack, "pair_scan_slack_weight")
    require_int(bridges, "maximum_virtual_bridge_nodes")
    if bridges > 2:
        raise ValueError("zero, one or two bridge nodes required")


def run_operator_slice(state, budget, *, progress, candidate_checks, time_slice_seconds,
                       pair_scan_slack_weight=0, maximum_virtual_bridge_nodes=2, name=None):
    """One resumable operator. This entry never starts any sibling or replay."""
    _validate(state, budget, pair_scan_slack_weight, maximum_virtual_bridge_nodes)
    if not isinstance(progress, NumericOperatorProgress):
        raise ValueError("NumericOperatorProgress required")
    before = state.plan.generation
    with budget.phase_scope(name or progress.operator, candidate_checks, time_slice_seconds) as phase:
        _drive_operator(state, budget, phase, progress, pair_scan_slack_weight, maximum_virtual_bridge_nodes)
    return NumericStageResult(phase.release_unused(), (), before, state.plan.generation,
                              progress.complete_for(state))


def _shares(total):
    first = (total * 50 // 100, total * 25 // 100, total * 10 // 100)
    return (*first, total - sum(first))


def _basic_children(state, budget, phase, progress, slack, bridges):
    if not isinstance(progress, NumericBasicProgress) or tuple(progress.operators) != BASIC_OPERATORS:
        raise ValueError("ordered NumericBasicProgress required")
    limits = _shares(phase.candidate_limit)
    seconds = max(0.0, phase.soft_deadline - phase.started_at)
    times = (seconds * .5, seconds * .25, seconds * .1, seconds * .15)
    results = []
    for operator, limit, duration in zip(BASIC_OPERATORS, limits, times):
        if budget.must_stop:
            break
        item = progress.operators[operator]
        if item.operator != operator:
            raise ValueError("operator progress routed to a different family")
        results.append(run_operator_slice(state, budget, progress=item,
            candidate_checks=limit, time_slice_seconds=duration,
            pair_scan_slack_weight=slack, maximum_virtual_bridge_nodes=bridges,
            name=f"{phase.name}:{operator}"))
    if progress.complete_for(state) and not budget.must_stop:
        phase.finish()
    return tuple(results)


def run_basic_slice(state, budget, *, candidate_checks, time_slice_seconds, progress,
                    pair_scan_slack_weight, maximum_virtual_bridge_nodes=2, name="basic"):
    """Reserve all four sibling shares before any of them consumes a candidate."""
    _validate(state, budget, pair_scan_slack_weight, maximum_virtual_bridge_nodes)
    before = state.plan.generation
    with budget.phase_scope(name, candidate_checks, time_slice_seconds) as phase:
        children = _basic_children(state, budget, phase, progress,
                                   pair_scan_slack_weight, maximum_virtual_bridge_nodes)
    return NumericStageResult(phase.release_unused(), children, before, state.plan.generation,
                              progress.complete_for(state))


def run_split_scan_slice(state, budget, *, candidate_checks, time_slice_seconds, progress,
                         pair_scan_slack_weight, maximum_virtual_bridge_nodes=2, name="split:scan"):
    """Scan only; remember accepted splits even when the slice exits early."""
    if not isinstance(progress, NumericSplitProgress):
        raise ValueError("NumericSplitProgress required")
    progress.observe(state)
    try:
        result = run_operator_slice(state, budget, progress=progress.scan,
            candidate_checks=candidate_checks, time_slice_seconds=time_slice_seconds,
            pair_scan_slack_weight=pair_scan_slack_weight,
            maximum_virtual_bridge_nodes=maximum_virtual_bridge_nodes, name=name)
    finally:
        progress.observe(state)
    return replace(result, pending_replay=progress.pending_replay)


def run_replay_slice(state, budget, *, candidate_checks, time_slice_seconds, progress,
                     pair_scan_slack_weight, maximum_virtual_bridge_nodes=2, name="split:replay"):
    """At most one logical replay per split cycle; resumed slices do not recount."""
    _validate(state, budget, pair_scan_slack_weight, maximum_virtual_bridge_nodes)
    if not isinstance(progress, NumericSplitProgress):
        raise ValueError("NumericSplitProgress required")
    before = state.plan.generation
    children = ()
    with budget.phase_scope(name, candidate_checks, time_slice_seconds) as phase:
        if not progress.pending_replay:
            phase.finish(SearchPhaseExit.NOT_APPLICABLE)
        elif phase.allows_new_work():
            if not progress.replay_started:
                state.replay_count += 1
                progress.replay_started = True
            children = _basic_children(state, budget, phase, progress.replay,
                                       pair_scan_slack_weight, maximum_virtual_bridge_nodes)
            if progress.replay.complete_for(state):
                progress.pending_replay = False
    return NumericStageResult(phase.release_unused(), children, before, state.plan.generation,
                              not progress.pending_replay, progress.pending_replay)


def run_split_slice(state, budget, *, candidate_checks, time_slice_seconds, progress,
                    pair_scan_slack_weight, maximum_virtual_bridge_nodes=2, name="split"):
    """Half for scan, half for bounded replay, both within the same parent grant."""
    _validate(state, budget, pair_scan_slack_weight, maximum_virtual_bridge_nodes)
    if not isinstance(progress, NumericSplitProgress):
        raise ValueError("NumericSplitProgress required")
    before = state.plan.generation
    children = []
    with budget.phase_scope(name, candidate_checks, time_slice_seconds) as phase:
        replay_limit = phase.candidate_limit // 2
        scan_limit = phase.candidate_limit - replay_limit
        seconds = max(0.0, phase.soft_deadline - phase.started_at)
        children.append(run_split_scan_slice(state, budget, progress=progress,
            candidate_checks=scan_limit, time_slice_seconds=seconds / 2,
            pair_scan_slack_weight=pair_scan_slack_weight,
            maximum_virtual_bridge_nodes=maximum_virtual_bridge_nodes, name=f"{name}:scan"))
        if not budget.must_stop:
            children.append(run_replay_slice(state, budget, progress=progress,
                candidate_checks=replay_limit, time_slice_seconds=seconds / 2,
                pair_scan_slack_weight=pair_scan_slack_weight,
                maximum_virtual_bridge_nodes=maximum_virtual_bridge_nodes, name=f"{name}:replay"))
        complete = progress.scan.complete_for(state) and not progress.pending_replay
        if complete and not budget.must_stop:
            phase.finish()
    return NumericStageResult(phase.release_unused(), tuple(children), before, state.plan.generation,
                              complete, progress.pending_replay)
