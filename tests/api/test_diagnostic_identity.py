"""Public diagnostics are the original core classes, not look-alike DTOs."""

from dataclasses import FrozenInstanceError, fields, replace

import pytest

from apsgo_scheduler.api import diagnostics
from apsgo_scheduler.core import contracts


def issue():
    return diagnostics.DiagnosticIssue(
        code="missing_weight",
        phase=diagnostics.DiagnosticPhase.INPUT_NORMALIZATION,
        field_path="orders[0].weight",
        subject_id="order-1",
        message="订单重量不能为空",
        severity=diagnostics.DiagnosticSeverity.ERROR,
    )


def test_api_reexports_exact_core_classes():
    assert diagnostics.__all__ == ["DiagnosticIssue", "DiagnosticPhase", "DiagnosticSeverity"]
    for name in diagnostics.__all__:
        assert getattr(diagnostics, name) is getattr(contracts, name)
    assert type(issue()) is contracts.DiagnosticIssue
    assert [field.name for field in fields(contracts.DiagnosticIssue)] == [
        "code",
        "phase",
        "field_path",
        "subject_id",
        "message",
        "severity",
    ]


def test_diagnostic_is_immutable_and_optional_location_is_explicit():
    result = issue()
    with pytest.raises(FrozenInstanceError):
        result.code = "changed"
    assert not hasattr(result, "__dict__")
    unlocated = replace(result, field_path=None, subject_id=None)
    assert unlocated.field_path is None
    assert unlocated.subject_id is None


@pytest.mark.parametrize("name", ["code", "field_path", "subject_id", "message"])
def test_diagnostic_rejects_blank_or_nontext_fields(name):
    for value in ("", " \t", 1):
        with pytest.raises(ValueError):
            replace(issue(), **{name: value})


@pytest.mark.parametrize(
    "name,value",
    [("phase", "search"), ("phase", None), ("severity", "error"), ("severity", None)],
)
def test_diagnostic_requires_enum_identity(name, value):
    with pytest.raises(ValueError):
        replace(issue(), **{name: value})


def test_diagnostic_enum_values_cover_all_pipeline_stages():
    assert {item.value for item in diagnostics.DiagnosticSeverity} == {"error", "warning", "info"}
    assert {item.value for item in diagnostics.DiagnosticPhase} == {
        "request_validation",
        "input_normalization",
        "rule_loading",
        "construction",
        "search",
        "core_audit",
        "result_assembly",
        "result_audit",
    }
