"""The supervision loop as something a service can run.

What matters for a long-running process: it stops promptly when asked, it does not take
the game servers down with it, and it notices state changed by the API rather than
overwriting it from a stale in-memory copy.
"""

from __future__ import annotations

import threading
import time

import pytest

from dzsm import runner
from dzsm.config import Config, InstanceConfig
from dzsm.state import State, save_state


@pytest.fixture
def cfg(config: Config) -> Config:
    return config.model_copy(
        update={
            "instances": {"chernarus": InstanceConfig(port=2302, mods=[])},
            "supervise_poll_seconds": 30,
        }
    )


def _supervise_in_background(
    cfg: Config, state: State, stop: threading.Event
) -> threading.Thread:
    thread = threading.Thread(
        target=runner.supervise, args=(cfg, state, ["chernarus"]), kwargs={"stop": stop}
    )
    thread.start()
    return thread


def test_it_stops_promptly_rather_than_waiting_out_the_poll(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """wait() not sleep(): a service stop should not sit through a 30s interval."""
    monkeypatch.setattr(runner, "is_running", lambda config, state, name: True)
    stop = threading.Event()
    thread = _supervise_in_background(cfg, State(), stop)

    time.sleep(0.1)
    started = time.monotonic()
    stop.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert time.monotonic() - started < 2, "should not have waited for the poll interval"


def test_stopping_does_not_stop_the_instances(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dzsm going down must not take the game servers with it."""
    monkeypatch.setattr(runner, "is_running", lambda config, state, name: True)
    monkeypatch.setattr(
        runner, "stop", lambda *args, **kwargs: pytest.fail("must not stop instances")
    )
    stop = threading.Event()
    thread = _supervise_in_background(cfg, State(), stop)

    time.sleep(0.1)
    stop.set()
    thread.join(timeout=5)

    assert not thread.is_alive()


def test_an_already_set_stop_exits_immediately(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "is_running", lambda config, state, name: True)
    stop = threading.Event()
    stop.set()

    thread = _supervise_in_background(cfg, State(), stop)
    thread.join(timeout=5)

    assert not thread.is_alive()


# --- state shared with the API -----------------------------------------------


def test_state_is_re_read_each_pass(cfg: Config) -> None:
    """The API mutates state in another thread; a stale copy here would overwrite it."""
    on_disk = State()
    on_disk.pins["chernarus"] = {"CF": 1}
    save_state(cfg.state_file, on_disk)

    stale = State()
    reloaded = runner._reload(cfg, stale)

    assert reloaded.pins == {"chernarus": {"CF": 1}}


def test_an_unreadable_state_keeps_the_in_memory_copy(cfg: Config) -> None:
    """A corrupt file must not take the supervisor down with it."""
    cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_file.write_text("{not json", encoding="utf-8")
    current = State()
    current.pins["chernarus"] = {"CF": 2}

    assert runner._reload(cfg, current) is current


def test_a_missing_state_file_is_fine(cfg: Config) -> None:
    assert runner._reload(cfg, State()).pins == {}
