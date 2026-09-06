import sqlite3
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Event

import pytest

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_scheduler.api.rule_management import SetActiveRulesRequest
from apsgo_scheduler.app.rule_set_compiler import dumps_rule_set_spec
from apsgo_scheduler.app.rule_set_loader import RuleSetLoadError, fingerprint_rule_set_spec
from apsgo_v7_service import rule_management as service_module
from apsgo_v7_service.gqga4 import (
    GQGA4_INITIAL_RULES,
    GQGA4_INITIAL_VIRTUAL_PROTOTYPES,
    compile_gqga4_rule_set,
    normalize_gqga4_rule_snapshot,
)
from apsgo_v7_service.rule_management import (
    RuleManagementServiceError,
    get_active_gqga4_rules,
    set_active_gqga4_rules,
)
from apsgo_v7_service.rule_store import RuleStore

STAMP = "2026-09-07T00:00:00.000000+00:00"
ACTOR = "rule-service-test"
NOW = datetime(2026, 9, 7, 10, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8)))
EXPECTED_NOW = "2026-09-07T02:00:00.123456+00:00"
OPERATION_A = "00000000-0000-4000-8000-000000000101"
OPERATION_B = "00000000-0000-4000-8000-000000000102"


def _seed_active_v1(database_path):
    rules, prototypes = normalize_gqga4_rule_snapshot(
        GQGA4_INITIAL_RULES, GQGA4_INITIAL_VIRTUAL_PROTOTYPES
    )
    compiled = compile_gqga4_rule_set(rules, 1)
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
            store.activate_version(rule_set_id, version_id, None, updated_at=STAMP)
    return rule_set_id, version_id


def _replace_rule(rules, rule_id, **parameter_changes):
    result = []
    for rule in rules:
        if rule.rule_id == rule_id:
            parameters = dict(rule.parameters)
            parameters.update(parameter_changes)
            rule = replace(rule, parameters=parameters)
        result.append(rule)
    return tuple(result)


def _request(
    *,
    operation_id=OPERATION_A,
    expected_active_version_id=1,
    rules=None,
    prototypes=None,
    remark="调整规则",
):
    return SetActiveRulesRequest(
        save_operation_id=operation_id,
        expected_active_version_id=expected_active_version_id,
        rules=(
            _replace_rule(
                GQGA4_INITIAL_RULES,
                "chain_weight_range",
                min_weight=Decimal("701.0"),
            )
            if rules is None
            else rules
        ),
        virtual_prototypes=(GQGA4_INITIAL_VIRTUAL_PROTOTYPES if prototypes is None else prototypes),
        remark=remark,
    )


def _db_state(database_path):
    with sqlite3.connect(database_path) as connection:
        return {
            "rule_sets": connection.execute("SELECT COUNT(*) FROM v7_rule_set").fetchone()[0],
            "versions": connection.execute("SELECT COUNT(*) FROM v7_rule_set_version").fetchone()[
                0
            ],
            "rules": connection.execute("SELECT COUNT(*) FROM v7_rule_definition").fetchone()[0],
            "active": connection.execute(
                "SELECT active_version_id FROM v7_rule_set WHERE product_line_code = 'GQGA4'"
            ).fetchone()[0],
            "version_numbers": tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version_no FROM v7_rule_set_version ORDER BY version_no"
                )
            ),
            "base_versions": tuple(
                row[0]
                for row in connection.execute(
                    "SELECT based_on_version_id FROM v7_rule_set_version ORDER BY version_no"
                )
            ),
        }


def test_save_creates_one_complete_next_version_and_activates_it(tmp_path):
    database_path = tmp_path / "rules.sqlite3"
    _, initial_version_id = _seed_active_v1(database_path)

    response = set_active_gqga4_rules(
        _request(remark="  调整链重参数  "),
        database_path,
        audit_actor=f"  {ACTOR}  ",
        clock=lambda: NOW,
    )

    assert response.previous_active_version_id == initial_version_id
    assert response.saved_version_id == response.active_rules.active_version_id
    assert response.saved_version_is_active is True
    assert response.idempotent_replay is False
    assert response.active_rules.rule_set_spec.version == "2"
    assert response.active_rules.remark == "调整链重参数"
    assert response.active_rules.activated_by == ACTOR
    assert response.active_rules.activated_at == EXPECTED_NOW
    assert len(response.active_rules.rule_set_spec.rules) == 17
    assert len(response.active_rules.virtual_prototypes) == 27
    assert _db_state(database_path) == {
        "rule_sets": 1,
        "versions": 2,
        "rules": 34,
        "active": response.saved_version_id,
        "version_numbers": (1, 2),
        "base_versions": (None, initial_version_id),
    }

    with RuleStore.open(database_path) as store:
        version = store.find_version(response.saved_version_id)
        definitions = store.list_rule_definitions(response.saved_version_id)
    assert version.created_by == version.activated_by == ACTOR
    assert version.created_at == version.activated_at == EXPECTED_NOW
    assert version.remark == "调整链重参数"
    assert [item.sequence_no for item in definitions] == list(range(1, 18))
    assert get_active_gqga4_rules(database_path) == response.active_rules


