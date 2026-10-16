"""Process supervision: start, stop, crash restart, scheduled restart (spec §1, §7).

Rule 4 lives here: a mod update failure must never block a restart. If activation can't
resolve a new version, the instance starts on its old one and the failure is logged
loudly. A server up with stale mods beats a server that is down.

Start is also the *only* moment activation happens. Everything downloaded while a server
was running takes effect here and nowhere else, which is what makes staggered restarts
safe rather than deadlocking (spec §2).
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import psutil

from dzsm.config import Config, ConfigError, InstanceConfig, load_config
from dzsm.instance import (
    battleye_config_path,
    build_launch_args,
    ensure_battleye_config,
    ensure_layout,
    prepare_mission,
    prepare_server_config,
    prepare_server_directory,
    read_battleye_password,
    read_battleye_port,
    server_executable,
    sync_keys,
)
from dzsm.mods import ModCacheError, mod_paths, resolve_mod_versions, server_mod_paths
from dzsm.rcon import RconClient, RconError
from dzsm.state import ProcessInfo, State, StateError, load_state, save_state

_log = logging.getLogger(__name__)


class RunnerError(Exception):
    """Instance could not be started or stopped."""


def start(
    config: Config, state: State, instance_name: str, *, refresh_server: bool = False
) -> int:
    """Start one instance and return its pid.

    Resolves which mod versions to activate, writes them into state as this instance's
    pins, builds argv, and spawns. Uses subprocess.Popen with an argv list — never
    shell=True (spec §4).

    Rule 4: if resolving newer versions fails, the instance falls back to the versions it
    was already pinned to, logs loudly, and starts anyway.
    """
    if instance_name not in config.instances:
        raise RunnerError(f"unknown instance {instance_name!r}")
    if is_running(config, state, instance_name):
        raise RunnerError(f"instance {instance_name!r} is already running")

    # Starting is the release: whatever held it down, this is someone asking for it up.
    if state.held.pop(instance_name, None) is not None:
        _log.info("instance %s: hold released", instance_name)

    instance = config.instances[instance_name]
    # Resolving (and, when isolated, copying) the server happens here rather than at
    # config load, because it is an activation decision like mod versions: a new build
    # takes effect at a restart, never under a running server.
    server_dir, build = prepare_server_directory(
        config, state, instance_name, refresh=refresh_server
    )
    exe = server_executable(server_dir, instance.server_executable)
    ensure_layout(config, server_dir, instance_name)
    mission = prepare_mission(config, server_dir, instance_name)
    # Before launch: BattlEye reads this at startup, so a password generated afterwards
    # would not take effect until the restart after next.
    rcon_port = instance.resolved_rcon_port()
    if rcon_port is not None:
        ensure_battleye_config(config, instance_name, rcon_port)

    versions = _versions_to_activate(config, state, instance_name)
    try:
        paths = mod_paths(config, instance_name, versions)
        server_paths = server_mod_paths(config, instance_name, versions)
    except ModCacheError as exc:
        raise RunnerError(
            f"instance {instance_name!r} cannot start: {exc}"
        ) from exc

    # Keys for both lists. A server-only mod's key is not strictly needed — clients never
    # load it — but omitting it would kick any player who happens to have that mod
    # enabled, and keys are tiny. Matches how admins populate the directory by hand.
    sync_keys(server_dir, instance_name, [*paths, *server_paths])
    # Launch from a copy, so serverDZ.cfg stays editable while the server runs. Made
    # after prepare_mission, which writes the template line into the editable file.
    active_config = prepare_server_config(config, instance_name)
    argv = build_launch_args(
        config, instance_name, instance, exe, paths, server_paths, active_config
    )
    _log.info("instance %s: starting on port %s", instance_name, instance.port)
    _log.debug("instance %s: argv %s", instance_name, argv)

    try:
        # Working directory is the server installation: DayZ resolves mpmissions/ and
        # addons/ relative to it. Mods do not need it — they are absolute (spec §5 #1).
        process = subprocess.Popen(  # noqa: S603 — argv list, no shell
            argv,
            cwd=str(exe.parent),
        )
    except OSError as exc:
        raise RunnerError(f"instance {instance_name!r} failed to start: {exc}") from exc

    _apply_process_tuning(instance, process.pid, instance_name)

    state.pins[instance_name] = dict(versions)
    state.server_builds[instance_name] = build
    state.processes[instance_name] = ProcessInfo(
        pid=process.pid,
        port=instance.port,
        started_at=datetime.now().astimezone(),
        create_time=_create_time(process.pid),
    )
    _log.info(
        "instance %s: started pid %s on server build %s, mission %s, versions %s",
        instance_name,
        process.pid,
        build,
        mission or "(shared)",
        versions or "(no workshop mods)",
    )
    return process.pid


def stop(
    config: Config,
    state: State,
    instance_name: str,
    *,
    timeout: float | None = None,
    hold: bool = True,
) -> None:
    """Stop an instance, giving it time to flush persistence before killing it.

    By default this also *holds* the instance down: the supervisor will not restart it,
    on crash or on schedule, until someone starts it again. An explicit stop that the
    supervisor immediately undoes is worse than no supervisor at all.

    `hold=False` is for a stop that is one half of a restart — the supervisor's own
    scheduled bounce, which is not a request for the instance to stay down.
    """
    if hold:
        state.held[instance_name] = datetime.now().astimezone()
        _log.info("instance %s: held down; `instance start` will release it", instance_name)

    process = _live_process(config, state, instance_name)
    if process is None:
        _log.info("instance %s: not running", instance_name)
        state.processes.pop(instance_name, None)
        return

    grace = config.stop_timeout_seconds if timeout is None else timeout
    _log.info("instance %s: stopping pid %s (up to %.0fs)", instance_name, process.pid, grace)

    # Escalating: ask nicely over RCON, then signal, then kill. DayZ flushes persistence
    # on its own shutdown path, so #shutdown is the only one that is properly clean.
    if _request_shutdown(config, state, instance_name) and _wait_for_exit(process, grace):
        state.processes.pop(instance_name, None)
        _log.info("instance %s: shut down via rcon", instance_name)
        return

    process.terminate()
    if not _wait_for_exit(process, grace):
        _log.warning(
            "instance %s: pid %s did not exit within %.0fs, killing",
            instance_name,
            process.pid,
            grace,
        )
        process.kill()
        with contextlib.suppress(psutil.Error):
            process.wait(timeout=10)

    state.processes.pop(instance_name, None)
    _log.info("instance %s: stopped", instance_name)


def is_running(config: Config, state: State, instance_name: str) -> bool:
    """True if the instance's process is alive."""
    return _live_process(config, state, instance_name) is not None


