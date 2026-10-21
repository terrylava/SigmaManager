"""Workshop search and adding a mod to the config.

Steam's own page cannot be embedded — it sends frame-ancestors headers, and a
cross-origin frame could not be read from anyway — so the browser is our own UI over
their Web API. No network here: search goes through httpx.MockTransport.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import tomlkit

from dzsm import settings
from dzsm.config import load_config
from dzsm.steam import SteamError, get_workshop_item, search_workshop

KEY = "abc123"  # noqa: S105 — a test fixture

CONFIG = """\
root = 'D:\\dztest'

[steam]
username = "someone"
steamcmd_path = 'D:\\dztest\\steamcmd\\steamcmd.exe'

[mods.CF]
kind = "workshop"
workshop_id = 1559212036

[instances.chernarus]
port = 2302
mods = ["CF"]
"""


def _hit(workshop_id: int, title: str) -> dict[str, object]:
    return {
        "result": 1,
        "publishedfileid": str(workshop_id),
        "title": title,
        "short_description": "does things",
        "preview_url": "https://example.invalid/preview.jpg",
        "time_updated": 1771519119,
        "subscriptions": 4242,
        "file_size": "1048576",
    }


def _client(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


# --- search ------------------------------------------------------------------


def test_a_search_returns_items_and_a_total() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"response": {"total": 57, "publishedfiledetails": [_hit(1, "Alpha")]}}
        )

    with _client(handler) as client:
        items, total = search_workshop(KEY, "alpha", client=client)

    assert total == 57
    assert items[0].title == "Alpha"
    assert items[0].workshop_id == 1
    assert items[0].subscriptions == 4242
    assert items[0].file_size == 1048576


def test_the_dayz_appid_and_a_text_query_are_sent() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"response": {"total": 0, "publishedfiledetails": []}})

    with _client(handler) as client:
        search_workshop(KEY, "trader", client=client)

    assert seen["appid"] == "221100"
    assert seen["search_text"] == "trader"
    assert seen["key"] == KEY


def test_an_empty_query_does_not_fetch_the_whole_workshop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("should not have called Steam")

    with _client(handler) as client:
        assert search_workshop(KEY, "   ", client=client) == ([], 0)


def test_no_api_key_says_where_to_get_one() -> None:
    """This is the first thing anyone opening the browser without a key will see."""
    with pytest.raises(SteamError, match="steamcommunity.com/dev/apikey"):
        search_workshop(None, "trader")


def test_a_rejected_key_says_so_rather_than_a_bare_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    with _client(handler) as client, pytest.raises(SteamError, match="rejected the Web API key"):
        search_workshop(KEY, "trader", client=client)


def test_a_missing_preview_is_not_fatal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        entry = _hit(1, "Alpha")
        del entry["preview_url"]
        return httpx.Response(200, json={"response": {"total": 1, "publishedfiledetails": [entry]}})

    with _client(handler) as client:
        items, _ = search_workshop(KEY, "alpha", client=client)

    assert items[0].preview_url is None


def test_an_untitled_item_falls_back_to_its_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        entry = _hit(99, "")
        return httpx.Response(200, json={"response": {"total": 1, "publishedfiledetails": [entry]}})

    with _client(handler) as client:
        items, _ = search_workshop(KEY, "x", client=client)

    assert "99" in items[0].title


# --- lookup by id (no key) ---------------------------------------------------


def test_a_lookup_needs_no_key() -> None:
    """So pasting a Workshop link works for people who never made one."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": {"publishedfiledetails": [_hit(7, "Beta")]}})

    with _client(handler) as client:
        item = get_workshop_item(7, client=client)

    assert item is not None
    assert item.title == "Beta"


def test_a_missing_item_is_none_not_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        missing = {"publishedfileid": "7", "result": 9}
        return httpx.Response(200, json={"response": {"publishedfiledetails": [missing]}})

    with _client(handler) as client:
        assert get_workshop_item(7, client=client) is None


# --- naming ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Community Framework", "Community_Framework"),
        ("DayZ-Expansion-Core", "DayZ-Expansion-Core"),
        ("Trader  ✅ v1.2", "Trader_v1_2"),
        ("[BETA] Cars!", "BETA_Cars"),
        ("", "mod_42"),
        ("🔥🔥🔥", "mod_42"),
        ("2000 Guns", "m2000_Guns"),
    ],
)
def test_a_config_name_is_derived_from_the_title(title: str, expected: str) -> None:
    """Workshop titles are full of emoji, brackets and version stamps; none of that
    belongs in a name people type into a mods list."""
    assert settings.mod_config_name(title, set(), 42) == expected


def test_a_clashing_name_gets_the_id_appended() -> None:
    """Several Workshop mods genuinely share a title."""
    assert settings.mod_config_name("Trader", {"Trader"}, 42) == "Trader_42"


# --- adding to the config ----------------------------------------------------


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


def test_a_mod_is_added(config_file: Path) -> None:
    document = settings.read_document(config_file)
    settings.add_workshop_mod(document, "Trader", 99)
    settings.save(config_file, document)

    loaded = load_config(config_file)
    assert loaded.mods["Trader"].workshop_id == 99  # type: ignore[union-attr]


def test_adding_to_an_instance_appends_to_the_load_order(config_file: Path) -> None:
    """Appended, never inserted: dzsm cannot know where a new mod belongs relative to
    the frameworks already listed."""
    document = settings.read_document(config_file)
    settings.add_workshop_mod(document, "Trader", 99)
    settings.add_mod_to_instance(document, "chernarus", "Trader")
    settings.save(config_file, document)

    assert load_config(config_file).instances["chernarus"].mods == ["CF", "Trader"]


