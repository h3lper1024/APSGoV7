"""Strict GQGA4 monthly-solve transport conversion, without HTTP concerns."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal, DecimalException
from enum import Enum
from uuid import UUID

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_scheduler.api.request import OrderInput, PeriodInput
from apsgo_scheduler.core.contracts import (
    DiagnosticIssue,
    DiagnosticPhase,
    DiagnosticSeverity,
    RuleScope,
    SolverPolicy,
    INTEGER_NUMERIC_SEMANTICS_KEY,
    fingerprint,
    freeze_tuple,
    require_int,
    require_text,
)
from apsgo_scheduler.core.model import MaterialRole, SchedulePlan
from apsgo_scheduler.core.rules.base import RuleViolation
from apsgo_scheduler.core.delivery_timing import OrderTimingInput, production_hours, validate_earliest_start

from .scheduling import BoundSchedulingResult, SchedulingTaskInput

MONTH_SOLVE_CONTRACT_VERSION = "v7-month-solve-v1"
MONTH_DELIVERY_CONTRACT_VERSION = "v7-month-solve-v2"
MONTH_EARLIEST_START_CONTRACT_VERSION = "v7-month-solve-v3"
_TIMING_FIELDS = frozenset(("due_date", "furnace_speed_mpm", "process_speed_mpm"))

_ROOT_FIELDS = frozenset(("contract_version", "request_id", "expected_active_version_id", "periods", "orders"))
_PERIOD_FIELDS = frozenset(("period_id", "sequence"))
_ORDER_FIELDS = frozenset(
    (
        "source_order_id",
        "source_period",
        "is_virtual",
        "weight",
        "grade",
        "grade_class",
        "hot_roll_grade",
        "width",
        "thickness",
        "min_temperature",
        "max_temperature",
        "customer_grade",
        "customer_name",
        "execution_standard",
        "surface_grade",
    )
)
_WIDTH_REASONS = frozenset(
    (
        "missing_width",
        "invalid_width",
        "synthetic_width_increase_exceeded",
        "width_transition_exceeded",
        "virtual_bridge_reverse_width_exceeded",
        "reverse_width",
        "consecutive_reverse_width",
    )
)


class MonthSchedulingContractError(ValueError):
    """Malformed monthly request with a stable, field-locatable issue."""

    def __init__(self, issues: tuple[DiagnosticIssue, ...]):
        self.issues = freeze_tuple(issues, DiagnosticIssue, "issues")
        if not self.issues:
            raise ValueError("contract error requires at least one issue")
        super().__init__(self.issues[0].message)


class MonthSchedulingMappingError(ValueError):
    """A bound result cannot be represented by the frozen monthly contract."""


@dataclass(frozen=True, slots=True)
class MonthSolveRequest:
    expected_active_version_id: int
    raw_request_fingerprint: str
    typed_request_fingerprint: str
    task_input: SchedulingTaskInput

    def __post_init__(self) -> None:
        require_int(self.expected_active_version_id, "expected_active_version_id", minimum=1)
        require_text(self.raw_request_fingerprint, "raw_request_fingerprint")
        require_text(self.typed_request_fingerprint, "typed_request_fingerprint")
        if not isinstance(self.task_input, SchedulingTaskInput):
            raise ValueError("task_input must be SchedulingTaskInput")
        if self.typed_request_fingerprint != fingerprint(self.task_input):
            raise ValueError("typed_request_fingerprint does not match task_input")


class _DuplicateJsonKey(ValueError):
    pass


def _raise_contract(
    code: str,
    field_path: str,
    message: str,
    subject_id: str | None = None,
) -> None:
    raise MonthSchedulingContractError(
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


def _object_from_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey(key)
        value[key] = item
    return value


def _reject_constant(value: str):
    raise ValueError(f"invalid JSON number: {value}")


def _load_json(payload: str | bytes | bytearray):
    if not isinstance(payload, (str, bytes, bytearray)):
        _raise_contract("invalid_json", "body", "请求体必须是 JSON 文本或字节。")
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = bytes(payload).decode("utf-8")
        except UnicodeDecodeError as error:
            _raise_contract("invalid_json", "body", f"请求体不是合法 UTF-8 JSON：{error}。")
    try:
        return json.loads(
            payload,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_from_pairs,
        )
    except _DuplicateJsonKey as error:
        _raise_contract("duplicate_json_key", "body", f"JSON 对象包含重复键：{error}。")
    except (DecimalException, json.JSONDecodeError, RecursionError, ValueError) as error:
        _raise_contract("invalid_json", "body", f"请求体不是合法 JSON：{error}。")


def _exact_object(value, path: str, names: frozenset[str], optional=frozenset()) -> Mapping:
    if not isinstance(value, Mapping):
        _raise_contract("invalid_field_type", path or "body", "字段必须是 JSON 对象。")
    missing = sorted(names - value.keys())
    if missing:
        _raise_contract(
            "missing_field",
            f"{path}.{missing[0]}" if path else missing[0],
            "缺少必需字段。",
        )
    unknown = sorted(value.keys() - names - optional)
    if unknown:
        _raise_contract(
            "unknown_field",
            f"{path}.{unknown[0]}" if path else unknown[0],
            "请求包含未定义字段。",
        )
    return value


def _array(value, path: str) -> list:
    if not isinstance(value, list):
        _raise_contract("invalid_field_type", path, "字段必须是 JSON 数组。")
    if not value:
        _raise_contract("empty_collection", path, "至少需要一条记录。")
    return value


def _text(value, path: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        _raise_contract("invalid_field_type", path, "字段必须是文本。")
    value = value.strip()
    if not value and not optional:
        _raise_contract("invalid_field_value", path, "字段不能为空或只包含空白字符。")
    return value or None


def _decimal(value, path: str, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if type(value) is int:
        value = Decimal(value)
    if not isinstance(value, Decimal) or not value.is_finite():
        _raise_contract("invalid_field_type", path, "字段必须是有限十进制数。")
    return value


def _positive_decimal(value, path: str, *, optional: bool = False) -> Decimal | None:
    value = _decimal(value, path, optional=optional)
    if value is not None and value <= 0:
        _raise_contract("invalid_field_value", path, "字段必须大于零。")
    return value


def _positive_int(value, path: str) -> int:
    if type(value) is not int or value < 1:
        _raise_contract("invalid_field_type", path, "字段必须是正整数。")
    return value


def _period(value, index: int) -> PeriodInput:
    path = f"periods[{index}]"
    item = _exact_object(value, path, _PERIOD_FIELDS)
    sequence = item["sequence"]
    if type(sequence) is not int or sequence < 0:
        _raise_contract("invalid_field_type", f"{path}.sequence", "字段必须是非负整数。")
    return PeriodInput(_text(item["period_id"], f"{path}.period_id"), sequence)


def _material_role(customer_grade, hot_roll_grade, execution_standard) -> MaterialRole:
    transition = (
        (customer_grade or "") != "战略客户"
        and (hot_roll_grade or "").upper() == "SPHC"
        and (execution_standard or "") == "Q/TB 305-2017"
    )
    return MaterialRole.ACTUAL_TRANSITION if transition else MaterialRole.NORMAL_REAL


def _order(value, index: int, *, delivery=False, earliest=False) -> OrderInput:
    path = f"orders[{index}]"
    item = _exact_object(value, path, _ORDER_FIELDS | _TIMING_FIELDS if delivery else _ORDER_FIELDS,
                         {"earliest_start_at"} if earliest else frozenset())
    source_order_id = _text(item["source_order_id"], f"{path}.source_order_id")
    is_virtual = item["is_virtual"]
    if type(is_virtual) is not bool:
        _raise_contract("invalid_field_type", f"{path}.is_virtual", "字段必须是 JSON 布尔值。", source_order_id)
    if is_virtual:
        _raise_contract(
            "unsupported_input_virtual_order",
            f"{path}.is_virtual",
            "月计划输入不能包含生成型虚拟材料。",
            source_order_id,
        )
    grade = _text(item["grade"], f"{path}.grade")
    grade_class = _text(item["grade_class"], f"{path}.grade_class", optional=True)
    hot_roll_grade = _text(item["hot_roll_grade"], f"{path}.hot_roll_grade", optional=True)
    customer_grade = _text(item["customer_grade"], f"{path}.customer_grade", optional=True)
    customer_name = _text(item["customer_name"], f"{path}.customer_name", optional=True)
    execution_standard = _text(
        item["execution_standard"], f"{path}.execution_standard", optional=True
    )
    surface_grade = _text(item["surface_grade"], f"{path}.surface_grade", optional=True)
    minimum = _decimal(item["min_temperature"], f"{path}.min_temperature", optional=True)
    maximum = _decimal(item["max_temperature"], f"{path}.max_temperature", optional=True)
    if minimum is not None and maximum is not None and minimum > maximum:
        _raise_contract(
            "invalid_field_value",
            f"{path}.max_temperature",
            "最高温度不能低于最低温度。",
            source_order_id,
        )
    attributes = {
        "grade_class": grade_class,
        "hot_roll_grade": hot_roll_grade,
        "soft_hard_class": None,
        "customer_grade": customer_grade,
        "customer_name": customer_name,
        "execution_standard": execution_standard,
        "surface_grade": surface_grade,
    }
    return OrderInput(
        node_id=source_order_id,
        source_order_id=source_order_id,
        source_resource_id=source_order_id,
        source_period=_text(item["source_period"], f"{path}.source_period"),
        weight=_positive_decimal(item["weight"], f"{path}.weight"),
        width=_positive_decimal(item["width"], f"{path}.width", optional=True),
        thickness=_positive_decimal(item["thickness"], f"{path}.thickness", optional=True),
        min_temperature=minimum,
        max_temperature=maximum,
        grade=grade,
        material_role=_material_role(customer_grade, hot_roll_grade, execution_standard),
        rule_attributes=attributes,
    )


def _ensure_unique(items: Sequence, field_name: str, path: str) -> None:
    seen = set()
    for index, item in enumerate(items):
        value = getattr(item, field_name)
        if value in seen:
            _raise_contract(
                "duplicate_identity",
                f"{path}[{index}].{field_name}",
                "字段值与前面的记录重复。",
                value if isinstance(value, str) else None,
            )
        seen.add(value)


def _schedule_start(value):
    text = _text(value, "schedule_start_at")
    try:
        parsed = datetime.fromisoformat(text)
        if "T" not in text or parsed.tzinfo is None or parsed.utcoffset() is None or parsed.microsecond % 1000:
            raise ValueError("timezone required")
        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(timespec="milliseconds")
    except (ValueError, OverflowError):
        _raise_contract("invalid_schedule_start", "schedule_start_at", "计划生产开始时间必须是带时区的有效日期时间。")


def _order_timing(raw, order, index):
    path = f"orders[{index}]"
    due = _text(raw["due_date"], f"{path}.due_date")
    try:
        parsed_due = date.fromisoformat(due)
        if parsed_due.isoformat() != due:
            raise ValueError("noncanonical date")
        parsed_due + timedelta(days=1)
    except (ValueError, OverflowError):
        _raise_contract("invalid_due_date", f"{path}.due_date", "交货日期必须为有效的 YYYY-MM-DD。", order.source_order_id)
    speeds = tuple(_decimal(raw[name], f"{path}.{name}", optional=True)
                   for name in ("furnace_speed_mpm", "process_speed_mpm"))
    speed = next((value for value in speeds if value is not None and value > 0), None)
    if speed is None:
        _raise_contract("missing_production_speed", f"{path}.furnace_speed_mpm", "炉区速度和备用工艺速度至少有一个有效正值。", order.source_order_id)
    for name in ("width", "thickness"):
        _positive_decimal(raw[name], f"{path}.{name}")
    earliest = raw.get("earliest_start_at")
    if earliest is not None:
        try:
            validate_earliest_start(earliest)
        except (ValueError, TypeError, OverflowError):
            _raise_contract("invalid_earliest_start", f"{path}.earliest_start_at",
                "最早开始时间必须为带 +08:00 的秒级北京时间。", order.source_order_id)
    return OrderTimingInput(order.source_order_id, due, production_hours(
        order.weight, order.width, order.thickness, speed
    ), earliest_start_at=earliest)


def loads_month_solve_request(
    payload: str | bytes | bytearray,
    policy: SolverPolicy,
) -> MonthSolveRequest:
    """Parse the external body exactly, then build the existing service input."""

    if not isinstance(policy, SolverPolicy):
        raise ValueError("policy must be SolverPolicy")
    raw = _load_json(payload)
    earliest = isinstance(raw, Mapping) and raw.get("contract_version") == MONTH_EARLIEST_START_CONTRACT_VERSION
    delivery = earliest or isinstance(raw, Mapping) and raw.get("contract_version") == MONTH_DELIVERY_CONTRACT_VERSION
    raw = _exact_object(raw, "", _ROOT_FIELDS | {"schedule_start_at"} if delivery else _ROOT_FIELDS)
    if raw["contract_version"] not in (MONTH_SOLVE_CONTRACT_VERSION, MONTH_DELIVERY_CONTRACT_VERSION, MONTH_EARLIEST_START_CONTRACT_VERSION):
        _raise_contract(
            "unsupported_contract_version",
            "contract_version",
            "contract_version 必须为受支持的月计划 v1、v2 或 v3 契约。",
        )
    request_id = _text(raw["request_id"], "request_id")
    try:
        request_id = str(UUID(request_id))
    except ValueError:
        _raise_contract("invalid_field_value", "request_id", "request_id 必须是 UUID 文本。")
    expected_version = _positive_int(
        raw["expected_active_version_id"], "expected_active_version_id"
    )
    periods = tuple(_period(item, index) for index, item in enumerate(_array(raw["periods"], "periods")))
    order_rows = _array(raw["orders"], "orders")
    orders = tuple(_order(item, index, delivery=delivery, earliest=earliest) for index, item in enumerate(order_rows))
    _ensure_unique(periods, "period_id", "periods")
    _ensure_unique(periods, "sequence", "periods")
    _ensure_unique(orders, "source_order_id", "orders")
    if {period.sequence for period in periods} != set(range(len(periods))):
        _raise_contract(
            "invalid_period_sequence",
            "periods",
            "计划期序号必须从零开始连续，且不能重复。",
        )
    known_periods = {period.period_id for period in periods}
    for index, order in enumerate(orders):
        if order.source_period not in known_periods:
            _raise_contract(
                "unknown_source_period",
                f"orders[{index}].source_period",
                "订单来源计划期不在本次任务的计划期目录中。",
                order.source_order_id,
            )
    task_input = SchedulingTaskInput(
        contract_version=raw["contract_version"],
        request_id=request_id,
        product_line_code="GQGA4",
        process_code="default",
        scenario="month",
        orders=orders,
        periods=tuple(sorted(periods, key=lambda item: item.sequence)),
        policy=replace(policy, numeric_semantics_key=INTEGER_NUMERIC_SEMANTICS_KEY) if delivery else policy,
        schedule_start_at=_schedule_start(raw["schedule_start_at"]) if delivery else None,
        order_timing=tuple(_order_timing(row, order, index) for index, (row, order) in enumerate(zip(order_rows, orders))) if delivery else (),
    )
    return MonthSolveRequest(
        expected_active_version_id=expected_version,
        raw_request_fingerprint=fingerprint(raw),
        typed_request_fingerprint=fingerprint(task_input),
        task_input=task_input,
    )


def _enum(value):
    return value.value if isinstance(value, Enum) else value


def _issue_data(issue: DiagnosticIssue) -> dict:
    return {
        "code": issue.code,
        "phase": issue.phase.value,
        "field_path": issue.field_path,
        "subject_id": issue.subject_id,
        "message": issue.message,
        "severity": issue.severity.value,
    }


def _violation_data(violation: RuleViolation) -> dict:
    return {
        "rule_id": violation.rule_id,
        "scope": violation.scope.value,
        "subject_id": violation.subject_id,
        "reason_code": violation.reason_code,
        "message": violation.message,
        "disposition": violation.disposition.value,
        "severity": violation.severity,
    }


def _lineage_data(value):
    if value is None:
        return None
    return {part.name: _enum(getattr(value, part.name)) for part in fields(value)}


def _warning_targets(plan: SchedulePlan, violations: Sequence[RuleViolation]):
    warnings = {
        node.node_id: {"width_warning": [], "thickness_warning": [], "temperature_warning": [],
                       "chain_warning": [], "business_warning": []}
        for chain in plan.chains
        for node in chain.nodes
    }
    chains = {chain.chain_id: chain for chain in plan.chains}
    edges = {
        f"{chain.chain_id}:{left.node_id}>{right.node_id}": right.node_id
        for chain in plan.chains
        for left, right in zip(chain.nodes, chain.nodes[1:])
    }
    for violation in violations:
        field = "chain_warning" if violation.scope is RuleScope.CHAIN else "business_warning"
        node_id = violation.subject_id if violation.subject_id in warnings else None
        if violation.reason_code in _WIDTH_REASONS:
            field = "width_warning"
        elif violation.reason_code in {"thickness", "thickness_transition_exceeded"}:
            field = "thickness_warning"
        elif violation.reason_code in {"temperature", "temperature_overlap_below_minimum"}:
            field = "temperature_warning"
        if node_id is None:
            node_id = edges.get(violation.subject_id)
        if node_id is None and violation.subject_id in chains:
            node_id = chains[violation.subject_id].nodes[0].node_id
        if node_id is None:
            # Both evaluators identify a chain segment by chain/rule and inclusive positions.
            for chain in plan.chains:
                prefix = f"{chain.chain_id}:{violation.rule_id}:"
                if not violation.subject_id.startswith(prefix):
                    continue
                positions = violation.subject_id[len(prefix):].removeprefix("virtual_anchor:").split("-")
                if len(positions) != 2 or not all(part.isdecimal() for part in positions):
                    continue
                start, end = map(int, positions)
                if 0 <= start <= end < len(chain.nodes):
                    target = end if (
                        field in {"width_warning", "thickness_warning", "temperature_warning"}
                        or violation.scope is RuleScope.EDGE
                    ) else start
                    node_id = chain.nodes[target].node_id
                    break
        if node_id is None and violation.scope is RuleScope.PLAN and violation.subject_id == "plan":
            # Global findings remain in the full response; do not blame an arbitrary order.
            continue
        if node_id is None:
            raise MonthSchedulingMappingError(
                f"warning subject is not present in the release plan: {violation.subject_id}"
            )
        warnings[node_id][field].append(violation.message)
    return warnings


def build_month_solve_rows(
    plan: SchedulePlan,
    violations: Sequence[RuleViolation],
) -> tuple[dict, ...]:
    """Convert one release plan in its existing order; never sort or reevaluate it."""

    if not isinstance(plan, SchedulePlan):
        raise MonthSchedulingMappingError("rows require a SchedulePlan")
    violations = freeze_tuple(violations, RuleViolation, "violations")
    warnings = _warning_targets(plan, violations)
    chain_counts = {}
    rows = []
    for chain in plan.chains:
        chain_counts[chain.assigned_period] = chain_counts.get(chain.assigned_period, 0) + 1
        chain_sequence = chain_counts[chain.assigned_period]
        for node_sequence, node in enumerate(chain.nodes, start=1):
            node_warnings = warnings[node.node_id]
            rows.append(
                {
                    "node_id": node.node_id,
                    "source_order_id": node.source_order_id,
                    "source_resource_id": node.source_resource_id,
                    "source_period": node.source_period,
                    "assigned_period": chain.assigned_period,
                    "chain_id": chain.chain_id,
                    "chain_sequence": chain_sequence,
                    "node_sequence": node_sequence,
                    "weight": node.weight,
                    "width": node.width,
                    "thickness": node.thickness,
                    "min_temperature": node.min_temperature,
                    "max_temperature": node.max_temperature,
                    "grade": node.grade,
                    "grade_class": node.rule_attributes.get("grade_class"),
                    "hot_roll_grade": node.rule_attributes.get("hot_roll_grade"),
                    "soft_hard_class": node.rule_attributes.get("soft_hard_class"),
                    "material_role": node.material_role.value,
                    "split_lineage": _lineage_data(node.split_lineage),
                    "virtual_lineage": _lineage_data(node.virtual_lineage),
                    **{
                        name: "；".join(messages) or None
                        for name, messages in node_warnings.items()
                    },
                }
            )
    return tuple(rows)


def _audit_data(result) -> dict:
    core, public = result.core_audit, result.audit_report
    return {
        "passed": core.passed and public.passed,
        "integrity_passed": core.integrity_passed and public.passed,
        "writeback_blocking_violation_count": core.writeback_blocking_violation_count,
        "core": {part.name: _enum(getattr(core, part.name)) for part in fields(core)},
        "result": {part.name: _enum(getattr(public, part.name)) for part in fields(public)},
    }


def _manifest_data(manifest) -> dict:
    return {
        part.name: _enum(getattr(manifest, part.name))
        for part in fields(manifest)
    }


def _preparation_data(report) -> dict:
    return {
        "grade_dictionary_fingerprint": report.grade_dictionary_fingerprint,
        "input_order_count": report.input_order_count,
        "matched_order_count": report.matched_order_count,
        "missing_order_count": report.missing_order_count,
        "missing_grades": [
            {part.name: getattr(item, part.name) for part in fields(item)}
            for item in report.missing_grades
        ],
        "report_fingerprint": report.report_fingerprint,
    }


def month_solve_response_data(
    request: MonthSolveRequest,
    bound: BoundSchedulingResult,
) -> dict:
    """Build one response from a bound result and its audited release only."""

    if not isinstance(request, MonthSolveRequest) or not isinstance(bound, BoundSchedulingResult):
        raise MonthSchedulingMappingError("response requires a parsed request and bound result")
    result = bound.result
    if (
        request.expected_active_version_id != bound.active_rule_set_version_id
        or request.typed_request_fingerprint != bound.task_input_fingerprint
        or request.task_input.request_id != result.request_id
        or request.task_input.contract_version != result.contract_version
    ):
        raise MonthSchedulingMappingError("bound result does not match the monthly request")
    release = result.release
    publishable = (
        release is not None and result.status.publishable
        and result.core_audit.writeback_eligible and result.audit_report.passed
    )
    if release is not None and not publishable:
        raise MonthSchedulingMappingError("release does not satisfy audited writeback eligibility")
    if release is None:
        quality, metrics, violations, rows = [], {}, [], []
    else:
        if len(bound.quality_spec) != len(release.evaluation.quality_key):
            raise MonthSchedulingMappingError("quality values do not match the bound quality specification")
        quality = [
            {
                "criterion_id": criterion.criterion_id,
                "metric_key": criterion.metric_key,
                "value": value,
            }
            for criterion, value in zip(bound.quality_spec, release.evaluation.quality_key)
        ]
        metrics = dict(release.evaluation.metrics)
        violations = [_violation_data(item) for item in release.evaluation.violations]
        rows = list(build_month_solve_rows(release.plan, release.evaluation.violations))
    trusted_evaluation = (
        release.evaluation if release is not None else
        result.diagnostic_candidate.search_evaluation
        if result.core_audit.integrity_passed and result.diagnostic_candidate is not None else None
    )
    return {
        "contract_version": request.task_input.contract_version,
        "request_id": result.request_id,
        "status": result.status.value,
        "stop_reason": result.stop_reason.value,
        "publishable": publishable,
        "business_rules_satisfied": None if trusted_evaluation is None else not trusted_evaluation.violations,
        "active_rule_set_version_id": bound.active_rule_set_version_id,
        "rule_set_version": bound.rule_set_version,
        "rule_set_fingerprint": bound.rule_set_fingerprint,
        "grade_dictionary_fingerprint": bound.grade_dictionary_fingerprint,
        "raw_request_fingerprint": request.raw_request_fingerprint,
        "typed_request_fingerprint": request.typed_request_fingerprint,
        "request_fingerprint": bound.request_fingerprint,
        "binding_fingerprint": bound.binding_fingerprint,
        "result_fingerprint": result.result_fingerprint,
        "bound_result_fingerprint": bound.bound_result_fingerprint,
        "preparation_report": _preparation_data(bound.preparation_report),
        "quality": quality,
        "metrics": metrics,
        "issues": [_issue_data(issue) for issue in result.issues],
        "audit_summary": _audit_data(result),
        "run_manifest": _manifest_data(result.run_manifest),
        "violations": violations,
        "rows": rows,
        **({"diagnostic_violations": [_violation_data(item) for item in result.diagnostic_candidate.search_evaluation.violations]}
           if request.task_input.contract_version == MONTH_EARLIEST_START_CONTRACT_VERSION
           and not publishable and result.diagnostic_candidate is not None else {}),
        **({"schedule_start_at": request.task_input.schedule_start_at,
            "delivery_report": bound.delivery_report}
           if request.task_input.schedule_start_at is not None else {}),
    }


def dumps_month_solve_response(request: MonthSolveRequest, bound: BoundSchedulingResult, *, date_configuration_path=None) -> str:
    data = month_solve_response_data(request, bound)
    if bound.delivery_report is not None and data["publishable"] and date_configuration_path is not None:
        from .month_plan_auxiliary import latest_dates_from_delivery
        data["latest_dates"] = latest_dates_from_delivery(bound, date_configuration_path)
    return dumps_exact_json(data)


def dumps_month_solve_error(
    code: str,
    message: str,
    *,
    request_id: str | None = None,
    expected_active_version_id: int | None = None,
    current_active_version_id: int | None = None,
    issues: Sequence[DiagnosticIssue] = (),
) -> str:
    require_text(code, "code")
    require_text(message, "message")
    require_text(request_id, "request_id", allow_none=True)
    for name, value in (
        ("expected_active_version_id", expected_active_version_id),
        ("current_active_version_id", current_active_version_id),
    ):
        if value is not None:
            require_int(value, name, minimum=1)
    issues = freeze_tuple(issues, DiagnosticIssue, "issues")
    return dumps_exact_json(
        {
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
                "expected_active_version_id": expected_active_version_id,
                "current_active_version_id": current_active_version_id,
                "issues": [_issue_data(issue) for issue in issues],
            }
        }
    )


__all__ = [
    "MONTH_SOLVE_CONTRACT_VERSION",
    "MonthSchedulingContractError",
    "MonthSchedulingMappingError",
    "MonthSolveRequest",
    "build_month_solve_rows",
    "dumps_month_solve_error",
    "dumps_month_solve_response",
    "loads_month_solve_request",
    "month_solve_response_data",
]
