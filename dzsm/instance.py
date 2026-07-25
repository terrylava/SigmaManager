"""Instance layout, keys, and launch arguments (spec §4).

The launch-arg construction here is the whole activation mechanism. There is no swap
step, no junction to create or delete — pointing an instance at a new mod version is
just building this list with a different path in it.
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

from dzsm.config import Config, InstanceConfig
from dzsm.state import State

_log = logging.getLogger(__name__)


class InstanceError(Exception):
    """Instance is not launchable."""


def build_mod_argument(mod_paths: list[Path]) -> str:
    """Build the -mod= value from absolute paths, in load order.

    The list order IS the load order — framework mods before their dependents. Callers
    must pass the order from InstanceConfig.mods, never a sorted or dict-derived one.

    The trailing semicolon matches Bohemia's own examples. It is harmless; keep it.
    """
    return f"-mod={';'.join(str(p) for p in mod_paths)};"


def build_launch_args(
    config: Config,
    instance_name: str,
    instance: InstanceConfig,
    server_exe: Path,
    mod_paths: list[Path],
) -> list[str]:
    """Build the full argv for one instance.

    A list, never a shell string, and the caller must never use shell=True. Passing argv
    verbatim is what makes backslashes, spaces, and drive letters safe without quoting
    gymnastics.

    Absolute paths in -mod= are CONFIRMED (spec §5 item 1, verified 2026-07-25 against
    DayZ 1.29 with verifySignatures = 2, server and mods on different drives). No
    per-instance copying is needed.
    """
    instance_root = config.instance_dir(instance_name)
    return [
        str(server_exe),
        f"-config={instance.config_file}",
        f"-port={instance.port}",
        build_mod_argument(mod_paths),
        f"-profiles={instance_root / 'profiles'}",
        "-dologs",
        "-adminlog",
        "-netlog",
        "-freezecheck",
        *instance.extra_args,
    ]


def server_directory(config: Config, state: State) -> Path:
    """Directory holding the DayZ server binary.

    Prefers config.server_app_path (an existing Steam install) and otherwise uses the
    version directory recorded in state. Raises InstanceError when neither is available
    — steam.update_server_app() is still a stub, so a fresh setup with no external
    install genuinely cannot run anything yet.
    """
    if config.server_app_path is not None:
        if not config.server_app_path.is_dir():
            raise InstanceError(
                f"server_app_path does not exist: {config.server_app_path}"
            )
        return config.server_app_path

    if state.serverapp_version is None:
        raise InstanceError(
            "no server app available: server_app_path is unset and no server app has "
            "been downloaded. Point server_app_path at a DayZ server installation."
        )
    return config.serverapp_dir / state.serverapp_version


def server_executable(config: Config, state: State) -> Path:
    """Absolute path to the server binary."""
    name = "DayZServer_x64.exe" if sys.platform == "win32" else "DayZServer"
    exe = server_directory(config, state) / name
    if not exe.is_file():
        raise InstanceError(f"server executable not found: {exe}")
    return exe


def ensure_layout(config: Config, state: State, instance_name: str) -> Path:
    """Create the instance directory skeleton if missing (spec §3).

    instances/<name>/{profiles,keys}/ plus serverDZ.cfg, seeded from the server
    installation's own config the first time. Returns the instance root.

    Never overwrites an existing serverDZ.cfg — that file is the user's, and clobbering a
    tuned server config on every start would be its own kind of data loss.
    """
    root = config.instance_dir(instance_name)
    for subdirectory in ("profiles", "keys"):
        (root / subdirectory).mkdir(parents=True, exist_ok=True)

    instance = config.instances[instance_name]
    destination = root / instance.config_file
    if not destination.exists():
        source = server_directory(config, state) / "serverDZ.cfg"
        if source.is_file():
            shutil.copy2(source, destination)
            _log.info(
                "instance %s: seeded %s from %s", instance_name, destination, source
            )
        else:
            raise InstanceError(
                f"instance {instance_name!r} has no {instance.config_file} and none "
                f"could be seeded from {source}"
            )

    return root


def sync_keys(config: Config, instance_name: str, paths: list[Path]) -> list[Path]:
    """Copy each mod's .bikey files into the instance's keys/ directory.

    Spec §5 item 2 is still open (~65%): a server loads its own mods without keys, so
    item 1's confirmation says nothing about this. The keys directory governs whether a
    connecting *client's* mods are accepted, which needs a client to test. Copying is
    cheap and harmless either way, so dzsm does it rather than betting on the answer.

    Returns the keys placed — empty list if the mods ship none, never None (Rule 1).
    """
    destination = config.instance_dir(instance_name) / "keys"
    destination.mkdir(parents=True, exist_ok=True)

    placed: list[Path] = []
    for mod_path in paths:
        # Mods are inconsistent about the casing of this directory.
        for directory_name in ("keys", "Keys"):
            key_directory = mod_path / directory_name
            if not key_directory.is_dir():
                continue
            for key in key_directory.glob("*.bikey"):
                target = destination / key.name
                try:
                    shutil.copy2(key, target)
                except OSError as exc:
                    # A key we cannot place is a client-side connection failure later,
                    # not a reason to abort the start (Rule 3, Rule 4).
                    _log.error(
                        "instance %s: could not copy key %s: %s", instance_name, key, exc
                    )
                    continue
                placed.append(target)
            break

    _log.debug("instance %s: %d key(s) in %s", instance_name, len(placed), destination)
    return placed
