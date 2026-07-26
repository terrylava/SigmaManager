"""Adding mods that are already on disk.

For mods dzsm did not download — a server's !Workshop folder, an old install, a mod you
built. Nothing is copied or downloaded: the path is referenced where it is, and steamcmd
never touches it (spec §9).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dzsm import settings
from dzsm.config import Config, LocalMod, load_config
from dzsm.mods import ModCacheError, looks_like_mod, scan_for_mods

CONFIG = """\
root = '{root}'
allow_long_root = true

[steam]
username = "someone"
steamcmd_path = '{root}/steamcmd/steamcmd.exe'

[instances.chernarus]
port = 2302
mods = []
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(root=tmp_path.as_posix()), encoding="utf-8")
    return path


def _mod_dir(parent: Path, name: str, *, addons: bool = True, keys: bool = False) -> Path:
    path = parent / name
    path.mkdir(parents=True)
    if addons:
        (path / "addons").mkdir()
        (path / "addons" / "data.pbo").write_bytes(b"x" * 2048)
    if keys:
        (path / "keys").mkdir()
        (path / "keys" / "mod.bikey").write_bytes(b"k")
    return path


# --- recognising a mod folder ------------------------------------------------


def test_a_folder_with_addons_is_a_mod(tmp_path: Path) -> None:
    """addons/ is the real marker — that is where the PBOs live."""
    assert looks_like_mod(_mod_dir(tmp_path, "SomeMod"))


def test_an_at_prefixed_folder_is_a_mod(tmp_path: Path) -> None:
    """The @ prefix is only a convention, but it is a strong hint."""
    assert looks_like_mod(_mod_dir(tmp_path, "@Empty", addons=False))


def test_an_unrelated_folder_is_not(tmp_path: Path) -> None:
    (tmp_path / "screenshots").mkdir()

    assert not looks_like_mod(tmp_path / "screenshots")


def test_a_file_is_not(tmp_path: Path) -> None:
    (tmp_path / "readme.txt").write_text("x", encoding="utf-8")

    assert not looks_like_mod(tmp_path / "readme.txt")


# --- scanning ----------------------------------------------------------------


def test_a_folder_of_mods_is_scanned(config_file: Path, tmp_path: Path) -> None:
    """Pointed at something like a server's !Workshop directory."""
    workshop = tmp_path / "!Workshop"
    _mod_dir(workshop, "@CF")
    _mod_dir(workshop, "@Expansion")
    (workshop / "notes.txt").write_text("x", encoding="utf-8")

    found = scan_for_mods(load_config(config_file), workshop)

    assert [c.name for c in found] == ["@CF", "@Expansion"]


def test_a_single_mod_folder_is_scanned(config_file: Path, tmp_path: Path) -> None:
    """People will point it straight at one mod, so that has to work too."""
    mod = _mod_dir(tmp_path / "mods", "@Solo")

    found = scan_for_mods(load_config(config_file), mod)

    assert [c.name for c in found] == ["@Solo"]


def test_scanning_reports_size_and_contents(config_file: Path, tmp_path: Path) -> None:
    _mod_dir(tmp_path / "w", "@WithKeys", keys=True)

    found = scan_for_mods(load_config(config_file), tmp_path / "w")

    assert found[0].has_addons is True
    assert found[0].has_keys is True
    assert found[0].size_bytes > 0


def test_a_missing_directory_is_an_error(config_file: Path, tmp_path: Path) -> None:
    with pytest.raises(ModCacheError, match="not a directory"):
        scan_for_mods(load_config(config_file), tmp_path / "nope")


def test_an_already_configured_folder_is_flagged(config_file: Path, tmp_path: Path) -> None:
    """So the same directory is not added twice under two names."""
    mod = _mod_dir(tmp_path / "w", "@CF")
    document = settings.read_document(config_file)
    settings.add_local_mod(document, "CF", mod)
    settings.save(config_file, document)

    found = scan_for_mods(load_config(config_file), tmp_path / "w")

    assert found[0].configured_as == "CF"


