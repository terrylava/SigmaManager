"""Backups of an instance's world.

The mission directory holds `storage_<instanceId>/` — every base, stash, vehicle and
placed object players have built — alongside `db/`, the `cfg*.json` files and
`types.xml`, which is the economy someone tuned. Losing it loses other people's work,
and nothing here was backing it up.

Two things about where that directory lives make this less obvious than it sounds.

It is inside `serverapp/<buildid>/mpmissions/`, so the world moves to a new directory
at every server update. `instance._mission_from_another_build` carries it across, which
is why the previous build still holds a copy — but that is a side effect of the update
path, not a backup, and it goes the moment that build is pruned. Backups therefore live
under `root/backups/`, outside `serverapp/` entirely.

And DayZ writes persistence continuously while the server runs, so copying a live
`storage_1` can catch a record mid-write. The one moment the world is certainly
consistent is between a stop and the next start, which is a window dzsm already owns.
"""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dzsm.config import Config
from dzsm.instance import InstanceError, effective_server_dir, read_template
from dzsm.mods import ModCacheError, promote_tree, require_space, safe_rmtree
from dzsm.state import State

_log = logging.getLogger(__name__)

# "2026-10-22-110723". Sorts chronologically as text, which is the only ordering this
# needs and avoids trusting directory mtime — the same reasoning as build ids.
#
# Seconds, not minutes. At minute resolution a restore taken within a minute of a
# backup collided, and the safety copy of the world about to be replaced was silently
# skipped — which is the one moment it matters most.
STAMP_FORMAT = "%Y-%m-%d-%H%M%S"
STAMP_LENGTH = 15

# Written beside a finished backup so a half-copied one can be told apart from a good
# one. promote_tree already renames into place, so this is belt and braces for the case
# where the process dies between the rename and the next line.
MARKER = ".dzsm-backup"


class BackupError(Exception):
    """A backup or restore could not be completed. Nothing partial is left behind."""


@dataclass(frozen=True)
class Backup:
    """One stored copy of an instance's world."""

    instance: str
    stamp: str
    path: Path
    size_bytes: int
    taken_at: datetime

    @property
    def mission(self) -> str:
        """The mission directory name this copy was taken from."""
        inner = [p for p in self.path.iterdir() if p.is_dir()]
        return inner[0].name if inner else "?"


def backups_dir(config: Config, instance_name: str) -> Path:
    return config.root / "backups" / instance_name


def current_mission_dir(config: Config, state: State, instance_name: str) -> Path | None:
    """The mission directory this instance is actually running, or None.

    Read from the instance's own serverDZ.cfg, because dzsm owns that `template` line
    and keeps it pointed at the resolved mission — asking the config rather than
    re-deriving the name is what keeps this honest when someone switches map.
    """
    instance = config.instances[instance_name]
    mission = read_template(config.instance_dir(instance_name) / instance.config_file)
    if mission is None:
        return None
    try:
        server_dir = effective_server_dir(config, state, instance_name)
    except InstanceError:
        return None
    path = server_dir / "mpmissions" / mission
    return path if path.is_dir() else None


def list_backups(config: Config, instance_name: str) -> list[Backup]:
    """Every stored copy for an instance, newest first.

    Returns a list, empty when there are none and when the directory does not exist —
    never None (Rule 1).
    """
    root = backups_dir(config, instance_name)
    if not root.is_dir():
        return []

    found: list[Backup] = []
    for entry in sorted(root.iterdir(), reverse=True):
        if not entry.is_dir() or not (entry / MARKER).is_file():
            continue
        try:
            # Only the timestamp; a "-2" disambiguating suffix is not part of it.
            taken = datetime.strptime(entry.name[:STAMP_LENGTH], STAMP_FORMAT)
        except ValueError:
            # Not one of ours, or renamed by hand. Left alone rather than guessed at.
            continue
        found.append(
            Backup(
                instance=instance_name,
                stamp=entry.name,
                path=entry,
                size_bytes=_tree_size(entry),
                taken_at=taken,
            )
        )
    return found


def take(
    config: Config, state: State, instance_name: str, *, keep: int | None = None
) -> Backup | None:
    """Copy this instance's world. Returns the backup, or None when there is nothing yet.

    None rather than an error when the instance has no mission directory: an instance
    that has never started has no world to lose, and failing a restart over that would
    be absurd.

    Raises BackupError when there is a world and it could not be copied.
    """
    mission = current_mission_dir(config, state, instance_name)
    if mission is None:
        _log.info(
            "instance %s: no mission directory yet, nothing to back up", instance_name
        )
        return None

    stamp = _free_stamp(backups_dir(config, instance_name))
    target = backups_dir(config, instance_name) / stamp

    try:
        require_space(config, _tree_size(mission), what=f"a backup of {instance_name!r}")
    except ModCacheError as exc:
        raise BackupError(str(exc)) from exc

    _log.info("instance %s: backing up %s to %s", instance_name, mission.name, target)
    try:
        # The mission name is kept as the inner directory so a restore knows where it
        # came from, and so a backup is readable without consulting dzsm.
        promote_tree(mission, target / mission.name)
        (target / MARKER).write_text(mission.name, encoding="utf-8")
    except OSError as exc:
        safe_rmtree(target)
        raise BackupError(f"instance {instance_name!r}: backup failed: {exc}") from exc

    prune(config, instance_name, keep if keep is not None else config.backup_keep)
    return next((b for b in list_backups(config, instance_name) if b.stamp == stamp), None)


