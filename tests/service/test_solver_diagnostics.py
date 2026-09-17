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
from apsgo_v7_service import diagnostics as diagnostic_module
from apsgo_v7_service.app import MONTH_SOLVE_PATH, create_app
from apsgo_v7_service.diagnostics import (
    DiagnosticStreamHandler,
    RunDiagnostics,
    TimestampFormatter,
    _candidate_csv,
    _json_values,
    request_context,
    start_cpu_timing,
    timing_metrics,
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
    started = perf_counter()
    cpu_started, cpu_count = start_cpu_timing()
    token = request_context.set(RunDiagnostics(
        None, "request-one", started, cpu_started=cpu_started, cpu_count=cpu_count,
    ))
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
    assert "process_cpu_seconds=" in lines[0]
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


def test_file_or_formatter_failure_does_not_escape_logging(capsys, monkeypatch):
    class BrokenStream:
        def write(self, value):
            raise OSError("disk full")

    handler = DiagnosticStreamHandler(BrokenStream())
    handler.setFormatter(TimestampFormatter())
    logger = logging.Logger("isolated", logging.INFO)
    logger.addHandler(handler)
    def broken_clock():
        raise OSError("CPU clock failure")

    monkeypatch.setattr(diagnostic_module, "process_time", broken_clock)
    token = request_context.set(RunDiagnostics(
        None, "failed-logging", perf_counter(), cpu_started=0, cpu_count=8,
    ))
    try:
        logger.info("keep scheduling")
    finally:
        request_context.reset(token)
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


def test_business_writeback_log_preserves_real_violations_and_audit_flags(tmp_path, caplog):
    database = tmp_path / "rules.sqlite3"
    initialize_gqga4_rules(database, initial_grade_dictionary=sample_grade_dictionary())
    application = create_app(database, monthly_solve_policy=replace(policy(), candidate_check_limit=0))
    # No search budget to split a 600-ton IF narrow order whose run limit is 500.
    body = dumps_exact_json(request_data(orders=[order(weight=Decimal('600'), grade_class='IF钢')]))
    with caplog.at_level(logging.INFO), TestClient(application) as client:
        response = client.post(MONTH_SOLVE_PATH, content=body, headers={"content-type": "application/json"})
    result = response.json()
    assert response.status_code == 200
    assert result["status"] == "publishable_with_violations" and len(result["rows"]) == 1
    assert "publishable=True" in caplog.text and "stop_reason=" in caplog.text
    assert "core_audit_passed=False" in caplog.text
    assert "integrity_passed=True writeback_blocking_violation_count=0" in caplog.text
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
    assert summary["process_cpu_seconds"] >= 0
    assert summary["cpu_core_equivalent"] >= 0
    assert summary["cpu_count"] is None or summary["cpu_count"] > 0
    if summary["cpu_count"] is not None:
        assert summary["machine_cpu_percent_estimate"] >= 0
    else:
        assert summary["machine_cpu_percent_estimate"] is None
    assert not {"process_cpu_seconds", "cpu_core_equivalent", "cpu_count",
                "machine_cpu_percent_estimate"}.intersection(response.json())
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
                assert response.json()["rows"] and result["release"] is not None
                assert response.json()["business_rules_satisfied"] is False
                assert candidate["search_evaluation"]["violations"] and result["issues"]
                assert all(row["publishable"] == "True" for row in rows)
            else:
                assert nodes[0]["width"] == Decimal("1000.25")
                assert result["release"]["evaluation"] == candidate["search_evaluation"]
    lines = (folder / "solve.log").read_text().splitlines()
    assert lines and all(re.match(PREFIX, line) for line in lines)
    assert "month_solve_finished" in lines[-1]
    assert "process_cpu_seconds=" in lines[-1] and "cpu_core_equivalent=" in lines[-1]
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


@pytest.mark.parametrize("elapsed,cpu,count,cores,percent", [
    (10.0, 2.0, 8, 0.2, 2.5),
    (10.0, 10.0, 8, 1.0, 12.5),
    (10.0, 25.0, 8, 2.5, 31.25),
    (0.0, 2.0, 8, None, None),
    (10.0, 2.0, None, 0.2, None),
    (0.0000001, 0.0000004, 8, 4.0, 50.0),
])
def test_cpu_timing_uses_unrounded_paired_deltas(monkeypatch, elapsed, cpu, count, cores, percent):
    monkeypatch.setattr(diagnostic_module, "perf_counter", lambda: elapsed)
    monkeypatch.setattr(diagnostic_module, "process_time", lambda: cpu)
    assert timing_metrics(0.0, 0.0, count) == {
        "elapsed_seconds": elapsed,
        "process_cpu_seconds": round(cpu, 6),
        "cpu_core_equivalent": cores,
        "cpu_count": count,
        "machine_cpu_percent_estimate": percent,
    }


@pytest.mark.parametrize("count", [8, None, 0, -1, True, "8", OSError("unavailable")])
def test_cpu_count_is_sampled_once_and_unknown_is_not_one(monkeypatch, count):
    calls = []

    def cpu_count():
        calls.append("count")
        if isinstance(count, Exception):
            raise count
        return count

    monkeypatch.setattr(diagnostic_module.os, "cpu_count", cpu_count)
    monkeypatch.setattr(diagnostic_module, "process_time", lambda: 7.0)
    monkeypatch.setattr(diagnostic_module, "perf_counter", lambda: 10.0)
    cpu_started, sampled_count = start_cpu_timing()
    assert cpu_started == 7.0
    assert sampled_count == (8 if count == 8 else None)
    for _ in range(3):
        assert timing_metrics(0.0, cpu_started, sampled_count)["cpu_count"] == sampled_count
    assert calls == ["count"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0, OSError("clock unavailable")])
