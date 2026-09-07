import asyncio
import json
import logging
import sqlite3
from copy import deepcopy
from dataclasses import fields
from decimal import Decimal

import pytest
import uvicorn
import yaml
from fastapi.testclient import TestClient

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_scheduler.api.rule_management import (
    MAX_RULE_MANAGEMENT_ARRAY_LENGTH,
    MAX_RULE_MANAGEMENT_STRING_LENGTH,
)
from apsgo_v7_service import app as http_module
from apsgo_v7_service import rule_management as service_module
from apsgo_v7_service.gqga4 import (
    GQGA4_INITIAL_RULES,
    GQGA4_INITIAL_VIRTUAL_PROTOTYPES,
    compile_gqga4_rule_set,
    normalize_gqga4_rule_snapshot,
)
from apsgo_v7_service.rule_store import RuleStore
from tests.service.grade_dictionary_support import sample_grade_dictionary

GET_PATH = "/api/v1/rule-sets/GQGA4/default/month/getActiveRules"
POST_PATH = "/api/v1/rule-sets/GQGA4/default/month/setActiveRules"
OPERATION_A = "00000000-0000-4000-8000-000000000201"
OPERATION_B = "00000000-0000-4000-8000-000000000202"
STAMP = "2026-09-07T00:00:00.000000+00:00"
ERROR_FIELDS = {
    "code",
    "message",
    "expected_active_version_id",
    "current_active_version_id",
    "issues",
}


