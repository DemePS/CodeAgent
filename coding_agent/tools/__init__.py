"""The tools Claude can call, by name."""

import re
from collections.abc import Callable

from .. import state, usage
from ..common import ToolError
from ..schemas import TOOLS
from ..skills import tool_load_skill
from ..tools.browser import tool_screenshot_page
from ..tools.documents import (
    tool_edit_excel,
    tool_read_excel,
    tool_read_pdf,
    tool_search_pdf,
    tool_restore_backup,
    tool_view_image,
)
from ..tools.excel_add import tool_add_chart, tool_add_pivot_table, tool_add_table
from ..tools.excel_look import tool_format_excel, tool_view_excel
from ..tools.excel_script import tool_run_python_excel
from ..tools.files import (
    tool_change_directory,
    tool_copy_path,
    tool_delete_file,
    tool_delete_folder,
    tool_edit_file,
    tool_grep,
    tool_list_directory,
    tool_read_file,
    tool_write_file,
)
from ..tools.git import tool_git
from ..tools.interaction import tool_ask_human
from ..tools.mail import tool_mail_draft, tool_mail_login, tool_send_mail
from ..tools.sms import tool_send_sms
from ..tools.network import tool_clone_repo, tool_download_file
from ..tools.web import (
    tool_web_back,
    tool_web_click,
    tool_web_close,
    tool_web_look,
    tool_web_open,
    tool_web_page,
    tool_web_set_token,
    tool_web_sign_in,
    tool_web_type,
)
from ..tools.python_runner import tool_run_python

TOOL_HANDLERS = {
    "list_directory": tool_list_directory,
    "change_directory": tool_change_directory,
    "grep": tool_grep,
    "read_file": tool_read_file,
    "edit_file": tool_edit_file,
    "write_file": tool_write_file,
    "screenshot_page": tool_screenshot_page,
    "web_open": tool_web_open,
    "web_click": tool_web_click,
    "web_type": tool_web_type,
    "web_back": tool_web_back,
    "web_page": tool_web_page,
    "web_look": tool_web_look,
    "web_close": tool_web_close,
    "web_sign_in": tool_web_sign_in,
    "web_set_token": tool_web_set_token,
    "view_image": tool_view_image,
    "read_pdf": tool_read_pdf,
    "search_pdf": tool_search_pdf,
    "read_excel": tool_read_excel,
    "edit_excel": tool_edit_excel,
    "view_excel": tool_view_excel,
    "format_excel": tool_format_excel,
    "add_chart": tool_add_chart,
    "add_table": tool_add_table,
    "add_pivot_table": tool_add_pivot_table,
    "restore_backup": tool_restore_backup,
    "copy_path": tool_copy_path,
    "delete_file": tool_delete_file,
    "delete_folder": tool_delete_folder,
    "ask_human": tool_ask_human,
    "git": tool_git,
    "download_file": tool_download_file,
    "mail_login": tool_mail_login,
    "mail_draft": tool_mail_draft,
    "send_mail": tool_send_mail,
    "send_sms": tool_send_sms,
    "clone_repo": tool_clone_repo,
    "run_python": tool_run_python,
    "run_python_excel": tool_run_python_excel,
    "load_skill": tool_load_skill,
}


BUILTIN_TOOLS = frozenset(TOOL_HANDLERS) | {t["name"] for t in TOOLS}
_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


def register_tool(schema: dict, handler: Callable[..., str | list]) -> None:
    """Add a tool of the application to the tools Claude can call.

    `schema` is the tool definition sent to Claude: {"name", "description", "input_schema": {"type": "object", "properties": ...,
    "required": [...]}}. `handler` is called as handler(**arguments), one keyword parameter per property, and returns a string or a list of
    content blocks ({"type": "text", ...}, {"type": "image", ...}); to report a failure Claude should see, it raises
    coding_agent.common.ToolError. It runs in the agent's thread and can use coding_agent.state (state.ui, state.workspace).
    The application's code is responsible for what the handler does: CodeAgent's path checks and confirmations only guard its own tools.

    Registering does not enable the tool: a session offers it only when it is in the `tools=[...]` of open_project (or `tools=None`, all).
    A built-in tool cannot be replaced; registering the same handler again does nothing; another handler under a name already taken is an error."""
    if not isinstance(schema, dict):
        raise TypeError("The tool schema must be a dict.")
    name = schema.get("name")
    if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
        raise ValueError("A tool name is 1 to 64 letters, digits, '_' or '-'.")
    if not isinstance(schema.get("description"), str) or not schema["description"].strip():
        raise ValueError(f"Tool {name}: a description is needed: it is how Claude decides when to call it.")
    if not isinstance(schema.get("input_schema"), dict) or schema["input_schema"].get("type") != "object":
        raise ValueError(f"Tool {name}: input_schema must be a JSON schema of type 'object'.")
    if not callable(handler):
        raise TypeError(f"Tool {name}: the handler must be callable.")
    if name in BUILTIN_TOOLS:
        raise ValueError(f"{name} is a built-in tool and cannot be replaced.")
    if TOOL_HANDLERS.get(name) is handler:
        return
    if name in TOOL_HANDLERS:
        raise ValueError(f"A tool named {name} is already registered.")
    TOOLS.append(schema)
    TOOL_HANDLERS[name] = handler


def run_tool(block) -> dict:
    """Execute one tool_use block, report the outcome to the UI, and build its tool_result."""
    result = _run(block)
    content = result["content"]
    usage.record_tool(block.name, content)
    if result.get("is_error"):
        summary = str(content).splitlines()[0][:300] if content else "error"
    elif isinstance(content, list):  # text, images, documents
        summary = f"{len(content)} block(s): {', '.join(b.get('type', '?') for b in content)}"
    else:
        summary = f"{len(str(content)):,} characters"
    state.ui.tool_result(block.name, call_summary(block.input), not result.get("is_error"), summary)
    return result


def call_summary(arguments: dict) -> str:
    """The arguments of a call, short and without file contents: path='a.xlsx', pages='1'."""
    parts = []
    for key, value in arguments.items():
        if key in ("content", "old_string", "new_string", "code"):
            parts.append(f"{key}=<{len(str(value)):,} chars>")
        elif key == "changes" and isinstance(value, list):
            parts.append(f"changes=<{len(value)} cell(s)>")
        else:
            parts.append(f"{key}={value!r}"[:120])
    return ", ".join(parts)


def _run(block) -> dict:
    handler = TOOL_HANDLERS.get(block.name)
    if state.tool_names is not None and block.name not in state.tool_names:
        handler = None  # not enabled in this session
    try:
        if handler is None:
            raise ToolError(f"Unknown tool: {block.name}")
        output = handler(**block.input)
        return {"type": "tool_result", "tool_use_id": block.id, "content": output}
    except ToolError as e:
        return {"type": "tool_result", "tool_use_id": block.id, "content": str(e), "is_error": True}
    except TypeError as e:  # bad/missing arguments from the model
        return {"type": "tool_result", "tool_use_id": block.id, "content": f"Invalid arguments: {e}", "is_error": True}
    except Exception as e:
        return {"type": "tool_result", "tool_use_id": block.id, "content": f"{type(e).__name__}: {e}", "is_error": True}


