"""Bind existing positional recipes to the immutable plan that produced them."""

from dataclasses import dataclass

from .contracts import require_int

SEGMENT_ACTIONS = frozenset(("width_node_move", "width_node_exchange", "width_block_move", "width_block_exchange"))
ORDER_ACTIONS = frozenset(("width_chain_order_relocation", "chain_order_relocation"))


@dataclass(frozen=True, slots=True)
class CandidateEdit:
    view: object
    recipe: tuple
    sequence: int
    guard: str
    anchors: tuple

    @classmethod
    def bind(cls, view, recipe, sequence, guard):
        require_int(sequence, "candidate_sequence")
        if guard not in ("chain_order", "width_optimization"):
            raise ValueError("unknown candidate entry guard")
        recipe = tuple(recipe)
        action, *indices = recipe
        for index in indices:
            require_int(index, "candidate_position")
        chains = view.plan.chains
        if action in ORDER_ACTIONS:
            if len(indices) != 2:
                raise ValueError("chain relocation needs two positions")
            source, target = indices
            if source == target or max(source, target) >= len(chains):
                raise ValueError("invalid chain relocation positions")
            if chains[source].assigned_period != chains[target].assigned_period:
                raise ValueError("chain relocation changes assigned period")
            anchors = (chains[source], chains[target])
        elif action in SEGMENT_ACTIONS:
            if guard != "width_optimization" or len(indices) != 6:
                raise ValueError("segment edit requires width guard and six positions")
            source, target, start, stop, other_start, other_stop = indices
            if source == target or max(source, target) >= len(chains):
                raise ValueError("segment chains must be distinct and current")
            left, right = chains[source], chains[target]
            if not (start < stop <= len(left.nodes) and other_start <= other_stop <= len(right.nodes)):
                raise ValueError("segment boundaries are out of range")
            if (action.endswith("move") and other_start != other_stop) or (
                action.endswith("exchange") and other_start == other_stop
            ):
                raise ValueError("segment operation and target range disagree")
            if action.startswith("width_node") and (stop - start != 1 or other_stop - other_start > 1):
                raise ValueError("node edit must select individual nodes")
            anchors = (left, right, left.nodes[start:stop], right.nodes[other_start:other_stop])
        elif action == "delivery_intra_move":
            if guard != "width_optimization" or len(indices) != 3:
                raise ValueError("intra-chain edit requires width guard and three positions")
            source, start, target = indices
            if source >= len(chains) or not target < start < len(chains[source].nodes):
                raise ValueError("invalid intra-chain move")
            anchors = (chains[source], chains[source].nodes[start])
        else:
            raise ValueError(f"unsupported pure candidate action: {action}")
        return cls(view, recipe, sequence, guard, anchors)

    def restore(self, state, cache, sequence, guard):
        if not self.view.matches(state, cache) or self.sequence != sequence or self.guard != guard:
            raise ValueError("candidate plan generation, sequence or guard is stale")
        verified = type(self).bind(self.view, self.recipe, self.sequence, self.guard)
        if verified.anchors != self.anchors:
            raise ValueError("candidate source nodes or chains differ")
        return self.recipe
