import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_scheduler.api.rule_management import SetActiveRulesRequest
from apsgo_v7_service import migrate_gqga4_grade_dictionary as migration_module
from apsgo_v7_service import rule_management as rule_management_module
from apsgo_v7_service import rule_store as rule_store_module
from apsgo_v7_service.grade_dictionary import (
    GradeDictionaryEntry,
    GradeDictionarySnapshot,
)
from apsgo_v7_service.gqga4 import (
    GQGA4_INITIAL_RULES,
    GQGA4_INITIAL_VIRTUAL_PROTOTYPES,
    compile_gqga4_rule_set,
    normalize_gqga4_rule_snapshot,
)
from apsgo_v7_service.migrate_gqga4_grade_dictionary import (
    EXPECTED_GQGA4_ENTRY_COUNT,
    MIGRATION_AUDIT_ACTOR,
    backup_sqlite_database,
    load_gqga4_grade_dictionary_from_v3_sqlite,
    migrate_gqga4_grade_dictionary,
)
from apsgo_v7_service.rule_management import (
    RuleManagementServiceError,
    set_active_gqga4_rules,
)
from apsgo_v7_service.rule_store import LEGACY_SCHEMA_VERSION, SCHEMA_VERSION, RuleStore
from tests.service.grade_dictionary_support import write_v3_grade_dictionary_database

NOW = datetime(2026, 9, 7, 10, 30, 0, 123456, tzinfo=timezone(timedelta(hours=8)))
UTC_STAMP = "2026-09-07T02:30:00.123456+00:00"
MIGRATION_OPERATION = "00000000-0000-4000-8000-000000000701"
LATER_OPERATION = "00000000-0000-4000-8000-000000000702"
LEGACY_REMARK = "迁移前用户备注"


def _formal_dictionary(seed=""):
    entries = [
        GradeDictionaryEntry(
            source_grade=f"St04D{seed}+Z",
            normalized_grade=f"ST04D{seed}+Z",
            soft_hard_class="软钢",
            roll_type="小辊",
            steel_classes="普通钢|IF钢",
            is_if_steel=True,
            enabled=True,
            source_file="4镀锌钢种对应.xlsx",
            source_row_count=2,
            source_rows="2,8",
            remark="正式迁移测试行",
        )
    ]
    entries.extend(
        GradeDictionaryEntry(
            source_grade=f"GRADE{seed}{index:03d}+Z",
            normalized_grade=f"GRADE{seed}{index:03d}+Z",
            soft_hard_class="软钢" if index % 2 else "硬钢",
            roll_type="小辊" if index % 3 else "大辊",
            steel_classes="测试类别",
            is_if_steel=False,
            enabled=True,
            source_file="4镀锌钢种对应.xlsx",
            source_row_count=1,
            source_rows=str(index + 2),
            remark="",
        )
        for index in range(1, EXPECTED_GQGA4_ENTRY_COUNT)
    )
    return GradeDictionarySnapshot("GQGA4", tuple(entries))


def _legacy_rules():
    result = []
    for rule in GQGA4_INITIAL_RULES:
        if rule.rule_id == "chain_weight_range":
            parameters = dict(rule.parameters)
            parameters["min_weight"] = Decimal("711")
            rule = replace(rule, parameters=parameters)
        result.append(rule)
    return normalize_gqga4_rule_snapshot(
        tuple(result), GQGA4_INITIAL_VIRTUAL_PROTOTYPES
    )


