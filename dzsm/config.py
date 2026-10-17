"""Configuration models and loading (spec §3, §4, §10).

Every other module takes its types from here. Validation happens once, at load, so no
downstream code has to re-check whether a mod name resolves or whether a path is sane.
"""

from __future__ import annotations

import os
import re
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

# BattlEye's RCON port sits three above the game port in a stock DayZ layout: a server on
# 2302 listens for RCON on 2305.
RCON_PORT_OFFSET = 3

# How many consecutive ports the game itself binds from its game port: the port, +1 for
# Steam query, +2 for the Steam master link.
GAME_PORT_SPAN = 3

# With RCON at +3, one instance occupies four consecutive ports, so game ports have to be
# at least this far apart. 2302 and 2402 is the conventional spacing; 2302 and 2304 is a
# collision that looks fine in config.
PORT_BLOCK_SIZE = RCON_PORT_OFFSET + 1


# Characters that make a string more than a bare name: path separators (both, since a
# config written on Windows is read on Linux and vice versa), a drive-letter colon, and
# control characters. Used to keep settings that get joined onto a managed directory from
# escaping it.
_PATHISH = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def is_bare_name(value: str) -> bool:
    """True if `value` is a single file or directory name, safe to join onto a directory.

    The check the path-traversal guards share: no separators, no drive letter, not '..'.
    """
    return bool(value) and value not in {".", ".."} and not _PATHISH.search(value)


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

    # Name of the env var holding a Steam Web API key, for the endpoints that need one
    # (searching the Workshop, rather than looking up ids you already have). Like every
    # other credential here it is never in config: an API key is tied to your account,
    # and config files get copied between machines and pasted into support threads.
    api_key_env: str = "DZSM_STEAM_API_KEY"

    def password(self) -> str | None:
        """Read the password from the environment. None if unset."""
        return os.environ.get(self.password_env)

    def api_key(self) -> str | None:
        """Steam Web API key from the environment. None if unset."""
        return os.environ.get(self.api_key_env) or None


class ApiConfig(BaseModel):
    """Web API settings (spec §10, §11).

    Loopback by default. `dzsm serve` refuses to bind a non-loopback address unless a
    token is configured — this API can start, stop, and delete things, and binding it to
    0.0.0.0 unauthenticated would hand that to anyone who can reach the port.
    """

    host: str = "127.0.0.1"
    port: int = 8080
    # Name of the env var holding the API token. Like the Steam password, the token
    # itself is never in config.
    token_env: str = "DZSM_API_TOKEN"  # noqa: S105 — a var name, not a secret

    def token(self) -> str | None:
        """The configured token, or None when unset."""
        return os.environ.get(self.token_env) or None


class RconConfig(BaseModel):
    """BattlEye RCON, used for restart warnings and admin chat.

    Off unless an instance sets `rcon_port` — dzsm will not guess a port, and a wrong
    guess means silently talking to nothing.
    """

    host: str = "127.0.0.1"
    # Name of the env var holding the RCON password. Like every other credential here,
    # the password itself is never in config.
    password_env: str = "DZSM_RCON_PASSWORD"  # noqa: S105 — a var name, not a secret
    # How long to wait for a single RCON exchange once connected.
    timeout_seconds: float = 5.0
    # How long after an instance starts before RCON is expected to answer. BattlEye binds
    # long after the server is otherwise up — ~43s on a vanilla 1.29 server, and a heavily
    # modded one can take many minutes. Automated attempts inside this window are skipped
    # rather than left to time out; manual ones still try, and say so if they fail.
    startup_grace_seconds: int = 300
    # Warnings come in two tiers. From `early_warning_lead_seconds` before a restart they
    # repeat every `early_warning_interval_seconds`; inside `final_warning_lead_seconds`
    # they switch to `final_warning_interval_seconds`. Defaults: every 5 minutes from 30
    # minutes out, then every 4 seconds for the last 5 minutes.
    early_warning_lead_seconds: int = 1800
    early_warning_interval_seconds: int = 300
    final_warning_lead_seconds: int = 300
    final_warning_interval_seconds: int = 4
    # {remaining} is filled with a human phrase like "2 minutes" or "20 seconds".
    warning_message: str = "Server restart in {remaining}"
    # Used instead when the restart is because the game itself updated. Worth its own
    # wording: players whose client just updated and cannot connect are owed the reason,
    # and "server restart" reads like the routine one they can wait out.
    update_warning_message: str = "Game update - server restarting in {remaining}"
    # Stop servers with RCON's #shutdown rather than signalling the process, so DayZ
    # closes down on its own terms and flushes persistence. Falls back to terminate then
    # kill if it does not exit.
    shutdown_via_rcon: bool = True
    # Write a BattlEye server config with a generated RCON password when an instance has
    # none. BattlEye ships no default config, so without this RCON never listens at all.
    # The password is generated with `secrets` and read back from that file; it has to be
    # plaintext because BattlEye has no other way to be told it.
    manage_battleye_config: bool = True

    def password(self, override_env: str | None = None) -> str | None:
        return os.environ.get(override_env or self.password_env) or None


