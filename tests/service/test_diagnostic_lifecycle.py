import asyncio
import csv
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event, get_ident

import httpx
import pytest
from fastapi.testclient import TestClient

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_v7_service import app as http_module
from apsgo_v7_service import diagnostics as diagnostic_module
from apsgo_v7_service import scheduling as scheduling_module
from apsgo_v7_service.diagnostics import request_context
from apsgo_v7_service.month_scheduling import MonthSchedulingMappingError
from apsgo_v7_service.rule_management import initialize_gqga4_rules
from tests.service.grade_dictionary_support import sample_grade_dictionary
from tests.service.test_month_scheduling import policy, request_data


@pytest.fixture
def diagnostic_app(tmp_path):
    database = tmp_path / "rules.sqlite3"
    initialize_gqga4_rules(database, initial_grade_dictionary=sample_grade_dictionary())
    directory = tmp_path / "diagnostics"
    return http_module.create_app(
        database, monthly_solve_policy=policy(), diagnostics_directory=directory,
    ), directory


@pytest.fixture
def cpu_clock(monkeypatch):
    clock = {"elapsed": 100.0, "cpu": 10.0, "cpu_count_calls": 0}

    def cpu_count():
        clock["cpu_count_calls"] += 1
        return 8

    monkeypatch.setattr(http_module, "perf_counter", lambda: clock["elapsed"])
    monkeypatch.setattr(diagnostic_module, "perf_counter", lambda: clock["elapsed"])
    monkeypatch.setattr(diagnostic_module, "process_time", lambda: clock["cpu"])
    monkeypatch.setattr(diagnostic_module.os, "cpu_count", cpu_count)
    return clock


def _post(client, body=None):
    return client.post(
        http_module.MONTH_SOLVE_PATH,
        content=dumps_exact_json(request_data() if body is None else body),
        headers={"content-type": "application/json"},
    )


def _runs(directory):
    return tuple(sorted(directory.glob("runs/*/*")))


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)


def _log_timing(line):
    return {
        key: None if value == "-" else Decimal(value)
        for key, value in re.findall(
            r"\b(elapsed_seconds|process_cpu_seconds|cpu_core_equivalent|cpu_count|"
            r"machine_cpu_percent_estimate)=(-|[0-9]+(?:\.[0-9]+)?)(?=\s|$)", line,
        )
    }


def _assert_summary_timing(summary, *, elapsed, cpu, cores, machine_percent):
    assert summary["schema_version"] == 1
    assert summary["elapsed_seconds"] == Decimal(str(elapsed))
    assert summary["process_cpu_seconds"] == Decimal(str(cpu))
    assert summary["cpu_core_equivalent"] == Decimal(str(cores))
    assert summary["cpu_count"] == 8
    assert summary["machine_cpu_percent_estimate"] == Decimal(str(machine_percent))


def _handlers():
    return {
        name: tuple(logging.getLogger(name).handlers)
        for name in ("uvicorn", "apsgo_scheduler", "apsgo_v7_service")
    }


async def _wait_until(predicate):
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, "background diagnostic work did not finish"
        await asyncio.sleep(0.01)


