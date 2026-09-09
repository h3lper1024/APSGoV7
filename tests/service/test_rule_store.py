import sqlite3

import pytest

from apsgo_v7_service import rule_store as rule_store_module
from apsgo_v7_service.rule_store import (
    LEGACY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    RuleStore,
    RuleStoreConflict,
    RuleStoreSchemaError,
)

STAMP = "2026-09-07T10:00:00+08:00"
IDENTITY = ("GQGA4", "default", "month")
HASH = "a" * 64
FINGERPRINT = "b" * 64
DICTIONARY_FINGERPRINT = "c" * 64


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
    dictionary_fingerprint=DICTIONARY_FINGERPRINT,
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
        grade_dictionary_fingerprint=dictionary_fingerprint,
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


def _create_dictionary_entry(store, version_id, **overrides):
    values = {
        "source_grade": " St04D+Z ",
        "normalized_grade": "ST04D+Z",
        "soft_hard_class": "软钢",
        "roll_type": "A",
        "steel_classes": "普通钢|IF钢",
        "is_if_steel": False,
        "enabled": True,
        "source_file": "4镀锌钢种对应.xlsx",
        "source_row_count": 1,
        "source_rows": "2",
        "remark": "",
    }
    values.update(overrides)
    return store.create_grade_dictionary_entry(version_id, **values)


def _create_v1_database(database_path, *, with_active_version=True):
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in rule_store_module._V1_SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {LEGACY_SCHEMA_VERSION}")
        if not with_active_version:
            return
        connection.execute(
            """
            INSERT INTO v7_rule_set (
                id, product_line_code, process_code, scenario,
                active_version_id, created_at, updated_at
            ) VALUES (1, ?, ?, ?, NULL, ?, ?)
            """,
            (*IDENTITY, STAMP, STAMP),
        )
        connection.execute(
            """
            INSERT INTO v7_rule_set_version (
                id, rule_set_id, version_no, based_on_version_id,
                save_operation_id, request_hash, compiled_rule_set_json,
                rule_set_fingerprint, virtual_prototypes_json,
                editor_snapshot_json, remark, created_by, created_at,
                activated_by, activated_at
            ) VALUES (1, 1, 1, NULL, ?, ?, ?, ?, '[]', '{}', '', ?, ?, ?, ?)
            """,
            (
                "legacy-operation",
                HASH,
                '{"version":1}',
                FINGERPRINT,
                "legacy",
                STAMP,
                "legacy",
                STAMP,
            ),
        )
        connection.execute(
            """
            INSERT INTO v7_rule_definition (
                rule_set_version_id, sequence_no, rule_id, rule_type,
                rule_name, scope, enabled, rule_version, parameters_json
            ) VALUES (1, 1, 'rule-one', 'ExampleRule', '示例规则', 'EDGE', 1, '1', '{}')
            """
        )
        connection.execute(
            "UPDATE v7_rule_set SET active_version_id = 1 WHERE id = 1"
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
        assert tables == {
            "v7_rule_set",
            "v7_rule_set_version",
            "v7_rule_definition",
            "v7_grade_dictionary_entry",
        }
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(v7_rule_set_version)")
        }
        assert "grade_dictionary_fingerprint" in columns
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


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
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(RuleStoreSchemaError, match="newer"):
        RuleStore.open(database_path)

    database_path.unlink()
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE v7_rule_set (id INTEGER PRIMARY KEY)")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    with pytest.raises(RuleStoreSchemaError, match="schema objects"):
        RuleStore.open(database_path)


def test_ordinary_open_and_initialize_reject_a_valid_v1_database(database_path):
    _create_v1_database(database_path)

    with pytest.raises(RuleStoreSchemaError, match="unsupported database schema version: 1"):
        RuleStore.initialize(database_path)
    with pytest.raises(RuleStoreSchemaError, match="unexpected database schema version: 1"):
        RuleStore.open(database_path)


