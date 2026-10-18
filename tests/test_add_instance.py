"""Adding a server.

Two things make this more than writing a TOML table. A DayZ instance occupies a *block*
of UDP ports, not one, so "is this port free" is not the question it looks like. And the
instance name is not a label — it becomes a directory and part of the mission name.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dzsm import api, settings
from dzsm.config import ConfigError, load_config

CONFIG = """\
root = '{root}'
allow_long_root = true

[steam]
username = "someone"
steamcmd_path = '{root}/steamcmd/steamcmd.exe'

[mods.CF]
kind = "workshop"
workshop_id = 1559212036

[mods.Expansion]
kind = "workshop"
workshop_id = 2116157322

[instances.chernarus]
port = 2402
mods = ["CF", "Expansion"]
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(root=tmp_path.as_posix()), encoding="utf-8")
    return path


@pytest.fixture
def client(config_file: Path) -> TestClient:
    return TestClient(api.create_app(config_file))


def _add(path: Path, name: str, **kwargs: object) -> object:
    document = settings.read_document(path)
    settings.add_instance(document, name, **kwargs)  # type: ignore[arg-type]
    return settings.save(path, document)


# --- the port block ----------------------------------------------------------


def test_an_instance_claims_four_ports(config_file: Path) -> None:
    """The game binds a three-wide span, BattlEye RCON sits at +3, and the Steam query
    port goes at +1 — which the game leaves free. See tests/test_query_port.py."""
    config = load_config(config_file)

    assert config.instances["chernarus"].claimed_ports() == {2402, 2403, 2404, 2405}


def test_overlapping_blocks_are_refused(tmp_path: Path) -> None:
    """2402 and 2404 pass a naive "same port?" check and then collide on query and RCON.
    The symptom is the second server failing to bind, or RCON reaching the wrong one."""
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(root=tmp_path.as_posix()) + "[instances.b]\nport = 2404\nmods = []\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="both claim UDP port"):
        load_config(path)


def test_the_refusal_names_both_blocks(tmp_path: Path) -> None:
    """So the fix is obvious rather than requiring someone to know the layout."""
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(root=tmp_path.as_posix()) + "[instances.b]\nport = 2404\nmods = []\n",
        encoding="utf-8",
    )

    message = str(pytest.raises(ConfigError, load_config, path).value)

    assert "2402, 2403, 2404, 2405" in message
    assert "2404, 2405, 2406, 2407" in message


def test_four_apart_is_accepted(tmp_path: Path) -> None:
    """The minimum spacing that does not collide."""
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(root=tmp_path.as_posix()) + "[instances.b]\nport = 2406\nmods = []\n",
        encoding="utf-8",
    )

    assert load_config(path).instances["b"].port == 2406


def test_an_explicit_rcon_port_is_also_reserved(tmp_path: Path) -> None:
    """Pointing RCON somewhere else does not make that port free for another instance."""
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(root=tmp_path.as_posix())
        + "[instances.b]\nport = 2500\nmods = []\nrcon_port = 2403\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="both claim UDP port 2403"):
        load_config(path)


def test_a_disabled_rcon_port_is_not_reserved(tmp_path: Path) -> None:
    """An instance that opted out of RCON is not holding a port for it."""
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(root=tmp_path.as_posix())
        + "[instances.b]\nport = 2500\nmods = []\nrcon_enabled = false\n",
        encoding="utf-8",
    )

    assert load_config(path).instances["b"].claimed_ports() == {2500, 2501, 2502}


# --- suggesting a port -------------------------------------------------------


def test_the_suggested_port_clears_every_existing_block(config_file: Path) -> None:
    assert settings.suggest_port(load_config(config_file)) == 2502


def test_the_suggestion_steps_up_rather_than_filling_a_gap(tmp_path: Path) -> None:
    """A gap is usually deliberate — 2302 left alone because something else on the box
    uses it — and quietly reusing it is how a new server collides with something dzsm
    cannot see."""
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(root=tmp_path.as_posix()) + "[instances.b]\nport = 2602\nmods = []\n",
        encoding="utf-8",
    )

    assert settings.suggest_port(load_config(path)) == 2702


