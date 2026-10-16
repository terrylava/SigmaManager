"""Player sessions from the admin log, and the live list from RCON.

The admin log is the interesting half. Its format is Bohemia's, it carries a date only in
its header, and it stops without warning when a server crashes — so the parsing is where
every edge case lives, and it is tested against text captured from a real server rather
than against something invented here.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from dzsm.config import Config, InstanceConfig
from dzsm.players import (
    PlayerStoreError,
    Session,
    connect,
    ingest,
    parse_admin_log,
    recent_sessions,
    totals,
)
from dzsm.rcon import parse_players

# Captured verbatim from D:\sigmamanagertest\instances\chernarus\profiles on 2026-10-16,
# the ADM file from a real client session. Note the id is DayZ's own hash, not a Steam id,
# and that "is connecting" precedes "is connected" by half a minute.
REAL_LOG = """\


******************************************************************************
AdminLog started on 2026-07-25 at 07:46:53
07:48:08 | Player "Terrylava" (id=oXcgjFgN5G1X3b6DiZVY24sSO0Qem5-bkegq8Mj1Cv0=) is connecting
07:48:36 | Player "Terrylava" (id=oXcgjFgN5G1X3b6DiZVY24sSO0Qem5-bkegq8Mj1Cv0= \
pos=<15112.1, 13885.2, 18.8>) is connected
07:54:48 | Player "Terrylava" (id=oXcgjFgN5G1X3b6DiZVY24sSO0Qem5-bkegq8Mj1Cv0= \
pos=<15197.5, 13783.9, 1.5>)[HP: 100] hit by Infected into RightArm(17) for 0 damage
08:03:30 | Player "Terrylava" (id=oXcgjFgN5G1X3b6DiZVY24sSO0Qem5-bkegq8Mj1Cv0= \
pos=<15192.6, 13766.4, 4.7>) has been disconnected
"""

PLAYER_ID = "oXcgjFgN5G1X3b6DiZVY24sSO0Qem5-bkegq8Mj1Cv0="


def _parse(text: str) -> list[Session]:
    return parse_admin_log(text, instance="chernarus", source="test.ADM")


# --- parsing a real log ------------------------------------------------------


def test_a_real_session_is_recovered() -> None:
    sessions = _parse(REAL_LOG)

    assert len(sessions) == 1
    assert sessions[0].name == "Terrylava"
    assert sessions[0].player_id == PLAYER_ID


def test_the_session_starts_at_connected_not_connecting() -> None:
    """"is connecting" may never complete — it is an attempt, not a session."""
    assert _parse(REAL_LOG)[0].started_at == datetime(2026, 7, 25, 7, 48, 36)


def test_the_session_ends_at_the_disconnect() -> None:
    session = _parse(REAL_LOG)[0]

    assert session.ended_at == datetime(2026, 7, 25, 8, 3, 30)
    assert session.closed_by == "disconnect"
    assert 890 < session.seconds < 900  # ~14.9 minutes


def test_the_date_comes_from_the_header() -> None:
    """Event lines carry a bare time. Without the header there is no date at all."""
    assert _parse(REAL_LOG)[0].started_at.date() == datetime(2026, 7, 25).date()


def test_an_empty_log_yields_nothing() -> None:
    """Most .ADM files are a header and nothing else — a server nobody joined."""
    empty = "\n\nAdminLog started on 2026-07-25 at 06:05:05\n"

    assert _parse(empty) == []


def test_a_log_without_a_header_is_skipped_not_guessed() -> None:
    """Guessing a date would file sessions at an arbitrary point in history."""
    assert _parse('07:48:36 | Player "X" (id=abc) is connected\n') == []


def test_event_lines_are_ignored() -> None:
    """Hits, emotes and deaths all mention a player; only connect and disconnect bound a
    session."""
    assert len(_parse(REAL_LOG)) == 1


# --- the awkward cases -------------------------------------------------------


def test_a_log_that_stops_without_a_disconnect_is_closed_at_its_end() -> None:
    """A crash or a restart ends the file mid-session. Leaving it open would hand that
    player unbounded hours the moment anything asked for a duration."""
    text = (
        "AdminLog started on 2026-07-25 at 07:00:00\n"
        '07:10:00 | Player "A" (id=aaa) is connected\n'
        '07:40:00 | Player "A" (id=aaa pos=<1,2,3>) performed something\n'
    )

    session = _parse(text)[0]

    assert session.ended_at == datetime(2026, 7, 25, 7, 40, 0)
    assert session.closed_by == "log_end"


def test_a_log_crossing_midnight_advances_the_date() -> None:
    """The clock going backwards is the only evidence the date moved — the lines never
    say."""
    text = (
        "AdminLog started on 2026-07-25 at 23:50:00\n"
        '23:55:00 | Player "A" (id=aaa) is connected\n'
        '00:10:00 | Player "A" (id=aaa) has been disconnected\n'
    )

    session = _parse(text)[0]

    assert session.started_at == datetime(2026, 7, 25, 23, 55, 0)
    assert session.ended_at == datetime(2026, 7, 26, 0, 10, 0)
    assert session.seconds == 900


def test_a_disconnect_with_no_connect_is_dropped() -> None:
    """The session began in a log dzsm does not have. There is no start time to
    attribute it to, so inventing one would be worse than losing it."""
    text = (
        "AdminLog started on 2026-07-25 at 07:00:00\n"
        '07:10:00 | Player "A" (id=aaa) has been disconnected\n'
    )

    assert _parse(text) == []


def test_two_connects_without_a_disconnect_close_the_first() -> None:
    text = (
        "AdminLog started on 2026-07-25 at 07:00:00\n"
        '07:10:00 | Player "A" (id=aaa) is connected\n'
        '07:20:00 | Player "A" (id=aaa) is connected\n'
        '07:30:00 | Player "A" (id=aaa) has been disconnected\n'
    )

    sessions = sorted(_parse(text), key=lambda s: s.started_at)

    assert len(sessions) == 2
    assert sessions[0].closed_by == "reconnect"
    assert sessions[0].ended_at == datetime(2026, 7, 25, 7, 20, 0)


def test_two_players_are_tracked_independently() -> None:
    text = (
        "AdminLog started on 2026-07-25 at 07:00:00\n"
        '07:10:00 | Player "A" (id=aaa) is connected\n'
        '07:11:00 | Player "B" (id=bbb) is connected\n'
        '07:20:00 | Player "A" (id=aaa) has been disconnected\n'
        '07:40:00 | Player "B" (id=bbb) has been disconnected\n'
    )

    by_id = {s.player_id: s for s in _parse(text)}

    assert by_id["aaa"].seconds == 600
    assert by_id["bbb"].seconds == 1740


def test_a_name_containing_a_quote_does_not_truncate() -> None:
    """The name is matched up to the last `" (id=` on the line, not the first quote."""
    text = (
        "AdminLog started on 2026-07-25 at 07:00:00\n"
        '07:10:00 | Player "He said "hi"" (id=aaa) is connected\n'
        '07:20:00 | Player "He said "hi"" (id=aaa) has been disconnected\n'
    )

    assert _parse(text)[0].name == 'He said "hi"'


# --- the database ------------------------------------------------------------


@pytest.fixture
def cfg(config: Config) -> Config:
    config.instances = {"chernarus": InstanceConfig(port=2302, mods=[])}
    return config


def _write_log(cfg: Config, name: str, text: str) -> Path:
    profiles = cfg.instance_dir("chernarus") / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    path = profiles / name
    path.write_text(text, encoding="utf-8")
    return path


def test_ingest_records_a_session(cfg: Config) -> None:
    _write_log(cfg, "DayZServer_x64_2026-07-25_07-46-53.ADM", REAL_LOG)

    assert ingest(cfg, "chernarus") == 1
    assert len(recent_sessions(cfg)) == 1


def test_ingest_skips_an_unchanged_file(cfg: Config) -> None:
    _write_log(cfg, "DayZServer_x64_2026-07-25_07-46-53.ADM", REAL_LOG)
    ingest(cfg, "chernarus")

    assert ingest(cfg, "chernarus") == 0


def test_re_reading_a_grown_file_does_not_duplicate(cfg: Config) -> None:
    """The open log grows while anyone is connected, so it is re-read repeatedly. Rows
    upsert on (instance, player_id, started_at) rather than tracking byte offsets."""
    path = _write_log(cfg, "DayZServer_x64_2026-07-25_07-46-53.ADM", REAL_LOG)
    ingest(cfg, "chernarus")
    path.write_text(
        REAL_LOG + '08:10:00 | Player "Other" (id=bbb) is connected\n', encoding="utf-8"
    )

    ingest(cfg, "chernarus")

    assert len(recent_sessions(cfg)) == 2


def test_a_running_sessions_end_time_advances(cfg: Config) -> None:
    """Until a real disconnect replaces it, an open session is capped at the end of the
    log — which moves as the log grows."""
    head = (
        "AdminLog started on 2026-07-25 at 07:00:00\n"
        '07:10:00 | Player "A" (id=aaa) is connected\n'
    )
    path = _write_log(cfg, "DayZServer_x64_2026-07-25_07-00-00.ADM", head)
    ingest(cfg, "chernarus")
    first = recent_sessions(cfg)[0].ended_at

    path.write_text(head + '07:50:00 | Player "A" (id=aaa) did something\n', encoding="utf-8")
    ingest(cfg, "chernarus")

    assert recent_sessions(cfg)[0].ended_at > first


def test_a_later_disconnect_replaces_the_log_end_guess(cfg: Config) -> None:
    head = (
        "AdminLog started on 2026-07-25 at 07:00:00\n"
        '07:10:00 | Player "A" (id=aaa) is connected\n'
    )
    path = _write_log(cfg, "DayZServer_x64_2026-07-25_07-00-00.ADM", head)
    ingest(cfg, "chernarus")
    assert recent_sessions(cfg)[0].closed_by == "log_end"

    path.write_text(
        head + '07:30:00 | Player "A" (id=aaa) has been disconnected\n', encoding="utf-8"
    )
    ingest(cfg, "chernarus")

    assert recent_sessions(cfg)[0].closed_by == "disconnect"


def test_no_profiles_directory_is_not_an_error(cfg: Config) -> None:
    """A server that has never started has no logs, which is a normal state."""
    assert ingest(cfg, "chernarus") == 0


def test_totals_accumulate_across_sessions(cfg: Config) -> None:
    _write_log(
        cfg,
        "DayZServer_x64_2026-07-25_07-00-00.ADM",
        "AdminLog started on 2026-07-25 at 07:00:00\n"
        '07:00:00 | Player "A" (id=aaa) is connected\n'
        '08:00:00 | Player "A" (id=aaa) has been disconnected\n',
    )
    _write_log(
        cfg,
        "DayZServer_x64_2026-07-26_07-00-00.ADM",
        "AdminLog started on 2026-07-26 at 07:00:00\n"
        '07:00:00 | Player "A" (id=aaa) is connected\n'
        '09:00:00 | Player "A" (id=aaa) has been disconnected\n',
    )
    ingest(cfg, "chernarus")

    ranking = totals(cfg)

    assert len(ranking) == 1
    assert ranking[0].seconds == 3 * 3600
    assert ranking[0].sessions == 2


def test_the_ranking_is_longest_first(cfg: Config) -> None:
    _write_log(
        cfg,
        "DayZServer_x64_2026-07-25_07-00-00.ADM",
        "AdminLog started on 2026-07-25 at 07:00:00\n"
        '07:00:00 | Player "Short" (id=aaa) is connected\n'
        '07:10:00 | Player "Short" (id=aaa) has been disconnected\n'
        '07:00:00 | Player "Long" (id=bbb) is connected\n'
        '09:00:00 | Player "Long" (id=bbb) has been disconnected\n',
    )
    ingest(cfg, "chernarus")

    assert [entry.name for entry in totals(cfg)] == ["Long", "Short"]


def test_the_latest_name_wins(cfg: Config) -> None:
    """Names change; the id is what identifies someone."""
    _write_log(
        cfg,
        "DayZServer_x64_2026-07-25_07-00-00.ADM",
        "AdminLog started on 2026-07-25 at 07:00:00\n"
        '07:00:00 | Player "OldName" (id=aaa) is connected\n'
        '08:00:00 | Player "OldName" (id=aaa) has been disconnected\n',
    )
    _write_log(
        cfg,
        "DayZServer_x64_2026-07-26_07-00-00.ADM",
        "AdminLog started on 2026-07-26 at 07:00:00\n"
        '07:00:00 | Player "NewName" (id=aaa) is connected\n'
        '08:00:00 | Player "NewName" (id=aaa) has been disconnected\n',
    )
    ingest(cfg, "chernarus")

    assert totals(cfg)[0].name == "NewName"


def test_an_unwritable_database_fails_loudly(config: Config) -> None:
    """Rule 3: a broken store must say so rather than silently reporting no sessions."""
    config.root.joinpath("players.db").mkdir(parents=True, exist_ok=True)

    with pytest.raises(PlayerStoreError):
        with connect(config) as connection:
            connection.execute("SELECT 1")


# --- the live list from RCON -------------------------------------------------

# BattlEye's `players` table.
PLAYERS_OUTPUT = """Players on server:
[#] [IP Address]:[Port] [Ping] [GUID] [Name]
--------------------------------------------------
0   192.168.1.50:2304     31   d41d8cd98f00b204e9800998ecf8427e(OK) Terrylava
1   10.0.0.7:2304         120  098f6bcd4621d373cade4e832627b4f6(?) Someone Else
2   10.0.0.9:2304         45   ad0234829205b9033196ba818f7a872b(OK) Loading Guy (Lobby)
(3 players in total)
"""


def test_the_live_list_is_parsed() -> None:
    slots = parse_players(PLAYERS_OUTPUT)

    assert [s.name for s in slots] == ["Terrylava", "Someone Else", "Loading Guy"]


def test_names_with_spaces_survive() -> None:
    assert parse_players(PLAYERS_OUTPUT)[1].name == "Someone Else"


def test_ping_and_address_are_read() -> None:
    slot = parse_players(PLAYERS_OUTPUT)[0]

    assert slot.ping == 31
    assert slot.ip == "192.168.1.50"
    assert slot.port == 2304


def test_an_unverified_guid_is_flagged() -> None:
    """BattlEye marks a GUID it has not yet verified with (?), and that matters for a ban
    check — an unverified player is not confirmed to be who the GUID says."""
    assert parse_players(PLAYERS_OUTPUT)[1].verified is False
    assert parse_players(PLAYERS_OUTPUT)[0].verified is True


def test_a_player_still_loading_is_flagged_and_unsuffixed() -> None:
    slot = parse_players(PLAYERS_OUTPUT)[2]

    assert slot.lobby is True
    assert slot.name == "Loading Guy"


def test_an_empty_server_parses_to_an_empty_list() -> None:
    """Empty, not None (Rule 1) — and the header and footer must not become rows."""
    empty = (
        "Players on server:\n"
        "[#] [IP Address]:[Port] [Ping] [GUID] [Name]\n"
        "--------------------------------------------------\n"
        "(0 players in total)\n"
    )

    assert parse_players(empty) == []


def test_unexpected_output_costs_rows_not_an_exception() -> None:
    """A format change should degrade the live view, not break the page that shows it."""
    assert parse_players("something else entirely") == []
