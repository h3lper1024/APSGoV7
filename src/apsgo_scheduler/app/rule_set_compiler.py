"""Compile editable rule values against immutable server-owned metadata."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from decimal import Decimal, DecimalException
from math import isfinite
from sys import maxsize

from ..api.json_codec import dumps_exact_json
from ..api.request import (
    QualityCriterionSpec,
    RuleDefinitionSpec,
    RuleSetSpec,
    VirtualPrototypeInput,
)
from ..api.rule_management import EditableRuleInput
from ..core.contracts import (
    DiagnosticIssue,
    DiagnosticPhase,
    DiagnosticSeverity,
    RuleScope,
)
from ..core.model import Node
from .input_normalizer import TEXT_ATTRIBUTES, UPPERCASE_ATTRIBUTES
from .rule_set_loader import fingerprint_rule_set_spec, load_rule_set


class RuleSetCompilationError(ValueError):
    """Field-locatable editor data errors; no partial snapshot is returned."""

    def __init__(self, issues: tuple[DiagnosticIssue, ...]):
        self.issues = tuple(issues)
        if not self.issues:
            raise ValueError("compilation error requires at least one issue")
        super().__init__("；".join(issue.message for issue in self.issues))


@dataclass(frozen=True, slots=True)
class CompiledRuleSetSnapshot:
    rule_set_spec: RuleSetSpec
    compiled_rule_set_json: str

    def __post_init__(self):
        if not isinstance(self.rule_set_spec, RuleSetSpec):
            raise ValueError("rule_set_spec must be RuleSetSpec")
        if not isinstance(self.compiled_rule_set_json, str) or not self.compiled_rule_set_json:
            raise ValueError("compiled_rule_set_json must be nonempty text")


class _ParameterValueError(ValueError):
    def __init__(self, path: str, message: str):
        self.path = path
        super().__init__(message)


class _DuplicateJsonKey(ValueError):
    pass


def _issue(code: str, path: str, message: str, subject_id: str | None = None):
    return DiagnosticIssue(
        code,
        DiagnosticPhase.REQUEST_VALIDATION,
        path,
        subject_id,
        message,
        DiagnosticSeverity.ERROR,
    )


def _normalize_parameter(value, samples: tuple, path: str):
    nullable = any(sample is None for sample in samples)
    concrete = tuple(sample for sample in samples if sample is not None)
    if value is None:
        if nullable:
            return None
        raise _ParameterValueError(path, "参数不能为空。")
    if not concrete:
        raise _ParameterValueError(path, "参数必须为空值。")

    if all(isinstance(sample, Mapping) for sample in concrete):
        if not isinstance(value, Mapping):
            raise _ParameterValueError(path, "参数必须是对象。")
        key_sets = {frozenset(sample) for sample in concrete}
        if len(key_sets) != 1:
            raise ValueError(f"template mapping shape is inconsistent at {path}")
        expected = next(iter(key_sets))
        missing = sorted(expected - value.keys())
        unknown = sorted(value.keys() - expected)
        if missing:
            raise _ParameterValueError(f"{path}.{missing[0]}", "缺少规则参数。")
        if unknown:
            raise _ParameterValueError(f"{path}.{unknown[0]}", "规则包含未定义参数。")
        return {
            key: _normalize_parameter(
                value[key], tuple(sample[key] for sample in concrete), f"{path}.{key}"
            )
            for key in expected
        }

    if all(isinstance(sample, tuple) for sample in concrete):
        if not isinstance(value, (tuple, list)):
            raise _ParameterValueError(path, "参数必须是数组。")
        item_samples = tuple(item for sample in concrete for item in sample)
        if not item_samples and value:
            raise _ParameterValueError(path, "该参数数组必须为空。")
        return tuple(
            _normalize_parameter(item, item_samples, f"{path}[{index}]")
            for index, item in enumerate(value)
        )

    kinds = {type(sample) for sample in concrete}
    if kinds == {Decimal}:
        if type(value) is int:
            value = Decimal(value)
        if not isinstance(value, Decimal) or not value.is_finite():
            raise _ParameterValueError(path, "参数必须是有限十进制数。")
        return value
    if kinds == {int}:
        if (
            isinstance(value, Decimal)
            and value.is_finite()
            and -maxsize <= value <= maxsize
            and value == value.to_integral_value()
        ):
            value = int(value)
        if type(value) is not int or not -maxsize <= value <= maxsize:
            raise _ParameterValueError(path, "参数必须是整数。")
        return value
    if kinds == {bool}:
        if type(value) is not bool:
            raise _ParameterValueError(path, "参数必须是布尔值。")
        return value
    if kinds == {str}:
        if not isinstance(value, str):
            raise _ParameterValueError(path, "参数必须是文本。")
        return value
    raise ValueError(f"template scalar type is inconsistent at {path}")


def normalize_rule_inputs(
    template: RuleSetSpec, inputs: tuple[EditableRuleInput, ...]
) -> tuple[EditableRuleInput, ...]:
    """Require an exact rule set, normalize types and return server template order."""

    if not isinstance(template, RuleSetSpec):
        raise ValueError("template must be RuleSetSpec")
    if not isinstance(inputs, (tuple, list)) or any(
        not isinstance(item, EditableRuleInput) for item in inputs
    ):
        raise ValueError("inputs must contain EditableRuleInput values")

    positions = {}
    issues = []
    expected = {item.rule_id for item in template.rules}
    for index, item in enumerate(inputs):
        if item.rule_id in positions:
            issues.append(
                _issue(
                    "duplicate_rule_id",
                    f"rules[{index}].rule_id",
                    "规则标识与前面的记录重复。",
                    item.rule_id,
                )
            )
        elif item.rule_id not in expected:
            issues.append(
                _issue(
                    "unknown_rule_id",
                    f"rules[{index}].rule_id",
                    "规则标识不属于当前服务端模板。",
                    item.rule_id,
                )
            )
        else:
            positions[item.rule_id] = (index, item)
    for item in template.rules:
        if item.rule_id not in positions:
            issues.append(
                _issue(
                    "missing_rule_id",
                    "rules",
                    "请求缺少服务端模板要求的规则。",
                    item.rule_id,
                )
            )
    if issues:
        raise RuleSetCompilationError(tuple(issues))

    normalized = []
    for definition in template.rules:
        index, item = positions[definition.rule_id]
        try:
            parameters = _normalize_parameter(
                item.parameters,
                (definition.parameters,),
                f"rules[{index}].parameters",
            )
        except _ParameterValueError as error:
            issues.append(
                _issue(
                    "invalid_rule_parameter",
                    error.path,
                    str(error),
                    definition.rule_id,
                )
            )
            continue
        normalized.append(EditableRuleInput(definition.rule_id, item.enabled, parameters))
    if issues:
        raise RuleSetCompilationError(tuple(issues))
    return tuple(normalized)


def normalize_virtual_prototypes(
    prototypes: tuple[VirtualPrototypeInput, ...],
    required_physical_fields: tuple[str, ...] = (),
) -> tuple[VirtualPrototypeInput, ...]:
    """Validate the independent prototype snapshot while preserving its order."""

    if not isinstance(prototypes, (tuple, list)) or any(
        not isinstance(item, VirtualPrototypeInput) for item in prototypes
    ):
        raise ValueError("prototypes must contain VirtualPrototypeInput values")
    issues = []
    seen = set()
    normalized = []
    reserved_attributes = {part.name for part in fields(Node)}
    for index, item in enumerate(prototypes):
        prefix = f"virtual_prototypes[{index}]"
        attributes = dict(item.rule_attributes)
        for name in TEXT_ATTRIBUTES:
            value = attributes.get(name)
            if value is None:
                continue
            if not isinstance(value, str):
                issues.append(
                    _issue(
                        "invalid_text_attribute",
                        f"{prefix}.rule_attributes.{name}",
                        "已知文本属性必须是文本或空值。",
                        item.prototype_id.strip() or None,
                    )
                )
                continue
            attributes[name] = (
                value.strip().upper() if name in UPPERCASE_ATTRIBUTES else value.strip()
            )
        for name in sorted(set(attributes) & reserved_attributes):
            issues.append(
                _issue(
                    "shadowed_core_field",
                    f"{prefix}.rule_attributes.{name}",
                    "规则属性不能覆盖虚拟材料核心字段。",
                    item.prototype_id.strip() or None,
                )
            )
        item = replace(
            item,
            prototype_id=item.prototype_id.strip(),
            grade=item.grade.strip().upper(),
            rule_attributes=attributes,
        )
        normalized.append(item)
        if not item.prototype_id:
            issues.append(
                _issue(
                    "empty_prototype_id",
                    f"{prefix}.prototype_id",
                    "虚拟材料原型标识不能为空。",
                )
            )
        elif item.prototype_id in seen:
            issues.append(
                _issue(
                    "duplicate_prototype_id",
                    f"{prefix}.prototype_id",
                    "虚拟材料原型标识与前面的记录重复。",
                    item.prototype_id,
                )
            )
        seen.add(item.prototype_id)
        for name in ("unit_weight", "width", "thickness", "min_temperature", "max_temperature"):
            value = getattr(item, name)
            if value is None:
                continue
            if not value.is_finite():
                issues.append(
                    _issue(
                        "non_finite_number",
                        f"{prefix}.{name}",
                        "数值不能为 NaN 或无穷值。",
                        item.prototype_id,
                    )
                )
            elif name != "unit_weight" and not isfinite(float(value)):
                issues.append(
                    _issue(
                        "non_finite_float_projection",
                        f"{prefix}.{name}",
                        "物理数值超出连接计算可使用的有限浮点范围。",
                        item.prototype_id,
                    )
                )
            elif name in ("unit_weight", "width", "thickness") and value <= 0:
                issues.append(
                    _issue(
                        (
                            "non_positive_weight"
                            if name == "unit_weight"
                            else "non_positive_dimension"
                        ),
                        f"{prefix}.{name}",
                        "重量、宽度和厚度必须大于零。",
                        item.prototype_id,
                    )
                )
        for name in required_physical_fields:
            if getattr(item, name) is None:
                issues.append(
                    _issue(
                        "missing_required_field",
                        f"{prefix}.{name}",
                        f"启用规则要求字段 {name} 提供有效值。",
                        item.prototype_id,
                    )
                )
        if (
            item.min_temperature is not None
            and item.max_temperature is not None
            and item.min_temperature.is_finite()
            and item.max_temperature.is_finite()
            and item.min_temperature > item.max_temperature
        ):
            issues.append(
                _issue(
                    "reversed_temperature_range",
                    f"{prefix}.min_temperature",
                    "最低温度不能高于最高温度。",
                    item.prototype_id,
                )
            )
    if issues:
        raise RuleSetCompilationError(tuple(issues))
    return tuple(normalized)


def normalize_rule_set_snapshot(
    template: RuleSetSpec,
    inputs: tuple[EditableRuleInput, ...],
    prototypes: tuple[VirtualPrototypeInput, ...],
) -> tuple[tuple[EditableRuleInput, ...], tuple[VirtualPrototypeInput, ...]]:
    rules = normalize_rule_inputs(template, inputs)
    try:
        template_version = int(template.version)
    except (TypeError, ValueError) as error:
        raise ValueError("template version must be a positive canonical integer") from error
    if template_version < 1 or str(template_version) != template.version:
        raise ValueError("template version must be a positive canonical integer")
    loaded = load_rule_set(compile_rule_set(template, rules, template_version).rule_set_spec)
    required = tuple(
        dict.fromkeys(
            name
            for rule in loaded.rules
            for name in rule.required_fields()
            if name in ("width", "thickness")
        )
    )
    return rules, normalize_virtual_prototypes(prototypes, required)


def _rule_set_data(spec: RuleSetSpec) -> dict:
    return {
        "product_line_code": spec.product_line_code,
        "process_code": spec.process_code,
        "scenario": spec.scenario,
        "version": spec.version,
        "rules": [
            {
                "rule_id": item.rule_id,
                "rule_type": item.rule_type,
                "name": item.name,
                "scope": item.scope.value,
                "enabled": item.enabled,
                "version": item.version,
                "parameters": item.parameters,
            }
            for item in spec.rules
        ],
        "quality_spec": [
            {part.name: getattr(item, part.name) for part in fields(item)}
            for item in spec.quality_spec
        ],
        "allowed_final_deviation_codes": sorted(spec.allowed_final_deviation_codes),
        "fingerprint": spec.fingerprint,
    }


def dumps_rule_set_spec(spec: RuleSetSpec) -> str:
    if not isinstance(spec, RuleSetSpec):
        raise ValueError("spec must be RuleSetSpec")
    return dumps_exact_json(_rule_set_data(spec))


def _object_from_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError(f"invalid JSON number: {value}")


def _exact_mapping(value, names: set[str], path: str):
    if not isinstance(value, Mapping) or set(value) != names:
        raise ValueError(f"{path} has an invalid field set")
    return value


def load_compiled_rule_set_json(payload: str | bytes | bytearray) -> RuleSetSpec:
    """Strictly read, verify and load the persisted complete rule snapshot."""

    if not isinstance(payload, (str, bytes, bytearray)):
        raise ValueError("compiled rule set payload must be JSON text or bytes")
    try:
        raw = json.loads(
            payload,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_from_pairs,
        )
    except (
        _DuplicateJsonKey,
        DecimalException,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as error:
        reason = f"duplicate JSON key: {error}" if isinstance(error, _DuplicateJsonKey) else error
        raise ValueError(f"compiled rule set JSON is invalid: {reason}") from error
    root = _exact_mapping(
        raw,
        {
            "product_line_code",
            "process_code",
            "scenario",
            "version",
            "rules",
            "quality_spec",
            "allowed_final_deviation_codes",
            "fingerprint",
        },
        "rule_set_spec",
    )
    if not isinstance(root["rules"], list):
        raise ValueError("rule_set_spec.rules must be an array")
    rules = []
    for index, value in enumerate(root["rules"]):
        path = f"rule_set_spec.rules[{index}]"
        item = _exact_mapping(
            value,
            {"rule_id", "rule_type", "name", "scope", "enabled", "version", "parameters"},
            path,
        )
        if not isinstance(item["parameters"], Mapping):
            raise ValueError(f"{path}.parameters must be an object")
        try:
            scope = RuleScope(item["scope"])
        except (TypeError, ValueError) as error:
            raise ValueError(f"{path}.scope is invalid") from error
        rules.append(RuleDefinitionSpec(**(item | {"scope": scope})))
    if not isinstance(root["quality_spec"], list):
        raise ValueError("rule_set_spec.quality_spec must be an array")
    quality = []
    quality_fields = {part.name for part in fields(QualityCriterionSpec)}
    for index, value in enumerate(root["quality_spec"]):
        path = f"rule_set_spec.quality_spec[{index}]"
        quality.append(QualityCriterionSpec(**_exact_mapping(value, quality_fields, path)))
    deviations = root["allowed_final_deviation_codes"]
    if not isinstance(deviations, list) or any(not isinstance(item, str) for item in deviations):
        raise ValueError("rule_set_spec.allowed_final_deviation_codes must be a text array")
    if len(deviations) != len(set(deviations)):
        raise ValueError("rule_set_spec.allowed_final_deviation_codes contains duplicates")
    spec = RuleSetSpec(
        product_line_code=root["product_line_code"],
        process_code=root["process_code"],
        scenario=root["scenario"],
        version=root["version"],
        rules=tuple(rules),
        quality_spec=tuple(quality),
        allowed_final_deviation_codes=frozenset(deviations),
        fingerprint=root["fingerprint"],
    )
    load_rule_set(spec)
    return spec


def compile_rule_set(
    template: RuleSetSpec,
    inputs: tuple[EditableRuleInput, ...],
    version_no: int,
) -> CompiledRuleSetSnapshot:
    """Complete fixed metadata, sign, load, round-trip and load again."""

    if type(version_no) is not int or version_no < 1:
        raise ValueError("version_no must be a positive integer")
    load_rule_set(template)
    normalized = normalize_rule_inputs(template, inputs)
    definitions = tuple(
        replace(item, enabled=value.enabled, parameters=value.parameters)
        for item, value in zip(template.rules, normalized)
    )
    unsigned = replace(
        template,
        version=str(version_no),
        rules=definitions,
        fingerprint="pending",
    )
    spec = replace(unsigned, fingerprint=fingerprint_rule_set_spec(unsigned))
    load_rule_set(spec)
    payload = dumps_rule_set_spec(spec)
    roundtrip = load_compiled_rule_set_json(payload)
    if roundtrip != spec or dumps_rule_set_spec(roundtrip) != payload:
        raise RuntimeError("compiled rule set changed during JSON round-trip")
    return CompiledRuleSetSnapshot(roundtrip, payload)


__all__ = [
    "CompiledRuleSetSnapshot",
    "RuleSetCompilationError",
    "compile_rule_set",
    "dumps_rule_set_spec",
    "load_compiled_rule_set_json",
    "normalize_rule_inputs",
    "normalize_rule_set_snapshot",
    "normalize_virtual_prototypes",
]
