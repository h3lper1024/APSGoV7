from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from apsgo_scheduler.core.contracts import CONSTRUCTION_ORDER_KEY, NUMERIC_SEMANTICS_KEY
from apsgo_v7_service.configuration import (
    ServiceConfigurationError,
    load_service_configuration,
)

ROOT = Path(__file__).resolve().parents[2]


def _monthly_solve(**changes):
    values = {
        "seed": 590531,
        "total_time_limit_seconds": 180,
        "finalization_reserve_seconds": 10,
        "candidate_check_limit": 200000,
        "whole_chain_pair_scan_slack_weight": 40,
        "maximum_virtual_bridge_nodes": 2,
    }
    values.update(changes)
    return values


def _values(**changes):
    values = {
        "database_path": "../data/rules.sqlite3",
        "database_timeout_seconds": 5.0,
        "listen_host": "127.0.0.1",
        "listen_port": 8001,
        "monthly_solve": _monthly_solve(),
    }
    values.update(changes)
    return values


def _write(configuration_path, values=None):
    configuration_path.parent.mkdir(parents=True, exist_ok=True)
    configuration_path.write_text(
        yaml.safe_dump(_values() if values is None else values, sort_keys=False),
        encoding="utf-8",
    )
    return configuration_path


def test_tracked_default_configuration_points_to_the_v7_database():
    result = load_service_configuration(ROOT / "config" / "apsgo_v7_service.yaml")

    assert result.database_path == ROOT / "data" / "apsgo_v7_rules.sqlite3"
    assert result.database_timeout_seconds == 5.0
    assert result.listen_host == "0.0.0.0"
    assert result.listen_port == 8001
    assert result.monthly_solve_policy.seed == 590531
    assert result.monthly_solve_policy.total_time_limit_seconds == Decimal("180")
    assert result.monthly_solve_policy.finalization_reserve_seconds == Decimal("10")
    assert result.monthly_solve_policy.candidate_check_limit == 200000
    assert result.monthly_solve_policy.construction_order_key == CONSTRUCTION_ORDER_KEY
    assert result.monthly_solve_policy.numeric_semantics_key == NUMERIC_SEMANTICS_KEY
    assert result.monthly_solve_policy.whole_chain_pair_scan_slack_weight == Decimal("40")
    assert result.monthly_solve_policy.maximum_virtual_bridge_nodes == 2


def test_configuration_resolves_database_relative_to_its_own_file(tmp_path, monkeypatch):
    configuration_path = _write(tmp_path / "deployment" / "config" / "service.yaml")
    expected_database = (configuration_path.parent / "../data/rules.sqlite3").resolve()

    monkeypatch.chdir(tmp_path)
    first = load_service_configuration(configuration_path)
    monkeypatch.chdir(tmp_path / "deployment")
    second = load_service_configuration(configuration_path)

    assert first == second
    assert first.database_path == expected_database
    assert first.database_timeout_seconds == 5.0
    assert first.listen_host == "127.0.0.1"
    assert first.listen_port == 8001
    with pytest.raises(FrozenInstanceError):
        first.listen_port = 9000
    with pytest.raises(FrozenInstanceError):
        first.monthly_solve_policy.seed = 1


def test_configuration_preserves_an_absolute_database_target(tmp_path):
    database_path = tmp_path / "database" / "rules.sqlite3"
    configuration_path = _write(
        tmp_path / "config.yaml",
        _values(database_path=str(database_path)),
    )

    result = load_service_configuration(configuration_path)

    assert result.database_path == database_path.resolve()


def test_configuration_loads_valid_monthly_solve_values(tmp_path):
    configuration_path = _write(
        tmp_path / "config.yaml",
        _values(
            monthly_solve=_monthly_solve(
                seed=-1,
                total_time_limit_seconds=12.125,
                finalization_reserve_seconds=2.025,
                candidate_check_limit=0,
                whole_chain_pair_scan_slack_weight=0.5,
                maximum_virtual_bridge_nodes=0,
            )
        ),
    )

    policy = load_service_configuration(configuration_path).monthly_solve_policy

    assert policy.seed == -1
    assert policy.total_time_limit_seconds == Decimal("12.125")
    assert policy.finalization_reserve_seconds == Decimal("2.025")
    assert policy.candidate_check_limit == 0
    assert policy.whole_chain_pair_scan_slack_weight == Decimal("0.5")
    assert policy.maximum_virtual_bridge_nodes == 0


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("[]", "root must be a YAML mapping"),
        ("database_path: [", "invalid YAML"),
        (
            "database_path: one\ndatabase_path: two\n"
            "database_timeout_seconds: 5\nlisten_host: 127.0.0.1\nlisten_port: 8001\n",
            "duplicate YAML key: database_path",
        ),
        (
            "database_path: !!python/name:builtins.str ''\n"
            "database_timeout_seconds: 5\nlisten_host: 127.0.0.1\nlisten_port: 8001\n",
            "invalid YAML",
        ),
        (
            "database_path: rules.sqlite3\ndatabase_timeout_seconds: .nan\n"
            "listen_host: 127.0.0.1\nlisten_port: 8001\n"
            "monthly_solve: {seed: 590531, total_time_limit_seconds: 180, "
            "finalization_reserve_seconds: 10, candidate_check_limit: 200000, "
            "whole_chain_pair_scan_slack_weight: 40, maximum_virtual_bridge_nodes: 2}\n",
            "must be finite and greater than zero",
        ),
        (
            "database_path: rules.sqlite3\ndatabase_timeout_seconds: .inf\n"
            "listen_host: 127.0.0.1\nlisten_port: 8001\n"
            "monthly_solve: {seed: 590531, total_time_limit_seconds: 180, "
            "finalization_reserve_seconds: 10, candidate_check_limit: 200000, "
            "whole_chain_pair_scan_slack_weight: 40, maximum_virtual_bridge_nodes: 2}\n",
            "must be finite and greater than zero",
        ),
    ],
)
def test_configuration_rejects_invalid_yaml_shapes(tmp_path, source, message):
    configuration_path = tmp_path / "config.yaml"
    configuration_path.write_text(source, encoding="utf-8")

    with pytest.raises(ServiceConfigurationError, match=message):
        load_service_configuration(configuration_path)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"extra": True}, "unknown keys: extra"),
        ({"listen_port": None}, "listen_port must be an integer"),
        ({"listen_port": True}, "listen_port must be an integer"),
        ({"listen_port": 0}, "listen_port must be an integer"),
        ({"listen_port": 65_536}, "listen_port must be an integer"),
        ({"listen_host": "localhost"}, "listen_host must be 127.0.0.1 or 0.0.0.0"),
        ({"database_timeout_seconds": "5"}, "must be a number"),
        ({"database_timeout_seconds": True}, "must be a number"),
        ({"database_timeout_seconds": 0}, "must be finite and greater than zero"),
        ({"database_path": " "}, "database_path must be nonempty text"),
        ({"database_path": ":memory:"}, "database_path must identify a persistent file"),
    ],
)
def test_configuration_rejects_invalid_values(tmp_path, changes, message):
    configuration_path = _write(tmp_path / "config.yaml", _values(**changes))

    with pytest.raises(ServiceConfigurationError, match=message):
        load_service_configuration(configuration_path)


