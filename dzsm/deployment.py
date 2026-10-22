"""Files the binary carries so a bare exe can set itself up on a fresh server.

The scheduled-task launcher, its installer, the deployment notes and a commented example
config all live in the source tree, which is exactly what someone who copied one .exe to
a server does not have. `dzsm init` already solved this for config.toml; everything
needed to run the thing unattended has the same problem.

Nothing here ever overwrites without being asked. The launcher is where the Steam Web
API key and the dashboard token get written by hand, and replacing it during an upgrade
would quietly wipe both.
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

_log = logging.getLogger(__name__)


class DeploymentError(Exception):
    """A file could not be written. Nothing partial is left behind for that file."""


# Source inside the bundle (or the source tree) -> name written into the target
# directory. Flattened on purpose: someone deploying wants four files in one folder, not
# a deploy/ subdirectory to cd into.
DEPLOY_FILES: dict[str, str] = {
    "deploy/dzsm-daemon.cmd": "dzsm-daemon.cmd",
    "deploy/install-task.ps1": "install-task.ps1",
    "deploy/README.md": "DEPLOY-README.md",
    "config.example.toml": "config.example.toml",
}


def bundled_path(relative: str) -> Path:
    """Where a carried file lives now: unpacked beside the exe, or in the source tree.

    Same reasoning as api._dashboard_path. A frozen build unpacks data files under
    sys._MEIPASS, so __file__ points into the module archive rather than a directory.
    """
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle is not None:
        return Path(bundle) / relative
    return Path(__file__).parent.parent / relative


def write_files(
    directory: Path, *, force: bool = False
) -> tuple[list[Path], list[Path]]:
    """Write the deployment files into `directory`.

    Returns (written, skipped) — both lists, never None and never a bool (Rule 1).
    `skipped` is the files already there, which is the normal case on a second run.

    Raises DeploymentError if the directory cannot be made, or a carried file is missing
    from the build: silently writing three of four files would leave someone with a
    deployment that looks complete.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DeploymentError(f"cannot create {directory}: {exc}") from exc

    missing = [name for name in DEPLOY_FILES if not bundled_path(name).is_file()]
    if missing:
        raise DeploymentError(
            f"this build is missing {', '.join(missing)} — it was built without the "
            f"data files in dzsm.spec"
        )

    written: list[Path] = []
    skipped: list[Path] = []
    for name, target_name in DEPLOY_FILES.items():
        target = directory / target_name
        if target.exists() and not force:
            skipped.append(target)
            continue
        try:
            shutil.copyfile(bundled_path(name), target)
        except OSError as exc:
            raise DeploymentError(f"cannot write {target}: {exc}") from exc
        _log.info("wrote %s", target)
        written.append(target)
    return written, skipped
