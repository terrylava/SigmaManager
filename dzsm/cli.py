"""typer CLI (spec §10, §11).

Milestone 1 is live: `mods check`, `mods update`, `mods list`. Instance commands are
still stubs — they are blocked on verification item #1 in spec §5.
"""

from __future__ import annotations

import ipaddress
import logging
from pathlib import Path
from typing import Annotated, NoReturn, Optional

import typer

from dzsm import runner
from dzsm.config import Config, ConfigError, WorkshopMod, load_config
from dzsm.instance import InstanceError
from dzsm.mods import (
    ModCacheError,
    UnusedMod,
    collect_garbage,
    delete_version,
    promote_snapshot,
    promote_tree,
    prunable,
    remove_mod,
    unused_mods,
    version_status,
)
from dzsm.runner import RunnerError
from dzsm.state import State, StateError, load_state, save_state
from dzsm.steam import (
    SteamError,
    SteamGuardRequired,
    check_updates,
    download_mod,
    login_interactive,
    update_server_app,
    verify_download,
)
from dzsm.steam import login as steam_login

app = typer.Typer(
    name="dzsm",
    help="DayZ server manager: versioned mod cache, steamcmd integration, supervision.",
    no_args_is_help=True,
)

mods_app = typer.Typer(
    name="mods",
    help="Mod cache: check, download, list, GC.",
    no_args_is_help=True,
)
instance_app = typer.Typer(
    name="instance",
    help="Server instances.",
    no_args_is_help=True,
)
server_app = typer.Typer(
    name="server",
    help="The DayZ server application itself.",
    no_args_is_help=True,
)
app.add_typer(mods_app)
app.add_typer(server_app)
app.add_typer(instance_app)

ConfigPath = Annotated[
    Path,
    typer.Option("--config", "-c", help="Path to config.toml.", envvar="DZSM_CONFIG"),
]
ModFilter = Annotated[
    Optional[list[str]],  # noqa: UP045 — typer resolves Optional more reliably
    typer.Option("--only", help="Limit to these mod names. Repeatable."),
]


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Configure logging before any command runs (Rule 3: failures are never silent)."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )


@app.command()
def check(config_path: ConfigPath = Path("config.toml")) -> None:
    """Validate config and report the resolved layout. Touches nothing."""
    cfg = _load_config(config_path)
    typer.echo(f"root        {cfg.root}")
    typer.echo(f"modcache    {cfg.modcache_dir}")
    typer.echo(f"instances   {cfg.instances_dir}")
    typer.echo(f"state       {cfg.state_file}")
    typer.echo(f"mods        {len(cfg.mods)}")
    typer.echo(f"instances   {', '.join(cfg.instances) or '(none)'}")


