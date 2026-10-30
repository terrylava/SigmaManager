"""Restarting onto a downloaded mod update, with a countdown.

A mod update used to be downloaded and then wait for whatever restart came next, which
on a server with `restart_after_hours = 7` was hours of refusing every client whose
launcher had already pulled the new version. Three things had to change, and each has
tests here:

- an update starts a countdown of its own (`mod_update_grace_minutes`);
- the supervisor notices one that arrives mid-run — it used to work out the next restart
  only at the previous one, so it never saw an update land;
- the daemon supervises instances added after it started. It used to capture the list
  once, and an instance added from the UI ran unsupervised for days.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from dzsm import api, mods, runner
from dzsm.config import Config, InstanceConfig, WorkshopMod
from dzsm.state import ProcessInfo, State, load_state, save_state

CF = 1559212036
OLD, NEW, NEWER = 100, 200, 300


@pytest.fixture
def cfg(config: Config) -> Config:
    return config.model_copy(
        update={
            "mods": {"CF": WorkshopMod(workshop_id=CF)},
            "instances": {"chernarus": InstanceConfig(port=2302, mods=["CF"])},
            "supervise_poll_seconds": 30,
        }
    )


def _running(*, started_minutes_ago: float = 60.0) -> State:
    """An instance running OLD, with OLD in the cache."""
    state = State()
    state.record_version(CF, OLD)
    _arrived(state, OLD, minutes_ago=started_minutes_ago + 60)
    state.pins["chernarus"] = {"CF": OLD}
    state.processes["chernarus"] = ProcessInfo(
        pid=1,
        port=2302,
        started_at=datetime.now(UTC) - timedelta(minutes=started_minutes_ago),
        create_time=None,
    )
    return state


def _arrived(state: State, version: int, *, minutes_ago: float) -> None:
    if version not in state.known_versions(CF):
        state.record_version(CF, version)
    state.versions[CF][version].downloaded_at = datetime.now(UTC) - timedelta(
        minutes=minutes_ago
    )


def _local(value: datetime) -> datetime:
    return value.astimezone().replace(tzinfo=None)


# --- the deadline ------------------------------------------------------------------


def test_a_version_downloaded_mid_run_starts_the_countdown(cfg: Config) -> None:
    state = _running()
    _arrived(state, NEW, minutes_ago=5)

    due = runner.mod_restart_due(cfg, state, "chernarus")

    expected = _local(state.versions[CF][NEW].downloaded_at) + timedelta(minutes=30)
    assert due == expected


def test_nothing_newer_means_no_restart(cfg: Config) -> None:
    assert runner.mod_restart_due(cfg, _running(), "chernarus") is None


def test_the_first_arrival_sets_the_time(cfg: Config) -> None:
    """A countdown that jumped back each time another mod finished downloading would
    never reach zero on a busy update day. Later arrivals ride along."""
    state = _running()
    _arrived(state, NEW, minutes_ago=10)
    first = runner.mod_restart_due(cfg, state, "chernarus")
    _arrived(state, NEWER, minutes_ago=1)

    assert runner.mod_restart_due(cfg, state, "chernarus") == first


def test_the_version_a_rollback_left_is_not_an_update(cfg: Config) -> None:
    """After rolling back, the version that failed is still the newest in the cache;
    counting it would restart straight back onto it."""
    state = _running(started_minutes_ago=10)
    _arrived(state, NEW, minutes_ago=60)
    state.rolled_back_from["chernarus"] = {"CF": NEW}

    assert runner.mod_restart_due(cfg, state, "chernarus") is None


def test_a_fix_published_after_a_rollback_is_an_update(cfg: Config) -> None:
    """Only the version that failed is skipped, not everything after it."""
    state = _running(started_minutes_ago=10)
    _arrived(state, NEW, minutes_ago=60)
    _arrived(state, NEWER, minutes_ago=2)
    state.rolled_back_from["chernarus"] = {"CF": NEW}

    assert runner.mod_restart_due(cfg, state, "chernarus") is not None


def test_a_rollback_records_what_it_moved_off(cfg: Config) -> None:
    state = State()
    state.good_pins["chernarus"] = {"CF": OLD}
    state.pins["chernarus"] = {"CF": NEW}

    runner._roll_back(cfg, state, "chernarus")

    assert state.forced_versions["chernarus"] == {"CF": OLD}
    assert state.rolled_back_from["chernarus"] == {"CF": NEW}


def test_a_version_that_just_missed_a_restart_is_still_an_update(cfg: Config) -> None:
    """The live failure. The restart began while the batch was downloading; NamantH's
    update finished seconds before the new process started but was not part of it. The
    old rule ("downloaded after this process started") skipped it for good, and every
    updated client was kicked for a PBO the server did not have."""
    state = _running(started_minutes_ago=1)
    _arrived(state, NEW, minutes_ago=1.2)

    due = runner.mod_restart_due(cfg, state, "chernarus")

    assert due is not None
    started = _local(state.processes["chernarus"].started_at)
    # Counted from the start it missed, not from the download before it.
    assert due == started + timedelta(minutes=30)


def test_an_update_restart_waits_for_the_batch(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, fast: None
) -> None:
    """Restarting when the first mod of a batch has landed activates that one and leaves
    the rest behind. Due or empty, it waits until the downloads are done."""
    save_state(cfg.state_file, _running())
    _counting(monkeypatch, 0)
    restarts: list[str] = []
    monkeypatch.setattr(runner, "_warn_players", lambda *a, **kw: None)
    monkeypatch.setattr(
        runner,
        "_due",
        lambda *a: runner.Due(at=datetime.now() - timedelta(seconds=1), mods_update=True),
    )
    monkeypatch.setattr(
        runner, "_restart_and_save", lambda c, s, n, *, reason: restarts.append(reason)
    )

    with mods.downloading():
        _supervise_briefly(cfg, _running(), ["chernarus"])
        assert restarts == []

    _supervise_briefly(cfg, _running(), ["chernarus"])
    assert restarts


def test_a_scheduled_restart_does_not_wait_for_downloads(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, fast: None
) -> None:
    """A routine restart's time is a promise to players; it has no reason to wait."""
    save_state(cfg.state_file, _running())
    restarts: list[str] = []
    monkeypatch.setattr(runner, "_warn_players", lambda *a, **kw: None)
    monkeypatch.setattr(
        runner, "_due", lambda *a: runner.Due(at=datetime.now() - timedelta(seconds=1))
    )
    monkeypatch.setattr(
        runner, "_restart_and_save", lambda c, s, n, *, reason: restarts.append(reason)
    )

    with mods.downloading():
        _supervise_briefly(cfg, _running(), ["chernarus"])

    assert restarts


