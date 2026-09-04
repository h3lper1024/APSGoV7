"""Expose one application entry and the existing core function, without draft APIs."""

import ast
from inspect import signature

import pytest

from .test_clean_room_boundaries import PACKAGE


def application_entries(package):
    return [
        (path.relative_to(package).as_posix(), node.name)
        for path in sorted(package.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "solve_request"
    ]


def test_only_service_defines_the_application_entry():
    assert application_entries(PACKAGE) == [("app/service.py", "solve_request")]


def test_public_entry_is_an_alias_not_a_second_wrapper():
    from apsgo_scheduler import api, app, core
    from apsgo_scheduler.app import service
    from apsgo_scheduler.core import solver

    assert app.__all__ == ["solve_request"]
    assert app.solve_request is service.solve_request
    assert core.__all__ == ["solve"]
    assert core.solve is solver.solve
    assert tuple(signature(app.solve_request).parameters) == ("request", "cancellation")
    assert not hasattr(api, "solve_request")
    for module in (api, app, core):
        assert not hasattr(module, "DraftSchedulingRelease")
        assert not hasattr(module, "DraftSchedulingResult")


@pytest.mark.parametrize(
    "name,module",
    (
        ("SchedulingRequest", "request"),
        ("OrderInput", "request"),
        ("PeriodInput", "request"),
        ("VirtualPrototypeInput", "request"),
        ("RuleSetSpec", "request"),
        ("RuleDefinitionSpec", "request"),
        ("QualityCriterionSpec", "request"),
        ("SchedulingResult", "result"),
        ("SchedulingRelease", "result"),
        ("RunManifest", "result"),
        ("ResultAuditReport", "result"),
        ("ResultAuditStatus", "result"),
        ("DiagnosticIssue", "diagnostics"),
        ("DiagnosticPhase", "diagnostics"),
        ("DiagnosticSeverity", "diagnostics"),
    ),
)
def test_api_reexports_the_existing_contracts_without_copying_classes(name, module):
    from apsgo_scheduler import api

    assert name in api.__all__
    assert getattr(api, name) is getattr(getattr(api, module), name)


def test_public_request_dependencies_are_available_without_importing_application_code():
    from apsgo_scheduler import api
    from apsgo_scheduler.core import contracts, model

    for name in (
        "SolverPolicy",
        "RuleScope",
        "SearchStopReason",
        "SolveStatus",
        "ControlledSplitMode",
    ):
        assert getattr(api, name) is getattr(contracts, name)
    for name in ("MaterialRole", "VirtualPurpose"):
        assert getattr(api, name) is getattr(model, name)


def test_entry_scanner_detects_an_accidental_second_entry(tmp_path):
    source = tmp_path / "api"
    source.mkdir()
    (source / "alternate.py").write_text("def solve_request(request): pass\n", encoding="utf-8")
    assert application_entries(tmp_path) == [("api/alternate.py", "solve_request")]