def _write_v1_target(database_path):
    rules, prototypes = _legacy_rules()
    compiled = compile_gqga4_rule_set(rules, 1)
    timestamp = "2026-09-06T00:00:00.000000+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in rule_store_module._V1_SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO v7_rule_set (
                id, product_line_code, process_code, scenario,
                active_version_id, created_at, updated_at
            ) VALUES (1, 'GQGA4', 'default', 'month', NULL, ?, ?)
            """,
            (timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO v7_rule_set_version (
                id, rule_set_id, version_no, based_on_version_id,
                save_operation_id, request_hash, compiled_rule_set_json,
                rule_set_fingerprint, virtual_prototypes_json,
                editor_snapshot_json, remark, created_by, created_at,
                activated_by, activated_at
            ) VALUES (1, 1, 1, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "bootstrap:gqga4:v1",
                rule_management_module._request_hash(None, rules, prototypes, LEGACY_REMARK),
                compiled.compiled_rule_set_json,
                compiled.rule_set_spec.fingerprint,
                rule_management_module._virtual_prototypes_json(prototypes),
                rule_management_module._editor_snapshot_json(
                    rules, prototypes, LEGACY_REMARK
                ),
                LEGACY_REMARK,
                "legacy-rule-service",
                timestamp,
                "legacy-rule-service",
                timestamp,
            ),
        )
        connection.executemany(
            """
            INSERT INTO v7_rule_definition (
                rule_set_version_id, sequence_no, rule_id, rule_type,
                rule_name, scope, enabled, rule_version, parameters_json
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    sequence_no,
                    rule.rule_id,
                    rule.rule_type,
                    rule.name,
                    rule.scope.value,
                    int(rule.enabled),
                    rule.version,
                    dumps_exact_json(rule.parameters),
                )
                for sequence_no, rule in enumerate(compiled.rule_set_spec.rules, start=1)
            ),
        )
        connection.execute("UPDATE v7_rule_set SET active_version_id = 1 WHERE id = 1")
        connection.execute(f"PRAGMA user_version = {LEGACY_SCHEMA_VERSION}")
    return rules, prototypes, compiled.rule_set_spec


def _database_sha256(database_path):
    return migration_module._sha256(database_path)


def _migration_paths(tmp_path, *, dictionary=None):
    source_path = tmp_path / "v3.sqlite3"
    target_path = tmp_path / "v7.sqlite3"
    backup_path = tmp_path / "v7-before-grade-migration.sqlite3"
    write_v3_grade_dictionary_database(
        source_path, _formal_dictionary() if dictionary is None else dictionary
    )
    _write_v1_target(target_path)
    return source_path, target_path, backup_path


def _migrate(source_path, target_path, backup_path, **overrides):
    arguments = {
        "database_path": target_path,
        "v3_database_path": source_path,
        "backup_path": backup_path,
        "save_operation_id": MIGRATION_OPERATION,
        "expected_active_version_id": 1,
        "clock": lambda: NOW,
    }
    arguments.update(overrides)
    return migrate_gqga4_grade_dictionary(**arguments)


def _schema_state(database_path):
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(v7_rule_set_version)")
        }
        return {
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "versions": connection.execute(
                "SELECT COUNT(*) FROM v7_rule_set_version"
            ).fetchone()[0],
            "active": connection.execute(
                "SELECT active_version_id FROM v7_rule_set WHERE id = 1"
            ).fetchone()[0],
            "dictionary_table": "v7_grade_dictionary_entry" in tables,
            "dictionary_column": "grade_dictionary_fingerprint" in columns,
        }


def test_v3_dictionary_loader_preserves_all_fields_and_source_bytes(tmp_path):
    source_path = tmp_path / "v3.sqlite3"
    snapshot_path = tmp_path / "source-snapshot.sqlite3"
    expected = _formal_dictionary()
    write_v3_grade_dictionary_database(source_path, expected)
    before = _database_sha256(source_path)

    loaded = load_gqga4_grade_dictionary_from_v3_sqlite(source_path)
    snapshot = backup_sqlite_database(source_path, snapshot_path)

    assert loaded.database_path == source_path.resolve()
    assert loaded.database_sha256 == snapshot.database_sha256
    assert before == _database_sha256(source_path)
    assert loaded.snapshot == expected
    assert len(loaded.snapshot.entries) == EXPECTED_GQGA4_ENTRY_COUNT
    first = loaded.snapshot.find_enabled(" st04d+z ")
    assert first == expected.find_enabled("ST04D+Z")
    assert (
        first.roll_type,
        first.soft_hard_class,
        first.steel_classes,
        first.is_if_steel,
        first.source_file,
        first.source_row_count,
        first.source_rows,
        first.remark,
    ) == (
        "小辊",
        "软钢",
        "普通钢|IF钢",
        True,
        "4镀锌钢种对应.xlsx",
        2,
        "2,8",
        "正式迁移测试行",
    )


