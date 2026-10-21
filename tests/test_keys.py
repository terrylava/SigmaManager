"""Getting a mod's .bikey into the server's keys/ directory.

This is the one part of the design whose failure lands on a *player* rather than on the
server: the mod loads, the server runs, and the client is kicked at login for a PBO the
server cannot verify. Nothing in the server log connects the two, so both ways of losing
a key are tested here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dzsm.config import Config
from dzsm.instance import carry_keys_forward, key_directories, sync_keys


def _mod(root: Path, name: str, key_dir: str, key: str) -> Path:
    path = root / name
    (path / key_dir).mkdir(parents=True)
    (path / key_dir / key).write_bytes(b"key")
    (path / "addons").mkdir()
    return path


# --- which directory holds the keys ------------------------------------------


@pytest.mark.parametrize("name", ["keys", "Keys", "Key", "key", "KEYS"])
def test_every_spelling_of_the_key_directory_is_found(tmp_path: Path, name: str) -> None:
    """All four were present in one real mod cache. Matching only the plural forms meant
    every mod using the singular shipped a key the server never installed."""
    mod = _mod(tmp_path, "mod", name, "Author.bikey")

    assert [d.name for d in key_directories(mod)] == [name]


def test_a_directory_that_merely_starts_with_key_is_not_one(tmp_path: Path) -> None:
    """"keyframes" is not a key directory."""
    mod = tmp_path / "mod"
    (mod / "keyframes").mkdir(parents=True)

    assert key_directories(mod) == []


def test_a_mod_with_no_key_directory_is_not_an_error(tmp_path: Path) -> None:
    (tmp_path / "mod").mkdir()

    assert key_directories(tmp_path / "mod") == []


@pytest.mark.parametrize("name", ["keys", "Keys", "Key", "key"])
def test_the_key_is_installed_whatever_the_directory_is_called(
    tmp_path: Path, name: str
) -> None:
    server = tmp_path / "server"
    server.mkdir()
    mod = _mod(tmp_path, "mod", name, "Super.bikey")

    added = sync_keys(server, "chernarus", [mod])

    assert [key.name for key in added] == ["Super.bikey"]
    assert (server / "keys" / "Super.bikey").is_file()


def test_an_existing_key_is_left_alone(tmp_path: Path) -> None:
    """A running server may have it open, and re-copying gains nothing."""
    server = tmp_path / "server"
    (server / "keys").mkdir(parents=True)
    (server / "keys" / "Super.bikey").write_bytes(b"original")
    mod = _mod(tmp_path, "mod", "Key", "Super.bikey")

    assert sync_keys(server, "chernarus", [mod]) == []
    assert (server / "keys" / "Super.bikey").read_bytes() == b"original"


# --- surviving a server update -----------------------------------------------


def _build(config: Config, build: str, *keys: str) -> Path:
    path = config.serverapp_dir / build / "keys"
    path.mkdir(parents=True)
    for key in keys:
        (path / key).write_bytes(b"key")
    return path.parent


def test_keys_are_carried_forward_to_a_new_build(config: Config) -> None:
    """Keys live in serverapp/<buildid>/keys/, so a server update starts from an empty
    set. Without this, a key only comes back if an instance loading that exact mod is
    started afterwards — and one belonging to an instance left down is lost for good."""
    _build(config, "25460313", "Super.bikey", "CTAPOCTA.bikey")
    new = _build(config, "25963285")

    added = carry_keys_forward(config, new)

    assert sorted(key.name for key in added) == ["CTAPOCTA.bikey", "Super.bikey"]


def test_the_newest_other_build_wins(config: Config) -> None:
    """By build id, not directory mtime — the same ordering as missions and mod
    versions."""
    _build(config, "25460313", "Old.bikey")
    _build(config, "25963285", "New.bikey")
    target = _build(config, "26103115")

    carry_keys_forward(config, target)

    assert (target / "keys" / "New.bikey").is_file()
    assert (target / "keys" / "Old.bikey").is_file()


def test_carrying_forward_never_overwrites(config: Config) -> None:
    _build(config, "25460313", "Super.bikey")
    new = _build(config, "25963285")
    (new / "keys" / "Super.bikey").write_bytes(b"current")

    assert carry_keys_forward(config, new) == []
    assert (new / "keys" / "Super.bikey").read_bytes() == b"current"


def test_a_build_never_carries_from_itself(config: Config) -> None:
    only = _build(config, "25963285", "Super.bikey")

    assert carry_keys_forward(config, only) == []


def test_no_other_build_is_not_an_error(config: Config) -> None:
    """A first install has nothing to carry forward."""
    new = _build(config, "25963285")

    assert carry_keys_forward(config, new) == []
