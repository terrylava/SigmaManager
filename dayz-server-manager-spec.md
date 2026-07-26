# DayZ Server Manager — Design Spec

Handoff document for a Claude Code session. Written to be read cold.

---

## 1. What this is

A replacement for OmegaManager (`om.py`), a now-deprecated DayZ server manager. The
successor tool ("Architect") is subscription-only, so we're building our own.

The tool must:

1. Download and update the DayZ **server app** via steamcmd (appid `223350`)
2. Download and update **Steam Workshop mods** via steamcmd (client appid `221100`)
3. Support **local / non-workshop mods** for testing
4. Place mods and keys where each server instance needs them
5. Launch server instances and supervise them (restart on crash + on schedule)

Target: **CLI + config file first**, web UI later. Cross-platform (Windows + Linux).
Must scale to **many instances**.

---

## 2. The constraint that drives the whole design

**A running DayZ server holds OS-level locks on its mod files.** You cannot update
mod files in place while any server using them is running.

OmegaManager's consequence: with two servers on staggered restart times sharing a
single mod copy, there is never a window where no server holds the lock — so mods
**never update**.

OmegaManager's solution was three tiers of full copies:
1. steamcmd download dir
2. a staging mods folder
3. a third full copy inside *each* server instance

That works but costs `N_instances + 2` copies of every mod on disk.

### Our solution: immutable versioned directories

**Never overwrite a mod directory. Always write a new one.**

```
modcache/
  1559212036/                 # workshop id
    1719878400/               # version = time_updated from Steam Web API
    1721005200/               # new version, written while servers are running
```

Nothing is ever written into a directory a server holds open. The lock problem is
solved *by construction*, not by copy count.

Each instance points at a **specific version**:

```
chernarus -> modcache/1559212036/1721005200   (restarted, on new version)
namalsk   -> modcache/1559212036/1719878400   (not restarted yet, on old version)
```

Two servers, same mod, different versions, both running. **Staggered restarts become
a feature instead of a deadlock.**

Disk cost is one copy per *version*, not per *instance*.

### Download is decoupled from activation

This is the core thing `om.py` got wrong — it conflated them.

- **Download** can run at any time, with every server up.
- **Activation** happens only at an instance's restart, by pointing that instance's
  launch args at the new version directory.

---

## 3. Directory layout

```
root/
  steamcmd/
    steamapps/workshop/content/221100/<id>/    # steamcmd's mutable working dir
  .serverapp-download/                         # steamcmd's mutable server app copy
  serverapp/
    <buildid>/                                 # versioned like mods; buildid from
                                               # appmanifest_223350.acf
  modcache/
    <workshop_id>/
      <time_updated>/                          # immutable snapshot
  localmods/
    @MyTestMod/                                # never touched by steamcmd
  instances/
    chernarus/
      serverDZ.cfg
      profiles/
      server/                                  # only when isolated_server = true:
                                               # a private copy of one server build
  state.json
  config.toml
```

Three tiers, but different from om.py's:

1. `steamcmd/steamapps/workshop/content/221100/<id>/` — steamcmd's own mutable dir.
   Unavoidable; steamcmd overwrites in place.
2. `modcache/<id>/<time_updated>/` — immutable snapshot, copied out of tier 1.
3. Instance references tier 2 **by absolute path in `-mod=`** (see §4).

---

## 4. Mod activation: absolute paths, not junctions

An earlier iteration of this design used directory junctions (Windows) / symlinks
(Linux) from each instance dir into `modcache`. **That was rejected.** Reasons:

- A junction **shares the lock** with its target. Server A holding a PBO open through
  a junction locks the file in `modcache`, so the update fails exactly as before.
  Junctions save disk but do not solve the locking problem.
- Deleting a directory junction **can fail** if a file inside is open and locked
  (Access Denied), so hot-swapping a junction is not reliable.
- `shutil.rmtree()` on a junction can walk through the reparse point and delete the
  *target* — a live footgun that has cost people their whole mod cache.

Instead: put the version directory's **absolute path directly in `-mod=`**.

```
-mod="D:\dz\modcache\1559212036\1721005200;D:\dz\localmods\@MyTestMod;"
```

There is nothing to create, delete, or swap. Activation is just "write different
launch args at next restart." Versioning is still required — the server still locks
files at the absolute path — but the swap machinery disappears entirely.

### Rules for building the `-mod=` string

