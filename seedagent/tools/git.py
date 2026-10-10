"""Read-only git: status, diff and log, hardened against repository config."""

import os
import re
import subprocess
from pathlib import Path

from .. import state
from ..common import ToolError, display, resolve, truncate
from ..config import GIT, GIT_TIMEOUT_SECONDS

# Read-only git. Only status, diff and log, built from structured arguments (no free-form options),
# and hardened so the repository's own config cannot make git run programs or write files:
GIT_SAFETY = [
    "-c", "core.fsmonitor=false",       # fsmonitor hooks are commands run by `git status`
    "-c", "core.pager=cat",
    "-c", "diff.external=",
    "-c", "log.showSignature=false",    # would run gpg
    "-c", "status.submoduleSummary=false",
    "-c", "color.ui=false",
]
GIT_ENV = {
    "GIT_OPTIONAL_LOCKS": "0",   # `git status` must not refresh (write) the index
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_EXTERNAL_DIFF": "",
}
GIT_REF = re.compile(r"[A-Za-z0-9._/~^@{}+-]+")  # commits, branches, ranges -- no options, no "rev:path"


def git_run(args: list[str]) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_EXTERNAL_DIFF", "GIT_DIR", "GIT_WORK_TREE")}
    env.update(GIT_ENV)
    try:
        proc = subprocess.run(
            [GIT, *GIT_SAFETY, *args], cwd=state.cwd, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"git timed out after {GIT_TIMEOUT_SECONDS} s; narrow it with paths or max_count.")
    if proc.returncode != 0:
        raise ToolError(f"git failed: {proc.stderr.strip() or proc.stdout.strip() or proc.returncode}")
    return proc.stdout


def tool_git(command: str, paths: list[str] | None = None, ref: str | None = None, staged: bool = False,
             stat: bool = False, patch: bool = False, max_count: int = 20) -> str:
    if GIT is None:
        raise ToolError("git is not installed.")
    if command not in ("status", "diff", "log"):
        raise ToolError("Only status, diff and log are available (read-only).")
    try:
        top = Path(git_run(["rev-parse", "--show-toplevel"]).strip()).resolve()
    except ToolError:
        raise ToolError("The workspace is not inside a git repository.")
    # Paths go after "--" so they are never read as options; without paths, stay in the workspace
    # (it may be a sub-folder of a larger repository).
    pathspec = [str(resolve(x)) for x in paths] if paths else ([] if top == state.workspace else [str(state.workspace)])
    if ref is not None and (ref.startswith("-") or not GIT_REF.fullmatch(ref)):
        raise ToolError(f"Invalid ref {ref!r}: use a commit, branch, tag or range such as 'HEAD~2' or 'main...HEAD'.")
    if command == "status":
        args = ["status", "--short", "--branch", "--untracked-files=all"]
    elif command == "diff":
        args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
        args += ["--cached"] if staged else []
        args += ["--stat"] if stat else []
        args += [ref] if ref else []
    else:
        max_count = max(1, min(int(max_count), 200))
        args = ["log", f"--max-count={max_count}", "--no-color", "--no-ext-diff", "--no-textconv", "--date=short"]
        if patch or stat:
            args += ["--format=commit %h%nAuthor: %an <%ae>%nDate:   %ad%n%n%w(0,4,4)%B"]
            args += ["--patch"] if patch else []
            args += ["--stat"] if stat else []
        else:
            args += ["--format=%h %ad %an: %s%d"]
        args += [ref] if ref else []
    state.ui.status(f"[git] {' '.join(args[:1] + ([ref] if ref else []) + [display(Path(x)) for x in pathspec])}")
    output = git_run([*args, "--", *pathspec])
    return truncate(output) or {"status": "(clean)", "diff": "(no differences)", "log": "(no commits)"}[command]


