"""The terminal front end: `coding-agent -d <project> [instruction]`."""

import argparse
import sys
from pathlib import Path

import anthropic

from . import history, session, state
from .config import CLEAR_AT, COMPACT_AT, UV, WEB_SEARCH, _get_client, provider_label, uses_deepseek
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
    parser.add_argument("--read", action="append", default=[], metavar="DIR",
                        help="Another folder the agent may read but never write (repeat for several).")
    parser.add_argument("--where", action="store_true", help="Show where memory and skills are read from, then exit.")
    parser.add_argument("--check", action="store_true", help="Test each step of a call to Claude (sign-in, request, streaming, thinking, tools), then exit.")
    parser.add_argument("--memories", nargs="?", const="", metavar="NAME",
                        help="List every project's memory (or show one project's notes), then exit.")
    parser.add_argument("--forget", metavar="NAME",
                        help="Delete a project's memory (NAME, 'all', or '.' for the -d project), after confirmation, then exit.")
    parser.add_argument("--forget-logins", nargs="?", const="all", metavar="SITE",
                        help="Delete the sign-ins kept for the browsing tools (a site, or all), then exit.")
    parser.add_argument("--forget-secrets", action="store_true", help="Delete the mail and SMS logins saved in ~/.coding-agent/secrets, then exit.")
    parser.add_argument("--check-browser", action="store_true", help="Test the browser used by screenshot_page, then exit.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.memories is not None or args.forget:  # developers: no project opened, no call to Claude
        from . import memories
        from .config import MEMORY_HOME
        current = session.project_id(Path(args.dir).expanduser().resolve())
        if args.forget:
            print(memories.forget(MEMORY_HOME, current if args.forget == "." else args.forget))
        elif args.memories:
            print(memories.show(MEMORY_HOME, current if args.memories == "." else args.memories))
        else:
            print(memories.listing(MEMORY_HOME, current))
        return
    if args.forget_logins:
        from . import websessions
        removed = websessions.forget(args.forget_logins)
        print("Removed: " + ", ".join(removed) if removed else "No saved sign-in" + ("" if args.forget_logins == "all" else f" for {args.forget_logins}") + ".")
        return
    if args.forget_secrets:
        from . import credentials
        removed = credentials.forget()
        print("Removed: " + ", ".join(removed) if removed else "No saved login.")
        return
    if args.check_browser:
        print(check_browser())
        return
    try:
        workspace = Path(args.dir).expanduser().resolve()
        if args.check:
            from .diagnose import run_check
            session.open_project(workspace, ui=TerminalUI())
            raise SystemExit(0 if run_check() else 1)
        if args.where:  # no API client, no saved conversation needed
            session.open_project(workspace, ui=TerminalUI())
            print(f"Workspace: {state.workspace}")
            print(locations_report(verbose=True))
            return
        client = _get_client()
        from .cleanup import run_once as clean_up
        if (cleaned := clean_up()) and cleaned != "Clean-up: nothing to delete":  # old backups, unused memories
            print(cleaned)
        print(f"Workspace: {workspace}")
        print(f"Python runner: {'uv run (' + UV + ')' if UV else sys.executable + ' (uv not found)'}")
        session.open_project(workspace, ui=TerminalUI(), resume=args.resume, auto=args.auto)
        state.ask_read_outside = True  # reading outside the project: asks first, once per folder
        for folder in args.read:  # read-only folders, e.g. documents kept elsewhere
            print(f"Read-only: {session.add_read_folder(folder)}")
    except NotADirectoryError as e:
        raise SystemExit(str(e))
    print(f"{provider_label()}: {connection_summary()}")
    print(locations_report(verbose=False))
    print(f"Web search: {'off' if WEB_SEARCH == 'off' or uses_deepseek() else 'web_search_' + WEB_SEARCH}")

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
    history.enable()  # up arrow recalls earlier questions
    while True:
        print_memory_status()
        try:
            print()
            user_input = read_text(history.prompt("\033[1mYou:\033[0m ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        history.remember(user_input)
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