def test_v3_dictionary_loader_hashes_and_reads_the_same_committed_wal_snapshot(tmp_path):
    source_path = tmp_path / "v3.sqlite3"
    expected = _formal_dictionary()
    write_v3_grade_dictionary_database(source_path, expected)
    writer = sqlite3.connect(source_path)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        main_file_before = _database_sha256(source_path)
        writer.execute(
            "UPDATE aps_gqga4_grade_dictionary SET remark = ? WHERE grade = ?",
            ("WAL 中已提交", expected.entries[0].source_grade),
        )
        writer.commit()
        assert _database_sha256(source_path) == main_file_before

        first = load_gqga4_grade_dictionary_from_v3_sqlite(source_path)
        second = load_gqga4_grade_dictionary_from_v3_sqlite(source_path)

        assert first.snapshot.entries[0].remark == "WAL 中已提交"
        assert first.snapshot == second.snapshot
        assert first.database_sha256 == second.database_sha256
        assert first.database_sha256 != main_file_before
    finally:
        writer.close()


@pytest.mark.parametrize(
    ("mutation_sql", "parameters"),
    (
        ("DELETE FROM aps_gqga4_grade_dictionary WHERE id = 1", ()),
        ("UPDATE aps_gqga4_grade_dictionary SET enabled = 0 WHERE id = 1", ()),
        ("UPDATE aps_gqga4_grade_dictionary SET is_if_steel = 2 WHERE id = 1", ()),
        ("UPDATE aps_gqga4_grade_dictionary SET source_row_count = -1 WHERE id = 1", ()),
        (
            "UPDATE aps_gqga4_grade_dictionary SET grade = "
            "(SELECT lower(grade) FROM aps_gqga4_grade_dictionary WHERE id = 1) "
            "WHERE id = 2",
            (),
        ),
    ),
)
def test_v3_dictionary_loader_rejects_incomplete_or_invalid_formal_source(
    tmp_path, mutation_sql, parameters
):
    source_path = tmp_path / "v3.sqlite3"
    write_v3_grade_dictionary_database(source_path, _formal_dictionary())
    with sqlite3.connect(source_path) as connection:
        connection.execute(mutation_sql, parameters)
    before = _database_sha256(source_path)

    with pytest.raises(RuleManagementServiceError) as caught:
        load_gqga4_grade_dictionary_from_v3_sqlite(source_path)

    assert caught.value.code == "source_dictionary_invalid"
    assert _database_sha256(source_path) == before


@pytest.mark.parametrize("source_shape", ("missing_table", "missing_column"))
def test_v3_dictionary_loader_maps_sql_schema_errors_to_stable_service_error(
    tmp_path, source_shape
):
    source_path = tmp_path / "v3.sqlite3"
    if source_shape == "missing_table":
        with sqlite3.connect(source_path):
            pass
    else:
        write_v3_grade_dictionary_database(source_path, _formal_dictionary())
        with sqlite3.connect(source_path) as connection:
            connection.execute(
                "ALTER TABLE aps_gqga4_grade_dictionary "
                "RENAME COLUMN remark TO missing_remark"
            )

    with pytest.raises(RuleManagementServiceError) as caught:
        load_gqga4_grade_dictionary_from_v3_sqlite(source_path)

    assert caught.value.code == "source_dictionary_invalid"
    assert isinstance(caught.value.__cause__, sqlite3.DatabaseError)


