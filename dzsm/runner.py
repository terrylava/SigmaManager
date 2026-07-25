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
import time
from datetime import datetime, timedelta

import psutil

from dzsm.config import Config, InstanceConfig
from dzsm.instance import build_launch_args, ensure_layout, server_executable, sync_keys
from dzsm.mods import ModCacheError, mod_paths, resolve_mod_versions, server_mod_paths
from dzsm.state import ProcessInfo, State, save_state

_log = logging.getLogger(__name__)


class RunnerError(Exception):
    """Instance could not be started or stopped."""


def start(config: Config, state: State, instance_name: str) -> int:
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

    instance = config.instances[instance_name]
    exe = server_executable(config, state)
    ensure_layout(config, state, instance_name)

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
    sync_keys(config, state, instance_name, [*paths, *server_paths])
    argv = build_launch_args(config, instance_name, instance, exe, paths, server_paths)
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

    state.pins[instance_name] = dict(versions)
    state.processes[instance_name] = ProcessInfo(
        pid=process.pid,
        port=instance.port,
        started_at=datetime.now().astimezone(),
        create_time=_create_time(process.pid),
    )
    _log.info(
        "instance %s: started pid %s with versions %s",
        instance_name,
        process.pid,
        versions or "(no workshop mods)",
    )
    return process.pid


def stop(config: Config, state: State, instance_name: str, *, timeout: float | None = None) -> None:
    """Stop an instance, giving it time to flush persistence before killing it."""
    process = _live_process(config, state, instance_name)
    if process is None:
        _log.info("instance %s: not running", instance_name)
        state.processes.pop(instance_name, None)
        return

    grace = config.stop_timeout_seconds if timeout is None else timeout
    _log.info("instance %s: stopping pid %s (up to %.0fs)", instance_name, process.pid, grace)
    process.terminate()
    try:
        process.wait(timeout=grace)
    except psutil.TimeoutExpired:
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


def next_restart(instance: InstanceConfig, *, now: datetime | None = None) -> datetime | None:
    """Next scheduled restart, or None if the instance has no schedule.

    Times are local "HH:MM". An entry that has already passed today rolls to tomorrow.
    """
    if not instance.restart_schedule:
        return None

    current = now or datetime.now()
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

    return min(candidates) if candidates else None


def supervise(config: Config, state: State, instance_names: list[str]) -> None:
    """Run the supervision loop: restart on crash and on schedule.

    Restart schedules are deliberately staggered across instances. Under this design that
    is a feature — two instances can run different versions of the same mod, so
    staggering no longer deadlocks updates the way it did in om.py (spec §2).

    Runs until interrupted. State is saved after every transition so a crash of dzsm
    itself does not lose track of what it started.
    """
    unknown = [name for name in instance_names if name not in config.instances]
    if unknown:
        raise RunnerError(f"unknown instance(s): {', '.join(unknown)}")
    if not instance_names:
        raise RunnerError("no instances to supervise")

    due: dict[str, datetime | None] = {}
    for name in instance_names:
        if not is_running(config, state, name):
            _start_and_save(config, state, name, reason="initial start")
        due[name] = next_restart(config.instances[name])

    _log.info("supervising %s; Ctrl-C to stop", ", ".join(instance_names))
    try:
        while True:
            time.sleep(config.supervise_poll_seconds)
            now = datetime.now()

            for name in instance_names:
                scheduled = due[name]
                if scheduled is not None and now >= scheduled:
                    _log.info("instance %s: scheduled restart", name)
                    _restart_and_save(config, state, name)
                    due[name] = next_restart(config.instances[name], now=now)
                    continue

                if not is_running(config, state, name):
                    # Crash restart. Activation runs again here, so a mod that finished
                    # downloading since the last start is picked up now.
                    _log.warning("instance %s: process gone, restarting", name)
                    _start_and_save(config, state, name, reason="crash restart")
    except KeyboardInterrupt:
        _log.info("supervision interrupted; leaving instances running")


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
        stop(config, state, instance_name)
    except (RunnerError, psutil.Error) as exc:
        _log.error("instance %s: stop failed during restart: %s", instance_name, exc)
    _start_and_save(config, state, instance_name, reason="scheduled restart")


def _save(config: Config, state: State) -> None:
    try:
        save_state(config.state_file, state)
    except OSError as exc:
        _log.error("could not save state to %s: %s", config.state_file, exc)
