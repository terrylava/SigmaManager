"""Per-server mod management and manager-wide settings.

The server page edits one instance in isolation: add a configured mod to *this* server,
take it off *this* server, or fork it into an editable copy only *this* server uses. The
distinction from the assignment panel matters — that one answers "which servers use this
mod", these answer "what does this server run".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dzsm import settings
from dzsm.config import Config, InstanceConfig, LocalMod, WorkshopMod, load_config
from dzsm.mods import ModCacheError, local_copy
from dzsm.state import State

CONFIG = """\
root = '{root}'
allow_long_root = true

[steam]
username = "someone"
steamcmd_path = '{root}/steamcmd/steamcmd.exe'

[rcon]
startup_grace_seconds = 300

[mods.CF]
kind = "workshop"
workshop_id = 1559212036

[mods.Expansion]
kind = "workshop"
workshop_id = 2116157322

[mods.CF_fork]
kind = "local"
path = '{root}/localmods/CF_fork'

[instances.chernarus]
port = 2302
mods = ["CF", "Expansion"]

[instances.sakhal]
port = 2402
mods = ["CF"]
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(root=tmp_path.as_posix()), encoding="utf-8")
    return path


def _edit(path: Path, apply: object) -> Config:
    document = settings.read_document(path)
    apply(document)  # type: ignore[operator]
    return settings.save(path, document)


# --- one server's mods -------------------------------------------------------


def test_a_mod_is_added_to_this_server_only(config_file: Path) -> None:
    loaded = _edit(
        config_file, lambda d: settings.set_instance_mod(d, "sakhal", "Expansion", "mods")
    )

    assert loaded.instances["sakhal"].mods == ["CF", "Expansion"]
    assert loaded.instances["chernarus"].mods == ["CF", "Expansion"]


def test_removing_from_one_server_leaves_the_other(config_file: Path) -> None:
    loaded = _edit(
        config_file, lambda d: settings.remove_mod_from_instance(d, "chernarus", "CF")
    )

    assert loaded.instances["chernarus"].mods == ["Expansion"]
    assert loaded.instances["sakhal"].mods == ["CF"]


def test_removing_from_a_server_keeps_it_configured(config_file: Path) -> None:
    """"Not on this server" is not "forget this mod" — the download stays too."""
    loaded = _edit(
        config_file, lambda d: settings.remove_mod_from_instance(d, "chernarus", "CF")
    )

    assert "CF" in loaded.mods


def test_removing_clears_every_list(config_file: Path) -> None:
    """Unlike unticking in the assignment panel, this means the server does not use it."""
    _edit(config_file, lambda d: settings.set_instance_mod(d, "sakhal", "CF", "keep_mods"))
    loaded = _edit(
        config_file, lambda d: settings.remove_mod_from_instance(d, "sakhal", "CF")
    )

    assert loaded.instances["sakhal"].keep_mods == []
    assert loaded.instances["sakhal"].mods == []


def test_changing_the_list_moves_rather_than_duplicates(config_file: Path) -> None:
    loaded = _edit(
        config_file,
        lambda d: settings.set_instance_mod(d, "chernarus", "Expansion", "server_mods"),
    )

    assert loaded.instances["chernarus"].mods == ["CF"]
    assert loaded.instances["chernarus"].server_mods == ["Expansion"]


def test_removing_a_mod_the_server_does_not_have_is_harmless(config_file: Path) -> None:
    loaded = _edit(
        config_file, lambda d: settings.remove_mod_from_instance(d, "sakhal", "Expansion")
    )

    assert loaded.instances["sakhal"].mods == ["CF"]


def test_an_unknown_instance_is_refused(config_file: Path) -> None:
    with pytest.raises(settings.SettingsError, match="no instance"):
        _edit(config_file, lambda d: settings.set_instance_mod(d, "ghost", "CF", "mods"))


def test_an_unknown_mod_is_refused(config_file: Path) -> None:
    with pytest.raises(settings.SettingsError, match="no mod named"):
        _edit(config_file, lambda d: settings.set_instance_mod(d, "sakhal", "Ghost", "mods"))


# --- replacing in place ------------------------------------------------------


def test_a_replacement_keeps_its_position(config_file: Path) -> None:
    """Load order is the whole point: CF must stay first, or Expansion breaks."""
    loaded = _edit(
        config_file, lambda d: settings.replace_mod_in_instance(d, "chernarus", "CF", "CF_fork")
    )

    assert loaded.instances["chernarus"].mods == ["CF_fork", "Expansion"]


def test_a_replacement_only_touches_the_named_instance(config_file: Path) -> None:
    loaded = _edit(
        config_file, lambda d: settings.replace_mod_in_instance(d, "chernarus", "CF", "CF_fork")
    )

    assert loaded.instances["sakhal"].mods == ["CF"]


def test_replacing_something_absent_is_refused(config_file: Path) -> None:
    with pytest.raises(settings.SettingsError, match="does not use"):
        _edit(
            config_file,
            lambda d: settings.replace_mod_in_instance(d, "sakhal", "Expansion", "CF_fork"),
        )


def test_replacing_with_a_mod_already_there_is_refused(config_file: Path) -> None:
    """It would collapse two entries into one and silently drop a mod."""
    with pytest.raises(settings.SettingsError, match="already lists"):
        _edit(
            config_file,
            lambda d: settings.replace_mod_in_instance(d, "chernarus", "CF", "Expansion"),
        )


# --- local copies ------------------------------------------------------------


@pytest.fixture
def cached(config: Config) -> Config:
    """The shared config fixture, with a workshop mod to fork."""
    config.mods = {"CF": WorkshopMod(workshop_id=1559212036)}
    config.instances = {"chernarus": InstanceConfig(port=2302, mods=["CF"])}
    return config


