"""FastAPI web layer (spec §10, §11).

Deliberately thin. It reuses the pydantic models from config.py and state.py directly and
holds no notion of its own about what an instance or a mod version is — every route is a
call into the same functions the CLI uses, so the two can never disagree about what
"running" or "pinned" means.

Access model:

- Read routes are open. They disclose mod names, versions, ports, and paths — nothing
  secret; the Steam password and API token live in environment variables and are never
  returned.
- Write routes (start, stop, update, delete) require the token from `api.token_env` when
  one is set.
- When no token is set, writes are still permitted, because `dzsm serve` refuses to bind
  anything but a loopback address in that case — local access is already equivalent to
  having the CLI.

Long operations (mod and server downloads) return 202 with a job id rather than holding a
connection open for twenty minutes.
"""

from __future__ import annotations

import logging
import secrets
import sys
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from dzsm import runner
from dzsm.config import Config, WorkshopMod, load_config
from dzsm.instance import InstanceError
from dzsm.mods import (
    ModCacheError,
    delete_version,
    promote_snapshot,
    promote_tree,
    remove_mod,
    unused_mods,
    version_status,
)
from dzsm.state import State, load_state, save_state
from dzsm.steam import (
    SteamError,
    check_updates,
    download_mod,
    update_server_app,
    verify_download,
)

_log = logging.getLogger(__name__)

TOKEN_HEADER = "X-DZSM-Token"  # noqa: S105 — a header name, not a secret


class Job(BaseModel):
    """A long-running operation. Downloads can take many minutes."""

    id: str
    kind: str
    status: str  # running | succeeded | failed
    started_at: datetime
    finished_at: datetime | None = None
    detail: str | None = None


