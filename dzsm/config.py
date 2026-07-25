"""Configuration models and loading (spec §3, §4, §10).

Every other module takes its types from here. Validation happens once, at load, so no
downstream code has to re-check whether a mod name resolves or whether a path is sane.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

# Steam application ids. The server app and the workshop content live under different
# ids — workshop mods are published against the *client* app.
SERVER_APPID = 223350
WORKSHOP_APPID = 221100

# Windows path ceiling. Mod trees are deep (a mod's own addons/ tree routinely runs
# 120+ chars) and a version directory adds ~30 more, so the root has to stay tiny.
MAX_PATH = 260
MAX_ROOT_PATH_LEN = 40


class ConfigError(Exception):
    """Configuration is unusable. Raised at load; never swallowed (Rule 3)."""


class WorkshopMod(BaseModel):
    """A mod steamcmd downloads and dzsm version-caches."""

    kind: Literal["workshop"] = "workshop"
    workshop_id: int
    # Set to freeze an instance-independent version, e.g. while chasing a regression.
    # None means "track latest".
    pinned_version: int | None = None


class LocalMod(BaseModel):
    """A mod that lives on disk and is never touched by steamcmd (spec §9)."""

    kind: Literal["local"] = "local"
    path: Path
    # Local mods are pinned by definition — update logic skips them.
    pinned: Literal[True] = True


Mod = Annotated[WorkshopMod | LocalMod, Field(discriminator="kind")]


class SteamConfig(BaseModel):
    """steamcmd credentials and location (spec §6).

    Anonymous login does not work for DayZ: both 223350 and workshop downloads require
    an account that owns the game.
    """

    username: str
    # Name of the env var holding the password. The password itself is never in config.
    password_env: str = "DZSM_STEAM_PASSWORD"  # noqa: S105 — a var name, not a secret
    steamcmd_path: Path
    # Download steamcmd from Valve when it isn't at steamcmd_path. Set false on a locked
    # down box where fetching an executable at runtime is not acceptable; dzsm then
    # fails with instructions instead.
    auto_install: bool = True

    def password(self) -> str | None:
        """Read the password from the environment. None if unset."""
        return os.environ.get(self.password_env)


class InstanceConfig(BaseModel):
    """One server instance."""

    port: int = 2302
    config_file: str = "serverDZ.cfg"
    # ORDER IS LOAD ORDER (spec §4). This list is the only source of truth for it.
    # Never sort it, never rebuild it from the mods table's iteration order.
    mods: list[str]
    # "HH:MM" local times.
    restart_schedule: list[str] = Field(default_factory=list)
    extra_args: list[str] = Field(default_factory=list)


class Config(BaseModel):
    """Top-level config, loaded from TOML."""

    root: Path
    steam: SteamConfig
    mods: dict[str, Mod] = Field(default_factory=dict)
    instances: dict[str, InstanceConfig] = Field(default_factory=dict)
    # Optional retention window, in days. None (the default) means nothing is ever
    # deleted automatically — versions accumulate until someone removes one. Setting a
    # number opts in to automatic pruning of versions that are unused, not the newest for
    # their mod, and older than this. See spec §8.
    gc_retention_days: int | None = None
    # "hardlink" is the untested optimization in spec §12. It corrupts old version
    # directories if steamcmd writes files in place. Do not default it on.
    snapshot_strategy: Literal["copy", "hardlink"] = "copy"
    # Escape hatch for the MAX_PATH guard. Only set this if you know the fully resolved
    # mod paths stay under 260 chars — the check is a length heuristic on the root, not
    # a measurement of the deepest file that will actually exist.
    allow_long_root: bool = False
    # steamcmd can legitimately take a long time on a big mod.
    download_timeout_seconds: int = 3600
    # Path to an existing DayZ server installation. Interim: until update_server_app()
    # lands, dzsm cannot download appid 223350 itself, so instances run against a server
    # installed by Steam. When unset, the server app is taken from
    # serverapp/<state.serverapp_version>/ instead.
    server_app_path: Path | None = None
    # How often the supervision loop checks liveness and the restart schedule.
    supervise_poll_seconds: int = 15
    # Grace period for a server to shut down before it is killed. DayZ flushes
    # persistence on exit, so do not make this small.
    stop_timeout_seconds: int = 60

    # --- derived paths (spec §3) ---

    @property
    def steamcmd_dir(self) -> Path:
        """Where steamcmd lives, derived from the executable rather than assumed.

        steamcmd puts downloads under its own install directory. Deriving this from
        steamcmd_path means the download paths are right even when the binary isn't at
        the spec §3 location.
        """
        return self.steam.steamcmd_path.parent

    @property
    def workshop_download_dir(self) -> Path:
        """steamcmd's own mutable working dir. Tier 1 — overwritten in place."""
        return self.steamcmd_dir / "steamapps" / "workshop" / "content" / str(WORKSHOP_APPID)

    @property
    def workshop_acf(self) -> Path:
        """steamcmd's workshop manifest. Its mtime is one of the download checks."""
        return self.steamcmd_dir / "steamapps" / "workshop" / f"appworkshop_{WORKSHOP_APPID}.acf"

    @property
    def serverapp_dir(self) -> Path:
        """Tier 2 for the server app — immutable serverapp/<buildid>/ directories."""
        return self.root / "serverapp"

    @property
    def serverapp_download_dir(self) -> Path:
        """Tier 1 for the server app: steamcmd's mutable working copy, overwritten in
        place on every update. Kept out of serverapp/ so it can never be mistaken for a
        version directory."""
        return self.root / ".serverapp-download"

    @property
    def modcache_dir(self) -> Path:
        """Tier 2 — immutable version snapshots. Nothing here is ever overwritten."""
        return self.root / "modcache"

    @property
    def localmods_dir(self) -> Path:
        return self.root / "localmods"

    @property
    def instances_dir(self) -> Path:
        return self.root / "instances"

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    def version_dir(self, workshop_id: int, version: int) -> Path:
        """Absolute path of one immutable version snapshot."""
        return self.modcache_dir / str(workshop_id) / str(version)

    def instance_dir(self, name: str) -> Path:
        return self.instances_dir / name

    # --- validation ---

    @field_validator("root")
    @classmethod
    def _root_must_be_absolute(cls, v: Path) -> Path:
        if not v.is_absolute():
            raise ValueError(f"root must be an absolute path, got {v!r}")
        return v

    @model_validator(mode="after")
    def _root_must_be_short_on_windows(self) -> Config:
        # Windows only: PATH_MAX on Linux is 4096, so there is nothing to guard there.
        #
        # Deliberately NOT conditional on long_paths_enabled(). The registry flag only
        # takes effect for processes whose manifest opts in; Python's does, but
        # DayZServer_x64.exe is a 2013-era engine and almost certainly does not. A
        # config that passes validation because *we* can handle long paths, and then
        # fails when the server can't load a mod, is worse than failing here.
        if sys.platform != "win32" or self.allow_long_root:
            return self
        length = len(str(self.root))
        if length > MAX_ROOT_PATH_LEN:
            hint = (
                ""
                if long_paths_enabled()
                else " (Win32 long path support is also off on this machine)"
            )
            raise ValueError(
                f"root is {length} chars ({self.root}); keep it under "
                f"{MAX_ROOT_PATH_LEN}{hint}. Mod trees plus version directories will "
                f"otherwise exceed MAX_PATH ({MAX_PATH}) at runtime, and the server "
                f"binary cannot be assumed to be long-path aware. Set "
                f"allow_long_root = true to override."
            )
        return self

    @model_validator(mode="after")
    def _instances_reference_known_mods(self) -> Config:
        for instance_name, instance in self.instances.items():
            for mod_name in instance.mods:
                if mod_name not in self.mods:
                    raise ValueError(
                        f"instance {instance_name!r} references unknown mod {mod_name!r}; "
                        f"known mods: {sorted(self.mods)}"
                    )
            duplicates = {m for m in instance.mods if instance.mods.count(m) > 1}
            if duplicates:
                raise ValueError(
                    f"instance {instance_name!r} lists {sorted(duplicates)} more than once"
                )
        return self

    @field_validator("gc_retention_days")
    @classmethod
    def _retention_must_be_positive(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError(
                f"gc_retention_days must be at least 1 day, got {v}. To disable "
                f"automatic deletion entirely, omit it or set it to null."
            )
        return v

    @model_validator(mode="after")
    def _ports_are_unique(self) -> Config:
        seen: dict[int, str] = {}
        for name, instance in self.instances.items():
            if instance.port in seen:
                raise ValueError(
                    f"instances {seen[instance.port]!r} and {name!r} both use port "
                    f"{instance.port}"
                )
            seen[instance.port] = name
        return self


def long_paths_enabled() -> bool:
    """True if Win32 long path support is on (or we're not on Windows)."""
    if sys.platform != "win32":
        return True
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\FileSystem",
        )
        with key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
        return bool(value)
    except OSError:
        return False


def load_config(path: Path) -> Config:
    """Load and validate TOML config.

    Raises ConfigError with something actionable in it — never returns a partial or
    falsy Config (Rule 1, Rule 3).
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read config at {path}: {exc}") from exc

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid config in {path}:\n{exc}") from exc
