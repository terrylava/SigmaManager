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

from dzsm import settings, steam
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


def _without_comments(html: str) -> str:
    """Drop JS line comments.

    The file explains the inline-handler bug in prose, quoting the broken markup. Checking
    the raw text would match that explanation and fail on the comment describing the fix.
    """
    return "\n".join(
        line for line in html.splitlines() if not line.lstrip().startswith("//")
    )


def test_every_button_names_a_handler_that_exists() -> None:
    """The other half of the dead-button problem: a control wired to a missing action.

    Delegation looks the name up in `actions`, so a typo produces a button that renders,
    clicks, and does nothing — the same silent failure inline handlers had. Checked
    statically because there is no runtime to catch it in.
    """
    html = _dashboard_html()
    body = re.search(r"const actions = \{(.*?)\n\};", html, re.DOTALL)
    assert body, "could not find the actions object"
    defined = set(re.findall(r"^  (\w+):", body.group(1), re.MULTILINE))
    used = set(re.findall(r'data-(?:act|change|enter)="(\w+)"', html))

    assert used - defined == set(), f"controls reference undefined actions: {used - defined}"


def test_dynamic_values_reach_handlers_as_attributes() -> None:
    """No handler argument may be built into markup as code.

    `onclick="f(' + JSON.stringify(name) + ')"` renders `onclick="f("CF")"`, which the
    HTML parser truncates at the second quote. Values go through attr(), which escapes
    them, and come back via dataset.
    """
    code = _without_comments(_dashboard_html())

    assert "JSON.stringify(name)" not in code
    assert "onclick" not in code


@pytest.mark.parametrize(
    "path",
    [
        "/api/instances",
        "/api/config/global",
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
    assert "steam_query_port" in instance
    assert "steam_query_port_pin" in instance
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
    """Seeding Steam Guard stays in the terminal; the page says so rather than leaving
    someone hunting for a button.

    Mods and settings used to be listed here too. They are editable from the browser now,
    so naming them would send people to the file for something the page can do.
    """
    html = client.get("/").text

    assert "login --interactive" in html


def test_the_page_has_no_inline_event_handlers(client: TestClient) -> None:
    """Every handler is delegated, and every dynamic value goes into a data-* attribute.

    Inline handlers are built by string concatenation, and a value containing a quote —
    which JSON.stringify emits for every string — terminates the attribute early and
    leaves a syntax error the browser reports nowhere. That silently killed two buttons.
    Delegation makes the failure mode impossible, so the absence is worth asserting.
    """
    code = _without_comments(client.get("/").text)

    assert not re.search(r"\bon(click|change|input|keydown)\s*=", code), (
        "inline event handler in dashboard.html; use data-act/data-change/data-enter"
    )


GLOBAL_FIELD_KEY = re.compile(r"\{k: '([a-z_.]+)', t: '(num|bool|text)'")


def _global_fields() -> list[tuple[str, str]]:
    """The manager-settings fields the page renders, as (key, type)."""
    block = _dashboard_html().split("const GLOBAL_FIELDS = [")[1].split("\n];")[0]
    return GLOBAL_FIELD_KEY.findall(block)


def test_every_manager_setting_on_the_page_is_editable() -> None:
    """A field the page renders but the server refuses breaks the *whole* panel, not
    just itself.

    Save sends only what changed — but a key missing from EDITABLE_GLOBAL_FIELDS is
    missing from global_values too, so it renders blank or unticked, never equals the
    value it is compared against, and is therefore in every patch. One absent key means
    every Save is rejected, whatever the person actually edited. That shipped: three
    game-update fields were on the page and not in the set, so the panel could not save
    at all.
    """
    missing = {key for key, _ in _global_fields()} - settings.EDITABLE_GLOBAL_FIELDS

    assert not missing, f"rendered but not editable, so every Save is refused: {missing}"


def test_every_manager_setting_is_reported_back(config: Config) -> None:
    """The other direction: a value the page cannot read renders as a blank box, which
    then looks like an instruction to clear the setting."""
    values = settings.global_values(config)

    assert {key for key, _ in _global_fields()} <= set(values)


def test_the_page_renders_every_editable_setting() -> None:
    """The reverse gap is milder but still a dead end: a setting the API accepts and
    nothing on the page exposes can only be changed by hand in config.toml."""
    rendered = {key for key, _ in _global_fields()}
    # Deliberately not on the page: root-adjacent plumbing nobody should click.
    hidden = {"allow_long_root", "snapshot_strategy", "server_appid", "api.host",
              "api.port", "rcon.host", "rcon.timeout_seconds"}

    assert settings.EDITABLE_GLOBAL_FIELDS - rendered == hidden


def test_the_ports_panel_checks_the_launcher_range() -> None:
    """The default query port (game port +1) is outside 27015-27019 on purpose, so the
    page has to say so rather than leaving a server quietly missing from the vanilla
    launcher. Structural, not cosmetic: the panel must consult the resolved port."""
    code = _without_comments(_dashboard_html())

    assert "const VANILLA_QUERY_RANGE = [27015, 27019];" in code
    assert "launcherNote(i.steam_query_port)" in code


SEARCH_SORT = re.compile(r"\['([a-z]+)', '[^']+'\]")


def test_the_sort_options_match_the_rankings_the_api_accepts() -> None:
    """The page hardcodes the list because it is static HTML, so the only thing stopping
    the two drifting is this test. A stale name renders a dropdown entry that 422s."""
    block = _dashboard_html().split("const SEARCH_SORTS = [")[1].split("];")[0]

    assert set(SEARCH_SORT.findall(block)) == set(steam.RANKINGS)


def test_the_category_filters_match_the_tags_steam_knows() -> None:
    """Guessing this vocabulary does not work — "Weapons" and "Buildings" both match
    nothing — so a typo here is a checkbox that silently returns no results."""
    block = _dashboard_html().split("const SEARCH_TAGS = [")[1].split("];")[0]
    rendered = set(re.findall(r"'([A-Za-z ]+)'", block))

    assert rendered == set(steam.WORKSHOP_TAGS)


def test_typing_schedules_a_search_rather_than_sending_one() -> None:
    """Ten keystrokes must be one request, not ten. Checked live: typing "helicopter"
    sent exactly one."""
    code = _without_comments(_dashboard_html())

    assert "scheduleSearch(e.target.value)" in code
    assert "clearTimeout(searchTimer)" in code
    assert "SEARCH_DEBOUNCE_MS" in code


def test_the_search_button_beats_a_pending_keystroke() -> None:
    """Otherwise pressing Search races the timer it was meant to pre-empt."""
    code = _without_comments(_dashboard_html())
    handler = code.split("search: ()")[1].split(",\n")[0]

    assert "clearTimeout(searchTimer)" in handler


def test_a_stale_response_cannot_overwrite_a_newer_one() -> None:
    """Searches issued while typing do not have to come back in order: a slow "zom"
    landing after a fast "zombie" would replace the right results with older ones."""
    code = _without_comments(_dashboard_html())

    assert "const seq = ++searchSeq;" in code
    assert code.count("if (seq !== searchSeq) return;") >= 3
