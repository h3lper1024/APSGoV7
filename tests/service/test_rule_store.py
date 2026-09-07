import sqlite3

import pytest

from apsgo_v7_service.rule_store import (
    RuleStore,
    RuleStoreConflict,
    RuleStoreSchemaError,
)

STAMP = "2026-09-07T10:00:00+08:00"
IDENTITY = ("GQGA4", "default", "month")
HASH = "a" * 64
FINGERPRINT = "b" * 64


@pytest.fixture
def database_path(tmp_path):
    return tmp_path / "rules.sqlite3"


@pytest.fixture
def store(database_path):
    with RuleStore.initialize(database_path) as value:
        yield value


def _create_rule_set(store, suffix=""):
    return store.create_rule_set(
        f"{IDENTITY[0]}{suffix}",
        IDENTITY[1],
        IDENTITY[2],
        created_at=STAMP,
    )


def _create_version(
    store,
    rule_set_id,
    version_no=1,
    operation_id="00000000-0000-4000-8000-000000000001",
    based_on_version_id=None,
):
    return store.create_version(
        rule_set_id,
        version_no,
        based_on_version_id,
        operation_id,
        HASH,
        f'{{"version":{version_no}}}',
        FINGERPRINT,
        "[]",
        "{}",
        "",
        "service-process",
        STAMP,
        "service-process",
        STAMP,
    )


def _create_rule(store, version_id, sequence_no=1, rule_id="rule-one"):
    return store.create_rule_definition(
        version_id,
        sequence_no,
        rule_id,
        "ExampleRule",
        "示例规则",
        "EDGE",
        True,
        "1",
        "{}",
    )


def _count(database_path, table):
    with sqlite3.connect(database_path) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_database_path_must_be_explicit_and_nonblank(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TypeError, match="database_path"):
        RuleStore.initialize()
    with pytest.raises(ValueError, match="database_path"):
        RuleStore.initialize(None)
    with pytest.raises(ValueError, match="database_path"):
        RuleStore.open("")
    assert not (tmp_path / "data").exists()


def test_empty_database_initializes_and_reopens_without_changing_data(database_path):
    with RuleStore.initialize(database_path) as first:
        with first.transaction(write=True):
            rule_set_id = _create_rule_set(first)

    with RuleStore.open(database_path) as reopened:
        assert reopened.find_rule_set(*IDENTITY).id == rule_set_id

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'v7_%'"
            )
        }
        assert tables == {"v7_rule_set", "v7_rule_set_version", "v7_rule_definition"}
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_open_never_creates_a_missing_database(database_path):
    with pytest.raises(FileNotFoundError):
        RuleStore.open(database_path)
    assert not database_path.exists()


def test_empty_temporary_database_can_be_removed_and_reinitialized(database_path):
    with RuleStore.initialize(database_path):
        pass
    database_path.unlink()
    with RuleStore.initialize(database_path):
        pass
    assert database_path.is_file()


def test_newer_or_incompatible_schema_is_rejected(database_path):
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 2")
    with pytest.raises(RuleStoreSchemaError, match="newer"):
        RuleStore.open(database_path)

    database_path.unlink()
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE v7_rule_set (id INTEGER PRIMARY KEY)")
        connection.execute("PRAGMA user_version = 1")
    with pytest.raises(RuleStoreSchemaError, match="schema objects"):
        RuleStore.open(database_path)


def test_schema_with_a_same_named_but_weakened_trigger_is_rejected(database_path):
    with RuleStore.initialize(database_path):
        pass
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER trg_v7_rule_set_version_no_update")
        connection.execute(
            """
            CREATE TRIGGER trg_v7_rule_set_version_no_update
            BEFORE UPDATE ON v7_rule_set_version
            BEGIN
                SELECT 1;
            END
            """
        )
    with pytest.raises(RuleStoreSchemaError, match="schema objects"):
        RuleStore.open(database_path)


def test_writes_require_one_explicit_non_nested_transaction(store):
    with pytest.raises(RuntimeError, match="immediate transaction"):
        _create_rule_set(store)
    with store.transaction():
        with pytest.raises(RuntimeError, match="immediate transaction"):
            _create_rule_set(store)
    with store.transaction(write=True):
        _create_rule_set(store)
        with pytest.raises(RuntimeError, match="nested"):
            with store.transaction(write=True):
                pass


def test_business_identity_is_unique_in_the_database(store):
    with store.transaction(write=True):
        _create_rule_set(store)
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction(write=True):
            _create_rule_set(store)
    assert store.find_rule_set(*IDENTITY) is not None


