"""Stopping an instance keeps it stopped.

A supervisor that immediately undoes an explicit stop is worse than no supervisor: you
cannot take a server down for maintenance, and the "fix" people reach for is killing the
supervisor, which loses the crash restarts too.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

import pytest

from dzsm import runner
from dzsm.config import Config, InstanceConfig
from dzsm.state import ProcessInfo, State, save_state


@pytest.fixture
def cfg(config: Config) -> Config:
    return config.model_copy(
        update={
            "instances": {"chernarus": InstanceConfig(port=2302, mods=[])},
            "supervise_poll_seconds": 30,
        }
    )


def _stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing is running, and stopping is a no-op."""
    monkeypatch.setattr(runner, "_live_process", lambda config, state, name: None)


# --- the hold itself ---------------------------------------------------------


def test_stopping_holds_the_instance_down(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    _stopped(monkeypatch)
    state = State()

    runner.stop(cfg, state, "chernarus")

    assert "chernarus" in state.held


def test_no_hold_when_asked(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-hold is how you bounce an instance under a supervisor."""
    _stopped(monkeypatch)
    state = State()

    runner.stop(cfg, state, "chernarus", hold=False)

    assert state.held == {}


def test_starting_releases_the_hold(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    state = State()
    state.held["chernarus"] = datetime.now().astimezone()
    monkeypatch.setattr(runner, "is_running", lambda config, state, name: False)
    monkeypatch.setattr(
        runner,
        "prepare_server_directory",
        lambda *args, **kwargs: (_ for _ in ()).throw(runner.RunnerError("stop here")),
    )

    with pytest.raises(runner.RunnerError):
        runner.start(cfg, state, "chernarus")

    assert "chernarus" not in state.held, "the release happens before anything can fail"


def test_a_scheduled_restart_does_not_leave_it_held(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor's own bounce is not a request for the instance to stay down."""
    _stopped(monkeypatch)
    state = State()
    monkeypatch.setattr(runner, "start", lambda config, state, name, **kwargs: 1)

    runner._restart_and_save(cfg, state, "chernarus")

    assert state.held == {}


# --- the supervisor respects it ----------------------------------------------


def _supervise_briefly(cfg: Config, state: State) -> None:
    stop = threading.Event()
    thread = threading.Thread(
        target=runner.supervise, args=(cfg, state, ["chernarus"]), kwargs={"stop": stop}
    )
    thread.start()
    time.sleep(0.3)
    stop.set()
    thread.join(timeout=5)


def test_a_held_instance_is_not_started_on_startup(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = State()
    state.held["chernarus"] = datetime.now().astimezone()
    save_state(cfg.state_file, state)
    monkeypatch.setattr(runner, "is_running", lambda config, state, name: False)
    monkeypatch.setattr(
        runner, "start", lambda *args, **kwargs: pytest.fail("must not start a held instance")
    )

    _supervise_briefly(cfg, state)


def test_a_held_instance_is_not_crash_restarted(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The instance is down because it was told to be, not because it fell over."""
    state = State()
    state.held["chernarus"] = datetime.now().astimezone()
    state.processes["chernarus"] = ProcessInfo(
        pid=1, port=2302, started_at=datetime.now().astimezone(), create_time=None
    )
    save_state(cfg.state_file, state)
    monkeypatch.setattr(runner, "is_running", lambda config, state, name: False)
    monkeypatch.setattr(
        runner, "start", lambda *args, **kwargs: pytest.fail("must not restart a held instance")
    )

    _supervise_briefly(cfg, state)


def test_a_held_instance_skips_its_scheduled_restart(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduled = cfg.model_copy(
        update={
            "instances": {
                "chernarus": InstanceConfig(
                    port=2302, mods=[], restart_schedule=["00:00"], restart_after_hours=None
                )
            }
        }
    )
    state = State()
    state.held["chernarus"] = datetime.now().astimezone()
    save_state(scheduled.state_file, state)
    monkeypatch.setattr(runner, "is_running", lambda config, state, name: False)
    monkeypatch.setattr(
        runner, "start", lambda *args, **kwargs: pytest.fail("must not restart a held instance")
    )
    monkeypatch.setattr(
        runner, "stop", lambda *args, **kwargs: pytest.fail("must not restart a held instance")
    )

    _supervise_briefly(scheduled, state)


def test_an_unheld_instance_is_still_supervised(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hold must be specific, not a way to switch supervision off by accident."""
    state = State()
    save_state(cfg.state_file, state)
    monkeypatch.setattr(runner, "is_running", lambda config, state, name: False)
    started: list[str] = []

    def record(config: Config, state: State, name: str, **kwargs: object) -> int:
        started.append(name)
        return 1

    monkeypatch.setattr(runner, "start", record)

    _supervise_briefly(cfg, state)

    assert started == ["chernarus"]
