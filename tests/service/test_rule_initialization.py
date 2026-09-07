import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Event

import pytest

from apsgo_scheduler.api.rule_management import SetActiveRulesRequest
from apsgo_v7_service import rule_management as service_module
from apsgo_v7_service.gqga4 import (
    GQGA4_INITIAL_RULES,
    GQGA4_INITIAL_VIRTUAL_PROTOTYPES,
    compile_gqga4_rule_set,
    normalize_gqga4_rule_snapshot,
)
from apsgo_v7_service.rule_management import (
    INITIALIZATION_OPERATION_ID,
    RuleManagementServiceError,
    get_active_gqga4_rules,
    initialize_gqga4_rules,
    set_active_gqga4_rules,
)
from apsgo_v7_service.rule_store import RuleStore

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 7, 10, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8)))
UTC_STAMP = "2026-09-07T02:00:00.123456+00:00"
EXPECTED_FINGERPRINT = "d963206b01c0d439303c1d6ae7d7374e20eaa0777801d12c0bebc89bfe6349c0"


def _write_service_configuration(configuration_path, database_path):
    configuration_path.parent.mkdir(parents=True, exist_ok=True)
    configuration_path.write_text(
        json.dumps(
            {
                "database_path": str(database_path),
                "database_timeout_seconds": 5.0,
                "listen_host": "127.0.0.1",
                "listen_port": 8001,
            }
        ),
        encoding="utf-8",
    )
    return configuration_path


def _counts(database_path):
    with sqlite3.connect(database_path) as connection:
        return tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("v7_rule_set", "v7_rule_set_version", "v7_rule_definition")
        )


def _database_dump(database_path):
    with sqlite3.connect(database_path) as connection:
        return tuple(connection.iterdump())


def _changed_rules():
    result = []
    for rule in GQGA4_INITIAL_RULES:
        if rule.rule_id == "chain_weight_range":
            parameters = dict(rule.parameters)
            parameters["min_weight"] = Decimal("701")
            rule = replace(rule, parameters=parameters)
        result.append(rule)
    return tuple(result)


def test_empty_database_initializes_one_fully_verified_active_version(tmp_path):
    database_path = tmp_path / "nested" / "rules.sqlite3"

    result = initialize_gqga4_rules(
        database_path,
        audit_actor="  deployment-process  ",
        clock=lambda: NOW,
    )

    active = result.active_rules
    spec = active.rule_set_spec
    assert result.created is True
    assert active == get_active_gqga4_rules(database_path)
    assert active.based_on_version_id is None
    assert active.remark == ""
    assert active.activated_at == UTC_STAMP
    assert active.activated_by == "deployment-process"
    assert (spec.product_line_code, spec.process_code, spec.scenario, spec.version) == (
        "GQGA4",
        "default",
        "month",
        "1",
    )
    assert spec.fingerprint == EXPECTED_FINGERPRINT
    assert len(spec.rules) == 17
    assert sum(rule.enabled for rule in spec.rules) == 16
    assert [rule.rule_id for rule in spec.rules if not rule.enabled] == [
        "forbid_consecutive_reverse_width"
    ]
    assert len(spec.quality_spec) == 7
    assert spec.allowed_final_deviation_codes == frozenset({"chain_weight_below_minimum"})
    assert len(active.virtual_prototypes) == 27
    assert _counts(database_path) == (1, 1, 17)

    with RuleStore.open(database_path) as store:
        version = store.find_version(active.active_version_id)
        definitions = store.list_rule_definitions(active.active_version_id)
    assert version.version_no == 1
    assert version.based_on_version_id is None
    assert version.save_operation_id == INITIALIZATION_OPERATION_ID
    assert version.created_by == version.activated_by == "deployment-process"
    assert version.created_at == version.activated_at == UTC_STAMP
    assert [item.sequence_no for item in definitions] == list(range(1, 18))
    assert "/Users/" not in version.compiled_rule_set_json
    assert "/Users/" not in version.virtual_prototypes_json
    assert "tests/" not in version.compiled_rule_set_json
    assert "tests/" not in version.virtual_prototypes_json


