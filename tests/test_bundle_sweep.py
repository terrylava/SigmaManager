r"""Clearing up unpacked bundles a previous run left in TEMP.

A onefile build unpacks itself into %TEMP%\_MEIxxxxx and deletes it on exit. Stopped
abruptly it cannot: Windows will not delete a DLL still mapped by a thread that has not
unwound. The copy stays, 25 MB a time. Twelve of them accumulated on one machine in a
day of testing.

The whole risk here is deleting something that matters, so that is what is tested.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from dzsm.api import BUNDLE_MARKER, sweep_stale_bundles


def _bundle(root: Path, name: str, *, ours: bool = True) -> Path:
    path = root / name
    (path / "pydantic_core").mkdir(parents=True)
    (path / "python313.dll").write_bytes(b"MZ")
    if ours:
        (path / BUNDLE_MARKER).parent.mkdir(parents=True, exist_ok=True)
        (path / BUNDLE_MARKER).write_text("<!doctype html>", encoding="utf-8")
    return path


def test_a_leftover_bundle_is_removed(tmp_path: Path) -> None:
    stale = _bundle(tmp_path, "_MEI123456")

    removed = sweep_stale_bundles(temp_dir=tmp_path, keep=None)

    assert removed == [stale]
    assert not stale.exists()


def test_another_programs_bundle_is_left_alone(tmp_path: Path) -> None:
    """Every onefile application unpacks under the same _MEI prefix. Deleting one of
    theirs would be rude at best, and could break a running program at worst."""
    theirs = _bundle(tmp_path, "_MEI999999", ours=False)

    assert sweep_stale_bundles(temp_dir=tmp_path, keep=None) == []
    assert theirs.is_dir()


def test_the_bundle_this_process_is_running_from_is_kept(tmp_path: Path) -> None:
    """Deleting it would pull dashboard.html out from under the server that is starting."""
    mine = _bundle(tmp_path, "_MEI111111")

    assert sweep_stale_bundles(temp_dir=tmp_path, keep=mine) == []
    assert (mine / BUNDLE_MARKER).is_file()


def test_a_bundle_in_use_is_left_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The rename is the liveness test, and it has to be a rename rather than a delete:
    a partial delete inside a running copy breaks it in a way that looks like a corrupt
    install. A rename that fails leaves the directory exactly as it was."""
    busy = _bundle(tmp_path, "_MEI222222")

    def refuse(self: Path, target: Path) -> Path:
        raise PermissionError(32, "The process cannot access the file")

    monkeypatch.setattr(Path, "rename", refuse)

    assert sweep_stale_bundles(temp_dir=tmp_path, keep=None) == []
    assert (busy / "python313.dll").is_file()
    assert (busy / BUNDLE_MARKER).is_file()


def test_several_leftovers_go_in_one_pass(tmp_path: Path) -> None:
    stale = [_bundle(tmp_path, f"_MEI00000{n}") for n in range(3)]

    removed = sweep_stale_bundles(temp_dir=tmp_path, keep=None)

    assert sorted(removed) == sorted(stale)
    assert not any(path.exists() for path in stale)


def test_an_empty_temp_is_not_an_error(tmp_path: Path) -> None:
    assert sweep_stale_bundles(temp_dir=tmp_path, keep=None) == []


def test_a_missing_temp_is_not_an_error(tmp_path: Path) -> None:
    """Never a reason to fail a start (Rule 3)."""
    assert sweep_stale_bundles(temp_dir=tmp_path / "gone", keep=None) == []


def test_a_file_named_like_a_bundle_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "_MEI_not_a_directory").write_text("x", encoding="utf-8")

    assert sweep_stale_bundles(temp_dir=tmp_path, keep=None) == []


@pytest.mark.skipif(sys.platform != "win32", reason="deleting an open file only fails here")
def test_an_open_file_really_does_stop_the_sweep(tmp_path: Path) -> None:
    """Not a mock: the actual behaviour the design leans on. Windows refuses to rename a
    directory holding an open handle, which is what makes 'in use' detectable at all."""
    busy = _bundle(tmp_path, "_MEI333333")

    with (busy / "python313.dll").open("rb"):
        assert sweep_stale_bundles(temp_dir=tmp_path, keep=None) == []

    assert busy.is_dir()
