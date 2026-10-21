"""Activation, supervision helpers, and the Rule 4 fallback.

No DayZ server is launched: subprocess.Popen and psutil are replaced. What matters here
is the decision-making — which versions get activated, what happens when that fails, and
whether a recycled pid can be mistaken for a live server.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

import psutil
import pytest

from dzsm import runner
from dzsm.config import Config, InstanceConfig, LocalMod, WorkshopMod
from dzsm.mods import ModCacheError, mod_paths, resolve_mod_versions
from dzsm.state import ProcessInfo, State

WORKSHOP_ID = 1559212036
OLD = 1719878400
NEW = 1721005200


@pytest.fixture
def cfg(config: Config, tmp_path: Path) -> Config:
    """A config with one instance running one workshop mod and one local mod."""
    local = tmp_path / "localmods" / "@Test"
    local.mkdir(parents=True)
    return config.model_copy(
        update={
            "mods": {
                "CF": WorkshopMod(workshop_id=WORKSHOP_ID),
                "Local": LocalMod(path=local),
            },
            "instances": {
                "chernarus": InstanceConfig(port=2302, mods=["CF", "Local"]),
            },
        }
    )


def _cache(cfg: Config, version: int) -> Path:
    path = cfg.version_dir(WORKSHOP_ID, version)
    (path / "addons").mkdir(parents=True)
    (path / "addons" / "mod.pbo").write_bytes(b"x")
    return path


# --- version resolution ------------------------------------------------------


def test_resolves_the_newest_cached_version(cfg: Config) -> None:
    """Activation picks up whatever landed while the server was running."""
    state = State()
    for version in (OLD, NEW):
        _cache(cfg, version)
        state.record_version(WORKSHOP_ID, version)

    assert resolve_mod_versions(cfg, state, "chernarus") == {"CF": NEW}


def test_local_mods_have_no_version(cfg: Config) -> None:
    state = State()
    _cache(cfg, NEW)
    state.record_version(WORKSHOP_ID, NEW)

    assert "Local" not in resolve_mod_versions(cfg, state, "chernarus")


def test_explicit_pin_beats_the_newest(cfg: Config) -> None:
    pin = WorkshopMod(workshop_id=WORKSHOP_ID, pinned_version=OLD)
    pinned = cfg.model_copy(update={"mods": {**cfg.mods, "CF": pin}})
    state = State()
    for version in (OLD, NEW):
        _cache(pinned, version)
        state.record_version(WORKSHOP_ID, version)

    assert resolve_mod_versions(pinned, state, "chernarus") == {"CF": OLD}


def test_uncached_mod_raises(cfg: Config) -> None:
    with pytest.raises(ModCacheError, match="no cached version"):
        resolve_mod_versions(cfg, State(), "chernarus")


def test_mod_paths_follow_declared_order_not_alphabetical(cfg: Config) -> None:
    """Load order is the config order — CF before Local, despite the alphabet."""
    state = State()
    _cache(cfg, NEW)
    state.record_version(WORKSHOP_ID, NEW)

    paths = mod_paths(cfg, "chernarus", {"CF": NEW})
    assert paths[0] == cfg.version_dir(WORKSHOP_ID, NEW)
    assert paths[1].name == "@Test"


def test_mod_paths_rejects_a_missing_directory(cfg: Config) -> None:
    """State can claim a version the disk no longer has."""
    with pytest.raises(ModCacheError, match="does not exist"):
        mod_paths(cfg, "chernarus", {"CF": NEW})


# --- Rule 4 ------------------------------------------------------------------


def test_falls_back_to_previous_pins_when_resolution_fails(cfg: Config) -> None:
    """A server up with stale mods beats a server that is down."""
    state = State()
    state.pins["chernarus"] = {"CF": OLD}
    # Nothing recorded in state.versions, so resolution cannot find a current version.

    assert runner.versions_to_activate(cfg, state, "chernarus") == {"CF": OLD}


def test_first_start_with_no_pins_still_raises(cfg: Config) -> None:
    """There is nothing to degrade to, so this is a genuine failure."""
    with pytest.raises(ModCacheError):
        runner.versions_to_activate(cfg, State(), "chernarus")


# --- liveness ----------------------------------------------------------------


class _FakeProcess:
    def __init__(self, create_time: float, running: bool = True) -> None:
        self._create_time = create_time
        self._running = running

    def is_running(self) -> bool:
        return self._running

    def status(self) -> str:
        return psutil.STATUS_RUNNING

    def create_time(self) -> float:
        return self._create_time


def _pinned_process(state: State, create_time: float | None) -> None:
    state.processes["chernarus"] = ProcessInfo(
        pid=4242, port=2302, started_at=datetime.now().astimezone(), create_time=create_time
    )


def test_no_recorded_process_is_not_running(cfg: Config) -> None:
    assert runner.is_running(cfg, State(), "chernarus") is False


def test_matching_create_time_is_running(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    state = State()
    _pinned_process(state, 1000.0)
    monkeypatch.setattr(psutil, "Process", lambda pid: _FakeProcess(1000.0))

    assert runner.is_running(cfg, state, "chernarus") is True


def test_recycled_pid_is_not_running(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """A live pid proves nothing on its own — the OS reuses them.

    Getting this wrong means dzsm refuses to restart a server that is actually down.
    """
    state = State()
    _pinned_process(state, 1000.0)
    monkeypatch.setattr(psutil, "Process", lambda pid: _FakeProcess(9999.0))

    assert runner.is_running(cfg, state, "chernarus") is False


def test_dead_pid_is_not_running(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    state = State()
    _pinned_process(state, 1000.0)

    def gone(pid: int) -> _FakeProcess:
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(psutil, "Process", gone)
    assert runner.is_running(cfg, state, "chernarus") is False


# --- restart schedule --------------------------------------------------------


def test_no_schedule_means_no_restart() -> None:
    assert runner.next_restart(InstanceConfig(mods=[])) is None


def test_next_restart_picks_the_next_time_today() -> None:
    instance = InstanceConfig(mods=[], restart_schedule=["04:00", "10:00", "22:00"])
    now = datetime(2026, 7, 25, 11, 30)

    assert runner.next_restart(instance, now=now) == datetime(2026, 7, 25, 22, 0)


def test_next_restart_rolls_over_midnight() -> None:
    instance = InstanceConfig(mods=[], restart_schedule=["04:00", "10:00"])
    now = datetime(2026, 7, 25, 23, 0)

    assert runner.next_restart(instance, now=now) == datetime(2026, 7, 26, 4, 0)


def test_invalid_schedule_entry_is_skipped_not_fatal() -> None:
    """Rule 3: log it, but one typo must not take the supervisor down."""
    instance = InstanceConfig(mods=[], restart_schedule=["not-a-time", "06:00"])
    now = datetime(2026, 7, 25, 5, 0)

    assert runner.next_restart(instance, now=now) == datetime(2026, 7, 25, 6, 0)


# --- start -------------------------------------------------------------------


class _FakePopen:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.pid = 4242
        self.argv = args[0] if args else kwargs.get("args")


@pytest.fixture
def ready(cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """A config whose server executable and mod cache both exist."""
    server = tmp_path / "server"
    server.mkdir()
    exe = server / ("DayZServer_x64.exe" if sys.platform == "win32" else "DayZServer")
    exe.write_bytes(b"MZ")
    (server / "serverDZ.cfg").write_text("hostname = 'test';", encoding="utf-8")
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    return cfg.model_copy(update={"server_app_path": server})


def test_start_records_pins_and_process(
    ready: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = State()
    _cache(ready, NEW)
    state.record_version(WORKSHOP_ID, NEW)
    monkeypatch.setattr(runner, "_create_time", lambda pid: 1234.0)

    pid = runner.start(ready, state, "chernarus")

    assert pid == 4242
    assert state.pins["chernarus"] == {"CF": NEW}
    assert state.processes["chernarus"].pid == 4242
    assert state.processes["chernarus"].port == 2302


def test_start_creates_the_instance_layout(
    ready: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = State()
    _cache(ready, NEW)
    state.record_version(WORKSHOP_ID, NEW)
    monkeypatch.setattr(runner, "_create_time", lambda pid: 1234.0)

    runner.start(ready, state, "chernarus")

    root = ready.instance_dir("chernarus")
    assert (root / "profiles").is_dir()
    assert (root / "serverDZ.cfg").is_file()
    # Keys go to the server installation, not here — the engine reads them from there.
    assert not (root / "keys").exists()


def test_start_rejects_an_unknown_instance(ready: Config) -> None:
    with pytest.raises(runner.RunnerError, match="unknown instance"):
        runner.start(ready, State(), "nope")


def test_start_refuses_an_over_long_command_line(
    ready: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checked before spawning, because Popen reports this as a bare OSError that says
    nothing about the mod list being what is too long (Rule 3)."""
    state = State()
    _cache(ready, NEW)
    state.record_version(WORKSHOP_ID, NEW)
    monkeypatch.setattr(runner, "command_line_length", lambda _argv: 40000)

    with pytest.raises(runner.RunnerError, match="over the 32767"):
        runner.start(ready, state, "chernarus")


def test_the_over_long_refusal_says_what_to_do_about_it(
    ready: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A length alone leaves someone guessing. The lever is the root path, since every
    mod path starts with it."""
    state = State()
    _cache(ready, NEW)
    state.record_version(WORKSHOP_ID, NEW)
    monkeypatch.setattr(runner, "command_line_length", lambda _argv: 40000)

    message = str(
        pytest.raises(runner.RunnerError, runner.start, ready, state, "chernarus").value
    )

    assert "2 mods" in message
    assert "Shorten `root`" in message


def test_a_command_line_inside_the_limit_starts(
    ready: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must not fire on anything real: two mods is 300-odd characters."""
    state = State()
    _cache(ready, NEW)
    state.record_version(WORKSHOP_ID, NEW)
    monkeypatch.setattr(runner, "_create_time", lambda pid: 1234.0)

    assert runner.start(ready, state, "chernarus") == 4242
