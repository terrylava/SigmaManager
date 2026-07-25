"""Launch-argument construction tests.

The -mod= string is the entire activation mechanism, so its shape is worth pinning down:
absolute paths, semicolon separated, declared order, trailing semicolon, and argv as a
list rather than a shell string.
"""

from __future__ import annotations

from pathlib import Path

from dzsm.config import Config, InstanceConfig, SteamConfig
from dzsm.instance import build_launch_args, build_mod_argument


def _config() -> Config:
    return Config(
        root=Path(r"D:\dz"),
        steam=SteamConfig(
            username="someone",
            steamcmd_path=Path(r"D:\dz\steamcmd\steamcmd.exe"),
        ),
        mods={},
        instances={},
    )


def test_mod_argument_is_semicolon_separated_with_trailing_semicolon() -> None:
    arg = build_mod_argument([Path(r"D:\dz\modcache\1\2"), Path(r"D:\dz\localmods\@Test")])
    assert arg.startswith("-mod=")
    assert arg.endswith(";")
    assert arg.count(";") == 2


def test_mod_argument_preserves_declared_order() -> None:
    """Load order is the semicolon order. Never sorted."""
    paths = [
        Path(r"D:\dz\localmods\@Zulu"),
        Path(r"D:\dz\localmods\@Alpha"),
        Path(r"D:\dz\localmods\@Mike"),
    ]
    entries = build_mod_argument(paths).removeprefix("-mod=").rstrip(";").split(";")
    assert entries == [str(p) for p in paths]


def test_mod_argument_keeps_absolute_windows_paths_intact() -> None:
    """Drive letters and backslashes survive verbatim — no quoting, no escaping."""
    arg = build_mod_argument([Path(r"D:\dz\modcache\1559212036\1721005200")])
    assert r"D:\dz\modcache\1559212036\1721005200" in arg


def test_empty_mod_list_yields_a_bare_argument() -> None:
    assert build_mod_argument([]) == "-mod=;"


def test_launch_args_is_a_list_of_separate_arguments() -> None:
    """argv list, never a shell string — this is what makes backslashes safe."""
    args = build_launch_args(
        _config(),
        "chernarus",
        InstanceConfig(port=2302, mods=[]),
        Path(r"D:\dz\serverapp\1\DayZServer_x64.exe"),
        [Path(r"D:\dz\modcache\1559212036\1721005200")],
    )
    assert isinstance(args, list)
    assert all(isinstance(a, str) for a in args)
    assert "-port=2302" in args
    assert any(a.startswith("-mod=") for a in args)
    assert "-freezecheck" in args


def test_profiles_points_inside_the_instance_directory() -> None:
    args = build_launch_args(
        _config(),
        "chernarus",
        InstanceConfig(port=2302, mods=[]),
        Path(r"D:\dz\serverapp\1\DayZServer_x64.exe"),
        [],
    )
    profiles = next(a for a in args if a.startswith("-profiles="))
    assert profiles.endswith(str(Path(r"instances\chernarus\profiles")))


def test_extra_args_are_appended() -> None:
    args = build_launch_args(
        _config(),
        "chernarus",
        InstanceConfig(port=2302, mods=[], extra_args=["-cpuCount=4"]),
        Path(r"D:\dz\serverapp\1\DayZServer_x64.exe"),
        [],
    )
    assert args[-1] == "-cpuCount=4"
