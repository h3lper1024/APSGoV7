"""Task timing, separate from physical specifications and their connection cache."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from types import MappingProxyType
from zoneinfo import ZoneInfo

from .contracts import freeze_tuple, require_decimal, require_text, sum_weights

TIMING_SEMANTICS = "delivery_completion_hours_v1"


def _context():
    return Context(prec=28, rounding=ROUND_HALF_EVEN)


def score_time_seconds(hours):
    """Round a completed duration, never a node's contribution to the running clock."""
    require_decimal(hours, "score_hours", nonnegative=True)
    exact = Context(prec=max(28, len(hours.as_tuple().digits) + 4), rounding=ROUND_HALF_EVEN)
    return exact.multiply(hours, Decimal(3600)).to_integral_value(rounding=ROUND_HALF_EVEN)


def weighted_wait_seconds(weight, hours):
    seconds = score_time_seconds(hours)
    require_decimal(weight, "original_weight", positive=True)
    exact = Context(prec=len(weight.as_tuple().digits) + len(seconds.as_tuple().digits) + 1)
    return exact.multiply(weight, seconds)


def score_seconds_to_hours(seconds):
    return _context().divide(seconds, Decimal(3600))


def production_hours(weight, width, thickness, speed):
    """Tonnes, millimetres and metres/minute to hours, without display rounding."""
    for name, value in zip(("weight", "width", "thickness", "speed"),
                           (weight, width, thickness, speed)):
        require_decimal(value, name, positive=True)
    with localcontext(_context()):
        return weight * Decimal(1000000) / (width * thickness * Decimal("7.85") * speed * 60)


def _start(value):
    require_text(value, "schedule_start_at")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("schedule_start_at must contain a timezone offset")
    return parsed.astimezone(ZoneInfo("Asia/Shanghai"))


def _due(value, start):
    require_text(value, "due_date")
    parsed = date.fromisoformat(value)
    if value != parsed.isoformat():
        raise ValueError("due_date must use YYYY-MM-DD")
    end = datetime.combine(parsed + timedelta(days=1), datetime.min.time(), start.tzinfo)
    delta = end - start
    with localcontext(_context()):
        return (Decimal(delta.days * 86400 + delta.seconds)
                + Decimal(delta.microseconds) / 1000000) / 3600


def validate_earliest_start(value):
    """Factory wall time must be explicit; never infer a browser/local timezone."""
    require_text(value, "earliest_start_at")
    parsed = datetime.fromisoformat(value)
    if (parsed.utcoffset() != timedelta(hours=8)
            or value != parsed.isoformat(timespec="seconds") or parsed.microsecond):
        raise ValueError("earliest_start_at must use YYYY-MM-DDTHH:MM:SS+08:00")
    return value


@dataclass(frozen=True, slots=True)
class OrderTimingInput:
    source_order_id: str
    due_date: str
    duration_hours: Decimal
    earliest_start_at: str | None = field(default=None, metadata={"omit_none": True})

    def __post_init__(self):
        require_text(self.source_order_id, "source_order_id")
        require_text(self.due_date, "due_date")
        require_decimal(self.duration_hours, "duration_hours", positive=True)
        if self.earliest_start_at is not None:
            validate_earliest_start(self.earliest_start_at)


@dataclass(frozen=True, slots=True)
class DeliveryTimingInput:
    schedule_start_at: str
    orders: tuple[OrderTimingInput, ...]
    virtual_hours_per_tonne: Mapping[str, Decimal]
    semantics: str = TIMING_SEMANTICS

    def __post_init__(self):
        require_text(self.schedule_start_at, "schedule_start_at")
        object.__setattr__(self, "orders", freeze_tuple(self.orders, OrderTimingInput, "timing.orders"))
        if self.semantics != TIMING_SEMANTICS:
            raise ValueError("unsupported delivery timing semantics")
        if not isinstance(self.virtual_hours_per_tonne, Mapping):
            raise ValueError("virtual_hours_per_tonne must be a mapping")
        rates = dict(self.virtual_hours_per_tonne)
        for key, value in rates.items():
            require_text(key, "prototype_id")
            require_decimal(value, "virtual_hours_per_tonne", positive=True)
        object.__setattr__(self, "virtual_hours_per_tonne", MappingProxyType(rates))


@dataclass(frozen=True, slots=True)
class OrderDeliveryTiming:
    due_date: str
    due_hours: Decimal
    weight: Decimal
    hours_per_tonne: Decimal
    earliest_start_at: str | None = field(default=None, metadata={"omit_none": True})

    def __post_init__(self):
        require_text(self.due_date, "due_date")
        for name in ("due_hours", "weight", "hours_per_tonne"):
            require_decimal(getattr(self, name), name, positive=name != "due_hours")
        if self.earliest_start_at is not None:
            validate_earliest_start(self.earliest_start_at)


