"""Release input and package validation contracts."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from release.verify_release import (
    MANIFEST_PATH,
    PACKAGE_CONFIGURATION_PATH,
    PACKAGE_DATABASE_PATH,
    ReleaseValidationError,
    _sha256,
    main,
    verify_package,
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


def _database_manifest(source: dict, seed_path: Path) -> dict:
    database = source["database"]
    return {
        "source_path": "data/apsgo_v7_rules.sqlite3",
        "source_sha256": database["sha256"],
        "seed_path": PACKAGE_DATABASE_PATH.as_posix(),
        "seed_sha256": _sha256(seed_path),
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


def _write_manifest(package: Path, database: dict) -> None:
    files = {
        path.relative_to(package).as_posix(): _sha256(path)
        for path in package.rglob("*")
        if path.is_file() and path.name != MANIFEST_PATH.name
    }
    (package / MANIFEST_PATH).write_text(
        json.dumps(
            {"manifest_version": 1, "files": files, "database": database},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
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
    shutil.copy2(source_repository / "data/apsgo_v7_rules.sqlite3", seed)
    _write_manifest(package, _database_manifest(source, seed))
    return package


def test_source_mode_reports_complete_relative_identity(source_repository: Path, capsys):
    before = _sha256(source_repository / "data/apsgo_v7_rules.sqlite3")
    assert main(["source", "--repository-root", str(source_repository)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pass"
    assert payload["mode"] == "source"
    assert payload["git"]["branch"] is None
    assert payload["configuration"]["path"] == "config/apsgo_v7_service.yaml"
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
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
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
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
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


def test_check_only_precedes_all_build_output_mutations():
    source = (RELEASE_ROOT / "build_exe.ps1").read_text(encoding="utf-8")
    check_only = source.index("if ($CheckOnly)")
    exit_zero = source.index("exit 0", check_only)
    for mutation in (
        "Remove-Item -LiteralPath",
        "New-Item -ItemType Directory",
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
