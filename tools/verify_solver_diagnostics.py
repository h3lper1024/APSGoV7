#!/usr/bin/env python3
"""Retain a reproducible, sequential GQGA4 diagnostics off/on observation pair."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import logging.config
import os
import platform
import re
import sqlite3
import subprocess
import sys
import traceback
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import yaml  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from apsgo_scheduler.api.json_codec import dumps_exact_json  # noqa: E402
from apsgo_scheduler.core.contracts import fingerprint  # noqa: E402
from apsgo_v7_service.app import MONTH_SOLVE_PATH, create_app  # noqa: E402
from apsgo_v7_service.configuration import load_service_configuration  # noqa: E402
from apsgo_v7_service.diagnostics import log_configuration  # noqa: E402
from apsgo_v7_service.month_scheduling import loads_month_solve_request  # noqa: E402
from release.verify_release import _read_database  # noqa: E402

DEFAULT_REQUEST = ROOT / (
    "docs/implementation/evidence/apsgo_v7_month_scheduling_and_grade_preparation/"
    "stage_08_real_http_integration/run_06/request.canonical.json"
)
EXPECTED_ORDER_COUNT = 531
TIME_LIMITS = {"search_time_limit_reached", "finalization_time_limit_reached"}
LOG_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{4} [A-Z]+ \S+ ")


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)


def _write_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(dumps_exact_json(value) + "\n")


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _code_identity(root):
    files = {
        path.relative_to(root).as_posix(): _sha256(path)
        for package in ("apsgo_scheduler", "apsgo_v7_service")
        for path in sorted((root / "src" / package).rglob("*.py"))
    }
    result = {
        "git_available": (root / ".git").exists(),
        "git_commit": None,
        "git_status": None,
        "production_source_files": files,
        "production_source_sha256": hashlib.sha256(
            dumps_exact_json(files).encode("utf-8")
        ).hexdigest(),
    }
    # Archive exports have no Git metadata; never borrow an enclosing checkout's HEAD.
    if result["git_available"]:
        result["git_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
        result["git_status"] = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=root, text=True
        )
    return result


def _database_state(path):
    return {
        suffix or "database": _sha256(candidate) if candidate.exists() else None
        for suffix in ("", "-journal", "-wal", "-shm")
        for candidate in (Path(str(path) + suffix),)
    }


def _response_summary(response, request, snapshot):
    manifest = response.get("run_manifest", {})
    rows = response.get("rows", [])
    expected = {order["source_order_id"]: order["weight"] for order in request["orders"]}
    actual = defaultdict(Decimal)
    for row in rows:
        if row["material_role"] != "virtual_sphc":
            actual[row["source_order_id"]] += row["weight"]
    return {
        "status": response.get("status"),
        "publishable": response.get("publishable") is True,
        "stop_reason": response.get("stop_reason"),
        "quality": response.get("quality", []),
        "audit_summary": response.get("audit_summary"),
        "issues": response.get("issues", []),
        "violation_count": len(response.get("violations", [])),
        "counters": manifest.get("counters", {}),
        "stage_duration_seconds": manifest.get("stage_duration_seconds", {}),
        "result_fingerprint": response.get("result_fingerprint"),
        "deterministic_run_fingerprint": manifest.get("deterministic_run_fingerprint"),
        "trace_fingerprint": manifest.get("trace_fingerprint"),
        "row_count": len(rows),
        "chain_count": len({row["chain_id"] for row in rows}),
        "real_weight": sum(actual.values(), Decimal(0)),
        "virtual_weight": sum(
            (row["weight"] for row in rows if row["material_role"] == "virtual_sphc"),
            Decimal(0),
        ),
        "source_conservation_passed": dict(actual) == expected,
        "identity_passed": (
            response.get("request_id") == request["request_id"]
            and response.get("active_rule_set_version_id") == snapshot["active_version_id"]
            and response.get("rule_set_fingerprint") == snapshot["rule_set_fingerprint"]
            and response.get("grade_dictionary_fingerprint")
            == snapshot["grade_dictionary_fingerprint"]
        ),
    }


def _worker(output, mode):
    """Each process owns its logging configuration and one private HTTP client."""
    run = output / mode
    metrics = {"mode": mode, "status": "error"}
    started = perf_counter()
    try:
        configuration = load_service_configuration(output / "service.yaml")
        directory = run / "diagnostics" if mode == "on" else None
        logging.config.dictConfig(log_configuration(directory))
        application = create_app(
            configuration.database_path,
            timeout_seconds=configuration.database_timeout_seconds,
            monthly_solve_policy=configuration.monthly_solve_policy,
            diagnostics_directory=directory,
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            solve_started = perf_counter()
            response = client.post(
                MONTH_SOLVE_PATH,
                content=(output / "request.json").read_bytes(),
                headers={"content-type": "application/json"},
            )
            metrics["http_elapsed_seconds"] = Decimal(str(perf_counter() - solve_started))
        with (run / "response.json").open("xb") as stream:
            stream.write(response.content)
        metrics["http_status"] = response.status_code
        metrics["response_bytes"] = len(response.content)
        metrics["response_sha256"] = _sha256(run / "response.json")
        parsed = _read_json(run / "response.json")
        metrics.update(_response_summary(
            parsed, _read_json(output / "request.json"), _read_json(output / "input.json")["snapshot"]
        ))
    except Exception as error:
        metrics["error"] = {"type": type(error).__name__, "message": str(error)}
        traceback.print_exc()
    finally:
        logging.shutdown()
        metrics["worker_elapsed_seconds"] = Decimal(str(perf_counter() - started))
        _write_json(run / "metrics.json", metrics)
    return 1 if "error" in metrics else 0


def _log_statistics(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    matches = [LOG_PREFIX.match(line) for line in lines]
    return {
        "lines": len(lines),
        "timestamps_passed": bool(lines) and all(matches),
        "accepted_event_lines": sum(
            bool(match) and line[match.end():].startswith("solver_move_accepted ")
            for line, match in zip(lines, matches)
        ),
    }


def _csv_text(value):
    text = "".join(
        char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
        for char in str(value or "")
    )
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def _candidate_csv_check(path, candidate, publishable):
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    expected = []
    counts = defaultdict(int)
    for chain in candidate["plan"]["chains"]:
        counts[chain["assigned_period"]] += 1
        for sequence, node in enumerate(chain["nodes"], 1):
            expected.append((
                _csv_text(chain["chain_id"]), _csv_text(chain["assigned_period"]),
                str(counts[chain["assigned_period"]]), str(sequence),
                _csv_text(node["node_id"]), _csv_text(node["source_order_id"]), node["weight"],
            ))
    actual = [
        (row["chain_id"], row["assigned_period"], row["chain_sequence"], row["node_sequence"],
         row["node_id"], row["source_order_id"], Decimal(row["weight"]))
        for row in rows
    ]
    numeric = {"chain_sequence", "node_sequence", "weight", "width", "thickness", "min_temperature", "max_temperature"}
    controls_passed = all(
        all(char.isprintable() for char in value)
        and (key in numeric or not value.lstrip().startswith(("=", "+", "-", "@")))
        for row in rows for key, value in row.items()
    )
    return {
        "candidate_csv_rows": len(rows), "expected_candidate_nodes": len(expected),
        "candidate_csv_weight": sum((item[-1] for item in actual), Decimal(0)),
        "expected_candidate_weight": sum((item[-1] for item in expected), Decimal(0)),
        "candidate_csv_controls_passed": controls_passed,
        "candidate_csv_passed": actual == expected and controls_passed and all(
            row["artifact_kind"] == "diagnostic_candidate"
            and row["publishable"] == str(publishable)
            for row in rows
        ),
    }


def _diagnostic_check(output, mode):
    run = output / mode
    diagnostic = run / "diagnostics"
    files = sorted(path for path in diagnostic.rglob("*") if path.is_file())
    sizes = {path.relative_to(run).as_posix(): path.stat().st_size for path in files}
    console = (run / "console.log").read_text(encoding="utf-8", errors="replace")
    console_statistics = _log_statistics(run / "console.log")
    result = {
        "console_log_lines": len(console.splitlines()),
        "console_log_bytes": (run / "console.log").stat().st_size,
        "diagnostic_file_bytes": sum(sizes.values()),
        "diagnostic_file_sizes": sizes,
        "retained_run_file_bytes": sum(path.stat().st_size for path in run.rglob("*") if path.is_file()),
        "diagnostic_write_failed_logged": "diagnostic_write_failed" in console,
        "console_timestamps_passed": console_statistics["timestamps_passed"],
        "console_accepted_event_lines": console_statistics["accepted_event_lines"],
    }
    if mode == "off":
        result["diagnostic_files_passed"] = (
            not diagnostic.exists() and result["console_timestamps_passed"]
        )
        return result
    summaries = list(diagnostic.rglob("diagnostic_summary.json"))
    result["diagnostic_files_passed"] = False
    if len(summaries) != 1:
        return result
    summary_path = summaries[0]
    summary = _read_json(summary_path)
    run_directory = summary_path.parent
    bound = summary.get("bound_result") or {}
    candidate = (bound.get("result") or {}).get("diagnostic_candidate")
    required = {"request.json", "prepared_request.json", "response.json", "solve.log"}
    if candidate is not None:
        required.add("candidate_rows.csv")
    result["candidate_present"] = candidate is not None
    result["candidate_quality"] = (
        None if candidate is None else candidate["search_evaluation"]["quality_key"]
    )
    result["diagnostic_write_failures"] = summary.get("write_failures")
    if all((run_directory / name).is_file() for name in required):
        prepared = _read_json(run_directory / "prepared_request.json")
        response = _read_json(run / "response.json")
        solve_log = _log_statistics(run_directory / "solve.log")
        service_log = _log_statistics(diagnostic / "service.log")
        expected_accepted = (bound.get("result") or {}).get("run_manifest", {}).get("counters", {}).get("accepted_move_count")
        result.update({
            "solve_log_lines": solve_log["lines"],
            "service_log_lines": service_log["lines"],
            "solve_timestamps_passed": solve_log["timestamps_passed"],
            "service_timestamps_passed": service_log["timestamps_passed"],
            "solve_accepted_event_lines": solve_log["accepted_event_lines"],
            "expected_accepted_move_count": expected_accepted,
            "accepted_event_count_passed": (
                type(expected_accepted) is int
                and solve_log["accepted_event_lines"] == expected_accepted
                and console_statistics["accepted_event_lines"] == expected_accepted
            ),
        })
        if candidate is not None:
            result.update(_candidate_csv_check(
                run_directory / "candidate_rows.csv", candidate, summary["publishable"]
            ))
        result["diagnostic_files_passed"] = (
            (diagnostic / "service.log").is_file()
            and summary.get("write_failures") == []
            and not result["diagnostic_write_failed_logged"]
            and result["console_timestamps_passed"]
            and result["solve_timestamps_passed"] and result["service_timestamps_passed"]
            and result["accepted_event_count_passed"]
            and (candidate is None or result["candidate_csv_passed"])
            and (run_directory / "request.json").read_bytes() == (output / "request.json").read_bytes()
            and (run_directory / "response.json").read_bytes() == (run / "response.json").read_bytes()
            and prepared["request_fingerprint"] == response.get("request_fingerprint")
            and bound.get("result", {}).get("result_fingerprint") == response.get("result_fingerprint")
        )
    return result


def _compare(responses):
    if len(responses) != 2 or any("run_manifest" not in value for value in responses):
        return {"status": "not_compared_missing_result"}
    if any(value.get("stop_reason") in TIME_LIMITS for value in responses):
        return {"status": "not_compared_time_limited"}
    normalized = [
        value | {"run_manifest": {
            key: item for key, item in value["run_manifest"].items()
            if key != "stage_duration_seconds"
        }}
        for value in responses
    ]
    identities = {}
    for key in ("deterministic_run_fingerprint", "trace_fingerprint"):
        values = [value["run_manifest"].get(key) for value in responses]
        identities[key] = bool(values[0]) and values[0] == values[1]
    same = normalized[0] == normalized[1] and all(identities.values())
    return {
        "status": "match" if same else "mismatch",
        "stable_response_equal": normalized[0] == normalized[1],
        "fingerprints_equal": identities,
        "excluded_observation_fields": ["run_manifest.stage_duration_seconds"],
    }


def verify(output, service_config, request_path):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"status": "error", "runs": [], "output_directory": str(output)}
    source = None
    before = None
    try:
        service_config, request_path = service_config.resolve(), request_path.resolve()
        configuration = load_service_configuration(service_config)
        source = configuration.database_path
        before = _database_state(source)
        if any(before[suffix] is not None for suffix in ("-journal", "-wal", "-shm")):
            raise ValueError("source database has sidecars; a frozen snapshot is required")
        snapshot = _read_database(source, timeout_seconds=configuration.database_timeout_seconds)
        target = output / "rules_snapshot.sqlite3"
        with sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True) as original:
            with sqlite3.connect(target) as copied:
                original.backup(copied)
        if _read_database(target, timeout_seconds=5.0) != snapshot or _database_state(source) != before:
            raise ValueError("source or snapshot identity changed while freezing")
        snapshot_before = _database_state(target)
        request = _read_json(request_path)
        if len(request["orders"]) != EXPECTED_ORDER_COUNT:
            raise ValueError(f"expected {EXPECTED_ORDER_COUNT} orders, got {len(request['orders'])}")
        old_expectation = request["expected_active_version_id"]
        request["expected_active_version_id"] = snapshot["active_version_id"]
        payload = dumps_exact_json(request)
        parsed = loads_month_solve_request(payload, configuration.monthly_solve_policy)
        _write_json(output / "request.json", request)
        values = yaml.safe_load(service_config.read_text(encoding="utf-8"))
        values["database_path"] = str(target)
        values["listen_host"] = "127.0.0.1"
        values["diagnostics"] = {"enabled": False, "output_directory": "diagnostics"}
        with (output / "service.yaml").open("x", encoding="utf-8") as stream:
            yaml.safe_dump(values, stream, allow_unicode=True, sort_keys=False)
        if load_service_configuration(output / "service.yaml").monthly_solve_policy != configuration.monthly_solve_policy:
            raise ValueError("frozen YAML changed solver policy")
        inputs = {
            "source_database": str(source), "source_database_state": before,
            "snapshot": snapshot, "snapshot_state": snapshot_before,
            "source_configuration": str(service_config), "configuration_sha256": _sha256(service_config),
            "request_fixture": str(request_path), "fixture_sha256": _sha256(request_path),
            "original_expected_active_version_id": old_expectation,
            "expected_active_version_id": snapshot["active_version_id"],
            "request_sha256": _sha256(output / "request.json"),
            "order_count": len(request["orders"]), "periods": request["periods"],
            "real_weight": sum((order["weight"] for order in request["orders"]), Decimal(0)),
            "raw_request_fingerprint": parsed.raw_request_fingerprint,
            "typed_request_fingerprint": parsed.typed_request_fingerprint,
            "policy": values["monthly_solve"], "policy_fingerprint": fingerprint(configuration.monthly_solve_policy),
            **_code_identity(ROOT),
            "probe_sha256": _sha256(Path(__file__).resolve()),
            "platform": platform.platform(), "python": sys.version,
        }
        _write_json(output / "input.json", inputs)
        report["input"] = inputs
        responses = []
        for mode in ("off", "on"):
            run = output / mode
            run.mkdir()
            with (run / "console.log").open("xb") as log:
                try:
                    child = subprocess.run(
                        [sys.executable, str(Path(__file__).resolve()), "--output-dir", str(output), "--worker", mode],
                        cwd=ROOT,
                        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"},
                        stdout=log, stderr=subprocess.STDOUT,
                        timeout=float(configuration.monthly_solve_policy.total_time_limit_seconds) + 90,
                        check=False,
                    )
                    run_report = {"mode": mode, "process_exit_code": child.returncode}
                except subprocess.TimeoutExpired:
                    run_report = {"mode": mode, "process_exit_code": None, "error": "worker_timeout"}
            try:
                if (run / "metrics.json").exists():
                    run_report.update(_read_json(run / "metrics.json"))
                run_report.update(_diagnostic_check(output, mode))
                if (run / "response.json").exists():
                    responses.append(_read_json(run / "response.json"))
            except Exception as error:
                run_report["evidence_error"] = str(error)
            report["runs"].append(run_report)
        report["comparison"] = _compare(responses)
        report["snapshot_unchanged"] = _database_state(target) == snapshot_before
        successful = report["snapshot_unchanged"] and all(
            run.get("process_exit_code") == 0 and run.get("http_status") == 200
            and run.get("status") == "success" and run.get("violation_count") == 0
            and run.get("publishable") is True and run.get("identity_passed") is True
            and run.get("source_conservation_passed") is True
            and run.get("diagnostic_files_passed") is True
            and (run.get("audit_summary") or {}).get("passed") is True
            and "evidence_error" not in run
            for run in report["runs"]
        )
        report["status"] = (
            "pass" if successful and report["comparison"]["status"] == "match" and report["snapshot_unchanged"]
            else "observed_time_limited" if successful and report["comparison"]["status"] == "not_compared_time_limited"
            else "fail"
        )
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        with (output / "probe_error.log").open("x", encoding="utf-8") as stream:
            traceback.print_exc(file=stream)
    finally:
        if source is not None and before is not None:
            try:
                report["source_database_state_after"] = _database_state(source)
                report["source_database_unchanged"] = report["source_database_state_after"] == before
            except OSError as error:
                report["source_database_check_error"] = str(error)
                report["source_database_unchanged"] = False
            if not report["source_database_unchanged"]:
                report["status"] = "fail"
        report["scope"] = "Two sequential TestClient observations; not Windows or 20-pair performance acceptance."
        _write_json(output / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True, help="A new directory; existing paths are rejected.")
    parser.add_argument("--service-config", type=Path, default=ROOT / "config/apsgo_v7_service.yaml")
    parser.add_argument("--request", type=Path, default=DEFAULT_REQUEST)
    parser.add_argument("--worker", choices=("off", "on"), help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    if arguments.worker is not None:
        return _worker(arguments.output_dir, arguments.worker)
    try:
        report = verify(arguments.output_dir, arguments.service_config, arguments.request)
    except OSError as error:
        parser.exit(2, f"Cannot create output directory: {error}\n")
    print(dumps_exact_json({"status": report["status"], "report": str(arguments.output_dir / "report.json")}))
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