- **Never build a shell string. Never use `shell=True`.** Use a list and
  `subprocess.Popen`. Python passes each argument verbatim, so backslashes, spaces,
  and drive letters are all safe:
  ```python
  args = [
      exe,
      "-config=serverDZ.cfg",
      f"-port={port}",
      f"-mod={';'.join(mod_paths)};",
      "-profiles=profiles",
      "-dologs", "-adminlog", "-netlog", "-freezecheck",
  ]
  ```
- **Trailing semicolon**: Bohemia's own examples include one. Harmless. Include it.
- **Load order is the semicolon order.** It must come from explicit config, never
  from dict iteration order or alphabetical sort. Framework mods (e.g. CF) load
  before their dependents.
- **Server-only mods go in `-serverMod=`**, built the same way from the instance's
  `server_mods` list. Clients never load these — admin tools and server-side logic. A mod
  belongs to one list or the other, never both; config load rejects the overlap. The
  argument is omitted entirely when the list is empty. Their `.bikey` files are still
  synced: not strictly required, since clients do not load them, but omitting one would
  kick any player who happens to have that mod enabled.
- **`MAX_PATH` (260 chars) is a real risk.** Mod trees are deep and version dirs add
  segments. Keep the root **short** — `D:\dz\`, not
  `C:\Users\Lava\Documents\DayZ Server Manager\`. Alternatively enable Win32 long
  path support in the registry. Validate path length at config load and fail loudly.

---

## 5. Things to VERIFY on the actual box before writing much code

These are load-bearing assumptions that are not fully confirmed. Test them first.

| # | Assumption | Confidence | How to test |
|---|-----------|-----------|-------------|
| 1 | `-mod=` accepts absolute paths incl. drive letters | **CONFIRMED 2026-07-25** | Launch one instance with an absolute modcache path; check RPT for mod load lines and no signature errors |
| 2 | `.bikey` files must be placed in the **server's** `keys/` dir when the mod lives outside the server dir | **CONFIRMED 2026-07-25** | Run with `verifySignatures=2` and a mod outside the server dir; see if a client can connect |
| 3 | Native Linux DayZ server viability on stable branch | ~45% | Sources conflict. Bohemia's wiki documents `./DayZServer` for Linux, but other sources say the native Linux build is experimental-branch only and stable requires Wine/Proton. Confirm before committing to a Linux code path |

**Test #1 command:**
```
DayZServer_x64.exe -config=serverDZ.cfg -port=2302 "-mod=D:\dz\modcache\1559212036\1721005200;" -profiles=profiles -dologs
```
Check the RPT log for mod load lines and signature errors.

If #1 fails, fall back to per-instance copies (om.py's approach) — but keep the
version directories regardless.

### Test #1 result — CONFIRMED, 2026-07-25

Ran the DayZ 1.29 server (installed on `G:`) against a CF version directory on `D:`,
with `verifySignatures = 2`:

```
FileSystem: Adding package 'D:\sigmamanagertest\modcache\1559212036\1771519119\addons\legacy.pbo' to RV filesystem
FileSystem: Adding package 'D:\sigmamanagertest\modcache\1559212036\1771519119\addons\scripts.pbo' to RV filesystem
FileSystem: Adding package 'D:\sigmamanagertest\modcache\1559212036\1771519119\addons\gui.pbo' to RV filesystem
```

The script module defines then listed `JM_CommunityFramework`, `CF_MODULE_CONFIG`,
`CF_EXPRESSION`, `CF_MODSTORAGE`, `CF_SURFACES` and the rest of CF's defines, so the mod
did not merely mount — its Enforce Script compiled and loaded. Startup proceeded into
world and entity creation with no signature or `.bikey` errors.

Absolute paths work, including across drives. **The fallback to per-instance copies is
not needed.** Activation stays "write a different path into `-mod=` at next restart."

Note this does *not* settle #2. The server loads its own mods without needing keys; the
`keys/` directory governs whether a connecting *client's* mods are accepted, and no
client was involved in this test.

### Test #2 result — CONFIRMED, 2026-07-25

Keys belong in the **server installation's** `keys/` directory — `<server dir>/keys/` —
not in the instance directory. `-profiles` does not affect key lookup.

Verified by connecting a real client to an instance running with `verifySignatures = 2`,
CF loaded from `modcache/`, and CF's `Jacob_Mango_V3.bikey` placed next to vanilla
`dayz.bikey` in `serverapp/<buildid>/keys/`. The client connected.

**This is the one place dzsm writes into a promoted `serverapp/<buildid>/` directory**,
which everything else treats as immutable. It is safe because the immutability rule
exists to stop files being overwritten while a running server holds locks on them, and
this only ever *adds* new files:

- An existing key of the same name is skipped, never re-copied.
- Keys are never deleted.

Consequence: instances sharing a server build share one key set, so a mod trusted on one
instance is trusted on all of them. om.py avoided this by giving each instance a full
server copy; this design accepts the shared key set rather than duplicating ~4 GiB per
instance. Keys are tiny and accumulating them is harmless.

---

## 6. Steam integration

### Login

**Anonymous login does not work for DayZ.** Both the server app (`223350`) and
workshop downloads require a Steam account that owns DayZ. This is confirmed by
multiple sources and is the #1 gotcha for people coming from other games.

- Seed Steam Guard **once, interactively**. steamcmd caches a sentry file afterward,
  making subsequent runs non-interactive.
- **Never store the password in the config file.** Use an environment variable or the
  OS keyring (`keyring` package).
- Detect the Steam Guard prompt in steamcmd output and fail with a clear, actionable
  message rather than hanging forever on stdin.

### Update detection

Before downloading anything, ask the Steam Web API what changed.

- Endpoint: `ISteamRemoteStorage/GetPublishedFileDetails/v1/`
  (POST, form-encoded, `itemcount=N` + `publishedfileids[0]=<id>`), or
  `IPublishedFileService/GetDetails`.
- Read `time_updated` per mod.
- Compare against `state.json`.
- **Download only what changed.** This turns a 20-minute blind update cycle into
  ~30 seconds.
- `time_updated` doubles as the version directory name — it is the canonical
  version identity.

### steamcmd is not trustworthy

**steamcmd exits 0 on failed downloads.** This is almost certainly the root of the
bug that killed om.py. After *every* steamcmd invocation, verify explicitly:

- The expected folder exists at `steamapps/workshop/content/221100/<id>`
- The folder is non-empty
- The `.acf` manifest timestamp advanced
- Parse steamcmd stdout for known error strings (rate limiting, `Timeout downloading
  item`, disk space, login failure)

Only after all checks pass may the snapshot be promoted to a version directory.

---

## 7. Non-negotiable code rules

### Rule 1: functions that return collections never return a bool

The bug that broke om.py was:

```
TypeError: 'bool' object is not iterable
  File "om.py", line 5450, in get_tracked_workshop_items
