"""Task-local directed edge decisions, independent of temporary node identities."""

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from math import isfinite
from random import Random
from time import perf_counter
from types import MappingProxyType

from .budget import SolveRuntimeBudget
from .contracts import (
    NUMERIC_SEMANTICS_KEY,
    RuleScope,
    SearchStopReason,
    fingerprint,
    freeze_tuple,
    require_decimal,
    require_enum,
    require_int,
    require_text,
)
from .model import MaterialRole, Node, SchedulingProblem
from .rules.base import (
    EdgeRuleSubject,
    MetricContribution,
    RuleContribution,
    RuleDisposition,
    RuleEvaluationContext,
    RuleViolation,
)
from .rules.rule_set import ProcessRuleSet

_PHYSICAL_FIELDS = frozenset(("width", "thickness", "min_temperature", "max_temperature"))
_NODE_FINGERPRINT_MEMO_LIMIT = 1024
_CORE_FIELDS = _PHYSICAL_FIELDS | {"weight", "grade", "material_role", "source_period"}
_LINEAGE_FIELDS = frozenset(
    (
        "virtual_lineage.prototype_id",
        "virtual_lineage.purpose",
        "virtual_lineage.related_partition_id",
        "split_lineage.partition_id",
    )
)
_IDENTITY_FIELDS = frozenset(
    (
        "node_id",
        "source_order_id",
        "source_resource_id",
        "candidate",
        "candidate_id",
        "candidate_sequence",
        "chain",
        "chain_id",
        "position",
        "accepted_sequence",
        "accepted_source_sequence",
        "acceptedseq",
        "virtual_lineage",
        "split_lineage",
        "rule_attributes",
    )
)


def _finite_projection(value: Decimal | None, name: str) -> float | None:
    require_decimal(value, name, allow_none=True)
    if value is None:
        return None
    projected = float(value)
    if not isfinite(projected):
        raise ValueError(f"{name} must have a finite float projection")
    return projected


@dataclass(frozen=True, slots=True)
class _EdgeDecision:
    allowed: bool
    violations: tuple[tuple[str, str, str, RuleDisposition, Decimal], ...]
    metrics: tuple[MetricContribution, ...]

    def bind(self, subject_id: str) -> RuleContribution:
        return RuleContribution(
            tuple(
                RuleViolation(
                    rule_id, RuleScope.EDGE, subject_id, reason, message, disposition, severity
                )
                for rule_id, reason, message, disposition, severity in self.violations
            ),
            self.metrics,
        )


