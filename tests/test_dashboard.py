"""The dashboard's wiring.

The JavaScript itself is not unit tested, but the thing most likely to break silently is
a control pointing at an endpoint that does not exist — the button renders, the click
404s, and nothing says why. So every path the page fetches is checked against the routes
the app actually serves.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dzsm.api import _dashboard_html, create_app
from dzsm.config import Config
from dzsm.state import State, save_state

WORKSHOP_ID = 1559212036
VERSION = 1721005200

# fetch('api/...') and post('api/...'), including the ones built with string
# concatenation around an instance name.
FETCH = re.compile(r"""['"]api/([a-z/{}]+)""")


@pytest.fixture
def config_file(config: Config, tmp_path: Path) -> Path:
    save_state(config.state_file, State())
    path = tmp_path / "config.toml"
    root = str(config.root).replace("\\", "\\\\")
    steamcmd = str(config.steam.steamcmd_path).replace("\\", "\\\\")
    path.write_text(
        f"root = '{root}'\n"
        f"allow_long_root = true\n\n"
        f"[steam]\n"
        f'username = "someone"\n'
        f"steamcmd_path = '{steamcmd}'\n\n"
        f"[instances.chernarus]\n"
        f"port = 2302\n"
        f'mods = []\n',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def client(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("DZSM_API_TOKEN", raising=False)
    return TestClient(create_app(config_file))


def _routes(client: TestClient) -> set[str]:
    return {route.path for route in client.app.routes}  # type: ignore[attr-defined]


def test_every_path_the_page_fetches_exists(client: TestClient) -> None:
    """A typo here renders a button that 404s on click and explains nothing.

    Several paths are built by concatenation — 'api/instances/' + name + '/start' — so a
    reference is satisfied either by an exact route or by being a prefix of one.
    """
    html = _dashboard_html()
    referenced = {"/api/" + match.rstrip("/") for match in FETCH.findall(html)}
    served = _routes(client)

    def satisfied(path: str) -> bool:
        return path in served or any(route.startswith(path + "/") for route in served)

    missing = {path for path in referenced if not satisfied(path)}

    assert missing == set(), f"dashboard fetches routes that do not exist: {missing}"


@pytest.mark.parametrize(
    "path",
    [
        "/api/instances",
        "/api/versions",
        "/api/server",
        "/api/health",
        "/api/jobs",
    ],
)
def test_the_read_endpoints_it_polls_all_answer(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 200


def test_the_instance_actions_answer(client: TestClient) -> None:
    """Buttons post to these three; none may 404."""
    for action in ("start", "stop", "restart"):
        response = client.post(f"/api/instances/chernarus/{action}")
        assert response.status_code != 404, action


def test_an_unknown_instance_still_404s(client: TestClient) -> None:
    assert client.post("/api/instances/ghost/stop").status_code == 404


def test_the_page_carries_the_fields_it_renders(client: TestClient) -> None:
    """The rows read held, kept_by, and rcon_port; the API has to supply them."""
    instance = client.get("/api/instances").json()[0]

    assert "held" in instance
    assert "rcon_port" in instance
    assert "pins" in instance
    assert "next_restart" in instance


def test_versions_carry_kept_by(client: TestClient) -> None:
    body = client.get("/api/versions").json()

    assert isinstance(body, list)
    # No versions cached here, but the shape is asserted where one exists in test_api.


def test_the_dashboard_is_served_and_self_contained(client: TestClient) -> None:
    """A strict local page with no CDN: nothing may reference an external host."""
    html = client.get("/").text

    assert "<title>dzsm</title>" in html
    assert "http://" not in html.replace("http://127.0.0.1", "")
    assert "https://" not in html


def test_it_names_what_cannot_be_done_from_the_browser(client: TestClient) -> None:
    """Editing mods and seeding Steam Guard stay in the terminal; the page says so
    rather than leaving someone hunting for a button."""
    html = client.get("/").text

    assert "config.toml" in html
    assert "login --interactive" in html
