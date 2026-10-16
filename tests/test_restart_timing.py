"""Uptime-based restarts and graceful shutdown.

An instance restarts a fixed number of hours after it started (default 4), independently
of any wall-clock schedule. Stopping goes through RCON first so DayZ flushes persistence
on its own shutdown path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psutil
import pytest

from dzsm import runner
from dzsm.config import Config, InstanceConfig, RconConfig
from dzsm.rcon import RconError
from dzsm.state import ProcessInfo, State


def _started(hours_ago: float) -> datetime:
    return datetime.now().astimezone() - timedelta(hours=hours_ago)


# --- uptime-based restarts ---------------------------------------------------


def test_four_hours_is_the_default() -> None:
    assert InstanceConfig(mods=[]).restart_after_hours == 4.0


def test_restart_is_measured_from_startup() -> None:
    instance = InstanceConfig(mods=[], restart_after_hours=4)
    now = datetime(2026, 7, 25, 12, 0)
    started = datetime(2026, 7, 25, 9, 30)

    assert next_restart_at(instance, started, now) == datetime(2026, 7, 25, 13, 30)


def test_a_down_instance_has_no_uptime_deadline() -> None:
    """Nothing has started, so there is nothing to measure four hours from."""
    instance = InstanceConfig(mods=[], restart_after_hours=4)

    assert runner.next_restart(instance, started_at=None) is None


def test_it_can_be_disabled() -> None:
    instance = InstanceConfig(mods=[], restart_after_hours=None)

    assert runner.next_restart(instance, started_at=_started(10)) is None


def test_whichever_comes_first_wins() -> None:
    """A schedule and an uptime limit are independent; the nearer one applies."""
    instance = InstanceConfig(mods=[], restart_after_hours=4, restart_schedule=["13:00"])
    now = datetime(2026, 7, 25, 12, 0)
    started = datetime(2026, 7, 25, 9, 30)  # uptime deadline 13:30

    assert next_restart_at(instance, started, now) == datetime(2026, 7, 25, 13, 0)


def test_the_uptime_limit_wins_when_it_is_sooner() -> None:
    instance = InstanceConfig(mods=[], restart_after_hours=1, restart_schedule=["23:00"])
    now = datetime(2026, 7, 25, 12, 0)
    started = datetime(2026, 7, 25, 11, 30)

    assert next_restart_at(instance, started, now) == datetime(2026, 7, 25, 12, 30)


def test_an_aware_start_time_does_not_explode() -> None:
    """State stores timezone-aware timestamps; the arithmetic here is naive local."""
    instance = InstanceConfig(mods=[], restart_after_hours=4)

    result = runner.next_restart(instance, started_at=datetime.now(UTC))

    assert result is not None
    assert result.tzinfo is None


def test_fractional_hours_work() -> None:
    instance = InstanceConfig(mods=[], restart_after_hours=0.5)
    now = datetime(2026, 7, 25, 12, 0)
    started = datetime(2026, 7, 25, 11, 50)

    assert next_restart_at(instance, started, now) == datetime(2026, 7, 25, 12, 20)


def next_restart_at(
    instance: InstanceConfig, started: datetime, now: datetime
) -> datetime | None:
    return runner.next_restart(instance, started_at=started, now=now)


def test_due_at_uses_the_recorded_process(config: Config) -> None:
    cfg = config.model_copy(
        update={
            "instances": {
                "chernarus": InstanceConfig(port=2302, mods=[], restart_after_hours=4)
            }
        }
    )
    state = State()
    state.processes["chernarus"] = ProcessInfo(
        pid=1, port=2302, started_at=_started(1), create_time=None
    )

    due = runner._due_at(cfg, state, "chernarus")

    assert due.at is not None
    assert timedelta(hours=2.9) < (due.at - datetime.now()) < timedelta(hours=3.1)


# --- graceful shutdown -------------------------------------------------------


class _Client:
    def __init__(self, fail: bool = False) -> None:
        self.shutdown_called = False
        self.closed = False
        self._fail = fail

    def shutdown(self) -> None:
        self.shutdown_called = True
        if self._fail:
            raise RconError("no reply")

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def cfg(config: Config) -> Config:
    return config.model_copy(
        update={
            "instances": {"chernarus": InstanceConfig(port=2302, mods=[], rcon_port=2306)}
        }
    )


def test_shutdown_is_requested_over_rcon(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client()
    monkeypatch.setattr(runner, "rcon_client", lambda config, name: client)

    assert runner._request_shutdown(cfg, State(), "chernarus") is True
    assert client.shutdown_called is True
    assert client.closed is True


def test_no_reply_still_counts_as_sent(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """The server usually dies before answering #shutdown."""
    client = _Client(fail=True)
    monkeypatch.setattr(runner, "rcon_client", lambda config, name: client)

    assert runner._request_shutdown(cfg, State(), "chernarus") is True