def test_sqlite_backup_includes_committed_wal_state_and_never_overwrites(tmp_path):
    target_path = tmp_path / "v7.sqlite3"
    backup_path = tmp_path / "backup.sqlite3"
    _write_v1_target(target_path)
    writer = sqlite3.connect(target_path)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("UPDATE v7_rule_set SET updated_at = 'wal-committed' WHERE id = 1")
        writer.commit()

        backup = backup_sqlite_database(target_path, backup_path)

        with sqlite3.connect(backup_path) as connection:
            assert connection.execute(
                "SELECT updated_at FROM v7_rule_set WHERE id = 1"
            ).fetchone()[0] == "wal-committed"
        assert backup.user_version == LEGACY_SCHEMA_VERSION
        assert backup.integrity_check == "ok"
        assert backup.created is True
        assert backup.database_sha256 == _database_sha256(backup_path)
        with pytest.raises(FileExistsError):
            backup_sqlite_database(target_path, backup_path)
    finally:
        writer.close()


def test_migration_upgrades_once_and_preserves_rules_while_binding_dictionary(tmp_path):
    source_path, target_path, backup_path = _migration_paths(tmp_path)
    source_before = _database_sha256(source_path)

    result = _migrate(source_path, target_path, backup_path)

    saved = result.save_result
    assert saved.previous_active_version_id == 1
    assert saved.saved_version_id == 2
    assert saved.saved_version_is_active is True
    assert saved.idempotent_replay is False
    assert saved.active_rules.active_version_id == 2
    assert saved.active_rules.based_on_version_id == 1
    assert saved.active_rules.rule_set_spec.version == "2"
    assert saved.active_rules.remark == LEGACY_REMARK
    assert saved.active_rules.activated_by == MIGRATION_AUDIT_ACTOR
    assert saved.active_rules.activated_at == UTC_STAMP
    assert result.source.snapshot.dictionary_fingerprint == (
        _formal_dictionary().dictionary_fingerprint
    )
    assert result.backup is not None
    assert result.backup.database_path == backup_path.resolve()
    assert result.backup.user_version == LEGACY_SCHEMA_VERSION
    assert _database_sha256(source_path) == source_before
    assert _schema_state(target_path) == {
        "user_version": SCHEMA_VERSION,
        "versions": 2,
        "active": 2,
        "dictionary_table": True,
        "dictionary_column": True,
    }
    assert _schema_state(backup_path) == {
        "user_version": LEGACY_SCHEMA_VERSION,
        "versions": 1,
        "active": 1,
        "dictionary_table": False,
        "dictionary_column": False,
    }

    with RuleStore.open(target_path) as store:
        initial = store.find_version(1)
        migrated = store.find_version(2)
        initial_rules = store.list_rule_definitions(1)
        migrated_rules = store.list_rule_definitions(2)
        dictionary = store.list_grade_dictionary_entries(2)
    assert initial.grade_dictionary_fingerprint is None
    assert migrated.based_on_version_id == 1
    assert migrated.grade_dictionary_fingerprint == (
        result.source.snapshot.dictionary_fingerprint
    )
    assert len(dictionary) == EXPECTED_GQGA4_ENTRY_COUNT
    assert tuple(
        (
            item.sequence_no,
            item.rule_id,
            item.rule_type,
            item.rule_name,
            item.scope,
            item.enabled,
            item.rule_version,
            item.parameters_json,
        )
        for item in migrated_rules
    ) == tuple(
        (
            item.sequence_no,
            item.rule_id,
            item.rule_type,
            item.rule_name,
            item.scope,
            item.enabled,
            item.rule_version,
            item.parameters_json,
        )
        for item in initial_rules
    )
    assert next(
        rule
        for rule in saved.active_rules.rule_set_spec.rules
        if rule.rule_id == "chain_weight_range"
    ).parameters["min_weight"] == Decimal("711")


