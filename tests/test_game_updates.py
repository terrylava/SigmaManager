"""Detecting a DayZ game update, and restarting onto it after a grace period.

This is the half of updating that mods never needed. A mod going stale costs nothing until
someone restarts; the game itself updating makes a server refuse every client immediately,
because clients update themselves through Steam. So detection has to be automatic, and the
restart that applies it has to be prompt without being rude.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dzsm import api, runner
from dzsm.config import Config, ConfigError, InstanceConfig, load_config
from dzsm.state import ProcessInfo, State, save_state
from dzsm.steam import SteamError, _branch_buildid

# Shape of steamcmd's `app_info_print` output. `branches` sits inside `depots`, and the
# word "public" also appears earlier as a manifest key — both are why the parser walks to
# the branches block rather than regexing the whole thing for a buildid.
APP_INFO = """
"223350"
{
\t"common"
\t{
\t\t"name"\t\t"DayZ Server"
\t\t"buildid"\t\t"1"
\t}
\t"depots"
\t{
\t\t"223351"
\t\t{
\t\t\t"manifests"
\t\t\t{
\t\t\t\t"public"
\t\t\t\t{
\t\t\t\t\t"gid"\t\t"7712345678"
\t\t\t\t\t"size"\t\t"4000000000"
\t\t\t\t}
\t\t\t}
\t\t}
\t\t"branches"
\t\t{
\t\t\t"public"
\t\t\t{
\t\t\t\t"buildid"\t\t"19138366"
\t\t\t\t"timeupdated"\t\t"1760000000"
\t\t\t}
\t\t\t"experimental"
\t\t\t{
\t\t\t\t"buildid"\t\t"19200111"
\t\t\t\t"timeupdated"\t\t"1760500000"
\t\t\t}
\t\t}
\t}
}
"""


# --- parsing app info --------------------------------------------------------


def test_the_public_branch_build_is_read() -> None:
    assert _branch_buildid(APP_INFO, "public") == "19138366"


def test_another_branch_is_read() -> None:
    assert _branch_buildid(APP_INFO, "experimental") == "19200111"


def test_it_does_not_pick_up_the_apps_own_buildid() -> None:
    """`common` carries a buildid too, and a depot manifest key is literally "public".
    A blind regex returns something plausible and wrong."""
    assert _branch_buildid(APP_INFO, "public") != "1"


def test_it_parses_what_steam_actually_printed() -> None:
    """Captured verbatim from `app_info_print 223350` on 2026-10-16, because this is
    substring matching on a format only Valve controls. Note the real experimental branch
    is named experimental_public, and a scalar key follows the branches block.
    """
    real = (
        '"branches"\n\t\t{\n\t\t\t"public"\n\t\t\t{\n'
        '\t\t\t\t"buildid"\t\t"25460313"\n'
        '\t\t\t\t"timeupdated"\t\t"1792059024"\n'
        '\t\t\t\t"timebuildupdated"\t\t"1790084512"\n\t\t\t}\n'
        '\t\t\t"experimental_public"\n\t\t\t{\n'
        '\t\t\t\t"buildid"\t\t"3889697"\n'
        '\t\t\t\t"description"\t\t"Unstable testing version"\n\t\t\t}\n'
        '\t\t}\n\t\t"privatebranches"\t\t"1"\n'
    )

    assert _branch_buildid(real, "public") == "25460313"
    assert _branch_buildid(real, "experimental_public") == "3889697"


def test_a_missing_branch_is_not_guessed() -> None:
    assert _branch_buildid(APP_INFO, "nosuchbranch") is None


def test_output_without_branches_is_not_guessed() -> None:
    assert _branch_buildid('"223350" { "common" { "buildid" "5" } }', "public") is None


def test_a_branch_without_a_buildid_does_not_borrow_the_next_ones() -> None:
    """Bounded to the branch's own block, so an empty branch returns None rather than
    reporting the following branch's build as its own."""
    info = '"branches" {\n"public"\n{\n"description" "x"\n}\n"beta"\n{\n"buildid" "99"\n}\n}'

    assert _branch_buildid(info, "public") is None


# --- when an instance is due to restart onto a new build ---------------------


@pytest.fixture
def cfg(config: Config) -> Config:
    config.instances = {"chernarus": InstanceConfig(port=2302, mods=[])}
    return config


