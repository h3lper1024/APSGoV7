"""Strict runtime configuration for the local APSGo V7 service."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from pathlib import Path

import yaml

DEFAULT_CONFIGURATION_PATH = Path("config/apsgo_v7_service.yaml")
_EXPECTED_KEYS = frozenset(
    {
        "database_path",
        "database_timeout_seconds",
        "listen_host",
        "listen_port",
    }
)
_ALLOWED_LISTEN_HOSTS = frozenset({"127.0.0.1", "0.0.0.0"})


class ServiceConfigurationError(ValueError):
    """The service configuration file is missing, malformed, or unsafe."""


@dataclass(frozen=True, slots=True)
class ServiceConfiguration:
    database_path: Path
    database_timeout_seconds: float
    listen_host: str
    listen_port: int


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ServiceConfigurationError("configuration keys must be text")
        if key in result:
            raise ServiceConfigurationError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


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


def _load_yaml(path: Path) -> dict:
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ServiceConfigurationError("configuration file must be UTF-8") from error
    except OSError as error:
        raise ServiceConfigurationError("configuration file cannot be read") from error
    try:
        value = yaml.load(source, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as error:
        raise ServiceConfigurationError("configuration file contains invalid YAML") from error
    if not isinstance(value, dict):
        raise ServiceConfigurationError("configuration root must be a YAML mapping")
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
    if not isinstance(value, str) or value not in _ALLOWED_LISTEN_HOSTS:
        raise ServiceConfigurationError("listen_host must be 127.0.0.1 or 0.0.0.0")
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
    values = _load_yaml(path)
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
