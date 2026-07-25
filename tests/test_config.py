"""Config validation tests.

These are the cheap guards on things that are expensive to discover at runtime: a mod
name that doesn't resolve, two instances on one port, a root path that will blow past
MAX_PATH once version directories are appended.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from dzsm.config import Config, InstanceConfig, LocalMod, SteamConfig, WorkshopMod


def _steam() -> SteamConfig:
    return SteamConfig(username="someone", steamcmd_path=Path(r"D:\dz\steamcmd\steamcmd.exe"))


def test_unknown_mod_reference_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown mod"):
        Config(
            root=Path(r"D:\dz"),
            steam=_steam(),
            mods={"CF": WorkshopMod(workshop_id=1559212036)},
            instances={"chernarus": InstanceConfig(mods=["CF", "Typo"])},
        )


def test_duplicate_ports_are_rejected() -> None:
    with pytest.raises(ValidationError, match="port"):
        Config(
            root=Path(r"D:\dz"),
            steam=_steam(),
            mods={},
            instances={
                "a": InstanceConfig(port=2302, mods=[]),
                "b": InstanceConfig(port=2302, mods=[]),
            },
        )


def test_duplicate_mod_in_one_instance_is_rejected() -> None:
    with pytest.raises(ValidationError, match="more than once"):
        Config(
            root=Path(r"D:\dz"),
            steam=_steam(),
            mods={"CF": WorkshopMod(workshop_id=1559212036)},
            instances={"chernarus": InstanceConfig(mods=["CF", "CF"])},
        )


def test_relative_root_is_rejected() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        Config(root=Path("dz"), steam=_steam(), mods={}, instances={})


def test_version_dir_layout() -> None:
    cfg = Config(root=Path(r"D:\dz"), steam=_steam(), mods={}, instances={})
    assert cfg.version_dir(1559212036, 1721005200).as_posix().endswith(
        "dz/modcache/1559212036/1721005200"
    )


def test_load_order_is_preserved_not_sorted() -> None:
    """Load order is the declared order. Framework mods must stay in front."""
    instance = InstanceConfig(mods=["CF", "Aaa", "Expansion"])
    assert instance.mods == ["CF", "Aaa", "Expansion"]


def test_local_mods_are_pinned() -> None:
    mod = LocalMod(path=Path(r"D:\dz\localmods\@MyTestMod"))
    assert mod.pinned is True


def test_long_root_is_rejected() -> None:
    """Unconditional, even where Win32 long paths are enabled — the flag only helps
    processes whose manifest opts in, and the DayZ server binary's does not."""
    long_root = Path(r"C:\Users\somebody\Documents\DayZ Server Manager\instances\root")
    with pytest.raises(ValidationError, match="MAX_PATH"):
        Config(root=long_root, steam=_steam(), mods={}, instances={})


def test_short_root_is_accepted() -> None:
    cfg = Config(root=Path(r"D:\dz"), steam=_steam(), mods={}, instances={})
    assert cfg.root == Path(r"D:\dz")


def test_password_comes_from_the_environment_not_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steam = _steam()
    monkeypatch.delenv(steam.password_env, raising=False)
    assert steam.password() is None
    monkeypatch.setenv(steam.password_env, "hunter2")
    assert steam.password() == "hunter2"