def test_failed_migration_rolls_back_schema_dictionary_version_and_activation(
    tmp_path, monkeypatch
):
    source_path, target_path, backup_path = _migration_paths(tmp_path)
    original_finalization = RuleStore.finalize_schema_v2

    def fail_finalization(self):
        raise RuntimeError("forced finalization failure")

    monkeypatch.setattr(RuleStore, "finalize_schema_v2", fail_finalization)
    with pytest.raises(RuntimeError, match="forced finalization failure"):
        _migrate(source_path, target_path, backup_path)

    assert _schema_state(target_path) == {
        "user_version": LEGACY_SCHEMA_VERSION,
        "versions": 1,
        "active": 1,
        "dictionary_table": False,
        "dictionary_column": False,
    }
    assert backup_path.is_file()
    assert _schema_state(backup_path) == _schema_state(target_path)

    monkeypatch.setattr(RuleStore, "finalize_schema_v2", original_finalization)
    retried = _migrate(source_path, target_path, backup_path)
    assert retried.backup is not None
    assert retried.backup.created is False
    assert retried.save_result.saved_version_is_active is True
    assert _schema_state(target_path)["user_version"] == SCHEMA_VERSION


def test_retry_refuses_an_existing_backup_from_another_target(tmp_path):
    source_path, target_path, backup_path = _migration_paths(tmp_path)
    other_target = tmp_path / "other-v7.sqlite3"
    _write_v1_target(other_target)
    with sqlite3.connect(other_target) as connection:
        connection.execute(
            "UPDATE v7_rule_set SET updated_at = 'different-target' WHERE id = 1"
        )
    backup_sqlite_database(other_target, backup_path)

    with pytest.raises(FileExistsError, match="does not match"):
        _migrate(source_path, target_path, backup_path)

    assert _schema_state(target_path)["user_version"] == LEGACY_SCHEMA_VERSION


def test_backup_removes_partial_file_when_post_copy_verification_fails(
    tmp_path, monkeypatch
):
    target_path = tmp_path / "v7.sqlite3"
    backup_path = tmp_path / "backup.sqlite3"
    _write_v1_target(target_path)

    def fail_verification(*args, **kwargs):
        raise RuntimeError("forced verification failure")

    monkeypatch.setattr(migration_module, "_inspect_sqlite_database", fail_verification)
    with pytest.raises(RuntimeError, match="forced verification failure"):
        backup_sqlite_database(target_path, backup_path)

    assert not backup_path.exists()


def test_same_uuid_replays_without_backup_or_reactivation_and_conflicts_on_change(tmp_path):
    source_path, target_path, backup_path = _migration_paths(tmp_path)
    first = _migrate(source_path, target_path, backup_path)

    replay = _migrate(source_path, target_path, backup_path)

    assert replay.backup is None
    assert replay.save_result.saved_version_id == first.save_result.saved_version_id
    assert replay.save_result.saved_version_is_active is True
    assert replay.save_result.idempotent_replay is True
    assert _schema_state(target_path)["versions"] == 2

    active = first.save_result.active_rules
    later = set_active_gqga4_rules(
        SetActiveRulesRequest(
            save_operation_id=LATER_OPERATION,
            expected_active_version_id=active.active_version_id,
            rules=tuple(
                replace(rule, enabled=not rule.enabled)
                if rule.rule_id == "forbid_consecutive_reverse_width"
                else rule
                for rule in (
                    rule_management_module.EditableRuleInput(
                        spec.rule_id, spec.enabled, spec.parameters
                    )
                    for spec in active.rule_set_spec.rules
                )
            ),
            virtual_prototypes=active.virtual_prototypes,
            remark="迁移后的用户版本",
        ),
        target_path,
        clock=lambda: NOW,
    )
    superseded = _migrate(source_path, target_path, backup_path)
    assert superseded.save_result.saved_version_id == first.save_result.saved_version_id
    assert superseded.save_result.active_rules.active_version_id == later.saved_version_id
    assert superseded.save_result.saved_version_is_active is False
    assert superseded.save_result.idempotent_replay is True
    assert _schema_state(target_path)["versions"] == 3

    with pytest.raises(RuleManagementServiceError) as changed_expected:
        _migrate(
            source_path,
            target_path,
            backup_path,
            expected_active_version_id=later.saved_version_id,
        )
    assert changed_expected.value.code == "operation_payload_conflict"

    alternate_source = tmp_path / "alternate-v3.sqlite3"
    write_v3_grade_dictionary_database(alternate_source, _formal_dictionary("X"))
    with pytest.raises(RuleManagementServiceError) as changed_dictionary:
        _migrate(alternate_source, target_path, backup_path)
    assert changed_dictionary.value.code == "operation_payload_conflict"


