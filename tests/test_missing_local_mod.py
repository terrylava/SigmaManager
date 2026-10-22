"""A local mod whose folder has gone.

A local mod is a directory someone put there by hand (spec §9). It can be moved, renamed
or deleted without dzsm hearing about it, and unlike a workshop mod there is nothing to
download and no older version to fall back to. The only choice is between a server that
is up without it and a server that is down, and the second helps nobody.

A workshop mod is the opposite case and must keep failing: a missing version directory
is a cache that should be there, and the start can fetch it.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

from dzsm import runner
from dzsm.config import Config, InstanceConfig, LocalMod, WorkshopMod
from dzsm.mods import ModCacheError, mod_paths, server_mod_paths
from dzsm.state import State

WORKSHOP_ID = 1559212036
VERSION = 1721005200


@pytest.fixture
def cfg(config: Config, tmp_path: Path) -> Config:
    """One workshop mod, one local mod that exists, one local mod that does not."""
    present = tmp_path / "localmods" / "@Present"
    present.mkdir(parents=True)
    cached = config.version_dir(WORKSHOP_ID, VERSION)
    (cached / "addons").mkdir(parents=True)
    return config.model_copy(
        update={
            "mods": {
                "CF": WorkshopMod(workshop_id=WORKSHOP_ID),
                "Present": LocalMod(path=present),
                "Gone": LocalMod(path=tmp_path / "localmods" / "@Gone"),
            },
            "instances": {
                "chernarus": InstanceConfig(port=2302, mods=["CF", "Present", "Gone"])
            },
        }
    )


def test_a_missing_local_mod_is_left_out(cfg: Config) -> None:
    paths = mod_paths(cfg, "chernarus", {"CF": VERSION})

    assert [p.name for p in paths] == [str(VERSION), "@Present"]


def test_the_load_order_of_the_rest_is_unchanged(cfg: Config) -> None:
    """The order is the semicolon order in -mod=, and framework mods have to precede
    their dependents. Dropping one must not reshuffle the others."""
    reordered = cfg.model_copy(
        update={
            "instances": {
                "chernarus": InstanceConfig(port=2302, mods=["Present", "Gone", "CF"])
            }
        }
    )

    paths = mod_paths(reordered, "chernarus", {"CF": VERSION})

    assert [p.name for p in paths] == ["@Present", str(VERSION)]


def test_it_is_logged_with_the_path(cfg: Config, caplog: pytest.LogCaptureFixture) -> None:
    """The next question is always "where did it go", so the answer is in the line."""
    with caplog.at_level(logging.ERROR):
        mod_paths(cfg, "chernarus", {"CF": VERSION})

    assert "Gone" in caplog.text
    assert "@Gone" in caplog.text
    assert "starting without it" in caplog.text


def test_a_missing_server_only_mod_is_also_left_out(config: Config, tmp_path: Path) -> None:
    """-serverMod= is the same decision: clients never load these, so a missing one is
    even less of a reason to keep the server down."""
    cfg = config.model_copy(
        update={
            "mods": {"Admin": LocalMod(path=tmp_path / "nope")},
            "instances": {
                "chernarus": InstanceConfig(port=2302, mods=[], server_mods=["Admin"])
            },
        }
    )

    assert server_mod_paths(cfg, "chernarus", {}) == []


def test_a_missing_workshop_version_still_refuses(cfg: Config) -> None:
    """The opposite case. A version directory that is not there is a cache that should
    have been, and the start can fetch it — silently running without it would hide that
    and hand every client a mod mismatch."""
    with pytest.raises(ModCacheError, match="path does not exist"):
        mod_paths(cfg, "chernarus", {"CF": 999})


# --- and the server actually starts -------------------------------------------


class _FakePopen:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.pid = 4242
        raw = cast("Sequence[object]", args[0] if args else kwargs["args"])
        self.argv: list[str] = [str(part) for part in raw]


def test_the_server_starts_without_it(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point. Before this, one hand-placed folder going missing took the
    server down and the error named a path rather than a way forward."""
    server = tmp_path / "server"
    server.mkdir()
    exe = server / ("DayZServer_x64.exe" if sys.platform == "win32" else "DayZServer")
    exe.write_bytes(b"MZ")
    (server / "serverDZ.cfg").write_text("hostname = 'test';", encoding="utf-8")
    spawned: dict[str, list[str]] = {}

    class _Capture(_FakePopen):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            spawned["argv"] = self.argv

    monkeypatch.setattr(subprocess, "Popen", _Capture)
    monkeypatch.setattr(runner, "_create_time", lambda pid: 1234.0)
    ready = cfg.model_copy(update={"server_app_path": server})
    state = State()
    state.record_version(WORKSHOP_ID, VERSION)

    assert runner.start(ready, state, "chernarus") == 4242

    mod_argument = next(a for a in spawned["argv"] if a.startswith("-mod="))

    assert "@Present" in mod_argument
    assert "@Gone" not in mod_argument
