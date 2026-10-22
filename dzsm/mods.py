"""Version cache: snapshot promotion, refcounting, GC (spec §2, §8, §12).

The invariant this module exists to protect: a directory under modcache/ is written
once and never modified again. Everything else in the design depends on it.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from dzsm.config import Config, LocalMod, WorkshopMod, is_bare_name
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


def promote_tree(
    source: Path,
    destination: Path,
    *,
    strategy: str = "copy",
    ignore: Callable[[str, list[str]], set[str]] | None = None,
) -> Path:
    """Copy a directory to an immutable destination, atomically from the reader's view.

    Copies into a staging directory and renames into place, so an interrupted copy can
    never leave a partially-populated version directory that later looks complete. Shared
    by the mod cache and the server app, which have the same problem for the same reason:
    a running server holds locks on whatever it is using, so nothing may ever be written
    over in place.

    "hardlink" is the untested optimization in spec §12 — it silently corrupts older
    versions if steamcmd writes files in place rather than delete+recreating.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".incoming-{destination.name}-{os.getpid()}"
    safe_rmtree(staging)

    copy_function = os.link if strategy == "hardlink" else shutil.copy2
    try:
        shutil.copytree(
            source, staging, copy_function=copy_function, symlinks=True, ignore=ignore
        )
        staging.replace(destination)
    except OSError as exc:
        try:
            safe_rmtree(staging)
        except (OSError, ModCacheError) as cleanup_exc:
            _log.error("mods: failed to clean up staging dir %s: %s", staging, cleanup_exc)
        raise ModCacheError(
            f"failed to promote {source} to {destination} ({strategy}): {exc}"
        ) from exc

    return destination


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

    promote_tree(source, destination, strategy=config.snapshot_strategy)
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
    kept_by: list[str] = field(default_factory=list)

    @property
    def in_use(self) -> bool:
        """Pinned by a running config, or held by a keep_mods claim.

        Both mean "do not reclaim this". Folding them together here is what stops the
        retention policy quietly expiring a mod that is only temporarily disabled.
        """
        return bool(self.pinned_by or self.kept_by)

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
    keeps = kept_by(config)

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
                    kept_by=keeps.get(mod_name, []) if mod_name else [],
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

    # A kept mod is temporarily disabled, not abandoned. Deleting its versions is the
    # exact thing keep_mods exists to prevent, so the guard belongs here as well as in
    # remove_mod — a per-version delete would otherwise walk straight around it.
    keeping = sorted(
        instance
        for mod_name, instances in kept_by(config).items()
        for instance in instances
        if _workshop_id_of(config, mod_name) == workshop_id
    )
    if keeping and not force:
        raise ModCacheError(
            f"refusing to delete mod {workshop_id} version {version}: instance(s) "
            f"{', '.join(keeping)} keep this mod (temporarily disabled, not abandoned). "
            f"Remove it from their keep_mods first, or pass force."
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
    kept_by: list[str] = field(default_factory=list)

    @property
    def deletable(self) -> bool:
        """Local mods are hand-authored source; kept mods are deliberately retained."""
        return self.kind != "local" and not self.kept_by


def kept_by(config: Config) -> dict[str, list[str]]:
    """Mod name -> instances that keep a claim on it without loading it.

    A kept mod is temporarily disabled, not abandoned. It does not appear as unused and
    cannot be removed by a cleanup, so its cache survives until it is re-enabled.
    """
    claims: dict[str, list[str]] = {}
    for instance_name, instance in config.instances.items():
        for mod_name in instance.keep_mods:
            claims.setdefault(mod_name, []).append(instance_name)
    return {name: sorted(instances) for name, instances in claims.items()}


@dataclass(frozen=True)
class LocalCandidate:
    """A directory on disk that looks like a DayZ mod."""

    path: Path
    name: str
    size_bytes: int
    has_addons: bool
    has_keys: bool
    configured_as: str | None


def looks_like_mod(path: Path) -> bool:
    """Whether a directory is plausibly a DayZ mod.

    An `addons/` directory is the real marker — that is where the PBOs live. The `@`
    prefix is only a convention, so it is accepted as a hint but not required: plenty of
    unpacked mods do not use it.
    """
    if not path.is_dir():
        return False
    return (path / "addons").is_dir() or (path / "Addons").is_dir() or path.name.startswith("@")


def scan_for_mods(config: Config, directory: Path) -> list[LocalCandidate]:
    """Find mod folders in (or at) a directory.

    Handles being pointed either at a folder full of mods — a server's `!Workshop`, say —
    or straight at a single mod folder, because both are things people will try.

    Reports what is already configured so the same directory is not added twice under two
    names.
    """
    if not directory.is_dir():
        raise ModCacheError(f"{directory} is not a directory")

    configured = {
        Path(str(mod.path)).resolve(): name
        for name, mod in config.mods.items()
        if isinstance(mod, LocalMod)
    }

    candidates = [directory] if looks_like_mod(directory) else []
    candidates += sorted(
        (child for child in directory.iterdir() if looks_like_mod(child)),
        key=lambda p: p.name.lower(),
    )

    return [
        LocalCandidate(
            path=path,
            name=path.name,
            size_bytes=_tree_size(path),
            has_addons=(path / "addons").is_dir() or (path / "Addons").is_dir(),
            has_keys=(path / "keys").is_dir() or (path / "Keys").is_dir(),
            configured_as=configured.get(path.resolve()),
        )
        for path in dict.fromkeys(candidates)
    ]


def record_check(
    config: Config, state: State, remote: dict[int, int]
) -> dict[str, int]:
    """Fold a check_updates answer into state. Returns mod name -> newer version available.

    Separate from the request that produced `remote` so the CLI and the web daemon record
    a check identically — otherwise checking from a terminal would leave the dashboard
    still reporting "never checked", and the two would disagree about what is out of date.

    Returns an empty dict when everything is current, which is a real answer rather than a
    falsy failure (Rule 1).
    """
    behind: dict[str, int] = {}
    for name, mod in config.mods.items():
        if not isinstance(mod, WorkshopMod):
            continue
        latest = remote.get(mod.workshop_id)
        if latest is None:
            # Steam did not answer for this id. Not knowing is not the same as being up
            # to date, so nothing is recorded for it.
            continue
        state.last_seen_remote[mod.workshop_id] = latest
        # A pinned mod is held at a version deliberately; reporting it as behind would
        # nag about a decision already made.
        if mod.pinned_version is None and latest not in state.known_versions(mod.workshop_id):
            behind[name] = latest
    state.last_checked_at = datetime.now(UTC)
    return behind


def local_copy(
    config: Config,
    state: State,
    mod_name: str,
    target_name: str,
    *,
    instance_name: str | None = None,
) -> tuple[Path, int | None]:
    """Fork a mod's files into localmods/<target_name>/, returning (path, source version).

    For editing a mod's contents — a types.xml inside a map mod, a config the author left
    in the PBO-adjacent tree — without steamcmd overwriting the change on the next update.
    The copy is a local mod (spec §9): unversioned, never downloaded, never swept.

    The source is the version the instance is *actually running* where one is pinned,
    rather than the newest cached one. Copying files the admin has not seen would be a
    surprising thing for a button labelled "make a local copy" to do, and the newest
    cached version is frequently one that has been downloaded but not yet activated.

    The version is returned rather than recorded in the config: once forked, the copy has
    no relationship to the Workshop item and must not be mistaken for a pin to it.
    """
    # The name reaches here straight from a request body, and it names a directory that is
    # about to be written to. `localmods_dir / "../../x"` escapes the root, and an
    # absolute path replaces it outright — and the failure path below deletes whatever it
    # produced, so an unchecked name is an arbitrary write *and* an arbitrary delete.
    if not is_bare_name(target_name):
        raise ModCacheError(
            f"{target_name!r} is not a usable name for a local copy: it must be a plain "
            f"folder name, with no path separators or '..'"
        )

    mod = config.mods.get(mod_name)
    if mod is None:
        raise ModCacheError(f"no mod named {mod_name!r}")

    source_version: int | None = None
    if isinstance(mod, WorkshopMod):
        pinned = state.pins.get(instance_name or "", {}).get(mod_name)
        source_version = pinned if pinned is not None else state.latest_version(mod.workshop_id)
        if source_version is None:
            raise ModCacheError(
                f"{mod_name!r} has no cached version to copy — run `dzsm mods update` first"
            )
        source = config.version_dir(mod.workshop_id, source_version)
    else:
        source = mod.path

    if not source.is_dir():
        raise ModCacheError(f"{mod_name!r} source files are missing: {source}")

    destination = config.localmods_dir / target_name
    # Second layer, deliberately: the name check above is a denylist of characters, and
    # this is the property that actually matters. If they ever disagree, this one decides.
    if destination.resolve().parent != config.localmods_dir.resolve():
        raise ModCacheError(f"refusing to write outside {config.localmods_dir}: {destination}")
    if destination.exists():
        raise ModCacheError(f"{destination} already exists; pick another name")

    destination.parent.mkdir(parents=True, exist_ok=True)
    # Always a real copy, never config.snapshot_strategy. Hardlinks would make every edit
    # to the fork an edit to the cached version it came from — the exact thing this exists
    # to avoid, and it would corrupt a snapshot other instances are running.
    promote_tree(source, destination, strategy="copy")
    return destination, source_version


def unused_mods(
    config: Config, state: State, *, include_kept: bool = False
) -> list[UnusedMod]:
    """Every mod nothing references. Reporting only — deletes nothing.

    Mods kept by an instance (`keep_mods`) are excluded unless include_kept, since being
    deliberately retained is the opposite of being unused.

    Returns a list, empty when everything is in use. Never None (Rule 1).
    """
    keeps = kept_by(config)
    referenced = {
        name
        for instance in config.instances.values()
        for name in (*instance.mods, *instance.server_mods)
    }
    if not include_kept:
        referenced |= set(keeps)
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
        claimed = keeps.get(name, [])
        if isinstance(mod, WorkshopMod):
            versions = state.known_versions(mod.workshop_id)
            directory = config.modcache_dir / str(mod.workshop_id)
            unused.append(
                UnusedMod(
                    mod_name=name,
                    workshop_id=mod.workshop_id,
                    kind="kept" if claimed else "unreferenced",
                    versions=versions,
                    size_bytes=_tree_size(directory),
                    path=directory,
                    pinned_by=pinned_by,
                    kept_by=claimed,
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
                    kept_by=claimed,
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

    keeping = sorted(
        instance
        for mod_name, instances in kept_by(config).items()
        for instance in instances
        if _workshop_id_of(config, mod_name) == workshop_id
    )
    if keeping and not force:
        raise ModCacheError(
            f"refusing to remove mod {workshop_id}: instance(s) {', '.join(keeping)} "
            f"keep it (temporarily disabled, not abandoned). Remove it from their "
            f"keep_mods first, or pass force."
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


def uncached_mods(config: Config, state: State, instance_name: str) -> dict[str, int]:
    """Workshop mods this instance needs that have NO version on disk at all.

    Name -> workshop id, in load order. Deliberately not "is there a newer version":
    an instance with an older copy can run, and making a restart wait on a download
    would break Rule 4. This is the other case — nothing to launch at all.

    A mod pinned to a version that is not cached counts, since that pin is what the
    instance would have to run.

    Returns a dict, empty when everything is present — never None (Rule 1).
    """
    instance = config.instances[instance_name]
    missing: dict[str, int] = {}
    for mod_name in [*instance.mods, *instance.server_mods]:
        mod = config.mods.get(mod_name)
        if not isinstance(mod, WorkshopMod):
            continue
        if mod.pinned_version is not None:
            if not config.version_dir(mod.workshop_id, mod.pinned_version).is_dir():
                missing[mod_name] = mod.workshop_id
            continue
        if not any(
            config.version_dir(mod.workshop_id, version).is_dir()
            for version in state.known_versions(mod.workshop_id)
        ):
            missing[mod_name] = mod.workshop_id
    return missing


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

    # Server-only mods are versioned and activated exactly like client-facing ones —
    # they just end up in a different launch argument.
    for mod_name in [*instance.mods, *instance.server_mods]:
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
    """Absolute paths for an instance's client-facing mods, in declared load order.

    The order is exactly InstanceConfig.mods and must never be sorted — it is the
    semicolon order in -mod=, and framework mods have to precede their dependents
    (spec §4).

    Raises ModCacheError if a *workshop* mod's version directory is missing: that is a
    cache that should have been there, and it can be fetched. A local mod whose folder
    has gone is skipped instead, with an error logged — nothing can be downloaded to
    replace it, so refusing to start only turns a missing mod into a missing server.
    """
    return _paths_for(config, instance_name, config.instances[instance_name].mods, versions)


def server_mod_paths(
    config: Config, instance_name: str, versions: dict[str, int]
) -> list[Path]:
    """Absolute paths for an instance's server-only mods, in declared load order.

    These become -serverMod=. Clients never load them, so they are not subject to
    client-side signature checks. Empty list when the instance declares none.
    """
    return _paths_for(
        config, instance_name, config.instances[instance_name].server_mods, versions
    )


def _paths_for(
    config: Config,
    instance_name: str,
    names: list[str],
    versions: dict[str, int],
) -> list[Path]:
    paths: list[Path] = []

    for mod_name in names:
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
            if isinstance(mod, LocalMod):
                # A local mod is a folder someone put there by hand, and it can be moved,
                # renamed or deleted without dzsm hearing about it. There is nothing to
                # download and nothing to fall back to, so the choice is between a server
                # that is up without it and a server that is down — and a server that is
                # down helps nobody. Loudly, with the path, because the next question is
                # always "where did it go".
                _log.error(
                    "instance %s: local mod %r is not at %s — starting without it",
                    instance_name,
                    mod_name,
                    path,
                )
                continue
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
