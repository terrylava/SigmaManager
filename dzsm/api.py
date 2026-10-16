"""FastAPI web layer (spec §10, §11).

Deliberately thin. It reuses the pydantic models from config.py and state.py directly and
holds no notion of its own about what an instance or a mod version is — every route is a
call into the same functions the CLI uses, so the two can never disagree about what
"running" or "pinned" means.

Access model:

- Write routes (start, stop, update, delete) require the token from `api.token_env` when
  one is set.
- So do reads that return the contents of a file or the configuration: serverDZ.cfg
  carries `passwordAdmin`, and config.toml carries the whole layout of the machine.
  Leaving those open meant that on a token-protected, network-bound deployment — the only
  deployment the token exists for — writes were refused while the admin password was
  handed to anyone who could reach the port.
- Other reads are open. They disclose mod names, versions, ports and job status; the
  Steam password and the API token live in environment variables and are never returned.
- When no token is set, writes are still permitted, because `dzsm serve` refuses to bind
  anything but a loopback address in that case — local access is already equivalent to
  having the CLI.
- Unsafe methods reject a cross-origin browser request whatever the token says: with no
  token (the loopback default) every no-body POST is a "simple request", so any page in
  the operator's browser could stop their servers with a plain form.

Long operations (mod and server downloads) return 202 with a job id rather than holding a
connection open for twenty minutes.
"""

from __future__ import annotations

import logging
import secrets
import sys
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from dzsm import players, runner, settings
from dzsm.config import Config, WorkshopMod, load_config
from dzsm.instance import InstanceError
from dzsm.mods import (
    ModCacheError,
    delete_version,
    local_copy,
    promote_snapshot,
    promote_tree,
    record_check,
    remove_mod,
    safe_rmtree,
    scan_for_mods,
    unused_mods,
    version_status,
)
from dzsm.rcon import PlayerSlot, RconError, RconEvent, RconSession, parse_players
from dzsm.state import State, load_state, save_state
from dzsm.steam import (
    SteamError,
    SteamGuardRequired,
    SteamLoginFailed,
    WorkshopItem,
    check_updates,
    download_mod,
    get_workshop_item,
    remote_server_build,
    search_workshop,
    update_server_app,
    verify_download,
)
from dzsm.steam import login as steam_login

_log = logging.getLogger(__name__)

TOKEN_HEADER = "X-DZSM-Token"  # noqa: S105 — a header name, not a secret

# Search paging limits. Steam's query API stops returning results well before the total
# it reports would suggest, so offering a page number for every match would produce empty
# pages; 50 is deep enough that nobody sensibly pages that far for a mod.
MAX_SEARCH_PER_PAGE = 50
MAX_SEARCH_PAGES = 50

# Methods that do not change anything, and so are not worth refusing cross-origin: the
# response is unreadable to the calling page anyway, and refusing them would break
# ordinary linking.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class AddMod(BaseModel):
    """Adding a Workshop mod to the config."""

    # Omitted lets dzsm derive one from the Workshop title.
    name: str | None = None
    # Instances to append it to, in load order. Empty adds it to the config only.
    instances: list[str] = []


class AssignMod(BaseModel):
    """Which instances carry a configured mod, in which of their three lists."""

    instances: list[str] = []
    # "mods" (client-facing), "server_mods" (-serverMod), or "keep_mods" (retained but
    # not loaded). Named list_name, not list: a field called `list` shadows the builtin
    # inside the class body, so pydantic resolves the sibling `list[str]` annotation
    # against this field's default and fails at import.
    list_name: str = "mods"


class ChatMessage(BaseModel):
    """Admin chat to send to a server."""

    message: str
    # BattlEye addresses a single player by their current slot number, which is the
    # number in the live player list. None broadcasts to everyone.
    player: int | None = None


class InstanceMod(BaseModel):
    """Which of one instance's three lists a mod belongs in."""

    list_name: str = "mods"


class LocalizeMod(BaseModel):
    """Forking a mod into an editable local copy for one instance."""

    # Omitted derives "<mod>_<instance>", which is what makes two servers' forks of the
    # same mod distinguishable in a mods list.
    name: str | None = None


class AddLocalMod(BaseModel):
    """Configuring a mod that is already on disk."""

    path: str
    name: str | None = None
    instances: list[str] = []


class ConfigText(BaseModel):
    """A whole config file, submitted as text."""

    text: str


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

    def is_running(self, kind: str) -> bool:
        """Whether a job of this kind is in flight, so a second is not started on top."""
        with self._lock:
            return any(j.kind == kind and j.status == "running" for j in self._jobs.values())


# How long after startup the first automatic check runs. Deliberately not the full
# interval: a daemon that has just come up should be able to say whether anything is out
# of date, rather than reporting "never checked" for the next half hour. Deliberately not
# zero either, so restarting the daemon in a loop does not hammer Steam.
FIRST_CHECK_DELAY_SECONDS = 30

# How often the checker wakes while checking is switched off, only to see whether it has
# been switched back on. Turning it on in the UI has to be noticed by the thread that
# does the work, and nothing else tells it.
DISABLED_POLL_SECONDS = 60


