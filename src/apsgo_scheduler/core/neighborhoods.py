"""Complete-candidate acceptance and reference-ordered local neighborhoods."""

from dataclasses import dataclass, replace
from decimal import Decimal
from fractions import Fraction
from math import ceil

from .chain_order import (
    chain_order_objective_index,
    has_inter_chain_width_rule,
    stable_group_plan,
)
from .contracts import (
    ControlledSplitMode,
    SearchStopReason,
    SolverPolicy,
    fingerprint,
    freeze_tuple,
    require_decimal,
    require_int,
    require_text,
    sum_decimals,
    sum_weights,
)
from .evaluation import evaluate_plan, quick_chain_prohibited_profile
from .model import Chain, MaterialRole, SchedulePlan, SearchState, SplitLineage, VirtualPurpose
from .resource_facts import derive_evaluation_resource_view
from .rules.base import (
    ChainRuleSubject,
    ControlledSplitDecision,
    ControlledSplitRuleSubject,
    PlanRuleSubject,
    RuleDisposition,
)
from .rules.concrete import WEIGHT_EPSILON, ChainWeightRangeRule, VirtualOutputRatioRule
from .virtual_material import VirtualFactory


def _identities(values, name):
    values = freeze_tuple(values, str, name)
    for value in values:
        require_text(value, name)
    if not values or len(set(values)) != len(values):
        raise ValueError(f"{name} must contain distinct nonempty identities")
    return values


@dataclass(frozen=True, slots=True)
class AcceptedMoveTrace:
    sequence: int
    action_name: str
    affected_chain_ids: tuple[str, ...]
    affected_source_order_ids: tuple[str, ...]
    quality_before: tuple[Decimal | int, ...]
    quality_after: tuple[Decimal | int, ...]
    candidate_check_count: int

    def __post_init__(self):
        require_int(self.sequence, "sequence", minimum=1)
        require_text(self.action_name, "action_name")
        require_int(self.candidate_check_count, "candidate_check_count")
        for name in ("affected_chain_ids", "affected_source_order_ids"):
            object.__setattr__(self, name, _identities(getattr(self, name), name))
        for name in ("quality_before", "quality_after"):
            values = freeze_tuple(getattr(self, name), (Decimal, int), name)
            for value in values:
                if isinstance(value, Decimal):
                    require_decimal(value, name)
                else:
                    require_int(value, name, minimum=None)
            object.__setattr__(self, name, values)
        if (
            not self.quality_before
            or len(self.quality_after) != len(self.quality_before)
            or not self.quality_after < self.quality_before
        ):
            raise ValueError("accepted trace requires equal-sized, strictly improving quality keys")


@dataclass(slots=True)
class SearchContext:
    factory: VirtualFactory
    policy: SolverPolicy
    complete_candidate_evaluation_count: int = 0
    accepted_move_traces: tuple[AcceptedMoveTrace, ...] = ()

    def __post_init__(self):
        if not isinstance(self.factory, VirtualFactory) or not isinstance(
            self.policy, SolverPolicy
        ):
            raise ValueError("search context requires VirtualFactory and SolverPolicy")
        if self.factory.cache.numeric_semantics_key != self.policy.numeric_semantics_key:
            raise ValueError("search policy numeric semantics differ from the bound cache")
        require_int(self.complete_candidate_evaluation_count, "complete_candidate_evaluation_count")
        self.accepted_move_traces = freeze_tuple(
            self.accepted_move_traces, AcceptedMoveTrace, "accepted_move_traces"
        )


def _validate_search(state, context):
    if not isinstance(state, SearchState) or not isinstance(context, SearchContext):
        raise ValueError("search requires SearchState and SearchContext")
    if tuple(item.chain_id for item in state.current_evaluation.chain_evaluations) != tuple(
        chain.chain_id for chain in state.current_plan.chains
    ):
        raise ValueError("current evaluation does not cover the current ordered chains")
    if len(state.current_evaluation.quality_key) != len(
        context.factory.cache.rule_set.quality_spec
    ):
        raise ValueError("current evaluation quality does not match the bound rule set")


