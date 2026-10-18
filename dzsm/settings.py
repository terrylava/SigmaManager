"""Editing config.toml programmatically, for the web UI.

Three rules govern everything here, because this rewrites the file the whole tool depends
on:

1. **Comments survive.** The generated config explains what every setting does. Rewriting
   it from a parsed dict would throw all of that away on the first save, so edits go
   through tomlkit, which round-trips style.
2. **Nothing invalid is ever written.** A change is applied to a copy, validated by
   loading it as a real Config, and only then does it touch the disk. A config that fails
   to load is worse than a rejected edit — every command stops working.
3. **The previous version is kept.** Written atomically, with the file it replaced saved
   alongside, so a bad-but-valid change is recoverable.
"""

from __future__ import annotations

import itertools
import logging
import os
import re
import shutil
import tempfile
import tomllib
from collections.abc import Collection
from pathlib import Path
from typing import Any

import tomlkit
from pydantic import ValidationError
from tomlkit import TOMLDocument

from dzsm.config import Config, ConfigError

# Where a first instance starts, and how far apart to space new ones. Four is the minimum
# that avoids a collision; 100 is the conventional spacing and leaves room to add a few
# ports to an instance later without reshuffling everything after it.
DEFAULT_GAME_PORT = 2302
PORT_SPACING = 100

_log = logging.getLogger(__name__)

BACKUP_SUFFIX = ".bak"

# Instance settings the UI may change. Deliberately a whitelist: everything here is
# either a scalar or a list of scalars, so an edit cannot restructure the document, and
# anything absent has to be edited in the file by hand.
EDITABLE_INSTANCE_FIELDS = frozenset(
    {
        "port",
        "mods",
        "server_mods",
        "keep_mods",
        "restart_schedule",
        "restart_after_hours",
        "cpu_affinity",
        "priority",
        "cpu_count",
        "dologs",
        "adminlog",
        "netlog",
        "freezecheck",
        "server_build",
        "server_executable",
        "isolated_server",
        "mission",
        "mission_source",
        "steam_query_port",
        "rcon_port",
        "rcon_enabled",
        "rcon_live",
        "extra_args",
        "config_file",
    }
)


# Manager-wide settings the UI may change, as dotted paths into the document. Same
# whitelist reasoning as EDITABLE_INSTANCE_FIELDS, with one addition: `root` is
# deliberately absent. Every path in the tool derives from it, so changing it through a
# form would orphan the mod cache, the instances, and the state file all at once, and
# nothing here moves any of them. That edit belongs in the file, deliberately.
EDITABLE_GLOBAL_FIELDS = frozenset(
    {
        "gc_retention_days",
        "snapshot_strategy",
        "download_timeout_seconds",
        "server_appid",
        "server_branch",
        "per_instance_missions",
        "mission_prefix",
        "supervise_poll_seconds",
        "stop_timeout_seconds",
        "mod_check_interval_minutes",
        "mod_check_auto_download",
        "allow_long_root",
        "api.host",
        "api.port",
        "rcon.host",
        "rcon.timeout_seconds",
        "rcon.startup_grace_seconds",
        "rcon.early_warning_lead_seconds",
        "rcon.early_warning_interval_seconds",
        "rcon.final_warning_lead_seconds",
        "rcon.final_warning_interval_seconds",
        "rcon.warning_message",
        "rcon.shutdown_via_rcon",
        "rcon.manage_battleye_config",
    }
)


class SettingsError(Exception):
    """An edit was rejected. The file on disk is unchanged."""


class FileLocked(SettingsError):
    """The file is open by a running process and cannot be replaced.

    On Windows a running DayZ server holds serverDZ.cfg open, so os.replace fails with
    access denied — the same locking constraint the whole mod-cache design is built
    around, surfacing on the config file too.
    """


def read_document(path: Path) -> TOMLDocument:
    """Parse config.toml preserving comments and formatting."""
    try:
        # utf-8-sig for the same reason as load_config: an editor-added byte-order mark
        # must not make the file unparseable. Saving writes plain UTF-8, so a round trip
        # through here quietly drops the mark rather than preserving it.
        return tomlkit.parse(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise SettingsError(f"cannot read {path}: {exc}") from exc
    except Exception as exc:  # tomlkit raises several parse error types
        raise SettingsError(f"{path} is not valid TOML: {exc}") from exc


def validate(document: TOMLDocument) -> Config:
    """Load a document as a real Config, so an invalid edit never reaches disk."""
    text = tomlkit.dumps(document)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"edit produced invalid TOML: {exc}") from exc
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise SettingsError(_readable(exc)) from exc