def test_the_first_instance_gets_the_conventional_port(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    root = tmp_path.as_posix()
    path.write_text(
        f"root = '{root}'\nallow_long_root = true\n[steam]\nusername = 'x'\n"
        f"steamcmd_path = '{root}/s.exe'\n",
        encoding="utf-8",
    )

    assert settings.suggest_port(load_config(path)) == 2302


# --- the name ----------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["my.server", "my server", "../escape", "a/b", "a\\b", "", "sakhal!"],
)
def test_an_unusable_name_is_refused(config_file: Path, name: str) -> None:
    """It becomes a folder under instances/ and part of the derived mission name, where a
    dot breaks DayZ's "<MissionName>.<TerrainName>" parsing and a space breaks the launch
    arguments."""
    with pytest.raises(settings.SettingsError):
        _add(config_file, name, port=2502)


@pytest.mark.parametrize("name", ["livonia", "pvp_hardcore", "test-2", "Sakhal99"])
def test_an_ordinary_name_is_accepted(config_file: Path, name: str) -> None:
    config = _add(config_file, name, port=2502)

    assert name in config.instances  # type: ignore[attr-defined]


def test_a_duplicate_name_is_refused(config_file: Path) -> None:
    with pytest.raises(settings.SettingsError, match="already exists"):
        _add(config_file, "chernarus", port=2502)


# --- what gets written -------------------------------------------------------


def test_the_new_table_labels_itself(config_file: Path) -> None:
    """tomlkit appends it after whatever section comments follow the last instance, so
    without the label it reads as whatever that comment described."""
    _add(config_file, "livonia", port=2502)

    assert "[instances.livonia] # added by dzsm" in config_file.read_text(encoding="utf-8")


def test_comments_survive(config_file: Path) -> None:
    config_file.write_text(
        "# keep me\n" + config_file.read_text(encoding="utf-8"), encoding="utf-8"
    )

    _add(config_file, "livonia", port=2502)

    assert "# keep me" in config_file.read_text(encoding="utf-8")


def test_an_unknown_mod_is_refused(config_file: Path) -> None:
    with pytest.raises(settings.SettingsError, match="unknown mod"):
        _add(config_file, "livonia", port=2502, mods=["NoSuchMod"])


def test_the_load_order_is_written_in_order(config_file: Path) -> None:
    """The order is the load order. Writing it sorted would break every framework
    dependency in it."""
    config = _add(config_file, "livonia", port=2502, mods=["Expansion", "CF"])

    assert config.instances["livonia"].mods == ["Expansion", "CF"]  # type: ignore[attr-defined]


def test_only_what_was_asked_for_is_written(config_file: Path) -> None:
    """A new instance littered with explicit copies of every default is harder to read
    and freezes those defaults forever."""
    _add(config_file, "livonia", port=2502)
    text = config_file.read_text(encoding="utf-8").split("[instances.livonia]")[1]

    assert "dologs" not in text
    assert "rcon_enabled" not in text


# --- the API -----------------------------------------------------------------


def test_the_api_creates_a_server(client: TestClient) -> None:
    response = client.post("/api/instances", json={"name": "livonia"})

    assert response.status_code == 201
    assert response.json()["port"] == 2502
    assert response.json()["claimed_ports"] == [2502, 2503, 2504, 2505]


def test_the_api_copies_a_load_order_in_order(client: TestClient) -> None:
    """The usual reason for a second server: the same mods on a different map."""
    response = client.post(
        "/api/instances", json={"name": "livonia", "copy_mods_from": "chernarus"}
    )

    assert response.json()["mods"] == ["CF", "Expansion"]


def test_copying_from_a_missing_server_404s(client: TestClient) -> None:
    response = client.post(
        "/api/instances", json={"name": "livonia", "copy_mods_from": "ghost"}
    )

    assert response.status_code == 404


def test_a_colliding_port_is_refused_by_the_api(client: TestClient) -> None:
    response = client.post("/api/instances", json={"name": "clash", "port": 2404})

    assert response.status_code == 422
    assert "2404" in response.json()["detail"]


def test_the_defaults_endpoint_prefills_the_form(client: TestClient) -> None:
    body = client.get("/api/instances/new").json()

    assert body["port"] == 2502
    assert body["mods"] == ["CF", "Expansion"]
    assert body["taken_ports"] == [2402, 2403, 2404, 2405]
    assert body["instances"]["chernarus"] == ["CF", "Expansion"]


def test_creating_a_server_needs_the_token(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file.write_text(
        config_file.read_text(encoding="utf-8")
        + '\n[api]\ntoken_env = "DZSM_ADD_TOKEN"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DZSM_ADD_TOKEN", "secret")  # noqa: S105 — a test fixture
    guarded = TestClient(api.create_app(config_file))

    assert guarded.post("/api/instances", json={"name": "livonia"}).status_code == 401
