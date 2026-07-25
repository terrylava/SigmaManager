"""Server app download and buildid versioning.

steamcmd is replaced throughout — the real thing downloads several GB. What is exercised
here is the version identity (buildid from the app manifest) and the same refusal to
trust a "success" that produced nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dzsm import steam
from dzsm.config import SERVER_APPID, Config
from dzsm.mods import promote_tree
from dzsm.steam import SteamCmdRun, SteamError, update_server_app

MANIFEST = f"""
"AppState"
{{
    "appid"     "{SERVER_APPID}"
    "name"      "DayZ Server"
    "buildid"   "18446744"
}}
"""


def _fake_run(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    *,
    returncode: int = 0,
    output: str = "Success! App '223350' fully installed.",
    manifest: str | None = MANIFEST,
    executable: bool = True,
) -> list[list[str]]:
    """Replace steamcmd with something that lays down what a real run would."""
    captured: list[list[str]] = []

    def run(steam_config: object, args: list[str], **kwargs: object) -> SteamCmdRun:
        captured.append(args)
        download = config.serverapp_download_dir
        if manifest is not None:
            path = download / "steamapps" / f"appmanifest_{SERVER_APPID}.acf"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(manifest, encoding="utf-8")
        if executable:
            download.mkdir(parents=True, exist_ok=True)
            for name in ("DayZServer_x64.exe", "DayZServer"):
                (download / name).write_bytes(b"MZ")
        return SteamCmdRun(returncode=returncode, output=output)

    monkeypatch.setattr(steam, "_run_steamcmd", run)
    return captured


def test_returns_the_buildid(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch, config)
    assert update_server_app(config) == "18446744"


def test_force_install_dir_precedes_app_update(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """steamcmd ignores force_install_dir if it comes after the app_update."""
    captured = _fake_run(monkeypatch, config)
    update_server_app(config)

    args = captured[0]
    assert args.index("+force_install_dir") < args.index("+app_update")
    assert args[args.index("+force_install_dir") + 1] == str(config.serverapp_download_dir)


def test_validate_is_opt_in(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _fake_run(monkeypatch, config)

    update_server_app(config)
    assert "validate" not in captured[0]

    update_server_app(config, validate=True)
    assert "validate" in captured[1]


def test_missing_manifest_is_a_failure(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit 0 with no manifest is the same lie steamcmd tells about mods."""
    _fake_run(monkeypatch, config, manifest=None)
    with pytest.raises(SteamError, match="no buildid"):
        update_server_app(config)


def test_unparseable_manifest_is_a_failure(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_run(monkeypatch, config, manifest='"AppState" { "name" "DayZ Server" }')
    with pytest.raises(SteamError, match="no buildid"):
        update_server_app(config)


def test_missing_executable_is_a_failure(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_run(monkeypatch, config, executable=False)
    with pytest.raises(SteamError, match="missing after the run"):
        update_server_app(config)


def test_error_marker_in_output_is_a_failure(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_run(monkeypatch, config, output="ERROR! Failed to install app '223350'")
    with pytest.raises(SteamError, match="steamcmd reported"):
        update_server_app(config)


def test_nonzero_exit_is_a_failure(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch, config, returncode=3)
    with pytest.raises(SteamError, match="exited 3"):
        update_server_app(config)


def test_self_update_exit_is_accepted(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit 7 means steamcmd replaced its own binary, not that the update failed."""
    _fake_run(monkeypatch, config, returncode=7)
    assert update_server_app(config) == "18446744"


# --- promotion ---------------------------------------------------------------


def test_promotion_produces_an_immutable_build_directory(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_run(monkeypatch, config)
    build_id = update_server_app(config)

    destination = config.serverapp_dir / build_id
    promote_tree(config.serverapp_download_dir, destination)

    assert (destination / "DayZServer_x64.exe").exists()
    assert config.serverapp_download_dir.is_dir(), "tier 1 stays put for the next update"


def test_two_builds_coexist(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same reason mods are versioned: a running server locks its own binaries."""
    _fake_run(monkeypatch, config)
    build_id = update_server_app(config)
    old = config.serverapp_dir / build_id
    promote_tree(config.serverapp_download_dir, old)

    (config.serverapp_download_dir / "DayZServer_x64.exe").write_bytes(b"MZ-newer")
    new = config.serverapp_dir / "18446745"
    promote_tree(config.serverapp_download_dir, new)

    assert (old / "DayZServer_x64.exe").read_bytes() == b"MZ"
    assert (new / "DayZServer_x64.exe").read_bytes() == b"MZ-newer"


def test_promotion_leaves_no_staging_directory(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_run(monkeypatch, config)
    update_server_app(config)
    promote_tree(config.serverapp_download_dir, config.serverapp_dir / "18446744")

    leftovers = [p.name for p in config.serverapp_dir.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_download_dir_is_outside_serverapp(config: Config) -> None:
    """Tier 1 must never be mistakable for a version directory."""
    assert config.serverapp_download_dir.parent == config.root
    assert config.serverapp_download_dir.name not in {
        p.name for p in [config.serverapp_dir]
    }


def test_buildid_parsing_handles_real_manifest_shape(tmp_path: Path) -> None:
    manifest = tmp_path / "appmanifest.acf"
    manifest.write_text(MANIFEST, encoding="utf-8")
    assert steam._read_buildid(manifest) == "18446744"


def test_buildid_of_a_missing_manifest_is_none(tmp_path: Path) -> None:
    assert steam._read_buildid(tmp_path / "nope.acf") is None
