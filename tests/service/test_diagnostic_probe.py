import csv
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_v7_service.rule_management import initialize_gqga4_rules
from tests.service.grade_dictionary_support import sample_grade_dictionary
from tests.service.test_month_scheduling import request_data
from tools import verify_solver_diagnostics as probe


def _small_input(tmp_path, monkeypatch):
    database = tmp_path / "rules.sqlite3"
    initialize_gqga4_rules(database, initial_grade_dictionary=sample_grade_dictionary())
    configuration = tmp_path / "service.yaml"
    values = yaml.safe_load((probe.ROOT / "config/apsgo_v7_service.yaml").read_text())
    values["database_path"] = str(database)
    configuration.write_text(yaml.safe_dump(values), encoding="utf-8")
    request = tmp_path / "request.json"
    request.write_text(
        dumps_exact_json(request_data(expected_active_version_id=999)), encoding="utf-8"
    )
    monkeypatch.setattr(probe, "EXPECTED_ORDER_COUNT", 2)
    return database, configuration, request


def test_small_real_pair_preserves_snapshot_and_compares_diagnostics(tmp_path, monkeypatch):
    database, configuration, request = _small_input(tmp_path, monkeypatch)
    original = database.read_bytes()
    original_request = request.read_bytes()
    output = tmp_path / "evidence"

    report = probe.verify(output, configuration, request)

    assert report["status"] == "pass", report
    assert report["comparison"]["status"] == "match"
    assert report["input"]["original_expected_active_version_id"] == 999
    assert report["input"]["expected_active_version_id"] == 1
    assert report["input"]["order_count"] == 2
    assert report["source_database_unchanged"] and report["snapshot_unchanged"]
    assert database.read_bytes() == original
    assert request.read_bytes() == original_request
    assert all(run["http_status"] == 200 for run in report["runs"])
    assert all(run["source_conservation_passed"] for run in report["runs"])
    assert report["runs"][0]["diagnostic_file_bytes"] == 0
    assert report["runs"][1]["diagnostic_file_bytes"] > 0
    assert report["runs"][1]["candidate_present"]
    assert all(run["console_timestamps_passed"] for run in report["runs"])
    assert report["runs"][1]["accepted_event_count_passed"]
    assert report["runs"][1]["candidate_csv_passed"]
    assert (output / "report.json").is_file()
    solve_log = next((output / "on/diagnostics").rglob("solve.log"))
    original_log = solve_log.read_bytes()
    with solve_log.open("a", encoding="utf-8") as stream:
        stream.write("missing timestamp\n")
    assert not probe._diagnostic_check(output, "on")["diagnostic_files_passed"]
    solve_log.write_bytes(original_log)
    first_line = original_log.decode("utf-8").splitlines()[0]
    prefix = probe.LOG_PREFIX.match(first_line).group()
    with solve_log.open("a", encoding="utf-8") as stream:
        stream.write(prefix + "solver_move_accepted sequence=999\n")
    invalid_counts = probe._diagnostic_check(output, "on")
    assert not invalid_counts["accepted_event_count_passed"]
    assert not invalid_counts["diagnostic_files_passed"]
    solve_log.write_bytes(original_log)
    csv_path = solve_log.parent / "candidate_rows.csv"
    with csv_path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames
        rows = list(reader)
    rows.reverse()
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    assert not probe._diagnostic_check(output, "on")["candidate_csv_passed"]


def test_failed_first_worker_keeps_evidence_and_still_runs_second(tmp_path, monkeypatch):
    _, configuration, request = _small_input(tmp_path, monkeypatch)
    original_run = subprocess.run

    def time_out_first(command, **kwargs):
        if command[-2:] == ["--worker", "off"]:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return original_run(command, **kwargs)

    monkeypatch.setattr(probe.subprocess, "run", time_out_first)
    output = tmp_path / "retained-failure"
    report = probe.verify(output, configuration, request)

    assert report["status"] == "fail", report
    assert report["runs"][0]["error"] == "worker_timeout"
    assert report["runs"][1]["http_status"] == 200
    assert report["comparison"]["status"] == "not_compared_missing_result"
    assert (output / "off/console.log").is_file()
    assert (output / "on/response.json").is_file()
    assert (output / "report.json").is_file()


def test_time_cutoff_is_not_reported_as_deterministic_success():
    response = {"stop_reason": "search_time_limit_reached", "run_manifest": {}}
    assert probe._compare([response, response]) == {"status": "not_compared_time_limited"}


def test_existing_output_is_never_overwritten(tmp_path):
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        probe.verify(tmp_path, Path("unused.yaml"), Path("unused.json"))
    assert sentinel.read_text() == "keep"


def test_clean_export_identity_uses_source_bytes_without_guessing_git(tmp_path, monkeypatch):
    source = tmp_path / "src" / "apsgo_v7_service" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")

    def no_git(*args, **kwargs):
        pytest.fail("Git must not be called for an archive export")

    monkeypatch.setattr(probe.subprocess, "check_output", no_git)
    first = probe._code_identity(tmp_path)
    assert first["git_available"] is False
    assert first["git_commit"] is None and first["git_status"] is None
    assert first["production_source_files"] == {
        "src/apsgo_v7_service/app.py": probe._sha256(source)
    }
    assert probe._code_identity(tmp_path) == first
    source.write_text("value = 2\n", encoding="utf-8")
    assert probe._code_identity(tmp_path)["production_source_sha256"] != first["production_source_sha256"]


def test_csv_probe_rejects_unescaped_formula_and_changed_weight(tmp_path):
    csv_path = tmp_path / "candidate.csv"
    candidate = {"plan": {"chains": [{
        "chain_id": "chain", "assigned_period": "period", "nodes": [{
            "node_id": "=order\nnext", "source_order_id": "source", "weight": Decimal("1.25"),
        }],
    }]}}
    fields = ["artifact_kind", "publishable", "chain_id", "assigned_period", "chain_sequence", "node_sequence", "node_id", "source_order_id", "weight"]

    def write(node_id, weight):
        with csv_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerow(["diagnostic_candidate", True, "chain", "period", 1, 1, node_id, "source", weight])

    write("'=order\\nnext", "1.25")
    assert probe._candidate_csv_check(csv_path, candidate, True)["candidate_csv_passed"]
    write("=order\nnext", "1.25")
    invalid = probe._candidate_csv_check(csv_path, candidate, True)
    assert not invalid["candidate_csv_passed"] and not invalid["candidate_csv_controls_passed"]
    write("'=order\\nnext", "1.26")
    assert not probe._candidate_csv_check(csv_path, candidate, True)["candidate_csv_passed"]
