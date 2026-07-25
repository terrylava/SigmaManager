"""Shared fixtures.

Every Config here sets allow_long_root: pytest's tmp_path is far past the 40-char root
limit, and that limit is a real production guard we do not want to weaken just to make
tests constructible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dzsm.config import Config, SteamConfig


@pytest.fixture
def config(tmp_path: Path) -> Config:
    steamcmd = tmp_path / "steamcmd" / "steamcmd.exe"
    steamcmd.parent.mkdir(parents=True)
    steamcmd.write_text("", encoding="utf-8")
    return Config(
        root=tmp_path,
        steam=SteamConfig(username="someone", steamcmd_path=steamcmd),
        mods={},
        instances={},
        allow_long_root=True,
    )


@pytest.fixture
def downloaded_mod(config: Config) -> Path:
    """A plausible steamcmd tier-1 download: mod files plus an .acf manifest."""
    mod_dir = config.workshop_download_dir / "1559212036"
    (mod_dir / "addons").mkdir(parents=True)
    (mod_dir / "addons" / "data.pbo").write_bytes(b"pbo-contents")
    (mod_dir / "keys").mkdir()
    (mod_dir / "keys" / "mod.bikey").write_bytes(b"key")
    config.workshop_acf.write_text('"AppWorkshop" { }', encoding="utf-8")
    return mod_dir
