"""Backups of an instance's world.

The mission directory holds `storage_<instanceId>/` — every base, stash and vehicle
players have built — alongside the economy someone tuned. Losing it loses other
people's work, and nothing was copying it anywhere.

Two things make this less obvious than it sounds, and both are tested here: the world
lives inside `serverapp/<buildid>/`, so it moves at every server update, and a restore
has to replace a directory that already exists, which is the one case promote_tree
cannot do.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dzsm import backup
from dzsm.config import Config, InstanceConfig
from dzsm.state import State

MISSION = "sigma_chernarus.chernarusplus"


@pytest.fixture
def cfg(config: Config) -> Config:
    return config.model_copy(
        update={"instances": {"chernarus": InstanceConfig(port=2302, mods=[])}}
    )


@pytest.fixture
def world(cfg: Config) -> Path:
    """A server build with a mission the instance's serverDZ.cfg points at."""
    mission = cfg.serverapp_dir / "25963285" / "mpmissions" / MISSION
    (mission / "storage_1").mkdir(parents=True)
    (mission / "storage_1" / "players.db").write_bytes(b"a base someone built")
    (mission / "db").mkdir()
    (mission / "db" / "types.xml").write_text("<types/>", encoding="utf-8")
    (mission / "init.c").write_text("// mission", encoding="utf-8")

    instance_dir = cfg.instance_dir("chernarus")
    instance_dir.mkdir(parents=True, exist_ok=True)
    (instance_dir / "serverDZ.cfg").write_text(
        f'hostname = "t";\ntemplate="{MISSION}";\n', encoding="utf-8"
    )
    return mission


@pytest.fixture
def state(cfg: Config) -> State:
    s = State()
    s.serverapp_version = "25963285"
    return s


# --- finding the world --------------------------------------------------------


def test_the_mission_is_found_through_the_instance_config(
    cfg: Config, state: State, world: Path
) -> None:
    """Asked of serverDZ.cfg rather than re-derived: dzsm owns that `template` line and
    keeps it pointed at the resolved mission, which is what stays honest when someone
    switches map."""
    assert backup.current_mission_dir(cfg, state, "chernarus") == world


def test_an_instance_that_has_never_started_has_no_world(cfg: Config, state: State) -> None:
    assert backup.current_mission_dir(cfg, state, "chernarus") is None


# --- taking one ---------------------------------------------------------------


def test_the_whole_mission_is_copied(cfg: Config, state: State, world: Path) -> None:
    """Not just storage_*: db/, the cfg*.json files and types.xml are the economy, and
    restoring a world without them restores half of it."""
    taken = backup.take(cfg, state, "chernarus")

    assert taken is not None
    copied = taken.path / MISSION
    assert (copied / "storage_1" / "players.db").read_bytes() == b"a base someone built"
    assert (copied / "db" / "types.xml").is_file()
    assert (copied / "init.c").is_file()


def test_backups_live_outside_the_server_build(
    cfg: Config, state: State, world: Path
) -> None:
    """The world is inside serverapp/<buildid>/, so it moves at every server update and
    goes entirely when that build is pruned. A backup kept in there would be deleted by
    the thing it exists to protect against."""
    taken = backup.take(cfg, state, "chernarus")

    assert taken is not None
    assert cfg.serverapp_dir not in taken.path.parents
    assert taken.path.is_relative_to(cfg.root / "backups")


def test_nothing_to_back_up_is_not_a_failure(cfg: Config, state: State) -> None:
    """An instance that has never started has no world to lose, and failing a restart
    over that would be absurd."""
    assert backup.take(cfg, state, "chernarus") is None


def test_a_half_copy_is_not_listed(cfg: Config, state: State, world: Path) -> None:
    """A directory with no marker was interrupted, and offering it for restore would
    hand someone an incomplete world believing it was whole."""
    taken = backup.take(cfg, state, "chernarus")
    assert taken is not None
    (taken.path / backup.MARKER).unlink()

    assert backup.list_backups(cfg, "chernarus") == []


# --- retention ----------------------------------------------------------------


