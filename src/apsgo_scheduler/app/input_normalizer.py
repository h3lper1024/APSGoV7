"""Normalize typed input once, collecting diagnostics before returning a complete problem."""

from dataclasses import fields, replace
from math import isfinite

from ..api.request import OrderInput, SchedulingRequest, validate_request
from ..core.contracts import (
    DiagnosticIssue,
    DiagnosticPhase,
    DiagnosticSeverity,
    RuleScope,
    fingerprint,
    sum_weights,
)
from ..core.model import Node, SchedulingProblem, VirtualMaterialPrototype
from ..core.delivery_timing import normalize_delivery_timing
from ..core.rules.concrete import WEIGHT_EPSILON, ChainWeightRangeRule
from ..core.rules.rule_set import ProcessRuleSet
from .rule_set_loader import RuleSetLoadError, load_rule_set

TEXT_ATTRIBUTES = (
    "grade_class",
    "soft_hard_class",
    "hot_roll_grade",
    "product_type",
    "surface_grade",
    "customer_grade",
    "customer_name",
    "execution_standard",
)
UPPERCASE_ATTRIBUTES = frozenset(("hot_roll_grade", "surface_grade"))
ORDER_IDENTITIES = ("node_id", "source_order_id", "source_resource_id", "source_period")
PHYSICAL_FIELDS = ("width", "thickness", "min_temperature", "max_temperature")


class InputNormalizationError(ValueError):
    """All safely discoverable input and rule diagnostics, in stable phase order."""

    def __init__(self, issues: tuple[DiagnosticIssue, ...]):
        phase_order = {phase: index for index, phase in enumerate(DiagnosticPhase)}
        self.issues = tuple(sorted(issues, key=lambda issue: phase_order[issue.phase]))
        super().__init__("；".join(issue.message for issue in self.issues))


