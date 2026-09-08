"""Release input and package validation contracts."""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_v7_service import app as http_module
from apsgo_v7_service.migrate_gqga4_grade_dictionary import backup_sqlite_database
from release import smoke_release as smoke_module
from release import verify_release as release_module
from release.smoke_release import (
    SmokeError,
    _assert_business_content_preserved,
    _assert_initial_identity,
    _assert_runtime_identity,
    _request_from_active,
    _stop,
    _write_smoke_configuration,
)
from release.verify_release import (
    MANIFEST_PATH,
    PACKAGE_CONFIGURATION_PATH,
    PACKAGE_DATABASE_PATH,
    ReleaseValidationError,
    _sha256,
    generate_manifest,
    main,
    verify_package,
    verify_seed,
    verify_source,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RELEASE_ROOT = PROJECT_ROOT / "release"


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _commit(root: Path, message: str = "fixture") -> None:
    _git(root, "add", "--all")
    _git(root, "commit", "-q", "-m", message)


@pytest.fixture
def source_repository(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / "config").mkdir(parents=True)
    (root / "data").mkdir()
    shutil.copy2(
        PROJECT_ROOT / "config/apsgo_v7_service.yaml",
        root / "config/apsgo_v7_service.yaml",
    )
    shutil.copy2(
        PROJECT_ROOT / "data/apsgo_v7_rules.sqlite3",
        root / "data/apsgo_v7_rules.sqlite3",
    )
    shutil.copy2(PROJECT_ROOT / "pyproject.toml", root / "pyproject.toml")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "APSGo Test")
    _git(root, "config", "user.email", "apsgo-test@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "core.autocrlf", "false")
    _commit(root)
    _git(root, "checkout", "--detach", "-q", "HEAD")
    return root


def _replace_immutable_version_value(database: Path, column: str, value) -> None:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'trigger' AND name = 'trg_v7_rule_set_version_no_update'"
        ).fetchone()
        assert row is not None
        trigger_sql = row[0]
        connection.execute("DROP TRIGGER trg_v7_rule_set_version_no_update")
        connection.execute(
            f"UPDATE v7_rule_set_version SET {column} = ? "
            "WHERE id = (SELECT active_version_id FROM v7_rule_set LIMIT 1)",
            (value,),
        )
        connection.execute(trigger_sql)


def _clear_active_version(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'trigger' AND name = 'trg_v7_rule_set_active_version_forward'"
        ).fetchone()
        assert row is not None
        trigger_sql = row[0]
        connection.execute("DROP TRIGGER trg_v7_rule_set_active_version_forward")
        connection.execute("UPDATE v7_rule_set SET active_version_id = NULL")
        connection.execute(trigger_sql)


def _database_manifest(source: dict, seed_path: Path, source_database: Path) -> dict:
    database = source["database"]
    seed = verify_seed(source_database, seed_path)
    return {
        "source_path": "data/apsgo_v7_rules.sqlite3",
        "source_sha256": database["sha256"],
        "source_content_sha256": seed["source"]["content_sha256"],
        "seed_path": PACKAGE_DATABASE_PATH.as_posix(),
        "seed_sha256": _sha256(seed_path),
        "seed_content_sha256": seed["seed"]["content_sha256"],
        **{
            key: database[key]
            for key in (
                "schema_version",
                "active_version_id",
                "based_on_version_id",
                "rule_count",
                "enabled_rule_count",
                "virtual_prototype_count",
                "grade_dictionary_entry_count",
                "rule_set_fingerprint",
                "grade_dictionary_fingerprint",
            )
        },
    }