```

Some function returned `False` on a failure path and a list on the success path. The
caller iterated it and crashed 1000 lines away from the actual problem.

**A function that returns a list returns a list — empty on failure, or it raises.**
Never `False`. Enforce this with type hints and `mypy --strict` in CI.

### Rule 2: never `shutil.rmtree()` anything that might be a reparse point

Even though the current design avoids junctions, GC code will be deleting
directories. Guard every recursive delete:

```python
import os, stat

def is_reparse_point(p: str) -> bool:
    try:
        st = os.stat(p, follow_symlinks=False)
    except OSError:
        return False
    return bool(getattr(st, "st_reparse_tag", 0))
```

Note: `os.path.islink()` is **unreliable for Windows junctions** — it does not
always return `True`. Do not trust it. `st_reparse_tag` is Windows-only and
Python 3.8+.

### Rule 3: fail loudly, never silently

om.py surfaced a 500 error with a traceback showing only Flask's own frames. Every
failure path must log the mod id, instance name, and operation. Never swallow an
exception to keep a loop going without recording what was lost.

### Rule 4: never block a restart on a mod update failure

If activation fails for any reason, keep the instance on its **old** version, log
loudly, and start the server anyway. Retry at the next restart. A server that is up
with slightly stale mods beats a server that is down.

---

## 8. Version retention

> **Revised 2026-07-25.** This section originally specified time-based garbage
> collection as the only behaviour, on by default with a 7-day window. It is now
> **opt-in and off by default**: `gc_retention_days` is unset unless you set it. An
> always-on retention timer eventually eats a version somebody still wanted, unattended,
> on a box where the whole point of the design is that old versions stay reachable.
> Turning it on is a deliberate choice about a specific disk.

`state.json` tracks which instance is pinned to which version.

- With retention off (the default), versions accumulate until a human deletes one. Disk
  is cheap; an unavailable rollback during an incident is not.
- `dzsm mods versions` reports every cached version with its size, age, whether it is the
  latest, which instances are pinned to it, and whether it is still on disk. It reclaims
  nothing.
- `dzsm mods delete <mod> <version>` removes exactly one version, and only because it was
  asked to.

### Optional retention

Set `gc_retention_days = N` to opt in to automatic pruning. A version is removed only
when **all three** hold:

1. No instance is pinned to it.
2. It is not the newest version of its mod. A freshly downloaded version nothing has
   activated yet is unused by definition — expiring it would delete the update you just
   fetched.
3. It is older than N days.

Pruning runs after `dzsm mods update`, which is when new versions arrive. `dzsm mods
prune` applies the policy on demand and is a dry run unless given `--apply`;
`--older-than N` overrides the configured window, and works even when retention is off.

**Automation gets no extra privileges.** Pruning goes through the same delete path as a
manual removal, so both refusals below apply to it identically.

### Deletion refuses in two cases:

1. **An instance is pinned to it.** That instance would fail to start, or lose a mod, at
   its next restart. `--force` overrides this for a stale pin, and clears the pin so
   state stops claiming a version that no longer exists.
2. **A pinned instance is currently running.** Never overridable, `--force` included. A
   running DayZ server holds OS-level locks on its mod files — the constraint this entire
   design is built around. Stop the instance first.

Rollback stays what it always was: point an instance at an older version directory. That
now works indefinitely rather than for N days.

### Unused mods

Separately from unused *versions*, a whole mod can fall out of use. `dzsm mods unused`
finds three kinds:

- **unreferenced** — configured, but no instance lists it in its `mods` array.
- **orphaned** — a `modcache/<workshop_id>/` directory whose id is not in config at all,
  usually a mod deleted from config without its cache being cleared. This is the case
  nothing else surfaces: `mods versions` and `mods prune` both work from configured mods,
  so an orphan is invisible to them and would sit on disk forever.
- **local** — a configured local mod no instance lists. Reported, but **never deletable
  by dzsm**: the directory is hand-authored source dzsm did not create (spec §9).

`--select` gives a numbered picker, `--all` takes everything removable, and both confirm
before deleting. Removal goes through the same refusals as any other delete.

### Keeping a mod through a temporary disable

An instance's `keep_mods` list holds mods it is not currently loading but still claims.
Moving a name from `mods` to `keep_mods` disables it without abandoning it:

- It is not launched.
- It is not reported as unused (`--include-kept` shows it, labelled with which instances
  keep it).
- **Neither `mods delete` nor `mods unused` removal will touch it** without `--force`.

Re-enabling is moving the name back — the cached versions are still there, so nothing is
re-downloaded. Without this, disabling a mod for a week meant it looked abandoned to
every cleanup path, and a `--all` sweep would reclaim it.

The guard lives in `delete_version` as well as `remove_mod`, deliberately: a per-version
delete would otherwise walk straight around a whole-mod protection and destroy the very
cache the keep exists to preserve.

### RCON and restart warnings

DayZ's RCON is BattlEye's: UDP, CRC32-framed, sequence-numbered. `dzsm/rcon.py` implements
it directly — the framing is worth care because a wrong CRC produces *no reply at all*
rather than an error, so an encoding bug is indistinguishable from an unreachable server.

**RCON is on by default**, on `port + 3` — the stock DayZ layout, where a 2302 server
listens for RCON on 2305. `rcon_port` overrides it; `rcon_enabled = false` opts an
instance out entirely.

### The BattlEye config

**BattlEye ships no default config.** Without a `BEServer_x64.cfg` containing an
`RConPassword`, RCON never listens at all — the port is simply closed, and every warning
fails against nothing. So dzsm writes one per instance before launch, generating a
password with `secrets`. There is deliberately no default password: a shared one is worse
than none.

The password is read back from that same file, so nothing has to be exported. An
environment variable still overrides it for anyone keeping credentials elsewhere.

Two behaviours of BattlEye's own make this fiddlier than it looks:

- **It consumes the file on startup**, renaming `BEServer_x64.cfg` to
  `BEServer_x64_active_<id>.cfg` (the id is hex, e.g. `1dc`). By the time the server is
  up, the original is gone. Anything reading the password afterwards must look at the
  renamed one — and so must anything deciding whether a password already exists, or it
  would mint a new one on every start and silently rotate the password out from under any
  admin tooling sharing it. On a clean shutdown BattlEye renames it back.
- **RCON binds well after the server is otherwise up** — measured at ~43 seconds on a
  *vanilla* 1.29 server, long after the mission has loaded, and a heavily modded server
  can take many minutes. A connection refused immediately after a start is expected, not
  a fault.

`startup_grace_seconds` (default 300) is how long after a start RCON is assumed not to
answer yet. Inside that window:

- **Automated attempts are skipped**, not left to time out. Without this, stopping a
  freshly started instance burned a socket timeout before falling through to signalling
  it; now it skips straight there and stops in about a second.
- **Manual `say`/`rcon` still try** — the window is a conservative default, not a fact
  about a particular box — but a failure explains itself and points at the setting rather
  than looking like a broken client.

It also guards restart warnings, which a short `restart_after_hours` or a crash-restart
loop could otherwise push into a window where nothing can answer.

To rotate a password, delete `RConPassword` from the config (or the file itself) and
restart; a fresh one is generated.

Warnings come in two tiers, counting down in human terms — "20 minutes", "1 minute",
"20 seconds":

| From | Interval |
|---|---|
| 30 minutes before (`early_warning_lead_seconds`) | every 5 minutes |
| 5 minutes before (`final_warning_lead_seconds`) | every 4 seconds |

plus one final message at the restart itself that ignores the rate limit.

The supervision loop's tick shortens to 4 seconds inside the final window — a 15-second
poll obviously cannot deliver a message every 4 seconds — but is never *lengthened* during
the slow early tier, because crash detection still has to happen on time.

### Stopping a server

`stop` escalates: RCON `#shutdown` → `terminate()` → `kill()`. Only the first is properly
clean, since DayZ flushes persistence on its own shutdown path rather than when signalled
from outside. Each step waits `stop_timeout_seconds` before the next.