def test_only_the_newest_are_kept(cfg: Config, state: State, world: Path) -> None:
    for stamp in ("2026-10-01-0100", "2026-10-02-0100", "2026-10-03-0100"):
        made = backup.backups_dir(cfg, "chernarus") / stamp / MISSION
        made.mkdir(parents=True)
        (made.parent / backup.MARKER).write_text(MISSION, encoding="utf-8")

    backup.prune(cfg, "chernarus", keep=2)

    assert [b.stamp for b in backup.list_backups(cfg, "chernarus")] == [
        "2026-10-03-0100",
        "2026-10-02-0100",
    ]


def test_keeping_zero_keeps_everything(cfg: Config, state: State, world: Path) -> None:
    """A retention number nobody has thought about must not be the thing that deletes
    someone's last copy — the stance gc_retention_days already takes."""
    backup.take(cfg, state, "chernarus")

    assert backup.prune(cfg, "chernarus", keep=0) == []
    assert len(backup.list_backups(cfg, "chernarus")) == 1


# --- restoring ----------------------------------------------------------------


def test_a_restore_replaces_a_world_that_is_there(
    cfg: Config, state: State, world: Path
) -> None:
    """The case promote_tree cannot do on its own: Windows will not rename a directory
    over one that already exists, and at restore time it always does."""
    taken = backup.take(cfg, state, "chernarus")
    assert taken is not None
    (world / "storage_1" / "players.db").write_bytes(b"ruined")
    (world / "init.c").unlink()

    backup.restore(cfg, state, "chernarus", taken.stamp)

    assert (world / "storage_1" / "players.db").read_bytes() == b"a base someone built"
    assert (world / "init.c").is_file()


def test_the_replaced_world_is_kept(cfg: Config, state: State, world: Path) -> None:
    """Restoring the wrong copy is an easy mistake and otherwise an unrecoverable one."""
    first = backup.take(cfg, state, "chernarus")
    assert first is not None
    (world / "storage_1" / "players.db").write_bytes(b"what was there before the restore")

    backup.restore(cfg, state, "chernarus", first.stamp)

    kept = [b for b in backup.list_backups(cfg, "chernarus") if b.stamp != first.stamp]
    assert kept, "the world that was replaced was not kept"
    saved = kept[0].path / MISSION / "storage_1" / "players.db"
    assert saved.read_bytes() == b"what was there before the restore"


def test_a_restore_leaves_no_staging_directories(
    cfg: Config, state: State, world: Path
) -> None:
    taken = backup.take(cfg, state, "chernarus")
    assert taken is not None

    backup.restore(cfg, state, "chernarus", taken.stamp)

    leftovers = [p.name for p in world.parent.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_restoring_something_that_is_not_there_says_what_is(
    cfg: Config, state: State, world: Path
) -> None:
    taken = backup.take(cfg, state, "chernarus")
    assert taken is not None

    with pytest.raises(backup.BackupError, match=taken.stamp):
        backup.restore(cfg, state, "chernarus", "1999-01-01-0000")


def test_a_failed_replace_leaves_the_world_alone(
    cfg: Config, state: State, world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one outcome that must never happen is an instance with no world at all."""
    taken = backup.take(cfg, state, "chernarus")
    assert taken is not None
    (world / "storage_1" / "players.db").write_bytes(b"current")

    real_rename = Path.rename
    calls = {"n": 0}

    def fail_second(self: Path, target: Path) -> Path:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(13, "denied")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_second)

    with pytest.raises(backup.BackupError, match="could not replace"):
        backup.restore(cfg, state, "chernarus", taken.stamp)

    assert (world / "storage_1" / "players.db").read_bytes() == b"current"


def test_two_backups_in_the_same_second_both_survive(
    cfg: Config, state: State, world: Path
) -> None:
    """A restore takes one to save the world it is about to replace. At minute
    resolution that collided with the backup taken moments earlier and the save was
    silently skipped — losing the world, at the one moment it mattered most."""
    first = backup.take(cfg, state, "chernarus")
    (world / "storage_1" / "players.db").write_bytes(b"changed since")
    second = backup.take(cfg, state, "chernarus")

    assert first is not None and second is not None
    assert first.stamp != second.stamp
    assert len(backup.list_backups(cfg, "chernarus")) == 2
    saved = second.path / MISSION / "storage_1" / "players.db"
    assert saved.read_bytes() == b"changed since"