def test_an_unreachable_rcon_falls_through_to_signalling(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable(config: Config, name: str) -> None:
        raise RconError("connect failed")

    monkeypatch.setattr(runner, "rcon_client", unreachable)

    assert runner._request_shutdown(cfg, State(), "chernarus") is False


def test_an_instance_without_rcon_falls_through(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = config.model_copy(
        update={"instances": {"chernarus": InstanceConfig(port=2302, mods=[])}}
    )
    monkeypatch.setattr(runner, "rcon_client", lambda config, name: None)

    assert runner._request_shutdown(plain, State(), "chernarus") is False


def test_it_can_be_turned_off(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    disabled = cfg.model_copy(update={"rcon": RconConfig(shutdown_via_rcon=False)})
    monkeypatch.setattr(
        runner, "rcon_client", lambda config, name: pytest.fail("should not connect")
    )

    assert runner._request_shutdown(disabled, State(), "chernarus") is False


# --- the startup grace window ------------------------------------------------


def _running(state: State, *, started_ago_seconds: float) -> State:
    state.processes["chernarus"] = ProcessInfo(
        pid=1,
        port=2302,
        started_at=datetime.now().astimezone() - timedelta(seconds=started_ago_seconds),
        create_time=None,
    )
    return state


def test_no_grace_for_an_instance_dzsm_did_not_start(cfg: Config) -> None:
    """Nothing to measure from, so do not block an attempt on a guess."""
    assert runner.rcon_grace_remaining(cfg, State(), "chernarus") == 0


def test_grace_remaining_counts_down(cfg: Config) -> None:
    state = _running(State(), started_ago_seconds=60)

    remaining = runner.rcon_grace_remaining(cfg, state, "chernarus")

    assert 235 < remaining < 245, "300s default, 60s elapsed"


def test_grace_expires(cfg: Config) -> None:
    state = _running(State(), started_ago_seconds=600)

    assert runner.rcon_grace_remaining(cfg, state, "chernarus") == 0


def test_shutdown_skips_rcon_during_the_grace_window(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BattlEye has not bound the port yet, so waiting out a socket timeout on every
    quick stop is pure delay."""
    state = _running(State(), started_ago_seconds=5)
    monkeypatch.setattr(
        runner, "rcon_client", lambda config, name: pytest.fail("should not connect")
    )

    assert runner._request_shutdown(cfg, state, "chernarus") is False


def test_shutdown_uses_rcon_once_the_window_has_passed(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _running(State(), started_ago_seconds=600)
    client = _Client()
    monkeypatch.setattr(runner, "rcon_client", lambda config, name: client)

    assert runner._request_shutdown(cfg, state, "chernarus") is True
    assert client.shutdown_called is True


def test_a_shorter_grace_can_be_configured(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    quick = cfg.model_copy(update={"rcon": RconConfig(startup_grace_seconds=10)})
    state = _running(State(), started_ago_seconds=30)
    client = _Client()
    monkeypatch.setattr(runner, "rcon_client", lambda config, name: client)

    assert runner._request_shutdown(quick, state, "chernarus") is True


class _Process:
    def __init__(self, exits: bool) -> None:
        self._exits = exits
        self.waited = False

    def wait(self, timeout: float | None = None) -> None:
        self.waited = True
        if not self._exits:
            raise psutil.TimeoutExpired(timeout or 0)


def test_waiting_reports_a_clean_exit() -> None:
    assert runner._wait_for_exit(_Process(exits=True), 1) is True  # type: ignore[arg-type]


def test_waiting_reports_a_timeout() -> None:
    assert runner._wait_for_exit(_Process(exits=False), 1) is False  # type: ignore[arg-type]