The server usually dies before answering `#shutdown`, so a missing reply is not treated as
failure — whether it worked is decided by watching the process, not by the reply. RCON not
being configured, or being unreachable, simply falls through to signalling.

### Editing config from the web UI

Three rules, because this rewrites the file the whole tool depends on:

1. **Comments survive.** The generated config explains every setting; rewriting it from a
   parsed dict would discard all of that on the first save. Edits go through tomlkit,
   which round-trips style.
2. **Nothing invalid reaches disk.** A change is applied to a copy, loaded as a real
   `Config`, and only then written. A config that fails to load is worse than a rejected
   edit — every command stops working.
3. **The previous version is kept** alongside as `.bak`, and the write is atomic, so a
   valid-but-wrong change is recoverable.

Instance settings are a **whitelist** (`EDITABLE_INSTANCE_FIELDS`): scalars and lists of
scalars only, so an edit cannot restructure the document. A `null` removes the key and
restores its default, which is what "all cores" and "leave the priority alone" actually
mean.

The running daemon adopts an edit immediately — the API rebinds its config, and the
supervisor re-reads it each pass — so a changed schedule or affinity does not wait for a
restart. A config left briefly unparseable by hand-editing does not take the supervisor
down; it keeps the last one that loaded.

`serverDZ.cfg` is edited as raw text and deliberately **not validated**: it is the
engine's format, not ours, and rejecting keys dzsm does not recognise would block exactly
the settings people come to the editor for.

