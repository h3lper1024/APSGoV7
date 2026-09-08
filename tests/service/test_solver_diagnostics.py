import csv
import io
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from time import perf_counter

import pytest
from fastapi.testclient import TestClient

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_v7_service.app import MONTH_SOLVE_PATH, create_app
from apsgo_v7_service.diagnostics import (
    DiagnosticStreamHandler,
    RunDiagnostics,
    TimestampFormatter,
    _candidate_csv,
    _json_values,
    request_context,
)
from apsgo_v7_service.rule_management import initialize_gqga4_rules
from tests.service.grade_dictionary_support import sample_grade_dictionary
from tests.service.test_month_scheduling import order, policy, request_data

PREFIX = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{4} "


def test_timestamp_every_exception_line_and_escaped_external_controls():
    output = io.StringIO()
    handler = DiagnosticStreamHandler(output)
    handler.setFormatter(TimestampFormatter())
    logger = logging.Logger("isolated", logging.INFO)
    logger.addHandler(handler)
    token = request_context.set(RunDiagnostics(None, "request-one", perf_counter()))
    try:
        try:
            raise ValueError("test failure")
        except ValueError:
            logger.exception("external=%s", "line\nfake\r\x1b[31m")
    finally:
        request_context.reset(token)
    lines = output.getvalue().splitlines()
    assert len(lines) > 2 and all(re.match(PREFIX, line) for line in lines)
    assert "external=line\\nfake\\r\\x1b[31m" in lines[0]
    assert "request_id=request-one" in lines[0] and "elapsed_seconds=" in lines[0]
    assert "ValueError: test failure" in lines[-1]
    assert request_context.get() is None


