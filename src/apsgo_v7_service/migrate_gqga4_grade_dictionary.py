"""Explicit one-time migration of the GQGA4 grade dictionary into V7."""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_scheduler.api.rule_management import EditableRuleInput, SetActiveRulesResponse

from .grade_dictionary import GradeDictionaryEntry, GradeDictionarySnapshot, normalize_grade
from .gqga4 import compile_gqga4_rule_set
from .rule_management import (
    MIGRATION_AUDIT_ACTOR,
    RuleManagementServiceError,
    _create_grade_dictionary_entries,
    _create_rule_definitions,
    _editor_snapshot_json,
    _find_rule_set,
    _read_active_rules,
    _read_grade_dictionary_snapshot,
    _read_rules_version,
    _request_hash,
    _timestamp,
    _utc_now,
    _virtual_prototypes_json,
)
from .rule_store import LEGACY_SCHEMA_VERSION, SCHEMA_VERSION, RuleStore

EXPECTED_GQGA4_ENTRY_COUNT = 230


@dataclass(frozen=True, slots=True)
class V3GradeDictionarySource:
    database_path: Path
    database_sha256: str
    snapshot: GradeDictionarySnapshot


@dataclass(frozen=True, slots=True)
class SqliteBackupResult:
    database_path: Path
    database_sha256: str
    integrity_check: str
    user_version: int
    created: bool = True


@dataclass(frozen=True, slots=True)
class Gqga4GradeDictionaryMigrationResult:
    save_result: SetActiveRulesResponse
    source: V3GradeDictionarySource
    backup: SqliteBackupResult | None


