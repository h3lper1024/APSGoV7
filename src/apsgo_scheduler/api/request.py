"""Immutable public input shapes and aggregate, rule-independent validation."""

from collections.abc import Mapping
from dataclasses import dataclass, fields
from decimal import Decimal

from ..core.contracts import (
    DiagnosticIssue,
    DiagnosticPhase,
    DiagnosticSeverity,
    RuleParameterValue,
    RuleScalar,
    RuleScope,
    SolverPolicy,
    fingerprint,
    freeze_rule_parameters,
    freeze_scalars,
    freeze_tuple,
    require_enum,
    require_int,
)
from ..core.model import MaterialRole, Node


def _text_fields(instance, *names):
    for name in names:
        if not isinstance(getattr(instance, name), str):
            raise ValueError(f"{name} must be text")


def _material_fields(instance, weight_field):
    _text_fields(instance, "grade")
    for name in (weight_field, "width", "thickness", "min_temperature", "max_temperature"):
        value = getattr(instance, name)
        if value is None and name != weight_field:
            continue
        if not isinstance(value, Decimal):
            raise ValueError(f"{name} must be Decimal")
    object.__setattr__(instance, "rule_attributes", freeze_scalars(instance.rule_attributes))


@dataclass(frozen=True, slots=True)
class OrderInput:
    node_id: str
    source_order_id: str
    source_resource_id: str
    source_period: str
    weight: Decimal
    width: Decimal | None
    thickness: Decimal | None
    min_temperature: Decimal | None
    max_temperature: Decimal | None
    grade: str
    material_role: MaterialRole
    rule_attributes: Mapping[str, RuleScalar]

    def __post_init__(self):
        _text_fields(self, "node_id", "source_order_id", "source_resource_id", "source_period")
        _material_fields(self, "weight")
        require_enum(self.material_role, MaterialRole, "material_role")


@dataclass(frozen=True, slots=True)
class PeriodInput:
    period_id: str
    sequence: int

    def __post_init__(self):
        _text_fields(self, "period_id")
        require_int(self.sequence, "sequence", minimum=None)


@dataclass(frozen=True, slots=True)
class VirtualPrototypeInput:
    prototype_id: str
    unit_weight: Decimal
    width: Decimal | None
    thickness: Decimal | None
    min_temperature: Decimal | None
    max_temperature: Decimal | None
    grade: str
    rule_attributes: Mapping[str, RuleScalar]

    def __post_init__(self):
        _text_fields(self, "prototype_id")
        _material_fields(self, "unit_weight")


@dataclass(frozen=True, slots=True)
class RuleDefinitionSpec:
    rule_id: str
    rule_type: str
    name: str
    scope: RuleScope
    enabled: bool
    version: str
    parameters: Mapping[str, RuleParameterValue]

    def __post_init__(self):
        _text_fields(self, "rule_id", "rule_type", "name", "version")
        require_enum(self.scope, RuleScope, "scope")
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be bool")
        object.__setattr__(self, "parameters", freeze_rule_parameters(self.parameters))


@dataclass(frozen=True, slots=True)
class QualityCriterionSpec:
    criterion_id: str
    metric_key: str
    direction: str
    aggregation: str
    numeric_projection: str

    def __post_init__(self):
        _text_fields(self, *(part.name for part in fields(self)))


@dataclass(frozen=True, slots=True)
class RuleSetSpec:
    product_line_code: str
    process_code: str
    scenario: str
    version: str
    rules: tuple[RuleDefinitionSpec, ...]
    quality_spec: tuple[QualityCriterionSpec, ...]
    allowed_final_deviation_codes: frozenset[str]
    fingerprint: str

    def __post_init__(self):
        _text_fields(
            self, "product_line_code", "process_code", "scenario", "version", "fingerprint"
        )
        object.__setattr__(self, "rules", freeze_tuple(self.rules, RuleDefinitionSpec, "rules"))
        object.__setattr__(
            self,
            "quality_spec",
            freeze_tuple(self.quality_spec, QualityCriterionSpec, "quality_spec"),
        )
        codes = self.allowed_final_deviation_codes
        if not isinstance(codes, (frozenset, set, tuple, list)) or any(
            not isinstance(code, str) for code in codes
        ):
            raise ValueError("allowed_final_deviation_codes must contain text codes")
        object.__setattr__(self, "allowed_final_deviation_codes", frozenset(codes))


@dataclass(frozen=True, slots=True)
class SchedulingRequest:
    contract_version: str
    request_id: str
    product_line_code: str
    process_code: str
    scenario: str
    orders: tuple[OrderInput, ...]
    periods: tuple[PeriodInput, ...]
    virtual_prototypes: tuple[VirtualPrototypeInput, ...]
    rule_set_spec: RuleSetSpec
    policy: SolverPolicy

    def __post_init__(self):
        _text_fields(
            self, "contract_version", "request_id", "product_line_code", "process_code", "scenario"
        )
        for name, item_type in (
            ("orders", OrderInput),
            ("periods", PeriodInput),
            ("virtual_prototypes", VirtualPrototypeInput),
        ):
            object.__setattr__(self, name, freeze_tuple(getattr(self, name), item_type, name))
        if not isinstance(self.rule_set_spec, RuleSetSpec):
            raise ValueError("rule_set_spec must be RuleSetSpec")
        if not isinstance(self.policy, SolverPolicy):
            raise ValueError("policy must be the core SolverPolicy")