def _normalize_chain(chain, context):
    periods = tuple(
        node.source_period
        for node in chain.nodes
        if node.material_role is not MaterialRole.GENERATED_VIRTUAL
    )
    index = context.factory.cache.context.period_index
    if any(period not in index for period in periods):
        raise ValueError("candidate contains an unknown source period")
    earliest = min(periods, key=index.__getitem__)
    return chain if chain.assigned_period == earliest else replace(chain, assigned_period=earliest)


def _split_piece_weights(parent_weight, decision):
    count = ceil(Fraction(parent_weight) / Fraction(decision.maximum_piece_weight))
    if count < 2 or count - 1 > decision.maximum_separator_node_count:
        return ()
    prefix = (decision.maximum_piece_weight,) * (count - 1)
    weights = prefix + (sum_decimals((parent_weight, sum_weights(prefix).copy_negate())),)
    maximum = sum_weights((decision.maximum_piece_weight, WEIGHT_EPSILON))
    return (
        weights
        if all(decision.minimum_piece_weight <= weight <= maximum for weight in weights)
        else ()
    )


def _split_partition_id(subject, decision, weights):
    return "partition-" + fingerprint(
        dict(
            parent_node=subject.parent_node,
            source_period=subject.source_period,
            origin_assigned_period=subject.origin_assigned_period,
            accepted_source_sequence=subject.accepted_split_source_count + 1,
            decision=decision,
            piece_weights=weights,
        )
    )


def _split_lineage(subject, decision, weights, partition_id):
    parent = subject.parent_node
    return SplitLineage(
        partition_id=partition_id,
        parent_node_id=parent.node_id,
        parent_source_order_id=parent.source_order_id,
        source_resource_id=parent.source_resource_id,
        source_period=parent.source_period,
        origin_assigned_period=subject.origin_assigned_period,
        split_mode=decision.mode,
        target_assigned_period=decision.target_assigned_period,
        accepted_source_sequence=subject.accepted_split_source_count + 1,
        parent_weight=parent.weight,
        piece_index=1,
        piece_count=len(weights),
        authorization_rule_id=decision.rule_id,
        authorization_rule_version=decision.rule_version,
        authorization_decision_fingerprint=decision.decision_fingerprint,
        reason_code=decision.reason_code,
    )