def test_failed_cpu_sample_is_missing_not_zero(monkeypatch, value):
    def sample():
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(diagnostic_module, "process_time", sample)
    monkeypatch.setattr(diagnostic_module, "perf_counter", lambda: 20.0)
    assert start_cpu_timing()[0] is None
    metrics = timing_metrics(10.0, 2.0, 8)
    assert metrics["elapsed_seconds"] == 10.0
    assert all(metrics[key] is None for key in (
        "process_cpu_seconds", "cpu_core_equivalent", "machine_cpu_percent_estimate",
    ))


def test_cpu_clock_regression_and_non_finite_ratio_are_missing(monkeypatch):
    monkeypatch.setattr(diagnostic_module, "perf_counter", lambda: 1e-310)
    monkeypatch.setattr(diagnostic_module, "process_time", lambda: 1.0)
    assert timing_metrics(0.0, 2.0, 8)["process_cpu_seconds"] is None
    metrics = timing_metrics(0.0, 0.0, 8)
    assert metrics["process_cpu_seconds"] == 1.0
    assert metrics["cpu_core_equivalent"] is None
    assert metrics["machine_cpu_percent_estimate"] is None


def test_formatter_preserves_explicit_pair_and_unscoped_logs(monkeypatch):
    def unexpected():
        raise AssertionError("must not resample explicit or unrelated log")

    monkeypatch.setattr(diagnostic_module, "perf_counter", unexpected)
    record = logging.LogRecord("isolated", logging.INFO, __file__, 0,
                               "month_solve elapsed_seconds=10.000000 process_cpu_seconds=2.000000", (), None)
    original = (record.msg, record.args)
    token = request_context.set(RunDiagnostics(None, "request-one", 0, cpu_started=0, cpu_count=8))
    try:
        text = TimestampFormatter().format(record)
    finally:
        request_context.reset(token)
    assert text.count("elapsed_seconds=") == text.count("process_cpu_seconds=") == 1
    assert (record.msg, record.args) == original
    record.msg = "rule_management result_code=active_rules_read"
    assert "process_cpu_seconds" not in TimestampFormatter().format(record)


