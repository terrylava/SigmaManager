"""Restart warnings over RCON.

The rules that matter: warnings are rate-limited to the configured interval, the loop
ticks fast enough to actually deliver them, and a broken RCON never delays the restart it
is announcing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from dzsm import runner
from dzsm.config import Config, InstanceConfig, RconConfig
from dzsm.rcon import RconError
from dzsm.state import ProcessInfo, State


@pytest.fixture
def cfg(config: Config) -> Config:
    return config.model_copy(
        update={
            "instances": {
                "chernarus": InstanceConfig(port=2302, mods=[], rcon_port=2306),
            }
        }
    )


class _Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.said: list[str] = []
        self.closed = False
        self._fail = fail

    def say(self, message: str) -> None:
        if self._fail:
            raise RconError("connection reset")
        self.said.append(message)

    def close(self) -> None:
        self.closed = True


# --- port derivation ---------------------------------------------------------


def test_rcon_port_defaults_to_three_above_the_game_port() -> None:
    """Stock DayZ layout: a 2302 server has RCON on 2305."""
    assert InstanceConfig(port=2302, mods=[]).resolved_rcon_port() == 2305


def test_the_offset_follows_a_custom_port() -> None:
    assert InstanceConfig(port=2502, mods=[]).resolved_rcon_port() == 2505


def test_an_explicit_port_wins() -> None:
    instance = InstanceConfig(port=2302, mods=[], rcon_port=2306)
    assert instance.resolved_rcon_port() == 2306


def test_rcon_can_be_turned_off() -> None:
    instance = InstanceConfig(port=2302, mods=[], rcon_enabled=False)
    assert instance.resolved_rcon_port() is None


def test_disabling_beats_an_explicit_port() -> None:
    """Otherwise "off" would quietly not be off."""
    instance = InstanceConfig(port=2302, mods=[], rcon_port=2306, rcon_enabled=False)
    assert instance.resolved_rcon_port() is None


def test_a_disabled_instance_gets_no_client(config: Config) -> None:
    disabled = config.model_copy(
        update={
            "instances": {
                "chernarus": InstanceConfig(port=2302, mods=[], rcon_enabled=False)
            }
        }
    )
    assert runner.rcon_client(disabled, "chernarus") is None


def test_a_missing_password_names_the_variable_and_the_way_out(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RCON is on by default now, so this error will be the first thing many users see."""
    enabled = config.model_copy(
        update={"instances": {"chernarus": InstanceConfig(port=2302, mods=[])}}
    )
    monkeypatch.delenv("DZSM_RCON_PASSWORD", raising=False)

    with pytest.raises(RconError, match="DZSM_RCON_PASSWORD"):
        runner.rcon_client(enabled, "chernarus")


# --- phrasing ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (300, "5 minutes"),
        (125, "2 minutes"),
        (90, "1 minute"),
        (45, "45 seconds"),
        (0, "0 seconds"),
    ],
)
def test_humanize(seconds: int, expected: str) -> None:
    assert runner.humanize(seconds) == expected


def test_humanize_never_goes_negative() -> None:
    """Clock skew must not announce a restart 'in -3 seconds'."""
    assert runner.humanize(-5) == "0 seconds"


# --- rate limiting -----------------------------------------------------------


