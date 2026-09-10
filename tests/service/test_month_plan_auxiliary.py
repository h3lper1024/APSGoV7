import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from apsgo_scheduler.api.json_codec import dumps_exact_json
import apsgo_v7_service.month_plan_auxiliary as month_plan_auxiliary
from apsgo_v7_service import app as http_module
from apsgo_v7_service.month_plan_auxiliary import (
    MONTH_PRESET_CONTRACT_VERSION,
    loads_preset_big_rolls_request,
    preset_big_rolls,
)
from apsgo_v7_service.month_scheduling import MonthSchedulingContractError

REQUEST_ID = "7812c8bc-41b1-4a13-b9c1-ecb35c07793d"
PATH = http_module.MONTH_PRESET_BIG_ROLLS_PATH


def _request(*orders, start_month="2024-01"):
    return {
        "contract_version": MONTH_PRESET_CONTRACT_VERSION,
        "request_id": REQUEST_ID,
        "start_month": start_month,
        "orders": [
            {
                "source_order_id": source_order_id,
                "due_date": due_date,
            }
            for source_order_id, due_date in orders
        ],
    }


def _post(client, value, *, content_type="application/json", headers=None):
    request_headers = {"content-type": content_type}
    request_headers.update(headers or {})
    return client.post(
        PATH,
        content=dumps_exact_json(value).encode("utf-8"),
        headers=request_headers,
    )


def _issue_code(error):
    return error.value.issues[0].code


def test_preset_boundaries_cross_months_and_keep_uncompressed_roll_numbers():
    data = preset_big_rolls(
        loads_preset_big_rolls_request(
            dumps_exact_json(
                _request(
                    ("day-5", "2024-01-05"),
                    ("day-6", "2024-01-06"),
                    ("day-25", "2024-01-25"),
                    ("day-26", "2024-01-26"),
                    ("leap-day", "2024-02-29"),
                    ("next-year", "2025-01-01"),
                )
            )
        )
    )

    assert [item["big_roll_number"] for item in data["periods"]] == [1, 2, 5, 6, 12, 73]
    assert [item["sequence"] for item in data["periods"]] == list(range(6))
    assert data["periods"][0]["start_date"] == "2024-01-01"
    assert data["periods"][0]["end_date"] == "2024-01-05"
    assert data["periods"][2]["start_date"] == "2024-01-21"
    assert data["periods"][2]["end_date"] == "2024-01-25"
    assert data["periods"][3]["start_date"] == "2024-01-26"
    assert data["periods"][3]["end_date"] == "2024-01-31"
    assert data["periods"][4]["start_date"] == "2024-02-26"
    assert data["periods"][4]["end_date"] == "2024-02-29"
    assert data["periods"][5]["start_date"] == "2025-01-01"
    assert data["periods"][5]["end_date"] == "2025-01-05"
    assert [item["source_period"] for item in data["assignments"]] == [
        "BR_00000001",
        "BR_00000002",
        "BR_00000005",
        "BR_00000006",
        "BR_00000012",
        "BR_00000073",
    ]
    assert [item["preset_sequence"] for item in data["assignments"]] == [1, 2, 3, 4, 5, 6]
    assert data["warnings"] == []


def test_missing_invalid_and_early_due_dates_fall_to_first_roll_with_warnings():
    data = preset_big_rolls(
        loads_preset_big_rolls_request(
            dumps_exact_json(
                _request(
                    ("missing", None),
                    ("invalid", "not-a-date"),
                    ("invalid-day", "2024-02-30"),
                    ("early", "2023-12-31"),
                )
            )
        )
    )

    assert data["periods"] == [
        {
            "period_id": "BR_00000001",
            "sequence": 0,
            "big_roll_number": 1,
            "start_date": "2024-01-01",
            "end_date": "2024-01-05",
        }
    ]
    assert [item["source_period"] for item in data["assignments"]] == ["BR_00000001"] * 4
    assert [item["code"] for item in data["warnings"]] == [
        "due_date_missing",
        "due_date_invalid",
        "due_date_invalid",
        "due_before_start_month",
    ]
    assert [item["subject_id"] for item in data["warnings"]] == [
        "missing",
        "invalid",
        "invalid-day",
        "early",
    ]


def test_preset_rejects_duplicate_sources_and_wrong_due_date_type():
    with pytest.raises(MonthSchedulingContractError) as duplicate:
        loads_preset_big_rolls_request(
            dumps_exact_json(_request(("same", "2024-01-01"), ("same", "2024-01-02")))
        )
    assert _issue_code(duplicate) == "duplicate_identity"

    with pytest.raises(MonthSchedulingContractError) as invalid_type:
        loads_preset_big_rolls_request(
            dumps_exact_json(_request(("typed", 20240101)))
        )
    assert _issue_code(invalid_type) == "invalid_field_type"
    assert invalid_type.value.issues[0].field_path == "orders[0].due_date"


