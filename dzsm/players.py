"""Player sessions, from the admin log (`-adminlog`).

Two sources of player data exist and they answer different questions, so they are kept
apart rather than merged:

- **RCON `players`** (dzsm/rcon.py) is the live view: who is on now, their ping, their
  BattlEye GUID. It needs the server running and RCON reachable.
- **The admin log** is the durable record. DayZ writes one `.ADM` per server run into the
  instance's profiles directory, with a connect and a disconnect line per session. That
  is enough to reconstruct *exact* concurrency at any past moment — strictly better than
  polling RCON on a timer, which samples and misses anything shorter than its interval —
  and it works on logs written before dzsm ever ran.

They cannot be joined per player. BattlEye's GUID and the admin log's `id=` are different
hashes of different things, and neither is reversible to a Steam id. So this module keys
everything on the admin log's id and never pretends a live slot and a stored session are
the same player record.

Storage is SQLite rather than state.json: that file is rewritten whole on every save,
which is the wrong shape entirely for an append-heavy ledger that will hold a row per
session forever.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from dzsm.config import Config

_log = logging.getLogger(__name__)

# Timestamps are stored exactly as the log states them: naive local time. The log records
# a date once in its header and a bare time on every line, so it asserts local wall clock
# and nothing else. Converting to UTC would require assuming the machine's zone never
# changed between the log being written and being read, which is an assumption this has no
# way to check and no need to make.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

_HEADER = re.compile(r"AdminLog started on (\d{4}-\d{2}-\d{2}) at (\d{2}:\d{2}:\d{2})")
_LINE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\s*\|\s*(?P<rest>.*)$")

# Player "Name" (id=HASH pos=<...>) is connected
#
# The name is matched greedily up to the last `" (id=` on the line, so a name containing a
# quote does not truncate it. The position block is optional: "is connecting" carries no
# pos, and only some event lines do.
_CONNECTED = re.compile(r'^Player "(?P<name>.*)" \(id=(?P<id>[^ )]+)(?: [^)]*)?\) is connected')
_DISCONNECTED = re.compile(
    r'^Player "(?P<name>.*)" \(id=(?P<id>[^ )]+)(?: [^)]*)?\) has been disconnected'
)


class PlayerStoreError(Exception):
    """The session database could not be opened or queried."""


@dataclass(frozen=True)
class Session:
    """One player's time on one server.

    `ended_at` is never None by the time a session is stored: a log that stops without a
    disconnect line — a crash, or a restart — is closed at the last timestamp the file
    contains. Leaving it open would hand that player unbounded hours the moment anything
    queried a duration, which is the obvious way for a leaderboard to become nonsense.
    """

    instance: str
    player_id: str
    name: str
    started_at: datetime
    ended_at: datetime
    # "disconnect" when the log said so, "log_end" when the file simply stopped.
    closed_by: str
    source: str

    @property
    def seconds(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    instance        TEXT NOT NULL,
    player_id       TEXT NOT NULL,
    name            TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    ended_at        TEXT NOT NULL,
    closed_by       TEXT NOT NULL,
    source          TEXT NOT NULL,
    PRIMARY KEY (instance, player_id, started_at)
);
CREATE INDEX IF NOT EXISTS sessions_by_time ON sessions (instance, started_at);
CREATE INDEX IF NOT EXISTS sessions_by_player ON sessions (player_id);

-- Which log files have been read, and how big they were at the time. A growing file is
-- simply re-read in full and its rows re-upserted; see ingest().
CREATE TABLE IF NOT EXISTS ingested (
    path  TEXT PRIMARY KEY,
    size  INTEGER NOT NULL
);

-- Messages BattlEye pushed to a held RCON connection. Chat above all: no command returns
-- chat history, so a line not written here when it arrives is gone permanently. The live
-- feed is a ring buffer in memory; this is the part that survives a restart.
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    instance  TEXT NOT NULL,
    at        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    name      TEXT,
    channel   TEXT,
    text      TEXT,
    raw       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_time ON events (instance, id DESC);
"""


@contextmanager
def connect(config: Config) -> Iterator[sqlite3.Connection]:
    """Open the session database, creating it and its schema if absent."""
    path = config.players_db
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=10.0)
    except (OSError, sqlite3.Error) as exc:
        raise PlayerStoreError(f"cannot open the player database at {path}: {exc}") from exc

    try:
        with closing(connection):
            connection.row_factory = sqlite3.Row
            connection.executescript(SCHEMA)
            yield connection
            connection.commit()
    except sqlite3.Error as exc:
        raise PlayerStoreError(f"player database error: {exc}") from exc


