"""Server-only mods (-serverMod=).

These are versioned and activated exactly like client-facing mods; the only difference is
which launch argument they land in. The interesting cases are the boundary between the
two lists and the argument's shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from dzsm.config import Config, InstanceConfig, WorkshopMod
from dzsm.instance import build_launch_args, build_mod_argument
from dzsm.mods import mod_paths, resolve_mod_versions, server_mod_paths
from dzsm.state import State

CLIENT_ID = 1559212036
SERVER_ID = 2116157322
VERSION = 1721005200


@pytest.fixture
def cfg(config: Config) -> Config:
    return config.model_copy(
        update={
            "mods": {
                "CF": WorkshopMod(workshop_id=CLIENT_ID),
                "AdminTools": WorkshopMod(workshop_id=SERVER_ID),
            },
            "instances": {
                "chernarus": InstanceConfig(
                    port=2302, mods=["CF"], server_mods=["AdminTools"]
                )
            },
        }
    )


def _cache(cfg: Config, state: State, workshop_id: int) -> Path:
    path = cfg.version_dir(workshop_id, VERSION)
    path.mkdir(parents=True)
    (path / "mod.pbo").write_bytes(b"x")
    state.record_version(workshop_id, VERSION)
    return path


# --- config validation -------------------------------------------------------


def test_server_mods_must_reference_known_mods(config: Config) -> None:
    with pytest.raises(ValidationError, match="server_mods references unknown mod"):
        config.model_validate(
            {
                **config.model_dump(),
                "mods": {"CF": {"kind": "workshop", "workshop_id": CLIENT_ID}},
                "instances": {"a": {"mods": ["CF"], "server_mods": ["Ghost"]}},
            }
        )


def test_a_mod_cannot_be_in_both_lists(config: Config) -> None:
    """-mod and -serverMod would each load it, and the engine has no reason to."""
    with pytest.raises(ValidationError, match="both mods and server_mods"):
        config.model_validate(
            {
                **config.model_dump(),
                "mods": {"CF": {"kind": "workshop", "workshop_id": CLIENT_ID}},
                "instances": {"a": {"mods": ["CF"], "server_mods": ["CF"]}},
            }
        )


def test_duplicate_server_mods_are_rejected(config: Config) -> None:
    with pytest.raises(ValidationError, match="server_mods lists"):
        config.model_validate(
            {
                **config.model_dump(),
                "mods": {"CF": {"kind": "workshop", "workshop_id": CLIENT_ID}},
                "instances": {"a": {"mods": [], "server_mods": ["CF", "CF"]}},
            }
        )


def test_server_mods_defaults_to_empty() -> None:
    assert InstanceConfig(mods=["CF"]).server_mods == []


# --- resolution --------------------------------------------------------------


def test_server_mods_are_versioned_like_any_other(cfg: Config) -> None:
    state = State()
    _cache(cfg, state, CLIENT_ID)
    _cache(cfg, state, SERVER_ID)

    versions = resolve_mod_versions(cfg, state, "chernarus")

    assert versions == {"CF": VERSION, "AdminTools": VERSION}


def test_an_uncached_server_mod_blocks_the_start(cfg: Config) -> None:
    state = State()
    _cache(cfg, state, CLIENT_ID)

    with pytest.raises(Exception, match="no cached version"):
        resolve_mod_versions(cfg, state, "chernarus")


def test_the_two_lists_resolve_separately(cfg: Config) -> None:
    state = State()
    client = _cache(cfg, state, CLIENT_ID)
    server = _cache(cfg, state, SERVER_ID)
    versions = {"CF": VERSION, "AdminTools": VERSION}

    assert mod_paths(cfg, "chernarus", versions) == [client]
    assert server_mod_paths(cfg, "chernarus", versions) == [server]


# --- launch arguments --------------------------------------------------------


def test_server_mod_argument_shape() -> None:
    arg = build_mod_argument([Path(r"D:\dz\modcache\1\2")], option="serverMod")

    assert arg.startswith("-serverMod=")
    assert arg.endswith(";")


def test_launch_args_include_both_options(cfg: Config) -> None:
    args = build_launch_args(
        cfg,
        "chernarus",
        cfg.instances["chernarus"],
        Path(r"D:\dz\serverapp\1\DayZServer_x64.exe"),
        [Path(r"D:\dz\modcache\1\2")],
        [Path(r"D:\dz\modcache\3\4")],
    )

    assert any(a.startswith("-mod=") for a in args)
    assert any(a.startswith("-serverMod=") for a in args)


def test_server_mod_is_omitted_when_there_are_none(cfg: Config) -> None:
    """An empty -serverMod= is harmless but says nothing."""
    args = build_launch_args(
        cfg,
        "chernarus",
        cfg.instances["chernarus"],
        Path(r"D:\dz\serverapp\1\DayZServer_x64.exe"),
        [Path(r"D:\dz\modcache\1\2")],
        [],
    )

    assert not any(a.startswith("-serverMod=") for a in args)


def test_server_mod_defaults_to_omitted(cfg: Config) -> None:
    """Callers that predate server mods keep working unchanged."""
    args = build_launch_args(
        cfg,
        "chernarus",
        cfg.instances["chernarus"],
        Path(r"D:\dz\serverapp\1\DayZServer_x64.exe"),
        [Path(r"D:\dz\modcache\1\2")],
    )

    assert not any(a.startswith("-serverMod=") for a in args)


def test_server_mod_order_is_preserved(cfg: Config) -> None:
    paths = [Path(r"D:\dz\z"), Path(r"D:\dz\a")]
    entries = (
        build_mod_argument(paths, option="serverMod")
        .removeprefix("-serverMod=")
        .rstrip(";")
        .split(";")
    )

    assert entries == [str(p) for p in paths]


def test_client_mods_still_precede_profiles(cfg: Config) -> None:
    """-serverMod slots between -mod and -profiles, matching Bohemia's own examples."""
    args = build_launch_args(
        cfg,
        "chernarus",
        cfg.instances["chernarus"],
        Path(r"D:\dz\serverapp\1\DayZServer_x64.exe"),
        [Path(r"D:\dz\modcache\1\2")],
        [Path(r"D:\dz\modcache\3\4")],
    )
    positions = {a.split("=")[0]: i for i, a in enumerate(args) if "=" in a}

    assert positions["-mod"] < positions["-serverMod"] < positions["-profiles"]
