"""Finding, verifying and swapping in a newer dzsm.

Updating the manager is unlike updating a mod: there is no steamcmd, and the artefact is
the program doing the asking. The parts that could go wrong badly — accepting a file
that is not what GitHub said it was, or leaving a machine with no dzsm at all — are what
is tested here.
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import httpx
import pytest

from dzsm.selfupdate import (
    Release,
    SelfUpdateError,
    apply,
    download,
    is_newer,
    latest_release,
    parse_version,
)

PAYLOAD = {
    "tag_name": "v0.2.0",
    "html_url": "https://github.com/terrylava/SigmaManager/releases/tag/v0.2.0",
    "assets": [
        {
            "name": "dzsm.exe",
            "browser_download_url": "https://example.invalid/dzsm.exe",
            "size": 7,
            "digest": "sha256:" + hashlib.sha256(b"payload").hexdigest(),
        }
    ],
}


def _client(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def _json(payload: object, status: int = 200) -> httpx.Client:
    return _client(lambda request: httpx.Response(status, json=payload))


# --- comparing versions ------------------------------------------------------


@pytest.mark.parametrize(
    ("published", "running", "expected"),
    [
        ("v0.2.0", "0.1.1", True),
        ("v0.1.1", "0.1.1", False),
        ("v0.1.0", "0.1.1", False),
        ("v0.1.10", "0.1.9", True),
        ("v1.0.0", "0.9.9", True),
    ],
)
def test_version_comparison(published: str, running: str, expected: bool) -> None:
    assert is_newer(published, running) is expected


def test_a_tag_with_no_numbers_is_not_an_update() -> None:
    """A tag named "latest" or "nightly" would otherwise have the dashboard crying wolf
    on every check, forever."""
    assert is_newer("latest", "0.1.1") is False


def test_the_v_prefix_is_optional() -> None:
    assert parse_version("v0.1.1") == parse_version("0.1.1") == (0, 1, 1)


# --- finding a release -------------------------------------------------------


def test_a_release_is_read() -> None:
    release = latest_release(client=_json(PAYLOAD))

    assert release is not None
    assert release.tag == "v0.2.0"
    assert release.size == 7
    assert release.digest == hashlib.sha256(b"payload").hexdigest()


def test_a_repository_with_no_releases_is_not_an_error() -> None:
    """A fork that has not published yet is a normal state, not a failure."""
    assert latest_release(client=_json({"message": "Not Found"}, status=404)) is None


def test_a_release_without_the_binary_offers_nothing() -> None:
    """Notes with no asset: there is nothing to update to."""
    payload = {**PAYLOAD, "assets": [{"name": "notes.txt", "size": 1}]}

    assert latest_release(client=_json(payload)) is None


def test_rate_limiting_says_so() -> None:
    """Unauthenticated calls are limited per IP, and the message has to make clear this
    is temporary rather than something to go and fix."""
    with pytest.raises(SelfUpdateError, match="rate-limited"):
        latest_release(client=_json({"message": "rate limit"}, status=403))


def test_an_unreachable_github_is_an_error_not_a_silence() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(SelfUpdateError, match="could not reach GitHub"):
        latest_release(client=_client(refuse))


# --- downloading -------------------------------------------------------------


def _release(**overrides: object) -> Release:
    base = Release(
        tag="v0.2.0",
        version=(0, 2, 0),
        url="https://example.invalid/dzsm.exe",
        size=7,
        digest=hashlib.sha256(b"payload").hexdigest(),
        notes_url="",
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def _serves(body: bytes) -> httpx.Client:
    return _client(lambda request: httpx.Response(200, content=body))


def test_a_good_download_is_kept(tmp_path: Path) -> None:
    got = download(_release(), tmp_path, client=_serves(b"payload"))

    assert got.read_bytes() == b"payload"


def test_a_file_that_is_not_what_github_said_is_refused(tmp_path: Path) -> None:
    """The steamcmd lesson, on a file that would go on to replace the program itself: a
    download that merely finished is not evidence."""
    with pytest.raises(SelfUpdateError, match="does not match its published checksum"):
        download(_release(), tmp_path, client=_serves(b"wrong!!"))


def test_a_refused_download_leaves_nothing_behind(tmp_path: Path) -> None:
    with pytest.raises(SelfUpdateError):
        download(_release(), tmp_path, client=_serves(b"wrong!!"))

    assert list(tmp_path.iterdir()) == []


def test_a_truncated_download_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SelfUpdateError, match="expected 7"):
        download(_release(), tmp_path, client=_serves(b"pay"))


def test_a_release_with_no_checksum_still_checks_the_size(tmp_path: Path) -> None:
    """Releases published before GitHub recorded digests have none. The absence is
    logged rather than treated as a pass."""
    got = download(_release(digest=None), tmp_path, client=_serves(b"payload"))

    assert got.is_file()


# --- swapping it in ----------------------------------------------------------


def test_the_new_binary_takes_the_name(tmp_path: Path) -> None:
    current = tmp_path / "dzsm.exe"
    current.write_bytes(b"old")
    fetched = tmp_path / "dzsm.exe.v0.2.0"
    fetched.write_bytes(b"new")

    retired = apply(fetched, current=current)

    assert current.read_bytes() == b"new"
    assert retired.read_bytes() == b"old"
    assert retired.name == "dzsm.exe.old"


def test_an_older_retired_copy_is_replaced(tmp_path: Path) -> None:
    """Otherwise every update leaves another 20 MB behind forever."""
    current = tmp_path / "dzsm.exe"
    current.write_bytes(b"v2")
    (tmp_path / "dzsm.exe.old").write_bytes(b"v0")
    fetched = tmp_path / "new.exe"
    fetched.write_bytes(b"v3")

    apply(fetched, current=current)

    assert (tmp_path / "dzsm.exe.old").read_bytes() == b"v2"
    assert len(list(tmp_path.iterdir())) == 2


def test_a_failed_move_puts_the_old_binary_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one outcome that must never happen is a machine with no dzsm on it."""
    current = tmp_path / "dzsm.exe"
    current.write_bytes(b"old")
    fetched = tmp_path / "new.exe"
    fetched.write_bytes(b"new")

    def refuse(src: str, dst: str) -> str:
        raise OSError(13, "denied")

    monkeypatch.setattr("dzsm.selfupdate.shutil.move", refuse)

    with pytest.raises(SelfUpdateError, match="cannot move"):
        apply(fetched, current=current)

    assert current.read_bytes() == b"old"


def test_a_missing_download_is_refused(tmp_path: Path) -> None:
    current = tmp_path / "dzsm.exe"
    current.write_bytes(b"old")

    with pytest.raises(SelfUpdateError, match="is not there"):
        apply(tmp_path / "gone.exe", current=current)

    assert current.read_bytes() == b"old"
