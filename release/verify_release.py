#!/usr/bin/env python3
"""Read-only validation for APSGo V7 release inputs and assembled packages."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from decimal import Decimal
from math import isfinite
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_scheduler.api.rule_management import dumps_active_rules_response
from apsgo_v7_service.configuration import (
    ServiceConfigurationError,
    load_service_configuration,
)
from apsgo_v7_service.rule_management import (
    RuleManagementServiceError,
    _find_rule_set,
    _read_active_scheduling_snapshot,
)
from apsgo_v7_service.rule_store import RuleStore, RuleStoreSchemaError, SCHEMA_VERSION

CONFIGURATION_PATH = Path("config/apsgo_v7_service.yaml")
SOURCE_DATABASE_PATH = Path("data/apsgo_v7_rules.sqlite3")
PACKAGE_CONFIGURATION_PATH = Path("config/apsgo_v7_service.example.yaml")
PACKAGE_DATABASE_PATH = Path("data/apsgo_v7_rules_seed.sqlite3")
RUNTIME_CONFIGURATION_PATH = Path("config/apsgo_v7_service.yaml")
RUNTIME_DATABASE_PATH = Path("data/apsgo_v7_rules.sqlite3")
MANIFEST_PATH = Path("release_manifest.json")
SIDECAR_SUFFIXES = ("-journal", "-shm", "-wal")
_REQUIRED_PACKAGE_FILES = frozenset(
    {
        "APSGoV7Service.exe",
        "README.md",
        "start_apsgo_v7_service.bat",
        "stop_apsgo_v7_service.bat",
        PACKAGE_CONFIGURATION_PATH.as_posix(),
        PACKAGE_DATABASE_PATH.as_posix(),
    }
)


class ReleaseValidationError(RuntimeError):
    """One stable, user-facing release validation failure."""

    def __init__(self, code: str, message: str, *, path: str | None = None):
        self.code = code
        self.path = path
        super().__init__(message)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_state(path: Path) -> tuple[int, int, str]:
    before = path.stat()
    digest = _sha256(path)
    after = path.stat()
    before_identity = (before.st_size, before.st_mtime_ns)
    after_identity = (after.st_size, after.st_mtime_ns)
    if before_identity != after_identity:
        raise ReleaseValidationError(
            "database_changed_during_verification",
            "数据库在发布验证期间发生变化。",
        )
    return after.st_size, after.st_mtime_ns, digest


def _run_git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except OSError as error:
        raise ReleaseValidationError(
            "repository_invalid", "无法执行 Git 发布输入检查。"
        ) from error
    if check and result.returncode != 0:
        raise ReleaseValidationError(
            "repository_invalid", "发布输入目录不是可读取的 Git 工作树。"
        )
    return result


def _repository_metadata(root: Path) -> dict:
    top_level = _run_git(root, "rev-parse", "--show-toplevel").stdout.strip()
    try:
        if Path(top_level).resolve(strict=True) != root:
            raise ReleaseValidationError(
                "repository_invalid", "发布输入必须从 Git 工作树根目录验证。"
            )
    except OSError as error:
        raise ReleaseValidationError(
            "repository_invalid", "Git 工作树根目录无法解析。"
        ) from error
    commit = _run_git(root, "rev-parse", "HEAD").stdout.strip()
    branch_result = _run_git(root, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    return {"commit": commit, "branch": branch}


def _git_status(root: Path) -> str:
    return _run_git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ).stdout


def _require_tracked(root: Path, relative_path: Path) -> None:
    result = _run_git(
        root,
        "ls-files",
        "--error-unmatch",
        "--",
        relative_path.as_posix(),
        check=False,
    )
    if result.returncode != 0:
        raise ReleaseValidationError(
            "source_input_not_tracked",
            "发布输入文件没有受 Git 跟踪。",
            path=relative_path.as_posix(),
        )


def _require_regular_file(root: Path, relative_path: Path, *, missing_code: str) -> Path:
    path = root / relative_path
    if not path.exists():
        raise ReleaseValidationError(
            missing_code,
            "发布输入文件不存在。",
            path=relative_path.as_posix(),
        )
    if path.is_symlink() or not path.is_file() or path.resolve() != path.absolute():
        raise ReleaseValidationError(
            "source_input_not_tracked",
            "发布输入必须是工作树内的普通文件，不能是符号链接。",
            path=relative_path.as_posix(),
        )
    return path


def _database_sidecars(database_path: Path) -> tuple[Path, ...]:
    return tuple(
        Path(f"{database_path}{suffix}")
        for suffix in SIDECAR_SUFFIXES
        if Path(f"{database_path}{suffix}").exists()
    )


def _reject_database_sidecars(root: Path, database_path: Path) -> None:
    sidecars = _database_sidecars(database_path)
    if sidecars:
        path = sidecars[0]
        try:
            display_path = path.relative_to(root).as_posix()
        except ValueError:
            display_path = path.name
        raise ReleaseValidationError(
            "database_sidecar_present",
            "发现禁止进入发布输入的 SQLite 辅助文件。",
            path=display_path,
        )


def _raw_database_path(configuration_path: Path) -> str:
    try:
        value = yaml.safe_load(configuration_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ReleaseValidationError(
            "configuration_invalid", "服务配置无法读取或不是合法 UTF-8 YAML。"
        ) from error
    configured = value.get("database_path") if isinstance(value, dict) else None
    if not isinstance(configured, str) or not configured.strip():
        raise ReleaseValidationError(
            "configuration_invalid", "服务配置缺少有效的 database_path。"
        )
    configured = configured.strip()
    if PurePosixPath(configured).is_absolute() or PureWindowsPath(configured).is_absolute():
        raise ReleaseValidationError(
            "configuration_invalid", "发布配置中的 database_path 必须使用相对路径。"
        )
    return configured


def _read_database(database_path: Path, *, timeout_seconds: float) -> dict:
    connection = None
    try:
        uri = f"{database_path.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=timeout_seconds,
            isolation_level=None,
        )
        connection.execute("PRAGMA query_only = ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise ReleaseValidationError(
                "database_integrity_invalid", "规则数据库无法进入只读检查模式。"
            )
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        quick_check = tuple(
            row[0] for row in connection.execute("PRAGMA quick_check").fetchall()
        )
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        foreign_key_violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if journal_mode != "delete":
            raise ReleaseValidationError(
                "database_integrity_invalid",
                "冻结发布数据库必须使用无辅助文件的 delete journal 模式。",
            )
        if quick_check != ("ok",) or foreign_key_violations:
            raise ReleaseValidationError(
                "database_integrity_invalid", "规则数据库完整性或外键检查失败。"
            )
        if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
            raise ReleaseValidationError(
                "database_schema_invalid",
                f"规则数据库 schema 必须为 {SCHEMA_VERSION}。",
            )
        store = RuleStore(connection)
        connection = None
        with store:
            with store.transaction():
                # The public helper reopens by path; keep the verified connection immutable.
                store._verify_schema()
                snapshot = _read_active_scheduling_snapshot(store, _find_rule_set(store))
                dumps_active_rules_response(snapshot.active_rules)
    except ReleaseValidationError:
        raise
    except (OSError, sqlite3.DatabaseError) as error:
        raise ReleaseValidationError(
            "database_integrity_invalid", "规则数据库无法完成只读完整性检查。"
        ) from error
    except RuleStoreSchemaError as error:
        raise ReleaseValidationError(
            "database_schema_invalid", "规则数据库 schema 与服务契约不一致。"
        ) from error
    except RuleManagementServiceError as error:
        known_code = (
            error.code
            if error.code
            in {
                "rule_set_not_initialized",
                "grade_dictionary_unavailable",
                "stored_snapshot_inconsistent",
            }
            else "stored_snapshot_inconsistent"
        )
        raise ReleaseValidationError(known_code, str(error)) from error
    except (RecursionError, TypeError, ValueError) as error:
        raise ReleaseValidationError(
            "stored_snapshot_inconsistent", "规则数据库活动快照不一致。"
        ) from error
    finally:
        if connection is not None:
            connection.close()
    active = snapshot.active_rules
    return {
        "quick_check": "ok",
        "schema_version": schema_version,
        "active_version_id": active.active_version_id,
        "based_on_version_id": active.based_on_version_id,
        "rule_count": len(active.rule_set_spec.rules),
        "enabled_rule_count": sum(rule.enabled for rule in active.rule_set_spec.rules),
        "virtual_prototype_count": len(active.virtual_prototypes),
        "grade_dictionary_entry_count": len(snapshot.grade_dictionary.entries),
        "rule_set_fingerprint": active.rule_set_spec.fingerprint,
        "grade_dictionary_fingerprint": snapshot.grade_dictionary.dictionary_fingerprint,
    }


def _database_content_sha256(database_path: Path, *, timeout_seconds: float) -> str:
    connection = None
    try:
        uri = f"{database_path.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=timeout_seconds,
            isolation_level=None,
        )
        connection.execute("PRAGMA query_only = ON")
        digest = hashlib.sha256()
        for statement in connection.iterdump():
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()
    except (OSError, sqlite3.DatabaseError) as error:
        raise ReleaseValidationError(
            "database_integrity_invalid", "规则数据库无法计算逻辑内容摘要。"
        ) from error
    finally:
        if connection is not None:
            connection.close()


def verify_source(repository_root: str | Path) -> dict:
    """Validate one clean Git worktree without changing its release inputs."""

    try:
        root = Path(repository_root).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise ReleaseValidationError(
            "repository_invalid", "发布输入目录不存在或无法解析。"
        ) from error
    if not root.is_dir():
        raise ReleaseValidationError(
            "repository_invalid", "发布输入目录必须是文件夹。"
        )

    git = _repository_metadata(root)
    configuration_path = _require_regular_file(
        root, CONFIGURATION_PATH, missing_code="configuration_invalid"
    )
    database_path = _require_regular_file(
        root, SOURCE_DATABASE_PATH, missing_code="database_missing"
    )
    _reject_database_sidecars(root, database_path)
    _require_tracked(root, CONFIGURATION_PATH)
    _require_tracked(root, SOURCE_DATABASE_PATH)
    if _git_status(root):
        raise ReleaseValidationError(
            "source_tree_not_clean", "发布输入 Git 工作树存在未提交改动。"
        )

    _raw_database_path(configuration_path)
    try:
        configuration = load_service_configuration(configuration_path)
    except ServiceConfigurationError as error:
        raise ReleaseValidationError("configuration_invalid", str(error)) from error
    if configuration.database_path != database_path.resolve():
        raise ReleaseValidationError(
            "configuration_database_mismatch",
            "服务配置没有指向受跟踪的发布基准数据库。",
            path=CONFIGURATION_PATH.as_posix(),
        )

    database_before = _file_state(database_path)
    configuration_before = _file_state(configuration_path)
    database = _read_database(
        database_path,
        timeout_seconds=configuration.database_timeout_seconds,
    )
    database_after = _file_state(database_path)
    configuration_after = _file_state(configuration_path)
    _reject_database_sidecars(root, database_path)
    if database_before != database_after or configuration_before != configuration_after:
        raise ReleaseValidationError(
            "database_changed_during_verification",
            "发布输入在验证期间发生变化。",
        )
    if _git_status(root):
        raise ReleaseValidationError(
            "database_changed_during_verification",
            "发布输入 Git 工作树在验证期间发生变化。",
        )

    database.update(
        {
            "path": SOURCE_DATABASE_PATH.as_posix(),
            "sha256": database_before[2],
        }
    )
    return {
        "status": "pass",
        "mode": "source",
        "git": git,
        "configuration": {
            "path": CONFIGURATION_PATH.as_posix(),
            "sha256": configuration_before[2],
            "database_path": SOURCE_DATABASE_PATH.as_posix(),
            "database_timeout_seconds": Decimal(
                str(configuration.database_timeout_seconds)
            ),
            "listen_host": configuration.listen_host,
            "listen_port": configuration.listen_port,
        },
        "database": database,
    }


def _standalone_database(value: str | Path, *, missing_code: str) -> Path:
    try:
        supplied = Path(value)
        path = supplied.resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise ReleaseValidationError(missing_code, "数据库文件不存在或无法解析。") from error
    if supplied.is_symlink() or not path.is_file():
        raise ReleaseValidationError(missing_code, "数据库必须是普通文件。")
    return path


def verify_seed(
    source_database: str | Path,
    seed_database: str | Path,
    *,
    timeout_seconds: float = 5.0,
) -> dict:
    """Verify one SQLite Backup API result against its source without writing either."""

    if type(timeout_seconds) not in (int, float) or not isfinite(timeout_seconds):
        raise ReleaseValidationError(
            "seed_input_invalid", "数据库超时必须是大于零的有限数字。"
        )
    if timeout_seconds <= 0:
        raise ReleaseValidationError(
            "seed_input_invalid", "数据库超时必须是大于零的有限数字。"
        )
    source_path = _standalone_database(
        source_database, missing_code="source_database_missing"
    )
    seed_path = _standalone_database(seed_database, missing_code="seed_database_missing")
    try:
        same_database = os.path.samefile(source_path, seed_path)
    except OSError as error:
        raise ReleaseValidationError(
            "seed_input_invalid", "无法确认源数据库与发布种子的文件身份。"
        ) from error
    if same_database:
        raise ReleaseValidationError(
            "seed_input_invalid", "发布种子不能与源数据库使用同一个文件。"
        )
    _reject_database_sidecars(source_path.parent, source_path)
    _reject_database_sidecars(seed_path.parent, seed_path)

    source_before = _file_state(source_path)
    seed_before = _file_state(seed_path)
    source = _read_database(source_path, timeout_seconds=timeout_seconds)
    seed = _read_database(seed_path, timeout_seconds=timeout_seconds)
    source_content_sha256 = _database_content_sha256(
        source_path, timeout_seconds=timeout_seconds
    )
    seed_content_sha256 = _database_content_sha256(
        seed_path, timeout_seconds=timeout_seconds
    )
    source_after = _file_state(source_path)
    seed_after = _file_state(seed_path)
    if source_before != source_after or seed_before != seed_after:
        raise ReleaseValidationError(
            "database_changed_during_verification",
            "源数据库或发布种子在验证期间发生变化。",
        )
    if source != seed or source_content_sha256 != seed_content_sha256:
        raise ReleaseValidationError(
            "seed_database_identity_mismatch", "发布种子与源数据库的逻辑身份不一致。"
        )
    return {
        "status": "pass",
        "mode": "seed",
        "source": {
            "sha256": source_before[2],
            "content_sha256": source_content_sha256,
            **source,
        },
        "seed": {
            "sha256": seed_before[2],
            "content_sha256": seed_content_sha256,
            **seed,
        },
    }


def _load_manifest(manifest_path: Path) -> dict:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str):
        raise ValueError(f"invalid JSON constant: {value}")

    try:
        value = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ReleaseValidationError(
            "manifest_invalid", "发布清单不是合法的 UTF-8 JSON。"
        ) from error
    if not isinstance(value, dict) or type(value.get("manifest_version")) is not int:
        raise ReleaseValidationError("manifest_invalid", "发布清单根结构无效。")
    if value["manifest_version"] != 1:
        raise ReleaseValidationError("manifest_invalid", "发布清单版本不受支持。")
    if not isinstance(value.get("files"), dict) or not isinstance(
        value.get("database"), dict
    ):
        raise ReleaseValidationError(
            "manifest_invalid", "发布清单缺少文件或数据库身份。"
        )
    return value


def _manifest_relative_path(value) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ReleaseValidationError("manifest_invalid", "发布清单文件路径无效。")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ReleaseValidationError(
            "manifest_invalid", "发布清单文件路径必须规范且相对。"
        )
    return value


def _package_files(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for directory, names, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in names:
            path = directory_path / name
            if path.is_symlink():
                raise ReleaseValidationError(
                    "package_layout_invalid",
                    "发布包不能包含符号链接。",
                    path=path.relative_to(root).as_posix(),
                )
        for name in filenames:
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise ReleaseValidationError(
                    "package_layout_invalid",
                    "发布包只能包含普通文件。",
                    path=relative,
                )
            files[relative] = path
    return files


def _validate_package_layout(files: dict[str, Path]) -> None:
    required = _REQUIRED_PACKAGE_FILES | {MANIFEST_PATH.as_posix()}
    missing = sorted(required - files.keys())
    if missing:
        raise ReleaseValidationError(
            "package_layout_invalid", "发布包缺少必需文件。", path=missing[0]
        )
    if not any(path.startswith("_internal/") for path in files):
        raise ReleaseValidationError(
            "package_layout_invalid", "发布包缺少 _internal 运行文件。", path="_internal"
        )
    for relative in files:
        if relative.endswith(SIDECAR_SUFFIXES):
            raise ReleaseValidationError(
                "database_sidecar_present",
                "发布包包含 SQLite 辅助文件。",
                path=relative,
            )
        if relative in {
            RUNTIME_CONFIGURATION_PATH.as_posix(),
            RUNTIME_DATABASE_PATH.as_posix(),
        }:
            raise ReleaseValidationError(
                "package_layout_invalid",
                "正式发布包不能包含现场配置或运行数据库。",
                path=relative,
            )
        allowed = (
            relative in required
            or relative.startswith("_internal/")
        )
        if not allowed:
            raise ReleaseValidationError(
                "package_layout_invalid", "发布包包含未允许的文件。", path=relative
            )


def _validate_manifest_files(root: Path, files: dict[str, Path], manifest: dict) -> None:
    declared = {}
    for raw_path, digest in manifest["files"].items():
        relative = _manifest_relative_path(raw_path)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ReleaseValidationError(
                "manifest_invalid", "发布清单包含无效 SHA-256。", path=relative
            )
        declared[relative] = digest
    actual_paths = set(files) - {MANIFEST_PATH.as_posix()}
    if set(declared) != actual_paths:
        difference = sorted(set(declared) ^ actual_paths)
        raise ReleaseValidationError(
            "manifest_invalid",
            "发布清单文件集合与目录不一致。",
            path=difference[0] if difference else None,
        )
    for relative, expected in declared.items():
        if _sha256(root / relative) != expected:
            raise ReleaseValidationError(
                "manifest_digest_mismatch",
                "发布文件摘要与清单不一致。",
                path=relative,
            )


def _validate_package_database_identity(manifest: dict, database: dict) -> None:
    declared = manifest["database"]
    required = {
        "source_path",
        "source_sha256",
        "seed_path",
        "seed_sha256",
        "schema_version",
        "active_version_id",
        "based_on_version_id",
        "rule_count",
        "enabled_rule_count",
        "virtual_prototype_count",
        "grade_dictionary_entry_count",
        "rule_set_fingerprint",
        "grade_dictionary_fingerprint",
    }
    if not required.issubset(declared):
        raise ReleaseValidationError(
            "manifest_invalid", "发布清单数据库身份字段不完整。"
        )
    source_sha256 = declared["source_sha256"]
    if (
        declared["source_path"] != SOURCE_DATABASE_PATH.as_posix()
        or not isinstance(source_sha256, str)
        or len(source_sha256) != 64
        or any(character not in "0123456789abcdef" for character in source_sha256)
    ):
        raise ReleaseValidationError("manifest_invalid", "发布清单源数据库身份无效。")
    expected = {
        "seed_path": PACKAGE_DATABASE_PATH.as_posix(),
        "seed_sha256": database["sha256"],
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
    for key, value in expected.items():
        if type(declared.get(key)) is not type(value) or declared.get(key) != value:
            raise ReleaseValidationError(
                "package_database_identity_mismatch",
                "发布种子数据库身份与清单不一致。",
                path=PACKAGE_DATABASE_PATH.as_posix(),
            )


def verify_package(package_root: str | Path) -> dict:
    """Validate one assembled portable-directory package without changing it."""

    try:
        root = Path(package_root).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise ReleaseValidationError(
            "package_layout_invalid", "发布包目录不存在或无法解析。"
        ) from error
    if not root.is_dir():
        raise ReleaseValidationError(
            "package_layout_invalid", "发布包路径必须是文件夹。"
        )

    files = _package_files(root)
    _validate_package_layout(files)
    manifest = _load_manifest(files[MANIFEST_PATH.as_posix()])
    _validate_manifest_files(root, files, manifest)

    configuration_path = files[PACKAGE_CONFIGURATION_PATH.as_posix()]
    _raw_database_path(configuration_path)
    try:
        configuration = load_service_configuration(configuration_path)
    except ServiceConfigurationError as error:
        raise ReleaseValidationError("configuration_invalid", str(error)) from error
    if configuration.database_path != (root / RUNTIME_DATABASE_PATH).resolve():
        raise ReleaseValidationError(
            "configuration_database_mismatch",
            "配置模板没有指向发布目录中的运行数据库。",
            path=PACKAGE_CONFIGURATION_PATH.as_posix(),
        )

    database_path = files[PACKAGE_DATABASE_PATH.as_posix()]
    database_before = _file_state(database_path)
    database = _read_database(
        database_path,
        timeout_seconds=configuration.database_timeout_seconds,
    )
    database_after = _file_state(database_path)
    if database_before != database_after:
        raise ReleaseValidationError(
            "database_changed_during_verification",
            "发布种子数据库在验证期间发生变化。",
        )
    database.update(
        {
            "path": PACKAGE_DATABASE_PATH.as_posix(),
            "sha256": database_before[2],
        }
    )
    _validate_package_database_identity(manifest, database)

    final_files = _package_files(root)
    _validate_package_layout(final_files)
    _validate_manifest_files(root, final_files, manifest)
    if set(files) != set(final_files):
        raise ReleaseValidationError(
            "database_changed_during_verification", "发布包在验证期间发生变化。"
        )
    return {
        "status": "pass",
        "mode": "package",
        "manifest_version": manifest["manifest_version"],
        "file_count": len(final_files) - 1,
        "database": database,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate APSGo V7 release inputs.")
    subparsers = parser.add_subparsers(dest="mode", required=True)
    source = subparsers.add_parser("source", help="validate one clean source worktree")
    source.add_argument("--repository-root", required=True)
    seed = subparsers.add_parser("seed", help="validate one generated database seed")
    seed.add_argument("--source-database", required=True)
    seed.add_argument("--seed-database", required=True)
    seed.add_argument("--timeout-seconds", type=float, default=5.0)
    package = subparsers.add_parser("package", help="validate one assembled release package")
    package.add_argument("--package-root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.mode == "source":
            result = verify_source(arguments.repository_root)
        elif arguments.mode == "seed":
            result = verify_seed(
                arguments.source_database,
                arguments.seed_database,
                timeout_seconds=arguments.timeout_seconds,
            )
        else:
            result = verify_package(arguments.package_root)
    except ReleaseValidationError as error:
        payload = {
            "status": "fail",
            "mode": arguments.mode,
            "error": {"code": error.code, "message": str(error), "path": error.path},
        }
        sys.stdout.write(f"{dumps_exact_json(payload)}\n")
        return 1
    sys.stdout.write(f"{dumps_exact_json(result)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