def _authorized_split_replacement(state, context, plan, affected, subject, decision, before, after):
    parent = subject.parent_node
    budget, cache = context.factory.budget, context.factory.cache
    donor = next(
        (
            chain
            for chain in state.current_plan.chains
            if parent.node_id in {n.node_id for n in chain.nodes}
        ),
        None,
    )
    if (
        not decision.eligible
        or parent.material_role is not MaterialRole.NORMAL_REAL
        or parent.split_lineage is not None
        or subject.subject_id != parent.node_id
        or before.get(parent.node_id) != parent
        or parent not in cache.problem.nodes
        or donor is None
        or affected != (donor.chain_id,)
        or subject.origin_assigned_period != donor.assigned_period
        or subject.source_period != parent.source_period
        or subject.accepted_split_source_count != state.split_sequence
        or state.split_sequence >= decision.maximum_accepted_source_count
        or parent.node_id in after
        or any(after.get(key) != value for key, value in before.items() if key != parent.node_id)
    ):
        return False
    index = cache.context.period_index
    if subject.source_period not in index or subject.origin_assigned_period not in index:
        raise ValueError("split candidate contains an unknown period")
    source_index, origin_index = index[subject.source_period], index[subject.origin_assigned_period]
    mode = (
        ControlledSplitMode.SAME_PERIOD_SPLIT
        if source_index == origin_index
        else ControlledSplitMode.FUTURE_BORROW_RETURN
    )
    if (
        source_index < origin_index
        or decision.mode is not mode
        or (decision.target_assigned_period != subject.source_period)
    ):
        return False
    if not budget.allows_search():
        return False
    confirmed = cache.rule_set.evaluate_controlled_split(subject, cache.context)
    if not budget.allows_search() or confirmed != decision:
        return False
    weights = _split_piece_weights(parent.weight, decision)
    if not weights:
        return False
    remaining = tuple(node for node in donor.nodes if node.node_id != parent.node_id)
    if remaining and all(
        node.material_role is MaterialRole.GENERATED_VIRTUAL for node in remaining
    ):
        return False
    retained_ids = tuple(
        chain.chain_id
        for chain in state.current_plan.chains
        if chain.chain_id != donor.chain_id or remaining
    )
    returned = plan.chains[-1]
    if (
        tuple(chain.chain_id for chain in plan.chains[:-1]) != retained_ids
        or returned.chain_id in {chain.chain_id for chain in state.current_plan.chains}
        or returned.assigned_period != decision.target_assigned_period
        or {node.node_id for node in returned.nodes} != after.keys() - before.keys()
        or len(returned.nodes) != len(weights) + len(weights) - 1
    ):
        return False
    if (
        remaining
        and next(chain for chain in plan.chains if chain.chain_id == donor.chain_id).nodes
        != remaining
    ):
        return False
    pieces, separators = returned.nodes[::2], returned.nodes[1::2]
    partition_id = _split_partition_id(subject, decision, weights)
    lineage = _split_lineage(subject, decision, weights, partition_id)
    for position, (piece, weight) in enumerate(zip(pieces, weights), start=1):
        if not budget.allows_search():
            return False
        if (
            piece.material_role is not MaterialRole.NORMAL_REAL
            or piece.weight != weight
            or piece.split_lineage != replace(lineage, piece_index=position)
            or replace(piece, node_id=parent.node_id, weight=parent.weight, split_lineage=None)
            != parent
        ):
            return False
    if sum_weights(piece.weight for piece in pieces) != parent.weight or sum_weights(
        node.weight for node in separators
    ) > sum_weights((decision.maximum_separator_weight, WEIGHT_EPSILON)):
        return False
    prototypes = {item.prototype_id: item for item in cache.problem.virtual_prototypes}
    for left, separator, right in zip(pieces, separators, pieces[1:]):
        if not budget.allows_search():
            return False
        if separator.material_role is not MaterialRole.GENERATED_VIRTUAL:
            return False
        virtual = separator.virtual_lineage
        prototype = prototypes.get(virtual.prototype_id)
        if (
            virtual.purpose is not VirtualPurpose.SPLIT_SEPARATOR
            or virtual.related_partition_id != partition_id
            or prototype is None
            or separator.weight != prototype.unit_weight
            or any(
                getattr(separator, name) != getattr(prototype, name)
                for name in ("width", "thickness", "grade", "rule_attributes")
            )
            or separator.min_temperature != parent.min_temperature
            or separator.max_temperature != parent.max_temperature
        ):
            return False
        for first, last in ((left, separator), (separator, right)):
            if not budget.allows_search():
                return False
            connected = cache.allows(first, last)
            if not budget.allows_search() or not connected:
                return False
    return budget.allows_search()