def test_configuration_rejects_missing_unknown_and_non_utf8_files(tmp_path):
    with pytest.raises(ServiceConfigurationError, match="does not exist"):
        load_service_configuration(tmp_path / "missing.yaml")

    missing_key = _values()
    missing_key.pop("listen_port")
    with pytest.raises(ServiceConfigurationError, match="missing keys: listen_port"):
        load_service_configuration(_write(tmp_path / "missing-key.yaml", missing_key))

    missing_policy = _values()
    missing_policy.pop("monthly_solve")
    with pytest.raises(ServiceConfigurationError, match="missing keys: monthly_solve"):
        load_service_configuration(_write(tmp_path / "missing-policy.yaml", missing_policy))

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(ServiceConfigurationError, match="must identify a file"):
        load_service_configuration(directory)

    non_utf8 = tmp_path / "non-utf8.yaml"
    non_utf8.write_bytes(b"\xff")
    with pytest.raises(ServiceConfigurationError, match="must be UTF-8"):
        load_service_configuration(non_utf8)


@pytest.mark.parametrize(
    ("monthly_solve", "message"),
    [
        (None, "monthly_solve must be a YAML mapping"),
        ({}, "missing keys:"),
        (_monthly_solve(extra=1), "unknown keys: extra"),
        (_monthly_solve(seed=True), "monthly_solve.seed must be an integer"),
        (
            _monthly_solve(candidate_check_limit=1.0),
            "monthly_solve.candidate_check_limit must be an integer",
        ),
        (
            _monthly_solve(maximum_virtual_bridge_nodes=False),
            "monthly_solve.maximum_virtual_bridge_nodes must be an integer",
        ),
        (
            _monthly_solve(total_time_limit_seconds="180"),
            "monthly_solve.total_time_limit_seconds must be a number",
        ),
        (
            _monthly_solve(finalization_reserve_seconds=True),
            "monthly_solve.finalization_reserve_seconds must be a number",
        ),
        (
            _monthly_solve(whole_chain_pair_scan_slack_weight=None),
            "monthly_solve.whole_chain_pair_scan_slack_weight must be a number",
        ),
        (
            _monthly_solve(total_time_limit_seconds=float("nan")),
            "total_time_limit_seconds must be a finite Decimal",
        ),
        (
            _monthly_solve(finalization_reserve_seconds=float("inf")),
            "finalization_reserve_seconds must be a finite Decimal",
        ),
        (
            _monthly_solve(candidate_check_limit=-1),
            "candidate_check_limit must be an integer with minimum 0",
        ),
        (
            _monthly_solve(total_time_limit_seconds=10, finalization_reserve_seconds=10),
            "finalization reserve must be smaller than total time",
        ),
        (
            _monthly_solve(whole_chain_pair_scan_slack_weight=-1),
            "whole_chain_pair_scan_slack_weight is outside its permitted range",
        ),
        (
            _monthly_solve(maximum_virtual_bridge_nodes=3),
            "only zero, one or two bridge nodes are supported",
        ),
    ],
)
def test_configuration_rejects_invalid_monthly_solve_policy(
    tmp_path, monthly_solve, message
):
    configuration_path = _write(
        tmp_path / "config.yaml",
        _values(monthly_solve=monthly_solve),
    )

    with pytest.raises(ServiceConfigurationError, match=message):
        load_service_configuration(configuration_path)


def test_configuration_does_not_expand_environment_or_home_markers(tmp_path, monkeypatch):
    monkeypatch.setenv("APSGO_CONFIG_TEST_HOME", str(tmp_path / "secret"))
    configuration_path = _write(
        tmp_path / "config" / "service.yaml",
        _values(database_path="$APSGO_CONFIG_TEST_HOME/~/rules.sqlite3"),
    )

    result = load_service_configuration(configuration_path)

    assert (
        result.database_path
        == (configuration_path.parent / "$APSGO_CONFIG_TEST_HOME/~/rules.sqlite3").resolve()
    )
    assert "secret" not in result.database_path.parts
