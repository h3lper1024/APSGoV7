"""Small real public solves protect the diagnostic-only replay boundary."""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from apsgo_scheduler.api.request import fingerprint_public_request
from apsgo_scheduler.app.service import solve_request
from apsgo_scheduler.core import solver
from apsgo_scheduler.core.contracts import fingerprint
from apsgo_v7_service.diagnostics import _json_values
from tests.app.test_input_normalizer import make_request
from tools import profile_solver_search
from tools.profile_solver_search import load_request, main, measure
from tools.verify_solver_diagnostics import _write_json


def prepared(tmp_path, **changes):
    request = make_request()
    request = replace(request, policy=replace(request.policy, candidate_check_limit=100))
    payload = {
        "request": _json_values(request),
        "request_fingerprint": fingerprint_public_request(request),
        "rule_set_fingerprint": request.rule_set_spec.fingerprint,
        **changes,
    }
    path = tmp_path / "prepared.json"
    _write_json(path, payload)
    return path, request


def test_bound_roundtrip_preserves_exact_values_order_and_identity(tmp_path):
    path, request = prepared(tmp_path)
    restored = load_request(path)
    assert restored == request
    assert isinstance(restored.orders[0].weight, Decimal)
    assert fingerprint_public_request(restored) == fingerprint_public_request(request)


@pytest.mark.parametrize("field", ["request_fingerprint", "rule_set_fingerprint"])
def test_invalid_bound_identity_rejected_before_run_or_output(tmp_path, field):
    path, _ = prepared(tmp_path, **{field: "bad"})
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        main(["--prepared-request", str(path), "--output-dir", str(output)])
    assert not output.exists()


def test_first_scope_never_enters_split_or_audit_and_restores_patch(tmp_path, monkeypatch):
    _, request = prepared(tmp_path)
    original = solver.run_local_search

    def forbidden(*args, **kwargs):
        pytest.fail("first-search diagnostic entered finalization")

    monkeypatch.setattr(solver, "run_controlled_order_split", forbidden)
    monkeypatch.setattr(solver, "audit_core_without_search_cache", forbidden)
    report = measure(request)
    assert report["measurement_scope"] == "first_local_search_only_no_release"
    assert "result" not in report
    assert report["first_search"]["final"]["stop_reason"] == "local_search_complete"
    assert solver.run_local_search is original


def test_full_scope_keeps_both_audits_and_first_stage_is_identical(tmp_path):
    _, request = prepared(tmp_path)
    original = solve_request(request)
    first = measure(request)
    full = measure(request, scope="full")
    assert first["first_search"] == full["first_search"]
    assert full["result"]["core_audit"]["passed"]
    assert full["result"]["audit_report"]["passed"]
    assert full["result"]["release"] is not None
    assert full["result"]["result_fingerprint"] == original.result_fingerprint


def test_snapshots_keep_complete_ordered_evaluations_from_the_actual_state(tmp_path, monkeypatch):
    _, request = prepared(tmp_path)
    original_snapshot = profile_solver_search._snapshot
    observed = []

    def snapshot(state, context):
        result = original_snapshot(state, context)
        assert result["evaluation"] == _json_values(state.current_evaluation)
        assert result["evaluation_fingerprint"] == fingerprint(state.current_evaluation)
        assert result["evaluation"]["quality_key"] == result["quality"]
        assert [item["chain_id"] for item in result["evaluation"]["chain_evaluations"]] == [
            chain.chain_id for chain in state.current_plan.chains
        ]
        observed.append(result)
        return result

    monkeypatch.setattr(profile_solver_search, "_snapshot", snapshot)
    report = measure(request, scope="full")
    assert observed == [
        report["first_search"]["initial"], report["first_search"]["final"], report["final_search"],
    ]


def test_check_copies_original_and_records_explicit_policy_override_without_solving(tmp_path, monkeypatch):
    path, request = prepared(tmp_path)
    output = tmp_path / "check"
    monkeypatch.setattr("tools.profile_solver_search.solve_request", lambda *_: pytest.fail("solve called"))
    arguments = ["--prepared-request", str(path), "--output-dir", str(output),
                 "--scope", "check", "--total-time-limit-seconds", "100"]
    assert main(arguments) == 0
    assert (output / "prepared_request.json").read_bytes() == path.read_bytes()
    identity = json.loads((output / "input_identity.json").read_text(), parse_float=Decimal)
    assert identity["original_request_fingerprint"] == fingerprint_public_request(request)
    assert identity["measured_request_fingerprint"] != identity["original_request_fingerprint"]
    assert identity["original_policy"]["total_time_limit_seconds"] == Decimal(12)
    assert identity["measured_policy"]["total_time_limit_seconds"] == Decimal(100)
    assert not (output / "measurement.json").exists()
    with pytest.raises(FileExistsError):
        main(arguments)


def test_source_change_after_read_cannot_change_frozen_input(tmp_path, monkeypatch):
    path, _ = prepared(tmp_path)
    original = path.read_bytes()
    read_bytes = Path.read_bytes

    def changing_read(current):
        payload = read_bytes(current)
        if current == path:
            path.write_bytes(b"changed after read")
        return payload

    monkeypatch.setattr(Path, "read_bytes", changing_read)
    output = tmp_path / "frozen"
    assert main(["--prepared-request", str(path), "--output-dir", str(output), "--scope", "check"]) == 0
    assert (output / "prepared_request.json").read_bytes() == original
    assert read_bytes(path) == b"changed after read"
