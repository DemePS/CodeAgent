"""run_python_excel: a Python script that changes a workbook through Excel (xlwings), for what no
dedicated tool does. Off unless AGENT_EXCEL_SCRIPTS=on (for developers: see the README).

Excel can do far more than edit a workbook -- run macros, start programs, open and save any file --
and the run_python sandbox cannot see what Excel does. So, in order:
1. the script is checked before it runs (check_script): no macros, no programs, no other files, no
   imports beyond a few harmless modules, no dynamic tricks (eval, getattr by name, _private
   attributes) that would get around the check;
2. the person approves the script (unless autonomous mode is on), and a backup of the workbook is
   kept before it runs (restore_backup puts it back);
3. it runs on the workbook in its own invisible Excel with macros forced off, inside the run_python
   sandbox (no processes, no file writes outside the temp folder); macros it would add are refused
   (the workbook is put back);
4. the agent and the person see what changed: cells, sheets, charts, tables, pivot tables...
Limits: the check reads the script's text, it does not prove what Excel will do. It is a strong
filter, not a sandbox for Excel; turn the tool on only on machines where that is acceptable.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from .. import backups, state
from ..common import ToolError, display, is_protected, rel_name, truncate
from ..config import EXCEL_SCRIPTS, RUN_TIMEOUT_SECONDS
from ..guard import GUARD_SOURCE
from .documents import excel_backend, excel_path, load_workbook, show_cell

ALLOWED_MODULES = {"math", "statistics", "datetime", "calendar", "decimal", "fractions", "re", "string",
                   "itertools", "collections", "json"}
BANNED_NAMES = {"eval", "exec", "compile", "__import__", "open", "globals", "locals", "vars", "delattr",
                "breakpoint", "input", "memoryview", "help", "exit", "quit", "__builtins__", "__loader__",
                "__spec__"}
# Attributes (any case) that run macros or programs, open, save or export other files, reach other
# workbooks or Excel itself, or fetch external data. A name starting with "_" is refused too.
BANNED_ATTRIBUTES = {
    # macros and Excel 4 (XLM) commands
    "run", "macro", "runautomacros", "executeexcel4macro", "evaluate", "vbproject", "vbe", "macrooptions",
    "excel4macrosheets", "excel4intlmacrosheets", "registerxll", "addins", "addins2", "automationsecurity",
    "ontime", "onkey", "onrepeat", "onundo", "onsheetactivate", "onwindow",
    # programs, links, keys and commands
    "shell", "sendkeys", "followhyperlink", "follow", "execute", "executemso", "commandbars",
    "ddeinitiate", "ddeexecute", "ddepoke", "ddeterminate", "ddeappreturncode",
    "oleobjects", "oleformat", "verb", "addoleobject", "activate_microsoft_app", "activatemicrosoftapp",
    # other files, other workbooks, Excel itself
    "save", "saveas", "savecopyas", "save_as", "exportasfixedformat", "to_pdf", "to_png", "to_html", "printout",
    "open", "opentext", "openxml", "opendatabase", "openlinks", "changelink", "updatelink", "workbooks",
    "books", "app", "apps", "application", "parent", "quit", "kill", "close",
    # external data
    "querytables", "querytable", "connections", "refreshall", "webquery",
}
# Formulas that start programs (DDE: =cmd|'/c ...'!A0) or call out (WEBSERVICE, XLM CALL/EXEC/REGISTER).
BANNED_FORMULA = re.compile(r"\w+\|'?[^'!]*'?!|\b(WEBSERVICE|FILTERXML|CALL|EXEC|REGISTER|REGISTER\.ID|EXECUTE)\s*\(",
                            re.IGNORECASE)
MAX_DIFF_ROWS = 200


def check_script(code: str) -> list[str]:
    """What the script may not do, as messages (empty: it may run)."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"syntax error line {e.lineno}: {e.msg}"]
    problems = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for module in modules:
                if module.split(".")[0] not in ALLOWED_MODULES:
                    problems.append(f"line {line}: import {module} (allowed: {', '.join(sorted(ALLOWED_MODULES))})")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            problems.append(f"line {line}: {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                problems.append(f"line {line}: .{node.attr} (private attributes are not allowed)")
            elif node.attr.lower() in BANNED_ATTRIBUTES:
                problems.append(f"line {line}: .{node.attr}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("getattr", "setattr", "hasattr"):
            name = node.args[1] if len(node.args) > 1 else None
            if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
                problems.append(f"line {line}: {node.func.id} with a computed name")
            elif name.value.startswith("_") or name.value.lower() in BANNED_ATTRIBUTES:
                problems.append(f"line {line}: {node.func.id}(..., {name.value!r})")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and BANNED_FORMULA.search(node.value):
            problems.append(f"line {line}: a formula that starts programs or calls out ({node.value[:60]!r})")
    return problems


# Run in the sandbox (GUARD_SOURCE) by the agent's own Python, which has xlwings: opens the copy in an
# invisible Excel of its own with macros and events off, runs the script with `book`, saves, quits.
RUNNER = r'''
import sys
import xlwings as xw

path, script = sys.argv[1], sys.argv[2]
app = xw.App(visible=False, add_book=False)
try:
    app.display_alerts = False
    app.screen_updating = False
    for setting, value in (("AutomationSecurity", 3), ("EnableEvents", False)):  # 3: macros forced off
        try:
            setattr(app.api, setting, value)
        except Exception:
            pass
    book = app.books.open(path, update_links=False)
    with open(script, encoding="utf-8") as f:
        source = f.read()
    exec(compile(source, "<script>", "exec"), {"__name__": "__main__", "book": book})
    book.save()
    book.close()
finally:
    app.quit()
'''


def execute(p: Path, folder: Path, code: str, timeout: int) -> subprocess.CompletedProcess:
    """Run the script on the workbook, in the sandbox, with the agent's Python (it has xlwings);
    the runner and the script are written to `folder` (in the temp folder)."""
    (folder / "runner.py").write_text(RUNNER, encoding="utf-8")
    (folder / "script.py").write_text(code, encoding="utf-8")
    env = dict(os.environ, AGENT_GUARD_WORKSPACE=str(state.workspace))
    cmd = [sys.executable, "-c", GUARD_SOURCE, "script", str(folder / "runner.py"), str(p), str(folder / "script.py")]
    try:
        return subprocess.run(cmd, cwd=folder, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise ToolError(f"The script did not finish within {timeout}s and was stopped; the workbook was not saved by it "
                        "(restore_backup puts back the previous version if needed). Its Excel may still be running: "
                        "close it in the Task Manager if so.")


PARTS = {"charts": "xl/charts/chart", "pictures": "xl/media/", "Excel tables": "xl/tables/",
         "pivot tables": "xl/pivotTables/", "slicers": "xl/slicers/", "comments": "xl/comments",
         "macros": "xl/vbaProject.bin", "external links": "xl/externalLinks/", "OLE objects": "xl/embeddings/"}


def parts(p: Path) -> dict[str, int]:
    try:
        names = zipfile.ZipFile(p).namelist()
    except (zipfile.BadZipFile, OSError):
        return {}
    return {label: sum(n.startswith(prefix) for n in names) for label, prefix in PARTS.items()}


class MacrosAdded(Exception):
    pass


def compare(before: Path, after: Path) -> tuple[list[tuple], list[str]]:
    """(changed cells as (cell, old, new), other changes as words) between two versions of a workbook."""
    old_wb, new_wb = load_workbook(before), load_workbook(after)
    notes = [f"new sheet {n}" for n in new_wb.sheetnames if n not in old_wb.sheetnames]
    notes += [f"sheet {n} removed" for n in old_wb.sheetnames if n not in new_wb.sheetnames]
    rows = []
    for name in new_wb.sheetnames:
        new_ws = new_wb[name]
        old_ws = old_wb[name] if name in old_wb.sheetnames else None
        max_row = max(new_ws.max_row, old_ws.max_row if old_ws else 0)
        max_col = max(new_ws.max_column, old_ws.max_column if old_ws else 0)
        for row in range(1, max_row + 1):
            for col in range(1, max_col + 1):
                new = new_ws.cell(row=row, column=col).value
                old = old_ws.cell(row=row, column=col).value if old_ws else None
                if show_cell(old) != show_cell(new):
                    rows.append((f"{name}!{new_ws.cell(row=row, column=col).coordinate}", show_cell(old), show_cell(new), None))
    old_parts, new_parts = parts(before), parts(after)
    for label in PARTS:
        a, b = old_parts.get(label, 0), new_parts.get(label, 0)
        if a != b:
            notes.append(f"{label}: {a} -> {b}")
    if old_parts.get("macros", 0) < new_parts.get("macros", 0):
        raise MacrosAdded()
    return rows, notes


def tool_run_python_excel(path: str, code: str, timeout: int = RUN_TIMEOUT_SECONDS) -> str:
    if not EXCEL_SCRIPTS:
        raise ToolError("run_python_excel is off (AGENT_EXCEL_SCRIPTS=on turns it on). Use the Excel tools.")
    if state.excel_edit_sheets or state.excel_protect_formulas or not state.excel_allow_format:
        raise ToolError("Excel scripts are not allowed in this application; use edit_excel.")
    p = excel_path(path)
    if is_protected(p):
        raise ToolError(f"{path} is part of the coding agent's own files and cannot be modified.")
    if excel_backend() != "xlwings":
        raise ToolError("run_python_excel needs Excel (the xlwings backend). Nothing was changed.")
    problems = check_script(code)
    if problems:
        raise ToolError("This script is not allowed (it could run macros or programs, touch other files, or get "
                        "around these checks); nothing was run:\n- " + "\n- ".join(problems)
                        + "\nThe script gets `book` (the workbook, xlwings) and may import only "
                        + ", ".join(sorted(ALLOWED_MODULES)) + ".")
    name = rel_name(p)
    state.ui.panel(f"Run a Python script on {name} (with Excel, macros off; a backup is kept first)",
                   code.splitlines(), tone="run")
    if state.auto_mode:
        state.ui.status(f"(autonomous mode: running the script on {name} without asking)")
    elif state.ui.confirm(f"Run this script on {name}?") != "yes":
        feedback = state.ui.ask_text("Why not / what should change? (optional): ")
        raise ToolError("The user declined to run this script." + (f" User feedback: {feedback}" if feedback else ""))

    before = p.read_bytes()
    backup = backups.save(p, before)  # first: whatever the script does, this version can be put back
    folder = Path(tempfile.mkdtemp(prefix="agent-excel-"))
    try:
        previous = folder / f"before{p.suffix}"
        previous.write_bytes(before)
        proc = execute(p, folder, code, timeout)
        output = f"--- stdout ---\n{proc.stdout or '(empty)'}\n--- stderr ---\n{proc.stderr or '(empty)'}"
        if proc.returncode != 0:
            changed = p.read_bytes() != before
            raise ToolError(f"The script failed (exit code {proc.returncode}); {name} was "
                            + (f"changed anyway: restore_backup puts it back (saved to {backup})."
                               if changed else "not changed.") + "\n" + truncate(output))
        try:
            rows, notes = compare(previous, p)
        except MacrosAdded:
            p.write_bytes(before)
            raise ToolError(f"The script added macros to {name}; that is not allowed. {name} was put back as it was.")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if not rows and not notes:
        return f"The script ran; it changed nothing in {display(p)}.\n" + truncate(output)
    state.ui.cell_changes(f"The script changed {name} ({len(rows)} cell(s))", rows[:MAX_DIFF_ROWS],
                          max(0, len(rows) - MAX_DIFF_ROWS))
    if notes:
        state.ui.panel(f"Other changes to {name}", notes)
    state.ui.success(f"Ran the script on {name} (previous version kept; restore_backup undoes it)")
    return (f"The script changed {display(p)}: {len(rows)} cell(s)" + (f"; {', '.join(notes)}" if notes else "")
            + f". Previous version saved to {backup} (restore_backup puts it back). Check the result with "
            "read_excel / view_excel.\n" + truncate(output))
