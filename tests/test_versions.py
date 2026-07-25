"""Version tracking and explicit deletion.

The governing rule: nothing deletes a cached version unless a human asked for that exact
version. These tests exist mostly to prove the refusals hold, because the failure they
prevent — removing files a running server has open, or a version an instance still needs
— is the expensive kind.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dzsm.config import Config, InstanceConfig, WorkshopMod
from dzsm.mods import ModCacheError, delete_version, version_status
from dzsm.state import State

WORKSHOP_ID = 1559212036
OLD = 1719878400
NEW = 1721005200


@pytest.fixture
def cfg(config: Config) -> Config:
    return config.model_copy(
        update={
            "mods": {"CF": WorkshopMod(workshop_id=WORKSHOP_ID)},
            "instances": {"chernarus": InstanceConfig(port=2302, mods=["CF"])},
        }
    )


def _cached(cfg: Config, state: State, version: int, size: int = 1024) -> Path:
    path = cfg.version_dir(WORKSHOP_ID, version)
    path.mkdir(parents=True)
    (path / "mod.pbo").write_bytes(b"x" * size)
    state.record_version(WORKSHOP_ID, version, size)
    return path


# --- reporting ---------------------------------------------------------------


def test_no_cache_reports_an_empty_list(cfg: Config) -> None:
    assert version_status(cfg, State()) == []


def test_reports_size_name_and_latest_flag(cfg: Config) -> None:
    state = State()
    _cached(cfg, state, OLD)
    _cached(cfg, state, NEW)

    statuses = version_status(cfg, state)

    assert [s.version for s in statuses] == [NEW, OLD]
    assert statuses[0].is_latest is True
    assert statuses[1].is_latest is False
    assert all(s.mod_name == "CF" for s in statuses)


def test_reports_which_instances_pin_a_version(cfg: Config) -> None:
    state = State()
    _cached(cfg, state, OLD)
    _cached(cfg, state, NEW)
    state.pins["chernarus"] = {"CF": OLD}

    by_version = {s.version: s for s in version_status(cfg, state)}

    assert by_version[OLD].pinned_by == ["chernarus"]
    assert by_version[OLD].in_use is True
    assert by_version[NEW].in_use is False


def test_reports_a_version_missing_from_disk(cfg: Config) -> None:
    """State can outlive the directory — say so rather than pretending it is fine."""
    state = State()
    state.record_version(WORKSHOP_ID, NEW, 1024)

    assert version_status(cfg, state)[0].exists is False


def test_reporting_deletes_nothing(cfg: Config) -> None:
    state = State()
    path = _cached(cfg, state, OLD)

    version_status(cfg, state)

    assert path.is_dir()


# --- explicit deletion -------------------------------------------------------


def test_deletes_an_unused_version(cfg: Config) -> None:
    state = State()
    path = _cached(cfg, state, OLD)
    _cached(cfg, state, NEW)

    delete_version(cfg, state, WORKSHOP_ID, OLD)

    assert not path.exists()
    assert state.known_versions(WORKSHOP_ID) == [NEW]


def test_refuses_a_version_an_instance_is_pinned_to(cfg: Config) -> None:
    state = State()
    path = _cached(cfg, state, OLD)
    state.pins["chernarus"] = {"CF": OLD}

    with pytest.raises(ModCacheError, match="pinned to it"):
        delete_version(cfg, state, WORKSHOP_ID, OLD)

    assert path.is_dir()


def test_force_overrides_a_stale_pin(cfg: Config) -> None:
    state = State()
    path = _cached(cfg, state, OLD)
    state.pins["chernarus"] = {"CF": OLD}

    delete_version(cfg, state, WORKSHOP_ID, OLD, force=True)

    assert not path.exists()
    # The pin is cleared too, so state stops claiming a version that is gone.
    assert "CF" not in state.pins["chernarus"]


def test_force_does_not_override_a_running_instance(cfg: Config) -> None:
    """A running DayZ server holds OS-level locks on these files. This is the constraint
    the whole design exists to respect, so force must not punch through it."""
    state = State()
    path = _cached(cfg, state, OLD)
    state.pins["chernarus"] = {"CF": OLD}

    with pytest.raises(ModCacheError, match="running instance"):
        delete_version(
            cfg, state, WORKSHOP_ID, OLD, running_instances=["chernarus"], force=True
        )

    assert path.is_dir()


def test_a_running_instance_pinned_elsewhere_does_not_block(cfg: Config) -> None:
    """Only a pin on *this* version matters, not merely that something is running."""
    state = State()
    old_path = _cached(cfg, state, OLD)
    _cached(cfg, state, NEW)
    state.pins["chernarus"] = {"CF": NEW}

    delete_version(cfg, state, WORKSHOP_ID, OLD, running_instances=["chernarus"])

    assert not old_path.exists()


def test_deleting_a_record_with_no_directory_clears_state(cfg: Config) -> None:
    state = State()
    state.record_version(WORKSHOP_ID, OLD, 1024)

    delete_version(cfg, state, WORKSHOP_ID, OLD)

    assert state.known_versions(WORKSHOP_ID) == []


def test_deletion_leaves_other_versions_untouched(cfg: Config) -> None:
    state = State()
    _cached(cfg, state, OLD)
    kept = _cached(cfg, state, NEW)

    delete_version(cfg, state, WORKSHOP_ID, OLD)

    assert (kept / "mod.pbo").exists()
