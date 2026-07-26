"""Guards against the specific holes found in the security review.

Each test names the hole it closes. They are here rather than spread across the feature
suites because a security guard that quietly stops applying is worse than one that was
never written, and a named regression test is what makes its removal loud.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dzsm import api
from dzsm.config import Config, ConfigError, InstanceConfig, WorkshopMod, load_config
from dzsm.mods import ModCacheError, local_copy
from dzsm.state import State
from dzsm.steam import _redact

TOKEN = "s3cret-token"  # noqa: S105 — a test fixture, not a real credential

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
    (tmp_path / "instances" / "chernarus").mkdir(parents=True)
    (tmp_path / "instances" / "chernarus" / "serverDZ.cfg").write_text(
        'passwordAdmin = "hunter2";\n', encoding="utf-8"
    )
    return path


@pytest.fixture
def client(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DZSM_TEST_TOKEN", TOKEN)
    return TestClient(api.create_app(config_file))


def _auth() -> dict[str, str]:
    return {api.TOKEN_HEADER: TOKEN}


# --- reads that expose credentials or the machine's layout -------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/instances/chernarus/serverdz",
        "/api/config",
        "/api/config/global",
    ],
)
def test_sensitive_reads_require_the_token(client: TestClient, path: str) -> None:
    """These were open. serverDZ.cfg carries passwordAdmin and config.toml the whole
    layout, so on a network-bound deployment the token refused writes while handing the
    admin password to anyone who could reach the port."""
    assert client.get(path).status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/api/instances/chernarus/serverdz",
        "/api/config",
        "/api/config/global",
    ],
)
def test_sensitive_reads_work_with_the_token(client: TestClient, path: str) -> None:
    assert client.get(path, headers=_auth()).status_code == 200


def test_the_admin_password_is_not_served_unauthenticated(client: TestClient) -> None:
    """The concrete thing being protected, asserted on the response body."""
    assert "hunter2" not in client.get("/api/instances/chernarus/serverdz").text


def test_ordinary_reads_stay_open(client: TestClient) -> None:
    """The gate is on file contents and configuration, not on everything: mod names and
    job status are not secret, and closing them would break nothing but help nobody."""
    for path in ("/api/health", "/api/instances", "/api/mods", "/api/jobs"):
        assert client.get(path).status_code == 200, path


# --- cross-origin state change (CSRF) ----------------------------------------


def test_a_cross_origin_post_is_refused(client: TestClient) -> None:
    """A no-body POST is a CORS "simple request": any page could fire it with a form.
    Stopping an instance also sets `held`, so a drive-by page could keep servers down."""
    response = client.post(
        "/api/instances/chernarus/stop",
        headers={**_auth(), "Origin": "https://evil.example"},
    )

    assert response.status_code == 403


def test_a_cross_origin_post_is_refused_by_fetch_metadata(client: TestClient) -> None:
    """Origin can be absent on some requests; Sec-Fetch-Site is the second signal."""
    response = client.post(
        "/api/instances/chernarus/stop",
        headers={**_auth(), "Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403


def test_the_dashboards_own_requests_are_allowed(client: TestClient) -> None:
    """Same-origin is what the page itself sends, and it must keep working."""
    response = client.post(
        "/api/instances/chernarus/stop",
        headers={**_auth(), "Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )

    assert response.status_code != 403


def test_a_non_browser_client_is_allowed(client: TestClient) -> None:
    """curl and the CLI send neither header. Refusing those would make the API
    browser-only, which is a bigger loss than the hole is a risk."""
    assert client.post("/api/instances/chernarus/stop", headers=_auth()).status_code != 403


def test_cross_origin_reads_are_still_allowed(client: TestClient) -> None:
    """A GET changes nothing and its response is unreadable cross-origin anyway."""
    response = client.get("/api/health", headers={"Origin": "https://evil.example"})

    assert response.status_code == 200


def test_the_refusal_happens_without_a_token_too(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loopback-without-token default is exactly where this matters most: there is no
    token to stop the request, so the origin check is the only thing that does."""
    monkeypatch.delenv("DZSM_TEST_TOKEN", raising=False)
    unauthenticated = TestClient(api.create_app(config_file))

    response = unauthenticated.post(
        "/api/instances/chernarus/stop", headers={"Origin": "https://evil.example"}
    )

    assert response.status_code == 403