def try_complete_candidate(
    state: SearchState,
    context: SearchContext,
    chains: tuple[Chain, ...],
    *,
    affected_chain_ids: tuple[str, ...],
    virtual_sequence: int,
    action_name: str,
    split_subject: ControlledSplitRuleSubject | None = None,
    split_decision: ControlledSplitDecision | None = None,
    chain_order_only: bool = False,
) -> bool:
    """Evaluate one structurally complete candidate; no business-wide hard filters."""
    _validate_search(state, context)
    if type(chain_order_only) is not bool:
        raise ValueError("chain_order_only must be boolean")
    if (split_subject is None) != (split_decision is None) or (
        split_subject is not None
        and (
            not isinstance(split_subject, ControlledSplitRuleSubject)
            or not isinstance(split_decision, ControlledSplitDecision)
        )
    ):
        raise ValueError("split_subject and split_decision must be a typed pair or both absent")
    chains = freeze_tuple(chains, Chain, "chains")
    affected = _identities(affected_chain_ids, "affected_chain_ids")
    require_int(virtual_sequence, "virtual_sequence")
    require_text(action_name, "action_name")
    if virtual_sequence < state.virtual_sequence:
        raise ValueError("candidate virtual sequence cannot move backwards")
    current = {chain.chain_id: chain for chain in state.current_plan.chains}
    if not set(affected) <= current.keys():
        raise ValueError("affected chain identity is absent from the current plan")
    budget, cache = context.factory.budget, context.factory.cache
    order_index = chain_order_objective_index(cache.rule_set) if chain_order_only else None
    if chain_order_only and (
        order_index is None
        or split_subject is not None
        or virtual_sequence != state.virtual_sequence
        or {chain.chain_id: chain for chain in chains} != current
    ):
        return False
    if not budget.allows_search():
        return False
    untouched = tuple(chain_id for chain_id in current if chain_id not in affected)
    if tuple(chain.chain_id for chain in chains if chain.chain_id in untouched) != untouched:
        return False
    normalized = []
    for chain in chains:
        if not budget.allows_search():
            return False
        if split_subject is not None and (
            not chain.nodes
            or all(node.material_role is MaterialRole.GENERATED_VIRTUAL for node in chain.nodes)
        ):
            return False
        if chain.chain_id in current and chain.chain_id not in affected:
            if chain != current[chain.chain_id]:
                return False
            normalized.append(current[chain.chain_id])
        else:
            normalized.append(_normalize_chain(chain, context))
    if (
        not normalized
        or len({chain.chain_id for chain in normalized}) != len(normalized)
        or len({node.node_id for chain in normalized for node in chain.nodes})
        != sum(len(chain.nodes) for chain in normalized)
    ):
        return False
    plan = SchedulePlan(tuple(normalized))
    if chain_order_only and {chain.chain_id: chain for chain in plan.chains} != current:
        return False
    before = {node.node_id: node for chain in state.current_plan.chains for node in chain.nodes}
    after = {node.node_id: node for chain in plan.chains for node in chain.nodes}
    if split_subject is None:
        if {
            key: node
            for key, node in before.items()
            if node.material_role is not MaterialRole.GENERATED_VIRTUAL
        } != {
            key: node
            for key, node in after.items()
            if node.material_role is not MaterialRole.GENERATED_VIRTUAL
        }:
            return False
    elif not _authorized_split_replacement(
        state, context, plan, affected, split_subject, split_decision, before, after
    ):
        return False
    new_sequences = []
    for chain in plan.chains:
        if not budget.allows_search():
            return False
        for node in chain.nodes:
            if node.split_lineage is not None and (
                chain.assigned_period != node.split_lineage.target_assigned_period
            ):
                return False
            if node.material_role is MaterialRole.GENERATED_VIRTUAL:
                if node.node_id in before:
                    if node != before[node.node_id]:
                        return False
                else:
                    if node.virtual_lineage.prototype_id not in cache.context.virtual_prototype_ids:
                        return False
                    new_sequences.append(node.virtual_lineage.accepted_sequence)
    if len(new_sequences) != virtual_sequence - state.virtual_sequence or any(
        sequence != state.virtual_sequence + offset
        for offset, sequence in enumerate(sorted(new_sequences), start=1)
    ):
        return False
    # Split authorization consumes the local append order; group only after all locks pass.
    if has_inter_chain_width_rule(cache.rule_set):
        if not budget.allows_search():
            return False
        plan = stable_group_plan(plan, cache.context.period_index)
    if not budget.allows_search():
        return False
    context.complete_candidate_evaluation_count += 1
    evaluation = evaluate_plan(plan, cache.rule_set, cache.context)
    if (
        not budget.allows_search()
        or not evaluation.quality_key < state.current_evaluation.quality_key
    ):
        return False
    if chain_order_only and (
        evaluation.quality_key[:order_index] != state.current_evaluation.quality_key[:order_index]
        or evaluation.quality_key[order_index] >= state.current_evaluation.quality_key[order_index]
    ):
        # Reordering must not turn floating accumulation drift into a higher-priority gain.
        return False
    sources = tuple(
        dict.fromkeys(
            node.source_order_id
            for chain in state.current_plan.chains
            if chain.chain_id in affected
            for node in chain.nodes
            if node.material_role is not MaterialRole.GENERATED_VIRTUAL
        )
    )
    trace = AcceptedMoveTrace(
        state.accepted_move_count + 1,
        action_name,
        affected,
        sources,
        state.current_evaluation.quality_key,
        evaluation.quality_key,
        budget.candidate_check_count,
    )
    traces = context.accepted_move_traces + (trace,)
    if not budget.allows_search():
        return False
    state.commit_accepted(
        plan,
        evaluation,
        virtual_sequence=virtual_sequence,
        split_mode=None if split_decision is None else split_decision.mode,
    )
    context.accepted_move_traces = traces
    return True


