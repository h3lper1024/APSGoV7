import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from apsgo_v7_service.configuration import (
    ServiceConfigurationError,
    load_service_configuration,
)

ROOT = Path(__file__).resolve().parents[2]


def _values(**changes):
    values = {
        "database_path": "../data/rules.sqlite3",
        "database_timeout_seconds": 5.0,
        "listen_host": "127.0.0.1",
        "listen_port": 8001,
    }
    values.update(changes)
    return values


def _write(configuration_path, values=None):
    configuration_path.parent.mkdir(parents=True, exist_ok=True)
    configuration_path.write_text(
        json.dumps(_values() if values is None else values),
        encoding="utf-8",
    )
    return configuration_path


def test_tracked_default_configuration_points_to_the_v7_database():
    result = load_service_configuration(ROOT / "config" / "apsgo_v7_service.json")

    assert result.database_path == ROOT / "data" / "apsgo_v7_rules.sqlite3"
    assert result.database_timeout_seconds == 5.0
    assert result.listen_host == "127.0.0.1"
    assert result.listen_port == 8001


def test_configuration_resolves_database_relative_to_its_own_file(tmp_path, monkeypatch):
    configuration_path = _write(tmp_path / "deployment" / "config" / "service.json")
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


def test_configuration_preserves_an_absolute_database_target(tmp_path):
    database_path = tmp_path / "database" / "rules.sqlite3"
    configuration_path = _write(
        tmp_path / "config.json",
        _values(database_path=str(database_path)),
    )

    result = load_service_configuration(configuration_path)

    assert result.database_path == database_path.resolve()


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("[]", "root must be a JSON object"),
        ("{", "invalid JSON"),
        (
            '{"database_path":"one","database_path":"two",'
            '"database_timeout_seconds":5,"listen_host":"127.0.0.1","listen_port":8001}',
            "duplicate JSON key: database_path",
        ),
        (
            '{"database_path":"rules.sqlite3","database_timeout_seconds":NaN,'
            '"listen_host":"127.0.0.1","listen_port":8001}',
            "unsupported JSON constant: NaN",
        ),
    ],
)
def test_configuration_rejects_invalid_json_shapes(tmp_path, source, message):
    configuration_path = tmp_path / "config.json"
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
        ({"listen_host": "localhost"}, "listen_host must be exactly 127.0.0.1"),
        ({"database_timeout_seconds": "5"}, "must be a number"),
        ({"database_timeout_seconds": True}, "must be a number"),
        ({"database_timeout_seconds": 0}, "must be finite and greater than zero"),
        ({"database_path": " "}, "database_path must be nonempty text"),
        ({"database_path": ":memory:"}, "database_path must identify a persistent file"),
    ],
)
def test_configuration_rejects_invalid_values(tmp_path, changes, message):
    configuration_path = _write(tmp_path / "config.json", _values(**changes))

    with pytest.raises(ServiceConfigurationError, match=message):
        load_service_configuration(configuration_path)


def test_configuration_rejects_missing_unknown_and_non_utf8_files(tmp_path):
    with pytest.raises(ServiceConfigurationError, match="does not exist"):
        load_service_configuration(tmp_path / "missing.json")

    missing_key = _values()
    missing_key.pop("listen_port")
    with pytest.raises(ServiceConfigurationError, match="missing keys: listen_port"):
        load_service_configuration(_write(tmp_path / "missing-key.json", missing_key))

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(ServiceConfigurationError, match="must identify a file"):
        load_service_configuration(directory)

    non_utf8 = tmp_path / "non-utf8.json"
    non_utf8.write_bytes(b"\xff")
    with pytest.raises(ServiceConfigurationError, match="must be UTF-8"):
        load_service_configuration(non_utf8)


def test_configuration_does_not_expand_environment_or_home_markers(tmp_path, monkeypatch):
    monkeypatch.setenv("APSGO_CONFIG_TEST_HOME", str(tmp_path / "secret"))
    configuration_path = _write(
        tmp_path / "config" / "service.json",
        _values(database_path="$APSGO_CONFIG_TEST_HOME/~/rules.sqlite3"),
    )

    result = load_service_configuration(configuration_path)

    assert (
        result.database_path
        == (configuration_path.parent / "$APSGO_CONFIG_TEST_HOME/~/rules.sqlite3").resolve()
    )
    assert "secret" not in result.database_path.parts
