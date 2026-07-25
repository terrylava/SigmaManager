"""Missions surviving a server update.

Missions live under the server directory because the engine resolves them relative to it.
That makes a server update the dangerous moment: without carrying them forward, every
instance's economy silently reverts to vanilla and its persistence is orphaned in the old
build's directory, while the server comes up looking perfectly healthy.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from dzsm.config import Config, InstanceConfig
from dzsm.instance import prepare_mission, prepare_server_directory
from dzsm.state import State

OLD_BUILD = "24041098"
NEW_BUILD = "24555555"
VANILLA = "dayzOffline.chernarusplus"
EXE = "DayZServer_x64.exe" if sys.platform == "win32" else "DayZServer"


def _build(config: Config, build: str) -> Path:
    """A server build with the vanilla mission DayZ ships."""
    root = config.serverapp_dir / build
    (root / "mpmissions" / VANILLA / "db").mkdir(parents=True)
    (root / "mpmissions" / VANILLA / "db" / "types.xml").write_text(
        "<types>vanilla</types>", encoding="utf-8"
    )
    (root / EXE).write_bytes(b"MZ")
    (root / "serverDZ.cfg").write_text(f'template="{VANILLA}";\n', encoding="utf-8")
    return root


def _instance_config(config: Config, server: Path, name: str = "chernarus") -> Path:
    root = config.instance_dir(name)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / "serverDZ.cfg"
    destination.write_text(
        (server / "serverDZ.cfg").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return destination


@pytest.fixture
def cfg(config: Config) -> Config:
    return config.model_copy(
        update={"instances": {"chernarus": InstanceConfig(port=2302, mods=[])}}
    )


# --- naming ------------------------------------------------------------------


def test_the_derived_name_is_prefixed(cfg: Config) -> None:
    """Prefixed so dzsm-managed missions are obvious beside vanilla's dayzOffline.*."""
    server = _build(cfg, OLD_BUILD)
    _instance_config(cfg, server)

    assert prepare_mission(cfg, server, "chernarus") == "sigma_chernarus.chernarusplus"


def test_the_prefix_is_configurable(cfg: Config) -> None:
    renamed = cfg.model_copy(update={"mission_prefix": "acme"})
    server = _build(renamed, OLD_BUILD)
    _instance_config(renamed, server)

    assert prepare_mission(renamed, server, "chernarus") == "acme_chernarus.chernarusplus"


def test_the_instance_name_is_still_in_it(config: Config) -> None:
    """A bare prefix would put every instance back on one shared mission."""
    two = config.model_copy(
        update={
            "instances": {
                "a": InstanceConfig(port=2302, mods=[]),
                "b": InstanceConfig(port=2402, mods=[]),
            }
        }
    )
    server = _build(two, OLD_BUILD)
    for name in ("a", "b"):
        _instance_config(two, server, name)

    assert prepare_mission(two, server, "a") != prepare_mission(two, server, "b")


@pytest.mark.parametrize("prefix", ["has.dot", "has space", "has/slash", ""])
def test_an_unusable_prefix_is_rejected(config: Config, prefix: str) -> None:
    """A dot breaks '<MissionName>.<TerrainName>' parsing; a space breaks the arguments."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="mission_prefix"):
        config.model_validate({**config.model_dump(), "mission_prefix": prefix})


# --- carrying forward across a server update ---------------------------------


def test_a_tuned_economy_survives_a_server_update(cfg: Config) -> None:
    old = _build(cfg, OLD_BUILD)
    _instance_config(cfg, old)
    mission = prepare_mission(cfg, old, "chernarus")
    assert mission is not None

    types = old / "mpmissions" / mission / "db" / "types.xml"
    types.write_text("<types>hand-tuned</types>", encoding="utf-8")

    new = _build(cfg, NEW_BUILD)
    carried = prepare_mission(cfg, new, "chernarus")

    assert carried == mission
    assert (new / "mpmissions" / mission / "db" / "types.xml").read_text(
        encoding="utf-8"
    ) == "<types>hand-tuned</types>"


def test_persistence_comes_with_it(cfg: Config) -> None:
    """A server update must not wipe the map. This is the one case where storage_* is
    deliberately copied — it is the same instance's own saved world."""
    old = _build(cfg, OLD_BUILD)
    _instance_config(cfg, old)
    mission = prepare_mission(cfg, old, "chernarus")
    assert mission is not None
    storage = old / "mpmissions" / mission / "storage_1"
    storage.mkdir()
    (storage / "players.db").write_bytes(b"saved world")

    new = _build(cfg, NEW_BUILD)
    prepare_mission(cfg, new, "chernarus")

    assert (new / "mpmissions" / mission / "storage_1" / "players.db").read_bytes() == (
        b"saved world"
    )


def test_the_old_build_is_left_untouched(cfg: Config) -> None:
    """Carrying forward is a copy, so rolling back to the old build still works."""
    old = _build(cfg, OLD_BUILD)
    _instance_config(cfg, old)
    mission = prepare_mission(cfg, old, "chernarus")
    assert mission is not None

    new = _build(cfg, NEW_BUILD)
    prepare_mission(cfg, new, "chernarus")

    assert (old / "mpmissions" / mission).is_dir()


