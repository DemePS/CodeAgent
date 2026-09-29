"""Documents: read PDFs, read and edit Excel workbooks, view images."""

import base64
import hashlib
import io
import os
import re
import time
from pathlib import Path

from .. import state
from ..common import (
    ToolError,
    display,
    is_protected,
    rel_name,
    resolve,
    resolve_readable,
    truncate,
)
from ..config import (
    BACKUP_HOME,
    EXCEL_MAX_CELLS,
    EXCEL_MAX_CHANGES,
    IMAGE_TYPES,
    MAX_IMAGE_BYTES,
    MAX_TOOL_OUTPUT_CHARS,
    PDF_MAX_VISUAL_PAGES,
)

# --- Seeing pages and images --------------------------------------------------------------------------
# Claude reads images directly, so a screenshot gives it the page's text *and* its layout, spacing
# and colors -- better than OCR. Images go back as tool_result image blocks.

def image_block(data: bytes, media_type: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                        "data": base64.b64encode(data).decode("ascii")}}


# --- PDFs and Excel workbooks ----------------------------------------------------------------------

def parse_pages(spec: str | None, count: int) -> list[int]:
    """'2,4,10-12' -> [2, 4, 10, 11, 12] (1-based), validated against the page count."""
    if not spec:
        return list(range(1, count + 1))
    pages: list[int] = []
    for part in spec.replace(" ", "").split(","):
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            raise ToolError(f"Invalid pages {spec!r}: use e.g. '3', '1-5' or '2,4,10-12'.")
        first, last = int(match.group(1)), int(match.group(2) or match.group(1))
        if not 1 <= first <= last <= count:
            raise ToolError(f"Pages {part} are outside the document (it has {count} page(s)).")
        pages.extend(p for p in range(first, last + 1) if p not in pages)
    return pages


SPREADSHEET_WORDS = re.compile(r"\.xls[xm]?\b|excel|spreadsheet|workbook|tableur|classeur", re.I)


def tool_read_pdf(path: str, pages: str | None = None, mode: str = "visual") -> list | str:
    # Spreadsheet first: know which fields are needed before reading documents.
    if SPREADSHEET_WORDS.search(state.turn["instruction"]) and not state.turn["excel_read"]:
        raise ToolError("This task involves a spreadsheet: open it with read_excel first, work out which "
                        "cells/fields must be filled (headers, units, formats, formula cells), list them, and "
                        "only then read the PDF pages that contain those fields.")
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.errors import PdfReadError
    except ImportError:
        raise ToolError("pypdf is not installed in the agent's environment (uv sync).")
    p = resolve_readable(path)
    if not p.is_file():
        raise ToolError(f"File not found: {path}")
    try:
        reader = PdfReader(str(p))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ToolError(f"{path} is password-protected.")
        count = len(reader.pages)
    except PdfReadError as e:
        raise ToolError(f"{path} is not a readable PDF: {e}")
    selected = parse_pages(pages, count)
    label = f"{display(p)}: {count} page(s); showing page(s) {pages or ('1-' + str(count))}"

    if mode == "text":
        state.ui.status(f"[pdf] {rel_name(p)} text, {len(selected)} page(s)")
        parts = []
        for n in selected:
            text = (reader.pages[n - 1].extract_text() or "").strip()
            parts.append(f"--- page {n} ---\n{text or '(no text layer: a scan or an image -- use mode visual)'}")
        return truncate(label + "\n" + "\n".join(parts))

    if len(selected) > PDF_MAX_VISUAL_PAGES:
        raise ToolError(f"{path} has {count} pages; read at most {PDF_MAX_VISUAL_PAGES} at a time in visual mode "
                        f"(e.g. pages='1-{PDF_MAX_VISUAL_PAGES}'), or use mode='text' to skim all of it first.")
    if len(selected) == count:
        data = p.read_bytes()
    else:  # only the requested pages, as a smaller PDF
        writer = PdfWriter()
        for n in selected:
            writer.add_page(reader.pages[n - 1])
        buffer = io.BytesIO()
        writer.write(buffer)
        data = buffer.getvalue()
    if len(data) > 20 * 1024 * 1024:
        raise ToolError(f"The selected pages are {len(data):,} bytes (limit 20 MB); read fewer pages at a time.")
    state.ui.status(f"[pdf] {rel_name(p)} ({len(selected)} of {count} page(s), {len(data):,} bytes)")
    return [
        {"type": "text", "text": label + (" (page numbers inside the document below restart at 1)"
                                          if len(selected) != count else "")},
        {"type": "document", "title": display(p), "context": f"pages: {len(selected)} ({pages or 'all'} of {count})",
         "source": {"type": "base64", "media_type": "application/pdf",
                    "data": base64.b64encode(data).decode("ascii")}},
    ]


