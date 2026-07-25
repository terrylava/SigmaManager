"""Login argument construction and the CLI login command.

No steamcmd is ever executed here: _login_args is pure, and the CLI tests replace the
steam functions. The interactive path is deliberately not exercised — it exists to
attach a real terminal, which a test harness cannot meaningfully provide.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dzsm import cli
from dzsm import steam as steam_module
from dzsm.config import Config, SteamConfig
from dzsm.steam import SteamError, SteamGuardRequired, _login_args

runner = CliRunner()


def _steam() -> SteamConfig:
    return SteamConfig(username="someone", steamcmd_path=Path(r"D:\dz\steamcmd\steamcmd.exe"))


# --- _login_args -------------------------------------------------------------


def test_login_args_include_the_password_when_the_env_var_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steam = _steam()
    monkeypatch.setenv(steam.password_env, "hunter2")
    assert _login_args(steam) == ["+login", "someone", "hunter2"]


def test_login_args_omit_the_password_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Falls back to steamcmd's cached credentials rather than hard-failing."""
    steam = _steam()
    monkeypatch.delenv(steam.password_env, raising=False)
    assert _login_args(steam) == ["+login", "someone"]


def test_login_args_treat_an_empty_password_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steam = _steam()
    monkeypatch.setenv(steam.password_env, "")
    assert _login_args(steam) == ["+login", "someone"]


# --- dzsm login --------------------------------------------------------------


@pytest.fixture
def config_file(tmp_path: Path, config: Config) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        "root = '{root}'\n"
        "allow_long_root = true\n\n"
        "[steam]\n"
        'username = "someone"\n'
        "steamcmd_path = '{steamcmd}'\n".format(
            root=str(config.root).replace("\\", "\\\\"),
            steamcmd=str(config.steam.steamcmd_path).replace("\\", "\\\\"),
        ),
        encoding="utf-8",
    )
    return path


def test_login_reports_success(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "steam_login", lambda steam, **kwargs: None)
    result = runner.invoke(cli.app, ["login", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "logged in as someone" in result.output


def test_login_points_at_interactive_mode_when_guard_is_required(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Guard message must name the fix, not just the failure (Rule 3)."""

    def guard(steam: object, **kwargs: object) -> None:
        raise SteamGuardRequired("steamcmd asked for a Steam Guard code")

    monkeypatch.setattr(cli, "steam_login", guard)
    result = runner.invoke(cli.app, ["login", "--config", str(config_file)])
    assert result.exit_code == 1
    assert "--interactive" in result.output


def test_login_surfaces_other_steam_errors(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(steam: object, **kwargs: object) -> None:
        raise SteamError("steamcmd not found at D:\\dz\\steamcmd\\steamcmd.exe")

    monkeypatch.setattr(cli, "steam_login", broken)
    result = runner.invoke(cli.app, ["login", "--config", str(config_file)])
    assert result.exit_code == 1
    assert "steamcmd not found" in result.output


def test_interactive_flag_uses_the_interactive_path(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []

    def fake_interactive(steam: SteamConfig, **kwargs: object) -> None:
        called.append(steam.username)

    monkeypatch.setattr(cli, "login_interactive", fake_interactive)
    monkeypatch.setattr(
        cli, "steam_login", lambda steam, **kwargs: pytest.fail("used the wrong path")
    )
    result = runner.invoke(cli.app, ["login", "--interactive", "--config", str(config_file)])
    assert result.exit_code == 0
    assert called == ["someone"]


# --- the exit-7 self-update case ---------------------------------------------


class _Completed:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _steamcmd_exiting(monkeypatch: pytest.MonkeyPatch, code: int, tmp_path: Path) -> SteamConfig:
    steam = SteamConfig(username="someone", steamcmd_path=tmp_path / "steamcmd.exe")
    steam.steamcmd_path.write_bytes(b"MZ")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed(code))
    return steam


def test_interactive_login_verifies_rather_than_trusting_exit_seven(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exit 7 means steamcmd replaced itself. Nothing was captured from that run, so the
    outcome is proven with a non-interactive login instead of assumed either way."""
    steam = _steamcmd_exiting(monkeypatch, 7, tmp_path)
    verified: list[str] = []
    monkeypatch.setattr(steam_module, "login", lambda s, **k: verified.append(s.username))

    steam_module.login_interactive(steam)

    assert verified == ["someone"]


def test_interactive_login_reports_failure_when_exit_seven_did_not_take(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the follow-up login fails, exit 7 really was a failure — surface it."""
    steam = _steamcmd_exiting(monkeypatch, 7, tmp_path)

    def broken(s: SteamConfig, **kwargs: object) -> None:
        raise SteamError("Login Failure: Invalid Password")

    monkeypatch.setattr(steam_module, "login", broken)

    with pytest.raises(SteamError, match="Invalid Password"):
        steam_module.login_interactive(steam)


def test_interactive_login_still_rejects_other_nonzero_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    steam = _steamcmd_exiting(monkeypatch, 5, tmp_path)
    with pytest.raises(SteamError, match="exited 5"):
        steam_module.login_interactive(steam)


def test_login_fails_clearly_on_a_missing_config(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["login", "--config", str(tmp_path / "nope.toml")])
    assert result.exit_code == 1
    assert "no config at" in result.output
