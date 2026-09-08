"""Deterministic virtual choices with caller-owned, proposed generation numbers."""

import logging
from dataclasses import dataclass, field
from itertools import pairwise
from math import isfinite

from .budget import SolveRuntimeBudget
from .compatibility import RuleEdgeDecisionCache, _finite_projection
from .contracts import require_int, require_text
from .evaluation import quick_chain_prohibited_profile
from .model import (
    Chain,
    MaterialRole,
    Node,
    VirtualLineage,
    VirtualMaterialPrototype,
    VirtualPurpose,
)
from .process_logging import emit
from .rules.concrete import ConsecutiveVirtualMaterialRule

logger = logging.getLogger(__name__)


def _require_nodes(*nodes):
    if any(not isinstance(node, Node) for node in nodes):
        raise ValueError("virtual material anchors must be Node")


def virtual_smoothness(left: Node, middle: Node, right: Node) -> float:
    """Use the reference's ordered float operations, without rounding the score."""
    _require_nodes(left, middle, right)
    costs = []
    for name in ("width", "thickness"):
        first, center, last = (
            _finite_projection(getattr(node, name), f"{node.node_id}.{name}") or 0.0
            for node in (left, middle, right)
        )
        costs.append(abs(first - center) + abs(center - last))
    width_cost, thickness_cost = costs
    score = width_cost + 100.0 * thickness_cost
    if not isfinite(score):
        raise ValueError("virtual smoothness must be finite")
    return score


