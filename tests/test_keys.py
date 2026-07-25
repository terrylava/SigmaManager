"""Key syncing.

Keys go to the server installation because that is where the engine reads them. That
directory is a promoted serverapp/<buildid>/ tree, which everything else treats as
immutable, so the rules here are narrow on purpose: only ever add, never overwrite,
never delete.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from dzsm.config import Config, InstanceConfig, WorkshopMod
from dzsm.instance import ensure_layout, sync_keys

WORKSHOP_ID = 1559212036
VERSION = 1721005200


@pytest.fixture
def ready(config: Config, tmp_path: Path) -> Config:
    server = tmp_path / "server"
    server.mkdir()
    exe = server / ("DayZServer_x64.exe" if sys.platform == "win32" else "DayZServer")
    exe.write_bytes(b"MZ")
    (server / "serverDZ.cfg").write_text("hostname = 'test';", encoding="utf-8")
    return config.model_copy(
        update={
            "server_app_path": server,
            "mods": {"CF": WorkshopMod(workshop_id=WORKSHOP_ID)},
            "instances": {"chernarus": InstanceConfig(port=2302, mods=["CF"])},
        }
    )


def _mod_with_keys(ready: Config, *names: str, directory: str = "keys") -> Path:
    path = ready.version_dir(WORKSHOP_ID, VERSION)
    (path / directory).mkdir(parents=True)
    for name in names:
        (path / directory / name).write_bytes(b"key-" + name.encode())
    return path


def _server_dir(ready: Config) -> Path:
    assert ready.server_app_path is not None
    return ready.server_app_path


def _server_keys(ready: Config) -> Path:
    return _server_dir(ready) / "keys"


def test_keys_land_in_the_server_directory(ready: Config) -> None:
    mod = _mod_with_keys(ready, "Jacob_Mango_V3.bikey")

    added = sync_keys(_server_dir(ready), "chernarus", [mod])

    assert (_server_keys(ready) / "Jacob_Mango_V3.bikey").is_file()
    assert added == [_server_keys(ready) / "Jacob_Mango_V3.bikey"]


def test_not_in_the_instance_directory(ready: Config) -> None:
    mod = _mod_with_keys(ready, "Jacob_Mango_V3.bikey")

    sync_keys(_server_dir(ready), "chernarus", [mod])

    assert not (ready.instance_dir("chernarus") / "keys").exists()


def test_uppercase_keys_directory_is_found(ready: Config) -> None:
    """Mods are inconsistent about the casing."""
    mod = _mod_with_keys(ready, "Some.bikey", directory="Keys")

    sync_keys(_server_dir(ready), "chernarus", [mod])

    assert (_server_keys(ready) / "Some.bikey").is_file()


def test_multiple_keys_from_one_mod(ready: Config) -> None:
    mod = _mod_with_keys(ready, "a.bikey", "b.bikey")

    added = sync_keys(_server_dir(ready), "chernarus", [mod])

    assert len(added) == 2


def test_an_existing_key_is_never_overwritten(ready: Config) -> None:
    """A running server may have the file open. Adding is safe; rewriting is not."""
    mod = _mod_with_keys(ready, "shared.bikey")
    destination = _server_keys(ready)
    destination.mkdir(parents=True)
    (destination / "shared.bikey").write_bytes(b"original-and-possibly-open")

    added = sync_keys(_server_dir(ready), "chernarus", [mod])

    assert added == []
    assert (destination / "shared.bikey").read_bytes() == b"original-and-possibly-open"


def test_existing_keys_from_other_mods_are_kept(ready: Config) -> None:
    """Never delete: another instance's mods may depend on keys this one knows nothing
    about."""
    mod = _mod_with_keys(ready, "new.bikey")
    destination = _server_keys(ready)
    destination.mkdir(parents=True)
    (destination / "someone_elses.bikey").write_bytes(b"keep me")

    sync_keys(_server_dir(ready), "chernarus", [mod])

    assert (destination / "someone_elses.bikey").exists()
    assert (destination / "new.bikey").exists()


def test_a_mod_with_no_keys_is_not_an_error(ready: Config) -> None:
    path = ready.version_dir(WORKSHOP_ID, VERSION)
    (path / "addons").mkdir(parents=True)

    assert sync_keys(_server_dir(ready), "chernarus", [path]) == []


def test_layout_no_longer_creates_an_instance_keys_dir(ready: Config) -> None:
    root = ensure_layout(ready, _server_dir(ready), "chernarus")

    assert (root / "profiles").is_dir()
    assert not (root / "keys").exists()
