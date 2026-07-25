# CLAUDE.md — dzsm

A DayZ server manager replacing the deprecated OmegaManager (`om.py`).

**Read [dayz-server-manager-spec.md](dayz-server-manager-spec.md) before making
non-trivial changes.** It is the design of record. This file is the short version plus
the things that are easy to get wrong.

## The one constraint that explains the whole design

A running DayZ server holds OS-level locks on its mod files. You cannot update a mod in
place while any server using it is running. With staggered restarts across two servers
sharing one mod copy, there is never a window where nobody holds the lock — which is why
om.py's mods never updated.

**The fix: never overwrite a mod directory; always write a new one.**

```
modcache/<workshop_id>/<time_updated>/
```

`time_updated` comes from the Steam Web API and is the canonical version identity.
Each instance points at a specific version directory. Two instances can run different
versions of the same mod simultaneously, so staggered restarts become a feature.

**Download is decoupled from activation.** Downloading is safe with every server up.
Activation happens only at an instance's own restart, by putting a different absolute
path in `-mod=`.

## Things that will be tempting and are wrong

- **Do not reintroduce junctions or symlinks.** An earlier design used them and it was
  rejected: a junction *shares the lock* with its target, so it doesn't solve the
  problem it appears to solve; deleting one can fail when a file inside is open; and
  `shutil.rmtree()` can walk through the reparse point and delete the target. Activation
  is an absolute path in `-mod=` and nothing else.
- **Do not build a shell string for the launch command, and never `shell=True`.** Use an
  argv list with `subprocess.Popen`. Python passes arguments verbatim, so backslashes,
  spaces, and drive letters are safe.
- **Do not sort or derive mod load order.** It is the order in `InstanceConfig.mods`,
  which is the semicolon order in `-mod=`. Framework mods load before dependents.
- **steamcmd exit 7 is not a failure.** It means steamcmd replaced its own binary and
  restarted, which every fresh install does on its first run — the session itself
  usually completed. `STEAMCMD_SELF_UPDATE_EXIT` handles it. `login_interactive` can't
  inspect that run (nothing is captured), so on 7 it proves the outcome by doing a
  non-interactive login, which can only succeed from cached credentials.
- **Do not trust steamcmd's exit code.** It exits 0 on failed downloads. This is almost
  certainly what killed om.py. Verify explicitly after every invocation: folder exists,
  folder non-empty, `.acf` manifest timestamp advanced, stdout free of known error
  strings. Only then promote a snapshot.
- **Do not use anonymous Steam login.** DayZ requires an account that owns the game, for
  both the server app (223350) and workshop content (221100).
- **Do not put the Steam password in config.** Env var or OS keyring only. Better still,
  seed the sentry with `dzsm login --interactive` and set no env var at all — steamcmd
  then authenticates from its own cache and dzsm never handles the password.
- **`steam.login_interactive()` intentionally leaves stdin open. Do not "fix" it.**
  Every other steamcmd call closes stdin so a Guard prompt fails fast instead of hanging.
  That one function is the deliberate exception: it exists so the user can type their
  password and Guard code straight into steamcmd, which is what seeds the sentry that
  makes all the other calls work non-interactively. It also passes only the username —
  dzsm must never read, log, or store the password there.
- **Keep the root path short** (`D:\dz`, not a Documents subfolder). MAX_PATH is a real
  runtime failure here; config load validates it.

## Non-negotiable code rules (spec §7)

1. **A function that returns a collection returns a collection** — empty on failure, or
   it raises. Never `False`. om.py died on `TypeError: 'bool' object is not iterable`
   1000 lines from the actual problem. `mypy --strict` is configured to enforce this;
   do not relax it.
2. **Never `shutil.rmtree()` anything that might be a reparse point.** Use
   `mods.safe_rmtree()`. `os.path.islink()` is unreliable for Windows junctions;
   `st_reparse_tag` is the trustworthy signal.
3. **Fail loudly.** Every failure path logs mod id, instance name, and operation. Never
   swallow an exception to keep a loop alive without recording what was lost.
