import json
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event

import pytest
from fastapi.testclient import TestClient

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_v7_service import app as http_module
from apsgo_v7_service import scheduling as scheduling_module
from apsgo_v7_service.rule_management import (
    RuleManagementServiceError,
    initialize_gqga4_rules,
)
from tests.service.grade_dictionary_support import sample_grade_dictionary
from tests.service.test_month_scheduling import order, policy, request_data

SOLVE_PATH = http_module.MONTH_SOLVE_PATH
SAVE_PATH = http_module.SET_ACTIVE_RULES_PATH


def _decode(response):
    return json.loads(response.content.decode("utf-8"), parse_float=Decimal)


def _post(client, value, *, content_type="application/json", headers=None):
    request_headers = {"content-type": content_type}
    request_headers.update(headers or {})
    return client.post(
        SOLVE_PATH,
        content=dumps_exact_json(value).encode("utf-8"),
        headers=request_headers,
    )


def _save_request(active):
    return {
        "save_operation_id": "00000000-0000-4000-8000-000000000951",
        "expected_active_version_id": active["active_version_id"],
        "rules": [
            {
                "rule_id": item["rule_id"],
                "enabled": item["enabled"],
                "parameters": item["parameters"],
            }
            for item in active["rules"]
        ],
        "virtual_prototypes": active["virtual_prototypes"],
        "remark": "求解运行期间保存的新版本",
    }


@pytest.fixture
def database_path(tmp_path):
    path = tmp_path / "rules.sqlite3"
    initialize_gqga4_rules(path, initial_grade_dictionary=sample_grade_dictionary())
    return path


@pytest.fixture
def application(database_path):
    return http_module.create_app(
        database_path,
        monthly_solve_policy=policy(),
    )


@pytest.fixture
def client(application):
    with TestClient(application, raise_server_exceptions=False) as value:
        yield value


def test_month_solve_http_returns_exact_publishable_rows(client):
    response = _post(client, request_data())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    data = _decode(response)
    assert data["status"] == "success"
    assert data["publishable"] is True
    assert data["active_rule_set_version_id"] == 1
    assert [row["source_order_id"] for row in data["rows"]] == ["order-1", "order-2"]
    assert data["rows"][0]["weight"] == Decimal("600.125")
    assert data["rows"][0]["width"] == Decimal("1000.25")
    assert data["violations"] == []
    assert data["audit_summary"]["passed"] is True


def test_stale_active_version_returns_409_before_search(client, monkeypatch):
    solve_calls = []
    monkeypatch.setattr(
        scheduling_module,
        "solve_request",
        lambda *args: solve_calls.append(args),
    )

    response = _post(client, request_data(expected_active_version_id=2))

    assert response.status_code == 409
    error = _decode(response)["error"]
    assert error["code"] == "active_version_conflict"
    assert error["request_id"] == request_data()["request_id"]
    assert error["expected_active_version_id"] == 2
    assert error["current_active_version_id"] == 1
    assert solve_calls == []


def test_solve_route_has_independent_media_type_and_body_size_limits(client):
    wrong_type = client.post(SOLVE_PATH, content=b"{}", headers={"content-type": "text/plain"})
    assert wrong_type.status_code == 415
    assert _decode(wrong_type)["error"]["code"] == "invalid_content_type"

    header_too_large = client.post(
        SOLVE_PATH,
        content=b"{}",
        headers={
            "content-type": "application/json",
            "content-length": str(http_module.MAX_MONTH_SOLVE_REQUEST_BODY_BYTES + 1),
        },
    )
    assert header_too_large.status_code == 413
    assert _decode(header_too_large)["error"]["code"] == "request_too_large"

    streamed_too_large = client.post(
        SOLVE_PATH,
        content=b" " * (http_module.MAX_MONTH_SOLVE_REQUEST_BODY_BYTES + 1),
        headers={"content-type": "application/json", "content-length": "1"},
    )
    assert streamed_too_large.status_code == 413
    assert _decode(streamed_too_large)["error"]["code"] == "request_too_large"


def test_contract_and_core_input_errors_keep_their_distinct_422_bodies(client):
    contract_response = _post(client, request_data(orders=[]))
    assert contract_response.status_code == 422
    contract_error = _decode(contract_response)["error"]
    assert contract_error["code"] == "scheduling_input_invalid"
    assert contract_error["issues"][0]["field_path"] == "orders"

    core_response = _post(client, request_data(orders=[order(width=None)]))
    assert core_response.status_code == 422
    core = _decode(core_response)
    assert (core["status"], core["stop_reason"], core["publishable"]) == (
        "failed",
        "input_invalid",
        False,
    )
    assert core["rows"] == [] and core["violations"] == []
    assert core["issues"]