def test_v2_without_matching_operation_is_not_silently_remigrated(tmp_path):
    source_path = tmp_path / "v3.sqlite3"
    target_path = tmp_path / "v7.sqlite3"
    backup_path = tmp_path / "backup.sqlite3"
    dictionary = _formal_dictionary()
    write_v3_grade_dictionary_database(source_path, dictionary)
    rule_management_module.initialize_gqga4_rules(
        target_path,
        initial_grade_dictionary=dictionary,
        clock=lambda: NOW,
    )

    with pytest.raises(RuleManagementServiceError) as caught:
        _migrate(source_path, target_path, backup_path)

    assert caught.value.code == "migration_already_applied"
    assert not backup_path.exists()
    assert _schema_state(target_path)["versions"] == 1


def test_ordinary_rule_save_cannot_masquerade_as_the_migration_operation(tmp_path):
    source_path = tmp_path / "v3.sqlite3"
    target_path = tmp_path / "v7.sqlite3"
    backup_path = tmp_path / "backup.sqlite3"
    dictionary = _formal_dictionary()
    write_v3_grade_dictionary_database(source_path, dictionary)
    initialized = rule_management_module.initialize_gqga4_rules(
        target_path,
        initial_grade_dictionary=dictionary,
        clock=lambda: NOW,
    )
    active = initialized.active_rules
    unchanged_rules = tuple(
        rule_management_module.EditableRuleInput(
            rule.rule_id, rule.enabled, rule.parameters
        )
        for rule in active.rule_set_spec.rules
    )
    with pytest.raises(ValueError, match="reserved"):
        set_active_gqga4_rules(
            SetActiveRulesRequest(
                save_operation_id=MIGRATION_OPERATION,
                expected_active_version_id=active.active_version_id,
                rules=unchanged_rules,
                virtual_prototypes=active.virtual_prototypes,
                remark=active.remark,
            ),
            target_path,
            audit_actor=MIGRATION_AUDIT_ACTOR,
            clock=lambda: NOW,
        )

    with pytest.raises(RuleManagementServiceError) as caught:
        _migrate(source_path, target_path, backup_path)

    assert caught.value.code == "migration_already_applied"
    assert not backup_path.exists()
    assert _schema_state(target_path)["versions"] == 1


def test_cli_prints_auditable_migration_result_and_replay(tmp_path, capsys):
    source_path, target_path, backup_path = _migration_paths(tmp_path)
    arguments = [
        "--database-path",
        str(target_path),
        "--v3-database-path",
        str(source_path),
        "--backup-path",
        str(backup_path),
        "--save-operation-id",
        MIGRATION_OPERATION,
        "--expected-active-version-id",
        "1",
    ]

    assert migration_module.main(arguments) == 0
    migrated = json.loads(capsys.readouterr().out)
    assert migrated["status"] == "migrated"
    assert migrated["grade_dictionary_entry_count"] == EXPECTED_GQGA4_ENTRY_COUNT
    assert migrated["backup_created"] is True
    assert migrated["backup_reused"] is False

    assert migration_module.main(arguments) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["status"] == "idempotent_replay"
    assert replay["saved_version_id"] == migrated["saved_version_id"]
    assert replay["backup_created"] is False
    assert replay["backup_reused"] is False
