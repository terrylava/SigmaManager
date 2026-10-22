"""Finding, fetching and swapping in a newer dzsm.

Updating the manager is not like updating a mod or the server app. There is no steamcmd,
the artefact is the program doing the asking, and on Windows a running .exe cannot be
overwritten — it can be *renamed*, which is the whole trick this module turns on.

Checking is automatic and costs one HTTP request. Downloading and applying are not: the
binary supervising someone's servers does not replace itself because a clock ticked.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

_log = logging.getLogger(__name__)

# Where releases come from. A fork changes this in config rather than editing the
# source, so an update never silently points at somebody else's binary.
DEFAULT_REPO = "terrylava/SigmaManager"
RELEASES_API = "https://api.github.com/repos/{repo}/releases/latest"

# The asset to look for. One name, not a pattern: a release carrying several files
# should not leave the updater guessing which one is the program.
ASSET_NAME = "dzsm.exe"

# What a replaced binary is renamed to before the new one takes its place.
RETIRED_SUFFIX = ".old"

_VERSION_PART = re.compile(r"\d+")


class SelfUpdateError(Exception):
    """A check, download or swap failed. Nothing has been replaced."""


@dataclass(frozen=True)
class Release:
    """A published release, as far as updating cares about it."""

    tag: str
    version: tuple[int, ...]
    url: str
    """Where the asset is downloaded from."""
    size: int
    digest: str | None
    """sha256 as GitHub reports it, or None for a release published before they did."""
    notes_url: str

    @property
    def name(self) -> str:
        return self.tag


def parse_version(text: str) -> tuple[int, ...]:
    """Turn "v0.1.1" or "0.1.1" into (0, 1, 1).

    Only the numbers, so "v0.2.0-rc1" sorts as (0, 2, 0, 1) — close enough to order
    releases, and this never decides anything on its own: it only answers "is the
    published one higher than mine", and a person runs the update.
    """
    return tuple(int(part) for part in _VERSION_PART.findall(text))


def is_newer(published: str, running: str) -> bool:
    """Whether `published` is a later version than `running`.

    An unparseable tag is not newer. The alternative — treating anything unrecognised as
    an update — would have the dashboard crying wolf over a tag named "latest".
    """
    theirs, mine = parse_version(published), parse_version(running)
    if not theirs or not mine:
        return False
    return theirs > mine


def latest_release(
    *,
    repo: str = DEFAULT_REPO,
    timeout: float = 15.0,
    client: httpx.Client | None = None,
) -> Release | None:
    """The newest published release, or None when the repository has none.

    Unauthenticated: the repository is public, and asking people to put a GitHub token
    in their config to learn that a version exists is a poor trade.
    """
    owns_client = client is None
    http = client if client is not None else httpx.Client(timeout=timeout)
    try:
        response = http.get(
            RELEASES_API.format(repo=repo),
            headers={"Accept": "application/vnd.github+json"},
        )
        if response.status_code == 404:
            return None
        if response.status_code == 403:
            # Unauthenticated calls are rate limited per IP. Not a failure worth
            # shouting about on a housekeeping tick.
            raise SelfUpdateError(
                "GitHub rate-limited the update check. It will be tried again later."
            )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise SelfUpdateError(f"could not reach GitHub: {exc}") from exc
    except ValueError as exc:
        raise SelfUpdateError(f"GitHub returned something that is not JSON: {exc}") from exc
    finally:
        if owns_client:
            http.close()

    tag = str(payload.get("tag_name") or "")
    if not tag:
        return None
    asset = next(
        (item for item in payload.get("assets") or [] if item.get("name") == ASSET_NAME),
        None,
    )
    if asset is None:
        # A release with notes but no binary. Nothing to offer.
        return None

    digest = str(asset.get("digest") or "")
    return Release(
        tag=tag,
        version=parse_version(tag),
        url=str(asset.get("browser_download_url") or asset.get("url") or ""),
        size=int(asset.get("size") or 0),
        digest=digest.removeprefix("sha256:") or None,
        notes_url=str(payload.get("html_url") or ""),
    )


def download(
    release: Release,
    directory: Path,
    *,
    timeout: float = 600.0,
    client: httpx.Client | None = None,
) -> Path:
    """Fetch the release asset and prove it is what GitHub said it was.

    Verified before it is ever a candidate to run: size, then sha256 against the digest
    in the release. The lesson from steamcmd is that a download which *looks* finished
    is not evidence, and here the file would go on to replace the program itself.

    A release published before GitHub recorded digests has none; the size is still
    checked and the absence is logged rather than quietly treated as a pass.
    """
    if not release.url:
        raise SelfUpdateError(f"release {release.tag} has no downloadable {ASSET_NAME}")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{ASSET_NAME}.{release.tag}"

    owns_client = client is None
    http = client if client is not None else httpx.Client(timeout=timeout, follow_redirects=True)
    digest = hashlib.sha256()
    written = 0
    try:
        with http.stream("GET", release.url) as response:
            response.raise_for_status()
            with target.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=1 << 20):
                    handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
    except httpx.HTTPError as exc:
        target.unlink(missing_ok=True)
        raise SelfUpdateError(f"downloading {release.tag} failed: {exc}") from exc
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise SelfUpdateError(f"cannot write {target}: {exc}") from exc
    finally:
        if owns_client:
            http.close()

    if release.size and written != release.size:
        target.unlink(missing_ok=True)
        raise SelfUpdateError(
            f"{release.tag} downloaded {written} bytes, expected {release.size}"
        )
    if release.digest is None:
        _log.warning(
            "release %s publishes no checksum; verified its size only (%d bytes)",
            release.tag,
            written,
        )
    elif digest.hexdigest() != release.digest:
        target.unlink(missing_ok=True)
        raise SelfUpdateError(
            f"{release.tag} does not match its published checksum — refusing it. "
            f"Expected {release.digest}, got {digest.hexdigest()}"
        )
    return target


def apply(new_binary: Path, *, current: Path | None = None) -> Path:
    """Put `new_binary` in place of the running program. Returns the retired copy.

    Windows will not let a running .exe be overwritten, but it will let one be renamed:
    the loaded image is held by the file's identity, not its name. So the running binary
    steps aside and the new one takes its place, which leaves the process running
    happily on the old image until it is restarted.

    Deliberately not restarting anything. This supervises game servers, and choosing the
    moment it goes down belongs to the person who knows what is happening on them.
    """
    target = current if current is not None else running_binary()
    if target is None:
        raise SelfUpdateError(
            "running from source, so there is no binary to replace. Build one with "
            "`pyinstaller dzsm.spec`."
        )
    if not new_binary.is_file():
        raise SelfUpdateError(f"{new_binary} is not there")

    retired = target.with_name(target.name + RETIRED_SUFFIX)
    # A previous update's copy, still around because the process that made it had it
    # open. Nothing holds it now.
    if retired.exists():
        try:
            retired.unlink()
        except OSError as exc:
            _log.warning("could not remove the previous %s: %s", retired, exc)
    try:
        target.rename(retired)
    except OSError as exc:
        raise SelfUpdateError(
            f"cannot move {target} aside: {exc}. Another dzsm may be running from it."
        ) from exc
    try:
        shutil.move(str(new_binary), str(target))
    except OSError as exc:
        # Put it back rather than leaving the machine with no dzsm at all.
        with_suppressed_error(retired, target)
        raise SelfUpdateError(f"cannot move {new_binary} into place: {exc}") from exc
    return retired


def with_suppressed_error(retired: Path, target: Path) -> None:
    """Restore the retired binary, and say so if even that fails."""
    try:
        retired.rename(target)
    except OSError as exc:  # pragma: no cover - only on a filesystem already misbehaving
        _log.error(
            "left %s in place of %s and could not restore it: %s", retired, target, exc
        )


def running_binary() -> Path | None:
    """The .exe this process is, or None when running from a source checkout."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable)


def staging_dir() -> Path:
    """Where a download is written: beside the binary, falling back to TEMP.

    Beside it because the move into place is then a rename on one volume rather than a
    copy across two, and a half-copied 20 MB binary is not something to risk when the
    destination is the program itself.
    """
    binary = running_binary()
    if binary is not None and os.access(binary.parent, os.W_OK):
        return binary.parent
    return Path(tempfile.gettempdir())
