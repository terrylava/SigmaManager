"""Keeping a mod through a temporary disable.

A mod moved from `mods` to `keep_mods` stops being launched but keeps its claim: it is
not reported as unused and cleanup will not take it. The point is that re-enabling it is
moving one line back, not re-downloading several GB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from dzsm.config import Config, InstanceConfig, WorkshopMod
from dzsm.mods import (
    ModCacheError,
    delete_version,
    kept_by,
    prunable,
    remove_mod,
    unused_mods,
    version_status,
)
from dzsm.state import State

LOADED_ID = 1559212036
KEPT_ID = 2116157322
VERSION = 1721005200


@pytest.fixture
def cfg(config: Config) -> Config:
    """CF is loaded; Expansion is temporarily disabled but kept."""
    return config.model_copy(
        update={
            "mods": {
                "CF": WorkshopMod(workshop_id=LOADED_ID),
                "Expansion": WorkshopMod(workshop_id=KEPT_ID),
            },
            "instances": {
                "chernarus": InstanceConfig(
                    port=2302, mods=["CF"], keep_mods=["Expansion"]
                )
            },
        }
    )


def _cached(cfg: Config, state: State, workshop_id: int) -> Path:
    path = cfg.version_dir(workshop_id, VERSION)
    path.mkdir(parents=True)
    (path / "mod.pbo").write_bytes(b"x" * 2048)
    state.record_version(workshop_id, VERSION, 2048)
    return path


# --- config ------------------------------------------------------------------


def test_keep_mods_defaults_to_empty() -> None:
    assert InstanceConfig(mods=["CF"]).keep_mods == []


def test_keep_mods_must_reference_known_mods(config: Config) -> None:
    with pytest.raises(ValidationError, match="keep_mods references unknown mod"):
        config.model_validate(
            {
                **config.model_dump(),
                "mods": {"CF": {"kind": "workshop", "workshop_id": LOADED_ID}},
                "instances": {"a": {"mods": ["CF"], "keep_mods": ["Ghost"]}},
            }
        )


def test_a_mod_cannot_be_kept_and_loaded(config: Config) -> None:
    """keep_mods means disabled. Loading it too is contradictory."""
    with pytest.raises(ValidationError, match="keep_mods while also loading it"):
        config.model_validate(
            {
                **config.model_dump(),
                "mods": {"CF": {"kind": "workshop", "workshop_id": LOADED_ID}},
                "instances": {"a": {"mods": ["CF"], "keep_mods": ["CF"]}},
            }
        )


def test_a_server_mod_cannot_also_be_kept(config: Config) -> None:
    with pytest.raises(ValidationError, match="keep_mods while also loading it"):
        config.model_validate(
            {
                **config.model_dump(),
                "mods": {"CF": {"kind": "workshop", "workshop_id": LOADED_ID}},
                "instances": {"a": {"mods": [], "server_mods": ["CF"], "keep_mods": ["CF"]}},
            }
        )


def test_kept_by_maps_mods_to_instances(cfg: Config) -> None:
    assert kept_by(cfg) == {"Expansion": ["chernarus"]}


# --- effect on unused detection ----------------------------------------------


def test_a_kept_mod_is_not_reported_as_unused(cfg: Config) -> None:
    state = State()
    _cached(cfg, state, LOADED_ID)
    _cached(cfg, state, KEPT_ID)

    assert unused_mods(cfg, state) == []


def test_without_the_keep_it_would_be_unused(config: Config) -> None:
    """Same setup minus keep_mods — proving the keep is what protects it."""
    unkept = config.model_copy(
        update={
            "mods": {
                "CF": WorkshopMod(workshop_id=LOADED_ID),
                "Expansion": WorkshopMod(workshop_id=KEPT_ID),
            },
            "instances": {"chernarus": InstanceConfig(port=2302, mods=["CF"])},
        }
    )
    state = State()
    _cached(unkept, state, KEPT_ID)

    assert [m.mod_name for m in unused_mods(unkept, state)] == ["Expansion"]


def test_include_kept_surfaces_it_with_the_reason(cfg: Config) -> None:
    state = State()
    _cached(cfg, state, KEPT_ID)

    found = {m.mod_name: m for m in unused_mods(cfg, state, include_kept=True)}

    assert found["Expansion"].kind == "kept"
    assert found["Expansion"].kept_by == ["chernarus"]
    assert found["Expansion"].deletable is False


# --- effect on removal -------------------------------------------------------


def test_removing_a_kept_mod_is_refused(cfg: Config) -> None:
    state = State()
    path = _cached(cfg, state, KEPT_ID)

    with pytest.raises(ModCacheError, match="keep it"):
        remove_mod(cfg, state, KEPT_ID)

    assert path.is_dir()


def test_force_removes_a_kept_mod(cfg: Config) -> None:
    """Explicit override, for when the mod really is finished with."""
    state = State()
    _cached(cfg, state, KEPT_ID)

    remove_mod(cfg, state, KEPT_ID, force=True)

    assert not (cfg.modcache_dir / str(KEPT_ID)).exists()


def test_a_kept_version_is_reported_as_in_use(cfg: Config) -> None:
    """Otherwise `mods versions` offers to reclaim space that is actually protected."""
    state = State()
    _cached(cfg, state, KEPT_ID)

    status = next(s for s in version_status(cfg, state) if s.workshop_id == KEPT_ID)

    assert status.kept_by == ["chernarus"]
    assert status.in_use is True


def test_retention_never_prunes_a_kept_mod(cfg: Config) -> None:
    """A mod disabled for a fortnight must not be expired by the retention policy."""
    enabled = cfg.model_copy(update={"gc_retention_days": 1})
    state = State()
    _cached(enabled, state, KEPT_ID)
    _cached(enabled, state, LOADED_ID)
    for workshop_id in (KEPT_ID, LOADED_ID):
        record = state.versions[workshop_id][VERSION]
        record.downloaded_at = datetime.now(UTC) - timedelta(days=90)

    assert prunable(enabled, state) == []


def test_deleting_a_kept_mods_version_is_refused(cfg: Config) -> None:
    """The guard must cover per-version deletes too, not just whole-mod removal —
    otherwise `mods delete` walks straight around keep_mods and destroys the cache the
    keep exists to protect."""
    state = State()
    path = _cached(cfg, state, KEPT_ID)

    with pytest.raises(ModCacheError, match="keep this mod"):
        delete_version(cfg, state, KEPT_ID, VERSION)

    assert path.is_dir()
    assert state.known_versions(KEPT_ID) == [VERSION]


def test_force_deletes_a_kept_mods_version(cfg: Config) -> None:
    state = State()
    path = _cached(cfg, state, KEPT_ID)

    delete_version(cfg, state, KEPT_ID, VERSION, force=True)

    assert not path.exists()


def test_deleting_an_unkept_mods_version_still_works(cfg: Config) -> None:
    state = State()
    path = _cached(cfg, state, LOADED_ID)

    delete_version(cfg, state, LOADED_ID, VERSION)

    assert not path.exists()


def test_an_unkept_mod_is_still_removable(cfg: Config) -> None:
    state = State()
    _cached(cfg, state, LOADED_ID)

    remove_mod(cfg, state, LOADED_ID)

    assert not (cfg.modcache_dir / str(LOADED_ID)).exists()


def test_the_cache_survives_for_re_enabling(cfg: Config) -> None:
    """The whole point: flip keep_mods back to mods and the versions are still there."""
    state = State()
    cached = _cached(cfg, state, KEPT_ID)

    unused_mods(cfg, state)
    with pytest.raises(ModCacheError):
        remove_mod(cfg, state, KEPT_ID)

    reenabled = cfg.model_copy(
        update={
            "instances": {
                "chernarus": InstanceConfig(port=2302, mods=["CF", "Expansion"])
            }
        }
    )
    assert (cached / "mod.pbo").exists()
    assert state.latest_version(KEPT_ID) == VERSION
    assert kept_by(reenabled) == {}
