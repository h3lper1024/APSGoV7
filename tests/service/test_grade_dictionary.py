from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from apsgo_scheduler.api.request import OrderInput
from apsgo_scheduler.core.contracts import fingerprint
from apsgo_scheduler.core.model import MaterialRole
from apsgo_v7_service.grade_dictionary import (
    GradeDictionaryEntry,
    GradeDictionarySnapshot,
    GradePreparationError,
    normalize_grade,
    prepare_orders_with_grade_dictionary,
)


def _entry(
    grade: str,
    soft_hard_class: str = "软钢",
    *,
    enabled: bool = True,
    source_rows: str = "2",
) -> GradeDictionaryEntry:
    return GradeDictionaryEntry(
        source_grade=grade,
        normalized_grade=normalize_grade(grade),
        soft_hard_class=soft_hard_class,
        roll_type="A",
        steel_classes="普通钢|IF钢",
        is_if_steel=False,
        enabled=enabled,
        source_file="gqga4.xlsx",
        source_row_count=1,
        source_rows=source_rows,
        remark="",
    )


def _snapshot(*entries: GradeDictionaryEntry) -> GradeDictionarySnapshot:
    return GradeDictionarySnapshot(" gqga4 ", entries)


def _order(
    index: int,
    grade: str,
    *,
    role: MaterialRole = MaterialRole.NORMAL_REAL,
    classification=None,
) -> OrderInput:
    return OrderInput(
        node_id=f"N{index}",
        source_order_id=f"O{index}",
        source_resource_id=f"R{index}",
        source_period="P0",
        weight=Decimal("10.25"),
        width=Decimal("1000"),
        thickness=Decimal("1.0"),
        min_temperature=Decimal("700"),
        max_temperature=Decimal("730"),
        grade=grade,
        material_role=role,
        rule_attributes={
            "hot_roll_grade": grade,
            "soft_hard_class": classification,
        },
    )


def test_snapshot_normalizes_identity_sorts_entries_and_has_stable_fingerprint():
    lower = _entry("St04D+Z")
    upper = _entry("St280D+Z", "硬钢")

    first = _snapshot(upper, lower)
    second = _snapshot(lower, upper)

    assert first.product_line_code == "GQGA4"
    assert tuple(item.normalized_grade for item in first.entries) == ("ST04D+Z", "ST280D+Z")
    assert first.dictionary_fingerprint == second.dictionary_fingerprint
    assert first.find_enabled(" st04d+z ") == lower
    with pytest.raises(FrozenInstanceError):
        first.product_line_code = "OTHER"


def test_snapshot_fingerprint_covers_disabled_and_source_fields():
    original = _snapshot(_entry("DC01"))
    changes = (
        {"source_grade": "dc01", "normalized_grade": "DC01"},
        {"soft_hard_class": "硬钢"},
        {"roll_type": "B"},
        {"steel_classes": "IF钢"},
        {"is_if_steel": True},
        {"enabled": False},
        {"source_file": "other.xlsx"},
        {"source_row_count": 2},
        {"source_rows": "3"},
        {"remark": "changed"},
    )

    for change in changes:
        changed = replace(_entry("DC01"), **change)
        assert _snapshot(changed).dictionary_fingerprint != original.dictionary_fingerprint


@pytest.mark.parametrize(
    "entry,error",
    (
        (lambda: _entry(" "), "source_grade"),
        (
            lambda: replace(_entry("DC01"), normalized_grade="OTHER"),
            "normalized_grade",
        ),
        (lambda: _entry("DC01", "中间态"), "soft_hard_class"),
        (lambda: replace(_entry("DC01"), roll_type=""), "roll_type"),
        (lambda: replace(_entry("DC01"), enabled=1), "enabled"),
        (lambda: replace(_entry("DC01"), source_row_count=-1), "source_row_count"),
    ),
)
def test_entry_rejects_invalid_persisted_values(entry, error):
    with pytest.raises(ValueError, match=error):
        entry()


