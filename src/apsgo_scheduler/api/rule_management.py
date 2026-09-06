"""Framework-independent contracts for reading and replacing active rule sets."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from uuid import UUID

from ..core.contracts import (
    DiagnosticIssue,
    DiagnosticPhase,
    DiagnosticSeverity,
    RuleParameterValue,
    freeze_rule_parameters,
    freeze_tuple,
    require_int,
    require_text,
)
from .json_codec import dumps_exact_json
from .request import RuleSetSpec, VirtualPrototypeInput

_MAX_RULE_PARAMETER_DEPTH = 64


def _operation_id(value: str) -> str:
    require_text(value, "save_operation_id")
    try:
        return str(UUID(value.strip()))
    except (AttributeError, ValueError) as error:
        raise ValueError("save_operation_id must be UUID text") from error


def _optional_positive_int(value: int | None, name: str) -> None:
    if value is not None:
        require_int(value, name, minimum=1)


def _unique(items: tuple, field_name: str, name: str) -> None:
    seen = set()
    for item in items:
        value = getattr(item, field_name)
        if value in seen:
            raise ValueError(f"{name} contains duplicate {field_name}: {value}")
        seen.add(value)


@dataclass(frozen=True, slots=True)
class EditableRuleInput:
    rule_id: str
    enabled: bool
    parameters: Mapping[str, RuleParameterValue]

    def __post_init__(self):
        require_text(self.rule_id, "rule_id")
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be bool")
        object.__setattr__(self, "parameters", freeze_rule_parameters(self.parameters))


@dataclass(frozen=True, slots=True)
class SetActiveRulesRequest:
    save_operation_id: str
    expected_active_version_id: int
    rules: tuple[EditableRuleInput, ...]
    virtual_prototypes: tuple[VirtualPrototypeInput, ...]
    remark: str = ""

    def __post_init__(self):
        object.__setattr__(self, "save_operation_id", _operation_id(self.save_operation_id))
        require_int(self.expected_active_version_id, "expected_active_version_id", minimum=1)
        object.__setattr__(self, "rules", freeze_tuple(self.rules, EditableRuleInput, "rules"))
        object.__setattr__(
            self,
            "virtual_prototypes",
            freeze_tuple(self.virtual_prototypes, VirtualPrototypeInput, "virtual_prototypes"),
        )
        if not isinstance(self.remark, str):
            raise ValueError("remark must be text")
        object.__setattr__(self, "remark", self.remark.strip())
        _unique(self.rules, "rule_id", "rules")
        _unique(self.virtual_prototypes, "prototype_id", "virtual_prototypes")


@dataclass(frozen=True, slots=True)
class ActiveRulesResponse:
    active_version_id: int
    based_on_version_id: int | None
    rule_set_spec: RuleSetSpec
    virtual_prototypes: tuple[VirtualPrototypeInput, ...]
    remark: str
    activated_at: str
    activated_by: str

    def __post_init__(self):
        require_int(self.active_version_id, "active_version_id", minimum=1)
        _optional_positive_int(self.based_on_version_id, "based_on_version_id")
        if not isinstance(self.rule_set_spec, RuleSetSpec):
            raise ValueError("rule_set_spec must be RuleSetSpec")
        object.__setattr__(
            self,
            "virtual_prototypes",
            freeze_tuple(self.virtual_prototypes, VirtualPrototypeInput, "virtual_prototypes"),
        )
        if not isinstance(self.remark, str):
            raise ValueError("remark must be text")
        require_text(self.activated_at, "activated_at")
        require_text(self.activated_by, "activated_by")
        _unique(self.virtual_prototypes, "prototype_id", "virtual_prototypes")
        _rule_set_version_no(self.rule_set_spec)


@dataclass(frozen=True, slots=True)
class SetActiveRulesResponse:
    active_rules: ActiveRulesResponse
    save_operation_id: str
    previous_active_version_id: int
    saved_version_id: int
    saved_version_is_active: bool
    idempotent_replay: bool

    def __post_init__(self):
        if not isinstance(self.active_rules, ActiveRulesResponse):
            raise ValueError("active_rules must be ActiveRulesResponse")
        object.__setattr__(self, "save_operation_id", _operation_id(self.save_operation_id))
        require_int(self.previous_active_version_id, "previous_active_version_id", minimum=1)
        require_int(self.saved_version_id, "saved_version_id", minimum=1)
        for name in ("saved_version_is_active", "idempotent_replay"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be bool")
        if self.saved_version_is_active != (
            self.saved_version_id == self.active_rules.active_version_id
        ):
            raise ValueError("saved_version_is_active does not match the active version")
        if not self.idempotent_replay and not self.saved_version_is_active:
            raise ValueError("a new save must be the active version")
        if (
            self.saved_version_is_active
            and self.previous_active_version_id != self.active_rules.based_on_version_id
        ):
            raise ValueError("the saved active version must be based on the previous version")


@dataclass(frozen=True, slots=True)
class RuleManagementError:
    code: str
    message: str
    expected_active_version_id: int | None
    current_active_version_id: int | None
    issues: tuple[DiagnosticIssue, ...]

    def __post_init__(self):
        require_text(self.code, "code")
        require_text(self.message, "message")
        _optional_positive_int(self.expected_active_version_id, "expected_active_version_id")
        _optional_positive_int(self.current_active_version_id, "current_active_version_id")
        object.__setattr__(self, "issues", freeze_tuple(self.issues, DiagnosticIssue, "issues"))


class RuleManagementContractError(ValueError):
    """A malformed JSON request with stable, field-locatable diagnostics."""

    def __init__(self, issues: tuple[DiagnosticIssue, ...]):
        self.issues = freeze_tuple(issues, DiagnosticIssue, "issues")
        if not self.issues:
            raise ValueError("contract error requires at least one issue")
        super().__init__(self.issues[0].message)


class _DuplicateJsonKey(ValueError):
    pass


def _raise_contract(code: str, field_path: str, message: str, subject_id: str | None = None):
    raise RuleManagementContractError(
        (
            DiagnosticIssue(
                code,
                DiagnosticPhase.REQUEST_VALIDATION,
                field_path,
                subject_id,
                message,
                DiagnosticSeverity.ERROR,
            ),
        )
    )


def _reject_constant(value: str):
    raise ValueError(f"invalid JSON number: {value}")


def _object_from_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _exact_object(value, field_path: str, required: set[str], optional: set[str] = frozenset()):
    if not isinstance(value, Mapping):
        _raise_contract("invalid_field_type", field_path or "body", "字段必须是 JSON 对象。")
    missing = sorted(required - value.keys())
    if missing:
        path = f"{field_path}.{missing[0]}" if field_path else missing[0]
        _raise_contract("missing_field", path, "缺少必需字段。")
    unknown = sorted(value.keys() - required - optional)
    if unknown:
        path = f"{field_path}.{unknown[0]}" if field_path else unknown[0]
        _raise_contract("unknown_field", path, "请求包含未定义字段。")
    return value


def _array(value, field_path: str) -> list:
    if not isinstance(value, list):
        _raise_contract("invalid_field_type", field_path, "字段必须是 JSON 数组。")
    return value


def _text(value, field_path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        _raise_contract("invalid_field_type", field_path, "字段必须是非空文本。")
    return value


def _bool(value, field_path: str) -> bool:
    if type(value) is not bool:
        _raise_contract("invalid_field_type", field_path, "字段必须是 JSON 布尔值。")
    return value


def _positive_int(value, field_path: str) -> int:
    if type(value) is not int or value < 1:
        _raise_contract("invalid_field_type", field_path, "字段必须是正整数。")
    return value


def _validate_parameter_depth(value, field_path: str) -> None:
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > _MAX_RULE_PARAMETER_DEPTH:
            _raise_contract(
                "maximum_nesting_exceeded",
                field_path,
                f"规则参数嵌套不能超过 {_MAX_RULE_PARAMETER_DEPTH} 层。",
            )
        if isinstance(item, Mapping):
            pending.extend((part, depth + 1) for part in item.values())
        elif isinstance(item, list):
            pending.extend((part, depth + 1) for part in item)


def _decimal(value, field_path: str, *, allow_none: bool) -> Decimal | None:
    if value is None and allow_none:
        return None
    if type(value) is int:
        value = Decimal(value)
    if not isinstance(value, Decimal) or not value.is_finite():
        _raise_contract("invalid_field_type", field_path, "字段必须是有限十进制数。")
    return value


def _editable_rule(value, index: int) -> EditableRuleInput:
    prefix = f"rules[{index}]"
    item = _exact_object(value, prefix, {"rule_id", "enabled", "parameters"})
    rule_id = _text(item["rule_id"], f"{prefix}.rule_id")
    parameters = item["parameters"]
    if not isinstance(parameters, Mapping):
        _raise_contract(
            "invalid_field_type", f"{prefix}.parameters", "字段必须是 JSON 对象。", rule_id
        )
    _validate_parameter_depth(parameters, f"{prefix}.parameters")
    try:
        return EditableRuleInput(rule_id, _bool(item["enabled"], f"{prefix}.enabled"), parameters)
    except RuleManagementContractError:
        raise
    except (RecursionError, ValueError) as error:
        _raise_contract("invalid_field_value", f"{prefix}.parameters", str(error), rule_id)


def _virtual_prototype(value, index: int) -> VirtualPrototypeInput:
    prefix = f"virtual_prototypes[{index}]"
    names = {
        "prototype_id",
        "unit_weight",
        "width",
        "thickness",
        "min_temperature",
        "max_temperature",
        "grade",
        "rule_attributes",
    }
    item = _exact_object(value, prefix, names)
    prototype_id = _text(item["prototype_id"], f"{prefix}.prototype_id")
    attributes = item["rule_attributes"]
    if not isinstance(attributes, Mapping):
        _raise_contract(
            "invalid_field_type",
            f"{prefix}.rule_attributes",
            "字段必须是 JSON 对象。",
            prototype_id,
        )
    try:
        return VirtualPrototypeInput(
            prototype_id=prototype_id,
            unit_weight=_decimal(item["unit_weight"], f"{prefix}.unit_weight", allow_none=False),
            width=_decimal(item["width"], f"{prefix}.width", allow_none=True),
            thickness=_decimal(item["thickness"], f"{prefix}.thickness", allow_none=True),
            min_temperature=_decimal(
                item["min_temperature"], f"{prefix}.min_temperature", allow_none=True
            ),
            max_temperature=_decimal(
                item["max_temperature"], f"{prefix}.max_temperature", allow_none=True
            ),
            grade=_text(item["grade"], f"{prefix}.grade", allow_empty=True),
            rule_attributes=attributes,
        )
    except RuleManagementContractError:
        raise
    except ValueError as error:
        _raise_contract(
            "invalid_field_value", f"{prefix}.rule_attributes", str(error), prototype_id
        )


def loads_set_active_rules_request(payload: str | bytes | bytearray) -> SetActiveRulesRequest:
    """Parse one POST body without routing numeric values through binary floats."""

    if not isinstance(payload, (str, bytes, bytearray)):
        _raise_contract("invalid_json", "body", "请求体必须是 JSON 文本或字节。")
    try:
        value = json.loads(
            payload,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_from_pairs,
        )
    except _DuplicateJsonKey as error:
        _raise_contract("duplicate_json_key", "body", f"JSON 对象包含重复键：{error}。")
    except (
        DecimalException,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as error:
        _raise_contract("invalid_json", "body", f"请求体不是合法 JSON：{error}。")

    value = _exact_object(
        value,
        "",
        {"save_operation_id", "expected_active_version_id", "rules", "virtual_prototypes"},
        {"remark"},
    )
    save_operation_id = _text(value["save_operation_id"], "save_operation_id")
    try:
        save_operation_id = _operation_id(save_operation_id)
    except ValueError as error:
        _raise_contract("invalid_field_value", "save_operation_id", str(error))
    rules = tuple(
        _editable_rule(item, index) for index, item in enumerate(_array(value["rules"], "rules"))
    )
    prototypes = tuple(
        _virtual_prototype(item, index)
        for index, item in enumerate(_array(value["virtual_prototypes"], "virtual_prototypes"))
    )
    seen_rules = set()
    for index, rule in enumerate(rules):
        if rule.rule_id in seen_rules:
            _raise_contract(
                "duplicate_identity",
                f"rules[{index}].rule_id",
                "规则标识与前面的记录重复。",
                rule.rule_id,
            )
        seen_rules.add(rule.rule_id)
    seen_prototypes = set()
    for index, prototype in enumerate(prototypes):
        if prototype.prototype_id in seen_prototypes:
            _raise_contract(
                "duplicate_identity",
                f"virtual_prototypes[{index}].prototype_id",
                "虚拟材料原型标识与前面的记录重复。",
                prototype.prototype_id,
            )
        seen_prototypes.add(prototype.prototype_id)
    remark = value.get("remark", "")
    if not isinstance(remark, str):
        _raise_contract("invalid_field_type", "remark", "字段必须是文本。")
    return SetActiveRulesRequest(
        save_operation_id=save_operation_id,
        expected_active_version_id=_positive_int(
            value["expected_active_version_id"], "expected_active_version_id"
        ),
        rules=rules,
        virtual_prototypes=prototypes,
        remark=remark,
    )


def _prototype_data(value: VirtualPrototypeInput) -> dict:
    return {
        "prototype_id": value.prototype_id,
        "unit_weight": value.unit_weight,
        "width": value.width,
        "thickness": value.thickness,
        "min_temperature": value.min_temperature,
        "max_temperature": value.max_temperature,
        "grade": value.grade,
        "rule_attributes": value.rule_attributes,
    }


def _editable_rule_data(value: EditableRuleInput) -> dict:
    return {"rule_id": value.rule_id, "enabled": value.enabled, "parameters": value.parameters}


def _rule_set_version_no(value: RuleSetSpec) -> int:
    try:
        version_no = int(value.version)
    except (TypeError, ValueError) as error:
        raise ValueError("rule_set_spec.version must be a positive canonical integer") from error
    if version_no < 1 or str(version_no) != value.version:
        raise ValueError("rule_set_spec.version must be a positive canonical integer")
    return version_no


def _active_rules_data(value: ActiveRulesResponse) -> dict:
    spec = value.rule_set_spec
    return {
        "product_line_code": spec.product_line_code,
        "process_code": spec.process_code,
        "scenario": spec.scenario,
        "active_version_id": value.active_version_id,
        "version_no": _rule_set_version_no(spec),
        "based_on_version_id": value.based_on_version_id,
        "fingerprint": spec.fingerprint,
        "rules": [
            {
                "sequence_no": index,
                "rule_id": rule.rule_id,
                "rule_type": rule.rule_type,
                "name": rule.name,
                "scope": rule.scope.value,
                "enabled": rule.enabled,
                "version": rule.version,
                "parameters": rule.parameters,
            }
            for index, rule in enumerate(spec.rules, start=1)
        ],
        "quality_spec": [
            {
                "criterion_id": criterion.criterion_id,
                "metric_key": criterion.metric_key,
                "direction": criterion.direction,
                "aggregation": criterion.aggregation,
                "numeric_projection": criterion.numeric_projection,
            }
            for criterion in spec.quality_spec
        ],
        "allowed_final_deviation_codes": sorted(spec.allowed_final_deviation_codes),
        "virtual_prototypes": [_prototype_data(item) for item in value.virtual_prototypes],
        "remark": value.remark,
        "activated_at": value.activated_at,
        "activated_by": value.activated_by,
    }


def dumps_set_active_rules_request(value: SetActiveRulesRequest) -> str:
    if not isinstance(value, SetActiveRulesRequest):
        raise ValueError("value must be SetActiveRulesRequest")
    return dumps_exact_json(
        {
            "save_operation_id": value.save_operation_id,
            "expected_active_version_id": value.expected_active_version_id,
            "rules": [_editable_rule_data(item) for item in value.rules],
            "virtual_prototypes": [_prototype_data(item) for item in value.virtual_prototypes],
            "remark": value.remark,
        }
    )


def dumps_active_rules_response(value: ActiveRulesResponse) -> str:
    if not isinstance(value, ActiveRulesResponse):
        raise ValueError("value must be ActiveRulesResponse")
    return dumps_exact_json(_active_rules_data(value))


def dumps_set_active_rules_response(value: SetActiveRulesResponse) -> str:
    if not isinstance(value, SetActiveRulesResponse):
        raise ValueError("value must be SetActiveRulesResponse")
    data = _active_rules_data(value.active_rules)
    data.update(
        {
            "save_operation_id": value.save_operation_id,
            "previous_active_version_id": value.previous_active_version_id,
            "saved_version_id": value.saved_version_id,
            "saved_version_is_active": value.saved_version_is_active,
            "idempotent_replay": value.idempotent_replay,
        }
    )
    return dumps_exact_json(data)


def dumps_rule_management_error(value: RuleManagementError) -> str:
    if not isinstance(value, RuleManagementError):
        raise ValueError("value must be RuleManagementError")
    return dumps_exact_json(
        {
            "error": {
                "code": value.code,
                "message": value.message,
                "expected_active_version_id": value.expected_active_version_id,
                "current_active_version_id": value.current_active_version_id,
                "issues": [
                    {
                        "code": issue.code,
                        "phase": issue.phase.value,
                        "field_path": issue.field_path,
                        "subject_id": issue.subject_id,
                        "message": issue.message,
                        "severity": issue.severity.value,
                    }
                    for issue in value.issues
                ],
            }
        }
    )


__all__ = [
    "ActiveRulesResponse",
    "EditableRuleInput",
    "RuleManagementContractError",
    "RuleManagementError",
    "SetActiveRulesRequest",
    "SetActiveRulesResponse",
    "dumps_active_rules_response",
    "dumps_rule_management_error",
    "dumps_set_active_rules_request",
    "dumps_set_active_rules_response",
    "loads_set_active_rules_request",
]