def test_default_audit_actor_is_server_owned(tmp_path):
    database_path = tmp_path / "rules.sqlite3"
    _seed_active_v1(database_path)

    response = set_active_gqga4_rules(_request(), database_path, clock=lambda: NOW)

    assert response.active_rules.activated_by == "v7-rule-service"


def test_normalized_equal_retry_replays_before_stale_version_check(tmp_path):
    database_path = tmp_path / "rules.sqlite3"
    _seed_active_v1(database_path)
    first_request = _request(remark="  同一次保存  ")
    first = set_active_gqga4_rules(first_request, database_path, clock=lambda: NOW)
    equivalent_rules = _replace_rule(
        tuple(reversed(first_request.rules)),
        "chain_weight_range",
        min_weight=Decimal("701.00"),
    )

    replay = set_active_gqga4_rules(
        _request(rules=equivalent_rules, remark="同一次保存"),
        database_path,
        clock=lambda: NOW,
    )

    assert replay.saved_version_id == first.saved_version_id
    assert replay.active_rules.active_version_id == first.saved_version_id
    assert replay.saved_version_is_active is True
    assert replay.idempotent_replay is True
    assert _db_state(database_path)["versions"] == 2
    assert _db_state(database_path)["rules"] == 34


@pytest.mark.parametrize(
    "changed_part",
    (
        "rule",
        "rule_enabled",
        "prototype_order",
        "prototype_content",
        "remark",
        "expected",
    ),
)
def test_same_operation_with_changed_normalized_payload_conflicts(tmp_path, changed_part):
    database_path = tmp_path / "rules.sqlite3"
    _seed_active_v1(database_path)
    original = _request()
    saved = set_active_gqga4_rules(original, database_path, clock=lambda: NOW)
    values = {
        "operation_id": OPERATION_A,
        "expected_active_version_id": original.expected_active_version_id,
        "rules": original.rules,
        "prototypes": original.virtual_prototypes,
        "remark": original.remark,
    }
    if changed_part == "rule":
        values["rules"] = _replace_rule(
            original.rules, "chain_weight_range", min_weight=Decimal("702.0")
        )
    elif changed_part == "rule_enabled":
        values["rules"] = tuple(
            replace(rule, enabled=not rule.enabled)
            if rule.rule_id == "forbid_consecutive_reverse_width"
            else rule
            for rule in original.rules
        )
    elif changed_part == "prototype_order":
        values["prototypes"] = tuple(reversed(original.virtual_prototypes))
    elif changed_part == "prototype_content":
        values["prototypes"] = (
            replace(original.virtual_prototypes[0], unit_weight=Decimal("21")),
            *original.virtual_prototypes[1:],
        )
    elif changed_part == "remark":
        values["remark"] = "另一项修改"
    else:
        values["expected_active_version_id"] = saved.saved_version_id

    with pytest.raises(RuleManagementServiceError) as caught:
        set_active_gqga4_rules(_request(**values), database_path, clock=lambda: NOW)

    assert caught.value.code == "operation_payload_conflict"
    assert caught.value.current_active_version_id == saved.saved_version_id
    assert _db_state(database_path)["versions"] == 2
    assert _db_state(database_path)["rules"] == 34


def test_save_operation_id_is_not_part_of_normalized_request_hash(tmp_path):
    request_hashes = []
    for index, operation_id in enumerate((OPERATION_A, OPERATION_B)):
        database_path = tmp_path / f"rules-{index}.sqlite3"
        _seed_active_v1(database_path)
        response = set_active_gqga4_rules(
            _request(operation_id=operation_id), database_path, clock=lambda: NOW
        )
        with RuleStore.open(database_path) as store:
            request_hashes.append(store.find_version(response.saved_version_id).request_hash)

    assert request_hashes[0] == request_hashes[1]


