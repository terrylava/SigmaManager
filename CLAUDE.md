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
- **`-config` must be absolute.** The working directory is the server installation, so a
  bare `-config=serverDZ.cfg` resolves to the *server's* config and every instance
  silently shares one — hostname, `instanceId`, `verifySignatures`, mission template, all
  of it. This shipped as a real bug and was only caught because a per-instance mission
  template failed to take effect. Same reasoning as `-mod=`: absolute or it is wrong.
- **Do not sort or derive mod load order.** It is the order in `InstanceConfig.mods`,
  which is the semicolon order in `-mod=`. Framework mods load before dependents. The
  same applies to `server_mods` → `-serverMod=`; a mod belongs to one list or the other,
  and config load rejects an overlap.
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

**Instances and supervision work too.** `dzsm instance start|stop|status|supervise` are
live: activation resolves the newest cached version at start, writes it into
`state.pins`, syncs `.bikey` files into the instance's `keys/`, and spawns the server
with an absolute-path `-mod=`. Verified end to end against DayZ 1.29 — started, showed
`CF=1771519119` running on port 2402, loaded the mod from the version directory, stopped
cleanly.

Also done: config models + validation, state models + atomic I/O,
`is_reparse_point`/`safe_rmtree`, launch-arg construction, `mods list`, refcounting.

**Automatic deletion is opt-in and off by default** (spec §8, revised 2026-07-25 — the
original always-on GC was made optional). `gc_retention_days` is unset unless the user
sets it; do not give it a default. When enabled, pruning removes only versions that are
unused, **not the newest for their mod**, and past the window — that middle condition is
what stops it deleting an update nobody has activated yet.

**`keep_mods` protects a temporarily disabled mod.** A name moved from an instance's
`mods` to its `keep_mods` is not launched, is not reported as unused, counts as `in_use`
for retention, and cannot be deleted without `--force` — so re-enabling it is moving the
line back, not re-downloading. The guard lives in **both** `delete_version` and
`remove_mod`: a per-version delete would otherwise walk straight around a whole-mod
protection, which is a bug that actually shipped once and destroyed a 667 MiB cache.

`dzsm mods versions` reports, `dzsm mods prune` applies the policy (dry run without
`--apply`), `dzsm mods delete <mod> <version>` removes one explicitly, and `dzsm mods
unused` finds whole mods nothing references — including **orphaned** cache directories
whose workshop id is not in config at all, which every other command is blind to because
they all iterate configured mods. Local mods are reported there but never deletable: that
directory is hand-authored source dzsm did not create. Deletion refuses a
version an instance is pinned to (`--force` overrides, and clears the pin), and *never*
one a running instance is pinned to — the server holds OS-level locks on those files,
`--force` included. Pruning goes through the same path, so automation gets no extra
privileges.

**The server app is managed too.** `dzsm server update` downloads appid 223350 via
steamcmd into `serverapp/<buildid>/`, versioned exactly like mods and for the same
reason — a running server locks its own binaries, so nothing may be overwritten in place.
The version identity is Steam's `buildid` from `appmanifest_223350.acf`, the server app's
equivalent of a mod's `time_updated`. Verification refuses a run that produced no
manifest, no buildid, or no executable, regardless of exit code.

**Instances select their build.** `server_build` pins one to a specific downloaded build;
unset means the newest. Resolution is instance pin → `config.server_app_path` → newest
downloaded, and the instance pin beats the global external path on purpose (more specific
intent). The choice activates at start and lands in `state.server_builds`, exactly like
mod versions — a new build never takes effect under a running server.

**RCON is BattlEye's protocol** (`dzsm/rcon.py`): UDP, CRC32-framed, sequence-numbered.
A wrong CRC gets *no reply at all* rather than an error, so framing bugs look exactly like
an unreachable server — hence the byte-level tests. Server-message packets must be
acknowledged or BattlEye retransmits them, and they interleave with command replies, so
the client acks and keeps waiting rather than treating one as the answer.

RCON is **on by default** on `port + 3` (a 2302 server → 2305, the stock DayZ layout).
`rcon_port` overrides, `rcon_enabled = false` opts out.

**BattlEye ships no config**, so dzsm writes one before launch with a `secrets`-generated
password, and reads it back from there — no env var needed, though one still overrides.
There is deliberately no default password; a shared one is worse than none.

Two BattlEye behaviours to not break:

