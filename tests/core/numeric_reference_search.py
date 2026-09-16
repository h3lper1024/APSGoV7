"""Retired reference helpers from 7108523; test-only, never a production fallback."""


from apsgo_scheduler.core._numeric_evaluation import summarize_numeric_candidate, materialize_numeric_evaluation
from apsgo_scheduler.core._numeric_resources import NumericResourceExtension
from tests.core.numeric_reference_resources import choose_split_separator, choose_virtual_bridge, extend_resource_workspace, split_group, split_piece_node


from apsgo_scheduler.core._numeric_rules import numeric_rows_prohibited_profile, numeric_edge_allowed
from apsgo_scheduler.core._numeric_state import NumericPlan, NumericTask, NumericSearchAction, split_target_periods_match
from apsgo_scheduler.core._numeric_units import NumericValueError, allocate_piece_milliseconds, checked_sum


from apsgo_scheduler.core.model import MaterialRole, VirtualPurpose
from apsgo_scheduler.core._numeric_search import NumericCandidateEdit, _build_plan, _quality

_GENERATED_VIRTUAL = tuple(MaterialRole).index(MaterialRole.GENERATED_VIRTUAL)

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

def _delivery_chain_indices(task, plan):
    def due(index):
        rows = _chain_rows(plan, index)
        owners = (int(task.nodes.source[row]) for row in rows if int(task.nodes.source[row]) >= 0)
        return min(max(0, int(task.originals.due_ms[source])) for source in owners)

    return tuple(sorted(range(plan.chain_ids.size), key=due))