def normalize_input(
    request: SchedulingRequest, rule_set: ProcessRuleSet | None = None
) -> SchedulingProblem:
    issues = []

    def issue(code, path, message, subject=None, phase=DiagnosticPhase.INPUT_NORMALIZATION):
        issues.append(
            DiagnosticIssue(
                code,
                phase,
                path,
                subject if isinstance(subject, str) and subject.strip() else None,
                message,
                DiagnosticSeverity.ERROR,
            )
        )

    if not isinstance(request, SchedulingRequest):
        issue(
            "invalid_request_type",
            "request",
            "输入必须是 SchedulingRequest。",
            phase=DiagnosticPhase.REQUEST_VALIDATION,
        )
        raise InputNormalizationError(tuple(issues))

    def normalize_material(item, path, identity_fields):
        values = {name: getattr(item, name).strip() for name in identity_fields}
        values["grade"] = item.grade.strip().upper()
        attributes = dict(item.rule_attributes)
        for name in TEXT_ATTRIBUTES:
            value = attributes.get(name)
            if value is None:
                continue
            if not isinstance(value, str):
                issue(
                    "invalid_text_attribute",
                    f"{path}.rule_attributes.{name}",
                    "已知文本属性必须是文本或空值，不能隐式转换其他类型。",
                    values[identity_fields[0]],
                )
                continue
            attributes[name] = (
                value.strip().upper() if name in UPPERCASE_ATTRIBUTES else value.strip()
            )
        return replace(item, **values, rule_attributes=attributes)

    normalized = replace(
        request,
        orders=tuple(
            normalize_material(item, f"orders[{index}]", ORDER_IDENTITIES)
            for index, item in enumerate(request.orders)
        ),
        periods=tuple(replace(item, period_id=item.period_id.strip()) for item in request.periods),
        virtual_prototypes=tuple(
            normalize_material(item, f"virtual_prototypes[{index}]", ("prototype_id",))
            for index, item in enumerate(request.virtual_prototypes)
        ),
    )
    # Request/rule domain identity stays raw and exact; only material/period identifiers change.
    issues.extend(validate_request(normalized))
    expected_rules = None
    try:
        expected_rules = load_rule_set(request.rule_set_spec)
    except RuleSetLoadError as error:
        issues.extend(error.issues)
    if rule_set is not None and not isinstance(rule_set, ProcessRuleSet):
        issue(
            "invalid_rule_set_type",
            "rule_set",
            "必须提供已经加载的 ProcessRuleSet。",
            phase=DiagnosticPhase.RULE_LOADING,
        )
    elif (
        rule_set is not None
        and expected_rules is not None
        and (
            rule_set != expected_rules
            or any(
                rule_set.rules_for_scope(scope) != expected_rules.rules_for_scope(scope)
                for scope in RuleScope
            )
        )
    ):
        issue(
            "rule_set_content_mismatch",
            "rule_set",
            "传入规则集的完整内容与请求中已核验的规则配置不一致。",
            phase=DiagnosticPhase.RULE_LOADING,
        )

    # Only declarations from the independently verified configuration may drive input checks.
    verified_rules = expected_rules.rules if expected_rules is not None else ()
    real_required = tuple(
        dict.fromkeys(name for rule in verified_rules for name in rule.required_fields())
    )
    # Physical prototype fields can be required by plan/chain rules as well as edges.
    prototype_required = (
        tuple(
            dict.fromkeys(
                name
                for rule in verified_rules
                for name in rule.required_fields()
                if name in ("width", "thickness")
            )
        )
        if expected_rules is not None
        else ()
    )
    maximum = next(
        (
            sum_weights((rule.parameters["max_weight"], WEIGHT_EPSILON))
            for rule in verified_rules
            if isinstance(rule, ChainWeightRangeRule)
        ),
        None,
    )
    order_fields = {part.name for part in fields(OrderInput)} - {"rule_attributes"}
    for collection, required, identity in (
        ("orders", real_required, "node_id"),
        ("virtual_prototypes", prototype_required, "prototype_id"),
    ):
        for index, item in enumerate(getattr(normalized, collection)):
            path, subject = f"{collection}[{index}]", getattr(item, identity)
            for name in PHYSICAL_FIELDS:
                value = getattr(item, name)
                # Decimal NaN/infinity already has a request diagnostic; do not duplicate it.
                if value is not None and value.is_finite() and not isfinite(float(value)):
                    issue(
                        "non_finite_float_projection",
                        f"{path}.{name}",
                        "物理数值超出连接计算可使用的有限浮点范围。",
                        subject,
                    )
            for name in required:
                is_field = name in order_fields
                value = getattr(item, name) if is_field else item.rule_attributes.get(name)
                if value is None or (isinstance(value, str) and not value.strip()):
                    issue(
                        "missing_required_field",
                        f"{path}.{name}" if is_field else f"{path}.rule_attributes.{name}",
                        f"启用规则要求字段 {name} 提供有效值。",
                        subject,
                    )
            if (
                collection == "orders"
                and maximum is not None
                and item.weight.is_finite()
                and item.weight > maximum
            ):
                issue(
                    "atomic_node_above_chain_maximum",
                    f"{path}.weight",
                    "原始订单重量超过启用链重上限及允许误差，当前求解不能前置拆分该订单。",
                    subject,
                )
    nodes = []
    priority_required = tuple(
        name
        for rule in verified_rules
        if rule.scope is RuleScope.NODE
        for name in rule.required_fields()
    )
    for index, item in enumerate(normalized.orders):
        path = f"orders[{index}]"
        try:
            node = Node(**{part.name: getattr(item, part.name) for part in fields(OrderInput)})
        except ValueError as error:
            if not any(found.field_path.startswith(f"{path}.") for found in issues):
                issue(
                    "invalid_normalized_node",
                    path,
                    f"标准化后的订单不满足节点契约：{error}",
                    item.node_id,
                )
            continue
        nodes.append(node)
        required_paths = {
            f"{path}.{name}" if name in order_fields else f"{path}.rule_attributes.{name}"
            for name in priority_required
        }
        if expected_rules is None or any(found.field_path in required_paths for found in issues):
            continue
        try:
            expected_rules.construction_priority(node)
        except ValueError as error:
            issue(
                "invalid_construction_priority",
                f"{path}.rule_attributes",
                f"节点属性不满足启用优先级规则：{error}",
                node.node_id,
            )
    if issues:
        raise InputNormalizationError(tuple(issues))

    values = dict(
        product_line_code=normalized.product_line_code,
        process_code=normalized.process_code,
        scenario=normalized.scenario,
        nodes=tuple(nodes),
        period_order=tuple(
            item.period_id for item in sorted(normalized.periods, key=lambda item: item.sequence)
        ),
        virtual_prototypes=tuple(
            VirtualMaterialPrototype(
                **{part.name: getattr(item, part.name) for part in fields(item)}
            )
            for item in normalized.virtual_prototypes
        ),
    )
    if normalized.delivery_timing is not None:
        try:
            values["delivery_timing"] = normalize_delivery_timing(
                normalized.delivery_timing, values["nodes"], values["virtual_prototypes"]
            )
        except (ValueError, OverflowError) as error:
            issue("invalid_delivery_timing", "delivery_timing", str(error), None)
            raise InputNormalizationError(tuple(issues)) from error
    return SchedulingProblem(
        problem_id=request.request_id,
        input_fingerprint=fingerprint(values),
        **values,
    )
