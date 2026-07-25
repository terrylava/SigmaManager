"""steamcmd bootstrap.

No network: Valve's archive is stood up in-memory and served through
httpx.MockTransport. The traversal tests matter because this writes an executable to
disk from a downloaded archive.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest

from dzsm.config import SteamConfig
from dzsm.steam import SteamError, ensure_steamcmd


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return buffer.getvalue()


def _serving(payload: bytes | None, status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if payload is None:
            return httpx.Response(status)
        return httpx.Response(status, content=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _steam(tmp_path: Path, *, auto_install: bool = True) -> SteamConfig:
    return SteamConfig(
        username="someone",
        steamcmd_path=tmp_path / "steamcmd" / "steamcmd.exe",
        auto_install=auto_install,
    )


def test_existing_steamcmd_is_left_alone(tmp_path: Path) -> None:
    """Idempotent: no download when the binary is already there."""
    steam = _steam(tmp_path)
    steam.steamcmd_path.parent.mkdir(parents=True)
    steam.steamcmd_path.write_bytes(b"already here")

    with _serving(None, status=500) as client:
        assert ensure_steamcmd(steam, client=client) == steam.steamcmd_path
    assert steam.steamcmd_path.read_bytes() == b"already here"


def test_missing_steamcmd_is_downloaded_and_extracted(tmp_path: Path) -> None:
    steam = _steam(tmp_path)
    archive = _zip_bytes({"steamcmd.exe": b"MZ-fake-binary", "README": b"docs"})

    with _serving(archive) as client:
        result = ensure_steamcmd(steam, client=client)

    assert result == steam.steamcmd_path
    assert steam.steamcmd_path.read_bytes() == b"MZ-fake-binary"


def test_auto_install_disabled_raises_with_instructions(tmp_path: Path) -> None:
    steam = _steam(tmp_path, auto_install=False)
    with pytest.raises(SteamError, match="auto_install"):
        ensure_steamcmd(steam)


def test_download_failure_raises(tmp_path: Path) -> None:
    steam = _steam(tmp_path)
    with _serving(None, status=503) as client:
        with pytest.raises(SteamError, match="could not download"):
            ensure_steamcmd(steam, client=client)


def test_archive_without_the_binary_raises(tmp_path: Path) -> None:
    """A 200 that unpacks into the wrong thing is still a failure."""
    steam = _steam(tmp_path)
    with _serving(_zip_bytes({"README": b"nothing useful"})) as client:
        with pytest.raises(SteamError, match="did not contain"):
            ensure_steamcmd(steam, client=client)


def test_corrupt_archive_raises(tmp_path: Path) -> None:
    steam = _steam(tmp_path)
    with _serving(b"not a zip at all") as client:
        with pytest.raises(SteamError, match="could not extract"):
            ensure_steamcmd(steam, client=client)


def test_traversal_member_is_refused(tmp_path: Path) -> None:
    """Zip-slip: a member escaping the target directory must abort the extraction."""
    steam = _steam(tmp_path)
    archive = _zip_bytes({"../../evil.exe": b"pwned", "steamcmd.exe": b"MZ"})

    with _serving(archive) as client, pytest.raises(SteamError, match="escapes"):
        ensure_steamcmd(steam, client=client)

    assert not (tmp_path.parent / "evil.exe").exists()
    assert not (tmp_path / "evil.exe").exists()


def test_no_archive_left_behind_on_success(tmp_path: Path) -> None:
    steam = _steam(tmp_path)
    with _serving(_zip_bytes({"steamcmd.exe": b"MZ"})) as client:
        ensure_steamcmd(steam, client=client)

    leftovers = [p.name for p in steam.steamcmd_path.parent.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_no_archive_left_behind_on_failure(tmp_path: Path) -> None:
    steam = _steam(tmp_path)
    with _serving(b"garbage") as client, pytest.raises(SteamError):
        ensure_steamcmd(steam, client=client)

    leftovers = [p.name for p in steam.steamcmd_path.parent.iterdir() if p.name.startswith(".")]
    assert leftovers == []
