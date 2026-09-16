"""Release-time inputs are original-order facts, not physical edge properties."""

from dataclasses import replace

import numpy as np
import pytest

from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.core._numeric_kernel import task_columns
from apsgo_scheduler.core._numeric_state import NumericTask
from apsgo_scheduler.core.contracts import contract_values
from tests.core.test_delivery_timing import timed_request


def prepared(earliest=None):
    request = timed_request()
    timing = request.delivery_timing
    request = replace(request, delivery_timing=replace(timing, orders=(
        replace(timing.orders[0], earliest_start_at=earliest), *timing.orders[1:])))
    rules = load_rule_set(request.rule_set_spec)
    problem = normalize_input(request, rules)
    return request, problem, NumericTask.build(problem, rules, request.delivery_timing)


@pytest.mark.parametrize("value", ["", "2026-06-01", "2026-06-01T00:00:00",
    "2026-06-01T00:00:00Z", "2026-06-01T00:00:00+00:00",
    "2026-06-01T00:00:00.000+08:00", "2026-06-01T00:00:00.001+08:00",
    "2026-02-30T00:00:00+08:00", True, 123])
def test_invalid_earliest_is_not_silently_unrestricted(value):
    with pytest.raises(ValueError):
        prepared(value)


@pytest.mark.parametrize("value,offset", [("2026-05-31T23:00:00+08:00", -3600000),
    ("2026-06-01T00:00:00+08:00", 0), ("2026-06-03T08:00:00+08:00", 201600000)])
def test_original_lower_bound_and_presence_are_authoritative(value, offset):
    request, problem, task = prepared(value)
    assert problem.delivery_timing.orders[request.orders[0].source_order_id].earliest_start_at == value
    assert task.originals.earliest_start_ms.tolist() == [offset, 0]
    assert task.originals.has_earliest_start.tolist() == [True, False]
    assert task.originals.earliest_start_ms.dtype == np.int64
    assert not task.originals.earliest_start_ms.flags.writeable
    assert not task.originals.has_earliest_start.flags.writeable
    columns = task_columns(task)
    assert columns.earliest_start is task.originals.earliest_start_ms
    assert columns.has_earliest_start is task.originals.has_earliest_start
    old_request, old_problem, old_task = prepared()
    assert old_request.orders == request.orders and old_problem.nodes == problem.nodes
    assert old_task.fingerprint != task.fingerprint
    assert old_problem.input_fingerprint != problem.input_fingerprint


def test_absent_input_keeps_legacy_serialization_and_marks_absence():
    request, problem, task = prepared()
    assert "earliest_start_at" not in contract_values(request.delivery_timing.orders[0])
    assert "earliest_start_at" not in contract_values(next(iter(problem.delivery_timing.orders.values())))
    assert not task.originals.has_earliest_start.any()
    assert not task.originals.earliest_start_ms.any()


def test_public_preparation_and_diagnostic_roundtrip(tmp_path):
    from apsgo_scheduler.api.json_codec import dumps_exact_json
    from apsgo_scheduler.api.request import fingerprint_public_request
    from apsgo_scheduler.app.delivery_request import prepare_delivery_request
    from apsgo_v7_service.diagnostics import _json_values
    from tests.app.test_delivery_preparation import preparation
    from tools.profile_solver_search import load_request
    from decimal import Decimal

    request, rows = preparation()
    for row in rows.values():
        row['earliest_start_at'] = "2026-06-01T01:00:00+08:00"
    request = prepare_delivery_request(request, schedule_start_at="2026-06-01T00:00:00+08:00",
        order_timing=rows, virtual_speed_mpm=Decimal(100))
    path = tmp_path / 'request.json'
    path.write_text(dumps_exact_json({'request': _json_values(request),
        'request_fingerprint': fingerprint_public_request(request),
        'rule_set_fingerprint': request.rule_set_spec.fingerprint}), encoding='utf-8')
    assert load_request(path) == request
