"""Turn ordered cover paths into a complete, directly connected initial plan."""

from dataclasses import dataclass
from math import isfinite
from time import perf_counter

from .budget import SolveRuntimeBudget
from .chain_order import has_inter_chain_width_rule, stable_group_plan
from .compatibility import ConstructionDAG, RuleEdgeDecisionCache
from .contracts import (
    CoreCandidateSnapshot,
    SearchStopReason,
    fingerprint,
    require_enum,
    require_text,
    sum_weights,
)
from .evaluation import evaluate_plan, quick_chain_prohibited_profile
from .model import Chain, SchedulePlan, SchedulingProblem
from .path_cover import PathCoverResult
from .rules.concrete import WEIGHT_EPSILON, ChainWeightRangeRule


@dataclass(frozen=True, slots=True)
class InitialSolutionResult:
    candidate: CoreCandidateSnapshot | None
    plan_fingerprint: str | None
    stop_reason: SearchStopReason | None
    elapsed_seconds: float

    def __post_init__(self):
        if self.stop_reason is None:
            if not isinstance(self.candidate, CoreCandidateSnapshot):
                raise ValueError("completed initial solution requires CoreCandidateSnapshot")
            require_text(self.plan_fingerprint, "plan_fingerprint")
        else:
            require_enum(self.stop_reason, SearchStopReason, "stop_reason")
            if self.candidate is not None or self.plan_fingerprint is not None:
                raise ValueError(
                    "unfinished initial solution must not expose a candidate or fingerprint"
                )
        if (
            type(self.elapsed_seconds) not in (int, float)
            or not isfinite(self.elapsed_seconds)
            or self.elapsed_seconds < 0
        ):
            raise ValueError("elapsed_seconds must be finite and nonnegative")

    @property
    def complete(self) -> bool:
        return self.stop_reason is None


def construct_initial_plan(
    problem: SchedulingProblem,
    dag: ConstructionDAG,
    path_cover: PathCoverResult,
    cache: RuleEdgeDecisionCache,
    budget: SolveRuntimeBudget,
) -> InitialSolutionResult:
    """Append or cut, without virtual material, candidate acceptance or publication."""
    if not isinstance(problem, SchedulingProblem) or not isinstance(cache, RuleEdgeDecisionCache):
        raise ValueError("initial construction requires a problem and its edge cache")
    if cache.problem != problem:
        raise ValueError("initial construction cache belongs to another problem")
    if not isinstance(dag, ConstructionDAG) or not dag.complete:
        raise ValueError("initial construction requires a complete ConstructionDAG")
    if not isinstance(path_cover, PathCoverResult) or not path_cover.complete:
        raise ValueError("initial construction requires a complete PathCoverResult")
    if path_cover.graph_fingerprint != dag.fingerprint:
        raise ValueError("path cover belongs to another construction graph")
    if not isinstance(budget, SolveRuntimeBudget):
        raise ValueError("initial construction requires the shared runtime budget")
    started = perf_counter()
    nodes = {node.node_id: node for node in problem.nodes}
    path_nodes = tuple(node_id for path in path_cover.paths for node_id in path)
    if (
        set(dag.ordered_node_ids) != set(nodes)
        or len(path_nodes) != len(nodes)
        or set(path_nodes) != set(nodes)
    ):
        raise ValueError("graph and path cover must contain every input node exactly once")
    if any(right not in dag.adjacency[left] for left, right in path_cover.matching_edges):
        raise ValueError("path cover contains an edge absent from the construction graph")
    rules, context = cache.rule_set, cache.context
    maximum = next(
        (
            sum_weights((rule.parameters["max_weight"], WEIGHT_EPSILON))
            for rule in rules.rules
            if isinstance(rule, ChainWeightRangeRule)
        ),
        None,
    )

    def interrupted():
        return InitialSolutionResult(None, None, budget.stop_reason, perf_counter() - started)

    if not budget.allows_search():
        return interrupted()
    for node in problem.nodes:
        if not budget.allows_search():
            return interrupted()
        # Normalization already enforces this; a direct core call must not bypass that boundary.
        if maximum is not None and node.weight > maximum:
            raise ValueError(f"{node.node_id}.weight exceeds the enabled atomic chain maximum")

    chains = []
    for path in path_cover.paths:
        if not budget.allows_search():
            return interrupted()
        current = None
        current_profile = None
        for node_id in path:
            if not budget.allows_search():
                return interrupted()
            node = nodes[node_id]
            if current is None:
                current = Chain(f"initial-{len(chains) + 1:06d}", (node,), node.source_period)
                continue
            within_weight = (
                maximum is None or sum_weights((current.total_weight, node.weight)) <= maximum
            )
            if within_weight:
                if not budget.allows_search():
                    return interrupted()
                connected = cache.allows(current.last_node, node)
                if not budget.allows_search():
                    return interrupted()
                if connected:
                    assigned = min(
                        (current.assigned_period, node.source_period),
                        key=context.period_index.__getitem__,
                    )
                    direct = Chain(current.chain_id, current.nodes + (node,), assigned)
                    if not budget.allows_search():
                        return interrupted()
                    direct_profile = quick_chain_prohibited_profile(direct, rules, context)
                    if not budget.allows_search():
                        return interrupted()
                    if current_profile is None:
                        current_profile = quick_chain_prohibited_profile(current, rules, context)
                        if not budget.allows_search():
                            return interrupted()
                    if direct_profile <= current_profile:
                        current, current_profile = direct, direct_profile
                        continue
            chains.append(current)
            current = Chain(f"initial-{len(chains) + 1:06d}", (node,), node.source_period)
            current_profile = None
        chains.append(current)

    if not budget.allows_search():
        return interrupted()
    plan = SchedulePlan(tuple(chains))
    if tuple(node.node_id for chain in plan.chains for node in chain.nodes) != path_nodes:
        raise ValueError("initial plan changed the ordered input cover")
    if has_inter_chain_width_rule(rules):
        if not budget.allows_search():
            return interrupted()
        plan = stable_group_plan(plan, context.period_index)
    if not budget.allows_search():
        return interrupted()
    evaluation = evaluate_plan(plan, rules, context)
    if not budget.allows_search():
        return interrupted()
    candidate = CoreCandidateSnapshot(plan, evaluation)
    plan_fingerprint = fingerprint(plan)
    if not budget.allows_search():
        return interrupted()
    result = InitialSolutionResult(candidate, plan_fingerprint, None, perf_counter() - started)
    if not budget.allows_search():
        return interrupted()
    return result
