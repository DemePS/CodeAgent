"""Tool definitions sent to Claude (names, descriptions, input schemas)."""

from .config import (
    EXCEL_MAX_CELLS,
    MAX_SCREENSHOT_TILES,
    PDF_MAX_VISUAL_PAGES,
    RUN_TIMEOUT_SECONDS,
    WEB_SEARCH,
    WEB_SEARCH_MAX_USES,
)

TOOLS = [
    {
        "name": "load_skill",
        "description": (
            "Load the full instructions of a skill listed in the <skills> block, e.g. before an AI/LLM, "
            "Azure, backend or frontend task. Returns the skill's SKILL.md."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Skill name exactly as listed."}},
            "required": ["name"],
        },
    },
    {
        "name": "list_directory",
        "description": (
            "List the folders (ending in /) and files (with sizes) in a directory, one level deep. "
            "Hides .git, virtual environments, node_modules and caches inside the listed directory, "
            "but you can list them directly (e.g. path='.venv/lib')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to list, relative to the current directory. Defaults to '.'.",
                },
            },
        },
    },
    {
        "name": "change_directory",
        "description": (
            "Change the current directory. Later relative paths in all tools, and run_python, use it. "
            "Must stay inside the repository; '/' goes back to the repository root. Returns the new "
            "current directory relative to the root."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Target directory, relative to the current one."}},
            "required": ["path"],
        },
    },
    {
        "name": "grep",
        "description": (
            "Search file contents in the workspace with a Python regular expression. "
            "Returns matching lines as 'path:line_number: text'. Use this to locate "
            "definitions, usages, or strings before reading files. Skips .git, virtual environments "
            "(.venv), node_modules and caches, unless path points inside one of them (e.g. "
            "'.venv/lib') or include_ignored is true -- useful for reading an installed library's "
            "source while debugging."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regex to search for."},
                "path": {
                    "type": "string",
                    "description": "File or directory to search, relative to the current directory. Defaults to '.'.",
                },
                "glob": {
                    "type": "string",
                    "description": "Only search files whose name matches this glob, e.g. '*.py'.",
                },
                "ignore_case": {"type": "boolean", "description": "Case-insensitive match."},
                "include_ignored": {
                    "type": "boolean",
                    "description": "Also search virtual environments, node_modules and caches (.git is always skipped).",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "download_file",
        "description": (
            "Download a file from an http(s) URL into the workspace. The user must approve every "
            "download, in every mode including autonomous mode. If destination is an existing "
            "folder (or omitted), the file name comes from the URL. Size is capped; redirects are "
            "followed (at most 5). Returns the saved path, size, content type and SHA-256 -- read "
            "the file with read_file if you need its contents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http:// or https:// URL."},
                "destination": {
                    "type": "string",
                    "description": "File path or existing folder, relative to the current directory (default: current directory).",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "clone_repo",
        "description": (
            "Clone a git repository (https://... or git@host:owner/repo.git) into a new folder of "
            "the workspace. The user must approve every clone, in every mode including autonomous "
            "mode. Shallow (depth 1) by default; set depth to 0 for the full history. The cloned "
            "folder is a separate repository: it shows up as untracked in the project."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Repository URL (https or ssh)."},
                "destination": {
                    "type": "string",
                    "description": "New folder, relative to the current directory (default: the repository's name).",
                },
                "branch": {"type": "string", "description": "Branch or tag to check out (default: the remote's default branch)."},
                "depth": {"type": "integer", "minimum": 0, "description": "Commits of history (default 1; 0 = full history)."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "git",
        "description": (
            "Read-only git for the workspace: 'status' (branch, staged, unstaged and untracked "
            "files), 'diff' (uncommitted changes; staged=true for the index; ref to compare with a "
            "commit or range such as 'HEAD~3' or 'main...HEAD'; stat=true for a summary), 'log' "
            "(recent commits, newest first; patch=true to include each commit's diff, e.g. "
            "ref='abc123' with max_count=1 to show one commit). It cannot modify the repository. "
            "Untracked files do not appear in diff; read them with read_file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "enum": ["status", "diff", "log"]},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Limit to these files or directories (relative to the current directory).",
                },
                "ref": {
                    "type": "string",
                    "description": "diff/log: a commit, branch, tag or range (e.g. 'HEAD~1', 'main..feature').",
                },
                "staged": {"type": "boolean", "description": "diff: show staged changes (the index)."},
                "stat": {"type": "boolean", "description": "diff/log: show changed files and line counts."},
                "patch": {"type": "boolean", "description": "log: include each commit's diff."},
                "max_count": {
                    "type": "integer", "minimum": 1, "maximum": 200,
                    "description": "log: number of commits (default 20).",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a text file from the workspace. Output lines are prefixed with line numbers. "
            "Optionally pass start_line/end_line (1-indexed, inclusive) to read part of a large file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the current directory."},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Edit an existing file by replacing an exact snippet. old_string must match the file "
            "exactly (whitespace and indentation included, without read_file's line-number prefix) "
            "and must occur exactly once unless replace_all is true -- include surrounding lines "
            "to make it unique. The user is shown a unified diff and must approve before anything "
            "is written; if they decline, the result contains their feedback."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the current directory."},
                "old_string": {"type": "string", "description": "Exact text to replace."},
                "new_string": {"type": "string", "description": "Replacement text."},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence instead of requiring a unique match.",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Create a new file, or overwrite a file with the given full content. Prefer edit_file "
            "for changes to an existing file. The user is shown a "
            "unified diff and must approve before anything is written; if they decline, the "
            "result contains their feedback."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the current directory."},
                "content": {"type": "string", "description": "The complete new file content."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "screenshot_page",
        "description": (
            "Open a web page in a headless browser and return a screenshot you can see, with the "
            "page title, HTTP status, console errors/warnings and failed requests. url is an "
            "http(s) URL (e.g. the dev server at http://localhost:5173/settings) or a path to an "
            "HTML file in the workspace. localhost, private addresses and workspace files open "
            "directly; public sites need the user's approval. full_page returns up to "
            f"{MAX_SCREENSHOT_TILES} viewport-sized images from the top; selector captures one "
            "element. The browser starts fresh each time (no login session)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http(s) URL, or a workspace HTML file path."},
                "width": {"type": "integer", "minimum": 320, "maximum": 2560, "description": "Viewport width in px (default 1280; 375 for mobile)."},
                "height": {"type": "integer", "minimum": 320, "maximum": 2000, "description": "Viewport height in px (default 800)."},
                "full_page": {"type": "boolean", "description": "Capture below the fold too (up to a few screens)."},
                "selector": {"type": "string", "description": "CSS selector: capture only this element."},
                "dark_mode": {"type": "boolean", "description": "Emulate prefers-color-scheme: dark."},
                "wait_ms": {"type": "integer", "minimum": 0, "maximum": 15000, "description": "Extra wait after load, for animations or data (default 500)."},
                "include_text": {"type": "boolean", "description": "Also return the page's visible text (exact, no OCR)."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "read_pdf",
        "description": (
            "Read a PDF from the workspace. When the task is to fill a spreadsheet, call read_excel "
            "on it first to know which fields you are looking for. mode 'visual' (default) gives you the pages themselves -- "
            "you see text, tables, layout and scanned pages, like reading the document; at most "
            f"{PDF_MAX_VISUAL_PAGES} pages per call. mode 'text' returns the extracted text of the "
            "pages (cheaper for long text documents; empty for scans). pages selects pages, e.g. '3', "
            "'1-5' or '2,4,10-12' (default: all). The result starts with the page count."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "PDF path relative to the current directory."},
                "pages": {"type": "string", "description": "Pages to read, e.g. '1-5' or '2,4,10-12'."},
                "mode": {"type": "string", "enum": ["visual", "text"]},
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_excel",
        "description": (
            "Read an Excel workbook (.xlsx/.xlsm) from the workspace. Without sheet (and range), a "
            "workbook with several sheets gets an overview: each sheet's size and first rows; then read "
            "the relevant sheet(s) in full with sheet=... (a one-sheet workbook is shown in full). Cells are shown as 'A1=value'; a formula "
            "cell shows its formula and its last calculated value, e.g. 'C5==SUM(C2:C4) -> 42'. range "
            f"limits it, e.g. 'A1:F40'. At most {EXCEL_MAX_CELLS} non-empty cells per call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workbook path relative to the current directory."},
                "sheet": {"type": "string", "description": "Sheet name (default: the first sheet)."},
                "range": {"type": "string", "description": "Cell range such as 'A1:H50' (default: the used area)."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "edit_excel",
        "description": (
            "Change cells in an Excel workbook (.xlsx/.xlsm), or create a new workbook if the file "
            "does not exist. Each change sets one cell: value is a number, text, true/false, null to "
            "clear, or a formula starting with '=' (e.g. '=SUM(B2:B9)'); set as_date for an ISO date "
            "('2025-03-31') and number_format to format it (e.g. '0.00', '#,##0 €', 'dd/mm/yyyy'). "
            "The user sees a cell-by-cell diff and approves it (unless autonomous mode is on). "
            "Formulas are recalculated when the file is opened in Excel. Charts, images and pivot "
            "tables are lost when saving with this tool; the user is warned first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workbook path relative to the current directory."},
                "changes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sheet": {"type": "string", "description": "Sheet name; required when the workbook has several sheets."},
                            "cell": {"type": "string", "description": "Cell address, e.g. 'B7'."},
                            "value": {"description": "Number, text, boolean, null, or a formula starting with '='."},
                            "as_date": {"type": "boolean", "description": "Store the text value as a date."},
                            "number_format": {"type": "string", "description": "Excel number format for the cell."},
                        },
                        "required": ["cell", "value"],
                    },
                },
                "create_sheets": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Sheets to add before applying the changes.",
                },
            },
            "required": ["path", "changes"],
        },
    },
    {
        "name": "restore_backup",
        "description": (
            "Undo changes to a workbook: before each change it saves, the agent keeps a copy of the workbook "
            "(for a few days). Without `version`, lists the previous versions of the workbook with their times; "
            "with `version` (an id from that list), puts that version back -- the person approves, and the "
            "current version is kept, so the restore can be undone too. Use it when the person asks to undo "
            "your changes or go back to an earlier state; to undo a whole job, restore the oldest version "
            "from that job."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The workbook (.xlsx or .xlsm) in the workspace."},
                "version": {"type": "string", "description": "A version id from the list; omit to list them."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "view_image",
        "description": (
            "Look at an image file in the workspace (png, jpg, gif, webp) -- e.g. a design mockup "
            "or a screenshot the user saved. Returns the image so you can see it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Image path relative to the current directory."}},
            "required": ["path"],
        },
    },
    {
        "name": "copy_path",
        "description": (
            "Copy a file or a folder (with everything in it) to another place in the workspace. If "
            "destination is an existing folder, the source is copied into it. Copying a text file "
            "shows the user a diff (like write_file); a binary file or a folder shows what will be "
            "created. The user approves the copy unless autonomous mode is on. An existing folder is "
            "never overwritten; .git folders are not copied and symlinks are copied as links."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "File or folder to copy, relative to the current directory."},
                "destination": {"type": "string", "description": "New path, or an existing folder to copy into."},
            },
            "required": ["source", "destination"],
        },
    },
    {
        "name": "delete_file",
        "description": (
            "Delete one file. The user must always approve the deletion, in every mode including "
            "autonomous mode; if they refuse, the result contains their reason. Only delete what the "
            "task requires, never to work around a problem."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path relative to the current directory."}},
            "required": ["path"],
        },
    },
    {
        "name": "delete_folder",
        "description": (
            "Delete a folder and everything inside it. The user is shown its contents and must always "
            "approve, in every mode including autonomous mode; if they refuse, the result contains "
            "their reason. Refused for the workspace root, .git folders and folders containing the "
            "agent's own files. Only delete what the task requires; to remove a single file use "
            "delete_file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Folder path relative to the current directory."}},
            "required": ["path"],
        },
    },
    {
        "name": "ask_human",
        "description": (
            "Ask the user a question and wait for their answer. Use for clarifications, "
            "choosing between approaches, or information only the user has."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
    {
        "name": "run_python",
        "description": (
            "Run Python in the current directory to check code for bugs, and return the exit code, "
            "stdout and stderr. Pass either `code` (a snippet, run like `python -c`) or `args` "
            "(arguments after `python`: a script and its arguments, or -m and a module, e.g. "
            "[\"script.py\"], [\"-m\", \"pytest\", \"-q\"], [\"-m\", \"py_compile\", \"app.py\"]). "
            "The code cannot start subprocesses (subprocess, os.system, multiprocessing, ...) and cannot "
            "create, modify, rename or delete files outside the temp folder and caches -- these raise "
            "PermissionError. Use edit_file / write_file / delete_file for file changes. The user must "
            "approve every run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python source to execute."},
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Arguments passed to the Python interpreter.",
                },
                "timeout": {
                    "type": "integer",
                    "minimum": 1,
                    "description": f"Seconds before the run is killed (default {RUN_TIMEOUT_SECONDS}).",
                },
            },
        },
    },
] + (
    [] if WEB_SEARCH == "off"
    else [{"type": f"web_search_{WEB_SEARCH}", "name": "web_search", "max_uses": WEB_SEARCH_MAX_USES}]
)


# Bootstrap that run_python executes instead of the code directly. It installs a CPython audit
# hook (PEP 578) that blocks every way of starting another process, then runs the snippet,
# script or module. Audit hooks cannot be removed from Python code once installed.
# Limits: this guards Python code, not native extensions that call the OS directly -- for
# real isolation run the agent in a container.
