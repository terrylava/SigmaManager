"""Removing a mod from the config.

Distinct from deleting downloaded files: this forgets the mod, that reclaims disk.
Removing a name must never quietly throw away a multi-GB download, and must never leave
a config that no longer loads because an instance still names something gone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dzsm import settings
from dzsm.config import load_config

# The root is a real path, so it is filled in from tmp_path rather than hardcoded — a
# literal D:\... here would have these tests writing to an actual drive.
CONFIG = """\
root = '{root}'
allow_long_root = true

[steam]
username = "someone"
steamcmd_path = '{root}/steamcmd/steamcmd.exe'

# Keep this comment.
[mods.CF]
kind = "workshop"
workshop_id = 1559212036

[mods.Expansion]
kind = "workshop"
workshop_id = 2116157322

[mods.AdminTools]
kind = "workshop"
workshop_id = 1564026768

[instances.chernarus]
port = 2302
mods = ["CF"]
server_mods = ["AdminTools"]
keep_mods = ["Expansion"]

[instances.sakhal]
port = 2402
mods = ["CF"]
"""


def _config_text(tmp_path: Path) -> str:
    return CONFIG.format(root=tmp_path.as_posix())


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(_config_text(tmp_path), encoding="utf-8")
    return path


def _remove(path: Path, name: str, *, force: bool = False) -> None:
    document = settings.read_document(path)
    settings.remove_mod_from_config(document, name, strip_from_instances=force)
    settings.save(path, document)


# --- what counts as "in use" -------------------------------------------------


def test_a_loaded_mod_is_in_use(config_file: Path) -> None:
    document = settings.read_document(config_file)

    assert settings.instances_using(document, "CF") == ["chernarus", "sakhal"]


def test_a_server_only_mod_is_in_use(config_file: Path) -> None:
    document = settings.read_document(config_file)

    assert settings.instances_using(document, "AdminTools") == ["chernarus"]


def test_a_kept_mod_is_in_use(config_file: Path) -> None:
    """keep_mods exists to stop a disabled mod being swept away; removing it from the
    config would defeat that."""
    document = settings.read_document(config_file)

    assert settings.instances_using(document, "Expansion") == ["chernarus"]


def test_a_mod_no_instance_names_is_not_in_use(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        _config_text(tmp_path).replace('server_mods = ["AdminTools"]', "server_mods = []"),
        encoding="utf-8",
    )
    document = settings.read_document(path)

    assert settings.instances_using(document, "AdminTools") == []


# --- removing ----------------------------------------------------------------


def test_an_unused_mod_is_removed(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        _config_text(tmp_path).replace('server_mods = ["AdminTools"]', "server_mods = []"),
        encoding="utf-8",
    )

    _remove(path, "AdminTools")

    assert "AdminTools" not in load_config(path).mods


def test_a_used_mod_is_refused_by_default(config_file: Path) -> None:
    """Removing it while an instance names it would leave a config that will not load."""
    with pytest.raises(settings.SettingsError, match="still used by chernarus, sakhal"):
        _remove(config_file, "CF")

    assert "CF" in load_config(config_file).mods


def test_force_strips_it_from_every_list(config_file: Path) -> None:
    _remove(config_file, "CF", force=True)
    loaded = load_config(config_file)

    assert "CF" not in loaded.mods
    assert loaded.instances["chernarus"].mods == []
    assert loaded.instances["sakhal"].mods == []


def test_force_strips_server_mods_too(config_file: Path) -> None:
    _remove(config_file, "AdminTools", force=True)

    assert load_config(config_file).instances["chernarus"].server_mods == []


def test_force_strips_keep_mods_too(config_file: Path) -> None:
    _remove(config_file, "Expansion", force=True)

    assert load_config(config_file).instances["chernarus"].keep_mods == []


def test_other_mods_are_untouched(config_file: Path) -> None:
    _remove(config_file, "CF", force=True)
    loaded = load_config(config_file)

    assert set(loaded.mods) == {"Expansion", "AdminTools"}
    assert loaded.instances["chernarus"].server_mods == ["AdminTools"]


def test_the_result_still_loads(config_file: Path) -> None:
    """The whole reason removal is refused by default."""
    _remove(config_file, "CF", force=True)

    load_config(config_file)


def test_comments_survive(config_file: Path) -> None:
    _remove(config_file, "Expansion", force=True)

    assert "# Keep this comment." in config_file.read_text(encoding="utf-8")


def test_removing_something_absent_is_refused(config_file: Path) -> None:
    with pytest.raises(settings.SettingsError, match="no mod named"):
        _remove(config_file, "Ghost")


def test_removal_does_not_touch_the_cache(config_file: Path) -> None:
    """Forgetting a name must not throw away a multi-GB download. The files stay and show
    up as orphaned, to be deleted deliberately."""
    cached = Path(load_config(config_file).modcache_dir) / "1559212036" / "1771519119"
    cached.mkdir(parents=True)
    (cached / "mod.pbo").write_bytes(b"x")

    _remove(config_file, "CF", force=True)

    assert (cached / "mod.pbo").exists()
