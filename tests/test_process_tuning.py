"""CPU affinity, priority, launch flags, and which binary to run.

Affinity and priority are applied after spawning because there is no portable way to ask
Popen for them, and neither may ever be fatal: a server on the wrong cores beats a server
that refused to start.
"""

from __future__ import annotations

import sys
from pathlib import Path

import psutil
import pytest

from dzsm import runner
from dzsm.config import Config, InstanceConfig
from dzsm.instance import InstanceError, build_launch_args, server_executable

EXE = "DayZServer_x64.exe" if sys.platform == "win32" else "DayZServer"


def _config(config: Config) -> Config:
    return config.model_copy(
        update={"instances": {"chernarus": InstanceConfig(port=2302, mods=[])}}
    )


def _args(config: Config, instance: InstanceConfig) -> list[str]:
    return build_launch_args(
        config, "chernarus", instance, Path(r"D:\dz\serverapp\1\DayZServer_x64.exe"), []
    )


# --- launch flags ------------------------------------------------------------


def test_logging_flags_are_on_by_default(config: Config) -> None:
    """A server without logs is much harder to diagnose after the fact."""
    args = _args(config, InstanceConfig(port=2302, mods=[]))

    assert {"-dologs", "-adminlog", "-netlog", "-freezecheck"} <= set(args)


def test_a_flag_can_be_turned_off(config: Config) -> None:
    """-netlog in particular is noisy enough that people disable it on a busy server."""
    args = _args(config, InstanceConfig(port=2302, mods=[], netlog=False))

    assert "-netlog" not in args
    assert "-dologs" in args


def test_all_flags_can_be_turned_off(config: Config) -> None:
    instance = InstanceConfig(
        port=2302, mods=[], dologs=False, adminlog=False, netlog=False, freezecheck=False
    )
    args = _args(config, instance)

    assert not any(
        a in {"-dologs", "-adminlog", "-netlog", "-freezecheck"} for a in args
    )


def test_cpu_count_is_omitted_unless_set(config: Config) -> None:
    args = _args(config, InstanceConfig(port=2302, mods=[]))

    assert not any(a.startswith("-cpuCount") for a in args)


def test_cpu_count_is_passed_when_set(config: Config) -> None:
    args = _args(config, InstanceConfig(port=2302, mods=[], cpu_count=4))

    assert "-cpuCount=4" in args


def test_extra_args_still_come_last(config: Config) -> None:
    """So they can override anything dzsm put before them."""
    args = _args(config, InstanceConfig(port=2302, mods=[], extra_args=["-mine"]))

    assert args[-1] == "-mine"


# --- which binary ------------------------------------------------------------


def test_the_default_binary_is_used(tmp_path: Path) -> None:
    (tmp_path / EXE).write_bytes(b"MZ")

    assert server_executable(tmp_path).name == EXE


def test_an_override_selects_a_different_binary(tmp_path: Path) -> None:
    """A diagnostic build ships beside the normal one rather than as its own download."""
    (tmp_path / EXE).write_bytes(b"MZ")
    (tmp_path / "DayZDiag_x64.exe").write_bytes(b"MZ")

    assert server_executable(tmp_path, "DayZDiag_x64.exe").name == "DayZDiag_x64.exe"


def test_a_missing_override_lists_what_is_there(tmp_path: Path) -> None:
    (tmp_path / EXE).write_bytes(b"MZ")

    with pytest.raises(InstanceError, match=EXE):
        server_executable(tmp_path, "DayZDiag_x64.exe")


# --- priority ----------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["idle", "below_normal", "normal", "above_normal", "high", "realtime"]
)
def test_every_priority_name_maps_to_something(name: str) -> None:
    assert isinstance(runner.priority_value(name), int)


@pytest.mark.skipif(sys.platform != "win32", reason="priority classes are Windows")
def test_windows_priorities_map_to_classes() -> None:
    assert runner.priority_value("high") == psutil.HIGH_PRIORITY_CLASS
    assert runner.priority_value("normal") == psutil.NORMAL_PRIORITY_CLASS


@pytest.mark.skipif(sys.platform == "win32", reason="nice values are POSIX")
def test_posix_priorities_invert() -> None:
    """Lower nice is higher priority — the inversion is why config takes a name."""
    assert runner.priority_value("high") < runner.priority_value("normal")
    assert runner.priority_value("idle") > runner.priority_value("normal")


# --- applying it -------------------------------------------------------------


class _FakeProcess:
    def __init__(self) -> None:
        self.cores: list[int] | None = None
        self.niceness: int | None = None
        self.fail = False

    def cpu_affinity(self, cores: list[int]) -> None:
        if self.fail:
            raise psutil.AccessDenied(1)
        self.cores = cores

    def nice(self, value: int) -> None:
        if self.fail:
            raise psutil.AccessDenied(1)
        self.niceness = value


def test_nothing_happens_when_neither_is_configured(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        psutil, "Process", lambda pid: pytest.fail("should not inspect the process")
    )

    runner._apply_process_tuning(InstanceConfig(port=2302, mods=[]), 1, "chernarus")


def test_affinity_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeProcess()
    monkeypatch.setattr(psutil, "Process", lambda pid: fake)
    monkeypatch.setattr(psutil, "cpu_count", lambda: 8)

    runner._apply_process_tuning(
        InstanceConfig(port=2302, mods=[], cpu_affinity=[0, 2, 4]), 1, "chernarus"
    )

    assert fake.cores == [0, 2, 4]


def test_cores_this_machine_does_not_have_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config written for a 16-core box must not fail on a 4-core one."""
    fake = _FakeProcess()
    monkeypatch.setattr(psutil, "Process", lambda pid: fake)
    monkeypatch.setattr(psutil, "cpu_count", lambda: 4)

    runner._apply_process_tuning(
        InstanceConfig(port=2302, mods=[], cpu_affinity=[0, 1, 9, 15]), 1, "chernarus"
    )

    assert fake.cores == [0, 1]


def test_affinity_naming_no_real_core_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinning to nothing would be worse than pinning to everything."""
    fake = _FakeProcess()
    monkeypatch.setattr(psutil, "Process", lambda pid: fake)
    monkeypatch.setattr(psutil, "cpu_count", lambda: 4)

    runner._apply_process_tuning(
        InstanceConfig(port=2302, mods=[], cpu_affinity=[9, 10]), 1, "chernarus"
    )

    assert fake.cores is None


def test_priority_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeProcess()
    monkeypatch.setattr(psutil, "Process", lambda pid: fake)

    runner._apply_process_tuning(
        InstanceConfig(port=2302, mods=[], priority="high"), 1, "chernarus"
    )

    assert fake.niceness == runner.priority_value("high")


def test_a_refused_priority_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raising priority usually needs elevation. A server on normal priority is worth
    far more than a server that refused to start."""
    fake = _FakeProcess()
    fake.fail = True
    monkeypatch.setattr(psutil, "Process", lambda pid: fake)
    monkeypatch.setattr(psutil, "cpu_count", lambda: 8)

    runner._apply_process_tuning(
        InstanceConfig(port=2302, mods=[], priority="realtime", cpu_affinity=[0]),
        1,
        "chernarus",
    )


def test_a_vanished_process_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    def gone(pid: int) -> None:
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(psutil, "Process", gone)

    runner._apply_process_tuning(
        InstanceConfig(port=2302, mods=[], priority="high"), 1, "chernarus"
    )
