"""Automatic update checking, and paging the Workshop search.

The check is the cheap half of the update cycle: one request covering every configured
mod, telling you whether a download is worth starting. Running it on a timer is what makes
the dashboard's answer to "is anything out of date" true without anyone pressing a button.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dzsm import api
from dzsm.config import Config, ConfigError, WorkshopMod, load_config
from dzsm.state import State, load_state, save_state
from dzsm.steam import SteamError, WorkshopItem

CF = 1559212036
EXPANSION = 2116157322


@pytest.fixture
def config(config: Config) -> Config:
    config.mods = {
        "CF": WorkshopMod(workshop_id=CF),
        "Expansion": WorkshopMod(workshop_id=EXPANSION),
    }
    return config


def _seed(config: Config, **kwargs: object) -> State:
    state = State(**kwargs)  # type: ignore[arg-type]
    save_state(config.state_file, state)
    return state


def _recording(calls: list[list[int]]) -> Callable[[list[int]], dict[int, int]]:
    """A check_updates stand-in that records what it was asked and finds nothing."""

    def check(ids: list[int]) -> dict[int, int]:
        calls.append(ids)
        return {}

    return check


# --- what a check records ----------------------------------------------------


def test_it_records_when_it_ran(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a timestamp there is no way to tell "nothing changed" from "never asked"."""
    _seed(config)
    monkeypatch.setattr(api, "check_updates", lambda ids: {CF: 100, EXPANSION: 200})

    api._check_and_record(config)

    assert load_state(config.state_file).last_checked_at is not None


