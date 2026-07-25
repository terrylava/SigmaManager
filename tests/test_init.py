"""First run.

Every other command needs a config, so a user who has just downloaded dzsm has exactly
one working move. If `init` does not produce something that validates and runs, there is
no path from "downloaded it" to "running a server".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from dzsm.cli import app
from dzsm.config import load_config

runner = CliRunner()


# Short on purpose: pytest's tmp_path is ~90 characters, which the MAX_PATH guard
# rightly rejects. Nothing here touches the filesystem under it.
ROOT = "D:\\dztest"


def _init(tmp_path: Path, root: str = ROOT) -> tuple[Path, str]:
    config = tmp_path / "config.toml"
    result = runner.invoke(
        app,
        ["init", "--config", str(config), "--root", root, "--username", "someone"],
    )
    return config, result.output


def test_it_writes_a_config(tmp_path: Path) -> None:
    config, output = _init(tmp_path)

    assert config.is_file()
    assert "wrote" in output


def test_the_config_it_writes_actually_validates(tmp_path: Path) -> None:
    """The whole point: a new user's first file must not need fixing before it loads."""
    config, _ = _init(tmp_path)

    loaded = load_config(config)

    assert loaded.steam.username == "someone"
    assert loaded.root == Path(ROOT)


def test_it_includes_a_startable_instance(tmp_path: Path) -> None:
    """So there is something to run once the server app is downloaded, rather than a
    config that validates and does nothing."""
    config, _ = _init(tmp_path)

    loaded = load_config(config)

    assert "server1" in loaded.instances
    assert loaded.instances["server1"].port == 2302


def test_it_tells_them_what_to_do_next(tmp_path: Path) -> None:
    _, output = _init(tmp_path)

    assert "dzsm login --interactive" in output
    assert "dzsm server update" in output
    assert "dzsm instance start server1" in output


def test_it_refuses_to_clobber_an_existing_config(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("root = 'D:/keep-me'\n", encoding="utf-8")

    result = runner.invoke(
        app, ["init", "--config", str(config), "--root", "D:/dz", "--username", "x"]
    )

    assert result.exit_code == 1
    assert "already exists" in result.output
    assert "keep-me" in config.read_text(encoding="utf-8")


def test_force_overwrites(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("root = 'D:/old'\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["init", "--config", str(config), "--root", ROOT, "--username", "someone", "--force"],
    )

    assert result.exit_code == 0
    assert load_config(config).steam.username == "someone"


def test_a_bad_root_is_caught_at_init_not_three_commands_later(tmp_path: Path) -> None:
    """A root that fails the MAX_PATH check should fail here, while they are still
    looking at the config."""
    long_root = "C:\\Users\\somebody\\Documents\\DayZ Server Manager\\servers\\root"
    config = tmp_path / "config.toml"

    result = runner.invoke(
        app, ["init", "--config", str(config), "--root", long_root, "--username", "x"]
    )

    assert result.exit_code == 1
    assert "MAX_PATH" in result.output


def test_windows_paths_survive_the_round_trip(tmp_path: Path) -> None:
    """Backslashes in a generated TOML string are the obvious way to write a broken
    file. Single quotes are literal in TOML, so they must go in unescaped."""
    config, _ = _init(tmp_path, root="D:\\dz")
    text = config.read_text(encoding="utf-8")

    assert "'D:\\dz'" in text, "escaping would put two backslashes in the value"
    assert "\\\\" not in text
    loaded = load_config(config)
    assert loaded.root == Path("D:\\dz")
    assert loaded.steam.steamcmd_path == Path("D:\\dz\\steamcmd\\steamcmd.exe")


def test_a_posix_root_gets_posix_separators(tmp_path: Path) -> None:
    """Checked as text: a POSIX path is not absolute on Windows, so it cannot be loaded
    here — but the generated file still has to be right for the machine it targets."""
    config, _ = _init(tmp_path, root="/srv/dz")
    text = config.read_text(encoding="utf-8")

    assert "'/srv/dz/steamcmd/steamcmd.exe'" in text
    assert "\\" not in text


def test_the_generated_config_is_plain_ascii(tmp_path: Path) -> None:
    """People open this in whatever editor Windows hands them; a stray em dash comes back
    as mojibake."""
    config, _ = _init(tmp_path)

    config.read_text(encoding="utf-8").encode("ascii")


def test_a_trailing_separator_does_not_double_up(tmp_path: Path) -> None:
    config, _ = _init(tmp_path, root="D:\\dz\\")

    assert load_config(config).steam.steamcmd_path == Path("D:\\dz\\steamcmd\\steamcmd.exe")


# --- the dead end it replaces ------------------------------------------------


def test_a_missing_config_points_at_init(tmp_path: Path) -> None:
    """Previously this said only "no such file", leaving a new user nowhere to go."""
    result = runner.invoke(app, ["check", "--config", str(tmp_path / "nope.toml")])

    assert result.exit_code == 1
    assert "dzsm init" in result.output


@pytest.mark.parametrize(
    "command",
    [["check"], ["login"], ["mods", "check"], ["instance", "status"], ["serve"]],
)
def test_every_entry_point_points_at_init(tmp_path: Path, command: list[str]) -> None:
    result = runner.invoke(app, [*command, "--config", str(tmp_path / "nope.toml")])

    assert "dzsm init" in result.output