# --- naming ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        ("@CF", "CF"),
        ("@DayZ-Expansion-Core", "DayZ-Expansion-Core"),
        ("@Some Mod v1.2", "Some_Mod_v1_2"),
        ("PlainFolder", "PlainFolder"),
        ("@", "local_mod"),
    ],
)
def test_a_name_is_derived_from_the_folder(folder: str, expected: str) -> None:
    """The @ marker is noise in a name instances refer to."""
    assert settings.local_mod_name(Path(folder), set()) == expected


def test_a_clashing_name_counts_up(tmp_path: Path) -> None:
    """No workshop id to disambiguate with, unlike a Workshop mod."""
    assert settings.local_mod_name(Path("@CF"), {"CF"}) == "CF_2"
    assert settings.local_mod_name(Path("@CF"), {"CF", "CF_2"}) == "CF_3"


# --- adding ------------------------------------------------------------------


def test_a_local_mod_is_added(config_file: Path, tmp_path: Path) -> None:
    mod = _mod_dir(tmp_path / "w", "@Solo")
    document = settings.read_document(config_file)
    settings.add_local_mod(document, "Solo", mod)
    settings.save(config_file, document)

    entry = load_config(config_file).mods["Solo"]
    assert isinstance(entry, LocalMod)
    assert entry.path == mod.resolve()


def test_it_is_marked_pinned(config_file: Path, tmp_path: Path) -> None:
    """Local mods have no versions and steamcmd never updates them."""
    mod = _mod_dir(tmp_path / "w", "@Solo")
    document = settings.read_document(config_file)
    settings.add_local_mod(document, "Solo", mod)
    settings.save(config_file, document)

    entry = load_config(config_file).mods["Solo"]
    assert isinstance(entry, LocalMod)
    assert entry.pinned is True


def test_it_can_be_added_to_an_instance(config_file: Path, tmp_path: Path) -> None:
    mod = _mod_dir(tmp_path / "w", "@Solo")
    document = settings.read_document(config_file)
    settings.add_local_mod(document, "Solo", mod)
    settings.add_mod_to_instance(document, "chernarus", "Solo")
    settings.save(config_file, document)

    assert load_config(config_file).instances["chernarus"].mods == ["Solo"]


def test_the_same_folder_twice_is_refused(config_file: Path, tmp_path: Path) -> None:
    """Adding one directory under two names would load the same mod twice."""
    mod = _mod_dir(tmp_path / "w", "@Solo")
    document = settings.read_document(config_file)
    settings.add_local_mod(document, "Solo", mod)

    with pytest.raises(settings.SettingsError, match="already configured as 'Solo'"):
        settings.add_local_mod(document, "SoloAgain", mod)


def test_a_duplicate_name_is_refused(config_file: Path, tmp_path: Path) -> None:
    document = settings.read_document(config_file)
    settings.add_local_mod(document, "Solo", _mod_dir(tmp_path / "a", "@One"))

    with pytest.raises(settings.SettingsError, match="already configured"):
        settings.add_local_mod(document, "Solo", _mod_dir(tmp_path / "b", "@Two"))


def test_nothing_is_copied(config_file: Path, tmp_path: Path) -> None:
    """The mod stays where it is; dzsm only records the path."""
    mod = _mod_dir(tmp_path / "w", "@Solo")
    document = settings.read_document(config_file)
    settings.add_local_mod(document, "Solo", mod)
    settings.save(config_file, document)

    config = load_config(config_file)
    assert (mod / "addons" / "data.pbo").exists()
    assert not (config.modcache_dir / "Solo").exists()


def test_comments_survive(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "# keep me\n" + CONFIG.format(root=tmp_path.as_posix()), encoding="utf-8"
    )
    document = settings.read_document(path)
    settings.add_local_mod(document, "Solo", _mod_dir(tmp_path / "w", "@Solo"))
    settings.save(path, document)

    assert "# keep me" in path.read_text(encoding="utf-8")


def test_a_local_mod_resolves_for_launching(config_file: Path, tmp_path: Path) -> None:
    """The point of adding it: it has to come out as a real path in -mod=."""
    from dzsm.mods import mod_paths

    mod = _mod_dir(tmp_path / "w", "@Solo")
    document = settings.read_document(config_file)
    settings.add_local_mod(document, "Solo", mod)
    settings.add_mod_to_instance(document, "chernarus", "Solo")
    config: Config = settings.save(config_file, document)

    assert mod_paths(config, "chernarus", {}) == [mod.resolve()]
