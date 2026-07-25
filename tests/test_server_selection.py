"""Per-instance server build selection and full isolation.

Two capabilities: pinning an instance to a specific downloaded build, and giving it a
private copy of the server. Together they let one box run several servers on different
builds with no shared state — which is the point of versioning the server app at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from dzsm.config import Config, InstanceConfig
from dzsm.instance import (
    BUILD_MARKER,
    EXTERNAL_BUILD,
    InstanceError,
    prepare_server_directory,
    resolve_server_build,
    server_executable,
)
from dzsm.state import State

OLD_BUILD = "24041098"
NEW_BUILD = "24555555"
EXE = "DayZServer_x64.exe" if sys.platform == "win32" else "DayZServer"


def _build(config: Config, build: str, payload: bytes = b"MZ") -> Path:
    path = config.serverapp_dir / build
    path.mkdir(parents=True)
    (path / EXE).write_bytes(payload)
    (path / "serverDZ.cfg").write_text("hostname = 'x';", encoding="utf-8")
    (path / "keys").mkdir()
    (path / "keys" / "dayz.bikey").write_bytes(b"vanilla")
    return path


def _with(
    config: Config,
    *,
    server_build: str | None = None,
    isolated_server: bool = False,
) -> Config:
    instance = InstanceConfig(
        port=2302,
        mods=[],
        server_build=server_build,
        isolated_server=isolated_server,
    )
    return config.model_copy(update={"instances": {"chernarus": instance}})


# --- build resolution --------------------------------------------------------


def test_defaults_to_the_newest_downloaded_build(config: Config) -> None:
    _build(config, OLD_BUILD)
    _build(config, NEW_BUILD)
    state = State()
    state.serverapp_version = NEW_BUILD

    path, build = resolve_server_build(_with(config), state, "chernarus")

    assert build == NEW_BUILD
    assert path == config.serverapp_dir / NEW_BUILD


def test_an_instance_can_pin_an_older_build(config: Config) -> None:
    """Hold production on a known-good build while something else tests a newer one."""
    _build(config, OLD_BUILD)
    _build(config, NEW_BUILD)
    state = State()
    state.serverapp_version = NEW_BUILD

    path, build = resolve_server_build(_with(config, server_build=OLD_BUILD), state, "chernarus")

    assert build == OLD_BUILD
    assert path == config.serverapp_dir / OLD_BUILD


def test_a_pin_to_a_missing_build_lists_what_exists(config: Config) -> None:
    _build(config, OLD_BUILD)
    state = State()
    state.serverapp_version = OLD_BUILD

    with pytest.raises(InstanceError, match=f"Available: {OLD_BUILD}"):
        resolve_server_build(_with(config, server_build="99999"), state, "chernarus")


def test_instance_pin_beats_the_global_external_path(config: Config, tmp_path: Path) -> None:
    """The pin is the more specific statement of intent."""
    _build(config, OLD_BUILD)
    external = tmp_path / "steam-server"
    external.mkdir()
    cfg = _with(config, server_build=OLD_BUILD).model_copy(
        update={"server_app_path": external}
    )

    _, build = resolve_server_build(cfg, State(), "chernarus")

    assert build == OLD_BUILD


def test_external_install_is_used_when_nothing_is_pinned(
    config: Config, tmp_path: Path
) -> None:
    external = tmp_path / "steam-server"
    external.mkdir()
    cfg = _with(config).model_copy(update={"server_app_path": external})

    path, build = resolve_server_build(cfg, State(), "chernarus")

    assert build == EXTERNAL_BUILD
    assert path == external


def test_no_server_at_all_is_an_actionable_error(config: Config) -> None:
    with pytest.raises(InstanceError, match="dzsm server update"):
        resolve_server_build(_with(config), State(), "chernarus")


# --- shared (default) --------------------------------------------------------


def test_shared_instances_run_from_the_build_directory(config: Config) -> None:
    _build(config, OLD_BUILD)
    state = State()
    state.serverapp_version = OLD_BUILD

    path, _ = prepare_server_directory(_with(config), state, "chernarus")

    assert path == config.serverapp_dir / OLD_BUILD
    assert not (config.instance_dir("chernarus") / "server").exists()


# --- isolation ---------------------------------------------------------------


def test_isolated_instance_gets_its_own_copy(config: Config) -> None:
    _build(config, OLD_BUILD)
    state = State()
    state.serverapp_version = OLD_BUILD

    path, build = prepare_server_directory(_with(config, isolated_server=True), state, "chernarus")

    assert path == config.instance_dir("chernarus") / "server"
    assert (path / EXE).read_bytes() == b"MZ"
    assert (path / BUILD_MARKER).read_text(encoding="utf-8") == build


def test_isolated_copies_do_not_share_keys(config: Config) -> None:
    """The om.py property: a mod trusted on one instance is not trusted on all."""
    _build(config, OLD_BUILD)
    state = State()
    state.serverapp_version = OLD_BUILD
    cfg = config.model_copy(
        update={
            "instances": {
                "a": InstanceConfig(port=2302, mods=[], isolated_server=True),
                "b": InstanceConfig(port=2402, mods=[], isolated_server=True),
            }
        }
    )

    first, _ = prepare_server_directory(cfg, state, "a")
    second, _ = prepare_server_directory(cfg, state, "b")
    (first / "keys" / "only_a.bikey").write_bytes(b"x")

    assert first != second
    assert not (second / "keys" / "only_a.bikey").exists()


def test_the_copy_is_reused_when_the_build_is_unchanged(config: Config) -> None:
    _build(config, OLD_BUILD)
    state = State()
    state.serverapp_version = OLD_BUILD
    cfg = _with(config, isolated_server=True)

    path, _ = prepare_server_directory(cfg, state, "chernarus")
    (path / "marker.txt").write_bytes(b"survived")
    again, _ = prepare_server_directory(cfg, state, "chernarus")

    assert again == path
    assert (path / "marker.txt").exists(), "a few GB should not be recopied for nothing"


def test_changing_the_build_replaces_the_copy(config: Config) -> None:
    _build(config, OLD_BUILD, payload=b"MZ-old")
    _build(config, NEW_BUILD, payload=b"MZ-new")
    state = State()
    state.serverapp_version = OLD_BUILD

    path, _ = prepare_server_directory(_with(config, isolated_server=True), state, "chernarus")
    assert (path / EXE).read_bytes() == b"MZ-old"

    state.serverapp_version = NEW_BUILD
    path, build = prepare_server_directory(_with(config, isolated_server=True), state, "chernarus")

    assert build == NEW_BUILD
    assert (path / EXE).read_bytes() == b"MZ-new"


def test_refresh_forces_a_recopy(config: Config) -> None:
    """External installs are always labelled the same, so dzsm cannot detect an update
    underneath one — refresh is the escape hatch."""
    _build(config, OLD_BUILD)
    state = State()
    state.serverapp_version = OLD_BUILD
    cfg = _with(config, isolated_server=True)

    path, _ = prepare_server_directory(cfg, state, "chernarus")
    (path / "stale.txt").write_bytes(b"should not survive")
    prepare_server_directory(cfg, state, "chernarus", refresh=True)

    assert not (path / "stale.txt").exists()


def test_two_instances_can_run_different_builds(config: Config) -> None:
    """The capability this whole change exists for."""
    _build(config, OLD_BUILD, payload=b"MZ-old")
    _build(config, NEW_BUILD, payload=b"MZ-new")
    state = State()
    state.serverapp_version = NEW_BUILD
    cfg = config.model_copy(
        update={
            "instances": {
                "prod": InstanceConfig(port=2302, mods=[], server_build=OLD_BUILD),
                "test": InstanceConfig(port=2402, mods=[]),
            }
        }
    )

    prod, prod_build = prepare_server_directory(cfg, state, "prod")
    test, test_build = prepare_server_directory(cfg, state, "test")

    assert prod_build == OLD_BUILD
    assert test_build == NEW_BUILD
    assert server_executable(prod).read_bytes() == b"MZ-old"
    assert server_executable(test).read_bytes() == b"MZ-new"