# --- path traversal ----------------------------------------------------------


@pytest.fixture
def cached(config: Config) -> Config:
    config.mods = {"CF": WorkshopMod(workshop_id=1559212036)}
    config.instances = {"chernarus": InstanceConfig(port=2302, mods=["CF"])}
    source = config.version_dir(1559212036, 100)
    (source / "addons").mkdir(parents=True)
    (source / "addons" / "a.pbo").write_bytes(b"x")
    return config


def _state() -> State:
    state = State()
    state.record_version(1559212036, 100)
    return state


@pytest.mark.parametrize(
    "name",
    [
        "../escaped",
        "..\\escaped",
        "../../escaped",
        "C:/Windows/Temp/escaped",
        "/tmp/escaped",  # noqa: S108 — the point is that it is refused
        "..",
        "",
        "sub/dir",
    ],
)
def test_a_local_copy_cannot_escape_localmods(cached: Config, name: str) -> None:
    """The name came from a request body and named a directory about to be written to.
    An absolute path is the worst case: pathlib's `base / "C:/x"` discards base."""
    with pytest.raises(ModCacheError):
        local_copy(cached, _state(), "CF", name)


def test_nothing_is_written_when_a_name_is_refused(cached: Config, tmp_path: Path) -> None:
    """The failure path deletes the destination, so an accepted bad name would have been
    an arbitrary delete as well as an arbitrary write."""
    outside = tmp_path.parent / "escaped"

    with pytest.raises(ModCacheError):
        local_copy(cached, _state(), "CF", f"../{outside.name}")

    assert not outside.exists()


def test_an_ordinary_name_still_works(cached: Config) -> None:
    path, _ = local_copy(cached, _state(), "CF", "CF_edited")

    assert path.parent == cached.localmods_dir


# --- settings that are joined onto managed directories -----------------------


@pytest.mark.parametrize(
    "field",
    ["server_executable", "config_file", "mission", "mission_source"],
)
@pytest.mark.parametrize("value", ["../../evil.exe", "C:/Windows/System32/calc.exe", ".."])
def test_path_like_instance_settings_are_refused(
    tmp_path: Path, field: str, value: str
) -> None:
    """server_executable is launched as a process and config_file is written to, both by
    joining onto a directory dzsm owns. Validated in the config model rather than at the
    API so a hand-edited file is covered too."""
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(root=tmp_path.as_posix()) + f'{field} = "{value}"\n', encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="plain file or directory name"):
        load_config(path)


def test_ordinary_values_for_those_settings_are_fine(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(root=tmp_path.as_posix()) + 'server_executable = "DayZDiag_x64.exe"\n',
        encoding="utf-8",
    )

    assert load_config(path).instances["chernarus"].server_executable == "DayZDiag_x64.exe"


# --- credentials in logs -----------------------------------------------------


def test_the_account_password_is_redacted() -> None:
    argv = ["steamcmd.exe", "+login", "user", "hunter2", "+quit"]

    assert "hunter2" not in _redact(argv, "hunter2")


def test_the_branch_password_is_redacted() -> None:
    """It is built from its own env var in update_server_app, so _redact was never handed
    it and matching on the account password alone left it in the debug log verbatim."""
    argv = ["steamcmd.exe", "+app_update", "223350", "-beta", "exp", "-betapassword", "brANCH"]

    assert "brANCH" not in _redact(argv, "account-password")


def test_redaction_keeps_the_rest_of_the_command_readable() -> None:
    """A log line with everything scrubbed is not worth writing."""
    redacted = _redact(["steamcmd.exe", "+app_update", "223350", "-betapassword", "x"], None)

    assert "+app_update 223350" in redacted
    assert redacted.endswith("-betapassword ***")
