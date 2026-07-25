"""Version cache: snapshot promotion, refcounting, GC (spec §2, §8, §12).

The invariant this module exists to protect: a directory under modcache/ is written
once and never modified again. Everything else in the design depends on it.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from dzsm.config import Config
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


def refcounts(config: Config, state: State) -> dict[tuple[int, int], int]:
    """Map (workshop_id, version) -> number of instances pinned to it.

    Empty dict when nothing is pinned. Never False (Rule 1).
    """
    raise NotImplementedError("spec §8")


def collect_garbage(config: Config, state: State, *, dry_run: bool = True) -> list[Path]:
    """Delete version directories that are unreferenced AND older than the retention
    window (config.gc_retention_days).

    Both conditions are required. The retention window is what makes rollback possible:
    reverting an instance to its previous version is a state edit, not a re-download.

    Returns the list of directories deleted (or that would be deleted, when dry_run).
    Empty list when there is nothing to collect.
    """
    raise NotImplementedError("spec §8")


def resolve_mod_paths(config: Config, state: State, instance_name: str) -> list[Path]:
    """Absolute paths for an instance's mods, in the instance's declared load order.

    Workshop mods resolve to their currently pinned version directory; local mods
    resolve to their configured path. The returned order is exactly the order in
    InstanceConfig.mods and must not be sorted (spec §4).

    Raises if a mod has no usable version on disk — a caller must not silently start a
    server with a mod missing.
    """
    raise NotImplementedError("spec §4, §9")
