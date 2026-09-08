#!/usr/bin/env python3
"""Read-only validation for APSGo V7 release inputs and assembled packages."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime
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
from apsgo_v7_service.rule_store import SCHEMA_VERSION, RuleStore, RuleStoreSchemaError

CONFIGURATION_PATH = Path("config/apsgo_v7_service.yaml")
SOURCE_DATABASE_PATH = Path("data/apsgo_v7_rules.sqlite3")
PACKAGE_CONFIGURATION_PATH = Path("config/apsgo_v7_service.example.yaml")
PACKAGE_DATABASE_PATH = Path("data/apsgo_v7_rules_seed.sqlite3")
RUNTIME_CONFIGURATION_PATH = Path("config/apsgo_v7_service.yaml")
RUNTIME_DATABASE_PATH = Path("data/apsgo_v7_rules.sqlite3")
MANIFEST_PATH = Path("release_manifest.json")
PROJECT_METADATA_PATH = Path("pyproject.toml")
SIDECAR_SUFFIXES = ("-journal", "-shm", "-wal")
EXPECTED_CONDA_ENVIRONMENT = "aps_3.10.18"
EXPECTED_PYTHON_VERSION = "3.10.18"
EXPECTED_PYINSTALLER_VERSION = "6.22.2"
_UTC_BUILD_TIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
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


def _project_version(root: Path) -> str:
    metadata_path = _require_regular_file(
        root, PROJECT_METADATA_PATH, missing_code="project_metadata_invalid"
    )
    _require_tracked(root, PROJECT_METADATA_PATH)
    try:
        text = metadata_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ReleaseValidationError(
            "project_metadata_invalid", "pyproject.toml 不是合法的 UTF-8 文本。"
        ) from error
    section = re.search(r"(?ms)^\[project\]\s*$\n(?P<body>.*?)(?=^\[|\Z)", text)
    version_match = (
        None
        if section is None
        else re.search(r'(?m)^version\s*=\s*"(?P<version>[^"]+)"\s*$', section["body"])
    )
    if version_match is None or not version_match["version"].strip():
        raise ReleaseValidationError(
            "project_metadata_invalid", "pyproject.toml 缺少 [project].version。"
        )
    return version_match["version"]


def _installed_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError as error:
        raise ReleaseValidationError(
            "build_environment_invalid", f"构建环境缺少 {distribution}。"
        ) from error


def _build_environment() -> dict:
    return {
        "operating_system": platform.system(),
        "architecture": platform.machine(),
        "conda_environment": EXPECTED_CONDA_ENVIRONMENT,
        "python_version": ".".join(str(part) for part in sys.version_info[:3]),
        "pyinstaller_version": _installed_version("pyinstaller"),
        "fastapi_version": _installed_version("fastapi"),
        "uvicorn_version": _installed_version("uvicorn"),
        "pyyaml_version": _installed_version("PyYAML"),
    }


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


_MANIFEST_ROOT_KEYS = frozenset(
    ("manifest_version", "application", "source", "build", "database", "validation", "files")
)
_APPLICATION_KEYS = frozenset(("name", "package_name", "project_name", "project_version"))
_SOURCE_KEYS = frozenset(
    ("git_commit", "git_branch", "configuration_path", "configuration_sha256")
)
_BUILD_KEYS = frozenset(
    (
        "built_at_utc",
        "operating_system",
        "architecture",
        "conda_environment",
        "python_version",
        "pyinstaller_version",
        "fastapi_version",
        "uvicorn_version",
        "pyyaml_version",
        "mode",
        "console",
        "upx",
        "entry",
        "service_entry",
        "worker_count",
    )
)
_DATABASE_KEYS = frozenset(
    (
        "source_path",
        "source_sha256",
        "source_content_sha256",
        "seed_path",
        "seed_sha256",
        "seed_content_sha256",
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
)
_VALIDATION_KEYS = frozenset(("build", "source", "seed", "package_static", "smoke"))


def _require_exact_keys(value, expected: frozenset[str], subject: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ReleaseValidationError(
            "manifest_invalid", f"发布清单 {subject} 字段集合无效。"
        )


def _require_text(value, subject: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ReleaseValidationError(
            "manifest_invalid", f"发布清单 {subject} 必须是非空文本。"
        )
    if any(ord(character) < 32 for character in value):
        raise ReleaseValidationError(
            "manifest_invalid", f"发布清单 {subject} 包含控制字符。"
        )
    return value


def _require_sha256(value, subject: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ReleaseValidationError(
            "manifest_invalid", f"发布清单 {subject} 不是规范 SHA-256。"
        )
    return value


def _require_positive_int(value, subject: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum:
        raise ReleaseValidationError(
            "manifest_invalid", f"发布清单 {subject} 必须是不小于 {minimum} 的整数。"
        )
    return value


def _validate_build_time(value) -> str:
    text = _require_text(value, "build.built_at_utc")
    if _UTC_BUILD_TIME_PATTERN.fullmatch(text) is None:
        raise ReleaseValidationError(
            "manifest_invalid", "发布清单构建时间必须是 UTC 秒级时间。"
        )
    try:
        datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise ReleaseValidationError(
            "manifest_invalid", "发布清单构建时间不是有效日期。"
        ) from error
    return text


def _validate_manifest_schema(manifest: dict, *, allow_pending_smoke: bool) -> None:
    _require_exact_keys(manifest, _MANIFEST_ROOT_KEYS, "根对象")
    if type(manifest["manifest_version"]) is not int or manifest["manifest_version"] != 1:
        raise ReleaseValidationError("manifest_invalid", "发布清单版本不受支持。")

    application = manifest["application"]
    _require_exact_keys(application, _APPLICATION_KEYS, "application")
    if (
        application["name"] != "APSGoV7Service"
        or application["package_name"] != "APSGoV7"
        or application["project_name"] != "apsgo-scheduler"
    ):
        raise ReleaseValidationError("manifest_invalid", "发布清单应用身份无效。")
    project_version = _require_text(
        application["project_version"], "application.project_version"
    )
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]*", project_version) is None:
        raise ReleaseValidationError("manifest_invalid", "发布清单项目版本无效。")

    source = manifest["source"]
    _require_exact_keys(source, _SOURCE_KEYS, "source")
    commit = _require_text(source["git_commit"], "source.git_commit")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ReleaseValidationError("manifest_invalid", "发布清单 Git 身份无效。")
    if source["git_branch"] is not None:
        _require_text(source["git_branch"], "source.git_branch")
    if source["configuration_path"] != CONFIGURATION_PATH.as_posix():
        raise ReleaseValidationError("manifest_invalid", "发布清单配置路径无效。")
    _require_sha256(source["configuration_sha256"], "source.configuration_sha256")

    build = manifest["build"]
    _require_exact_keys(build, _BUILD_KEYS, "build")
    _validate_build_time(build["built_at_utc"])
    if (
        build["operating_system"] != "Windows"
        or build["architecture"] not in {"AMD64", "x86_64"}
        or build["conda_environment"] != EXPECTED_CONDA_ENVIRONMENT
        or build["python_version"] != EXPECTED_PYTHON_VERSION
        or build["pyinstaller_version"] != EXPECTED_PYINSTALLER_VERSION
        or build["mode"] != "onedir"
        or build["console"] is not True
        or build["upx"] is not False
        or build["entry"] != "release/apsgo_v7_service_entry.py"
        or build["service_entry"] != "apsgo_v7_service.app:main"
        or type(build["worker_count"]) is not int
        or build["worker_count"] != 1
    ):
        raise ReleaseValidationError("manifest_invalid", "发布清单构建契约无效。")
    for key in ("fastapi_version", "uvicorn_version", "pyyaml_version"):
        version = _require_text(build[key], f"build.{key}")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]*", version) is None:
            raise ReleaseValidationError(
                "manifest_invalid", f"发布清单 build.{key} 版本无效。"
            )

    database = manifest["database"]
    _require_exact_keys(database, _DATABASE_KEYS, "database")
    if (
        database["source_path"] != SOURCE_DATABASE_PATH.as_posix()
        or database["seed_path"] != PACKAGE_DATABASE_PATH.as_posix()
    ):
        raise ReleaseValidationError("manifest_invalid", "发布清单数据库路径无效。")
    for key in (
        "source_sha256",
        "source_content_sha256",
        "seed_sha256",
        "seed_content_sha256",
        "rule_set_fingerprint",
        "grade_dictionary_fingerprint",
    ):
        _require_sha256(database[key], f"database.{key}")
    _require_positive_int(database["schema_version"], "database.schema_version")
    _require_positive_int(database["active_version_id"], "database.active_version_id")
    based_on = database["based_on_version_id"]
    if based_on is not None:
        _require_positive_int(based_on, "database.based_on_version_id")
    rule_count = _require_positive_int(database["rule_count"], "database.rule_count")
    enabled_count = _require_positive_int(
        database["enabled_rule_count"], "database.enabled_rule_count", allow_zero=True
    )
    if enabled_count > rule_count:
        raise ReleaseValidationError("manifest_invalid", "发布清单启用规则数量无效。")
    _require_positive_int(
        database["virtual_prototype_count"],
        "database.virtual_prototype_count",
        allow_zero=True,
    )
    _require_positive_int(
        database["grade_dictionary_entry_count"],
        "database.grade_dictionary_entry_count",
        allow_zero=True,
    )

    validation = manifest["validation"]
    _require_exact_keys(validation, _VALIDATION_KEYS, "validation")
    for key in ("build", "source", "seed", "package_static"):
        if validation[key] != "pass":
            raise ReleaseValidationError("manifest_invalid", "发布清单验证状态无效。")
    allowed_smoke = {"pass", "pending"} if allow_pending_smoke else {"pass"}
    if validation["smoke"] not in allowed_smoke:
        raise ReleaseValidationError("manifest_invalid", "发布清单冒烟状态无效。")
    if not isinstance(manifest["files"], dict) or not manifest["files"]:
        raise ReleaseValidationError("manifest_invalid", "发布清单文件集合无效。")


def _load_manifest(manifest_path: Path, *, allow_pending_smoke: bool) -> dict:
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
        raw = manifest_path.read_bytes()
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_float=Decimal,
            parse_constant=reject_constant,
        )
        _validate_manifest_schema(value, allow_pending_smoke=allow_pending_smoke)
        canonical = f"{dumps_exact_json(value)}\n".encode("utf-8")
    except ReleaseValidationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ReleaseValidationError(
            "manifest_invalid", "发布清单不是合法的规范 UTF-8 JSON。"
        ) from error
    if raw != canonical:
        raise ReleaseValidationError(
            "manifest_invalid", "发布清单必须使用固定键序、UTF-8 无 BOM 和单个 LF 结尾。"
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
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    files: dict[str, Path] = {}
    for directory, names, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in names:
            path = directory_path / name
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
            if path.is_symlink() or (reparse_flag and attributes & reparse_flag):
                raise ReleaseValidationError(
                    "package_layout_invalid",
                    "发布包不能包含符号链接或 Windows reparse point。",
                    path=path.relative_to(root).as_posix(),
                )
        for name in filenames:
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            file_state = path.lstat()
            attributes = getattr(file_state, "st_file_attributes", 0)
            if (
                path.is_symlink()
                or (reparse_flag and attributes & reparse_flag)
                or not stat.S_ISREG(file_state.st_mode)
            ):
                raise ReleaseValidationError(
                    "package_layout_invalid",
                    "发布包只能包含普通文件。",
                    path=relative,
                )
            files[relative] = path
    return files


def _validate_package_layout(files: dict[str, Path], *, require_manifest: bool) -> None:
    manifest_path = MANIFEST_PATH.as_posix()
    required = _REQUIRED_PACKAGE_FILES | ({manifest_path} if require_manifest else set())
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
        parts = PurePosixPath(relative).parts
        if (
            "__pycache__" in parts
            or ".pytest_cache" in parts
            or "tests" in parts
            or relative.endswith((".py", ".pyi", ".spec", ".log", ".DS_Store"))
        ):
            raise ReleaseValidationError(
                "package_layout_invalid",
                "正式发布包不能包含源码、测试、缓存或日志。",
                path=relative,
            )
        allowed = relative in (_REQUIRED_PACKAGE_FILES | {manifest_path}) or relative.startswith(
            "_internal/"
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
    expected = {
        "seed_path": PACKAGE_DATABASE_PATH.as_posix(),
        "seed_sha256": database["sha256"],
        "seed_content_sha256": database["content_sha256"],
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
    if declared["source_content_sha256"] != declared["seed_content_sha256"]:
        raise ReleaseValidationError(
            "package_database_identity_mismatch",
            "发布种子与源数据库的逻辑摘要不一致。",
            path=PACKAGE_DATABASE_PATH.as_posix(),
        )


def _package_root(value: str | Path) -> Path:
    try:
        root = Path(value).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise ReleaseValidationError(
            "package_layout_invalid", "发布包目录不存在或无法解析。"
        ) from error
    if not root.is_dir():
        raise ReleaseValidationError("package_layout_invalid", "发布包路径必须是文件夹。")
    return root


def _inspect_package(root: Path, *, require_manifest: bool) -> tuple[dict[str, Path], dict]:
    files = _package_files(root)
    _validate_package_layout(files, require_manifest=require_manifest)
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
        database_path, timeout_seconds=configuration.database_timeout_seconds
    )
    content_sha256 = _database_content_sha256(
        database_path, timeout_seconds=configuration.database_timeout_seconds
    )
    database_after = _file_state(database_path)
    if database_before != database_after:
        raise ReleaseValidationError(
            "database_changed_during_verification", "发布种子数据库在验证期间发生变化。"
        )
    database.update(
        {
            "path": PACKAGE_DATABASE_PATH.as_posix(),
            "sha256": database_before[2],
            "content_sha256": content_sha256,
            "database_timeout_seconds": Decimal(
                str(configuration.database_timeout_seconds)
            ),
        }
    )
    return files, database


def generate_manifest(
    repository_root: str | Path,
    package_root: str | Path,
    *,
    built_at_utc: str,
    smoke_status: str,
) -> dict:
    """Generate one canonical manifest value without writing package files."""

    source = verify_source(repository_root)
    root = _package_root(package_root)
    files, package_database = _inspect_package(root, require_manifest=False)
    seed = verify_seed(
        Path(repository_root).resolve() / SOURCE_DATABASE_PATH,
        root / PACKAGE_DATABASE_PATH,
        timeout_seconds=float(source["configuration"]["database_timeout_seconds"]),
    )
    if _sha256(root / PACKAGE_CONFIGURATION_PATH) != source["configuration"]["sha256"]:
        raise ReleaseValidationError(
            "configuration_database_mismatch",
            "包内配置模板与本次构建源配置不一致。",
            path=PACKAGE_CONFIGURATION_PATH.as_posix(),
        )

    build = _build_environment()
    build.update(
        {
            "built_at_utc": built_at_utc,
            "mode": "onedir",
            "console": True,
            "upx": False,
            "entry": "release/apsgo_v7_service_entry.py",
            "service_entry": "apsgo_v7_service.app:main",
            "worker_count": 1,
        }
    )
    database = {
        "source_path": SOURCE_DATABASE_PATH.as_posix(),
        "source_sha256": seed["source"]["sha256"],
        "source_content_sha256": seed["source"]["content_sha256"],
        "seed_path": PACKAGE_DATABASE_PATH.as_posix(),
        "seed_sha256": seed["seed"]["sha256"],
        "seed_content_sha256": seed["seed"]["content_sha256"],
        **{
            key: package_database[key]
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
    manifest = {
        "manifest_version": 1,
        "application": {
            "name": "APSGoV7Service",
            "package_name": "APSGoV7",
            "project_name": "apsgo-scheduler",
            "project_version": _project_version(Path(repository_root).resolve()),
        },
        "source": {
            "git_commit": source["git"]["commit"],
            "git_branch": source["git"]["branch"],
            "configuration_path": CONFIGURATION_PATH.as_posix(),
            "configuration_sha256": source["configuration"]["sha256"],
        },
        "build": build,
        "database": database,
        "validation": {
            "build": "pass",
            "source": "pass",
            "seed": "pass",
            "package_static": "pass",
            "smoke": smoke_status,
        },
        "files": {
            relative: _sha256(path)
            for relative, path in files.items()
            if relative != MANIFEST_PATH.as_posix()
        },
    }
    _validate_manifest_schema(manifest, allow_pending_smoke=True)
    return manifest


def verify_package(
    package_root: str | Path, *, allow_pending_smoke: bool = False
) -> dict:
    """Validate one assembled portable-directory package without changing it."""

    root = _package_root(package_root)
    files, database = _inspect_package(root, require_manifest=True)
    manifest = _load_manifest(
        files[MANIFEST_PATH.as_posix()], allow_pending_smoke=allow_pending_smoke
    )
    _validate_manifest_files(root, files, manifest)
    _validate_package_database_identity(manifest, database)
    if (
        manifest["source"]["configuration_sha256"]
        != _sha256(root / PACKAGE_CONFIGURATION_PATH)
    ):
        raise ReleaseValidationError(
            "manifest_digest_mismatch",
            "配置模板摘要与发布清单源身份不一致。",
            path=PACKAGE_CONFIGURATION_PATH.as_posix(),
        )

    final_files = _package_files(root)
    _validate_package_layout(final_files, require_manifest=True)
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
        "smoke_status": manifest["validation"]["smoke"],
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
    manifest = subparsers.add_parser("manifest", help="generate one release manifest")
    manifest.add_argument("--repository-root", required=True)
    manifest.add_argument("--package-root", required=True)
    manifest.add_argument("--built-at-utc", required=True)
    manifest.add_argument("--smoke-status", choices=("pending", "pass"), required=True)
    package = subparsers.add_parser("package", help="validate one assembled release package")
    package.add_argument("--package-root", required=True)
    package.add_argument("--allow-pending-smoke", action="store_true")
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
        elif arguments.mode == "manifest":
            result = generate_manifest(
                arguments.repository_root,
                arguments.package_root,
                built_at_utc=arguments.built_at_utc,
                smoke_status=arguments.smoke_status,
            )
        else:
            result = verify_package(
                arguments.package_root,
                allow_pending_smoke=arguments.allow_pending_smoke,
            )
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
