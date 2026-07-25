"""Finding and removing mods nothing references.

Two distinct things get called "unused": a mod configured but listed by no instance, and
a cache directory for a mod that is no longer in config at all. The second is the one
that matters most — it is invisible to every other command, so nothing else would ever
surface it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dzsm.config import Config, InstanceConfig, LocalMod, WorkshopMod
from dzsm.mods import ModCacheError, remove_mod, unused_mods
from dzsm.state import State

USED_ID = 1559212036
UNUSED_ID = 2116157322
ORPHAN_ID = 1234567890
VERSION = 1721005200


@pytest.fixture
def cfg(config: Config, tmp_path: Path) -> Config:
    local = tmp_path / "localmods" / "@Handmade"
    local.mkdir(parents=True)
    (local / "notes.txt").write_bytes(b"hand-authored")
    return config.model_copy(
        update={
            "mods": {
                "CF": WorkshopMod(workshop_id=USED_ID),
                "Expansion": WorkshopMod(workshop_id=UNUSED_ID),
                "Handmade": LocalMod(path=local),
            },
            "instances": {"chernarus": InstanceConfig(port=2302, mods=["CF"])},
        }
    )


def _cached(cfg: Config, state: State | None, workshop_id: int, version: int) -> Path:
    path = cfg.version_dir(workshop_id, version)
    path.mkdir(parents=True)
    (path / "mod.pbo").write_bytes(b"x" * 2048)
    if state is not None:
        state.record_version(workshop_id, version, 2048)
    return path


# --- detection ---------------------------------------------------------------


def test_a_mod_an_instance_uses_is_not_unused(cfg: Config) -> None:
    state = State()
    _cached(cfg, state, USED_ID, VERSION)

    assert all(mod.mod_name != "CF" for mod in unused_mods(cfg, state))


def test_configured_mod_no_instance_lists_is_unreferenced(cfg: Config) -> None:
    state = State()
    _cached(cfg, state, UNUSED_ID, VERSION)

    found = {mod.mod_name: mod for mod in unused_mods(cfg, state)}

    assert found["Expansion"].kind == "unreferenced"
    assert found["Expansion"].versions == [VERSION]
    assert found["Expansion"].size_bytes > 0


def test_cache_for_a_mod_absent_from_config_is_orphaned(cfg: Config) -> None:
    """The case nothing else can see: config forgot it, the disk did not."""
    _cached(cfg, None, ORPHAN_ID, VERSION)

    orphans = [mod for mod in unused_mods(cfg, State()) if mod.kind == "orphaned"]

    assert len(orphans) == 1
    assert orphans[0].workshop_id == ORPHAN_ID
    assert orphans[0].mod_name is None


def test_local_mods_are_reported_but_not_deletable(cfg: Config) -> None:
    """The directory is hand-authored source dzsm never created (spec §9)."""
    found = {mod.mod_name: mod for mod in unused_mods(cfg, State())}

    assert found["Handmade"].kind == "local"
    assert found["Handmade"].deletable is False


def test_reports_a_lingering_pin(cfg: Config) -> None:
    """A mod dropped from an instance can still be pinned from its last start."""
    state = State()
    _cached(cfg, state, UNUSED_ID, VERSION)
    state.pins["chernarus"] = {"Expansion": VERSION}

    found = {mod.mod_name: mod for mod in unused_mods(cfg, state)}

    assert found["Expansion"].pinned_by == ["chernarus"]


def test_detection_deletes_nothing(cfg: Config) -> None:
    state = State()
    path = _cached(cfg, state, UNUSED_ID, VERSION)

    unused_mods(cfg, state)

    assert path.is_dir()


# --- removal -----------------------------------------------------------------


def test_removes_every_version_and_the_directory(cfg: Config) -> None:
    state = State()
    _cached(cfg, state, UNUSED_ID, VERSION)
    _cached(cfg, state, UNUSED_ID, VERSION + 1)

    remove_mod(cfg, state, UNUSED_ID)

    assert not (cfg.modcache_dir / str(UNUSED_ID)).exists()
    assert state.known_versions(UNUSED_ID) == []


def test_removes_versions_that_were_never_recorded(cfg: Config) -> None:
    """An interrupted run or a hand-copied directory would otherwise linger forever."""
    state = State()
    _cached(cfg, state, UNUSED_ID, VERSION)
    stray = _cached(cfg, None, UNUSED_ID, VERSION + 99)

    remove_mod(cfg, state, UNUSED_ID)

    assert not stray.exists()


def test_removal_leaves_other_mods_alone(cfg: Config) -> None:
    state = State()
    kept = _cached(cfg, state, USED_ID, VERSION)
    _cached(cfg, state, UNUSED_ID, VERSION)

    remove_mod(cfg, state, UNUSED_ID)

    assert (kept / "mod.pbo").exists()
    assert state.known_versions(USED_ID) == [VERSION]


def test_refuses_a_pinned_mod(cfg: Config) -> None:
    state = State()
    path = _cached(cfg, state, UNUSED_ID, VERSION)
    state.pins["chernarus"] = {"Expansion": VERSION}

    with pytest.raises(ModCacheError, match="pinned to it"):
        remove_mod(cfg, state, UNUSED_ID)

    assert path.is_dir()


def test_refuses_a_running_instance_even_with_force(cfg: Config) -> None:
    state = State()
    path = _cached(cfg, state, UNUSED_ID, VERSION)
    state.pins["chernarus"] = {"Expansion": VERSION}

    with pytest.raises(ModCacheError, match="running instance"):
        remove_mod(cfg, state, UNUSED_ID, running_instances=["chernarus"], force=True)

    assert path.is_dir()


def test_force_removes_a_stale_pin(cfg: Config) -> None:
    state = State()
    _cached(cfg, state, UNUSED_ID, VERSION)
    state.pins["chernarus"] = {"Expansion": VERSION}

    remove_mod(cfg, state, UNUSED_ID, force=True)

    assert not (cfg.modcache_dir / str(UNUSED_ID)).exists()


def test_removing_an_orphan_needs_no_config_entry(cfg: Config) -> None:
    _cached(cfg, None, ORPHAN_ID, VERSION)
    state = State()

    remove_mod(cfg, state, ORPHAN_ID)

    assert not (cfg.modcache_dir / str(ORPHAN_ID)).exists()