def _state(*versions: int, pin: int | None = None) -> State:
    state = State()
    for version in versions:
        state.record_version(1559212036, version)
    if pin is not None:
        state.pins["chernarus"] = {"CF": pin}
    return state


def _cache(config: Config, workshop_id: int, version: int) -> Path:
    directory = config.version_dir(workshop_id, version)
    (directory / "addons").mkdir(parents=True)
    (directory / "addons" / "data.pbo").write_bytes(b"pbo")
    return directory


def test_a_local_copy_is_a_real_copy(cached: Config) -> None:
    """Never a hardlink: editing the fork must not edit the snapshot it came from."""
    _cache(cached, 1559212036, 100)
    state = _state(100)

    path, version = local_copy(cached, state, "CF", "CF_edited")

    assert (path / "addons" / "data.pbo").read_bytes() == b"pbo"
    assert version == 100

    (path / "addons" / "data.pbo").write_bytes(b"changed")
    assert (cached.version_dir(1559212036, 100) / "addons" / "data.pbo").read_bytes() == b"pbo"


def test_it_copies_the_version_the_instance_is_running(cached: Config) -> None:
    """Not the newest cached one: a download that has not been activated yet is not what
    the admin is looking at."""
    _cache(cached, 1559212036, 100)
    _cache(cached, 1559212036, 200)
    state = _state(100, 200, pin=100)

    _, version = local_copy(cached, state, "CF", "CF_fork", instance_name="chernarus")

    assert version == 100


def test_without_a_pin_it_copies_the_newest(cached: Config) -> None:
    _cache(cached, 1559212036, 100)
    _cache(cached, 1559212036, 200)
    state = _state(100, 200)

    _, version = local_copy(cached, state, "CF", "CF_fork")

    assert version == 200


def test_copying_an_uncached_mod_is_refused(cached: Config) -> None:
    with pytest.raises(ModCacheError, match="no cached version"):
        local_copy(cached, State(), "CF", "CF_fork")


def test_it_refuses_to_overwrite_an_existing_copy(cached: Config) -> None:
    _cache(cached, 1559212036, 100)
    state = _state(100)
    local_copy(cached, state, "CF", "CF_fork")

    with pytest.raises(ModCacheError, match="already exists"):
        local_copy(cached, state, "CF", "CF_fork")


def test_an_unknown_mod_is_refused_by_the_copy(cached: Config) -> None:
    with pytest.raises(ModCacheError, match="no mod named"):
        local_copy(cached, State(), "Ghost", "fork")


def test_a_forked_mod_configures_as_local(config_file: Path) -> None:
    """The copy has no relationship to the Workshop item, so it is never updated."""
    loaded = _edit(
        config_file,
        lambda d: settings.add_local_mod(d, "CF_edited", Path("D:/dzsm/localmods/CF_edited")),
    )

    assert isinstance(loaded.mods["CF_edited"], LocalMod)


# --- manager-wide settings ---------------------------------------------------


def test_a_global_setting_is_changed(config_file: Path) -> None:
    loaded = _edit(
        config_file, lambda d: settings.apply_global_patch(d, {"stop_timeout_seconds": 90})
    )

    assert loaded.stop_timeout_seconds == 90


def test_a_dotted_setting_reaches_the_subtable(config_file: Path) -> None:
    loaded = _edit(
        config_file,
        lambda d: settings.apply_global_patch(d, {"rcon.final_warning_interval_seconds": 8}),
    )

    assert loaded.rcon.final_warning_interval_seconds == 8


def test_a_dotted_setting_leaves_its_siblings_alone(config_file: Path) -> None:
    """The reason the patch is dotted rather than nested: a whole-table write would drop
    every key it did not mention."""
    loaded = _edit(
        config_file, lambda d: settings.apply_global_patch(d, {"rcon.host": "10.0.0.5"})
    )

    assert loaded.rcon.startup_grace_seconds == 300


def test_a_subtable_is_created_when_absent(config_file: Path) -> None:
    loaded = _edit(config_file, lambda d: settings.apply_global_patch(d, {"api.port": 9000}))

    assert loaded.api.port == 9000


def test_null_restores_the_default(config_file: Path) -> None:
    _edit(config_file, lambda d: settings.apply_global_patch(d, {"gc_retention_days": 30}))
    loaded = _edit(
        config_file, lambda d: settings.apply_global_patch(d, {"gc_retention_days": None})
    )

    assert loaded.gc_retention_days is None


def test_root_cannot_be_changed_here(config_file: Path) -> None:
    """Every path derives from it and nothing here moves anything, so a form must not."""
    with pytest.raises(settings.SettingsError, match="cannot be edited here"):
        _edit(config_file, lambda d: settings.apply_global_patch(d, {"root": "D:/elsewhere"}))


def test_an_invalid_global_value_is_refused(config_file: Path) -> None:
    with pytest.raises(settings.SettingsError):
        _edit(config_file, lambda d: settings.apply_global_patch(d, {"gc_retention_days": 0}))


def test_a_refused_global_edit_changes_nothing(config_file: Path) -> None:
    original = config_file.read_text(encoding="utf-8")

    with pytest.raises(settings.SettingsError):
        _edit(config_file, lambda d: settings.apply_global_patch(d, {"mission_prefix": "a.b"}))

    assert config_file.read_text(encoding="utf-8") == original


def test_the_reported_values_match_the_config(config_file: Path) -> None:
    """The form renders from these, so a mismatch shows the wrong current setting."""
    loaded = load_config(config_file)
    values = settings.global_values(loaded)

    assert values["rcon.startup_grace_seconds"] == 300
    assert values["stop_timeout_seconds"] == loaded.stop_timeout_seconds
    assert set(values) == set(settings.EDITABLE_GLOBAL_FIELDS)