def prune(config: Config, instance_name: str, keep: int) -> list[str]:
    """Delete all but the newest `keep` backups. Returns what was removed.

    `keep <= 0` keeps everything: a retention setting someone has not thought about must
    not be the thing that deletes their only copy. Same stance as `gc_retention_days`,
    which is unset until asked for.
    """
    if keep <= 0:
        return []
    removed: list[str] = []
    for backup in list_backups(config, instance_name)[keep:]:
        try:
            safe_rmtree(backup.path)
        except OSError as exc:
            _log.error("could not prune backup %s: %s", backup.path, exc)
            continue
        _log.info("instance %s: pruned backup %s", instance_name, backup.stamp)
        removed.append(backup.stamp)
    return removed


def restore(config: Config, state: State, instance_name: str, stamp: str) -> Path:
    """Put a stored world back. Returns the mission directory that was replaced.

    The caller must have stopped the instance: a running server holds its persistence
    open, so this would half-apply and leave a world that is neither.

    The current world is backed up first. Restoring the wrong copy is an easy mistake
    and an unrecoverable one otherwise.
    """
    backup = next((b for b in list_backups(config, instance_name) if b.stamp == stamp), None)
    if backup is None:
        available = ", ".join(b.stamp for b in list_backups(config, instance_name))
        raise BackupError(
            f"instance {instance_name!r} has no backup {stamp!r}. "
            f"Available: {available or 'none'}"
        )

    stored = [p for p in backup.path.iterdir() if p.is_dir()]
    if not stored:
        raise BackupError(f"backup {stamp} holds no mission directory")
    source = stored[0]

    try:
        server_dir = effective_server_dir(config, state, instance_name)
    except InstanceError as exc:
        raise BackupError(f"instance {instance_name!r}: {exc}") from exc
    target = server_dir / "mpmissions" / source.name

    if target.is_dir():
        _log.info("instance %s: backing up the current world before restoring", instance_name)
        take(config, state, instance_name)

    _log.info("instance %s: restoring %s from %s", instance_name, source.name, stamp)
    _replace_tree(source, target, instance_name)
    return target


def _replace_tree(source: Path, target: Path, instance_name: str) -> None:
    """Put `source` where `target` is, even though `target` already exists.

    promote_tree renames a staging copy into place, which is right for a mod version or
    a mission that does not exist yet — and fails on Windows the moment the destination
    does, because a non-empty directory cannot be renamed over. A restore is the case
    where it always does.

    So the same dance the binary updater uses: build the replacement beside it, move the
    old one out of the way, move the new one in, and only then delete the old one. The
    world is never absent, and a failure at any step leaves the original in place.
    """
    staging = target.parent / f".restoring-{target.name}-{os.getpid()}"
    outgoing = target.parent / f".replaced-{target.name}-{os.getpid()}"
    safe_rmtree(staging)
    safe_rmtree(outgoing)

    try:
        promote_tree(source, staging)
    except OSError as exc:
        safe_rmtree(staging)
        raise BackupError(f"instance {instance_name!r}: restore failed: {exc}") from exc

    moved = False
    try:
        if target.exists():
            target.rename(outgoing)
            moved = True
        staging.rename(target)
    except OSError as exc:
        if moved and not target.exists():
            # Put the world back rather than leaving the instance with none.
            with contextlib.suppress(OSError):
                outgoing.rename(target)
        safe_rmtree(staging)
        raise BackupError(
            f"instance {instance_name!r}: could not replace {target}: {exc}. "
            f"A running server holds its mission open — stop it first."
        ) from exc

    safe_rmtree(outgoing)


def _free_stamp(root: Path) -> str:
    """A stamp no backup is already using.

    Two in the same second is pathological, but a restore takes one to save the world it
    is about to replace — so a collision has to produce a second directory rather than
    quietly reusing the first, which is how the world being replaced would be lost.
    """
    base = datetime.now().strftime(STAMP_FORMAT)
    if not (root / base).exists():
        return base
    for suffix in range(2, 100):
        candidate = f"{base}-{suffix}"
        if not (root / candidate).exists():
            return candidate
    raise BackupError(f"cannot find an unused backup name under {root}")


def _tree_size(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total