class InstanceConfig(BaseModel):
    """One server instance."""

    port: int = 2302
    config_file: str = "serverDZ.cfg"
    # ORDER IS LOAD ORDER (spec §4). This list is the only source of truth for it.
    # Never sort it, never rebuild it from the mods table's iteration order.
    mods: list[str]
    # Server-only mods, emitted as -serverMod=. Clients never load these, so they are
    # for things like admin tools and server-side logic. Same ordering rule applies.
    server_mods: list[str] = Field(default_factory=list)
    # BattlEye RCON port. None derives it as port + RCON_PORT_OFFSET, the usual DayZ
    # layout — a 2302 server has RCON on 2305. Set it explicitly when the instance's
    # BEServer config says otherwise.
    rcon_port: int | None = None
    # Turn RCON off for this instance entirely: no restart warnings, no admin chat, and
    # stops fall back to signalling the process.
    rcon_enabled: bool = True
    # Env var holding this instance's RCON password, when it differs from the global one.
    rcon_password_env: str | None = None
    # Hold an RCON connection open while this instance is running, instead of connecting
    # per request. BattlEye *pushes* connects, disconnects and chat to whoever is
    # connected and offers no way to ask for any of it afterwards, so a held connection is
    # the only way to see chat at all — and it makes the player list live rather than
    # sampled. One connection per running instance; it is not opened while one is down.
    rcon_live: bool = True

    @field_validator("server_executable", "config_file", "mission", "mission_source")
    @classmethod
    def _must_be_a_bare_filename(cls, v: str | None) -> str | None:
        """Reject anything that is not a single name inside its own directory.

        Every one of these is joined onto a directory dzsm owns — the server directory,
        the instance directory, mpmissions/. A path separator escapes that directory, and
        an *absolute* path escapes it completely: pathlib's `base / "C:/x"` discards base
        entirely and returns C:/x. For server_executable that turns an edit of a settings
        field into "launch any binary on the machine"; for config_file it turns saving
        serverDZ.cfg into an arbitrary file write.

        Validated here rather than at the API so a hand-edited config is covered too.
        """
        if v is None:
            return v
        if not is_bare_name(v):
            raise ValueError(
                f"must be a plain file or directory name with no path separators, drive "
                f"letter, or '..', got {v!r}. These are looked up inside a directory dzsm "
                f"manages; a path would escape it."
            )
        return v

    def claimed_ports(self) -> set[int]:
        """Every UDP port this instance occupies.

        DayZ binds the game port plus the two above it — Steam query and the Steam master
        link — and BattlEye listens for RCON on its own. Reserving only the game port is
        what let two instances two apart pass validation and then fight over the rest.
        """
        ports = {self.port + offset for offset in range(GAME_PORT_SPAN)}
        rcon = self.resolved_rcon_port()
        if rcon is not None:
            ports.add(rcon)
        return ports

    def resolved_rcon_port(self) -> int | None:
        """The RCON port to talk to, or None when RCON is off for this instance."""
        if not self.rcon_enabled:
            return None
        if self.rcon_port is not None:
            return self.rcon_port
        return self.port + RCON_PORT_OFFSET
    # Mission directory name for this instance, e.g. "pvp.chernarusplus". None derives
    # "<instance>.<terrain>" from mission_source. dzsm keeps serverDZ.cfg's `template`
    # pointed at whatever this resolves to.
    mission: str | None = None
    # The mission to seed this instance's copy from, by name under the server's
    # mpmissions/. None uses whatever the server installation's own serverDZ.cfg names,
    # i.e. the vanilla offline mission for that map.
    mission_source: str | None = None
    # Pin this instance to a specific server build under serverapp/. None means the
    # newest downloaded build. Set it to hold production on a known-good build while a
    # test instance runs a newer one.
    server_build: str | None = None
    # Give this instance its own full copy of the server application, under
    # instances/<name>/server/. Costs a few GB per instance and buys complete isolation:
    # its own keys/ directory, its own mission files, no shared state with any other
    # instance. Off by default; instances otherwise share the build directory.
    isolated_server: bool = False
    # Mods this instance keeps a claim on without loading them. A mod moved here is
    # temporarily disabled, not abandoned: it stops being launched, but it no longer
    # shows up as unused and cannot be swept away by a cleanup. Move it back to `mods`
    # to re-enable it, with its cached versions still there.
    keep_mods: list[str] = Field(default_factory=list)
    # "HH:MM" local times.
    restart_schedule: list[str] = Field(default_factory=list)
    # CPU cores this instance may run on, as core numbers: [0, 1, 2, 3]. None lets it use
    # every core. Numbers rather than the affinity bitmask, because nobody should have to
    # work out that cores 0-3 are 0xF.
    cpu_affinity: list[int] | None = None
    # Scheduling priority. "high" is usually as far as anyone should go; "realtime" can
    # starve the rest of the machine, including whatever you would use to stop it.
    priority: Literal[
        "idle", "below_normal", "normal", "above_normal", "high", "realtime"
    ] | None = None
    # -cpuCount. Independent of cpu_affinity: affinity is which cores the OS may use,
    # this is how many threads the engine spawns.
    cpu_count: int | None = None
    # Standard DayZ logging flags, all on by default because a server without them is
    # much harder to debug after the fact.
    dologs: bool = True
    adminlog: bool = True
    netlog: bool = True
    freezecheck: bool = True
    # Override the server binary name, e.g. "DayZDiag_x64.exe" for a diagnostic build.
    server_executable: str | None = None
    # Restart this many hours after the instance started. Independent of
    # restart_schedule; when both are set, whichever comes first wins. None disables it.
    restart_after_hours: float | None = 4.0
    extra_args: list[str] = Field(default_factory=list)


