import io
import logging
import os
import re
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from fastapi.testclient import TestClient

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_v7_service.app import MONTH_SOLVE_PATH, create_app
from apsgo_v7_service.diagnostics import (
    DiagnosticStreamHandler,
    TimestampFormatter,
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
    token = request_context.set(("request-one", perf_counter()))
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