@dataclass(frozen=True, slots=True)
class RuleEdgeDecisionCache:
    problem: SchedulingProblem
    rule_set: ProcessRuleSet
    context: RuleEvaluationContext
    numeric_semantics_key: str = NUMERIC_SEMANTICS_KEY
    _semantic_fields: tuple[str, ...] = field(init=False, repr=False)
    _node_fingerprints: OrderedDict[int, tuple[Node, str]] = field(
        default_factory=OrderedDict, init=False, repr=False
    )
    _entries: dict[tuple[int, int], _EdgeDecision] = field(
        default_factory=dict, init=False, repr=False
    )
    _semantic_rows: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _batch_lookup_count: int = field(default=0, init=False, repr=False)
    _hit_count: int = field(default=0, init=False, repr=False)
    _miss_count: int = field(default=0, init=False, repr=False)

    def __post_init__(self):
        if not isinstance(self.problem, SchedulingProblem) or not isinstance(
            self.rule_set, ProcessRuleSet
        ):
            raise ValueError("edge cache requires SchedulingProblem and ProcessRuleSet")
        if not isinstance(self.context, RuleEvaluationContext):
            raise ValueError("edge cache requires RuleEvaluationContext")
        if self.numeric_semantics_key != NUMERIC_SEMANTICS_KEY:
            raise ValueError("unknown numeric_semantics_key")
        for name in ("product_line_code", "process_code", "scenario"):
            if getattr(self.problem, name) != getattr(self.rule_set, name):
                raise ValueError(f"edge cache {name} does not match its rule set")
        if (
            self.context.period_order != self.problem.period_order
            or self.context.delivery_timing != self.problem.delivery_timing
            or self.context.virtual_prototype_ids
            != tuple(item.prototype_id for item in self.problem.virtual_prototypes)
        ):
            raise ValueError("edge cache context does not match its problem")
        semantic_fields = {"material_role", *_LINEAGE_FIELDS}
        for rule in self.rule_set.rules_for_scope(RuleScope.EDGE):
            declared = rule.edge_semantic_fields()
            if not isinstance(declared, tuple) or any(
                type(name) is not str or not name.strip() for name in declared
            ):
                raise ValueError(
                    f"{rule.rule_id}.edge_semantic_fields must declare a tuple of field names"
                )
            if len(set(declared)) != len(declared):
                raise ValueError(f"{rule.rule_id}.edge_semantic_fields contains duplicate fields")
            for name in declared:
                if name in _CORE_FIELDS or name in _LINEAGE_FIELDS:
                    continue
                prefix, separator, attribute = name.partition(".")
                if (
                    prefix != "rule_attributes"
                    or not separator
                    or not attribute.strip()
                    or "." in attribute
                    or attribute in _IDENTITY_FIELDS
                ):
                    raise ValueError(
                        f"{rule.rule_id}.edge_semantic_fields contains unsupported field: {name}"
                    )
            semantic_fields.update(declared)
        object.__setattr__(self, "_semantic_fields", tuple(sorted(semantic_fields)))

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.problem.input_fingerprint, self.rule_set.fingerprint, self.numeric_semantics_key

    @property
    def hit_count(self) -> int:
        return self._hit_count

    @property
    def miss_count(self) -> int:
        return self._miss_count

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    def semantic_fingerprint(self, node: Node) -> str:
        if not isinstance(node, Node):
            raise ValueError("edge cache requires Node")
        memo = self._node_fingerprints.get(id(node))
        if memo is not None and memo[0] is node:
            self._node_fingerprints.move_to_end(id(node))
            return memo[1]
        if node.material_role is MaterialRole.GENERATED_VIRTUAL:
            if node.virtual_lineage.prototype_id not in self.context.virtual_prototype_ids:
                raise ValueError(f"{node.node_id}.prototype_id is unknown to this problem")
        elif node.source_period not in self.context.period_index:
            raise ValueError(f"{node.node_id}.source_period is unknown to this problem")
        values = {}
        for name in self._semantic_fields:
            if name.startswith("rule_attributes."):
                attribute = name.partition(".")[2]
                values[name] = (
                    attribute in node.rule_attributes,
                    node.rule_attributes.get(attribute),
                )
            elif name in _LINEAGE_FIELDS:
                parent, _, attribute = name.partition(".")
                lineage = getattr(node, parent)
                values[name] = None if lineage is None else getattr(lineage, attribute)
            else:
                value = getattr(node, name)
                if name in _PHYSICAL_FIELDS:
                    projected = _finite_projection(value, f"{node.node_id}.{name}")
                    values[name] = (value, None if projected is None else projected.hex())
                else:
                    values[name] = value
        result = fingerprint(values)
        # Keep the actual immutable object alive; an object address is never a semantic key.
        self._node_fingerprints[id(node)] = (node, result)
        # Bound only this encoding accelerator; semantic edge decisions remain task-local.
        if len(self._node_fingerprints) > _NODE_FINGERPRINT_MEMO_LIMIT:
            self._node_fingerprints.popitem(last=False)
        return result

    def _semantic_row(self, node: Node) -> int:
        semantic = self.semantic_fingerprint(node)
        row = self._semantic_rows.get(semantic)
        if row is None:
            row = len(self._semantic_rows)
            self._semantic_rows[semantic] = row
        return row

    def _known_semantic_row(self, node: Node) -> int:
        # Pure peek: do not evaluate future nodes or disturb the original cursor's LRU order.
        memo = self._node_fingerprints.get(id(node))
        return self._semantic_rows.get(memo[1], -1) if memo is not None and memo[0] is node else -1

    def _decision(self, left: Node, right: Node, subject_id: str) -> _EdgeDecision:
        # Each cache is already bound to one immutable task/rule/numeric identity.
        key = self._semantic_row(left), self._semantic_row(right)
        if key in self._entries:
            object.__setattr__(self, "_hit_count", self._hit_count + 1)
            return self._entries[key]
        contribution = self.rule_set.evaluate_edge(
            EdgeRuleSubject(subject_id, left, right), self.context
        )
        if any(item.subject_id != subject_id for item in contribution.violations):
            raise ValueError("cached edge violations must retain the input subject_id")
        decision = _EdgeDecision(
            not any(
                item.disposition is RuleDisposition.PROHIBITED for item in contribution.violations
            ),
            tuple(
                (item.rule_id, item.reason_code, item.message, item.disposition, item.severity)
                for item in contribution.violations
            ),
            contribution.metrics,
        )
        self._entries[key] = decision
        object.__setattr__(self, "_miss_count", self._miss_count + 1)
        return decision

    def evaluate_edge(self, subject: EdgeRuleSubject) -> RuleContribution:
        if not isinstance(subject, EdgeRuleSubject):
            raise ValueError("edge cache requires EdgeRuleSubject")
        return self._decision(subject.left, subject.right, subject.subject_id).bind(
            subject.subject_id
        )

    def allows(self, left: Node, right: Node) -> bool:
        return self._decision(left, right, "edge").allowed


