"""Previous versions of the workbooks the agent changed, and putting one back.

Before each change it saves to a workbook, the agent copies the workbook to
BACKUP_HOME/<folder>-<code>/<YYYYmmdd-HHMMSS>-<file name>: <folder>-<code> identifies the project
folder (its name and a hash of its full path), and the time is when the change was saved -- the
copy is the workbook as it was just before. Backups are kept a few days (cleanup.py).

Restoring a version first backs up the current one the same way, so a restore can be undone too.
Used by the restore_backup tool and by applications (e.g. a "previous versions" list in a window).
"""

from __future__ import annotations

import datetime
import hashlib
import os
import re
import shutil
import time
from pathlib import Path

from . import state
from .config import BACKUP_HOME

NAME = re.compile(r"^(\d{8})-(\d{6})-(.+)$")


def folder(workspace: Path | None = None) -> Path:
    """The backups of a project folder (default: the current workspace)."""
    ws = workspace or state.workspace
    return BACKUP_HOME / f"{ws.name}-{hashlib.sha256(str(ws).encode()).hexdigest()[:8]}"


def save(path: Path, content: bytes, workspace: Path | None = None) -> Path:
    """Keep `content` (a file's current bytes) as a previous version of `path`. Returns the copy."""
    target = folder(workspace)
    target.mkdir(parents=True, exist_ok=True)
    backup = target / f"{time.strftime('%Y%m%d-%H%M%S')}-{path.name}"
    while backup.exists():  # two saves within the same second
        backup = backup.with_name(f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(time.time() + 1))}-{path.name}")
        time.sleep(0.01)
    backup.write_bytes(content)
    return backup


def moment(version: str) -> datetime.datetime:
    match = NAME.match(version)
    if not match:
        raise ValueError(f"not a backup name: {version}")
    return datetime.datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")


def versions(name: str, workspace: Path | None = None) -> list[dict]:
    """The previous versions of the file called `name` in a project folder, newest first: id (the
    backup's file name), time (ISO, local time of the change it preceded) and size in bytes."""
    found = []
    backups = folder(workspace)
    for file in backups.iterdir() if backups.is_dir() else []:
        match = NAME.match(file.name)
        if not (file.is_file() and match and match.group(3) == name):
            continue
        try:
            when = moment(file.name)
        except ValueError:
            continue
        found.append({"id": file.name, "time": when.isoformat(timespec="seconds"), "size": file.stat().st_size})
    return sorted(found, key=lambda v: v["id"], reverse=True)


def restore(path: Path, version: str, workspace: Path | None = None, write=None) -> str:
    """Put a previous version back in place of `path`, after backing up the current one. Returns a
    sentence for the person. Raises ValueError (no such version) or PermissionError (the file is
    open, e.g. in Excel)."""
    if version not in {v["id"] for v in versions(path.name, workspace)}:
        raise ValueError(f"No previous version {version} of {path.name}.")
    source = folder(workspace) / version
    if path.exists():  # so that the restore can be undone
        save(path, path.read_bytes(), workspace)
    if write is not None:  # the file is held open by the agent (excel_lock): written through it
        write(source.read_bytes())
        return (f"{path.name} is back to its version from before the change of {moment(version):%d %b %Y at %H:%M}. "
                "The version it replaced is kept among the previous versions.")
    tmp = path.with_name(f".{path.name}.restore-tmp")
    try:
        shutil.copyfile(source, tmp)
        os.replace(tmp, path)
    except PermissionError:
        raise PermissionError(f"{path.name} could not be replaced: it is open (in Excel?). Close it and try again.")
    finally:
        if tmp.exists():
            tmp.unlink()
    return (f"{path.name} is back to its version from before the change of {moment(version):%d %b %Y at %H:%M}. "
            "The version it replaced is kept among the previous versions.")