- **It consumes the config on startup**, renaming `BEServer_x64.cfg` →
  `BEServer_x64_active_<hex>.cfg`, and renames it back on clean shutdown. Reading the
  password *and* checking whether one exists must both consider the renamed file
  (`battleye_config_candidates`). Miss that and dzsm generates a new password every start,
  silently breaking anything that shares it. There is a test for exactly this.
- **RCON binds ~43s after start on vanilla**, long after the mission loads, and much
  later on a heavily modded server. `startup_grace_seconds` (300) is how long dzsm assumes
  it will not answer: automated attempts inside that window are skipped rather than left
  to time out, while manual `say`/`rcon` still try and explain themselves on failure.
  Anything calling RCON automatically should check `rcon_grace_remaining` first.

Restart warnings run in two tiers: every 5 minutes from 30 minutes out, then every 4
seconds for the last 5. The supervise tick shortens to 4s inside the final window — a 15s
poll cannot deliver a message every 4s — but is never lengthened during the early tier,
since crash detection still has to happen on time.

**A warning failure must never affect the restart**: an unreachable port or bad password
is logged and the restart proceeds. Same reasoning as Rule 4 — a silent restart is a
nuisance, a server that fails to restart is an outage. The same applies to `stop`, which
escalates RCON `#shutdown` → terminate → kill; only the first flushes persistence
properly, but none of them may block on RCON working.

**Restarts have two sources**: `restart_schedule` (wall-clock) and `restart_after_hours`
(uptime, **default 4**), whichever is sooner. The uptime deadline is recomputed after each
restart so it counts from the new process. A down instance has no uptime deadline.
`next_restart` needs `started_at` — state stores it timezone-aware and the scheduling
arithmetic is naive local, so it is normalised on the way in rather than raising.

**Missions are per-instance by default** (`per_instance_missions`). The engine has no
mission path argument, so this means per-instance mission *names* —
`<mission_prefix>_<instance>.<terrain>`, default `sigma_chernarus.chernarusplus`, inside
the server's `mpmissions/`, seeded once and never overwritten afterwards. The instance
name must stay in the derived name: a bare prefix would put every instance back on one
shared mission.

**A mission missing from the current build is carried forward from the newest other
build**, not re-seeded from vanilla. Without that, a server update silently reverts every
instance's economy and orphans its persistence in the old build's directory, while the
server comes up looking healthy. Persistence is copied in that case — it is the same
instance's own saved world. Isolated instances get the same protection via a stash across
the server-copy replace, which happens before mission handling could otherwise help.
"Newest" is by build id, not directory mtime: mtime does not change when a nested file is
edited. Tens of MB, versus the several GB an isolated server costs.
`storage_*` is excluded when seeding, since that is another server's saved world. dzsm
owns the `template` line in the instance's serverDZ.cfg and keeps it pointed at the
resolved mission — validating instead would leave a mismatched instance quietly running
the shared one.

**`isolated_server = true`** gives an instance a private copy of its build at
`instances/<name>/server/`, so its own `keys/` and mission files. Without it, instances
sharing a build share that build's key set. The copy is keyed by a `.dzsm-build` marker so
an unchanged build never triggers a multi-GB recopy; `--refresh-server` forces one, which
is the only way to pick up changes to an `external` install since dzsm cannot detect them.
Copying happens only with the instance stopped.

Disk is explicitly the secondary concern here — managing many servers, mods, and versions
comes first. Do not "optimize" isolation or the version cache away.

**`dzsm init` is the entry point for a new user**, and the only command that works
without a config. Every other command routes a missing config through `_load_config`,
which points at it — without that a first-time user is dead-ended, since the binary
distribution has no `config.example.toml` in it. The generated config must *validate as
written* and contain a startable instance; a config that needs fixing before it loads is
the same dead end one step later.

Two things about generating TOML: single quotes are **literal** strings, so Windows paths
go in unescaped (escaping puts two backslashes in the value), and the file is kept to
plain ASCII because people open it in whatever editor Windows gives them.

**There is a PyInstaller build** (`pyinstaller dzsm.spec`). Two things break silently in a
frozen build and are already handled — do not undo them:

- `dashboard.html` is a data file, so `api._dashboard_path()` looks under `sys._MEIPASS`
  when frozen. Any other file read relative to `__file__` needs the same treatment.
- uvicorn resolves its loop, protocol, and lifespan implementations by string at runtime,
  so `collect_submodules("uvicorn")` is in the spec's hiddenimports. Without it the binary
  builds fine and dies the moment `serve` starts.

