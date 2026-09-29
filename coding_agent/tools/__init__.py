"""The tools Claude can call, by name."""

from .. import state
from ..common import ToolError
from ..skills import tool_load_skill
from ..tools.browser import tool_screenshot_page
from ..tools.documents import (
    tool_edit_excel,
    tool_read_excel,
    tool_read_pdf,
    tool_restore_backup,
    tool_view_image,
)
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
from ..tools.network import tool_clone_repo, tool_download_file
from ..tools.python_runner import tool_run_python

TOOL_HANDLERS = {
    "list_directory": tool_list_directory,
    "change_directory": tool_change_directory,
    "grep": tool_grep,
    "read_file": tool_read_file,
    "edit_file": tool_edit_file,
    "write_file": tool_write_file,
    "screenshot_page": tool_screenshot_page,
    "view_image": tool_view_image,
    "read_pdf": tool_read_pdf,
    "read_excel": tool_read_excel,
    "edit_excel": tool_edit_excel,
    "restore_backup": tool_restore_backup,
    "copy_path": tool_copy_path,
    "delete_file": tool_delete_file,
    "delete_folder": tool_delete_folder,
    "ask_human": tool_ask_human,
    "git": tool_git,
    "download_file": tool_download_file,
    "clone_repo": tool_clone_repo,
    "run_python": tool_run_python,
    "load_skill": tool_load_skill,
}


def run_tool(block) -> dict:
    """Execute one tool_use block, report the outcome to the UI, and build its tool_result."""
    result = _run(block)
    content = result["content"]
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


