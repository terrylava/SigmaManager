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
      mpmissions/
      keys/                                    # .bikey files copied here
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