class JobRegistry:
    """In-memory job tracking.

    Deliberately not persisted: a job describes work this process is doing, and if the
    process dies the work died with it. Resurrecting a "running" job from disk after a
    restart would be a lie.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, kind: str) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            status="running",
            started_at=datetime.now(UTC),
        )
        with self._lock:
            self._jobs[job.id] = job
        return job

    def finish(self, job_id: str, *, ok: bool, detail: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "succeeded" if ok else "failed"
            job.finished_at = datetime.now(UTC)
            job.detail = detail

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)


def create_app(config_path: Path) -> FastAPI:
    """Build the API around one config file.

    Config is read once at startup — changing it means restarting the server, same as any
    other daemon. State is re-read on every request, because the CLI may have changed it
    from another process.
    """
    config = load_config(config_path)
    jobs = JobRegistry()
    app = FastAPI(
        title="dzsm",
        summary="DayZ server manager",
        version="0.1.0",
    )

    def current_state() -> State:
        return load_state(config.state_file)

    def require_token(request: Request) -> None:
        """Gate write routes. A no-op when no token is configured (loopback-only mode)."""
        expected = config.api.token()
        if expected is None:
            return
        supplied = request.headers.get(TOKEN_HEADER, "")
        authorization = request.headers.get("Authorization", "")
        if not supplied and authorization.lower().startswith("bearer "):
            supplied = authorization[7:]
        # compare_digest, not ==, so a wrong token cannot be recovered by timing.
        if not supplied or not secrets.compare_digest(supplied, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"missing or invalid API token (send it as {TOKEN_HEADER})",
            )

    writes = [Depends(require_token)]

    # --- read routes ---------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "root": str(config.root),
            "authenticated": config.api.token() is not None,
        }

    @app.get("/api/instances")
    def list_instances() -> list[dict[str, Any]]:
        state = current_state()
        result = []
        for name, instance in config.instances.items():
            process = state.processes.get(name)
            upcoming = runner.next_restart(
                instance, started_at=process.started_at if process else None
            )
            result.append(
                {
                    "name": name,
                    "port": instance.port,
                    "running": runner.is_running(config, state, name),
                    "mods": instance.mods,
                    "server_mods": instance.server_mods,
                    "pins": state.pins.get(name, {}),
                    "rcon_port": instance.resolved_rcon_port(),
                    "server_build": state.server_builds.get(name),
                    "server_build_pin": instance.server_build,
                    "isolated_server": instance.isolated_server,
                    "next_restart": upcoming.isoformat() if upcoming else None,
                }
            )
        return result

    @app.get("/api/mods")
    def list_mods() -> list[dict[str, Any]]:
        state = current_state()
        result = []
        for name, mod in config.mods.items():
            entry: dict[str, Any] = {"name": name, "kind": mod.kind}
            if isinstance(mod, WorkshopMod):
                entry["workshop_id"] = mod.workshop_id
                entry["pinned_version"] = mod.pinned_version
                entry["cached_versions"] = state.known_versions(mod.workshop_id)
            else:
                entry["path"] = str(mod.path)
            result.append(entry)
        return result

    @app.get("/api/versions")
    def list_versions() -> list[dict[str, Any]]:
        state = current_state()
        return [
            {
                "mod_name": s.mod_name,
                "workshop_id": s.workshop_id,
                "version": s.version,
                "size_bytes": s.size_bytes,
                "age_days": s.age_days,
                "in_use": s.in_use,
                "pinned_by": s.pinned_by,
                "kept_by": s.kept_by,
                "is_latest": s.is_latest,
                "exists": s.exists,
            }
            for s in version_status(config, state)
        ]

    @app.get("/api/unused")
    def list_unused(include_kept: bool = False) -> list[dict[str, Any]]:
        state = current_state()
        return [
            {
                "mod_name": m.mod_name,
                "workshop_id": m.workshop_id,
                "kind": m.kind,
                "versions": m.versions,
                "size_bytes": m.size_bytes,
                "path": str(m.path),
                "pinned_by": m.pinned_by,
                "kept_by": m.kept_by,
                "deletable": m.deletable,
            }
            for m in unused_mods(config, state, include_kept=include_kept)
        ]

    @app.get("/api/server")
    def server_builds() -> dict[str, Any]:
        state = current_state()
        builds = (
            sorted(p.name for p in config.serverapp_dir.iterdir() if p.is_dir())
            if config.serverapp_dir.is_dir()
            else []
        )
        return {
            "builds": builds,
            "active": state.serverapp_version,
            "server_app_path": str(config.server_app_path) if config.server_app_path else None,
        }

    @app.get("/api/jobs")
    def list_jobs() -> list[Job]:
        return jobs.all()

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> Job:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no such job {job_id!r}")
        return job

    # --- instance control ----------------------------------------------------

    @app.post("/api/instances/{name}/start", dependencies=writes)
    def start_instance(name: str) -> dict[str, Any]:
        state = current_state()
        _known_instance(config, name)
        try:
            pid = runner.start(config, state, name)
        except (runner.RunnerError, InstanceError, ModCacheError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            save_state(config.state_file, state)
        return {"name": name, "pid": pid, "pins": state.pins.get(name, {})}

    @app.post("/api/instances/{name}/stop", dependencies=writes)
    def stop_instance(name: str) -> dict[str, Any]:
        state = current_state()
        _known_instance(config, name)
        try:
            runner.stop(config, state, name)
        except runner.RunnerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            save_state(config.state_file, state)
        return {"name": name, "running": False}

    @app.post("/api/instances/{name}/restart", dependencies=writes)
    def restart_instance(name: str) -> dict[str, Any]:
        state = current_state()
        _known_instance(config, name)
        try:
            runner.stop(config, state, name)
            pid = runner.start(config, state, name)
        except (runner.RunnerError, InstanceError, ModCacheError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            save_state(config.state_file, state)
        return {"name": name, "pid": pid, "pins": state.pins.get(name, {})}

    # --- mods ----------------------------------------------------------------

    @app.post("/api/mods/check", dependencies=writes)
    def check_mods() -> dict[str, Any]:
        """Ask Steam what changed. Fast, so it runs inline rather than as a job."""
        state = current_state()
        workshop = {
            name: mod for name, mod in config.mods.items() if isinstance(mod, WorkshopMod)
        }
        try:
            remote = check_updates([mod.workshop_id for mod in workshop.values()])
        except SteamError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        return {
            name: {
                "workshop_id": mod.workshop_id,
                "latest": remote.get(mod.workshop_id),
                "cached": state.known_versions(mod.workshop_id),
                "update_available": remote.get(mod.workshop_id) is not None
                and remote[mod.workshop_id] not in state.known_versions(mod.workshop_id),
            }
            for name, mod in workshop.items()
        }

    @app.post("/api/mods/update", status_code=202, dependencies=writes)
    def update_mods(background: BackgroundTasks) -> Job:
        """Download changed mods. Returns a job; poll /api/jobs/{id}."""
        job = jobs.create("mods.update")
        background.add_task(_run_mod_update, config, jobs, job.id)
        return job

    @app.delete("/api/versions/{workshop_id}/{version}", dependencies=writes)
    def delete_one_version(workshop_id: int, version: int, force: bool = False) -> dict[str, Any]:
        state = current_state()
        running = [n for n in config.instances if runner.is_running(config, state, n)]
        try:
            path = delete_version(
                config, state, workshop_id, version, running_instances=running, force=force
            )
        except ModCacheError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            save_state(config.state_file, state)
        return {"deleted": str(path)}

    @app.delete("/api/mods/{workshop_id}", dependencies=writes)
    def remove_one_mod(workshop_id: int, force: bool = False) -> dict[str, Any]:
        state = current_state()
        running = [n for n in config.instances if runner.is_running(config, state, n)]
        try:
            removed = remove_mod(
                config, state, workshop_id, running_instances=running, force=force
            )
        except ModCacheError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            save_state(config.state_file, state)
        return {"removed": [str(p) for p in removed]}

    # --- server app ----------------------------------------------------------

    @app.post("/api/server/update", status_code=202, dependencies=writes)
    def update_server(background: BackgroundTasks, validate: bool = False) -> Job:
        """Download the server app. Multi-GB, so always a job."""
        job = jobs.create("server.update")
        background.add_task(_run_server_update, config, jobs, job.id, validate)
        return job

    # --- dashboard -----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        # Read per request so editing the file during development shows up on refresh.
        return _dashboard_html()

    return app


def _dashboard_html() -> str:
    return _dashboard_path().read_text(encoding="utf-8")


def _dashboard_path() -> Path:
    """Where dashboard.html lives, in a source tree or inside a PyInstaller bundle.

    A frozen build unpacks data files under sys._MEIPASS, so __file__ points into the
    bundle's module archive rather than at a real directory.
    """
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle is not None:
        return Path(bundle) / "dzsm" / "dashboard.html"
    return Path(__file__).parent / "dashboard.html"


def _known_instance(config: Config, name: str) -> None:
    if name not in config.instances:
        raise HTTPException(status_code=404, detail=f"no such instance {name!r}")


def _run_mod_update(config: Config, jobs: JobRegistry, job_id: str) -> None:
    """Body of the mods.update job. Mirrors `dzsm mods update` exactly."""
    state = load_state(config.state_file)
    workshop = {name: mod for name, mod in config.mods.items() if isinstance(mod, WorkshopMod)}
    lines: list[str] = []
    failures = 0
    try:
        remote = check_updates([mod.workshop_id for mod in workshop.values()])
    except SteamError as exc:
        jobs.finish(job_id, ok=False, detail=f"update check failed: {exc}")
        return

    for name, mod in workshop.items():
        if mod.pinned_version is not None:
            lines.append(f"{name}: pinned to {mod.pinned_version}, skipped")
            continue
        latest = remote.get(mod.workshop_id)
        if latest is None:
            lines.append(f"{name}: no details from Steam")
            failures += 1
            continue
        if latest in state.known_versions(mod.workshop_id):
            lines.append(f"{name}: up to date ({latest})")
            continue
        try:
            result = download_mod(config, mod.workshop_id)
            verify_download(config, result)
            promote_snapshot(config, state, mod.workshop_id, latest)
            state.last_seen_remote[mod.workshop_id] = latest
            lines.append(f"{name}: promoted {latest}")
        except (SteamError, ModCacheError) as exc:
            # One broken mod must not stop the batch (Rule 3).
            _log.error("api: mod %s (%s) failed: %s", name, mod.workshop_id, exc)
            lines.append(f"{name}: FAILED {exc}")
            failures += 1
        finally:
            save_state(config.state_file, state)

    jobs.finish(job_id, ok=failures == 0, detail="\n".join(lines) or "nothing to do")


def _run_server_update(config: Config, jobs: JobRegistry, job_id: str, validate: bool) -> None:
    """Body of the server.update job."""
    try:
        build_id = update_server_app(config, validate=validate)
    except SteamError as exc:
        jobs.finish(job_id, ok=False, detail=str(exc))
        return

    destination = config.serverapp_dir / build_id
    if not destination.is_dir():
        try:
            promote_tree(
                config.serverapp_download_dir, destination, strategy=config.snapshot_strategy
            )
        except ModCacheError as exc:
            jobs.finish(job_id, ok=False, detail=str(exc))
            return

    state = load_state(config.state_file)
    state.serverapp_version = build_id
    save_state(config.state_file, state)
    jobs.finish(job_id, ok=True, detail=f"server app at build {build_id}")


def app_factory() -> FastAPI:  # pragma: no cover - uvicorn entry point
    """For `uvicorn dzsm.api:app_factory --factory`, using DZSM_CONFIG."""
    import os

    return create_app(Path(os.environ.get("DZSM_CONFIG", "config.toml")))