**A running server holds `serverDZ.cfg` open on Windows**, so replacing it fails with
access denied — the same locking constraint the mod cache is built around, surfacing on
the config file. That returns 409 with an explanation rather than a 500, and it costs
nothing in practice: the change would only take effect at the next start anyway.

### Process tuning

`cpu_affinity` takes **core numbers** — `[0, 1, 2, 3]` — not an affinity bitmask. Nobody
should have to work out that cores 0-3 are `0xF`. Cores the machine does not have are
dropped with a warning, so a config written for a 16-core box still starts on a 4-core
one; if none of the listed cores exist the instance is left on all of them, since pinning
to nothing is worse than pinning to everything.

`priority` takes a name — `idle` through `realtime`. Windows wants priority *classes* and
POSIX wants nice values, where lower means higher priority; the inversion is exactly why
config takes a name rather than a number.

Both are applied after spawning, because there is no portable way to ask `Popen` for them,
and **neither is ever fatal**. Raising priority usually needs elevation, and a server
running at normal priority beats a server that refused to start.

`-cpuCount` is separate from affinity and both are configurable: affinity is which cores
the OS may schedule on, `cpu_count` is how many threads the engine spawns.

The logging flags (`dologs`, `adminlog`, `netlog`, `freezecheck`) are toggleable rather
than hardcoded, all on by default — `-netlog` is the one people turn off on a busy server.

