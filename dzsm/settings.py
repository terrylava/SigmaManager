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
        "rcon_port",
        "rcon_enabled",
        "extra_args",
        "config_file",
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
        return tomlkit.parse(path.read_text(encoding="utf-8"))
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


def mod_config_name(title: str, existing: Collection[str], workshop_id: int) -> str:
    """A config key for a Workshop mod, derived from its title.

    Keys are what instances refer to, so they have to be stable, readable, and unique.
    Anything outside letters, digits, underscore and dash is dropped rather than escaped —
    Workshop titles are full of emoji, brackets and version stamps, none of which belong
    in a name people type into a mods list.
    """
    # A *run* of unusable characters becomes one underscore, not one each: "Trader ✅ v1"
    # should not come out as "Trader___v1".
    cleaned = re.sub(r"_{2,}", "_", re.sub(r"[^A-Za-z0-9_-]+", "_", title)).strip("_-")
    candidate = cleaned[:48] or f"mod_{workshop_id}"
    if candidate[0].isdigit():
        candidate = f"m{candidate}"
    if candidate not in existing:
        return candidate
    # Collisions are real: several Workshop mods share a title.
    return f"{candidate}_{workshop_id}"


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

    entry = tomlkit.table()
    entry["kind"] = "workshop"
    entry["workshop_id"] = workshop_id
    mods[name] = entry
    return document


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
        return path.read_text(encoding="utf-8")
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
