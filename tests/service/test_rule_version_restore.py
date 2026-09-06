import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from apsgo_scheduler.api.rule_management import SetActiveRulesRequest
from apsgo_v7_service.gqga4 import (
    GQGA4_INITIAL_RULES,
    GQGA4_INITIAL_VIRTUAL_PROTOTYPES,
)
from apsgo_v7_service.restore_gqga4_rule_version import restore_gqga4_rule_version
from apsgo_v7_service.rule_management import (
    RuleManagementServiceError,
    get_active_gqga4_rules,
    initialize_gqga4_rules,
    set_active_gqga4_rules,
)
from apsgo_v7_service.rule_store import DATABASE_PATH_ENVIRONMENT_VARIABLE, RuleStore

ROOT = Path(__file__).resolve().parents[2]
RESTORE_OPERATION = "00000000-0000-4000-8000-000000001101"
SAVE_OPERATION_1 = "00000000-0000-4000-8000-000000001102"
SAVE_OPERATION_2 = "00000000-0000-4000-8000-000000001103"
STALE_RESTORE_OPERATION = "00000000-0000-4000-8000-000000001104"


def _rules_with_minimum(minimum: str):
    result = []
    for rule in GQGA4_INITIAL_RULES:
        if rule.rule_id == "chain_weight_range":
            parameters = dict(rule.parameters)
            parameters["min_weight"] = Decimal(minimum)
            rule = replace(rule, parameters=parameters)
        result.append(rule)
    return tuple(result)


def _save(database_path, operation_id: str, minimum: str, remark: str):
    active = get_active_gqga4_rules(database_path)
    return set_active_gqga4_rules(
        SetActiveRulesRequest(
            save_operation_id=operation_id,
            expected_active_version_id=active.active_version_id,
            rules=_rules_with_minimum(minimum),
            virtual_prototypes=GQGA4_INITIAL_VIRTUAL_PROTOTYPES,
            remark=remark,
        ),
        database_path,
    )


def _minimum_weight(active_rules):
    rule = next(
        item for item in active_rules.rule_set_spec.rules if item.rule_id == "chain_weight_range"
    )
    return rule.parameters["min_weight"]


def _counts(database_path):
    with sqlite3.connect(database_path) as connection:
        return (
            connection.execute("SELECT COUNT(*) FROM v7_rule_set_version").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM v7_rule_definition").fetchone()[0],
        )


def test_restore_copies_historical_page_content_into_a_new_audited_version(tmp_path):
    database_path = tmp_path / "rules.sqlite3"
    initialize_gqga4_rules(database_path)
    historical = _save(database_path, SAVE_OPERATION_1, "710", "历史规则备注")
    current = _save(database_path, SAVE_OPERATION_2, "720", "当前规则备注")

    with RuleStore.open(database_path) as store:
        historical_record_before = store.find_version(historical.saved_version_id)
        historical_rules_before = store.list_rule_definitions(historical.saved_version_id)

    with pytest.raises(RuleManagementServiceError) as caught:
        restore_gqga4_rule_version(
            historical.saved_version_id,
            STALE_RESTORE_OPERATION,
            historical.saved_version_id,
            database_path,
        )
    assert caught.value.code == "active_version_conflict"
    assert caught.value.expected_active_version_id == historical.saved_version_id
    assert caught.value.current_active_version_id == current.saved_version_id
    assert _counts(database_path) == (3, 51)

    restored = restore_gqga4_rule_version(
        historical.saved_version_id,
        RESTORE_OPERATION,
        current.saved_version_id,
        database_path,
    )

    assert restored.idempotent_replay is False
    assert restored.previous_active_version_id == current.saved_version_id
    assert restored.saved_version_is_active is True
    assert restored.active_rules.active_version_id == restored.saved_version_id
    assert restored.active_rules.rule_set_spec.version == "4"
    assert _minimum_weight(restored.active_rules) == Decimal("710")
    assert restored.active_rules.virtual_prototypes == historical.active_rules.virtual_prototypes
    assert restored.active_rules.remark == historical.active_rules.remark == "历史规则备注"
    assert _counts(database_path) == (4, 68)

    with RuleStore.open(database_path) as store:
        restored_record = store.find_version(restored.saved_version_id)
        assert store.find_version(historical.saved_version_id) == historical_record_before
        assert store.list_rule_definitions(historical.saved_version_id) == historical_rules_before
    assert restored_record.based_on_version_id == current.saved_version_id
    assert restored_record.save_operation_id == RESTORE_OPERATION
    assert (
        restored_record.created_by
        == restored_record.activated_by
        == f"v7-rule-restore:source-version-{historical.saved_version_id}"
    )