def test_real_log_configuration_routes_core_uvicorn_and_service_and_rotates(tmp_path):
    script = """
import logging, logging.config, pathlib, sys
from apsgo_v7_service.diagnostics import log_configuration
directory = pathlib.Path(sys.argv[1])
config = log_configuration(directory)
assert config['handlers']['file']['maxBytes'] == 10 * 1024 * 1024
assert config['handlers']['file']['backupCount'] == 3
logging.config.dictConfig(config)
for name in ('uvicorn.error', 'uvicorn.error.apsgo_v7.rule_management', 'apsgo_scheduler.core.solver'):
    logging.getLogger(name).info('route-check')
handler = logging.getLogger('uvicorn').handlers[1]
handler.maxBytes = 128
for i in range(8):
    logging.getLogger('uvicorn.error').info('rotation-check %s', i)
logging.shutdown()
assert (directory / 'service.log.3').is_file()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        capture_output=True, text=True, check=True,
    )
    lines = result.stderr.splitlines()
    assert sum("route-check" in line for line in lines) == 3
    assert all(re.match(PREFIX, line) for line in lines)
    assert all(re.match(PREFIX, line) for line in (tmp_path / "service.log").read_text().splitlines())


def test_file_or_formatter_failure_does_not_escape_logging(capsys):
    class BrokenStream:
        def write(self, value):
            raise OSError("disk full")

    handler = DiagnosticStreamHandler(BrokenStream())
    handler.setFormatter(TimestampFormatter())
    logger = logging.Logger("isolated", logging.INFO)
    logger.addHandler(handler)
    logger.info("keep scheduling")
    warning = capsys.readouterr().err
    assert re.match(PREFIX, warning) and "diagnostic_write_failed" in warning


def test_missing_configuration_exits_with_only_timestamped_error_lines(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", "import logging, sys; logging.basicConfig(format='PREEXISTING %(message)s'); from apsgo_v7_service.app import main; main(['--config', sys.argv[1]])", str(tmp_path / "missing.yaml")],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "service_start_failed" in result.stderr
    assert "configuration file does not exist" in result.stderr
    assert all(re.match(PREFIX, line) for line in result.stderr.splitlines())


def test_non_publishable_http_log_preserves_real_reason_without_changing_rows(tmp_path, caplog):
    database = tmp_path / "rules.sqlite3"
    initialize_gqga4_rules(database, initial_grade_dictionary=sample_grade_dictionary())
    application = create_app(database, monthly_solve_policy=replace(policy(), candidate_check_limit=0))
    # No search budget to split a 600-ton IF narrow order whose run limit is 500.
    body = dumps_exact_json(request_data(orders=[order(weight=Decimal('600'), grade_class='IF钢')]))
    with caplog.at_level(logging.INFO), TestClient(application) as client:
        response = client.post(MONTH_SOLVE_PATH, content=body, headers={"content-type": "application/json"})
    result = response.json()
    assert response.status_code == 200
    assert result["status"] == "complete_not_publishable" and result["rows"] == []
    assert "publishable=False" in caplog.text and "stop_reason=" in caplog.text
    assert "if_narrow_run_weight" in caplog.text
    assert "elapsed_seconds=" in caplog.text
    assert "subject=initial-000001:chain_if_narrow_real_weight_lte:0-0" in caplog.text


@pytest.fixture
def diagnostic_database(tmp_path):
    path = tmp_path / "rules.sqlite3"
    initialize_gqga4_rules(path, initial_grade_dictionary=sample_grade_dictionary())
    return path


def solve_with_files(database, directory, value=None, solver_policy=None):
    application = create_app(database, monthly_solve_policy=solver_policy or policy(), diagnostics_directory=directory)
    with TestClient(application) as client:
        return client.post(MONTH_SOLVE_PATH, content=dumps_exact_json(value or request_data()),
                           headers={"content-type": "application/json"})


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)


@pytest.mark.parametrize("case", ["success", "prohibited", "input_invalid", "binding_conflict"])
def test_real_artifacts_preserve_candidate_audits_and_exact_response(diagnostic_database, tmp_path, case, caplog):
    root = tmp_path / "诊断 文件"
    value = request_data()
    budget = policy()
    if case == "prohibited":
        value = request_data(orders=[order(weight=Decimal("600"), grade_class="IF钢")])
        budget = replace(budget, candidate_check_limit=0)
    elif case == "input_invalid":
        value = request_data(orders=[order(width=None)])
    elif case == "binding_conflict":
        value = request_data(expected_active_version_id=2)
    with caplog.at_level(logging.INFO):
        response = solve_with_files(diagnostic_database, root, value, budget)
    folder = next(root.glob("runs/*/*"))
    assert (folder / "response.json").read_bytes() == response.content
    assert read_json(folder / "request.json") == value
    summary = read_json(folder / "diagnostic_summary.json")
    assert summary["write_failures"] == []
    assert summary["http_status"] == response.status_code
    assert set(summary["written_files"]) == {path.name for path in folder.iterdir()}
    if case == "binding_conflict":
        assert response.status_code == 409 and summary["bound_result"] is None
        assert summary["exception"]["type"] == "RuleManagementServiceError"
        assert not (folder / "prepared_request.json").exists()
    else:
        prepared = read_json(folder / "prepared_request.json")
        assert prepared["request"]["orders"][0]["rule_attributes"]["soft_hard_class"] == "软钢"
        result = summary["bound_result"]["result"]
        assert result["status"] == response.json()["status"]
        assert result["run_manifest"] == read_json(folder / "response.json")["run_manifest"]
        candidate = result["diagnostic_candidate"]
        if case == "input_invalid":
            assert candidate is None and result["issues"]
            assert not (folder / "candidate_rows.csv").exists()
        else:
            with (folder / "candidate_rows.csv").open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            nodes = [node for chain in candidate["plan"]["chains"] for node in chain["nodes"]]
            assert [row["node_id"] for row in rows] == [node["node_id"] for node in nodes]
            assert [Decimal(row["weight"]) for row in rows] == [node["weight"] for node in nodes]
            if case == "prohibited":
                assert response.json()["rows"] == [] and result["release"] is None
                assert candidate["search_evaluation"]["violations"] and result["issues"]
                assert all(row["publishable"] == "False" for row in rows)
            else:
                assert nodes[0]["width"] == Decimal("1000.25")
                assert result["release"]["evaluation"] == candidate["search_evaluation"]
    lines = (folder / "solve.log").read_text().splitlines()
    assert lines and all(re.match(PREFIX, line) for line in lines)
    assert "month_solve_finished" in lines[-1]
    assert request_context.get() is None


@pytest.mark.parametrize("failure", ["directory", "handler", "csv", "serialize", "replace", "close"])
def test_diagnostic_failures_do_not_replace_business_response(diagnostic_database, tmp_path, monkeypatch, capsys, failure):
    from apsgo_v7_service import diagnostics as module

    def broken(*args, **kwargs):
        raise OSError("diagnostic disk failure")

    if failure == "directory":
        monkeypatch.setattr(module, "mkdtemp", broken)
    elif failure == "handler":
        monkeypatch.setattr(module, "DiagnosticFileHandler", broken)
    elif failure == "csv":
        monkeypatch.setattr(module, "_candidate_csv", broken)
    elif failure == "serialize":
        monkeypatch.setattr(module, "_json_values", broken)
    elif failure == "replace":
        monkeypatch.setattr(Path, "replace", broken)
    else:
        original = module.DiagnosticFileHandler.close

        def broken_close(self):
            original(self)
            broken()

        monkeypatch.setattr(module.DiagnosticFileHandler, "close", broken_close)
    root = tmp_path / "diagnostics"
    response = solve_with_files(diagnostic_database, root)
    assert response.status_code == 200 and response.json()["publishable"] is True
    assert "diagnostic_write_failed" in capsys.readouterr().err
    assert not list(root.rglob("*.tmp"))
    if failure in {"handler", "csv", "close"}:
        summary = read_json(next(root.glob("runs/*/*/diagnostic_summary.json")))
        assert summary["write_failures"]
        assert summary["write_failures"][0]["filename"] == (
            "candidate_rows.csv" if failure == "csv" else "solve.log"
        )
    for name in ("uvicorn", "apsgo_scheduler", "apsgo_v7_service"):
        assert not any(getattr(handler, "failure_callback", None) for handler in logging.getLogger(name).handlers)


def test_csv_keeps_order_lineage_and_blocks_spreadsheet_formulas():
    from apsgo_scheduler.core.model import (
        Chain,
        MaterialRole,
        SchedulePlan,
        VirtualLineage,
        VirtualPurpose,
    )
    from tests.service.test_month_scheduling import real_node, split_piece

    virtual = real_node("virtual", None, None, material_role=MaterialRole.GENERATED_VIRTUAL,
                        virtual_lineage=VirtualLineage("prototype", VirtualPurpose.SPLIT_SEPARATOR, "partition-1", 1))
    plan = SchedulePlan((Chain("chain", (split_piece(1, "300"), virtual,
                                         real_node("=formula", "@order", "P0")), "P0"),))
    rows = list(csv.DictReader(io.StringIO(_candidate_csv(plan, False))))
    assert json.loads(rows[0]["split_lineage"])["partition_id"] == "partition-1"
    assert json.loads(rows[1]["virtual_lineage"])["prototype_id"] == "prototype"
    assert rows[1]["source_order_id"] == ""
    assert rows[2]["node_id"] == "'=formula" and rows[2]["source_order_id"] == "'@order"
    encoded = json.loads(dumps_exact_json(_json_values(plan)))
    assert encoded["chains"][0]["nodes"][2]["node_id"] == "=formula"


def test_file_switch_preserves_deterministic_result(diagnostic_database, tmp_path):
    off = solve_with_files(diagnostic_database, None).json()
    on = solve_with_files(diagnostic_database, tmp_path / "on").json()
    for response in (off, on):
        response["run_manifest"].pop("stage_duration_seconds")
    assert off == on


def test_throwing_external_handler_does_not_change_http_result(diagnostic_database, tmp_path, monkeypatch):
    class Broken(logging.Handler):
        def emit(self, record):
            raise OSError("external handler failure")

    # Startup has no business work; exercise the actual endpoint without a lifespan.
    from apsgo_v7_service import app as module
    monkeypatch.setattr(module._LOGGER, "handlers", [Broken()])
    client = TestClient(create_app(diagnostic_database, monthly_solve_policy=policy(),
                                   diagnostics_directory=tmp_path / "on"))
    response = client.post(MONTH_SOLVE_PATH, content=dumps_exact_json(request_data()),
                           headers={"content-type": "application/json"})
    assert response.status_code == 200 and response.json()["publishable"]