def _timeout_seconds(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout_seconds must be a positive finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("timeout_seconds must be a positive finite number")
    return result


def _existing_absolute_file(value: str | Path, name: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError(f"{name} must identify an existing database file")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    try:
        path = path.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"{name} must identify an existing database file") from error
    if not path.is_file():
        raise ValueError(f"{name} must identify an existing database file")
    return path


def _absolute_output_path(value: str | Path, name: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError(f"{name} must not be blank")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path.resolve(strict=False)


def _canonical_operation_uuid(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("save_operation_id must be UUID text")
    try:
        result = UUID(value.strip())
    except (AttributeError, ValueError) as error:
        raise ValueError("save_operation_id must be UUID text") from error
    if result.int == 0:
        raise ValueError("save_operation_id must not be the empty UUID")
    return str(result)


def _positive_version_id(value: int) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("expected_active_version_id must be a positive integer")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_only_connection(path: Path, timeout_seconds: float) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        timeout=timeout_seconds,
        isolation_level=None,
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise RuntimeError("SQLite query-only mode could not be enabled")
    return connection


def _source_entry(row: sqlite3.Row) -> GradeDictionaryEntry:
    text_fields = (
        "product_line_code",
        "grade",
        "roll_type",
        "soft_hard_class",
        "steel_classes",
        "source_file",
        "source_rows",
        "remark",
    )
    if any(type(row[name]) is not str for name in text_fields):
        raise ValueError("V3 grade dictionary contains a non-text text field")
    if row["product_line_code"] != "GQGA4":
        raise ValueError("V3 grade dictionary contains another product line")
    for name in ("is_if_steel", "enabled"):
        if type(row[name]) is not int or row[name] not in (0, 1):
            raise ValueError(f"V3 grade dictionary {name} must be integer 0 or 1")
    row_count = row["source_row_count"]
    if type(row_count) is not int or row_count < 0:
        raise ValueError("V3 grade dictionary source_row_count must be a nonnegative integer")
    return GradeDictionaryEntry(
        source_grade=row["grade"],
        normalized_grade=normalize_grade(row["grade"]),
        soft_hard_class=row["soft_hard_class"],
        roll_type=row["roll_type"],
        steel_classes=row["steel_classes"],
        is_if_steel=bool(row["is_if_steel"]),
        enabled=bool(row["enabled"]),
        source_file=row["source_file"],
        source_row_count=row_count,
        source_rows=row["source_rows"],
        remark=row["remark"],
    )


def load_gqga4_grade_dictionary_from_v3_sqlite(
    database_path: str | Path,
    *,
    timeout_seconds: float = 5.0,
) -> V3GradeDictionarySource:
    """Read and fully validate the frozen GQGA4 dictionary through a read-only URI."""

    path = _existing_absolute_file(database_path, "database_path")
    timeout = _timeout_seconds(timeout_seconds)
    connection = None
    try:
        with TemporaryDirectory(prefix="v7-grade-source-") as directory:
            snapshot_path = Path(directory) / "source-snapshot.sqlite3"
            snapshot_backup = backup_sqlite_database(
                path,
                snapshot_path,
                timeout_seconds=timeout,
            )
            connection = _read_only_connection(snapshot_path, timeout)
            connection.execute("BEGIN")
            rows = connection.execute(
                """
                SELECT product_line_code, grade, roll_type, soft_hard_class,
                       steel_classes, is_if_steel, enabled, source_file,
                       source_row_count, source_rows, remark
                FROM aps_gqga4_grade_dictionary
                WHERE product_line_code = ?
                ORDER BY UPPER(TRIM(grade)), id
                """,
                ("GQGA4",),
            ).fetchall()
            connection.commit()
            connection.close()
            connection = None
            snapshot_sha256 = snapshot_backup.database_sha256
    except (OSError, RuntimeError, sqlite3.DatabaseError) as error:
        raise RuleManagementServiceError(
            "source_dictionary_invalid",
            "无法创建并读取 V3 软硬钢字典的一致性快照。",
        ) from error
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    try:
        entries = tuple(_source_entry(row) for row in rows)
        snapshot = GradeDictionarySnapshot("GQGA4", entries)
        if len(snapshot.entries) != EXPECTED_GQGA4_ENTRY_COUNT:
            raise ValueError(
                "GQGA4 grade dictionary must contain exactly "
                f"{EXPECTED_GQGA4_ENTRY_COUNT} entries"
            )
        if not all(entry.enabled for entry in snapshot.entries):
            raise ValueError("all GQGA4 migration dictionary entries must be enabled")
    except (TypeError, ValueError) as error:
        raise RuleManagementServiceError(
            "source_dictionary_invalid",
            f"V3 GQGA4 软硬钢字典不符合迁移契约：{error}",
        ) from error

    return V3GradeDictionarySource(path, snapshot_sha256, snapshot)


def _inspect_sqlite_database(
    database_path: Path,
    timeout_seconds: float,
    *,
    created: bool,
) -> SqliteBackupResult:
    verification = _read_only_connection(database_path, timeout_seconds)
    try:
        integrity_rows = tuple(
            row[0] for row in verification.execute("PRAGMA integrity_check").fetchall()
        )
        user_version = verification.execute("PRAGMA user_version").fetchone()[0]
    finally:
        verification.close()
    if integrity_rows != ("ok",):
        raise RuntimeError(f"SQLite backup integrity check failed: {integrity_rows}")
    return SqliteBackupResult(
        database_path,
        _sha256(database_path),
        "ok",
        user_version,
        created,
    )


def backup_sqlite_database(
    database_path: str | Path,
    backup_path: str | Path,
    *,
    timeout_seconds: float = 5.0,
) -> SqliteBackupResult:
    """Create one standalone SQLite backup without copying the main file directly."""

    source_path = _existing_absolute_file(database_path, "database_path")
    destination_path = _absolute_output_path(backup_path, "backup_path")
    timeout = _timeout_seconds(timeout_seconds)
    if destination_path.exists():
        raise FileExistsError(destination_path)
    if not destination_path.parent.is_dir():
        raise FileNotFoundError(destination_path.parent)

    descriptor = os.open(destination_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    source = None
    destination = None
    try:
        source = _read_only_connection(source_path, timeout)
        destination = sqlite3.connect(
            destination_path,
            timeout=timeout,
            isolation_level=None,
        )
        source.backup(destination)
        destination.close()
        destination = None
        source.close()
        source = None
        result = _inspect_sqlite_database(destination_path, timeout, created=True)
    except BaseException:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        destination_path.unlink(missing_ok=True)
        raise
    return result


_V1_CONTENT_QUERIES = (
    (
        "v7_rule_set",
        """SELECT id, product_line_code, process_code, scenario, active_version_id,
                  created_at, updated_at FROM v7_rule_set ORDER BY id""",
    ),
    (
        "v7_rule_set_version",
        """SELECT id, rule_set_id, version_no, based_on_version_id,
                  save_operation_id, request_hash, compiled_rule_set_json,
                  rule_set_fingerprint, virtual_prototypes_json,
                  editor_snapshot_json, remark, created_by, created_at,
                  activated_by, activated_at
             FROM v7_rule_set_version ORDER BY id""",
    ),
    (
        "v7_rule_definition",
        """SELECT id, rule_set_version_id, sequence_no, rule_id, rule_type,
                  rule_name, scope, enabled, rule_version, parameters_json
             FROM v7_rule_definition ORDER BY id""",
    ),
)


def _v1_content_fingerprint(database_path: Path, timeout_seconds: float) -> str:
    with RuleStore.open_for_schema_upgrade(
        database_path,
        timeout_seconds=timeout_seconds,
    ):
        pass
    connection = _read_only_connection(database_path, timeout_seconds)
    try:
        connection.execute("BEGIN")
        content = tuple(
            (name, tuple(tuple(row) for row in connection.execute(query).fetchall()))
            for name, query in _V1_CONTENT_QUERIES
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    return hashlib.sha256(dumps_exact_json(content).encode("utf-8")).hexdigest()


def _reuse_existing_backup(
    database_path: Path,
    backup_path: Path,
    timeout_seconds: float,
) -> SqliteBackupResult:
    existing = _inspect_sqlite_database(backup_path, timeout_seconds, created=False)
    if existing.user_version != LEGACY_SCHEMA_VERSION:
        raise FileExistsError("backup_path exists but is not a schema v1 backup")
    if _v1_content_fingerprint(backup_path, timeout_seconds) != _v1_content_fingerprint(
        database_path,
        timeout_seconds,
    ):
        raise FileExistsError("backup_path exists but does not match the current target")
    return existing


def _verify_stored_dictionary(
    store: RuleStore,
    rule_set,
    version_id: int,
    expected: GradeDictionarySnapshot,
) -> GradeDictionarySnapshot:
    snapshot = _read_grade_dictionary_snapshot(store, rule_set, version_id)
    if snapshot != expected:
        raise RuleManagementServiceError(
            "stored_snapshot_inconsistent",
            "规则版本中的软硬钢字典内容或指纹不一致。",
        )
    return snapshot


def _read_user_version(path: Path, timeout_seconds: float) -> int:
    connection = _read_only_connection(path, timeout_seconds)
    try:
        return connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()


def _save_response(
    store: RuleStore,
    rule_set,
    operation_id: str,
    previous_version_id: int,
    saved_version_id: int,
    *,
    replay: bool,
) -> SetActiveRulesResponse:
    active = _read_active_rules(store, rule_set)
    return SetActiveRulesResponse(
        active_rules=active,
        save_operation_id=operation_id,
        previous_active_version_id=previous_version_id,
        saved_version_id=saved_version_id,
        saved_version_is_active=saved_version_id == active.active_version_id,
        idempotent_replay=replay,
    )


def _replay_migration(
    store: RuleStore,
    operation_id: str,
    expected_active_version_id: int,
    source: V3GradeDictionarySource,
) -> Gqga4GradeDictionaryMigrationResult:
    rule_set = _find_rule_set(store)
    existing = store.find_version_by_operation(rule_set.id, operation_id)
    if existing is None:
        raise RuleManagementServiceError(
            "migration_already_applied",
            "目标数据库已是 schema v2，但找不到本次迁移操作记录。",
            expected_active_version_id=expected_active_version_id,
            current_active_version_id=rule_set.active_version_id,
        )
    if (
        existing.based_on_version_id != expected_active_version_id
        or existing.created_by != MIGRATION_AUDIT_ACTOR
        or existing.activated_by != MIGRATION_AUDIT_ACTOR
        or existing.grade_dictionary_fingerprint != source.snapshot.dictionary_fingerprint
    ):
        raise RuleManagementServiceError(
            "operation_payload_conflict",
            "同一迁移操作标识已对应另一项迁移内容或执行主体。",
            expected_active_version_id=expected_active_version_id,
            current_active_version_id=rule_set.active_version_id,
        )
    based_on = _read_rules_version(store, rule_set, expected_active_version_id)
    migrated = _read_rules_version(store, rule_set, existing.id)
    based_on_rules = tuple(
        EditableRuleInput(rule.rule_id, rule.enabled, rule.parameters)
        for rule in based_on.rule_set_spec.rules
    )
    migrated_rules = tuple(
        EditableRuleInput(rule.rule_id, rule.enabled, rule.parameters)
        for rule in migrated.rule_set_spec.rules
    )
    if (
        migrated_rules != based_on_rules
        or migrated.virtual_prototypes != based_on.virtual_prototypes
        or migrated.remark != based_on.remark
    ):
        raise RuleManagementServiceError(
            "operation_payload_conflict",
            "同一迁移操作标识对应的版本未完整继承原规则、原型和备注。",
            expected_active_version_id=expected_active_version_id,
            current_active_version_id=rule_set.active_version_id,
        )
    _verify_stored_dictionary(store, rule_set, existing.id, source.snapshot)
    response = _save_response(
        store,
        rule_set,
        operation_id,
        expected_active_version_id,
        existing.id,
        replay=True,
    )
    return Gqga4GradeDictionaryMigrationResult(response, source, None)


def _verify_backup_snapshot(
    backup: SqliteBackupResult,
    expected_active_rules,
    timeout_seconds: float,
) -> None:
    if backup.user_version != LEGACY_SCHEMA_VERSION:
        raise RuntimeError("migration backup is not a schema v1 database")
    with RuleStore.open_for_schema_upgrade(
        backup.database_path,
        timeout_seconds=timeout_seconds,
    ) as backup_store:
        with backup_store.transaction():
            backup_rule_set = _find_rule_set(backup_store)
            if backup_rule_set.active_version_id is None:
                raise RuntimeError("migration backup has no active GQGA4 rule version")
            backup_rules = _read_rules_version(
                backup_store,
                backup_rule_set,
                backup_rule_set.active_version_id,
            )
    if backup_rules != expected_active_rules:
        raise RuntimeError("migration backup does not contain the expected active rules")


def _migrate_v1(
    store: RuleStore,
    database_path: Path,
    backup_path: Path,
    operation_id: str,
    expected_active_version_id: int,
    source: V3GradeDictionarySource,
    clock: Callable[[], datetime],
    timeout_seconds: float,
) -> Gqga4GradeDictionaryMigrationResult:
    rule_set = _find_rule_set(store)
    current_active_version_id = rule_set.active_version_id
    if store.find_version_by_operation(rule_set.id, operation_id) is not None:
        raise RuleManagementServiceError(
            "operation_payload_conflict",
            "schema v1 中已存在相同操作标识，不能作为字典迁移重放。",
            expected_active_version_id=expected_active_version_id,
            current_active_version_id=current_active_version_id,
        )
    if current_active_version_id is None:
        raise RuleManagementServiceError(
            "rule_set_not_initialized",
            "GQGA4 月计划规则尚无启用版本。",
        )
    if current_active_version_id != expected_active_version_id:
        raise RuleManagementServiceError(
            "active_version_conflict",
            "活动规则版本已变化，请重新核对后再迁移。",
            expected_active_version_id=expected_active_version_id,
            current_active_version_id=current_active_version_id,
        )

    previous = _read_rules_version(store, rule_set, current_active_version_id)
    backup = (
        _reuse_existing_backup(database_path, backup_path, timeout_seconds)
        if backup_path.exists()
        else backup_sqlite_database(
            database_path,
            backup_path,
            timeout_seconds=timeout_seconds,
        )
    )
    _verify_backup_snapshot(backup, previous, timeout_seconds)
    store.apply_schema_v2_upgrade()

    rules = tuple(
        EditableRuleInput(rule.rule_id, rule.enabled, rule.parameters)
        for rule in previous.rule_set_spec.rules
    )
    prototypes = previous.virtual_prototypes
    version_no = store.next_version_no(rule_set.id)
    compiled = compile_gqga4_rule_set(rules, version_no)
    timestamp = _timestamp(clock)
    version_id = store.create_version(
        rule_set.id,
        version_no,
        current_active_version_id,
        operation_id,
        _request_hash(current_active_version_id, rules, prototypes, previous.remark),
        compiled.compiled_rule_set_json,
        compiled.rule_set_spec.fingerprint,
        _virtual_prototypes_json(prototypes),
        _editor_snapshot_json(rules, prototypes, previous.remark),
        previous.remark,
        MIGRATION_AUDIT_ACTOR,
        timestamp,
        MIGRATION_AUDIT_ACTOR,
        timestamp,
        grade_dictionary_fingerprint=source.snapshot.dictionary_fingerprint,
    )
    _create_rule_definitions(store, version_id, compiled.rule_set_spec.rules)
    _create_grade_dictionary_entries(store, version_id, source.snapshot)
    store.activate_version(
        rule_set.id,
        version_id,
        current_active_version_id,
        updated_at=timestamp,
    )
    updated_rule_set = _find_rule_set(store)
    _read_rules_version(store, updated_rule_set, version_id)
    _verify_stored_dictionary(store, updated_rule_set, version_id, source.snapshot)
    store.finalize_schema_v2()
    response = _save_response(
        store,
        updated_rule_set,
        operation_id,
        current_active_version_id,
        version_id,
        replay=False,
    )
    return Gqga4GradeDictionaryMigrationResult(response, source, backup)


def migrate_gqga4_grade_dictionary(
    *,
    database_path: str | Path,
    v3_database_path: str | Path,
    backup_path: str | Path,
    save_operation_id: str,
    expected_active_version_id: int,
    clock: Callable[[], datetime] = _utc_now,
    timeout_seconds: float = 5.0,
) -> Gqga4GradeDictionaryMigrationResult:
    """Migrate schema v1 once, or return the exact prior UUID result from schema v2."""

    timeout = _timeout_seconds(timeout_seconds)
    operation_id = _canonical_operation_uuid(save_operation_id)
    expected_version_id = _positive_version_id(expected_active_version_id)
    target_path = _existing_absolute_file(database_path, "database_path")
    source_path = _existing_absolute_file(v3_database_path, "v3_database_path")
    backup_output_path = _absolute_output_path(backup_path, "backup_path")
    if os.path.samefile(target_path, source_path):
        raise ValueError("database_path and v3_database_path must be different files")
    if backup_output_path.exists() and (
        os.path.samefile(backup_output_path, target_path)
        or os.path.samefile(backup_output_path, source_path)
    ):
        raise ValueError("backup_path must be different from source and target databases")

    source = load_gqga4_grade_dictionary_from_v3_sqlite(
        source_path,
        timeout_seconds=timeout,
    )
    with RuleStore.open_for_schema_upgrade(target_path, timeout_seconds=timeout) as store:
        with store.transaction(write=True):
            schema_version = _read_user_version(target_path, timeout)
            if schema_version == SCHEMA_VERSION:
                return _replay_migration(
                    store,
                    operation_id,
                    expected_version_id,
                    source,
                )
            if schema_version != LEGACY_SCHEMA_VERSION:
                raise RuntimeError(f"unsupported database schema version: {schema_version}")
            return _migrate_v1(
                store,
                target_path,
                backup_output_path,
                operation_id,
                expected_version_id,
                source,
                clock,
                timeout,
            )


def _positive_int_argument(value: str) -> int:
    try:
        return _positive_version_id(int(value))
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error


def _operation_uuid_argument(value: str) -> str:
    try:
        return _canonical_operation_uuid(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error).replace("save_operation_id ", "")) from error


def _existing_database_argument(value: str) -> Path:
    try:
        return _existing_absolute_file(value, "database path")
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error).replace("database path ", "")) from error


def _absolute_backup_argument(value: str) -> Path:
    try:
        return _absolute_output_path(value, "backup path")
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error).replace("backup path ", "")) from error


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate the V3 GQGA4 grade dictionary into an existing V7 rule database."
    )
    parser.add_argument("--save-operation-id", required=True, type=_operation_uuid_argument)
    parser.add_argument(
        "--expected-active-version-id",
        required=True,
        type=_positive_int_argument,
    )
    parser.add_argument("--database-path", required=True, type=_existing_database_argument)
    parser.add_argument("--v3-database-path", required=True, type=_existing_database_argument)
    parser.add_argument("--backup-path", required=True, type=_absolute_backup_argument)
    arguments = parser.parse_args(argv)

    result = migrate_gqga4_grade_dictionary(
        database_path=arguments.database_path,
        v3_database_path=arguments.v3_database_path,
        backup_path=arguments.backup_path,
        save_operation_id=arguments.save_operation_id,
        expected_active_version_id=arguments.expected_active_version_id,
    )
    saved = result.save_result
    status = (
        "superseded_replay"
        if not saved.saved_version_is_active
        else "idempotent_replay"
        if saved.idempotent_replay
        else "migrated"
    )
    print(
        dumps_exact_json(
            {
                "status": status,
                "database_path": str(arguments.database_path),
                "v3_database_path": str(arguments.v3_database_path),
                "source_database_sha256": result.source.database_sha256,
                "save_operation_id": saved.save_operation_id,
                "previous_active_version_id": saved.previous_active_version_id,
                "saved_version_id": saved.saved_version_id,
                "saved_version_is_active": saved.saved_version_is_active,
                "active_version_id": saved.active_rules.active_version_id,
                "rule_set_fingerprint": saved.active_rules.rule_set_spec.fingerprint,
                "grade_dictionary_fingerprint": result.source.snapshot.dictionary_fingerprint,
                "grade_dictionary_entry_count": len(result.source.snapshot.entries),
                "backup_created": (
                    result.backup.created if result.backup is not None else False
                ),
                "backup_reused": (
                    not result.backup.created if result.backup is not None else False
                ),
                "backup_path": (
                    str(result.backup.database_path) if result.backup is not None else None
                ),
                "backup_database_sha256": (
                    result.backup.database_sha256 if result.backup is not None else None
                ),
            }
        )
    )
    return 0 if saved.saved_version_is_active else 3


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXPECTED_GQGA4_ENTRY_COUNT",
    "Gqga4GradeDictionaryMigrationResult",
    "MIGRATION_AUDIT_ACTOR",
    "SqliteBackupResult",
    "V3GradeDictionarySource",
    "backup_sqlite_database",
    "load_gqga4_grade_dictionary_from_v3_sqlite",
    "main",
    "migrate_gqga4_grade_dictionary",
]