def _internal_variants(chain, context):
    variants = (chain.nodes,)
    reversed_nodes = tuple(reversed(chain.nodes))
    if reversed_nodes == chain.nodes:
        return variants
    budget, cache = context.factory.budget, context.factory.cache
    if not budget.allows_search():
        return ()
    reverse = replace(chain, nodes=reversed_nodes)
    reverse_profile = quick_chain_prohibited_profile(reverse, cache.rule_set, cache.context)
    if not budget.allows_search():
        return ()
    current_profile = quick_chain_prohibited_profile(chain, cache.rule_set, cache.context)
    if not budget.allows_search():
        return ()
    return variants + (reversed_nodes,) if reverse_profile <= current_profile else variants


def _boundary_join(left, right, context, virtual_sequence):
    if not context.factory.budget.allows_search():
        return None
    if not left or not right:
        return left + right, virtual_sequence
    bridge = context.factory.bridge(
        left[-1],
        right[0],
        max_nodes=context.policy.maximum_virtual_bridge_nodes,
        first_sequence=virtual_sequence + 1,
    )
    return None if bridge is None else (left + bridge + right, virtual_sequence + len(bridge))


def _candidate_merged_sequences(donor, target, context, virtual_sequence):
    donor_variants = _internal_variants(donor, context)
    if context.factory.budget.must_stop:
        return
    target_variants = _internal_variants(target, context)
    for donor_nodes in donor_variants:
        for target_nodes in target_variants:
            for left, right, action in (
                (target_nodes, donor_nodes, "whole_chain_append"),
                (donor_nodes, target_nodes, "whole_chain_prepend"),
            ):
                if not context.factory.budget.consume_candidate_check():
                    return
                joined = _boundary_join(left, right, context, virtual_sequence)
                if joined is not None:
                    nodes, sequence = joined
                    yield nodes, sequence, action
            # Endpoint insertions deliberately repeat the preceding concatenations.
            for position in range(len(target_nodes) + 1):
                if not context.factory.budget.consume_candidate_check():
                    return
                first = _boundary_join(
                    target_nodes[:position], donor_nodes, context, virtual_sequence
                )
                if first is None:
                    continue
                first_nodes, first_sequence = first
                joined = _boundary_join(
                    first_nodes, target_nodes[position:], context, first_sequence
                )
                if joined is not None:
                    nodes, sequence = joined
                    yield nodes, sequence, "whole_chain_insertion"


