"""SQLite storage primitives for immutable APSGo V7 rule-set versions."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from pathlib import Path

LEGACY_SCHEMA_VERSION = 1
SCHEMA_VERSION = 2


class RuleStoreSchemaError(RuntimeError):
    """The database schema cannot be safely used by this service version."""


class RuleStoreConflict(RuntimeError):
    """A conditional storage write no longer matches current database state."""


@dataclass(frozen=True, slots=True)
class RuleSetRecord:
    id: int
    product_line_code: str
    process_code: str
    scenario: str
    active_version_id: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class RuleSetVersionRecord:
    id: int
    rule_set_id: int
    version_no: int
    based_on_version_id: int | None
    save_operation_id: str
    request_hash: str
    compiled_rule_set_json: str
    rule_set_fingerprint: str
    virtual_prototypes_json: str
    editor_snapshot_json: str
    remark: str
    created_by: str
    created_at: str
    activated_by: str
    activated_at: str
    grade_dictionary_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class RuleDefinitionRecord:
    id: int
    rule_set_version_id: int
    sequence_no: int
    rule_id: str
    rule_type: str
    rule_name: str
    scope: str
    enabled: bool
    rule_version: str
    parameters_json: str


@dataclass(frozen=True, slots=True)
class GradeDictionaryEntryRecord:
    rule_set_version_id: int
    source_grade: str
    normalized_grade: str
    soft_hard_class: str
    roll_type: str
    steel_classes: str
    is_if_steel: bool
    enabled: bool
    source_file: str
    source_row_count: int
    source_rows: str
    remark: str


_V1_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS v7_rule_set (
        id INTEGER PRIMARY KEY,
        product_line_code TEXT NOT NULL CHECK(length(trim(product_line_code)) > 0),
        process_code TEXT NOT NULL CHECK(length(trim(process_code)) > 0),
        scenario TEXT NOT NULL CHECK(length(trim(scenario)) > 0),
        active_version_id INTEGER,
        created_at TEXT NOT NULL CHECK(length(trim(created_at)) > 0),
        updated_at TEXT NOT NULL CHECK(length(trim(updated_at)) > 0),
        FOREIGN KEY (id, active_version_id)
            REFERENCES v7_rule_set_version(rule_set_id, id)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS v7_rule_set_version (
        id INTEGER PRIMARY KEY,
        rule_set_id INTEGER NOT NULL,
        version_no INTEGER NOT NULL
            CHECK(typeof(version_no) = 'integer' AND version_no >= 1),
        based_on_version_id INTEGER,
        save_operation_id TEXT NOT NULL CHECK(length(trim(save_operation_id)) > 0),
        request_hash TEXT NOT NULL
            CHECK(length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
        compiled_rule_set_json TEXT NOT NULL
            CHECK(length(trim(compiled_rule_set_json)) > 0),
        rule_set_fingerprint TEXT NOT NULL
            CHECK(
                length(rule_set_fingerprint) = 64
                AND rule_set_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
        virtual_prototypes_json TEXT NOT NULL
            CHECK(length(trim(virtual_prototypes_json)) > 0),
        editor_snapshot_json TEXT NOT NULL
            CHECK(length(trim(editor_snapshot_json)) > 0),
        remark TEXT NOT NULL,
        created_by TEXT NOT NULL CHECK(length(trim(created_by)) > 0),
        created_at TEXT NOT NULL CHECK(length(trim(created_at)) > 0),
        activated_by TEXT NOT NULL CHECK(length(trim(activated_by)) > 0),
        activated_at TEXT NOT NULL CHECK(length(trim(activated_at)) > 0),
        CHECK(based_on_version_id IS NULL OR based_on_version_id <> id),
        FOREIGN KEY (rule_set_id) REFERENCES v7_rule_set(id) ON DELETE RESTRICT,
        FOREIGN KEY (rule_set_id, based_on_version_id)
            REFERENCES v7_rule_set_version(rule_set_id, id)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS v7_rule_definition (
        id INTEGER PRIMARY KEY,
        rule_set_version_id INTEGER NOT NULL,
        sequence_no INTEGER NOT NULL
            CHECK(typeof(sequence_no) = 'integer' AND sequence_no >= 1),
        rule_id TEXT NOT NULL CHECK(length(trim(rule_id)) > 0),
        rule_type TEXT NOT NULL CHECK(length(trim(rule_type)) > 0),
        rule_name TEXT NOT NULL CHECK(length(trim(rule_name)) > 0),
        scope TEXT NOT NULL CHECK(length(trim(scope)) > 0),
        enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
        rule_version TEXT NOT NULL CHECK(length(trim(rule_version)) > 0),
        parameters_json TEXT NOT NULL CHECK(length(trim(parameters_json)) > 0),
        FOREIGN KEY (rule_set_version_id)
            REFERENCES v7_rule_set_version(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_v7_rule_set_identity
    ON v7_rule_set(product_line_code, process_code, scenario)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_v7_rule_set_version_owner_id
    ON v7_rule_set_version(rule_set_id, id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_v7_rule_set_version_number
    ON v7_rule_set_version(rule_set_id, version_no)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_v7_rule_set_version_operation
    ON v7_rule_set_version(rule_set_id, save_operation_id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_v7_rule_definition_rule
    ON v7_rule_definition(rule_set_version_id, rule_id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_v7_rule_definition_sequence
    ON v7_rule_definition(rule_set_version_id, sequence_no)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_v7_rule_set_identity_immutable
    BEFORE UPDATE OF product_line_code, process_code, scenario, created_at ON v7_rule_set
    BEGIN
        SELECT RAISE(ABORT, 'v7_rule_set identity is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_v7_rule_set_no_delete
    BEFORE DELETE ON v7_rule_set
    BEGIN
        SELECT RAISE(ABORT, 'v7_rule_set cannot be deleted');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_v7_rule_set_active_version_forward
    BEFORE UPDATE OF active_version_id ON v7_rule_set
    WHEN OLD.active_version_id IS NOT NULL AND (
        NEW.active_version_id IS NULL
        OR (
            SELECT version_no
            FROM v7_rule_set_version
            WHERE id = NEW.active_version_id AND rule_set_id = NEW.id
        ) <= (
            SELECT version_no
            FROM v7_rule_set_version
            WHERE id = OLD.active_version_id AND rule_set_id = OLD.id
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'active rule-set version must move forward');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_v7_rule_set_version_no_update
    BEFORE UPDATE ON v7_rule_set_version
    BEGIN
        SELECT RAISE(ABORT, 'v7_rule_set_version is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_v7_rule_set_version_no_delete
    BEFORE DELETE ON v7_rule_set_version
    BEGIN
        SELECT RAISE(ABORT, 'v7_rule_set_version cannot be deleted');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_v7_rule_definition_no_historical_insert
    BEFORE INSERT ON v7_rule_definition
    WHEN EXISTS (
        SELECT 1
        FROM v7_rule_set_version AS target_version
        JOIN v7_rule_set AS owner ON owner.id = target_version.rule_set_id
        JOIN v7_rule_set_version AS active_version
            ON active_version.id = owner.active_version_id
        WHERE target_version.id = NEW.rule_set_version_id
          AND target_version.version_no <= active_version.version_no
    )
    BEGIN
        SELECT RAISE(ABORT, 'cannot append a rule to an active or historical version');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_v7_rule_definition_no_update
    BEFORE UPDATE ON v7_rule_definition
    BEGIN
        SELECT RAISE(ABORT, 'v7_rule_definition is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_v7_rule_definition_no_delete
    BEFORE DELETE ON v7_rule_definition
    BEGIN
        SELECT RAISE(ABORT, 'v7_rule_definition cannot be deleted');
    END
    """,
)

