"""Workbooks locked while the agent works on them: nobody else can change one during an instruction.

The first time an instruction reads or changes a workbook of the project folder, the agent takes it;
when the instruction ends (finished, failed or stopped) every workbook is let go (loop.send). Opened
meanwhile in Excel, the workbook comes up read-only ("in use -- open read-only, or be notified").
- xlwings backend: the agent's invisible Excel keeps the workbook open for the whole instruction and
  saves into it, so the person's Excel sees it as locked for editing, as with a colleague.
- openpyxl backend: the agent keeps the file open with the right to write and lets others only read
  (Windows share mode; an exclusive advisory lock elsewhere); saves are written through that handle.
A workbook already open elsewhere (in the person's Excel) cannot be taken: changes to it are refused
until it is closed, reading it still works. If the agent's process dies, the system lets the file go.
Documents outside the project folder (read-only for the agent) are never locked.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .. import state
from ..common import ToolError, rel_name


@dataclass
class Held:
    path: Path
    file: object = None  # openpyxl: the open file (read + write, others read only)
    book: object = None  # xlwings: the workbook open in the agent's Excel


_held: dict[Path, Held] = {}


def in_project(p: Path) -> bool:
    try:
        p.resolve().relative_to(state.workspace.resolve())
        return True
    except (ValueError, AttributeError):
        return False


def held(p: Path) -> Held | None:
    return _held.get(p.resolve())


def hold(p: Path) -> Held | None:
    """Take the workbook for the rest of the instruction (nothing for a file that does not exist
    yet, or outside the project). Raises ToolError when another program has it open."""
    from .documents import excel_backend

    p = p.resolve()
    if p in _held:
        return _held[p]
    if not p.is_file() or not in_project(p):
        return None
    if excel_backend() == "xlwings":
        from . import excel_xl
        entry = Held(p, book=excel_xl.open_locked(p))
    else:
        entry = Held(p, file=open_exclusive(p))
    _held[p] = entry
    state.ui.status(f"[excel] {rel_name(p)} locked until this instruction ends (others can only read it)")
    return entry


def try_hold(p: Path) -> str:
    """hold() for a read: '' when locked (or nothing to lock), else a note on why it could not be."""
    try:
        hold(p)
        return ""
    except ToolError as e:
        return f"(not locked: {e})"


def in_use(p: Path) -> ToolError:
    return ToolError(f"{p.name} is open in another program (Excel?): ask the person to close it. While the agent "
                     "works on a workbook nobody else may change it; reading it still works.")


def open_exclusive(p: Path):
    """The file open for reading and writing, others allowed to read only."""
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        generic_read, generic_write, share_read, open_existing, normal = 0x80000000, 0x40000000, 1, 3, 0x80
        handle = create(str(p), generic_read | generic_write, share_read, None, open_existing, normal, None)
        if handle in (None, wintypes.HANDLE(-1).value):
            error = ctypes.get_last_error()
            if error in (32, 33):  # sharing / lock violation: open elsewhere
                raise in_use(p)
            raise ToolError(f"{p.name} could not be opened for the agent (Windows error {error}).")
        return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDWR | getattr(os, "O_BINARY", 0)), "r+b")
    import fcntl

    f = open(p, "r+b")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise in_use(p)
    return f


def write(p: Path, data: bytes) -> bool:
    """Write a whole new content into a workbook held by the openpyxl backend. False if not held."""
    entry = held(p)
    if entry is None or entry.file is None:
        return False
    f = entry.file
    f.seek(0)
    f.write(data)
    f.truncate()
    f.flush()
    os.fsync(f.fileno())
    return True


def release(p: Path) -> None:
    entry = _held.pop(p.resolve(), None)
    if entry is None:
        return
    try:
        if entry.file is not None:
            entry.file.close()
        if entry.book is not None:
            from . import excel_xl
            excel_xl.close_locked(entry.path, entry.book)
    except Exception:
        pass


def release_all() -> None:
    """Let every workbook go (the end of an instruction)."""
    for p in list(_held):
        release(p)