def improve_whole_chain(state: SearchState, context: SearchContext) -> SearchState:
    """Accept the first improving whole-chain edit, then restart this neighborhood."""
    _validate_search(state, context)
    budget, cache = context.factory.budget, context.factory.cache
    weight_rule = next(
        (rule for rule in cache.rule_set.rules if isinstance(rule, ChainWeightRangeRule)), None
    )
    ratio_rule = next(
        (rule for rule in cache.rule_set.rules if isinstance(rule, VirtualOutputRatioRule)), None
    )
    pair_maximum = (
        sum_weights(
            (
                weight_rule.parameters["max_weight"],
                context.policy.whole_chain_pair_scan_slack_weight,
                WEIGHT_EPSILON,
            )
        )
        if weight_rule is not None
        else None
    )
    while budget.allows_search():
        chains = state.current_plan.chains
        underweight = [
            index
            for index, item in enumerate(state.current_evaluation.chain_evaluations)
            if item.metrics.get("underweight_chain_count", 0)
        ]
        donor_order = underweight + [
            index for index in range(len(chains)) if index not in underweight
        ]
        accepted = False
        for donor_index in donor_order:
            if accepted:
                break
            if not budget.allows_search():
                return state
            for target_index, target in enumerate(chains):
                if not budget.allows_search():
                    return state
                if donor_index == target_index:
                    continue
                donor = chains[donor_index]
                if (
                    pair_maximum is not None
                    and sum_weights((donor.total_weight, target.total_weight)) > pair_maximum
                ):
                    continue
                for nodes, sequence, action in _candidate_merged_sequences(
                    donor, target, context, state.virtual_sequence
                ):
                    if not budget.allows_search():
                        return state
                    merged = _normalize_chain(
                        Chain(target.chain_id, nodes, target.assigned_period), context
                    )
                    if weight_rule is not None:
                        contribution = weight_rule.evaluate(
                            ChainRuleSubject(merged.chain_id, merged), cache.context
                        )
                        if not budget.allows_search():
                            return state
                        if any(
                            item.disposition is RuleDisposition.PROHIBITED
                            for item in contribution.violations
                        ):
                            continue
                    candidate = tuple(
                        chain
                        for index, chain in enumerate(chains)
                        if index not in (donor_index, target_index)
                    ) + (merged,)
                    if ratio_rule is not None:
                        plan = SchedulePlan(candidate)
                        resources = derive_evaluation_resource_view(plan, cache.context)
                        if not budget.allows_search():
                            return state
                        contribution = ratio_rule.evaluate(
                            PlanRuleSubject("plan", plan, resources), cache.context
                        )
                        if not budget.allows_search():
                            return state
                        if any(
                            item.disposition is RuleDisposition.PROHIBITED
                            for item in contribution.violations
                        ):
                            continue
                    if try_complete_candidate(
                        state,
                        context,
                        candidate,
                        affected_chain_ids=(donor.chain_id, target.chain_id),
                        virtual_sequence=sequence,
                        action_name=action,
                    ):
                        accepted = True
                        break
                if accepted or budget.must_stop:
                    break
            if budget.must_stop:
                break
        if not accepted:
            break
    return state


