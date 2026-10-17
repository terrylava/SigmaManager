"""Instance layout, keys, and launch arguments (spec §4).

The launch-arg construction here is the whole activation mechanism. There is no swap
step, no junction to create or delete — pointing an instance at a new mod version is
just building this list with a different path in it.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
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
    config_file: Path | None = None,
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
    # The active copy by default: the engine locks whatever this points at, so it must
    # not be the file people edit.
    resolved_config = config_file or (instance_root / instance.config_file)
    args = [
        str(server_exe),
        # Absolute, not the bare filename. The working directory is the server
        # installation, so a relative -config resolves to the *server's* serverDZ.cfg and
        # every instance silently shares one — including hostname, instanceId,
        # verifySignatures, and the mission template.
        f"-config={resolved_config}",
        f"-port={instance.port}",
        build_mod_argument(mod_paths),
    ]

    # Omitted entirely when there are none. An empty -serverMod= is harmless but says
    # nothing, and its absence makes the launch line easier to read in the RPT.
    if server_mod_paths:
        args.append(build_mod_argument(server_mod_paths, option="serverMod"))

    args.append(f"-profiles={instance_root / 'profiles'}")

    if instance.cpu_count is not None:
        args.append(f"-cpuCount={instance.cpu_count}")

    # Toggleable rather than hardcoded: they are on by default because a server without
    # logs is much harder to diagnose, but -netlog in particular is noisy enough that
    # people turn it off on a busy server.
    for flag, enabled in (
        ("dologs", instance.dologs),
        ("adminlog", instance.adminlog),
        ("netlog", instance.netlog),
        ("freezecheck", instance.freezecheck),
    ):
        if enabled:
            args.append(f"-{flag}")

    args += instance.extra_args
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

    stash: Path | None = None
    if target.exists():
        reason = "refresh requested" if refresh else f"build {current} -> {build}"
        _log.info("instance %s: replacing isolated server copy (%s)", instance_name, reason)
        # Missions live inside the server directory, so replacing it would take this
        # instance's tuned economy and its persistence with it. Set them aside first.
        stash = _stash_custom_missions(target, source)
        safe_rmtree(target)

    _log.info(
        "instance %s: copying server build %s into %s (this is a few GB)",
        instance_name,
        build,
        target,
    )
    promote_tree(source, target, strategy=config.snapshot_strategy)
    if stash is not None:
        _restore_missions(stash, target / "mpmissions", instance_name)
    marker.write_text(build, encoding="utf-8")
    return target, build


def _stash_custom_missions(server_dir: Path, source: Path) -> Path | None:
    """Move missions the incoming build does not ship out of the way. None if there are
    none.

    Only missions absent from the new build are kept: vanilla's own come back with it,
    and preferring the new copy of those means map updates are not silently pinned to an
    old build's files.
    """
    existing = server_dir / "mpmissions"
    incoming = source / "mpmissions"
    if not existing.is_dir():
        return None

    custom = [
        mission
        for mission in existing.iterdir()
        if mission.is_dir() and not (incoming / mission.name).exists()
    ]
    if not custom:
        return None

    stash = server_dir.parent / f".missions-stash-{os.getpid()}"
    safe_rmtree(stash)
    stash.mkdir(parents=True)
    for mission in custom:
        shutil.move(str(mission), str(stash / mission.name))
    return stash


def _restore_missions(stash: Path, destination: Path, instance_name: str) -> None:
    """Put stashed missions back into a freshly copied server directory."""
    destination.mkdir(parents=True, exist_ok=True)
    for mission in list(stash.iterdir()):
        target = destination / mission.name
        if target.exists():
            # The new build ships one by this name after all; keep the new one and drop
            # the stashed copy rather than guessing which is wanted.
            _log.warning(
                "instance %s: new build already provides mission %s, discarding the "
                "stashed copy at %s",
                instance_name,
                mission.name,
                mission,
            )
            continue
        shutil.move(str(mission), str(target))
        _log.info(
            "instance %s: preserved mission %s across the build change",
            instance_name,
            mission.name,
        )
    safe_rmtree(stash)


def server_executable(server_dir: Path, override: str | None = None) -> Path:
    """Absolute path to the server binary inside a prepared server directory.

    `override` names a different binary in the same directory — a diagnostic build, say,
    which ships alongside the normal one rather than as its own download.
    """
    name = override or ("DayZServer_x64.exe" if sys.platform == "win32" else "DayZServer")
    exe = server_dir / name
    if not exe.is_file():
        available = (
            sorted(p.name for p in server_dir.glob("*") if p.is_file() and p.suffix in {"", ".exe"})
            if server_dir.is_dir()
            else []
        )
        raise InstanceError(
            f"server executable not found: {exe}"
            + (f". Binaries there: {', '.join(available)}" if override and available else "")
        )
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


def _mission_from_another_build(
    config: Config, mission: str, *, exclude: Path
) -> Path | None:
    """The same mission under a different server build, newest first.

    Version directories are kept, so after a server update the previous build still holds
    this instance's tuned economy and its persistence. `exclude` is the mpmissions
    directory being seeded into, so a build never carries forward from itself.
    """
    if not config.serverapp_dir.is_dir():
        return None

    candidates = [
        build
        for build in config.serverapp_dir.iterdir()
        if build.is_dir()
        and (build / "mpmissions") != exclude
        and (build / "mpmissions" / mission).is_dir()
    ]
    if not candidates:
        return None

    # Ordered by build id, which is the canonical version identity — the same reasoning
    # as mod versions. Directory mtime would not work: it does not change when a nested
    # file like db/types.xml is edited, so the most recently *used* build is invisible
    # to it.
    newest = max(candidates, key=lambda build: _build_sort_key(build.name))
    return newest / "mpmissions" / mission


def _build_sort_key(name: str) -> tuple[int, str]:
    """Numeric build ids sort numerically; anything else falls back to name order."""
    return (int(name), "") if name.isdigit() else (-1, name)


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


def effective_server_dir(config: Config, state: State, instance_name: str) -> Path:
    """Where this instance's server files are *now*, without preparing anything.

    The read-only counterpart to prepare_server_directory: an isolated instance reads
    from its own copy once that exists, and from the shared build before it does. Needed
    because listing what a server has is a question the web UI asks constantly, and
    answering it must never trigger a multi-GB copy.
    """
    if config.instances[instance_name].isolated_server:
        own = config.instance_dir(instance_name) / "server"
        if (own / "mpmissions").is_dir():
            return own
    source, _ = resolve_server_build(config, state, instance_name)
    return source


def shared_missions(config: Config, state: State) -> list[str]:
    """Missions in the build every instance shares, with no instance in hand.

    For a server that does not exist yet: it has no build pin and no isolated copy, so it
    will read from whichever build is newest. Returns a list, empty when nothing is
    installed — never None (Rule 1).
    """
    if state.serverapp_version is not None:
        server_dir = config.serverapp_dir / state.serverapp_version
    elif config.server_app_path is not None:
        server_dir = config.server_app_path
    else:
        return []
    missions = server_dir / "mpmissions"
    if not missions.is_dir():
        return []
    return sorted(p.name for p in missions.iterdir() if p.is_dir())


def mission_terrain(mission: str) -> str:
    """The terrain a mission name selects.

    DayZ parses `<MissionName>.<TerrainName>` and loads the world named by the suffix, so
    the suffix is not decoration — a directory called `x.chernarusplus` holding Livonia
    files produces a server that starts and is wrong.
    """
    return mission.split(".", 1)[1] if "." in mission else DEFAULT_TERRAIN


def available_missions(config: Config, state: State, instance_name: str) -> list[str]:
    """Mission directories this instance's server can load, sorted.

    Returns a list, empty when the server has none or is not installed yet — never None
    (Rule 1), and never raising, because this exists to populate a picker and a dropdown
    that cannot be drawn is worse than an empty one.
    """
    try:
        server_dir = effective_server_dir(config, state, instance_name)
    except InstanceError:
        return []
    missions = server_dir / "mpmissions"
    if not missions.is_dir():
        return []
    return sorted(p.name for p in missions.iterdir() if p.is_dir())


def resolved_mission_name(config: Config, instance_name: str, source: str) -> str:
    """What this instance's own mission directory will be called.

    The terrain comes from the *source*, not from anything the user typed, which is what
    keeps the suffix honest when someone switches map.
    """
    instance = config.instances[instance_name]
    if instance.mission is not None:
        return instance.mission
    return f"{config.mission_prefix}_{instance_name}.{mission_terrain(source)}"


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

    mission = resolved_mission_name(config, instance_name, source_name)
    target = missions / mission

    if not target.exists():
        # Carry the mission forward from another build before falling back to vanilla.
        # Missions live under the server directory because the engine resolves them
        # relative to it, so a server update would otherwise reset every instance's
        # economy to vanilla and orphan its persistence in the old build's directory.
        carried = _mission_from_another_build(config, mission, exclude=missions)
        if carried is not None:
            _log.info(
                "instance %s: carrying mission %s forward from %s",
                instance_name,
                mission,
                carried.parent.parent,
            )
            # Persistence comes too: the same instance's saved world should survive a
            # server update, unlike a mission freshly seeded from vanilla.
            promote_tree(carried, target)
        else:
            _log.info(
                "instance %s: seeding mission %s from %s",
                instance_name,
                mission,
                source_name,
            )
            # Persistence lives in storage_<instanceId>/ inside the mission. Copying it
            # would hand this instance another server's saved world, so a seeded mission
            # starts empty and the engine creates its own on first run.
            promote_tree(source, target, ignore=_ignore_persistence)

    write_template(config.instance_dir(instance_name) / instance.config_file, mission)
    return mission


def battleye_config_path(config: Config, instance_name: str) -> Path:
    """Where BattlEye reads its server settings for this instance.

    BattlEye puts its files under <profiles>/BattlEye/, and -profiles is per instance, so
    each instance has its own RCON password and port.
    """
    return config.instance_dir(instance_name) / "profiles" / "BattlEye" / "BEServer_x64.cfg"


def battleye_config_candidates(config: Config, instance_name: str) -> list[Path]:
    """Every file that might hold this instance's RCON settings, best first.

    **BattlEye consumes its config on startup**: it reads BEServer_x64.cfg and renames it
    to BEServer_x64_active_<pid>.cfg, so the file is gone by the time the server is up.
    Anything reading the password afterwards has to look at the renamed one, and anything
    deciding whether a password already exists has to as well — otherwise it would mint a
    new one on every single start and silently rotate the password out from under any
    admin tooling sharing it.
    """
    path = battleye_config_path(config, instance_name)
    candidates = [path] if path.is_file() else []
    if path.parent.is_dir():
        active = sorted(
            path.parent.glob("BEServer_x64_active_*.cfg"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        candidates.extend(active)
    return candidates


def read_battleye_password(config: Config, instance_name: str) -> str | None:
    """This instance's RCON password, from whichever config still has it."""
    for candidate in battleye_config_candidates(config, instance_name):
        password = read_battleye_setting(candidate, "RConPassword")
        if password:
            return password
    return None


