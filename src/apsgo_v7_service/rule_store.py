"""SQLite storage primitives for immutable APSGo V7 rule-set versions."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from pathlib import Path

SCHEMA_VERSION = 1
DATABASE_PATH_ENVIRONMENT_VARIABLE = "APSGO_V7_RULE_DB_PATH"
DEFAULT_DATABASE_PATH = Path("data/apsgo_v7_rules.sqlite3")


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


_SCHEMA_STATEMENTS = (
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
def _expected_schema_signature() -> tuple[tuple[str, str, str], ...]:
    with sqlite3.connect(":memory:") as reference:
        for statement in _SCHEMA_STATEMENTS:
            reference.execute(statement)
        return _schema_signature(reference)


def configured_database_path(environment: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environment is None else environment
    configured = values.get(DATABASE_PATH_ENVIRONMENT_VARIABLE)
    if configured is None:
        return DEFAULT_DATABASE_PATH
    if not configured.strip():
        raise ValueError(f"{DATABASE_PATH_ENVIRONMENT_VARIABLE} must not be blank")
    return Path(configured.strip())


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
        path = configured_database_path() if database_path is None else database_path
        if not str(path).strip():
            raise ValueError("database_path must not be blank")
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
    def initialize(
        cls, database_path: str | Path | None = None, *, timeout_seconds: float = 5.0
    ) -> RuleStore:
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
    def open(
        cls, database_path: str | Path | None = None, *, timeout_seconds: float = 5.0
    ) -> RuleStore:
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
                for statement in _SCHEMA_STATEMENTS:
                    self._connection.execute(statement)
                if current == 0:
                    self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._verify_schema()
        except RuleStoreSchemaError:
            raise
        except sqlite3.DatabaseError as error:
            raise RuleStoreSchemaError("database schema initialization failed") from error

    def _verify_schema(self) -> None:
        current = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuleStoreSchemaError(
                f"database schema version {current} is newer than supported {SCHEMA_VERSION}"
            )
        if current != SCHEMA_VERSION:
            raise RuleStoreSchemaError(f"unexpected database schema version: {current}")
        if _schema_signature(self._connection) != _expected_schema_signature():
            raise RuleStoreSchemaError("database schema objects do not match schema version 1")
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
    ) -> int:
        self._require_write_transaction()
        cursor = self._connection.execute(
            """
            INSERT INTO v7_rule_set_version (
                rule_set_id, version_no, based_on_version_id, save_operation_id,
                request_hash, compiled_rule_set_json, rule_set_fingerprint,
                virtual_prototypes_json, editor_snapshot_json, remark,
                created_by, created_at, activated_by, activated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    "DATABASE_PATH_ENVIRONMENT_VARIABLE",
    "DEFAULT_DATABASE_PATH",
    "RuleDefinitionRecord",
    "RuleSetRecord",
    "RuleSetVersionRecord",
    "RuleStore",
    "RuleStoreConflict",
    "RuleStoreSchemaError",
    "SCHEMA_VERSION",
    "configured_database_path",
]