def test_the_update_job_does_not_overwrite_what_the_supervisor_saved(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job read state once and saved that copy after every mod, so a restart the
    supervisor recorded during a long download was erased: state.json went on naming a
    process that had been replaced, on versions it no longer ran."""
    save_state(cfg.state_file, _running())
    monkeypatch.setattr(api, "check_updates", lambda ids: {CF: NEW})

    def download(config: Config, workshop_id: int) -> object:
        # Mid-download, the supervisor restarts the instance and records it.
        restarted = load_state(config.state_file)
        restarted.processes["chernarus"] = ProcessInfo(
            pid=4242, port=2302, started_at=datetime.now(UTC), create_time=None
        )
        save_state(config.state_file, restarted)
        return object()

    def promote(config: Config, state: State, workshop_id: int, version: int) -> Path:
        state.record_version(workshop_id, version)
        return config.version_dir(workshop_id, version)

    monkeypatch.setattr(api, "download_mod", download)
    monkeypatch.setattr(api, "verify_download", lambda config, result: None)
    monkeypatch.setattr(api, "promote_snapshot", promote)
    jobs = api.JobRegistry()
    job = jobs.create("mods.update")

    api._run_mod_update(cfg, jobs, job.id)

    after = load_state(cfg.state_file)
    assert after.processes["chernarus"].pid == 4242, "the supervisor's write survived"
    assert NEW in after.known_versions(CF), "and the promotion was recorded too"


def test_a_stopped_instance_has_no_update_deadline(cfg: Config) -> None:
    """Its next start picks up the newest anyway."""
    state = _running()
    _arrived(state, NEW, minutes_ago=5)
    state.processes.clear()

    assert runner.mod_restart_due(cfg, state, "chernarus") is None


def test_zero_grace_leaves_it_to_the_schedule(cfg: Config) -> None:
    off = cfg.model_copy(update={"mod_update_grace_minutes": 0})
    state = _running()
    _arrived(state, NEW, minutes_ago=5)

    assert runner.mod_restart_due(off, state, "chernarus") is None


def test_a_pinned_mod_is_not_moved(cfg: Config) -> None:
    pinned = cfg.model_copy(
        update={"mods": {"CF": WorkshopMod(workshop_id=CF, pinned_version=OLD)}}
    )
    pinned.version_dir(CF, OLD).mkdir(parents=True)
    state = _running()
    _arrived(state, NEW, minutes_ago=5)

    assert runner.mod_restart_due(pinned, state, "chernarus") is None


def test_a_newly_added_mod_is_not_an_update(cfg: Config) -> None:
    """Adding a mod is a config change and waits for the next restart like any other."""
    state = _running()
    state.pins["chernarus"] = {}
    state.pins["chernarus"]["Other"] = 1
    _arrived(state, NEW, minutes_ago=5)

    assert runner.mod_restart_due(cfg, state, "chernarus") is None


def test_it_beats_a_later_scheduled_restart(cfg: Config) -> None:
    state = _running(started_minutes_ago=10)
    _arrived(state, NEW, minutes_ago=1)

    due = runner.due_at(cfg, state, "chernarus")

    assert due.mods_update is True
    assert due.reason == "mod update"


def test_a_sooner_scheduled_restart_still_wins(cfg: Config) -> None:
    """It picks the update up anyway, so there is no reason to restart twice."""
    soon = cfg.model_copy(
        update={
            "instances": {
                "chernarus": InstanceConfig(port=2302, mods=["CF"], restart_after_hours=1.1)
            }
        }
    )
    state = _running(started_minutes_ago=60)
    _arrived(state, NEW, minutes_ago=1)

    due = runner.due_at(soon, state, "chernarus")

    assert due.mods_update is False
    assert due.reason == "scheduled"


def test_an_update_noticed_late_still_gets_a_full_warning(cfg: Config) -> None:
    """Downloaded while the daemon was down, the derived deadline is already past.
    Restarting the moment it is noticed would give players no warning at all."""
    state = _running(started_minutes_ago=600)
    _arrived(state, NEW, minutes_ago=300)
    now = datetime.now()

    due = runner._due(cfg, state, "chernarus", None, {}, now)

    assert due.mods_update is True
    assert due.at is not None
    assert due.at >= now + timedelta(minutes=29)


def test_the_late_notice_is_remembered_between_passes(cfg: Config) -> None:
    """Otherwise every pass would push the deadline another 30 minutes out."""
    state = _running(started_minutes_ago=600)
    _arrived(state, NEW, minutes_ago=300)
    noticed: dict[tuple[str, str], datetime] = {}
    first = datetime.now()

    a = runner._due(cfg, state, "chernarus", None, noticed, first)
    b = runner._due(cfg, state, "chernarus", None, noticed, first + timedelta(minutes=10))

    assert a.at == b.at


def test_players_are_told_it_is_a_mod_update(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    said: list[str] = []

    class FakeClient:
        def say(self, message: str) -> None:
            said.append(message)

        def close(self) -> None:
            pass

    monkeypatch.setattr(runner, "rcon_grace_remaining", lambda *a: 0.0)
    monkeypatch.setattr(runner, "rcon_client", lambda *a: FakeClient())

    runner._warn_players(
        cfg, State(), "chernarus", {}, remaining=600, final=True, mods_update=True
    )

    assert said == ["Mod update - server restarting in 10 minutes"]


# --- the supervisor --------------------------------------------------------------


def _supervise_briefly(
    cfg: Config, state: State, names: list[str] | None, *, config_path: Path | None = None
) -> None:
    stop = threading.Event()
    thread = threading.Thread(
        target=runner.supervise,
        args=(cfg, state, names),
        kwargs={"stop": stop, "config_path": config_path},
    )
    thread.start()
    time.sleep(0.4)
    stop.set()
    thread.join(timeout=5)


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passes every few milliseconds, everything up, nothing healthy-tracked."""
    monkeypatch.setattr(runner, "_tick_seconds", lambda config, due: 0.02)
    monkeypatch.setattr(runner, "is_running", lambda config, state, name: True)
    monkeypatch.setattr(runner, "_note_healthy", lambda *a: None)


def test_an_update_landing_mid_run_is_noticed(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, fast: None
) -> None:
    """The bug itself: the deadline was worked out once per restart, so an update that
    arrived afterwards was invisible until some other restart happened."""
    state = _running()
    save_state(cfg.state_file, state)
    warnings: list[dict[str, Any]] = []
    monkeypatch.setattr(
        runner, "_warn_players", lambda *a, **kw: warnings.append(kw)
    )

    def land_update() -> None:
        time.sleep(0.15)
        later = _running()
        _arrived(later, NEW, minutes_ago=0)
        save_state(cfg.state_file, later)

    lander = threading.Thread(target=land_update)
    lander.start()
    _supervise_briefly(cfg, state, ["chernarus"])
    lander.join()

    assert any(kw.get("mods_update") for kw in warnings)


def test_a_due_update_restarts_with_its_reason(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, fast: None
) -> None:
    save_state(cfg.state_file, _running())
    restarts: list[str] = []
    monkeypatch.setattr(runner, "_warn_players", lambda *a, **kw: None)
    monkeypatch.setattr(
        runner,
        "_due",
        lambda *a: runner.Due(at=datetime.now() - timedelta(seconds=1), mods_update=True),
    )
    monkeypatch.setattr(
        runner, "_restart_and_save", lambda c, s, n, *, reason: restarts.append(reason)
    )

    _supervise_briefly(cfg, _running(), ["chernarus"])

    assert restarts and restarts[0] == "mod update restart"


def test_an_instance_added_after_startup_is_supervised(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, fast: None
) -> None:
    """Added from the web UI and started there, it used to get no crash restarts, no
    scheduled restarts and no updates, while looking like every other instance."""
    save_state(cfg.state_file, _running())
    grown = cfg.model_copy(
        update={
            "instances": {
                **cfg.instances,
                "livonia": InstanceConfig(port=2402, mods=["CF"]),
            }
        }
    )
    monkeypatch.setattr(runner, "_reload_config", lambda path, current: grown)
    seen: set[str] = set()

    def record(c: Config, s: State, name: str, *a: object) -> runner.Due:
        seen.add(name)
        return runner.Due(at=None)

    monkeypatch.setattr(runner, "_due", record)

    _supervise_briefly(cfg, _running(), None, config_path=Path("config.toml"))

    assert "livonia" in seen


def test_a_new_instance_that_is_not_running_is_not_started(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, fast: None
) -> None:
    """It may still be being set up. Supervision starts when someone presses Start."""
    save_state(cfg.state_file, _running())
    grown = cfg.model_copy(
        update={
            "instances": {
                **cfg.instances,
                "livonia": InstanceConfig(port=2402, mods=["CF"]),
            }
        }
    )
    monkeypatch.setattr(runner, "_reload_config", lambda path, current: grown)
    monkeypatch.setattr(
        runner, "is_running", lambda config, state, name: name == "chernarus"
    )
    monkeypatch.setattr(
        runner,
        "_start_and_save",
        lambda c, s, name, **kw: pytest.fail(f"must not start {name}"),
    )

    _supervise_briefly(cfg, _running(), None, config_path=Path("config.toml"))


def test_an_explicit_list_is_not_widened(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, fast: None
) -> None:
    """`--only` means only."""
    save_state(cfg.state_file, _running())
    grown = cfg.model_copy(
        update={
            "instances": {
                **cfg.instances,
                "livonia": InstanceConfig(port=2402, mods=["CF"]),
            }
        }
    )
    monkeypatch.setattr(runner, "_reload_config", lambda path, current: grown)
    seen: set[str] = set()

    def record(c: Config, s: State, name: str, *a: object) -> runner.Due:
        seen.add(name)
        return runner.Due(at=None)

    monkeypatch.setattr(runner, "_due", record)

    _supervise_briefly(cfg, _running(), ["chernarus"], config_path=Path("config.toml"))

    assert seen == {"chernarus"}


# --- the checker cannot die ---------------------------------------------------------


class _Clock:
    """A stop event that lets a fixed number of passes through."""

    def __init__(self, passes: int) -> None:
        self.passes = passes

    def wait(self, delay: float) -> bool:
        if self.passes <= 0:
            return True
        self.passes -= 1
        return False


def test_an_unexpected_error_does_not_end_the_checker(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only SteamError used to be caught. Anything else ended the thread for good, and
    nothing reported it: the dashboard just kept showing the last check time."""
    calls: list[int] = []

    def explode(config: Config) -> dict[str, int]:
        calls.append(1)
        raise OSError("disk full")

    monkeypatch.setattr(api, "_check_and_record", explode)
    jobs = api.JobRegistry()

    api._auto_check(lambda: cfg, jobs, _Clock(passes=2))  # type: ignore[arg-type]

    assert len(calls) == 2
    assert [job.status for job in jobs.all()] == ["failed", "failed"]


def test_a_crashed_update_job_does_not_block_later_checks(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job left "running" makes every later pass skip, which is a dead checker by
    another name."""
    monkeypatch.setattr(
        api, "_mod_update_body", lambda *a: (_ for _ in ()).throw(KeyError("boom"))
    )
    jobs = api.JobRegistry()
    job = jobs.create("mods.update")

    api._run_mod_update(cfg, jobs, job.id)

    assert not jobs.is_running("mods.update")
    finished = jobs.get(job.id)
    assert finished is not None
    assert finished.status == "failed"


def test_an_overdue_instance_gets_a_countdown_when_adopted(cfg: Config) -> None:
    """Unsupervised for days, it is far past its window. Restarting on the first pass
    would throw everyone out with no warning."""
    overdue = datetime.now() - timedelta(days=2)

    adopted = runner._adopted(cfg, overdue)

    assert adopted is not None
    assert adopted >= datetime.now() + timedelta(minutes=29)


def test_an_instance_on_schedule_keeps_its_time(cfg: Config) -> None:
    later = datetime.now() + timedelta(hours=2)

    assert runner._adopted(cfg, later) == later


# --- nobody online ------------------------------------------------------------------


def _counting(monkeypatch: pytest.MonkeyPatch, answer: int | None) -> list[str]:
    asked: list[str] = []

    def players_online(config: Config, state: State, name: str) -> int | None:
        asked.append(name)
        return answer

    monkeypatch.setattr(runner, "players_online", players_online)
    return asked


MODS_DUE = runner.Due(at=datetime.now() + timedelta(minutes=20), mods_update=True)


def test_an_empty_server_skips_the_update_countdown(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _counting(monkeypatch, 0)

    assert runner._empty_for_update(cfg, State(), "chernarus", MODS_DUE, {}, datetime.now())


def test_a_game_update_is_cut_short_too(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _counting(monkeypatch, 0)
    due = runner.Due(at=datetime.now() + timedelta(minutes=5), update=True)

    assert runner._empty_for_update(cfg, State(), "chernarus", due, {}, datetime.now())


def test_someone_online_keeps_the_countdown(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _counting(monkeypatch, 1)

    assert not runner._empty_for_update(
        cfg, State(), "chernarus", MODS_DUE, {}, datetime.now()
    )


def test_not_knowing_keeps_the_countdown(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RCON down is not an empty server. Restarting a full one because it could not be
    asked is the outcome this must never produce."""
    _counting(monkeypatch, None)

    assert not runner._empty_for_update(
        cfg, State(), "chernarus", MODS_DUE, {}, datetime.now()
    )


def test_a_scheduled_restart_keeps_its_time(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its time is someone's deliberate choice; only an update's wait is for players."""
    asked = _counting(monkeypatch, 0)
    due = runner.Due(at=datetime.now() + timedelta(minutes=20))

    assert not runner._empty_for_update(cfg, State(), "chernarus", due, {}, datetime.now())
    assert asked == []


def test_it_can_be_turned_off(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    off = cfg.model_copy(update={"update_restart_when_empty": False})
    asked = _counting(monkeypatch, 0)

    assert not runner._empty_for_update(off, State(), "chernarus", MODS_DUE, {}, datetime.now())
    assert asked == []


def test_it_asks_once_a_minute_not_every_pass(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _counting(monkeypatch, 3)
    polled: dict[str, datetime] = {}
    now = datetime.now()

    for seconds in (0, 15, 30, 45, 61):
        runner._empty_for_update(
            cfg, State(), "chernarus", MODS_DUE, polled, now + timedelta(seconds=seconds)
        )

    assert len(asked) == 2


def test_players_still_loading_count(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Someone mid-connect is about to be playing."""
    from dzsm.rcon import parse_players

    listing = (
        "Players on server:\n"
        "[#] [IP Address]:[Port] [Ping] [GUID] [Name]\n"
        "--------------------------------------------------\n"
        "0   1.2.3.4:2304    0    abcdef0123456789abcdef0123456789(?) Someone (Lobby)\n"
        "(1 players in total)"
    )

    class FakeClient:
        def players(self) -> object:
            return parse_players(listing)

        def close(self) -> None:
            pass

    monkeypatch.setattr(runner, "rcon_grace_remaining", lambda *a: 0.0)
    monkeypatch.setattr(runner, "rcon_client", lambda *a: FakeClient())

    assert runner.players_online(cfg, State(), "chernarus") == 1


def test_the_supervisor_restarts_an_empty_server_early(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, fast: None
) -> None:
    save_state(cfg.state_file, _running())
    _counting(monkeypatch, 0)
    restarts: list[str] = []
    monkeypatch.setattr(runner, "_warn_players", lambda *a, **kw: None)
    monkeypatch.setattr(
        runner,
        "_due",
        lambda *a: runner.Due(at=datetime.now() + timedelta(minutes=25), mods_update=True),
    )
    monkeypatch.setattr(
        runner, "_restart_and_save", lambda c, s, n, *, reason: restarts.append(reason)
    )

    _supervise_briefly(cfg, _running(), ["chernarus"])

    assert restarts and restarts[0] == "mod update restart"


def test_one_instance_failing_does_not_end_supervision(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, fast: None
) -> None:
    """An OSError from a file the running server holds locked came out through the loop
    and ended supervision for every instance, silently."""
    two = cfg.model_copy(
        update={
            "instances": {
                **cfg.instances,
                "livonia": InstanceConfig(port=2402, mods=["CF"]),
            }
        }
    )
    save_state(two.state_file, _running())
    passes: list[str] = []

    def flaky(c: Config, s: State, name: str, *a: object) -> runner.Due:
        passes.append(name)
        if name == "chernarus":
            raise PermissionError("serverDZ.active.cfg is locked")
        return runner.Due(at=None)

    monkeypatch.setattr(runner, "_due", flaky)

    _supervise_briefly(two, _running(), ["chernarus", "livonia"])

    assert passes.count("chernarus") > 1, "it kept trying the broken one"
    assert passes.count("livonia") > 1, "and kept supervising the other"