def apply_instance_patch(
    document: TOMLDocument, instance_name: str, patch: dict[str, Any]
) -> TOMLDocument:
    """Set instance fields in the document. None removes a key, restoring its default.

    Does not validate — callers pair this with validate() so a rejected edit can report
    what was wrong without having touched anything.
    """
    instances = document.get("instances")
    if instances is None or instance_name not in instances:
        raise SettingsError(f"no instance {instance_name!r} in the config")

    unknown = sorted(set(patch) - EDITABLE_INSTANCE_FIELDS)
    if unknown:
        raise SettingsError(
            f"these settings cannot be edited here: {', '.join(unknown)}. "
            f"Editable: {', '.join(sorted(EDITABLE_INSTANCE_FIELDS))}"
        )

    table = instances[instance_name]
    for key, value in patch.items():
        if value is None:
            table.pop(key, None)
        else:
            table[key] = value
    return document


def apply_global_patch(document: TOMLDocument, patch: dict[str, Any]) -> TOMLDocument:
    """Set manager-wide settings, keyed by dotted path. None removes a key.

    Dotted rather than nested, so the whitelist can name `rcon.timeout_seconds` without
    also permitting a whole `rcon` table to be replaced wholesale — a nested patch could
    drop every sibling key in one request, which is not an edit anyone means to make from
    a form.
    """
    unknown = sorted(set(patch) - EDITABLE_GLOBAL_FIELDS)
    if unknown:
        raise SettingsError(
            f"these settings cannot be edited here: {', '.join(unknown)}. "
            f"Editable: {', '.join(sorted(EDITABLE_GLOBAL_FIELDS))}"
        )

    for dotted, value in patch.items():
        head, _, leaf = dotted.rpartition(".")
        table: Any = document
        if head:
            existing = document.get(head)
            if existing is None:
                existing = tomlkit.table()
                document[head] = existing
            table = existing
        if value is None:
            table.pop(leaf, None)
        else:
            table[leaf] = value
    return document


def global_values(config: Config) -> dict[str, Any]:
    """Current values of the editable manager settings, by the same dotted paths."""
    values: dict[str, Any] = {}
    for dotted in EDITABLE_GLOBAL_FIELDS:
        target: Any = config
        for part in dotted.split("."):
            target = getattr(target, part)
        values[dotted] = target
    return values


def mod_config_name(title: str, existing: Collection[str], workshop_id: int) -> str:
    """A config key for a Workshop mod, derived from its title.

    Keys are what instances refer to, so they have to be stable, readable, and unique.
    Anything outside letters, digits, underscore and dash is dropped rather than escaped —
    Workshop titles are full of emoji, brackets and version stamps, none of which belong
    in a name people type into a mods list.
    """
    candidate = _clean_name(title) or f"mod_{workshop_id}"
    if candidate not in existing:
        return candidate
    # Collisions are real: several Workshop mods share a title.
    return f"{candidate}_{workshop_id}"


def _clean_name(text: str) -> str:
    """Reduce arbitrary text to something usable as a config key.

    A *run* of unusable characters becomes one underscore, not one each: "Trader ✅ v1"
    should not come out as "Trader___v1".
    """
    cleaned = re.sub(r"_{2,}", "_", re.sub(r"[^A-Za-z0-9_-]+", "_", text)).strip("_-")[:48]
    if cleaned and cleaned[0].isdigit():
        cleaned = f"m{cleaned}"
    return cleaned


def add_workshop_mod(
    document: TOMLDocument, name: str, workshop_id: int
) -> TOMLDocument:
    """Add a [mods.<name>] entry. Refuses to overwrite an existing one."""
    mods = document.setdefault("mods", tomlkit.table(is_super_table=True))
    if name in mods:
        raise SettingsError(f"a mod named {name!r} is already configured")

    for existing_name, existing in mods.items():
        if existing.get("workshop_id") == workshop_id:
            raise SettingsError(
                f"workshop id {workshop_id} is already configured as {existing_name!r}"
            )

    entry = _new_mod_table()
    entry["kind"] = "workshop"
    entry["workshop_id"] = workshop_id
    entry.add(tomlkit.nl())
    mods[name] = entry
    return document