def test_it_reports_what_is_behind(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _seed(config)
    state.record_version(CF, 100)
    save_state(config.state_file, state)
    monkeypatch.setattr(api, "check_updates", lambda ids: {CF: 100, EXPANSION: 200})

    behind = api._check_and_record(config)

    assert behind == {"Expansion": 200}


def test_it_records_the_remote_version_either_way(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recorded even when already cached, so the UI can show what the latest one is."""
    state = _seed(config)
    state.record_version(CF, 100)
    save_state(config.state_file, state)
    monkeypatch.setattr(api, "check_updates", lambda ids: {CF: 100, EXPANSION: 200})

    api._check_and_record(config)

    assert load_state(config.state_file).last_seen_remote == {CF: 100, EXPANSION: 200}


def test_a_pinned_mod_is_never_behind(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pinning is a decision already made; reporting it as out of date nags about it."""
    config.mods["CF"] = WorkshopMod(workshop_id=CF, pinned_version=50)
    _seed(config)
    monkeypatch.setattr(api, "check_updates", lambda ids: {CF: 100})

    assert "CF" not in api._check_and_record(config)


def test_an_id_steam_did_not_answer_for_is_skipped(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """check_updates omits failed ids; a missing answer is not "up to date"."""
    _seed(config)
    monkeypatch.setattr(api, "check_updates", lambda ids: {CF: 100})

    behind = api._check_and_record(config)

    assert behind == {"CF": 100}
    assert EXPANSION not in load_state(config.state_file).last_seen_remote


def test_local_mods_are_not_asked_about(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[list[int]] = []
    monkeypatch.setattr(api, "check_updates", _recording(asked))
    _seed(config)

    api._check_and_record(config)

    assert sorted(asked[0]) == sorted([CF, EXPANSION])


# --- the loop ----------------------------------------------------------------


class _Clock:
    """A stop event that lets the loop run a fixed number of passes, then stops it.

    The loop is a `while not stop.wait(delay)`, so driving it means answering wait() —
    False to run a pass, True to end. Recording the delays is how the interval is asserted
    without any real waiting.
    """

    def __init__(self, passes: int) -> None:
        self.remaining = passes
        self.delays: list[float] = []
        self._set = False

    def wait(self, timeout: float | None = None) -> bool:
        self.delays.append(timeout if timeout is not None else -1)
        if self.remaining <= 0:
            return True
        self.remaining -= 1
        return False

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True


def test_the_first_check_is_soon_not_a_full_interval(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon that just came up should be able to say whether anything is out of date."""
    _seed(config)
    monkeypatch.setattr(api, "check_updates", lambda ids: {})
    clock = _Clock(passes=1)

    api._auto_check(lambda: config, api.JobRegistry(), clock)  # type: ignore[arg-type]

    assert clock.delays[0] == api.FIRST_CHECK_DELAY_SECONDS


def test_it_waits_the_configured_interval(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.mod_check_interval_minutes = 45
    _seed(config)
    monkeypatch.setattr(api, "check_updates", lambda ids: {})
    clock = _Clock(passes=2)

    api._auto_check(lambda: config, api.JobRegistry(), clock)  # type: ignore[arg-type]

    assert clock.delays[1] == 45 * 60


def test_a_disabled_interval_checks_nothing(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.mod_check_interval_minutes = 0
    _seed(config)
    calls: list[list[int]] = []
    monkeypatch.setattr(api, "check_updates", _recording(calls))
    clock = _Clock(passes=2)

    api._auto_check(lambda: config, api.JobRegistry(), clock)  # type: ignore[arg-type]

    assert calls == []


def test_a_disabled_interval_still_polls_the_setting(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turning checks back on in the UI has to be noticed by the thread that does them."""
    config.mod_check_interval_minutes = 0
    _seed(config)
    monkeypatch.setattr(api, "check_updates", lambda ids: {})
    clock = _Clock(passes=2)

    api._auto_check(lambda: config, api.JobRegistry(), clock)  # type: ignore[arg-type]

    assert clock.delays[1] == api.DISABLED_POLL_SECONDS


def test_the_interval_is_re_read_between_passes(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It closes over the config getter, so a settings change applies without a restart."""
    config.mod_check_interval_minutes = 30
    _seed(config)
    monkeypatch.setattr(api, "check_updates", lambda ids: {})
    clock = _Clock(passes=3)

    def get_config() -> Config:
        config.mod_check_interval_minutes = 5 if len(clock.delays) > 1 else 30
        return config

    api._auto_check(get_config, api.JobRegistry(), clock)  # type: ignore[arg-type]

    assert clock.delays[1] == 30 * 60
    assert clock.delays[2] == 5 * 60


def test_a_steam_failure_does_not_stop_the_loop(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This runs unattended; a flaky API must not take the checker down (Rule 3)."""
    _seed(config)

    def explode(ids: list[int]) -> dict[int, int]:
        raise SteamError("Steam is down")

    monkeypatch.setattr(api, "check_updates", explode)
    jobs = api.JobRegistry()
    clock = _Clock(passes=2)

    api._auto_check(lambda: config, jobs, clock)  # type: ignore[arg-type]

    assert len(clock.delays) == 3
    assert [j.status for j in jobs.all()] == ["failed", "failed"]


def test_it_does_not_download_by_default(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(config)
    monkeypatch.setattr(api, "check_updates", lambda ids: {CF: 100})
    downloads: list[str] = []
    monkeypatch.setattr(api, "_run_mod_update", lambda c, j, i: downloads.append(i))

    api._auto_check(lambda: config, api.JobRegistry(), _Clock(passes=1))  # type: ignore[arg-type]

    assert downloads == []


def test_it_downloads_when_asked(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    config.mod_check_auto_download = True
    _seed(config)
    monkeypatch.setattr(api, "check_updates", lambda ids: {CF: 100})
    downloads: list[str] = []
    monkeypatch.setattr(api, "_run_mod_update", lambda c, j, i: downloads.append(i))

    api._auto_check(lambda: config, api.JobRegistry(), _Clock(passes=1))  # type: ignore[arg-type]

    assert len(downloads) == 1


def test_it_does_not_download_when_nothing_changed(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.mod_check_auto_download = True
    state = _seed(config)
    state.record_version(CF, 100)
    save_state(config.state_file, state)
    monkeypatch.setattr(api, "check_updates", lambda ids: {CF: 100})
    downloads: list[str] = []
    monkeypatch.setattr(api, "_run_mod_update", lambda c, j, i: downloads.append(i))

    api._auto_check(lambda: config, api.JobRegistry(), _Clock(passes=1))  # type: ignore[arg-type]

    assert downloads == []


def test_it_skips_a_pass_while_an_update_is_running(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 20-minute download must not have a check land on top of it."""
    _seed(config)
    calls: list[list[int]] = []
    monkeypatch.setattr(api, "check_updates", _recording(calls))
    jobs = api.JobRegistry()
    jobs.create("mods.update")

    api._auto_check(lambda: config, jobs, _Clock(passes=1))  # type: ignore[arg-type]

    assert calls == []


def test_a_finished_update_does_not_block_the_next_pass(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(config)
    calls: list[list[int]] = []
    monkeypatch.setattr(api, "check_updates", _recording(calls))
    jobs = api.JobRegistry()
    job = jobs.create("mods.update")
    jobs.finish(job.id, ok=True, detail="done")

    api._auto_check(lambda: config, jobs, _Clock(passes=1))  # type: ignore[arg-type]

    assert len(calls) == 1


# --- the interval setting ----------------------------------------------------


def _write(tmp_path: Path, extra: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f"root = '{tmp_path.as_posix()}'\nallow_long_root = true\n{extra}\n"
        f"[steam]\nusername = 'x'\n"
        f"steamcmd_path = '{tmp_path.as_posix()}/steamcmd/steamcmd.exe'\n",
        encoding="utf-8",
    )
    return path


def test_the_default_is_thirty_minutes(tmp_path: Path) -> None:
    assert load_config(_write(tmp_path, "")).mod_check_interval_minutes == 30


def test_zero_turns_it_off(tmp_path: Path) -> None:
    """0, not null: TOML has no null, so a nullable field could not express "off" at all
    once the default is 30 - omitting the key would just give you 30 back."""
    loaded = load_config(_write(tmp_path, "mod_check_interval_minutes = 0"))

    assert loaded.mod_check_interval_minutes == 0


def test_a_negative_interval_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Use 0 to turn"):
        load_config(_write(tmp_path, "mod_check_interval_minutes = -5"))


def test_the_registry_reports_a_running_job(config: Config) -> None:
    jobs = api.JobRegistry()
    jobs.create("mods.update")

    assert jobs.is_running("mods.update")
    assert not jobs.is_running("mods.check")


def test_a_real_event_satisfies_the_loops_contract() -> None:
    """The loop is driven by a fake clock, so this checks the real thing agrees."""
    stop = threading.Event()
    stop.set()

    assert stop.wait(0.01) is True


# --- search paging -----------------------------------------------------------


@pytest.fixture
def search_client(config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    path = tmp_path / "config.toml"
    path.write_text(
        f"root = '{tmp_path.as_posix()}'\nallow_long_root = true\n[steam]\n"
        f"username = 'x'\nsteamcmd_path = '{tmp_path.as_posix()}/steamcmd/steamcmd.exe'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DZSM_STEAM_API_KEY", "key")
    return TestClient(api.create_app(path))


def _fake_search(total: int) -> Callable[..., tuple[list[WorkshopItem], int]]:
    def search(
        api_key: str | None, text: str, *, page: int = 1, per_page: int = 20, **kw: object
    ) -> tuple[list[WorkshopItem], int]:
        start = (page - 1) * per_page
        count = max(0, min(per_page, total - start))
        items = [
            WorkshopItem(
                workshop_id=1000 + start + i,
                title=f"mod {start + i}",
                description="",
                preview_url=None,
                time_updated=0,
                subscriptions=0,
                file_size=0,
            )
            for i in range(count)
        ]
        return items, total

    return search


def test_a_second_page_returns_different_items(
    search_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "search_workshop", _fake_search(60))

    first = search_client.get("/api/workshop/search?q=trader&page=1").json()
    second = search_client.get("/api/workshop/search?q=trader&page=2").json()

    assert [i["workshop_id"] for i in first["items"]] != [
        i["workshop_id"] for i in second["items"]
    ]


def test_it_reports_how_many_pages_there_are(
    search_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser needs the count to offer page numbers it has not fetched."""
    monkeypatch.setattr(api, "search_workshop", _fake_search(45))

    body = search_client.get("/api/workshop/search?q=trader").json()

    assert body["pages"] == 3  # 45 matches at 20 a page, rounded up
    assert body["per_page"] == 20


def test_paging_is_capped(search_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Steam stops answering long before its own total runs out, so offering a page per
    match would produce empty pages."""
    monkeypatch.setattr(api, "search_workshop", _fake_search(100_000))

    assert search_client.get("/api/workshop/search?q=a").json()["pages"] == api.MAX_SEARCH_PAGES


def test_one_page_of_results_reports_one_page(
    search_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "search_workshop", _fake_search(3))

    assert search_client.get("/api/workshop/search?q=a").json()["pages"] == 1


def test_no_results_still_reports_one_page(
    search_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0 pages would render a pager for an empty list."""
    monkeypatch.setattr(api, "search_workshop", _fake_search(0))

    assert search_client.get("/api/workshop/search?q=a").json()["pages"] == 1


def test_page_zero_is_refused(search_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "search_workshop", _fake_search(60))

    assert search_client.get("/api/workshop/search?q=a&page=0").status_code == 422


def test_an_oversized_page_is_clamped_not_refused(
    search_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "search_workshop", _fake_search(500))

    body = search_client.get("/api/workshop/search?q=a&per_page=5000").json()

    assert body["per_page"] == api.MAX_SEARCH_PER_PAGE


def test_last_checked_survives_a_round_trip(config: Config) -> None:
    when = datetime.now(UTC) - timedelta(minutes=5)
    save_state(config.state_file, State(last_checked_at=when))

    assert load_state(config.state_file).last_checked_at == when
