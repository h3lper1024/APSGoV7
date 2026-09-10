"""GQGA4 month-plan auxiliary endpoints that do not touch solver state."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_scheduler.core.contracts import (
    DiagnosticIssue,
    DiagnosticPhase,
    DiagnosticSeverity,
)

from .month_scheduling import (
    MonthSchedulingContractError,
    _array,
    _decimal,
    _exact_object,
    _load_json,
    _positive_decimal,
    _positive_int,
    _text,
)

MONTH_PRESET_CONTRACT_VERSION = "month-preset-v1"
MONTH_LATEST_DATES_CONTRACT_VERSION = "month-latest-dates-v1"

_ROOT_FIELDS = frozenset(("contract_version", "request_id", "start_month", "orders"))
_ORDER_FIELDS = frozenset(("source_order_id", "due_date"))
_LATEST_ROOT_FIELDS = frozenset(
    (
        "contract_version",
        "request_id",
        "product_line_code",
        "current_process",
        "calc_date",
        "solve_reference",
        "periods",
        "rows",
    )
)
_SOLVE_REFERENCE_FIELDS = frozenset(("solve_request_id", "bound_result_fingerprint"))
_LATEST_PERIOD_FIELDS = frozenset(("period_id", "sequence"))
_LATEST_ROW_FIELDS = frozenset(
    (
        "node_id",
        "source_order_id",
        "assigned_period",
        "chain_id",
        "chain_sequence",
        "node_sequence",
        "weight_t",
        "width_mm",
        "thickness_mm",
        "furnace_speed_mpm",
        "process_speed_mpm",
        "production_rate_tph",
        "production_rate_source",
    )
)
_DATE_CONFIG_FIELDS = frozenset(("time_zone", "lead_days", "product_line_process"))
_LEAD_DAY_FIELDS = frozenset(
    ("casting", "grinding", "hot_rolling", "tempering", "pickling_rolling")
)
_PROCESS_ORDER = (
    "casting",
    "grinding",
    "hot_rolling",
    "tempering",
    "pickling_rolling",
    "coating",
)
_CURRENT_PROCESSES = frozenset(
    ("continuous_annealing", "galvanizing", "pickling_rolling")
)
_MONTH_PATTERN = re.compile(r"^(\d{4})-(\d{2})$")
_DATE_PATTERN = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class MonthPlanDateConfigurationError(RuntimeError):
    """The standalone month-plan date configuration cannot be used."""


@dataclass(frozen=True, slots=True)
class MonthPlanDateConfiguration:
    time_zone: str
    lead_days: dict[str, Decimal]
    product_line_process: dict[str, str]


@dataclass(frozen=True, slots=True)
class LatestDatePeriodInput:
    period_id: str
    sequence: int


@dataclass(frozen=True, slots=True)
class LatestDateRowInput:
    node_id: str
    source_order_id: str | None
    assigned_period: str
    chain_id: str
    chain_sequence: int
    node_sequence: int
    weight_t: Decimal
    width_mm: Decimal | None
    thickness_mm: Decimal | None
    furnace_speed_mpm: Decimal | None
    process_speed_mpm: Decimal | None
    production_rate_tph: Decimal | None
    production_rate_source: str | None


@dataclass(frozen=True, slots=True)
class CalculateLatestDatesRequest:
    request_id: str
    product_line_code: str
    current_process: str
    calc_date: date
    solve_request_id: str
    bound_result_fingerprint: str
    periods: tuple[LatestDatePeriodInput, ...]
    rows: tuple[LatestDateRowInput, ...]


@dataclass(frozen=True, slots=True)
class PresetOrderInput:
    source_order_id: str
    due_date: date | None
    warning_code: str | None


@dataclass(frozen=True, slots=True)
class PresetBigRollsRequest:
    request_id: str
    start_month: date
    orders: tuple[PresetOrderInput, ...]


def _issue(
    code: str,
    field_path: str,
    message: str,
    subject_id: str | None = None,
    *,
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR,
) -> DiagnosticIssue:
    return DiagnosticIssue(
        code,
        DiagnosticPhase.REQUEST_VALIDATION,
        field_path,
        subject_id,
        message,
        severity,
    )


def _raise_contract(
    code: str,
    field_path: str,
    message: str,
    subject_id: str | None = None,
) -> None:
    raise MonthSchedulingContractError((_issue(code, field_path, message, subject_id),))


def _month(value: str, path: str) -> date:
    match = _MONTH_PATTERN.fullmatch(value)
    if match is None:
        _raise_contract("invalid_field_value", path, "起始月必须使用 YYYY-MM 格式。")
    year, month = (int(part) for part in match.groups())
    try:
        return date(year, month, 1)
    except ValueError:
        _raise_contract("invalid_field_value", path, "起始月必须是有效年月。")


def _due_date(value, path: str, source_order_id: str) -> tuple[date | None, str | None]:
    if value is None:
        return None, "due_date_missing"
    if not isinstance(value, str):
        _raise_contract(
            "invalid_field_type",
            path,
            "交期必须是 YYYY-MM-DD 文本或 null。",
            source_order_id,
        )
    text = value.strip()
    match = _DATE_PATTERN.fullmatch(text)
    if match is None:
        return None, "due_date_invalid"
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day), None
    except ValueError:
        return None, "due_date_invalid"


def _order(value, index: int) -> PresetOrderInput:
    path = f"orders[{index}]"
    item = _exact_object(value, path, _ORDER_FIELDS)
    source_order_id = _text(item["source_order_id"], f"{path}.source_order_id")
    due_date, warning_code = _due_date(
        item["due_date"], f"{path}.due_date", source_order_id
    )
    return PresetOrderInput(source_order_id, due_date, warning_code)


def loads_preset_big_rolls_request(payload: str | bytes | bytearray) -> PresetBigRollsRequest:
    raw = _exact_object(_load_json(payload), "", _ROOT_FIELDS)
    if raw["contract_version"] != MONTH_PRESET_CONTRACT_VERSION:
        _raise_contract(
            "unsupported_contract_version",
            "contract_version",
            f"contract_version 必须为 {MONTH_PRESET_CONTRACT_VERSION}。",
        )
    request_id = _text(raw["request_id"], "request_id")
    try:
        request_id = str(UUID(request_id))
    except ValueError:
        _raise_contract("invalid_field_value", "request_id", "request_id 必须是 UUID 文本。")
    start_month = _month(_text(raw["start_month"], "start_month"), "start_month")
    orders = tuple(_order(item, index) for index, item in enumerate(_array(raw["orders"], "orders")))
    seen = set()
    for index, order in enumerate(orders):
        if order.source_order_id in seen:
            _raise_contract(
                "duplicate_identity",
                f"orders[{index}].source_order_id",
                "来源订单编号与前面的记录重复。",
                order.source_order_id,
            )
        seen.add(order.source_order_id)
    return PresetBigRollsRequest(request_id, start_month, orders)


def _add_months(month: date, offset: int) -> date:
    month_index = month.year * 12 + month.month - 1 + offset
    return date(month_index // 12, month_index % 12 + 1, month.day)


def _period_range(start_month: date, big_roll_number: int) -> tuple[date, date]:
    month = _add_months(start_month, (big_roll_number - 1) // 6)
    segment = (big_roll_number - 1) % 6
    start_day = segment * 5 + 1
    last_day = calendar.monthrange(month.year, month.month)[1]
    end_day = last_day if segment == 5 else min(start_day + 4, last_day)
    return date(month.year, month.month, start_day), date(month.year, month.month, end_day)


def _big_roll_number(order: PresetOrderInput, start_month: date) -> tuple[int, str | None]:
    if order.warning_code is not None:
        return 1, order.warning_code
    assert order.due_date is not None
    month_offset = (order.due_date.year - start_month.year) * 12 + order.due_date.month - start_month.month
    if month_offset < 0:
        return 1, "due_before_start_month"
    return month_offset * 6 + min((order.due_date.day - 1) // 5, 5) + 1, None


def _warning_message(code: str) -> str:
    return {
        "due_date_missing": "交期为空，按兼容口径归入第一个大辊。",
        "due_date_invalid": "交期无法解析，按兼容口径归入第一个大辊。",
        "due_before_start_month": "交期早于起始月，按兼容口径归入第一个大辊。",
    }[code]


def preset_big_rolls(request: PresetBigRollsRequest) -> dict:
    if not isinstance(request, PresetBigRollsRequest):
        raise ValueError("request must be PresetBigRollsRequest")
    assignments = []
    warnings = []
    used_roll_numbers = set()
    for index, order in enumerate(request.orders, start=1):
        big_roll_number, warning_code = _big_roll_number(order, request.start_month)
        used_roll_numbers.add(big_roll_number)
        assignments.append(
            {
                "source_order_id": order.source_order_id,
                "source_period": f"BR_{big_roll_number:08d}",
                "preset_sequence": index,
            }
        )
        if warning_code is not None:
            warnings.append(
                _issue(
                    warning_code,
                    f"orders[{index - 1}].due_date",
                    _warning_message(warning_code),
                    order.source_order_id,
                    severity=DiagnosticSeverity.WARNING,
                )
            )
    periods = []
    for sequence, big_roll_number in enumerate(sorted(used_roll_numbers)):
        start_date, end_date = _period_range(request.start_month, big_roll_number)
        periods.append(
            {
                "period_id": f"BR_{big_roll_number:08d}",
                "sequence": sequence,
                "big_roll_number": big_roll_number,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            }
        )
    return {
        "contract_version": MONTH_PRESET_CONTRACT_VERSION,
        "request_id": request.request_id,
        "status": "success",
        "start_month": request.start_month.isoformat()[:7],
        "periods": periods,
        "assignments": assignments,
        "warnings": [
            {
                "code": warning.code,
                "field_path": warning.field_path,
                "subject_id": warning.subject_id,
                "message": warning.message,
            }
            for warning in warnings
        ],
    }


def dumps_preset_big_rolls_response(request: PresetBigRollsRequest) -> str:
    return dumps_exact_json(preset_big_rolls(request))


def _strict_date(value, path: str) -> date:
    text = _text(value, path)
    match = _DATE_PATTERN.fullmatch(text)
    if match is None:
        _raise_contract("invalid_field_value", path, "日期必须使用 YYYY-MM-DD 格式。")
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        _raise_contract("invalid_field_value", path, "日期必须是有效公历日期。")


def _latest_period(value, index: int) -> LatestDatePeriodInput:
    path = f"periods[{index}]"
    item = _exact_object(value, path, _LATEST_PERIOD_FIELDS)
    sequence = item["sequence"]
    if type(sequence) is not int or sequence < 0:
        _raise_contract("invalid_field_type", f"{path}.sequence", "字段必须是非负整数。")
    return LatestDatePeriodInput(_text(item["period_id"], f"{path}.period_id"), sequence)


def _latest_row(value, index: int) -> LatestDateRowInput:
    path = f"rows[{index}]"
    item = _exact_object(value, path, _LATEST_ROW_FIELDS)
    return LatestDateRowInput(
        node_id=_text(item["node_id"], f"{path}.node_id"),
        source_order_id=_text(
            item["source_order_id"], f"{path}.source_order_id", optional=True
        ),
        assigned_period=_text(item["assigned_period"], f"{path}.assigned_period"),
        chain_id=_text(item["chain_id"], f"{path}.chain_id"),
        chain_sequence=_positive_int(item["chain_sequence"], f"{path}.chain_sequence"),
        node_sequence=_positive_int(item["node_sequence"], f"{path}.node_sequence"),
        weight_t=_positive_decimal(item["weight_t"], f"{path}.weight_t"),
        width_mm=_decimal(item["width_mm"], f"{path}.width_mm", optional=True),
        thickness_mm=_decimal(item["thickness_mm"], f"{path}.thickness_mm", optional=True),
        furnace_speed_mpm=_decimal(
            item["furnace_speed_mpm"], f"{path}.furnace_speed_mpm", optional=True
        ),
        process_speed_mpm=_decimal(
            item["process_speed_mpm"], f"{path}.process_speed_mpm", optional=True
        ),
        production_rate_tph=_decimal(
            item["production_rate_tph"], f"{path}.production_rate_tph", optional=True
        ),
        production_rate_source=_text(
            item["production_rate_source"],
            f"{path}.production_rate_source",
            optional=True,
        ),
    )


def loads_calculate_latest_dates_request(
    payload: str | bytes | bytearray,
) -> CalculateLatestDatesRequest:
    raw = _exact_object(_load_json(payload), "", _LATEST_ROOT_FIELDS)
    if raw["contract_version"] != MONTH_LATEST_DATES_CONTRACT_VERSION:
        _raise_contract(
            "unsupported_contract_version",
            "contract_version",
            f"contract_version 必须为 {MONTH_LATEST_DATES_CONTRACT_VERSION}。",
        )
    request_id = _text(raw["request_id"], "request_id")
    try:
        request_id = str(UUID(request_id))
    except ValueError:
        _raise_contract("invalid_field_value", "request_id", "request_id 必须是 UUID 文本。")
    reference = _exact_object(
        raw["solve_reference"], "solve_reference", _SOLVE_REFERENCE_FIELDS
    )
    solve_request_id = _text(reference["solve_request_id"], "solve_reference.solve_request_id")
    try:
        solve_request_id = str(UUID(solve_request_id))
    except ValueError:
        _raise_contract(
            "invalid_field_value",
            "solve_reference.solve_request_id",
            "solve_request_id 必须是 UUID 文本。",
        )
    current_process = _text(raw["current_process"], "current_process")
    if current_process not in _CURRENT_PROCESSES:
        _raise_contract(
            "invalid_field_value",
            "current_process",
            "current_process 必须是 continuous_annealing、galvanizing 或 pickling_rolling。",
        )
    periods = tuple(
        _latest_period(item, index)
        for index, item in enumerate(_array(raw["periods"], "periods"))
    )
    rows = tuple(
        _latest_row(item, index)
        for index, item in enumerate(_array(raw["rows"], "rows"))
    )
    period_ids = set()
    period_sequences = set()
    for index, period in enumerate(periods):
        if period.period_id in period_ids:
            _raise_contract(
                "duplicate_identity",
                f"periods[{index}].period_id",
                "计划期编号与前面的记录重复。",
                period.period_id,
            )
        if period.sequence in period_sequences:
            _raise_contract(
                "duplicate_identity",
                f"periods[{index}].sequence",
                "计划期序号与前面的记录重复。",
                period.period_id,
            )
        period_ids.add(period.period_id)
        period_sequences.add(period.sequence)
    if period_sequences != set(range(len(periods))):
        _raise_contract(
            "invalid_period_sequence",
            "periods",
            "计划期序号必须从零开始连续，且不能重复。",
        )
    node_ids = set()
    chain_keys = set()
    chain_identities = {}
    current_chain = None
    expected_node_sequence = 0
    for index, row in enumerate(rows):
        if row.node_id in node_ids:
            _raise_contract(
                "duplicate_node_id",
                f"rows[{index}].node_id",
                "节点编号与前面的记录重复。",
                row.node_id,
            )
        node_ids.add(row.node_id)
        if row.assigned_period not in period_ids:
            _raise_contract(
                "unknown_assigned_period",
                f"rows[{index}].assigned_period",
                "节点排入期不在本次计划期目录中。",
                row.node_id,
            )
        chain_key = (row.assigned_period, row.chain_id, row.chain_sequence)
        existing_identity = chain_identities.setdefault(row.chain_id, chain_key)
        if existing_identity != chain_key:
            _raise_contract(
                "inconsistent_chain_identity",
                f"rows[{index}].chain_id",
                "同一链身份不能对应不同计划期或链序号。",
                row.node_id,
            )
        if chain_key != current_chain:
            if chain_key in chain_keys:
                _raise_contract(
                    "non_contiguous_chain_nodes",
                    f"rows[{index}].chain_id",
                    "同一链的节点必须在求解结果中连续排列。",
                    row.node_id,
                )
            current_chain = chain_key
            expected_node_sequence = 1
            chain_keys.add(chain_key)
        else:
            expected_node_sequence += 1
        if row.node_sequence != expected_node_sequence:
            _raise_contract(
                "invalid_node_sequence",
                f"rows[{index}].node_sequence",
                "同一链的节点序号必须从 1 开始连续排列。",
                row.node_id,
            )
    return CalculateLatestDatesRequest(
        request_id=request_id,
        product_line_code=_text(raw["product_line_code"], "product_line_code"),
        current_process=current_process,
        calc_date=_strict_date(raw["calc_date"], "calc_date"),
        solve_request_id=solve_request_id,
        bound_result_fingerprint=_text(
            reference["bound_result_fingerprint"],
            "solve_reference.bound_result_fingerprint",
        ),
        periods=periods,
        rows=rows,
    )


def load_month_plan_date_configuration(path: str | Path) -> MonthPlanDateConfiguration:
    try:
        configuration_path = Path(path)
        payload = configuration_path.read_bytes()
        raw = _exact_object(_load_json(payload), "", _DATE_CONFIG_FIELDS)
        time_zone = _text(raw["time_zone"], "time_zone")
        ZoneInfo(time_zone)
        lead_raw = _exact_object(raw["lead_days"], "lead_days", _LEAD_DAY_FIELDS)
        lead_days = {}
        for name in _PROCESS_ORDER[:-1]:
            value = _decimal(lead_raw[name], f"lead_days.{name}")
            if value < 0:
                raise ValueError(f"lead_days.{name} must be nonnegative")
            lead_days[name] = value
        product_raw = raw["product_line_process"]
        if not isinstance(product_raw, dict) or not product_raw:
            raise ValueError("product_line_process must be a nonempty object")
        product_line_process = {}
        for product_line, process in product_raw.items():
            if not isinstance(product_line, str) or not product_line.strip():
                raise ValueError("product_line_process keys must be nonempty text")
            if not isinstance(process, str) or process.strip() not in _CURRENT_PROCESSES:
                raise ValueError("product_line_process values must be supported processes")
            product_line_process[product_line.strip()] = process.strip()
    except (OSError, MonthSchedulingContractError, ValueError, ZoneInfoNotFoundError) as error:
        raise MonthPlanDateConfigurationError(
            "月计划日期配置不可用或内容不合法。"
        ) from error
    return MonthPlanDateConfiguration(time_zone, lead_days, product_line_process)


def _positive_decimal_value(value: Decimal | None) -> bool:
    return value is not None and value > 0


def _date_warning(code: str, field_path: str, node_id: str, message: str) -> dict:
    issue = _issue(
        code,
        field_path,
        message,
        node_id,
        severity=DiagnosticSeverity.WARNING,
    )
    return {
        "code": issue.code,
        "field_path": issue.field_path,
        "subject_id": issue.subject_id,
        "message": issue.message,
    }


def _row_duration_hours(
    row: LatestDateRowInput,
    index: int,
    current_process: str,
) -> tuple[float, dict | None]:
    path = f"rows[{index}]"
    if current_process == "pickling_rolling":
        if row.furnace_speed_mpm is not None or row.process_speed_mpm is not None:
            _raise_contract(
                "invalid_speed_unit",
                f"{path}.furnace_speed_mpm",
                "酸轧当前工序只能使用吨产速，不能传米速度。",
                row.node_id,
            )
        if not _positive_decimal_value(row.production_rate_tph):
            _raise_contract(
                "invalid_production_rate",
                f"{path}.production_rate_tph",
                "酸轧吨产速必须为有效正数。",
                row.node_id,
            )
        if row.production_rate_source is None:
            _raise_contract(
                "missing_production_rate_source",
                f"{path}.production_rate_source",
                "酸轧吨产速必须带有效来源标识。",
                row.node_id,
            )
        return float(row.weight_t) / float(row.production_rate_tph), None
    if row.production_rate_tph is not None or row.production_rate_source is not None:
        _raise_contract(
            "invalid_speed_unit",
            f"{path}.production_rate_tph",
            "连退/镀锌当前工序只能使用米速度，不能传吨产速。",
            row.node_id,
        )
    speed = (
        row.furnace_speed_mpm
        if _positive_decimal_value(row.furnace_speed_mpm)
        else row.process_speed_mpm
    )
    if not _positive_decimal_value(speed):
        return 0.0, _date_warning(
            "zero_duration_missing_speed",
            f"{path}.furnace_speed_mpm",
            row.node_id,
            "炉区及备用速度均无有效正值，按原 C# 兼容逻辑计 0 小时。",
        )
    if not _positive_decimal_value(row.width_mm) or not _positive_decimal_value(row.thickness_mm):
        return 0.0, _date_warning(
            "zero_duration_invalid_dimensions",
            f"{path}.width_mm",
            row.node_id,
            "宽度或厚度不是有效正值，按原 C# 兼容逻辑计 0 小时。",
        )
    hours = (
        float(row.weight_t)
        * 1_000_000
        / (float(row.width_mm) * float(row.thickness_mm) * 7.85 * float(speed) * 60)
    )
    if hours != hours or hours in (float("inf"), float("-inf")):
        _raise_contract(
            "duration_overflow",
            f"{path}.weight_t",
            "当前工序工时计算溢出。",
            row.node_id,
        )
    return hours, None


def _local_iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _latest_dates(anchor_process: str, anchor_at: datetime, config: MonthPlanDateConfiguration) -> dict:
    anchor_index = _PROCESS_ORDER.index(anchor_process)
    latest = {name: None for name in _PROCESS_ORDER}
    latest[anchor_process] = anchor_at
    cursor = anchor_at
    for index in range(anchor_index - 1, -1, -1):
        process = _PROCESS_ORDER[index]
        cursor -= timedelta(days=float(config.lead_days[process]))
        latest[process] = cursor
    return {
        name: None if value is None else _local_iso(value)
        for name, value in latest.items()
    }


def calculate_latest_dates(
    request: CalculateLatestDatesRequest,
    configuration_path: str | Path,
) -> dict:
    if not isinstance(request, CalculateLatestDatesRequest):
        raise ValueError("request must be CalculateLatestDatesRequest")
    config = load_month_plan_date_configuration(configuration_path)
    configured_process = config.product_line_process.get(request.product_line_code)
    if configured_process is None:
        _raise_contract(
            "unknown_product_line",
            "product_line_code",
            "日期配置中没有该产线的当前工序映射。",
        )
    if configured_process != request.current_process:
        _raise_contract(
            "current_process_mismatch",
            "current_process",
            "当前工序与该产线的日期配置不一致。",
        )
    anchor_process = (
        "pickling_rolling" if request.current_process == "pickling_rolling" else "coating"
    )
    timezone = ZoneInfo(config.time_zone)
    start_at = datetime.combine(request.calc_date, time.min, tzinfo=timezone)
    cumulative = 0.0
    rows = []
    warnings = []
    for index, row in enumerate(request.rows):
        hours, warning = _row_duration_hours(row, index, request.current_process)
        if warning is not None:
            warnings.append(warning)
        process_start = start_at + timedelta(hours=cumulative)
        cumulative += hours
        process_latest = start_at + timedelta(hours=cumulative)
        rows.append(
            {
                "node_id": row.node_id,
                "duration_hours": Decimal(str(round(hours, 2))),
                "cumulative_hours": Decimal(str(cumulative)),
                "current_process_start_at": _local_iso(process_start),
                "current_process_latest_at": _local_iso(process_latest),
                "latest_dates": _latest_dates(anchor_process, process_latest, config),
            }
        )
    return {
        "contract_version": MONTH_LATEST_DATES_CONTRACT_VERSION,
        "request_id": request.request_id,
        "status": "success",
        "product_line_code": request.product_line_code,
        "current_process": request.current_process,
        "anchor_process": anchor_process,
        "time_zone": config.time_zone,
        "calc_date": request.calc_date.isoformat(),
        "solve_reference": {
            "solve_request_id": request.solve_request_id,
            "bound_result_fingerprint": request.bound_result_fingerprint,
        },
        "lead_days_snapshot": config.lead_days,
        "rows": rows,
        "warnings": warnings,
    }


def dumps_calculate_latest_dates_response(
    request: CalculateLatestDatesRequest,
    configuration_path: str | Path,
) -> str:
    return dumps_exact_json(calculate_latest_dates(request, configuration_path))


__all__ = [
    "MONTH_LATEST_DATES_CONTRACT_VERSION",
    "MONTH_PRESET_CONTRACT_VERSION",
    "CalculateLatestDatesRequest",
    "LatestDatePeriodInput",
    "LatestDateRowInput",
    "MonthPlanDateConfiguration",
    "MonthPlanDateConfigurationError",
    "PresetBigRollsRequest",
    "PresetOrderInput",
    "calculate_latest_dates",
    "dumps_calculate_latest_dates_response",
    "dumps_preset_big_rolls_response",
    "load_month_plan_date_configuration",
    "loads_calculate_latest_dates_request",
    "loads_preset_big_rolls_request",
    "preset_big_rolls",
]
