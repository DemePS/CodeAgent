"""A stand-in for xlwings, backed by openpyxl: what the xlwings backend asks Excel to do really
happens to the file, so the backend's own logic (checks, text kept as text, saving, workbooks open
in the person's Excel, formatting, rendering) can be tested without Excel. It proves nothing about
Excel itself: scripts/compare_excel_backends.py does that on a PC with Excel."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import openpyxl
from openpyxl.styles import PatternFill
from openpyxl.utils import get_column_letter, range_boundaries

apps: list = []  # Excel instances already running (the person's), besides the one the backend starts


def minimal_pdf() -> bytes:
    from pypdf import PdfWriter
    import io

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class Font:
    def __init__(self, rng):
        self._rng = rng

    def _set(self, **kw):
        from copy import copy
        for cell in self._rng.cells():
            font = copy(cell.font)
            for k, v in kw.items():
                setattr(font, k, v)
            cell.font = font

    bold = property(lambda self: None, lambda self, v: self._set(b=v))
    italic = property(lambda self: None, lambda self, v: self._set(i=v))
    color = property(lambda self: None, lambda self, rgb: self._set(color="FF%02X%02X%02X" % rgb))


class Range:
    def __init__(self, sheet, address):
        self.sheet, self.address = sheet, address
        self.api = SimpleNamespace(WrapText=None, HorizontalAlignment=None, Borders=SimpleNamespace(LineStyle=None),
                                   AutoFilter=lambda *a: sheet.calls.append(("autofilter", address)))
        self.columns = SimpleNamespace(autofit=lambda: sheet.calls.append(("autofit", address)))

    def cells(self):
        ws = self.sheet.ws
        if ":" not in self.address:
            return [ws[self.address]]
        return [c for row in ws[self.address] for c in row]

    @property
    def row(self):
        return self.cells()[0].row

    @property
    def column(self):
        return self.cells()[0].column

    @property
    def value(self):
        return self.cells()[0].value

    @value.setter
    def value(self, v):
        if isinstance(v, str) and v.startswith("'"):  # Excel keeps the text, not the apostrophe
            v = v[1:]
        elif isinstance(v, str):  # what Excel does to text typed without an apostrophe
            try:
                v = float(v) if "." in v else int(v)
            except ValueError:
                pass
        self.cells()[0].value = v

    @property
    def formula(self):
        return self.cells()[0].value

    @formula.setter
    def formula(self, f):
        self.cells()[0].value = f

    @property
    def number_format(self):
        return self.cells()[0].number_format

    @number_format.setter
    def number_format(self, f):
        for cell in self.cells():
            cell.number_format = f

    @property
    def font(self):
        return Font(self)

    @property
    def color(self):
        return None

    @color.setter
    def color(self, rgb):
        hex_ = "FF%02X%02X%02X" % rgb
        for cell in self.cells():
            cell.fill = PatternFill(fill_type="solid", fgColor=hex_)

    @property
    def column_width(self):
        return None

    @column_width.setter
    def column_width(self, width):
        min_col, _, max_col, _ = range_boundaries(self.address)
        for c in range(min_col, max_col + 1):
            self.sheet.ws.column_dimensions[get_column_letter(c)].width = width

    def to_png(self, path):
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"range " + self.address.encode())


class Sheet:
    def __init__(self, book, ws):
        self.book, self.ws, self.calls = book, ws, []

    @property
    def name(self):
        return self.ws.title

    @name.setter
    def name(self, value):
        self.ws.title = value

    def range(self, address):
        return Range(self, address)

    def activate(self):
        self.book.app.active_sheet = self

    def to_pdf(self, path):
        Path(path).write_bytes(minimal_pdf())


class Sheets:
    def __init__(self, book):
        self.book = book

    def _all(self):
        return [Sheet(self.book, ws) for ws in self.book.wb.worksheets]

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._all()[key]
        return Sheet(self.book, self.book.wb[key])

    def __len__(self):
        return len(self.book.wb.worksheets)

    def add(self, name, after=None):
        self.book.wb.create_sheet(name)
        return self[name]


class Book:
    def __init__(self, app, path=None, saved=True):
        self.app, self.path = app, Path(path) if path else None
        self.wb = openpyxl.load_workbook(path) if path else openpyxl.Workbook()
        if not path:
            self.wb.active.title = "Feuil1"  # a French Excel
        self.api = SimpleNamespace(Saved=saved)
        self.closed = False
        self.saves = 0

    @property
    def fullname(self):
        return str(self.path) if self.path else "Classeur1"

    @property
    def sheets(self):
        return Sheets(self)

    def save(self, path=None):
        if path:
            self.path = Path(path)
        self.wb.save(str(self.path))
        self.saves += 1
        self.app.saved.append(self.path.name)

    def close(self):
        self.closed = True
        self.app.open_books.remove(self)


class Books:
    def __init__(self, app):
        self.app = app

    def open(self, path, update_links=None):
        book = Book(self.app, path)
        self.app.open_books.append(book)
        return book

    def add(self):
        book = Book(self.app)
        self.app.open_books.append(book)
        return book

    def __iter__(self):
        return iter(list(self.app.open_books))


class App:
    started = 0

    def __init__(self, visible=True, add_book=True):
        App.started += 1
        self.visible, self.open_books, self.saved = visible, [], []
        self.display_alerts = self.screen_updating = True
        self.api = SimpleNamespace(ActiveWindow=SimpleNamespace(FreezePanes=False, SplitRow=0, SplitColumn=0))
        self.quit_called = False

    @property
    def books(self):
        return Books(self)

    def quit(self):
        self.quit_called = True
