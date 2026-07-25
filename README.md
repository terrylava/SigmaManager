# dzsm — DayZ Server Manager

A replacement for the deprecated OmegaManager (`om.py`). Downloads and updates the DayZ
server app and Steam Workshop mods via steamcmd, places them where each server instance
needs them, and supervises the running instances.

The full design is in [dayz-server-manager-spec.md](dayz-server-manager-spec.md). Read it
before changing anything — the directory layout and the no-junctions decision are load
bearing, not stylistic.

## The one-paragraph version

A running DayZ server holds OS-level locks on its mod files, so mods cannot be updated in
place while any server using them is up. `dzsm` never overwrites a mod directory: every
download lands in a new immutable `modcache/<workshop_id>/<time_updated>/` directory, and
an instance picks up a new version only at its own restart, by pointing `-mod=` at a
different absolute path. Download is fully decoupled from activation, so staggered
restarts stop being a deadlock.

Milestone 1 is done: `dzsm mods check` and `dzsm mods update` can query the Steam Web
API, download via steamcmd, verify the download actually happened, and promote it to an
immutable version directory. `ruff`, `mypy --strict`, and `pytest` (59 tests) pass.

No steamcmd run has been executed against a real Steam account yet — the Web API path is
verified against live Steam, the download path only against fakes.

| Module | State |
|---|---|
| `config.py` | Done |
| `state.py` | Done |
| `steam.py` | Done |
| `mods.py` | Done |
| `instance.py` | Done |
| `runner.py` | Done |
| `cli.py` | Done |
| `api.py` | Done |

## Standalone binary

```bash
pip install -e ".[dev,web,build]" && pyinstaller dzsm.spec --noconfirm --clean
```

Produces `dist/dzsm.exe` (~18 MB), which runs on a machine with **no Python installed** —
the point being that a game server box should not need a development environment. Copy the
one file across and it works.

Two things to know about the default onefile build:

- It unpacks to a temp directory on every start, costing a second or two, and leaves a
  `_MEI*` directory behind if the process is *killed* rather than stopped cleanly.
- Frozen binaries are false-positived by antivirus reasonably often.

If either matters — and for a long-lived service the first one might — set
`ONEFILE = False` in `dzsm.spec` for a folder build that starts instantly and leaves
nothing behind.

## Running it always

```bash
dzsm daemon
```

One long-running process: supervises every instance (crash restart, scheduled restart,
RCON warnings) and serves the dashboard and API. This is what to run as a service.

Instances are deliberately **left running** when it exits — dzsm going down must not take
the game servers with it. Restart the daemon and it adopts them again from `state.json`.

`dzsm instance stop` holds an instance down: the supervisor leaves it alone until you
start it again, so taking a server out for maintenance works without stopping the daemon.
The hold is in `state.json`, so it survives a daemon restart. Use `--no-hold` to stop one
and let the supervisor bring it straight back, which is how to bounce it.

### As a Windows service

The simplest option with no extra software is Task Scheduler, run at startup whether or
not anyone logs in:

```bash
schtasks /create /tn dzsm /tr "\"D:\SigmaManager\.venv\Scripts\dzsm.exe\" daemon --config D:\dz\config.toml" /sc onstart /ru SYSTEM /rl HIGHEST
```

For real service semantics — automatic restart if dzsm itself dies, proper stop control —
use [NSSM](https://nssm.cc/):

```bash
nssm install dzsm "D:\SigmaManager\.venv\Scripts\dzsm.exe" "daemon --config D:\dz\config.toml"
```

### As a systemd service

```ini
[Unit]
Description=dzsm
After=network-online.target

[Service]
ExecStart=/opt/dzsm/.venv/bin/dzsm daemon --config /opt/dz/config.toml
Restart=always
RestartSec=10
# dzsm restarting must not take the servers with it.
KillMode=process

[Install]
WantedBy=multi-user.target
```

## Web API

```bash
dzsm serve
```

Serves a read-only dashboard at `/` and a JSON API under `/api`. Binds `127.0.0.1` by
default; binding anywhere else requires a token, since these routes can start, stop, and
delete things. Set one with `DZSM_API_TOKEN` and send it as `X-DZSM-Token` or
`Authorization: Bearer`. Reads stay open either way. Interactive docs at `/docs`.

## First run

steamcmd installs itself. The first command that needs it downloads Valve's archive to
`<root>/steamcmd/` and extracts it; set `auto_install = false` under `[steam]` to refuse
that and get instructions instead.

Seed Steam Guard once. steamcmd caches a sentry afterward, and every later dzsm command
runs non-interactively:

```bash
dzsm login --interactive
```

Then verify it took:

```bash
dzsm login
```

If you would rather not rely on the credential cache, export the password instead —
never put it in config.toml:

```bash
$env:DZSM_STEAM_PASSWORD = "..."
```

Three assumptions in spec §5 are unverified and gate real work. See CLAUDE.md.

## Getting started

With the standalone binary (see below), or after installing from source:

```bash
dzsm init
```

It asks where things should live and which Steam account owns DayZ, writes a
`config.toml` that already validates, and prints what to do next:

```
  1. dzsm login --interactive     seed Steam Guard once
  2. dzsm server update           download the DayZ server (~4 GB)
  3. dzsm gui                     dashboard, in your browser
```

`dzsm gui` supervises everything, serves the dashboard, and opens it — creating a config
first if there isn't one, so it also works as the very first command you run.

From the dashboard you can start, stop, and restart instances, update mods, and update
the server app. Two things stay in the terminal on purpose: **Steam Guard**, which needs a
code typed into steamcmd itself, and **adding or removing mods and instances**, which is
editing `config.toml`.

## From source

Requires Python 3.11+.

```bash
python -m venv .venv && .venv\Scripts\activate && pip install -e ".[dev,web]"
```

## Checks

```bash
ruff check . && mypy && pytest
```