@dataclass(frozen=True, slots=True)
class ConstructionDAG:
    ordered_node_ids: tuple[str, ...]
    adjacency: Mapping[str, tuple[str, ...]]
    checked_edge_count: int
    allowed_edge_count: int
    cache_hit_count: int
    direction_rejected_edge_count: int
    elapsed_seconds: float
    stop_reason: SearchStopReason | None
    fingerprint: str | None = field(init=False)

    def __post_init__(self):
        ids = freeze_tuple(self.ordered_node_ids, str, "ordered_node_ids")
        for value in ids:
            require_text(value, "node_id")
        if len(set(ids)) != len(ids):
            raise ValueError("graph nodes must be distinct")
        if not isinstance(self.adjacency, Mapping) or set(self.adjacency) != set(ids):
            raise ValueError("adjacency must cover exactly the ordered graph nodes")
        position = {name: index for index, name in enumerate(ids)}
        adjacency = {}
        for left in ids:
            targets = freeze_tuple(self.adjacency[left], str, "adjacency targets")
            if len(set(targets)) != len(targets) or any(
                right not in position or position[right] <= position[left] for right in targets
            ):
                raise ValueError("graph edges must be distinct and point strictly forward")
            adjacency[left] = targets
        for name in (
            "checked_edge_count",
            "allowed_edge_count",
            "cache_hit_count",
            "direction_rejected_edge_count",
        ):
            require_int(getattr(self, name), name)
        possible_edges = len(ids) * (len(ids) - 1) // 2
        if (
            self.allowed_edge_count != sum(map(len, adjacency.values()))
            or self.allowed_edge_count + self.direction_rejected_edge_count
            > self.checked_edge_count
            or self.cache_hit_count > self.checked_edge_count
            or self.checked_edge_count > possible_edges
        ):
            raise ValueError("graph counters do not match checked forward edges")
        if self.stop_reason is not None:
            require_enum(self.stop_reason, SearchStopReason, "stop_reason")
        elif self.checked_edge_count != possible_edges:
            raise ValueError("complete graph must have checked all forward pairs")
        if (
            type(self.elapsed_seconds) not in (float, int)
            or not isfinite(self.elapsed_seconds)
            or self.elapsed_seconds < 0
        ):
            raise ValueError("graph elapsed_seconds must be finite and nonnegative")
        object.__setattr__(self, "ordered_node_ids", ids)
        object.__setattr__(self, "adjacency", MappingProxyType(adjacency))
        object.__setattr__(
            self,
            "fingerprint",
            fingerprint({"ordered_node_ids": ids, "adjacency": adjacency})
            if self.complete
            else None,
        )

    @property
    def complete(self) -> bool:
        return self.stop_reason is None


