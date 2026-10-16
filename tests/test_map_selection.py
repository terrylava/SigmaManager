"""Choosing which map a server runs.

The mission name decides the terrain: DayZ parses "<MissionName>.<TerrainName>" and loads
the world named by the suffix. So this is not a cosmetic setting — a suffix that disagrees
with the mission's contents gives a server that starts, reports healthy, and is running
the wrong world.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dzsm import api, instance
from dzsm.config import Config, ConfigError, InstanceConfig, load_config
from dzsm.state import State, save_state

VANILLA = ("dayzOffline.chernarusplus", "dayzOffline.enoch", "dayzOffline.sakhal")

CONFIG = """\
root = '{root}'
allow_long_root = true

[steam]
username = "someone"
steamcmd_path = '{root}/steamcmd/steamcmd.exe'

[instances.chernarus]
port = 2302
mods = []
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(root=tmp_path.as_posix()), encoding="utf-8")
    build = tmp_path / "serverapp" / "24041098"
    for mission in VANILLA:
        (build / "mpmissions" / mission).mkdir(parents=True)
    state = State()
    state.serverapp_version = "24041098"
    save_state(tmp_path / "state.json", state)
    return path


@pytest.fixture
def client(config_file: Path) -> TestClient:
    return TestClient(api.create_app(config_file))


# --- terrain from a mission name ---------------------------------------------


@pytest.mark.parametrize(
    ("mission", "terrain"),
    [
        ("dayzOffline.chernarusplus", "chernarusplus"),
        ("dayzOffline.enoch", "enoch"),
        ("sigma_pvp.sakhal", "sakhal"),
        ("something.with.dots", "with.dots"),
    ],
)
def test_the_terrain_is_the_suffix(mission: str, terrain: str) -> None:
    assert instance.mission_terrain(mission) == terrain


def test_a_mission_with_no_suffix_falls_back_to_the_default() -> None:
    assert instance.mission_terrain("justaname") == instance.DEFAULT_TERRAIN


# --- listing what a server can run -------------------------------------------


def test_the_available_missions_are_listed(config_file: Path) -> None:
    config = load_config(config_file)
    state = State(serverapp_version="24041098")

    assert instance.available_missions(config, state, "chernarus") == sorted(VANILLA)


def test_no_server_installed_lists_nothing_rather_than_raising(config: Config) -> None:
    """This populates a dropdown. A picker that cannot be drawn is worse than an empty
    one, so it returns a list — empty, never None (Rule 1)."""
    config.instances = {"a": InstanceConfig(port=2302, mods=[])}

    assert instance.available_missions(config, State(), "a") == []


def test_an_isolated_instance_reads_its_own_copy(tmp_path: Path) -> None:
    """Its missions live under instances/<name>/server/, and listing them must never
    trigger the multi-GB copy that preparing one would."""
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(root=tmp_path.as_posix()) + "isolated_server = true\n",
        encoding="utf-8",
    )
    build = tmp_path / "serverapp" / "1"
    (build / "mpmissions" / "dayzOffline.chernarusplus").mkdir(parents=True)
    own = tmp_path / "instances" / "chernarus" / "server" / "mpmissions"
    (own / "dayzOffline.sakhal").mkdir(parents=True)

    config = load_config(path)
    state = State(serverapp_version="1")

    assert instance.available_missions(config, state, "chernarus") == ["dayzOffline.sakhal"]


# --- the derived name --------------------------------------------------------


def test_the_derived_name_takes_its_terrain_from_the_source(config_file: Path) -> None:
    """Which is what keeps the suffix honest when someone switches map — the terrain comes
    from the mission being copied, not from anything typed."""
    config = load_config(config_file)

    name = instance.resolved_mission_name(config, "chernarus", "dayzOffline.enoch")

    assert name == "sigma_chernarus.enoch"


def test_the_instance_name_stays_in_the_derived_name(config_file: Path) -> None:
    """A bare prefix would put every instance back on one shared mission, which is the
    collision per-instance missions exist to prevent."""
    config = load_config(config_file)

    name = instance.resolved_mission_name(config, "chernarus", "dayzOffline.sakhal")

    assert "chernarus" in name


def test_an_explicit_mission_wins_over_the_derived_one(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(root=tmp_path.as_posix()) + 'mission = "pvp.enoch"\n',
        encoding="utf-8",
    )
    config = load_config(path)

    assert (
        instance.resolved_mission_name(config, "chernarus", "dayzOffline.enoch")
        == "pvp.enoch"
    )


# --- the guard against a contradictory pair ----------------------------------


def test_a_mission_whose_terrain_contradicts_its_source_is_refused(tmp_path: Path) -> None:
    """The engine loads the terrain named by the suffix, so this combination gives a
    directory full of Livonia files loaded as Chernarus: a server that starts, looks
    healthy, and is wrong."""
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(root=tmp_path.as_posix())
        + 'mission = "pvp.chernarusplus"\nmission_source = "dayzOffline.enoch"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="names terrain"):
        load_config(path)


def test_matching_suffixes_are_fine(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(root=tmp_path.as_posix())
        + 'mission = "pvp.enoch"\nmission_source = "dayzOffline.enoch"\n',
        encoding="utf-8",
    )

    assert load_config(path).instances["chernarus"].mission == "pvp.enoch"


def test_a_derived_mission_is_never_caught_by_the_guard(tmp_path: Path) -> None:
    """Only a hand-written name can contradict its source; the derived one takes its
    suffix from it."""
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(root=tmp_path.as_posix()) + 'mission_source = "dayzOffline.enoch"\n',
        encoding="utf-8",
    )

    assert load_config(path).instances["chernarus"].mission is None


# --- the API -----------------------------------------------------------------


def test_the_api_lists_the_maps_with_their_terrains(client: TestClient) -> None:
    body = client.get("/api/instances/chernarus/missions").json()

    terrains = {entry["name"]: entry["terrain"] for entry in body["available"]}
    assert terrains["dayzOffline.enoch"] == "enoch"
    assert len(body["available"]) == 3


def test_the_api_reports_what_it_will_run_as(client: TestClient) -> None:
    """The derived name is not obvious and is the thing that decides the terrain, so the
    UI shows it rather than making someone work it out."""
    body = client.get("/api/instances/chernarus/missions").json()

    assert body["resolved"] == "sigma_chernarus.chernarusplus"


def test_seeded_missions_are_flagged_and_not_offered_as_sources(client: TestClient) -> None:
    """A per-instance mission is somebody's edited world, not something to copy from."""
    body = client.get("/api/instances/chernarus/missions").json()

    assert all(entry["seeded"] is False for entry in body["available"])


def test_changing_the_map_is_a_settings_patch(client: TestClient) -> None:
    response = client.patch(
        "/api/instances/chernarus/settings",
        json={"mission_source": "dayzOffline.sakhal"},
    )
    assert response.status_code == 200

    body = client.get("/api/instances/chernarus/missions").json()
    assert body["mission_source"] == "dayzOffline.sakhal"
    assert body["resolved"] == "sigma_chernarus.sakhal"


def test_an_unknown_instance_404s(client: TestClient) -> None:
    assert client.get("/api/instances/ghost/missions").status_code == 404
