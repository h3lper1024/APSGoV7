"""Immutable grade-dictionary snapshots and order preparation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from apsgo_scheduler.api.request import OrderInput
from apsgo_scheduler.core.contracts import (
    DiagnosticIssue,
    DiagnosticPhase,
    DiagnosticSeverity,
    fingerprint,
    freeze_tuple,
    require_int,
    require_text,
)
from apsgo_scheduler.core.model import MaterialRole

SOFT_HARD_CLASSES = frozenset(("软钢", "硬钢"))


def normalize_grade(value: str) -> str:
    """Return the only grade lookup form used by V7."""

    if not isinstance(value, str):
        raise ValueError("grade must be text")
    return value.strip().upper()


@dataclass(frozen=True, slots=True)
class GradeDictionaryEntry:
    source_grade: str
    normalized_grade: str
    soft_hard_class: str
    roll_type: str
    steel_classes: str
    is_if_steel: bool
    enabled: bool
    source_file: str
    source_row_count: int
    source_rows: str
    remark: str

    def __post_init__(self):
        require_text(self.source_grade, "source_grade")
        require_text(self.normalized_grade, "normalized_grade")
        if self.normalized_grade != normalize_grade(self.source_grade):
            raise ValueError("normalized_grade does not match source_grade")
        if self.soft_hard_class not in SOFT_HARD_CLASSES:
            raise ValueError("soft_hard_class must be 软钢 or 硬钢")
        require_text(self.roll_type, "roll_type")
        for name in ("steel_classes", "source_file", "source_rows", "remark"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be text")
        for name in ("is_if_steel", "enabled"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be bool")
        require_int(self.source_row_count, "source_row_count")


@dataclass(frozen=True, slots=True)
class GradeDictionarySnapshot:
    product_line_code: str
    entries: tuple[GradeDictionaryEntry, ...]
    dictionary_fingerprint: str = field(init=False)
    _enabled_by_grade: Mapping[str, GradeDictionaryEntry] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self):
        require_text(self.product_line_code, "product_line_code")
        product_line_code = self.product_line_code.strip().upper()
        entries = tuple(
            sorted(
                freeze_tuple(self.entries, GradeDictionaryEntry, "entries"),
                key=lambda item: item.normalized_grade,
            )
        )
        if not entries:
            raise ValueError("grade dictionary must not be empty")
        if len({entry.normalized_grade for entry in entries}) != len(entries):
            raise ValueError("grade dictionary contains duplicate normalized grades")
        object.__setattr__(self, "product_line_code", product_line_code)
        object.__setattr__(self, "entries", entries)
        object.__setattr__(
            self,
            "_enabled_by_grade",
            MappingProxyType(
                {entry.normalized_grade: entry for entry in entries if entry.enabled}
            ),
        )
        object.__setattr__(
            self,
            "dictionary_fingerprint",
            fingerprint(
                {
                    "product_line_code": product_line_code,
                    "entries": entries,
                }
            ),
        )

    def find_enabled(self, grade: str) -> GradeDictionaryEntry | None:
        return self._enabled_by_grade.get(normalize_grade(grade))


@dataclass(frozen=True, slots=True)
class MissingGrade:
    normalized_grade: str
    source_order_id: str
    source_grade: str

    def __post_init__(self):
        require_text(self.normalized_grade, "normalized_grade")
        require_text(self.source_order_id, "source_order_id")
        require_text(self.source_grade, "source_grade")


@dataclass(frozen=True, slots=True)
class GradePreparationReport:
    grade_dictionary_fingerprint: str
    input_order_count: int
    matched_order_count: int
    missing_order_count: int
    missing_grades: tuple[MissingGrade, ...]
    report_fingerprint: str = field(init=False)

    def __post_init__(self):
        require_text(self.grade_dictionary_fingerprint, "grade_dictionary_fingerprint")
        for name in ("input_order_count", "matched_order_count", "missing_order_count"):
            require_int(getattr(self, name), name)
        missing_grades = freeze_tuple(self.missing_grades, MissingGrade, "missing_grades")
        if self.matched_order_count + self.missing_order_count != self.input_order_count:
            raise ValueError("grade preparation counts do not add up")
        if len(missing_grades) != self.missing_order_count:
            raise ValueError("missing grade details do not match missing_order_count")
        ordered_missing = tuple(
            sorted(
                missing_grades,
                key=lambda item: (
                    item.normalized_grade,
                    item.source_order_id,
                    item.source_grade,
                ),
            )
        )
        if ordered_missing != missing_grades:
            raise ValueError("missing grade details must be sorted")
        object.__setattr__(self, "missing_grades", missing_grades)
        object.__setattr__(
            self,
            "report_fingerprint",
            fingerprint(
                {
                    "grade_dictionary_fingerprint": self.grade_dictionary_fingerprint,
                    "input_order_count": self.input_order_count,
                    "matched_order_count": self.matched_order_count,
                    "missing_order_count": self.missing_order_count,
                    "missing_grades": missing_grades,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class PreparedOrders:
    orders: tuple[OrderInput, ...]
    report: GradePreparationReport

    def __post_init__(self):
        object.__setattr__(self, "orders", freeze_tuple(self.orders, OrderInput, "orders"))
        if not isinstance(self.report, GradePreparationReport):
            raise ValueError("report must be GradePreparationReport")


class GradePreparationError(ValueError):
    def __init__(self, issues: tuple[DiagnosticIssue, ...]):
        self.issues = freeze_tuple(issues, DiagnosticIssue, "issues")
        if not self.issues:
            raise ValueError("grade preparation error requires at least one issue")
        super().__init__("；".join(issue.message for issue in self.issues))


def prepare_orders_with_grade_dictionary(
    orders: Sequence[OrderInput],
    snapshot: GradeDictionarySnapshot,
) -> PreparedOrders:
    """Attach one frozen dictionary classification without mutating typed orders."""

    if not isinstance(snapshot, GradeDictionarySnapshot):
        raise ValueError("snapshot must be GradeDictionarySnapshot")
    orders = freeze_tuple(orders, OrderInput, "orders")
    prepared = []
    missing = []
    issues = []

    def issue(code: str, path: str, subject_id: str, message: str) -> None:
        issues.append(
            DiagnosticIssue(
                code,
                DiagnosticPhase.INPUT_NORMALIZATION,
                path,
                subject_id,
                message,
                DiagnosticSeverity.ERROR,
            )
        )

    for index, order in enumerate(orders):
        path = f"orders[{index}]"
        if order.material_role is MaterialRole.GENERATED_VIRTUAL:
            issue(
                "unsupported_input_material_role",
                f"{path}.material_role",
                order.source_order_id,
                "原始订单不能使用生成型虚拟材料角色。",
            )
            continue
        normalized_grade = normalize_grade(order.grade)
        if not normalized_grade:
            issue(
                "invalid_grade",
                f"{path}.grade",
                order.source_order_id,
                "订单牌号不能为空或只包含空白字符。",
            )
            continue
        entry = snapshot.find_enabled(normalized_grade)
        derived_class = entry.soft_hard_class if entry is not None else None
        existing = order.rule_attributes.get("soft_hard_class")
        if existing is not None and not isinstance(existing, str):
            issue(
                "invalid_text_attribute",
                f"{path}.rule_attributes.soft_hard_class",
                order.source_order_id,
                "已有软硬钢分类必须是文本或空值。",
            )
            continue
        existing = existing.strip() if isinstance(existing, str) else ""
        if existing and existing != derived_class:
            issue(
                "soft_hard_class_conflict",
                f"{path}.rule_attributes.soft_hard_class",
                order.source_order_id,
                "已有软硬钢分类与当前字典快照不一致。",
            )
            continue
        attributes = dict(order.rule_attributes)
        attributes["soft_hard_class"] = derived_class
        prepared.append(replace(order, rule_attributes=attributes))
        if entry is None:
            missing.append(MissingGrade(normalized_grade, order.source_order_id, order.grade))

    if issues:
        raise GradePreparationError(tuple(issues))
    missing = tuple(
        sorted(
            missing,
            key=lambda item: (item.normalized_grade, item.source_order_id, item.source_grade),
        )
    )
    report = GradePreparationReport(
        grade_dictionary_fingerprint=snapshot.dictionary_fingerprint,
        input_order_count=len(orders),
        matched_order_count=len(orders) - len(missing),
        missing_order_count=len(missing),
        missing_grades=missing,
    )
    return PreparedOrders(tuple(prepared), report)


__all__ = [
    "GradeDictionaryEntry",
    "GradeDictionarySnapshot",
    "GradePreparationError",
    "GradePreparationReport",
    "MissingGrade",
    "PreparedOrders",
    "normalize_grade",
    "prepare_orders_with_grade_dictionary",
]
