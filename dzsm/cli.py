"""typer CLI (spec §10, §11).

Milestone 1 is live: `mods check`, `mods update`, `mods list`. Instance commands are
still stubs — they are blocked on verification item #1 in spec §5.
"""

from __future__ import annotations

import ipaddress
import logging
import threading
import webbrowser
from pathlib import Path
from typing import Annotated, Any, NoReturn, Optional

import typer

from dzsm import __version__, deployment, mods, players, runner, selfupdate
from dzsm import backup as dzsm_backup
from dzsm import instance as dzsm_instance
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
    record_check,
    remove_mod,
    unused_mods,
    version_status,
)
from dzsm.rcon import RconError
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


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"dzsm {__version__}")
        raise typer.Exit


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


# Eager, so --version answers before anything else runs — including on a machine where
# the config is broken, which is one of the times you most want to know what you have.
VersionFlag = Annotated[
    bool,
    typer.Option(
        "--version", "-V", help="Print the version and exit.",
        callback=_show_version, is_eager=True,
    ),
]


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
    version: VersionFlag = False,
) -> None:
    """Configure logging before any command runs (Rule 3: failures are never silent)."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )


@app.command()
def init(
    config_path: ConfigPath = Path("config.toml"),
    root: Annotated[
        Optional[str],  # noqa: UP045 — typer resolves Optional more reliably
        typer.Option("--root", help="Where mods, server builds, and instances live."),
    ] = None,
    username: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option("--username", help="Steam account that owns DayZ."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing config.")
    ] = False,
) -> None:
    """Create a starter config and print what to do next.

    This is the first command to run. Everything else needs a config, and there is no
    useful default for where your servers should live or which Steam account owns DayZ.
    """
    if config_path.exists() and not force:
        _fail(
            ValueError(
                f"{config_path} already exists. Edit it, or pass --force to replace it."
            )
        )

    cfg = _create_config(config_path, root, username)
    typer.echo("")
    typer.echo(f"Everything will live under {cfg.root}")
    typer.echo("")
    typer.secho("Next:", bold=True)
    typer.echo("  1. dzsm login --interactive     seed Steam Guard once")
    typer.echo("  2. dzsm server update           download the DayZ server (~4 GB)")
    typer.echo("  3. dzsm gui                     dashboard, in your browser")
    typer.echo("")
    typer.echo(f"Add mods and more instances in {config_path}; everything else can be")
    typer.echo("driven from the dashboard.")


def _create_config(
    config_path: Path, root: str | None, username: str | None
) -> Config:
    """Prompt for what cannot be guessed, write a config, and prove it loads."""
    if root is None:
        typer.echo("Where should mods, server builds, and instances live?")
        typer.echo("Keep it short — deep mod paths hit the Windows 260-character limit.")
        root = typer.prompt("  root", default=f"{Path.cwd().anchor}dz")

    if username is None:
        typer.echo("")
        typer.echo("Steam account name that owns DayZ (anonymous login does not work).")
        username = typer.prompt("  username")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_starter_config(root, username), encoding="utf-8")
    typer.secho(f"wrote {config_path}", fg=typer.colors.GREEN)

    # Validate what we just wrote rather than assuming — a root that fails the path
    # length check should be caught here, not three commands later.
    try:
        return load_config(config_path)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        typer.secho("Edit the file and re-run `dzsm check`.", fg=typer.colors.YELLOW)
        raise typer.Exit(1) from exc


def _starter_config(root: str, username: str) -> str:
    """A config that works as written, with one vanilla instance to start.

    TOML single quotes are *literal* strings, so Windows paths go in verbatim — escaping
    the backslashes would put two of them in the value. Kept to plain ASCII as well:
    this is a file people open in whatever editor Windows gives them.
    """
    root = root.rstrip("\\/")
    separator = "\\" if "\\" in root or ":" in root else "/"
    return f"""\
# dzsm configuration. Run `dzsm check` after editing to validate it.