def improve_real_node_relocation(state: SearchState, context: SearchContext) -> SearchState:
    """Move one real node into an underweight chain, restarting after each improvement."""
    _validate_search(state, context)
    budget, cache = context.factory.budget, context.factory.cache
    if not budget.allows_search():
        return state
    weight_rule = next(
        (rule for rule in cache.rule_set.rules if isinstance(rule, ChainWeightRangeRule)), None
    )
    if weight_rule is None:
        return state
    minimum = weight_rule.parameters["min_weight"]
    remaining_minimum = sum_decimals((minimum, WEIGHT_EPSILON.copy_negate()))
    maximum = sum_weights((weight_rule.parameters["max_weight"], WEIGHT_EPSILON))
    while budget.allows_search():
        chains = state.current_plan.chains
        underweight = [
            index
            for index, item in enumerate(state.current_evaluation.chain_evaluations)
            if item.metrics.get("underweight_chain_count", 0)
        ]
        accepted = False
        for target_index in underweight:
            if not budget.allows_search():
                return state
            target = chains[target_index]
            target_weight = target.total_weight
            for donor_index, donor in enumerate(chains):
                if not budget.allows_search():
                    return state
                donor_weight = donor.total_weight
                if donor_index == target_index or donor_weight <= minimum:
                    continue
                for node_index, moved in enumerate(donor.nodes):
                    if not budget.allows_search():
                        return state
                    if moved.material_role is MaterialRole.GENERATED_VIRTUAL:
                        continue
                    remaining_weight = sum_decimals((donor_weight, moved.weight.copy_negate()))
                    if remaining_weight < remaining_minimum:
                        continue
                    if sum_weights((target_weight, moved.weight)) > maximum:
                        continue
                    donor_nodes = donor.nodes[:node_index] + donor.nodes[node_index + 1 :]
                    if node_index > 0 and node_index + 1 < len(donor.nodes):
                        if not budget.allows_search():
                            return state
                        connected = cache.allows(
                            donor.nodes[node_index - 1], donor.nodes[node_index + 1]
                        )
                        if not budget.allows_search():
                            return state
                        if not connected:
                            continue
                    for position in range(len(target.nodes) + 1):
                        if not budget.consume_candidate_check():
                            return state
                        if position > 0:
                            connected = cache.allows(target.nodes[position - 1], moved)
                            if not budget.allows_search():
                                return state
                            if not connected:
                                continue
                        if position < len(target.nodes):
                            connected = cache.allows(moved, target.nodes[position])
                            if not budget.allows_search():
                                return state
                            if not connected:
                                continue
                        # Preserve position counts before applying the complete-chain invariant.
                        if not any(
                            node.material_role is not MaterialRole.GENERATED_VIRTUAL
                            for node in donor_nodes
                        ):
                            continue
                        target_nodes = target.nodes[:position] + (moved,) + target.nodes[position:]
                        candidate = list(chains)
                        candidate[donor_index] = replace(donor, nodes=donor_nodes)
                        candidate[target_index] = replace(target, nodes=target_nodes)
                        if try_complete_candidate(
                            state,
                            context,
                            tuple(candidate),
                            affected_chain_ids=(donor.chain_id, target.chain_id),
                            virtual_sequence=state.virtual_sequence,
                            action_name="real_node_relocation",
                        ):
                            accepted = True
                            break
                        if budget.must_stop:
                            return state
                    if accepted:
                        break
                if accepted:
                    break
            if accepted:
                break
        if not accepted:
            break
    return state


