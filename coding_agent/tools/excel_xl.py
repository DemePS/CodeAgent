"""The xlwings Excel backend: Excel itself opens, changes, formats, renders and saves the workbooks.

Chosen with AGENT_EXCEL_BACKEND (see documents.excel_backend): "xlwings", "openpyxl", or "auto" (the
default: xlwings when it is installed and Excel can be started -- Windows or macOS with Excel --,
else openpyxl). What changes with Excel doing the work:
- nothing in the file is lost or damaged on save: charts, images, pivot tables, slicers, conditional
  formats, macros... (openpyxl rewrites the whole file and drops or damages them);
- formulas are recalculated at once (openpyxl leaves the old results until Excel opens the file);
- a sheet or range can be rendered as Excel shows it (view_excel), and formatted (format_excel);
- a workbook the person has open in Excel is changed in that window (if it has no unsaved changes).
Reading stays with openpyxl in both backends: it never changes the file, and it is fast.

Excel runs invisibly (one instance for the whole session, closed at exit); each workbook is closed
again after each tool call, so the person can open it meanwhile.
"""

from __future__ import annotations

import atexit
import os
import tempfile
import threading
from pathlib import Path

from ..common import ToolError

_app = None
_lock = threading.RLock()
_available: bool | None = None


def installed() -> bool:
    try:
        import xlwings  # noqa: F401
    except ImportError:
        return False
    return True


def available() -> bool:
    """Can Excel be driven here? (xlwings installed and Excel started once; the answer is kept.)"""
    global _available
    if _available is None:
        try:
            _available = installed() and app() is not None
        except Exception:
            _available = False
    return _available


def app():
    """The invisible Excel instance of this session (started on first use)."""
    global _app
    import xlwings as xw

    with _lock:
        if _app is not None:
            try:
                _app.books  # still alive?
                return _app
            except Exception:
                _app = None
        _app = xw.App(visible=False, add_book=False)
        for setting, value in (("display_alerts", False), ("screen_updating", False)):
            try:
                setattr(_app, setting, value)
            except Exception:
                pass
        return _app


@atexit.register
def quit_excel() -> None:
    global _app
    with _lock:
        if _app is not None:
            try:
                _app.quit()
            except Exception:
                pass
            _app = None


def same_file(a, b: Path) -> bool:
    try:
        return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))
    except Exception:
        return False


def open_book(p: Path):
    """(book, opened_here): the workbook as open in the person's Excel, else opened in ours.
    A workbook the person has open with unsaved changes is refused: saving would save them too."""
    import xlwings as xw

    for other in list(getattr(xw, "apps", [])):
        if other is _app or (_app is not None and getattr(other, "pid", None) is not None
                             and getattr(other, "pid", None) == getattr(_app, "pid", object())):
            continue  # our own invisible Excel
        for book in list(other.books):
            if same_file(book.fullname, p):
                try:
                    saved = bool(book.api.Saved)
                except Exception:  # cannot tell (e.g. macOS): as if it had unsaved changes
                    saved = False
                if not saved:
                    raise ToolError(f"{p.name} is open in Excel with unsaved changes: ask the person to save "
                                    "(or close) it first. Nothing was changed.")
                return book, False
    try:
        return app().books.open(str(p), update_links=False), True
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Excel could not open {p.name}: {type(e).__name__}: {e}")


def close_book(book, opened_here: bool) -> None:
    if opened_here:
        try:
            book.close()
        except Exception:
            pass


def cell_value(value):
    """A value as Excel should store it: text stays text (a leading apostrophe stops Excel from
    turning '00123' into 123 or '1/2' into a date); formulas are formulas."""
    if isinstance(value, str) and value and not value.startswith("="):
        return "'" + value
    return value


