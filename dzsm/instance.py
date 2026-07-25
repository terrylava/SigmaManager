"""Instance layout, keys, and launch arguments (spec §4).

The launch-arg construction here is the whole activation mechanism. There is no swap
step, no junction to create or delete — pointing an instance at a new mod version is
just building this list with a different path in it.
"""

from __future__ import annotations

import logging
import re
import shutil
import sys
from pathlib import Path

from dzsm.config import Config, InstanceConfig
from dzsm.mods import promote_tree, safe_rmtree
from dzsm.state import State

_log = logging.getLogger(__name__)


class InstanceError(Exception):
    """Instance is not launchable."""


def build_mod_argument(mod_paths: list[Path], *, option: str = "mod") -> str:
    """Build a -mod= or -serverMod= value from absolute paths, in load order.

    The list order IS the load order — framework mods before their dependents. Callers
    must pass the order from InstanceConfig.mods, never a sorted or dict-derived one.

    The trailing semicolon matches Bohemia's own examples. It is harmless; keep it.
    """
    return f"-{option}={';'.join(str(p) for p in mod_paths)};"


def build_launch_args(
    config: Config,
    instance_name: str,
    instance: InstanceConfig,
    server_exe: Path,
    mod_paths: list[Path],
    server_mod_paths: list[Path] | None = None,
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
    args = [
        str(server_exe),
        # Absolute, not the bare filename. The working directory is the server
        # installation, so a relative -config resolves to the *server's* serverDZ.cfg and
        # every instance silently shares one — including hostname, instanceId,
        # verifySignatures, and the mission template.
        f"-config={instance_root / instance.config_file}",
        f"-port={instance.port}",
        build_mod_argument(mod_paths),
    ]

    # Omitted entirely when there are none. An empty -serverMod= is harmless but says
    # nothing, and its absence makes the launch line easier to read in the RPT.
    if server_mod_paths:
        args.append(build_mod_argument(server_mod_paths, option="serverMod"))

    args += [
        f"-profiles={instance_root / 'profiles'}",
        "-dologs",
        "-adminlog",
        "-netlog",
        "-freezecheck",
        *instance.extra_args,
    ]
    return args


BUILD_MARKER = ".dzsm-build"
EXTERNAL_BUILD = "external"


def resolve_server_build(
    config: Config, state: State, instance_name: str
) -> tuple[Path, str]:
    """The shared build directory this instance should run, and its label.

    Resolution order, most specific first:

    1. The instance's own `server_build` pin, from serverapp/<build>/.
    2. `config.server_app_path`, an externally installed server. Labelled "external"
       because dzsm did not download it and cannot know its build id.
    3. The newest downloaded build, from state.

    An instance pin beats the global external path deliberately — it is the more specific
    statement of intent, and it is the whole point of being able to hold one instance on
    a known-good build.
    """
    instance = config.instances[instance_name]

    if instance.server_build is not None:
        directory = config.serverapp_dir / instance.server_build
        if not directory.is_dir():
            available = (
                sorted(p.name for p in config.serverapp_dir.iterdir() if p.is_dir())
                if config.serverapp_dir.is_dir()
                else []
            )
            raise InstanceError(
                f"instance {instance_name!r} is pinned to server build "
                f"{instance.server_build!r}, which is not downloaded. Available: "
                f"{', '.join(available) or 'none'}"
            )
        return directory, instance.server_build

    if config.server_app_path is not None:
        if not config.server_app_path.is_dir():
            raise InstanceError(f"server_app_path does not exist: {config.server_app_path}")
        return config.server_app_path, EXTERNAL_BUILD

    if state.serverapp_version is None:
        raise InstanceError(
            "no server app available: no build has been downloaded and server_app_path "
            "is unset. Run `dzsm server update`, or point server_app_path at an existing "
            "DayZ server installation."
        )
    return config.serverapp_dir / state.serverapp_version, state.serverapp_version


def prepare_server_directory(
    config: Config,
    state: State,
    instance_name: str,
    *,
    refresh: bool = False,
) -> tuple[Path, str]:
    """The directory this instance will actually run from, ready to use.

    For a shared instance that is the build directory itself. For an isolated one it is
    instances/<name>/server/, holding a full private copy — its own keys/, its own
    mission files, no state shared with any other instance.

    The copy is made when it is missing, when the resolved build has changed, or when
    refresh is set. A marker file records which build the copy holds; an externally
    installed server is always labelled "external", so dzsm cannot tell when it has been
    updated underneath — use refresh after updating one.

    Only ever called with the instance stopped (runner.start checks first), so the copy
    is never rewritten while a server holds locks on it.
    """
    source, build = resolve_server_build(config, state, instance_name)
    instance = config.instances[instance_name]
    if not instance.isolated_server:
        return source, build

    target = config.instance_dir(instance_name) / "server"
    marker = target / BUILD_MARKER
    current = marker.read_text(encoding="utf-8").strip() if marker.is_file() else None

    if target.is_dir() and current == build and not refresh:
        return target, build

    if target.exists():
        reason = "refresh requested" if refresh else f"build {current} -> {build}"
        _log.info("instance %s: replacing isolated server copy (%s)", instance_name, reason)
        safe_rmtree(target)

    _log.info(
        "instance %s: copying server build %s into %s (this is a few GB)",
        instance_name,
        build,
        target,
    )
    promote_tree(source, target, strategy=config.snapshot_strategy)
    marker.write_text(build, encoding="utf-8")
    return target, build


def server_executable(server_dir: Path) -> Path:
    """Absolute path to the server binary inside a prepared server directory."""
    name = "DayZServer_x64.exe" if sys.platform == "win32" else "DayZServer"
    exe = server_dir / name
    if not exe.is_file():
        raise InstanceError(f"server executable not found: {exe}")
    return exe


def ensure_layout(config: Config, server_dir: Path, instance_name: str) -> Path:
    """Create the instance directory skeleton if missing (spec §3).

    instances/<name>/{profiles,keys}/ plus serverDZ.cfg, seeded from the server
    installation's own config the first time. Returns the instance root.

    Never overwrites an existing serverDZ.cfg — that file is the user's, and clobbering a
    tuned server config on every start would be its own kind of data loss.
    """
    root = config.instance_dir(instance_name)
    # No keys/ here: the engine reads keys from the server installation, so sync_keys
    # targets <server dir>/keys/ instead.
    (root / "profiles").mkdir(parents=True, exist_ok=True)

    instance = config.instances[instance_name]
    destination = root / instance.config_file
    if not destination.exists():
        source = server_dir / "serverDZ.cfg"
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


TEMPLATE_PATTERN = re.compile(r'^(\s*template\s*=\s*)"([^"]*)"', re.MULTILINE)
DEFAULT_TERRAIN = "chernarusplus"


def _ignore_persistence(directory: str, names: list[str]) -> set[str]:
    """Skip storage_* when seeding a mission — that is another server's saved world."""
    return {name for name in names if name.startswith("storage_")}


def read_template(config_file: Path) -> str | None:
    """The mission named by a serverDZ.cfg's `template`, or None."""
    try:
        text = config_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = TEMPLATE_PATTERN.search(text)
    return match.group(2) if match else None


def write_template(config_file: Path, mission: str) -> None:
    """Point a serverDZ.cfg's `template` at a mission, preserving everything else.

    dzsm owns this one line. An instance whose template does not match its own mission
    directory would silently run a shared one, which is the collision per-instance
    missions exist to prevent — so it is kept in sync rather than merely validated.
    """
    text = config_file.read_text(encoding="utf-8", errors="replace")
    replacement = f'\\g<1>"{mission}"'
    if TEMPLATE_PATTERN.search(text):
        updated = TEMPLATE_PATTERN.sub(replacement, text, count=1)
    else:
        updated = text.rstrip("\n") + f'\ntemplate="{mission}";\n'
    if updated != text:
        config_file.write_text(updated, encoding="utf-8")


def prepare_mission(config: Config, server_dir: Path, instance_name: str) -> str | None:
    """Give this instance its own mission directory, seeded from a vanilla one.

    Mission folders hold types.xml, events.xml, cfgspawnabletypes.xml, init.c *and* the
    persistence storage, so instances sharing one clobber each other's economy and saves.
    Different maps per instance is the common case — a set of servers sharing mods but
    running different terrains with their own loot tables.

    The engine has no mission path argument: it resolves `mpmissions/<template>` relative
    to the server directory. Per-instance missions therefore mean per-instance mission
    *names* inside that directory, which is also why this costs tens of MB rather than
    the several GB of a full isolated server.

    Never overwrites an existing mission directory — once seeded, it is yours to edit.
    Returns the mission name, or None when the feature is off.
    """
    instance = config.instances[instance_name]
    if not config.per_instance_missions and instance.mission is None:
        return None

    missions = server_dir / "mpmissions"
    if not missions.is_dir():
        # Nothing to seed from. Not an error: the server simply ships no missions, and
        # failing the start over it would be worse than running whatever it does have.
        _log.warning(
            "instance %s: %s does not exist, skipping per-instance mission setup",
            instance_name,
            missions,
        )
        return None

    available = sorted(p.name for p in missions.iterdir() if p.is_dir())
    # Ordered candidates for what to copy from. The first that actually exists wins —
    # "exists" matters because after the first run the instance's own template names its
    # per-instance mission, and if that directory is later deleted the instance must be
    # able to fall back and re-seed rather than deadlocking on a missing source.
    if instance.mission_source is not None:
        # Explicitly configured: a name that does not exist is a config mistake, and
        # quietly substituting something else would hide it.
        if not (missions / instance.mission_source).is_dir():
            raise InstanceError(
                f"instance {instance_name!r}: mission_source "
                f"{instance.mission_source!r} not found in {missions}. Available: "
                f"{', '.join(available) or 'none'}"
            )
        source_name: str | None = instance.mission_source
    else:
        candidates = [
            read_template(config.instance_dir(instance_name) / instance.config_file),
            read_template(server_dir / "serverDZ.cfg"),
            available[0] if len(available) == 1 else None,
        ]
        source_name = next(
            (name for name in candidates if name and (missions / name).is_dir()), None
        )

    if source_name is None:
        raise InstanceError(
            f"instance {instance_name!r}: no usable mission to seed from. Set "
            f"mission_source to one of: {', '.join(available) or 'none'}"
        )

    source = missions / source_name

    terrain = source_name.split(".", 1)[1] if "." in source_name else DEFAULT_TERRAIN
    mission = instance.mission or f"{instance_name}.{terrain}"
    target = missions / mission

    if not target.exists():
        _log.info(
            "instance %s: seeding mission %s from %s", instance_name, mission, source_name
        )
        # Persistence lives in storage_<instanceId>/ inside the mission. Copying it would
        # hand this instance another server's saved world, so a seeded mission starts
        # empty and the engine creates its own on first run.
        promote_tree(source, target, ignore=_ignore_persistence)

    write_template(config.instance_dir(instance_name) / instance.config_file, mission)
    return mission


def sync_keys(server_dir: Path, instance_name: str, paths: list[Path]) -> list[Path]:
    """Add each mod's .bikey files to the *server's* keys/ directory.

    The engine reads keys from the server installation, not from -profiles, so they go to
    `<server dir>/keys/`. For a shared instance that is inside a promoted
    `serverapp/<buildid>/` directory, which everything else treats as immutable —
    deliberately excepted here, and safe:

    - Keys are only ever **added**, never overwritten or deleted. The immutability rule
      exists because a running server holds OS-level locks on files it has open; creating
      a new small file alongside them breaks nothing.
    - An existing key of the same name is left alone rather than re-copied, so a file a
      running server may have open is never touched.

    Instances sharing a build share one key set, so a mod trusted on one is trusted on
    all of them. Set `isolated_server` on an instance to avoid that: it gets its own
    server copy and therefore its own keys/, which is what om.py's per-instance copies
    gave you.

    Returns the keys newly added — empty list when everything was already present, never
    None (Rule 1).
    """
    destination = server_dir / "keys"
    destination.mkdir(parents=True, exist_ok=True)

    added: list[Path] = []
    for mod_path in paths:
        # Mods are inconsistent about the casing of this directory.
        for directory_name in ("keys", "Keys"):
            key_directory = mod_path / directory_name
            if not key_directory.is_dir():
                continue
            for key in key_directory.glob("*.bikey"):
                target = destination / key.name
                if target.exists():
                    continue
                try:
                    shutil.copy2(key, target)
                except OSError as exc:
                    # A key we cannot place is a client-side connection failure later,
                    # not a reason to abort the start (Rule 3, Rule 4).
                    _log.error(
                        "instance %s: could not add key %s: %s", instance_name, key, exc
                    )
                    continue
                added.append(target)
                _log.info("instance %s: added key %s", instance_name, target.name)
            break

    return added
