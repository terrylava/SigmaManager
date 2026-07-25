"""Per-instance mission directories.

Mission folders hold types.xml, events.xml, cfgspawnabletypes.xml, init.c *and* the
persistence storage. Instances sharing one clobber each other's economy and saves, which
is the whole reason this is on by default. The realistic case is several servers sharing
a mod set but running different maps with their own loot tables.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from dzsm.config import Config, InstanceConfig
from dzsm.instance import InstanceError, prepare_mission, read_template, write_template

VANILLA = "dayzOffline.chernarusplus"
SAKHAL = "dayzOffline.sakhal"


@pytest.fixture
def server(config: Config, tmp_path: Path) -> Path:
    """A server directory with the vanilla missions DayZ ships."""
    root = tmp_path / "server"
    (root / "mpmissions").mkdir(parents=True)
    exe = root / ("DayZServer_x64.exe" if sys.platform == "win32" else "DayZServer")
    exe.write_bytes(b"MZ")
    (root / "serverDZ.cfg").write_text(
        'hostname = "EXAMPLE";\ninstanceId = 1;\ntemplate="' + VANILLA + '"; // comment\n',
        encoding="utf-8",
    )
    for name in (VANILLA, SAKHAL):
        mission = root / "mpmissions" / name
        (mission / "db").mkdir(parents=True)
        (mission / "db" / "types.xml").write_text("<types/>", encoding="utf-8")
        (mission / "init.c").write_text("void main() {}", encoding="utf-8")
    return root


def _cfg(config: Config, **instance_kwargs: str | None) -> Config:
    mission = instance_kwargs.get("mission")
    source = instance_kwargs.get("mission_source")
    instance = InstanceConfig(port=2302, mods=[], mission=mission, mission_source=source)
    return config.model_copy(update={"instances": {"chernarus": instance}})


def _seed_instance_config(config: Config, server: Path, name: str = "chernarus") -> Path:
    """ensure_layout normally does this; do it directly to keep these tests focused."""
    root = config.instance_dir(name)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / "serverDZ.cfg"
    destination.write_text(
        (server / "serverDZ.cfg").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return destination


# --- template parsing --------------------------------------------------------


def test_reads_the_template(server: Path) -> None:
    assert read_template(server / "serverDZ.cfg") == VANILLA


def test_missing_file_reads_as_none(tmp_path: Path) -> None:
    assert read_template(tmp_path / "nope.cfg") is None


def test_writing_the_template_preserves_the_rest(server: Path) -> None:
    path = server / "serverDZ.cfg"
    write_template(path, "pvp.chernarusplus")
    text = path.read_text(encoding="utf-8")

    assert read_template(path) == "pvp.chernarusplus"
    assert 'hostname = "EXAMPLE";' in text
    assert "instanceId = 1;" in text
    assert "// comment" in text, "the trailing comment should survive"


def test_writing_a_template_that_is_absent_appends_it(tmp_path: Path) -> None:
    path = tmp_path / "serverDZ.cfg"
    path.write_text('hostname = "x";\n', encoding="utf-8")

    write_template(path, "pvp.chernarusplus")

    assert read_template(path) == "pvp.chernarusplus"


# --- seeding -----------------------------------------------------------------


def test_each_instance_gets_its_own_mission(config: Config, server: Path) -> None:
    cfg = _cfg(config)
    _seed_instance_config(cfg, server)

    mission = prepare_mission(cfg, server, "chernarus")

    assert mission == "chernarus.chernarusplus"
    assert (server / "mpmissions" / mission / "init.c").is_file()


def test_the_instance_config_points_at_it(config: Config, server: Path) -> None:
    """Otherwise the instance silently runs the shared mission — the exact collision."""
    cfg = _cfg(config)
    instance_config = _seed_instance_config(cfg, server)

    prepare_mission(cfg, server, "chernarus")

    assert read_template(instance_config) == "chernarus.chernarusplus"


def test_the_source_mission_is_untouched(config: Config, server: Path) -> None:
    cfg = _cfg(config)
    _seed_instance_config(cfg, server)

    prepare_mission(cfg, server, "chernarus")
    (server / "mpmissions" / "chernarus.chernarusplus" / "db" / "types.xml").write_text(
        "<types>customised</types>", encoding="utf-8"
    )

    original = server / "mpmissions" / VANILLA / "db" / "types.xml"
    assert original.read_text(encoding="utf-8") == "<types/>"


def test_an_existing_mission_is_never_overwritten(config: Config, server: Path) -> None:
    """Once seeded it is the user's to edit — a restart must not revert their economy."""
    cfg = _cfg(config)
    _seed_instance_config(cfg, server)
    prepare_mission(cfg, server, "chernarus")

    types = server / "mpmissions" / "chernarus.chernarusplus" / "db" / "types.xml"
    types.write_text("<types>hand-tuned</types>", encoding="utf-8")
    prepare_mission(cfg, server, "chernarus")

    assert types.read_text(encoding="utf-8") == "<types>hand-tuned</types>"


