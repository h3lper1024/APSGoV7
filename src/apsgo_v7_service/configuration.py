"""Strict runtime configuration for the local APSGo V7 service."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import isfinite
from pathlib import Path

import yaml

from apsgo_scheduler.core.contracts import (
    CONSTRUCTION_ORDER_KEY,
    NUMERIC_SEMANTICS_KEY,
    SolverPolicy,
)

DEFAULT_CONFIGURATION_PATH = Path("config/apsgo_v7_service.yaml")
_EXPECTED_KEYS = frozenset(
    {
        "database_path",
        "database_timeout_seconds",
        "listen_host",
        "listen_port",
        "monthly_solve",
    }
)
_EXPECTED_MONTHLY_SOLVE_KEYS = frozenset(
    {
        "seed",
        "total_time_limit_seconds",
        "finalization_reserve_seconds",
        "candidate_check_limit",
        "whole_chain_pair_scan_slack_weight",
        "maximum_virtual_bridge_nodes",
    }
)
_OPTIONAL_KEYS = frozenset({"diagnostics"})
_EXPECTED_DIAGNOSTICS_KEYS = frozenset({"enabled", "output_directory"})
_ALLOWED_LISTEN_HOSTS = frozenset({"127.0.0.1", "0.0.0.0"})


class ServiceConfigurationError(ValueError):
    """The service configuration file is missing, malformed, or unsafe."""


@dataclass(frozen=True, slots=True)
class ServiceConfiguration:
    database_path: Path
    database_timeout_seconds: float
    listen_host: str
    listen_port: int
    monthly_solve_policy: SolverPolicy
    diagnostics_directory: Path | None = None


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
    if keys - _OPTIONAL_KEYS != _EXPECTED_KEYS:
        missing = sorted(_EXPECTED_KEYS - keys)
        unknown = sorted(keys - _EXPECTED_KEYS - _OPTIONAL_KEYS)
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


def _diagnostics_directory(value, configuration_directory: Path) -> Path | None:
    if not isinstance(value, dict):
        raise ServiceConfigurationError("diagnostics must be a YAML mapping")
    keys = frozenset(value)
    if keys != _EXPECTED_DIAGNOSTICS_KEYS:
        missing = sorted(_EXPECTED_DIAGNOSTICS_KEYS - keys)
        unknown = sorted(keys - _EXPECTED_DIAGNOSTICS_KEYS)
        details = []
        if missing:
            details.append(f"missing keys: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown keys: {', '.join(unknown)}")
        raise ServiceConfigurationError(
            "diagnostics keys must match exactly; " + "; ".join(details)
        )
    if type(value["enabled"]) is not bool:
        raise ServiceConfigurationError("diagnostics.enabled must be a boolean")
    directory = value["output_directory"]
    if not isinstance(directory, str) or not directory.strip():
        raise ServiceConfigurationError(
            "diagnostics.output_directory must be nonempty text"
        )
    try:
        path = Path(directory.strip())
        if not path.is_absolute():
            path = configuration_directory / path
        resolved = path.resolve()
        if resolved.exists() and not resolved.is_dir():
            raise ServiceConfigurationError(
                "diagnostics.output_directory must identify a directory"
            )
    except ServiceConfigurationError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise ServiceConfigurationError(
            "diagnostics.output_directory is invalid"
        ) from error
    return resolved if value["enabled"] else None


def _monthly_solve_policy(value) -> SolverPolicy:
    if not isinstance(value, dict):
        raise ServiceConfigurationError("monthly_solve must be a YAML mapping")
    keys = frozenset(value)
    if keys != _EXPECTED_MONTHLY_SOLVE_KEYS:
        missing = sorted(_EXPECTED_MONTHLY_SOLVE_KEYS - keys)
        unknown = sorted(keys - _EXPECTED_MONTHLY_SOLVE_KEYS)
        details = []
        if missing:
            details.append(f"missing keys: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown keys: {', '.join(unknown)}")
        raise ServiceConfigurationError(
            "monthly_solve keys must match exactly; " + "; ".join(details)
        )
    for name in ("seed", "candidate_check_limit", "maximum_virtual_bridge_nodes"):
        if type(value[name]) is not int:
            raise ServiceConfigurationError(f"monthly_solve.{name} must be an integer")
    decimals = {}
    for name in (
        "total_time_limit_seconds",
        "finalization_reserve_seconds",
        "whole_chain_pair_scan_slack_weight",
    ):
        if type(value[name]) not in (int, float):
            raise ServiceConfigurationError(f"monthly_solve.{name} must be a number")
        decimals[name] = Decimal(str(value[name]))
    try:
        return SolverPolicy(
            seed=value["seed"],
            total_time_limit_seconds=decimals["total_time_limit_seconds"],
            finalization_reserve_seconds=decimals["finalization_reserve_seconds"],
            candidate_check_limit=value["candidate_check_limit"],
            construction_order_key=CONSTRUCTION_ORDER_KEY,
            numeric_semantics_key=NUMERIC_SEMANTICS_KEY,
            whole_chain_pair_scan_slack_weight=decimals[
                "whole_chain_pair_scan_slack_weight"
            ],
            maximum_virtual_bridge_nodes=value["maximum_virtual_bridge_nodes"],
        )
    except ValueError as error:
        raise ServiceConfigurationError(f"monthly_solve is invalid: {error}") from error


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
        monthly_solve_policy=_monthly_solve_policy(values["monthly_solve"]),
        diagnostics_directory=(
            _diagnostics_directory(values["diagnostics"], path.parent)
            if "diagnostics" in values
            else None
        ),
    )


__all__ = [
    "DEFAULT_CONFIGURATION_PATH",
    "ServiceConfiguration",
    "ServiceConfigurationError",
    "load_service_configuration",
]