def apply_changes(p: Path, existed: bool, create_sheets: list[str], writes: list[tuple]) -> None:
    """Write the (already checked and approved) changes with Excel and save. writes: (sheet, cell,
    value, number_format)."""
    with _lock:
        if existed:
            book, opened_here = open_book(p)
        else:
            try:
                book, opened_here = app().books.add(), True
            except Exception as e:
                raise ToolError(f"Excel could not create a workbook: {type(e).__name__}: {e}")
        try:
            if not existed:  # named as openpyxl names it ("Sheet"), not by Excel's language ("Feuil1")
                book.sheets[0].name = "Sheet"
            for name in create_sheets:
                book.sheets.add(name, after=book.sheets[len(book.sheets) - 1])
            for sheet, cell, value, number_format in writes:
                rng = book.sheets[sheet].range(cell)
                if number_format:
                    rng.number_format = number_format
                if isinstance(value, str) and value.startswith("="):
                    rng.formula = value
                else:
                    rng.value = cell_value(value)
            try:
                if existed:
                    book.save()
                else:
                    book.save(str(p))
            except Exception as e:
                raise ToolError(f"Excel could not save {p.name} (locked or read-only?): {type(e).__name__}: {e}")
        finally:
            close_book(book, opened_here)


# --- format_excel ------------------------------------------------------------------------------------

XL_CONTINUOUS, XL_THIN = 1, 2
ALIGN = {"left": -4131, "center": -4108, "right": -4152}


def rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def apply_format(p: Path, sheet: str, ranges: list[str], style: dict) -> list[str]:
    """Format with Excel; returns the settings that could not be applied (as words)."""
    skipped = []
    with _lock:
        book, opened_here = open_book(p)
        try:
            ws = book.sheets[sheet]
            for address in ranges:
                rng = ws.range(address)
                if style.get("bold") is not None:
                    rng.font.bold = style["bold"]
                if style.get("italic") is not None:
                    rng.font.italic = style["italic"]
                if style.get("font_color"):
                    rng.font.color = rgb(style["font_color"])
                if style.get("fill"):
                    rng.color = rgb(style["fill"])
                if style.get("number_format"):
                    rng.number_format = style["number_format"]
                for name, action in (("wrap", lambda: setattr(rng.api, "WrapText", bool(style["wrap"]))),
                                     ("align", lambda: setattr(rng.api, "HorizontalAlignment", ALIGN[style["align"]])),
                                     ("border", lambda: setattr(rng.api.Borders, "LineStyle", XL_CONTINUOUS if style["border"] else -4142)),
                                     ("column_width", lambda: set_width(rng, style["column_width"]))):
                    if style.get(name) is None:
                        continue
                    try:
                        action()
                    except Exception:
                        skipped.append(name)
            if style.get("autofilter"):
                try:
                    ws.range(ranges[0]).api.AutoFilter(1)
                except Exception:
                    skipped.append("autofilter")
            if style.get("freeze"):
                try:
                    freeze(book, ws, style["freeze"])
                except Exception:
                    skipped.append("freeze")
            try:
                book.save()
            except Exception as e:
                raise ToolError(f"Excel could not save {p.name} (locked or read-only?): {type(e).__name__}: {e}")
        finally:
            close_book(book, opened_here)
    return skipped


def set_width(rng, width) -> None:
    if width == "auto":
        rng.columns.autofit()
    else:
        rng.column_width = float(width)


def freeze(book, ws, cell: str) -> None:
    """Freeze the rows above and the columns left of `cell` (e.g. 'A2': the first row)."""
    ws.activate()
    window = book.app.api.ActiveWindow
    window.FreezePanes = False
    window.ScrollRow = window.ScrollColumn = 1  # from the top: no old scroll position left in the view
    target = ws.range(cell)
    window.SplitRow = target.row - 1
    window.SplitColumn = target.column - 1
    window.FreezePanes = True


# --- view_excel --------------------------------------------------------------------------------------

def render(p: Path, sheet: str, address: str | None) -> tuple[bytes, str]:
    """(data, media type): a range as a PNG picture, or a whole sheet as the PDF Excel prints."""
    with _lock:
        book, opened_here = open_book(p)
        try:
            ws = book.sheets[sheet]
            with tempfile.TemporaryDirectory() as tmp:
                if address:
                    out = Path(tmp) / "range.png"
                    try:
                        ws.range(address).to_png(str(out))
                    except Exception as e:
                        raise ToolError(f"Excel could not render {sheet}!{address}: {type(e).__name__}: {e}")
                    return out.read_bytes(), "image/png"
                out = Path(tmp) / "sheet.pdf"
                try:
                    ws.to_pdf(str(out))
                except Exception as e:
                    raise ToolError(f"Excel could not render sheet {sheet}: {type(e).__name__}: {e}")
                return out.read_bytes(), "application/pdf"
        finally:
            close_book(book, opened_here)


