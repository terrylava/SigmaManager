"""state.json — which versions exist, and which instance is pinned to which (spec §8).

This file is the difference between a working GC and a deleted mod cache, so writes are
atomic: serialize to a temp file in the same directory, fsync, then os.replace. A crash
mid-write leaves the previous state intact rather than a truncated JSON file.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError


class StateError(Exception):
    """State file is unreadable or corrupt."""


class ModVersion(BaseModel):
    """One immutable snapshot in modcache/<workshop_id>/<version>/."""

    workshop_id: int
    # Steam's `time_updated`. This is the canonical version identity, not a counter
    # we invent (spec §6).
    version: int
    downloaded_at: datetime
    # Bytes on disk, for reporting. 0 if never measured.
    size_bytes: int = 0


class ProcessInfo(BaseModel):
    """A server process dzsm started.

    create_time is recorded alongside the pid because pids get reused: on its own a live
    pid proves nothing about whether it is still *our* process.
    """

    pid: int
    port: int
    started_at: datetime
    create_time: float | None = None


class State(BaseModel):
    """Everything dzsm knows that isn't in config."""

    # workshop_id -> version -> snapshot record
    versions: dict[int, dict[int, ModVersion]] = Field(default_factory=dict)
    # instance name -> mod name -> version currently activated for that instance.
    # This is what an instance's -mod= string was built from at its last start, and
    # what refcounting reads.
    pins: dict[str, dict[str, int]] = Field(default_factory=dict)
    # workshop_id -> time_updated last seen from the Steam Web API.
    last_seen_remote: dict[int, int] = Field(default_factory=dict)
    # instance name -> the process dzsm last started for it.
    processes: dict[str, ProcessInfo] = Field(default_factory=dict)
    # instance name -> when it was deliberately stopped. A held instance is not restarted
    # by the supervisor, on crash or on schedule: someone asked for it to be down, and a
    # supervisor that immediately undoes an explicit stop is worse than no supervisor.
    # Starting it clears the hold.
    held: dict[str, datetime] = Field(default_factory=dict)
    # instance name -> the server build it activated at its last start. Separate from
    # `pins` because a build id is a string and a mod version is an int, and conflating
    # them would make both harder to reason about.
    server_builds: dict[str, str] = Field(default_factory=dict)
    # Installed server app version, if any.
    serverapp_version: str | None = None
    # When Steam was last asked what changed. Separate from last_seen_remote, which only
    # records the answer: without this there is no way to tell "nothing has changed" from
    # "nothing has been checked", and those need very different responses.
    last_checked_at: datetime | None = None
    # Whether steamcmd could last authenticate from its cached sentry. None means nothing
    # has exercised it yet — which is not the same as broken, and must not be shown as if
    # it were. The sentry expires without warning, and the failure otherwise surfaces as a
    # download dying at whatever moment happens to need one.
    steam_session_ok: bool | None = None
    steam_session_checked_at: datetime | None = None
    # Why it failed, for showing without making anyone read the log.
    steam_session_detail: str | None = None

    def record_steam_session(self, *, ok: bool, detail: str | None = None) -> None:
        """Record what an operation that used steamcmd's credentials just proved.

        Called from downloads as well as the explicit check: a successful mod download is
        proof the session works, and there is no reason to make someone press a button to
        learn what dzsm already knows.
        """
        self.steam_session_ok = ok
        self.steam_session_checked_at = datetime.now(UTC)
        self.steam_session_detail = detail

    def record_version(self, workshop_id: int, version: int, size_bytes: int = 0) -> None:
        """Register a promoted snapshot."""
        self.versions.setdefault(workshop_id, {})[version] = ModVersion(
            workshop_id=workshop_id,
            version=version,
            downloaded_at=datetime.now(UTC),
            size_bytes=size_bytes,
        )

    def remove_version(self, workshop_id: int, version: int) -> None:
        """Forget a version. Called only from an explicit delete — nothing expires."""
        versions = self.versions.get(workshop_id)
        if versions is None:
            return
        versions.pop(version, None)
        if not versions:
            del self.versions[workshop_id]

    def known_versions(self, workshop_id: int) -> list[int]:
        """Versions on disk for a mod, newest first. Empty list if none — never False."""
        return sorted(self.versions.get(workshop_id, {}), reverse=True)

    def latest_version(self, workshop_id: int) -> int | None:
        versions = self.known_versions(workshop_id)
        return versions[0] if versions else None

    # Refcounting deliberately lives in mods.py, not here: pins are keyed by the mod's
    # *config name* and versions by workshop_id, so resolving one to the other needs
    # the Config. Doing it here without that mapping would miscount mods that happen to
    # share a time_updated value.


def load_state(path: Path) -> State:
    """Read state.json. A missing file is a fresh install, not an error."""
    if not path.exists():
        return State()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"cannot read state at {path}: {exc}") from exc
    try:
        return State.model_validate(data)
    except ValidationError as exc:
        raise StateError(f"state at {path} is corrupt:\n{exc}") from exc


def save_state(path: Path, state: State) -> None:
    """Atomically write state.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = state.model_dump_json(indent=2)

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        # Path.replace is os.replace — atomic on the same filesystem.
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
