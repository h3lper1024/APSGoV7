"""Task timing, separate from physical specifications and their connection cache."""

from collections.abc import Mapping
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class OrderTimingInput:
    source_order_id: str
    due_date: str
    duration_hours: Decimal

    def __post_init__(self):
        require_text(self.source_order_id, "source_order_id")
        require_text(self.due_date, "due_date")
        require_decimal(self.duration_hours, "duration_hours", positive=True)


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

    def __post_init__(self):
        require_text(self.due_date, "due_date")
        for name in ("due_hours", "weight", "hours_per_tonne"):
            require_decimal(getattr(self, name), name, positive=name != "due_hours")


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
