"""Reporting whether steamcmd's cached session still works.

dzsm cannot log in for you — `login_interactive` hands steamcmd the terminal so the
password and Guard code never pass through dzsm at all. What it can do is notice that the
sentry has gone stale and say so, instead of letting a download discover it at whatever
moment happens to need one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dzsm import api
from dzsm.state import State, load_state, save_state
from dzsm.steam import SteamError, SteamGuardRequired, SteamLoginFailed, _raise_for_output

TOKEN = "tok"  # noqa: S105 — a test fixture

CONFIG = """\
root = '{root}'
allow_long_root = true

[steam]
username = "someone"
steamcmd_path = '{root}/steamcmd/steamcmd.exe'

[api]
token_env = "DZSM_TEST_TOKEN"

[mods.CF]
kind = "workshop"
workshop_id = 1559212036

[instances.chernarus]
port = 2302
mods = ["CF"]
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(root=tmp_path.as_posix()), encoding="utf-8")
    return path


@pytest.fixture
def client(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DZSM_TEST_TOKEN", TOKEN)
    return TestClient(api.create_app(config_file))


def _auth() -> dict[str, str]:
    return {api.TOKEN_HEADER: TOKEN}


# --- what the panel reads ----------------------------------------------------


def test_a_fresh_install_is_unknown_not_broken(client: TestClient) -> None:
    """Nothing has exercised the session yet. Showing that as a failure would train
    people to ignore the one warning that matters."""
    body = client.get("/api/steam", headers=_auth()).json()

    assert body["ok"] is None
    assert body["checked_at"] is None


def test_it_names_the_account(client: TestClient) -> None:
    assert client.get("/api/steam", headers=_auth()).json()["username"] == "someone"


def test_it_carries_the_command_that_fixes_it(client: TestClient, config_file: Path) -> None:
    """A stale session should not turn into a hunt through the docs, so the command comes
    with this daemon's own config path already in it."""
    body = client.get("/api/steam", headers=_auth()).json()

    assert "login --interactive" in body["login_command"]
    assert str(config_file) in body["login_command"]


def test_it_reports_whether_a_password_is_stored(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.get("/api/steam", headers=_auth()).json()["password_env_set"] is False


def test_the_status_needs_the_token(client: TestClient) -> None:
    """It names the account and the steamcmd location — the same class of thing as the
    config file, which is gated."""
    assert client.get("/api/steam").status_code == 401


def test_it_reflects_a_recorded_failure(client: TestClient, config_file: Path) -> None:
    state = load_state(Path(config_file).parent / "state.json")
    state.record_steam_session(ok=False, detail="Steam Guard code required")
    save_state(Path(config_file).parent / "state.json", state)

    body = client.get("/api/steam", headers=_auth()).json()

    assert body["ok"] is False
    assert body["detail"] == "Steam Guard code required"
    assert body["checked_at"] is not None


# --- the explicit check ------------------------------------------------------


def test_verifying_records_success(
    client: TestClient, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "steam_login", lambda steam: None)

    client.post("/api/steam/verify", headers=_auth())

    assert client.get("/api/steam", headers=_auth()).json()["ok"] is True


def test_verifying_records_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(steam: object) -> None:
        raise SteamGuardRequired("login failed: steamcmd asked for a Steam Guard code\ntail")

    monkeypatch.setattr(api, "steam_login", refuse)

    client.post("/api/steam/verify", headers=_auth())
    body = client.get("/api/steam", headers=_auth()).json()

    assert body["ok"] is False
    # First line only: the raw message carries a multi-line steamcmd tail.
    assert "\n" not in body["detail"]


def test_verifying_needs_the_token(client: TestClient) -> None:
    assert client.post("/api/steam/verify").status_code == 401


# --- passive detection -------------------------------------------------------


def test_a_guard_prompt_during_a_download_marks_the_session_stale() -> None:
    """The valuable half: the 30-minute auto-check uses the Web API and never touches
    steamcmd, so only a download exercises the sentry."""
    state = State()

    api._note_steam_session(None, state, SteamGuardRequired("needs a code\ndetail"))  # type: ignore[arg-type]

    assert state.steam_session_ok is False


def test_an_ordinary_download_failure_does_not() -> None:
    """A download dies for a dozen reasons that say nothing about credentials — a mod
    pulled from the Workshop, a full disk. Reporting those as "log in again" would send
    people to fix the one thing that is not wrong."""
    state = State()
    state.record_steam_session(ok=True)

    api._note_steam_session(None, state, SteamError("disk full"))  # type: ignore[arg-type]

    assert state.steam_session_ok is True


def test_a_refused_sign_in_during_a_download_marks_the_session_stale() -> None:
    """The likelier of the two in practice: change the configured account and steamcmd
    has no sentry for the new name, so it reports a bad password rather than a Guard
    prompt. Missing this meant the panel stayed green while every download failed."""
    state = State()

    api._note_steam_session(None, state, SteamLoginFailed("Invalid Password\ntail"))  # type: ignore[arg-type]

    assert state.steam_session_ok is False


def test_a_credential_failure_is_classified_from_steamcmd_output() -> None:
    """Asserted on real steamcmd wording, since the classification is substring matching
    on output that only Valve controls."""
    output = (
        "Cached credentials not found.\n\npassword: \nProceeding with login using "
        "username/password.\nLogging in user 'someone' to Steam Public...ERROR "
        "(Invalid Password)\n"
    )

    with pytest.raises(SteamLoginFailed):
        _raise_for_output(output, context="login as someone")


def test_an_unrelated_steamcmd_error_is_not_a_credential_failure() -> None:
    with pytest.raises(SteamError) as caught:
        _raise_for_output("Disk write failure\n", context="download")

    assert not isinstance(caught.value, SteamLoginFailed)


def test_a_successful_download_counts_as_proof() -> None:
    """No reason to make someone press a button to learn what dzsm already knows."""
    state = State()

    api._note_steam_session(None, state, None)  # type: ignore[arg-type]

    assert state.steam_session_ok is True
    assert state.steam_session_checked_at is not None


def test_recording_clears_a_previous_failure() -> None:
    state = State()
    state.record_steam_session(ok=False, detail="needs a code")

    state.record_steam_session(ok=True)

    assert state.steam_session_ok is True
    assert state.steam_session_detail is None


def test_the_session_status_survives_a_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = State()
    state.record_steam_session(ok=False, detail="needs a code")
    save_state(path, state)

    reloaded = load_state(path)

    assert reloaded.steam_session_ok is False
    assert reloaded.steam_session_detail == "needs a code"