_V1_TO_V2_SCHEMA_STATEMENTS = (
    """
    ALTER TABLE v7_rule_set_version
    ADD COLUMN grade_dictionary_fingerprint TEXT
        CHECK(
            grade_dictionary_fingerprint IS NULL
            OR (
                length(grade_dictionary_fingerprint) = 64
                AND grade_dictionary_fingerprint NOT GLOB '*[^0-9a-f]*'
            )
        )
    """,
    """
    CREATE TABLE v7_grade_dictionary_entry (
        rule_set_version_id INTEGER NOT NULL,
        source_grade TEXT NOT NULL CHECK(length(trim(source_grade)) > 0),
        normalized_grade TEXT NOT NULL
            CHECK(
                length(trim(normalized_grade)) > 0
                AND normalized_grade = upper(trim(source_grade))
            ),
        soft_hard_class TEXT NOT NULL CHECK(soft_hard_class IN ('软钢', '硬钢')),
        roll_type TEXT NOT NULL CHECK(length(trim(roll_type)) > 0),
        steel_classes TEXT NOT NULL,
        is_if_steel INTEGER NOT NULL CHECK(is_if_steel IN (0, 1)),
        enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
        source_file TEXT NOT NULL,
        source_row_count INTEGER NOT NULL
            CHECK(typeof(source_row_count) = 'integer' AND source_row_count >= 0),
        source_rows TEXT NOT NULL,
        remark TEXT NOT NULL,
        PRIMARY KEY (rule_set_version_id, normalized_grade),
        FOREIGN KEY (rule_set_version_id)
            REFERENCES v7_rule_set_version(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER trg_v7_grade_dictionary_no_historical_insert
    BEFORE INSERT ON v7_grade_dictionary_entry
    WHEN EXISTS (
        SELECT 1
        FROM v7_rule_set_version AS target_version
        JOIN v7_rule_set AS owner ON owner.id = target_version.rule_set_id
        JOIN v7_rule_set_version AS active_version
            ON active_version.id = owner.active_version_id
        WHERE target_version.id = NEW.rule_set_version_id
          AND target_version.version_no <= active_version.version_no
    )
    BEGIN
        SELECT RAISE(
            ABORT,
            'cannot append a grade dictionary entry to an active or historical version'
        );
    END
    """,
    """
    CREATE TRIGGER trg_v7_grade_dictionary_no_update
    BEFORE UPDATE ON v7_grade_dictionary_entry
    BEGIN
        SELECT RAISE(ABORT, 'v7_grade_dictionary_entry is immutable');
    END
    """,
    """
    CREATE TRIGGER trg_v7_grade_dictionary_no_delete
    BEFORE DELETE ON v7_grade_dictionary_entry
    BEGIN
        SELECT RAISE(ABORT, 'v7_grade_dictionary_entry cannot be deleted');
    END
    """,
)


