"""Web API.

The access model is the part worth testing hardest: reads open, writes gated when a
token is configured, and no route that can start or delete anything reachable without
one. Downloads are never exercised — those go through the same functions the CLI tests
already cover.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dzsm.api import TOKEN_HEADER, JobRegistry, create_app
from dzsm.cli import _is_loopback
from dzsm.config import Config
from dzsm.state import State, save_state

WORKSHOP_ID = 1559212036
VERSION = 1721005200
TOKEN = "s3cret-token"  # noqa: S105 — a test fixture, not a credential


@pytest.fixture
def config_file(config: Config, tmp_path: Path) -> Path:
    """A config on disk with one instance and one cached mod."""
    cached = config.version_dir(WORKSHOP_ID, VERSION)
    cached.mkdir(parents=True)
    (cached / "mod.pbo").write_bytes(b"x" * 4096)

    state = State()
    state.record_version(WORKSHOP_ID, VERSION, 4096)
    state.pins["chernarus"] = {"CF": VERSION}
    save_state(config.state_file, state)

    path = tmp_path / "config.toml"
    root = str(config.root).replace("\\", "\\\\")
    steamcmd = str(config.steam.steamcmd_path).replace("\\", "\\\\")
    path.write_text(
        f"root = '{root}'\n"
        f"allow_long_root = true\n\n"
        f"[steam]\n"
        f'username = "someone"\n'
        f"steamcmd_path = '{steamcmd}'\n\n"
        f"[mods.CF]\n"
        f'kind = "workshop"\n'
        f"workshop_id = {WORKSHOP_ID}\n\n"
        f"[instances.chernarus]\n"
        f"port = 2302\n"
        f'mods = ["CF"]\n',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def client(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Unauthenticated: no token in the environment."""
    monkeypatch.delenv("DZSM_API_TOKEN", raising=False)
    return TestClient(create_app(config_file))


@pytest.fixture
def secured(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DZSM_API_TOKEN", TOKEN)
    return TestClient(create_app(config_file))


# --- read routes -------------------------------------------------------------


def test_health(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["authenticated"] is False


def test_instances_report_state(client: TestClient) -> None:
    body = client.get("/api/instances").json()
    assert len(body) == 1
    assert body[0]["name"] == "chernarus"
    assert body[0]["running"] is False
    assert body[0]["pins"] == {"CF": VERSION}


def test_mods_list_cached_versions(client: TestClient) -> None:
    body = client.get("/api/mods").json()
    assert body[0]["workshop_id"] == WORKSHOP_ID
    assert body[0]["cached_versions"] == [VERSION]


def test_versions_report_usage(client: TestClient) -> None:
    body = client.get("/api/versions").json()
    assert body[0]["in_use"] is True
    assert body[0]["pinned_by"] == ["chernarus"]


def test_unused_endpoint(client: TestClient) -> None:
    assert client.get("/api/unused").json() == []


def test_server_builds(client: TestClient) -> None:
    body = client.get("/api/server").json()
    assert body["builds"] == []
    assert body["active"] is None


def test_dashboard_renders(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "dzsm" in response.text


def test_reads_are_open_even_when_a_token_is_set(secured: TestClient) -> None:
    """Reads disclose nothing secret, so a token is not required for them."""
    assert secured.get("/api/instances").status_code == 200
    assert secured.get("/api/health").json()["authenticated"] is True


# --- write gating ------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/instances/chernarus/start"),
        ("post", "/api/instances/chernarus/stop"),
        ("post", "/api/instances/chernarus/restart"),
        ("post", "/api/mods/check"),
        ("post", "/api/mods/update"),
        ("post", "/api/server/update"),
        ("delete", f"/api/versions/{WORKSHOP_ID}/{VERSION}"),
        ("delete", f"/api/mods/{WORKSHOP_ID}"),
    ],
)
def test_every_write_route_requires_the_token(
    secured: TestClient, method: str, path: str
) -> None:
    """The list is exhaustive on purpose: a new write route added without the dependency
    should fail this test rather than ship unauthenticated."""
    assert getattr(secured, method)(path).status_code == 401


def test_wrong_token_is_rejected(secured: TestClient) -> None:
    response = secured.post(
        "/api/instances/chernarus/stop", headers={TOKEN_HEADER: "wrong"}
    )
    assert response.status_code == 401


def test_correct_token_is_accepted(secured: TestClient) -> None:
    response = secured.post(
        "/api/instances/chernarus/stop", headers={TOKEN_HEADER: TOKEN}
    )
    assert response.status_code == 200


def test_bearer_token_also_works(secured: TestClient) -> None:
    response = secured.post(
        "/api/instances/chernarus/stop", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 200


def test_writes_are_open_when_no_token_is_configured(client: TestClient) -> None:
    """Loopback-only mode: `dzsm serve` refuses to bind elsewhere without a token."""
    assert client.post("/api/instances/chernarus/stop").status_code == 200


# --- behaviour ---------------------------------------------------------------


def test_unknown_instance_is_404(secured: TestClient) -> None:
    response = secured.post("/api/instances/ghost/stop", headers={TOKEN_HEADER: TOKEN})
    assert response.status_code == 404


def test_deleting_a_pinned_version_is_refused(secured: TestClient) -> None:
    """The CLI's refusals apply here identically — the API is not a way around them."""
    response = secured.delete(
        f"/api/versions/{WORKSHOP_ID}/{VERSION}", headers={TOKEN_HEADER: TOKEN}
    )
    assert response.status_code == 409
    assert "pinned" in response.json()["detail"]


def test_force_allows_deleting_a_stale_pin(secured: TestClient) -> None:
    response = secured.delete(
        f"/api/versions/{WORKSHOP_ID}/{VERSION}?force=true", headers={TOKEN_HEADER: TOKEN}
    )
    assert response.status_code == 200


def test_unknown_job_is_404(client: TestClient) -> None:
    assert client.get("/api/jobs/nope").status_code == 404


# --- jobs --------------------------------------------------------------------


def test_job_lifecycle() -> None:
    jobs = JobRegistry()
    job = jobs.create("mods.update")
    assert job.status == "running"

    jobs.finish(job.id, ok=True, detail="done")
    finished = jobs.get(job.id)
    assert finished is not None
    assert finished.status == "succeeded"
    assert finished.finished_at is not None


def test_failed_job_records_why() -> None:
    jobs = JobRegistry()
    job = jobs.create("server.update")
    jobs.finish(job.id, ok=False, detail="steamcmd exited 3")

    recorded = jobs.get(job.id)
    assert recorded is not None
    assert recorded.status == "failed"
    assert "exited 3" in (recorded.detail or "")


def test_finishing_an_unknown_job_is_harmless() -> None:
    JobRegistry().finish("nope", ok=True, detail="")


# --- the loopback guard ------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.5"])
def test_loopback_addresses(host: str) -> None:
    assert _is_loopback(host) is True


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "192.168.1.10", "example.com", ""],  # noqa: S104 — asserting it is refused
)
def test_non_loopback_addresses(host: str) -> None:
    """0.0.0.0 is the dangerous one: it binds every interface."""
    assert _is_loopback(host) is False