def _auto_check(
    get_config: Callable[[], Config], jobs: JobRegistry, stop: threading.Event
) -> None:
    """Ask Steam what changed, on an interval, for as long as the daemon is up.

    Reads the interval from the *current* config on every pass rather than capturing it,
    so changing it in the web UI takes effect without a restart — the same reason the
    supervisor re-reads config.

    A failure here is logged and nothing else. This runs unattended, and an update check
    is not something that should be able to take the daemon down (Rule 3).
    """
    delay: float = FIRST_CHECK_DELAY_SECONDS
    while not stop.wait(delay):
        config = get_config()
        interval = config.mod_check_interval_minutes
        if interval <= 0:
            # Turned off, but pollable: someone may turn it back on from the UI, and this
            # thread is what would have to notice.
            delay = DISABLED_POLL_SECONDS
            continue
        delay = interval * 60

        if jobs.is_running("mods.update") or jobs.is_running("mods.check"):
            _log.info("auto-check: an update is already running, skipping this pass")
            continue

        job = jobs.create("mods.check")
        try:
            found = _check_and_record(config)
        except SteamError as exc:
            _log.error("auto-check failed: %s", exc)
            jobs.finish(job.id, ok=False, detail=str(exc))
            continue

        detail = (
            f"{len(found)} update(s) available: {', '.join(sorted(found))}"
            if found
            else "everything up to date"
        )
        jobs.finish(job.id, ok=True, detail=detail)

        if found and config.mod_check_auto_download:
            download = jobs.create("mods.update")
            _run_mod_update(config, jobs, download.id)

        # Separate from the mod check and deliberately after it: this one costs a steamcmd
        # run and needs a working session, so a broken sentry must not stop mods being
        # checked over the Web API, which needs neither.
        if config.check_server_build:
            _check_server_build(config, jobs)

        # Folded into the same housekeeping tick rather than the supervision loop: the
        # open admin log grows continuously while anyone is connected, and re-parsing it
        # every 15s to learn nothing would be pure waste.
        _ingest_sessions(config)


RECONCILE_SECONDS = 10.0


class SessionRegistry:
    """Holds one live RCON session per running instance that wants one.

    Sessions follow instances rather than being started once at boot: a connection to a
    stopped server is pointless, and opening one would log a failure every backoff
    interval forever. Reconciled on a timer, which also covers an instance started or
    stopped from the CLI in another process.
    """

    def __init__(self, get_config: Callable[[], Config]) -> None:
        self._get_config = get_config
        self._sessions: dict[str, RconSession] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="dzsm-rcon-sessions", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=3.0)
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.stop()

    def get(self, name: str) -> RconSession | None:
        with self._lock:
            return self._sessions.get(name)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile()
            except Exception:
                # This thread holds every live connection; it must not die (Rule 3).
                _log.exception("rcon: session reconcile failed")
            if self._stop.wait(RECONCILE_SECONDS):
                break

    def reconcile(self) -> None:
        """Start sessions for running instances that want one, stop the rest."""
        config = self._get_config()
        state = load_state(config.state_file)

        # Deliberately *not* gated on rcon_grace_remaining, unlike every one-shot caller.
        # The grace exists so a synchronous caller does not sit through a socket timeout
        # against a port BattlEye has not bound yet. A session is a background thread with
        # its own backoff, so it can simply keep trying — and waiting out the full 300s
        # grace would leave the chat feed dead for five minutes after every restart, which
        # is exactly when an admin is most likely to be watching it.
        wanted = {
            name
            for name, instance in config.instances.items()
            if instance.rcon_live
            and instance.rcon_enabled
            and runner.is_running(config, state, name)
        }

        with self._lock:
            running = set(self._sessions)

        for name in running - wanted:
            session = self._sessions.pop(name, None)
            if session is not None:
                _log.info("rcon: stopping live session for %s", name)
                session.stop()

        for name in wanted - running:
            port = runner.rcon_port_for(config, name)
            if port is None:
                continue
            session = RconSession(
                config.rcon.host,
                port,
                # A provider, not a value: dzsm can regenerate the BattlEye config, and a
                # session holding the old password would never reconnect.
                lambda n=name: runner.rcon_password_for(self._get_config(), n),  # type: ignore[misc]
                timeout=config.rcon.timeout_seconds,
                on_event=lambda event, n=name: self._persist(n, event),  # type: ignore[misc]
            )
            with self._lock:
                self._sessions[name] = session
            session.start()
            _log.info("rcon: starting live session for %s on port %s", name, port)

    def _persist(self, instance: str, event: RconEvent) -> None:
        """Write the messages worth keeping. Chat above all — nothing can re-fetch it."""
        if event.kind not in PERSISTED_EVENTS:
            return
        config = self._get_config()
        try:
            players.record_event(
                config,
                instance,
                kind=event.kind,
                raw=event.raw,
                name=event.name,
                channel=event.channel,
                text=event.text,
            )
        except players.PlayerStoreError as exc:
            _log.error("players: could not record %s event: %s", event.kind, exc)


# "other" and "admin" are left in the live feed but not stored: BattlEye emits a lot of
# them and none is worth a row forever.
PERSISTED_EVENTS = frozenset({"chat", "connect", "disconnect", "kick"})


def _players_json(name: str, slots: list[PlayerSlot]) -> dict[str, Any]:
    return {
        "instance": name,
        "available": True,
        "reason": None,
        "players": [
            {
                "number": slot.number,
                "name": slot.name,
                "ping": slot.ping,
                "guid": slot.guid,
                "ip": slot.ip,
                "verified": slot.verified,
                "lobby": slot.lobby,
            }
            for slot in slots
        ],
    }


def _ingest_sessions(config: Config) -> None:
    """Fold any new admin-log sessions into the ledger.

    Never fatal, and never per-instance fatal: one unreadable profiles directory must not
    stop the others being read, and none of it may take the housekeeping thread down
    (Rule 3).
    """
    for name in config.instances:
        try:
            written = players.ingest(config, name)
        except players.PlayerStoreError as exc:
            _log.error("players: ingest for %s failed: %s", name, exc)
            continue
        if written:
            _log.info("players: recorded %d session(s) for %s", written, name)


