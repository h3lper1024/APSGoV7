"""Timing normalization keeps calendar data out of physical node identity."""

from dataclasses import replace
from decimal import Decimal, Inexact, localcontext

import pytest

from apsgo_scheduler.api.request import fingerprint_public_request
from apsgo_scheduler.app.input_normalizer import InputNormalizationError, normalize_input
from apsgo_scheduler.core.contracts import contract_values, fingerprint
from apsgo_scheduler.core.delivery_timing import (
    DeliveryTimingInput, OrderTimingInput, production_hours,
)
from tests.app.test_input_normalizer import make_request

D = Decimal


def timed_request(**changes):
    request = make_request()
    timing = DeliveryTimingInput(
        "2026-06-01T00:00:00+08:00",
        tuple(OrderTimingInput(order.source_order_id, "2026-06-01", D(10))
              for order in request.orders),
        {p.prototype_id: D("0.1") for p in request.virtual_prototypes},
    )
    return replace(request, delivery_timing=replace(timing, **changes))


def test_duration_and_independent_decimal_context():
    expected = production_hours(D("7.85"), D(1000), D(1), D(100))
    assert expected == D(1) / 6
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = True
        assert production_hours(D("7.85"), D(1000), D(1), D(100)) == expected
        assert normalize_input(timed_request()).delivery_timing.orders["order-0"].due_hours == 24


@pytest.mark.parametrize("bad", [None, True, D(0), D(-1), D("NaN"), D("Infinity")])
def test_invalid_speed_is_not_zero_duration(bad):
    with pytest.raises(ValueError):
        production_hours(D(1), D(1000), D(1), bad)


def test_old_projection_and_physical_nodes_are_unchanged():
    old = make_request()
    new = timed_request()
    assert fingerprint_public_request(old) == fingerprint(contract_values(old, ("delivery_timing",)))
    assert "delivery_timing" not in contract_values(old)
    assert normalize_input(new).nodes == normalize_input(old).nodes
    assert fingerprint_public_request(new) != fingerprint_public_request(old)
    assert normalize_input(new).input_fingerprint != normalize_input(old).input_fingerprint


def test_aware_start_and_day_end_boundary():
    timing = normalize_input(timed_request(schedule_start_at="2026-06-01T03:30:00+08:00")).delivery_timing
    assert timing.orders["order-0"].due_hours == D("20.5")
    utc = normalize_input(timed_request(schedule_start_at="2026-05-31T16:00:00+00:00")).delivery_timing
    assert utc.schedule_start_at == "2026-06-01T00:00:00+08:00"
    with pytest.raises(InputNormalizationError):
        normalize_input(timed_request(schedule_start_at="2026-06-01T00:00:00"))


@pytest.mark.parametrize("changes", [
    {"orders": ()},
    {"orders": (OrderTimingInput("order-0", "2026-06-01", D(1)),) * 2},
    {"orders": (OrderTimingInput("order-0", "invalid", D(1)), OrderTimingInput("order-1", "2026-06-01", D(1)))},
    {"virtual_hours_per_tonne": {}},
])
def test_missing_duplicate_or_invalid_timing_rejected(changes):
    with pytest.raises(InputNormalizationError):
        normalize_input(timed_request(**changes))