def test_version_number_and_operation_id_are_unique_within_each_rule_set(store):
    with store.transaction(write=True):
        rule_set_id = _create_rule_set(store)
        _create_version(store, rule_set_id)

    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction(write=True):
            _create_version(
                store,
                rule_set_id,
                version_no=1,
                operation_id="00000000-0000-4000-8000-000000000002",
            )
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction(write=True):
            _create_version(store, rule_set_id, version_no=2)

    with store.transaction(write=True):
        other_rule_set_id = _create_rule_set(store, "-OTHER")
        _create_version(store, other_rule_set_id)
    assert store.find_version_by_operation(rule_set_id, "00000000-0000-4000-8000-000000000001")


def test_rule_identity_and_sequence_are_unique_within_each_version(store):
    with store.transaction(write=True):
        rule_set_id = _create_rule_set(store)
        version_id = _create_version(store, rule_set_id)
        _create_rule(store, version_id)

    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction(write=True):
            _create_rule(store, version_id, sequence_no=2)
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction(write=True):
            _create_rule(store, version_id, rule_id="rule-two")
    assert [item.rule_id for item in store.list_rule_definitions(version_id)] == ["rule-one"]


def test_complete_version_can_be_written_and_activated_atomically(store):
    with store.transaction(write=True):
        rule_set_id = _create_rule_set(store)
        assert store.next_version_no(rule_set_id) == 1
        version_id = _create_version(store, rule_set_id)
        _create_rule(store, version_id, sequence_no=2, rule_id="rule-two")
        _create_rule(store, version_id)
        store.activate_version(rule_set_id, version_id, None, updated_at=STAMP)

    rule_set = store.find_rule_set(*IDENTITY)
    version = store.find_version(version_id)
    assert rule_set.active_version_id == version_id
    assert version.compiled_rule_set_json == '{"version":1}'
    assert version.version_no == 1
    assert version.based_on_version_id is None
    assert version.save_operation_id == "00000000-0000-4000-8000-000000000001"
    assert version.request_hash == HASH
    assert version.rule_set_fingerprint == FINGERPRINT
    assert version.virtual_prototypes_json == "[]"
    assert version.editor_snapshot_json == "{}"
    assert version.remark == ""
    assert version.created_by == "service-process"
    assert version.created_at == STAMP
    assert version.activated_by == "service-process"
    assert version.activated_at == STAMP
    definitions = store.list_rule_definitions(version_id)
    assert [item.sequence_no for item in definitions] == [1, 2]
    definition = definitions[0]
    assert (
        definition.rule_set_version_id,
        definition.sequence_no,
        definition.rule_id,
        definition.rule_type,
        definition.rule_name,
        definition.scope,
        definition.enabled,
        definition.rule_version,
        definition.parameters_json,
    ) == (version_id, 1, "rule-one", "ExampleRule", "示例规则", "EDGE", True, "1", "{}")


def test_composite_foreign_keys_reject_cross_rule_set_base_and_active_versions(store):
    with store.transaction(write=True):
        first_id = _create_rule_set(store)
        second_id = _create_rule_set(store, "-OTHER")
        first_version_id = _create_version(store, first_id)

    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction(write=True):
            _create_version(
                store,
                second_id,
                operation_id="00000000-0000-4000-8000-000000000002",
                based_on_version_id=first_version_id,
            )
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction(write=True):
            store.activate_version(second_id, first_version_id, None, updated_at=STAMP)

    assert store.find_rule_set("GQGA4-OTHER", "default", "month").active_version_id is None


def test_version_insert_failure_rolls_back_every_write(store, database_path):
    with store.transaction(write=True):
        rule_set_id = _create_rule_set(store)

    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction(write=True):
            _create_version(store, rule_set_id)
            _create_version(
                store,
                rule_set_id,
                operation_id="00000000-0000-4000-8000-000000000002",
            )
    assert _count(database_path, "v7_rule_set_version") == 0


def test_rule_insert_failure_rolls_back_the_version_and_rules(store, database_path):
    with store.transaction(write=True):
        rule_set_id = _create_rule_set(store)

    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction(write=True):
            version_id = _create_version(store, rule_set_id)
            _create_rule(store, version_id)
            _create_rule(store, version_id, rule_id="rule-two")
    assert _count(database_path, "v7_rule_set_version") == 0
    assert _count(database_path, "v7_rule_definition") == 0


