"""The Steam query port.

Measured on DayZ 1.29 with netstat, two servers side by side: `-port=2402` binds 2402,
2404, 2405 and `-port=2502` binds 2502, 2504, 2505. The query port is in neither block —
it defaults to 27016 for whichever server started first and 27017 for the next, so two
instances swap query ports simply by restarting in a different order. Both the in-game
browser and DZSA list servers *by* query port, so firewall rules and players' saved
favourites follow the wrong server when that happens.

dzsm therefore pins it: game port +1, which the engine leaves free and the four-wide port
block already reserves. Also measured — `steamQueryPort = 2403;` on a 2402 server bound
2403 and dropped 27016 entirely.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dzsm import api, settings
from dzsm.config import Config, ConfigError, InstanceConfig, load_config
from dzsm.instance import prepare_server_config

CONFIG = """\
root = '{root}'
allow_long_root = true

[steam]
username = "someone"
steamcmd_path = '{root}/steamcmd/steamcmd.exe'

[instances.chernarus]
port = 2402
mods = []
"""


def _write(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(root=tmp_path.as_posix()) + extra, encoding="utf-8")
    return path


# --- the default -------------------------------------------------------------


def test_the_default_is_derived_not_left_to_the_engine(tmp_path: Path) -> None:
    """Unset means game port +1 — an explicit number, not 27016-in-start-order."""
    config = load_config(_write(tmp_path))

    assert config.instances["chernarus"].steam_query_port is None
    assert config.instances["chernarus"].resolved_steam_query_port() == 2403


def test_the_default_is_inside_the_reserved_block(tmp_path: Path) -> None:
    """Which is why no extra collision maths is needed for it: +1 was already reserved,
    and the engine does not bind it."""
    config = load_config(_write(tmp_path))
    instance = config.instances["chernarus"]

    assert instance.resolved_steam_query_port() in instance.claimed_ports()
    assert instance.claimed_ports() == {2402, 2403, 2404, 2405}


def test_an_explicit_port_wins(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, "steam_query_port = 27031\n"))

    assert config.instances["chernarus"].resolved_steam_query_port() == 27031


def test_an_explicit_port_joins_the_claimed_block(tmp_path: Path) -> None:
    """Moving it out of the block has to move the *reservation* too, or the next
    instance's port could be placed on top of it."""
    config = load_config(_write(tmp_path, "steam_query_port = 27031\n"))

    assert config.instances["chernarus"].claimed_ports() == {2402, 2403, 2404, 2405, 27031}


# --- collisions --------------------------------------------------------------


def test_two_instances_cannot_share_a_query_port(tmp_path: Path) -> None:
    """The failure this whole thing exists to prevent: one query port, two servers, so
    the browser lists one of them and whichever it is depends on boot order."""
    path = _write(
        tmp_path,
        "steam_query_port = 27016\n\n"
        "[instances.livonia]\nport = 2502\nmods = []\nsteam_query_port = 27016\n",
    )

    with pytest.raises(ConfigError) as exc:
        load_config(path)

    message = str(exc.value)
    assert "27016" in message
    assert "chernarus" in message and "livonia" in message
    # Named as a query port on *both* sides, so the message says what to change rather
    # than leaving someone to work out which setting produced the number.
    assert "uses it as its Steam query port" in message
    assert "wants it as its Steam query port" in message


def test_a_query_port_cannot_land_on_another_game_port(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "steam_query_port = 2502\n\n[instances.livonia]\nport = 2502\nmods = []\n",
    )

    with pytest.raises(ConfigError) as exc:
        load_config(path)

    message = str(exc.value)
    assert "2502" in message
    assert "Steam query port" in message
    assert "game port" in message


def test_a_query_port_cannot_land_on_another_rcon_port(tmp_path: Path) -> None:
    """RCON is the worst one to collide with: admin commands reaching the wrong server."""
    path = _write(
        tmp_path,
        "steam_query_port = 2505\n\n[instances.livonia]\nport = 2502\nmods = []\n",
    )

    with pytest.raises(ConfigError) as exc:
        load_config(path)

    assert "BattlEye RCON port" in str(exc.value)


def test_a_query_port_cannot_land_on_another_master_port(tmp_path: Path) -> None:
    """+2 is bound by the game and belongs to nobody in particular, so the message has to
    say which port of the other instance's block it is rather than go blank."""
    path = _write(
        tmp_path,
        "steam_query_port = 2504\n\n[instances.livonia]\nport = 2502\nmods = []\n",
    )

    with pytest.raises(ConfigError) as exc:
        load_config(path)

    assert "game port +2" in str(exc.value)


def test_an_instance_may_pin_its_own_derived_port(tmp_path: Path) -> None:
    """Writing out the value it would have derived is not a collision with itself."""
    config = load_config(_write(tmp_path, "steam_query_port = 2403\n"))

    assert config.instances["chernarus"].resolved_steam_query_port() == 2403


# --- what the engine is handed -----------------------------------------------


@pytest.fixture
def cfg(config: Config) -> Config:
    return config.model_copy(
        update={"instances": {"chernarus": InstanceConfig(port=2402, mods=[])}}
    )


@pytest.fixture
def editable(cfg: Config) -> Path:
    path = cfg.instance_dir("chernarus") / "serverDZ.cfg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('hostname = "mine";\n', encoding="utf-8")
    return path


def test_the_port_is_written_into_the_generated_config(cfg: Config, editable: Path) -> None:
    active = prepare_server_config(cfg, "chernarus")

    assert "steamQueryPort = 2403;" in active.read_text(encoding="utf-8")