class Config(BaseModel):
    """Top-level config, loaded from TOML."""

    root: Path
    steam: SteamConfig
    mods: dict[str, Mod] = Field(default_factory=dict)
    instances: dict[str, InstanceConfig] = Field(default_factory=dict)
    api: ApiConfig = Field(default_factory=ApiConfig)
    rcon: RconConfig = Field(default_factory=RconConfig)
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
    # Which server application to download. 223350 is the stable DayZ server; the
    # experimental server is a different app. Configurable rather than hardcoded so a
    # branch or app change does not need a new release.
    server_appid: int = SERVER_APPID
    # Steam beta branch for the server app, passed to steamcmd as `-beta <name>`. None
    # is the default (public) branch. Builds from different branches land in separate
    # serverapp/<buildid>/ directories like any other, so instances can be pinned to
    # whichever they need.
    server_branch: str | None = None
    # Password for a locked beta branch, if one needs it.
    server_branch_password_env: str = "DZSM_BRANCH_PASSWORD"  # noqa: S105 — a var name
    # Path to an existing DayZ server installation. Interim: until update_server_app()
    # lands, dzsm cannot download appid 223350 itself, so instances run against a server
    # installed by Steam. When unset, the server app is taken from
    # serverapp/<state.serverapp_version>/ instead.
    server_app_path: Path | None = None
    # Give every instance its own mission directory, seeded from a vanilla one. On by
    # default: mission folders hold types.xml, events.xml, cfgspawnabletypes.xml, init.c
    # AND the persistence storage, so instances sharing one would clobber each other's
    # economy and saves. Set false only if you deliberately want them shared.
    per_instance_missions: bool = True
    # Prefix for derived mission names, so dzsm-managed missions are obvious next to
    # vanilla's dayzOffline.* ones. The instance name is still included — a bare prefix
    # would put every instance back on one shared mission, which is the collision
    # per-instance missions exist to prevent.
    mission_prefix: str = "sigma"
    # How often the supervision loop checks liveness and the restart schedule.
    supervise_poll_seconds: int = 15
    # How often the daemon asks Steam what changed, in minutes. 0 turns it off and leaves
    # checking to the button and the CLI. A check is one request covering every configured
    # mod, so this is cheap to run often.
    #
    # 0 rather than null for "off", because TOML has no null: omitting the key would give
    # the default of 30, so with a nullable field there would be no way to express "off"
    # in the config file at all.
    mod_check_interval_minutes: int = 30
    # Download what the check found, rather than only reporting it. Safe to leave on:
    # downloading is decoupled from activation (spec §2), so a new version lands in the
    # cache and is not used by anything until an instance next starts. Off by default
    # anyway — it spends bandwidth and disk without being asked.
    mod_check_auto_download: bool = False
    # Also ask Steam whether the *server app* has a new build, on the same interval. This
    # is the one that matters most and is easiest to miss: when DayZ updates, clients
    # update themselves and a server left on the old build stops accepting them entirely.
    # Costs one steamcmd `app_info_print` per check, which downloads nothing.
    check_server_build: bool = True
    # Download a new server build automatically. Off by default: several GB, unlike a
    # mod check. Nothing activates it until an instance restarts either way.
    server_update_auto_download: bool = False
    # Once a newer server build is on disk, restart instances onto it after this many
    # minutes, warning players over RCON meanwhile. 0 leaves it to the normal restart
    # schedule.
    #
    # A grace period rather than an immediate restart: the server is unjoinable from the
    # moment the game updates, so waiting for a scheduled restart hours away is its own
    # outage — but throwing players out mid-session with no warning is worse than ten
    # minutes of being unable to take new ones.
    server_update_grace_minutes: int = 10
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

    @property
    def players_db(self) -> Path:
        """Session ledger. SQLite, not state.json — that file is rewritten whole on every
        save, which is the wrong shape for a table that grows a row per session forever."""
        return self.root / "players.db"

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
            for field, names in (
                ("mods", instance.mods),
                ("server_mods", instance.server_mods),
                ("keep_mods", instance.keep_mods),
            ):
                for mod_name in names:
                    if mod_name not in self.mods:
                        raise ValueError(
                            f"instance {instance_name!r} {field} references unknown mod "
                            f"{mod_name!r}; known mods: {sorted(self.mods)}"
                        )
                duplicates = {name for name in names if names.count(name) > 1}
                if duplicates:
                    raise ValueError(
                        f"instance {instance_name!r} {field} lists {sorted(duplicates)} "
                        f"more than once"
                    )

            # A mod is either client-facing or server-only, never both: -mod and
            # -serverMod would each load it, and the engine has no reason to.
            both = set(instance.mods) & set(instance.server_mods)
            if both:
                raise ValueError(
                    f"instance {instance_name!r} lists {sorted(both)} in both mods and "
                    f"server_mods; a mod belongs to one or the other"
                )

            # keep_mods means "disabled but retained". Listing a mod there *and* loading
            # it is contradictory, and silently ignoring one of the two would make it
            # unclear which won.
            loaded = {*instance.mods, *instance.server_mods}
            contradictory = loaded & set(instance.keep_mods)
            if contradictory:
                raise ValueError(
                    f"instance {instance_name!r} lists {sorted(contradictory)} in "
                    f"keep_mods while also loading it; keep_mods is for mods that are "
                    f"temporarily disabled"
                )
        return self

    @field_validator("server_branch")
    @classmethod
    def _branch_must_be_a_simple_name(cls, v: str | None) -> str | None:
        if v is not None and (not v or any(c.isspace() for c in v)):
            raise ValueError(f"server_branch must be a branch name with no spaces, got {v!r}")
        return v

    @field_validator("mission_prefix")
    @classmethod
    def _prefix_must_be_a_safe_name(cls, v: str) -> str:
        # A dot would break DayZ's "<MissionName>.<TerrainName>" parsing; a space would
        # break the launch arguments.
        if not v or any(c in v for c in ". \t/\\"):
            raise ValueError(
                f"mission_prefix must be a simple name with no dots, spaces, or path "
                f"separators, got {v!r}"
            )
        return v

    @field_validator("gc_retention_days")
    @classmethod
    def _retention_must_be_positive(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError(
                f"gc_retention_days must be at least 1 day, got {v}. To disable "
                f"automatic deletion entirely, omit it or set it to null."
            )
        return v

    @field_validator("server_update_grace_minutes")
    @classmethod
    def _grace_cannot_be_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(
                f"server_update_grace_minutes cannot be negative, got {v}. Use 0 to leave "
                f"a new build to the normal restart schedule."
            )
        return v

    @field_validator("mod_check_interval_minutes")
    @classmethod
    def _check_interval_cannot_be_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(
                f"mod_check_interval_minutes cannot be negative, got {v}. Use 0 to turn "
                f"automatic checking off."
            )
        return v

    @model_validator(mode="after")
    def _mission_terrain_matches_its_source(self) -> Config:
        """An explicit `mission` must name the same terrain as the mission it seeds from.

        DayZ parses "<MissionName>.<TerrainName>" and loads the world named by the
        suffix. So `mission = "pvp.chernarusplus"` seeded from `dayzOffline.enoch` gives a
        directory full of Livonia files that the engine loads as Chernarus — a server that
        starts, reports healthy, and is wrong. The derived name cannot hit this because it
        takes its suffix from the source; only a hand-written one can.
        """
        for name, instance in self.instances.items():
            if instance.mission is None or instance.mission_source is None:
                continue
            wanted = instance.mission.partition(".")[2]
            seeded = instance.mission_source.partition(".")[2]
            if wanted and seeded and wanted != seeded:
                raise ValueError(
                    f"instance {name!r}: mission {instance.mission!r} names terrain "
                    f"{wanted!r} but mission_source {instance.mission_source!r} is "
                    f"{seeded!r}. The engine loads the terrain named by the suffix, so "
                    f"this would run {seeded!r} content as {wanted!r}. Either match the "
                    f"suffixes or leave `mission` unset to have it derived."
                )
        return self

    @model_validator(mode="after")
    def _ports_are_unique(self) -> Config:
        """No two instances may claim the same UDP port.

        Comparing only the game port is not enough: DayZ binds a *block* — the game port,
        +1 for Steam query, +2 for the Steam master link — and BattlEye adds RCON on top.
        So instances two apart pass a naive check and then collide, which surfaces as the
        second server failing to bind or, worse, as RCON commands reaching the wrong
        server. Instances need to be at least four apart.
        """
        seen: dict[int, str] = {}
        for name, instance in self.instances.items():
            for port in instance.claimed_ports():
                owner = seen.get(port)
                if owner is not None and owner != name:
                    raise ValueError(
                        f"instances {owner!r} and {name!r} both claim UDP port {port}. "
                        f"A DayZ instance uses its game port plus the two above it "
                        f"(Steam query and master) and its BattlEye RCON port, so game "
                        f"ports must be at least {PORT_BLOCK_SIZE} apart — "
                        f"{owner!r} holds {sorted(self.instances[owner].claimed_ports())} "
                        f"and {name!r} wants {sorted(instance.claimed_ports())}."
                    )
                seen[port] = name
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
        # utf-8-sig, not utf-8: Notepad and PowerShell's `Set-Content -Encoding utf8`
        # both write a byte-order mark, and decoded as plain UTF-8 it becomes a U+FEFF
        # character before the first key. TOML then rejects the file with "Invalid
        # statement (at line 1, column 1)", which tells the reader nothing about why.
        # Same reasoning as prepare_server_config, where this already bit once.
        data = tomllib.loads(raw.decode("utf-8-sig"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid config in {path}:\n{exc}") from exc