# --- excel_add -----------------------------------------------------------------------------------------

XL_CHART_TYPES = {"column": "column_clustered", "bar": "bar_clustered", "line": "line", "pie": "pie",
                  "area": "area", "scatter": "xy_scatter"}
XL_COLUMNS = 2  # xlColumns: each column of the source is a series
XL_DATABASE = 1  # xlDatabase: a pivot cache built from a range with a header row
XL_ROW_FIELD, XL_COLUMN_FIELD = 1, 2
XL_SUMMARY = {"sum": -4157, "count": -4112, "average": -4106, "max": -4136, "min": -4139}
CHART_WIDTH, CHART_HEIGHT = 480, 288  # points: the size Excel gives a new chart


def chart_object(chart):
    """The Chart of an xlwings chart's api (Windows gives (ChartObject, Chart))."""
    api = chart.api
    return api[1] if isinstance(api, tuple) else api


def save_book(book, p: Path) -> None:
    try:
        book.save()
    except Exception as e:
        raise ToolError(f"Excel could not save {p.name} (locked or read-only?): {type(e).__name__}: {e}")


def add_chart(p: Path, sheet: str, source: str, spec: dict) -> list[str]:
    """Add a chart with Excel and save; returns the settings that could not be applied (as words)."""
    skipped = []
    with _lock:
        book, opened_here = open_book(p)
        try:
            ws = book.sheets[sheet]
            try:
                target = ws.range(spec["anchor"])
                chart = ws.charts.add(left=target.left, top=target.top, width=CHART_WIDTH, height=CHART_HEIGHT)
                chart.set_source_data(ws.range(source))
                chart.chart_type = XL_CHART_TYPES[spec["chart_type"]]
            except Exception as e:
                raise ToolError(f"Excel could not add the chart: {type(e).__name__}: {e}. Nothing was saved.")
            try:
                chart_object(chart).PlotBy = XL_COLUMNS
            except Exception:
                skipped.append("one series per column")
            if spec["title"]:
                try:
                    api = chart_object(chart)
                    api.HasTitle = True
                    api.ChartTitle.Text = spec["title"]
                except Exception:
                    skipped.append("title")
            save_book(book, p)
        finally:
            close_book(book, opened_here)
    return skipped


def add_table(p: Path, sheet: str, source: str, spec: dict) -> None:
    """Turn a range into an Excel table with Excel and save."""
    with _lock:
        book, opened_here = open_book(p)
        try:
            ws = book.sheets[sheet]
            try:
                ws.tables.add(source=ws.range(source), name=spec["name"], table_style_name=spec["style"],
                              has_headers=True)
            except Exception as e:
                raise ToolError(f"Excel could not add the table: {type(e).__name__}: {e}. Nothing was saved.")
            save_book(book, p)
        finally:
            close_book(book, opened_here)


def add_pivot_table(p: Path, sheet: str, source: str, spec: dict) -> None:
    """Build a pivot table with Excel (on a new or existing sheet) and save."""
    with _lock:
        book, opened_here = open_book(p)
        try:
            try:
                if spec["new_sheet"]:
                    target_ws = book.sheets.add(spec["target_sheet"], after=book.sheets[len(book.sheets) - 1])
                else:
                    target_ws = book.sheets[spec["target_sheet"]]
                cache = book.api.PivotCaches().Create(SourceType=XL_DATABASE,
                                                      SourceData=book.sheets[sheet].range(source).api)
                pivot = cache.CreatePivotTable(TableDestination=target_ws.range(spec["anchor"]).api,
                                               TableName=spec["name"])
                for position, name in enumerate(spec["rows"], start=1):
                    field = pivot.PivotFields(name)
                    field.Orientation, field.Position = XL_ROW_FIELD, position
                for position, name in enumerate(spec["columns"], start=1):
                    field = pivot.PivotFields(name)
                    field.Orientation, field.Position = XL_COLUMN_FIELD, position
                for name, summary in spec["values"]:
                    pivot.AddDataField(pivot.PivotFields(name), f"{summary.capitalize()} of {name}", XL_SUMMARY[summary])
            except Exception as e:
                raise ToolError(f"Excel could not build the pivot table: {type(e).__name__}: {e}. Nothing was saved.")
            save_book(book, p)
        finally:
            close_book(book, opened_here)