def improve_virtual_weight_fill(state: SearchState, context: SearchContext) -> SearchState:
    """Try one virtual filler at each ordered boundary, accepting only strict improvement."""
    _validate_search(state, context)
    budget, cache = context.factory.budget, context.factory.cache
    if not budget.allows_search():
        return state
    weight_rule = next(
        (rule for rule in cache.rule_set.rules if isinstance(rule, ChainWeightRangeRule)), None
    )
    if weight_rule is None:
        return state
    ratio_rule = next(
        (rule for rule in cache.rule_set.rules if isinstance(rule, VirtualOutputRatioRule)), None
    )
    maximum = weight_rule.parameters["max_weight"]
    maximum_with_tolerance = sum_weights((maximum, WEIGHT_EPSILON))
    while budget.allows_search():
        chains = state.current_plan.chains
        underweight = [
            index
            for index, item in enumerate(state.current_evaluation.chain_evaluations)
            if item.metrics.get("underweight_chain_count", 0)
        ]
        accepted = False
        for target_index in underweight:
            if not budget.allows_search():
                return state
            target = chains[target_index]
            target_weight = target.total_weight
            if target_weight >= maximum:
                continue
            for position in range(len(target.nodes) + 1):
                if not budget.allows_search():
                    return state
                left = target.nodes[position - 1] if position > 0 else None
                right = target.nodes[position] if position < len(target.nodes) else None
                anchor_left, anchor_right = left or right, right or left
                if anchor_left is None or anchor_right is None:
                    continue
                for prototype in cache.problem.virtual_prototypes:
                    if not budget.consume_candidate_check():
                        return state
                    sequence = state.virtual_sequence + 1
                    filler = context.factory.materialize(
                        prototype,
                        anchor_left,
                        anchor_right,
                        purpose=VirtualPurpose.WEIGHT_FILL,
                        sequence=sequence,
                    )
                    if not budget.allows_search():
                        return state
                    if left is not None:
                        connected = cache.allows(left, filler)
                        if not budget.allows_search():
                            return state
                        if not connected:
                            continue
                    if right is not None:
                        connected = cache.allows(filler, right)
                        if not budget.allows_search():
                            return state
                        if not connected:
                            continue
                    if sum_weights((target_weight, filler.weight)) > maximum_with_tolerance:
                        continue
                    nodes = target.nodes[:position] + (filler,) + target.nodes[position:]
                    candidate = list(chains)
                    candidate[target_index] = _normalize_chain(
                        replace(target, nodes=nodes), context
                    )
                    candidate = tuple(candidate)
                    if ratio_rule is not None:
                        plan = SchedulePlan(candidate)
                        resources = derive_evaluation_resource_view(plan, cache.context)
                        if not budget.allows_search():
                            return state
                        contribution = ratio_rule.evaluate(
                            PlanRuleSubject("plan", plan, resources), cache.context
                        )
                        if not budget.allows_search():
                            return state
                        if any(
                            item.disposition is RuleDisposition.PROHIBITED
                            for item in contribution.violations
                        ):
                            continue
                    if try_complete_candidate(
                        state,
                        context,
                        candidate,
                        affected_chain_ids=(target.chain_id,),
                        virtual_sequence=sequence,
                        action_name="virtual_weight_fill",
                    ):
                        accepted = True
                        break
                    if budget.must_stop:
                        return state
                if accepted:
                    break
            if accepted:
                break
        if not accepted:
            break
    return state


def improve_chain_order(state: SearchState, context: SearchContext) -> SearchState:
    """Relocate one whole chain within its period, restarting on first improvement."""
    _validate_search(state, context)
    budget, cache = context.factory.budget, context.factory.cache
    if chain_order_objective_index(cache.rule_set) is None:
        return state
    while budget.allows_search():
        chains = state.current_plan.chains
        accepted = False
        for source_index, moved in enumerate(chains):
            if not budget.allows_search():
                return state
            positions = [
                index
                for index, chain in enumerate(chains)
                if chain.assigned_period == moved.assigned_period
            ]
            for position in positions:
                if position == source_index:
                    continue
                if not budget.consume_candidate_check():
                    return state
                remaining = chains[:source_index] + chains[source_index + 1 :]
                candidate = remaining[:position] + (moved,) + remaining[position:]
                if try_complete_candidate(
                    state,
                    context,
                    candidate,
                    affected_chain_ids=(moved.chain_id,),
                    virtual_sequence=state.virtual_sequence,
                    action_name="chain_order_relocation",
                    chain_order_only=True,
                ):
                    accepted = True
                    break
                if budget.must_stop:
                    return state
            if accepted:
                break
        if not accepted:
            break
    return state


def run_local_search(state: SearchState, context: SearchContext) -> SearchState:
    """Run the fixed neighborhoods once each; preserve any real interruption."""
    _validate_search(state, context)
    budget = context.factory.budget
    for neighborhood in (
        improve_whole_chain,
        improve_real_node_relocation,
        improve_virtual_weight_fill,
    ):
        if not budget.allows_search():
            return state
        neighborhood(state, context)
    if chain_order_objective_index(context.factory.cache.rule_set) is not None:
        if not budget.allows_search():
            return state
        improve_chain_order(state, context)
    if budget.allows_search():
        budget.stop_reason = SearchStopReason.LOCAL_SEARCH_COMPLETE
    return state