@dataclass(frozen=True, slots=True)
class DeliveryTiming:
    schedule_start_at: str
    orders: Mapping[str, OrderDeliveryTiming]
    virtual_hours_per_tonne: Mapping[str, Decimal]
    semantics: str = TIMING_SEMANTICS

    def __post_init__(self):
        start = _start(self.schedule_start_at)
        if self.semantics != TIMING_SEMANTICS or not self.orders:
            raise ValueError("delivery timing requires supported semantics and orders")
        for key, value in self.orders.items():
            require_text(key, "source_order_id")
            if not isinstance(value, OrderDeliveryTiming) or value.due_hours != _due(value.due_date, start):
                raise ValueError("order timing does not match its due date and start")
        for key, value in self.virtual_hours_per_tonne.items():
            require_text(key, "prototype_id")
            require_decimal(value, "virtual_hours_per_tonne", positive=True)
        object.__setattr__(self, "orders", MappingProxyType(dict(self.orders)))
        object.__setattr__(self, "virtual_hours_per_tonne", MappingProxyType(dict(self.virtual_hours_per_tonne)))


def normalize_delivery_timing(value, nodes, prototypes):
    if not isinstance(value, DeliveryTimingInput):
        raise ValueError("delivery_timing must be DeliveryTimingInput")
    start = _start(value.schedule_start_at)
    inputs = {item.source_order_id.strip(): item for item in value.orders}
    if len(inputs) != len(value.orders) or set(inputs) != {node.source_order_id for node in nodes}:
        raise ValueError("timing orders must match every original order exactly once")
    rates = {key.strip(): rate for key, rate in value.virtual_hours_per_tonne.items()}
    if len(rates) != len(value.virtual_hours_per_tonne) or set(rates) != {p.prototype_id for p in prototypes}:
        raise ValueError("virtual timing must match every prototype exactly once")
    with localcontext(_context()):
        orders = {
            node.source_order_id: OrderDeliveryTiming(
                inputs[node.source_order_id].due_date,
                _due(inputs[node.source_order_id].due_date, start),
                node.weight, inputs[node.source_order_id].duration_hours / node.weight,
                inputs[node.source_order_id].earliest_start_at,
            )
            for node in nodes
        }
    return DeliveryTiming(start.isoformat(), orders, rates)


@dataclass(frozen=True, slots=True)
class DeliveryPerformance:
    newly_late_original_weight: Decimal
    delivery_wait_tardiness_tonne_hours: Decimal
    original_completion_hours: Mapping[str, Decimal]
    node_times: tuple[tuple[str, Decimal, Decimal], ...]
    old_backlog_last_completion_hours: Decimal


def evaluate_delivery(plan, timing, *, details=False, second_precision=False):
    """One ordered scan; split orders complete only at their last real fragment."""
    if not isinstance(timing, DeliveryTiming):
        raise ValueError("delivery evaluation requires validated task timing")
    if type(second_precision) is not bool:
        raise ValueError("second_precision must be boolean")
    completion, weights, rows, seen = {}, {}, [], set()
    with localcontext(_context()):
        clock = Decimal(0)
        for chain in plan.chains:
            for node in chain.nodes:
                if node.node_id in seen:
                    raise ValueError("duplicate node in delivery evaluation")
                seen.add(node.node_id)
                before = clock
                if node.virtual_lineage is not None:
                    rate = timing.virtual_hours_per_tonne.get(node.virtual_lineage.prototype_id)
                    if rate is None:
                        raise ValueError("missing virtual prototype timing")
                else:
                    order = timing.orders.get(node.source_order_id)
                    if order is None:
                        raise ValueError("missing original order timing")
                    rate = order.hours_per_tonne
                    weights.setdefault(node.source_order_id, []).append(node.weight)
                clock += node.weight * rate
                if node.virtual_lineage is None:
                    completion[node.source_order_id] = clock
                if details:
                    rows.append((node.node_id, before, clock))
        if set(completion) != set(timing.orders) or any(
            sum_weights(weights[key]) != order.weight for key, order in timing.orders.items()
        ):
            raise ValueError("delivery evaluation requires conserved original order weights")
        newly_late, burden, clearance = Decimal(0), Decimal(0), Decimal(0)
        seconds_burdens = []
        for key, order in timing.orders.items():
            finish = completion[key]
            if order.due_hours <= 0:
                clearance = max(clearance, finish)
            if order.due_hours > 0 and finish > order.due_hours:
                newly_late += order.weight
            wait = max(Decimal(0), finish - max(Decimal(0), order.due_hours))
            if second_precision:
                seconds_burdens.append(weighted_wait_seconds(order.weight, wait))
            else:
                burden += order.weight * wait
        if second_precision:
            burden = score_seconds_to_hours(sum_weights(seconds_burdens))
            clearance = score_seconds_to_hours(score_time_seconds(clearance))
    return DeliveryPerformance(newly_late, burden, MappingProxyType(completion), tuple(rows), clearance)


