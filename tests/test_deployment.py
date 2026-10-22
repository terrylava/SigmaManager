"""Files the binary carries, and the version it reports.

A bare exe copied to a fresh server has no source tree, so the scheduled-task launcher,
its installer, the notes and the example config have to travel inside it. `dzsm init`
already solved exactly this for config.toml.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

import dzsm
from dzsm import deployment
from dzsm.cli import app

runner = CliRunner()


# --- the version -------------------------------------------------------------


def test_the_version_is_reported() -> None:
    """A binary that cannot say what it is cannot be updated safely, and nobody can tell
    you which build their problem is on."""
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert dzsm.__version__ in result.stdout


def test_the_packaging_version_cannot_drift_from_the_code() -> None:
    """These were already apart — pyproject said 0.1.0 while the newest tag was v0.1.1.
    An updater comparing its own version against a release would have believed it."""
    pyproject = tomllib.loads(
        (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert pyproject["project"].get("version") is None, "version must stay dynamic"
    assert pyproject["tool"]["hatch"]["version"]["path"] == "dzsm/__init__.py"


# --- the carried files ---------------------------------------------------------


def test_every_carried_file_exists_in_this_build() -> None:
    """A build made without the data files in dzsm.spec would otherwise only fail on
    someone's server, at the moment they were trying to set it up."""
    for name in deployment.DEPLOY_FILES:
        assert deployment.bundled_path(name).is_file(), name


def test_the_spec_carries_what_deployment_expects() -> None:
    """The list lives in two places — Python and the PyInstaller spec — and only this
    stops them drifting."""
    spec = (Path(__file__).parent.parent / "dzsm.spec").read_text(encoding="utf-8")

    for name in deployment.DEPLOY_FILES:
        assert name in spec, f"{name} is not in dzsm.spec datas"


def test_the_files_are_written(tmp_path: Path) -> None:
    written, skipped = deployment.write_files(tmp_path)

    assert skipped == []
    assert sorted(p.name for p in written) == [
        "DEPLOY-README.md",
        "config.example.toml",
        "dzsm-daemon.cmd",
        "install-task.ps1",
    ]


def test_they_land_flat_rather_than_in_a_subdirectory(tmp_path: Path) -> None:
    """Someone deploying wants four files in one folder, not a deploy/ to cd into."""
    deployment.write_files(tmp_path)

    assert not (tmp_path / "deploy").exists()
    assert (tmp_path / "dzsm-daemon.cmd").is_file()


def test_an_existing_file_is_never_clobbered(tmp_path: Path) -> None:
    """The launcher is where the Steam key and the dashboard token are written by hand.
    Replacing it during an upgrade would wipe both without a word."""
    launcher = tmp_path / "dzsm-daemon.cmd"
    launcher.write_text('set "DZSM_STEAM_API_KEY=mine"', encoding="utf-8")

    written, skipped = deployment.write_files(tmp_path)

    assert launcher in skipped
    assert launcher not in written
    assert launcher.read_text(encoding="utf-8") == 'set "DZSM_STEAM_API_KEY=mine"'


def test_force_replaces_it(tmp_path: Path) -> None:
    launcher = tmp_path / "dzsm-daemon.cmd"
    launcher.write_text("old", encoding="utf-8")

    written, skipped = deployment.write_files(tmp_path, force=True)

    assert launcher in written
    assert skipped == []
    assert "DZSM_HOME" in launcher.read_text(encoding="utf-8")


def test_a_missing_directory_is_created(tmp_path: Path) -> None:
    target = tmp_path / "new" / "place"

    deployment.write_files(target)

    assert (target / "install-task.ps1").is_file()


def test_a_build_missing_a_carried_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writing three of four files would leave someone with a deployment that looks
    complete (Rule 3)."""
    monkeypatch.setattr(deployment, "bundled_path", lambda name: tmp_path / "nope")

    with pytest.raises(deployment.DeploymentError, match="built without the data files"):
        deployment.write_files(tmp_path)


def test_the_command_reports_what_it_did(tmp_path: Path) -> None:
    result = runner.invoke(app, ["deploy", str(tmp_path)])

    assert result.exit_code == 0
    assert "install-task.ps1" in result.stdout
    assert "powershell" in result.stdout
