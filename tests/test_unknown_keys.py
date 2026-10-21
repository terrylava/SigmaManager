"""A config key dzsm does not recognise is refused, by name.

Silently ignoring one is the worst of the three options. `restart_hours` instead of
`restart_after_hours` reads as "that setting does nothing", and nothing anywhere
connects the two — which is the same shape as every other bug this project has spent a
day digging out.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from dzsm.config import ConfigError, load_config

BASE = """\
root = '{root}'
allow_long_root = true

[steam]
username = "someone"
steamcmd_path = '{root}/steamcmd/steamcmd.exe'

[mods.CF]
kind = "workshop"
workshop_id = 1559212036

[instances.chernarus]
port = 2302
mods = ["CF"]
"""


@pytest.fixture
def write(tmp_path: Path) -> Callable[..., Path]:
    def _write(extra: str = "") -> Path:
        path = tmp_path / "config.toml"
        path.write_text(BASE.format(root=tmp_path.as_posix()) + extra, encoding="utf-8")
        return path

    return _write


def test_a_valid_config_still_loads(write: Callable[..., Path]) -> None:
    assert load_config(write()).instances["chernarus"].port == 2302


def test_an_unknown_instance_setting_is_refused(write: Callable[..., Path]) -> None:
    with pytest.raises(ConfigError, match="unknown setting `restart_hours`"):
        load_config(write("restart_hours = 4\n"))


def test_the_refusal_names_the_table(write: Callable[..., Path]) -> None:
    message = str(pytest.raises(ConfigError, load_config, write("nope = 1\n")).value)

    assert "[instances.chernarus]" in message


def test_the_refusal_suggests_the_real_setting(write: Callable[..., Path]) -> None:
    """The location alone is the easy half — whoever typed it knows where."""
    message = str(pytest.raises(ConfigError, load_config, write("restart_hours = 4\n")).value)

    assert "Did you mean `restart_after_hours`?" in message


def test_the_refusal_lists_what_is_valid(write: Callable[..., Path]) -> None:
    message = str(pytest.raises(ConfigError, load_config, write("nope = 1\n")).value)

    assert "valid here:" in message
    assert "restart_after_hours" in message


def test_a_setting_in_the_wrong_table_is_refused(write: Callable[..., Path]) -> None:
    """server_app_path is real, but global. Put under an instance it did nothing, which
    is how this was found."""
    with pytest.raises(ConfigError, match="unknown setting `server_app_path`"):
        load_config(write('server_app_path = "G:/DayZServer"\n'))


def test_an_unknown_top_level_key_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        BASE.format(root=tmp_path.as_posix()).replace(
            "allow_long_root = true", "allow_long_root = true\nsnapshot_stratagy = 'copy'"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Did you mean `snapshot_strategy`"):
        load_config(path)


def test_an_unknown_rcon_setting_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        BASE.format(root=tmp_path.as_posix()) + "\n[rcon]\nstartup_grace = 300\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Did you mean `startup_grace_seconds`"):
        load_config(path)


def test_a_mod_table_reports_the_table_people_can_see(tmp_path: Path) -> None:
    """A discriminated union puts its tag in the error path, so this would otherwise
    name [mods.Other.workshop] — a table that does not appear in the file."""
    path = tmp_path / "config.toml"
    path.write_text(
        BASE.format(root=tmp_path.as_posix())
        + '\n[mods.Other]\nkind = "workshop"\nworkshop_id = 2\npinnedversion = 3\n',
        encoding="utf-8",
    )

    message = str(pytest.raises(ConfigError, load_config, path).value)

    assert "[mods.Other]" in message
    assert "workshop]" not in message


def test_a_real_validation_error_keeps_pydantic_s_report(tmp_path: Path) -> None:
    """The rewrite only covers unknown keys. A missing or malformed value needs the
    fuller report, including the value that was rejected."""
    path = tmp_path / "config.toml"
    path.write_text(
        BASE.format(root=tmp_path.as_posix()).replace("port = 2302", 'port = "not a port"'),
        encoding="utf-8",
    )

    message = str(pytest.raises(ConfigError, load_config, path).value)

    assert "unknown setting" not in message
    assert "port" in message