@dataclass(frozen=True, slots=True)
class VirtualFactory:
    cache: RuleEdgeDecisionCache
    budget: SolveRuntimeBudget
    _node_id_prefix: str = field(init=False, repr=False)
    _numeric_catalog: object = field(default=None, init=False, repr=False, compare=False)
    _numeric_events: set[str] = field(default_factory=set, init=False, repr=False, compare=False)

    def __post_init__(self):
        if not isinstance(self.cache, RuleEdgeDecisionCache):
            raise ValueError("virtual factory requires the task edge cache")
        if not isinstance(self.budget, SolveRuntimeBudget):
            raise ValueError("virtual factory requires the shared runtime budget")
        prefix = "virtual-"
        while any(node.node_id.startswith(prefix) for node in self.cache.problem.nodes):
            prefix = "_" + prefix
        object.__setattr__(self, "_node_id_prefix", prefix)

    def materialize(
        self,
        prototype: VirtualMaterialPrototype,
        left_anchor: Node,
        right_anchor: Node,
        *,
        purpose: VirtualPurpose,
        sequence: int,
        related_partition_id: str | None = None,
    ) -> Node:
        """Prepare one node; only a later complete-candidate commit publishes its number."""
        if not isinstance(prototype, VirtualMaterialPrototype) or prototype not in (
            self.cache.problem.virtual_prototypes
        ):
            raise ValueError("prototype must match the bound task catalog")
        _require_nodes(left_anchor, right_anchor)
        lineage = VirtualLineage(prototype.prototype_id, purpose, related_partition_id, sequence)
        lower = tuple(
            node.min_temperature
            for node in (left_anchor, right_anchor)
            if node.min_temperature is not None
        )
        upper = tuple(
            node.max_temperature
            for node in (left_anchor, right_anchor)
            if node.max_temperature is not None
        )
        node = Node(
            node_id=f"{self._node_id_prefix}{sequence:06d}",
            source_order_id=None,
            source_resource_id=None,
            source_period=None,
            weight=prototype.unit_weight,
            width=prototype.width,
            thickness=prototype.thickness,
            min_temperature=min(lower) if lower else None,
            max_temperature=max(upper) if upper else None,
            grade=prototype.grade,
            material_role=MaterialRole.GENERATED_VIRTUAL,
            rule_attributes=prototype.rule_attributes,
            virtual_lineage=lineage,
        )
        for name in ("width", "thickness", "min_temperature", "max_temperature"):
            _finite_projection(getattr(node, name), f"{node.node_id}.{name}")
        return node

    def _connected(self, *nodes: Node) -> bool:
        for left, right in pairwise(nodes):
            if not self.budget.allows_search():
                return False
            allowed = self.cache.allows(left, right)
            if not self.budget.allows_search() or not allowed:
                return False
        return True

    def bridge(
        self,
        left: Node,
        right: Node,
        *,
        max_nodes: int,
        first_sequence: int,
    ) -> tuple[Node, ...] | None:
        """Prefer a direct edge, then a single bridge, then the best double bridge."""
        _require_nodes(left, right)
        require_int(max_nodes, "max_nodes")
        if max_nodes > 2:
            raise ValueError("only zero, one or two bridge nodes are supported")
        require_int(first_sequence, "first_sequence", minimum=1)
        if self._connected(left, right):
            return () if self.budget.allows_search() else None
        max_nodes = min(
            (
                max_nodes,
                *(
                    rule.parameters["max_count"]
                    for rule in self.cache.rule_set.rules
                    if isinstance(rule, ConsecutiveVirtualMaterialRule)
                ),
            )
        )
        if not self.budget.allows_search() or max_nodes == 0:
            return None
        result = self._numeric_bridge(
            left, right, max_nodes=max_nodes, first_sequence=first_sequence,
        )
        if result is not NotImplemented:
            return result
        return self._python_bridge(left, right, max_nodes=max_nodes, first_sequence=first_sequence)

    def _numeric_bridge(self, left, right, *, max_nodes, first_sequence):
        if not self.cache.problem.virtual_prototypes or self._numeric_catalog is NotImplemented:
            return NotImplemented
        from . import _bridge_numeric as numeric

        catalog = self._numeric_catalog
        if catalog is not None and not catalog.matches(self.cache):
            object.__setattr__(self, "_numeric_catalog", None)
            catalog = None
        if catalog is None:
            if numeric.supported_rules(self) is None:
                object.__setattr__(self, "_numeric_catalog", NotImplemented)
                return NotImplemented
            catalog = numeric.prepare_catalog(self)
            if catalog is None:
                if not self.budget.allows_search():
                    return None
                object.__setattr__(self, "_numeric_catalog", NotImplemented)
                return NotImplemented
            object.__setattr__(self, "_numeric_catalog", catalog)
        if not self.budget.allows_search():
            return None
        try:
            for offset in range(max_nodes):
                _ = f"{first_sequence + offset:06d}"
        except ValueError:
            # Unbounded integers can exceed Python's string conversion limit during materialize.
            return NotImplemented if self.budget.allows_search() else None
        if not self.budget.allows_search():
            return None
        arrays = numeric.prepare_bridge(catalog, left, right, self.budget)
        if arrays is None:
            return NotImplemented if self.budget.allows_search() else None
        if "start" not in self._numeric_events:
            self._numeric_events.add("start")
            emit(logger, "solver_bridge_numeric_start")
        safe, selected = numeric.scan_single(catalog, *arrays, self.budget)
        if not safe:
            return NotImplemented if self.budget.allows_search() else None
        self._numeric_ready("single", selected, numeric._scan_block.nopython_signatures)
        if selected is None and max_nodes > 1:
            safe, selected = numeric.scan_double(catalog, *arrays, self.budget)
            if not safe:
                return NotImplemented if self.budget.allows_search() else None
            self._numeric_ready("double", selected, numeric._scan_block.nopython_signatures)
        if not self.budget.allows_search() or selected is None:
            return None
        result = []
        for offset, index in enumerate(selected):
            if not self.budget.allows_search():
                return None
            result.append(
                self.materialize(
                    self.cache.problem.virtual_prototypes[index], left, right,
                    purpose=VirtualPurpose.EDGE_BRIDGE, sequence=first_sequence + offset,
                )
            )
            if not self.budget.allows_search():
                return None
        return tuple(result) if self.budget.allows_search() else None

    def _numeric_ready(self, mode, selected, signatures):
        if mode not in self._numeric_events and signatures:
            self._numeric_events.add(mode)
            emit(
                logger, "solver_bridge_numeric_ready", mode=mode, nopython=True,
                found=selected is not None,
            )

    def _python_bridge(self, left, right, *, max_nodes, first_sequence):
        """The original ordered virtual search, after the unchanged public direct-edge prefix."""
        best, best_score = None, None
        first_nodes = {}
        for prototype in self.cache.problem.virtual_prototypes:
            if not self.budget.allows_search():
                return None
            virtual = self.materialize(
                prototype, left, right, purpose=VirtualPurpose.EDGE_BRIDGE, sequence=first_sequence
            )
            first_nodes[prototype.prototype_id] = virtual
            if self._connected(left, virtual, right):
                score = virtual_smoothness(left, virtual, right)
                if best_score is None or score < best_score:
                    best, best_score = (virtual,), score
        if best is not None:
            return best if self.budget.allows_search() else None
        if not self.budget.allows_search() or max_nodes < 2:
            return None
        second_nodes = {}
        for first_prototype in self.cache.problem.virtual_prototypes:
            if not self.budget.allows_search():
                return None
            # The complete single-bridge pass already materialized each first position.
            first = first_nodes[first_prototype.prototype_id]
            if not self._connected(left, first):
                continue
            for second_prototype in self.cache.problem.virtual_prototypes:
                if not self.budget.allows_search():
                    return None
                second = second_nodes.get(second_prototype.prototype_id)
                if second is None:
                    second = self.materialize(
                        second_prototype,
                        left,
                        right,
                        purpose=VirtualPurpose.EDGE_BRIDGE,
                        sequence=first_sequence + 1,
                    )
                    second_nodes[second_prototype.prototype_id] = second
                if self._connected(first, second, right):
                    score = virtual_smoothness(left, first, second) + virtual_smoothness(
                        first, second, right
                    )
                    if not isfinite(score):
                        raise ValueError("double virtual bridge smoothness must be finite")
                    if best_score is None or score < best_score:
                        best, best_score = (first, second), score
        return best if self.budget.allows_search() else None

    def separator(
        self,
        left_piece: Node,
        right_piece: Node,
        *,
        first_sequence: int,
        related_partition_id: str,
    ) -> Node | None:
        """Force one separator, ranking prohibited profile before smoothness."""
        _require_nodes(left_piece, right_piece)
        require_int(first_sequence, "first_sequence", minimum=1)
        require_text(related_partition_id, "related_partition_id")
        context = self.cache.context
        periods = tuple(
            node.source_period
            for node in (left_piece, right_piece)
            if node.material_role is not MaterialRole.GENERATED_VIRTUAL
        )
        if not periods or any(period not in context.period_index for period in periods):
            raise ValueError("separator needs input-supported anchors in the task periods")
        assigned = min(periods, key=context.period_index.__getitem__)
        best, best_score = None, None
        for prototype in self.cache.problem.virtual_prototypes:
            if not self.budget.allows_search():
                return None
            virtual = self.materialize(
                prototype,
                left_piece,
                right_piece,
                purpose=VirtualPurpose.SPLIT_SEPARATOR,
                sequence=first_sequence,
                related_partition_id=related_partition_id,
            )
            if not self._connected(left_piece, virtual, right_piece):
                continue
            chain = Chain("virtual-separator", (left_piece, virtual, right_piece), assigned)
            if not self.budget.allows_search():
                return None
            profile = quick_chain_prohibited_profile(chain, self.cache.rule_set, context)
            if not self.budget.allows_search():
                return None
            score = (*profile, virtual_smoothness(left_piece, virtual, right_piece))
            if best_score is None or score < best_score:
                best, best_score = virtual, score
        return best if self.budget.allows_search() else None