def test_snapshot_rejects_empty_or_duplicate_normalized_grades():
    with pytest.raises(ValueError, match="must not be empty"):
        _snapshot()
    with pytest.raises(ValueError, match="duplicate"):
        _snapshot(_entry("DC01"), _entry("dc01"))


def test_preparation_handles_normal_transition_disabled_and_unknown_grades():
    snapshot = _snapshot(
        _entry("St04D+Z"),
        _entry("St280D+Z", "硬钢"),
        _entry("DISABLED", enabled=False),
    )
    orders = (
        _order(0, " st04d+z "),
        _order(
            1,
            "st280d+z",
            role=MaterialRole.ACTUAL_TRANSITION,
            classification=" 硬钢 ",
        ),
        _order(2, "UNKNOWN"),
        _order(3, "disabled"),
    )
    before = fingerprint(orders)

    result = prepare_orders_with_grade_dictionary(orders, snapshot)

    assert tuple(item.rule_attributes["soft_hard_class"] for item in result.orders) == (
        "软钢",
        "硬钢",
        None,
        None,
    )
    assert result.orders[1].material_role is MaterialRole.ACTUAL_TRANSITION
    assert result.report.input_order_count == 4
    assert result.report.matched_order_count == 2
    assert result.report.missing_order_count == 2
    assert tuple(
        (item.normalized_grade, item.source_order_id, item.source_grade)
        for item in result.report.missing_grades
    ) == (("DISABLED", "O3", "disabled"), ("UNKNOWN", "O2", "UNKNOWN"))
    assert result.report.grade_dictionary_fingerprint == snapshot.dictionary_fingerprint
    assert result.report.report_fingerprint
    assert fingerprint(orders) == before
    assert fingerprint(result.orders) != before


def test_preparation_report_is_stable_for_the_same_order_sequence():
    snapshot = _snapshot(_entry("DC01"))
    orders = (_order(0, " x "), _order(1, "DC01"), _order(2, "A"), _order(3, "X"))

    first = prepare_orders_with_grade_dictionary(orders, snapshot)
    second = prepare_orders_with_grade_dictionary(orders, snapshot)

    assert first == second
    assert first.report.report_fingerprint == second.report.report_fingerprint
    assert tuple(
        (item.normalized_grade, item.source_order_id, item.source_grade)
        for item in first.report.missing_grades
    ) == (("A", "O2", "A"), ("X", "O0", " x "), ("X", "O3", "X"))


def test_preparation_aggregates_conflicts_and_exposes_no_partial_result():
    snapshot = _snapshot(_entry("DC01"), _entry("DC02", "硬钢"))
    orders = (
        _order(0, "DC01", classification="硬钢"),
        _order(1, "UNKNOWN", classification="软钢"),
        _order(2, "DC02", classification=7),
        _order(3, "  "),
        _order(4, "DC01", role=MaterialRole.GENERATED_VIRTUAL),
    )

    with pytest.raises(GradePreparationError) as caught:
        prepare_orders_with_grade_dictionary(orders, snapshot)

    assert tuple(issue.code for issue in caught.value.issues) == (
        "soft_hard_class_conflict",
        "soft_hard_class_conflict",
        "invalid_text_attribute",
        "invalid_grade",
        "unsupported_input_material_role",
    )
    assert tuple(issue.subject_id for issue in caught.value.issues) == tuple(
        f"O{index}" for index in range(5)
    )


def test_preparation_rejects_wrong_container_or_snapshot_type():
    snapshot = _snapshot(_entry("DC01"))
    with pytest.raises(ValueError, match="ordered sequence"):
        prepare_orders_with_grade_dictionary(iter((_order(0, "DC01"),)), snapshot)
    with pytest.raises(ValueError, match="snapshot"):
        prepare_orders_with_grade_dictionary((_order(0, "DC01"),), object())
