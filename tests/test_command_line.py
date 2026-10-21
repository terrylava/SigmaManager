"""How long the launch command line is, and what happens when it is too long.

Every DayZ guide quotes 8191 characters, which is cmd.exe's limit and binds because the
usual setup launches the server from a .bat file. dzsm spawns the process directly with
an argv list and no shell, so the ceiling here is the OS one — four times higher. That
difference is worth a test, because "go back through a shell" is exactly the kind of
change someone makes for an unrelated reason.
"""

from __future__ import annotations

from pathlib import Path

from dzsm.config import Config, InstanceConfig, WorkshopMod
from dzsm.instance import (
    COMMAND_LINE_LIMIT,
    build_launch_args,
    command_line_length,
)

SEP = "\\"  # one backslash, spelled this way so the file carries no escape puzzles


def test_the_length_is_the_joined_form_not_the_sum() -> None:
    """The limit applies to the single string Windows builds, so that is what is
    measured: arguments joined with spaces, and a quoted one counts its quotes."""
    args = ["a.exe", "-port=2302", "-config=C:" + SEP + "with space" + SEP + "x.cfg"]

    joined = 'a.exe -port=2302 "-config=C:' + SEP + 'with space' + SEP + 'x.cfg"'

    assert command_line_length(args) == len(joined)


def test_a_space_free_argument_is_not_quoted() -> None:
    assert command_line_length(["one", "two"]) == len("one two")


def test_the_limit_is_the_os_one_not_the_shell_one() -> None:
    """8191 is cmd.exe. Dropping to it would cut the usable mod count by four."""
    assert COMMAND_LINE_LIMIT == 32767


def _config(root: Path, mod_count: int) -> tuple[Config, list[Path]]:
    """A config rooted wherever asked. The root need not exist: nothing here starts a
    server, and a realistic short root is far shorter than pytest's tmp_path, which
    would otherwise dominate every length measured."""
    tmp_path = root
    steamcmd = tmp_path / "steamcmd" / "steamcmd.exe"
    names = [f"Mod{n}" for n in range(mod_count)]
    config = Config(
        root=tmp_path,
        allow_long_root=True,
        steam={"username": "someone", "steamcmd_path": steamcmd},  # type: ignore[arg-type]
        mods={name: WorkshopMod(workshop_id=1559212036 + n) for n, name in enumerate(names)},
        instances={"chernarus": InstanceConfig(port=2302, mods=names)},
    )
    paths = [config.modcache_dir / str(1559212036 + n) / "1771519119" for n in range(mod_count)]
    return config, paths


REAL_ROOT = Path("D:/dz")


def _length(root: Path, mod_count: int) -> int:
    config, paths = _config(root, mod_count)
    return command_line_length(
        build_launch_args(
            config,
            "chernarus",
            config.instances["chernarus"],
            root / "serverapp" / "24041098" / "DayZServer_x64.exe",
            paths,
        )
    )


def test_a_hundred_mods_is_nowhere_near_the_limit() -> None:
    """The number worth knowing, measured on a realistic root: a heavily modded server
    is not close, so "the command line is too long" does not explain a mod that fails
    to load."""
    assert _length(REAL_ROOT, 100) < COMMAND_LINE_LIMIT / 4


def test_there_is_a_mod_count_only_a_shell_would_choke_on() -> None:
    """The difference is not theoretical: at this many mods a .bat-launched server is
    over cmd.exe's limit while dzsm still has most of its headroom. That is the whole
    reason the argv list matters beyond quoting."""
    length = _length(REAL_ROOT, 250)

    assert length > 8191
    assert length < COMMAND_LINE_LIMIT


def test_the_length_grows_by_one_path_per_mod() -> None:
    """So the headroom can be reasoned about from the root path length alone."""
    step = _length(REAL_ROOT, 2) - _length(REAL_ROOT, 1)

    assert step == len(str(REAL_ROOT / "modcache" / "1559212037" / "1771519119")) + 1


def test_the_root_path_is_what_drives_it() -> None:
    """Every mod path starts with root, so a long root multiplies across the whole list.
    This is the lever to pull if anyone ever does run out — and it is why config load
    keeps root short for MAX_PATH in the first place.

    Three extra occurrences beyond the mods themselves: the executable, -config and
    -profiles all sit under root too.
    """
    short = Path("D:/dz")
    deep = Path("D:/a-much-longer-root-directory-name")
    grew = len(str(deep)) - len(str(short))

    assert _length(deep, 50) - _length(short, 50) == (50 + 3) * grew