def construction_direction_allowed(left: Node, right: Node) -> bool:
    """Construction-only direction policy; never part of the reusable rule cache."""
    if not isinstance(left, Node) or not isinstance(right, Node):
        raise ValueError("construction direction requires two Nodes")
    a = _finite_projection(left.width, "left.width")
    b = _finite_projection(right.width, "right.width")
    return a is None or b is None or not a + 1e-9 < b


def build_construction_dag(
    problem: SchedulingProblem,
    cache: RuleEdgeDecisionCache,
    budget: SolveRuntimeBudget,
    *,
    seed: int,
) -> ConstructionDAG:
    """Freeze the reference's two stable orders, or return an unsigned partial graph."""
    if not isinstance(problem, SchedulingProblem) or not isinstance(cache, RuleEdgeDecisionCache):
        raise ValueError("construction requires a problem and its edge cache")
    if problem != cache.problem:
        raise ValueError("construction cache belongs to another problem")
    if not isinstance(budget, SolveRuntimeBudget):
        raise ValueError("construction requires the shared runtime budget")
    require_int(seed, "seed", minimum=None)
    started = perf_counter()
    initial_hits = cache.hit_count
    ordered = list(problem.nodes)
    adjacency = {node.node_id: [] for node in ordered}
    checked = allowed = direction_rejected = 0

    def result():
        graph = ConstructionDAG(
            tuple(node.node_id for node in ordered),
            adjacency,
            checked,
            allowed,
            cache.hit_count - initial_hits,
            direction_rejected,
            perf_counter() - started,
            budget.stop_reason,
        )
        # Include freezing and fingerprint construction before publishing this immutable value.
        object.__setattr__(graph, "elapsed_seconds", perf_counter() - started)
        return graph

    if not budget.allows_search():
        return result()
    projections = {}
    for node in ordered:
        if not budget.allows_search():
            return result()
        projections[node.node_id] = (
            _finite_projection(node.width, "width"),
            _finite_projection(node.thickness, "thickness"),
            _finite_projection(node.min_temperature, "min_temperature"),
            _finite_projection(node.weight, "weight"),
            cache.rule_set.construction_priority(node),
        )

    def main_key(node):
        width, thickness, temperature, _, priority = projections[node.node_id]
        return (
            -width if width is not None else float("inf"),
            priority,
            thickness if thickness is not None else float("inf"),
            temperature if temperature is not None else float("inf"),
        )

    rng = Random(seed)
    rng.shuffle(ordered)
    ordered.sort(key=main_key)
    # ponytail: O(N²) forward scan matches the reference; index only after measured equivalence.
    for index, left in enumerate(ordered):
        if not budget.allows_search():
            return result()
        candidates = adjacency[left.node_id]
        for right in ordered[index + 1 :]:
            if not budget.allows_search():
                return result()
            rule_allowed = cache.allows(left, right)
            checked += 1
            if not rule_allowed:
                continue
            if not construction_direction_allowed(left, right):
                direction_rejected += 1
                continue
            candidates.append(right.node_id)
            allowed += 1
        rng.shuffle(candidates)
        left_thickness = projections[left.node_id][1] or 0.0

        def successor_key(name):
            _, thickness, _, weight, priority = projections[name]
            return abs(left_thickness - (thickness or 0.0)), -weight, priority

        candidates.sort(key=successor_key)
    if not budget.allows_search():
        return result()
    graph = result()
    # Signing a large graph also costs time; do not let its final checkpoint escape the budget.
    if not budget.allows_search():
        return replace(
            graph, stop_reason=budget.stop_reason, elapsed_seconds=perf_counter() - started
        )
    return graph
