"""BattlEye server config management.

BattlEye ships no default config, so without one there is no RConPassword, RCON never
listens, and every restart warning fails against a port nothing is bound to. dzsm
generates a password per instance and reads it back from the same file the server read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dzsm import runner
from dzsm.config import Config, InstanceConfig, RconConfig
from dzsm.instance import (
    battleye_config_candidates,
    battleye_config_path,
    ensure_battleye_config,
    read_battleye_password,
    read_battleye_port,
    read_battleye_setting,
)
from dzsm.rcon import RconError


@pytest.fixture
def cfg(config: Config) -> Config:
    return config.model_copy(
        update={"instances": {"chernarus": InstanceConfig(port=2302, mods=[])}}
    )


def _password(cfg: Config) -> str | None:
    return read_battleye_setting(battleye_config_path(cfg, "chernarus"), "RConPassword")


# --- parsing -----------------------------------------------------------------


def test_reads_a_bare_key_value_line(tmp_path: Path) -> None:
    path = tmp_path / "BEServer_x64.cfg"
    path.write_text("RConPassword hunter2\nRConPort 2305\n", encoding="utf-8")

    assert read_battleye_setting(path, "RConPassword") == "hunter2"
    assert read_battleye_setting(path, "RConPort") == "2305"


def test_matching_is_case_insensitive(tmp_path: Path) -> None:
    path = tmp_path / "BEServer_x64.cfg"
    path.write_text("rconpassword hunter2\n", encoding="utf-8")

    assert read_battleye_setting(path, "RConPassword") == "hunter2"


def test_comments_and_blanks_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "BEServer_x64.cfg"
    path.write_text("// RConPassword commented\n\nRConPassword real\n", encoding="utf-8")

    assert read_battleye_setting(path, "RConPassword") == "real"


def test_a_missing_file_reads_as_none(tmp_path: Path) -> None:
    assert read_battleye_setting(tmp_path / "nope.cfg", "RConPassword") is None


def test_a_key_with_no_value_reads_as_none(tmp_path: Path) -> None:
    path = tmp_path / "BEServer_x64.cfg"
    path.write_text("RConPassword \n", encoding="utf-8")

    assert read_battleye_setting(path, "RConPassword") is None


# --- generation --------------------------------------------------------------


def test_a_config_is_written_with_a_password_and_port(cfg: Config) -> None:
    path = ensure_battleye_config(cfg, "chernarus", 2305)

    assert path is not None
    assert read_battleye_setting(path, "RConPort") == "2305"
    password = read_battleye_setting(path, "RConPassword")
    assert password is not None
    assert len(password) >= 24, "generated passwords should not be guessable"


def test_generated_passwords_differ_per_instance(config: Config) -> None:
    """A shared default would be worse than no password at all."""
    two = config.model_copy(
        update={
            "instances": {
                "a": InstanceConfig(port=2302, mods=[]),
                "b": InstanceConfig(port=2402, mods=[]),
            }
        }
    )
    ensure_battleye_config(two, "a", 2305)
    ensure_battleye_config(two, "b", 2405)

    first = read_battleye_setting(battleye_config_path(two, "a"), "RConPassword")
    second = read_battleye_setting(battleye_config_path(two, "b"), "RConPassword")

    assert first != second


def test_the_password_contains_no_spaces(cfg: Config) -> None:
    """The format is space-separated, so a space would truncate the value."""
    ensure_battleye_config(cfg, "chernarus", 2305)
    password = _password(cfg)

    assert password is not None
    assert " " not in password


def test_an_existing_password_is_never_replaced(cfg: Config) -> None:
    """It may already be shared with admin tooling; rotating it on every start would
    break that silently."""
    ensure_battleye_config(cfg, "chernarus", 2305)
    original = _password(cfg)

    ensure_battleye_config(cfg, "chernarus", 2305)

    assert _password(cfg) == original


def test_other_settings_survive_when_a_password_is_added(cfg: Config) -> None:
    path = battleye_config_path(cfg, "chernarus")
    path.parent.mkdir(parents=True)
    path.write_text("MaxPing 350\nRConPort 2999\n", encoding="utf-8")

    ensure_battleye_config(cfg, "chernarus", 2305)

    assert read_battleye_setting(path, "MaxPing") == "350"
    assert read_battleye_setting(path, "RConPort") == "2999", "an existing port stands"
    assert _password(cfg) is not None


def test_management_can_be_disabled(cfg: Config) -> None:
    disabled = cfg.model_copy(update={"rcon": RconConfig(manage_battleye_config=False)})

    assert ensure_battleye_config(disabled, "chernarus", 2305) is None
    assert not battleye_config_path(disabled, "chernarus").exists()


# --- BattlEye consuming the file ---------------------------------------------


def _consume(cfg: Config, pid: int = 5516) -> Path:
    """Do what BattlEye does on startup: read the config and rename it."""
    path = battleye_config_path(cfg, "chernarus")
    active = path.with_name(f"BEServer_x64_active_{pid}.cfg")
    path.rename(active)
    return active


def test_the_password_is_found_after_battleye_renames_the_file(cfg: Config) -> None:
    """BattlEye renames BEServer_x64.cfg to BEServer_x64_active_<pid>.cfg on startup, so
    by the time the server is up the original is gone."""
    ensure_battleye_config(cfg, "chernarus", 2305)
    original = _password(cfg)
    _consume(cfg)

    assert not battleye_config_path(cfg, "chernarus").exists()
    assert read_battleye_password(cfg, "chernarus") == original


def test_the_port_is_found_after_the_rename(cfg: Config) -> None:
    ensure_battleye_config(cfg, "chernarus", 2305)
    _consume(cfg)

    assert read_battleye_port(cfg, "chernarus") == 2305


def test_the_password_is_not_rotated_on_the_next_start(cfg: Config) -> None:
    """The bug this guards: with the original consumed, a naive check finds no config and
    mints a new password every single start, silently breaking anything sharing it."""
    ensure_battleye_config(cfg, "chernarus", 2305)
    original = _password(cfg)
    _consume(cfg)

    ensure_battleye_config(cfg, "chernarus", 2305)

    assert _password(cfg) == original


def test_the_restored_config_keeps_the_consumed_port(cfg: Config) -> None:
    path = battleye_config_path(cfg, "chernarus")
    path.parent.mkdir(parents=True)
    path.write_text("RConPassword kept\nRConPort 2999\n", encoding="utf-8")
    _consume(cfg)

    ensure_battleye_config(cfg, "chernarus", 2305)

    assert read_battleye_setting(path, "RConPort") == "2999"


def test_the_newest_consumed_config_wins(cfg: Config) -> None:
    """Several restarts leave several active files behind."""
    ensure_battleye_config(cfg, "chernarus", 2305)
    first = _password(cfg)
    _consume(cfg, pid=1)

    ensure_battleye_config(cfg, "chernarus", 2305)
    _consume(cfg, pid=2)

    assert read_battleye_password(cfg, "chernarus") == first


def test_deleting_the_password_generates_a_fresh_one(cfg: Config) -> None:
    """The documented way to rotate it."""
    ensure_battleye_config(cfg, "chernarus", 2305)
    original = _password(cfg)
    for candidate in battleye_config_candidates(cfg, "chernarus"):
        candidate.unlink()

    ensure_battleye_config(cfg, "chernarus", 2305)

    assert _password(cfg) != original


# --- resolution --------------------------------------------------------------


def test_the_client_uses_the_generated_password(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DZSM_RCON_PASSWORD", raising=False)
    ensure_battleye_config(cfg, "chernarus", 2305)
    captured: dict[str, object] = {}

    class _Client:
        def __init__(self, host: str, port: int, password: str, **kwargs: object) -> None:
            captured["port"] = port
            captured["password"] = password

        def connect(self) -> None:
            pass

    monkeypatch.setattr(runner, "RconClient", _Client)
    runner.rcon_client(cfg, "chernarus")

    assert captured["password"] == _password(cfg)
    assert captured["port"] == 2305


def test_the_environment_overrides_the_file(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_battleye_config(cfg, "chernarus", 2305)
    monkeypatch.setenv("DZSM_RCON_PASSWORD", "from-the-environment")
    captured: dict[str, object] = {}

    class _Client:
        def __init__(self, host: str, port: int, password: str, **kwargs: object) -> None:
            captured["password"] = password

        def connect(self) -> None:
            pass

    monkeypatch.setattr(runner, "RconClient", _Client)
    runner.rcon_client(cfg, "chernarus")

    assert captured["password"] == "from-the-environment"  # noqa: S105 — a test fixture


def test_the_port_comes_from_battleyes_config_when_it_disagrees(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """That file is what the server actually read, so it beats the derived default."""
    monkeypatch.delenv("DZSM_RCON_PASSWORD", raising=False)
    path = battleye_config_path(cfg, "chernarus")
    path.parent.mkdir(parents=True)
    path.write_text("RConPassword secret\nRConPort 2999\n", encoding="utf-8")
    captured: dict[str, object] = {}

    class _Client:
        def __init__(self, host: str, port: int, password: str, **kwargs: object) -> None:
            captured["port"] = port

        def connect(self) -> None:
            pass

    monkeypatch.setattr(runner, "RconClient", _Client)
    runner.rcon_client(cfg, "chernarus")

    assert captured["port"] == 2999


def test_an_explicit_config_port_still_wins(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = cfg.model_copy(
        update={
            "instances": {"chernarus": InstanceConfig(port=2302, mods=[], rcon_port=2400)}
        }
    )
    path = battleye_config_path(explicit, "chernarus")
    path.parent.mkdir(parents=True)
    path.write_text("RConPassword secret\nRConPort 2999\n", encoding="utf-8")
    captured: dict[str, object] = {}

    class _Client:
        def __init__(self, host: str, port: int, password: str, **kwargs: object) -> None:
            captured["port"] = port

        def connect(self) -> None:
            pass

    monkeypatch.setattr(runner, "RconClient", _Client)
    runner.rcon_client(explicit, "chernarus")

    assert captured["port"] == 2400


def test_no_password_anywhere_says_how_to_fix_it(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DZSM_RCON_PASSWORD", raising=False)

    with pytest.raises(RconError, match="rcon_enabled"):
        runner.rcon_client(cfg, "chernarus")