def test_a_warning_is_sent(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(runner, "rcon_client", lambda config, name: recorder)

    runner._warn_players(cfg, State(), "chernarus", {}, remaining=120)

    assert recorder.said == ["Server restart in 2 minutes"]
    assert recorder.closed is True


def test_warnings_respect_the_interval(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(runner, "rcon_client", lambda config, name: recorder)
    warned = {"chernarus": datetime.now()}

    runner._warn_players(cfg, State(), "chernarus", warned, remaining=120)

    assert recorder.said == [], "a warning one second after the last must be suppressed"


def test_a_warning_is_sent_once_the_interval_has_passed(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(runner, "rcon_client", lambda config, name: recorder)
    warned = {"chernarus": datetime.now() - timedelta(seconds=10)}

    runner._warn_players(cfg, State(), "chernarus", warned, remaining=60)

    assert recorder.said == ["Server restart in 1 minute"]


def test_the_final_warning_ignores_the_interval(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restart is happening now; players get told regardless of when the last was."""
    recorder = _Recorder()
    monkeypatch.setattr(runner, "rcon_client", lambda config, name: recorder)
    warned = {"chernarus": datetime.now()}

    runner._warn_players(cfg, State(), "chernarus", warned, remaining=0, final=True)

    assert recorder.said == ["Server restart in 0 seconds"]


# --- failure is never fatal --------------------------------------------------


def test_a_failing_send_does_not_raise(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 4 in spirit: a flaky RCON must not delay or prevent the restart."""
    recorder = _Recorder(fail=True)
    monkeypatch.setattr(runner, "rcon_client", lambda config, name: recorder)

    runner._warn_players(cfg, State(), "chernarus", {}, remaining=60)

    assert recorder.closed is True, "the socket is still closed on the failure path"


def test_an_unreachable_rcon_does_not_raise(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable(config: Config, name: str) -> None:
        raise RconError("connect failed")

    monkeypatch.setattr(runner, "rcon_client", unreachable)
    runner._warn_players(cfg, State(), "chernarus", {}, remaining=60)


def test_no_warning_while_the_instance_is_still_starting(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short restart_after_hours, or a crash-restart loop, can put a warning inside the
    window where BattlEye has not bound the port yet."""
    recorder = _Recorder()
    monkeypatch.setattr(
        runner, "rcon_client", lambda config, name: pytest.fail("should not connect")
    )
    state = State()
    state.processes["chernarus"] = ProcessInfo(
        pid=1, port=2302, started_at=datetime.now().astimezone(), create_time=None
    )

    runner._warn_players(cfg, state, "chernarus", {}, remaining=60)

    assert recorder.said == []


def test_an_instance_without_rcon_is_silently_skipped(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opting out is not an error."""
    plain = config.model_copy(
        update={"instances": {"chernarus": InstanceConfig(port=2302, mods=[])}}
    )
    monkeypatch.setattr(runner, "rcon_client", lambda config, name: None)

    warned: dict[str, datetime] = {}
    runner._warn_players(plain, State(), "chernarus", warned, remaining=60)

    assert warned == {}


# --- the two tiers -----------------------------------------------------------


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (3600, None),   # an hour out: silence
        (1801, None),   # just outside the early window
        (1800, 300),    # 30 minutes: every 5 minutes
        (600, 300),     # still the early tier
        (301, 300),     # one second before the switch
        (300, 4),       # 5 minutes: every 4 seconds
        (10, 4),
        (0, 4),
    ],
)
def test_the_interval_tiers(cfg: Config, remaining: int, expected: int | None) -> None:
    """Every 5 minutes from 30 minutes out, then every 4 seconds for the last 5."""
    assert runner.warning_interval(cfg, remaining) == expected


def test_the_early_tier_repeats_at_five_minutes(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(runner, "rcon_client", lambda config, name: recorder)
    warned = {"chernarus": datetime.now() - timedelta(seconds=299)}

    runner._warn_players(cfg, State(), "chernarus", warned, remaining=1200)
    assert recorder.said == [], "299s after the last is still inside the 5 minute tier"

    warned["chernarus"] = datetime.now() - timedelta(seconds=301)
    runner._warn_players(cfg, State(), "chernarus", warned, remaining=1200)
    assert recorder.said == ["Server restart in 20 minutes"]


def test_nothing_is_said_outside_both_windows(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(runner, "rcon_client", lambda config, name: recorder)

    runner._warn_players(cfg, State(), "chernarus", {}, remaining=7200)

    assert recorder.said == []


# --- loop timing -------------------------------------------------------------


def test_the_loop_ticks_slowly_when_no_restart_is_near(cfg: Config) -> None:
    due = {"chernarus": runner.Due(datetime.now() + timedelta(hours=3))}
    assert runner._tick_seconds(cfg, due) == cfg.supervise_poll_seconds


def test_the_loop_ticks_fast_inside_the_final_window(cfg: Config) -> None:
    """A 15s poll cannot deliver a message every 4s, so the tick has to shorten."""
    due = {"chernarus": runner.Due(datetime.now() + timedelta(seconds=60))}

    assert runner._tick_seconds(cfg, due) == cfg.rcon.final_warning_interval_seconds


def test_the_early_window_does_not_slow_the_loop_down(cfg: Config) -> None:
    """The early tier warns every 5 minutes, but crash detection still needs the poll."""
    due = {"chernarus": runner.Due(datetime.now() + timedelta(minutes=20))}

    assert runner._tick_seconds(cfg, due) == cfg.supervise_poll_seconds


def test_an_instance_with_no_schedule_does_not_speed_up_the_loop(cfg: Config) -> None:
    assert runner._tick_seconds(cfg, {"chernarus": runner.Due(None)}) == cfg.supervise_poll_seconds


def test_a_custom_interval_is_honoured(config: Config) -> None:
    tuned = config.model_copy(
        update={"rcon": RconConfig(final_warning_interval_seconds=10)}
    )
    due = {"chernarus": runner.Due(datetime.now() + timedelta(seconds=30))}

    assert runner._tick_seconds(tuned, due) == 10