def excel_path(path: str, must_exist: bool = True, readable: bool = False) -> Path:
    """A workbook path: readable anywhere the agent may read, writable only in the workspace."""
    p = resolve_readable(path) if readable else resolve(path)
    if p.suffix.lower() not in (".xlsx", ".xlsm"):
        hint = " Save it as .xlsx in Excel first." if p.suffix.lower() == ".xls" else ""
        raise ToolError(f"{path}: only .xlsx and .xlsm workbooks are supported.{hint} For .csv use read_file/edit_file.")
    if must_exist and not p.is_file():
        raise ToolError(f"File not found: {path}")
    return p


def load_workbook(p: Path, **options):
    try:
        import openpyxl
    except ImportError:
        raise ToolError("openpyxl is not installed in the agent's environment (uv sync).")
    try:
        return openpyxl.load_workbook(str(p), keep_vba=p.suffix.lower() == ".xlsm", **options)
    except PermissionError:
        raise ToolError(f"{rel_name(p)} is locked by another program (is it open in Excel?). Ask the user to close it.")
    except Exception as e:
        raise ToolError(f"{rel_name(p)} could not be opened as a workbook: {type(e).__name__}: {e}")


def show_cell(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    text = str(value)
    return text if len(text) <= 200 else text[:200] + "..."


def tool_read_excel(path: str, sheet: str | None = None, range: str | None = None) -> str:
    state.turn["excel_read"] = True  # any attempt counts: the workbook may not exist yet (to be created)
    p = excel_path(path, readable=True)
    formulas = load_workbook(p)                  # formulas as written
    values = load_workbook(p, data_only=True)    # the values Excel calculated last time it saved
    names = formulas.sheetnames
    ws_name = sheet or names[0]
    if ws_name not in names:
        raise ToolError(f"No sheet {ws_name!r}. Sheets: {', '.join(names)}")
    ws, wv = formulas[ws_name], values[ws_name]
    lines = [f"{display(p)} -- sheets: " + "; ".join(
        f"{n} ({formulas[n].max_row} rows x {formulas[n].max_column} cols)" for n in names)]
    try:
        cells = ws[range] if range else ws.iter_rows()
    except ValueError:
        raise ToolError(f"Invalid range {range!r}; use e.g. 'A1:F40'.")
    if range and not isinstance(cells, tuple):  # a single cell
        rows = ((cells,),)
    elif range and cells and not isinstance(cells[0], tuple):  # a single column such as "A:A"
        rows = tuple((c,) for c in cells)
    else:
        rows = cells
    if ws.merged_cells.ranges:
        lines.append("merged: " + ", ".join(str(r) for r in list(ws.merged_cells.ranges)[:50]))
    lines.append(f"--- sheet {ws_name}" + (f" range {range}" if range else "") + " ---")
    shown = size = 0

    def rows_seen_label(row) -> str:
        return str(next((c.row for c in row if hasattr(c, "row")), "?"))

    for row in rows:
        parts = []
        for c in row:
            if c.value is None or not hasattr(c, "coordinate"):
                continue
            if isinstance(c.value, str) and c.value.startswith("="):
                cached = wv[c.coordinate].value
                parts.append(f"{c.coordinate}={c.value} -> {show_cell(cached) if cached is not None else '(not calculated)'}")
            else:
                fmt = f" [{c.number_format}]" if c.number_format not in ("General", None) else ""
                parts.append(f"{c.coordinate}={show_cell(c.value)}{fmt}")
        if parts:
            lines.append(" | ".join(parts))
            shown += len(parts)
            size += len(lines[-1]) + 1
            if shown >= EXCEL_MAX_CELLS or size > MAX_TOOL_OUTPUT_CHARS - 1000:
                last = rows_seen_label(row)
                lines.append(f"... stopped at row {last} after {shown} cells; read the rest with a range "
                             f"starting below row {last}")
                break
    if shown == 0:
        lines.append("(no values)")
    state.ui.status(f"[excel] {rel_name(p)} sheet {ws_name}{' ' + range if range else ''}: {shown} cell(s)")
    return truncate("\n".join(lines))


def lossy_features(p: Path) -> list[str]:
    """Parts of an .xlsx that openpyxl drops or damages when it saves the file."""
    import zipfile
    try:
        names = zipfile.ZipFile(p).namelist()
    except (zipfile.BadZipFile, OSError):
        return []
    found = []
    for prefix, label in (("xl/charts/", "charts"), ("xl/media/", "images"), ("xl/pivotTables/", "pivot tables"),
                          ("xl/slicers/", "slicers"), ("xl/externalLinks/", "links to other workbooks"),
                          ("xl/threadedComments/", "threaded comments"), ("xl/ctrlProps/", "form controls")):
        if any(n.startswith(prefix) for n in names):
            found.append(label)
    return found


def tool_edit_excel(path: str, changes: list, create_sheets: list | None = None) -> str:
    import datetime
    p = excel_path(path, must_exist=False)
    if is_protected(p):
        raise ToolError(f"{path} is part of the coding agent's own files and cannot be modified.")
    if not changes and not create_sheets:
        raise ToolError("No changes given.")
    if len(changes) > EXCEL_MAX_CHANGES:
        raise ToolError(f"At most {EXCEL_MAX_CHANGES} cells per call; split the changes.")
    existed = p.exists()
    before = p.read_bytes() if existed else None
    if existed:
        wb = load_workbook(p)
    else:
        import openpyxl
        wb = openpyxl.Workbook()
    name = rel_name(p)

    for sheet_name in create_sheets or []:
        if sheet_name in wb.sheetnames:
            raise ToolError(f"Sheet {sheet_name!r} already exists.")
        wb.create_sheet(sheet_name)
    rows = []
    for change in changes:
        sheet_name = change.get("sheet") or wb.sheetnames[0]
        if sheet_name not in wb.sheetnames:
            raise ToolError(f"No sheet {sheet_name!r} in {name}. Sheets: {', '.join(wb.sheetnames)} "
                            "(add it with create_sheets).")
        ws = wb[sheet_name]
        coord = str(change.get("cell", "")).upper().strip()
        if not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", coord):
            raise ToolError(f"Invalid cell {change.get('cell')!r}; use an address such as 'B7'.")
        cell = ws[coord]
        if type(cell).__name__ == "MergedCell":
            raise ToolError(f"{sheet_name}!{coord} is inside a merged range; write to its top-left cell instead.")
        value = change.get("value")
        if change.get("as_date") and isinstance(value, str):
            try:
                value = datetime.date.fromisoformat(value[:10]) if len(value) <= 10 else datetime.datetime.fromisoformat(value)
            except ValueError:
                raise ToolError(f"{sheet_name}!{coord}: {value!r} is not an ISO date (YYYY-MM-DD).")
        if isinstance(value, (dict, list)):
            raise ToolError(f"{sheet_name}!{coord}: a cell value must be a number, text, boolean or null.")
        old = cell.value
        cell.value = value
        if change.get("number_format"):
            cell.number_format = change["number_format"]
        if show_cell(old) != show_cell(value) or change.get("number_format"):
            rows.append((f"{sheet_name}!{coord}", show_cell(old), show_cell(value), change.get("number_format")))

    lossy = lossy_features(p) if existed else []
    state.ui.cell_changes(f"{'Modify' if existed else 'Create'} workbook {name} ({len(rows)} cell(s)"
                          f"{', new sheets: ' + ', '.join(create_sheets) if create_sheets else ''})",
                          rows[:200], max(0, len(rows) - 200))
    if lossy:
        state.ui.warning(f"{name} contains {', '.join(lossy)}; saving with this tool removes or damages "
                         "them. A backup is kept, but check the file in Excel afterwards.")
    question = f"Apply these cell changes to {name}?" if existed else f"Create {name}?"
    if state.auto_mode and not lossy:
        state.ui.status(f"(autonomous mode: {'modifying' if existed else 'creating'} {name} without asking)")
    else:  # lossy saves always ask, even in autonomous mode
        if state.auto_mode:
            state.ui.status("(autonomous mode: this save can lose content, so it needs your approval)")
        if state.ui.confirm(question) != "yes":
            feedback = state.ui.ask_text(f"Why not / what should change in {name}? (optional): ")
            state.ui.failure(f"{name} was not {'modified' if existed else 'created'}")
            raise ToolError(f"The user rejected this change; {name} was NOT {'modified' if existed else 'created'}."
                            + (f" User feedback: {feedback}" if feedback else ""))

    if (p.read_bytes() if p.exists() else None) != before:
        state.ui.failure(f"{name} changed on disk while waiting for approval -- not written")
        raise ToolError(f"{name} was changed on disk (probably saved in Excel) after the diff was shown; nothing "
                        "was written. Read it again and redo the changes.")
    backup = None
    if existed:  # a copy of the previous version, outside the project
        project = f"{state.workspace.name}-{hashlib.sha256(str(state.workspace).encode()).hexdigest()[:8]}"
        backup = BACKUP_HOME / project / f"{time.strftime('%Y%m%d-%H%M%S')}-{p.name}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(before)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.agent-tmp")
    try:
        wb.save(str(tmp))
        os.replace(tmp, p)
    except PermissionError:
        raise ToolError(f"{name} could not be written: it is locked (open in Excel?). Ask the user to close it.")
    finally:
        if tmp.exists():
            tmp.unlink()
    state.ui.success(f"{'Modified' if existed else 'Created'} {name} ({len(rows)} cell(s))")
    return (f"{'Modified' if existed else 'Created'} {display(p)}: {len(rows)} cell(s) changed."
            + (f" Previous version saved to {backup}." if backup else "")
            + (f" Warning: {', '.join(lossy)} may have been lost." if lossy else "")
            + (" Formulas are recalculated when the file is opened in Excel." if any(
                isinstance(r[2], str) and r[2].startswith("=") for r in rows) else ""))


def tool_view_image(path: str) -> list:
    p = resolve_readable(path)
    if not p.is_file():
        raise ToolError(f"File not found: {path}")
    media_type = IMAGE_TYPES.get(p.suffix.lower())
    if media_type is None:
        raise ToolError(f"Not a supported image ({', '.join(IMAGE_TYPES)}). For SVG, read it with read_file.")
    data = p.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise ToolError(f"{path} is {len(data):,} bytes; images must be under {MAX_IMAGE_BYTES:,}. "
                        "Ask the user for a smaller export.")
    state.ui.status(f"[image] {rel_name(p)} ({len(data):,} bytes)")
    return [{"type": "text", "text": f"{display(p)} ({len(data):,} bytes):"}, image_block(data, media_type)]


