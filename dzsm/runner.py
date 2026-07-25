"""Process supervision: start, stop, crash restart, scheduled restart (spec §1, §7).

Rule 4 lives here: a mod update failure must never block a restart. If activation can't
resolve a new version, the instance starts on its old one and the failure is logged
loudly. A server up with stale mods beats a server that is down.
"""

from __future__ import annotations

from dzsm.config import Config, InstanceConfig
from dzsm.state import State


class RunnerError(Exception):
    """Instance could not be started or stopped."""


def start(config: Config, state: State, instance_name: str) -> int:
    """Start one instance and return its pid.

    This is the only point at which mod activation happens: resolve the mod paths, write
    the pins into state, build argv, spawn. Downloads that landed while the server was
    running take effect here and nowhere else.

    Uses subprocess.Popen with an argv list. Never shell=True (spec §4).

    Rule 4: if resolving new versions fails, fall back to the instance's existing pins,
    log the failure with mod id + instance name + operation, and start anyway.
    """
    raise NotImplementedError("spec §7 Rule 4")


def stop(config: Config, instance_name: str, *, timeout: float = 60.0) -> None:
    """Stop an instance, giving it time to flush its persistence before killing it."""
    raise NotImplementedError("spec §1")


def is_running(config: Config, state: State, instance_name: str) -> bool:
    """True if the instance's process is alive (psutil, cross-platform)."""
    raise NotImplementedError("spec §1")


def supervise(config: Config, state: State, instance_names: list[str]) -> None:
    """Run the supervision loop: restart on crash and on schedule.

    Restart schedules are per-instance and deliberately staggered. Under this design
    that is a feature — two instances can run different versions of the same mod, so
    staggering no longer deadlocks updates the way it did in om.py.
    """
    raise NotImplementedError("spec §1, §2")


def next_restart(instance: InstanceConfig) -> str | None:
    """Next scheduled restart time as HH:MM, or None if the instance has no schedule."""
    raise NotImplementedError("spec §1")
