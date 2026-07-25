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

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv && .venv\Scripts\activate && pip install -e ".[dev]"
```

## Checks

```bash
ruff check . && mypy && pytest
```