def _write_manifest(
    package: Path,
    database: dict,
    source_repository: Path,
    *,
    smoke_status: str = "pass",
) -> None:
    files = {
        path.relative_to(package).as_posix(): _sha256(path)
        for path in package.rglob("*")
        if path.is_file() and path.name != MANIFEST_PATH.name
    }
    (package / MANIFEST_PATH).write_text(
        dumps_exact_json(
            {
                "manifest_version": 1,
                "application": {
                    "name": "APSGoV7Service",
                    "package_name": "APSGoV7",
                    "project_name": "apsgo-scheduler",
                    "project_version": "0.1.0.dev0",
                },
                "source": {
                    "git_commit": _git(source_repository, "rev-parse", "HEAD"),
                    "git_branch": None,
                    "configuration_path": "config/apsgo_v7_service.yaml",
                    "configuration_sha256": _sha256(
                        source_repository / "config/apsgo_v7_service.yaml"
                    ),
                },
                "build": {
                    "built_at_utc": "2026-09-08T00:00:00Z",
                    "operating_system": "Windows",
                    "architecture": "AMD64",
                    "conda_environment": "aps_3.10.18",
                    "python_version": "3.10.18",
                    "pyinstaller_version": "6.22.2",
                    "fastapi_version": "0.128.8",
                    "uvicorn_version": "0.40.0",
                    "pyyaml_version": "6.0.3",
                    "mode": "onedir",
                    "console": True,
                    "upx": False,
                    "entry": "release/apsgo_v7_service_entry.py",
                    "service_entry": "apsgo_v7_service.app:main",
                    "worker_count": 1,
                },
                "database": database,
                "validation": {
                    "build": "pass",
                    "source": "pass",
                    "seed": "pass",
                    "package_static": "pass",
                    "smoke": smoke_status,
                },
                "files": files,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _rewrite_manifest(package: Path, manifest: dict) -> None:
    (package / MANIFEST_PATH).write_text(
        dumps_exact_json(manifest) + "\n", encoding="utf-8"
    )


@pytest.fixture
def release_package(tmp_path: Path, source_repository: Path) -> Path:
    source = verify_source(source_repository)
    package = tmp_path / "APSGoV7"
    (package / "_internal").mkdir(parents=True)
    (package / "config").mkdir()
    (package / "data").mkdir()
    (package / "APSGoV7Service.exe").write_bytes(b"test exe")
    (package / "_internal/runtime.bin").write_bytes(b"test runtime")
    (package / "start_apsgo_v7_service.bat").write_text("@echo off\n", encoding="utf-8")
    (package / "stop_apsgo_v7_service.bat").write_text("@echo off\n", encoding="utf-8")
    (package / "README.md").write_text("test package\n", encoding="utf-8")
    shutil.copy2(
        source_repository / "config/apsgo_v7_service.yaml",
        package / PACKAGE_CONFIGURATION_PATH,
    )
    seed = package / PACKAGE_DATABASE_PATH
    backup_sqlite_database(source_repository / "data/apsgo_v7_rules.sqlite3", seed)
    _write_manifest(
        package,
        _database_manifest(
            source, seed, source_repository / "data/apsgo_v7_rules.sqlite3"
        ),
        source_repository,
    )
    return package


def test_source_mode_reports_complete_relative_identity(source_repository: Path, capsys):
    before = _sha256(source_repository / "data/apsgo_v7_rules.sqlite3")
    assert main(["source", "--repository-root", str(source_repository)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pass"
    assert payload["mode"] == "source"
    assert payload["git"]["branch"] is None
    assert payload["configuration"]["path"] == "config/apsgo_v7_service.yaml"
    assert payload["configuration"]["database_timeout_seconds"] == 5.0
    assert payload["database"]["path"] == "data/apsgo_v7_rules.sqlite3"
    assert payload["database"]["rule_count"] == 17
    assert payload["database"]["enabled_rule_count"] == 16
    assert payload["database"]["virtual_prototype_count"] == 27
    assert payload["database"]["grade_dictionary_entry_count"] == 230
    assert str(source_repository) not in json.dumps(payload)
    assert _sha256(source_repository / "data/apsgo_v7_rules.sqlite3") == before
    assert not any((source_repository / "data").glob("*.sqlite3-*"))
    assert _git(source_repository, "status", "--porcelain=v1") == ""


def test_source_mode_rejects_dirty_tree(source_repository: Path):
    (source_repository / "untracked.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(ReleaseValidationError) as raised:
        verify_source(source_repository)
    assert raised.value.code == "source_tree_not_clean"


def test_source_mode_rejects_untracked_database(source_repository: Path):
    _git(source_repository, "rm", "--cached", "data/apsgo_v7_rules.sqlite3")
    with pytest.raises(ReleaseValidationError) as raised:
        verify_source(source_repository)
    assert raised.value.code == "source_input_not_tracked"


@pytest.mark.parametrize("suffix", ["-journal", "-shm", "-wal"])
def test_source_mode_rejects_database_sidecars(source_repository: Path, suffix: str):
    Path(f"{source_repository / 'data/apsgo_v7_rules.sqlite3'}{suffix}").touch()
    with pytest.raises(ReleaseValidationError) as raised:
        verify_source(source_repository)
    assert raised.value.code == "database_sidecar_present"


def test_source_mode_rejects_wrong_database_path(source_repository: Path):
    configuration = source_repository / "config/apsgo_v7_service.yaml"
    configuration.write_text(
        configuration.read_text(encoding="utf-8").replace(
            "../data/apsgo_v7_rules.sqlite3", "../data/other.sqlite3"
        ),
        encoding="utf-8",
    )
    _commit(source_repository, "wrong database")
    with pytest.raises(ReleaseValidationError) as raised:
        verify_source(source_repository)
    assert raised.value.code == "configuration_database_mismatch"


def test_source_mode_rejects_absolute_database_path(source_repository: Path):
    configuration = source_repository / "config/apsgo_v7_service.yaml"
    database = source_repository / "data/apsgo_v7_rules.sqlite3"
    configuration.write_text(
        configuration.read_text(encoding="utf-8").replace(
            "../data/apsgo_v7_rules.sqlite3", database.as_posix()
        ),
        encoding="utf-8",
    )
    _commit(source_repository, "absolute database")
    with pytest.raises(ReleaseValidationError) as raised:
        verify_source(source_repository)
    assert raised.value.code == "configuration_invalid"


def test_source_mode_rejects_wrong_schema(source_repository: Path):
    database = source_repository / "data/apsgo_v7_rules.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 99")
    _commit(source_repository, "wrong schema")
    with pytest.raises(ReleaseValidationError) as raised:
        verify_source(source_repository)
    assert raised.value.code == "database_schema_invalid"


def test_source_mode_rejects_corrupt_database(source_repository: Path):
    database = source_repository / "data/apsgo_v7_rules.sqlite3"
    database.write_bytes(b"not a sqlite database")
    _commit(source_repository, "corrupt database")
    with pytest.raises(ReleaseValidationError) as raised:
        verify_source(source_repository)
    assert raised.value.code == "database_integrity_invalid"


def test_source_mode_rejects_stored_fingerprint_mismatch(source_repository: Path):
    database = source_repository / "data/apsgo_v7_rules.sqlite3"
    _replace_immutable_version_value(database, "rule_set_fingerprint", "0" * 64)
    _commit(source_repository, "wrong fingerprint")
    with pytest.raises(ReleaseValidationError) as raised:
        verify_source(source_repository)
    assert raised.value.code == "stored_snapshot_inconsistent"


def test_source_mode_rejects_missing_active_version(source_repository: Path):
    database = source_repository / "data/apsgo_v7_rules.sqlite3"
    _clear_active_version(database)
    _commit(source_repository, "no active version")
    with pytest.raises(ReleaseValidationError) as raised:
        verify_source(source_repository)
    assert raised.value.code == "rule_set_not_initialized"


def test_source_mode_rejects_missing_grade_dictionary_identity(source_repository: Path):
    database = source_repository / "data/apsgo_v7_rules.sqlite3"
    _replace_immutable_version_value(database, "grade_dictionary_fingerprint", None)
    _commit(source_repository, "no dictionary identity")
    with pytest.raises(ReleaseValidationError) as raised:
        verify_source(source_repository)
    assert raised.value.code == "stored_snapshot_inconsistent"


def test_package_mode_accepts_complete_manifest_without_writing(release_package: Path):
    seed = release_package / PACKAGE_DATABASE_PATH
    before = _sha256(seed)
    result = verify_package(release_package)
    assert result["status"] == "pass"
    assert result["mode"] == "package"
    assert result["file_count"] == 7
    assert result["database"]["active_version_id"] == 5
    assert _sha256(seed) == before
    assert not any((release_package / "data").glob("*.sqlite3-*"))


def test_package_mode_rejects_changed_file_digest(release_package: Path):
    (release_package / "README.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ReleaseValidationError) as raised:
        verify_package(release_package)
    assert raised.value.code == "manifest_digest_mismatch"


def test_package_mode_rejects_runtime_database(release_package: Path):
    shutil.copy2(
        release_package / PACKAGE_DATABASE_PATH,
        release_package / "data/apsgo_v7_rules.sqlite3",
    )
    with pytest.raises(ReleaseValidationError) as raised:
        verify_package(release_package)
    assert raised.value.code == "package_layout_invalid"


def test_package_mode_rejects_sidecar(release_package: Path):
    Path(f"{release_package / PACKAGE_DATABASE_PATH}-wal").touch()
    with pytest.raises(ReleaseValidationError) as raised:
        verify_package(release_package)
    assert raised.value.code == "database_sidecar_present"


def test_package_mode_rejects_unexpected_file(release_package: Path):
    (release_package / "debug.log").write_text("debug\n", encoding="utf-8")
    with pytest.raises(ReleaseValidationError) as raised:
        verify_package(release_package)
    assert raised.value.code == "package_layout_invalid"


def test_package_mode_rejects_database_identity_mismatch(release_package: Path):
    manifest_path = release_package / MANIFEST_PATH
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["database"]["active_version_id"] += 1
    _rewrite_manifest(release_package, manifest)
    with pytest.raises(ReleaseValidationError) as raised:
        verify_package(release_package)
    assert raised.value.code == "package_database_identity_mismatch"


def test_package_mode_rejects_corrupt_seed_after_digest_check(release_package: Path):
    seed = release_package / PACKAGE_DATABASE_PATH
    seed.write_bytes(b"not a sqlite database")
    manifest_path = release_package / MANIFEST_PATH
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][PACKAGE_DATABASE_PATH.as_posix()] = _sha256(seed)
    manifest["database"]["seed_sha256"] = _sha256(seed)
    _rewrite_manifest(release_package, manifest)
    with pytest.raises(ReleaseValidationError) as raised:
        verify_package(release_package)
    assert raised.value.code == "database_integrity_invalid"


def test_package_mode_rejects_symlink(release_package: Path):
    link = release_package / "_internal/link.bin"
    try:
        os.symlink(release_package / "_internal/runtime.bin", link)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(ReleaseValidationError) as raised:
        verify_package(release_package)
    assert raised.value.code == "package_layout_invalid"


def _windows_build_environment() -> dict:
    return {
        "operating_system": "Windows",
        "architecture": "AMD64",
        "conda_environment": "aps_3.10.18",
        "python_version": "3.10.18",
        "pyinstaller_version": "6.22.2",
        "fastapi_version": "0.128.8",
        "uvicorn_version": "0.40.0",
        "pyyaml_version": "6.0.3",
    }


def test_manifest_generation_is_canonical_complete_and_deterministic(
    release_package: Path, source_repository: Path, monkeypatch
):
    monkeypatch.setattr(release_module, "_build_environment", _windows_build_environment)
    first = generate_manifest(
        source_repository,
        release_package,
        built_at_utc="2026-09-08T01:02:03Z",
        smoke_status="pending",
    )
    second = generate_manifest(
        source_repository,
        release_package,
        built_at_utc="2026-09-08T01:02:03Z",
        smoke_status="pending",
    )
    assert first == second
    assert first["application"] == {
        "name": "APSGoV7Service",
        "package_name": "APSGoV7",
        "project_name": "apsgo-scheduler",
        "project_version": "0.1.0.dev0",
    }
    assert first["source"]["git_branch"] is None
    assert first["build"] == _windows_build_environment() | {
        "built_at_utc": "2026-09-08T01:02:03Z",
        "mode": "onedir",
        "console": True,
        "upx": False,
        "entry": "release/apsgo_v7_service_entry.py",
        "service_entry": "apsgo_v7_service.app:main",
        "worker_count": 1,
    }
    assert first["validation"]["smoke"] == "pending"
    assert MANIFEST_PATH.as_posix() not in first["files"]
    assert set(first["files"]) == {
        path.relative_to(release_package).as_posix()
        for path in release_package.rglob("*")
        if path.is_file() and path != release_package / MANIFEST_PATH
    }
    assert (
        first["database"]["source_content_sha256"]
        == first["database"]["seed_content_sha256"]
    )
    encoded = (dumps_exact_json(first) + "\n").encode("utf-8")
    assert not encoded.startswith(b"\xef\xbb\xbf")
    assert str(source_repository).encode("utf-8") not in encoded
    assert str(release_package).encode("utf-8") not in encoded


def test_package_mode_requires_completed_smoke_by_default(release_package: Path):
    manifest = json.loads((release_package / MANIFEST_PATH).read_text(encoding="utf-8"))
    manifest["validation"]["smoke"] = "pending"
    _rewrite_manifest(release_package, manifest)
    with pytest.raises(ReleaseValidationError) as raised:
        verify_package(release_package)
    assert raised.value.code == "manifest_invalid"
    assert verify_package(release_package, allow_pending_smoke=True)["smoke_status"] == "pending"


def test_package_mode_rejects_noncanonical_or_unknown_manifest_fields(
    release_package: Path,
):
    manifest_path = release_package / MANIFEST_PATH
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["unexpected"] = True
    _rewrite_manifest(release_package, manifest)
    with pytest.raises(ReleaseValidationError) as raised:
        verify_package(release_package)
    assert raised.value.code == "manifest_invalid"

    del manifest["unexpected"]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ReleaseValidationError) as raised:
        verify_package(release_package)
    assert raised.value.code == "manifest_invalid"


@pytest.mark.parametrize(
    "relative",
    ("_internal/apsgo_v7_service/app.py", "_internal/__pycache__/app.pyc", "_internal/tests/data.bin"),
)
def test_package_mode_rejects_source_test_and_cache_residue(
    release_package: Path, relative: str
):
    path = release_package / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"residue")
    with pytest.raises(ReleaseValidationError) as raised:
        verify_package(release_package)
    assert raised.value.code == "package_layout_invalid"


def _response_json(response) -> dict:
    return json.loads(response.content.decode("utf-8"), parse_float=Decimal)


def test_disposable_runtime_get_save_restart_get_preserves_formal_inputs(
    tmp_path: Path, release_package: Path, source_repository: Path
):
    formal_seed = release_package / PACKAGE_DATABASE_PATH
    source_database = source_repository / "data/apsgo_v7_rules.sqlite3"
    seed_before = _sha256(formal_seed)
    source_before = _sha256(source_database)
    runtime = tmp_path / "runtime"
    shutil.copytree(release_package, runtime)
    shutil.copy2(
        runtime / PACKAGE_CONFIGURATION_PATH,
        runtime / "config/apsgo_v7_service.yaml",
    )
    runtime_database = runtime / "data/apsgo_v7_rules.sqlite3"
    shutil.copy2(runtime / PACKAGE_DATABASE_PATH, runtime_database)

    get_path = "/api/v1/rule-sets/GQGA4/default/month/getActiveRules"
    post_path = "/api/v1/rule-sets/GQGA4/default/month/setActiveRules"
    operation_id = "00000000-0000-4000-8000-000000000904"
    with TestClient(
        http_module.create_app(runtime_database), raise_server_exceptions=False
    ) as client:
        first_response = client.get(get_path)
        assert first_response.status_code == 200
        first = _response_json(first_response)
        manifest = json.loads(
            (release_package / MANIFEST_PATH).read_text(encoding="utf-8")
        )
        _assert_initial_identity(first, manifest)
        request = _request_from_active(first, operation_id)
        saved_response = client.post(
            post_path,
            content=dumps_exact_json(request).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        assert saved_response.status_code == 200
        saved = _response_json(saved_response)
        active = _response_json(client.get(get_path))
        assert saved["previous_active_version_id"] == first["active_version_id"]
        assert saved["saved_version_id"] == active["active_version_id"]
        assert saved["saved_version_is_active"] is True
        assert saved["idempotent_replay"] is False
        _assert_business_content_preserved(first, active)

        for key in (
            "product_line_code",
            "process_code",
            "scenario",
            "rules",
            "quality_spec",
            "allowed_final_deviation_codes",
            "virtual_prototypes",
        ):
            changed = deepcopy(active)
            changed[key] = None
            with pytest.raises(SmokeError, match=key):
                _assert_business_content_preserved(first, changed)

        missing_initial = deepcopy(first)
        missing_active = deepcopy(active)
        del missing_initial["quality_spec"]
        del missing_active["quality_spec"]
        with pytest.raises(SmokeError, match="missing quality_spec"):
            _assert_business_content_preserved(missing_initial, missing_active)

    with TestClient(
        http_module.create_app(runtime_database), raise_server_exceptions=False
    ) as restarted_client:
        restarted = _response_json(restarted_client.get(get_path))
    assert restarted == active
    runtime_identity = release_module._read_database(
        runtime_database, timeout_seconds=5.0
    )
    _assert_runtime_identity(runtime_identity, first, active, manifest)
    for key in (
        "rule_count",
        "enabled_rule_count",
        "virtual_prototype_count",
        "grade_dictionary_entry_count",
        "grade_dictionary_fingerprint",
    ):
        changed = dict(runtime_identity)
        changed[key] = None
        with pytest.raises(SmokeError, match=key):
            _assert_runtime_identity(changed, first, active, manifest)
    assert _sha256(formal_seed) == seed_before
    assert _sha256(source_database) == source_before
    assert verify_package(release_package)["status"] == "pass"


def _database_seed(tmp_path: Path, source_repository: Path) -> Path:
    seed = tmp_path / "apsgo_v7_rules_seed.sqlite3"
    backup_sqlite_database(
        source_repository / "data/apsgo_v7_rules.sqlite3",
        seed,
    )
    return seed


def test_seed_mode_accepts_sqlite_backup_without_exposing_absolute_paths(
    tmp_path: Path, source_repository: Path, capsys
):
    source = source_repository / "data/apsgo_v7_rules.sqlite3"
    seed = _database_seed(tmp_path, source_repository)
    assert main(
        [
            "seed",
            "--source-database",
            str(source),
            "--seed-database",
            str(seed),
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pass"
    assert payload["mode"] == "seed"
    assert payload["source"]["sha256"] == _sha256(source)
    assert payload["seed"]["sha256"] == _sha256(seed)
    assert payload["source"]["rule_set_fingerprint"] == payload["seed"][
        "rule_set_fingerprint"
    ]
    assert payload["source"]["content_sha256"] == payload["seed"]["content_sha256"]
    assert str(tmp_path) not in json.dumps(payload)


def test_seed_mode_rejects_same_source_and_destination(source_repository: Path):
    source = source_repository / "data/apsgo_v7_rules.sqlite3"
    with pytest.raises(ReleaseValidationError) as raised:
        verify_seed(source, source)
    assert raised.value.code == "seed_input_invalid"


def test_seed_mode_rejects_hard_link_to_source(
    tmp_path: Path, source_repository: Path
):
    source = source_repository / "data/apsgo_v7_rules.sqlite3"
    linked_seed = tmp_path / "linked-seed.sqlite3"
    os.link(source, linked_seed)
    with pytest.raises(ReleaseValidationError) as raised:
        verify_seed(source, linked_seed)
    assert raised.value.code == "seed_input_invalid"


def test_seed_mode_rejects_symbolic_link_to_source(
    tmp_path: Path, source_repository: Path
):
    source = source_repository / "data/apsgo_v7_rules.sqlite3"
    linked_seed = tmp_path / "linked-seed.sqlite3"
    try:
        linked_seed.symlink_to(source)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(ReleaseValidationError) as raised:
        verify_seed(source, linked_seed)
    assert raised.value.code == "seed_database_missing"


def test_seed_mode_rejects_corrupt_seed(tmp_path: Path, source_repository: Path):
    seed = _database_seed(tmp_path, source_repository)
    seed.write_bytes(b"not a sqlite database")
    with pytest.raises(ReleaseValidationError) as raised:
        verify_seed(source_repository / "data/apsgo_v7_rules.sqlite3", seed)
    assert raised.value.code == "database_integrity_invalid"


def test_seed_mode_compares_historical_database_content(
    tmp_path: Path, source_repository: Path
):
    seed = _database_seed(tmp_path, source_repository)
    with sqlite3.connect(seed) as connection:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'trigger' AND name = 'trg_v7_rule_set_version_no_update'"
        ).fetchone()[0]
        active = connection.execute(
            "SELECT active_version_id FROM v7_rule_set LIMIT 1"
        ).fetchone()[0]
        historical = connection.execute(
            "SELECT id FROM v7_rule_set_version WHERE id <> ? ORDER BY id LIMIT 1",
            (active,),
        ).fetchone()[0]
        connection.execute("DROP TRIGGER trg_v7_rule_set_version_no_update")
        connection.execute(
            "UPDATE v7_rule_set_version SET remark = remark || ' changed' WHERE id = ?",
            (historical,),
        )
        connection.execute(trigger)
    with pytest.raises(ReleaseValidationError) as raised:
        verify_seed(source_repository / "data/apsgo_v7_rules.sqlite3", seed)
    assert raised.value.code == "seed_database_identity_mismatch"


def test_seed_mode_rejects_sidecar(tmp_path: Path, source_repository: Path):
    seed = _database_seed(tmp_path, source_repository)
    Path(f"{seed}-wal").touch()
    with pytest.raises(ReleaseValidationError) as raised:
        verify_seed(source_repository / "data/apsgo_v7_rules.sqlite3", seed)
    assert raised.value.code == "database_sidecar_present"


def test_seed_mode_rejects_nonpositive_timeout(tmp_path: Path, source_repository: Path):
    seed = _database_seed(tmp_path, source_repository)
    with pytest.raises(ReleaseValidationError) as raised:
        verify_seed(
            source_repository / "data/apsgo_v7_rules.sqlite3",
            seed,
            timeout_seconds=0,
        )
    assert raised.value.code == "seed_input_invalid"


def test_frozen_entry_only_delegates_to_existing_service_main():
    source = (RELEASE_ROOT / "apsgo_v7_service_entry.py").read_text(encoding="utf-8")
    assert "from apsgo_v7_service.app import main" in source
    assert 'if __name__ == "__main__":\n    main()' in source
    assert "uvicorn" not in source
    assert "multiprocessing" not in source


def test_build_dependency_is_one_exact_pyinstaller_candidate():
    requirements = (RELEASE_ROOT / "requirements-build.txt").read_text(
        encoding="utf-8"
    )
    assert requirements == "PyInstaller==6.22.2\n"


def test_batch_entry_is_only_a_powershell_exit_code_bridge():
    source = (RELEASE_ROOT / "build_exe.bat").read_text(encoding="utf-8")
    assert source.splitlines() == [
        "@echo off",
        "setlocal",
        'powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File '
        '"%~dp0build_exe.ps1" %*',
        'set "EXIT_CODE=%ERRORLEVEL%"',
        "exit /b %EXIT_CODE%",
    ]


def test_powershell_build_contract_is_fixed_and_does_not_install_dependencies():
    source = (RELEASE_ROOT / "build_exe.ps1").read_text(encoding="utf-8")
    for expected in (
        '$ExpectedCondaEnvironment = "aps_3.10.18"',
        '$ExpectedPythonVersion = "3.10.18"',
        '$ApplicationName = "APSGoV7Service"',
        '$PackageName = "APSGoV7"',
        "[Environment]::Is64BitOperatingSystem",
        "[Environment]::Is64BitProcess",
        '"--onedir"',
        '"--console"',
        '"--noupx"',
        '"--contents-directory", "_internal"',
        '"--collect-submodules", "uvicorn"',
        "$Verifier source --repository-root $ProjectRoot",
        "if ($null -ne $SourceValidation.git.branch)",
        'if ($CheckOnly -and $Clean)',
    ):
        assert expected in source
    assert '"-m", "pip"' not in source
    assert '"pip", "install"' not in source
    assert "Get-ChildItem" not in source
    assert "Library\\bin" not in source
    assert "GQGA5" not in source


def test_multiline_python_is_sent_over_stdin_not_native_command_arguments():
    source = (RELEASE_ROOT / "build_exe.ps1").read_text(encoding="utf-8")
    assert "$RuntimeInspectionCode | & $CondaPython -" in source
    assert "$PyInstallerVersionCode | & $CondaPython -" in source
    assert "$SeedBackupCode | & $CondaPython -" in source
    assert "& $CondaPython -c" not in source


def test_build_uses_sqlite_backup_and_copies_only_release_runtime_templates():
    source = (RELEASE_ROOT / "build_exe.ps1").read_text(encoding="utf-8")
    assert "backup_sqlite_database" in source
    assert source.count("$Verifier source --repository-root $ProjectRoot") == 2
    assert source.count("$Verifier seed") == 2
    assert "$SeedValidation.source.sha256 -ne $SourceValidation.database.sha256" in source
    assert "$FinalSourceValidation.git.commit -ne $SourceValidation.git.commit" in source
    assert "--seed-database $PackageSeedDatabase" in source
    assert "Copy-Item -LiteralPath $SourceDatabase" not in source
    assert "apsgo_v7_service.example.yaml" in source
    assert "apsgo_v7_rules_seed.sqlite3" in source
    assert "start_apsgo_v7_service.bat" in source
    assert "stop_apsgo_v7_service.bat" in source


def test_start_script_initializes_missing_files_and_preserves_existing_files():
    source = (RELEASE_ROOT / "start_apsgo_v7_service.bat").read_text(encoding="utf-8")
    assert 'cd /d "%~dp0"' in source
    assert "%~dp0APSGoV7Service.exe" in source
    assert "%~dp0config\\apsgo_v7_service.example.yaml" in source
    assert "%~dp0config\\apsgo_v7_service.yaml" in source
    assert "%~dp0data\\apsgo_v7_rules_seed.sqlite3" in source
    assert "%~dp0data\\apsgo_v7_rules.sqlite3" in source
    assert (
        'call :initialize_file "%APSGO_V7_CONFIG_TEMPLATE%" "%APSGO_V7_CONFIG%"'
        in source
    )
    assert (
        'call :initialize_file "%APSGO_V7_DATABASE_SEED%" '
        '"%APSGO_V7_RUNTIME_DATABASE%"' in source
    )
    assert "if ([IO.File]::Exists($destination)) { exit 0 }" in source
    assert "[IO.File]::Copy($source, $temporary, $false)" in source
    assert "[IO.File]::Move($temporary, $destination)" in source
    assert "copy /" not in source.lower()
    assert "[IO.FileAccess]::ReadWrite" in source
    assert '"%APSGO_V7_EXE%" --config "%APSGO_V7_CONFIG%"' in source
    assert "listen_host" not in source
    assert "listen_port" not in source
    assert "candidate_check_limit" not in source


def test_stop_script_matches_only_the_executable_in_its_own_directory():
    source = (RELEASE_ROOT / "stop_apsgo_v7_service.bat").read_text(encoding="utf-8")
    assert "%~dp0APSGoV7Service.exe" in source
    assert "Get-Process -Name 'APSGoV7Service'" in source
    assert "$_.Path" in source
    assert "[StringComparison]::OrdinalIgnoreCase" in source
    assert "Stop-Process -Id $process.Id -Force" in source
    assert "$remaining = Get-Process -Id $process.Id -ErrorAction SilentlyContinue" in source
    assert "$remaining.WaitForExit(15000)" in source
    assert "taskkill" not in source.lower()
    assert "python" not in source.lower()
    assert "listen_port" not in source


def test_check_only_precedes_all_build_output_mutations():
    source = (RELEASE_ROOT / "build_exe.ps1").read_text(encoding="utf-8")
    check_only = source.index("if ($CheckOnly)")
    exit_zero = source.index("exit 0", check_only)
    for mutation in (
        "Remove-Item -LiteralPath",
        "New-Item -ItemType Directory",
        "backup_sqlite_database",
        "Copy-Item -LiteralPath",
        "& $CondaPython @PyInstallerArguments",
        "Move-Item -LiteralPath",
    ):
        assert check_only < exit_zero < source.index(mutation)
    assert source.index("$env:PYTHONDONTWRITEBYTECODE") < source.index("& conda run")
    assert source.index('$env:GIT_OPTIONAL_LOCKS = "0"') < source.index(
        "$Verifier source --repository-root $ProjectRoot"
    )
    assert "foreach ($path in @($BuildRoot, $PackageDirectory))" in source
    assert source.count("Remove-Item -LiteralPath") == 1
    assert '"--distpath", $PyInstallerDistDirectory' in source
    assert (
        "$BuiltDirectory = Join-Path $PyInstallerDistDirectory $ApplicationName"
        in source
    )


def test_build_generates_pending_manifest_smokes_copy_then_seals_final_manifest():
    source = (RELEASE_ROOT / "build_exe.ps1").read_text(encoding="utf-8")
    pending = source.index('Write-ReleaseManifest -SmokeStatus "pending"')
    precheck = source.index("--allow-pending-smoke", pending)
    smoke = source.index("$SmokeRunner", precheck)
    passed = source.index('Write-ReleaseManifest -SmokeStatus "pass"', smoke)
    final_check = source.index("$FinalPackageValidationJson", passed)
    completed = source.index("onedir build completed", final_check)
    assert pending < precheck < smoke < passed < final_check < completed
    assert "[IO.File]::WriteAllText" in source
    assert "System.Text.UTF8Encoding($false)" in source
    assert "Copy-Item -LiteralPath $ReleaseReadme -Destination $PackageReadme" in source
    assert "release_manifest.json" in source


def test_windows_smoke_uses_unmodified_disposable_package_and_exact_rule_roundtrip():
    source = (RELEASE_ROOT / "smoke_release.py").read_text(encoding="utf-8")
    for expected in (
        "shutil.copytree(package, smoke_package)",
        'prefix="APSGo V7 smoke "',
        '"APSGoV7Service.exe"), "--help"',
        '"start_apsgo_v7_service.bat"',
        '"stop_apsgo_v7_service.bat"',
        "GET_ACTIVE_RULES_PATH",
        "SET_ACTIVE_RULES_PATH",
        '"expected_active_version_id": active["active_version_id"]',
        '"virtual_prototypes": deepcopy(active["virtual_prototypes"])',
        "parse_float=Decimal",
        "_reject_database_sidecars",
        "_read_database",
        "ProxyHandler({})",
        "_available_loopback_port()",
        '"listen_host: 127.0.0.1"',
        'Path(system_root) / "System32" / "taskkill.exe"',
        '"/PID", str(process.pid), "/T", "/F"',
    ):
        assert expected in source
    assert "safe_dump" not in source
    assert "MONTH_SOLVE_PATH" not in source


def test_smoke_runtime_configuration_is_loopback_only_and_keeps_template_unchanged(
    tmp_path: Path,
):
    template = tmp_path / "config" / "apsgo_v7_service.example.yaml"
    runtime = tmp_path / "config" / "apsgo_v7_service.yaml"
    template.parent.mkdir()
    shutil.copy2(PROJECT_ROOT / "config/apsgo_v7_service.yaml", template)
    before = template.read_bytes()

    configuration = _write_smoke_configuration(template, runtime, 49123)

    assert configuration.listen_host == "127.0.0.1"
    assert configuration.listen_port == 49123
    assert template.read_bytes() == before


@pytest.mark.parametrize("stop_failure", ("nonzero", "timeout"))
def test_smoke_stop_uses_exact_spawned_process_tree_fallback(
    tmp_path: Path, monkeypatch, stop_failure: str
):
    class Process:
        pid = 904
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            self.returncode = 0
            return 0

    process = Process()
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        if len(calls) == 1:
            if stop_failure == "timeout":
                raise subprocess.TimeoutExpired(arguments, kwargs["timeout"])
            return subprocess.CompletedProcess(arguments, 1, b"", b"stop failed")
        return subprocess.CompletedProcess(arguments, 0, b"", b"")

    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    monkeypatch.setattr(smoke_module.subprocess, "run", run)
    log = io.BytesIO()

    _stop(tmp_path, process, log)

    assert calls[1][1:] == ["/PID", "904", "/T", "/F"]
    assert process.poll() == 0
    assert log.closed


def test_smoke_stop_accepts_process_already_exited_race(tmp_path: Path, monkeypatch):
    class ExitedProcess:
        pid = 905

        @staticmethod
        def poll():
            return 0

    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 1, b"", b"already stopped")

    monkeypatch.setattr(smoke_module.subprocess, "run", run)
    log = io.BytesIO()

    _stop(tmp_path, ExitedProcess(), log)

    assert len(calls) == 1
    assert log.closed


def test_release_readme_explains_runtime_files_network_and_manifest_boundary():
    source = (RELEASE_ROOT / "README.md").read_text(encoding="utf-8")
    for expected in (
        "start_apsgo_v7_service.bat",
        "stop_apsgo_v7_service.bat",
        "apsgo_v7_service.example.yaml",
        "apsgo_v7_service.yaml",
        "apsgo_v7_rules_seed.sqlite3",
        "apsgo_v7_rules.sqlite3",
        "0.0.0.0",
        "没有登录鉴权",
        "不是数字签名",
    ):
        assert expected in source