def _check_server_build(config: Config, jobs: JobRegistry) -> None:
    """Ask Steam whether the server app has a newer build, and act on the answer.

    This is the half of "game updates" that nothing used to cover. A mod check talks to
    the Web API about workshop items and never learns that DayZ itself moved, so a server
    could sit on an old build — unjoinable, because clients update themselves — while the
    dashboard reported everything healthy.
    """
    if jobs.is_running("server.update"):
        return

    job = jobs.create("server.check")
    state = load_state(config.state_file)
    try:
        remote = remote_server_build(config)
    except SteamError as exc:
        _log.error("server build check failed: %s", exc)
        _note_steam_session(config, state, exc)
        save_state(config.state_file, state)
        jobs.finish(job.id, ok=False, detail=str(exc))
        return

    state.latest_remote_server_build = remote
    state.server_build_checked_at = datetime.now(UTC)
    _note_steam_session(config, state, None)
    save_state(config.state_file, state)

    behind = remote != state.serverapp_version
    jobs.finish(
        job.id,
        ok=True,
        detail=(
            f"server build {remote} available (running {state.serverapp_version})"
            if behind
            else f"server build {remote} is current"
        ),
    )

    if behind and config.server_update_auto_download:
        download = jobs.create("server.update")
        _run_server_update(config, jobs, download.id, False)


def _check_and_record(config: Config) -> dict[str, int]:
    """Ask Steam for every configured mod, record the answers, return what is behind.

    Maps mod name -> the newer version available. Empty when everything is current, which
    is a real answer and not a falsy failure (Rule 1).
    """
    workshop = {name: mod for name, mod in config.mods.items() if isinstance(mod, WorkshopMod)}
    remote = check_updates([mod.workshop_id for mod in workshop.values()])

    state = load_state(config.state_file)
    behind = record_check(config, state, remote)
    save_state(config.state_file, state)
    return behind