**`dzsm daemon` is the long-running form**: supervision in a background thread, uvicorn on
the main one, so a Ctrl-C or service stop shuts both down. Three properties it must keep:

- **A stopped instance stays stopped.** `runner.stop` records a hold in `state.held`, and
  the supervisor skips held instances entirely — no crash restart, no scheduled restart —
  until `start` releases it. A supervisor that undoes an explicit stop makes maintenance
  impossible and pushes people to kill the supervisor instead, losing every other
  instance's crash restarts. `hold=False` exists for the two cases that are not a request
  to stay down: `--no-hold`, and the supervisor's own restart.
- **Instances are left running when it exits.** dzsm going down must not take the game
  servers with it; it adopts them again from `state.json` on restart.
- **The supervisor waits on an `Event`, not `sleep`**, so a service stop does not sit
  through a poll interval.
- **State is re-read at the top of each supervision pass.** The API mutates it from
  another thread of the same process; without the reload, the supervisor's older copy
  would overwrite those changes at its next save.

**The web API is live** (`dzsm serve` alone, or inside `dzsm daemon`). It is deliberately thin — every route calls the
same functions the CLI does, so the two can never disagree about what "running" or
"pinned" means. Do not let it grow its own model of an instance or a version.

Access model, which is the part to not weaken:

- Reads are open; they disclose nothing secret.
- Writes require the token from `api.token_env` when one is set, compared with
  `secrets.compare_digest`.
- With no token, writes are still allowed — because `serve` **refuses to bind a
  non-loopback address without one**. That guard is what makes the whole model hold; if
  you ever make writes open on a bound interface, the refusal must go with it.
- `tests/test_api.py::test_every_write_route_requires_the_token` lists every write route
  explicitly. A new one added without the dependency fails that test rather than shipping
  unauthenticated. Add to the list when you add a route.

Downloads return 202 with a job id; jobs are in-memory and deliberately not persisted, as
a "running" job restored from disk after a restart would be a lie.

**`server_app_path` is now a fallback, not the norm.** dzsm downloads and versions the
server app itself; this option points at an externally installed server for people who
would rather Steam managed it. dzsm only reads from there — profiles, logs, and
serverDZ.cfg live under `instances/<name>/`, and keys go to that install's `keys/`.

Everything here has been exercised against live Steam and a real DayZ 1.29 server:
steamcmd bootstrap, Guard seeding, cached-credential login, mod download and
verification, promotion, server app download, instance start/stop, supervision with crash
and scheduled restarts, key sync confirmed by a connecting client, `-serverMod=`, and an
isolated per-instance server copy.

Two details worth knowing before touching this code:

- `config.workshop_download_dir` derives from `steam.steamcmd_path.parent`, not from
  `root`. steamcmd puts downloads under its own install dir, so deriving it from the
  binary keeps the paths right even when the layout differs from spec §3.
- `allow_long_root` exists solely so tests can build a `Config` rooted at pytest's
  `tmp_path`. It is not a normal thing for a user to set.

### Unverified assumptions — these gate real work (spec §5)

| # | Assumption | Status |
|---|---|---|
| 1 | `-mod=` accepts absolute paths including drive letters | **CONFIRMED 2026-07-25** |
| 2 | `.bikey` files go in the **server's** `keys/` dir, not the instance's | **CONFIRMED 2026-07-25** |
| 3 | Native Linux DayZ server is viable on stable branch (sources conflict; may be experimental-only) | ~45%, open |

**#1 is settled.** DayZ 1.29 server on `G:` loaded a CF version directory on `D:` with
`verifySignatures = 2` — PBOs mounted from the absolute path and CF's script defines
(`JM_CommunityFramework`, `CF_MODULE_CONFIG`, …) appeared, so it compiled and ran. No
signature errors. The per-instance-copy fallback is not needed; `instance.py` is
unblocked, and `build_launch_args` is validated in shape.

**#2 is settled too**, by an actual client connection (a server boot cannot answer it —
a server loads its own mods without keys; `keys/` governs whether a connecting *client's*
mods are accepted). Keys go in `<server dir>/keys/`, which is inside a promoted
`serverapp/<buildid>/` directory. **This is the only place dzsm writes into one**, and it
is safe because it only ever adds: an existing key of the same name is skipped rather
than re-copied (a running server may have it open), and keys are never deleted. Do not
"tidy" that into an overwrite or a sync-with-delete.

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
