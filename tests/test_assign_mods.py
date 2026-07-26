"""Assigning an already-configured mod to servers.

The three lists are mutually exclusive — config validation rejects a mod that is both
loaded and kept, or both client and server-only — so assigning to one must clear the
others for that instance. Otherwise a tick in the UI produces a config that will not load.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dzsm import settings
from dzsm.config import load_config

CONFIG = """\
root = '{root}'
allow_long_root = true

[steam]
username = "someone"
steamcmd_path = '{root}/steamcmd/steamcmd.exe'

[mods.CF]
kind = "workshop"
workshop_id = 1559212036

[mods.AdminTools]
kind = "workshop"
workshop_id = 1564026768

[instances.chernarus]
port = 2302
mods = ["CF"]

[instances.sakhal]
port = 2402
mods = []
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(root=tmp_path.as_posix()), encoding="utf-8")
    return path


def _assign(path: Path, mod: str, instances: list[str], list_name: str = "mods") -> None:
    document = settings.read_document(path)
    settings.set_mod_instances(document, mod, list_name, instances)
    settings.save(path, document)


# --- assigning ---------------------------------------------------------------


def test_a_mod_is_added_to_another_server(config_file: Path) -> None:
    """The whole point: CF is on chernarus, put it on sakhal too."""
    _assign(config_file, "CF", ["chernarus", "sakhal"])
    loaded = load_config(config_file)

    assert loaded.instances["chernarus"].mods == ["CF"]
    assert loaded.instances["sakhal"].mods == ["CF"]


def test_unticking_removes_it(config_file: Path) -> None:
    _assign(config_file, "CF", ["sakhal"])
    loaded = load_config(config_file)

    assert loaded.instances["chernarus"].mods == []
    assert loaded.instances["sakhal"].mods == ["CF"]


def test_assigning_to_nothing_unloads_it_everywhere(config_file: Path) -> None:
    _assign(config_file, "CF", [])

    assert load_config(config_file).instances["chernarus"].mods == []


def test_it_appends_rather_than_reordering(config_file: Path) -> None:
    """Load order is the semicolon order; an assignment must not shuffle it."""
    _assign(config_file, "AdminTools", ["chernarus"])

    assert load_config(config_file).instances["chernarus"].mods == ["CF", "AdminTools"]


def test_assigning_twice_is_idempotent(config_file: Path) -> None:
    _assign(config_file, "CF", ["chernarus"])
    _assign(config_file, "CF", ["chernarus"])

    assert load_config(config_file).instances["chernarus"].mods == ["CF"]


def test_other_mods_are_untouched(config_file: Path) -> None:
    _assign(config_file, "AdminTools", ["sakhal"])
    loaded = load_config(config_file)

    assert loaded.instances["chernarus"].mods == ["CF"]


# --- the lists are mutually exclusive ----------------------------------------


def test_moving_to_server_only_clears_the_loaded_list(config_file: Path) -> None:
    """A mod in both mods and server_mods fails validation, so the tick has to move it."""
    _assign(config_file, "CF", ["chernarus"], "server_mods")
    loaded = load_config(config_file)

    assert loaded.instances["chernarus"].mods == []
    assert loaded.instances["chernarus"].server_mods == ["CF"]


def test_moving_to_kept_clears_the_loaded_list(config_file: Path) -> None:
    _assign(config_file, "CF", ["chernarus"], "keep_mods")
    loaded = load_config(config_file)

    assert loaded.instances["chernarus"].mods == []
    assert loaded.instances["chernarus"].keep_mods == ["CF"]


def test_moving_back_to_loaded_clears_kept(config_file: Path) -> None:
    _assign(config_file, "CF", ["chernarus"], "keep_mods")
    _assign(config_file, "CF", ["chernarus"], "mods")
    loaded = load_config(config_file)

    assert loaded.instances["chernarus"].mods == ["CF"]
    assert loaded.instances["chernarus"].keep_mods == []


def test_unticking_one_list_leaves_the_others_alone(config_file: Path) -> None:
    """Unloading a mod must not silently discard a keep_mods claim on it."""
    _assign(config_file, "CF", ["chernarus"], "keep_mods")
    _assign(config_file, "CF", [], "mods")

    assert load_config(config_file).instances["chernarus"].keep_mods == ["CF"]


def test_the_result_always_loads(config_file: Path) -> None:
    for list_name in ("mods", "server_mods", "keep_mods"):
        _assign(config_file, "CF", ["chernarus", "sakhal"], list_name)
        load_config(config_file)


# --- refusals ----------------------------------------------------------------


def test_an_unknown_mod_is_refused(config_file: Path) -> None:
    with pytest.raises(settings.SettingsError, match="no mod named"):
        _assign(config_file, "Ghost", ["chernarus"])


def test_an_unknown_instance_is_refused(config_file: Path) -> None:
    with pytest.raises(settings.SettingsError, match="unknown instance"):
        _assign(config_file, "CF", ["ghost"])


def test_an_unknown_list_is_refused(config_file: Path) -> None:
    with pytest.raises(settings.SettingsError, match="unknown list"):
        _assign(config_file, "CF", ["chernarus"], "extra_mods")


def test_a_refusal_changes_nothing(config_file: Path) -> None:
    original = config_file.read_text(encoding="utf-8")

    with pytest.raises(settings.SettingsError):
        _assign(config_file, "CF", ["ghost"])

    assert config_file.read_text(encoding="utf-8") == original


def test_comments_survive(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "# keep me\n" + CONFIG.format(root=tmp_path.as_posix()), encoding="utf-8"
    )

    _assign(path, "CF", ["sakhal"])

    assert "# keep me" in path.read_text(encoding="utf-8")