def test_cancelled_asgi_coroutine_keeps_worker_artifacts_and_cleans_up(
    diagnostic_app, cpu_clock, monkeypatch, caplog,
):
    application, directory = diagnostic_app
    entered, cancellation_seen, release = Event(), Event(), Event()
    worker_threads, resets = [], []
    original_solve = scheduling_module.solve_request
    previous_handlers = _handlers()

    class ContextProbe:
        set = staticmethod(request_context.set)

        @staticmethod
        def reset(token):
            request_context.reset(token)
            resets.append((get_ident(), request_context.get()))

    def pause_first_solve(request, cancellation=None):
        if not entered.is_set():
            worker_threads.append(get_ident())
            entered.set()
            deadline = time.monotonic() + 10
            while not cancellation.is_cancelled():
                assert time.monotonic() < deadline
                time.sleep(0.005)
            cancellation_seen.set()
            assert release.wait(10)
        return original_solve(request, cancellation)

    monkeypatch.setattr(http_module, "request_context", ContextProbe())
    monkeypatch.setattr(scheduling_module, "solve_request", pause_first_solve)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://testserver",
        ) as client:
            active = asyncio.create_task(_post(client))
            try:
                await _wait_until(entered.is_set)
                cpu_clock.update(elapsed=108.0, cpu=14.0)
                active.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await active
                await _wait_until(cancellation_seen.is_set)
                first_run, = _runs(directory)
                assert (first_run / "prepared_request.json").is_file()
                assert not (first_run / "diagnostic_summary.json").exists()
                assert _handlers() != previous_handlers
                busy = await _post(client)
                assert busy.status_code == 429
                assert _runs(directory) == (first_run,)
            finally:
                cpu_clock.update(elapsed=116.0, cpu=22.0)
                release.set()
                if not active.done():
                    active.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await active
                if worker_threads:
                    await _wait_until(lambda: (worker_threads[0], None) in resets)

            assert _handlers() == previous_handlers
            summary = _read(first_run / "diagnostic_summary.json")
            _assert_summary_timing(summary, elapsed=16, cpu=12, cores="0.75", machine_percent="9.375")
            result = summary["bound_result"]["result"]
            assert summary["client_disconnected"] is True
            assert result["status"] == "cancelled"
            assert result["stop_reason"] == "user_cancelled"
            assert result["diagnostic_candidate"] is None
            assert not (first_run / "candidate_rows.csv").exists()
            assert _read(first_run / "response.json")["status"] == "cancelled"
            log = (first_run / "solve.log").read_text(encoding="utf-8")
            assert "month_solve_client_disconnected" in log
            assert "month_solve_finished" in log
            disconnected = next(line for line in log.splitlines() if "month_solve_client_disconnected" in line)
            assert _log_timing(disconnected)["process_cpu_seconds"] == 4
            assert "month_solve_worker_finished" not in log
            retry = await _post(client)
            assert retry.status_code == 200 and retry.json()["status"] == "success"
            assert len(_runs(directory)) == 2
            assert _handlers() == previous_handlers
            assert request_context.get() is None
            assert cpu_clock["cpu_count_calls"] == 3

    with caplog.at_level(logging.INFO):
        asyncio.run(exercise())


def test_same_request_id_creates_independent_files_and_log_contexts(
    diagnostic_app, cpu_clock, monkeypatch, caplog,
):
    application, directory = diagnostic_app
    original_solve = scheduling_module.solve_request
    previous_handlers = _handlers()

    def marked_solve(request, cancellation=None):
        cpu_clock["elapsed"] += 8.0
        cpu_clock["cpu"] += 4.0
        logging.getLogger("apsgo_scheduler.lifecycle_test").info(
            "run_marker=%s", request.orders[0].source_order_id,
        )
        return original_solve(request, cancellation)

    monkeypatch.setattr(scheduling_module, "solve_request", marked_solve)
    with caplog.at_level(logging.INFO), TestClient(application) as client:
        for marker in ("first-only", "second-only"):
            body = request_data()
            body["orders"][0]["source_order_id"] = marker
            response = _post(client, body)
            assert response.status_code == 200
            assert response.json()["status"] == "success"
            assert _handlers() == previous_handlers

    runs = _runs(directory)
    assert len(runs) == 2 and runs[0].parent == runs[1].parent
    for run in runs:
        raw = _read(run / "request.json")
        marker = raw["orders"][0]["source_order_id"]
        other = "second-only" if marker == "first-only" else "first-only"
        log = (run / "solve.log").read_text(encoding="utf-8")
        assert f"run_marker={marker}" in log
        assert f"run_marker={other}" not in log
        assert _read(run / "prepared_request.json")["request"]["orders"][0]["source_order_id"] == marker
        assert _read(run / "response.json")["rows"][0]["source_order_id"] == marker
        _assert_summary_timing(
            _read(run / "diagnostic_summary.json"), elapsed=8, cpu=4, cores="0.5", machine_percent="6.25",
        )
        marker_line = next(line for line in log.splitlines() if f"run_marker={marker}" in line)
        assert _log_timing(marker_line)["process_cpu_seconds"] == 4
    assert cpu_clock["cpu_count_calls"] == 2
    assert request_context.get() is None