def test_the_editable_config_is_left_alone(cfg: Config, editable: Path) -> None:
    """dzsm owns this key only in the file it generates. The one people edit keeps its
    own contents — that is what makes taking a second key acceptable at all."""
    prepare_server_config(cfg, "chernarus")

    assert editable.read_text(encoding="utf-8") == 'hostname = "mine";\n'


def test_a_hand_written_value_is_overridden(cfg: Config, editable: Path) -> None:
    """config.toml wins, because it is the value port validation can see. Deferring to
    the file would let two instances share a port config load swore was unique."""
    editable.write_text('hostname = "mine";\nsteamQueryPort = 27016;\n', encoding="utf-8")

    active = prepare_server_config(cfg, "chernarus")
    text = active.read_text(encoding="utf-8")

    assert "steamQueryPort = 2403;" in text
    assert "27016" not in text


def test_overriding_a_hand_written_value_is_logged(
    cfg: Config, editable: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Silently ignoring what someone wrote in a file is how they spend an evening
    wondering why their edit does nothing (Rule 3)."""
    editable.write_text('steamQueryPort = 27016;\n', encoding="utf-8")

    with caplog.at_level("WARNING"):
        prepare_server_config(cfg, "chernarus")

    assert "chernarus" in caplog.text
    assert "27016" in caplog.text and "2403" in caplog.text


def test_a_matching_hand_written_value_is_not_logged(
    cfg: Config, editable: Path, caplog: pytest.LogCaptureFixture
) -> None:
    editable.write_text("steamQueryPort = 2403;\n", encoding="utf-8")

    with caplog.at_level("WARNING"):
        prepare_server_config(cfg, "chernarus")

    assert "steamQueryPort" not in caplog.text


def test_the_key_is_not_duplicated(cfg: Config, editable: Path) -> None:
    """A second steamQueryPort line leaves it to the engine which one it honours."""
    editable.write_text("steamQueryPort=27016;\n", encoding="utf-8")

    active = prepare_server_config(cfg, "chernarus")

    assert active.read_text(encoding="utf-8").count("steamQueryPort") == 1


def test_a_commented_out_line_is_not_mistaken_for_the_setting(
    cfg: Config, editable: Path
) -> None:
    """The stock serverDZ.cfg is mostly commented-out examples. Substituting into one
    would leave the real setting unwritten while looking like it had worked."""
    editable.write_text("// steamQueryPort = 27016;\n", encoding="utf-8")

    text = prepare_server_config(cfg, "chernarus").read_text(encoding="utf-8")

    assert "// steamQueryPort = 27016;" in text
    assert "\nsteamQueryPort = 2403;" in text


def test_an_explicit_port_reaches_the_engine(config: Config) -> None:
    cfg = config.model_copy(
        update={
            "instances": {
                "chernarus": InstanceConfig(port=2402, mods=[], steam_query_port=27031)
            }
        }
    )
    path = cfg.instance_dir("chernarus") / "serverDZ.cfg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('hostname = "mine";\n', encoding="utf-8")

    active = prepare_server_config(cfg, "chernarus")

    assert "steamQueryPort = 27031;" in active.read_text(encoding="utf-8")


# --- editing it ---------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(api.create_app(_write(tmp_path)))


def test_the_api_reports_both_the_resolved_port_and_the_pin(client: TestClient) -> None:
    """The form needs the raw field: "derived" and "pinned to the same number by hand"
    look identical otherwise, and saving would quietly turn one into the other."""
    instance = client.get("/api/instances").json()[0]

    assert instance["steam_query_port"] == 2403
    assert instance["steam_query_port_pin"] is None


def test_it_can_be_pinned_through_the_api(client: TestClient) -> None:
    response = client.patch(
        "/api/instances/chernarus/settings", json={"steam_query_port": 27031}
    )

    assert response.status_code == 200
    instance = client.get("/api/instances").json()[0]
    assert instance["steam_query_port"] == 27031
    assert instance["steam_query_port_pin"] == 27031


def test_clearing_it_restores_the_derived_port(client: TestClient) -> None:
    client.patch("/api/instances/chernarus/settings", json={"steam_query_port": 27031})

    client.patch("/api/instances/chernarus/settings", json={"steam_query_port": None})

    instance = client.get("/api/instances").json()[0]
    assert instance["steam_query_port"] == 2403
    assert instance["steam_query_port_pin"] is None


def test_a_colliding_edit_is_refused(tmp_path: Path) -> None:
    """Validated by loading it as a real Config before it touches disk, so the collision
    check applies to a web edit exactly as it does to a hand-written file."""
    path = _write(tmp_path, "\n[instances.livonia]\nport = 2502\nmods = []\n")
    client = TestClient(api.create_app(path))

    response = client.patch(
        "/api/instances/chernarus/settings", json={"steam_query_port": 2502}
    )

    assert response.status_code == 422
    assert "2502" in response.json()["detail"]
    assert load_config(path).instances["chernarus"].steam_query_port is None


def test_a_new_server_is_told_its_query_port(tmp_path: Path) -> None:
    client = TestClient(api.create_app(_write(tmp_path)))

    created = client.post("/api/instances", json={"name": "livonia", "mods": []}).json()

    assert created["steam_query_port"] == created["port"] + 1


def test_the_suggested_port_block_includes_query_ports(tmp_path: Path) -> None:
    """So the add-server form can warn before the save rather than after it."""
    client = TestClient(api.create_app(_write(tmp_path, "steam_query_port = 27031\n")))

    taken = client.get("/api/instances/new").json()["taken_ports"]

    assert 27031 in taken


def test_the_field_is_editable_from_the_ui(tmp_path: Path) -> None:
    """A whitelist miss is a form that saves nothing and says it worked."""
    assert "steam_query_port" in settings.EDITABLE_INSTANCE_FIELDS
