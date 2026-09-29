"""`coding-agent --check`: find out which step of a call to Claude fails or hangs.

Each step adds one thing to the request the agent sends (sign-in, a plain request, streaming,
thinking, then the agent's own instructions and tools), without retries and with a time limit, so
the first step that fails names the cause."""

from __future__ import annotations

import os
import time

from . import state
from .config import MODEL, _get_client
from .errors import connection_summary, describe

TIME_LIMIT_SECONDS = 90


def _explain(error: BaseException) -> str:
    return describe(error) or f"{type(error).__name__}: {error}"


def _sign_in() -> None:
    if os.environ.get("ANTHROPIC_FOUNDRY_API_KEY"):
        return
    from .signin import SignIn
    SignIn().get_token(os.environ.get("TOKEN_SCOPE", "https://ai.azure.com/.default"))


def steps():
    """(name, function) pairs, in order."""
    client = _get_client().with_options(max_retries=0, timeout=TIME_LIMIT_SECONDS)
    hello = [{"role": "user", "content": "Say hello in three words."}]

    def plain():
        client.messages.create(model=MODEL, max_tokens=50, messages=hello)

    def streamed(**extra):
        def call():
            with client.messages.stream(model=MODEL, max_tokens=2000, messages=hello, **extra) as stream:
                stream.get_final_message()
        return call

    def as_the_agent():
        from .loop import active_tools
        from .prompts import SYSTEM_PROMPT
        streamed(cache_control={"type": "ephemeral"}, thinking={"type": "adaptive"},
                 system=(state.system_prompt or SYSTEM_PROMPT).format(workspace=state.workspace),
                 tools=active_tools())()

    return [
        ("Microsoft sign-in (token)", _sign_in),
        ("A short request", plain),
        ("Streamed answer", streamed()),
        ("Streamed answer with thinking", streamed(thinking={"type": "adaptive"})),
        ("The agent's request (instructions, tools, caching, thinking)", as_the_agent),
    ]


def run_check(print=print) -> bool:  # noqa: A002 -- replaceable for tests
    """Run the steps, printing each result; stops at the first failure. True if all pass."""
    print(f"Claude: {connection_summary()}")
    for name, step in steps():
        print(f"- {name}: ", end="", flush=True)
        started = time.monotonic()
        try:
            step()
        except Exception as error:
            print(f"FAILED after {time.monotonic() - started:.1f} s")
            print(f"  {_explain(error)}")
            return False
        print(f"ok ({time.monotonic() - started:.1f} s)")
    print("Everything works: Claude answers the agent's requests.")
    return True
