"""Validate versioned input configuration before constructing a frozen rule set."""

from collections.abc import Mapping
from dataclasses import fields
from inspect import isabstract
from types import MappingProxyType

from ..api.request import RuleSetSpec
from ..core.contracts import (
    DiagnosticIssue,
    DiagnosticPhase,
    DiagnosticSeverity,
    fingerprint,
)
from ..core.rules.base import (
    NumericProjection,
    QualityAggregation,
    QualityCriterion,
    QualityDirection,
    Rule,
)
from ..core.rules.concrete import (
    ChainWeightRangeRule,
    ConsecutiveReverseWidthRule,
    ConsecutiveVirtualMaterialRule,
    ContinuousNarrowSteelWeightRule,
    ControlledOrderSplitRule,
    FutureFillWeightTargetRule,
    HighSurfaceRunCountRule,
    InterChainWidthGapRule,
    DeliveryDuePerformanceRule,
    LateOriginalPeriodMoveRule,
    ReverseWidthCountRule,
    SameSpecContinuousRealWeightRule,
    SoftHardConnectionRule,
    StrategicCustomerPriorityRule,
    SyntheticNodePriorityRule,
    SyntheticWidthLimitRule,
    TemperatureOverlapRule,
    ThicknessTransitionRule,
    VirtualOutputRatioRule,
    WidthTransitionRule,
)
from ..core.rules.rule_set import ProcessRuleSet

RULE_REGISTRY: Mapping[str, type[Rule]] = MappingProxyType(
    {
        "ChainWeightRangeRule": ChainWeightRangeRule,
        "ConsecutiveReverseWidthRule": ConsecutiveReverseWidthRule,
        "ConsecutiveVirtualMaterialRule": ConsecutiveVirtualMaterialRule,
        "ContinuousNarrowSteelWeightRule": ContinuousNarrowSteelWeightRule,
        "ControlledOrderSplitRule": ControlledOrderSplitRule,
        "FutureFillWeightTargetRule": FutureFillWeightTargetRule,
        "HighSurfaceRunCountRule": HighSurfaceRunCountRule,
        "InterChainWidthGapRule": InterChainWidthGapRule,
        "DeliveryDuePerformanceRule": DeliveryDuePerformanceRule,
        "LateOriginalPeriodMoveRule": LateOriginalPeriodMoveRule,
        "ReverseWidthCountRule": ReverseWidthCountRule,
        "SameSpecContinuousRealWeightRule": SameSpecContinuousRealWeightRule,
        "SoftHardConnectionRule": SoftHardConnectionRule,
        "StrategicCustomerPriorityRule": StrategicCustomerPriorityRule,
        "WidthTransitionRule": WidthTransitionRule,
        "SyntheticWidthLimitRule": SyntheticWidthLimitRule,
        "SyntheticNodePriorityRule": SyntheticNodePriorityRule,
        "TemperatureOverlapRule": TemperatureOverlapRule,
        "ThicknessTransitionRule": ThicknessTransitionRule,
        "VirtualOutputRatioRule": VirtualOutputRatioRule,
    }
)


class RuleSetLoadError(ValueError):
    """Configuration diagnostics for the caller; no partial rule set is returned."""

    def __init__(self, issues: tuple[DiagnosticIssue, ...]):
        self.issues = tuple(issues)
        super().__init__("；".join(issue.message for issue in self.issues))


def fingerprint_rule_set_spec(spec: RuleSetSpec) -> str:
    """Cover disabled records too, preserving rule and quality criterion order."""
    if not isinstance(spec, RuleSetSpec):
        raise ValueError("spec must be RuleSetSpec")
    return fingerprint(
        {part.name: getattr(spec, part.name) for part in fields(spec) if part.name != "fingerprint"}
    )


