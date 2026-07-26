"""Launching from a copy of serverDZ.cfg.

The engine holds its -config file open for the whole run, so whatever is passed there
cannot be edited while the server is up — not in place, not by atomic replace. Launching
from a copy keeps the file people edit free, and a change lands at the next start, which
is the only time the server reads it anyway. This is how om.py did it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dzsm.config import Config, InstanceConfig
from dzsm.instance import (
    ACTIVE_CONFIG_SUFFIX,
    InstanceError,
    active_config_path,
    build_launch_args,
    prepare_server_config,
)

ORIGINAL = 'hostname = "mine";\ninstanceId = 1;\n'


@pytest.fixture
def cfg(config: Config) -> Config:
    return config.model_copy(
        update={"instances": {"chernarus": InstanceConfig(port=2302, mods=[])}}
    )


@pytest.fixture
def editable(cfg: Config) -> Path:
    path = cfg.instance_dir("chernarus") / "serverDZ.cfg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ORIGINAL, encoding="utf-8")
    return path


def test_the_copy_is_a_different_file(cfg: Config, editable: Path) -> None:
    active = prepare_server_config(cfg, "chernarus")

    assert active != editable
    assert active.name.endswith(ACTIVE_CONFIG_SUFFIX)


def test_the_copy_carries_the_settings(cfg: Config, editable: Path) -> None:
    active = prepare_server_config(cfg, "chernarus")
    text = active.read_text(encoding="utf-8")

    assert 'hostname = "mine";' in text
    assert "instanceId = 1;" in text


def test_the_copy_says_not_to_edit_it(cfg: Config, editable: Path) -> None:
    """Anyone who opens it should be told where the real file is."""
    active = prepare_server_config(cfg, "chernarus")

    assert "serverDZ.cfg" in active.read_text(encoding="utf-8").splitlines()[0]


def test_the_launch_argument_points_at_the_copy(cfg: Config, editable: Path) -> None:
    """The whole point: the engine must lock the copy, not the editable file."""
    active = prepare_server_config(cfg, "chernarus")
    args = build_launch_args(
        cfg,
        "chernarus",
        cfg.instances["chernarus"],
        Path(r"D:\dz\serverapp\1\DayZServer_x64.exe"),
        [],
        None,
        active,
    )

    value = next(a for a in args if a.startswith("-config=")).removeprefix("-config=")
    assert Path(value) == active
    assert Path(value) != editable


def test_it_is_regenerated_every_start(cfg: Config, editable: Path) -> None:
    """So the copy can never drift from the file people edit."""
    prepare_server_config(cfg, "chernarus")
    editable.write_text('hostname = "edited while running";\n', encoding="utf-8")

    active = prepare_server_config(cfg, "chernarus")

    assert "edited while running" in active.read_text(encoding="utf-8")


def test_the_editable_file_is_untouched(cfg: Config, editable: Path) -> None:
    prepare_server_config(cfg, "chernarus")

    assert editable.read_text(encoding="utf-8") == ORIGINAL


def test_a_byte_order_mark_is_stripped(cfg: Config) -> None:
    """A serverDZ.cfg saved by a Windows editor often starts with a BOM. Read as plain
    utf-8 it survives as U+FEFF, and prepending the header buries it mid-file where it
    corrupts the first real setting — the engine then exits on startup with nothing
    useful in the RPT. Observed, not hypothetical."""
    source = cfg.instance_dir("chernarus") / "serverDZ.cfg"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"\xef\xbb\xbf" + ORIGINAL.encode("utf-8"))

    active = prepare_server_config(cfg, "chernarus")
    text = active.read_text(encoding="utf-8")

    assert "﻿" not in text
    assert not active.read_bytes().startswith(b"\xef\xbb\xbf")
    assert 'hostname = "mine";' in text


def test_the_first_setting_is_not_mangled_by_the_header(cfg: Config, editable: Path) -> None:
    """Whatever the header does, the line after it has to be a clean setting."""
    active = prepare_server_config(cfg, "chernarus")
    text = active.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if not ln.startswith("//")]

    assert lines[0] == 'hostname = "mine";'


def test_a_missing_source_is_an_error(cfg: Config) -> None:
    with pytest.raises(InstanceError, match="serverDZ.cfg"):
        prepare_server_config(cfg, "chernarus")


def test_the_path_follows_a_custom_config_name(config: Config) -> None:
    custom = config.model_copy(
        update={
            "instances": {
                "chernarus": InstanceConfig(port=2302, mods=[], config_file="mine.cfg")
            }
        }
    )

    assert active_config_path(custom, "chernarus").name == f"mine{ACTIVE_CONFIG_SUFFIX}"


def test_the_default_launch_argument_is_still_the_instance_file(cfg: Config) -> None:
    """Callers that do not pass a copy keep the previous behaviour."""
    args = build_launch_args(
        cfg,
        "chernarus",
        cfg.instances["chernarus"],
        Path(r"D:\dz\serverapp\1\DayZServer_x64.exe"),
        [],
    )
    value = next(a for a in args if a.startswith("-config=")).removeprefix("-config=")

    assert Path(value).name == "serverDZ.cfg"