@pytest.mark.parametrize("failure_at", ["start", "later"])
def test_cpu_sampling_failure_preserves_response_and_final_logs(
    diagnostic_database, tmp_path, monkeypatch, failure_at, caplog,
):
    expected = solve_with_files(diagnostic_database, None).json()
    samples = iter([] if failure_at == "start" else [1.0])
    monkeypatch.setattr(diagnostic_module, "process_time", lambda: next(samples))
    root = tmp_path / "failed_cpu"
    with caplog.at_level(logging.INFO):
        response = solve_with_files(diagnostic_database, root).json()
    for result in (expected, response):
        result["run_manifest"].pop("stage_duration_seconds")
    assert response == expected
    summary = read_json(next(root.glob("runs/*/*/diagnostic_summary.json")))
    assert summary["write_failures"] == []
    assert summary["process_cpu_seconds"] is None
    assert summary["cpu_core_equivalent"] is None
    assert summary["machine_cpu_percent_estimate"] is None
    text = next(root.glob("runs/*/*/solve.log")).read_text()
    assert "month_solve_finished" in text and "process_cpu_seconds=-" in text
    assert "month_solve_worker_finished" in caplog.text


def test_missing_start_cannot_be_rebuilt_from_later_cpu_sample(monkeypatch):
    samples = iter([float("nan"), 100.0])
    monkeypatch.setattr(diagnostic_module, "process_time", lambda: next(samples))
    monkeypatch.setattr(diagnostic_module, "perf_counter", lambda: 10.0)
    cpu_started, count = start_cpu_timing()
    assert cpu_started is None
    assert timing_metrics(0.0, cpu_started, count)["process_cpu_seconds"] is None
    assert next(samples) == 100.0


def test_two_formatters_sample_pairs_without_mutating_shared_record(monkeypatch):
    wall, cpu = iter([20.0, 30.0]), iter([5.0, 8.0])
    monkeypatch.setattr(diagnostic_module, "perf_counter", lambda: next(wall))
    monkeypatch.setattr(diagnostic_module, "process_time", lambda: next(cpu))
    record = logging.LogRecord("isolated", logging.INFO, __file__, 0,
                               "solver_stage_finished stage=%s", ("local_search",), None)
    token = request_context.set(RunDiagnostics(None, "one", 10.0, cpu_started=2.0, cpu_count=8))
    try:
        first, second = TimestampFormatter().format(record), TimestampFormatter().format(record)
    finally:
        request_context.reset(token)
    assert "elapsed_seconds=10.000000 process_cpu_seconds=3.000000" in first
    assert "elapsed_seconds=20.000000 process_cpu_seconds=6.000000" in second
    assert record.msg == "solver_stage_finished stage=%s" and record.args == ("local_search",)


@pytest.mark.parametrize("configured", [True, False])
def test_throwing_handler_preserves_early_http_error(diagnostic_database, monkeypatch, capsys, configured):
    from apsgo_v7_service import app as module

    class Broken(logging.Handler):
        def emit(self, record):
            raise OSError("external handler failure")

    monkeypatch.setattr(module._LOGGER, "handlers", [Broken()])
    monkeypatch.setattr(module._LOGGER, "level", logging.INFO)
    client = TestClient(create_app(diagnostic_database,
                                  monthly_solve_policy=policy() if configured else None))
    response = client.post(MONTH_SOLVE_PATH, content="{", headers={"content-type": "application/json"})
    assert response.status_code == (400 if configured else 503)
    text = capsys.readouterr().err
    assert "month_solve request_id=" in text and "process_cpu_seconds=" in text
    assert all(re.match(PREFIX, line) for line in text.splitlines())
    if not configured:
        assert "month_solve_error" in text and "monthly_solve_not_configured" in text
        assert "月计划求解策略尚未配置" in text