### Server branches and binaries

`server_appid` and `server_branch` choose what gets downloaded: the experimental server is
a different app id, and beta branches go through steamcmd's `-beta`. Builds from different
branches land in separate `serverapp/<buildid>/` directories like any other, so an
instance can be pinned to whichever it needs with `server_build` — stable and experimental
side by side, no reinstalling.

`server_executable` per instance runs a different binary from the same directory, which is
how a diagnostic build works: it ships alongside the normal one rather than as its own
download.

### Stopping means stopping

`dzsm instance stop` **holds the instance down**: the supervisor will not restart it, on
crash or on schedule, until someone starts it again. Starting releases the hold.

Without this a supervisor immediately undoes an explicit stop, so you cannot take a server
down for maintenance — and the workaround people reach for is killing the supervisor,
which loses the crash restarts for everything else too.

The hold lives in `state.json`, so it survives a daemon restart: an instance you took down
on Friday is still down on Monday.

Two things deliberately do *not* hold:

- `--no-hold` on a stop, which is how to bounce an instance under a supervisor.
- The supervisor's own scheduled restart, which is a stop and a start rather than a
  request for the instance to stay down.

### Restart timing

Two independent sources, whichever comes first:

- `restart_schedule`, wall-clock "HH:MM" times.
- `restart_after_hours`, measured from when the instance actually started. **Defaults to
  4.** Set to nothing to disable.

The uptime deadline is recomputed after every restart, so it counts from the new process
rather than drifting from the old one. An instance that is down has no uptime deadline —
there is nothing to measure from.

**Warnings are never allowed to affect the restart.** An unreachable port, a wrong
password, or a mid-send failure is logged and the restart proceeds on schedule — the same
reasoning as Rule 4. A server that restarts silently is a nuisance; a server that fails to
restart because it could not announce itself is an outage.

`dzsm instance say` and `dzsm instance rcon` expose the same connection for manual use,
so RCON can be verified without waiting for a scheduled restart.

### Per-instance missions

**On by default** (`per_instance_missions`). Mission folders hold `types.xml`,
`events.xml`, `cfgspawnabletypes.xml`, `cfgeconomycore.xml`, `init.c` — and the
persistence storage. Instances sharing one clobber each other's economy and saves. The
motivating case: several servers sharing a mod set but running different maps, each with
its own loot tables.

The engine has no mission path argument — it resolves `mpmissions/<template>` relative to
the server directory. So per-instance missions mean per-instance mission *names* inside
that directory: `<mission_prefix>_<instance>.<terrain>` (default `sigma_chernarus.
chernarusplus`), seeded once from a vanilla mission. That costs tens of MB rather than the
several GB an isolated server copy would.

The prefix makes dzsm-managed missions obvious next to vanilla's `dayzOffline.*`. The
instance name stays in it deliberately: a bare prefix would put every instance back on one
shared mission, which is the collision this exists to prevent.

### Missions survive a server update

Because missions live under the server build, a new build would otherwise find no mission
for an instance and re-seed from vanilla — silently reverting a tuned economy and orphaning
the persistence in the old build's directory, while the server comes up looking healthy.

So a mission missing from the current build is **carried forward** from the newest other
build that has it, and only seeded from vanilla when no previous copy exists anywhere.
Persistence comes with it, which is the one case `storage_*` is deliberately copied: it is
the same instance's own saved world, not somebody else's. The old build keeps its copy, so
rolling back still works.

Isolated instances need the same protection by a different route — their server copy is
replaced wholesale on a build change, and that happens *before* mission handling could
help. Missions the incoming build does not ship are set aside and restored afterwards.
Vanilla missions are not, so a map update is never pinned to an old build's files.

"Newest other build" is ordered by build id, not directory mtime: a directory's mtime does
not change when a nested file like `db/types.xml` is edited, so the build actually in use
would be invisible to it.

- `mission` overrides the derived name; `mission_source` overrides what it is seeded from.
- An existing mission directory is **never overwritten** — once seeded it is yours to edit,
  and a restart must not revert a tuned economy.
- `storage_*` is excluded when seeding: that is another server's saved world.
- dzsm keeps the instance's `serverDZ.cfg` `template` pointed at the resolved mission.
  Validating instead of writing would leave a mismatched instance silently running the
  shared mission, which is the collision this exists to prevent.