@app.command()
def login(
    config_path: ConfigPath = Path("config.toml"),
    interactive: Annotated[
        bool,
        typer.Option(
            "--interactive",
            "-i",
            help="Hand the terminal to steamcmd so you can type your password and "
            "Steam Guard code. Do this once; steamcmd caches a sentry afterward.",
        ),
    ] = False,
) -> None:
    """Verify the steamcmd login, or seed Steam Guard with --interactive.

    Without --interactive this is non-interactive by design: steamcmd runs with stdin
    closed, so a Guard prompt fails fast instead of hanging forever.
    """
    cfg = _load_config(config_path)
    try:
        if interactive:
            typer.echo(f"Starting steamcmd for {cfg.steam.username}.")
            typer.echo("Type your password and Steam Guard code directly into steamcmd.")
            login_interactive(cfg.steam)
        else:
            steam_login(cfg.steam)
    except SteamGuardRequired as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        typer.secho("Or run: dzsm login --interactive", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(1) from exc
    except SteamError as exc:
        _fail(exc)

    typer.secho(f"logged in as {cfg.steam.username}", fg=typer.colors.GREEN)


@app.command()
def serve(
    config_path: ConfigPath = Path("config.toml"),
    host: Annotated[
        Optional[str],  # noqa: UP045 — typer resolves Optional more reliably
        typer.Option("--host", help="Override api.host."),
    ] = None,
    port: Annotated[
        Optional[int],  # noqa: UP045
        typer.Option("--port", help="Override api.port."),
    ] = None,
) -> None:
    """Run the web API and dashboard.

    Binds loopback by default. Binding anywhere else requires a token, because these
    routes can start, stop, and delete things.
    """
    cfg = _load_config(config_path)
    bind_host = host or cfg.api.host
    bind_port = port or cfg.api.port

    if not _is_loopback(bind_host) and cfg.api.token() is None:
        _fail(
            ValueError(
                f"refusing to bind {bind_host} without a token: this API can start, "
                f"stop, and delete things, and anyone able to reach the port could do "
                f"so. Set {cfg.api.token_env} in the environment, or bind 127.0.0.1."
            )
        )

    try:
        import uvicorn

        from dzsm.api import create_app
    except ImportError as exc:
        _fail(
            ImportError(
                f"the web extra is not installed: {exc}. Install it with "
                f'pip install "dzsm[web]"'
            )
        )

    if cfg.api.token() is None:
        typer.secho(
            f"no token set ({cfg.api.token_env} is unset) — writes are unauthenticated, "
            f"which is why this is bound to {bind_host}",
            fg=typer.colors.YELLOW,
        )
    typer.echo(f"http://{bind_host}:{bind_port}")
    uvicorn.run(create_app(config_path), host=bind_host, port=bind_port, log_level="info")


def _is_loopback(host: str) -> bool:
    """True for addresses only reachable from this machine."""
    if host in {"localhost", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A hostname we cannot resolve to a literal. Treat as non-loopback: the safe
        # assumption is that it is reachable.
        return False


@mods_app.command("check")
def mods_check(config_path: ConfigPath = Path("config.toml")) -> None:
    """Ask the Steam Web API which mods changed. Downloads nothing."""
    cfg = _load_config(config_path)
    state = _load_state(cfg)
    workshop = _workshop_mods(cfg)
    if not workshop:
        typer.echo("no workshop mods configured")
        return

    try:
        remote = check_updates([mod.workshop_id for mod in workshop.values()])
    except SteamError as exc:
        _fail(exc)

    for name, mod in workshop.items():
        latest = remote.get(mod.workshop_id)
        cached = state.known_versions(mod.workshop_id)
        if latest is None:
            status = "unknown (API returned no details)"
        elif latest in cached:
            status = f"up to date ({latest})"
        else:
            status = f"UPDATE {cached[0] if cached else 'none'} -> {latest}"
        typer.echo(f"{name:<24} {mod.workshop_id:<12} {status}")


@mods_app.command("update")
def mods_update(
    config_path: ConfigPath = Path("config.toml"),
    only: ModFilter = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Re-download even when the cached version matches."),
    ] = False,
) -> None:
    """Download changed mods and promote them to version directories.

    Safe to run with every server up — this never activates anything. Activation happens
    at each instance's next restart.
    """
    cfg = _load_config(config_path)
    state = _load_state(cfg)
    workshop = _workshop_mods(cfg, only)
    if not workshop:
        typer.echo("nothing to do")
        return

    try:
        remote = check_updates([mod.workshop_id for mod in workshop.values()])
    except SteamError as exc:
        _fail(exc)

    failures = 0
    for name, mod in workshop.items():
        if mod.pinned_version is not None:
            typer.echo(f"{name}: pinned to {mod.pinned_version}, skipping")
            continue

        latest = remote.get(mod.workshop_id)
        if latest is None:
            # Logged as a warning by check_updates; surface it here too rather than
            # letting the mod silently vanish from the run (Rule 3).
            typer.secho(f"{name}: no details from Steam, skipping", fg=typer.colors.YELLOW)
            failures += 1
            continue

        if not force and latest in state.known_versions(mod.workshop_id):
            typer.echo(f"{name}: up to date ({latest})")
            continue

        try:
            result = download_mod(cfg, mod.workshop_id)
            verify_download(cfg, result)
            path = promote_snapshot(cfg, state, mod.workshop_id, latest)
        except (SteamError, ModCacheError) as exc:
            # Keep going: one broken mod must not stop the rest of the batch. The
            # failure is recorded here and in the exit code, never swallowed (Rule 3).
            typer.secho(f"{name} ({mod.workshop_id}): {exc}", fg=typer.colors.RED, err=True)
            failures += 1
            continue
        finally:
            # State is saved even on failure — a promotion that succeeded before a later
            # mod broke must not be forgotten.
            _save_state(cfg, state)

        state.last_seen_remote[mod.workshop_id] = latest
        _save_state(cfg, state)
        typer.secho(f"{name}: promoted {latest} -> {path}", fg=typer.colors.GREEN)

    # Automatic pruning is opt-in and runs here because this is when new versions arrive.
    # With gc_retention_days unset this is a no-op.
    if cfg.gc_retention_days is not None:
        running = [name for name in cfg.instances if runner.is_running(cfg, state, name)]
        pruned = collect_garbage(cfg, state, running_instances=running, dry_run=False)
        _save_state(cfg, state)
        for path in pruned:
            typer.echo(f"pruned {path}")

    if failures:
        typer.secho(f"{failures} mod(s) failed", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@mods_app.command("list")
def mods_list(config_path: ConfigPath = Path("config.toml")) -> None:
    """Show cached versions per mod and which instances are pinned to each."""
    cfg = _load_config(config_path)
    state = _load_state(cfg)

    for name, mod in cfg.mods.items():
        if not isinstance(mod, WorkshopMod):
            typer.echo(f"{name:<24} local  {mod.path}")
            continue
        versions = state.known_versions(mod.workshop_id)
        if not versions:
            typer.echo(f"{name:<24} {mod.workshop_id:<12} (nothing cached)")
            continue
        for version in versions:
            pinned_by = [
                instance
                for instance, pins in state.pins.items()
                if pins.get(name) == version
            ]
            marker = f"<- {', '.join(pinned_by)}" if pinned_by else ""
            typer.echo(f"{name:<24} {mod.workshop_id:<12} {version} {marker}")


@mods_app.command("versions")
def mods_versions(
    config_path: ConfigPath = Path("config.toml"),
    unused_only: Annotated[
        bool,
        typer.Option("--unused", help="Show only versions no instance is pinned to."),
    ] = False,
) -> None:
    """List every cached version, its size, age, and what is pinned to it.

    Reporting only. Nothing is ever deleted automatically — use `mods delete`.
    """
    cfg = _load_config(config_path)
    state = _load_state(cfg)
    statuses = version_status(cfg, state)
    if unused_only:
        statuses = [s for s in statuses if not s.in_use]

    if not statuses:
        typer.echo("no cached versions")
        return

    reclaimable = 0
    for status in statuses:
        name = status.mod_name or f"(unconfigured {status.workshop_id})"
        age = f"{status.age_days:.1f}d" if status.age_days is not None else "?"
        size = f"{status.size_bytes / (1024 * 1024):.1f} MiB"
        if status.pinned_by:
            tag = typer.style(f"in use by {', '.join(status.pinned_by)}", fg=typer.colors.GREEN)
        elif status.kept_by:
            tag = typer.style(f"kept by {', '.join(status.kept_by)}", fg=typer.colors.CYAN)
        else:
            tag = typer.style("unused", fg=typer.colors.YELLOW)
            reclaimable += status.size_bytes
        latest = " latest" if status.is_latest else ""
        missing = "" if status.exists else typer.style("  MISSING ON DISK", fg=typer.colors.RED)
        typer.echo(
            f"{name:<20} {status.version:<12} {size:>10}  {age:>7}{latest:<7} {tag}{missing}"
        )

    if reclaimable:
        megabytes = reclaimable / (1024 * 1024)
        typer.echo("")
        typer.echo(f"{megabytes:.1f} MiB in unused versions (kept until deleted)")


@mods_app.command("unused")
def mods_unused(
    config_path: ConfigPath = Path("config.toml"),
    select: Annotated[
        bool,
        typer.Option("--select", help="Pick which mods to remove, interactively."),
    ] = False,
    all_unused: Annotated[
        bool,
        typer.Option("--all", help="Select every removable unused mod."),
    ] = False,
    include_kept: Annotated[
        bool,
        typer.Option("--include-kept", help="Also show mods held by keep_mods."),
    ] = False,
) -> None:
    """Find mods nothing references, and optionally remove them.

    Covers both mods configured but listed by no instance, and cache directories for
    mods that are no longer in config at all. Mods held by an instance's keep_mods are
    excluded — being deliberately retained is the opposite of unused. Reports only
    unless --select or --all.
    """
    cfg = _load_config(config_path)
    state = _load_state(cfg)
    unused = unused_mods(cfg, state, include_kept=include_kept)

    if not unused:
        typer.echo("no unused mods")
        return

    for index, mod in enumerate(unused, start=1):
        _echo_unused(index, mod)

    removable = [mod for mod in unused if mod.deletable]
    total = sum(mod.size_bytes for mod in removable)
    typer.echo("")
    typer.echo(f"{total / (1024 * 1024):.1f} MiB in {len(removable)} removable mod(s)")

    if not (select or all_unused):
        typer.echo("re-run with --select to choose, or --all to take everything")
        return

    chosen = removable if all_unused else _prompt_for_selection(unused)
    if not chosen:
        typer.echo("nothing selected")
        return

    typer.echo("")
    for mod in chosen:
        typer.echo(f"  {mod.mod_name or mod.workshop_id}  {mod.path}")
    if not typer.confirm(f"Remove {len(chosen)} mod(s)?"):
        typer.echo("aborted")
        raise typer.Exit(1)

    running = [name for name in cfg.instances if runner.is_running(cfg, state, name)]
    removed = 0
    try:
        for mod in chosen:
            if mod.workshop_id is None:
                continue
            try:
                remove_mod(cfg, state, mod.workshop_id, running_instances=running)
                removed += 1
            except ModCacheError as exc:
                typer.secho(str(exc), fg=typer.colors.RED, err=True)
    finally:
        _save_state(cfg, state)

    typer.secho(f"removed {removed} mod(s)", fg=typer.colors.GREEN)


def _echo_unused(index: int, mod: UnusedMod) -> None:
    name = mod.mod_name or "(not in config)"
    identifier = mod.workshop_id if mod.workshop_id is not None else "local"
    size = f"{mod.size_bytes / (1024 * 1024):.1f} MiB"
    detail = f"{len(mod.versions)} version(s)" if mod.versions else "no cached versions"
    if mod.kept_by:
        tag = typer.style(f"kept by {', '.join(mod.kept_by)}", fg=typer.colors.CYAN)
    elif mod.kind == "local":
        tag = typer.style("local — not removable by dzsm", fg=typer.colors.BLUE)
    elif mod.kind == "orphaned":
        tag = typer.style("orphaned cache", fg=typer.colors.YELLOW)
    else:
        tag = typer.style("no instance uses it", fg=typer.colors.YELLOW)
    pinned = (
        typer.style(f"  still pinned by {', '.join(mod.pinned_by)}", fg=typer.colors.RED)
        if mod.pinned_by
        else ""
    )
    typer.echo(f"{index:>3}) {name:<20} {identifier:<12} {size:>10}  {detail:<18} {tag}{pinned}")


def _prompt_for_selection(unused: list[UnusedMod]) -> list[UnusedMod]:
    """Numbered multi-select. Returns the chosen removable mods, empty if none."""
    typer.echo("")
    raw = typer.prompt(
        "Select numbers to remove (comma separated), 'a' for all, or 'q' to quit",
        default="q",
    ).strip().lower()

    if raw in {"q", ""}:
        return []
    if raw == "a":
        return [mod for mod in unused if mod.deletable]

    chosen: list[UnusedMod] = []
    for token in raw.replace(",", " ").split():
        if not token.isdigit() or not 1 <= int(token) <= len(unused):
            typer.secho(f"ignoring invalid selection {token!r}", fg=typer.colors.YELLOW)
            continue
        mod = unused[int(token) - 1]
        if not mod.deletable:
            reason = (
                f"kept by {', '.join(mod.kept_by)}"
                if mod.kept_by
                else "local mods are hand-authored source, dzsm will not delete them"
            )
            typer.secho(f"skipping {mod.mod_name}: {reason}", fg=typer.colors.YELLOW)
            continue
        chosen.append(mod)
    return chosen


@mods_app.command("prune")
def mods_prune(
    config_path: ConfigPath = Path("config.toml"),
    older_than: Annotated[
        Optional[int],  # noqa: UP045 — typer resolves Optional more reliably
        typer.Option("--older-than", help="Days. Overrides gc_retention_days for this run."),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Actually delete. Default is a dry run."),
    ] = False,
) -> None:
    """Apply the retention policy: remove unused, non-latest versions past the window.

    Off unless gc_retention_days is set in config or --older-than is passed. Dry run by
    default. Versions pinned to an instance, or to a running one, are never removed.
    """
    cfg = _load_config(config_path)
    state = _load_state(cfg)

    if older_than is None and cfg.gc_retention_days is None:
        typer.echo(
            "retention is disabled (gc_retention_days is unset). "
            "Pass --older-than N, or set it in config."
        )
        return

    running = [name for name in cfg.instances if runner.is_running(cfg, state, name)]
    candidates = prunable(cfg, state, retention_days=older_than)
    if not candidates:
        typer.echo("nothing past the retention window")
        return

    total = sum(status.size_bytes for status in candidates)
    for status in candidates:
        name = status.mod_name or str(status.workshop_id)
        age = f"{status.age_days:.1f}d" if status.age_days is not None else "?"
        size = status.size_bytes / (1024 * 1024)
        typer.echo(f"{name:<20} {status.version:<12} {size:>8.1f} MiB  {age}")

    if not apply:
        typer.echo("")
        typer.echo(f"would free {total / (1024 * 1024):.1f} MiB — re-run with --apply")
        return

    try:
        deleted = collect_garbage(
            cfg, state, running_instances=running, retention_days=older_than, dry_run=False
        )
    finally:
        _save_state(cfg, state)

    typer.secho(f"deleted {len(deleted)} version(s)", fg=typer.colors.GREEN)


@mods_app.command("delete")
def mods_delete(
    mod: Annotated[str, typer.Argument(help="Mod name from config, or a workshop id.")],
    version: Annotated[int, typer.Argument(help="Version (time_updated) to delete.")],
    config_path: ConfigPath = Path("config.toml"),
    force: Annotated[
        bool,
        typer.Option("--force", help="Delete even if an instance is pinned to it."),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation.")] = False,
) -> None:
    """Delete one cached version. Nothing is removed unless you run this."""
    cfg = _load_config(config_path)
    state = _load_state(cfg)

    configured = cfg.mods.get(mod)
    if isinstance(configured, WorkshopMod):
        workshop_id = configured.workshop_id
    elif mod.isdigit():
        workshop_id = int(mod)
    else:
        _fail(ValueError(f"{mod!r} is not a configured workshop mod or a workshop id"))

    path = cfg.version_dir(workshop_id, version)
    if not yes:
        typer.echo(f"About to delete {path}")
        if not typer.confirm("Delete it?"):
            typer.echo("aborted")
            raise typer.Exit(1)

    running = [name for name in cfg.instances if runner.is_running(cfg, state, name)]
    try:
        deleted = delete_version(
            cfg, state, workshop_id, version, running_instances=running, force=force
        )
    except ModCacheError as exc:
        _fail(exc)
    finally:
        _save_state(cfg, state)

    typer.secho(f"deleted {deleted}", fg=typer.colors.GREEN)


@server_app.command("update")
def server_update(
    config_path: ConfigPath = Path("config.toml"),
    validate: Annotated[
        bool,
        typer.Option("--validate", help="Re-check every file. Slow; use if suspect."),
    ] = False,
) -> None:
    """Download or update the DayZ server app into a versioned directory.

    Safe to run with servers up: like mods, this never touches a directory an instance is
    using. A new build lands beside the old one and is picked up at the next restart.
    """
    cfg = _load_config(config_path)
    state = _load_state(cfg)

    try:
        build_id = update_server_app(cfg, validate=validate)
    except SteamError as exc:
        _fail(exc)

    destination = cfg.serverapp_dir / build_id
    if destination.is_dir():
        typer.echo(f"server app already promoted at build {build_id}")
    else:
        try:
            promote_tree(
                cfg.serverapp_download_dir, destination, strategy=cfg.snapshot_strategy
            )
        except ModCacheError as exc:
            _fail(exc)
        typer.secho(f"promoted server app build {build_id} -> {destination}", fg=typer.colors.GREEN)

    state.serverapp_version = build_id
    _save_state(cfg, state)

    if cfg.server_app_path is not None:
        typer.secho(
            f"note: server_app_path is set ({cfg.server_app_path}), so instances still "
            f"use that install. Unset it to run the managed build.",
            fg=typer.colors.YELLOW,
        )


@server_app.command("list")
def server_list(config_path: ConfigPath = Path("config.toml")) -> None:
    """Show downloaded server app builds and which one instances will use."""
    cfg = _load_config(config_path)
    state = _load_state(cfg)

    builds = (
        sorted(p.name for p in cfg.serverapp_dir.iterdir() if p.is_dir())
        if cfg.serverapp_dir.is_dir()
        else []
    )
    if not builds:
        typer.echo("no server app builds downloaded")
    for build in builds:
        marker = " <- active" if build == state.serverapp_version else ""
        typer.echo(f"{build}{marker}")

    if cfg.server_app_path is not None:
        typer.echo("")
        typer.echo(f"instances use server_app_path: {cfg.server_app_path}")


@instance_app.command("start")
def instance_start(
    name: str,
    config_path: ConfigPath = Path("config.toml"),
) -> None:
    """Start an instance, activating any newer mod versions in the process."""
    cfg = _load_config(config_path)
    state = _load_state(cfg)
    try:
        pid = runner.start(cfg, state, name)
    except (RunnerError, InstanceError, ModCacheError) as exc:
        _fail(exc)
    finally:
        _save_state(cfg, state)
    typer.secho(f"{name}: started (pid {pid})", fg=typer.colors.GREEN)


@instance_app.command("stop")
def instance_stop(name: str, config_path: ConfigPath = Path("config.toml")) -> None:
    """Stop an instance."""
    cfg = _load_config(config_path)
    state = _load_state(cfg)
    if name not in cfg.instances:
        _fail(ValueError(f"unknown instance {name!r}"))
    try:
        runner.stop(cfg, state, name)
    except RunnerError as exc:
        _fail(exc)
    finally:
        _save_state(cfg, state)
    typer.echo(f"{name}: stopped")


@instance_app.command("status")
def instance_status(config_path: ConfigPath = Path("config.toml")) -> None:
    """Show each instance: running, pinned versions, next scheduled restart."""
    cfg = _load_config(config_path)
    state = _load_state(cfg)
    if not cfg.instances:
        typer.echo("no instances configured")
        return

    for name, instance in cfg.instances.items():
        running = runner.is_running(cfg, state, name)
        marker = typer.style("running", fg=typer.colors.GREEN) if running else "stopped"
        upcoming = runner.next_restart(instance)
        when = upcoming.strftime("%Y-%m-%d %H:%M") if upcoming else "no schedule"
        pins = state.pins.get(name, {})
        versions = ", ".join(f"{k}={v}" for k, v in pins.items()) or "(none pinned)"
        typer.echo(f"{name:<16} {marker:<18} port {instance.port}  next {when}")
        typer.echo(f"{'':<16} {versions}")


@instance_app.command("supervise")
def instance_supervise(
    config_path: ConfigPath = Path("config.toml"),
    only: Annotated[
        Optional[list[str]],  # noqa: UP045 — typer resolves Optional more reliably
        typer.Option("--only", help="Supervise just these instances. Repeatable."),
    ] = None,
) -> None:
    """Run the supervision loop in the foreground."""
    cfg = _load_config(config_path)
    state = _load_state(cfg)
    names = only or list(cfg.instances)
    try:
        runner.supervise(cfg, state, names)
    except (RunnerError, InstanceError) as exc:
        _fail(exc)
    finally:
        _save_state(cfg, state)


# --- internals ---------------------------------------------------------------


def _fail(exc: Exception) -> NoReturn:
    typer.secho(str(exc), fg=typer.colors.RED, err=True)
    raise typer.Exit(1) from exc


def _load_config(path: Path) -> Config:
    try:
        return load_config(path)
    except ConfigError as exc:
        _fail(exc)


def _load_state(config: Config) -> State:
    try:
        return load_state(config.state_file)
    except StateError as exc:
        _fail(exc)


def _save_state(config: Config, state: State) -> None:
    try:
        save_state(config.state_file, state)
    except OSError as exc:
        _fail(exc)


def _workshop_mods(config: Config, only: list[str] | None = None) -> dict[str, WorkshopMod]:
    """Workshop mods by name, optionally filtered. Local mods are excluded — steamcmd
    never touches them (spec §9)."""
    if only:
        unknown = [name for name in only if name not in config.mods]
        if unknown:
            _fail(ValueError(f"unknown mod(s): {', '.join(unknown)}"))
    return {
        name: mod
        for name, mod in config.mods.items()
        if isinstance(mod, WorkshopMod) and (not only or name in only)
    }


if __name__ == "__main__":
    app()