def fingerprint_public_request(request: SchedulingRequest) -> str:
    if not isinstance(request, SchedulingRequest):
        raise ValueError("request must be SchedulingRequest")
    return fingerprint(request)


def validate_request(request: SchedulingRequest) -> tuple[DiagnosticIssue, ...]:
    """Collect semantic errors; constructors enforce only serializable input shapes.

    SolverPolicy is already a validated core value. Rule-specific requirements,
    implementation lookup and fingerprint verification belong to the loader.
    """
    if not isinstance(request, SchedulingRequest):
        raise ValueError("request must be SchedulingRequest")
    issues = []

    def issue(code, path, message, subject=None):
        issues.append(
            DiagnosticIssue(
                code,
                DiagnosticPhase.REQUEST_VALIDATION,
                path,
                subject if subject and subject.strip() else None,
                message,
                DiagnosticSeverity.ERROR,
            )
        )

    def nonempty(value, path, subject=None):
        if not value.strip():
            issue("empty_identity", path, "字段不能为空或只包含空白字符。", subject)

    def unique(items, field_name, prefix):
        seen = set()
        for index, item in enumerate(items):
            value = getattr(item, field_name)
            path = f"{prefix}[{index}].{field_name}"
            if isinstance(value, str):
                nonempty(value, path)
            if value in seen:
                issue("duplicate_identity", path, "字段值与前面的记录重复。")
            seen.add(value)

    for name in ("contract_version", "request_id", "product_line_code", "process_code", "scenario"):
        nonempty(getattr(request, name), name)
    spec = request.rule_set_spec
    for name in ("product_line_code", "process_code", "scenario", "version", "fingerprint"):
        nonempty(getattr(spec, name), f"rule_set_spec.{name}")
    for name in ("product_line_code", "process_code", "scenario"):
        if getattr(request, name) != getattr(spec, name):
            issue(
                "rule_set_identity_mismatch",
                f"rule_set_spec.{name}",
                f"必须与请求字段 {name} 逐字符完全相同。",
            )
    for name in ("orders", "periods"):
        if not getattr(request, name):
            issue("empty_collection", name, "至少需要一条记录。")
    for name in ("node_id", "source_order_id", "source_resource_id"):
        unique(request.orders, name, "orders")
    unique(request.periods, "period_id", "periods")
    unique(request.periods, "sequence", "periods")
    for index, period in enumerate(request.periods):
        if period.sequence not in range(len(request.periods)):
            issue(
                "invalid_period_sequence",
                f"periods[{index}].sequence",
                "计划期序号必须从零开始连续，且不能重复。",
            )
    known_periods = {period.period_id for period in request.periods}
    unique(request.virtual_prototypes, "prototype_id", "virtual_prototypes")

    reserved_attributes = {part.name for part in fields(Node)}
    for collection, weight_name, id_name in (
        ("orders", "weight", "node_id"),
        ("virtual_prototypes", "unit_weight", "prototype_id"),
    ):
        for index, item in enumerate(getattr(request, collection)):
            prefix, subject = f"{collection}[{index}]", getattr(item, id_name)
            for name in (weight_name, "width", "thickness", "min_temperature", "max_temperature"):
                value = getattr(item, name)
                if value is None:
                    continue
                if not value.is_finite():
                    issue(
                        "non_finite_number",
                        f"{prefix}.{name}",
                        "数值不能为 NaN 或无穷值。",
                        subject,
                    )
                elif name in (weight_name, "width", "thickness") and value <= 0:
                    issue(
                        "non_positive_weight" if name == weight_name else "non_positive_dimension",
                        f"{prefix}.{name}",
                        "数值必须大于零。",
                        subject,
                    )
            minimum, maximum = item.min_temperature, item.max_temperature
            if (
                minimum is not None
                and maximum is not None
                and minimum.is_finite()
                and maximum.is_finite()
                and minimum > maximum
            ):
                issue(
                    "reversed_temperature_interval",
                    f"{prefix}.max_temperature",
                    "最高温度不能低于最低温度。",
                    subject,
                )
            for name in sorted(reserved_attributes & item.rule_attributes.keys()):
                issue(
                    "shadowed_core_field",
                    f"{prefix}.rule_attributes.{name}",
                    "规则扩展属性不能覆盖核心字段。",
                    subject,
                )
            if isinstance(item, OrderInput):
                nonempty(item.source_period, f"{prefix}.source_period", subject)
                if item.source_period not in known_periods:
                    issue(
                        "unknown_source_period",
                        f"{prefix}.source_period",
                        "订单来源计划期不在本次任务的计划期目录中。",
                        subject,
                    )
                if item.material_role is MaterialRole.GENERATED_VIRTUAL:
                    issue(
                        "unsupported_input_material_role",
                        f"{prefix}.material_role",
                        "虚拟材料只能由求解器根据原型生成，不能作为真实订单输入。",
                        subject,
                    )

    for index, rule in enumerate(spec.rules):
        for name in ("rule_id", "rule_type", "name", "version"):
            nonempty(getattr(rule, name), f"rule_set_spec.rules[{index}].{name}", rule.rule_id)
    for index, criterion in enumerate(spec.quality_spec):
        for part in fields(criterion):
            nonempty(
                getattr(criterion, part.name),
                f"rule_set_spec.quality_spec[{index}].{part.name}",
                criterion.criterion_id,
            )
    for code in sorted(spec.allowed_final_deviation_codes):
        nonempty(code, "rule_set_spec.allowed_final_deviation_codes")
    return tuple(issues)