def parse_admin_log(text: str, *, instance: str, source: str) -> list[Session]:
    """Turn one .ADM file into sessions.

    Pure text in, sessions out — no filesystem, no database — because the parsing is the
    part with all the edge cases and it should be testable against a captured log without
    standing anything up.

    Returns a list, empty for a log nobody connected to (most of them). Never None.
    """
    lines = text.splitlines()
    date = _starting_date(lines)
    if date is None:
        # No header means this is not an admin log, or it was truncated before the line
        # that dates everything after it. Guessing a date would put sessions at an
        # arbitrary point in history.
        _log.warning("players: %s has no AdminLog header, skipping", source)
        return []

    sessions: list[Session] = []
    open_sessions: dict[str, tuple[str, datetime]] = {}
    previous = _at(date, 0, 0, 0)
    last_seen = previous

    for line in lines:
        match = _LINE.match(line)
        if match is None:
            continue
        stamp = _at(date, int(match[1]), int(match[2]), int(match[3]))
        # A log that runs past midnight keeps writing bare times, so the only evidence of
        # the date advancing is the clock going backwards.
        if stamp < previous:
            date += timedelta(days=1)
            stamp = _at(date, int(match[1]), int(match[2]), int(match[3]))
        previous = stamp
        last_seen = stamp

        rest = match["rest"]
        connected = _CONNECTED.match(rest)
        if connected is not None:
            player = connected["id"]
            if player in open_sessions:
                # Two connects with no disconnect between them. Close the first here
                # rather than discarding one, which would lose the time either way.
                name, started = open_sessions.pop(player)
                sessions.append(
                    Session(instance, player, name, started, stamp, "reconnect", source)
                )
            open_sessions[player] = (connected["name"], stamp)
            continue

        disconnected = _DISCONNECTED.match(rest)
        if disconnected is not None:
            player = disconnected["id"]
            opened = open_sessions.pop(player, None)
            if opened is None:
                # A disconnect with no matching connect: the session started in a log
                # dzsm has not got. There is no start time to attribute it to, so it is
                # dropped rather than invented.
                continue
            name, started = opened
            sessions.append(
                Session(instance, player, name, started, stamp, "disconnect", source)
            )

    # Whatever is still open when the file ends did not disconnect cleanly.
    for player, (name, started) in open_sessions.items():
        sessions.append(Session(instance, player, name, started, last_seen, "log_end", source))

    return sessions


def _starting_date(lines: list[str]) -> datetime | None:
    for line in lines:
        match = _HEADER.search(line)
        if match is not None:
            return datetime.strptime(match[1], "%Y-%m-%d")
    return None


def _at(date: datetime, hour: int, minute: int, second: int) -> datetime:
    return date.replace(hour=hour, minute=minute, second=second, microsecond=0)


def admin_logs(config: Config, instance_name: str) -> list[Path]:
    """Every .ADM in an instance's profiles directory, oldest first.

    Sorted by name, which for DayZ's `DayZServer_x64_<date>_<time>.ADM` is chronological
    and does not depend on mtime — a file copied or restored keeps its name but not its
    timestamps.
    """
    profiles = config.instance_dir(instance_name) / "profiles"
    if not profiles.is_dir():
        return []
    return sorted(
        (p for p in profiles.iterdir() if p.is_file() and p.suffix.upper() == ".ADM"),
        key=lambda p: p.name,
    )


def ingest(config: Config, instance_name: str) -> int:
    """Read any new or grown admin logs into the database. Returns sessions written.

    A file is re-read in full whenever its size has changed, and its rows are upserted on
    (instance, player_id, started_at). That makes ingest idempotent without tracking byte
    offsets — offsets are the fragile part of tailing, and re-parsing a few hundred KB of
    text costs nothing next to getting them wrong. It also means the currently-open log is
    picked up repeatedly as it grows, with the running session's end time advancing until
    a real disconnect replaces it.
    """
    written = 0
    with connect(config) as connection:
        known = {
            row["path"]: row["size"]
            for row in connection.execute("SELECT path, size FROM ingested")
        }
        for path in admin_logs(config, instance_name):
            key = str(path)
            try:
                size = path.stat().st_size
            except OSError as exc:
                _log.warning("players: cannot stat %s: %s", path, exc)
                continue
            if known.get(key) == size:
                continue

            try:
                # The server has this file open for writing; reading is fine, but a line
                # may be half-written, so undecodable bytes are replaced rather than fatal.
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                _log.warning("players: cannot read %s: %s", path, exc)
                continue

            sessions = parse_admin_log(text, instance=instance_name, source=path.name)
            for session in sessions:
                connection.execute(
                    "INSERT INTO sessions "
                    "(instance, player_id, name, started_at, ended_at, closed_by, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (instance, player_id, started_at) DO UPDATE SET "
                    "ended_at = excluded.ended_at, closed_by = excluded.closed_by, "
                    "name = excluded.name",
                    (
                        session.instance,
                        session.player_id,
                        session.name,
                        session.started_at.strftime(TIMESTAMP_FORMAT),
                        session.ended_at.strftime(TIMESTAMP_FORMAT),
                        session.closed_by,
                        session.source,
                    ),
                )
            written += len(sessions)
            connection.execute(
                "INSERT INTO ingested (path, size) VALUES (?, ?) "
                "ON CONFLICT (path) DO UPDATE SET size = excluded.size",
                (key, size),
            )
    return written


