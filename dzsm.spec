# PyInstaller build. Run with: pyinstaller dzsm.spec
#
# Produces a single dzsm executable that runs on a machine with no Python at all — which
# is the point, since the target for this is a game server box, not a dev machine.
#
# onefile vs onedir: onefile is the default here because a single file is far easier to
# drop on a server and register as a service. It unpacks to a temp directory on every
# start, which costs a second or two and can leave _MEI* directories behind if the process
# is killed rather than stopped. If you are running this as a long-lived service and that
# bothers you, set ONEFILE = False for a folder build that starts instantly.

from PyInstaller.utils.hooks import collect_submodules

ONEFILE = True

# uvicorn resolves its loop, protocol, and lifespan implementations by string at runtime,
# so static analysis cannot see them and they have to be collected explicitly. Without
# this the binary builds fine and then fails the moment `dzsm serve` starts.
hiddenimports = [
    *collect_submodules("uvicorn"),
    "dzsm.api",
]

analysis = Analysis(
    ["dzsm/__main__.py"],
    pathex=[],
    binaries=[],
    # The dashboard is read from disk at request time; api._dashboard_path knows to look
    # under sys._MEIPASS when frozen.
    datas=[
        ("dzsm/dashboard.html", "dzsm"),
        # Carried so a bare exe on a fresh server can write out everything needed to
        # run unattended. Without these, the binary distribution dead-ends the same
        # way it used to with config.example.toml.
        ("deploy/dzsm-daemon.cmd", "deploy"),
        ("deploy/install-task.ps1", "deploy"),
        ("deploy/README.md", "deploy"),
        ("config.example.toml", "."),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "mypy", "ruff"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)

if ONEFILE:
    executable = EXE(
        pyz,
        analysis.scripts,
        analysis.binaries,
        analysis.datas,
        [],
        name="dzsm",
        console=True,
        strip=False,
        upx=False,
        disable_windowed_traceback=False,
    )
else:
    executable = EXE(
        pyz,
        analysis.scripts,
        [],
        exclude_binaries=True,
        name="dzsm",
        console=True,
        strip=False,
        upx=False,
    )
    collection = COLLECT(
        executable,
        analysis.binaries,
        analysis.datas,
        strip=False,
        upx=False,
        name="dzsm",
    )
