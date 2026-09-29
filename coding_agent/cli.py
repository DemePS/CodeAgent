"""The terminal front end: `coding-agent -d <project> [instruction]`."""

import argparse
import sys
from pathlib import Path

import anthropic

from . import session, state
from .config import CLEAR_AT, COMPACT_AT, UV, WEB_SEARCH, _get_client
from .errors import connection_summary
from .context import compact_between_instructions, context_status, reset_usage
from .conversation import save_conversation
from .loop import send, set_auto_mode
from .memory import print_memory_status
from .skills import discover_skills, locations_report, skills_catalog
from .tools.browser import check_browser
from .ui import TerminalUI, read_text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Personal coding agent (Claude on Azure).")
    parser.add_argument("instruction", nargs="?", help="Task for the agent. Omit to start in interactive mode.")
    parser.add_argument("-d", "--dir", default=".", help="Project directory the agent works in (default: current directory).")
    parser.add_argument("-i", "--interactive", action="store_true", help="Keep chatting after the instruction finishes.")
    parser.add_argument("-r", "--resume", action="store_true", help="Continue the last conversation in this project.")
    parser.add_argument("--auto", action="store_true", help="Autonomous mode: apply edits and Python runs without asking.")
    parser.add_argument("--where", action="store_true", help="Show where memory and skills are read from, then exit.")
    parser.add_argument("--check-browser", action="store_true", help="Test the browser used by screenshot_page, then exit.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.check_browser:
        print(check_browser())
        return
    try:
        workspace = Path(args.dir).expanduser().resolve()
        if args.where:  # no API client, no saved conversation needed
            session.open_project(workspace, ui=TerminalUI())
            print(f"Workspace: {state.workspace}")
            print(locations_report(verbose=True))
            return
        client = _get_client()
        from .cleanup import run as clean_up
        if (cleaned := clean_up()) != "Clean-up: nothing to delete":  # old backups, unused memories
            print(cleaned)
        print(f"Workspace: {workspace}")
        print(f"Python runner: {'uv run (' + UV + ')' if UV else sys.executable + ' (uv not found)'}")
        session.open_project(workspace, ui=TerminalUI(), resume=args.resume, auto=args.auto)
    except NotADirectoryError as e:
        raise SystemExit(str(e))
    print(f"Claude: {connection_summary()}")
    print(locations_report(verbose=False))
    print(f"Web search: {'off' if WEB_SEARCH == 'off' else 'web_search_' + WEB_SEARCH}")

    try:
        interact(client, session.messages, args)
    finally:
        session.close()


def interact(client: anthropic.Anthropic, messages: list, args: argparse.Namespace) -> None:
    if args.instruction:
        ok = send(client, messages, args.instruction)
        if not args.interactive:
            if not ok:
                raise SystemExit(1)  # main() still waits for the memory update first
            return

    print("Interactive mode. Type 'exit' to quit. Multi-line pastes are sent as one message (or wrap "
          "text in \"\"\" lines).\nCommands: /auto (toggle autonomous mode), /mode, /skills (re-scan "
          "skills), /context, /compact, /clear.")
    while True:
        print_memory_status()
        try:
            user_input = read_text("\n\033[1mYou:\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if user_input.lower() in ("exit", "quit"):
            break
        if user_input.lower() == "/auto":
            set_auto_mode(not state.auto_mode)
            continue
        if user_input.lower() == "/mode":
            print(f"Autonomous mode is {'ON' if state.auto_mode else 'OFF'}.")
            continue
        if user_input.lower() == "/context":
            print(f"Context: {context_status(messages)} in {len(messages)} messages "
                  f"(window from AGENT_CONTEXT_WINDOW; tool outputs cleared past {CLEAR_AT:.0%}, "
                  f"compaction past {COMPACT_AT:.0%}).")
            print(f"This session: {state.context['cleared']} tool output(s) cleared, "
                  f"{state.context['compactions']} compaction(s).")
            continue
        if user_input.lower() == "/compact":
            try:
                compact_between_instructions(client, messages)
            except (anthropic.APIError, RuntimeError) as e:
                print(f"[compaction failed] {e}")
            continue
        if user_input.lower() == "/clear":
            messages.clear()
            state.pending_blocks.clear()
            state.memory_sent = False
            reset_usage()
            save_conversation(messages)
            print("Started a fresh conversation (memory notes are kept).")
            continue
        if user_input.lower() == "/skills":
            state.skills = discover_skills()
            print(locations_report(verbose=True))
            state.skills_note = "Updated skill list (the user re-scanned the skill folders):\n" + skills_catalog()
            continue
        if user_input:
            send(client, messages, user_input)


if __name__ == "__main__":
    main()