def test_restore_replays_the_original_request_without_reactivating_a_superseded_result(tmp_path):
    database_path = tmp_path / "rules.sqlite3"
    initial = initialize_gqga4_rules(database_path).active_rules
    current = _save(database_path, SAVE_OPERATION_1, "720", "当前规则")

    first = restore_gqga4_rule_version(
        initial.active_version_id,
        RESTORE_OPERATION,
        current.saved_version_id,
        database_path,
    )
    immediate_replay = restore_gqga4_rule_version(
        initial.active_version_id,
        RESTORE_OPERATION,
        current.saved_version_id,
        database_path,
    )

    assert immediate_replay.idempotent_replay is True
    assert immediate_replay.previous_active_version_id == current.saved_version_id
    assert immediate_replay.saved_version_id == first.saved_version_id
    assert immediate_replay.saved_version_is_active is True
    assert _counts(database_path) == (3, 51)

    with pytest.raises(RuleManagementServiceError) as changed_expected:
        restore_gqga4_rule_version(
            initial.active_version_id,
            RESTORE_OPERATION,
            first.saved_version_id,
            database_path,
        )
    assert changed_expected.value.code == "operation_payload_conflict"
    assert _counts(database_path) == (3, 51)

    later = _save(database_path, SAVE_OPERATION_2, "730", "后续规则")
    counts_before_replay = _counts(database_path)
    superseded_replay = restore_gqga4_rule_version(
        initial.active_version_id,
        RESTORE_OPERATION,
        current.saved_version_id,
        database_path,
    )

    assert superseded_replay.idempotent_replay is True
    assert superseded_replay.saved_version_id == first.saved_version_id
    assert superseded_replay.saved_version_is_active is False
    assert superseded_replay.active_rules.active_version_id == later.saved_version_id
    assert _minimum_weight(superseded_replay.active_rules) == Decimal("730")
    assert _counts(database_path) == counts_before_replay


def test_restore_rejects_the_active_source_and_same_operation_for_another_source(tmp_path):
    database_path = tmp_path / "rules.sqlite3"
    initial = initialize_gqga4_rules(database_path).active_rules
    current = _save(database_path, SAVE_OPERATION_1, "700", "")

    with pytest.raises(ValueError, match="historical version"):
        restore_gqga4_rule_version(
            current.saved_version_id,
            RESTORE_OPERATION,
            current.saved_version_id,
            database_path,
        )
    assert _counts(database_path) == (2, 34)

    restored = restore_gqga4_rule_version(
        initial.active_version_id,
        RESTORE_OPERATION,
        current.saved_version_id,
        database_path,
    )
    with pytest.raises(RuleManagementServiceError) as caught:
        restore_gqga4_rule_version(
            current.saved_version_id,
            RESTORE_OPERATION,
            current.saved_version_id,
            database_path,
        )

    assert caught.value.code == "operation_payload_conflict"
    assert caught.value.current_active_version_id == restored.saved_version_id
    assert _counts(database_path) == (3, 51)