def test_adding_twice_to_an_instance_is_idempotent(config_file: Path) -> None:
    document = settings.read_document(config_file)
    settings.add_mod_to_instance(document, "chernarus", "CF")
    settings.save(config_file, document)

    assert load_config(config_file).instances["chernarus"].mods == ["CF"]


def test_a_duplicate_name_is_refused(config_file: Path) -> None:
    document = settings.read_document(config_file)

    with pytest.raises(settings.SettingsError, match="already configured"):
        settings.add_workshop_mod(document, "CF", 999)


def test_a_duplicate_workshop_id_is_refused_by_name(config_file: Path) -> None:
    """Adding the same mod twice under two names would download it twice and pin it
    inconsistently."""
    document = settings.read_document(config_file)

    with pytest.raises(settings.SettingsError, match="already configured as 'CF'"):
        settings.add_workshop_mod(document, "CommunityFramework", 1559212036)


def test_adding_to_an_unknown_instance_is_refused(config_file: Path) -> None:
    document = settings.read_document(config_file)
    settings.add_workshop_mod(document, "Trader", 99)

    with pytest.raises(settings.SettingsError, match="no instance"):
        settings.add_mod_to_instance(document, "ghost", "Trader")


def test_comments_survive_adding_a_mod(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("# keep me\n" + CONFIG, encoding="utf-8")

    document = settings.read_document(path)
    settings.add_workshop_mod(document, "Trader", 99)
    settings.save(path, document)

    assert "# keep me" in path.read_text(encoding="utf-8")


def test_a_mod_added_to_no_instance_is_still_configured(config_file: Path) -> None:
    """Adding to the config without loading it anywhere is a real thing to want."""
    document = settings.read_document(config_file)
    settings.add_workshop_mod(document, "Trader", 99)
    settings.save(config_file, document)

    loaded = load_config(config_file)
    assert "Trader" in loaded.mods
    assert loaded.instances["chernarus"].mods == ["CF"]


def test_an_added_table_does_not_absorb_the_comment_above_it(tmp_path: Path) -> None:
    """tomlkit appends after any section comments that follow the last mod, so without a
    label the new entry looks like whatever that comment was describing."""
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG + "\n# ----------\n# Instances.\n# ----------\n\n"
        "[instances.second]\nport = 2500\nmods = []\n",
        encoding="utf-8",
    )

    document = settings.read_document(path)
    settings.add_workshop_mod(document, "Trader", 99)
    settings.save(path, document)
    text = path.read_text(encoding="utf-8")

    assert "[mods.Trader] # added by dzsm" in text
    # And it must not butt straight against the table that follows it.
    assert "\n\n[instances.second]" in text
    assert load_config(path).instances["second"].port == 2500


def test_the_added_entry_is_a_workshop_mod(config_file: Path) -> None:
    document = settings.read_document(config_file)
    settings.add_workshop_mod(document, "Trader", 99)
    settings.save(config_file, document)

    assert 'kind = "workshop"' in tomlkit.dumps(settings.read_document(config_file))


def test_results_are_ranked_by_the_search_text() -> None:
    """The ranking mode is the difference between a search box and a popularity chart.

    query_type is EPublishedFileQueryType. 12 is RankedByTextSearch. 9, which was here
    first under a comment claiming it was relevance, is RankedByTotalUniqueSubscriptions:
    the text only decides what matches, and among those the most subscribed wins. Live,
    that returned a base-building mod, an airdrop mod and a build-anywhere mod as the top
    three hits for "zombie", and pushed the exactly-titled "Hercul" to third for
    "hercul".
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json={"response": {"total": 0, "publishedfiledetails": []}})

    search_workshop(KEY, "zombie", client=_client(handler))

    assert seen["query_type"] == "12"


def test_the_ranking_mode_is_named_not_inlined() -> None:
    """So that changing it means stating which ranking you mean."""
    from dzsm.steam import TEXT_SEARCH_RANKING

    assert TEXT_SEARCH_RANKING == 12


# --- sort and category filters -----------------------------------------------


def test_a_sort_is_named_not_numbered() -> None:
    """Steam's enum number has no business in a URL or a <select>."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json={"response": {"total": 0, "publishedfiledetails": []}})

    search_workshop(KEY, "trader", ranking="updated", client=_client(handler))

    assert seen["query_type"] == "21"


def test_an_unknown_sort_is_refused() -> None:
    with pytest.raises(SteamError, match="unknown sort"):
        search_workshop(KEY, "trader", ranking="popularity")


def test_tags_go_out_as_an_indexed_array() -> None:
    """requiredtags is indexed in the query string, not a repeated key."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json={"response": {"total": 0, "publishedfiledetails": []}})

    search_workshop(KEY, "zombie", tags=("Character", "Sound"), client=_client(handler))

    assert seen["requiredtags[0]"] == "Character"
    assert seen["requiredtags[1]"] == "Sound"


def test_an_unknown_tag_is_refused_rather_than_returning_nothing() -> None:
    """Steam answers an unknown tag with zero results and no error, which reads as
    "nothing matched" rather than "that filter does not exist". "Weapons" and
    "Buildings" are the obvious guesses and both match nothing."""
    with pytest.raises(SteamError, match="unknown tag"):
        search_workshop(KEY, "zombie", tags=("Weapons",))


def test_a_category_with_no_text_is_a_browse() -> None:
    """Every Vehicle mod by Recently updated is how you find what was rebuilt for a new
    patch, and it needs no search text at all."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"response": {"total": 0, "publishedfiledetails": []}})

    search_workshop(KEY, "", tags=("Vehicle",), client=_client(handler))

    assert called


def test_an_empty_query_with_no_tags_still_fetches_nothing() -> None:
    """The guard that stops an empty box pulling down the whole Workshop."""
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not have been called")

    assert search_workshop(KEY, "   ", client=_client(handler)) == ([], 0)