def test_repeated_initialization_only_verifies_and_changes_no_rows(tmp_path):
    database_path = tmp_path / "rules.sqlite3"
    first = initialize_gqga4_rules(database_path, clock=lambda: NOW)
    before = _database_dump(database_path)

    second = initialize_gqga4_rules(
        database_path,
        clock=lambda: (_ for _ in ()).throw(AssertionError("clock must not be called")),
    )

    assert first.created is True
    assert second.created is False
    assert second.active_rules == first.active_rules
    assert _database_dump(database_path) == before


def test_initializer_preserves_a_later_user_version(tmp_path, monkeypatch):
    database_path = tmp_path / "rules.sqlite3"
    first = initialize_gqga4_rules(database_path, clock=lambda: NOW)
    saved = set_active_gqga4_rules(
        SetActiveRulesRequest(
            save_operation_id="00000000-0000-4000-8000-000000000601",
            expected_active_version_id=first.active_rules.active_version_id,
            rules=_changed_rules(),
            virtual_prototypes=GQGA4_INITIAL_VIRTUAL_PROTOTYPES,
            remark="用户启用版本",
        ),
        database_path,
        clock=lambda: NOW,
    )
    before = _database_dump(database_path)
    original_compile = service_module.compile_gqga4_rule_set
    compiled_versions = []

    def record_compile(rules, version_no):
        compiled_versions.append(version_no)
        return original_compile(rules, version_no)

    monkeypatch.setattr(service_module, "compile_gqga4_rule_set", record_compile)

    repeated = initialize_gqga4_rules(database_path)

    assert repeated.created is False
    assert repeated.active_rules == saved.active_rules
    assert repeated.active_rules.rule_set_spec.version == "2"
    assert compiled_versions == [2]
    assert _database_dump(database_path) == before


def test_existing_inactive_identity_is_rejected_without_repair(tmp_path):
    database_path = tmp_path / "rules.sqlite3"
    with RuleStore.initialize(database_path) as store:
        with store.transaction(write=True):
            store.create_rule_set("GQGA4", "default", "month", created_at=UTC_STAMP)
    before = _database_dump(database_path)

    with pytest.raises(RuleManagementServiceError) as caught:
        initialize_gqga4_rules(database_path)

    assert caught.value.code == "rule_set_not_initialized"
    assert _database_dump(database_path) == before


def test_existing_incomplete_active_version_is_rejected_without_repair(tmp_path):
    database_path = tmp_path / "rules.sqlite3"
    rules, prototypes = normalize_gqga4_rule_snapshot(
        GQGA4_INITIAL_RULES, GQGA4_INITIAL_VIRTUAL_PROTOTYPES
    )
    compiled = compile_gqga4_rule_set(rules, 1)
    with RuleStore.initialize(database_path) as store:
        with store.transaction(write=True):
            rule_set_id = store.create_rule_set("GQGA4", "default", "month", created_at=UTC_STAMP)
            version_id = store.create_version(
                rule_set_id,
                1,
                None,
                INITIALIZATION_OPERATION_ID,
                service_module._request_hash(None, rules, prototypes, ""),
                compiled.compiled_rule_set_json,
                compiled.rule_set_spec.fingerprint,
                service_module._virtual_prototypes_json(prototypes),
                service_module._editor_snapshot_json(rules, prototypes, ""),
                "",
                "v7-rule-service",
                UTC_STAMP,
                "v7-rule-service",
                UTC_STAMP,
            )
            store.activate_version(rule_set_id, version_id, None, updated_at=UTC_STAMP)
    before = _database_dump(database_path)

    with pytest.raises(RuleManagementServiceError) as caught:
        initialize_gqga4_rules(database_path)

    assert caught.value.code == "stored_snapshot_inconsistent"
    assert _database_dump(database_path) == before