def _state(*, installed: str, running: str, promoted_minutes_ago: float = 0.0) -> State:
    state = State()
    state.serverapp_version = installed
    state.serverapp_promoted_at = datetime.now(UTC) - timedelta(minutes=promoted_minutes_ago)
    state.server_builds["chernarus"] = running
    state.processes["chernarus"] = ProcessInfo(
        pid=1, port=2302, started_at=datetime.now(UTC), create_time=None
    )
    return state


def test_a_newer_build_schedules_a_restart_after_the_grace(cfg: Config) -> None:
    state = _state(installed="200", running="100")

    due = runner.build_restart_due(cfg, state, "chernarus")

    assert due is not None
    # Default grace is 10 minutes, measured from when the build was promoted.
    assert timedelta(minutes=9) < (due - datetime.now()) < timedelta(minutes=11)


def test_the_grace_counts_from_the_build_not_from_now(cfg: Config) -> None:
    """Otherwise a daemon restart would reset the countdown and the restart would keep
    being pushed out forever."""
    state = _state(installed="200", running="100", promoted_minutes_ago=8)

    due = runner.build_restart_due(cfg, state, "chernarus")

    assert due is not None
    assert (due - datetime.now()) < timedelta(minutes=3)


def test_an_instance_already_on_the_build_is_not_due(cfg: Config) -> None:
    """Self-clearing: once it restarts, server_builds matches and this stops firing. A
    stored deadline would need clearing at exactly the right moment, and a missed clear
    is a restart loop."""
    state = _state(installed="200", running="200")

    assert runner.build_restart_due(cfg, state, "chernarus") is None


def test_a_pinned_instance_is_never_moved(cfg: Config) -> None:
    """A server_build pin is a deliberate statement about which build to run. A grace
    period is not permission to override it."""
    cfg.instances["chernarus"] = InstanceConfig(port=2302, mods=[], server_build="100")
    state = _state(installed="200", running="100")

    assert runner.build_restart_due(cfg, state, "chernarus") is None


def test_a_zero_grace_leaves_it_to_the_schedule(cfg: Config) -> None:
    cfg.server_update_grace_minutes = 0
    state = _state(installed="200", running="100")

    assert runner.build_restart_due(cfg, state, "chernarus") is None


def test_an_instance_that_never_started_is_not_due(cfg: Config) -> None:
    """There is nothing to move it off of; it picks the newest up when it does start."""
    state = _state(installed="200", running="100")
    del state.server_builds["chernarus"]

    assert runner.build_restart_due(cfg, state, "chernarus") is None


def test_no_promotion_timestamp_means_no_deadline(cfg: Config) -> None:
    """An older state file predates the field. Inventing a deadline from it would restart
    every instance the moment the daemon came up."""
    state = _state(installed="200", running="100")
    state.serverapp_promoted_at = None

    assert runner.build_restart_due(cfg, state, "chernarus") is None


# --- how it combines with the ordinary schedule ------------------------------


def test_the_sooner_deadline_wins_and_is_flagged_as_an_update(cfg: Config) -> None:
    cfg.instances["chernarus"] = InstanceConfig(port=2302, mods=[], restart_after_hours=4.0)
    state = _state(installed="200", running="100")

    due = runner._due_at(cfg, state, "chernarus")

    assert due.update is True
    assert due.at is not None
    assert (due.at - datetime.now()) < timedelta(minutes=11)


def test_a_nearer_scheduled_restart_wins_and_is_not_flagged(cfg: Config) -> None:
    """No point announcing a game update for a restart that was happening anyway."""
    cfg.instances["chernarus"] = InstanceConfig(
        port=2302, mods=[], restart_after_hours=0.05  # 3 minutes
    )
    state = _state(installed="200", running="100")

    due = runner._due_at(cfg, state, "chernarus")

    assert due.update is False


def test_with_no_update_pending_it_is_the_plain_schedule(cfg: Config) -> None:
    cfg.instances["chernarus"] = InstanceConfig(port=2302, mods=[], restart_after_hours=4.0)
    state = _state(installed="200", running="200")

    assert runner._due_at(cfg, state, "chernarus").update is False


# --- what players are told ---------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.said: list[str] = []

    def say(self, message: str) -> None:
        self.said.append(message)

    def close(self) -> None:
        pass