@dataclass(frozen=True)
class StoredEvent:
    """One pushed RCON message, as recorded."""

    id: int
    instance: str
    at: datetime
    kind: str
    raw: str
    name: str | None
    channel: str | None
    text: str | None


def record_event(
    config: Config,
    instance: str,
    *,
    kind: str,
    raw: str,
    name: str | None = None,
    channel: str | None = None,
    text: str | None = None,
) -> None:
    """Persist one pushed message.

    Takes the fields rather than an rcon.RconEvent so this module does not depend on the
    protocol layer — the ledger should not care how a message reached it.
    """
    with connect(config) as connection:
        connection.execute(
            "INSERT INTO events (instance, at, kind, name, channel, text, raw) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                instance,
                datetime.now().strftime(TIMESTAMP_FORMAT),
                kind,
                name,
                channel,
                text,
                raw,
            ),
        )


def recent_events(
    config: Config,
    *,
    instance: str | None = None,
    kinds: tuple[str, ...] | None = None,
    limit: int = 200,
) -> list[StoredEvent]:
    """The latest stored messages, newest first.

    `kinds` filters to e.g. just chat. Passed as a JSON array and matched with json_each
    rather than building an IN clause, so the statement stays static.
    """
    query = """
        SELECT * FROM events
         WHERE (:instance IS NULL OR instance = :instance)
           AND (:kinds IS NULL OR kind IN (SELECT value FROM json_each(:kinds)))
      ORDER BY id DESC
         LIMIT :limit
    """
    arguments = {
        "instance": instance,
        "kinds": json.dumps(list(kinds)) if kinds else None,
        "limit": limit,
    }
    with connect(config) as connection:
        rows = connection.execute(query, arguments).fetchall()
    return [
        StoredEvent(
            id=row["id"],
            instance=row["instance"],
            at=datetime.strptime(row["at"], TIMESTAMP_FORMAT),
            kind=row["kind"],
            raw=row["raw"],
            name=row["name"],
            channel=row["channel"],
            text=row["text"],
        )
        for row in rows
    ]


@dataclass(frozen=True)
class PlayerTotals:
    """One player's accumulated time, for a ranking."""

    player_id: str
    name: str
    sessions: int
    seconds: float
    last_seen: datetime


def totals(config: Config, *, instance: str | None = None, limit: int = 50) -> list[PlayerTotals]:
    """Accumulated time per player, longest first.

    `name` is the most recent one seen for that id, since names change and the id is what
    actually identifies someone.
    """
    # One static statement with the optional filter expressed as a parameter, rather than
    # concatenating a WHERE clause: nothing here is built from a value, so there is no
    # injection surface to reason about and no lint to suppress.
    query = """
        SELECT player_id,
               SUM(strftime('%s', ended_at) - strftime('%s', started_at)) AS seconds,
               COUNT(*) AS sessions,
               MAX(ended_at) AS last_seen
          FROM sessions
         WHERE (:instance IS NULL OR instance = :instance)
      GROUP BY player_id
      ORDER BY seconds DESC
         LIMIT :limit
    """

    with connect(config) as connection:
        rows = connection.execute(query, {"instance": instance, "limit": limit}).fetchall()
        result = []
        for row in rows:
            name = connection.execute(
                "SELECT name FROM sessions WHERE player_id = ? "
                "ORDER BY started_at DESC LIMIT 1",
                (row["player_id"],),
            ).fetchone()
            result.append(
                PlayerTotals(
                    player_id=row["player_id"],
                    name=name["name"] if name else row["player_id"],
                    sessions=row["sessions"],
                    seconds=float(row["seconds"] or 0),
                    last_seen=datetime.strptime(row["last_seen"], TIMESTAMP_FORMAT),
                )
            )
    return result


def recent_sessions(
    config: Config, *, instance: str | None = None, limit: int = 50
) -> list[Session]:
    """The latest sessions, newest first."""
    query = """
        SELECT * FROM sessions
         WHERE (:instance IS NULL OR instance = :instance)
      ORDER BY started_at DESC
         LIMIT :limit
    """

    with connect(config) as connection:
        rows = connection.execute(query, {"instance": instance, "limit": limit}).fetchall()
    return [
        Session(
            instance=row["instance"],
            player_id=row["player_id"],
            name=row["name"],
            started_at=datetime.strptime(row["started_at"], TIMESTAMP_FORMAT),
            ended_at=datetime.strptime(row["ended_at"], TIMESTAMP_FORMAT),
            closed_by=row["closed_by"],
            source=row["source"],
        )
        for row in rows
    ]
