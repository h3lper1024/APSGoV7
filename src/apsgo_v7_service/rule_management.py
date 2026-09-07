"""Transactional GQGA4 rule initialization, reads, and save-and-activate operations."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
from pathlib import Path

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_scheduler.api.request import VirtualPrototypeInput
from apsgo_scheduler.api.rule_management import (
    ActiveRulesResponse,
    EditableRuleInput,
    SetActiveRulesRequest,
    SetActiveRulesResponse,
    dumps_active_rules_response,
    dumps_set_active_rules_response,
)
from apsgo_scheduler.app.rule_set_compiler import load_compiled_rule_set_json
from apsgo_scheduler.core.contracts import fingerprint

from .grade_dictionary import GradeDictionaryEntry, GradeDictionarySnapshot
from .gqga4 import (
    GQGA4_INITIAL_RULES,
    GQGA4_INITIAL_VIRTUAL_PROTOTYPES,
    GQGA4_RULE_SET_TEMPLATE,
    compile_gqga4_rule_set,
    normalize_gqga4_rule_snapshot,
)
from .rule_store import RuleSetRecord, RuleStore, RuleStoreConflict

DEFAULT_AUDIT_ACTOR = "v7-rule-service"
INITIALIZATION_OPERATION_ID = "bootstrap:gqga4:v1"
MIGRATION_AUDIT_ACTOR = "v7-grade-dictionary-migration"


@dataclass(frozen=True, slots=True)
class RuleInitializationResult:
    """The verified active snapshot and whether this call created it."""

    created: bool
    active_rules: ActiveRulesResponse
    grade_dictionary_fingerprint: str
    grade_dictionary_entry_count: int


class RuleManagementServiceError(RuntimeError):
    """A stable service-layer failure for later HTTP status mapping."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        expected_active_version_id: int | None = None,
        current_active_version_id: int | None = None,
    ):
        self.code = code
        self.expected_active_version_id = expected_active_version_id
        self.current_active_version_id = current_active_version_id
        super().__init__(message)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(clock: Callable[[], datetime]) -> str:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _audit_actor(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("audit_actor must be nonempty text")
    actor = value.strip()
    if actor == MIGRATION_AUDIT_ACTOR:
        raise ValueError("audit_actor is reserved for the grade dictionary migration")
    return actor


def _record_data(value) -> dict:
    return {part.name: getattr(value, part.name) for part in fields(value)}


def _editor_snapshot_data(
    rules: tuple[EditableRuleInput, ...],
    prototypes: tuple[VirtualPrototypeInput, ...],
    remark: str,
) -> dict:
    return {
        "rules": [_record_data(item) for item in rules],
        "virtual_prototypes": [_record_data(item) for item in prototypes],
        "remark": remark,
    }


def _virtual_prototypes_json(prototypes: tuple[VirtualPrototypeInput, ...]) -> str:
    return dumps_exact_json([_record_data(item) for item in prototypes])


def _editor_snapshot_json(
    rules: tuple[EditableRuleInput, ...],
    prototypes: tuple[VirtualPrototypeInput, ...],
    remark: str,
) -> str:
    return dumps_exact_json(_editor_snapshot_data(rules, prototypes, remark))


def _request_hash(
    expected_active_version_id: int | None,
    rules: tuple[EditableRuleInput, ...],
    prototypes: tuple[VirtualPrototypeInput, ...],
    remark: str,
) -> str:
    template = GQGA4_RULE_SET_TEMPLATE
    return fingerprint(
        {
            "product_line_code": template.product_line_code,
            "process_code": template.process_code,
            "scenario": template.scenario,
            "expected_active_version_id": expected_active_version_id,
            **_editor_snapshot_data(rules, prototypes, remark),
        }
    )


def _reject_json_constant(value: str):
    raise ValueError(f"invalid JSON number: {value}")


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_virtual_prototypes(payload: str) -> tuple[VirtualPrototypeInput, ...]:
    try:
        raw = json.loads(
            payload,
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
        if not isinstance(raw, list):
            raise ValueError("virtual prototype snapshot must be an array")
        expected_fields = {part.name for part in fields(VirtualPrototypeInput)}
        prototypes = []
        for item in raw:
            if not isinstance(item, dict) or set(item) != expected_fields:
                raise ValueError("virtual prototype snapshot has an invalid field set")
            prototypes.append(VirtualPrototypeInput(**item))
        result = tuple(prototypes)
        if len({item.prototype_id for item in result}) != len(result):
            raise ValueError("virtual prototype snapshot contains duplicate identities")
        if _virtual_prototypes_json(result) != payload:
            raise ValueError("virtual prototype snapshot is not canonical JSON")
        return result
    except (DecimalException, RecursionError, TypeError, ValueError) as error:
        raise RuleManagementServiceError(
            "stored_snapshot_inconsistent", "活动规则版本中的虚拟材料原型快照不一致。"
        ) from error


def _stored_inconsistent(message: str) -> RuleManagementServiceError:
    return RuleManagementServiceError("stored_snapshot_inconsistent", message)


def _find_rule_set(store: RuleStore) -> RuleSetRecord:
    template = GQGA4_RULE_SET_TEMPLATE
    rule_set = store.find_rule_set(
        template.product_line_code,
        template.process_code,
        template.scenario,
    )
    if rule_set is None:
        raise RuleManagementServiceError("rule_set_not_initialized", "GQGA4 月计划规则尚未初始化。")
    return rule_set


def _create_rule_definitions(store: RuleStore, version_id: int, rules) -> None:
    for sequence_no, rule in enumerate(rules, start=1):
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


def _create_grade_dictionary_entries(
    store: RuleStore,
    version_id: int,
    snapshot: GradeDictionarySnapshot,
) -> None:
    for entry in snapshot.entries:
        store.create_grade_dictionary_entry(
            version_id,
            entry.source_grade,
            entry.normalized_grade,
            entry.soft_hard_class,
            entry.roll_type,
            entry.steel_classes,
            entry.is_if_steel,
            entry.enabled,
            entry.source_file,
            entry.source_row_count,
            entry.source_rows,
            entry.remark,
        )


def _read_grade_dictionary_snapshot(
    store: RuleStore,
    rule_set: RuleSetRecord,
    version_id: int,
) -> GradeDictionarySnapshot:
    version = store.find_version(version_id)
    if version is None or version.rule_set_id != rule_set.id:
        raise _stored_inconsistent("规则版本与软硬钢字典所属规则集不一致。")
    if version.grade_dictionary_fingerprint is None:
        raise _stored_inconsistent("规则版本缺少软硬钢字典指纹。")

    try:
        snapshot = GradeDictionarySnapshot(
            rule_set.product_line_code,
            tuple(
                GradeDictionaryEntry(
                    source_grade=record.source_grade,
                    normalized_grade=record.normalized_grade,
                    soft_hard_class=record.soft_hard_class,
                    roll_type=record.roll_type,
                    steel_classes=record.steel_classes,
                    is_if_steel=record.is_if_steel,
                    enabled=record.enabled,
                    source_file=record.source_file,
                    source_row_count=record.source_row_count,
                    source_rows=record.source_rows,
                    remark=record.remark,
                )
                for record in store.list_grade_dictionary_entries(version.id)
            ),
        )
    except (RecursionError, TypeError, ValueError) as error:
        raise _stored_inconsistent("规则版本中的软硬钢字典快照不一致。") from error
    if snapshot.dictionary_fingerprint != version.grade_dictionary_fingerprint:
        raise _stored_inconsistent("规则版本中的软硬钢字典指纹不一致。")
    return snapshot


def _read_rules_version(
    store: RuleStore,
    rule_set: RuleSetRecord,
    version_id: int,
) -> ActiveRulesResponse:
    version = store.find_version(version_id)
    if version is None or version.rule_set_id != rule_set.id:
        raise _stored_inconsistent("活动规则版本与规则集不一致。")
    if version.remark != version.remark.strip():
        raise _stored_inconsistent("活动规则版本的备注未按页面契约规范化。")

    try:
        spec = load_compiled_rule_set_json(version.compiled_rule_set_json)
    except (RecursionError, TypeError, ValueError) as error:
        raise _stored_inconsistent("活动规则版本的完整编译快照无法通过权威加载。") from error

    template = GQGA4_RULE_SET_TEMPLATE
    identity = (spec.product_line_code, spec.process_code, spec.scenario)
    expected_identity = (
        template.product_line_code,
        template.process_code,
        template.scenario,
    )
    if (
        identity != expected_identity
        or spec.version != str(version.version_no)
        or spec.fingerprint != version.rule_set_fingerprint
    ):
        raise _stored_inconsistent("活动规则版本的身份、版本号或指纹不一致。")

    prototypes = _load_virtual_prototypes(version.virtual_prototypes_json)
    editable_rules = tuple(
        EditableRuleInput(item.rule_id, item.enabled, item.parameters) for item in spec.rules
    )
    try:
        normalized_rules, normalized_prototypes = normalize_gqga4_rule_snapshot(
            editable_rules, prototypes
        )
        recompiled = compile_gqga4_rule_set(normalized_rules, version.version_no)
    except (RecursionError, TypeError, ValueError) as error:
        raise _stored_inconsistent("活动规则版本不符合 GQGA4 固定规则模板。") from error
    if (
        normalized_rules != editable_rules
        or normalized_prototypes != prototypes
        or recompiled.rule_set_spec != spec
        or recompiled.compiled_rule_set_json != version.compiled_rule_set_json
    ):
        raise _stored_inconsistent("活动规则版本与 GQGA4 固定规则模板不一致。")

    definitions = store.list_rule_definitions(version.id)
    if len(definitions) != len(spec.rules) or tuple(
        item.sequence_no for item in definitions
    ) != tuple(range(1, len(spec.rules) + 1)):
        raise _stored_inconsistent("活动规则版本的逐条规则数量或顺序不连续。")
    for record, rule in zip(definitions, spec.rules):
        expected = (
            rule.rule_id,
            rule.rule_type,
            rule.name,
            rule.scope.value,
            rule.enabled,
            rule.version,
            dumps_exact_json(rule.parameters),
        )
        actual = (
            record.rule_id,
            record.rule_type,
            record.rule_name,
            record.scope,
            record.enabled,
            record.rule_version,
            record.parameters_json,
        )
        if actual != expected:
            raise _stored_inconsistent("活动规则版本的逐条规则与完整编译快照不一致。")

    if version.editor_snapshot_json != _editor_snapshot_json(
        normalized_rules, normalized_prototypes, version.remark
    ):
        raise _stored_inconsistent("活动规则版本的页面快照与编译快照不一致。")
    if version.request_hash != _request_hash(
        version.based_on_version_id,
        normalized_rules,
        normalized_prototypes,
        version.remark,
    ):
        raise _stored_inconsistent("活动规则版本的请求摘要与规范化页面快照不一致。")

    return ActiveRulesResponse(
        active_version_id=version.id,
        based_on_version_id=version.based_on_version_id,
        rule_set_spec=spec,
        virtual_prototypes=prototypes,
        remark=version.remark,
        activated_at=version.activated_at,
        activated_by=version.activated_by,
    )


def _read_active_rules(store: RuleStore, rule_set: RuleSetRecord) -> ActiveRulesResponse:
    if rule_set.active_version_id is None:
        raise RuleManagementServiceError(
            "rule_set_not_initialized", "GQGA4 月计划规则尚无启用版本。"
        )
    response = _read_rules_version(store, rule_set, rule_set.active_version_id)
    _read_grade_dictionary_snapshot(store, rule_set, rule_set.active_version_id)
    return response


def get_active_gqga4_rules(
    database_path: str | Path, *, timeout_seconds: float = 5.0
) -> ActiveRulesResponse:
    """Read one fully verified active GQGA4 monthly rule snapshot."""

    with RuleStore.open(database_path, timeout_seconds=timeout_seconds) as store:
        with store.transaction():
            response = _read_active_rules(store, _find_rule_set(store))
            dumps_active_rules_response(response)
            return response


def initialize_gqga4_rules(
    database_path: str | Path,
    *,
    initial_grade_dictionary: GradeDictionarySnapshot | None = None,
    audit_actor: str = DEFAULT_AUDIT_ACTOR,
    clock: Callable[[], datetime] = _utc_now,
    timeout_seconds: float = 5.0,
) -> RuleInitializationResult:
    """Create and verify the initial GQGA4 rule version, or verify the existing one."""

    if database_path is None or not str(database_path).strip():
        raise ValueError("database_path must not be blank")
    if initial_grade_dictionary is not None and not isinstance(
        initial_grade_dictionary, GradeDictionarySnapshot
    ):
        raise ValueError("initial_grade_dictionary must be GradeDictionarySnapshot")
    if initial_grade_dictionary is None and (
        str(database_path) == ":memory:" or not Path(database_path).is_file()
    ):
        raise ValueError("initial_grade_dictionary is required for a new rule database")

    open_store = RuleStore.open if initial_grade_dictionary is None else RuleStore.initialize
    with open_store(database_path, timeout_seconds=timeout_seconds) as store:
        with store.transaction(write=initial_grade_dictionary is not None):
            template = GQGA4_RULE_SET_TEMPLATE
            rule_set = store.find_rule_set(
                template.product_line_code,
                template.process_code,
                template.scenario,
            )
            if rule_set is not None:
                active = _read_active_rules(store, rule_set)
                stored_dictionary = _read_grade_dictionary_snapshot(
                    store, rule_set, active.active_version_id
                )
                if initial_grade_dictionary is not None:
                    if (
                        initial_grade_dictionary.product_line_code
                        != stored_dictionary.product_line_code
                        or initial_grade_dictionary.dictionary_fingerprint
                        != stored_dictionary.dictionary_fingerprint
                    ):
                        raise RuleManagementServiceError(
                            "initial_grade_dictionary_conflict",
                            "初始化软硬钢字典与现有活动版本不一致。",
                            current_active_version_id=active.active_version_id,
                        )
                dumps_active_rules_response(active)
                return RuleInitializationResult(
                    created=False,
                    active_rules=active,
                    grade_dictionary_fingerprint=stored_dictionary.dictionary_fingerprint,
                    grade_dictionary_entry_count=len(stored_dictionary.entries),
                )

            actor = _audit_actor(audit_actor)
            if not isinstance(initial_grade_dictionary, GradeDictionarySnapshot):
                raise ValueError("initial_grade_dictionary is required for a new rule database")
            if initial_grade_dictionary.product_line_code != template.product_line_code:
                raise ValueError("initial_grade_dictionary must belong to GQGA4")
            rules, prototypes = normalize_gqga4_rule_snapshot(
                GQGA4_INITIAL_RULES, GQGA4_INITIAL_VIRTUAL_PROTOTYPES
            )
            compiled = compile_gqga4_rule_set(rules, 1)
            timestamp = _timestamp(clock)
            rule_set_id = store.create_rule_set(
                template.product_line_code,
                template.process_code,
                template.scenario,
                created_at=timestamp,
            )
            version_id = store.create_version(
                rule_set_id,
                1,
                None,
                INITIALIZATION_OPERATION_ID,
                _request_hash(None, rules, prototypes, ""),
                compiled.compiled_rule_set_json,
                compiled.rule_set_spec.fingerprint,
                _virtual_prototypes_json(prototypes),
                _editor_snapshot_json(rules, prototypes, ""),
                "",
                actor,
                timestamp,
                actor,
                timestamp,
                grade_dictionary_fingerprint=(
                    initial_grade_dictionary.dictionary_fingerprint
                ),
            )
            _create_rule_definitions(store, version_id, compiled.rule_set_spec.rules)
            _create_grade_dictionary_entries(store, version_id, initial_grade_dictionary)
            store.activate_version(rule_set_id, version_id, None, updated_at=timestamp)
            active_rule_set = _find_rule_set(store)
            active = _read_active_rules(store, active_rule_set)
            stored_dictionary = _read_grade_dictionary_snapshot(
                store, active_rule_set, active.active_version_id
            )
            dumps_active_rules_response(active)
            return RuleInitializationResult(
                created=True,
                active_rules=active,
                grade_dictionary_fingerprint=stored_dictionary.dictionary_fingerprint,
                grade_dictionary_entry_count=len(stored_dictionary.entries),
            )


def set_active_gqga4_rules(
    request: SetActiveRulesRequest,
    database_path: str | Path,
    *,
    audit_actor: str = DEFAULT_AUDIT_ACTOR,
    clock: Callable[[], datetime] = _utc_now,
    timeout_seconds: float = 5.0,
) -> SetActiveRulesResponse:
    """Persist and activate one complete rule version, or replay its prior result."""

    if not isinstance(request, SetActiveRulesRequest):
        raise ValueError("request must be SetActiveRulesRequest")
    actor = _audit_actor(audit_actor)
    normalized_rules, normalized_prototypes = normalize_gqga4_rule_snapshot(
        request.rules, request.virtual_prototypes
    )
    request_hash = _request_hash(
        request.expected_active_version_id,
        normalized_rules,
        normalized_prototypes,
        request.remark,
    )

    with RuleStore.open(database_path, timeout_seconds=timeout_seconds) as store:
        try:
            with store.transaction(write=True):
                rule_set = _find_rule_set(store)
                previous = rule_set.active_version_id
                existing = store.find_version_by_operation(rule_set.id, request.save_operation_id)
                if existing is not None:
                    if (
                        existing.request_hash != request_hash
                        or existing.created_by != actor
                        or existing.activated_by != actor
                    ):
                        raise RuleManagementServiceError(
                            "operation_payload_conflict",
                            "同一保存操作标识已对应另一项请求或执行主体。",
                            expected_active_version_id=request.expected_active_version_id,
                            current_active_version_id=previous,
                        )
                    if existing.based_on_version_id is None:
                        raise _stored_inconsistent("保存操作记录缺少原活动版本。")
                    if existing.id != rule_set.active_version_id:
                        _read_grade_dictionary_snapshot(store, rule_set, existing.id)
                    active = _read_active_rules(store, rule_set)
                    response = SetActiveRulesResponse(
                        active_rules=active,
                        save_operation_id=request.save_operation_id,
                        previous_active_version_id=existing.based_on_version_id,
                        saved_version_id=existing.id,
                        saved_version_is_active=existing.id == active.active_version_id,
                        idempotent_replay=True,
                    )
                    dumps_set_active_rules_response(response)
                    return response

                if previous is None:
                    raise RuleManagementServiceError(
                        "rule_set_not_initialized", "GQGA4 月计划规则尚无启用版本。"
                    )
                if request.expected_active_version_id != previous:
                    raise RuleManagementServiceError(
                        "active_version_conflict",
                        "活动规则版本已变化，请重新加载后再保存。",
                        expected_active_version_id=request.expected_active_version_id,
                        current_active_version_id=previous,
                    )

                source_dictionary = _read_grade_dictionary_snapshot(
                    store, rule_set, previous
                )
                version_no = store.next_version_no(rule_set.id)
                compiled = compile_gqga4_rule_set(normalized_rules, version_no)
                timestamp = _timestamp(clock)
                version_id = store.create_version(
                    rule_set.id,
                    version_no,
                    previous,
                    request.save_operation_id,
                    request_hash,
                    compiled.compiled_rule_set_json,
                    compiled.rule_set_spec.fingerprint,
                    _virtual_prototypes_json(normalized_prototypes),
                    _editor_snapshot_json(normalized_rules, normalized_prototypes, request.remark),
                    request.remark,
                    actor,
                    timestamp,
                    actor,
                    timestamp,
                    grade_dictionary_fingerprint=(
                        source_dictionary.dictionary_fingerprint
                    ),
                )
                _create_rule_definitions(store, version_id, compiled.rule_set_spec.rules)
                copied_count = store.copy_grade_dictionary_entries(previous, version_id)
                if copied_count != len(source_dictionary.entries):
                    raise _stored_inconsistent(
                        "保存的新版本未完整继承活动软硬钢字典。"
                    )
                store.activate_version(rule_set.id, version_id, previous, updated_at=timestamp)
                active = _read_active_rules(store, _find_rule_set(store))
                response = SetActiveRulesResponse(
                    active_rules=active,
                    save_operation_id=request.save_operation_id,
                    previous_active_version_id=previous,
                    saved_version_id=version_id,
                    saved_version_is_active=True,
                    idempotent_replay=False,
                )
                dumps_set_active_rules_response(response)
                return response
        except RuleStoreConflict as error:
            with store.transaction():
                current = _find_rule_set(store).active_version_id
            raise RuleManagementServiceError(
                "active_version_conflict",
                "活动规则版本已变化，请重新加载后再保存。",
                expected_active_version_id=request.expected_active_version_id,
                current_active_version_id=current,
            ) from error


__all__ = [
    "DEFAULT_AUDIT_ACTOR",
    "INITIALIZATION_OPERATION_ID",
    "MIGRATION_AUDIT_ACTOR",
    "RuleInitializationResult",
    "RuleManagementServiceError",
    "get_active_gqga4_rules",
    "initialize_gqga4_rules",
    "set_active_gqga4_rules",
]
