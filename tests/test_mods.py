"""Reparse-point guard tests (Rule 2).

The failure this prevents — rmtree following a junction and deleting the target — is
unrecoverable, so the guard gets a test even though the surrounding module is stubbed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from dzsm.config import Config
from dzsm.mods import ModCacheError, is_reparse_point, promote_snapshot, safe_rmtree
from dzsm.state import State


def _link_directory(link: Path, target: Path) -> bool:
    """Create a directory reparse point. True if it worked.

    On Windows this makes a *junction*, not a symlink: junctions need no special
    privilege, and they are the case Rule 2 actually cares about — the one where
    os.path.islink() cannot be trusted.
    """
    if sys.platform == "win32":
        result = subprocess.run(  # noqa: S603
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607
            capture_output=True,
            check=False,
        )
        return result.returncode == 0
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    return True


def test_plain_directory_is_not_a_reparse_point(tmp_path: Path) -> None:
    assert is_reparse_point(tmp_path) is False


def test_missing_path_is_not_a_reparse_point(tmp_path: Path) -> None:
    assert is_reparse_point(tmp_path / "nope") is False


def test_safe_rmtree_removes_a_plain_tree(tmp_path: Path) -> None:
    target = tmp_path / "victim"
    (target / "nested").mkdir(parents=True)
    (target / "nested" / "f.txt").write_text("x", encoding="utf-8")
    safe_rmtree(target)
    assert not target.exists()


def test_safe_rmtree_on_missing_path_is_a_noop(tmp_path: Path) -> None:
    safe_rmtree(tmp_path / "nope")


def test_junction_is_detected_as_a_reparse_point(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    if not _link_directory(link, real):
        pytest.skip("cannot create a directory link on this machine")
    assert is_reparse_point(link) is True


def test_safe_rmtree_refuses_a_linked_directory(tmp_path: Path) -> None:
    """The failure this prevents: rmtree walking the reparse point and deleting the
    target, taking the whole mod cache with it."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "keepme.txt").write_text("important", encoding="utf-8")
    link = tmp_path / "link"
    if not _link_directory(link, real):
        pytest.skip("cannot create a directory link on this machine")

    with pytest.raises(ModCacheError, match="reparse point"):
        safe_rmtree(link)
    assert (real / "keepme.txt").exists()


# --- promote_snapshot --------------------------------------------------------

WORKSHOP_ID = 1559212036
VERSION = 1721005200


def test_promote_copies_the_tree_into_a_version_directory(
    config: Config, downloaded_mod: Path
) -> None:
    state = State()
    dest = promote_snapshot(config, state, WORKSHOP_ID, VERSION)

    assert dest == config.version_dir(WORKSHOP_ID, VERSION)
    assert (dest / "addons" / "data.pbo").read_bytes() == b"pbo-contents"
    assert (dest / "keys" / "mod.bikey").exists()


def test_promote_records_the_version_in_state(config: Config, downloaded_mod: Path) -> None:
    state = State()
    promote_snapshot(config, state, WORKSHOP_ID, VERSION)

    assert state.known_versions(WORKSHOP_ID) == [VERSION]
    assert state.versions[WORKSHOP_ID][VERSION].size_bytes > 0


def test_promote_leaves_the_source_intact(config: Config, downloaded_mod: Path) -> None:
    """Tier 1 stays where it is — steamcmd owns that directory."""
    promote_snapshot(config, State(), WORKSHOP_ID, VERSION)
    assert (downloaded_mod / "addons" / "data.pbo").exists()


def test_promote_never_overwrites_an_existing_version(
    config: Config, downloaded_mod: Path
) -> None:
    """The core invariant: a version directory is written once and never again."""
    state = State()
    dest = promote_snapshot(config, state, WORKSHOP_ID, VERSION)
    (dest / "addons" / "data.pbo").write_bytes(b"in-use-by-a-running-server")

    again = promote_snapshot(config, state, WORKSHOP_ID, VERSION)

    assert again == dest
    assert (dest / "addons" / "data.pbo").read_bytes() == b"in-use-by-a-running-server"


def test_two_versions_of_one_mod_coexist(config: Config, downloaded_mod: Path) -> None:
    """The whole point: an unrestarted instance keeps the old version while a new one
    lands beside it."""
    state = State()
    old = promote_snapshot(config, state, WORKSHOP_ID, 1719878400)
    (downloaded_mod / "addons" / "data.pbo").write_bytes(b"updated-contents")
    new = promote_snapshot(config, state, WORKSHOP_ID, VERSION)

    assert old != new
    assert (old / "addons" / "data.pbo").read_bytes() == b"pbo-contents"
    assert (new / "addons" / "data.pbo").read_bytes() == b"updated-contents"
    assert state.known_versions(WORKSHOP_ID) == [VERSION, 1719878400]


def test_promote_raises_when_the_download_is_missing(config: Config) -> None:
    with pytest.raises(ModCacheError, match="does not exist"):
        promote_snapshot(config, State(), WORKSHOP_ID, VERSION)


def test_promote_leaves_no_staging_directory_behind(
    config: Config, downloaded_mod: Path
) -> None:
    promote_snapshot(config, State(), WORKSHOP_ID, VERSION)
    leftovers = [p.name for p in (config.modcache_dir / str(WORKSHOP_ID)).iterdir()]
    assert leftovers == [str(VERSION)]


def test_promote_with_hardlink_strategy_produces_readable_content(
    config: Config, downloaded_mod: Path
) -> None:
    """spec §12 — shipped off by default, so at minimum prove it does not corrupt."""
    linked = config.model_copy(update={"snapshot_strategy": "hardlink"})
    dest = promote_snapshot(linked, State(), WORKSHOP_ID, VERSION)
    assert (dest / "addons" / "data.pbo").read_bytes() == b"pbo-contents"
