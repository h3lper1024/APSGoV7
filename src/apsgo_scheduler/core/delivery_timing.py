"""Task timing, separate from physical specifications and their connection cache."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from types import MappingProxyType
from zoneinfo import ZoneInfo

from .contracts import freeze_tuple, require_decimal, require_text

TIMING_SEMANTICS = "delivery_completion_hours_v1"


def _context():
    return Context(prec=28, rounding=ROUND_HALF_EVEN)


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