def add_local_mod(document: TOMLDocument, name: str, path: Path) -> TOMLDocument:
    """Add a [mods.<name>] entry pointing at a directory already on disk.

    For mods dzsm did not download: a hand-built mod, one from an existing server
    install, or a Steam-subscribed copy. steamcmd never touches these, and they have no
    versions — the path is used directly (spec §9).
    """
    mods = document.setdefault("mods", tomlkit.table(is_super_table=True))
    if name in mods:
        raise SettingsError(f"a mod named {name!r} is already configured")

    resolved = path.resolve()
    for existing_name, existing in mods.items():
        if existing.get("kind") != "local":
            continue
        if Path(str(existing.get("path"))).resolve() == resolved:
            raise SettingsError(f"{resolved} is already configured as {existing_name!r}")

    entry = _new_mod_table()
    entry["kind"] = "local"
    entry["path"] = str(resolved)
    entry.add(tomlkit.nl())
    mods[name] = entry
    return document


def _new_table(label: str) -> Any:
    """A table that will read correctly wherever tomlkit decides to put it.

    tomlkit appends a new sub-table at the end of its super-table's rendered span, which
    in a commented config lands *after* whatever section comments follow the last entry —
    so the new entry can appear to be what a comment above it describes. It cannot be
    repositioned (a dotted-key table append is refused outright), so instead the table
    labels itself and is separated by blank lines on both sides.
    """
    table = tomlkit.table()
    table.trivia.indent = "\n"
    table.comment(label)
    return table


def _new_mod_table() -> Any:
    return _new_table("added by dzsm")


def local_mod_name(directory: Path, existing: Collection[str]) -> str:
    """A config key for a local mod, from its directory name.

    DayZ mod folders are conventionally "@Something"; the marker is noise in a name
    instances refer to, so it is dropped.
    """
    candidate = _clean_name(directory.name.lstrip("@")) or "local_mod"
    if candidate not in existing:
        return candidate
    # No workshop id to disambiguate with, so count up instead.
    for suffix in itertools.count(2):
        alternative = f"{candidate}_{suffix}"
        if alternative not in existing:
            return alternative
    raise AssertionError("unreachable")


def add_mod_to_instance(
    document: TOMLDocument, instance_name: str, mod_name: str
) -> TOMLDocument:
    """Append a mod to an instance's load order.

    Appended, never inserted: the order is the load order, and dzsm has no way to know
    where a new mod belongs relative to the frameworks already listed.
    """
    instances = document.get("instances")
    if instances is None or instance_name not in instances:
        raise SettingsError(f"no instance {instance_name!r} in the config")

    table = instances[instance_name]
    mods = table.get("mods")
    if mods is None:
        mods = tomlkit.array()
        table["mods"] = mods
    if mod_name in list(mods):
        return document
    mods.append(mod_name)
    return document


MOD_LISTS = ("mods", "server_mods", "keep_mods")


def set_mod_instances(
    document: TOMLDocument,
    mod_name: str,
    list_name: str,
    instance_names: Collection[str],
) -> TOMLDocument:
    """Set which instances carry a mod in one of their three lists.

    Ticked instances get it appended to `list_name`; unticked ones lose it from that list
    only, so unloading a mod does not silently discard a `keep_mods` claim on it.

    Adding to one list removes it from the other two for that instance, because the three
    are mutually exclusive: config validation rejects a mod that is both loaded and kept,
    or both client and server-only. Doing it here means a tick cannot produce a config
    that will not load.
    """
    if list_name not in MOD_LISTS:
        raise SettingsError(f"unknown list {list_name!r}; expected one of {', '.join(MOD_LISTS)}")

    mods = document.get("mods")
    if mods is None or mod_name not in mods:
        raise SettingsError(f"no mod named {mod_name!r} in the config")

    instances = document.get("instances") or {}
    unknown = sorted(set(instance_names) - set(instances))
    if unknown:
        raise SettingsError(f"unknown instance(s): {', '.join(unknown)}")

    wanted = set(instance_names)
    for instance_name, table in instances.items():
        if instance_name in wanted:
            for other in MOD_LISTS:
                if other != list_name:
                    _drop(table, other, mod_name)
            _ensure(table, list_name, mod_name)
        else:
            _drop(table, list_name, mod_name)
    return document


def _ensure(table: Any, list_name: str, mod_name: str) -> None:
    array = table.get(list_name)
    if array is None:
        array = tomlkit.array()
        table[list_name] = array
    if mod_name not in list(array):
        array.append(mod_name)


