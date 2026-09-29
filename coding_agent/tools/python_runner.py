"""run_python: run project code in its own environment, inside the guard sandbox."""

import os
import subprocess
import sys
from pathlib import Path

from .. import state
from ..common import ToolError, truncate
from ..config import RUN_TIMEOUT_SECONDS, UV
from ..guard import GUARD_SOURCE


def project_env() -> dict:
    """Environment for code run in the project: never the agent's own virtualenv.

    When the agent itself runs from its own environment (e.g. `uv run coding-agent`), variables
    such as VIRTUAL_ENV point at it; `uv run` in the project must use the project's environment.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PROJECT", "PYTHONHOME", "PYTHONPATH", "CONDA_PREFIX")}
    if sys.prefix != sys.base_prefix:  # the agent runs in a virtualenv: take its bin/ off PATH
        own_bin = os.path.normcase(str(Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")))
        env["PATH"] = os.pathsep.join(d for d in env.get("PATH", "").split(os.pathsep)
                                      if os.path.normcase(d.rstrip("/\\")) != own_bin)
    env["AGENT_GUARD_WORKSPACE"] = str(state.workspace)  # read by GUARD_SOURCE
    return env




def uv_sync_flag() -> str:
    """How `uv run` may touch the environment: never rewrite uv.lock, never add dependencies.

    With a uv.lock, --frozen installs exactly what it pins; without one, --no-sync uses the
    existing .venv as-is instead of creating a lockfile.
    """
    for folder in (state.cwd, *state.cwd.parents):
        if (folder / "pyproject.toml").is_file():
            return "--frozen" if (folder / "uv.lock").is_file() else "--no-sync"
    return "--no-sync"


def tool_run_python(code: str | None = None, args: list[str] | None = None, timeout: int = RUN_TIMEOUT_SECONDS) -> str:
    if bool(code) == bool(args):
        raise ToolError("Pass exactly one of `code` or `args`.")
    if code:
        guarded = ["code", code]
    elif args[0] == "-m" and len(args) > 1:
        guarded = ["module", *args[1:]]
    elif not args[0].startswith("-"):
        guarded = ["script", *args]
    else:
        raise ToolError("args must be a script path or -m <module>, optionally followed by arguments.")
    python = [UV, "run", uv_sync_flag(), "--quiet", "python"] if UV else [sys.executable]
    cmd = [*python, "-c", GUARD_SOURCE, *guarded]

    state.ui.panel("Run Python", [f"({'uv run python' if UV else sys.executable}, subprocesses and file changes blocked)",
                                  code if code else "python " + " ".join(args)], tone="run")
    if not (state.always_allow_python or state.auto_mode):
        answer = state.ui.confirm("\nRun this?", ("yes", "no", "always for this session"))
        if answer == "always for this session":
            state.always_allow_python = True
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
        raise ToolError(f"Timed out after {timeout}s.")

    state.ui.status(f"[exit code {proc.returncode}]")
    return truncate(
        f"exit code: {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout or '(empty)'}\n"
        f"--- stderr ---\n{proc.stderr or '(empty)'}"
    )