@pytest.mark.parametrize(
    ("change", "issue_code"),
    (
        ({"contract_version": "wrong"}, "unsupported_contract_version"),
        ({"request_id": "not-a-uuid"}, "invalid_field_value"),
        ({"start_month": "2024-1"}, "invalid_field_value"),
        ({"orders": []}, "empty_collection"),
    ),
)
def test_preset_rejects_invalid_root_values(change, issue_code):
    value = _request(("order-1", "2024-01-01"))
    value.update(change)
    with pytest.raises(MonthSchedulingContractError) as error:
        loads_preset_big_rolls_request(dumps_exact_json(value))
    assert _issue_code(error) == issue_code


@pytest.fixture
def client(tmp_path):
    with TestClient(
        http_module.create_app(tmp_path / "unused.sqlite3"),
        raise_server_exceptions=False,
    ) as value:
        yield value


def test_preset_http_success_returns_exact_shape_without_database(client):
    response = _post(
        client,
        _request(
            ("order-1", "2026-09-05"),
            ("order-2", "2026-10-01"),
            start_month="2026-09",
        ),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    data = json.loads(response.content.decode("utf-8"))
    assert set(data) == {
        "contract_version",
        "request_id",
        "status",
        "start_month",
        "periods",
        "assignments",
        "warnings",
    }
    assert data["request_id"] == REQUEST_ID
    assert data["start_month"] == "2026-09"
    assert data["periods"][-1]["period_id"] == "BR_00000007"
    assert data["warnings"] == []


def test_preset_http_has_independent_media_type_and_size_errors(client):
    wrong_type = client.post(PATH, content=b"{}", headers={"content-type": "text/plain"})
    assert wrong_type.status_code == 415
    assert json.loads(wrong_type.content.decode("utf-8"))["error"]["code"] == "invalid_content_type"

    too_large = client.post(
        PATH,
        content=b"{}",
        headers={
            "content-type": "application/json",
            "content-length": str(http_module.MAX_MONTH_PLAN_AUXILIARY_REQUEST_BODY_BYTES + 1),
        },
    )
    assert too_large.status_code == 413
    assert json.loads(too_large.content.decode("utf-8"))["error"]["code"] == "request_too_large"


def test_preset_http_maps_shape_errors_to_400_and_input_errors_to_422(client):
    shape = _request(("order-1", "2026-09-05"))
    shape["unexpected"] = True
    shape_response = _post(client, shape)
    assert shape_response.status_code == 400
    assert json.loads(shape_response.content.decode("utf-8"))["error"]["code"] == "invalid_request"

    input_response = _post(client, _request(("order-1", 20260905)))
    assert input_response.status_code == 422
    error = json.loads(input_response.content.decode("utf-8"))["error"]
    assert error["code"] == "invalid_input"
    assert error["issues"][0]["field_path"] == "orders[0].due_date"

DATE_PATH = http_module.MONTH_CALCULATE_LATEST_DATES_PATH
SOLVE_REQUEST_ID = "0f8e5d09-03f2-4ad9-a850-2ac0e67eb451"
FINGERPRINT = "a" * 64


def _date_config(tmp_path, *, product_line="GQGA4", process="galvanizing"):
    path = tmp_path / "month_plan_dates.json"
    path.write_text(
        json.dumps(
            {
                "time_zone": "Asia/Shanghai",
                "lead_days": {
                    "casting": 3,
                    "grinding": 2,
                    "hot_rolling": 4,
                    "tempering": 3,
                    "pickling_rolling": 3,
                },
                "product_line_process": {product_line: process},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _latest_row(
    node_id,
    *,
    chain_id="CHAIN-1",
    chain_sequence=1,
    node_sequence=1,
    weight_t=Decimal("94.2"),
    width_mm=Decimal("1000"),
    thickness_mm=Decimal("1"),
    furnace_speed_mpm=Decimal("100"),
    process_speed_mpm=None,
    production_rate_tph=None,
    production_rate_source=None,
    assigned_period="BR_00000001",
):
    return {
        "node_id": node_id,
        "source_order_id": None if node_id.startswith("V") else node_id,
        "assigned_period": assigned_period,
        "chain_id": chain_id,
        "chain_sequence": chain_sequence,
        "node_sequence": node_sequence,
        "weight_t": weight_t,
        "width_mm": width_mm,
        "thickness_mm": thickness_mm,
        "furnace_speed_mpm": furnace_speed_mpm,
        "process_speed_mpm": process_speed_mpm,
        "production_rate_tph": production_rate_tph,
        "production_rate_source": production_rate_source,
    }


def _latest_request(*rows, product_line="GQGA4", current_process="galvanizing"):
    return {
        "contract_version": "month-latest-dates-v1",
        "request_id": REQUEST_ID,
        "product_line_code": product_line,
        "current_process": current_process,
        "calc_date": "2026-09-01",
        "solve_reference": {
            "solve_request_id": SOLVE_REQUEST_ID,
            "bound_result_fingerprint": FINGERPRINT,
        },
        "periods": [{"period_id": "BR_00000001", "sequence": 0}],
        "rows": list(rows),
    }


def _calculate(value, config_path):
    return month_plan_auxiliary.calculate_latest_dates(
        month_plan_auxiliary.loads_calculate_latest_dates_request(dumps_exact_json(value)),
        config_path,
    )


def test_latest_dates_calculate_coating_hours_and_previous_processes(tmp_path):
    config_path = _date_config(tmp_path)
    data = _calculate(
        _latest_request(
            _latest_row("A"),
            _latest_row(
                "B",
                node_sequence=2,
                weight_t=Decimal("47.1"),
                furnace_speed_mpm=None,
                process_speed_mpm=Decimal("100"),
            ),
            _latest_row(
                "V-1",
                node_sequence=3,
                weight_t=Decimal("20"),
                furnace_speed_mpm=None,
                process_speed_mpm=None,
            ),
        ),
        config_path,
    )

    assert data["anchor_process"] == "coating"
    assert data["time_zone"] == "Asia/Shanghai"
    assert data["lead_days_snapshot"]["pickling_rolling"] == 3
    assert [row["duration_hours"] for row in data["rows"]] == [2, 1, 0]
    assert [row["cumulative_hours"] for row in data["rows"]] == [2, 3, 3]
    first = data["rows"][0]
    assert first["current_process_start_at"] == "2026-09-01T00:00:00+08:00"
    assert first["current_process_latest_at"] == "2026-09-01T02:00:00+08:00"
    assert first["latest_dates"] == {
        "casting": "2026-08-17T02:00:00+08:00",
        "grinding": "2026-08-20T02:00:00+08:00",
        "hot_rolling": "2026-08-22T02:00:00+08:00",
        "tempering": "2026-08-26T02:00:00+08:00",
        "pickling_rolling": "2026-08-29T02:00:00+08:00",
        "coating": "2026-09-01T02:00:00+08:00",
    }
    assert data["rows"][1]["current_process_latest_at"] == "2026-09-01T03:00:00+08:00"
    assert [warning["code"] for warning in data["warnings"]] == ["zero_duration_missing_speed"]
    assert data["warnings"][0]["subject_id"] == "V-1"


def test_latest_dates_warn_for_invalid_dimensions_and_cross_midnight(tmp_path):
    config_path = _date_config(tmp_path)
    data = _calculate(
        _latest_request(
            _latest_row("long", weight_t=Decimal("1083.3")),
            _latest_row(
                "bad-size",
                node_sequence=2,
                weight_t=Decimal("10"),
                width_mm=Decimal("0"),
            ),
        ),
        config_path,
    )

    assert data["rows"][0]["duration_hours"] == 23
    assert data["rows"][1]["current_process_latest_at"] == "2026-09-01T23:00:00+08:00"
    assert [warning["code"] for warning in data["warnings"]] == [
        "zero_duration_invalid_dimensions"
    ]


def test_latest_dates_pickling_branch_uses_tph_and_stops_after_anchor(tmp_path):
    config_path = _date_config(tmp_path, product_line="XQPT", process="pickling_rolling")
    data = _calculate(
        _latest_request(
            _latest_row(
                "acid",
                furnace_speed_mpm=None,
                production_rate_tph=Decimal("47.1"),
                production_rate_source="fixed_xqpt",
            ),
            product_line="XQPT",
            current_process="pickling_rolling",
        ),
        config_path,
    )

    row = data["rows"][0]
    assert data["anchor_process"] == "pickling_rolling"
    assert row["duration_hours"] == 2
    assert row["latest_dates"] == {
        "casting": "2026-08-20T02:00:00+08:00",
        "grinding": "2026-08-23T02:00:00+08:00",
        "hot_rolling": "2026-08-25T02:00:00+08:00",
        "tempering": "2026-08-29T02:00:00+08:00",
        "pickling_rolling": "2026-09-01T02:00:00+08:00",
        "coating": None,
    }


def test_latest_dates_reject_wrong_units_missing_rate_source_and_bad_chains(tmp_path):
    config_path = _date_config(tmp_path, product_line="XQPT", process="pickling_rolling")
    acid = _latest_request(
        _latest_row(
            "acid",
            furnace_speed_mpm=None,
            production_rate_tph=Decimal("47.1"),
            production_rate_source=None,
        ),
        product_line="XQPT",
        current_process="pickling_rolling",
    )
    with pytest.raises(MonthSchedulingContractError) as missing_source:
        _calculate(acid, config_path)
    assert _issue_code(missing_source) == "missing_production_rate_source"

    acid["rows"][0]["production_rate_source"] = "fixed_xqpt"
    acid["rows"][0]["furnace_speed_mpm"] = Decimal("100")
    with pytest.raises(MonthSchedulingContractError) as wrong_unit:
        _calculate(acid, config_path)
    assert _issue_code(wrong_unit) == "invalid_speed_unit"

    duplicate = _latest_request(_latest_row("same"), _latest_row("same", node_sequence=2))
    with pytest.raises(MonthSchedulingContractError) as duplicate_node:
        month_plan_auxiliary.loads_calculate_latest_dates_request(dumps_exact_json(duplicate))
    assert _issue_code(duplicate_node) == "duplicate_node_id"

    broken = _latest_request(
        _latest_row("A", chain_id="CHAIN-1"),
        _latest_row("B", chain_id="CHAIN-2", chain_sequence=2),
        _latest_row("C", chain_id="CHAIN-1", node_sequence=2),
    )
    with pytest.raises(MonthSchedulingContractError) as non_contiguous:
        month_plan_auxiliary.loads_calculate_latest_dates_request(dumps_exact_json(broken))
    assert _issue_code(non_contiguous) == "non_contiguous_chain_nodes"


def test_latest_dates_configuration_failure_is_explicit(tmp_path):
    with pytest.raises(month_plan_auxiliary.MonthPlanDateConfigurationError):
        _calculate(_latest_request(_latest_row("A")), tmp_path / "missing.json")


@pytest.fixture
def date_client(tmp_path):
    with TestClient(
        http_module.create_app(
            tmp_path / "unused.sqlite3",
            month_plan_dates_path=_date_config(tmp_path),
        ),
        raise_server_exceptions=False,
    ) as value:
        yield value


def _post_dates(client, value, *, content_type="application/json", headers=None):
    request_headers = {"content-type": content_type}
    request_headers.update(headers or {})
    return client.post(
        DATE_PATH,
        content=dumps_exact_json(value).encode("utf-8"),
        headers=request_headers,
    )


def test_latest_dates_http_success_has_exact_contract(date_client):
    response = _post_dates(date_client, _latest_request(_latest_row("A")))

    assert response.status_code == 200
    data = json.loads(response.content.decode("utf-8"))
    assert set(data) == {
        "contract_version",
        "request_id",
        "status",
        "product_line_code",
        "current_process",
        "anchor_process",
        "time_zone",
        "calc_date",
        "solve_reference",
        "lead_days_snapshot",
        "rows",
        "warnings",
    }
    assert data["request_id"] == REQUEST_ID
    assert data["solve_reference"]["solve_request_id"] == SOLVE_REQUEST_ID
    assert data["rows"][0]["latest_dates"]["coating"].endswith("+08:00")


def test_latest_dates_http_maps_media_size_shape_input_and_config_errors(tmp_path, date_client):
    wrong_type = date_client.post(DATE_PATH, content=b"{}", headers={"content-type": "text/plain"})
    assert wrong_type.status_code == 415

    too_large = date_client.post(
        DATE_PATH,
        content=b"{}",
        headers={
            "content-type": "application/json",
            "content-length": str(http_module.MAX_MONTH_LATEST_DATES_REQUEST_BODY_BYTES + 1),
        },
    )
    assert too_large.status_code == 413

    shape = _latest_request(_latest_row("A"))
    shape["unexpected"] = True
    assert _post_dates(date_client, shape).status_code == 400

    invalid = _latest_request(_latest_row("A", assigned_period="BR_99999999"))
    response = _post_dates(date_client, invalid)
    assert response.status_code == 422
    assert json.loads(response.content.decode("utf-8"))["error"]["issues"][0]["code"] == "unknown_assigned_period"

    with TestClient(
        http_module.create_app(
            tmp_path / "unused.sqlite3",
            month_plan_dates_path=tmp_path / "missing.json",
        ),
        raise_server_exceptions=False,
    ) as unavailable:
        response = _post_dates(unavailable, _latest_request(_latest_row("A")))
    assert response.status_code == 503
    assert json.loads(response.content.decode("utf-8"))["error"]["code"] == "date_configuration_unavailable"
