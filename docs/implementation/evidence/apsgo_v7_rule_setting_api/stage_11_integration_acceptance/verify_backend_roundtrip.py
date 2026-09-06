#!/usr/bin/env python3
"""Exercise the production rule API through a real loopback Uvicorn process."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[5]
SRC = ROOT / "src"
sys.dont_write_bytecode = True
for directory in (ROOT, SRC):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from apsgo_scheduler.api.json_codec import dumps_exact_json  # noqa: E402
from apsgo_v7_service.app import (  # noqa: E402
    GET_ACTIVE_RULES_PATH,
    SET_ACTIVE_RULES_PATH,
)
from apsgo_v7_service.rule_store import DEFAULT_DATABASE_PATH  # noqa: E402

CHAIN_WEIGHT_RULE_ID = "chain_weight_range"
OPERATION_CHAIN_WEIGHT = "11111111-1111-4111-8111-111111111111"
OPERATION_STALE = "22222222-2222-4222-8222-222222222222"
OPERATION_INVALID = "33333333-3333-4333-8333-333333333333"
OPERATION_EMPTY_PROTOTYPES = "44444444-4444-4444-8444-444444444444"
OPERATION_RESTORE_PROTOTYPES = "55555555-5555-4555-8555-555555555555"
OPERATION_PRECISE_SEQUENCES = "66666666-6666-4666-8666-666666666666"
OPERATION_HISTORICAL_RESTORE = "77777777-7777-4777-8777-777777777777"
THICKNESS_RULE_ID = "thickness_jump_limit"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _path_family_state(path: Path) -> dict[str, str | None]:
    return {
        str(candidate.relative_to(ROOT)): _sha256(candidate) if candidate.is_file() else None
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
    }


def _database_state(path: Path) -> dict[str, int | str]:
    with sqlite3.connect(path) as connection:
        active_version_id = connection.execute(
            "SELECT active_version_id FROM v7_rule_set"
        ).fetchone()[0]
        return {
            "version_count": connection.execute(
                "SELECT COUNT(*) FROM v7_rule_set_version"
            ).fetchone()[0],
            "rule_definition_count": connection.execute(
                "SELECT COUNT(*) FROM v7_rule_definition"
            ).fetchone()[0],
            "active_version_id": active_version_id,
        }


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _http_json(
    base_url: str,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    payload = None if body is None else dumps_exact_json(body).encode("utf-8")
    headers = {} if body is None else {"Content-Type": "application/json"}
    request = Request(base_url + path, data=payload, headers=headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            status = response.status
            content = response.read()
    except HTTPError as error:
        status = error.code
        content = error.read()
    decoded = json.loads(content.decode("utf-8"), parse_float=Decimal)
    _require(isinstance(decoded, dict), "HTTP response must be a JSON object")
    return status, decoded


def _wait_for_server(process: subprocess.Popen, base_url: str, log_path: Path) -> dict:
    deadline = time.monotonic() + 15
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            status, body = _http_json(base_url, "GET", GET_ACTIVE_RULES_PATH)
            _require(status == 200, f"initial GET returned HTTP {status}")
            return body
        except (ConnectionError, TimeoutError, URLError) as error:
            last_error = error
            time.sleep(0.05)
    logs = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    raise RuntimeError(f"Uvicorn did not become ready: {last_error}; log={logs}")


def _request_from_active(active: dict, operation_id: str, remark: str) -> dict:
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
        "remark": remark,
    }


def _rule(request: dict, rule_id: str) -> dict:
    matches = [rule for rule in request["rules"] if rule["rule_id"] == rule_id]
    _require(len(matches) == 1, f"expected exactly one rule: {rule_id}")
    return matches[0]


def _error(body: dict, expected_code: str) -> dict:
    error = body.get("error")
    _require(isinstance(error, dict), "error response is missing the error object")
    _require(error.get("code") == expected_code, f"expected error code {expected_code}")
    return error


def _assert_active_matches_save(
    active: dict,
    saved: dict,
    operation_id: str,
    previous_active_version_id: int,
) -> None:
    _require(saved["save_operation_id"] == operation_id, "POST operation id changed")
    _require(
        saved["previous_active_version_id"] == previous_active_version_id,
        "POST previous active version changed",
    )
    _require(
        saved["saved_version_id"] == active["active_version_id"]
        and saved["active_version_id"] == active["active_version_id"],
        "POST saved and active version identities differ",
    )
    _require(saved["saved_version_is_active"] is True, "new saved version is not active")
    _require(saved["idempotent_replay"] is False, "new save was marked as a replay")
    _require(
        all(saved.get(name) == value for name, value in active.items()),
        "POST active snapshot and subsequent GET differ",
    )


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _run_isolated() -> dict[str, object]:
    temporary_database: Path | None = None

    with TemporaryDirectory(prefix="apsgo-v7-stage-11-") as directory:
        temporary_database = Path(directory) / "rules.sqlite3"
        backup_database = Path(directory) / "rules.initial.backup.sqlite3"
        restored_backup_database = Path(directory) / "rules.restored.sqlite3"
        log_path = Path(directory) / "uvicorn.log"
        environment = os.environ.copy()
        environment.update(
            {
                "APSGO_V7_RULE_DB_PATH": str(temporary_database),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": os.pathsep.join(
                    part
                    for part in (str(SRC), str(ROOT), environment.get("PYTHONPATH", ""))
                    if part
                ),
            }
        )
        initialized = subprocess.run(
            [sys.executable, "-m", "apsgo_v7_service.initialize_gqga4_rules"],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        _require(
            initialized.returncode == 0,
            f"database initialization failed: {initialized.stderr[-2000:]}",
        )
        initialization = json.loads(initialized.stdout.strip(), parse_float=Decimal)
        _require(initialization["status"] == "initialized", "temporary database was not new")
        with (
            sqlite3.connect(temporary_database) as source,
            sqlite3.connect(backup_database) as backup,
        ):
            source.backup(backup)
        initial_backup_state = _database_state(backup_database)
        _require(
            initial_backup_state
            == {
                "version_count": 1,
                "rule_definition_count": 17,
                "active_version_id": initialization["active_version_id"],
            },
            "initial database backup is incomplete",
        )

        port = _free_loopback_port()
        base_url = f"http://127.0.0.1:{port}"
        with log_path.open("w+", encoding="utf-8") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    f"from apsgo_v7_service.app import run_server; run_server(port={port})",
                ],
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                initial = _wait_for_server(process, base_url, log_path)
                _require(len(initial["rules"]) == 17, "initial response must contain 17 rules")
                _require(
                    sum(rule["enabled"] for rule in initial["rules"]) == 16,
                    "initial response must contain 16 enabled rules",
                )
                _require(
                    len(initial["quality_spec"]) == 7,
                    "initial response must contain seven quality criteria",
                )
                original_prototypes = deepcopy(initial["virtual_prototypes"])
                _require(len(original_prototypes) == 27, "initial response must have 27 prototypes")

                chain_request = _request_from_active(
                    initial, OPERATION_CHAIN_WEIGHT, "阶段 11 链重修改"
                )
                changed_minimum = Decimal("700.25")
                _rule(chain_request, CHAIN_WEIGHT_RULE_ID)["parameters"]["min_weight"] = (
                    changed_minimum
                )
                status, chain_saved = _http_json(
                    base_url, "POST", SET_ACTIVE_RULES_PATH, chain_request
                )
                _require(status == 200, f"chain-weight POST returned HTTP {status}")
                status, chain_current = _http_json(base_url, "GET", GET_ACTIVE_RULES_PATH)
                _require(status == 200, f"chain-weight GET returned HTTP {status}")
                _assert_active_matches_save(
                    chain_current,
                    chain_saved,
                    OPERATION_CHAIN_WEIGHT,
                    initial["active_version_id"],
                )
                _require(
                    _rule(
                        _request_from_active(chain_current, OPERATION_STALE, ""),
                        CHAIN_WEIGHT_RULE_ID,
                    )["parameters"]["min_weight"]
                    == changed_minimum,
                    "chain-weight decimal did not round-trip",
                )
                _require(
                    chain_current["active_version_id"] != initial["active_version_id"],
                    "chain-weight save did not activate a new version",
                )

                state_after_chain = _database_state(temporary_database)
                status, replay = _http_json(base_url, "POST", SET_ACTIVE_RULES_PATH, chain_request)
                _require(status == 200, f"idempotent replay returned HTTP {status}")
                _require(replay["idempotent_replay"] is True, "replay was not marked idempotent")
                _require(
                    replay["saved_version_id"] == chain_saved["saved_version_id"],
                    "replay did not return the original saved version",
                )
                _require(
                    _database_state(temporary_database) == state_after_chain,
                    "idempotent replay created a database version",
                )

                stale_request = _request_from_active(initial, OPERATION_STALE, "阶段 11 过期页面")
                status, stale_response = _http_json(
                    base_url, "POST", SET_ACTIVE_RULES_PATH, stale_request
                )
                stale_error = _error(stale_response, "active_version_conflict")
                _require(status == 409, f"stale save returned HTTP {status}")
                _require(
                    stale_error["expected_active_version_id"] == initial["active_version_id"]
                    and stale_error["current_active_version_id"]
                    == chain_current["active_version_id"],
                    "409 response did not identify expected and current versions",
                )
                _require(
                    _database_state(temporary_database) == state_after_chain,
                    "409 conflict changed the database",
                )

                invalid_request = _request_from_active(
                    chain_current, OPERATION_INVALID, "阶段 11 非法参数"
                )
                _rule(invalid_request, CHAIN_WEIGHT_RULE_ID)["parameters"]["min_weight"] = Decimal(
                    "2001"
                )
                status, invalid_response = _http_json(
                    base_url, "POST", SET_ACTIVE_RULES_PATH, invalid_request
                )
                invalid_error = _error(invalid_response, "rule_set_validation_failed")
                _require(status == 422, f"invalid save returned HTTP {status}")
                _require(bool(invalid_error["issues"]), "422 response has no field-level issue")
                _require(
                    _database_state(temporary_database) == state_after_chain,
                    "422 validation failure changed the database",
                )

                precise_request = _request_from_active(
                    chain_current,
                    OPERATION_PRECISE_SEQUENCES,
                    "阶段 11 有序小数参数",
                )
                precise_ranges = _rule(precise_request, THICKNESS_RULE_ID)["parameters"]["ranges"]
                precise_ranges[1]["tolerance"] = Decimal("0.200125")
                original_range_order = [(item["min"], item["max"]) for item in precise_ranges]
                original_prototype_order = [
                    item["prototype_id"] for item in precise_request["virtual_prototypes"]
                ]
                for prototype in precise_request["virtual_prototypes"]:
                    prototype["unit_weight"] = Decimal("20.125")
                status, precise_saved = _http_json(
                    base_url, "POST", SET_ACTIVE_RULES_PATH, precise_request
                )
                _require(status == 200, f"precise-sequence POST returned HTTP {status}")
                status, precise_current = _http_json(base_url, "GET", GET_ACTIVE_RULES_PATH)
                _require(status == 200, f"precise-sequence GET returned HTTP {status}")
                _assert_active_matches_save(
                    precise_current,
                    precise_saved,
                    OPERATION_PRECISE_SEQUENCES,
                    chain_current["active_version_id"],
                )
                returned_ranges = _rule(
                    _request_from_active(precise_current, OPERATION_STALE, ""),
                    THICKNESS_RULE_ID,
                )["parameters"]["ranges"]
                _require(
                    [(item["min"], item["max"]) for item in returned_ranges] == original_range_order
                    and returned_ranges[1]["tolerance"] == Decimal("0.200125"),
                    "thickness ranges lost order or decimal precision",
                )
                _require(
                    [item["prototype_id"] for item in precise_current["virtual_prototypes"]]
                    == original_prototype_order
                    and all(
                        item["unit_weight"] == Decimal("20.125")
                        for item in precise_current["virtual_prototypes"]
                    ),
                    "virtual prototypes lost order or decimal precision",
                )

                empty_request = _request_from_active(
                    precise_current, OPERATION_EMPTY_PROTOTYPES, "阶段 11 关闭虚拟材料"
                )
                empty_request["virtual_prototypes"] = []
                status, empty_saved = _http_json(
                    base_url, "POST", SET_ACTIVE_RULES_PATH, empty_request
                )
                _require(status == 200, f"empty-prototype POST returned HTTP {status}")
                status, empty_current = _http_json(base_url, "GET", GET_ACTIVE_RULES_PATH)
                _require(status == 200, f"empty-prototype GET returned HTTP {status}")
                _assert_active_matches_save(
                    empty_current,
                    empty_saved,
                    OPERATION_EMPTY_PROTOTYPES,
                    precise_current["active_version_id"],
                )
                _require(
                    empty_current["virtual_prototypes"] == [],
                    "disabled virtual material must persist an empty prototype collection",
                )

                restore_request = _request_from_active(
                    empty_current,
                    OPERATION_RESTORE_PROTOTYPES,
                    "阶段 11 恢复虚拟材料",
                )
                restore_request["virtual_prototypes"] = original_prototypes
                status, restore_saved = _http_json(
                    base_url, "POST", SET_ACTIVE_RULES_PATH, restore_request
                )
                _require(status == 200, f"prototype restore POST returned HTTP {status}")
                status, restored = _http_json(base_url, "GET", GET_ACTIVE_RULES_PATH)
                _require(status == 200, f"prototype restore GET returned HTTP {status}")
                _assert_active_matches_save(
                    restored,
                    restore_saved,
                    OPERATION_RESTORE_PROTOTYPES,
                    empty_current["active_version_id"],
                )
                _require(
                    restored["virtual_prototypes"] == original_prototypes,
                    "restored prototype metadata differs from the original snapshot",
                )

                final_database_state = _database_state(temporary_database)
                _require(
                    final_database_state
                    == {
                        "version_count": 5,
                        "rule_definition_count": 85,
                        "active_version_id": restored["active_version_id"],
                    },
                    "final database version or rule counts are incorrect",
                )
                status, historical_replay = _http_json(
                    base_url, "POST", SET_ACTIVE_RULES_PATH, chain_request
                )
                _require(status == 200, f"historical replay returned HTTP {status}")
                _require(
                    historical_replay["idempotent_replay"] is True
                    and historical_replay["saved_version_id"] == chain_saved["saved_version_id"]
                    and historical_replay["saved_version_is_active"] is False
                    and historical_replay["active_version_id"] == restored["active_version_id"],
                    "historical replay changed or reactivated the saved version",
                )
                _require(
                    _database_state(temporary_database) == final_database_state,
                    "historical replay changed the database",
                )
                operation_conflict_request = deepcopy(chain_request)
                operation_conflict_request["remark"] = "阶段 11 复用操作标识但改变内容"
                status, operation_conflict_response = _http_json(
                    base_url,
                    "POST",
                    SET_ACTIVE_RULES_PATH,
                    operation_conflict_request,
                )
                operation_conflict_error = _error(
                    operation_conflict_response, "operation_payload_conflict"
                )
                _require(status == 409, f"operation conflict returned HTTP {status}")
                _require(
                    _database_state(temporary_database) == final_database_state,
                    "operation conflict changed the database",
                )
            finally:
                _stop_process(process)

        restore_command = [
            sys.executable,
            "-m",
            "apsgo_v7_service.restore_gqga4_rule_version",
            "--database-path",
            str(temporary_database),
            "--source-version-id",
            str(initial["active_version_id"]),
            "--save-operation-id",
            OPERATION_HISTORICAL_RESTORE,
            "--expected-active-version-id",
            str(restored["active_version_id"]),
        ]
        restored_process = subprocess.run(
            restore_command,
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        _require(
            restored_process.returncode == 0,
            f"historical restore failed: {restored_process.stderr[-2000:]}",
        )
        historical_restore = json.loads(restored_process.stdout, parse_float=Decimal)
        _require(historical_restore["status"] == "restored", "restore was not newly applied")
        restore_replay_process = subprocess.run(
            restore_command,
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        _require(
            restore_replay_process.returncode == 0,
            f"historical restore replay failed: {restore_replay_process.stderr[-2000:]}",
        )
        historical_restore_replay = json.loads(restore_replay_process.stdout, parse_float=Decimal)
        _require(
            historical_restore_replay["status"] == "idempotent_replay"
            and historical_restore_replay["saved_version_id"]
            == historical_restore["saved_version_id"],
            "historical restore replay created or selected another version",
        )
        state_after_restore = _database_state(temporary_database)
        _require(
            state_after_restore
            == {
                "version_count": 6,
                "rule_definition_count": 102,
                "active_version_id": historical_restore["saved_version_id"],
            },
            "historical restore database state is incorrect",
        )

        shutil.copyfile(backup_database, restored_backup_database)
        backup_environment = environment | {"APSGO_V7_RULE_DB_PATH": str(restored_backup_database)}
        backup_check_process = subprocess.run(
            [sys.executable, "-m", "apsgo_v7_service.initialize_gqga4_rules"],
            cwd=ROOT,
            env=backup_environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        _require(
            backup_check_process.returncode == 0,
            f"restored backup validation failed: {backup_check_process.stderr[-2000:]}",
        )
        backup_check = json.loads(backup_check_process.stdout, parse_float=Decimal)
        _require(
            backup_check["status"] == "already_initialized"
            and _database_state(restored_backup_database) == initial_backup_state,
            "restored backup differs from the initial database",
        )

        summary = {
            "passed": True,
            "transport": "real_uvicorn_subprocess_on_127.0.0.1",
            "initialization": initialization["status"],
            "initial_version_id": initial["active_version_id"],
            "chain_weight": {
                "min_weight": changed_minimum,
                "active_version_id": chain_current["active_version_id"],
                "fingerprint": chain_current["fingerprint"],
            },
            "idempotent_replay": {
                "status": 200,
                "saved_version_id": replay["saved_version_id"],
                "created_another_version": False,
            },
            "conflict": {
                "status": 409,
                "code": stale_error["code"],
                "database_unchanged": True,
            },
            "validation": {
                "status": 422,
                "code": invalid_error["code"],
                "first_issue_path": invalid_error["issues"][0]["field_path"],
                "database_unchanged": True,
            },
            "virtual_prototypes": {
                "empty_count": len(empty_current["virtual_prototypes"]),
                "restored_count": len(restored["virtual_prototypes"]),
                "metadata_restored_exactly": True,
            },
            "ordered_decimal_roundtrip": {
                "thickness_tolerance": returned_ranges[1]["tolerance"],
                "prototype_unit_weight": precise_current["virtual_prototypes"][0]["unit_weight"],
                "thickness_range_count": len(returned_ranges),
                "prototype_count": len(precise_current["virtual_prototypes"]),
            },
            "historical_replay": {
                "status": 200,
                "saved_version_is_active": False,
                "current_active_version_id": historical_replay["active_version_id"],
                "created_another_version": False,
            },
            "operation_payload_conflict": {
                "status": 409,
                "code": operation_conflict_error["code"],
                "database_unchanged": True,
            },
            "database": final_database_state,
            "historical_restore": {
                "status": historical_restore["status"],
                "replay_status": historical_restore_replay["status"],
                "source_version_id": historical_restore["source_version_id"],
                "saved_version_id": historical_restore["saved_version_id"],
                "database": state_after_restore,
            },
            "database_backup_restore": {
                "status": backup_check["status"],
                "database": initial_backup_state,
            },
        }

    _require(
        temporary_database is not None and not temporary_database.exists(),
        "temporary database remains",
    )
    summary["temporary_database_removed"] = True
    return summary


def _run() -> dict[str, object]:
    default_database = ROOT / DEFAULT_DATABASE_PATH
    default_before = _path_family_state(default_database)
    try:
        summary = _run_isolated()
    finally:
        default_after = _path_family_state(default_database)
        _require(
            default_after == default_before,
            "default rule database was created or modified",
        )
    summary["default_database_unchanged"] = True
    summary["default_database_file_count_before"] = sum(
        value is not None for value in default_before.values()
    )
    summary["default_database_file_count_after"] = sum(
        value is not None for value in default_after.values()
    )
    return summary


def main() -> int:
    try:
        summary = _run()
    except Exception as error:
        print(
            dumps_exact_json(
                {
                    "passed": False,
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            )
        )
        return 2
    print(dumps_exact_json(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