def _schema_signature(connection: sqlite3.Connection) -> tuple[tuple[str, str, str], ...]:
    rows = connection.execute(
        """
        SELECT type, name, sql
        FROM sqlite_master
        WHERE name GLOB 'v7_*'
           OR name GLOB 'ux_v7_*'
           OR name GLOB 'trg_v7_*'
        """
    )
    return tuple(sorted((row[0], row[1], " ".join(row[2].split())) for row in rows))


@cache
def _expected_schema_signature(
    schema_version: int = SCHEMA_VERSION,
) -> tuple[tuple[str, str, str], ...]:
    if schema_version not in (LEGACY_SCHEMA_VERSION, SCHEMA_VERSION):
        raise ValueError(f"unsupported schema signature version: {schema_version}")
    with sqlite3.connect(":memory:") as reference:
        for statement in _V1_SCHEMA_STATEMENTS:
            reference.execute(statement)
        if schema_version == SCHEMA_VERSION:
            for statement in _V1_TO_V2_SCHEMA_STATEMENTS:
                reference.execute(statement)
        return _schema_signature(reference)


class RuleStore:
    """One SQLite connection with explicit read or immediate-write transactions."""

    def __init__(self, connection: sqlite3.Connection):
        if not isinstance(connection, sqlite3.Connection):
            raise ValueError("connection must be sqlite3.Connection")
        if connection.isolation_level is not None or connection.in_transaction:
            raise ValueError("connection must use autocommit and have no active transaction")
        self._connection = connection
        self._write_transaction_active = False
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        if self._connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuleStoreSchemaError("SQLite foreign-key enforcement could not be enabled")

    @classmethod
    def _open_unchecked(
        cls,
        database_path: str | Path | None,
        *,
        timeout_seconds: float,
        create_parent: bool,
    ) -> RuleStore:
        if database_path is None or not str(database_path).strip():
            raise ValueError("database_path must not be blank")
        path = database_path
        if str(path) != ":memory:":
            path = Path(path)
            if create_parent:
                path.parent.mkdir(parents=True, exist_ok=True)
            elif not path.is_file():
                raise FileNotFoundError(path)
        connection = sqlite3.connect(path, timeout=timeout_seconds, isolation_level=None)
        try:
            return cls(connection)
        except BaseException:
            connection.close()
            raise

    @classmethod
    def initialize(cls, database_path: str | Path, *, timeout_seconds: float = 5.0) -> RuleStore:
        store = cls._open_unchecked(
            database_path, timeout_seconds=timeout_seconds, create_parent=True
        )
        try:
            store.initialize_schema()
            return store
        except BaseException:
            store.close()
            raise

    @classmethod
    def open(cls, database_path: str | Path, *, timeout_seconds: float = 5.0) -> RuleStore:
        store = cls._open_unchecked(
            database_path, timeout_seconds=timeout_seconds, create_parent=False
        )
        try:
            with store.transaction():
                store._verify_schema()
            return store
        except BaseException:
            store.close()
            raise

    @classmethod
    def open_for_schema_upgrade(
        cls,
        database_path: str | Path,
        *,
        timeout_seconds: float = 5.0,
    ) -> RuleStore:
        """Open an exact v1 or v2 database for the explicit migration command."""

        store = cls._open_unchecked(
            database_path, timeout_seconds=timeout_seconds, create_parent=False
        )
        try:
            with store.transaction():
                current = store._connection.execute("PRAGMA user_version").fetchone()[0]
                if current not in (LEGACY_SCHEMA_VERSION, SCHEMA_VERSION):
                    if current > SCHEMA_VERSION:
                        raise RuleStoreSchemaError(
                            "database schema version "
                            f"{current} is newer than supported {SCHEMA_VERSION}"
                        )
                    raise RuleStoreSchemaError(
                        f"unsupported database schema version: {current}"
                    )
                store._verify_schema(expected_version=current)
            return store
        except BaseException:
            store.close()
            raise

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> RuleStore:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[None]:
        if type(write) is not bool:
            raise ValueError("write must be bool")
        if self._connection.in_transaction:
            raise RuntimeError("nested rule-store transactions are not supported")
        self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        self._write_transaction_active = write
        try:
            yield
            self._connection.commit()
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        finally:
            self._write_transaction_active = False

    def initialize_schema(self) -> None:
        try:
            with self.transaction(write=True):
                current = self._connection.execute("PRAGMA user_version").fetchone()[0]
                if current > SCHEMA_VERSION:
                    raise RuleStoreSchemaError(
                        "database schema version "
                        f"{current} is newer than supported {SCHEMA_VERSION}"
                    )
                if current not in (0, SCHEMA_VERSION):
                    raise RuleStoreSchemaError(f"unsupported database schema version: {current}")
                if current == 0:
                    if _schema_signature(self._connection):
                        raise RuleStoreSchemaError(
                            "unversioned database already contains rule-store schema objects"
                        )
                    for statement in _V1_SCHEMA_STATEMENTS:
                        self._connection.execute(statement)
                    for statement in _V1_TO_V2_SCHEMA_STATEMENTS:
                        self._connection.execute(statement)
                    self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._verify_schema()
        except RuleStoreSchemaError:
            raise
        except sqlite3.DatabaseError as error:
            raise RuleStoreSchemaError("database schema initialization failed") from error

    def apply_schema_v2_upgrade(self) -> None:
        """Apply only the v1-to-v2 DDL inside the migration's write transaction."""

        self._require_write_transaction()
        self._verify_schema(expected_version=LEGACY_SCHEMA_VERSION)
        try:
            for statement in _V1_TO_V2_SCHEMA_STATEMENTS:
                self._connection.execute(statement)
        except sqlite3.DatabaseError as error:
            raise RuleStoreSchemaError("database schema upgrade to version 2 failed") from error

    def finalize_schema_v2(self) -> None:
        """Mark a fully written migration as v2 immediately before commit."""

        self._require_write_transaction()
        current = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if current != LEGACY_SCHEMA_VERSION:
            raise RuleStoreSchemaError(
                "schema version 2 finalization requires database schema version "
                f"{LEGACY_SCHEMA_VERSION}, found {current}"
            )
        self._verify_schema_objects(SCHEMA_VERSION)
        self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._verify_schema()

    def _verify_schema(self, *, expected_version: int = SCHEMA_VERSION) -> None:
        if expected_version not in (LEGACY_SCHEMA_VERSION, SCHEMA_VERSION):
            raise ValueError(f"unsupported schema verification version: {expected_version}")
        current = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuleStoreSchemaError(
                f"database schema version {current} is newer than supported {SCHEMA_VERSION}"
            )
        if current != expected_version:
            raise RuleStoreSchemaError(f"unexpected database schema version: {current}")
        self._verify_schema_objects(expected_version)

    def _verify_schema_objects(self, schema_version: int) -> None:
        if _schema_signature(self._connection) != _expected_schema_signature(schema_version):
            raise RuleStoreSchemaError(
                f"database schema objects do not match schema version {schema_version}"
            )
        violations = self._connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuleStoreSchemaError(f"database contains foreign-key violations: {violations}")

    def _require_write_transaction(self) -> None:
        if not self._connection.in_transaction or not self._write_transaction_active:
            raise RuntimeError("write operation requires an explicit immediate transaction")

    def create_rule_set(
        self,
        product_line_code: str,
        process_code: str,
        scenario: str,
        *,
        created_at: str,
    ) -> int:
        self._require_write_transaction()
        cursor = self._connection.execute(
            """
            INSERT INTO v7_rule_set (
                product_line_code, process_code, scenario, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (product_line_code, process_code, scenario, created_at, created_at),
        )
        return cursor.lastrowid

    def find_rule_set(
        self, product_line_code: str, process_code: str, scenario: str
    ) -> RuleSetRecord | None:
        row = self._connection.execute(
            """
            SELECT id, product_line_code, process_code, scenario, active_version_id,
                   created_at, updated_at
            FROM v7_rule_set
            WHERE product_line_code = ? AND process_code = ? AND scenario = ?
            """,
            (product_line_code, process_code, scenario),
        ).fetchone()
        return None if row is None else RuleSetRecord(**dict(row))

    def next_version_no(self, rule_set_id: int) -> int:
        self._require_write_transaction()
        row = self._connection.execute(
            """
            SELECT COALESCE(MAX(version_no), 0) + 1 AS next_version_no
            FROM v7_rule_set_version
            WHERE rule_set_id = ?
            """,
            (rule_set_id,),
        ).fetchone()
        return row["next_version_no"]

    def create_version(
        self,
        rule_set_id: int,
        version_no: int,
        based_on_version_id: int | None,
        save_operation_id: str,
        request_hash: str,
        compiled_rule_set_json: str,
        rule_set_fingerprint: str,
        virtual_prototypes_json: str,
        editor_snapshot_json: str,
        remark: str,
        created_by: str,
        created_at: str,
        activated_by: str,
        activated_at: str,
        grade_dictionary_fingerprint: str | None = None,
    ) -> int:
        self._require_write_transaction()
        cursor = self._connection.execute(
            """
            INSERT INTO v7_rule_set_version (
                rule_set_id, version_no, based_on_version_id, save_operation_id,
                request_hash, compiled_rule_set_json, rule_set_fingerprint,
                virtual_prototypes_json, editor_snapshot_json, remark,
                created_by, created_at, activated_by, activated_at,
                grade_dictionary_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rule_set_id,
                version_no,
                based_on_version_id,
                save_operation_id,
                request_hash,
                compiled_rule_set_json,
                rule_set_fingerprint,
                virtual_prototypes_json,
                editor_snapshot_json,
                remark,
                created_by,
                created_at,
                activated_by,
                activated_at,
                grade_dictionary_fingerprint,
            ),
        )
        return cursor.lastrowid

    def find_version(self, version_id: int) -> RuleSetVersionRecord | None:
        row = self._connection.execute(
            "SELECT * FROM v7_rule_set_version WHERE id = ?", (version_id,)
        ).fetchone()
        return None if row is None else RuleSetVersionRecord(**dict(row))

    def find_version_by_operation(
        self, rule_set_id: int, save_operation_id: str
    ) -> RuleSetVersionRecord | None:
        row = self._connection.execute(
            """
            SELECT * FROM v7_rule_set_version
            WHERE rule_set_id = ? AND save_operation_id = ?
            """,
            (rule_set_id, save_operation_id),
        ).fetchone()
        return None if row is None else RuleSetVersionRecord(**dict(row))

    def create_rule_definition(
        self,
        rule_set_version_id: int,
        sequence_no: int,
        rule_id: str,
        rule_type: str,
        rule_name: str,
        scope: str,
        enabled: bool,
        rule_version: str,
        parameters_json: str,
    ) -> int:
        self._require_write_transaction()
        cursor = self._connection.execute(
            """
            INSERT INTO v7_rule_definition (
                rule_set_version_id, sequence_no, rule_id, rule_type, rule_name,
                scope, enabled, rule_version, parameters_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rule_set_version_id,
                sequence_no,
                rule_id,
                rule_type,
                rule_name,
                scope,
                enabled,
                rule_version,
                parameters_json,
            ),
        )
        return cursor.lastrowid

    def list_rule_definitions(self, version_id: int) -> tuple[RuleDefinitionRecord, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM v7_rule_definition
            WHERE rule_set_version_id = ?
            ORDER BY sequence_no
            """,
            (version_id,),
        ).fetchall()
        return tuple(
            RuleDefinitionRecord(**(dict(row) | {"enabled": bool(row["enabled"])})) for row in rows
        )

    def create_grade_dictionary_entry(
        self,
        rule_set_version_id: int,
        source_grade: str,
        normalized_grade: str,
        soft_hard_class: str,
        roll_type: str,
        steel_classes: str,
        is_if_steel: bool,
        enabled: bool,
        source_file: str,
        source_row_count: int,
        source_rows: str,
        remark: str,
    ) -> None:
        self._require_write_transaction()
        self._connection.execute(
            """
            INSERT INTO v7_grade_dictionary_entry (
                rule_set_version_id, source_grade, normalized_grade,
                soft_hard_class, roll_type, steel_classes, is_if_steel, enabled,
                source_file, source_row_count, source_rows, remark
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rule_set_version_id,
                source_grade,
                normalized_grade,
                soft_hard_class,
                roll_type,
                steel_classes,
                is_if_steel,
                enabled,
                source_file,
                source_row_count,
                source_rows,
                remark,
            ),
        )

    def list_grade_dictionary_entries(
        self, version_id: int
    ) -> tuple[GradeDictionaryEntryRecord, ...]:
        rows = self._connection.execute(
            """
            SELECT rule_set_version_id, source_grade, normalized_grade,
                   soft_hard_class, roll_type, steel_classes, is_if_steel, enabled,
                   source_file, source_row_count, source_rows, remark
            FROM v7_grade_dictionary_entry
            WHERE rule_set_version_id = ?
            ORDER BY normalized_grade
            """,
            (version_id,),
        ).fetchall()
        return tuple(
            GradeDictionaryEntryRecord(
                **(
                    dict(row)
                    | {
                        "is_if_steel": bool(row["is_if_steel"]),
                        "enabled": bool(row["enabled"]),
                    }
                )
            )
            for row in rows
        )

    def copy_grade_dictionary_entries(
        self,
        source_version_id: int,
        target_version_id: int,
    ) -> int:
        self._require_write_transaction()
        cursor = self._connection.execute(
            """
            INSERT INTO v7_grade_dictionary_entry (
                rule_set_version_id, source_grade, normalized_grade,
                soft_hard_class, roll_type, steel_classes, is_if_steel, enabled,
                source_file, source_row_count, source_rows, remark
            )
            SELECT ?, source_grade, normalized_grade,
                   soft_hard_class, roll_type, steel_classes, is_if_steel, enabled,
                   source_file, source_row_count, source_rows, remark
            FROM v7_grade_dictionary_entry
            WHERE rule_set_version_id = ?
            ORDER BY normalized_grade
            """,
            (target_version_id, source_version_id),
        )
        return cursor.rowcount

    def activate_version(
        self,
        rule_set_id: int,
        version_id: int,
        expected_active_version_id: int | None,
        *,
        updated_at: str,
    ) -> None:
        self._require_write_transaction()
        cursor = self._connection.execute(
            """
            UPDATE v7_rule_set
            SET active_version_id = ?, updated_at = ?
            WHERE id = ? AND active_version_id IS ?
            """,
            (version_id, updated_at, rule_set_id, expected_active_version_id),
        )
        if cursor.rowcount != 1:
            raise RuleStoreConflict("active rule-set version changed")


__all__ = [
    "GradeDictionaryEntryRecord",
    "LEGACY_SCHEMA_VERSION",
    "RuleDefinitionRecord",
    "RuleSetRecord",
    "RuleSetVersionRecord",
    "RuleStore",
    "RuleStoreConflict",
    "RuleStoreSchemaError",
    "SCHEMA_VERSION",
]
