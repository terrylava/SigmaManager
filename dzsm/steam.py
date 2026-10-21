"""steamcmd + Steam Web API (spec §6).

This is the module that broke om.py. The verification layer is not optional garnish —
steamcmd exits 0 on failed downloads, so an unverified "success" is how a broken mod
gets promoted into the cache and stays there.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from dzsm.config import WORKSHOP_APPID, Config, SteamConfig

_log = logging.getLogger(__name__)

# Valve's official steamcmd distribution.
STEAMCMD_WINDOWS_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
STEAMCMD_LINUX_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"

# POST, form-encoded: itemcount=N + publishedfileids[0]=<id>
GET_PUBLISHED_FILE_DETAILS = (
    "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
)

# Workshop search. The only Steam endpoint here that needs a Web API key.
QUERY_FILES = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"

# Substrings in steamcmd output that mean the run failed regardless of exit code.
STEAMCMD_ERROR_MARKERS = (
    "Timeout downloading item",
    "Rate Limit Exceeded",
    "ERROR! Download item",
    "ERROR! Failed to install app",
    "Disk write failure",
    "No subscription",
)

# Failures that specifically mean "these credentials will not sign in" — as opposed to the
# markers above, which are about the work rather than the account. Split out so a download
# dying can tell the difference between a transient Workshop problem and a session that
# needs re-seeding: only the latter is worth sending someone to a terminal over.
#
# "Cached credentials not found" is the one that actually happens: change the configured
# account and steamcmd has no sentry for the new name, so it prompts for a password,
# reads EOF because stdin is closed, and reports an invalid one.
STEAM_CREDENTIAL_MARKERS = (
    "Invalid Password",
    "FAILED login",
    "Login Failure",
    "Cached credentials not found",
)

# Substrings that mean steamcmd wanted an interactive code. We run with stdin closed,
# so this surfaces as a fast failure instead of a process hanging forever.
STEAM_GUARD_MARKERS = (
    "Steam Guard",
    "Two-factor code",
    "two-factor code",
)

# What Steam calls the default branch in app metadata. `server_branch = None` means this
# one; the VDF never calls it "stable" or "default".
PUBLIC_BRANCH = "public"

# Steam Web API per-item result code for success.
_RESULT_OK = 1

# steamcmd returns 7 when it has replaced its own binary and restarted — which every
# fresh install does on its first run. The session itself usually completed fine, so 7 is
# not evidence of failure on its own and must not be reported as one.
STEAMCMD_SELF_UPDATE_EXIT = 7


class SteamError(Exception):
    """A steamcmd run or Web API call failed."""


class SteamGuardRequired(SteamError):
    """steamcmd is waiting on a Steam Guard code.

    Raised instead of hanging on stdin forever. The fix is a one-time interactive login
    to seed the sentry file; the message says so.
    """


class SteamLoginFailed(SteamError):
    """steamcmd could not sign in with the credentials it has.

    A sibling of SteamGuardRequired rather than the same thing: both mean "re-seed the
    session in a terminal", but this one is what you get after changing the configured
    account, where there is no sentry for the new name at all. Distinct from a plain
    SteamError so a caller can tell a credential problem from a failed download and only
    send people to a terminal for the former.
    """


@dataclass(frozen=True)
class SteamCmdRun:
    """Raw result of one steamcmd invocation. The exit code is recorded, not trusted."""

    returncode: int
    output: str


@dataclass(frozen=True)
class DownloadResult:
    """Everything verify_download() needs to decide whether a download really happened.

    The .acf mtimes are captured around the run because steamcmd's manifest advancing is
    one of the few signals that is actually correlated with work having been done.
    """

    workshop_id: int
    path: Path
    returncode: int
    output: str
    acf_mtime_before: float | None
    acf_mtime_after: float | None


def check_updates(
    workshop_ids: list[int],
    *,
    timeout: float = 30.0,
    client: httpx.Client | None = None,
) -> dict[int, int]:
    """Ask the Steam Web API for each mod's time_updated.

    Maps workshop_id -> time_updated. This runs before any download and is what turns a
    20-minute blind update cycle into ~30 seconds: only ids whose time_updated moved
    past state.last_seen_remote need downloading.

    time_updated doubles as the version directory name — it is the canonical version
    identity, so this function's output is what names things downstream.

    Ids the API reports as failed are logged and omitted from the result rather than
    aborting the batch; a caller that needs one specific id must check for its absence.
    Returns an empty dict for an empty request. Raises SteamError on transport or
    protocol failure — never returns False or None (Rule 1).
    """
    if not workshop_ids:
        return {}

    form = {"itemcount": str(len(workshop_ids))}
    for index, workshop_id in enumerate(workshop_ids):
        form[f"publishedfileids[{index}]"] = str(workshop_id)

    owns_client = client is None
    http = client if client is not None else httpx.Client(timeout=timeout)
    try:
        response = http.post(GET_PUBLISHED_FILE_DETAILS, data=form)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise SteamError(f"Steam Web API request failed: {exc}") from exc
    except ValueError as exc:
        raise SteamError(f"Steam Web API returned non-JSON: {exc}") from exc
    finally:
        if owns_client:
            http.close()

    details = payload.get("response", {}).get("publishedfiledetails")
    if not isinstance(details, list):
        raise SteamError(
            f"Steam Web API response missing publishedfiledetails; got keys "
            f"{sorted(payload.get('response', {}))}"
        )

    updates: dict[int, int] = {}
    for entry in details:
        raw_id = entry.get("publishedfileid")
        if raw_id is None:
            _log.warning("steam: API returned an entry with no publishedfileid: %r", entry)
            continue
        workshop_id = int(raw_id)
        if entry.get("result") != _RESULT_OK:
            _log.warning(
                "steam: mod %s lookup failed (result=%s) — omitted from update check",
                workshop_id,
                entry.get("result"),
            )
            continue
        time_updated = entry.get("time_updated")
        if time_updated is None:
            _log.warning("steam: mod %s has no time_updated — omitted", workshop_id)
            continue
        updates[workshop_id] = int(time_updated)

    return updates


@dataclass(frozen=True)
class WorkshopItem:
    """One Workshop entry, as much of it as the browser needs."""

    workshop_id: int
    title: str
    description: str
    preview_url: str | None
    time_updated: int | None
    subscriptions: int | None
    file_size: int | None


# Steam ranks results by whichever EPublishedFileQueryType it is handed, and most of
# them ignore how well the text matched. Named rather than inlined so the next person
# changing it has to say which ranking they mean.
TEXT_SEARCH_RANKING = 12


def search_workshop(
    api_key: str | None,
    text: str,
    *,
    page: int = 1,
    per_page: int = 20,
    timeout: float = 30.0,
    client: httpx.Client | None = None,
) -> tuple[list[WorkshopItem], int]:
    """Search the DayZ Workshop. Returns (items, total matches).

    This is the one Steam endpoint dzsm uses that needs a Web API key — looking up mods
    whose ids are already known does not, which is why everything else works without one.

    Steam's page is not embeddable (it sends frame-ancestors headers, and a cross-origin
    frame could not be read from anyway), so the browser is built over this instead.

    Returns an empty list for an empty query rather than fetching the whole Workshop.
    """
    if not text.strip():
        return [], 0
    if not api_key:
        raise SteamError(
            "searching the Workshop needs a Steam Web API key. Get one from "
            "https://steamcommunity.com/dev/apikey and set it in the environment "
            "variable named by steam.api_key_env."
        )

    params = {
        "key": api_key,
        "appid": str(WORKSHOP_APPID),
        "search_text": text,
        # EPublishedFileQueryType. 12 is RankedByTextSearch: the search text decides the
        # ORDER, not just which items match. 9, which was here first, is
        # RankedByTotalUniqueSubscriptions -- the text only filters, and the most
        # subscribed survivor wins. That put a base-building mod at the top of a search
        # for "zombie" and pushed an exact title match to third for "hercul". Measured
        # against live Steam, not read off the enum.
        "query_type": str(TEXT_SEARCH_RANKING),
        "page": str(max(1, page)),
        "numperpage": str(per_page),
        "filetype": "0",
        "return_short_description": "true",
        "return_previews": "true",
        "return_metadata": "true",
        "strip_description_bbcode": "true",
    }

    owns_client = client is None
    http = client if client is not None else httpx.Client(timeout=timeout)
    try:
        response = http.get(QUERY_FILES, params=params)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            raise SteamError(
                "Steam rejected the Web API key. Check the value in the environment "
                "variable named by steam.api_key_env."
            ) from exc
        raise SteamError(f"Workshop search failed: {exc}") from exc
    except httpx.HTTPError as exc:
        raise SteamError(f"Workshop search failed: {exc}") from exc
    except ValueError as exc:
        raise SteamError(f"Workshop search returned non-JSON: {exc}") from exc
    finally:
        if owns_client:
            http.close()

    body = payload.get("response", {})
    return [_item(entry) for entry in body.get("publishedfiledetails") or []], int(
        body.get("total", 0)
    )


def get_workshop_item(
    workshop_id: int,
    *,
    timeout: float = 30.0,
    client: httpx.Client | None = None,
) -> WorkshopItem | None:
    """One Workshop item by id. No API key needed — this is the public endpoint.

    So pasting a Workshop link works even without a key, which is the fallback when
    someone does not want to create one.
    """
    owns_client = client is None
    http = client if client is not None else httpx.Client(timeout=timeout)
    try:
        response = http.post(
            GET_PUBLISHED_FILE_DETAILS,
            data={"itemcount": "1", "publishedfileids[0]": str(workshop_id)},
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise SteamError(f"Workshop lookup failed: {exc}") from exc
    except ValueError as exc:
        raise SteamError(f"Workshop lookup returned non-JSON: {exc}") from exc
    finally:
        if owns_client:
            http.close()

    details = payload.get("response", {}).get("publishedfiledetails") or []
    if not details or details[0].get("result") != _RESULT_OK:
        return None
    return _item(details[0])


def _item(entry: dict[str, Any]) -> WorkshopItem:
    def number(key: str) -> int | None:
        value = entry.get(key)
        return int(value) if isinstance(value, (int, str)) and str(value).isdigit() else None

    return WorkshopItem(
        workshop_id=int(entry["publishedfileid"]),
        title=str(entry.get("title") or f"({entry['publishedfileid']})"),
        description=str(entry.get("short_description") or entry.get("description") or ""),
        preview_url=entry.get("preview_url"),
        time_updated=number("time_updated"),
        subscriptions=number("subscriptions") or number("lifetime_subscriptions"),
        file_size=number("file_size"),
    )


def ensure_steamcmd(
    steam: SteamConfig,
    *,
    timeout: float = 120.0,
    client: httpx.Client | None = None,
) -> Path:
    """Return the steamcmd path, downloading Valve's distribution if it isn't there.

    Idempotent: a no-op once the binary exists. Extraction is guarded against archive
    members that escape the target directory — this writes an executable to disk, so the
    archive is treated as untrusted input even though it comes from Valve over HTTPS.

    Raises SteamError if auto_install is disabled or the download does not produce a
    usable binary. Never returns a path that does not exist (Rule 1).
    """
    if steam.steamcmd_path.is_file():
        return steam.steamcmd_path

    if not steam.auto_install:
        raise SteamError(
            f"steamcmd not found at {steam.steamcmd_path} and auto_install is disabled. "
            f"Install it manually there, or set auto_install = true under [steam]."
        )

    target_dir = steam.steamcmd_path.parent
    url = STEAMCMD_WINDOWS_URL if sys.platform == "win32" else STEAMCMD_LINUX_URL
    _log.info("steam: steamcmd not found, downloading from %s", url)
    target_dir.mkdir(parents=True, exist_ok=True)

    owns_client = client is None
    http = client if client is not None else httpx.Client(timeout=timeout, follow_redirects=True)
    archive_suffix = ".zip" if url.endswith(".zip") else ".tar.gz"
    fd, archive_name = tempfile.mkstemp(dir=target_dir, prefix=".steamcmd-", suffix=archive_suffix)
    os.close(fd)
    archive = Path(archive_name)
    try:
        response = http.get(url)
        response.raise_for_status()
        archive.write_bytes(response.content)
    except httpx.HTTPError as exc:
        archive.unlink(missing_ok=True)
        raise SteamError(f"could not download steamcmd from {url}: {exc}") from exc
    finally:
        if owns_client:
            http.close()

    try:
        if archive_suffix == ".zip":
            _extract_zip(archive, target_dir)
        else:
            _extract_tar(archive, target_dir)
    except (OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
        raise SteamError(f"could not extract steamcmd archive {archive}: {exc}") from exc
    finally:
        archive.unlink(missing_ok=True)

    if not steam.steamcmd_path.is_file():
        raise SteamError(
            f"steamcmd archive from {url} did not contain {steam.steamcmd_path.name}; "
            f"extracted into {target_dir} but the binary is missing"
        )

    if sys.platform != "win32":
        mode = steam.steamcmd_path.stat().st_mode
        steam.steamcmd_path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP)

    _log.info("steam: installed steamcmd at %s", steam.steamcmd_path)
    return steam.steamcmd_path


def login(steam: SteamConfig, *, timeout: float = 180.0) -> None:
    """Verify steamcmd can log in non-interactively.

    Anonymous login does not work for DayZ — both 223350 and workshop downloads need an
    account that owns the game. Raises SteamGuardRequired if steamcmd prompts, with the
    interactive-seeding instructions in the message.
    """
    run = _run_steamcmd(steam, [*_login_args(steam), "+quit"], timeout=timeout)
    _raise_for_output(run.output, context=f"login as {steam.username}")
    if run.returncode not in (0, STEAMCMD_SELF_UPDATE_EXIT):
        raise SteamError(
            f"login as {steam.username} failed: steamcmd exited {run.returncode}\n"
            f"{_tail(run.output)}"
        )
    _log.info("steam: login as %s succeeded", steam.username)


def login_interactive(steam: SteamConfig, *, timeout: float | None = None) -> None:
    """Run steamcmd attached to the terminal so the user can complete Steam Guard.

    This is the one place dzsm deliberately does NOT close stdin. It exists to seed the
    sentry file once; every other steamcmd call in this module runs non-interactively
    and fails fast rather than blocking on a prompt.

    dzsm passes only the username. The password and the Guard code are typed by the user
    straight into steamcmd — they are never read, logged, or stored here. steamcmd caches
    a sentry afterward, which is what makes subsequent runs non-interactive.
    """
    ensure_steamcmd(steam)
    argv = [str(steam.steamcmd_path), "+login", steam.username, "+quit"]
    _log.info("steam: starting interactive login for %s", steam.username)
    try:
        # No capture_output and no stdin redirect: steamcmd inherits this terminal.
        proc = subprocess.run(argv, check=False, timeout=timeout)  # noqa: S603
    except subprocess.TimeoutExpired as exc:
        raise SteamError("interactive login timed out") from exc
    except OSError as exc:
        raise SteamError(f"could not run steamcmd at {steam.steamcmd_path}: {exc}") from exc

    if proc.returncode == STEAMCMD_SELF_UPDATE_EXIT:
        # A fresh steamcmd replaces its own binary on first run and exits 7 even when the
        # login completed and the sentry was written. Nothing was captured from this run
        # to inspect, so prove the outcome instead of guessing: a non-interactive login
        # can only succeed from cached credentials, which is exactly what we wanted.
        _log.info("steam: steamcmd updated itself (exit 7); verifying the session took")
        login(steam)
    elif proc.returncode != 0:
        raise SteamError(
            f"interactive login as {steam.username} failed: steamcmd exited "
            f"{proc.returncode}. Check the output above."
        )
    _log.info("steam: interactive login as %s succeeded; sentry cached", steam.username)


def download_mod(config: Config, workshop_id: int) -> DownloadResult:
    """Run steamcmd workshop_download_item for one mod.

    The return value is NOT safe to use until verify_download() has passed on it —
    steamcmd exits 0 on failure, so this function deliberately does not raise on a bad
    run. It reports; verification judges.
    """
    steam = config.steam
    acf_before = _mtime(config.workshop_acf)

    _log.info("steam: downloading mod %s", workshop_id)
    run = _run_steamcmd(
        steam,
        [
            *_login_args(steam),
            "+workshop_download_item",
            str(WORKSHOP_APPID),
            str(workshop_id),
            "+quit",
        ],
        timeout=float(config.download_timeout_seconds),
    )

    return DownloadResult(
        workshop_id=workshop_id,
        path=config.workshop_download_dir / str(workshop_id),
        returncode=run.returncode,
        output=run.output,
        acf_mtime_before=acf_before,
        acf_mtime_after=_mtime(config.workshop_acf),
    )


def verify_download(config: Config, result: DownloadResult) -> None:
    """Prove a steamcmd download actually succeeded. Raise SteamError if not.

    steamcmd's exit code is worthless as a success signal, so every one of these must
    hold before mods.promote_snapshot() may run:

    1. output contains none of STEAMCMD_ERROR_MARKERS (and no Steam Guard prompt)
    2. steamapps/workshop/content/221100/<id> exists
    3. it is non-empty
    4. the .acf manifest timestamp advanced

    A non-zero exit code is treated as failure too — it is informative when set, just
    not when clear.
    """
    workshop_id = result.workshop_id
    _raise_for_output(result.output, context=f"download of mod {workshop_id}")

    # Exit 7 (self-update restart) is not a failure. The four checks below are independent
    # evidence of whether the download actually happened, so accepting 7 costs nothing.
    if result.returncode not in (0, STEAMCMD_SELF_UPDATE_EXIT):
        raise SteamError(
            f"download of mod {workshop_id} failed: steamcmd exited {result.returncode}\n"
            f"{_tail(result.output)}"
        )

    if not result.path.is_dir():
        raise SteamError(
            f"download of mod {workshop_id} failed: steamcmd reported success but "
            f"{result.path} does not exist\n{_tail(result.output)}"
        )

    if not any(p.is_file() for p in result.path.rglob("*")):
        raise SteamError(
            f"download of mod {workshop_id} failed: {result.path} contains no files\n"
            f"{_tail(result.output)}"
        )

    if result.acf_mtime_after is None:
        raise SteamError(
            f"download of mod {workshop_id} failed: workshop manifest "
            f"{config.workshop_acf} does not exist after the run"
        )

    if result.acf_mtime_before is not None and result.acf_mtime_after <= result.acf_mtime_before:
        raise SteamError(
            f"download of mod {workshop_id} failed: workshop manifest "
            f"{config.workshop_acf} did not advance (still "
            f"{result.acf_mtime_after}), so steamcmd did no work despite exiting 0"
        )

    _log.info("steam: mod %s download verified at %s", workshop_id, result.path)


def update_server_app(config: Config, *, validate: bool = False) -> str:
    """Download/update the DayZ server app (223350) into a version directory.

    Versioned the same way mods are, for the same reason: a running server locks its own
    binaries too, so an in-place update while any instance is up would fail or corrupt.
    The version identity is Steam's `buildid` from the app manifest — the server app's
    equivalent of a workshop mod's `time_updated`.

    steamcmd downloads into a fixed working directory (tier 1, mutable) and the result is
    promoted into serverapp/<buildid>/ (tier 2, immutable) only after verification.

    `validate` makes steamcmd re-check every file — slow, but the fix when an install is
    suspected corrupt.

    Returns the buildid. Raises SteamError on any failure; never returns a version that
    was not actually installed (Rule 1).
    """
    appid = config.server_appid
    download_dir = config.serverapp_download_dir
    download_dir.mkdir(parents=True, exist_ok=True)
    manifest = download_dir / "steamapps" / f"appmanifest_{appid}.acf"
    build_before = _read_buildid(manifest)

    branch = config.server_branch
    _log.info(
        "steam: updating server app %s (%s branch) into %s",
        appid,
        branch or "public",
        download_dir,
    )
    args = [
        # force_install_dir must precede app_update, and steamcmd is happier with it
        # before login too.
        "+force_install_dir",
        str(download_dir),
        *_login_args(config.steam),
        "+app_update",
        str(appid),
    ]
    if branch is not None:
        # -beta must follow the appid, not precede it.
        args += ["-beta", branch]
        password = os.environ.get(config.server_branch_password_env)
        if password:
            args += ["-betapassword", password]
    if validate:
        args.append("validate")
    args.append("+quit")

    run = _run_steamcmd(config.steam, args, timeout=float(config.download_timeout_seconds))
    _raise_for_output(run.output, context=f"server app {appid} update")
    if run.returncode not in (0, STEAMCMD_SELF_UPDATE_EXIT):
        raise SteamError(
            f"server app update failed: steamcmd exited {run.returncode}\n"
            f"{_tail(run.output)}"
        )

    build_id = _read_buildid(manifest)
    if build_id is None:
        raise SteamError(
            f"server app update failed: no buildid in {manifest} after the run. "
            f"steamcmd reported success but produced no usable manifest.\n"
            f"{_tail(run.output)}"
        )

    executable = download_dir / (
        "DayZServer_x64.exe" if sys.platform == "win32" else "DayZServer"
    )
    if not executable.is_file():
        raise SteamError(
            f"server app update failed: {executable} is missing after the run\n"
            f"{_tail(run.output)}"
        )

    if build_before is not None and build_before == build_id:
        _log.info("steam: server app already at build %s", build_id)

    return build_id


def remote_server_build(config: Config, *, timeout: float = 120.0) -> str:
    """The build id Steam currently publishes for the server app, without downloading it.

    There is no Web API equivalent of GetPublishedFileDetails for an app's branch build,
    so this goes through steamcmd's `app_info_print`, which prints the app's metadata and
    transfers no content. It takes seconds rather than the better part of an hour, which
    is what makes it usable on a timer.

    Needs a working session like any other steamcmd call, so a stale sentry surfaces here
    as SteamLoginFailed and feeds the same session status everything else does.

    Returns the build id. Raises SteamError when it cannot be determined — a guess here
    would either hide a game update or invent one (Rule 1, Rule 3).
    """
    appid = config.server_appid
    branch = config.server_branch or PUBLIC_BRANCH
    run = _run_steamcmd(
        config.steam,
        [
            *_login_args(config.steam),
            # app_info_update 1 forces a refresh; without it steamcmd happily prints a
            # cached copy from before the update being looked for.
            "+app_info_update",
            "1",
            "+app_info_print",
            str(appid),
            "+quit",
        ],
        timeout=timeout,
    )
    _raise_for_output(run.output, context=f"server app {appid} info")

    build_id = _branch_buildid(run.output, branch)
    if build_id is None:
        raise SteamError(
            f"could not read the {branch!r} branch build id for app {appid} from "
            f"steamcmd's app info. The branch may not exist, or the output format may "
            f"have changed.\n{_tail(run.output)}"
        )
    return build_id


def _branch_buildid(output: str, branch: str) -> str | None:
    """Pull one branch's buildid out of `app_info_print` output.

    Walks to the branches section and then to the named branch, rather than regexing the
    whole output for a buildid: the app has one at its top level and every depot carries
    its own, so a blind search would return something plausible and wrong.
    """
    start = output.find('"branches"')
    if start == -1:
        return None
    section = output[start:]

    opening = re.search(rf'"{re.escape(branch)}"\s*\{{', section)
    if opening is None:
        return None
    # Bounded to this branch's own block, so a branch without a buildid returns None
    # instead of picking up the next branch's.
    closing = section.find("}", opening.end())
    block = section[opening.end() : closing if closing != -1 else len(section)]

    match = re.search(r'"buildid"\s+"(\d+)"', block)
    return match.group(1) if match else None


def _read_buildid(manifest: Path) -> str | None:
    """Pull buildid out of steamcmd's app manifest (a VDF file).

    Returns None when the manifest is absent or unparseable — callers treat that as a
    failed install rather than guessing a version.
    """
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'"buildid"\s+"(\d+)"', text)
    return match.group(1) if match else None


# --- internals ---------------------------------------------------------------


def _is_contained(destination: Path, member_path: Path) -> bool:
    """True if member_path stays inside destination once resolved."""
    try:
        member_path.resolve().relative_to(destination.resolve())
    except ValueError:
        return False
    return True


def _extract_zip(archive: Path, destination: Path) -> None:
    """Extract a zip, refusing members that escape the destination.

    Python's ZipFile.extract() sanitizes absolute paths and drive letters but the guard
    is cheap and this writes an executable, so members are checked explicitly.
    """
    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            if not _is_contained(destination, destination / member):
                raise SteamError(
                    f"refusing to extract {archive}: member {member!r} escapes {destination}"
                )
        zf.extractall(destination)  # noqa: S202 — members validated above


def _extract_tar(archive: Path, destination: Path) -> None:
    """Extract a tarball with the same containment guard, plus the data filter."""
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf.getmembers():
            if not _is_contained(destination, destination / member.name):
                raise SteamError(
                    f"refusing to extract {archive}: member {member.name!r} escapes "
                    f"{destination}"
                )
        # filter="data" additionally strips links, device nodes, and setuid bits.
        tf.extractall(destination, filter="data")  # noqa: S202 — members validated above


def _login_args(steam: SteamConfig) -> list[str]:
    """Build the +login arguments.

    The password is optional on purpose: once `dzsm login --interactive` has seeded the
    sentry, steamcmd can authenticate from its own cached credentials and dzsm never
    needs to handle the password at all. When the env var is absent we omit it and let
    steamcmd try the cache — if that fails, the login-failure markers produce a clear
    error rather than a hang, because stdin is closed.
    """
    password = steam.password()
    if not password:
        _log.debug(
            "steam: %s is unset; relying on steamcmd's cached credentials",
            steam.password_env,
        )
        return ["+login", steam.username]
    return ["+login", steam.username, password]


def _run_steamcmd(steam: SteamConfig, args: list[str], *, timeout: float) -> SteamCmdRun:
    """Invoke steamcmd with stdin closed so an interactive prompt fails fast.

    argv list, never a shell string (spec §4). stdout and stderr are merged because
    steamcmd is inconsistent about which one carries its errors.
    """
    ensure_steamcmd(steam)
    argv = [str(steam.steamcmd_path), *args]
    _log.debug("steam: exec %s", _redact(argv, steam.password()))
    try:
        proc = subprocess.run(  # noqa: S603 — argv list, no shell, path from config
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SteamError(f"steamcmd timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise SteamError(f"could not run steamcmd at {steam.steamcmd_path}: {exc}") from exc

    return SteamCmdRun(returncode=proc.returncode, output=(proc.stdout or "") + (proc.stderr or ""))


def _raise_for_output(output: str, *, context: str) -> None:
    """Scan steamcmd output for known failure signatures."""
    for marker in STEAM_GUARD_MARKERS:
        if marker in output:
            raise SteamGuardRequired(
                f"{context} failed: steamcmd asked for a Steam Guard code, and dzsm runs "
                f"it with stdin closed so it cannot hang waiting for one. Seed the "
                f"sentry once by hand:\n"
                f"    steamcmd +login <username> +quit\n"
                f"enter the code when prompted, then re-run. Subsequent runs are "
                f"non-interactive.\n{_tail(output)}"
            )
    for marker in STEAM_CREDENTIAL_MARKERS:
        if marker in output:
            raise SteamLoginFailed(
                f"{context} failed: steamcmd reported {marker!r}. The configured account "
                f"has no usable cached session — most often because the account was "
                f"changed and steamcmd has no sentry for the new name. Seed it once:\n"
                f"    dzsm login --interactive\n{_tail(output)}"
            )
    for marker in STEAMCMD_ERROR_MARKERS:
        if marker in output:
            raise SteamError(f"{context} failed: steamcmd reported {marker!r}\n{_tail(output)}")


# steamcmd flags whose *next* argument is a secret. Redacting by position as well as by
# value catches secrets this function was never handed: -betapassword is built in
# update_server_app from its own env var, so matching against the account password alone
# left the branch password in the debug log verbatim.
SECRET_FLAGS = frozenset({"-betapassword"})


def _redact(argv: list[str], password: str | None) -> str:
    """Render a steamcmd command line with credentials removed."""
    out: list[str] = []
    secret_next = False
    for arg in argv:
        if secret_next:
            out.append("***")
            secret_next = False
            continue
        secret_next = arg in SECRET_FLAGS
        out.append("***" if password and arg == password else arg)
    return " ".join(out)


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _tail(output: str, lines: int = 15) -> str:
    """Last few lines of steamcmd output, for error messages (Rule 3)."""
    return "\n".join(output.strip().splitlines()[-lines:])
