"""Editing an instance's serverDZ.cfg from the web UI.

The file is the engine's format, so dzsm does not parse it — which makes the editor a
plain text box, and puts the whole risk in the plumbing around it. Two things went wrong
there: the text was poked into the box with `.value` after render, so the next repaint
rebuilt the panel empty and the file appeared to vanish; and the save read the box at
click time, which meant saving the emptiness back over the file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dzsm import api
from dzsm.api import _dashboard_html

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

CFG_TEXT = """\
hostname = "Sigma";
password = "";
passwordAdmin = "hunter2";
template = "sigma_chernarus.chernarusplus";
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(root=tmp_path.as_posix()), encoding="utf-8")
    return path


@pytest.fixture
def client(config_file: Path) -> TestClient:
    return TestClient(api.create_app(config_file))


@pytest.fixture
def cfg(config_file: Path, tmp_path: Path) -> Path:
    path = tmp_path / "instances" / "chernarus" / "serverDZ.cfg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CFG_TEXT, encoding="utf-8")
    return path


# --- the page ----------------------------------------------------------------


def test_the_text_is_rendered_into_the_box() -> None:
    """Not assigned to it afterwards. A repaint rebuilds the panel from these strings, so
    anything only present in the DOM is gone four seconds later."""
    code = _dashboard_html()

    assert "id=\"cfgtext\" spellcheck=\"false\">" in code
    assert "esc(cfgText" in code


def test_nothing_assigns_the_box_contents_imperatively() -> None:
    """The bug itself: `getElementById('cfgtext').value = out.text`."""
    assert not re.search(r"""getElementById\(['"]cfgtext['"]\)\.value\s*=""", _dashboard_html())


def test_the_box_keeps_what_was_typed_across_a_repaint() -> None:
    """The input listener is what makes the held copy track the box; without it a repaint
    would restore the file as it was loaded and silently discard the edits."""
    code = _dashboard_html()

    assert "if (e.target.id === 'cfgtext') cfgText = e.target.value;" in code


def test_closing_the_editor_drops_the_held_text() -> None:
    """Otherwise the next server's editor opens showing the previous server's config."""
    code = _dashboard_html()

    assert "function closeCfg() { cfgEditing = null; cfgText = null; cfgPath = ''; }" in code
    assert code.count("closeCfg()") >= 4  # cancel, save, hashchange, and the definition


# --- reading -----------------------------------------------------------------


def test_the_file_is_returned_with_its_path(client: TestClient, cfg: Path) -> None:
    body = client.get("/api/instances/chernarus/serverdz").json()

    assert body["text"] == CFG_TEXT
    assert Path(body["path"]) == cfg


def test_a_missing_file_explains_itself(client: TestClient) -> None:
    """It is seeded at the first start, so before then there is nothing to edit."""
    response = client.get("/api/instances/chernarus/serverdz")

    assert response.status_code == 404
    assert "first time the instance starts" in response.json()["detail"]


def test_an_unknown_instance_is_refused(client: TestClient) -> None:
    assert client.get("/api/instances/ghost/serverdz").status_code == 404


# --- writing -----------------------------------------------------------------


def test_a_save_round_trips_exactly(client: TestClient, cfg: Path) -> None:
    """Not parsed, not reformatted: whatever the engine understands is allowed."""
    text = CFG_TEXT + 'motd[] = {"one", "two"};\n'

    assert client.put(
        "/api/instances/chernarus/serverdz", json={"text": text}
    ).status_code == 200
    assert cfg.read_text(encoding="utf-8") == text


def test_the_previous_version_is_kept(client: TestClient, cfg: Path) -> None:
    client.put("/api/instances/chernarus/serverdz", json={"text": "hostname = \"x\";\n"})

    assert cfg.with_suffix(".cfg.bak").read_text(encoding="utf-8") == CFG_TEXT


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_a_blank_file_is_refused(client: TestClient, cfg: Path, text: str) -> None:
    """An empty serverDZ.cfg is a server with no hostname, no mission template and no
    admin password — nobody means it, and it is exactly what the editor bug would have
    sent once the box had been emptied by a repaint."""
    response = client.put("/api/instances/chernarus/serverdz", json={"text": text})

    assert response.status_code == 422
    assert cfg.read_text(encoding="utf-8") == CFG_TEXT