@pytest.mark.parametrize("failure_point", ("definitions", "activation", "readback"))
def test_initialization_failure_rolls_back_all_seed_rows(tmp_path, monkeypatch, failure_point):
    database_path = tmp_path / "rules.sqlite3"

    if failure_point == "definitions":

        def fail_definitions(store, version_id, rules):
            rule = rules[0]
            store.create_rule_definition(
                version_id,
                1,
                rule.rule_id,
                rule.rule_type,
                rule.name,
                rule.scope.value,
                rule.enabled,
                rule.version,
                "{}",
            )
            raise RuntimeError("forced definition failure")

        monkeypatch.setattr(service_module, "_create_rule_definitions", fail_definitions)
    elif failure_point == "activation":
        monkeypatch.setattr(
            RuleStore,
            "activate_version",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("forced activation failure")
            ),
        )
    else:
        monkeypatch.setattr(
            service_module,
            "_read_active_rules",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("forced read failure")),
        )

    with pytest.raises(RuntimeError, match="forced"):
        initialize_gqga4_rules(database_path, clock=lambda: NOW)

    assert _counts(database_path) == (0, 0, 0)
    monkeypatch.undo()
    assert initialize_gqga4_rules(database_path, clock=lambda: NOW).created is True


def test_two_overlapping_initializers_create_only_one_version(tmp_path):
    database_path = tmp_path / "rules.sqlite3"
    first_has_write_lock = Event()
    release_first = Event()

    def slow_clock():
        first_has_write_lock.set()
        assert release_first.wait(2)
        return NOW

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            initialize_gqga4_rules,
            database_path,
            clock=slow_clock,
            timeout_seconds=2,
        )
        assert first_has_write_lock.wait(1)
        second = executor.submit(
            initialize_gqga4_rules,
            database_path,
            clock=lambda: NOW,
            timeout_seconds=2,
        )
        with pytest.raises(FutureTimeoutError):
            second.result(timeout=0.05)
        release_first.set()
        results = (first.result(timeout=2), second.result(timeout=2))

    assert sorted(result.created for result in results) == [False, True]
    assert results[0].active_rules == results[1].active_rules
    assert _counts(database_path) == (1, 1, 17)


def test_module_command_uses_configuration_path_and_reports_repeat_status(tmp_path):
    database_path = tmp_path / "command" / "rules.sqlite3"
    ignored_legacy_database = tmp_path / "ignored-legacy-environment.sqlite3"
    configuration_path = _write_service_configuration(
        tmp_path / "config" / "service.json",
        "../command/rules.sqlite3",
    )
    environment = os.environ.copy()
    environment["APSGO_V7_RULE_DB_PATH"] = str(ignored_legacy_database)
    environment["PYTHONPATH"] = str(ROOT / "src")
    command = [
        sys.executable,
        "-m",
        "apsgo_v7_service.initialize_gqga4_rules",
        "--config",
        str(configuration_path),
    ]

    first = subprocess.run(
        command, cwd=tmp_path, env=environment, check=True, capture_output=True, text=True
    )
    second = subprocess.run(
        command,
        cwd=tmp_path / "command",
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first.stderr == second.stderr == ""
    assert first_payload["status"] == "initialized"
    assert second_payload["status"] == "already_initialized"
    assert not ignored_legacy_database.exists()
    for payload in (first_payload, second_payload):
        assert payload["active_version_id"] == 1
        assert payload["version"] == "1"
        assert payload["fingerprint"] == EXPECTED_FINGERPRINT
        assert payload["rule_count"] == 17
        assert payload["virtual_prototype_count"] == 27
    assert _counts(database_path) == (1, 1, 17)
    assert get_active_gqga4_rules(database_path).activated_by == "v7-rule-service"


def test_module_command_rejects_invalid_configuration_before_creating_database(tmp_path):
    configuration_path = _write_service_configuration(
        tmp_path / "config" / "service.json",
        "   ",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "apsgo_v7_service.initialize_gqga4_rules",
            "--config",
            str(configuration_path),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "database_path must be nonempty text" in result.stderr
    assert not (tmp_path / "data" / "apsgo_v7_rules.sqlite3").exists()