def test_active_pointer_failure_rolls_back_the_new_version(store, database_path):
    with store.transaction(write=True):
        rule_set_id = _create_rule_set(store)
        first_version_id = _create_version(store, rule_set_id)
        _create_rule(store, first_version_id)
        store.activate_version(rule_set_id, first_version_id, None, updated_at=STAMP)

    with pytest.raises(RuleStoreConflict, match="active rule-set version changed"):
        with store.transaction(write=True):
            second_version_id = _create_version(
                store,
                rule_set_id,
                version_no=2,
                operation_id="00000000-0000-4000-8000-000000000002",
                based_on_version_id=first_version_id,
            )
            store.activate_version(rule_set_id, second_version_id, None, updated_at=STAMP)
    assert _count(database_path, "v7_rule_set_version") == 1
    assert store.find_rule_set(*IDENTITY).active_version_id == first_version_id


def test_active_and_historical_versions_are_immutable(store, database_path):
    with store.transaction(write=True):
        rule_set_id = _create_rule_set(store)
        historical_version_id = _create_version(store, rule_set_id)
        historical_rule_id = _create_rule(store, historical_version_id)
        store.activate_version(rule_set_id, historical_version_id, None, updated_at=STAMP)
    with store.transaction(write=True):
        active_version_id = _create_version(
            store,
            rule_set_id,
            version_no=2,
            operation_id="00000000-0000-4000-8000-000000000002",
            based_on_version_id=historical_version_id,
        )
        _create_rule(store, active_version_id)
        store.activate_version(
            rule_set_id, active_version_id, historical_version_id, updated_at=STAMP
        )

    statements = (
        (
            "UPDATE v7_rule_set_version SET remark = 'changed' WHERE id = ?",
            historical_version_id,
        ),
        ("DELETE FROM v7_rule_set_version WHERE id = ?", historical_version_id),
        ("UPDATE v7_rule_definition SET enabled = 0 WHERE id = ?", historical_rule_id),
        ("DELETE FROM v7_rule_definition WHERE id = ?", historical_rule_id),
        ("UPDATE v7_rule_set SET scenario = 'day' WHERE id = ?", rule_set_id),
        ("DELETE FROM v7_rule_set WHERE id = ?", rule_set_id),
    )
    for statement, identity in statements:
        with sqlite3.connect(database_path) as connection:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, (identity,))

    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction(write=True):
            _create_rule(store, historical_version_id, sequence_no=2, rule_id="late-rule")
    assert store.find_version(historical_version_id).remark == ""
    assert len(store.list_rule_definitions(historical_version_id)) == 1
    assert store.find_rule_set(*IDENTITY).active_version_id == active_version_id


def test_active_pointer_cannot_be_cleared_or_moved_backwards(store, database_path):
    with store.transaction(write=True):
        rule_set_id = _create_rule_set(store)
        first_version_id = _create_version(store, rule_set_id)
        _create_rule(store, first_version_id)
        store.activate_version(rule_set_id, first_version_id, None, updated_at=STAMP)
    with store.transaction(write=True):
        second_version_id = _create_version(
            store,
            rule_set_id,
            version_no=2,
            operation_id="00000000-0000-4000-8000-000000000002",
            based_on_version_id=first_version_id,
        )
        _create_rule(store, second_version_id)
        store.activate_version(rule_set_id, second_version_id, first_version_id, updated_at=STAMP)

    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction(write=True):
            store.activate_version(
                rule_set_id, first_version_id, second_version_id, updated_at=STAMP
            )
    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE v7_rule_set SET active_version_id = NULL WHERE id = ?", (rule_set_id,)
            )
    assert store.find_rule_set(*IDENTITY).active_version_id == second_version_id


def test_begin_immediate_prevents_a_second_writer(database_path):
    first = RuleStore.initialize(database_path, timeout_seconds=0.01)
    second = RuleStore.open(database_path, timeout_seconds=0.01)
    try:
        with first.transaction(write=True):
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                with second.transaction(write=True):
                    pass
    finally:
        second.close()
        first.close()


def test_open_and_read_do_not_request_a_write_lock(database_path):
    first = RuleStore.initialize(database_path, timeout_seconds=0.01)
    try:
        with first.transaction(write=True):
            _create_rule_set(first)
            with RuleStore.open(database_path, timeout_seconds=0.01) as reader:
                with reader.transaction():
                    assert reader.find_rule_set(*IDENTITY) is None
        with RuleStore.open(database_path, timeout_seconds=0.01) as reader:
            assert reader.find_rule_set(*IDENTITY) is not None
    finally:
        first.close()