def _drop(table: Any, list_name: str, mod_name: str) -> None:
    array = table.get(list_name)
    if array is None:
        return
    while mod_name in list(array):
        array.remove(mod_name)


INSTANCE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def instance_name_error(name: str) -> str | None:
    """Why this name cannot be an instance, or None if it can.

    Stricter than a TOML key needs to be, because the name is not just a label: it becomes
    a directory under `instances/`, and it goes inside the derived mission name, where a
    dot would break DayZ's "<MissionName>.<TerrainName>" parsing and a space would break
    the launch arguments.
    """
    if not name:
        return "a server needs a name"
    if not INSTANCE_NAME_PATTERN.match(name):
        return (
            f"{name!r} cannot be used: a server name may contain only letters, digits, "
            f"underscore and dash. It becomes a folder under instances/ and part of the "
            f"mission name, where anything else breaks."
        )
    return None


def suggest_port(config: Config) -> int:
    """A free game port for a new instance.

    Steps up from the highest existing port rather than filling the lowest gap: a gap is
    usually deliberate — leaving 2302 alone because something else on the box already
    uses it, say — and quietly reusing it is how a new server collides with a thing dzsm
    cannot see.
    """
    if not config.instances:
        return DEFAULT_GAME_PORT
    highest = max(instance.port for instance in config.instances.values())
    return highest + PORT_SPACING


def add_instance(
    document: TOMLDocument,
    name: str,
    *,
    port: int,
    mods: Collection[str] = (),
    mission_source: str | None = None,
) -> TOMLDocument:
    """Add an `[instances.<name>]` table.

    Deliberately minimal: a port, a load order, and optionally which map to seed from.
    Everything else has a default that works, and a new instance littered with explicit
    copies of every default is harder to read and freezes those defaults forever.
    """
    problem = instance_name_error(name)
    if problem is not None:
        raise SettingsError(problem)

    instances = document.setdefault("instances", tomlkit.table(is_super_table=True))
    if name in instances:
        raise SettingsError(f"a server named {name!r} already exists")

    known = set(document.get("mods") or {})
    unknown = sorted(set(mods) - known)
    if unknown:
        raise SettingsError(
            f"unknown mod(s): {', '.join(unknown)}. Configure them first; known: "
            f"{', '.join(sorted(known)) or 'none'}"
        )

    # Same positioning problem as a new [mods.X]: tomlkit appends it at the end of the
    # super-table's rendered span, which in a commented config lands after whatever
    # section comments follow the last instance. So it labels itself.
    entry = _new_table("added by dzsm")
    entry["port"] = port
    # An array even when empty, so the load order is visibly a list to edit rather than a
    # key someone has to know to add.
    array = tomlkit.array()
    for mod_name in mods:
        array.append(mod_name)
    entry["mods"] = array
    if mission_source is not None:
        entry["mission_source"] = mission_source
    entry.add(tomlkit.nl())
    instances[name] = entry
    return document


def set_instance_mod(
    document: TOMLDocument, instance_name: str, mod_name: str, list_name: str
) -> TOMLDocument:
    """Put one mod in one of one instance's three lists, leaving other instances alone.

    The single-server counterpart to set_mod_instances: that one answers "which servers
    load this mod", this one answers "does this server load this mod". Moving between
    lists is the same operation as adding, since the three are mutually exclusive.
    """
    if list_name not in MOD_LISTS:
        raise SettingsError(f"unknown list {list_name!r}; expected one of {', '.join(MOD_LISTS)}")

    mods = document.get("mods")
    if mods is None or mod_name not in mods:
        raise SettingsError(f"no mod named {mod_name!r} in the config")

    table = _instance_table(document, instance_name)
    for other in MOD_LISTS:
        if other != list_name:
            _drop(table, other, mod_name)
    _ensure(table, list_name, mod_name)
    return document


def remove_mod_from_instance(
    document: TOMLDocument, instance_name: str, mod_name: str
) -> TOMLDocument:
    """Take a mod out of one instance entirely, leaving it configured and downloaded.

    Removes it from all three lists: "this server does not use this mod" has no useful
    reading in which a keep_mods claim survives, unlike unticking one list in the
    assignment panel.
    """
    table = _instance_table(document, instance_name)
    for list_name in MOD_LISTS:
        _drop(table, list_name, mod_name)
    return document