def test_rule_get_and_rejected_busy_request_do_not_enter_active_run(
    diagnostic_app, cpu_clock, monkeypatch, caplog,
):
    application, directory = diagnostic_app
    entered, release = Event(), Event()
    original_solve = scheduling_module.solve_request
    previous_handlers = _handlers()

    def paused_solve(request, cancellation=None):
        cpu_clock.update(elapsed=108.0, cpu=14.0)
        entered.set()
        assert release.wait(10)
        return original_solve(request, cancellation)

    monkeypatch.setattr(scheduling_module, "solve_request", paused_solve)
    with (
        caplog.at_level(logging.INFO),
        TestClient(application) as first_client,
        TestClient(application) as second_client,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        future = pool.submit(_post, first_client)
        try:
            assert entered.wait(10)
            run, = _runs(directory)
            active_rules = second_client.get(http_module.GET_ACTIVE_RULES_PATH)
            assert active_rules.status_code == 200
            busy = _post(second_client, request_data(
                request_id="00000000-0000-4000-8000-000000000999",
            ))
            assert busy.status_code == 429
            assert busy.json()["error"]["code"] == "scheduling_busy"
            assert _runs(directory) == (run,)
            assert len(tuple((directory / "runs").iterdir())) == 1
        finally:
            release.set()
        assert future.result(timeout=10).status_code == 200

    log = (run / "solve.log").read_text(encoding="utf-8")
    assert "month_solve_started" in log and "month_solve_finished" in log
    assert "rule_management method=GET" not in log
    assert "scheduling_busy" not in log
    assert "00000000-0000-4000-8000-000000000999" not in log
    assert "rule_management method=GET" in caplog.text
    rule_line = next(record.getMessage() for record in caplog.records if "rule_management method=GET" in record.getMessage())
    assert "process_cpu_seconds=" not in rule_line
    busy_line = next(record.getMessage() for record in caplog.records if "result_code=scheduling_busy" in record.getMessage())
    assert _log_timing(busy_line)["process_cpu_seconds"] == 0
    assert "cpu_count=8" in busy_line
    assert cpu_clock["cpu_count_calls"] == 2
    assert _handlers() == previous_handlers


def test_response_serialization_failure_keeps_http_error_and_bound_candidate(
    diagnostic_app, cpu_clock, monkeypatch, caplog,
):
    application, directory = diagnostic_app
    previous_handlers = _handlers()

    def fail_mapping(*args):
        cpu_clock.update(elapsed=108.0, cpu=14.0)
        raise MonthSchedulingMappingError("private response serialization detail")

    monkeypatch.setattr(http_module, "dumps_month_solve_response", fail_mapping)
    with caplog.at_level(logging.INFO), TestClient(application) as client:
        response = _post(client)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "result_mapping_failed"
    assert "private response serialization detail" not in response.text
    run, = _runs(directory)
    assert (run / "response.json").read_bytes() == response.content
    summary = _read(run / "diagnostic_summary.json")
    _assert_summary_timing(summary, elapsed=8, cpu=4, cores="0.5", machine_percent="6.25")
    assert summary["http_status"] == 500
    assert summary["exception"]["type"] == "MonthSchedulingMappingError"
    result = summary["bound_result"]["result"]
    assert result["status"] == "success"
    assert result["diagnostic_candidate"]["plan"]["chains"]
    assert result["core_audit"]["passed"] is True
    assert summary["write_failures"] == []
    with (run / "candidate_rows.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [row["source_order_id"] for row in rows] == ["order-1", "order-2"]
    assert all(row["artifact_kind"] == "diagnostic_candidate" for row in rows)
    assert _handlers() == previous_handlers
    assert request_context.get() is None


def test_cpu_timing_includes_body_parsing_and_samples_summary_before_worker_finish(
    diagnostic_app, cpu_clock, monkeypatch, caplog,
):
    application, directory = diagnostic_app
    events = []
    original_read = http_module._read_request_body
    original_parse = http_module.loads_month_solve_request
    original_close = diagnostic_module.DiagnosticFileHandler.close
    original_write = diagnostic_module.RunDiagnostics.write

    async def read_body(*args, **kwargs):
        assert cpu_clock["cpu_count_calls"] == 1
        events.append("body")
        body = await original_read(*args, **kwargs)
        cpu_clock.update(elapsed=104.0, cpu=12.0)
        return body

    def parse_body(*args, **kwargs):
        events.append("parse")
        parsed = original_parse(*args, **kwargs)
        cpu_clock.update(elapsed=108.0, cpu=14.0)
        return parsed

    def close_log(handler):
        original_close(handler)
        events.append("log_closed")
        cpu_clock.update(elapsed=112.0, cpu=16.0)

    def write_file(diagnostic, filename, render):
        if filename == "diagnostic_summary.json":
            assert diagnostic.handler._closed
            events.append("summary")
        original_write(diagnostic, filename, render)
        if filename == "diagnostic_summary.json":
            events.append("summary_written")
            cpu_clock.update(elapsed=116.0, cpu=18.0)

    monkeypatch.setattr(http_module, "_read_request_body", read_body)
    monkeypatch.setattr(http_module, "loads_month_solve_request", parse_body)
    monkeypatch.setattr(diagnostic_module.DiagnosticFileHandler, "close", close_log)
    monkeypatch.setattr(diagnostic_module.RunDiagnostics, "write", write_file)
    previous_formatter = caplog.handler.formatter
    caplog.handler.setFormatter(diagnostic_module.TimestampFormatter())
    try:
        with caplog.at_level(logging.INFO), TestClient(application) as client:
            response = _post(client)
        log = caplog.text
    finally:
        caplog.handler.setFormatter(previous_formatter)

    assert response.status_code == 200 and response.json()["status"] == "success"
    assert events == ["body", "parse", "log_closed", "summary", "summary_written"]
    run, = _runs(directory)
    summary = _read(run / "diagnostic_summary.json")
    _assert_summary_timing(summary, elapsed=12, cpu=6, cores="0.5", machine_percent="6.25")
    finished = next(line for line in log.splitlines() if "month_solve_worker_finished" in line)
    assert _log_timing(finished) == {
        "elapsed_seconds": Decimal(16),
        "process_cpu_seconds": Decimal(8),
        "cpu_core_equivalent": Decimal("0.5"),
        "cpu_count": Decimal(8),
        "machine_cpu_percent_estimate": Decimal("6.25"),
    }
    run_log = (run / "solve.log").read_text(encoding="utf-8")
    assert "month_solve_worker_finished" not in run_log
    assert cpu_clock["cpu_count_calls"] == 1
    assert request_context.get() is None


@pytest.mark.parametrize(
    ("case", "status", "error_code"),
    [
        ("invalid_json", 400, "invalid_request"),
        ("contract_input", 422, "scheduling_input_invalid"),
        ("media_type", 415, "invalid_content_type"),
        ("body_size", 413, "request_too_large"),
        ("unconfigured", 503, "monthly_solve_not_configured"),
        ("body_read_error", 500, "internal_server_error"),
    ],
)
def test_pre_worker_errors_log_cpu_without_creating_run_files(
    diagnostic_app, cpu_clock, monkeypatch, caplog, case, status, error_code,
):
    application, directory = diagnostic_app
    original_check = http_module._require_no_query

    def check_request(request):
        original_check(request)
        cpu_clock.update(elapsed=108.0, cpu=14.0)

    async def fail_body_read(*args, **kwargs):
        raise RuntimeError("private body read detail")

    monkeypatch.setattr(http_module, "_require_no_query", check_request)
    body = dumps_exact_json(request_data())
    headers = {"content-type": "application/json"}
    if case == "invalid_json":
        body = "{"
    elif case == "contract_input":
        body = dumps_exact_json(request_data(orders=[]))
    elif case == "media_type":
        headers["content-type"] = "text/plain"
    elif case == "body_size":
        headers["content-length"] = str(http_module.MAX_MONTH_SOLVE_REQUEST_BODY_BYTES + 1)
    elif case == "unconfigured":
        application = http_module.create_app(
            directory.parent / "rules.sqlite3", diagnostics_directory=directory,
        )
    elif case == "body_read_error":
        monkeypatch.setattr(http_module, "_read_request_body", fail_body_read)

    previous_handlers = _handlers()
    with caplog.at_level(logging.INFO), TestClient(application) as client:
        response = client.post(http_module.MONTH_SOLVE_PATH, content=body, headers=headers)

    assert response.status_code == status
    assert response.json()["error"]["code"] == error_code
    assert "private body read detail" not in response.text
    assert not directory.exists()
    logs = [record.getMessage() for record in caplog.records if record.getMessage().startswith("month_solve")]
    assert logs and all("process_cpu_seconds=4.000000" in line for line in logs)
    assert _log_timing(logs[0]) == {
        "elapsed_seconds": Decimal(8),
        "process_cpu_seconds": Decimal(4),
        "cpu_core_equivalent": Decimal("0.5"),
        "cpu_count": Decimal(8),
        "machine_cpu_percent_estimate": Decimal("6.25"),
    }
    assert "month_solve_started" not in caplog.text
    assert "month_solve_worker_finished" not in caplog.text
    assert cpu_clock["cpu_count_calls"] == 1
    assert _handlers() == previous_handlers
    assert request_context.get() is None
