"""Instance layout, keys, and launch arguments (spec §4).

The launch-arg construction here is the whole activation mechanism. There is no swap
step, no junction to create or delete — pointing an instance at a new mod version is
just building this list with a different path in it.
"""

from __future__ import annotations

from pathlib import Path

from dzsm.config import Config, InstanceConfig


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


def ensure_layout(config: Config, instance_name: str) -> Path:
    """Create the instance directory skeleton if missing (spec §3).

    instances/<name>/{profiles,mpmissions,keys}/ plus serverDZ.cfg. Returns the instance
    root.
    """
    raise NotImplementedError("spec §3")


def sync_keys(config: Config, instance_name: str, mod_paths: list[Path]) -> list[Path]:
    """Copy each mod's .bikey files into the instance's keys/ directory.

    Verification item #2 in spec §5 (~65% confidence): it is not confirmed that keys
    must be copied when the mod lives outside the server directory. Test with
    verifySignatures=2 and check whether a client can connect.

    Returns the keys placed. Empty list if the mods ship none.
    """
    raise NotImplementedError("spec §5 item 2")