**`-config` must be an absolute path.** The working directory is the server installation,
so a bare `-config=serverDZ.cfg` resolves to the *server's* config and every instance
silently shares one — hostname, `instanceId`, `verifySignatures`, and the mission template
included. This was a real bug, found only because a per-instance template failed to take
effect.

### Choosing a server build per instance

The server app is versioned like mods, so instances select from it the same way.

- `server_build` pins an instance to a specific build under `serverapp/`. Unset means the
  newest downloaded one. This is what lets production sit on a known-good build while a
  test instance runs a newer one — both from the same cache, no reinstalling.
- Resolution order is instance pin → `server_app_path` → newest downloaded. An instance
  pin beats the global external path deliberately: it is the more specific statement of
  intent.
- The choice is activated at start and recorded in `state.server_builds`, exactly like
  mod versions. A new build never takes effect under a running server.

### Full isolation

`isolated_server = true` gives an instance a private copy of its build at
`instances/<name>/server/`. Costs a few GB per instance and buys what om.py's
per-instance copies gave: its own `keys/`, its own mission files, no shared state with
any other instance.

Without it, instances sharing a build share that build's `keys/`, so a mod trusted on one
is trusted on all. Isolation is the fix when that matters — for example when one instance
runs admin tooling that players on another should not be able to load.

The copy is made when it is missing, when the resolved build changes, or on
`--refresh-server`. A `.dzsm-build` marker records which build it holds, so an unchanged
build never triggers a multi-GB recopy. An externally installed server is always labelled
`external`, so dzsm cannot detect an update underneath it — use `--refresh-server` after
updating one.

Copying only ever happens with the instance stopped, so a private copy is never rewritten
while a server holds locks on it.

### Pins and references refresh on launch

`runner.start` replaces an instance's entire pin map with what it just resolved, so pins
always describe what is actually loaded. A mod dropped from an instance's config stops
being pinned at its next start, and its versions become eligible for cleanup without any
manual bookkeeping. The Rule 4 fallback path filters the previous pins to currently
referenced mods for the same reason.

---

## 9. Local / non-workshop mods

```
localmods/
  @MyTestMod/
```

- steamcmd never touches this directory.
- No version directories — reference the path directly, or snapshot it if you need
  to edit while a server is up.
- Marked `pinned = true` in config so update logic skips it.
- **Per-instance opt-in**, so a test mod loads only on the test instance.

---

## 10. Stack

| Concern | Choice | Why |
|---|---|---|
| CLI | `typer` | Type hints become the CLI |
| Config | `pydantic-settings` + TOML | Validated at load, not at runtime |
| HTTP | `httpx` | Steam Web API |
| Process supervision | `psutil` | Cross-platform process state |
| Web UI (later) | `fastapi` | Reuses the same pydantic models for free |
| Type checking | `mypy --strict` | Enforces Rule 1 |

### Modules

```
dzsm/
  config.py      # pydantic models, TOML load, path-length validation
  steam.py       # steamcmd wrapper + Web API update check + download verification
  mods.py        # version cache, snapshot promotion, GC, refcount
  instance.py    # instance layout, keys, launch arg construction
  runner.py      # process supervision, restart schedule, crash restart
  state.py       # state.json read/write, atomic
  cli.py         # typer commands
  api.py         # fastapi (later)
```

---

## 11. Suggested build order

1. `config.py` + `state.py` — nothing works without these, and they're pure logic
2. `steam.py` — the piece that broke om.py; write the verification layer first
3. `mods.py` — version cache and GC
4. `instance.py` — launch args (blocked on verification test #1 in §5)
5. `runner.py` — supervision
6. `cli.py` — wire it up
7. `api.py` — later

### Suggested first milestone

A CLI that can: log in, check one mod for updates via the Web API, download it if
changed, verify the download actually succeeded, and promote it to a version
directory. No instances, no launching. That single path exercises every hard part of
the Steam integration.

---

## 12. Optional optimization (test before trusting)

Instead of *copying* from steamcmd's dir into the version directory, **hardlink**
each file (same volume only).

- Works if steamcmd delete+recreates files — the hardlink retains the old inode, so
  the old version directory keeps its original content.
- **Breaks if steamcmd writes in place** — the old version directory would be
  silently corrupted.

Ship with `copy` first. Test hardlink behavior against real steamcmd updates before
enabling it. Confidence this works: ~50%.