# Everything dzsm manages lives here: steamcmd, downloaded server builds, the mod cache,
# and per-instance files. Keep the path short - mod trees are deep and Windows stops at
# 260 characters.
root = '{root}'

[steam]
# Must own DayZ. Anonymous login does not work for it.
username = "{username}"
# dzsm downloads steamcmd here if it is missing, and generates a BattlEye RCON password
# per instance. Neither needs anything from you.
steamcmd_path = '{root}{separator}steamcmd{separator}steamcmd.exe'

# ---------------------------------------------------------------------------
# Mods. Add one entry per Workshop mod, then run `dzsm mods update`.
# The key is the name instances refer to; workshop_id is from the Workshop URL.
# ---------------------------------------------------------------------------

# [mods.CF]
# kind = "workshop"
# workshop_id = 1559212036

# ---------------------------------------------------------------------------
# Instances. One server each. Ports must differ; RCON defaults to port + 3.
# ---------------------------------------------------------------------------

[instances.server1]
port = 2302
# ORDER IS LOAD ORDER - framework mods such as CF go before what depends on them.
mods = []
# Restart this many hours after starting. Remove the line to disable.
restart_after_hours = 4
# Wall-clock restarts as well, if you want them: ["04:00", "16:00"]
restart_schedule = []
"""


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
    state = _load_state(cfg)
    try:
        if interactive:
            typer.echo(f"Starting steamcmd for {cfg.steam.username}.")
            typer.echo("Type your password and Steam Guard code directly into steamcmd.")
            login_interactive(cfg.steam)
        else:
            steam_login(cfg.steam)
    except SteamGuardRequired as exc:
        # Recorded so the dashboard says the session is stale rather than waiting for a
        # download to discover it at an inconvenient moment.
        state.record_steam_session(ok=False, detail=str(exc).split("\n")[0])
        save_state(cfg.state_file, state)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        typer.secho("Or run: dzsm login --interactive", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(1) from exc
    except SteamError as exc:
        _fail(exc)

    # And recorded on success, so fixing it from a terminal clears the warning in the
    # browser instead of leaving two views of the same thing disagreeing.
    state.record_steam_session(ok=True)
    save_state(cfg.state_file, state)
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

    _check_bind(cfg, bind_host)
    uvicorn, create_app = _web_dependencies(cfg)
    tls = _tls_for(cfg, bind_host)

    if cfg.api.token() is None:
        typer.secho(
            f"no token set ({cfg.api.token_env} is unset) — writes are unauthenticated, "
            f"which is why this is bound to {bind_host}",
            fg=typer.colors.YELLOW,
        )
    typer.echo(_url(bind_host, bind_port, tls))
    uvicorn.run(
        create_app(config_path),
        host=bind_host,
        port=bind_port,
        log_level="info",
        **_ssl_args(tls),
    )


@app.command()
def gui(
    config_path: ConfigPath = Path("config.toml"),
    port: Annotated[
        Optional[int],  # noqa: UP045 — typer resolves Optional more reliably
        typer.Option("--port", help="Override api.port."),
    ] = None,
) -> None:
    """Open the dashboard, creating a config first if there isn't one.

    The one command for driving dzsm from a browser: it supervises every instance, serves
    the dashboard, and opens it. Loopback only — for anything reachable from elsewhere,
    use `dzsm daemon` with a token.
    """
    if not config_path.exists():
        typer.secho("No config yet, so let's make one.", bold=True)
        _create_config(config_path, None, None)
        typer.echo("")
        typer.secho(
            "Seed Steam Guard once before downloading anything: "
            "dzsm login --interactive",
            fg=typer.colors.YELLOW,
        )
        typer.echo("")

    daemon(config_path=config_path, host="127.0.0.1", port=port, only=None, open_browser=True)


@app.command()
def daemon(
    config_path: ConfigPath = Path("config.toml"),
    host: Annotated[
        Optional[str],  # noqa: UP045 — typer resolves Optional more reliably
        typer.Option("--host", help="Override api.host."),
    ] = None,
    port: Annotated[
        Optional[int],  # noqa: UP045
        typer.Option("--port", help="Override api.port."),
    ] = None,
    only: Annotated[
        Optional[list[str]],  # noqa: UP045
        typer.Option("--only", help="Supervise just these instances. Repeatable."),
    ] = None,
    open_browser: Annotated[
        bool, typer.Option("--open", help="Open the dashboard once it is listening.")
    ] = False,
) -> None:
    """Supervise instances and serve the API, in one long-running process.

    This is what to run as a service: it keeps servers up, restarts them on schedule, and
    stays reachable for the dashboard and API. Supervision runs in a background thread;
    the web server holds the main one, so Ctrl-C (or a service stop) shuts both down.

    Instances are deliberately left running when it exits — dzsm going down should not
    take the servers with it.
    """
    cfg = _load_config(config_path)
    state = _load_state(cfg)
    bind_host = host or cfg.api.host
    bind_port = port or cfg.api.port
    names = only or list(cfg.instances)

    if not names:
        _fail(ValueError("no instances configured to supervise"))
    unknown = [name for name in names if name not in cfg.instances]
    if unknown:
        _fail(ValueError(f"unknown instance(s): {', '.join(unknown)}"))
    _check_bind(cfg, bind_host)
    uvicorn, create_app = _web_dependencies(cfg)
    tls = _tls_for(cfg, bind_host)

    stop = threading.Event()
    supervisor = threading.Thread(
        target=runner.supervise,
        args=(cfg, state, names),
        kwargs={"stop": stop, "config_path": config_path},
        name="dzsm-supervisor",
        daemon=True,
    )
    supervisor.start()

    typer.echo(f"supervising {', '.join(names)}")
    url = _url(bind_host, bind_port, tls)
    typer.echo(url)
    if open_browser:
        _open_when_listening(url)
    try:
        uvicorn.run(
            create_app(config_path),
            host=bind_host,
            port=bind_port,
            log_level="info",
            **_ssl_args(tls),
        )
    finally:
        stop.set()
        # The supervisor may be mid-restart; give it time to finish rather than leaving
        # a half-started instance unrecorded in state.
        supervisor.join(timeout=cfg.stop_timeout_seconds)
        if supervisor.is_alive():
            typer.secho(
                "supervisor did not stop within the timeout", fg=typer.colors.YELLOW, err=True
            )


def _open_when_listening(url: str, *, delay: float = 1.0) -> None:
    """Open a browser shortly after the server starts.

    uvicorn.run blocks, so this is scheduled rather than called after it: a browser opened
    before the port is listening just shows a connection error.
    """

    def opener() -> None:
        try:
            webbrowser.open(url)
        except OSError as exc:  # pragma: no cover - headless or no handler
            typer.secho(f"could not open a browser: {exc}", fg=typer.colors.YELLOW, err=True)

    threading.Timer(delay, opener).start()


def _web_dependencies(cfg: Config) -> tuple[Any, Any]:
    """Import uvicorn and the app factory, failing usefully when the extra is missing."""
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
    return uvicorn, create_app


def _check_bind(cfg: Config, bind_host: str) -> None:
    """Refuse to listen anywhere a stranger could reach without the protection for it.

    Two conditions for leaving loopback, and the reasoning is the same for both: the
    default must never be the setting that hands control to whoever is on the network.

    - A token, because these routes start, stop and delete things.
    - TLS, because over plain HTTP that token crosses the network in clear text on every
      request. `allow_plain_http` is the deliberate way past it, for a network nobody
      else can see into.

    One function for `serve` and `daemon`: the check used to be written out twice, and
    two copies of a security rule drift.
    """
    if _is_loopback(bind_host):
        return
    if cfg.api.token() is None:
        _fail(
            ValueError(
                f"refusing to bind {bind_host} without a token: this API can start, "
                f"stop, and delete things, and anyone able to reach the port could do "
                f"so. Set {cfg.api.token_env} in the environment, or bind 127.0.0.1."
            )
        )
    if not cfg.api.tls and not cfg.api.allow_plain_http:
        _fail(
            ValueError(
                f"refusing to bind {bind_host} over plain HTTP: the token would cross "
                f"the network in clear text on every request. Set `tls = true` under "
                f"[api] — dzsm makes its own certificate — or `allow_plain_http = true` "
                f"if nobody else can see this network."
            )
        )


def _tls_for(cfg: Config, bind_host: str) -> Any:
    """The certificate to serve with, or None when TLS is off.

    Imported here rather than at the top: cryptography belongs to the web extra, and the
    rest of the CLI must keep working without it.
    """
    if not cfg.api.tls:
        return None
    from dzsm import tls

    try:
        material = tls.ensure_certificate(cfg, bind_host)
    except tls.TlsError as exc:
        _fail(exc)
    if material.generated:
        typer.secho(
            "made a new certificate — every PC that connects will warn once, and should "
            "compare this fingerprint before accepting it:",
            fg=typer.colors.YELLOW,
        )
    typer.echo(f"certificate fingerprint (SHA-256): {material.fingerprint}")
    return material


def _ssl_args(material: Any) -> dict[str, str]:
    if material is None:
        return {}
    return {"ssl_certfile": str(material.cert_path), "ssl_keyfile": str(material.key_path)}


def _url(host: str, port: int, material: Any) -> str:
    scheme = "https" if material is not None else "http"
    # 0.0.0.0 is not somewhere a browser can go; the local name is what works there.
    shown = "localhost" if host in {"0.0.0.0", "::"} else host  # noqa: S104 - display only
    return f"{scheme}://{shown}:{port}"


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

    # Recorded, not just printed: the dashboard reports what the last check found, and a
    # check run from a terminal is still a check.
    record_check(cfg, state, remote)
    save_state(cfg.state_file, state)

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
        flags = []
        if build == state.serverapp_version:
            flags.append("newest")
        users = sorted(n for n, b in state.server_builds.items() if b == build)
        if users:
            flags.append(f"used by {', '.join(users)}")
        suffix = f"  <- {'; '.join(flags)}" if flags else ""
        typer.echo(f"{build}{suffix}")

    if cfg.server_app_path is not None:
        typer.echo("")
        typer.echo(f"instances use server_app_path: {cfg.server_app_path}")


@instance_app.command("start")
def instance_start(
    name: str,
    config_path: ConfigPath = Path("config.toml"),
    refresh_server: Annotated[
        bool,
        typer.Option(
            "--refresh-server",
            help="Re-copy the server for an isolated instance. Needed after updating an "
            "external install, which dzsm cannot detect.",
        ),
    ] = False,
) -> None:
    """Start an instance, activating any newer mod versions in the process."""
    cfg = _load_config(config_path)
    state = _load_state(cfg)
    try:
        runner.clear_failures(state, name)
        pid = runner.start(cfg, state, name, refresh_server=refresh_server)
    except (RunnerError, InstanceError, ModCacheError) as exc:
        _fail(exc)
    finally:
        _save_state(cfg, state)
    typer.secho(f"{name}: started (pid {pid})", fg=typer.colors.GREEN)


@instance_app.command("stop")
def instance_stop(
    name: str,
    config_path: ConfigPath = Path("config.toml"),
    hold: Annotated[
        bool,
        typer.Option(
            "--hold/--no-hold",
            help="Keep it down. --no-hold lets a running supervisor restart it.",
        ),
    ] = True,
) -> None:
    """Stop an instance, and keep it stopped.

    A supervisor will not restart a stopped instance until you start it again. Use
    --no-hold to stop it and let the supervisor bring it straight back, which is how to
    bounce one.
    """
    cfg = _load_config(config_path)
    state = _load_state(cfg)
    if name not in cfg.instances:
        _fail(ValueError(f"unknown instance {name!r}"))
    try:
        runner.stop(cfg, state, name, hold=hold)
    except RunnerError as exc:
        _fail(exc)
    finally:
        _save_state(cfg, state)
    typer.echo(f"{name}: stopped" + (" and held down" if hold else ""))


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
        if running:
            marker = typer.style("running", fg=typer.colors.GREEN)
        elif name in state.held:
            marker = typer.style("held down", fg=typer.colors.CYAN)
        else:
            marker = "stopped"
        process = state.processes.get(name)
        upcoming = runner.next_restart(
            instance, started_at=process.started_at if process else None
        )
        when = upcoming.strftime("%Y-%m-%d %H:%M") if upcoming else "no schedule"
        pins = state.pins.get(name, {})
        versions = ", ".join(f"{k}={v}" for k, v in pins.items()) or "(none pinned)"
        build = state.server_builds.get(name, "?")
        if instance.server_build is not None:
            build += f" (pinned to {instance.server_build})"
        if instance.isolated_server:
            build += ", isolated copy"
        rcon_port = instance.resolved_rcon_port()
        rcon = f"rcon {rcon_port}" if rcon_port else "rcon off"
        typer.echo(f"{name:<16} {marker:<18} port {instance.port}  {rcon}  next {when}")
        typer.echo(f"{'':<16} server {build}")
        typer.echo(f"{'':<16} {versions}")


@app.command()
def update(
    check: Annotated[
        bool, typer.Option("--check", help="Say what is available and stop.")
    ] = False,
    config_path: ConfigPath = Path("config.toml"),
) -> None:
    """Fetch the newest published dzsm and put it in place.

    Nothing is downloaded until you run this. The daemon's own check only looks, because
    a binary supervising someone's game servers has no business replacing itself on a
    timer.

    The running program keeps the image it already loaded, so this never interrupts
    anything: the new binary takes effect the next time dzsm starts. The one it replaces
    is kept alongside as dzsm.exe.old in case the new one disappoints.
    """
    repo = selfupdate.DEFAULT_REPO
    if config_path.exists():
        # Only to learn which repository to ask. An update must still be possible on a
        # machine whose config will not load - that is one of the reasons to update.
        try:
            repo = _load_config(config_path).update_repo
        except typer.Exit:
            raise
        except Exception:  # noqa: BLE001 - a broken config is not a reason to refuse
            typer.echo(f"(config unreadable; asking {repo})")

    try:
        release = selfupdate.latest_release(repo=repo)
    except selfupdate.SelfUpdateError as exc:
        _fail(exc)
    if release is None:
        typer.echo(f"{repo} has published no release with a {selfupdate.ASSET_NAME}.")
        return

    if not selfupdate.is_newer(release.tag, __version__):
        typer.secho(
            f"dzsm {__version__} is current ({release.tag} is the newest published).",
            fg=typer.colors.GREEN,
        )
        return

    typer.secho(f"dzsm {release.tag} is available (running {__version__}).", fg=typer.colors.YELLOW)
    typer.echo(f"  {release.notes_url}")
    if check:
        typer.echo("Run `dzsm update` to fetch it.")
        return

    binary = selfupdate.running_binary()
    if binary is None:
        _fail(
            selfupdate.SelfUpdateError(
                "running from source, so there is nothing to replace. Pull the repo "
                "instead, or build with `pyinstaller dzsm.spec`."
            )
        )

    typer.echo(f"Downloading {release.size / 1_048_576:.1f} MiB...")
    try:
        fetched = selfupdate.download(release, selfupdate.staging_dir())
        retired = selfupdate.apply(fetched)
    except selfupdate.SelfUpdateError as exc:
        _fail(exc)

    typer.secho(f"Installed {release.tag} as {binary}.", fg=typer.colors.GREEN)
    typer.echo(f"The previous build is {retired}.")
    typer.echo("Restart dzsm for it to take effect - this process keeps the old one.")


backup_app = typer.Typer(
    name="backup",
    help="Copies of an instance's world: bases, stashes, vehicles, economy.",
    no_args_is_help=True,
)
app.add_typer(backup_app)


@backup_app.command("take")
def backup_take(
    name: str,
    config_path: ConfigPath = Path("config.toml"),
) -> None:
    """Back up one instance's world now.

    Taken from the running server if it is up, which DayZ does not guarantee is
    consistent — it writes persistence continuously. The copies worth trusting are the
    ones taken in the restart window, which happens on its own when backup_on_restart
    is left on. This is for before you do something risky.
    """
    cfg = _load_config(config_path)
    state = _load_state(cfg)
    if name not in cfg.instances:
        raise typer.BadParameter(f"unknown instance {name!r}")
    if runner.is_running(cfg, state, name):
        typer.secho(
            "the server is running, so persistence may be mid-write; stop it first for "
            "a copy you can rely on",
            fg=typer.colors.YELLOW,
        )

    try:
        taken = dzsm_backup.take(cfg, state, name)
    except dzsm_backup.BackupError as exc:
        _fail(exc)
    if taken is None:
        typer.echo(f"{name} has no mission directory yet, so there is nothing to copy.")
        return
    size = f"{taken.size_bytes / (1024 * 1024):.1f} MiB"
    typer.secho(f"{taken.stamp}  {size}  {taken.path}", fg=typer.colors.GREEN)


@backup_app.command("list")
def backup_list(
    name: str,
    config_path: ConfigPath = Path("config.toml"),
) -> None:
    """Show stored copies, newest first."""
    cfg = _load_config(config_path)
    if name not in cfg.instances:
        raise typer.BadParameter(f"unknown instance {name!r}")

    stored = dzsm_backup.list_backups(cfg, name)
    if not stored:
        typer.echo(f"no backups for {name}")
        return
    for item in stored:
        size = f"{item.size_bytes / (1024 * 1024):.1f} MiB"
        typer.echo(f"{item.stamp:<18} {size:>12}  {item.mission}")


@backup_app.command("restore")
def backup_restore(
    name: str,
    stamp: str,
    config_path: ConfigPath = Path("config.toml"),
) -> None:
    """Put a stored world back, replacing the current one.

    Refuses while the instance is running: a live server holds its persistence open, so
    this would half-apply and leave a world that is neither. The current world is backed
    up first, because restoring the wrong copy is an easy mistake and otherwise an
    unrecoverable one.
    """
    cfg = _load_config(config_path)
    state = _load_state(cfg)
    if name not in cfg.instances:
        raise typer.BadParameter(f"unknown instance {name!r}")
    if runner.is_running(cfg, state, name):
        _fail(
            dzsm_backup.BackupError(
                f"instance {name!r} is running. Stop it first — a live server holds its "
                f"persistence open, and a half-applied restore is worse than none."
            )
        )

    try:
        restored = dzsm_backup.restore(cfg, state, name, stamp)
    except dzsm_backup.BackupError as exc:
        _fail(exc)
    typer.secho(f"restored {stamp} into {restored}", fg=typer.colors.GREEN)
    typer.echo("The world that was there has been kept as a new backup.")


DeployDir = Annotated[
    Path, typer.Argument(help="Where to write them. Defaults to the current directory.")
]


@app.command()
def deploy(
    directory: DeployDir = Path(),
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite files that exist.")
    ] = False,
) -> None:
    """Write out the files needed to run dzsm unattended.

    The scheduled-task launcher, its installer, the deployment notes, and a commented
    example config. They are carried inside the binary because a bare exe on a fresh
    server otherwise dead-ends: the scripts live in the source tree, which is exactly
    what someone deploying a single file does not have.

    Never overwrites. The launcher is where the Steam key and API token are written by
    hand, and clobbering it on an upgrade would wipe them.
    """
    try:
        written, skipped = deployment.write_files(directory, force=force)
    except deployment.DeploymentError as exc:
        _fail(exc)

    for path in written:
        typer.secho(f"wrote {path}", fg=typer.colors.GREEN)
    for path in skipped:
        typer.echo(f"kept {path} (already there; --force to replace)")
    if written:
        typer.echo("")
        typer.echo("Next: edit dzsm-daemon.cmd for your paths and keys, then")
        typer.echo("  powershell -ExecutionPolicy Bypass -File install-task.ps1")


@instance_app.command("args")
def instance_args(
    name: str,
    config_path: ConfigPath = Path("config.toml"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print only the length."),
) -> None:
    """Show the command line this instance would start with, and how long it is.

    Read-only: it resolves versions and paths exactly as `start` does but prepares
    nothing, so it is safe to run against a server that is up. The length is the number
    the OS limit applies to — the joined, quoted form, not the sum of the arguments.

    The limit people hit in DayZ guides is cmd.exe's 8191, which binds on the batch
    files most setups launch from. dzsm does not go through a shell, so the ceiling here
    is the OS one, four times higher.
    """
    cfg = _load_config(config_path)
    state = _load_state(cfg)
    if name not in cfg.instances:
        raise typer.BadParameter(f"unknown instance {name!r}")

    instance = cfg.instances[name]
    try:
        server_dir = dzsm_instance.effective_server_dir(cfg, state, name)
        exe = dzsm_instance.server_executable(server_dir, instance.server_executable)
        versions = runner.versions_to_activate(cfg, state, name)
        paths = mods.mod_paths(cfg, name, versions)
        server_paths = mods.server_mod_paths(cfg, name, versions)
    except (InstanceError, ModCacheError) as exc:
        _fail(InstanceError(f"cannot build the command line for {name!r}: {exc}"))

    argv = dzsm_instance.build_launch_args(
        cfg, name, instance, exe, paths, server_paths,
        dzsm_instance.active_config_path(cfg, name),
    )
    length = dzsm_instance.command_line_length(argv)
    limit = dzsm_instance.COMMAND_LINE_LIMIT

    if quiet:
        typer.echo(length)
        return

    for argument in argv:
        typer.echo(f"  {argument}")
    typer.echo("")
    typer.echo(f"{len(paths)} mods, {len(server_paths)} server mods")
    headroom = limit - length
    summary = f"{length} characters of {limit} ({headroom} spare)"
    if length > limit:
        typer.echo(typer.style(f"{summary} - OVER THE LIMIT", fg=typer.colors.RED))
        raise typer.Exit(1)
    typer.echo(typer.style(summary, fg=typer.colors.GREEN))


@instance_app.command("say")
def instance_say(
    name: str,
    message: Annotated[str, typer.Argument(help="Text to broadcast to every player.")],
    config_path: ConfigPath = Path("config.toml"),
) -> None:
    """Send an admin chat message over RCON."""
    cfg = _load_config(config_path)
    state = _load_state(cfg)
    if name not in cfg.instances:
        _fail(ValueError(f"unknown instance {name!r}"))

    try:
        client = runner.rcon_client(cfg, name)
    except RconError as exc:
        # rcon_client connects, so this is where a still-starting server surfaces.
        _fail(_with_startup_hint(cfg, state, name, exc))
    if client is None:
        _fail(ValueError(f"instance {name!r} has rcon_enabled = false"))

    try:
        client.say(message)
    except RconError as exc:
        _fail(_with_startup_hint(cfg, state, name, exc))
    finally:
        client.close()
    typer.secho(f"{name}: sent", fg=typer.colors.GREEN)


@instance_app.command("players")
def instance_players(
    name: str,
    config_path: ConfigPath = Path("config.toml"),
) -> None:
    """List who is connected right now, over RCON."""
    cfg = _load_config(config_path)
    state = _load_state(cfg)
    if name not in cfg.instances:
        _fail(ValueError(f"unknown instance {name!r}"))

    try:
        client = runner.rcon_client(cfg, name)
    except RconError as exc:
        _fail(_with_startup_hint(cfg, state, name, exc))
    if client is None:
        _fail(ValueError(f"instance {name!r} has rcon_enabled = false"))

    try:
        slots = client.players()
    except RconError as exc:
        _fail(_with_startup_hint(cfg, state, name, exc))
    finally:
        client.close()

    if not slots:
        typer.echo(f"{name}: nobody connected")
        return
    for slot in slots:
        flags = []
        if slot.lobby:
            flags.append("loading")
        if not slot.verified:
            flags.append("unverified")
        suffix = f"  ({', '.join(flags)})" if flags else ""
        typer.echo(f"{slot.number:<3} {slot.name:<24} {slot.ping:>4}ms  {slot.guid}{suffix}")
    typer.echo(f"{len(slots)} connected")


@instance_app.command("sessions")
def instance_sessions(
    name: Annotated[
        Optional[str],  # noqa: UP045 — typer resolves Optional more reliably
        typer.Argument(help="Instance to report on. Omit for every instance."),
    ] = None,
    config_path: ConfigPath = Path("config.toml"),
    limit: Annotated[int, typer.Option("--limit", help="How many rows to show.")] = 20,
) -> None:
    """Hours logged per player, from the admin logs.

    Reads any new or grown .ADM files first, so this is also how you backfill a server
    that has been running longer than dzsm has.
    """
    cfg = _load_config(config_path)
    if name is not None and name not in cfg.instances:
        _fail(ValueError(f"unknown instance {name!r}"))

    targets = [name] if name else list(cfg.instances)
    try:
        for instance_name in targets:
            players.ingest(cfg, instance_name)
        ranking = players.totals(cfg, instance=name, limit=limit)
    except players.PlayerStoreError as exc:
        _fail(exc)

    if not ranking:
        typer.echo("no sessions recorded yet (is -adminlog on, and has anyone played?)")
        return

    typer.echo(f"{'Player':<24} {'Hours':>8} {'Sessions':>9}  Last seen")
    for entry in ranking:
        hours = entry.seconds / 3600
        typer.echo(
            f"{entry.name:<24} {hours:>8.1f} {entry.sessions:>9}  "
            f"{entry.last_seen.strftime('%Y-%m-%d %H:%M')}"
        )


@instance_app.command("rcon")
def instance_rcon(
    name: str,
    command: Annotated[str, typer.Argument(help="Raw BattlEye command, e.g. 'players'.")],
    config_path: ConfigPath = Path("config.toml"),
) -> None:
    """Run a raw RCON command and print the reply."""
    cfg = _load_config(config_path)
    state = _load_state(cfg)
    if name not in cfg.instances:
        _fail(ValueError(f"unknown instance {name!r}"))

    try:
        client = runner.rcon_client(cfg, name)
    except RconError as exc:
        # rcon_client connects, so this is where a still-starting server surfaces.
        _fail(_with_startup_hint(cfg, state, name, exc))
    if client is None:
        _fail(ValueError(f"instance {name!r} has rcon_enabled = false"))

    try:
        typer.echo(client.command(command))
    except RconError as exc:
        _fail(_with_startup_hint(cfg, state, name, exc))
    finally:
        client.close()


def _with_startup_hint(
    config: Config, state: State, instance_name: str, exc: RconError
) -> RconError:
    """Explain an RCON failure that is really just an instance still starting up."""
    remaining = runner.rcon_grace_remaining(config, state, instance_name)
    if remaining <= 0:
        return exc
    return RconError(
        f"{exc}\nThis instance started recently — BattlEye usually needs a few minutes "
        f"on a modded server before RCON answers. Try again in about "
        f"{remaining:.0f}s, or lower rcon.startup_grace_seconds if that is too "
        f"conservative for this box."
    )


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
    if not path.exists():
        # Every command needs a config, so this is the first thing a new user hits.
        # Saying only "no such file" leaves them with nowhere to go.
        _fail(
            ConfigError(
                f"no config at {path}.\n"
                f"Run `dzsm init` to create one, or point --config (or DZSM_CONFIG) at "
                f"an existing file."
            )
        )
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
