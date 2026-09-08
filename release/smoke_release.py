#!/usr/bin/env python3
"""Run the Windows package smoke test against a disposable package copy."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from copy import deepcopy
from decimal import Decimal
from math import isfinite
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_v7_service.app import GET_ACTIVE_RULES_PATH, SET_ACTIVE_RULES_PATH
from apsgo_v7_service.configuration import load_service_configuration

try:
    from .verify_release import (
        CONFIGURATION_PATH,
        MANIFEST_PATH,
        PACKAGE_CONFIGURATION_PATH,
        PACKAGE_DATABASE_PATH,
        RUNTIME_CONFIGURATION_PATH,
        RUNTIME_DATABASE_PATH,
        SOURCE_DATABASE_PATH,
        _read_database,
        _reject_database_sidecars,
        _sha256,
        verify_package,
    )
except ImportError:  # Direct execution from the tracked release directory.
    from verify_release import (  # type: ignore[no-redef]
        CONFIGURATION_PATH,
        MANIFEST_PATH,
        PACKAGE_CONFIGURATION_PATH,
        PACKAGE_DATABASE_PATH,
        RUNTIME_CONFIGURATION_PATH,
        RUNTIME_DATABASE_PATH,
        SOURCE_DATABASE_PATH,
        _read_database,
        _reject_database_sidecars,
        _sha256,
        verify_package,
    )


class SmokeError(RuntimeError):
    pass


_LOCAL_HTTP_OPENER = build_opener(ProxyHandler({}))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


def _http_json(
    base_url: str,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    timeout_seconds: float,
) -> tuple[int, dict]:
    data = None if body is None else dumps_exact_json(body).encode("utf-8")
    headers = {} if data is None else {"Content-Type": "application/json; charset=utf-8"}
    request = Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with _LOCAL_HTTP_OPENER.open(request, timeout=timeout_seconds) as response:
            status = response.status
            content = response.read()
    except HTTPError as error:
        status = error.code
        content = error.read()
    value = json.loads(content.decode("utf-8"), parse_float=Decimal)
    _require(isinstance(value, dict), "HTTP response must be a JSON object")
    return status, value


def _wait_for_rules(
    process: subprocess.Popen,
    base_url: str,
    log_path: Path,
    *,
    startup_timeout_seconds: float,
    request_timeout_seconds: float,
) -> dict:
    deadline = time.monotonic() + startup_timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            status, body = _http_json(
                base_url,
                "GET",
                GET_ACTIVE_RULES_PATH,
                timeout_seconds=request_timeout_seconds,
            )
            _require(status == 200, f"rules GET returned HTTP {status}: {body}")
            return body
        except (ConnectionError, TimeoutError, URLError, OSError) as error:
            last_error = error
            time.sleep(0.1)
    logs = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    raise SmokeError(f"service did not become ready: {last_error}; log={logs}")


def _request_from_active(active: dict, operation_id: str) -> dict:
    return {
        "save_operation_id": operation_id,
        "expected_active_version_id": active["active_version_id"],
        "rules": [
            {
                "rule_id": rule["rule_id"],
                "enabled": rule["enabled"],
                "parameters": deepcopy(rule["parameters"]),
            }
            for rule in active["rules"]
        ],
        "virtual_prototypes": deepcopy(active["virtual_prototypes"]),
        "remark": f"Windows release smoke {operation_id}",
    }


def _assert_initial_identity(active: dict, manifest: dict) -> None:
    database = manifest["database"]
    _require(active.get("product_line_code") == "GQGA4", "product line mismatch")
    _require(active.get("process_code") == "default", "process mismatch")
    _require(active.get("scenario") == "month", "scenario mismatch")
    _require(active.get("active_version_id") == database["active_version_id"], "active version mismatch")
    _require(active.get("based_on_version_id") == database["based_on_version_id"], "base version mismatch")
    _require(active.get("fingerprint") == database["rule_set_fingerprint"], "rule fingerprint mismatch")
    _require(len(active.get("rules", ())) == database["rule_count"], "rule count mismatch")
    _require(
        sum(rule.get("enabled") is True for rule in active["rules"])
        == database["enabled_rule_count"],
        "enabled rule count mismatch",
    )
    _require(
        len(active.get("virtual_prototypes", ())) == database["virtual_prototype_count"],
        "virtual prototype count mismatch",
    )


def _assert_saved(saved: dict, active: dict, operation_id: str, previous: int) -> None:
    _require(saved.get("save_operation_id") == operation_id, "save operation id changed")
    _require(saved.get("previous_active_version_id") == previous, "previous version changed")
    _require(saved.get("saved_version_id") == saved.get("active_version_id"), "saved version is not active")
    _require(saved.get("saved_version_is_active") is True, "saved version was not activated")
    _require(saved.get("idempotent_replay") is False, "first save was reported as replay")
    for key, value in active.items():
        _require(saved.get(key) == value, f"POST and following GET differ at {key}")


def _assert_business_content_preserved(initial: dict, active: dict) -> None:
    for key in (
        "product_line_code",
        "process_code",
        "scenario",
        "rules",
        "quality_spec",
        "allowed_final_deviation_codes",
        "virtual_prototypes",
    ):
        _require(key in initial and key in active, f"saved rules missing {key}")
        _require(active[key] == initial[key], f"saved rules changed at {key}")
    _require(
        active.get("based_on_version_id") == initial.get("active_version_id"),
        "saved rules do not reference the previous active version",
    )
    _require(
        active.get("version_no") == initial.get("version_no") + 1,
        "saved rule version number is not consecutive",
    )
    _require(
        active.get("active_version_id") != initial.get("active_version_id"),
        "save did not create a new active version",
    )
    _require(
        active.get("fingerprint") != initial.get("fingerprint"),
        "new rule version did not receive a new fingerprint",
    )


def _assert_runtime_identity(
    runtime: dict, initial: dict, active: dict, manifest: dict
) -> None:
    database = manifest["database"]
    expected = {
        "quick_check": "ok",
        "schema_version": database["schema_version"],
        "active_version_id": active["active_version_id"],
        "based_on_version_id": initial["active_version_id"],
        "rule_count": len(active["rules"]),
        "enabled_rule_count": sum(rule["enabled"] is True for rule in active["rules"]),
        "virtual_prototype_count": len(active["virtual_prototypes"]),
        "grade_dictionary_entry_count": database["grade_dictionary_entry_count"],
        "rule_set_fingerprint": active["fingerprint"],
        "grade_dictionary_fingerprint": database["grade_dictionary_fingerprint"],
    }
    for key, value in expected.items():
        _require(runtime.get(key) == value, f"runtime database identity mismatch at {key}")


def _available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _write_smoke_configuration(template: Path, destination: Path, port: int):
    text = template.read_text(encoding="utf-8")
    host_pattern = r"(?m)^listen_host:\s*.*$"
    port_pattern = r"(?m)^listen_port:\s*.*$"
    _require(len(re.findall(host_pattern, text)) == 1, "listen_host must occur once")
    _require(len(re.findall(port_pattern, text)) == 1, "listen_port must occur once")
    text = re.sub(host_pattern, "listen_host: 127.0.0.1", text)
    text = re.sub(port_pattern, f"listen_port: {port}", text)
    destination.write_text(text, encoding="utf-8", newline="\n")
    configuration = load_service_configuration(destination)
    _require(configuration.listen_host == "127.0.0.1", "smoke host is not loopback")
    _require(configuration.listen_port == port, "smoke port changed")
    return configuration


def _start(package: Path, log_path: Path) -> tuple[subprocess.Popen, object]:
    log = log_path.open("ab")
    try:
        process = subprocess.Popen(
            [os.environ.get("ComSpec", "cmd.exe"), "/d", "/c", str(package / "start_apsgo_v7_service.bat")],
            cwd=package,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    except BaseException:
        log.close()
        raise
    return process, log


def _force_stop_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    system_root = os.environ.get("SystemRoot")
    _require(bool(system_root), "SystemRoot is unavailable for emergency cleanup")
    taskkill = Path(system_root) / "System32" / "taskkill.exe"
    result = subprocess.run(
        [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired as error:
        detail = result.stderr.decode("utf-8", errors="replace")[-2000:]
        raise SmokeError(f"emergency process-tree cleanup timed out: {detail}") from error
    _require(process.poll() is not None, "emergency process-tree cleanup did not exit")


def _stop(package: Path, process: subprocess.Popen, log) -> None:
    failure = ""
    try:
        try:
            result = subprocess.run(
                [
                    os.environ.get("ComSpec", "cmd.exe"),
                    "/d",
                    "/c",
                    str(package / "stop_apsgo_v7_service.bat"),
                ],
                cwd=package,
                check=False,
                capture_output=True,
                timeout=30,
            )
            failure = result.stderr.decode("utf-8", errors="replace")[-2000:]
            if result.returncode == 0:
                process.wait(timeout=20)
                return
        except (OSError, subprocess.TimeoutExpired) as error:
            failure = str(error)
        if process.poll() is not None:
            return
        try:
            _force_stop_process_tree(process)
        except Exception as error:
            raise SmokeError(
                f"stop script failed ({failure}); emergency cleanup failed: {error}"
            ) from error
    finally:
        log.close()


def run_smoke(
    repository_root: str | Path,
    package_root: str | Path,
    *,
    startup_timeout_seconds: float = 60.0,
    request_timeout_seconds: float = 10.0,
) -> dict:
    if os.name != "nt" or not sys.platform.startswith("win"):
        raise SmokeError("Windows x64 is required for the release smoke test")
    if (
        type(startup_timeout_seconds) not in (int, float)
        or not isfinite(startup_timeout_seconds)
        or startup_timeout_seconds <= 0
        or type(request_timeout_seconds) not in (int, float)
        or not isfinite(request_timeout_seconds)
        or request_timeout_seconds <= 0
    ):
        raise SmokeError("smoke timeouts must be positive finite numbers")
    repository = Path(repository_root).resolve(strict=True)
    package = Path(package_root).resolve(strict=True)
    package_result = verify_package(package, allow_pending_smoke=True)
    manifest = json.loads(
        (package / MANIFEST_PATH).read_text(encoding="utf-8"), parse_float=Decimal
    )
    source_database = repository / SOURCE_DATABASE_PATH
    formal_seed = package / PACKAGE_DATABASE_PATH
    source_sha256 = _sha256(source_database)
    seed_sha256 = _sha256(formal_seed)
    configuration_sha256 = _sha256(repository / CONFIGURATION_PATH)
    _require(
        configuration_sha256 == manifest["source"]["configuration_sha256"],
        "source configuration changed before smoke",
    )

    package_configuration = load_service_configuration(
        package / PACKAGE_CONFIGURATION_PATH
    )
    temporary_root = Path(tempfile.mkdtemp(prefix="APSGo V7 smoke "))
    smoke_package = temporary_root / "APSGo V7 package"
    shutil.copytree(package, smoke_package)
    log_path = temporary_root / "service.log"
    process = None
    log = None
    success = False
    try:
        verify_package(smoke_package, allow_pending_smoke=True)
        help_result = subprocess.run(
            [str(smoke_package / "APSGoV7Service.exe"), "--help"],
            cwd=smoke_package,
            check=False,
            capture_output=True,
            timeout=30,
        )
        _require(help_result.returncode == 0, "packaged executable --help failed")
        _require(not (smoke_package / RUNTIME_CONFIGURATION_PATH).exists(), "runtime YAML already exists")
        _require(not (smoke_package / RUNTIME_DATABASE_PATH).exists(), "runtime database already exists")

        configuration = _write_smoke_configuration(
            smoke_package / PACKAGE_CONFIGURATION_PATH,
            smoke_package / RUNTIME_CONFIGURATION_PATH,
            _available_loopback_port(),
        )
        base_url = f"http://127.0.0.1:{configuration.listen_port}"
        process, log = _start(smoke_package, log_path)
        initial = _wait_for_rules(
            process,
            base_url,
            log_path,
            startup_timeout_seconds=startup_timeout_seconds,
            request_timeout_seconds=request_timeout_seconds,
        )
        time.sleep(0.2)
        _require(process.poll() is None, "new service process exited during startup")
        _assert_initial_identity(initial, manifest)
        operation_id = str(uuid.uuid4())
        status, saved = _http_json(
            base_url,
            "POST",
            SET_ACTIVE_RULES_PATH,
            _request_from_active(initial, operation_id),
            timeout_seconds=request_timeout_seconds,
        )
        _require(status == 200, f"rules POST returned HTTP {status}: {saved}")
        status, active = _http_json(
            base_url,
            "GET",
            GET_ACTIVE_RULES_PATH,
            timeout_seconds=request_timeout_seconds,
        )
        _require(status == 200, f"following rules GET returned HTTP {status}: {active}")
        _assert_saved(saved, active, operation_id, initial["active_version_id"])
        _assert_business_content_preserved(initial, active)
        _stop(smoke_package, process, log)
        process = None
        log = None

        process, log = _start(smoke_package, log_path)
        restarted = _wait_for_rules(
            process,
            base_url,
            log_path,
            startup_timeout_seconds=startup_timeout_seconds,
            request_timeout_seconds=request_timeout_seconds,
        )
        _require(restarted == active, "active rules changed after restart")
        _stop(smoke_package, process, log)
        process = None
        log = None

        runtime_database = smoke_package / RUNTIME_DATABASE_PATH
        _reject_database_sidecars(smoke_package, runtime_database)
        runtime = _read_database(
            runtime_database,
            timeout_seconds=package_configuration.database_timeout_seconds,
        )
        _assert_runtime_identity(runtime, initial, active, manifest)
        _require(_sha256(smoke_package / PACKAGE_DATABASE_PATH) == seed_sha256, "smoke seed changed")
        _require(_sha256(source_database) == source_sha256, "source database changed")
        _require(_sha256(formal_seed) == seed_sha256, "formal package seed changed")
        _require(
            _sha256(repository / CONFIGURATION_PATH) == configuration_sha256,
            "source configuration changed",
        )
        verify_package(package, allow_pending_smoke=True)
        success = True
        return {
            "status": "pass",
            "mode": "smoke",
            "initial_active_version_id": initial["active_version_id"],
            "saved_active_version_id": active["active_version_id"],
            "restarted": True,
            "package_file_count": package_result["file_count"],
        }
    finally:
        if process is not None and log is not None:
            try:
                _stop(smoke_package, process, log)
            except Exception as error:
                sys.stderr.write(f"Emergency service stop failed: {error}\n")
        if success:
            shutil.rmtree(temporary_root)
        else:
            sys.stderr.write(f"Smoke artifacts preserved at: {temporary_root}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Smoke-test one APSGo V7 Windows package.")
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--package-root", required=True)
    parser.add_argument("--startup-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=10.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = run_smoke(
            arguments.repository_root,
            arguments.package_root,
            startup_timeout_seconds=arguments.startup_timeout_seconds,
            request_timeout_seconds=arguments.request_timeout_seconds,
        )
    except Exception as error:
        sys.stderr.write(f"APSGo V7 release smoke failed: {error}\n")
        return 1
    sys.stdout.write(f"{dumps_exact_json(result)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
