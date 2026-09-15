"""Read-only numerical layout. The immutable domain plan remains authoritative."""

from dataclasses import dataclass
from types import MappingProxyType

import numpy as np

from .compatibility import RuleEdgeDecisionCache, _finite_projection
from .contracts import require_int
from .model import MaterialRole

PHYSICAL_FIELDS = ("width", "thickness", "min_temperature", "max_temperature")


def readonly(values, dtype):
    array = np.asarray(values, dtype=dtype)
    array.setflags(write=False)
    return array


@dataclass(frozen=True, slots=True, eq=False)
class NodeColumns:
    nodes: tuple
    physical: object
    present: object
    finite: object
    roles: object
    periods: object
    sources: object
    prototypes: object
    weights: tuple

    @classmethod
    def build(cls, nodes, periods, sources, prototypes):
        values, present, finite = [], [], []
        for name in PHYSICAL_FIELDS:
            column, exists, supported = [], [], []
            for node in nodes:
                value = getattr(node, name)
                exists.append(value is not None)
                try:
                    number = _finite_projection(value, name)
                except (ValueError, OverflowError):
                    # An unused physical field may be legal but not projectable.
                    number = None
                supported.append(value is not None and number is not None)
                column.append(np.nan if number is None else number)
            values.append(column)
            present.append(exists)
            finite.append(supported)
        roles = {role: index for index, role in enumerate(MaterialRole)}
        return cls(tuple(nodes), readonly(values, np.float64), readonly(present, np.bool_),
                   readonly(finite, np.bool_), readonly([roles[n.material_role] for n in nodes], np.int8),
                   readonly([periods.get(n.source_period, -1) for n in nodes], np.int64),
                   readonly([sources.get(n.source_order_id, -1) for n in nodes], np.int64),
                   readonly([prototypes.get(n.virtual_lineage.prototype_id, -1) if n.virtual_lineage else -1
                             for n in nodes], np.int64), tuple(n.weight for n in nodes))


@dataclass(frozen=True, slots=True, eq=False)
class TaskNumericCatalog:
    cache: object
    columns: NodeColumns
    rows: object
    periods: object
    sources: object
    prototypes: object
    timing: object
    edge_fields: tuple

    @classmethod
    def build(cls, cache):
        problem = cache.problem
        periods = MappingProxyType(dict(cache.context.period_index))
        sources = MappingProxyType({n.source_order_id: i for i, n in enumerate(problem.nodes)})
        prototypes = MappingProxyType({p.prototype_id: i for i, p in enumerate(problem.virtual_prototypes)})
        columns = NodeColumns.build(problem.nodes, periods, sources, prototypes)
        return cls(cache, columns, MappingProxyType({n.node_id: i for i, n in enumerate(problem.nodes)}),
                   periods, sources, prototypes, problem.delivery_timing, cache._semantic_fields)


@dataclass(frozen=True, slots=True, eq=False)
class PlanNumericView:
    catalog: TaskNumericCatalog
    plan: object
    generation: int
    dynamic: NodeColumns
    node_rows: object
    offsets: object
    chain_periods: object
    chain_positions: object
    node_positions: object
    source_positions: object
    nodes_by_id: object

    @classmethod
    def build(cls, catalog, plan, generation):
        require_int(generation, "plan_generation")
        dynamic, rows, offsets = [], [], [0]
        positions, sources, nodes, chains = {}, {}, {}, {}
        for i, chain in enumerate(plan.chains):
            if chain.chain_id in chains:
                raise ValueError("duplicate chain identity in numeric view")
            chains[chain.chain_id] = i
            for j, node in enumerate(chain.nodes):
                if node.node_id in positions:
                    raise ValueError("duplicate node identity in numeric view")
                positions[node.node_id], nodes[node.node_id] = (i, j), node
                if node.source_order_id is not None:
                    sources.setdefault(node.source_order_id, []).append((i, j))
                row = catalog.rows.get(node.node_id)
                if row is None or catalog.columns.nodes[row] != node:
                    row = len(catalog.columns.nodes) + len(dynamic)
                    dynamic.append(node)
                rows.append(row)
            offsets.append(len(rows))
        return cls(catalog, plan, generation,
                   NodeColumns.build(tuple(dynamic), catalog.periods, catalog.sources, catalog.prototypes),
                   readonly(rows, np.int64), readonly(offsets, np.int64),
                   readonly([catalog.periods.get(c.assigned_period, -1) for c in plan.chains], np.int64),
                   MappingProxyType(chains), MappingProxyType(positions),
                   MappingProxyType({key: tuple(value) for key, value in sources.items()}), MappingProxyType(nodes))

    def matches(self, state, cache):
        return (self.catalog.cache is cache and self.plan is state.current_plan
                and self.generation == state.accepted_move_count)

    def node_at_row(self, row):
        require_int(row, "node_row")
        count = len(self.catalog.columns.nodes)
        if row >= count + len(self.dynamic.nodes):
            raise ValueError("numeric node row is out of range")
        return self.catalog.columns.nodes[row] if row < count else self.dynamic.nodes[row - count]


def direct_insertion_flags(cache, node, target_nodes):
    """Batch only known decisions: -1 resumes the authoritative query at the old cursor.

    Pre-evaluating missing edges would move errors, budget polls and cancellation
    ahead of the candidate which requested them. No decision cache is duplicated.
    """
    if type(cache) is not RuleEdgeDecisionCache:
        return None
    inserted = cache._known_semantic_row(node)
    rows = tuple(cache._known_semantic_row(item) for item in target_nodes)

    def known(left, right):
        value = cache._entries.get((left, right)) if left >= 0 and right >= 0 else None
        return -1 if value is None else int(value.allowed)

    left = np.fromiter((1, *(known(row, inserted) for row in rows)), dtype=np.int8)
    right = np.fromiter((*(known(inserted, row) for row in rows), 1), dtype=np.int8)
    # Preserve short-circuit semantics when the left decision is not known.
    flags = np.where(left == 0, 0, np.where(left == 1, right, -1)).astype(np.int8)
    flags.setflags(write=False)
    object.__setattr__(cache, "_batch_lookup_count", cache._batch_lookup_count + len(flags))
    return flags
