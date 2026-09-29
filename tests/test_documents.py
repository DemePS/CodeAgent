"""PDF and Excel tools, including the spreadsheet-first rule."""

import openpyxl
import pytest

from coding_agent import state
from coding_agent.common import ToolError
from coding_agent.tools import documents


def make_pdf(path, pages):
    """A minimal PDF with one line of text per page (no PDF library needed)."""
    objs = ["<</Type/Catalog/Pages 2 0 R>>"]
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objs.append(f"<</Type/Pages/Kids[{kids}]/Count {len(pages)}>>")
    font = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        stream = f"BT /F1 14 Tf 20 100 Td ({text}) Tj ET"
        objs.append(f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 400 200]/Contents {4 + 2 * i} 0 R"
                    f"/Resources<</Font<</F1 {font} 0 R>>>>>>")
        objs.append(f"<</Length {len(stream)}>>stream\n{stream}\nendstream")
    objs.append("<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")
    out, offsets = "%PDF-1.4\n", []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer<</Size {len(objs) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n"
    path.write_bytes(out.encode("latin-1"))


@pytest.fixture
def invoice(workspace):
    make_pdf(workspace / "invoice.pdf", ["Invoice INV-31 Sensors 12 x 45.50", "Total 642.00 EUR", "Terms"])
    return workspace / "invoice.pdf"


@pytest.fixture
def workbook(workspace):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Costs"
    ws.append(["Item", "Qty", "Unit price", "Total"])
    ws["D2"] = "=B2*C2"
    wb.save(workspace / "costs.xlsx")
    return workspace / "costs.xlsx"


def test_pdf_text_and_page_subset(invoice):
    text = documents.tool_read_pdf("invoice.pdf", mode="text")
    assert "3 page(s)" in text and "Total 642.00 EUR" in text
    blocks = documents.tool_read_pdf("invoice.pdf", pages="1-2")
    doc = blocks[1]
    assert doc["type"] == "document" and doc["context"].startswith("pages: 2")
    with pytest.raises(ToolError, match="outside the document"):
        documents.tool_read_pdf("invoice.pdf", pages="2-9")


def test_spreadsheet_first_rule(invoice, workbook, monkeypatch):
    monkeypatch.setattr(state, "turn", {"instruction": "Fill costs.xlsx from invoice.pdf", "excel_read": False})
    with pytest.raises(ToolError, match="read_excel first"):
        documents.tool_read_pdf("invoice.pdf", mode="text")
    assert "D2==B2*C2" in documents.tool_read_excel("costs.xlsx")
    assert "642.00" in documents.tool_read_pdf("invoice.pdf", mode="text")


def test_edit_excel_diff_backup_and_values(workspace, workbook, ui, tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "BACKUP_HOME", tmp_path / "backups")
    ui.answers = ["yes"]
    result = documents.tool_edit_excel("costs.xlsx", [
        {"sheet": "Costs", "cell": "B2", "value": 12},
        {"cell": "C2", "value": 45.5, "number_format": "#,##0.00"},
        {"cell": "A9", "value": "2025-04-30", "as_date": True},
    ])
    assert "3 cell(s) changed" in result and "Previous version saved" in result
    ws = openpyxl.load_workbook(workbook)["Costs"]
    assert ws["B2"].value == 12 and ws["C2"].number_format == "#,##0.00" and ws["D2"].value == "=B2*C2"
    assert ws["A9"].value.year == 2025
    title, rows, more = ui.of("cells")[0][1:]
    assert ("Costs!B2", "", "12", None) in rows and more == 0
    assert list((tmp_path / "backups").rglob("*-costs.xlsx"))


def test_edit_excel_refusals(workspace, workbook):
    with pytest.raises(ToolError, match="No sheet"):
        documents.tool_edit_excel("costs.xlsx", [{"sheet": "Nope", "cell": "A1", "value": 1}])
    with pytest.raises(ToolError, match="Invalid cell"):
        documents.tool_edit_excel("costs.xlsx", [{"cell": "1A", "value": 1}])
    with pytest.raises(ToolError, match="only .xlsx and .xlsm"):
        documents.tool_read_excel("old.xls")


@pytest.fixture
def three_sheets(workspace):
    wb = openpyxl.Workbook()
    wb.active.title = "Data"
    for i in range(2000):  # a big tab that is not the one to fill
        wb.active.append([f"row {i}", i, i * 2.5])
    form = wb.create_sheet("Costs")
    form.append(["Item", "Qty", "Unit price"])
    wb.create_sheet("Lookup").append(["Code", "Label"])
    wb.save(workspace / "book.xlsx")
    return workspace / "book.xlsx"


def test_a_workbook_with_several_sheets_gets_an_overview_first(three_sheets):
    text = documents.tool_read_excel("book.xlsx")
    assert "3 sheets (overview" in text
    assert "--- sheet Data: 2000 rows x 3 cols ---" in text and "--- sheet Costs: 1 rows x 3 cols ---" in text
    assert "A6=row 5" in text and "A7=" not in text  # only the first rows of the big tab
    assert len(text) < 2000
    full = documents.tool_read_excel("book.xlsx", sheet="Costs")
    assert "--- sheet Costs ---" in full and "A1=Item" in full


def test_a_one_sheet_workbook_is_shown_in_full(workbook):
    text = documents.tool_read_excel("costs.xlsx")
    assert "--- sheet Costs ---" in text and "overview" not in text


def test_only_the_chosen_sheets_can_be_changed(workspace, three_sheets, ui, monkeypatch):
    monkeypatch.setattr(state, "excel_edit_sheets", {three_sheets.resolve(): {"Costs"}})
    assert "sheets to fill (the person chose them" in documents.tool_read_excel("book.xlsx")
    with pytest.raises(ToolError, match="Only these sheets of book.xlsx may be filled: Costs.*Not: Data"):
        documents.tool_edit_excel("book.xlsx", [{"sheet": "Costs", "cell": "A2", "value": "x"},
                                                {"sheet": "Data", "cell": "A2", "value": "x"}])
    with pytest.raises(ToolError, match="Not: Data"):  # no sheet named: the first sheet, Data
        documents.tool_edit_excel("book.xlsx", [{"cell": "A2", "value": "x"}])
    with pytest.raises(ToolError, match="Not: Notes"):
        documents.tool_edit_excel("book.xlsx", [], create_sheets=["Notes"])
    ui.answers = ["yes"]
    assert "1 cell(s) changed" in documents.tool_edit_excel("book.xlsx", [{"sheet": "Costs", "cell": "A2", "value": "Sensors"}])