def test_two_instances_do_not_share_a_mission(config: Config, server: Path) -> None:
    """The MapLink case: shared mods, different maps, separate types and events."""
    cfg = config.model_copy(
        update={
            "instances": {
                "chernarus": InstanceConfig(port=2302, mods=[]),
                "sakhal": InstanceConfig(port=2402, mods=[], mission_source=SAKHAL),
            }
        }
    )
    for name in ("chernarus", "sakhal"):
        _seed_instance_config(cfg, server, name)

    first = prepare_mission(cfg, server, "chernarus")
    second = prepare_mission(cfg, server, "sakhal")

    assert first == "chernarus.chernarusplus"
    assert second == "sakhal.sakhal"

    types = server / "mpmissions" / str(first) / "db" / "types.xml"
    types.write_text("<types>chernarus only</types>", encoding="utf-8")
    other = server / "mpmissions" / str(second) / "db" / "types.xml"
    assert other.read_text(encoding="utf-8") == "<types/>"


def test_persistence_is_not_copied(config: Config, server: Path) -> None:
    """storage_* is another server's saved world. A seeded mission starts empty."""
    storage = server / "mpmissions" / VANILLA / "storage_1"
    storage.mkdir()
    (storage / "players.db").write_bytes(b"someone elses save")
    cfg = _cfg(config)
    _seed_instance_config(cfg, server)

    prepare_mission(cfg, server, "chernarus")

    seeded = server / "mpmissions" / "chernarus.chernarusplus"
    assert (seeded / "init.c").is_file(), "the mission itself is still copied"
    assert not (seeded / "storage_1").exists()


def test_an_explicit_mission_name_is_honoured(config: Config, server: Path) -> None:
    cfg = _cfg(config, mission="hardcore.chernarusplus")
    _seed_instance_config(cfg, server)

    assert prepare_mission(cfg, server, "chernarus") == "hardcore.chernarusplus"


def test_the_terrain_comes_from_the_source(config: Config, server: Path) -> None:
    cfg = _cfg(config, mission_source=SAKHAL)
    _seed_instance_config(cfg, server)

    assert prepare_mission(cfg, server, "chernarus") == "chernarus.sakhal"


def test_a_missing_source_lists_what_exists(config: Config, server: Path) -> None:
    cfg = _cfg(config, mission_source="nope.chernarusplus")
    _seed_instance_config(cfg, server)

    with pytest.raises(InstanceError, match=VANILLA):
        prepare_mission(cfg, server, "chernarus")


def test_a_deleted_mission_is_reseeded(config: Config, server: Path) -> None:
    """After the first run the instance's template names its own mission. If that
    directory is deleted, resolution must fall back rather than deadlock on it."""
    cfg = _cfg(config)
    _seed_instance_config(cfg, server)
    mission = prepare_mission(cfg, server, "chernarus")
    assert mission is not None

    shutil.rmtree(server / "mpmissions" / mission)

    assert prepare_mission(cfg, server, "chernarus") == mission
    assert (server / "mpmissions" / mission / "init.c").is_file()


# --- opting out --------------------------------------------------------------


def test_can_be_disabled(config: Config, server: Path) -> None:
    cfg = _cfg(config).model_copy(update={"per_instance_missions": False})
    _seed_instance_config(cfg, server)

    assert prepare_mission(cfg, server, "chernarus") is None
    assert not (server / "mpmissions" / "chernarus.chernarusplus").exists()


def test_an_explicit_mission_still_applies_when_disabled(
    config: Config, server: Path
) -> None:
    """Turning the default off should not silently ignore an explicit request."""
    cfg = _cfg(config, mission="pvp.chernarusplus").model_copy(
        update={"per_instance_missions": False}
    )
    _seed_instance_config(cfg, server)

    assert prepare_mission(cfg, server, "chernarus") == "pvp.chernarusplus"
