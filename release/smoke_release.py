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
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
from math import isfinite
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_scheduler.api.rule_management import (
    SetActiveRulesRequest,
    dumps_set_active_rules_request,
)
from apsgo_v7_service.app import GET_ACTIVE_RULES_PATH, MONTH_SOLVE_PATH, SET_ACTIVE_RULES_PATH
from apsgo_v7_service.configuration import load_service_configuration
from apsgo_v7_service.gqga4 import GQGA4_INITIAL_RULES, GQGA4_INITIAL_VIRTUAL_PROTOTYPES
from apsgo_v7_service.month_scheduling import MONTH_SOLVE_CONTRACT_VERSION

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


def _write_smoke_configuration(
    template: Path, destination: Path, port: int, *, bridge_runtime: Path | None = None,
):
    text = template.read_text(encoding="utf-8")
    host_pattern = r"(?m)^listen_host:\s*.*$"
    port_pattern = r"(?m)^listen_port:\s*.*$"
    _require(len(re.findall(host_pattern, text)) == 1, "listen_host must occur once")
    _require(len(re.findall(port_pattern, text)) == 1, "listen_port must occur once")
    text = re.sub(host_pattern, "listen_host: 127.0.0.1", text)
    text = re.sub(port_pattern, f"listen_port: {port}", text)
    if bridge_runtime is not None:
        # These are owned temporary paths; retain the template's complete solver policy.
        database = json.dumps(str(bridge_runtime / "rules.sqlite3"))
        diagnostics = json.dumps(str(bridge_runtime / "diagnostics"))
        text, count = re.subn(r"(?m)^database_path:\s*.*$", lambda _: f"database_path: {database}", text)
        _require(count == 1, "database_path must occur once")
        text = re.sub(r"(?m)^diagnostics:\s*\n(?:[ \t]+[^\n]*\n)*", "", text)
        text = text.rstrip() + f"\ndiagnostics:\n  enabled: true\n  output_directory: {diagnostics}\n"
    destination.write_text(text, encoding="utf-8", newline="\n")
    configuration = load_service_configuration(destination)
    _require(configuration.listen_host == "127.0.0.1", "smoke host is not loopback")
    _require(configuration.listen_port == port, "smoke port changed")
    return configuration