def next_restart(
    instance: InstanceConfig,
    *,
    started_at: datetime | None = None,
    now: datetime | None = None,
) -> datetime | None:
    """When this instance restarts next, or None if nothing schedules it.

    Two independent sources, whichever comes first:

    - `restart_schedule`, local "HH:MM" times. An entry already past today rolls over.
    - `restart_after_hours`, measured from when the instance actually started. Needs
      started_at; without it (the instance is down) there is no uptime to measure.
    """
    current = _local(now) or datetime.now()
    candidates: list[datetime] = []

    for entry in instance.restart_schedule:
        try:
            hour, _, minute = entry.partition(":")
            scheduled = current.replace(
                hour=int(hour), minute=int(minute), second=0, microsecond=0
            )
        except ValueError:
            _log.error("invalid restart_schedule entry %r, ignoring", entry)
            continue
        if scheduled <= current:
            scheduled += timedelta(days=1)
        candidates.append(scheduled)

    started = _local(started_at)
    if instance.restart_after_hours is not None and started is not None:
        candidates.append(started + timedelta(hours=instance.restart_after_hours))

    return min(candidates) if candidates else None


def _local(value: datetime | None) -> datetime | None:
    """Normalise to naive local time.

    State records timezone-aware timestamps; the scheduling arithmetic here is all naive
    local. Mixing the two raises, so everything is converted on the way in.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone().replace(tzinfo=None)


def rcon_port_for(config: Config, instance_name: str) -> int | None:
    """The port an instance's RCON listens on, or None when it is off or unknown.

    BattlEye's own config is the authority: it is what the server actually read. An
    explicit setting in dzsm's config overrides it, and the derived port is the last
    resort.
    """
    instance = config.instances[instance_name]
    if not instance.rcon_enabled:
        return None
    return (
        instance.rcon_port
        or read_battleye_port(config, instance_name)
        or instance.resolved_rcon_port()
    )


def rcon_password_for(config: Config, instance_name: str) -> str | None:
    """An instance's RCON password. The environment wins, then BattlEye's own config.

    Deliberately re-read rather than cached anywhere: dzsm can regenerate the BattlEye
    config, and a long-lived session holding a stale password would fail to reconnect
    forever with no indication why.
    """
    instance = config.instances[instance_name]
    return config.rcon.password(instance.rcon_password_env) or read_battleye_password(
        config, instance_name
    )


def rcon_client(config: Config, instance_name: str) -> RconClient | None:
    """A connected RCON client for this instance, or None when it is not configured.

    Returns None rather than raising for "not configured" — an instance without
    `rcon_port` has simply opted out, which is not an error. A configured instance that
    cannot connect *does* raise, because that is a broken setup worth hearing about.
    """
    instance = config.instances[instance_name]
    port = rcon_port_for(config, instance_name)
    if port is None:
        return None

    password = rcon_password_for(config, instance_name)
    if not password:
        env = instance.rcon_password_env or config.rcon.password_env
        raise RconError(
            f"instance {instance_name!r}: RCON is enabled (port {port}) but no password "
            f"is available — nothing under "
            f"{battleye_config_path(config, instance_name).parent} has an RConPassword "
            f"and {env} is unset. Start the instance once to have one generated, or turn "
            f"RCON off with rcon_enabled = false."
        )

    client = RconClient(
        config.rcon.host,
        port,
        password,
        timeout=config.rcon.timeout_seconds,
    )
    client.connect()
    return client


def seconds_since_start(state: State, instance_name: str) -> float | None:
    """How long this instance has been up, or None if dzsm did not start it."""
    process = state.processes.get(instance_name)
    started = _local(process.started_at) if process else None
    if started is None:
        return None
    return (datetime.now() - started).total_seconds()


def rcon_grace_remaining(config: Config, state: State, instance_name: str) -> float:
    """Seconds left before RCON is expected to answer. 0 once the window has passed.

    BattlEye binds well after the server is otherwise up, so an attempt inside this window
    is not evidence of a problem — it is simply early.
    """
    uptime = seconds_since_start(state, instance_name)
    if uptime is None:
        return 0.0
    return max(0.0, config.rcon.startup_grace_seconds - uptime)


def humanize(seconds: float) -> str:
    """A phrase players will read in chat: "5 minutes", "1 minute", "20 seconds"."""
    total = max(0, int(round(seconds)))
    if total >= 120:
        return f"{total // 60} minutes"
    if total >= 60:
        return "1 minute"
    return f"{total} seconds"


def supervise(
    config: Config,
    state: State,
    instance_names: list[str],
    *,
    stop: threading.Event | None = None,
    config_path: Path | None = None,
) -> None:
    """Run the supervision loop: restart on crash and on schedule.

    Restart schedules are deliberately staggered across instances. Under this design that
    is a feature — two instances can run different versions of the same mod, so
    staggering no longer deadlocks updates the way it did in om.py (spec §2).

    Runs until interrupted, or until `stop` is set — which is how the daemon shuts it
    down from another thread. State is saved after every transition so a crash of dzsm
    itself does not lose track of what it started, and re-read at the top of each pass so
    an instance started or stopped through the API is not clobbered by this loop's older
    in-memory copy.
    """
    unknown = [name for name in instance_names if name not in config.instances]
    if unknown:
        raise RunnerError(f"unknown instance(s): {', '.join(unknown)}")
    if not instance_names:
        raise RunnerError("no instances to supervise")

    due: dict[str, Due] = {}
    warned: dict[str, datetime] = {}
    for name in instance_names:
        if name in state.held:
            _log.info("instance %s: held down, not starting", name)
        elif not is_running(config, state, name):
            _start_and_save(config, state, name, reason="initial start")
        due[name] = _due_at(config, state, name)

    _log.info("supervising %s; Ctrl-C to stop", ", ".join(instance_names))
    waiter = stop or threading.Event()
    try:
        while not waiter.is_set():
            # wait(), not sleep(): a daemon shutting down should not have to sit through
            # a full poll interval.
            if waiter.wait(_tick_seconds(config, due)):
                break
            now = datetime.now()
            state = _reload(config, state)
            # Config can be edited through the web UI while this is running; without
            # re-reading it, a changed restart schedule or affinity would not take effect
            # until the daemon restarted.
            if config_path is not None:
                config = _reload_config(config_path, config)

            for name in instance_names:
                if name in state.held:
                    # Deliberately down. Not a crash, not due a restart — leave it alone
                    # until someone starts it.
                    continue

                scheduled = due[name].at
                updating = due[name].update
                if scheduled is not None and now >= scheduled:
                    _log.info(
                        "instance %s: %s restart",
                        name,
                        "server build update" if updating else "scheduled",
                    )
                    _warn_players(
                        config, state, name, warned, remaining=0.0, final=True, update=updating
                    )
                    _restart_and_save(config, state, name)
                    warned.pop(name, None)
                    # Recomputed after the restart, so an uptime-based window starts
                    # counting from the new process rather than the old one.
                    due[name] = _due_at(config, state, name)
                    continue

                if scheduled is not None:
                    # _warn_players decides which tier applies, or stays quiet.
                    _warn_players(
                        config,
                        state,
                        name,
                        warned,
                        remaining=(scheduled - now).total_seconds(),
                        update=updating,
                    )

                if not is_running(config, state, name):
                    # Crash restart. Activation runs again here, so a mod that finished
                    # downloading since the last start is picked up now.
                    _log.warning("instance %s: process gone, restarting", name)
                    _start_and_save(config, state, name, reason="crash restart")
                    warned.pop(name, None)
                    due[name] = _due_at(config, state, name)
    except KeyboardInterrupt:
        _log.info("supervision interrupted; leaving instances running")
    else:
        _log.info("supervision stopped; leaving instances running")


def _reload_config(path: Path, current: Config) -> Config:
    """Re-read config, keeping the running one if the file is broken.

    Someone editing config.toml by hand can leave it briefly unparseable. That must not
    take down a supervisor that is otherwise perfectly able to keep servers up.
    """
    try:
        return load_config(path)
    except ConfigError as exc:
        _log.error("config is currently invalid, continuing with the loaded one: %s", exc)
        return current


def _reload(config: Config, current: State) -> State:
    """Re-read state, keeping the in-memory copy if the file is unreadable.

    The API mutates state from another thread of the same process; without this, the
    supervisor's older copy would overwrite those changes at its next save.
    """
    try:
        return load_state(config.state_file)
    except StateError as exc:
        _log.error("could not re-read state, continuing with the in-memory copy: %s", exc)
        return current


# --- internals ---------------------------------------------------------------


def _versions_to_activate(config: Config, state: State, instance_name: str) -> dict[str, int]:
    """Versions for this start, falling back to the existing pins (Rule 4).

    A server up with slightly stale mods beats a server that is down, so a resolution
    failure never propagates — it degrades to whatever the instance last ran.
    """
    try:
        return resolve_mod_versions(config, state, instance_name)
    except ModCacheError as exc:
        previous = state.pins.get(instance_name)
        if not previous:
            # Nothing to fall back to. This is a genuine first-start failure.
            raise

        # Drop entries for mods this instance no longer references. Even on the fallback
        # path, pins must describe what is actually being loaded — a stale name left here
        # would keep claiming a version forever and block its deletion.
        instance = config.instances[instance_name]
        referenced = {*instance.mods, *instance.server_mods}
        fallback = {name: version for name, version in previous.items() if name in referenced}

        _log.error(
            "instance %s: could not resolve current mod versions (%s); starting on the "
            "previous versions %s instead",
            instance_name,
            exc,
            fallback,
        )
        return fallback


def warning_interval(config: Config, remaining: float) -> int | None:
    """How often to warn at this distance from a restart, or None to stay quiet.

    Two tiers: every 5 minutes from 30 minutes out, then every 4 seconds for the last 5.
    """
    if remaining <= config.rcon.final_warning_lead_seconds:
        return config.rcon.final_warning_interval_seconds
    if remaining <= config.rcon.early_warning_lead_seconds:
        return config.rcon.early_warning_interval_seconds
    return None


def _tick_seconds(config: Config, due: Mapping[str, Due]) -> float:
    """How long to sleep before the next pass.

    The poll interval, shortened when an instance is close enough to a restart that
    warnings need to go out faster — a 15s poll cannot deliver a message every 4s. Never
    lengthened past the poll interval: crash detection still has to happen on time during
    the slow early tier.
    """
    now = datetime.now()
    tick = float(config.supervise_poll_seconds)
    for entry in due.values():
        scheduled = entry.at
        if scheduled is None:
            continue
        remaining = (scheduled - now).total_seconds()
        if remaining <= 0:
            continue
        interval = warning_interval(config, remaining)
        if interval is not None:
            tick = min(tick, float(interval))
    return tick


def _warn_players(
    config: Config,
    state: State,
    instance_name: str,
    warned: dict[str, datetime],
    *,
    remaining: float,
    final: bool = False,
    update: bool = False,
) -> None:
    """Announce an incoming restart over RCON, as global admin chat (`say -1`).

    Rate-limited to the configured interval, and completely non-fatal: an unreachable
    RCON port must never delay or prevent the restart itself (Rule 4). A failure is
    logged once per attempt and the loop carries on.

    `update` picks the game-update wording over the routine-restart one.
    """
    now = datetime.now()
    last = warned.get(instance_name)
    interval = warning_interval(config, remaining)
    if not final:
        if interval is None:
            return
        if last is not None and (now - last).total_seconds() < interval:
            return

    # A restart scheduled very soon after a start (a short restart_after_hours, or a
    # crash-restart loop) can put a warning inside the window where RCON cannot answer.
    if rcon_grace_remaining(config, state, instance_name) > 0:
        return

    template = (
        config.rcon.update_warning_message if update else config.rcon.warning_message
    )
    message = template.format(remaining=humanize(remaining))
    try:
        client = rcon_client(config, instance_name)
    except RconError as exc:
        _log.error("instance %s: rcon unavailable for restart warning: %s", instance_name, exc)
        warned[instance_name] = now
        return
    if client is None:
        return

    try:
        client.say(message)
        warned[instance_name] = now
    except RconError as exc:
        _log.error("instance %s: could not send restart warning: %s", instance_name, exc)
        warned[instance_name] = now
    finally:
        client.close()


def priority_value(name: str) -> int:
    """Map a priority name to what psutil.nice() wants on this platform.

    Windows takes priority *classes*; POSIX takes nice values, where lower is higher
    priority — the inversion is the whole reason this mapping exists rather than a raw
    number in config.
    """
    if sys.platform == "win32":
        classes = {
            "idle": psutil.IDLE_PRIORITY_CLASS,
            "below_normal": psutil.BELOW_NORMAL_PRIORITY_CLASS,
            "normal": psutil.NORMAL_PRIORITY_CLASS,
            "above_normal": psutil.ABOVE_NORMAL_PRIORITY_CLASS,
            "high": psutil.HIGH_PRIORITY_CLASS,
            "realtime": psutil.REALTIME_PRIORITY_CLASS,
        }
        return int(classes[name])
    nice = {
        "idle": 19,
        "below_normal": 10,
        "normal": 0,
        "above_normal": -5,
        "high": -10,
        "realtime": -20,
    }
    return nice[name]


def _apply_process_tuning(instance: InstanceConfig, pid: int, instance_name: str) -> None:
    """Set CPU affinity and priority on a freshly started server.

    Never fatal: a server running on the wrong cores is worth far more than a server that
    refused to start because affinity could not be set. Both are applied after spawning
    because there is no portable way to ask Popen for them.
    """
    if instance.cpu_affinity is None and instance.priority is None:
        return

    try:
        process = psutil.Process(pid)
    except psutil.Error as exc:
        _log.error("instance %s: could not tune process %s: %s", instance_name, pid, exc)
        return

    if instance.cpu_affinity is not None:
        available = psutil.cpu_count() or 1
        cores = sorted({core for core in instance.cpu_affinity if 0 <= core < available})
        ignored = sorted(set(instance.cpu_affinity) - set(cores))
        if ignored:
            _log.warning(
                "instance %s: ignoring cores %s, this machine has %s",
                instance_name,
                ignored,
                available,
            )
        if not cores:
            _log.error(
                "instance %s: cpu_affinity names no core this machine has; leaving it "
                "on all %s",
                instance_name,
                available,
            )
        else:
            try:
                process.cpu_affinity(cores)
                _log.info("instance %s: pinned to cores %s", instance_name, cores)
            except (psutil.Error, OSError) as exc:
                _log.error("instance %s: could not set cpu affinity: %s", instance_name, exc)

    if instance.priority is not None:
        try:
            process.nice(priority_value(instance.priority))
            _log.info("instance %s: priority %s", instance_name, instance.priority)
        except (psutil.Error, OSError) as exc:
            # Raising priority usually needs privilege; saying so beats a bare errno.
            _log.error(
                "instance %s: could not set priority %s (this usually needs elevation): %s",
                instance_name,
                instance.priority,
                exc,
            )


@dataclass(frozen=True)
class Due:
    """When an instance restarts next, and what is asking for it.

    The reason travels with the time because it decides what players are told: a routine
    restart and a game update read very differently to someone who has just been unable
    to connect.
    """

    at: datetime | None
    # True when the soonest deadline is a newly downloaded server build rather than the
    # ordinary schedule.
    update: bool = False


def build_restart_due(
    config: Config, state: State, instance_name: str
) -> datetime | None:
    """When this instance should restart to pick up a newer server build, if ever.

    Derived rather than stored, which is what makes it self-clearing: once the instance
    restarts, `state.server_builds[name]` equals the promoted build and this returns None
    on its own. A stored deadline would need clearing at exactly the right moment, and a
    missed clear means a restart loop.

    Returns None for an instance pinned with `server_build` — that pin is a deliberate
    statement about which build to run, and a grace period is not permission to override
    it. The divergence is reported instead.
    """
    grace = config.server_update_grace_minutes
    if grace <= 0:
        return None

    target = state.serverapp_version
    promoted = _local(state.serverapp_promoted_at)
    if target is None or promoted is None:
        return None
    if config.instances[instance_name].server_build is not None:
        return None

    running = state.server_builds.get(instance_name)
    # None means the instance has never started under a recorded build, so there is
    # nothing to move it off of; it will pick the newest up whenever it does start.
    if running is None or running == target:
        return None

    return promoted + timedelta(minutes=grace)


def _due_at(config: Config, state: State, instance_name: str) -> Due:
    """When this instance is next due to restart, using its current process start time."""
    process = state.processes.get(instance_name)
    scheduled = next_restart(
        config.instances[instance_name],
        started_at=process.started_at if process else None,
    )
    update = build_restart_due(config, state, instance_name)

    if update is not None and (scheduled is None or update <= scheduled):
        return Due(at=update, update=True)
    return Due(at=scheduled)


def _request_shutdown(config: Config, state: State, instance_name: str) -> bool:
    """Ask the server to stop over RCON. True if the request went out.

    False means "fall through to signalling it" — not configured, still starting up,
    unreachable, or the server died before replying. Never raises: stopping must work with
    RCON broken.
    """
    if not config.rcon.shutdown_via_rcon:
        return False

    remaining = rcon_grace_remaining(config, state, instance_name)
    if remaining > 0:
        # No point waiting out a socket timeout against a port BattlEye has not bound yet.
        _log.info(
            "instance %s: only just started, RCON not expected for another %.0fs — "
            "signalling instead",
            instance_name,
            remaining,
        )
        return False
    try:
        client = rcon_client(config, instance_name)
    except RconError as exc:
        _log.warning("instance %s: rcon unavailable for shutdown: %s", instance_name, exc)
        return False
    if client is None:
        return False

    try:
        client.shutdown()
    except RconError as exc:
        # The server frequently dies before answering, which surfaces as a missing reply.
        # Treat it as sent and let the wait below decide whether it actually worked.
        _log.debug("instance %s: no reply to #shutdown (%s)", instance_name, exc)
    finally:
        client.close()
    return True


def _wait_for_exit(process: psutil.Process, timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
    except psutil.TimeoutExpired:
        return False
    except psutil.Error:
        return True
    return True


def _create_time(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


def _live_process(config: Config, state: State, instance_name: str) -> psutil.Process | None:
    """The instance's running process, or None.

    Guards against pid reuse: a recorded pid that now belongs to something else must not
    be mistaken for a live server, or dzsm would refuse to start an instance that is
    actually down.
    """
    info = state.processes.get(instance_name)
    if info is None:
        return None

    try:
        process = psutil.Process(info.pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        if info.create_time is not None and abs(process.create_time() - info.create_time) > 1.0:
            _log.debug(
                "instance %s: pid %s was reused by another process", instance_name, info.pid
            )
            return None
    except psutil.Error:
        return None
    return process


def _start_and_save(config: Config, state: State, instance_name: str, *, reason: str) -> None:
    try:
        start(config, state, instance_name)
    except (RunnerError, ModCacheError) as exc:
        _log.error("instance %s: %s failed: %s", instance_name, reason, exc)
    finally:
        _save(config, state)


def _restart_and_save(config: Config, state: State, instance_name: str) -> None:
    try:
        # hold=False: this stop is one half of a restart, not a request to stay down.
        stop(config, state, instance_name, hold=False)
    except (RunnerError, psutil.Error) as exc:
        _log.error("instance %s: stop failed during restart: %s", instance_name, exc)
    _start_and_save(config, state, instance_name, reason="scheduled restart")


def _save(config: Config, state: State) -> None:
    try:
        save_state(config.state_file, state)
    except OSError as exc:
        _log.error("could not save state to %s: %s", config.state_file, exc)