def test_large_finite_decimal_is_saved_and_read_without_precision_loss(tmp_path):
    database_path = tmp_path / "rules.sqlite3"
    _seed_active_v1(database_path)
    unit_weight = Decimal("1e10000")
    prototypes = (
        replace(GQGA4_INITIAL_VIRTUAL_PROTOTYPES[0], unit_weight=unit_weight),
        *GQGA4_INITIAL_VIRTUAL_PROTOTYPES[1:],
    )

    saved = set_active_gqga4_rules(
        _request(prototypes=prototypes), database_path, clock=lambda: NOW
    )

    assert saved.active_rules.virtual_prototypes[0].unit_weight == unit_weight
    assert get_active_gqga4_rules(database_path).virtual_prototypes[0].unit_weight == unit_weight


def test_fresh_operation_with_stale_expected_version_conflicts_without_write(tmp_path):
    database_path = tmp_path / "rules.sqlite3"
    _seed_active_v1(database_path)
    first = set_active_gqga4_rules(_request(), database_path, clock=lambda: NOW)

    with pytest.raises(RuleManagementServiceError) as caught:
        set_active_gqga4_rules(_request(operation_id=OPERATION_B), database_path, clock=lambda: NOW)

    assert caught.value.code == "active_version_conflict"
    assert caught.value.expected_active_version_id == 1
    assert caught.value.current_active_version_id == first.saved_version_id
    assert _db_state(database_path)["versions"] == 2


@pytest.mark.parametrize("second_operation_id", (OPERATION_A, OPERATION_B))
def test_two_overlapping_saves_use_separate_connections_and_one_version(
    tmp_path, second_operation_id
):
    database_path = tmp_path / "rules.sqlite3"
    _seed_active_v1(database_path)
    first_has_write_lock = Event()
    release_first = Event()
    second_started = Event()

    def held_clock():
        first_has_write_lock.set()
        assert release_first.wait(2.0)
        return NOW

    def save_second():
        second_started.set()
        return set_active_gqga4_rules(
            _request(operation_id=second_operation_id),
            database_path,
            clock=lambda: NOW,
            timeout_seconds=2.0,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            set_active_gqga4_rules,
            _request(operation_id=OPERATION_A),
            database_path,
            clock=held_clock,
            timeout_seconds=2.0,
        )
        assert first_has_write_lock.wait(1.0)
        second = executor.submit(save_second)
        assert second_started.wait(1.0)
        try:
            with pytest.raises(FutureTimeoutError):
                second.result(timeout=0.05)
        finally:
            release_first.set()
        first_response = first.result(timeout=2.0)

        if second_operation_id == OPERATION_A:
            second_response = second.result(timeout=2.0)
            assert second_response.saved_version_id == first_response.saved_version_id
            assert second_response.idempotent_replay is True
        else:
            with pytest.raises(RuleManagementServiceError) as caught:
                second.result(timeout=2.0)
            assert caught.value.code == "active_version_conflict"
            assert caught.value.current_active_version_id == first_response.saved_version_id

    assert _db_state(database_path)["versions"] == 2
    assert _db_state(database_path)["rules"] == 34


def test_replay_of_superseded_save_returns_current_without_reactivation(tmp_path):
    database_path = tmp_path / "rules.sqlite3"
    _seed_active_v1(database_path)
    first = set_active_gqga4_rules(_request(), database_path, clock=lambda: NOW)
    second = set_active_gqga4_rules(
        _request(
            operation_id=OPERATION_B,
            expected_active_version_id=first.saved_version_id,
        ),
        database_path,
        clock=lambda: NOW,
    )

    replay = set_active_gqga4_rules(_request(), database_path, clock=lambda: NOW)

    assert replay.previous_active_version_id == 1
    assert replay.saved_version_id == first.saved_version_id
    assert replay.active_rules.active_version_id == second.saved_version_id
    assert replay.saved_version_is_active is False
    assert replay.idempotent_replay is True
    assert _db_state(database_path)["version_numbers"] == (1, 2, 3)
    assert _db_state(database_path)["base_versions"] == (None, 1, 2)


