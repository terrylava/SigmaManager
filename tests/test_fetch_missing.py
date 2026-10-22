"""Downloading a mod an instance has never had, before it tries to start.

Adding a mod to a server and pressing Start is one action in someone's head. It used to
be two: the start refused with "run `dzsm mods update`", which says what to do but makes
the person go and do it for something they had just asked for in the browser.

The line that matters is between "nothing to launch" and "could launch something older".
Only the first justifies making a start wait on steamcmd; the second is Rule 4.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from dzsm import runner
from dzsm.config import Config, InstanceConfig, LocalMod, WorkshopMod
from dzsm.mods import ModCacheError, uncached_mods
from dzsm.state import State
from dzsm.steam import SteamError

WORKSHOP_ID = 1559212036
OLD = 1719878400
NEW = 1721005200


@pytest.fixture
def cfg(config: Config, tmp_path: Path) -> Config:
    local = tmp_path / "localmods" / "@Test"
    local.mkdir(parents=True)
    return config.model_copy(
        update={
            "mods": {
                "CF": WorkshopMod(workshop_id=WORKSHOP_ID),
                "Local": LocalMod(path=local),
            },
            "instances": {"chernarus": InstanceConfig(port=2302, mods=["CF", "Local"])},
        }
    )


def _cache(cfg: Config, version: int) -> Path:
    path = cfg.version_dir(WORKSHOP_ID, version)
    (path / "addons").mkdir(parents=True)
    (path / "addons" / "mod.pbo").write_bytes(b"x")
    return path


# --- which mods count as missing ---------------------------------------------


def test_a_mod_with_nothing_cached_is_missing(cfg: Config) -> None:
    assert uncached_mods(cfg, State(), "chernarus") == {"CF": WORKSHOP_ID}


def test_a_mod_with_any_version_cached_is_not(cfg: Config) -> None:
    """Even an old one. The instance can run it, and making a restart wait on a
    download is exactly what Rule 4 forbids."""
    _cache(cfg, OLD)
    state = State()
    state.record_version(WORKSHOP_ID, OLD)

    assert uncached_mods(cfg, state, "chernarus") == {}


def test_a_local_mod_is_never_fetched(cfg: Config) -> None:
    """It is hand-authored source dzsm did not create and cannot download."""
    assert "Local" not in uncached_mods(cfg, State(), "chernarus")


def test_a_pin_to_an_uncached_version_counts(cfg: Config) -> None:
    """The pin is what the instance would have to run, so an instance pinned to a
    version nobody downloaded has nothing to launch."""
    _cache(cfg, OLD)
    state = State()
    state.record_version(WORKSHOP_ID, OLD)
    pinned_mod = WorkshopMod(workshop_id=WORKSHOP_ID, pinned_version=NEW)
    pinned = cfg.model_copy(update={"mods": {**cfg.mods, "CF": pinned_mod}})

    assert uncached_mods(pinned, state, "chernarus") == {"CF": WORKSHOP_ID}


def test_a_state_entry_without_the_directory_counts(cfg: Config) -> None:
    """state.json says it is there and the disk disagrees — a deleted cache, or a
    half-finished copy. The disk wins."""
    state = State()
    state.record_version(WORKSHOP_ID, NEW)

    assert uncached_mods(cfg, state, "chernarus") == {"CF": WORKSHOP_ID}


def test_server_only_mods_are_included(config: Config, tmp_path: Path) -> None:
    """They are versioned and activated exactly like client-facing ones."""
    cfg = config.model_copy(
        update={
            "mods": {"Admin": WorkshopMod(workshop_id=777)},
            "instances": {
                "chernarus": InstanceConfig(port=2302, mods=[], server_mods=["Admin"])
            },
        }
    )

    assert uncached_mods(cfg, State(), "chernarus") == {"Admin": 777}


# --- what start does about it -------------------------------------------------


class _FakePopen:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.pid = 4242


@pytest.fixture
def ready(cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    server = tmp_path / "server"
    server.mkdir()
    exe = server / ("DayZServer_x64.exe" if sys.platform == "win32" else "DayZServer")
    exe.write_bytes(b"MZ")
    (server / "serverDZ.cfg").write_text("hostname = 'test';", encoding="utf-8")
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(runner, "_create_time", lambda pid: 1234.0)
    return cfg.model_copy(update={"server_app_path": server})


def test_a_missing_mod_is_fetched_then_the_server_starts(
    ready: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[int] = []

    def fetch(config: Config, state: State, workshop_id: int) -> int:
        asked.append(workshop_id)
        _cache(config, NEW)
        state.record_version(workshop_id, NEW)
        return NEW

    monkeypatch.setattr(runner, "fetch_mod", fetch)

    assert runner.start(ready, State(), "chernarus") == 4242
    assert asked == [WORKSHOP_ID]


def test_nothing_is_fetched_when_everything_is_cached(
    ready: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common case, and it must not cost a Steam round trip at every restart."""
    _cache(ready, NEW)
    state = State()
    state.record_version(WORKSHOP_ID, NEW)

    def refuse(*args: object, **kwargs: object) -> int:  # pragma: no cover
        raise AssertionError("should not have gone to Steam")

    monkeypatch.setattr(runner, "fetch_mod", refuse)

    assert runner.start(ready, state, "chernarus") == 4242


def test_the_fetch_can_be_turned_off(
    ready: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For a metered link, or someone who wants downloads to happen only when they say
    so. The start then fails the way it used to."""
    def refuse(*args: object, **kwargs: object) -> int:  # pragma: no cover
        raise AssertionError("should not have gone to Steam")

    monkeypatch.setattr(runner, "fetch_mod", refuse)
    off = ready.model_copy(update={"fetch_missing_mods": False})

    with pytest.raises(runner.RunnerError, match="cannot start"):
        runner.start(off, State(), "chernarus")


def test_a_failed_fetch_still_names_the_mod(
    ready: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Steam being unreachable must not turn into a traceback or a steamcmd transcript.
    The error that follows names the mod and the path it looked in."""
    def refuse(config: Config, state: State, workshop_id: int) -> int:
        raise SteamError("Steam did not report a version")

    monkeypatch.setattr(runner, "fetch_mod", refuse)

    with pytest.raises(runner.RunnerError, match="CF"):
        runner.start(ready, State(), "chernarus")


def test_one_unavailable_mod_does_not_hide_another(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both get tried, so the log names both rather than sending someone round the loop
    twice (Rule 3)."""
    cfg = config.model_copy(
        update={
            "mods": {
                "CF": WorkshopMod(workshop_id=1),
                "Expansion": WorkshopMod(workshop_id=2),
            },
            "instances": {"chernarus": InstanceConfig(port=2302, mods=["CF", "Expansion"])},
        }
    )
    tried: list[int] = []

    def refuse(config: Config, state: State, workshop_id: int) -> int:
        tried.append(workshop_id)
        raise ModCacheError("download could not be verified")

    monkeypatch.setattr(runner, "fetch_mod", refuse)

    assert runner._fetch_missing_mods(cfg, State(), "chernarus") == []
    assert tried == [1, 2]


def test_what_was_fetched_is_recorded(
    ready: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A promoted version that never reaches state.json would be downloaded again at the
    next start."""
    def fetch(config: Config, state: State, workshop_id: int) -> int:
        _cache(config, NEW)
        state.record_version(workshop_id, NEW)
        return NEW

    monkeypatch.setattr(runner, "fetch_mod", fetch)
    runner.start(ready, State(), "chernarus")

    from dzsm.state import load_state

    assert NEW in load_state(ready.state_file).known_versions(WORKSHOP_ID)