def read_battleye_port(config: Config, instance_name: str) -> int | None:
    """This instance's configured RCON port, from whichever config still has it."""
    for candidate in battleye_config_candidates(config, instance_name):
        value = read_battleye_setting(candidate, "RConPort")
        if value and value.isdigit():
            return int(value)
    return None


def read_battleye_setting(path: Path, key: str) -> str | None:
    """One space-separated setting from a BEServer config, or None.

    The format is bare `Key value` lines — no quoting, no sections.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        name, _, value = stripped.partition(" ")
        if name.casefold() == key.casefold():
            return value.strip() or None
    return None


def ensure_battleye_config(config: Config, instance_name: str, port: int) -> Path | None:
    """Make sure this instance has a BattlEye config with an RCON password.

    BattlEye ships no default config, so without this there is no RConPassword, RCON
    never listens, and every restart warning fails against a port nothing is bound to.

    A password is generated with `secrets` when the file is missing or has none. An
    existing password is never touched — it may already be shared with admin tooling.

    The file is plaintext by necessity: BattlEye reads it directly and has no other way
    to be told. It is written under the instance's own profiles directory, not somewhere
    shared, and the value is never logged.

    Returns the config path, or None when management is disabled.
    """
    if not config.rcon.manage_battleye_config:
        return None

    path = battleye_config_path(config, instance_name)
    path.parent.mkdir(parents=True, exist_ok=True)

    if read_battleye_setting(path, "RConPassword"):
        return path

    # Reuse the password from a config BattlEye already consumed, rather than minting a
    # new one. Without this the password would change on every start, because BattlEye
    # renames the file it read and there is never a BEServer_x64.cfg to find.
    existing = read_battleye_password(config, instance_name)
    if existing:
        port = read_battleye_port(config, instance_name) or port
        path.write_text(
            "// Written by dzsm, restoring the password BattlEye consumed on the last\n"
            "// start. Delete RConPassword to have a new one generated.\n"
            f"RConPassword {existing}\n"
            f"RConPort {port}\n"
            "RestrictRCon 0\n",
            encoding="utf-8",
        )
        _log.debug("instance %s: restored the existing RCON password", instance_name)
        return path

    password = secrets.token_urlsafe(24)
    if path.is_file():
        # Keep whatever else is configured; only add what is missing.
        text = path.read_text(encoding="utf-8", errors="replace").rstrip("\n")
        lines = [text, f"RConPassword {password}"]
        if read_battleye_setting(path, "RConPort") is None:
            lines.append(f"RConPort {port}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _log.info("instance %s: added a generated RCON password to %s", instance_name, path)
    else:
        path.write_text(
            "// Written by dzsm. The password is generated and read back from here;\n"
            "// BattlEye requires it in plaintext. Delete RConPassword to have a new\n"
            "// one generated at the next start.\n"
            f"RConPassword {password}\n"
            f"RConPort {port}\n"
            "RestrictRCon 0\n",
            encoding="utf-8",
        )
        _log.info("instance %s: generated a BattlEye RCON config at %s", instance_name, path)

    return path


ACTIVE_CONFIG_SUFFIX = ".active.cfg"


def active_config_path(config: Config, instance_name: str) -> Path:
    """The serverDZ.cfg copy actually handed to the engine.

    Named apart from the editable one so it is obvious which is which in a directory
    listing.
    """
    instance = config.instances[instance_name]
    stem = Path(instance.config_file).stem
    return config.instance_dir(instance_name) / f"{stem}{ACTIVE_CONFIG_SUFFIX}"


def prepare_server_config(config: Config, instance_name: str) -> Path:
    """Copy the instance's serverDZ.cfg to the file the server will actually open.

    The engine holds its `-config` file open for the whole run, so whichever file is
    passed to it cannot be edited while the server is up — not in place, not by atomic
    replace. Launching from a copy keeps `serverDZ.cfg` itself editable at all times, and
    an edit takes effect at the next start, which is the only time the server reads it
    anyway.

    Regenerated on every start, so the copy can never drift from the file people edit.
    """
    source = config.instance_dir(instance_name) / config.instances[instance_name].config_file
    if not source.is_file():
        raise InstanceError(f"instance {instance_name!r} has no {source.name} at {source}")

    active = active_config_path(config, instance_name)
    header = (
        f"// Generated by dzsm from {source.name} at every start. Edit {source.name}\n"
        f"// instead — changes here are overwritten, and the running server holds this\n"
        f"// file open so it cannot be edited anyway.\n"
    )
    # utf-8-sig, not utf-8: a serverDZ.cfg saved by a Windows editor often starts with a
    # byte-order mark. Reading it as plain utf-8 keeps that as a U+FEFF character, and
    # prepending the header would then bury it mid-file, where it corrupts the first real
    # setting and the engine exits on startup with nothing useful in the RPT.
    body = source.read_text(encoding="utf-8-sig", errors="replace")
    active.write_text(header + body, encoding="utf-8")
    return active


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