def test_initialize_rejects_unversioned_existing_rule_store_objects(database_path):
    _create_v1_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 0")

    with pytest.raises(RuleStoreSchemaError, match="unversioned database"):
        RuleStore.initialize(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert rule_store_module._schema_signature(
            connection
        ) == rule_store_module._expected_schema_signature(LEGACY_SCHEMA_VERSION)


def test_v1_upgrade_and_fresh_v2_use_the_same_schema_chain(database_path, tmp_path):
    fresh_path = tmp_path / "fresh.sqlite3"
    with RuleStore.initialize(fresh_path):
        pass
    _create_v1_database(database_path)

    with RuleStore.open_for_schema_upgrade(database_path) as migrated:
        assert migrated.find_version(1).grade_dictionary_fingerprint is None
        with migrated.transaction(write=True):
            migrated.apply_schema_v2_upgrade()
            version_id = _create_version(
                migrated,
                1,
                version_no=2,
                operation_id="00000000-0000-4000-8000-000000000002",
                based_on_version_id=1,
            )
            _create_rule(migrated, version_id)
            _create_dictionary_entry(migrated, version_id)
            migrated.activate_version(1, version_id, 1, updated_at=STAMP)
            migrated.finalize_schema_v2()

    with RuleStore.open(database_path) as migrated, RuleStore.open(fresh_path):
        assert migrated.find_version(1).grade_dictionary_fingerprint is None
        assert migrated.find_version(2).grade_dictionary_fingerprint == DICTIONARY_FINGERPRINT
        assert len(migrated.list_grade_dictionary_entries(2)) == 1
    with sqlite3.connect(database_path) as migrated, sqlite3.connect(fresh_path) as fresh:
        assert rule_store_module._schema_signature(migrated) == rule_store_module._schema_signature(
            fresh
        )


def test_failed_v1_upgrade_rolls_back_schema_and_data(database_path):
    _create_v1_database(database_path)

    with RuleStore.open_for_schema_upgrade(database_path) as store:
        with pytest.raises(RuntimeError, match="stop migration"):
            with store.transaction(write=True):
                store.apply_schema_v2_upgrade()
                version_id = _create_version(
                    store,
                    1,
                    version_no=2,
                    operation_id="00000000-0000-4000-8000-000000000002",
                    based_on_version_id=1,
                )
                _create_dictionary_entry(store, version_id)
                raise RuntimeError("stop migration")

    with RuleStore.open_for_schema_upgrade(database_path) as reopened:
        assert reopened.find_version(1).grade_dictionary_fingerprint is None
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == LEGACY_SCHEMA_VERSION
        assert rule_store_module._schema_signature(
            connection
        ) == rule_store_module._expected_schema_signature(LEGACY_SCHEMA_VERSION)
        assert connection.execute("SELECT COUNT(*) FROM v7_rule_set_version").fetchone()[0] == 1


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


def test_schema_with_a_weakened_dictionary_trigger_is_rejected(database_path):
    with RuleStore.initialize(database_path):
        pass
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER trg_v7_grade_dictionary_no_update")
        connection.execute(
            """
            CREATE TRIGGER trg_v7_grade_dictionary_no_update
            BEFORE UPDATE ON v7_grade_dictionary_entry
            BEGIN
                SELECT 1;
            END
            """
        )
    with pytest.raises(RuleStoreSchemaError, match="schema objects"):
        RuleStore.open(database_path)


def test_schema_upgrade_open_rejects_a_weakened_v1_schema(database_path):
    _create_v1_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER trg_v7_rule_definition_no_update")

    with pytest.raises(RuleStoreSchemaError, match="schema version 1"):
        RuleStore.open_for_schema_upgrade(database_path)


def test_writes_require_one_explicit_non_nested_transaction(store):
    with pytest.raises(RuntimeError, match="immediate transaction"):
        _create_rule_set(store)
    with pytest.raises(RuntimeError, match="immediate transaction"):
        _create_dictionary_entry(store, 1)
    with pytest.raises(RuntimeError, match="immediate transaction"):
        store.copy_grade_dictionary_entries(1, 2)
    with pytest.raises(RuntimeError, match="immediate transaction"):
        store.apply_schema_v2_upgrade()
    with pytest.raises(RuntimeError, match="immediate transaction"):
        store.finalize_schema_v2()
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


def test_dictionary_entries_are_sorted_and_copied_to_a_forward_version(store):
    with store.transaction(write=True):
        rule_set_id = _create_rule_set(store)
        first_version_id = _create_version(store, rule_set_id)
        _create_dictionary_entry(
            store,
            first_version_id,
            source_grade="ST280D+Z",
            normalized_grade="ST280D+Z",
            soft_hard_class="硬钢",
            roll_type="B",
            steel_classes="高强钢",
            is_if_steel=True,
            enabled=False,
            source_row_count=2,
            source_rows="3|4",
            remark="disabled source row",
        )
        _create_dictionary_entry(store, first_version_id)
        store.activate_version(rule_set_id, first_version_id, None, updated_at=STAMP)
    with store.transaction(write=True):
        second_version_id = _create_version(
            store,
            rule_set_id,
            version_no=2,
            operation_id="00000000-0000-4000-8000-000000000002",
            based_on_version_id=first_version_id,
        )
        assert (
            store.copy_grade_dictionary_entries(first_version_id, second_version_id) == 2
        )
        store.activate_version(
            rule_set_id, second_version_id, first_version_id, updated_at=STAMP
        )

    entries = store.list_grade_dictionary_entries(second_version_id)
    assert [entry.normalized_grade for entry in entries] == ["ST04D+Z", "ST280D+Z"]
    assert entries[0].rule_set_version_id == second_version_id
    assert entries[0].source_grade == " St04D+Z "
    assert entries[0].soft_hard_class == "软钢"
    assert entries[0].roll_type == "A"
    assert entries[0].steel_classes == "普通钢|IF钢"
    assert entries[0].is_if_steel is False
    assert entries[0].enabled is True
    assert entries[0].source_file == "4镀锌钢种对应.xlsx"
    assert entries[0].source_row_count == 1
    assert entries[0].source_rows == "2"
    assert entries[0].remark == ""
    assert entries[1].is_if_steel is True
    assert entries[1].enabled is False


@pytest.mark.parametrize(
    "overrides",
    (
        {"source_grade": " "},
        {"normalized_grade": "OTHER"},
        {"soft_hard_class": "软钢|硬钢"},
        {"roll_type": ""},
        {"is_if_steel": 2},
        {"enabled": -1},
        {"source_row_count": -1},
        {"source_row_count": 1.5},
    ),
)
def test_dictionary_constraints_reject_invalid_rows(store, overrides):
    with store.transaction(write=True):
        rule_set_id = _create_rule_set(store)
        version_id = _create_version(store, rule_set_id)

    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction(write=True):
            _create_dictionary_entry(store, version_id, **overrides)
    assert store.list_grade_dictionary_entries(version_id) == ()


def test_dictionary_identity_foreign_key_and_version_fingerprint_are_constrained(store):
    with store.transaction(write=True):
        rule_set_id = _create_rule_set(store)
        version_id = _create_version(store, rule_set_id)
        _create_dictionary_entry(store, version_id)

    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction(write=True):
            _create_dictionary_entry(store, version_id)
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction(write=True):
            _create_dictionary_entry(store, 999)
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction(write=True):
            _create_version(
                store,
                rule_set_id,
                version_no=2,
                operation_id="00000000-0000-4000-8000-000000000002",
                dictionary_fingerprint="NOT-A-FINGERPRINT",
            )

    assert len(store.list_grade_dictionary_entries(version_id)) == 1


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
    assert version.grade_dictionary_fingerprint == DICTIONARY_FINGERPRINT
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


def test_dictionary_insert_failure_rolls_back_the_version_rules_and_dictionary(
    store, database_path
):
    with store.transaction(write=True):
        rule_set_id = _create_rule_set(store)

    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction(write=True):
            version_id = _create_version(store, rule_set_id)
            _create_rule(store, version_id)
            _create_dictionary_entry(store, version_id)
            _create_dictionary_entry(store, version_id)
    assert _count(database_path, "v7_rule_set_version") == 0
    assert _count(database_path, "v7_rule_definition") == 0
    assert _count(database_path, "v7_grade_dictionary_entry") == 0


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
        _create_dictionary_entry(store, historical_version_id)
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
        assert (
            store.copy_grade_dictionary_entries(historical_version_id, active_version_id)
            == 1
        )
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
        (
            """
            UPDATE v7_grade_dictionary_entry
            SET soft_hard_class = '硬钢'
            WHERE rule_set_version_id = ?
            """,
            historical_version_id,
        ),
        (
            "DELETE FROM v7_grade_dictionary_entry WHERE rule_set_version_id = ?",
            historical_version_id,
        ),
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
    for version_id in (historical_version_id, active_version_id):
        with pytest.raises(sqlite3.IntegrityError):
            with store.transaction(write=True):
                _create_dictionary_entry(
                    store,
                    version_id,
                    source_grade="DC01",
                    normalized_grade="DC01",
                )
    assert store.find_version(historical_version_id).remark == ""
    assert len(store.list_rule_definitions(historical_version_id)) == 1
    assert len(store.list_grade_dictionary_entries(historical_version_id)) == 1
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