@pytest.mark.parametrize(
    ("field_name", "corrupted_value"),
    (
        ("editor_snapshot_json", "{}"),
        ("compiled_rule_set_json", "{}"),
    ),
)
def test_restore_rejects_an_inconsistent_historical_snapshot(
    tmp_path, monkeypatch, field_name, corrupted_value
):
    database_path = tmp_path / "rules.sqlite3"
    historical = initialize_gqga4_rules(database_path).active_rules
    current = _save(database_path, SAVE_OPERATION_1, "720", "当前规则")
    before = _counts(database_path)
    original_find_version = RuleStore.find_version

    def find_corrupted_version(store, version_id):
        record = original_find_version(store, version_id)
        if version_id == historical.active_version_id:
            return replace(record, **{field_name: corrupted_value})
        return record

    monkeypatch.setattr(RuleStore, "find_version", find_corrupted_version)

    with pytest.raises(RuleManagementServiceError) as caught:
        restore_gqga4_rule_version(
            historical.active_version_id,
            RESTORE_OPERATION,
            current.saved_version_id,
            database_path,
        )

    assert caught.value.code == "stored_snapshot_inconsistent"
    assert _counts(database_path) == before


def test_restore_rejects_an_unknown_source_and_command_reports_json(tmp_path):
    database_path = tmp_path / "rules.sqlite3"
    initial = initialize_gqga4_rules(database_path).active_rules
    _save(database_path, SAVE_OPERATION_1, "720", "当前规则")
    before = _counts(database_path)

    with pytest.raises(ValueError, match="source_version_id"):
        restore_gqga4_rule_version(
            9999,
            RESTORE_OPERATION,
            get_active_gqga4_rules(database_path).active_version_id,
            database_path,
        )
    assert _counts(database_path) == before

    environment = os.environ.copy()
    environment[DATABASE_PATH_ENVIRONMENT_VARIABLE] = str(tmp_path / "wrong.sqlite3")
    environment["PYTHONPATH"] = str(ROOT / "src")
    command = [
        sys.executable,
        "-m",
        "apsgo_v7_service.restore_gqga4_rule_version",
        "--source-version-id",
        str(initial.active_version_id),
        "--save-operation-id",
        RESTORE_OPERATION,
        "--expected-active-version-id",
        str(get_active_gqga4_rules(database_path).active_version_id),
        "--database-path",
        str(database_path),
    ]
    first = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    replay = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    first_payload = json.loads(first.stdout)
    replay_payload = json.loads(replay.stdout)
    assert first.stderr == replay.stderr == ""
    assert first_payload["status"] == "restored"
    assert replay_payload["status"] == "idempotent_replay"
    assert first_payload["database_path"] == replay_payload["database_path"] == str(database_path)
    assert first_payload["source_version_id"] == initial.active_version_id
    assert replay_payload["saved_version_id"] == first_payload["saved_version_id"]
    assert replay_payload["saved_version_is_active"] is True
    later = _save(database_path, SAVE_OPERATION_2, "730", "后续规则")
    superseded = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    superseded_payload = json.loads(superseded.stdout)
    assert superseded.returncode == 3
    assert superseded.stderr == ""
    assert superseded_payload["status"] == "superseded_replay"
    assert superseded_payload["saved_version_is_active"] is False
    assert superseded_payload["active_version_id"] == later.saved_version_id
    assert _counts(database_path) == (4, 68)


def test_restore_command_rejects_an_invalid_operation_before_opening_the_database(tmp_path):
    missing_database = tmp_path / "missing.sqlite3"
    command = [
        sys.executable,
        "-m",
        "apsgo_v7_service.restore_gqga4_rule_version",
        "--source-version-id",
        "1",
        "--save-operation-id",
        "not-a-uuid",
        "--expected-active-version-id",
        "1",
        "--database-path",
        str(missing_database),
    ]
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=os.environ | {"PYTHONPATH": str(ROOT / "src")},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "--save-operation-id: must be UUID text" in result.stderr
    assert "Traceback" not in result.stderr
    assert not missing_database.exists()