def test_compile_failure_writes_nothing_and_does_not_consume_version_number(tmp_path):
    database_path = tmp_path / "rules.sqlite3"
    _seed_active_v1(database_path)
    invalid_rules = _replace_rule(
        GQGA4_INITIAL_RULES,
        "chain_weight_range",
        min_weight=Decimal("2001.0"),
    )

    with pytest.raises(RuleSetLoadError):
        set_active_gqga4_rules(_request(rules=invalid_rules), database_path, clock=lambda: NOW)
    assert _db_state(database_path)["version_numbers"] == (1,)
    assert _db_state(database_path)["rules"] == 17

    fixed = set_active_gqga4_rules(_request(), database_path, clock=lambda: NOW)
    assert fixed.active_rules.rule_set_spec.version == "2"


@pytest.mark.parametrize(
    ("trigger_name", "trigger_sql"),
    (
        (
            "test_fail_version_insert",
            """
            CREATE TRIGGER test_fail_version_insert
            BEFORE INSERT ON v7_rule_set_version
            BEGIN SELECT RAISE(ABORT, 'forced version failure'); END
            """,
        ),
        (
            "test_fail_rule_insert",
            """
            CREATE TRIGGER test_fail_rule_insert
            BEFORE INSERT ON v7_rule_definition
            WHEN NEW.sequence_no = 8
            BEGIN SELECT RAISE(ABORT, 'forced rule failure'); END
            """,
        ),
        (
            "test_fail_activation",
            """
            CREATE TRIGGER test_fail_activation
            BEFORE UPDATE OF active_version_id ON v7_rule_set
            WHEN NEW.active_version_id <> OLD.active_version_id
            BEGIN SELECT RAISE(ABORT, 'forced activation failure'); END
            """,
        ),
    ),
)
def test_database_failure_rolls_back_the_whole_save(tmp_path, trigger_name, trigger_sql):
    database_path = tmp_path / "rules.sqlite3"
    _seed_active_v1(database_path)
    before = _db_state(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(trigger_sql)

    with pytest.raises(sqlite3.IntegrityError, match="forced"):
        set_active_gqga4_rules(_request(), database_path, clock=lambda: NOW)
    assert _db_state(database_path) == before

    with sqlite3.connect(database_path) as connection:
        connection.execute(f'DROP TRIGGER "{trigger_name}"')
    recovered = set_active_gqga4_rules(_request(), database_path, clock=lambda: NOW)
    assert recovered.active_rules.rule_set_spec.version == "2"


def test_response_serialization_failure_rolls_back_before_commit(tmp_path, monkeypatch):
    database_path = tmp_path / "rules.sqlite3"
    _seed_active_v1(database_path)
    before = _db_state(database_path)

    def fail_serialization(_response):
        raise ValueError("forced response failure")

    monkeypatch.setattr(service_module, "dumps_set_active_rules_response", fail_serialization)
    with pytest.raises(ValueError, match="forced response failure"):
        set_active_gqga4_rules(_request(), database_path, clock=lambda: NOW)
    assert _db_state(database_path) == before
    monkeypatch.undo()
    assert (
        set_active_gqga4_rules(
            _request(), database_path, clock=lambda: NOW
        ).active_rules.rule_set_spec.version
        == "2"
    )


def test_conditional_activation_conflict_is_translated_after_full_rollback(tmp_path, monkeypatch):
    database_path = tmp_path / "rules.sqlite3"
    _seed_active_v1(database_path)
    before = _db_state(database_path)

    original_activate = RuleStore.activate_version

    def fail_activation(self, rule_set_id, version_id, _expected, *, updated_at):
        return original_activate(
            self,
            rule_set_id,
            version_id,
            None,
            updated_at=updated_at,
        )

    monkeypatch.setattr(RuleStore, "activate_version", fail_activation)
    with pytest.raises(RuleManagementServiceError) as caught:
        set_active_gqga4_rules(_request(), database_path, clock=lambda: NOW)

    assert caught.value.code == "active_version_conflict"
    assert caught.value.current_active_version_id == before["active"]
    assert _db_state(database_path) == before
    monkeypatch.undo()
    assert (
        set_active_gqga4_rules(
            _request(), database_path, clock=lambda: NOW
        ).active_rules.rule_set_spec.version
        == "2"
    )


@pytest.mark.parametrize(
    "corruption",
    (
        "compiled_json",
        "compiled_identity",
        "compiled_version",
        "stored_fingerprint",
        "rule_count",
        "sequence",
        "rule_content",
        "prototype",
        "prototype_decimal",
        "editor_snapshot",
        "remark",
        "request_hash",
    ),
)
def test_read_active_rules_rejects_inconsistent_persisted_snapshot(tmp_path, corruption):
    database_path = tmp_path / "rules.sqlite3"
    rule_set_id, initial_version_id = _seed_active_v1(database_path)
    rules, prototypes = normalize_gqga4_rule_snapshot(
        GQGA4_INITIAL_RULES, GQGA4_INITIAL_VIRTUAL_PROTOTYPES
    )
    compiled = compile_gqga4_rule_set(rules, 2)
    stored_spec = compiled.rule_set_spec
    if corruption == "compiled_identity":
        stored_spec = replace(stored_spec, product_line_code="OTHER", fingerprint="pending")
        stored_spec = replace(stored_spec, fingerprint=fingerprint_rule_set_spec(stored_spec))
    elif corruption == "compiled_version":
        stored_spec = replace(stored_spec, version="9", fingerprint="pending")
        stored_spec = replace(stored_spec, fingerprint=fingerprint_rule_set_spec(stored_spec))
    compiled_json = "{}" if corruption == "compiled_json" else dumps_rule_set_spec(stored_spec)
    stored_fingerprint = "e" * 64 if corruption == "stored_fingerprint" else stored_spec.fingerprint
    if corruption == "prototype":
        prototype_json = "[]"
    elif corruption == "prototype_decimal":
        prototype_json = service_module._virtual_prototypes_json(prototypes).replace(
            '"unit_weight":20.0',
            '"unit_weight":1e999999999999999999999999999999999999',
            1,
        )
    else:
        prototype_json = service_module._virtual_prototypes_json(prototypes)
    remark = " 未规范化备注 " if corruption == "remark" else ""
    request_hash = (
        "f" * 64
        if corruption == "request_hash"
        else service_module._request_hash(initial_version_id, rules, prototypes, remark)
    )

    with RuleStore.open(database_path) as store:
        with store.transaction(write=True):
            version_id = store.create_version(
                rule_set_id,
                2,
                initial_version_id,
                OPERATION_A,
                request_hash,
                compiled_json,
                stored_fingerprint,
                prototype_json,
                (
                    "{}"
                    if corruption == "editor_snapshot"
                    else service_module._editor_snapshot_json(rules, prototypes, remark)
                ),
                remark,
                ACTOR,
                STAMP,
                ACTOR,
                STAMP,
            )
            definitions = compiled.rule_set_spec.rules
            if corruption == "rule_count":
                definitions = definitions[:-1]
            for sequence_no, rule in enumerate(definitions, start=1):
                if corruption == "sequence" and sequence_no == len(definitions):
                    sequence_no += 1
                store.create_rule_definition(
                    version_id,
                    sequence_no,
                    rule.rule_id,
                    rule.rule_type,
                    "损坏名称" if corruption == "rule_content" and sequence_no == 1 else rule.name,
                    rule.scope.value,
                    rule.enabled,
                    rule.version,
                    dumps_exact_json(rule.parameters),
                )
            store.activate_version(rule_set_id, version_id, initial_version_id, updated_at=STAMP)

    with pytest.raises(RuleManagementServiceError) as caught:
        get_active_gqga4_rules(database_path)
    assert caught.value.code == "stored_snapshot_inconsistent"


def test_missing_rule_set_or_active_version_is_not_created_by_service(tmp_path):
    database_path = tmp_path / "rules.sqlite3"
    with RuleStore.initialize(database_path):
        pass
    with pytest.raises(RuleManagementServiceError) as missing:
        get_active_gqga4_rules(database_path)
    assert missing.value.code == "rule_set_not_initialized"

    with RuleStore.open(database_path) as store:
        with store.transaction(write=True):
            store.create_rule_set("GQGA4", "default", "month", created_at=STAMP)
    with pytest.raises(RuleManagementServiceError) as inactive:
        set_active_gqga4_rules(_request(), database_path, clock=lambda: NOW)
    assert inactive.value.code == "rule_set_not_initialized"
    assert _db_state(database_path)["versions"] == 0


def test_server_audit_inputs_are_validated_before_writing(tmp_path):
    database_path = tmp_path / "rules.sqlite3"
    _seed_active_v1(database_path)
    before = _db_state(database_path)

    with pytest.raises(ValueError, match="audit_actor"):
        set_active_gqga4_rules(_request(), database_path, audit_actor=" ")
    with pytest.raises(ValueError, match="timezone-aware"):
        set_active_gqga4_rules(
            _request(), database_path, clock=lambda: datetime(2026, 9, 7, 10, 0, 0)
        )
    assert _db_state(database_path) == before
