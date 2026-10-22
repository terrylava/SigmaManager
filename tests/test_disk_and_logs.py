"""Refusing a big operation on a nearly full disk, and not keeping logs forever.

Everything large lands under `root`: the mod cache, server builds of several GB, a
3.9 GB isolated copy per instance, the backups. Filling that volume does not fail one
download — it takes down every server on the box, and a DayZ server that cannot write
its persistence loses the world it was holding.

The logs are the other half. A live instance had 47 of them after a few days, one set
per start, with nothing to remove them.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from dzsm.config import Config, InstanceConfig
from dzsm.instance import prune_logs
from dzsm.mods import ModCacheError, free_space, require_space

GIB = 1024 * 1024 * 1024


@pytest.fixture
def cfg(config: Config) -> Config:
    return config.model_copy(
        update={"instances": {"chernarus": InstanceConfig(port=2302, mods=[])}}
    )


# --- the space guard ----------------------------------------------------------


def test_plenty_of_room_is_allowed(cfg: Config) -> None:
    require_space(cfg, 1024, what="a small thing")


def test_more_than_the_disk_holds_is_refused(cfg: Config) -> None:
    with pytest.raises(ModCacheError, match="not enough disk space"):
        require_space(cfg, 1_000_000 * GIB, what="something absurd")


def test_the_refusal_says_what_and_how_much(cfg: Config) -> None:
    """A number without a unit or a lever leaves someone guessing at what to delete."""
    message = str(
        pytest.raises(
            ModCacheError, require_space, cfg, 1_000_000 * GIB, what="the server app"
        ).value
    )

    assert "the server app" in message
    assert "GiB" in message
    assert "min_free_gb" in message


def test_the_floor_applies_even_to_a_free_operation(cfg: Config) -> None:
    """`needed` is 0 when the size is not known — a mod download, say. The floor is what
    keeps a running server able to write afterwards, so it still has to hold."""
    greedy = cfg.model_copy(update={"min_free_gb": 1_000_000.0})

    with pytest.raises(ModCacheError, match="not enough disk space"):
        require_space(greedy, 0, what="a mod")


def test_the_guard_can_be_turned_off(cfg: Config) -> None:
    """For a small disk someone is managing themselves."""
    off = cfg.model_copy(update={"min_free_gb": 0.0})

    require_space(off, 0, what="a mod")


def test_an_unreadable_volume_does_not_block_anything(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guard that cannot read the disk must not become the thing that stops a
    download (Rule 3 is about saying so, not about refusing)."""
    monkeypatch.setattr("dzsm.mods.free_space", lambda path: 0)

    require_space(cfg, 1_000_000 * GIB, what="something absurd")


def test_free_space_answers_for_a_directory_not_yet_created(tmp_path: Path) -> None:
    """root may not exist yet on a first run; the question is about the volume."""
    assert free_space(tmp_path / "not" / "there" / "yet") > 0


# --- log retention ------------------------------------------------------------


def _log_file(profiles: Path, name: str, *, days_old: float) -> Path:
    path = profiles / name
    path.write_text("x", encoding="utf-8")
    when = time.time() - days_old * 86400
    os.utime(path, (when, when))
    return path


@pytest.fixture
def profiles(cfg: Config) -> Path:
    path = cfg.instance_dir("chernarus") / "profiles"
    path.mkdir(parents=True)
    return path


def test_old_logs_are_removed(cfg: Config, profiles: Path) -> None:
    for n in range(5):
        _log_file(profiles, f"DayZServer_x64_{n}.RPT", days_old=100 - n)

    removed = prune_logs(cfg, "chernarus", older_than_days=30)

    assert len(removed) == 3


def test_the_newest_are_always_kept(cfg: Config, profiles: Path) -> None:
    """However old they are. An instance that has been down for months still needs
    recent context to read when someone comes back to it."""
    for n in range(4):
        _log_file(profiles, f"old_{n}.RPT", days_old=900 + n)

    prune_logs(cfg, "chernarus", older_than_days=30)

    assert len(list(profiles.glob("*.RPT"))) == 2


def test_each_kind_keeps_its_own(cfg: Config, profiles: Path) -> None:
    """An RPT and an ADM answer different questions, so keeping two files in total
    would leave one of them with none."""
    for n in range(4):
        _log_file(profiles, f"a_{n}.RPT", days_old=900)
        _log_file(profiles, f"a_{n}.ADM", days_old=900)

    prune_logs(cfg, "chernarus", older_than_days=30)

    assert len(list(profiles.glob("*.RPT"))) == 2
    assert len(list(profiles.glob("*.ADM"))) == 2


def test_recent_logs_are_left_alone(cfg: Config, profiles: Path) -> None:
    for n in range(5):
        _log_file(profiles, f"recent_{n}.log", days_old=2)

    assert prune_logs(cfg, "chernarus", older_than_days=30) == []


def test_retention_of_zero_keeps_everything(cfg: Config, profiles: Path) -> None:
    for n in range(5):
        _log_file(profiles, f"ancient_{n}.RPT", days_old=5000)

    assert prune_logs(cfg, "chernarus", older_than_days=0) == []
    assert len(list(profiles.glob("*.RPT"))) == 5


def test_only_engine_logs_are_touched(cfg: Config, profiles: Path) -> None:
    """A mod's own files and the BattlEye directory live here too, and deleting
    something dzsm does not understand is not tidying up."""
    _log_file(profiles, "keep_me.json", days_old=900)
    (profiles / "BattlEye").mkdir()
    for n in range(4):
        _log_file(profiles, f"a_{n}.RPT", days_old=900)

    prune_logs(cfg, "chernarus", older_than_days=30)

    assert (profiles / "keep_me.json").is_file()
    assert (profiles / "BattlEye").is_dir()


def test_a_missing_profiles_directory_is_not_an_error(cfg: Config) -> None:
    """An instance that has never started has none."""
    assert prune_logs(cfg, "chernarus", older_than_days=30) == []


def test_a_file_that_cannot_be_deleted_is_skipped(
    cfg: Config, profiles: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Most likely held open by the running server. It becomes a candidate again next
    time, by which point it is closed."""
    for n in range(4):
        _log_file(profiles, f"a_{n}.RPT", days_old=900)

    def refuse(self: Path, **kwargs: object) -> None:
        raise OSError(32, "in use")

    monkeypatch.setattr(Path, "unlink", refuse)

    assert prune_logs(cfg, "chernarus", older_than_days=30) == []
    assert len(list(profiles.glob("*.RPT"))) == 4