def create_app(config_path: Path) -> FastAPI:
    """Build the API around one config file.

    Config is read once at startup — changing it means restarting the server, same as any
    other daemon. State is re-read on every request, because the CLI may have changed it
    from another process.
    """
    # Rebound by the settings routes. Every route below closes over this same name, so
    # rebinding it here is what makes an edit take effect immediately rather than at the
    # next daemon restart.
    config = load_config(config_path)
    jobs = JobRegistry()
    checker_stop = threading.Event()
    # Reads `config` through a lambda, not a captured value, so a settings edit that
    # turns rcon_live on or changes a port is picked up at the next reconcile.
    sessions = SessionRegistry(lambda: config)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Run the automatic update check for as long as the daemon is up.

        A thread rather than an async task: check_updates and steamcmd are both blocking,
        and running them on the event loop would stall every request for the duration of
        a download.
        """
        checker = threading.Thread(
            # Reads `config` through a closure, not a captured value, so an interval
            # changed through the settings form is picked up on the next pass.
            target=_auto_check,
            args=(lambda: config, jobs, checker_stop),
            name="dzsm-autocheck",
            daemon=True,
        )
        checker.start()
        sessions.start()
        try:
            yield
        finally:
            checker_stop.set()
            checker.join(timeout=5)
            sessions.stop()

    app = FastAPI(
        title="dzsm",
        summary="DayZ server manager",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def block_cross_origin(request: Request, call_next: Any) -> Any:
        """Refuse state-changing requests that a browser made from another site.

        Without this, `POST /api/instances/x/stop` is a CORS "simple request": it carries
        no body and no custom header, so any page the operator visits can fire it with a
        plain <form> and no preflight. The response is unreadable cross-origin, but the
        server still stops — and stopping also sets `held`, so the supervisor will not
        bring it back. On the loopback-without-token default that is a drive-by outage.

        Only browsers send these headers, so curl and the CLI are unaffected. That is the
        intended trade: this closes the browser-driven hole without turning the API into
        something only a browser can call.
        """
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            site = request.headers.get("sec-fetch-site")
            host = request.headers.get("host", "")
            # "none" is a user typing the URL; "same-origin" is the dashboard itself.
            cross = (origin is not None and urlparse(origin).netloc != host) or (
                site is not None and site not in {"same-origin", "none"}
            )
            if cross:
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={
                        "detail": (
                            f"cross-origin {request.method} refused (origin {origin!r}). "
                            f"This endpoint changes state and is only callable from the "
                            f"dashboard itself or from a non-browser client."
                        )
                    },
                )
        return await call_next(request)

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
                    "held": name in state.held,
                    "mods": instance.mods,
                    "server_mods": instance.server_mods,
                    "keep_mods": instance.keep_mods,
                    "pins": state.pins.get(name, {}),
                    "rcon_port": instance.resolved_rcon_port(),
                    "rcon_live": instance.rcon_live,
                    "cpu_affinity": instance.cpu_affinity,
                    "priority": instance.priority,
                    "cpu_count": instance.cpu_count,
                    "server_executable": instance.server_executable,
                    "restart_after_hours": instance.restart_after_hours,
                    "restart_schedule": instance.restart_schedule,
                    "dologs": instance.dologs,
                    "adminlog": instance.adminlog,
                    "netlog": instance.netlog,
                    "freezecheck": instance.freezecheck,
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
                cached = state.known_versions(mod.workshop_id)
                latest = state.last_seen_remote.get(mod.workshop_id)
                entry["workshop_id"] = mod.workshop_id
                entry["pinned_version"] = mod.pinned_version
                entry["cached_versions"] = cached
                entry["latest_remote"] = latest
                # A pinned mod is never "behind": it is held at a version deliberately,
                # and reporting it as out of date would nag about a decision already made.
                entry["update_available"] = (
                    mod.pinned_version is None and latest is not None and latest not in cached
                )
            else:
                entry["path"] = str(mod.path)
                entry["update_available"] = False
            result.append(entry)
        return result

    @app.get("/api/steam", dependencies=writes)
    def steam_status() -> dict[str, Any]:
        """Whether steamcmd can still authenticate, and what to do when it cannot.

        Token-gated: it names the Steam account and the steamcmd location, which is the
        same class of thing as the config file.

        dzsm never handles the password — `dzsm login --interactive` hands steamcmd the
        terminal so the password and Guard code go straight into it. So this reports and
        instructs; it cannot log in.
        """
        state = current_state()
        return {
            "username": config.steam.username,
            "ok": state.steam_session_ok,
            "checked_at": (
                state.steam_session_checked_at.isoformat()
                if state.steam_session_checked_at
                else None
            ),
            "detail": state.steam_session_detail,
            "steamcmd_installed": config.steam.steamcmd_path.is_file(),
            # The exact command, with this daemon's config path already in it, so a stale
            # session does not turn into a hunt through the docs.
            "login_command": f'dzsm login --interactive --config "{config_path}"',
            "password_env_set": config.steam.password() is not None,
        }

    @app.post("/api/steam/verify", status_code=202, dependencies=writes)
    def verify_steam(background: BackgroundTasks) -> Job:
        """Prove the cached session still works. A job: steamcmd can take a while."""
        job = jobs.create("steam.verify")
        background.add_task(_run_steam_verify, config, jobs, job.id)
        return job

    @app.get("/api/instances/{name}/players", dependencies=writes)
    def instance_players(name: str) -> dict[str, Any]:
        """Who is connected right now, from RCON.

        Token-gated: it returns player names and IP addresses. The IP is passed through
        for the live view and never stored (see players.py) — it is the only personal
        data here that nothing needs a durable record of.

        A server that is down, still starting, or has RCON off is not an error: each
        returns an empty list and says which, because "no players" and "cannot ask" are
        different answers and a dashboard showing 0 for both would be lying.
        """
        _known_instance(config, name)
        state = current_state()

        # The held session first, before any of the checks below. It is already connected
        # and proven to answer, so the startup grace does not apply to it — gating on the
        # grace here reported "RCON is not expected to answer" while the live feed from
        # that very connection was scrolling past on the same page.
        session = sessions.get(name)
        if session is not None and session.status().connected:
            try:
                slots = parse_players(session.send("players"))
            except RconError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            return _players_json(name, slots)

        if not runner.is_running(config, state, name):
            return {"instance": name, "available": False, "reason": "not running", "players": []}

        grace = runner.rcon_grace_remaining(config, state, name)
        if grace > 0:
            return {
                "instance": name,
                "available": False,
                "reason": f"RCON is not expected to answer for another {grace:.0f}s",
                "players": [],
            }

        try:
            client = runner.rcon_client(config, name)
        except RconError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if client is None:
            return {"instance": name, "available": False, "reason": "RCON is off", "players": []}

        try:
            slots = client.players()
        except RconError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            client.close()

        return _players_json(name, slots)

    @app.get("/api/instances/{name}/feed", dependencies=writes)
    def instance_feed(name: str, after: int = 0, history: int = 0) -> dict[str, Any]:
        """Live messages from the held RCON connection: chat, connects, disconnects.

        `after` is a cursor, not a timestamp, so a client polling fast never sees a line
        twice and never misses two that arrived in the same second. `history` asks for
        that many stored messages instead, for the first load of a page.
        """
        _known_instance(config, name)
        session = sessions.get(name)

        stored: list[dict[str, Any]] = []
        if history:
            try:
                stored = [
                    {
                        "at": event.at.isoformat(),
                        "kind": event.kind,
                        "name": event.name,
                        "channel": event.channel,
                        "text": event.text,
                        "raw": event.raw,
                        "stored": True,
                    }
                    for event in reversed(
                        players.recent_events(config, instance=name, limit=history)
                    )
                ]
            except players.PlayerStoreError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc

        if session is None:
            instance = config.instances[name]
            reason = (
                "RCON is off for this instance"
                if not instance.rcon_enabled
                else "the live connection is disabled (rcon_live)"
                if not instance.rcon_live
                else "not connected — the instance is down or still starting"
            )
            return {
                "instance": name,
                "live": False,
                "reason": reason,
                "cursor": after,
                "events": stored,
                "status": None,
            }

        cursor, fresh = session.events_since(after)
        status = session.status()
        return {
            "instance": name,
            "live": status.connected,
            "reason": None if status.connected else status.last_error,
            "cursor": cursor,
            "events": stored
            + [
                {
                    "at": None,
                    "kind": event.kind,
                    "name": event.name,
                    "channel": event.channel,
                    "text": event.text,
                    "raw": event.raw,
                    "stored": False,
                }
                for event in fresh
            ],
            "status": {
                "connected": status.connected,
                "since": status.since,
                "attempts": status.attempts,
                "events_seen": status.events_seen,
            },
        }

    @app.post("/api/instances/{name}/say", dependencies=writes)
    def instance_say(name: str, body: ChatMessage) -> dict[str, Any]:
        """Send admin chat. Global by default, or to one player by slot number.

        Prefers the held session so this does not open a second connection for one line,
        and falls back to a one-shot client when there is no live session.
        """
        _known_instance(config, name)
        if not body.message.strip():
            raise HTTPException(status_code=422, detail="nothing to send")

        session = sessions.get(name)
        try:
            if session is not None and session.status().connected:
                if body.player is None:
                    session.say(body.message)
                else:
                    session.say_to(body.player, body.message)
                return {"sent": True, "via": "live session"}

            client = runner.rcon_client(config, name)
            if client is None:
                raise HTTPException(
                    status_code=409, detail=f"instance {name!r} has rcon_enabled = false"
                )
            try:
                if body.player is None:
                    client.say(body.message)
                else:
                    client.say_to(body.player, body.message)
            finally:
                client.close()
        except RconError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"sent": True, "via": "one-shot connection"}

    @app.get("/api/players", dependencies=writes)
    def player_ledger(instance: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Accumulated time per player, and the latest sessions, from the admin logs.

        Token-gated: this is personal data about third parties, which is a different
        thing to expose than a mod list.
        """
        if instance is not None and instance not in config.instances:
            raise HTTPException(status_code=404, detail=f"no such instance {instance!r}")
        try:
            ranking = players.totals(config, instance=instance, limit=limit)
            latest = players.recent_sessions(config, instance=instance, limit=limit)
        except players.PlayerStoreError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return {
            "totals": [
                {
                    "player_id": entry.player_id,
                    "name": entry.name,
                    "sessions": entry.sessions,
                    "seconds": entry.seconds,
                    "last_seen": entry.last_seen.isoformat(),
                }
                for entry in ranking
            ],
            "sessions": [
                {
                    "instance": session.instance,
                    "player_id": session.player_id,
                    "name": session.name,
                    "started_at": session.started_at.isoformat(),
                    "ended_at": session.ended_at.isoformat(),
                    "seconds": session.seconds,
                    "closed_by": session.closed_by,
                }
                for session in latest
            ],
        }

    @app.post("/api/players/ingest", dependencies=writes)
    def ingest_players() -> dict[str, Any]:
        """Read any new or grown admin logs into the ledger.

        Inline rather than a job: it is file parsing, not a download, and re-reading every
        .ADM an instance has takes well under a second.
        """
        written = {}
        for name in config.instances:
            try:
                written[name] = players.ingest(config, name)
            except players.PlayerStoreError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"sessions": written}

    @app.get("/api/updates")
    def update_status() -> dict[str, Any]:
        """What the last check found, and when the next one is due."""
        state = current_state()
        behind = []
        for name, mod in config.mods.items():
            if not isinstance(mod, WorkshopMod) or mod.pinned_version is not None:
                continue
            latest = state.last_seen_remote.get(mod.workshop_id)
            if latest is not None and latest not in state.known_versions(mod.workshop_id):
                behind.append(name)
        remote_build = state.latest_remote_server_build
        running_build = state.serverapp_version
        # An instance pinned to a build that is no longer the newest. Reported rather
        # than acted on: the pin is a deliberate choice, but after a game update it also
        # means that instance will not accept the updated clients, and nothing else here
        # would ever say so.
        pinned_behind = sorted(
            name
            for name, instance in config.instances.items()
            if instance.server_build is not None
            and remote_build is not None
            and instance.server_build != remote_build
        )
        return {
            "last_checked": state.last_checked_at.isoformat() if state.last_checked_at else None,
            "interval_minutes": config.mod_check_interval_minutes,
            "auto_download": config.mod_check_auto_download,
            "available": sorted(behind),
            "server": {
                "checking": config.check_server_build,
                "installed": running_build,
                "latest": remote_build,
                "update_available": remote_build is not None and remote_build != running_build,
                "checked_at": (
                    state.server_build_checked_at.isoformat()
                    if state.server_build_checked_at
                    else None
                ),
                "auto_download": config.server_update_auto_download,
                "grace_minutes": config.server_update_grace_minutes,
                "pinned_behind": pinned_behind,
                # Instances that will restart onto a newly downloaded build, and when.
                "restarting": {
                    name: due.isoformat()
                    for name in config.instances
                    if (due := runner.build_restart_due(config, state, name)) is not None
                },
            },
        }

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
            "appid": config.server_appid,
            "branch": config.server_branch,
            # So the dashboard can offer real core numbers rather than asking for a mask.
            "cpu_count": psutil.cpu_count(),
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
    def stop_instance(name: str, hold: bool = True) -> dict[str, Any]:
        """Stop an instance. It stays down until started, unless hold=false."""
        state = current_state()
        _known_instance(config, name)
        try:
            runner.stop(config, state, name, hold=hold)
        except runner.RunnerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            save_state(config.state_file, state)
        return {"name": name, "running": False, "held": hold}

    @app.post("/api/instances/{name}/restart", dependencies=writes)
    def restart_instance(name: str) -> dict[str, Any]:
        state = current_state()
        _known_instance(config, name)
        try:
            # hold=False: a restart is not a request for the instance to stay down.
            runner.stop(config, state, name, hold=False)
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
        workshop = {
            name: mod for name, mod in config.mods.items() if isinstance(mod, WorkshopMod)
        }
        try:
            # Goes through the same recording path as the automatic check, so pressing the
            # button and waiting for the timer leave the state file saying the same thing.
            _check_and_record(config)
        except SteamError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        state = current_state()
        remote = state.last_seen_remote

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

    # --- workshop ------------------------------------------------------------

    @app.get("/api/workshop/search")
    def workshop_search(q: str, page: int = 1, per_page: int = 20) -> dict[str, Any]:
        """Search the DayZ Workshop, one page at a time.

        Steam's own page cannot be embedded — it sends frame-ancestors headers, and a
        cross-origin frame could not be read from anyway — so this is our own search over
        their Web API.

        `total` is Steam's count of everything matching, not what came back, so the caller
        can offer the pages it has not fetched.
        """
        state = current_state()
        if page < 1:
            raise HTTPException(status_code=422, detail="page starts at 1")
        per_page = max(1, min(per_page, MAX_SEARCH_PER_PAGE))
        try:
            items, total = search_workshop(
                config.steam.api_key(), q, page=page, per_page=per_page
            )
        except SteamError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        configured = {
            mod.workshop_id: name
            for name, mod in config.mods.items()
            if isinstance(mod, WorkshopMod)
        }
        return {
            "total": total,
            "page": page,
            "per_page": per_page,
            # Steam caps how deep paging can go; without this the browser would offer
            # pages that come back empty.
            "pages": max(1, min(-(-total // per_page), MAX_SEARCH_PAGES)),
            "items": [_workshop_json(item, configured, state) for item in items],
        }

    @app.get("/api/workshop/{workshop_id}")
    def workshop_item(workshop_id: int) -> dict[str, Any]:
        """One item by id. Needs no API key, so pasting a Workshop link always works."""
        state = current_state()
        try:
            item = get_workshop_item(workshop_id)
        except SteamError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if item is None:
            raise HTTPException(status_code=404, detail=f"no Workshop item {workshop_id}")

        configured = {
            mod.workshop_id: name
            for name, mod in config.mods.items()
            if isinstance(mod, WorkshopMod)
        }
        return _workshop_json(item, configured, state)

    @app.post("/api/workshop/{workshop_id}/add", dependencies=writes)
    def workshop_add(workshop_id: int, body: AddMod) -> dict[str, Any]:
        """Add a Workshop mod to the config, optionally to some instances' load order.

        Does not download it: that is `mods update`, which is a job. Appended to a load
        order rather than inserted, because dzsm cannot know where a new mod belongs
        relative to the frameworks already there.
        """
        nonlocal config
        try:
            item = get_workshop_item(workshop_id)
        except SteamError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if item is None:
            raise HTTPException(status_code=404, detail=f"no Workshop item {workshop_id}")

        unknown = [name for name in body.instances if name not in config.instances]
        if unknown:
            raise HTTPException(
                status_code=422, detail=f"unknown instance(s): {', '.join(unknown)}"
            )

        try:
            document = settings.read_document(config_path)
            name = body.name or settings.mod_config_name(
                item.title, set(config.mods), workshop_id
            )
            settings.add_workshop_mod(document, name, workshop_id)
            for instance_name in body.instances:
                settings.add_mod_to_instance(document, instance_name, name)
            config = settings.save(config_path, document)
        except settings.SettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return {"added": name, "workshop_id": workshop_id, "instances": body.instances}

    @app.get("/api/localmods", dependencies=writes)
    def scan_local_mods(path: str) -> dict[str, Any]:
        """List mod folders in (or at) a directory, for mods already on disk.

        Token-gated despite being a read: it enumerates the filesystem, which is a
        different thing to expose than the mod list.
        """
        try:
            candidates = scan_for_mods(config, Path(path))
        except ModCacheError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        return {
            "path": path,
            "candidates": [
                {
                    "path": str(candidate.path),
                    "name": candidate.name,
                    "size_bytes": candidate.size_bytes,
                    "has_addons": candidate.has_addons,
                    "has_keys": candidate.has_keys,
                    "configured_as": candidate.configured_as,
                }
                for candidate in candidates
            ],
        }

    @app.post("/api/localmods/add", dependencies=writes)
    def add_local(body: AddLocalMod) -> dict[str, Any]:
        """Configure a mod that is already on disk. Nothing is downloaded or copied."""
        nonlocal config
        directory = Path(body.path)
        if not directory.is_dir():
            raise HTTPException(status_code=404, detail=f"{directory} is not a directory")

        unknown = [name for name in body.instances if name not in config.instances]
        if unknown:
            raise HTTPException(
                status_code=422, detail=f"unknown instance(s): {', '.join(unknown)}"
            )

        try:
            document = settings.read_document(config_path)
            name = body.name or settings.local_mod_name(directory, set(config.mods))
            settings.add_local_mod(document, name, directory)
            for instance_name in body.instances:
                settings.add_mod_to_instance(document, instance_name, name)
            config = settings.save(config_path, document)
        except settings.SettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return {"added": name, "path": str(directory), "instances": body.instances}

    @app.patch("/api/config/mods/{name}/instances", dependencies=writes)
    def assign_mod(name: str, body: AssignMod) -> dict[str, Any]:
        """Set which instances carry an already-configured mod.

        Ticked instances gain it, unticked ones lose it from that list only. Takes effect
        at each instance's next start, like every other activation decision.
        """
        nonlocal config
        if name not in config.mods:
            raise HTTPException(status_code=404, detail=f"no mod named {name!r}")

        try:
            document = settings.read_document(config_path)
            settings.set_mod_instances(document, name, body.list_name, body.instances)
            config = settings.save(config_path, document)
        except settings.SettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        state = current_state()
        running = [
            instance
            for instance in body.instances
            if runner.is_running(config, state, instance)
        ]
        return {
            "mod": name,
            "list_name": body.list_name,
            "instances": body.instances,
            "applies_at_next_start": running,
        }

    @app.put("/api/instances/{name}/mods/{mod_name}", dependencies=writes)
    def set_instance_mod(name: str, mod_name: str, body: InstanceMod) -> dict[str, Any]:
        """Add a configured mod to one instance, or move it between that instance's lists."""
        nonlocal config
        _known_instance(config, name)
        if mod_name not in config.mods:
            raise HTTPException(status_code=404, detail=f"no mod named {mod_name!r}")

        try:
            document = settings.read_document(config_path)
            settings.set_instance_mod(document, name, mod_name, body.list_name)
            config = settings.save(config_path, document)
        except settings.SettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        state = current_state()
        return {
            "instance": name,
            "mod": mod_name,
            "list_name": body.list_name,
            "applies_at_next_start": runner.is_running(config, state, name),
        }

    @app.delete("/api/instances/{name}/mods/{mod_name}", dependencies=writes)
    def unset_instance_mod(name: str, mod_name: str) -> dict[str, Any]:
        """Take a mod out of one instance. It stays configured and stays downloaded."""
        nonlocal config
        _known_instance(config, name)

        try:
            document = settings.read_document(config_path)
            settings.remove_mod_from_instance(document, name, mod_name)
            config = settings.save(config_path, document)
        except settings.SettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        state = current_state()
        return {
            "instance": name,
            "mod": mod_name,
            "applies_at_next_start": runner.is_running(config, state, name),
        }

    @app.post("/api/instances/{name}/mods/{mod_name}/localize", dependencies=writes)
    def localize_instance_mod(
        name: str, mod_name: str, body: LocalizeMod
    ) -> dict[str, Any]:
        """Give one instance its own editable copy of a mod.

        Copies the files this instance is running into localmods/, configures them as a
        local mod, and swaps the instance over to it *in place* so the load order does not
        change. The original stays configured for whoever else uses it.
        """
        nonlocal config
        _known_instance(config, name)
        if mod_name not in config.mods:
            raise HTTPException(status_code=404, detail=f"no mod named {mod_name!r}")

        target = body.name or settings.local_mod_name(
            Path(f"{mod_name}_{name}"), set(config.mods)
        )
        state = current_state()
        try:
            path, version = local_copy(config, state, mod_name, target, instance_name=name)
        except ModCacheError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        try:
            document = settings.read_document(config_path)
            settings.add_local_mod(document, target, path)
            settings.replace_mod_in_instance(document, name, mod_name, target)
            config = settings.save(config_path, document)
        except settings.SettingsError as exc:
            # The copy is already on disk. Leaving it there orphaned would be a silent
            # multi-GB leak and would block a retry under the same name, so it goes.
            safe_rmtree(path)
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return {
            "instance": name,
            "mod": mod_name,
            "local_name": target,
            "path": str(path),
            "copied_version": version,
            "applies_at_next_start": runner.is_running(config, state, name),
        }

    @app.post("/api/config/mods/{name}/localize", dependencies=writes)
    def localize_mod(name: str, body: LocalizeMod) -> dict[str, Any]:
        """Fork a mod into a standalone editable copy, not attached to any instance.

        The instance-scoped route swaps one server over to the copy; this one just makes
        it, for assigning afterwards. Same files, same rules.
        """
        nonlocal config
        if name not in config.mods:
            raise HTTPException(status_code=404, detail=f"no mod named {name!r}")

        target = body.name or settings.local_mod_name(Path(f"{name}_copy"), set(config.mods))
        state = current_state()
        try:
            path, version = local_copy(config, state, name, target)
        except ModCacheError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        try:
            document = settings.read_document(config_path)
            settings.add_local_mod(document, target, path)
            config = settings.save(config_path, document)
        except settings.SettingsError as exc:
            safe_rmtree(path)
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return {"mod": name, "local_name": target, "path": str(path), "copied_version": version}

    @app.delete("/api/config/mods/{name}", dependencies=writes)
    def remove_configured_mod(name: str, force: bool = False) -> dict[str, Any]:
        """Remove a mod from the config.

        Separate from `DELETE /api/mods/{workshop_id}`, which deletes downloaded files:
        this forgets the mod, that reclaims disk. Removing from the config leaves any
        cached versions behind, where they show up as orphaned and can be deleted
        separately — losing a multi-GB download because a name was removed would be a
        poor trade.
        """
        nonlocal config
        if name not in config.mods:
            raise HTTPException(status_code=404, detail=f"no mod named {name!r}")

        mod = config.mods[name]
        state = current_state()
        try:
            document = settings.read_document(config_path)
            using = settings.instances_using(document, name)
            settings.remove_mod_from_config(document, name, strip_from_instances=force)
            config = settings.save(config_path, document)
        except settings.SettingsError as exc:
            # 409: the request is fine, the config still depends on it.
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        cached = (
            state.known_versions(mod.workshop_id) if isinstance(mod, WorkshopMod) else []
        )
        return {"removed": name, "unassigned_from": using, "cached_versions": cached}

    # --- settings ------------------------------------------------------------

    @app.get("/api/config", dependencies=writes)
    def get_config() -> dict[str, Any]:
        """The config file as text, for editing directly."""
        return {
            "path": str(config_path),
            "text": settings.read_config_text(config_path),
            "editable_instance_fields": sorted(settings.EDITABLE_INSTANCE_FIELDS),
        }

    @app.put("/api/config", dependencies=writes)
    def put_config(body: ConfigText) -> dict[str, Any]:
        """Replace the whole config file. Rejected unless it loads as a valid config."""
        nonlocal config
        try:
            config = settings.save_raw(config_path, body.text)
        except settings.SettingsError as exc:
            # 422, not 500: the edit is wrong, not the server.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"saved": True, "instances": sorted(config.instances)}

    @app.get("/api/config/global", dependencies=writes)
    def get_global_settings() -> dict[str, Any]:
        """Manager-wide settings and their current values, by dotted path."""
        return {
            "values": settings.global_values(config),
            "editable": sorted(settings.EDITABLE_GLOBAL_FIELDS),
            "root": str(config.root),
            "config_path": str(config_path),
        }

    @app.patch("/api/config/global", dependencies=writes)
    def patch_global_settings(body: dict[str, Any]) -> dict[str, Any]:
        """Change manager-wide settings. A null value removes the key, restoring default."""
        nonlocal config
        try:
            document = settings.read_document(config_path)
            settings.apply_global_patch(document, body)
            config = settings.save(config_path, document)
        except settings.SettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"saved": True, "values": settings.global_values(config)}

    @app.patch("/api/instances/{name}/settings", dependencies=writes)
    def patch_instance(name: str, body: dict[str, Any]) -> dict[str, Any]:
        """Change instance settings. A null value removes the key, restoring its default.

        Takes effect for the *next* start — like mod versions, nothing is changed under a
        running server.
        """
        nonlocal config
        _known_instance(config, name)
        try:
            document = settings.read_document(config_path)
            settings.apply_instance_patch(document, name, body)
            config = settings.save(config_path, document)
        except settings.SettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        state = current_state()
        return {
            "saved": True,
            "applies_at_next_start": runner.is_running(config, state, name),
        }

    @app.get("/api/instances/{name}/serverdz", dependencies=writes)
    def get_serverdz(name: str) -> dict[str, Any]:
        """The instance's serverDZ.cfg, as text.

        Token-gated despite being a read: this file holds `passwordAdmin`, which is the
        in-game admin password, and the server's join password.
        """
        _known_instance(config, name)
        path = config.instance_dir(name) / config.instances[name].config_file
        if not path.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"{path} does not exist yet — it is seeded the first time the "
                f"instance starts",
            )
        return {"path": str(path), "text": path.read_text(encoding="utf-8", errors="replace")}

    @app.put("/api/instances/{name}/serverdz", dependencies=writes)
    def put_serverdz(name: str, body: ConfigText) -> dict[str, Any]:
        """Replace the instance's serverDZ.cfg.

        Not parsed or validated: it is the engine's format, not ours, and rejecting keys
        dzsm does not recognise would block exactly the settings people come here to add.
        The previous version is kept alongside it.
        """
        _known_instance(config, name)
        path = config.instance_dir(name) / config.instances[name].config_file
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            settings.write_text(path, body.text)
        except settings.FileLocked as exc:
            # 409, not 500: the request is fine, the world is not ready for it.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        state = current_state()
        return {
            "saved": True,
            "applies_at_next_start": runner.is_running(config, state, name),
        }

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


def _workshop_json(
    item: WorkshopItem, configured: dict[int, str], state: State
) -> dict[str, Any]:
    """One search result, plus what dzsm already knows about it.

    `configured` and `cached` are what let the browser show "already added" instead of
    offering to add something twice.
    """
    return {
        "workshop_id": item.workshop_id,
        "title": item.title,
        "description": item.description,
        "preview_url": item.preview_url,
        "time_updated": item.time_updated,
        "subscriptions": item.subscriptions,
        "file_size": item.file_size,
        "configured_as": configured.get(item.workshop_id),
        "cached_versions": state.known_versions(item.workshop_id),
    }


def _known_instance(config: Config, name: str) -> None:
    if name not in config.instances:
        raise HTTPException(status_code=404, detail=f"no such instance {name!r}")


def _run_steam_verify(config: Config, jobs: JobRegistry, job_id: str) -> None:
    """Body of the steam.verify job. A non-interactive login proves the sentry is good."""
    state = load_state(config.state_file)
    try:
        steam_login(config.steam)
    except SteamError as exc:
        state.record_steam_session(ok=False, detail=str(exc).split("\n")[0])
        save_state(config.state_file, state)
        jobs.finish(job_id, ok=False, detail=str(exc))
        return

    state.record_steam_session(ok=True)
    save_state(config.state_file, state)
    jobs.finish(job_id, ok=True, detail=f"signed in as {config.steam.username}")


def _note_steam_session(config: Config, state: State, exc: Exception | None) -> None:
    """Record what a steamcmd operation revealed about the cached session.

    Only credential failures count. A download can fail for a dozen reasons that say
    nothing about the account — a mod pulled from the Workshop, a full disk — and
    reporting those as "log in again" would send people to fix the one thing that is not
    wrong. Both a Guard prompt and a refused sign-in do mean exactly that, though, and
    missing the second was the likelier of the two to happen in practice.
    """
    if exc is None:
        state.record_steam_session(ok=True)
    elif isinstance(exc, SteamGuardRequired | SteamLoginFailed):
        state.record_steam_session(ok=False, detail=str(exc).split("\n")[0])


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
            # A completed download is proof the cached session works, so the dashboard
            # never has to ask for a check it already has the answer to.
            _note_steam_session(config, state, None)
        except (SteamError, ModCacheError) as exc:
            # One broken mod must not stop the batch (Rule 3).
            _log.error("api: mod %s (%s) failed: %s", name, mod.workshop_id, exc)
            lines.append(f"{name}: FAILED {exc}")
            failures += 1
            _note_steam_session(config, state, exc)
        finally:
            save_state(config.state_file, state)

    jobs.finish(job_id, ok=failures == 0, detail="\n".join(lines) or "nothing to do")


def _run_server_update(config: Config, jobs: JobRegistry, job_id: str, validate: bool) -> None:
    """Body of the server.update job."""
    try:
        build_id = update_server_app(config, validate=validate)
    except SteamError as exc:
        state = load_state(config.state_file)
        _note_steam_session(config, state, exc)
        save_state(config.state_file, state)
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
    if state.serverapp_version != build_id:
        # Stamped only on an actual change, so re-running an update does not keep pushing
        # the grace period out and leave instances never restarting onto the new build.
        state.serverapp_promoted_at = datetime.now(UTC)
    state.serverapp_version = build_id
    state.latest_remote_server_build = build_id
    _note_steam_session(config, state, None)
    save_state(config.state_file, state)
    jobs.finish(job_id, ok=True, detail=f"server app at build {build_id}")


def app_factory() -> FastAPI:  # pragma: no cover - uvicorn entry point
    """For `uvicorn dzsm.api:app_factory --factory`, using DZSM_CONFIG."""
    import os

    return create_app(Path(os.environ.get("DZSM_CONFIG", "config.toml")))