def test_missing_store_and_unavailable_dictionary_are_sanitized_503(tmp_path, monkeypatch):
    missing = tmp_path / "missing.sqlite3"
    with TestClient(
        http_module.create_app(missing, monthly_solve_policy=policy()),
        raise_server_exceptions=False,
    ) as missing_client:
        response = _post(missing_client, request_data())
    assert response.status_code == 503
    assert _decode(response)["error"]["code"] == "rule_store_unavailable"
    assert str(missing) not in response.text

    def unavailable(*args, **kwargs):
        raise RuleManagementServiceError(
            "grade_dictionary_unavailable",
            "private detail",
            current_active_version_id=1,
        )

    monkeypatch.setattr(http_module, "solve_gqga4_scheduling_task", unavailable)
    with TestClient(
        http_module.create_app(tmp_path / "unused.sqlite3", monthly_solve_policy=policy()),
        raise_server_exceptions=False,
    ) as unavailable_client:
        response = _post(unavailable_client, request_data())
    assert response.status_code == 503
    error = _decode(response)["error"]
    assert error["code"] == "grade_dictionary_unavailable"
    assert "private detail" not in response.text


def test_one_running_solve_rejects_the_second_but_rules_remain_available(
    application,
    monkeypatch,
):
    entered = Event()
    release = Event()
    original_solve = scheduling_module.solve_request

    def paused_solve(request, cancellation=None):
        entered.set()
        assert release.wait(10)
        return original_solve(request, cancellation)

    monkeypatch.setattr(scheduling_module, "solve_request", paused_solve)

    with (
        TestClient(application, raise_server_exceptions=False) as first_client,
        TestClient(application, raise_server_exceptions=False) as second_client,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        first_future = pool.submit(_post, first_client, request_data())
        assert entered.wait(10)

        busy = _post(second_client, request_data())
        assert busy.status_code == 429
        assert _decode(busy)["error"]["code"] == "scheduling_busy"

        active = _decode(second_client.get(http_module.GET_ACTIVE_RULES_PATH))
        saved = second_client.post(
            SAVE_PATH,
            content=dumps_exact_json(_save_request(active)).encode("utf-8"),
            headers={"content-type": "application/json"},
        )
        assert saved.status_code == 200
        assert _decode(saved)["active_version_id"] == 2

        release.set()
        first = first_future.result(timeout=10)

    assert first.status_code == 200
    assert _decode(first)["active_rule_set_version_id"] == 1


def test_request_disconnect_sets_solver_cancellation(client, monkeypatch):
    observed = Event()
    original_solve = scheduling_module.solve_request

    async def disconnected(request):
        return request.url.path == SOLVE_PATH

    def wait_for_cancellation(request, cancellation=None):
        deadline = time.monotonic() + 5
        while not cancellation.is_cancelled() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cancellation.is_cancelled()
        observed.set()
        return original_solve(request, cancellation)

    monkeypatch.setattr(http_module.Request, "is_disconnected", disconnected)
    monkeypatch.setattr(scheduling_module, "solve_request", wait_for_cancellation)

    response = _post(client, request_data())

    assert observed.is_set()
    assert response.status_code == 200
    data = _decode(response)
    assert (data["status"], data["stop_reason"], data["publishable"]) == (
        "cancelled",
        "user_cancelled",
        False,
    )
    assert data["rows"] == []


def test_worker_failure_is_sanitized_and_releases_the_solve_slot(
    application,
    monkeypatch,
):
    original = http_module.solve_gqga4_scheduling_task
    monkeypatch.setattr(
        http_module,
        "solve_gqga4_scheduling_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("private failure")),
    )

    with TestClient(application, raise_server_exceptions=False) as value:
        failed = _post(value, request_data())
        assert failed.status_code == 500
        assert _decode(failed)["error"]["code"] == "internal_server_error"
        assert "private failure" not in failed.text

        monkeypatch.setattr(http_module, "solve_gqga4_scheduling_task", original)
        recovered = _post(value, request_data())

    assert recovered.status_code == 200
    assert _decode(recovered)["publishable"] is True


def test_result_mapping_failure_uses_its_public_code_without_private_detail(
    client,
    monkeypatch,
):
    def fail_mapping(*args):
        raise http_module.MonthSchedulingMappingError("private mapping detail")

    monkeypatch.setattr(http_module, "dumps_month_solve_response", fail_mapping)

    response = _post(client, request_data())

    assert response.status_code == 500
    assert _decode(response)["error"]["code"] == "result_mapping_failed"
    assert "private mapping detail" not in response.text


def test_month_solve_is_not_available_without_a_loaded_policy(database_path):
    with TestClient(
        http_module.create_app(database_path),
        raise_server_exceptions=False,
    ) as value:
        response = _post(value, request_data())

    assert response.status_code == 503
    assert _decode(response)["error"]["code"] == "monthly_solve_not_configured"
