"""Editing config.toml programmatically.

This rewrites the file every command depends on, so the tests are mostly about what must
*not* happen: comments disappearing, an invalid config reaching disk, or the previous
version being lost.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomlkit

from dzsm import settings
from dzsm.config import load_config

CONFIG = """\
# The root everything lives under.
root = 'D:\\dztest'

[steam]
username = "someone"
steamcmd_path = 'D:\\dztest\\steamcmd\\steamcmd.exe'

[mods.CF]
kind = "workshop"
workshop_id = 1559212036

# The main server.
[instances.chernarus]
port = 2302
# ORDER IS LOAD ORDER.
mods = ["CF"]
restart_after_hours = 4
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


def _patch(path: Path, patch: dict[str, object], name: str = "chernarus") -> None:
    document = settings.read_document(path)
    settings.apply_instance_patch(document, name, patch)
    settings.save(path, document)


# --- comments survive --------------------------------------------------------


def test_comments_survive_an_edit(config_file: Path) -> None:
    """Rewriting from a parsed dict would throw away every explanation in the file."""
    _patch(config_file, {"port": 2402})
    text = config_file.read_text(encoding="utf-8")

    assert "# The root everything lives under." in text
    assert "# ORDER IS LOAD ORDER." in text
    assert "# The main server." in text
    assert "port = 2402" in text


def test_unrelated_settings_are_untouched(config_file: Path) -> None:
    _patch(config_file, {"port": 2402})
    loaded = load_config(config_file)

    assert loaded.instances["chernarus"].mods == ["CF"]
    assert loaded.instances["chernarus"].restart_after_hours == 4
    assert loaded.steam.username == "someone"


# --- what can be set ---------------------------------------------------------


def test_a_scalar_is_set(config_file: Path) -> None:
    _patch(config_file, {"priority": "high"})

    assert load_config(config_file).instances["chernarus"].priority == "high"


def test_a_list_is_set(config_file: Path) -> None:
    """Core numbers, which is the whole point of the affinity UI."""
    _patch(config_file, {"cpu_affinity": [0, 1, 2, 3]})

    assert load_config(config_file).instances["chernarus"].cpu_affinity == [0, 1, 2, 3]


def test_a_boolean_is_set(config_file: Path) -> None:
    _patch(config_file, {"netlog": False})

    assert load_config(config_file).instances["chernarus"].netlog is False


def test_null_removes_a_key_restoring_the_default(config_file: Path) -> None:
    _patch(config_file, {"cpu_affinity": [0, 1]})
    _patch(config_file, {"cpu_affinity": None})

    assert load_config(config_file).instances["chernarus"].cpu_affinity is None
    assert "cpu_affinity" not in config_file.read_text(encoding="utf-8")


def test_several_fields_at_once(config_file: Path) -> None:
    _patch(config_file, {"priority": "above_normal", "cpu_count": 4, "netlog": False})
    instance = load_config(config_file).instances["chernarus"]

    assert (instance.priority, instance.cpu_count, instance.netlog) == (
        "above_normal",
        4,
        False,
    )


# --- what cannot ------------------------------------------------------------


def test_an_unknown_field_is_refused(config_file: Path) -> None:
    """A whitelist, so an edit cannot restructure the document."""
    with pytest.raises(settings.SettingsError, match="cannot be edited"):
        _patch(config_file, {"root": "D:/elsewhere"})


def test_an_unknown_instance_is_refused(config_file: Path) -> None:
    with pytest.raises(settings.SettingsError, match="no instance"):
        _patch(config_file, {"port": 2402}, name="ghost")


def test_an_invalid_value_never_reaches_disk(config_file: Path) -> None:
    """The file must still load afterwards; a config that does not is worse than a
    rejected edit, because every command stops working."""
    original = config_file.read_text(encoding="utf-8")

    with pytest.raises(settings.SettingsError):
        _patch(config_file, {"priority": "ludicrous"})

    assert config_file.read_text(encoding="utf-8") == original
    load_config(config_file)


def test_a_duplicate_port_is_refused(config_file: Path) -> None:
    """Cross-instance validation applies to edits exactly as it does at load."""
    document = settings.read_document(config_file)
    document["instances"]["second"] = tomlkit.parse("port = 2500\nmods = []\n")
    settings.save(config_file, document)

    with pytest.raises(settings.SettingsError, match="port"):
        _patch(config_file, {"port": 2500})


def test_a_mod_that_does_not_exist_is_refused(config_file: Path) -> None:
    with pytest.raises(settings.SettingsError, match="unknown mod"):
        _patch(config_file, {"mods": ["CF", "Ghost"]})


def test_the_error_does_not_echo_the_whole_document(config_file: Path) -> None:
    """pydantic's raw report embeds the entire input, which is unreadable in a toast."""
    with pytest.raises(settings.SettingsError) as excinfo:
        _patch(config_file, {"priority": "ludicrous"})

    assert "steamcmd_path" not in str(excinfo.value)


# --- raw editing -------------------------------------------------------------


def test_raw_text_can_replace_the_file(config_file: Path) -> None:
    text = CONFIG.replace("port = 2302", "port = 2502")

    settings.save_raw(config_file, text)

    assert load_config(config_file).instances["chernarus"].port == 2502


def test_raw_invalid_toml_is_refused(config_file: Path) -> None:
    original = config_file.read_text(encoding="utf-8")

    with pytest.raises(settings.SettingsError, match="not valid TOML"):
        settings.save_raw(config_file, "root = 'D:/x'\n[unclosed\n")

    assert config_file.read_text(encoding="utf-8") == original


def test_raw_valid_toml_that_is_not_a_config_is_refused(config_file: Path) -> None:
    original = config_file.read_text(encoding="utf-8")

    with pytest.raises(settings.SettingsError):
        settings.save_raw(config_file, "hello = 'world'\n")

    assert config_file.read_text(encoding="utf-8") == original


# --- durability --------------------------------------------------------------


def test_the_previous_version_is_kept(config_file: Path) -> None:
    """A valid but wrong edit should be recoverable."""
    _patch(config_file, {"port": 2402})

    backup = config_file.with_suffix(config_file.suffix + settings.BACKUP_SUFFIX)
    assert backup.is_file()
    assert "port = 2302" in backup.read_text(encoding="utf-8")


def test_no_temp_files_are_left_behind(config_file: Path) -> None:
    _patch(config_file, {"port": 2402})

    leftovers = [p.name for p in config_file.parent.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_a_locked_file_reports_why(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows a running DayZ server holds serverDZ.cfg open, so os.replace fails with
    access denied. A bare errno there is useless; the message has to name the cause."""

    def denied(self: Path, target: Path) -> None:
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(Path, "replace", denied)

    with pytest.raises(settings.FileLocked, match="open by a running process"):
        settings.write_text(config_file, "root = 'D:/x'\n")


def test_a_locked_file_leaves_no_temp_behind(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(self: Path, target: Path) -> None:
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(Path, "replace", denied)
    with pytest.raises(settings.FileLocked):
        settings.write_text(config_file, "root = 'D:/x'\n")

    leftovers = [p.name for p in config_file.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_saving_returns_the_config_it_wrote(config_file: Path) -> None:
    """So a caller can adopt it immediately rather than re-reading and hoping."""
    document = settings.read_document(config_file)
    settings.apply_instance_patch(document, "chernarus", {"port": 2402})

    saved = settings.save(config_file, document)

    assert saved.instances["chernarus"].port == 2402
