"""Source checks for release smoke; these do not substitute for a Windows EXE run."""

import json
import logging
import subprocess
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_scheduler.core import _bridge_numeric as numeric
from apsgo_v7_service import app as http_module
from apsgo_v7_service.configuration import load_service_configuration
from apsgo_v7_service.rule_management import initialize_gqga4_rules
from release import smoke_release as smoke
from tests.service.grade_dictionary_support import sample_grade_dictionary
from tests.service.test_month_scheduling import policy


def post(client, path, request):
    response = client.post(
        path,
        content=dumps_exact_json(request).encode("utf-8"),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200, response.text
    return json.loads(response.content, parse_float=Decimal)


@pytest.fixture
def controlled_database(tmp_path):
    database = tmp_path / "rules.sqlite3"
    initialize_gqga4_rules(database, initial_grade_dictionary=sample_grade_dictionary())
    with TestClient(http_module.create_app(database)) as client:
        saved = post(client, http_module.SET_ACTIVE_RULES_PATH, smoke._controlled_rules_request(1))
    return database, saved["saved_version_id"]


@pytest.fixture(params=("single", "double"))
def actual_bridge_result(request, controlled_database, tmp_path, caplog):
    """Execute real solver twice in one app and again in a new app, using only temp data."""
    mode = request.param
    database, version = controlled_database
    diagnostics = tmp_path / "diagnostics"
    database_before = database.read_bytes()
    for name in ("apsgo_scheduler", "apsgo_v7_service"):
        caplog.set_level(logging.INFO, logger=name)
    responses = []
    for count in (2, 1):
        application = http_module.create_app(
            database, monthly_solve_policy=policy(), diagnostics_directory=diagnostics,
        )
        with TestClient(application) as client:
            for _ in range(count):
                value = smoke._bridge_request(version, mode)
                result = post(client, http_module.MONTH_SOLVE_PATH, value)
                smoke._assert_bridge_result(result, value, mode)
                logs = tuple((diagnostics / "runs" / value["request_id"]).glob("*/solve.log"))
                assert len(logs) == 1
                log = logs[0].read_text(encoding="utf-8")
                smoke._assert_bridge_log(log, value["request_id"], mode)
                responses.append((value, result, log))
    assert database.read_bytes() == database_before
    assert numeric._scan_block.nopython_signatures
    assert len({value["request_id"] for value, _, _ in responses}) == 3
    return mode, responses


def test_controlled_single_and_double_requests_run_actual_compiled_solver(actual_bridge_result):
    mode, responses = actual_bridge_result
    expected = 1 if mode == "single" else 2
    for request, result, _ in responses:
        assert len(request["orders"]) == 2
        assert {row["weight"] for row in request["orders"]} == {600}
        assert {row["width"] for row in request["orders"]} == {1000}
        assert sum(row["material_role"] == "virtual_sphc" for row in result["rows"]) == expected
        assert result["publishable"] is True
        assert result["audit_summary"]["passed"] is True


    request, result, _ = responses[0]
    for field, replacement in (("publishable", False), ("rows", [])):
        changed = deepcopy(result)
        changed[field] = replacement
        with pytest.raises(smoke.SmokeError):
            smoke._assert_bridge_result(changed, request, mode)


    request, _, log = responses[0]
    for changed in (
        "",
        log.replace("nopython=True", "nopython=False"),
        log.replace("solver_bridge_numeric_ready", "unrelated_event"),
        log.replace(request["request_id"], "00000000-0000-4000-8000-000000000000"),
    ):
        with pytest.raises(smoke.SmokeError):
            smoke._assert_bridge_log(changed, request["request_id"], mode)


def test_bridge_configuration_keeps_solver_policy_and_redirects_only_temp_data(tmp_path):
    template = Path(__file__).resolve().parents[2] / "config/apsgo_v7_service.yaml"
    before = template.read_bytes()
    original = load_service_configuration(template)
    runtime = tmp_path / "bridge runtime"
    runtime.mkdir()
    configuration = smoke._write_smoke_configuration(
        template, runtime / "service.yaml", 49123, bridge_runtime=runtime,
    )

    assert configuration.database_path == runtime / "rules.sqlite3"
    assert configuration.diagnostics_directory == runtime / "diagnostics"
    assert configuration.monthly_solve_policy == original.monthly_solve_policy
    assert configuration.database_timeout_seconds == original.database_timeout_seconds
    assert configuration.listen_host == "127.0.0.1"
    assert configuration.listen_port == 49123
    assert template.read_bytes() == before


def test_numeric_executable_start_uses_config_and_strips_external_python_paths(tmp_path, monkeypatch):
    calls = []
    environment = {
        "SystemRoot": "C:/Windows", "PATH": "external-python",
        "PYTHONPATH": "external", "CONDA_PREFIX": "external", "NUMBA_CACHE_DIR": "external",
        "KEEP_ME": "retained",
    }
    monkeypatch.setattr(smoke, "os", SimpleNamespace(environ=environment, pathsep=";"))

    def start(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(pid=904)

    monkeypatch.setattr(smoke.subprocess, "Popen", start)
    configuration = tmp_path / "runtime.yaml"
    process, log = smoke._start(tmp_path, tmp_path / "service.log", configuration=configuration)
    try:
        assert process.pid == 904
        command, options = calls[0]
        assert command == [str(tmp_path / "APSGoV7Service.exe"), "--config", str(configuration)]
        assert options["env"] == {
            "SystemRoot": "C:/Windows",
            "PATH": f"{Path('C:/Windows') / 'System32'};C:/Windows",
            "KEEP_ME": "retained",
        }
        assert options["cwd"] == tmp_path
        assert options["stdout"] is log
        assert options["stderr"] == subprocess.STDOUT
    finally:
        log.close()
    assert environment["PATH"] == "external-python"


@pytest.mark.parametrize("failure", (None, "deny", "verify", "body", "writable"))
def test_windows_acl_command_sequence_and_restoration_are_bounded_to_owned_copy(
    tmp_path, monkeypatch, failure,
):
    """Mock the Windows command boundary, not evidence of real ACL enforcement."""
    package = tmp_path / "APSGo V7 package"
    (package / "_internal").mkdir(parents=True)
    executable = package / "APSGoV7Service.exe"
    executable.write_bytes(b"test executable")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    calls = []
    monkeypatch.setattr(smoke, "os", SimpleNamespace(name="nt", environ={"SystemRoot": "C:/Windows"}))

    def run(command, **kwargs):
        calls.append((command, kwargs))
        label = next(name for name in ("save", "deny", "verify", "restore") if f"/{name}" in command)
        return subprocess.CompletedProcess(command, int(label == failure), b"command output", b"")

    monkeypatch.setattr(smoke.subprocess, "run", run)
    original_open = Path.open

    def guarded_open(path, mode="r", *args, **kwargs):
        if failure != "writable" and (
            path.name == ".apsgo-write-probe" or (path == executable and mode == "r+b")
        ):
            raise PermissionError("synthetic denied write")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    def use_installation():
        with smoke._readonly_installation(package, evidence):
            if failure == "body":
                raise smoke.SmokeError("synthetic body failure")

    if failure is None:
        use_installation()
    else:
        with pytest.raises(smoke.SmokeError):
            use_installation()
    commands = [command for command, _ in calls]
    assert commands[0][1:] == [package.name, "/save", str(evidence / "installation_acl.txt"), "/T", "/Q"]
    assert commands[-1][1:] == [".", "/restore", str(evidence / "installation_acl.txt"), "/Q"]
    assert commands[1][1:] == [package.name, "/deny", "*S-1-1-0:(WD,AD,WEA,WA,DE,DC)", "/T", "/Q"]
    assert all(command[0] == str(Path("C:/Windows") / "System32" / "icacls.exe") for command in commands)
    assert all(options["cwd"] == tmp_path and options["timeout"] == 60 for _, options in calls)
    assert (evidence / "acl_restore.log").read_bytes() == b"command output"
    assert executable.read_bytes() == b"test executable"


def test_smoke_evidence_is_exact_json_and_never_overwrites_an_existing_record(tmp_path):
    path = tmp_path / "sample.json"
    value = {"elapsed_seconds": Decimal("1.234567"), "passed": True}
    smoke._record_json(path, value)
    before = path.read_bytes()
    assert json.loads(before, parse_float=Decimal) == value
    with pytest.raises(FileExistsError):
        smoke._record_json(path, {"passed": False})
    assert path.read_bytes() == before