def replace_mod_in_instance(
    document: TOMLDocument, instance_name: str, old_name: str, new_name: str
) -> TOMLDocument:
    """Swap one mod for another in an instance, *at the same position*.

    Position matters: the list is the load order, and a framework has to precede the mods
    that extend it. Removing and appending would silently move the replacement to the end
    of the load order, which for something like CF breaks every mod that depends on it.
    """
    table = _instance_table(document, instance_name)
    found = False
    for list_name in MOD_LISTS:
        array = table.get(list_name)
        if array is None:
            continue
        names = list(array)
        if old_name not in names:
            continue
        found = True
        if new_name in names and new_name != old_name:
            raise SettingsError(
                f"instance {instance_name!r} already lists {new_name!r} in {list_name}"
            )
        # tomlkit arrays support index assignment and preserve surrounding formatting.
        array[names.index(old_name)] = new_name
    if not found:
        raise SettingsError(f"instance {instance_name!r} does not use {old_name!r}")
    return document


def _instance_table(document: TOMLDocument, instance_name: str) -> Any:
    instances = document.get("instances")
    if instances is None or instance_name not in instances:
        raise SettingsError(f"no instance {instance_name!r} in the config")
    return instances[instance_name]


def instances_using(document: TOMLDocument, mod_name: str) -> list[str]:
    """Instances that reference a mod anywhere — loaded, server-only, or merely kept."""
    using = []
    for instance_name, table in (document.get("instances") or {}).items():
        for key in ("mods", "server_mods", "keep_mods"):
            if mod_name in list(table.get(key) or []):
                using.append(instance_name)
                break
    return sorted(using)


def remove_mod_from_config(
    document: TOMLDocument, name: str, *, strip_from_instances: bool = False
) -> TOMLDocument:
    """Remove a [mods.<name>] entry.

    Refuses while an instance still references it, because config validation would reject
    the result anyway — an instance naming a mod that does not exist will not load. With
    `strip_from_instances` the name is pulled out of every load order first, which is a
    real edit to those instances and so is never the default.
    """
    mods = document.get("mods")
    if mods is None or name not in mods:
        raise SettingsError(f"no mod named {name!r} in the config")

    using = instances_using(document, name)
    if using and not strip_from_instances:
        raise SettingsError(
            f"{name!r} is still used by {', '.join(using)}. Remove it from them first, "
            f"or pass force to take it out of their load orders too."
        )

    for table in (document.get("instances") or {}).values():
        for key in ("mods", "server_mods", "keep_mods"):
            array = table.get(key)
            if array is None:
                continue
            while name in list(array):
                array.remove(name)

    del mods[name]
    return document


def save(path: Path, document: TOMLDocument) -> Config:
    """Validate, back up the current file, and write atomically.

    Returns the Config the new file loads as, so a caller can adopt it immediately rather
    than re-reading and hoping.
    """
    config = validate(document)
    write_text(path, tomlkit.dumps(document))
    return config


def write_text(path: Path, text: str) -> None:
    """Replace a file atomically, keeping the previous contents alongside it."""
    if path.exists():
        try:
            shutil.copy2(path, path.with_suffix(path.suffix + BACKUP_SUFFIX))
        except OSError as exc:
            # Not fatal: losing the backup is worth less than refusing a valid edit, but
            # it should be visible if it starts happening.
            _log.error("could not back up %s: %s", path, exc)

    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    except PermissionError as exc:
        # Windows refuses to replace a file another process has open. For serverDZ.cfg
        # that process is the running server, and there is nothing to do about it from
        # here — say which file and why rather than surfacing a bare errno.
        temp.unlink(missing_ok=True)
        raise FileLocked(
            f"{path.name} is open by a running process, so it cannot be replaced. Stop "
            f"the instance and try again — the change would only take effect at its next "
            f"start anyway."
        ) from exc
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def save_raw(path: Path, text: str) -> Config:
    """Validate raw TOML text and write it. For editing the whole file directly."""
    try:
        document = tomlkit.parse(text)
    except Exception as exc:
        raise SettingsError(f"not valid TOML: {exc}") from exc
    return save(path, document)


def read_config_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SettingsError(f"cannot read {path}: {exc}") from exc


def _readable(exc: ValidationError) -> str:
    """Turn pydantic's report into something that fits in a toast.

    The raw form includes the whole input document, which for a config is unreadable in a
    browser and can echo back more than anyone wants to look at.
    """
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "config"
        lines.append(f"{location}: {error['msg']}")
    return "; ".join(lines) or str(exc)


def config_error_to_settings_error(exc: ConfigError) -> SettingsError:
    return SettingsError(str(exc))
