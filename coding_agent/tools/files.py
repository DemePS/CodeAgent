"""File tools: list, search, read, edit, write, copy and delete, inside the workspace."""

import difflib
import os
import re
import shutil
from pathlib import Path

from .. import state
from ..common import (
    ToolError,
    approve,
    display,
    is_protected,
    rel_name,
    resolve,
    resolve_readable,
    truncate,
    writable_path,
)
from ..config import MAX_LISTING_ENTRIES, SKIP_DIRS


def tool_list_directory(path: str = ".") -> str:
    root = resolve_readable(path)
    if not root.is_dir():
        raise ToolError(f"Not a directory: {path}")
    try:
        entries = sorted(root.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError as e:
        raise ToolError(f"Cannot list {path}: {e}")

    lines = [f"{display(root)}/"]
    for e in entries:
        if e.name in SKIP_DIRS:
            continue
        if len(lines) > MAX_LISTING_ENTRIES:
            lines.append(f"... [stopped after {MAX_LISTING_ENTRIES} entries]")
            break
        if e.is_dir():
            lines.append(f"  {e.name}/")
        else:
            try:
                lines.append(f"  {e.name}  ({e.stat().st_size:,} bytes)")
            except OSError:
                lines.append(f"  {e.name}")
    return "\n".join(lines) if len(lines) > 1 else f"{lines[0]}\n  (empty)"


def tool_change_directory(path: str) -> str:
    target = state.workspace if path.strip() in ("/", "\\") else resolve(path)
    if not target.is_dir():
        raise ToolError(f"Not a directory: {path}")
    state.cwd = target
    rel = state.cwd.relative_to(state.workspace).as_posix()
    state.ui.status(f"[cwd] {'(repository root)' if rel == '.' else rel}")
    return f"Current directory is now: {'.' if rel == '.' else rel} (relative to the repository root)"


def tool_grep(
    pattern: str, path: str = ".", glob: str | None = None, ignore_case: bool = False, include_ignored: bool = False
) -> str:
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise ToolError(f"Invalid regex: {e}")

    root = resolve_readable(path)
    if not root.exists():
        raise ToolError(f"Path not found: {path}")

    skip = {".git"} if include_ignored else SKIP_DIRS
    files = [root] if root.is_file() else (
        Path(dirpath) / name
        for dirpath, dirnames, filenames in os.walk(root)
        if not dirnames.__setitem__(slice(None), [d for d in dirnames if d not in skip])
        for name in filenames
    )

    matches = []
    for f in files:
        if glob and not f.match(glob):
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    if regex.search(line):
                        matches.append(f"{display(f)}:{lineno}: {line.rstrip()}")
                        if len(matches) >= 500:
                            matches.append("... [stopped after 500 matches; narrow the search]")
                            return "\n".join(matches)
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable file
    return "\n".join(matches) if matches else "No matches."


def tool_read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    p = resolve_readable(path)
    if not p.is_file():
        raise ToolError(f"File not found: {path}")
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        raise ToolError(f"{path} is not a UTF-8 text file.")
    start = start_line or 1
    end = min(end_line or len(lines), len(lines))
    body = "\n".join(f"{i:>5}\t{lines[i - 1]}" for i in range(start, end + 1))
    return truncate(body or "(empty file)")


def confirm_and_write(path: str, p: Path, old: str, new: str) -> str:
    """Show a diff of old -> new, ask the user, and write the file if approved."""
    existed = p.exists()
    name = rel_name(p)  # shown to the user: relative to the repository root
    diff = list(difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile=f"a/{name}" if existed else "/dev/null",
        tofile=f"b/{name}",
        lineterm="",
    ))
    # Link the header to the first changed line: start at the first hunk's "+c" line number
    # ("@@ -a,b +c,d @@") and skip its unchanged context lines.
    first_line = 1
    for i, d in enumerate(diff):
        if d.startswith("@@"):
            first_line = max(int(re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", d).group(1)), 1)
            for context in diff[i + 1:]:
                if not context.startswith(" "):
                    break
                first_line += 1
            break
    state.ui.diff("Modify" if existed else "Create", name, p, first_line, diff)

    # Name the file again at the question: a long diff scrolls the header away.
    action = f"Apply this change to {name}?" if existed else f"Create {name}?"
    if state.auto_mode:
        state.ui.status(f"(autonomous mode: {'modifying' if existed else 'creating'} {name} without asking)")
        answer = "yes"
    else:
        answer = state.ui.confirm(f"\n{action}")
    if answer != "yes":
        feedback = state.ui.ask_text(f"Why not / what should change in {name}? (optional): ")
        state.ui.failure(f"{name} was not {'modified' if existed else 'created'}")
        raise ToolError(
            f"The user rejected this change; {name} was NOT {'modified' if existed else 'created'}."
            + (f" User feedback: {feedback}" if feedback else "")
        )

    # The file may have been edited (e.g. saved in your editor) while the prompt was waiting: the
    # new content was computed from the old one, so writing it now would silently undo that edit.
    try:
        current = p.read_text(encoding="utf-8") if p.exists() else None
    except (OSError, UnicodeDecodeError):
        current = None if not p.exists() else "\0changed"
    if current != (old if existed else None):
        state.ui.failure(f"{name} changed on disk while waiting for approval -- not written")
        raise ToolError(f"{name} was changed on disk (probably saved in the user's editor) after the diff was "
                        "shown; nothing was written. Read the file again and redo the change on its current content.")
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(new, encoding="utf-8")
    except PermissionError:
        raise ToolError(f"{name} could not be written: another program has it locked (on Windows, e.g. a file "
                        "open in Excel or a running process). Ask the user to close it, then try again.")
    state.ui.success(f"{'Modified' if existed else 'Created'} {name}")
    return f"{'Modified' if existed else 'Created'} {path} ({len(new.splitlines())} lines)."


def tool_edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    p = writable_path(path)
    if not p.is_file():
        raise ToolError(f"File not found: {path}. Use write_file to create a new file.")
    try:
        old = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{path} is not a UTF-8 text file.")

    if not old_string:
        raise ToolError("old_string is empty. Use write_file to create or rewrite a whole file.")
    if old_string == new_string:
        raise ToolError("old_string and new_string are identical; nothing to change.")
    count = old.count(old_string)
    if count == 0:
        raise ToolError(
            f"old_string was not found in {path}. Re-read the file with read_file and copy the "
            "text exactly, including whitespace, without the line-number prefix."
        )
    if count > 1 and not replace_all:
        raise ToolError(
            f"old_string occurs {count} times in {path}. Add surrounding lines to make it unique, "
            "or set replace_all to true."
        )

    new = old.replace(old_string, new_string) if replace_all else old.replace(old_string, new_string, 1)
    result = confirm_and_write(path, p, old, new)
    return result + (f" Replaced {count} occurrences." if replace_all and count > 1 else "")


def tool_write_file(path: str, content: str) -> str:
    p = writable_path(path)
    old = p.read_text(encoding="utf-8") if p.exists() else ""
    if old == content:
        return "No changes: file already has this content."
    return confirm_and_write(path, p, old, content)


def tool_copy_path(source: str, destination: str) -> str:
    src = resolve_readable(source)  # copying from a read-only folder into the workspace is fine
    if not src.exists():
        raise ToolError(f"Not found: {source}")
    dest = resolve(destination)
    if dest.is_dir():
        dest = resolve(str(Path(destination) / src.name))  # copy into the existing folder
    if dest == src or src in dest.parents:
        raise ToolError("Cannot copy a file or folder onto or into itself.")
    if src.name == ".git":
        raise ToolError("A .git folder cannot be copied.")
    rel = dest.relative_to(state.workspace).as_posix()
    if is_protected(dest) or any(prot in dest.parents or prot == dest for prot in state.protected_paths):
        raise ToolError(f"{rel} is part of the coding agent's own files and cannot be written.")

    if src.is_file():
        data = src.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is not None:  # a text file: same diff and approval as write_file
            p = writable_path(os.path.relpath(dest, state.cwd))
            old = p.read_text(encoding="utf-8") if p.exists() else ""
            if old == text:
                return f"No changes: {rel} already has this content."
            return confirm_and_write(display(p), p, old, text) + f" (copied from {display(src)})"
        existed = dest.exists()
        state.ui.panel(f"Copy {rel_name(src)} -> {rel_name(dest)} ({len(data):,} bytes, binary"
                       f"{', REPLACES the existing file' if existed else ''})")
        approve(f"Copy {rel_name(src)} to {rel_name(dest)}?")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        state.ui.success(f"Copied {rel_name(src)} to {rel_name(dest)}")
        return f"Copied {display(src)} to {display(dest)} ({len(data):,} bytes)."

    if dest.exists():
        raise ToolError(f"{rel} already exists; choose a new folder name (folders are never overwritten).")
    files = folders = size = 0
    for root, dirs, names in os.walk(src):
        dirs[:] = [d for d in dirs if d != ".git"]
        folders += len(dirs)
        for file_name in names:
            files += 1
            try:
                size += os.lstat(os.path.join(root, file_name)).st_size
            except OSError:
                pass
    entries = sorted((e for e in src.iterdir() if e.name != ".git"), key=lambda e: (not e.is_dir(), e.name.lower()))
    state.ui.panel(f"Copy folder {rel_name(src)}/ -> {rel_name(dest)}/ ({files} file(s), "
                   f"{folders} subfolder(s), {size:,} bytes)", folder_listing(entries))
    approve(f"Copy the folder {rel_name(src)}/ to {rel_name(dest)}/?")
    # symlinks=True: links are copied as links, so nothing outside the workspace is pulled in.
    shutil.copytree(src, dest, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    state.ui.success(f"Copied folder {rel_name(src)}/ to {rel_name(dest)}/")
    return f"Copied folder {display(src)} to {display(dest)} ({files} file(s), {folders} subfolder(s))."


def tool_delete_file(path: str) -> str:
    p = writable_path(path)
    if not p.is_file():
        raise ToolError(f"File not found: {path}")
    # Deleting cannot be undone, so it always needs human validation -- even in autonomous mode.
    size = p.stat().st_size
    name = rel_name(p)
    state.ui.panel(f"Delete {name} ({size:,} bytes)", tone="danger")
    if state.auto_mode:
        state.ui.status("(autonomous mode: deletions still need your approval)")
    if state.ui.confirm(f"Delete {name}?") != "yes":
        feedback = state.ui.ask_text(f"Why not delete {name}? (optional): ")
        state.ui.failure(f"{name} was not deleted")
        raise ToolError(f"The user refused the deletion; {name} was NOT deleted."
                        + (f" User feedback: {feedback}" if feedback else ""))
    p.unlink()
    state.ui.success(f"Deleted {name}")
    return f"Deleted {path}."


def folder_listing(entries: list[Path]) -> list[str]:
    """The first entries of a folder, as shown before copying or deleting it."""
    lines = [f"{e.name}{'/' if e.is_dir() and not e.is_symlink() else ''}" for e in entries[:30]]
    if len(entries) > 30:
        lines.append(f"... and {len(entries) - 30} more")
    return lines


def tool_delete_folder(path: str) -> str:
    p = resolve(path)
    if not p.is_dir():
        raise ToolError(f"Not a folder: {path}" + (" (use delete_file for a file)" if p.is_file() else ""))
    if p == state.workspace:
        raise ToolError("The workspace root cannot be deleted.")
    name = p.relative_to(state.workspace).as_posix()  # shown relative to the repository root
    git_refusal = ToolError(f"{name} is or contains a git repository (.git); it cannot be deleted by the agent.")
    if ".git" in p.relative_to(state.workspace).parts:
        raise git_refusal
    if any(prot == p or p in prot.parents or p in prot.resolve().parents or prot in p.parents
           for prot in state.protected_paths):
        raise ToolError(f"{path} contains the coding agent's own files and cannot be deleted.")

    # Show what would be lost; walk without following symlinks (rmtree does not follow them either).
    files = folders = size = 0
    for root, dirs, names in os.walk(p):
        if ".git" in dirs or ".git" in names:  # a nested repository (or submodule) anywhere inside
            raise git_refusal
        folders += len(dirs)
        for file_name in names:
            files += 1
            try:
                size += os.lstat(os.path.join(root, file_name)).st_size
            except OSError:
                pass
    entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    state.ui.panel(f"Delete folder {name}/ ({files} file(s), {folders} subfolder(s), {size:,} bytes)",
                   folder_listing(entries), tone="danger")
    if state.auto_mode:
        state.ui.status("(autonomous mode: deletions still need your approval)")
    if state.ui.confirm(f"Delete the folder {name}/ and everything in it?") != "yes":
        feedback = state.ui.ask_text(f"Why not delete {name}/? (optional): ")
        state.ui.failure(f"{name}/ was not deleted")
        raise ToolError(f"The user refused the deletion; {name}/ was NOT deleted."
                        + (f" User feedback: {feedback}" if feedback else ""))
    try:
        shutil.rmtree(p)
    except OSError as e:
        raise ToolError(f"Deletion stopped partway: {e}. Check what is left with list_directory.")
    state.ui.success(f"Deleted folder {name}/ ({files} file(s))")
    note = ""
    if state.cwd == p or p in state.cwd.parents:
        state.cwd = state.workspace
        note = " The current directory was inside it and is now the repository root."
    return f"Deleted folder {name} ({files} file(s), {folders} subfolder(s)).{note}"