def test_an_update_restart_says_so_in_chat(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Players whose client just updated and cannot connect are owed the reason. "Server
    restart" reads like the routine one they could have waited out."""
    recorder = _Recorder()
    monkeypatch.setattr(runner, "rcon_client", lambda c, n: recorder)
    monkeypatch.setattr(runner, "rcon_grace_remaining", lambda c, s, n: 0.0)

    runner._warn_players(cfg, State(), "chernarus", {}, remaining=120.0, update=True)

    assert recorder.said == ["Game update - server restarting in 2 minutes"]


def test_a_routine_restart_keeps_the_ordinary_wording(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(runner, "rcon_client", lambda c, n: recorder)
    monkeypatch.setattr(runner, "rcon_grace_remaining", lambda c, s, n: 0.0)

    runner._warn_players(cfg, State(), "chernarus", {}, remaining=120.0)

    assert recorder.said == ["Server restart in 2 minutes"]


# --- the setting -------------------------------------------------------------


def _write(tmp_path: Path, extra: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f"root = '{tmp_path.as_posix()}'\nallow_long_root = true\n{extra}\n"
        f"[steam]\nusername = 'x'\n"
        f"steamcmd_path = '{tmp_path.as_posix()}/steamcmd/steamcmd.exe'\n",
        encoding="utf-8",
    )
    return path


def test_the_default_grace_is_ten_minutes(tmp_path: Path) -> None:
    assert load_config(_write(tmp_path, "")).server_update_grace_minutes == 10


def test_game_update_checking_is_on_by_default(tmp_path: Path) -> None:
    """It is the update most likely to be missed and the cheapest to check for."""
    assert load_config(_write(tmp_path, "")).check_server_build is True


def test_downloading_a_game_update_is_off_by_default(tmp_path: Path) -> None:
    """Several GB, unlike a mod check."""
    assert load_config(_write(tmp_path, "")).server_update_auto_download is False


def test_a_negative_grace_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Use 0 to leave"):
        load_config(_write(tmp_path, "server_update_grace_minutes = -1"))


# --- the API surface ---------------------------------------------------------


API_CONFIG = """\
root = '{root}'
allow_long_root = true

[steam]
username = "someone"
steamcmd_path = '{root}/steamcmd/steamcmd.exe'

[instances.chernarus]
port = 2302
mods = []

[instances.pinned]
port = 2402
mods = []
server_build = "100"
"""


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    path = tmp_path / "config.toml"
    path.write_text(API_CONFIG.format(root=tmp_path.as_posix()), encoding="utf-8")
    state = State()
    state.serverapp_version = "100"
    state.latest_remote_server_build = "200"
    state.server_build_checked_at = datetime.now(UTC)
    save_state(tmp_path / "state.json", state)
    return TestClient(api.create_app(path))


def test_the_api_reports_an_available_game_update(client: TestClient) -> None:
    body = client.get("/api/updates").json()["server"]

    assert body["update_available"] is True
    assert body["latest"] == "200"
    assert body["installed"] == "100"


def test_the_api_reports_a_pin_left_behind(client: TestClient) -> None:
    """Nothing else would ever say so: the pin is honoured, and the instance quietly
    stops accepting updated clients."""
    assert client.get("/api/updates").json()["server"]["pinned_behind"] == ["pinned"]


def test_the_api_reports_the_grace_window(client: TestClient) -> None:
    body = client.get("/api/updates").json()["server"]

    assert body["grace_minutes"] == 10
    assert body["checking"] is True


def test_promoting_the_same_build_does_not_restart_the_clock(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running an update would otherwise keep pushing the grace out, so instances
    would never actually restart onto the build."""
    state = State()
    state.serverapp_version = "200"
    earlier = datetime.now(UTC) - timedelta(minutes=30)
    state.serverapp_promoted_at = earlier
    save_state(config.state_file, state)

    monkeypatch.setattr(api, "update_server_app", lambda c, validate=False: "200")
    (config.serverapp_dir / "200").mkdir(parents=True)

    api._run_server_update(config, api.JobRegistry(), "job", False)

    from dzsm.state import load_state

    assert load_state(config.state_file).serverapp_promoted_at == earlier


def test_a_failed_build_check_does_not_stop_the_daemon(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It runs unattended, and steamcmd is the flakiest thing here (Rule 3)."""
    save_state(config.state_file, State())

    def explode(cfg: Config) -> str:
        raise SteamError("steamcmd is unhappy")

    monkeypatch.setattr(api, "remote_server_build", explode)
    jobs = api.JobRegistry()

    api._check_server_build(config, jobs)

    assert [j.status for j in jobs.all()] == ["failed"]