def test_a_first_ever_start_still_seeds_from_vanilla(cfg: Config) -> None:
    server = _build(cfg, OLD_BUILD)
    _instance_config(cfg, server)

    mission = prepare_mission(cfg, server, "chernarus")
    assert mission is not None

    assert (server / "mpmissions" / mission / "db" / "types.xml").read_text(
        encoding="utf-8"
    ) == "<types>vanilla</types>"


def test_a_fresh_seed_does_not_inherit_persistence(cfg: Config) -> None:
    """Unchanged: only a carry-forward brings storage_*, never a vanilla seed."""
    server = _build(cfg, OLD_BUILD)
    storage = server / "mpmissions" / VANILLA / "storage_1"
    storage.mkdir()
    (storage / "players.db").write_bytes(b"someone elses")
    _instance_config(cfg, server)

    mission = prepare_mission(cfg, server, "chernarus")
    assert mission is not None

    assert not (server / "mpmissions" / mission / "storage_1").exists()


def test_the_newest_previous_build_is_preferred(cfg: Config) -> None:
    """Several builds may hold a copy; the most recent is the live one."""
    first = _build(cfg, "1000")
    _instance_config(cfg, first)
    mission = prepare_mission(cfg, first, "chernarus")
    assert mission is not None
    (first / "mpmissions" / mission / "db" / "types.xml").write_text(
        "<types>oldest</types>", encoding="utf-8"
    )

    second = _build(cfg, "2000")
    prepare_mission(cfg, second, "chernarus")
    (second / "mpmissions" / mission / "db" / "types.xml").write_text(
        "<types>newest</types>", encoding="utf-8"
    )

    third = _build(cfg, "3000")
    prepare_mission(cfg, third, "chernarus")

    assert (third / "mpmissions" / mission / "db" / "types.xml").read_text(
        encoding="utf-8"
    ) == "<types>newest</types>"


# --- isolated instances ------------------------------------------------------


def test_an_isolated_instances_mission_survives_a_build_change(config: Config) -> None:
    """The copy is replaced wholesale on a build change, which would take the mission
    with it — and prepare_server_directory runs before prepare_mission could help."""
    cfg = config.model_copy(
        update={
            "instances": {
                "chernarus": InstanceConfig(port=2302, mods=[], isolated_server=True)
            }
        }
    )
    _build(cfg, OLD_BUILD)
    state = State()
    state.serverapp_version = OLD_BUILD

    server, _ = prepare_server_directory(cfg, state, "chernarus")
    _instance_config(cfg, server)
    mission = prepare_mission(cfg, server, "chernarus")
    assert mission is not None
    (server / "mpmissions" / mission / "db" / "types.xml").write_text(
        "<types>hand-tuned</types>", encoding="utf-8"
    )

    _build(cfg, NEW_BUILD)
    state.serverapp_version = NEW_BUILD
    server, build = prepare_server_directory(cfg, state, "chernarus")

    assert build == NEW_BUILD
    assert (server / "mpmissions" / mission / "db" / "types.xml").read_text(
        encoding="utf-8"
    ) == "<types>hand-tuned</types>"


def test_the_new_builds_vanilla_missions_win(config: Config) -> None:
    """Vanilla missions come back with the new build, so a map update is not pinned to
    an old build's files."""
    cfg = config.model_copy(
        update={
            "instances": {
                "chernarus": InstanceConfig(port=2302, mods=[], isolated_server=True)
            }
        }
    )
    _build(cfg, OLD_BUILD)
    state = State()
    state.serverapp_version = OLD_BUILD
    server, _ = prepare_server_directory(cfg, state, "chernarus")
    (server / "mpmissions" / VANILLA / "db" / "types.xml").write_text(
        "<types>stale</types>", encoding="utf-8"
    )

    _build(cfg, NEW_BUILD)
    state.serverapp_version = NEW_BUILD
    server, _ = prepare_server_directory(cfg, state, "chernarus")

    assert (server / "mpmissions" / VANILLA / "db" / "types.xml").read_text(
        encoding="utf-8"
    ) == "<types>vanilla</types>"


def test_no_stash_directory_is_left_behind(config: Config) -> None:
    cfg = config.model_copy(
        update={
            "instances": {
                "chernarus": InstanceConfig(port=2302, mods=[], isolated_server=True)
            }
        }
    )
    _build(cfg, OLD_BUILD)
    state = State()
    state.serverapp_version = OLD_BUILD
    server, _ = prepare_server_directory(cfg, state, "chernarus")
    (server / "mpmissions" / "custom.chernarusplus").mkdir()

    _build(cfg, NEW_BUILD)
    state.serverapp_version = NEW_BUILD
    prepare_server_directory(cfg, state, "chernarus")

    leftovers = [p.name for p in cfg.instance_dir("chernarus").iterdir() if p.name.startswith(".")]
    assert leftovers == []