def load_rule_set(
    spec: RuleSetSpec, registry: Mapping[str, type[Rule]] = RULE_REGISTRY
) -> ProcessRuleSet:
    """Load only explicitly enabled classes from the supplied static registry."""
    issues = []

    def issue(code, path, message, subject=None):
        issues.append(
            DiagnosticIssue(
                code,
                DiagnosticPhase.RULE_LOADING,
                path,
                subject if isinstance(subject, str) and subject.strip() else None,
                message,
                DiagnosticSeverity.ERROR,
            )
        )

    def nonempty(value, path, subject=None):
        if not value.strip():
            issue("empty_identity", path, "规则配置字段不能为空或只包含空白字符。", subject)

    if not isinstance(spec, RuleSetSpec):
        issue("invalid_rule_set_spec", "rule_set_spec", "必须提供类型明确的规则集配置。")
        raise RuleSetLoadError(tuple(issues))
    if not isinstance(registry, Mapping):
        issue("invalid_rule_registry", "registry", "规则注册表必须是类名到具体规则类的映射。")

    for name in ("product_line_code", "process_code", "scenario", "version", "fingerprint"):
        nonempty(getattr(spec, name), f"rule_set_spec.{name}")
    for code in sorted(spec.allowed_final_deviation_codes):
        nonempty(code, "rule_set_spec.allowed_final_deviation_codes")
    seen_rule_ids = set()
    for index, definition in enumerate(spec.rules):
        prefix = f"rule_set_spec.rules[{index}]"
        for name in ("rule_id", "rule_type", "name", "version"):
            nonempty(getattr(definition, name), f"{prefix}.{name}", definition.rule_id)
        if definition.rule_id in seen_rule_ids:
            issue(
                "duplicate_rule_id",
                f"{prefix}.rule_id",
                "规则身份重复，停用规则也必须具有唯一身份。",
                definition.rule_id,
            )
        seen_rule_ids.add(definition.rule_id)

    criteria = []
    seen_criterion_ids = set()
    for index, criterion in enumerate(spec.quality_spec):
        prefix = f"rule_set_spec.quality_spec[{index}]"
        for part in fields(criterion):
            nonempty(getattr(criterion, part.name), f"{prefix}.{part.name}", criterion.criterion_id)
        if criterion.criterion_id in seen_criterion_ids:
            issue(
                "duplicate_criterion_id",
                f"{prefix}.criterion_id",
                "质量项身份不能重复。",
                criterion.criterion_id,
            )
        seen_criterion_ids.add(criterion.criterion_id)
        converted = {}
        enum_fields = (
            ("direction", QualityDirection),
            ("aggregation", QualityAggregation),
            ("numeric_projection", NumericProjection),
        )
        for name, enum_type in enum_fields:
            try:
                converted[name] = enum_type(getattr(criterion, name))
            except ValueError:
                issue(
                    "invalid_quality_option",
                    f"{prefix}.{name}",
                    f"质量项 {name} 使用了不支持的取值。",
                    criterion.criterion_id,
                )
        if (
            len(converted) == len(enum_fields)
            and criterion.criterion_id.strip()
            and criterion.metric_key.strip()
        ):
            try:
                criteria.append(
                    QualityCriterion(criterion.criterion_id, criterion.metric_key, **converted)
                )
            except ValueError as error:
                issue(
                    "invalid_quality_combination",
                    prefix,
                    f"质量项组合校验失败：{error}",
                    criterion.criterion_id,
                )

    if spec.fingerprint != fingerprint_rule_set_spec(spec):
        issue(
            "rule_set_fingerprint_mismatch",
            "rule_set_spec.fingerprint",
            "规则集指纹与完整配置不一致，不能使用未核验的配置。",
        )
    # Validate the complete signed configuration before executing any constructor.
    if issues:
        raise RuleSetLoadError(tuple(issues))

    rules = []
    for index, definition in enumerate(spec.rules):
        if not definition.enabled:
            continue
        prefix = f"rule_set_spec.rules[{index}]"
        rule_class = registry.get(definition.rule_type)
        if rule_class is None:
            issue(
                "unknown_enabled_rule",
                f"{prefix}.rule_type",
                "启用规则没有已注册的具体实现。",
                definition.rule_id,
            )
            continue
        if (
            not isinstance(rule_class, type)
            or rule_class.__bases__ != (Rule,)
            or isabstract(rule_class)
            or rule_class.__name__ != definition.rule_type
        ):
            issue(
                "invalid_rule_registration",
                f"{prefix}.rule_type",
                "注册项必须以具体类名映射到直接继承 Rule 的非抽象规则类。",
                definition.rule_id,
            )
            continue
        if getattr(rule_class, "supported_scope", None) is not definition.scope:
            issue(
                "rule_scope_mismatch",
                f"{prefix}.scope",
                "配置作用域与具体规则声明的作用域不一致。",
                definition.rule_id,
            )
            continue
        try:
            rule = rule_class(
                rule_id=definition.rule_id,
                name=definition.name,
                scope=definition.scope,
                enabled=True,
                version=definition.version,
                parameters=definition.parameters,
            )
        except ValueError as error:
            issue(
                "invalid_rule_parameters",
                f"{prefix}.parameters",
                f"规则参数校验失败：{error}",
                definition.rule_id,
            )
            continue
        required = rule.required_fields()
        if (
            not isinstance(required, tuple)
            or any(not isinstance(name, str) or not name.strip() for name in required)
            or len(set(required)) != len(required)
        ):
            issue(
                "invalid_required_fields",
                f"{prefix}.rule_type",
                "规则必需字段声明必须是无重复、非空字段名组成的元组。",
                definition.rule_id,
            )
            continue
        rules.append(rule)

    if issues:
        raise RuleSetLoadError(tuple(issues))
    try:
        return ProcessRuleSet(
            product_line_code=spec.product_line_code,
            process_code=spec.process_code,
            scenario=spec.scenario,
            version=spec.version,
            rules=tuple(rules),
            quality_spec=tuple(criteria),
            allowed_final_deviation_codes=spec.allowed_final_deviation_codes,
            fingerprint=spec.fingerprint,
        )
    except ValueError as error:
        issue("invalid_rule_set", "rule_set_spec", f"规则集组合校验失败：{error}")
        raise RuleSetLoadError(tuple(issues)) from error
