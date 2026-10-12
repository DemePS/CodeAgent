"""run_az: run Azure CLI (`az`) commands, confirmed like run_python since they can change cloud resources."""

import shlex
import subprocess

from .. import state
from ..common import ToolError, hide_secrets, scrubbed_env, truncate
from ..errors import redact
from ..config import AZ, AZ_TIMEOUT_SECONDS


def tool_run_az(args: list[str], timeout: int = AZ_TIMEOUT_SECONDS) -> str:
    if AZ is None:
        raise ToolError("The Azure CLI (az) is not installed.")
    if not args or any(not isinstance(a, str) or not a for a in args):
        raise ToolError("Pass `args`: the az command and its options, e.g. ['group', 'list'].")
    if args[0] == "az":
        args = args[1:]
    if not args:
        raise ToolError("Pass `args`: the az command and its options, e.g. ['group', 'list'].")
    cmd = [AZ, *args, "--output", "json"]

    state.ui.panel("Run az", [f"az {' '.join(shlex.quote(a) for a in args)}"], tone="run")
    if not (state.always_allow_az or state.auto_mode):
        answer = state.ui.confirm("\nRun this?", ("yes", "no", "always for this session"))
        if answer == "always for this session":
            state.always_allow_az = True
        elif answer != "yes":
            feedback = state.ui.ask_text("Why not / what should change? (optional): ")
            raise ToolError(
                "The user declined to run this."
                + (f" User feedback: {feedback}" if feedback else "")
            )

    try:
        proc = subprocess.run(
            cmd, cwd=state.cwd, capture_output=True, text=True, timeout=timeout,
            env=scrubbed_env(), stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"az timed out after {timeout}s.")

    state.ui.status(f"[exit code {proc.returncode}]")
    return truncate(hide_secrets(redact(
        f"exit code: {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout or '(empty)'}\n"
        f"--- stderr ---\n{proc.stderr or '(empty)'}"
    )))