4. **Never block a restart on a mod update failure.** Keep the instance on its old
   version, log loudly, start anyway, retry next restart.

## Layout

```
dzsm/
  config.py    pydantic models, TOML load, path-length validation
  state.py     state.json, atomic read/write
  steam.py     steamcmd wrapper, Web API update check, download verification
  mods.py      version cache, snapshot promotion, refcount, GC
  instance.py  instance layout, keys, launch arg construction
  runner.py    process supervision, crash + scheduled restart
  cli.py       typer commands
  api.py       fastapi (deferred until the CLI is real)
```

Runtime layout (under `root`, not the repo): `steamcmd/`, `serverapp/<version>/`,
`modcache/<id>/<version>/`, `localmods/`, `instances/<name>/`, `state.json`.

## Current status

**Milestone 1 complete.** `dzsm mods check` and `dzsm mods update` run the full path:
Steam Web API `time_updated` lookup → steamcmd download → four-part verification →
promotion to an immutable version directory → state save. `dzsm login` verifies
credentials, and `dzsm login --interactive` seeds Steam Guard.

`steam.ensure_steamcmd()` downloads and extracts Valve's steamcmd when it is missing, so
no manual install step exists. It runs from `_run_steamcmd` and `login_interactive`, is
idempotent, and guards extraction against archive members that escape the target
directory — it writes an executable, so the archive is treated as untrusted input.

Also done: config models + validation, state models + atomic I/O,
`is_reparse_point`/`safe_rmtree`, launch-arg construction, `mods list`.

Still stubbed (each raises `NotImplementedError` with a spec section reference):
`runner.py` entirely, `mods.refcounts`/`collect_garbage`, `instance.ensure_layout`/
`sync_keys`, `steam.update_server_app`, `api.py`.

The Web API path is verified against live Steam. **The steamcmd path has never run
against a real Steam account** — it is covered only by fakes, so the first real
`mods update` is the actual test of `download_mod`.

Two details worth knowing before touching this code:

- `config.workshop_download_dir` derives from `steam.steamcmd_path.parent`, not from
  `root`. steamcmd puts downloads under its own install dir, so deriving it from the
  binary keeps the paths right even when the layout differs from spec §3.
- `allow_long_root` exists solely so tests can build a `Config` rooted at pytest's
  `tmp_path`. It is not a normal thing for a user to set.

### Unverified assumptions — these gate real work (spec §5)

| # | Assumption | Confidence |
|---|---|---|
| 1 | `-mod=` accepts absolute paths including drive letters | ~85% |
| 2 | `.bikey` files must be copied into the instance's `keys/` when the mod lives outside the server dir | ~65% |
| 3 | Native Linux DayZ server is viable on stable branch (sources conflict; may be experimental-only) | ~45% |

#1 gates `instance.py`. If it fails, fall back to per-instance copies — but keep version
directories regardless. `config.py`, `state.py`, `steam.py`, and `mods.py` do not depend
on any of these.

### Build order (spec §11)

`config.py` + `state.py` → `steam.py` → `mods.py` → `instance.py` → `runner.py` →
`cli.py` → `api.py`.

**First milestone:** a CLI that logs in, checks one mod for updates via the Web API,
downloads it if changed, verifies the download actually succeeded, and promotes it to a
version directory. No instances, no launching. That single path exercises every hard part
of the Steam integration.

## Environment

Python 3.11+ required (`tomllib` in stdlib, `st_reparse_tag` available). Python 3.12.10
is installed at `%LOCALAPPDATA%\Programs\Python\Python312`, and `.venv/` in the repo root
has the project installed editable with dev extras.

```bash
.venv\Scripts\activate && ruff check . && mypy && pytest
```

All three pass. `mypy --strict` is on; `warn_unreachable` is deliberately off (see the
comment in pyproject.toml — it misfires on platform-guarded branches across the CI
matrix). Do not "fix" a mypy failure by loosening the config; Rule 1 depends on it.
