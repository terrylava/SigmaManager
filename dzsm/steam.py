"""steamcmd + Steam Web API (spec §6).

This is the module that broke om.py. The verification layer is not optional garnish —
steamcmd exits 0 on failed downloads, so an unverified "success" is how a broken mod
gets promoted into the cache and stays there.
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

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

# Substrings in steamcmd output that mean the run failed regardless of exit code.
STEAMCMD_ERROR_MARKERS = (
    "Timeout downloading item",
    "Rate Limit Exceeded",
    "ERROR! Download item",
    "ERROR! Failed to install app",
    "Disk write failure",
    "No subscription",
    "Invalid Password",
    "FAILED login",
    "Login Failure",
)

# Substrings that mean steamcmd wanted an interactive code. We run with stdin closed,
# so this surfaces as a fast failure instead of a process hanging forever.
STEAM_GUARD_MARKERS = (
    "Steam Guard",
    "Two-factor code",
    "two-factor code",
)

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


def update_server_app(config: Config) -> str:
    """Download/update the DayZ server app (223350) into a version directory.

    Versioned the same way mods are, for the same reason: a running server locks its own
    binaries too.
    """
    raise NotImplementedError("spec §1, §3")


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
    for marker in STEAMCMD_ERROR_MARKERS:
        if marker in output:
            raise SteamError(f"{context} failed: steamcmd reported {marker!r}\n{_tail(output)}")


def _redact(argv: list[str], password: str | None) -> str:
    if not password:
        return " ".join(argv)
    return " ".join("***" if arg == password else arg for arg in argv)


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _tail(output: str, lines: int = 15) -> str:
    """Last few lines of steamcmd output, for error messages (Rule 3)."""
    return "\n".join(output.strip().splitlines()[-lines:])