def _write_service_configuration(
    configuration_path,
    database_path,
    *,
    host="127.0.0.1",
    port=8001,
    timeout_seconds=5.0,
):
    configuration_path.parent.mkdir(parents=True, exist_ok=True)
    configuration_path.write_text(
        yaml.safe_dump(
            {
                "database_path": str(database_path),
                "database_timeout_seconds": timeout_seconds,
                "listen_host": host,
                "listen_port": port,
                "monthly_solve": {
                    "seed": 590531,
                    "total_time_limit_seconds": 180,
                    "finalization_reserve_seconds": 10,
                    "candidate_check_limit": 200000,
                    "whole_chain_pair_scan_slack_weight": 40,
                    "maximum_virtual_bridge_nodes": 2,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return configuration_path


def _record_data(value):
    return {part.name: getattr(value, part.name) for part in fields(value)}


def _seed_active_v1(database_path):
    rules, prototypes = normalize_gqga4_rule_snapshot(
        GQGA4_INITIAL_RULES, GQGA4_INITIAL_VIRTUAL_PROTOTYPES
    )
    compiled = compile_gqga4_rule_set(rules, 1)
    dictionary = sample_grade_dictionary()
    with RuleStore.initialize(database_path) as store:
        with store.transaction(write=True):
            rule_set_id = store.create_rule_set("GQGA4", "default", "month", created_at=STAMP)
            version_id = store.create_version(
                rule_set_id,
                1,
                None,
                "bootstrap:gqga4:v1",
                service_module._request_hash(None, rules, prototypes, ""),
                compiled.compiled_rule_set_json,
                compiled.rule_set_spec.fingerprint,
                service_module._virtual_prototypes_json(prototypes),
                service_module._editor_snapshot_json(rules, prototypes, ""),
                "",
                "bootstrap",
                STAMP,
                "bootstrap",
                STAMP,
                grade_dictionary_fingerprint=dictionary.dictionary_fingerprint,
            )
            for sequence_no, rule in enumerate(compiled.rule_set_spec.rules, start=1):
                store.create_rule_definition(
                    version_id,
                    sequence_no,
                    rule.rule_id,
                    rule.rule_type,
                    rule.name,
                    rule.scope.value,
                    rule.enabled,
                    rule.version,
                    dumps_exact_json(rule.parameters),
                )
            service_module._create_grade_dictionary_entries(
                store, version_id, dictionary
            )
            store.activate_version(rule_set_id, version_id, None, updated_at=STAMP)
    return rule_set_id, version_id


def _initial_request(operation_id=OPERATION_A, expected_version_id=1):
    return {
        "save_operation_id": operation_id,
        "expected_active_version_id": expected_version_id,
        "rules": [
            {
                "rule_id": rule.rule_id,
                "enabled": rule.enabled,
                "parameters": rule.parameters,
            }
            for rule in GQGA4_INITIAL_RULES
        ],
        "virtual_prototypes": [
            _record_data(prototype) for prototype in GQGA4_INITIAL_VIRTUAL_PROTOTYPES
        ],
        "remark": "HTTP 测试保存",
    }


def _request_from_active(active, operation_id=OPERATION_A):
    return {
        "save_operation_id": operation_id,
        "expected_active_version_id": active["active_version_id"],
        "rules": [
            {
                "rule_id": rule["rule_id"],
                "enabled": rule["enabled"],
                "parameters": rule["parameters"],
            }
            for rule in active["rules"]
        ],
        "virtual_prototypes": active["virtual_prototypes"],
        "remark": "HTTP 测试保存",
    }


def _json(response):
    return json.loads(response.content.decode("utf-8"), parse_float=Decimal)


def _post(client, value, *, content_type="application/json"):
    headers = {} if content_type is None else {"content-type": content_type}
    return client.post(
        POST_PATH,
        content=dumps_exact_json(value).encode("utf-8"),
        headers=headers,
    )


def _assert_error(
    response,
    status_code,
    *,
    code=None,
    issue_code=None,
    field_path=None,
    expected_version_id=None,
    current_version_id=None,
):
    assert response.status_code == status_code
    assert response.headers["content-type"].startswith("application/json")
    body = _json(response)
    assert set(body) == {"error"}
    error = body["error"]
    assert set(error) == ERROR_FIELDS
    assert isinstance(error["code"], str) and error["code"]
    assert isinstance(error["message"], str) and error["message"]
    assert error["expected_active_version_id"] == expected_version_id
    assert error["current_active_version_id"] == current_version_id
    assert isinstance(error["issues"], list)
    if code is not None:
        assert error["code"] == code
    if issue_code is not None:
        assert error["issues"]
        assert error["issues"][0]["code"] == issue_code
    if field_path is not None:
        assert error["issues"]
        assert error["issues"][0]["field_path"] == field_path
    for issue in error["issues"]:
        assert set(issue) == {
            "code",
            "phase",
            "field_path",
            "subject_id",
            "message",
            "severity",
        }
    assert "detail" not in body
    return error


def _database_state(database_path):
    with sqlite3.connect(database_path) as connection:
        active = connection.execute(
            "SELECT active_version_id FROM v7_rule_set WHERE product_line_code = 'GQGA4'"
        ).fetchone()
        return {
            "versions": connection.execute("SELECT COUNT(*) FROM v7_rule_set_version").fetchone()[
                0
            ],
            "rules": connection.execute("SELECT COUNT(*) FROM v7_rule_definition").fetchone()[0],
            "active": None if active is None else active[0],
        }


@pytest.fixture
def database_path(tmp_path):
    path = tmp_path / "rules.sqlite3"
    _seed_active_v1(path)
    return path


@pytest.fixture
def client(database_path):
    with TestClient(
        http_module.create_app(database_path=database_path), raise_server_exceptions=False
    ) as value:
        yield value


def test_http_surface_contains_the_three_confirmed_operations(database_path):
    application = http_module.create_app(database_path=database_path)
    assert http_module.MAX_REQUEST_BODY_BYTES == 262_144
    schema = application.openapi()
    assert set(schema["paths"]) == {GET_PATH, POST_PATH, http_module.MONTH_SOLVE_PATH}
    assert set(schema["paths"][GET_PATH]) == {"get"}
    assert set(schema["paths"][POST_PATH]) == {"post"}
    assert set(schema["paths"][http_module.MONTH_SOLVE_PATH]) == {"post"}
    writes = {
        (path, method)
        for path, operations in schema["paths"].items()
        for method in operations
        if method in {"post", "put", "patch", "delete"}
    }
    assert writes == {
        (POST_PATH, "post"),
        (http_module.MONTH_SOLVE_PATH, "post"),
    }

    with TestClient(application, raise_server_exceptions=False) as value:
        wrong_get = value.post(GET_PATH, content=b"{}")
        _assert_error(wrong_get, 405, code="method_not_allowed")
        assert wrong_get.headers["allow"] == "GET"
        wrong_post = value.get(POST_PATH)
        _assert_error(wrong_post, 405, code="method_not_allowed")
        assert wrong_post.headers["allow"] == "POST"
        wrong_solve = value.get(http_module.MONTH_SOLVE_PATH)
        _assert_error(wrong_solve, 405, code="method_not_allowed")
        assert wrong_solve.headers["allow"] == "POST"
        assert value.get(f"{GET_PATH}/").status_code == 404
        assert value.get(GET_PATH.replace("getActiveRules", "getactiverules")).status_code == 404


@pytest.mark.parametrize("kind", ("body", "query"))
def test_get_rejects_unplanned_input(client, kind):
    if kind == "body":
        response = client.request(
            "GET", GET_PATH, content=b"{}", headers={"content-type": "application/json"}
        )
    else:
        response = client.get(f"{GET_PATH}?unexpected=true")

    _assert_error(response, 400)


@pytest.mark.parametrize("content_type", (None, "text/plain", "application/x-www-form-urlencoded"))
def test_post_accepts_only_json(client, database_path, content_type):
    before = _database_state(database_path)
    response = _post(client, _initial_request(), content_type=content_type)

    _assert_error(response, 400)
    assert _database_state(database_path) == before


def test_post_rejects_oversized_body_before_writing(client, database_path):
    before = _database_state(database_path)
    response = client.post(
        POST_PATH,
        content=b" " * (http_module.MAX_REQUEST_BODY_BYTES + 1),
        headers={"content-type": "application/json", "content-length": "1"},
    )

    _assert_error(response, 400, code="request_too_large")
    assert _database_state(database_path) == before

    boundary = client.post(
        POST_PATH,
        content=b" " * http_module.MAX_REQUEST_BODY_BYTES,
        headers={"content-type": "application/json"},
    )
    _assert_error(boundary, 400, code="invalid_request", issue_code="invalid_json")
    assert _database_state(database_path) == before


@pytest.mark.parametrize(
    ("kind", "issue_code"),
    (
        ("string", "maximum_string_length_exceeded"),
        ("array", "maximum_array_length_exceeded"),
    ),
)
def test_http_maps_parser_size_limits_to_400(client, database_path, kind, issue_code):
    value = _initial_request()
    if kind == "string":
        value["remark"] = "a" * (MAX_RULE_MANAGEMENT_STRING_LENGTH + 1)
    else:
        value["rules"][0]["parameters"] = dict(value["rules"][0]["parameters"])
        value["rules"][0]["parameters"]["oversized"] = list(
            range(MAX_RULE_MANAGEMENT_ARRAY_LENGTH + 1)
        )
    before = _database_state(database_path)

    response = _post(client, value)

    _assert_error(response, 400, code="invalid_request", issue_code=issue_code)
    assert _database_state(database_path) == before


@pytest.mark.parametrize(
    ("payload", "issue_code"),
    (
        (b"{", "invalid_json"),
        (b'{"value":NaN}', "invalid_json"),
        (b'{"value":1,"value":2}', "duplicate_json_key"),
        (b'{"value":1e999999999999999999999999999999999999}', "invalid_json"),
        (b"\xff", "invalid_json"),
    ),
)
def test_malformed_json_returns_locatable_400(client, database_path, payload, issue_code):
    before = _database_state(database_path)
    response = client.post(POST_PATH, content=payload, headers={"content-type": "application/json"})

    _assert_error(response, 400, issue_code=issue_code, field_path="body")
    assert _database_state(database_path) == before


def test_valid_non_utf8_json_is_rejected(client, database_path):
    before = _database_state(database_path)
    payload = dumps_exact_json(_initial_request()).encode("utf-16")

    response = client.post(POST_PATH, content=payload, headers={"content-type": "application/json"})

    _assert_error(response, 400, code="invalid_request", issue_code="invalid_json")
    assert _database_state(database_path) == before


def test_escaped_lone_unicode_surrogate_is_a_locatable_400(client, database_path):
    before = _database_state(database_path)
    value = _initial_request()
    value["remark"] = chr(0xD800)

    response = _post(client, value)

    _assert_error(
        response,
        400,
        code="invalid_request",
        issue_code="invalid_unicode_scalar",
        field_path="remark",
    )
    assert _database_state(database_path) == before


@pytest.mark.parametrize("case", ("missing", "unknown", "type"))
def test_basic_request_shape_errors_are_400(client, database_path, case):
    value = _initial_request()
    if case == "missing":
        del value["rules"]
        issue_code, path = "missing_field", "rules"
    elif case == "unknown":
        value["unexpected"] = True
        issue_code, path = "unknown_field", "unexpected"
    else:
        value["expected_active_version_id"] = "1"
        issue_code, path = "invalid_field_type", "expected_active_version_id"
    before = _database_state(database_path)

    response = _post(client, value)

    _assert_error(response, 400, issue_code=issue_code, field_path=path)
    assert _database_state(database_path) == before


@pytest.mark.parametrize("case", ("missing", "duplicate", "unknown", "parameter"))
def test_rule_set_validation_errors_are_422_with_field_diagnostics(client, database_path, case):
    active = _json(client.get(GET_PATH))
    value = _request_from_active(active)
    if case == "missing":
        value["rules"] = value["rules"][:-1]
        issue_code, path = "missing_rule_id", "rules"
    elif case == "duplicate":
        value["rules"].insert(1, deepcopy(value["rules"][0]))
        issue_code, path = "duplicate_identity", "rules[1].rule_id"
    elif case == "unknown":
        value["rules"][0]["rule_id"] = "unknown-rule"
        issue_code, path = "unknown_rule_id", "rules[0].rule_id"
    else:
        value["rules"][0]["parameters"]["min_weight"] = Decimal("2001")
        issue_code, path = (
            "invalid_rule_parameters",
            "rule_set_spec.rules[0].parameters",
        )
    before = _database_state(database_path)

    response = _post(client, value)

    _assert_error(
        response,
        422,
        code="rule_set_validation_failed",
        issue_code=issue_code,
        field_path=path,
    )
    assert _database_state(database_path) == before


def test_uninitialized_rule_set_maps_get_and_post_to_404(tmp_path):
    missing_rule_set = tmp_path / "missing-rule-set.sqlite3"
    with RuleStore.initialize(missing_rule_set):
        pass
    with TestClient(
        http_module.create_app(database_path=missing_rule_set), raise_server_exceptions=False
    ) as value:
        _assert_error(value.get(GET_PATH), 404, code="rule_set_not_initialized")

    inactive_rule_set = tmp_path / "inactive-rule-set.sqlite3"
    with RuleStore.initialize(inactive_rule_set) as store:
        with store.transaction(write=True):
            store.create_rule_set("GQGA4", "default", "month", created_at=STAMP)
    with TestClient(
        http_module.create_app(database_path=inactive_rule_set), raise_server_exceptions=False
    ) as value:
        _assert_error(_post(value, _initial_request()), 404, code="rule_set_not_initialized")
    assert _database_state(inactive_rule_set) == {
        "versions": 0,
        "rules": 0,
        "active": None,
    }


def test_http_conflicts_preserve_the_first_saved_version(client, database_path):
    active = _json(client.get(GET_PATH))
    original = _request_from_active(active)
    saved = _post(client, original)
    assert saved.status_code == 200
    saved_body = _json(saved)
    expected_state = _database_state(database_path)

    changed_retry = deepcopy(original)
    changed_retry["remark"] = "同一操作的不同内容"
    operation_conflict = _post(client, changed_retry)
    _assert_error(
        operation_conflict,
        409,
        code="operation_payload_conflict",
        expected_version_id=active["active_version_id"],
        current_version_id=saved_body["active_version_id"],
    )

    stale = deepcopy(original)
    stale["save_operation_id"] = OPERATION_B
    active_conflict = _post(client, stale)
    _assert_error(
        active_conflict,
        409,
        code="active_version_conflict",
        expected_version_id=active["active_version_id"],
        current_version_id=saved_body["active_version_id"],
    )
    assert _database_state(database_path) == expected_state


def test_http_idempotent_replay_never_reactivates_a_historical_version(client, database_path):
    active = _json(client.get(GET_PATH))
    first_request = _request_from_active(active, OPERATION_A)
    first = _json(_post(client, first_request))

    immediate_replay = _json(_post(client, first_request))
    assert immediate_replay["saved_version_id"] == first["saved_version_id"]
    assert immediate_replay["saved_version_is_active"] is True
    assert immediate_replay["idempotent_replay"] is True
    assert _database_state(database_path)["versions"] == 2

    next_request = _request_from_active(first, OPERATION_B)
    next_request["remark"] = "后续版本"
    latest = _json(_post(client, next_request))
    historical_replay = _json(_post(client, first_request))

    assert historical_replay["active_version_id"] == latest["active_version_id"]
    assert historical_replay["saved_version_id"] == first["saved_version_id"]
    assert historical_replay["saved_version_is_active"] is False
    assert historical_replay["idempotent_replay"] is True
    assert _database_state(database_path) == {
        "versions": 3,
        "rules": 51,
        "active": latest["active_version_id"],
    }


def test_real_write_lock_maps_to_retryable_503(database_path):
    active = _json(TestClient(http_module.create_app(database_path=database_path)).get(GET_PATH))
    lock = sqlite3.connect(database_path, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    try:
        with TestClient(
            http_module.create_app(database_path=database_path, timeout_seconds=0.01),
            raise_server_exceptions=False,
        ) as value:
            response = _post(value, _request_from_active(active))
    finally:
        lock.rollback()
        lock.close()

    _assert_error(response, 503, code="rule_store_unavailable")
    assert _database_state(database_path) == {"versions": 1, "rules": 17, "active": 1}


def test_permanent_sqlite_operational_error_maps_to_sanitized_500(client, monkeypatch):
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: private_internal_table")

    monkeypatch.setattr(http_module, "get_active_gqga4_rules", fail)

    response = client.get(GET_PATH)

    _assert_error(response, 500, code="rule_store_failure")
    assert "private_internal_table" not in response.text
    assert "no such table" not in response.text


def test_database_write_failure_is_500_without_internal_details(client, database_path, caplog):
    active = _json(client.get(GET_PATH))
    before = _database_state(database_path)
    secret = "forced SELECT secret FROM v7_rule_set at /Users/private/rules.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            f"""
            CREATE TRIGGER test_http_internal_failure
            BEFORE INSERT ON v7_rule_set_version
            BEGIN SELECT RAISE(ABORT, '{secret}'); END
            """
        )

    with caplog.at_level(logging.INFO, logger=http_module._LOGGER.name):
        response = _post(client, _request_from_active(active))

    _assert_error(response, 500)
    assert _database_state(database_path) == before
    for fragment in (secret, "SELECT", "/Users/", "Traceback", "sqlite3"):
        assert fragment not in response.text
        assert fragment not in "\n".join(record.getMessage() for record in caplog.records)
    log = "\n".join(record.getMessage() for record in caplog.records)
    assert f"operation_id={OPERATION_A}" in log
    assert "previous_version_id=1" in log
    assert "status=500" in log
    assert "HTTP 测试保存" not in log


def test_incompatible_database_schema_is_a_sanitized_500(tmp_path):
    database_path = tmp_path / "private-schema.sqlite3"
    with RuleStore.initialize(database_path):
        pass
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 3")

    with TestClient(
        http_module.create_app(database_path=database_path), raise_server_exceptions=False
    ) as value:
        response = value.get(GET_PATH)

    error = _assert_error(response, 500, code="internal_server_error")
    assert error["issues"] == []
    for fragment in (str(database_path), "schema", "version 3", "RuleStoreSchemaError"):
        assert fragment not in response.text


def test_missing_database_is_503_without_leaking_its_path(tmp_path):
    database_path = tmp_path / "private-rules.sqlite3"
    with TestClient(
        http_module.create_app(database_path=database_path), raise_server_exceptions=False
    ) as value:
        response = value.get(GET_PATH)

    _assert_error(response, 503)
    assert str(database_path) not in response.text
    assert "FileNotFoundError" not in response.text


def test_get_post_get_closure_preserves_decimal_and_database_identity(client, database_path):
    first_response = client.get(GET_PATH)
    assert first_response.status_code == 200
    assert first_response.headers["content-type"].startswith("application/json")
    first = _json(first_response)
    assert len(first["rules"]) == 17
    assert len(first["quality_spec"]) == 7
    assert len(first["virtual_prototypes"]) == 27
    request = _request_from_active(first)
    precise_minimum = Decimal("700.123456789012345678901234567890123456789")
    large_weight = Decimal("1e10000")
    request["rules"][0]["parameters"]["min_weight"] = precise_minimum
    request["virtual_prototypes"][0]["unit_weight"] = large_weight

    saved_response = _post(client, request, content_type="application/json; charset=utf-8")
    assert saved_response.status_code == 200
    saved = _json(saved_response)
    current_response = client.get(GET_PATH)
    assert current_response.status_code == 200
    current = _json(current_response)

    assert saved["rules"][0]["parameters"]["min_weight"] == precise_minimum
    assert saved["virtual_prototypes"][0]["unit_weight"] == large_weight
    assert current["rules"][0]["parameters"]["min_weight"] == precise_minimum
    assert current["virtual_prototypes"][0]["unit_weight"] == large_weight
    assert "700.123456789012345678901234567890123456789" in saved_response.text
    assert "1e10000" in saved_response.text
    assert saved["previous_active_version_id"] == first["active_version_id"]
    assert saved["saved_version_id"] == saved["active_version_id"]
    assert saved["saved_version_is_active"] is True
    assert saved["idempotent_replay"] is False
    active_keys = set(current)
    assert {key: saved[key] for key in active_keys} == current

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT s.active_version_id, v.version_no, v.rule_set_fingerprint,
                   COUNT(d.id) AS rule_count
            FROM v7_rule_set AS s
            JOIN v7_rule_set_version AS v ON v.id = s.active_version_id
            JOIN v7_rule_definition AS d ON d.rule_set_version_id = v.id
            WHERE s.product_line_code = 'GQGA4'
            GROUP BY s.active_version_id, v.version_no, v.rule_set_fingerprint
            """
        ).fetchone()
    assert row == (
        current["active_version_id"],
        current["version_no"],
        current["fingerprint"],
        17,
    )


def test_service_work_runs_off_the_event_loop_thread(client, monkeypatch):
    observed_running_loops = []
    original = http_module.get_active_gqga4_rules

    def wrapped(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            observed_running_loops.append(False)
        else:
            observed_running_loops.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(http_module, "get_active_gqga4_rules", wrapped)

    assert client.get(GET_PATH).status_code == 200
    assert observed_running_loops == [False]


def test_success_log_keeps_audit_fields_but_not_body_or_database_path(
    client, database_path, caplog
):
    active = _json(client.get(GET_PATH))
    request = _request_from_active(active)
    request["remark"] = "BODY_SECRET_MUST_NOT_BE_LOGGED"

    with caplog.at_level(logging.INFO, logger=http_module._LOGGER.name):
        response = _post(client, request)

    assert response.status_code == 200
    saved = _json(response)
    log = "\n".join(record.getMessage() for record in caplog.records)
    assert f"operation_id={OPERATION_A}" in log
    assert f"previous_version_id={active['active_version_id']}" in log
    assert f"active_version_id={saved['active_version_id']}" in log
    assert f"fingerprint={saved['fingerprint']}" in log
    assert f"saved_version_id={saved['saved_version_id']}" in log
    assert "actor=v7-rule-service result_code=saved_and_activated" in log
    assert "idempotent_replay=False status=200" in log
    assert "BODY_SECRET_MUST_NOT_BE_LOGGED" not in log
    assert str(database_path) not in log


def test_run_server_loads_one_configuration_and_uses_the_confirmed_endpoint(tmp_path, monkeypatch):
    database_path = tmp_path / "data" / "rules.sqlite3"
    configuration_path = _write_service_configuration(
        tmp_path / "config" / "service.yaml",
        "../data/rules.sqlite3",
        host="0.0.0.0",
        port=8123,
        timeout_seconds=2.5,
    )
    calls = []
    applications = []

    def create_app(database_path, *, timeout_seconds, monthly_solve_policy):
        applications.append((database_path, timeout_seconds, monthly_solve_policy))
        return object()

    monkeypatch.setattr(http_module, "create_app", create_app)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    http_module.run_server(configuration_path)

    assert not hasattr(http_module, "app")
    assert http_module._LOGGER.name.startswith("uvicorn.error.")
    assert len(applications) == 1
    assert applications[0][:2] == (database_path.resolve(), 2.5)
    assert applications[0][2].seed == 590531
    assert applications[0][2].candidate_check_limit == 200000
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs.get("host", args[1] if len(args) > 1 else None) == "0.0.0.0"
    assert kwargs.get("port", args[2] if len(args) > 2 else None) == 8123
    assert kwargs["access_log"] is False
    assert kwargs["workers"] == 1


def test_run_server_rejects_missing_configuration_before_start(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    with pytest.raises(ValueError, match="configuration file does not exist"):
        http_module.run_server(tmp_path / "missing.yaml")

    assert calls == []
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize(
    "host",
    ("192.168.1.10", "::", "::1", "localhost", "127.0.0.2"),
)
def test_run_server_rejects_every_unsupported_host_before_start(tmp_path, monkeypatch, host):
    configuration_path = _write_service_configuration(
        tmp_path / "config.yaml",
        tmp_path / "rules.sqlite3",
        host=host,
    )
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    with pytest.raises(ValueError, match="127.0.0.1 or 0.0.0.0"):
        http_module.run_server(configuration_path)

    assert calls == []


def test_service_command_accepts_an_explicit_configuration_path(tmp_path, monkeypatch):
    configuration_path = tmp_path / "service.yaml"
    calls = []
    monkeypatch.setattr(http_module, "run_server", lambda path: calls.append(path))

    http_module.main(["--config", str(configuration_path)])

    assert calls == [configuration_path]