def _start(
    package: Path, log_path: Path, *, configuration: Path | None = None,
) -> tuple[subprocess.Popen, object]:
    log = log_path.open("ab")
    try:
        command = [os.environ.get("ComSpec", "cmd.exe"), "/d", "/c",
                   str(package / "start_apsgo_v7_service.bat")]
        environment = None
        if configuration is not None:
            command = [str(package / "APSGoV7Service.exe"), "--config", str(configuration)]
            system_root = os.environ.get("SystemRoot")
            _require(bool(system_root), "SystemRoot is unavailable for isolated EXE smoke")
            environment = {key: value for key, value in os.environ.items()
                           if key.upper() != "PATH"
                           and not key.upper().startswith(("PYTHON", "CONDA", "NUMBA"))}
            environment["PATH"] = os.pathsep.join((str(Path(system_root) / "System32"), system_root))
        process = subprocess.Popen(
            command,
            cwd=package,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=environment,
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


def _record_json(path: Path, value) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(dumps_exact_json(value) + "\n")


def _controlled_rules_request(active_version: int) -> dict:
    value = SetActiveRulesRequest(
        str(uuid.uuid4()), active_version, GQGA4_INITIAL_RULES,
        GQGA4_INITIAL_VIRTUAL_PROTOTYPES, "Disposable numeric bridge release smoke",
    )
    return json.loads(dumps_set_active_rules_request(value), parse_float=Decimal)


def _bridge_request(active_version: int, mode: str) -> dict:
    _require(mode in {"single", "double"}, "unknown bridge smoke mode")
    thicknesses = ("0.6", "1.0") if mode == "single" else ("0.4", "0.8")
    return {
        "contract_version": MONTH_SOLVE_CONTRACT_VERSION,
        "request_id": str(uuid.uuid4()),
        "expected_active_version_id": active_version,
        "periods": [{"period_id": "P0", "sequence": 0}],
        "orders": [
            {
                "source_order_id": name, "source_period": "P0", "is_virtual": False,
                "weight": Decimal(600), "grade": "DC01", "grade_class": "普通钢",
                "hot_roll_grade": "DC01", "width": Decimal(1000),
                "thickness": Decimal(thickness), "min_temperature": Decimal(700),
                "max_temperature": Decimal(800), "customer_grade": "战略客户",
                "customer_name": None, "execution_standard": None, "surface_grade": None,
            }
            for name, thickness in zip(("thin", "thick"), thicknesses)
        ],
    }


def _assert_bridge_result(response: dict, request: dict, mode: str) -> None:
    _require(response.get("contract_version") == MONTH_SOLVE_CONTRACT_VERSION, "solve contract changed")
    _require(response.get("request_id") == request["request_id"], "solve request identity changed")
    _require(response.get("active_rule_set_version_id") == request["expected_active_version_id"],
             "solve did not use the controlled active version")
    _require(response.get("status") == "success" and response.get("publishable") is True,
             "controlled bridge result is not publishable")
    _require(response.get("stop_reason") == "local_search_complete", "controlled solve was truncated")
    _require(response.get("issues") == [] and response.get("violations") == [],
             "controlled bridge solve has issues or violations")
    audit = response["audit_summary"]
    _require(audit["passed"] is True and audit["core"]["passed"] is True
             and audit["result"]["passed"] is True, "controlled bridge audits did not pass")
    _require(response["preparation_report"]["matched_order_count"] == 2
             and response["preparation_report"]["missing_order_count"] == 0,
             "controlled DC01 orders require an enabled dictionary entry")
    prototypes = ["0.8"] if mode == "single" else ["0.6", "0.5"]
    thicknesses = [request["orders"][1]["thickness"],
                   *(Decimal(value) for value in prototypes), request["orders"][0]["thickness"]]
    rows = response["rows"]
    _require(len(rows) == len(thicknesses), "controlled bridge node count changed")
    _require([row["source_order_id"] for row in rows] == ["thick", *([None] * len(prototypes)), "thin"],
             "controlled bridge real-node order changed")
    _require(len({row["chain_id"] for row in rows}) == 1, "controlled bridge did not merge the chains")
    for index, (row, thickness) in enumerate(zip(rows, thicknesses), start=1):
        virtual = 1 < index < len(rows)
        _require(row["node_sequence"] == index and row["chain_sequence"] == 1
                 and row["assigned_period"] == "P0", "controlled bridge order/period changed")
        _require(row["width"] == 1000 and row["thickness"] == thickness
                 and row["min_temperature"] == 700 and row["max_temperature"] == 800,
                 "controlled bridge geometry changed")
        _require(row["weight"] == (20 if virtual else 600) and row["split_lineage"] is None,
                 "controlled bridge weight or split changed")
        if virtual:
            _require(row["node_id"] == f"virtual-{index - 1:06d}"
                     and row["material_role"] == "virtual_sphc" and row["grade"] == "SPHC",
                     "controlled bridge virtual identity changed")
            _require(row["virtual_lineage"] == {
                "prototype_id": f"virtual_sphc:1000x{prototypes[index - 2]}",
                "purpose": "edge_bridge", "related_partition_id": None,
                "accepted_sequence": index - 1,
            }, "controlled bridge prototype or lineage changed")
        else:
            _require(row["material_role"] == "normal_real" and row["virtual_lineage"] is None,
                     "controlled real-node role changed")
    expected_quality = [0, 0, 0, 0, 0, 20 * len(prototypes), 1]
    _require([item["value"] for item in response["quality"]] == expected_quality,
             "controlled bridge quality changed")


def _assert_bridge_log(text: str, request_id: str, mode: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        event = re.search(r"\bsolver_bridge_numeric_(start|ready)\b", line)
        if event is None:
            continue
        fields = dict(re.findall(r"\b(\w+)=([^\s]+)", line))
        _require(fields.get("request_id") == request_id, "numeric event belongs to another request")
        value = {"event": event[0]}
        if event[1] == "ready":
            _require(fields.get("nopython") == "True" and fields.get("found") in {"True", "False"},
                     "numeric ready event lacks actual nopython/found proof")
            value.update(mode=fields.get("mode"), nopython=True, found=fields["found"] == "True")
        events.append(value)
    expected = [
        {"event": "solver_bridge_numeric_start"},
        {"event": "solver_bridge_numeric_ready", "mode": "single", "nopython": True,
         "found": mode == "single"},
    ]
    if mode == "double":
        expected.append({"event": "solver_bridge_numeric_ready", "mode": "double",
                         "nopython": True, "found": True})
    _require(events == expected, "controlled bridge did not execute the expected native modes")
    return events


@contextmanager
def _readonly_installation(package: Path, evidence: Path):
    """Deny writes on the owned copy with Windows DACLs, then restore its original ACLs."""
    _require(os.name == "nt" and package.parent == evidence.parent
             and package.name == "APSGo V7 package", "ACL target must be the disposable package")
    system_root = os.environ.get("SystemRoot")
    _require(bool(system_root), "SystemRoot is unavailable for ACL validation")
    executable = str(Path(system_root) / "System32" / "icacls.exe")
    backup = evidence / "installation_acl.txt"

    def invoke(label, arguments):
        result = subprocess.run([executable, *arguments], cwd=package.parent,
                                capture_output=True, check=False, timeout=60)
        (evidence / f"acl_{label}.log").write_bytes(result.stdout + result.stderr)
        _require(result.returncode == 0, f"Windows installation ACL {label} failed")

    invoke("save", [package.name, "/save", str(backup), "/T", "/Q"])
    try:
        # Explicit deny on every existing object; keep read/execute and owner ACL restoration.
        invoke("deny", [package.name, "/deny", "*S-1-1-0:(WD,AD,WEA,WA,DE,DC)", "/T", "/Q"])
        invoke("verify", [package.name, "/verify", "/T", "/Q"])
        for directory in (package, package / "_internal"):
            probe = directory / ".apsgo-write-probe"
            try:
                with probe.open("xb"):
                    pass
            except PermissionError:
                continue
            raise SmokeError(f"installation directory remains writable: {directory}")
        try:
            with (package / "APSGoV7Service.exe").open("r+b"):
                pass
        except PermissionError:
            pass
        else:
            raise SmokeError("installation executable remains writable")
        yield
    finally:
        invoke("restore", [".", "/restore", str(backup), "/Q"])


def _run_bridge_requests(base_url, configuration, active, evidence, label, *, request_timeout_seconds):
    results = []
    for mode in ("single", "double"):
        request = _bridge_request(active["active_version_id"], mode)
        stem = f"{label}_{mode}"
        _record_json(evidence / f"{stem}_request.json", request)
        started = time.perf_counter()
        status, response = _http_json(
            base_url, "POST", MONTH_SOLVE_PATH, request,
            timeout_seconds=float(configuration.monthly_solve_policy.total_time_limit_seconds)
            + request_timeout_seconds,
        )
        elapsed = Decimal(str(time.perf_counter() - started))
        _record_json(evidence / f"{stem}_response.json", response)
        _require(status == 200, f"controlled {mode} bridge returned HTTP {status}: {response}")
        _assert_bridge_result(response, request, mode)
        parent = configuration.diagnostics_directory / "runs" / request["request_id"]
        deadline = time.monotonic() + request_timeout_seconds
        while not list(parent.glob("*/diagnostic_summary.json")):
            _require(time.monotonic() < deadline, "controlled bridge diagnostics did not finish")
            time.sleep(0.05)
        summaries = list(parent.glob("*/diagnostic_summary.json"))
        _require(len(summaries) == 1, "controlled request has ambiguous diagnostic runs")
        run = summaries[0].parent
        events = _assert_bridge_log((run / "solve.log").read_text(encoding="utf-8"), request["request_id"], mode)
        results.append({"request_id": request["request_id"], "phase": label, "mode": mode, "events": events,
                        "elapsed_seconds": elapsed, "diagnostic_directory": str(run),
                        "response_sha256": _sha256(evidence / f"{stem}_response.json")})
    return results


def _run_numeric_smoke(package, temporary_root, evidence, *, startup_timeout_seconds, request_timeout_seconds):
    runtime = temporary_root / "bridge_runtime"
    runtime.mkdir()
    shutil.copy2(package / PACKAGE_DATABASE_PATH, runtime / "rules.sqlite3")
    seed_sha256 = _sha256(runtime / "rules.sqlite3")
    config_path = runtime / "service.yaml"
    configuration = _write_smoke_configuration(
        package / PACKAGE_CONFIGURATION_PATH, config_path, _available_loopback_port(), bridge_runtime=runtime,
    )
    base_url = f"http://127.0.0.1:{configuration.listen_port}"
    process = log = None
    samples = []
    log_path = evidence / "numeric_service.log"
    with _readonly_installation(package, evidence):
        try:
            process, log = _start(package, log_path, configuration=config_path)
            active = _wait_for_rules(process, base_url, log_path,
                                    startup_timeout_seconds=startup_timeout_seconds,
                                    request_timeout_seconds=request_timeout_seconds)
            controlled = _controlled_rules_request(active["active_version_id"])
            _record_json(evidence / "controlled_rules_request.json", controlled)
            status, saved = _http_json(base_url, "POST", SET_ACTIVE_RULES_PATH, controlled,
                                       timeout_seconds=request_timeout_seconds)
            _record_json(evidence / "controlled_rules_response.json", saved)
            _require(status == 200, f"controlled rules POST failed: {status}: {saved}")
            status, following = _http_json(base_url, "GET", GET_ACTIVE_RULES_PATH,
                                           timeout_seconds=request_timeout_seconds)
            _require(status == 200, "controlled rules GET failed")
            _assert_saved(saved, following, controlled["save_operation_id"], active["active_version_id"])
            replay = _request_from_active(following, controlled["save_operation_id"])
            _require(all(replay[key] == controlled[key] for key in ("rules", "virtual_prototypes")),
                     "controlled rules did not roundtrip")
            active = following
            controlled_database_sha256 = _sha256(configuration.database_path)
            _require("solver_bridge_numeric_" not in log_path.read_text(encoding="utf-8", errors="replace"),
                     "rule management triggered numerical compilation")
            for label in ("first", "repeat"):
                samples.extend(_run_bridge_requests(base_url, configuration, active, evidence, label,
                               request_timeout_seconds=request_timeout_seconds))
            _stop(package, process, log)
            process = log = None
            process, log = _start(package, log_path, configuration=config_path)
            restarted = _wait_for_rules(process, base_url, log_path,
                                       startup_timeout_seconds=startup_timeout_seconds,
                                       request_timeout_seconds=request_timeout_seconds)
            _require(restarted == active, "controlled rules changed across restart")
            samples.extend(_run_bridge_requests(base_url, configuration, active, evidence, "restart",
                           request_timeout_seconds=request_timeout_seconds))
            _stop(package, process, log)
            process = log = None
            _reject_database_sidecars(runtime, configuration.database_path)
            _require(_sha256(configuration.database_path) == controlled_database_sha256,
                     "bridge solving changed its controlled runtime database")
        finally:
            if process is not None and log is not None:
                _stop(package, process, log)
    _record_json(evidence / "numeric_smoke.json", {
        "status": "pass", "readonly_installation_acl_verified": True,
        "executable_python_path_removed": True, "samples": samples,
        "seed_sha256": seed_sha256, "controlled_database_sha256": controlled_database_sha256,
        "timing_scope": "small API observations; first request per process includes cold JIT, not paired acceptance",
    })
    return samples


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
    evidence = temporary_root / "evidence"
    evidence.mkdir()
    shutil.copy2(package / MANIFEST_PATH, evidence / MANIFEST_PATH)
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
        (evidence / "help_stdout.log").write_bytes(help_result.stdout)
        (evidence / "help_stderr.log").write_bytes(help_result.stderr)
        _require(help_result.returncode == 0, "packaged executable --help failed")
        _require(b"solver_bridge_numeric_" not in help_result.stdout + help_result.stderr,
                 "help triggered numeric bridge execution")
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
        _require("solver_bridge_numeric_" not in log_path.read_text(encoding="utf-8", errors="replace"),
                 "normal startup or rule management triggered numeric bridge execution")
        shutil.copy2(log_path, evidence / "normal_service.log")
        _record_json(evidence / "normal_rule_roundtrip.json", {
            "initial": initial, "saved": saved, "restarted": restarted, "runtime_identity": runtime,
        })
        numeric_samples = _run_numeric_smoke(
            smoke_package, temporary_root, evidence,
            startup_timeout_seconds=startup_timeout_seconds,
            request_timeout_seconds=request_timeout_seconds,
        )
        for relative, digest in manifest["files"].items():
            _require(_sha256(smoke_package / relative) == digest, f"smoke installation changed: {relative}")
        _require(not any(path.suffix.lower() in {".nbi", ".nbc"}
                         for directory in (smoke_package, temporary_root / "bridge_runtime")
                         for path in directory.rglob("*")),
                 "smoke installation or writable runtime contains Numba disk cache")
        _require(_sha256(smoke_package / PACKAGE_DATABASE_PATH) == seed_sha256, "smoke seed changed")
        _require(_sha256(source_database) == source_sha256, "source database changed")
        _require(_sha256(formal_seed) == seed_sha256, "formal package seed changed")
        _require(
            _sha256(repository / CONFIGURATION_PATH) == configuration_sha256,
            "source configuration changed",
        )
        verify_package(package, allow_pending_smoke=True)
        result = {
            "status": "pass",
            "mode": "smoke",
            "initial_active_version_id": initial["active_version_id"],
            "saved_active_version_id": active["active_version_id"],
            "restarted": True,
            "package_file_count": package_result["file_count"],
            "numeric_bridge_request_count": len(numeric_samples),
            "readonly_installation_acl_verified": True,
            "evidence_directory": str(temporary_root),
            "windows_host_validation": "EXE smoke on the executing Windows host; not a second-machine acceptance",
        }
        _record_json(evidence / "smoke_summary.json", result)
        success = True
        return result
    finally:
        if process is not None and log is not None:
            try:
                _stop(smoke_package, process, log)
            except Exception as error:
                sys.stderr.write(f"Emergency service stop failed: {error}\n")
        if success:
            # Keep small logs, exact requests/responses, ACLs and the disposable runtime DB.
            shutil.rmtree(smoke_package)
            sys.stderr.write(f"Smoke evidence preserved at: {temporary_root}\n")
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
