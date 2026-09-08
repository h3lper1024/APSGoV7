import asyncio
import csv
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event, get_ident

import httpx
import pytest
from fastapi.testclient import TestClient

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_v7_service import app as http_module
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
    diagnostic_app, monkeypatch, caplog,
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
                release.set()
                if not active.done():
                    active.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await active
                if worker_threads:
                    await _wait_until(lambda: (worker_threads[0], None) in resets)

            assert _handlers() == previous_handlers
            summary = _read(first_run / "diagnostic_summary.json")
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
            retry = await _post(client)
            assert retry.status_code == 200 and retry.json()["status"] == "success"
            assert len(_runs(directory)) == 2
            assert _handlers() == previous_handlers
            assert request_context.get() is None

    with caplog.at_level(logging.INFO):
        asyncio.run(exercise())


def test_same_request_id_creates_independent_files_and_log_contexts(
    diagnostic_app, monkeypatch, caplog,
):
    application, directory = diagnostic_app
    original_solve = scheduling_module.solve_request
    previous_handlers = _handlers()

    def marked_solve(request, cancellation=None):
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
    assert request_context.get() is None


def test_rule_get_and_rejected_busy_request_do_not_enter_active_run(
    diagnostic_app, monkeypatch, caplog,
):
    application, directory = diagnostic_app
    entered, release = Event(), Event()
    original_solve = scheduling_module.solve_request
    previous_handlers = _handlers()

    def paused_solve(request, cancellation=None):
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
    assert _handlers() == previous_handlers


def test_response_serialization_failure_keeps_http_error_and_bound_candidate(
    diagnostic_app, monkeypatch, caplog,
):
    application, directory = diagnostic_app
    previous_handlers = _handlers()

    def fail_mapping(*args):
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
