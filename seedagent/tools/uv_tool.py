"""run_uv: install packages into the project's environment with `uv add`, confirmed like run_python."""

import shlex
import subprocess

from .. import state
from ..common import ToolError, hide_secrets, truncate
from ..errors import redact
from ..config import UV, RUN_TIMEOUT_SECONDS
from .python_runner import project_env


def tool_run_uv(packages: list[str], dev: bool = False, timeout: int = RUN_TIMEOUT_SECONDS) -> str:
    if UV is None:
        raise ToolError("uv is not installed.")
    if not packages or any(not isinstance(p, str) or not p for p in packages):
        raise ToolError("Pass `packages`: one or more package names (optionally with a version), e.g. ['requests', 'pandas>=2'].")
    bad = [p for p in packages if p.startswith("-")]
    if bad:
        raise ToolError(f"Invalid package name(s): {bad!r}. Options are not accepted here.")
    args = ["add", *(["--dev"] if dev else []), *packages]
    cmd = [UV, *args]

    state.ui.panel("Run uv", [f"uv {' '.join(shlex.quote(a) for a in args)}"], tone="run")
    if not (state.always_allow_uv or state.auto_mode):
        answer = state.ui.confirm("\nRun this?", ("yes", "no", "always for this session"))
        if answer == "always for this session":
            state.always_allow_uv = True
        elif answer != "yes":
            feedback = state.ui.ask_text("Why not / what should change? (optional): ")
            raise ToolError(
                "The user declined to run this."
                + (f" User feedback: {feedback}" if feedback else "")
            )

    try:
        proc = subprocess.run(
            cmd, cwd=state.cwd, capture_output=True, text=True, timeout=timeout,
            env=project_env(),
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"uv timed out after {timeout}s.")

    state.ui.status(f"[exit code {proc.returncode}]")
    return truncate(hide_secrets(redact(
        f"exit code: {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout or '(empty)'}\n"
        f"--- stderr ---\n{proc.stderr or '(empty)'}"
    )))
