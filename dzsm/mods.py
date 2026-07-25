"""Version cache: snapshot promotion, refcounting, GC (spec §2, §8, §12).

The invariant this module exists to protect: a directory under modcache/ is written
once and never modified again. Everything else in the design depends on it.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dzsm.config import Config, WorkshopMod
from dzsm.state import State

_log = logging.getLogger(__name__)


class ModCacheError(Exception):
    """Snapshot promotion or GC failed."""


def is_reparse_point(path: Path) -> bool:
    """True if path is a junction, symlink, or other reparse point.

    os.path.islink() is unreliable for Windows junctions — it does not always return
    True. st_reparse_tag is the trustworthy signal (Windows, Python 3.8+).
    """
    try:
        st = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return bool(getattr(st, "st_reparse_tag", 0))


def safe_rmtree(path: Path) -> None:
    """Recursive delete that refuses to walk through a reparse point.

    Rule 2 (spec §7). shutil.rmtree() on a junction can follow the reparse point and
    delete the *target* — this has cost people their entire mod cache.
    """
    if not path.exists():
        return
    if is_reparse_point(path):
        raise ModCacheError(
            f"refusing to recursively delete {path}: it is a reparse point "
            f"(junction/symlink), and deleting through it can destroy the target"
        )
    shutil.rmtree(path)


def promote_snapshot(
    config: Config,
    state: State,
    workshop_id: int,
    version: int,
) -> Path:
    """Copy steamcmd's download into an immutable modcache version directory.

    Source is config.workshop_download_dir / str(workshop_id) (tier 1, mutable).
    Destination is config.version_dir(workshop_id, version) (tier 2, immutable).

    Must be called only after steam.verify_download() has passed — steamcmd exits 0 on
    failed downloads, so promoting an unverified snapshot bakes a broken mod into the
    cache permanently (spec §6).

    Writes to a temp directory and renames into place, so an interrupted copy never
    leaves a half-populated version directory that later looks complete.

    Honours config.snapshot_strategy. "hardlink" is untested (spec §12) — it corrupts
    older versions if steamcmd writes files in place rather than delete+recreating.

    Returns the version directory path. Raises ModCacheError on failure — never
    returns None to signal a problem (Rule 1).
    """
    source = config.workshop_download_dir / str(workshop_id)
    destination = config.version_dir(workshop_id, version)

    if destination.exists():
        # Already promoted. Immutability means there is nothing to refresh — re-copying
        # would be the one operation this whole design exists to avoid.
        _log.info(
            "mods: mod %s version %s already in cache at %s", workshop_id, version, destination
        )
        if workshop_id not in state.versions or version not in state.versions[workshop_id]:
            state.record_version(workshop_id, version, _tree_size(destination))
        return destination

    if not source.is_dir():
        raise ModCacheError(
            f"cannot promote mod {workshop_id} version {version}: steamcmd download "
            f"directory {source} does not exist"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)

    # Copy into a staging directory and rename into place, so an interrupted copy can
    # never leave a partially-populated version directory that later looks complete.
    staging = destination.parent / f".incoming-{version}-{os.getpid()}"
    safe_rmtree(staging)

    copy_function = os.link if config.snapshot_strategy == "hardlink" else shutil.copy2
    try:
        shutil.copytree(source, staging, copy_function=copy_function, symlinks=True)
        staging.replace(destination)
    except OSError as exc:
        try:
            safe_rmtree(staging)
        except (OSError, ModCacheError) as cleanup_exc:  # noqa: BLE001 — reported, not hidden
            _log.error(
                "mods: failed to clean up staging dir %s for mod %s: %s",
                staging,
                workshop_id,
                cleanup_exc,
            )
        raise ModCacheError(
            f"failed to promote mod {workshop_id} version {version} "
            f"({config.snapshot_strategy}) from {source} to {destination}: {exc}"
        ) from exc

    size = _tree_size(destination)
    state.record_version(workshop_id, version, size)
    _log.info(
        "mods: promoted mod %s version %s to %s (%.1f MiB)",
        workshop_id,
        version,
        destination,
        size / (1024 * 1024),
    )
    return destination


def _tree_size(path: Path) -> int:
    """Total size of the files in a tree. 0 if the tree is unreadable."""
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:  # noqa: PERF203 — a single unreadable file must not abort sizing
            continue
    return total


@dataclass(frozen=True)
class VersionStatus:
    """One cached version and everything needed to decide whether to keep it."""

    mod_name: str | None
    workshop_id: int
    version: int
    path: Path
    exists: bool
    size_bytes: int
    downloaded_at: datetime | None
    pinned_by: list[str]
    is_latest: bool

    @property
    def in_use(self) -> bool:
        return bool(self.pinned_by)

    @property
    def age_days(self) -> float | None:
        if self.downloaded_at is None:
            return None
        return (datetime.now(UTC) - self.downloaded_at).total_seconds() / 86400


def version_status(config: Config, state: State) -> list[VersionStatus]:
    """Every cached version, newest first per mod, with what references it.

    Reporting only — nothing is deleted here, and nothing anywhere deletes a version
    without being explicitly told to. Old versions are kept until someone asks for them
    to go, because the cache is also the rollback path: reverting an instance to its
    previous version is a state edit, not a re-download.

    Returns a list, empty when nothing is cached. Never None (Rule 1).
    """
    names = {
        mod.workshop_id: name
        for name, mod in config.mods.items()
        if isinstance(mod, WorkshopMod)
    }

    statuses: list[VersionStatus] = []
    for workshop_id, versions in state.versions.items():
        latest = max(versions, default=None)
        for version, record in versions.items():
            path = config.version_dir(workshop_id, version)
            mod_name = names.get(workshop_id)
            pinned_by = sorted(
                instance
                for instance, pins in state.pins.items()
                if mod_name is not None and pins.get(mod_name) == version
            )
            statuses.append(
                VersionStatus(
                    mod_name=mod_name,
                    workshop_id=workshop_id,
                    version=version,
                    path=path,
                    exists=path.is_dir(),
                    size_bytes=record.size_bytes,
                    downloaded_at=record.downloaded_at,
                    pinned_by=pinned_by,
                    is_latest=version == latest,
                )
            )

    statuses.sort(key=lambda s: (s.mod_name or str(s.workshop_id), -s.version))
    return statuses


def delete_version(
    config: Config,
    state: State,
    workshop_id: int,
    version: int,
    *,
    running_instances: Collection[str] = (),
    force: bool = False,
) -> Path:
    """Delete one cached version. Only ever called because a human asked for it.

    Refuses when an instance is pinned to it, because that instance would fail to start
    or silently lose a mod at its next restart. `force` overrides that for a stale pin,
    but never for a *running* instance: the server holds OS-level locks on those files,
    which is the constraint this entire design is built around.

    Returns the deleted path. Raises ModCacheError rather than reporting failure by
    return value (Rule 1).
    """
    path = config.version_dir(workshop_id, version)
    pinned_by = [
        instance
        for instance, pins in state.pins.items()
        for mod_name, pinned in pins.items()
        if pinned == version and _workshop_id_of(config, mod_name) == workshop_id
    ]

    running_pins = sorted(set(pinned_by) & set(running_instances))
    if running_pins:
        raise ModCacheError(
            f"refusing to delete mod {workshop_id} version {version}: it is in use by "
            f"running instance(s) {', '.join(running_pins)}. A running DayZ server holds "
            f"OS-level locks on its mod files — stop the instance first."
        )

    if pinned_by and not force:
        raise ModCacheError(
            f"refusing to delete mod {workshop_id} version {version}: instance(s) "
            f"{', '.join(sorted(pinned_by))} are pinned to it. Repoint them first, or "
            f"pass force to delete anyway."
        )

    if path.exists():
        safe_rmtree(path)
        _log.info("mods: deleted mod %s version %s at %s", workshop_id, version, path)
    else:
        _log.warning(
            "mods: mod %s version %s was recorded but not on disk at %s; "
            "clearing the record",
            workshop_id,
            version,
            path,
        )

    state.remove_version(workshop_id, version)

    # Drop any now-dangling pins so state does not keep claiming a version that is gone.
    for instance, pins in state.pins.items():
        for mod_name, pinned in list(pins.items()):
            if pinned == version and _workshop_id_of(config, mod_name) == workshop_id:
                del pins[mod_name]
                _log.warning(
                    "mods: cleared pin %s -> %s for instance %s (version deleted)",
                    mod_name,
                    version,
                    instance,
                )

    return path


@dataclass(frozen=True)
class UnusedMod:
    """A mod nothing is using, and what removing it would reclaim.

    Three kinds:

    - "unreferenced": configured as a workshop mod, but no instance lists it.
    - "orphaned": a modcache directory whose workshop id is not in config at all —
      usually a mod deleted from config without its cache being cleared.
    - "local": a configured local mod no instance lists. Reported but never deletable:
      the directory is hand-authored source that dzsm did not create (spec §9).
    """

    mod_name: str | None
    workshop_id: int | None
    kind: str
    versions: list[int]
    size_bytes: int
    path: Path
    pinned_by: list[str]

    @property
    def deletable(self) -> bool:
        return self.kind != "local"


def unused_mods(config: Config, state: State) -> list[UnusedMod]:
    """Every mod nothing references. Reporting only — deletes nothing.

    Returns a list, empty when everything is in use. Never None (Rule 1).
    """
    referenced = {name for instance in config.instances.values() for name in instance.mods}
    configured_ids = {
        mod.workshop_id for mod in config.mods.values() if isinstance(mod, WorkshopMod)
    }
    unused: list[UnusedMod] = []

    for name, mod in config.mods.items():
        if name in referenced:
            continue
        pinned_by = sorted(
            instance for instance, pins in state.pins.items() if name in pins
        )
        if isinstance(mod, WorkshopMod):
            versions = state.known_versions(mod.workshop_id)
            directory = config.modcache_dir / str(mod.workshop_id)
            unused.append(
                UnusedMod(
                    mod_name=name,
                    workshop_id=mod.workshop_id,
                    kind="unreferenced",
                    versions=versions,
                    size_bytes=_tree_size(directory),
                    path=directory,
                    pinned_by=pinned_by,
                )
            )
        else:
            unused.append(
                UnusedMod(
                    mod_name=name,
                    workshop_id=None,
                    kind="local",
                    versions=[],
                    size_bytes=_tree_size(mod.path),
                    path=mod.path,
                    pinned_by=pinned_by,
                )
            )

    # Cache directories for mods that are not in config at all. These are invisible to
    # every other command, so without this they would sit on disk forever.
    if config.modcache_dir.is_dir():
        for directory in sorted(config.modcache_dir.iterdir()):
            if not directory.is_dir() or not directory.name.isdigit():
                continue
            workshop_id = int(directory.name)
            if workshop_id in configured_ids:
                continue
            unused.append(
                UnusedMod(
                    mod_name=None,
                    workshop_id=workshop_id,
                    kind="orphaned",
                    versions=state.known_versions(workshop_id),
                    size_bytes=_tree_size(directory),
                    path=directory,
                    pinned_by=[],
                )
            )

    return unused


def remove_mod(
    config: Config,
    state: State,
    workshop_id: int,
    *,
    running_instances: Collection[str] = (),
    force: bool = False,
) -> list[Path]:
    """Remove every cached version of one mod, plus its cache directory.

    Same refusals as delete_version, checked up front against the whole mod rather than
    one version at a time: a pin blocks it, and a pin held by a running instance blocks
    it even with force.

    Also removes version directories present on disk but absent from state, which is how
    an interrupted run or a hand-copied directory would otherwise linger forever.
    """
    directory = config.modcache_dir / str(workshop_id)
    pinned_by = sorted(
        instance
        for instance, pins in state.pins.items()
        for mod_name in pins
        if _workshop_id_of(config, mod_name) == workshop_id
    )

    running_pins = sorted(set(pinned_by) & set(running_instances))
    if running_pins:
        raise ModCacheError(
            f"refusing to remove mod {workshop_id}: in use by running instance(s) "
            f"{', '.join(running_pins)}. Stop them first — a running DayZ server holds "
            f"OS-level locks on its mod files."
        )
    if pinned_by and not force:
        raise ModCacheError(
            f"refusing to remove mod {workshop_id}: instance(s) "
            f"{', '.join(pinned_by)} are pinned to it. Repoint them first, or pass force."
        )

    removed: list[Path] = []
    for version in state.known_versions(workshop_id):
        try:
            removed.append(
                delete_version(
                    config,
                    state,
                    workshop_id,
                    version,
                    running_instances=running_instances,
                    force=force,
                )
            )
        except ModCacheError as exc:
            _log.error("mods: could not remove version %s of %s: %s", version, workshop_id, exc)

    if directory.is_dir():
        # Sweeps up version directories that were never recorded in state.
        safe_rmtree(directory)
        _log.info("mods: removed cache directory %s", directory)
        if directory not in removed:
            removed.append(directory)

    return removed


def prunable(
    config: Config,
    state: State,
    *,
    retention_days: int | None = None,
) -> list[VersionStatus]:
    """Versions the retention policy would remove. Empty when the policy is off.

    Three conditions, all required:

    1. No instance is pinned to it.
    2. It is not the newest version of its mod. A freshly downloaded version that no
       instance has activated yet is unused by definition — expiring it would delete the
       update you just fetched.
    3. It is older than the retention window.

    Selection only; this deletes nothing.
    """
    days = retention_days if retention_days is not None else config.gc_retention_days
    if days is None:
        return []

    return [
        status
        for status in version_status(config, state)
        if not status.in_use
        and not status.is_latest
        and status.age_days is not None
        and status.age_days >= days
    ]


def collect_garbage(
    config: Config,
    state: State,
    *,
    running_instances: Collection[str] = (),
    retention_days: int | None = None,
    dry_run: bool = True,
) -> list[Path]:
    """Apply the optional retention policy (spec §8).

    Opt-in: with gc_retention_days unset this is a no-op, which is the default. Even when
    enabled it goes through delete_version, so the pinned and running-instance refusals
    apply exactly as they do to a manual delete — automation gets no extra privileges.

    A version that cannot be removed is logged and skipped rather than aborting the run
    (Rule 3). Returns what was deleted, or what would be when dry_run.
    """
    candidates = prunable(config, state, retention_days=retention_days)
    if dry_run:
        return [status.path for status in candidates]

    deleted: list[Path] = []
    for status in candidates:
        try:
            deleted.append(
                delete_version(
                    config,
                    state,
                    status.workshop_id,
                    status.version,
                    running_instances=running_instances,
                )
            )
        except ModCacheError as exc:
            _log.error(
                "mods: could not prune mod %s version %s: %s",
                status.workshop_id,
                status.version,
                exc,
            )
    return deleted


def _workshop_id_of(config: Config, mod_name: str) -> int | None:
    mod = config.mods.get(mod_name)
    return mod.workshop_id if isinstance(mod, WorkshopMod) else None


def resolve_mod_versions(config: Config, state: State, instance_name: str) -> dict[str, int]:
    """Workshop mod name -> the version this instance should activate at its next start.

    This is the activation decision, and the only moment it is ever made (spec §2). It
    prefers the newest cached version, which is what makes a restart pick up whatever was
    downloaded while the server was running. An explicit pinned_version in config wins
    over that.

    Local mods are absent from the result: they have no versions (spec §9).

    Raises ModCacheError if a workshop mod has nothing usable on disk. Returns a dict,
    empty when the instance has no workshop mods — never None (Rule 1).
    """
    instance = config.instances[instance_name]
    versions: dict[str, int] = {}

    for mod_name in instance.mods:
        mod = config.mods[mod_name]
        if not isinstance(mod, WorkshopMod):
            continue

        if mod.pinned_version is not None:
            if not config.version_dir(mod.workshop_id, mod.pinned_version).is_dir():
                raise ModCacheError(
                    f"instance {instance_name!r}: mod {mod_name!r} is pinned to version "
                    f"{mod.pinned_version} but that version is not in the cache"
                )
            versions[mod_name] = mod.pinned_version
            continue

        latest = state.latest_version(mod.workshop_id)
        if latest is None:
            raise ModCacheError(
                f"instance {instance_name!r}: mod {mod_name!r} "
                f"({mod.workshop_id}) has no cached version — run `dzsm mods update`"
            )
        versions[mod_name] = latest

    return versions


def mod_paths(config: Config, instance_name: str, versions: dict[str, int]) -> list[Path]:
    """Absolute paths for an instance's mods, in the instance's declared load order.

    The order is exactly InstanceConfig.mods and must never be sorted — it is the
    semicolon order in -mod=, and framework mods have to precede their dependents
    (spec §4).

    Raises ModCacheError if any path is missing, rather than quietly starting a server
    with a mod absent.
    """
    instance = config.instances[instance_name]
    paths: list[Path] = []

    for mod_name in instance.mods:
        mod = config.mods[mod_name]
        if isinstance(mod, WorkshopMod):
            version = versions.get(mod_name)
            if version is None:
                raise ModCacheError(
                    f"instance {instance_name!r}: no version resolved for mod {mod_name!r}"
                )
            path = config.version_dir(mod.workshop_id, version)
        else:
            path = mod.path

        if not path.is_dir():
            raise ModCacheError(
                f"instance {instance_name!r}: mod {mod_name!r} path does not exist: {path}"
            )
        paths.append(path)

    return paths


def refcounts(config: Config, state: State) -> dict[tuple[int, int], int]:
    """Map (workshop_id, version) -> number of instances pinned to it.

    Pins are keyed by the mod's config name while versions are keyed by workshop_id, so
    this needs the Config to bridge them — which is why it lives here and not on State.

    Empty dict when nothing is pinned. Never False (Rule 1).
    """
    counts: dict[tuple[int, int], int] = {}
    for instance_pins in state.pins.values():
        for mod_name, version in instance_pins.items():
            mod = config.mods.get(mod_name)
            if not isinstance(mod, WorkshopMod):
                # The mod was removed from config, or turned into a local mod, while an
                # instance was still pinned to it. Not fatal, but it means the version it
                # points at is now unreferenced and eligible for GC — say so (Rule 3).
                _log.warning(
                    "mods: pin references unknown or non-workshop mod %r (version %s); "
                    "its cache entry is now unreferenced",
                    mod_name,
                    version,
                )
                continue
            key = (mod.workshop_id, version)
            counts[key] = counts.get(key, 0) + 1
    return counts
