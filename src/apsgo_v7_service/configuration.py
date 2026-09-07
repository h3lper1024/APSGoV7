"""Strict runtime configuration for the local APSGo V7 service."""

from __future__ import annotations

import json
from dataclasses import dataclass
from math import isfinite
from pathlib import Path

DEFAULT_CONFIGURATION_PATH = Path("config/apsgo_v7_service.json")
_EXPECTED_KEYS = frozenset(
    {
        "database_path",
        "database_timeout_seconds",
        "listen_host",
        "listen_port",
    }
)
_LOOPBACK_HOST = "127.0.0.1"


class ServiceConfigurationError(ValueError):
    """The service configuration file is missing, malformed, or unsafe."""


@dataclass(frozen=True, slots=True)
class ServiceConfiguration:
    database_path: Path
    database_timeout_seconds: float
    listen_host: str
    listen_port: int


def _reject_json_constant(value: str):
    raise ServiceConfigurationError(f"unsupported JSON constant: {value}")


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ServiceConfigurationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _configuration_file(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ServiceConfigurationError("configuration_path must be nonempty text or Path")
    try:
        path = Path(value).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise ServiceConfigurationError("configuration file does not exist") from error
    if not path.is_file():
        raise ServiceConfigurationError("configuration path must identify a file")
    return path


def _load_json(path: Path) -> dict:
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ServiceConfigurationError("configuration file must be UTF-8") from error
    except OSError as error:
        raise ServiceConfigurationError("configuration file cannot be read") from error
    try:
        value = json.loads(
            source,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as error:
        raise ServiceConfigurationError(
            f"configuration file contains invalid JSON at line {error.lineno} column {error.colno}"
        ) from error
    if not isinstance(value, dict):
        raise ServiceConfigurationError("configuration root must be a JSON object")
    keys = frozenset(value)
    if keys != _EXPECTED_KEYS:
        missing = sorted(_EXPECTED_KEYS - keys)
        unknown = sorted(keys - _EXPECTED_KEYS)
        details = []
        if missing:
            details.append(f"missing keys: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown keys: {', '.join(unknown)}")
        raise ServiceConfigurationError(
            "configuration keys must match exactly; " + "; ".join(details)
        )
    return value


def _database_path(value, configuration_directory: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ServiceConfigurationError("database_path must be nonempty text")
    configured = value.strip()
    if configured == ":memory:":
        raise ServiceConfigurationError("database_path must identify a persistent file")
    path = Path(configured)
    if not path.is_absolute():
        path = configuration_directory / path
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise ServiceConfigurationError("database_path is invalid") from error
    if resolved.exists() and not resolved.is_file():
        raise ServiceConfigurationError("database_path must not identify a directory")
    return resolved


def _database_timeout_seconds(value) -> float:
    if type(value) not in (int, float):
        raise ServiceConfigurationError("database_timeout_seconds must be a number")
    try:
        result = float(value)
    except OverflowError as error:
        raise ServiceConfigurationError(
            "database_timeout_seconds must be finite and greater than zero"
        ) from error
    if not isfinite(result) or result <= 0:
        raise ServiceConfigurationError(
            "database_timeout_seconds must be finite and greater than zero"
        )
    return result


def _listen_host(value) -> str:
    if value != _LOOPBACK_HOST or not isinstance(value, str):
        raise ServiceConfigurationError(f"listen_host must be exactly {_LOOPBACK_HOST}")
    return value


def _listen_port(value) -> int:
    if type(value) is not int or not 1 <= value <= 65_535:
        raise ServiceConfigurationError("listen_port must be an integer from 1 through 65535")
    return value


def load_service_configuration(
    configuration_path: str | Path = DEFAULT_CONFIGURATION_PATH,
) -> ServiceConfiguration:
    """Read and freeze one complete configuration before service work begins."""

    path = _configuration_file(configuration_path)
    values = _load_json(path)
    return ServiceConfiguration(
        database_path=_database_path(values["database_path"], path.parent),
        database_timeout_seconds=_database_timeout_seconds(values["database_timeout_seconds"]),
        listen_host=_listen_host(values["listen_host"]),
        listen_port=_listen_port(values["listen_port"]),
    )


__all__ = [
    "DEFAULT_CONFIGURATION_PATH",
    "ServiceConfiguration",
    "ServiceConfigurationError",
    "load_service_configuration",
]
