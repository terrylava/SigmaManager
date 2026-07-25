"""typer CLI (spec §10, §11).

Milestone 1 is live: `mods check`, `mods update`, `mods list`. Instance commands are
still stubs — they are blocked on verification item #1 in spec §5.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, NoReturn, Optional

import typer

from dzsm import runner
from dzsm.config import Config, ConfigError, WorkshopMod, load_config
from dzsm.instance import InstanceError
from dzsm.mods import ModCacheError, promote_snapshot
from dzsm.runner import RunnerError
from dzsm.state import State, StateError, load_state, save_state
from dzsm.steam import (
    SteamError,
    SteamGuardRequired,
    check_updates,
    download_mod,
    login_interactive,
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
app.add_typer(mods_app)
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


@mods_app.command("gc")
def mods_gc(
    config_path: ConfigPath = Path("config.toml"),
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Actually delete. Default is a dry run."),
    ] = False,
) -> None:
    """Delete unreferenced version directories past the retention window."""
    raise NotImplementedError("mods.collect_garbage")


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
