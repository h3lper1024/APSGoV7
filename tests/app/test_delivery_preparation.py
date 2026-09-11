"""Backend-only preparation: explicit speeds and replayable optional extension."""

from decimal import Decimal

import pytest

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_scheduler.api.request import fingerprint_public_request
from apsgo_scheduler.app.delivery_request import prepare_delivery_request
from apsgo_scheduler.core.delivery_timing import production_hours
from apsgo_v7_service.diagnostics import _json_values
from tests.core.test_delivery_search_audit import search_case
from tools.profile_solver_search import load_request

D = Decimal


def preparation(furnace=D(100), process=None):
    request, _, _ = search_case(delivery=False)
    rows = {order.source_order_id: dict(due_date="2026-06-01", furnace_speed_mpm=furnace, process_speed_mpm=process) for order in request.orders}
    return request, rows


@pytest.mark.parametrize("furnace,process,selected", [(D(100), D(50), D(100)), (None, D(50), D(50)), (D(0), D(50), D(50))])
def test_speed_precedence_and_no_physical_or_policy_mutation(furnace, process, selected):
    old, rows = preparation(furnace, process)
    new = prepare_delivery_request(old, schedule_start_at="2026-06-01T00:00:00+08:00", order_timing=rows, virtual_speed_mpm=D(100))
    assert new.orders == old.orders and new.policy == old.policy
    assert old.delivery_timing is None
    first = old.orders[0]
    assert new.delivery_timing.orders[0].duration_hours == production_hours(first.weight, first.width, first.thickness, selected)


@pytest.mark.parametrize("furnace,process", [(True, D(100)), (D("NaN"), D(100)), ("100", D(100)), (None, None), (D(-1), D(0)), (D(100), False)])
def test_bad_speed_not_disguised_as_missing(furnace, process):
    old, rows = preparation(furnace, process)
    with pytest.raises(ValueError):
        prepare_delivery_request(old, schedule_start_at="2026-06-01T00:00:00+08:00", order_timing=rows, virtual_speed_mpm=D(100))


def test_new_backend_request_round_trip(tmp_path):
    old, rows = preparation()
    request = prepare_delivery_request(old, schedule_start_at="2026-06-01T00:00:00+08:00", order_timing=rows, virtual_speed_mpm=D(100))
    path = tmp_path / "request.json"
    path.write_text(dumps_exact_json(dict(request=_json_values(request), request_fingerprint=fingerprint_public_request(request), rule_set_fingerprint=request.rule_set_spec.fingerprint)), encoding="utf-8")
    assert load_request(path) == request