@dataclass(frozen=True, slots=True)
class _DeliverySnapshot:
    timing: DeliveryTiming
    second_precision: bool
    nodes: tuple
    durations: tuple
    ends: tuple
    contributions: tuple
    performance: DeliveryPerformance
    reused_prefix: int
    reused_suffix: int
    reused_durations: int
    reused_orders: int


def _evaluate_delivery_reused(plan, timing, second_precision, previous=None):
    """Keep the original ordered Decimal clock; reuse only exactly identical inputs."""
    if not isinstance(timing, DeliveryTiming):
        raise ValueError("delivery evaluation requires validated task timing")
    if type(second_precision) is not bool:
        raise ValueError("second_precision must be boolean")
    if previous is not None and (previous.timing is not timing or previous.second_precision != second_precision):
        previous = None
    nodes = tuple(node for chain in plan.chains for node in chain.nodes)
    old_nodes = {} if previous is None else {
        node.node_id: (node, duration) for node, duration in zip(previous.nodes, previous.durations)
    }
    durations, seen, weights = [], set(), {}
    unchanged_members = previous is not None and len(nodes) == len(old_nodes)
    reused_durations = 0
    with localcontext(_context()):
        for node in nodes:
            if node.node_id in seen:
                raise ValueError("duplicate node in delivery evaluation")
            seen.add(node.node_id)
            old = old_nodes.get(node.node_id)
            if old is not None and old[0] is node:
                durations.append(old[1])
                reused_durations += 1
            else:
                unchanged_members = False
                if node.virtual_lineage is not None:
                    rate = timing.virtual_hours_per_tonne.get(node.virtual_lineage.prototype_id)
                    if rate is None:
                        raise ValueError("missing virtual prototype timing")
                else:
                    order = timing.orders.get(node.source_order_id)
                    if order is None:
                        raise ValueError("missing original order timing")
                    rate = order.hours_per_tonne
                durations.append(node.weight * rate)
        if not unchanged_members:
            for node in nodes:
                if node.virtual_lineage is None:
                    weights.setdefault(node.source_order_id, []).append(node.weight)
            if set(weights) != set(timing.orders) or any(
                sum_weights(weights[key]) != order.weight for key, order in timing.orders.items()
            ):
                raise ValueError("delivery evaluation requires conserved original order weights")
        prefix, suffix = 0, 0
        if previous is not None:
            while prefix < min(len(nodes), len(previous.nodes)) and nodes[prefix] is previous.nodes[prefix]:
                prefix += 1
            while (suffix < min(len(nodes), len(previous.nodes)) - prefix
                   and nodes[-1-suffix] is previous.nodes[-1-suffix]):
                suffix += 1
        ends = [] if previous is None else list(previous.ends[:prefix])
        clock = ends[-1] if ends else Decimal(0)
        reused_suffix = 0
        for index in range(prefix, len(nodes)):
            if suffix and index == len(nodes) - suffix:
                old_start = len(previous.nodes) - suffix
                old_clock = previous.ends[old_start - 1] if old_start else Decimal(0)
                if clock.as_tuple() == old_clock.as_tuple():
                    ends.extend(previous.ends[old_start:])
                    reused_suffix = suffix
                    break
            clock += durations[index]
            ends.append(clock)
        completion = {node.source_order_id: end for node, end in zip(nodes, ends)
                      if node.virtual_lineage is None}
        contributions = []
        newly_late, burden, clearance = Decimal(0), Decimal(0), Decimal(0)
        reused_orders = 0
        for index, (key, order) in enumerate(timing.orders.items()):
            finish = completion[key]
            if previous is not None and previous.performance.original_completion_hours[key].as_tuple() == finish.as_tuple():
                late, cost, backlog = previous.contributions[index]
                reused_orders += 1
            else:
                late = order.weight if order.due_hours > 0 and finish > order.due_hours else Decimal(0)
                backlog = finish if order.due_hours <= 0 else Decimal(0)
                wait = max(Decimal(0), finish - max(Decimal(0), order.due_hours))
                cost = weighted_wait_seconds(order.weight, wait) if second_precision else order.weight * wait
            contributions.append((late, cost, backlog))
            # Match the reference's conditional addition, including its Decimal context.
            if late:
                newly_late += late
            if not second_precision:
                burden += cost
            clearance = max(clearance, backlog)
        if second_precision:
            burden = score_seconds_to_hours(sum_weights(item[1] for item in contributions))
            clearance = score_seconds_to_hours(score_time_seconds(clearance))
    performance = DeliveryPerformance(newly_late, burden, MappingProxyType(completion), (), clearance)
    return _DeliverySnapshot(timing, second_precision, nodes, tuple(durations), tuple(ends),
                             tuple(contributions), performance, prefix, reused_suffix,
                             reused_durations, reused_orders)


class _DeliveryReuse:
    def __init__(self, previous=None):
        self.previous = previous
        self.snapshot = None

    def evaluate(self, plan, timing, second_precision):
        self.snapshot = _evaluate_delivery_reused(plan, timing, second_precision, self.previous)
        return self.snapshot.performance
