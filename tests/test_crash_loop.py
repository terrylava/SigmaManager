"""An instance that will not stay up.

The supervisor used to do one thing about it: restart it, every poll interval, forever,
writing one line each time. That is the shape a mod broken by a game update produces,
and it is indistinguishable from a healthy server in everything except the log.

Two separate questions here. How long to keep trying, and whether the thing that
changed since it last worked is the mod versions.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from dzsm import runner
from dzsm.config import Config, InstanceConfig
from dzsm.state import FailureInfo, ProcessInfo, State


@pytest.fixture
def cfg(config: Config) -> Config:
    return config.model_copy(
        update={"instances": {"chernarus": InstanceConfig(port=2302, mods=[])}}
    )


def _failed(count: int, *, ago_seconds: float = 10_000) -> FailureInfo:
    when = datetime.now() - timedelta(seconds=ago_seconds)
    return FailureInfo(count=count, first_at=when, last_at=when, last_error="boom")


# --- backing off ---------------------------------------------------------------


def test_a_first_attempt_is_never_blocked(cfg: Config) -> None:
    assert runner._may_restart(cfg, State(), "chernarus", datetime.now()) is True


def test_a_fresh_failure_waits(cfg: Config) -> None:
    """15 seconds after the first, not immediately: a server that just died is rarely
    ready to start again in the same breath."""
    state = State()
    state.failures["chernarus"] = _failed(1, ago_seconds=2)

    assert runner._may_restart(cfg, state, "chernarus", datetime.now()) is False


def test_it_tries_again_once_the_wait_is_over(cfg: Config) -> None:
    state = State()
    state.failures["chernarus"] = _failed(1, ago_seconds=30)

    assert runner._may_restart(cfg, state, "chernarus", datetime.now()) is True


def test_the_wait_grows(cfg: Config) -> None:
    """15s, a minute, five, fifteen. A server that has failed four times is not going to
    be fixed by asking again in fifteen seconds."""
    state = State()
    state.failures["chernarus"] = _failed(4, ago_seconds=120)

    assert runner._may_restart(cfg, state, "chernarus", datetime.now()) is False

    state.failures["chernarus"] = _failed(4, ago_seconds=1000)
    assert runner._may_restart(cfg, state, "chernarus", datetime.now()) is True


def test_it_stops_at_the_limit(cfg: Config) -> None:
    state = State()
    state.failures["chernarus"] = _failed(cfg.crash_restart_limit)

    assert runner._may_restart(cfg, state, "chernarus", datetime.now()) is False


def test_the_limit_is_said_once(cfg: Config, caplog: pytest.LogCaptureFixture) -> None:
    """Not every poll. The count going past the limit is what makes that the last line
    written about it."""
    state = State()
    state.failures["chernarus"] = _failed(cfg.crash_restart_limit)

    runner._may_restart(cfg, state, "chernarus", datetime.now())
    first = caplog.text.count("Not trying again")
    runner._may_restart(cfg, state, "chernarus", datetime.now())

    assert first == 1
    assert caplog.text.count("Not trying again") == 1


def test_a_limit_of_zero_never_gives_up(cfg: Config) -> None:
    """For someone who would rather it kept trying than went quiet."""
    forever = cfg.model_copy(update={"crash_restart_limit": 0})
    state = State()
    state.failures["chernarus"] = _failed(50)

    assert runner._may_restart(forever, state, "chernarus", datetime.now()) is True


# --- recovering ----------------------------------------------------------------


def test_staying_up_clears_the_failures(cfg: Config) -> None:
    state = State()
    state.failures["chernarus"] = _failed(3)
    state.processes["chernarus"] = ProcessInfo(
        pid=1, port=2302, started_at=datetime.now() - timedelta(seconds=600)
    )

    runner._note_healthy(cfg, state, "chernarus")

    assert "chernarus" not in state.failures


def test_starting_is_not_enough(cfg: Config) -> None:
    """A process that has been alive for four seconds has not proved anything — that is
    exactly what a crash loop looks like from the outside."""
    state = State()
    state.failures["chernarus"] = _failed(3)
    state.processes["chernarus"] = ProcessInfo(
        pid=1, port=2302, started_at=datetime.now() - timedelta(seconds=4)
    )

    runner._note_healthy(cfg, state, "chernarus")

    assert "chernarus" in state.failures


def test_a_person_asking_clears_the_failed_state(cfg: Config) -> None:
    """A limit reached hours ago must not stop someone who has just fixed the problem.
    If it was not fixed, the count earns itself back on the next pass."""
    state = State()
    state.failures["chernarus"] = _failed(99)

    assert runner.clear_failures(state, "chernarus") is True
    assert "chernarus" not in state.failures


def test_the_supervisor_restarting_does_not_clear_it(cfg: Config) -> None:
    """The counter the backoff reads must survive the restart it is counting. This was
    inside start(), which the supervisor calls — so it wiped its own counter every pass
    and the backoff did nothing at all. 36 restarts in 75 seconds, watched."""
    state = State()
    state.failures["chernarus"] = _failed(3)

    runner._start_and_save(cfg, state, "chernarus", reason="crash restart")

    assert state.failures["chernarus"].count == 4


def test_a_supervised_start_that_cannot_run_at_all_is_counted_not_raised(
    cfg: Config,
) -> None:
    """InstanceError used to come out of start() uncaught, through the supervise loop,
    and end supervision for every instance — one broken server taking the others'
    crash restarts with it."""
    state = State()

    runner._start_and_save(cfg, state, "chernarus", reason="crash restart")

    assert state.failures["chernarus"].count == 1
    assert "no server app" in (state.failures["chernarus"].last_error or "")


def test_what_it_stayed_up_on_is_remembered(cfg: Config) -> None:
    """Versions earn the name "good" by surviving, not by being started."""
    state = State()
    state.pins["chernarus"] = {"CF": 1721005200}
    state.processes["chernarus"] = ProcessInfo(
        pid=1, port=2302, started_at=datetime.now() - timedelta(seconds=600)
    )

    runner._note_healthy(cfg, state, "chernarus")

    assert state.good_pins["chernarus"] == {"CF": 1721005200}


# --- rolling back --------------------------------------------------------------


def test_a_second_failure_rolls_back(cfg: Config) -> None:
    """The first can be anything — a port still held, a slow disk. By the second, if the
    versions changed since it last worked, they are the suspect."""
    state = State()
    state.good_pins["chernarus"] = {"CF": 100}
    state.pins["chernarus"] = {"CF": 200}

    runner._note_failure(cfg, state, "chernarus", "died")
    assert "chernarus" not in state.forced_versions

    runner._note_failure(cfg, state, "chernarus", "died again")
    assert state.forced_versions["chernarus"] == {"CF": 100}


def test_nothing_rolls_back_when_nothing_changed(cfg: Config) -> None:
    """If it is running what it last worked on, the mods are not what broke it, and
    pretending otherwise would hide the real cause."""
    state = State()
    state.good_pins["chernarus"] = {"CF": 100}
    state.pins["chernarus"] = {"CF": 100}

    runner._note_failure(cfg, state, "chernarus", "died")
    runner._note_failure(cfg, state, "chernarus", "died again")

    assert "chernarus" not in state.forced_versions


def test_nothing_rolls_back_without_a_known_good(cfg: Config) -> None:
    """A first start that never worked has nowhere to go back to."""
    state = State()
    state.pins["chernarus"] = {"CF": 200}

    runner._note_failure(cfg, state, "chernarus", "died")
    runner._note_failure(cfg, state, "chernarus", "died again")

    assert "chernarus" not in state.forced_versions


def test_rollback_can_be_turned_off(cfg: Config) -> None:
    off = cfg.model_copy(update={"rollback_on_failed_start": False})
    state = State()
    state.good_pins["chernarus"] = {"CF": 100}
    state.pins["chernarus"] = {"CF": 200}

    runner._note_failure(off, state, "chernarus", "died")
    runner._note_failure(off, state, "chernarus", "died again")

    assert "chernarus" not in state.forced_versions


def test_the_forced_versions_are_used_and_consumed(cfg: Config) -> None:
    """One attempt on the old versions. If it works they earn their way back to
    good_pins; if it does not, nothing is quietly pinned forever."""
    state = State()
    state.forced_versions["chernarus"] = {"CF": 100}

    assert runner.versions_to_activate(cfg, state, "chernarus") == {"CF": 100}
    assert "chernarus" not in state.forced_versions
