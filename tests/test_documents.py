"""PDF and Excel tools, including the spreadsheet-first rule."""

import sys

import openpyxl
import pytest

from coding_agent import backups, state
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


@pytest.fixture
def scan(workspace):
    """A PDF whose pages have no text layer, like a scan."""
    make_pdf(workspace / "scan.pdf", ["", "", ""])
    return workspace / "scan.pdf"


def test_pdf_text_and_page_subset(invoice, scan, monkeypatch):
    monkeypatch.setattr(documents, "uses_deepseek", lambda: False)  # the Claude path sends the PDF itself
    text = documents.tool_read_pdf("invoice.pdf", mode="text")
    assert "3 page(s)" in text and "Total 642.00 EUR" in text
    blocks = documents.tool_read_pdf("scan.pdf", pages="1-2", mode="visual")  # visual mode, for pages without a text layer
    doc = blocks[1]
    assert doc["type"] == "document" and doc["context"].startswith("pages: 2")
    with pytest.raises(ToolError, match="outside the document"):
        documents.tool_read_pdf("scan.pdf", pages="2-9", mode="visual")


def test_visual_mode_is_refused_for_pages_that_have_a_text_layer(invoice, scan, monkeypatch):
    for deepseek in (True, False):
        monkeypatch.setattr(documents, "uses_deepseek", lambda d=deepseek: d)
        with pytest.raises(ToolError, match=r"text layer: read them with mode='text'"):
            documents.tool_read_pdf("invoice.pdf", pages="1-2", mode="visual")
        with pytest.raises(ToolError, match="mode='text'"):
            documents.tool_read_pdf("invoice.pdf", pages="1-2", mode="visual")
    monkeypatch.setattr(documents, "uses_deepseek", lambda: False)
    assert documents.tool_read_pdf("scan.pdf", pages="1", mode="visual")[1]["type"] == "document"  # a scan is still read as pages
    assert "page(s)" in documents.tool_read_pdf("invoice.pdf", pages="1-2")  # the default mode is text


def test_spreadsheet_first_rule(invoice, workbook, monkeypatch):
    monkeypatch.setattr(state, "excel_first", True)
    monkeypatch.setattr(state, "turn", {"instruction": "Fill costs.xlsx from invoice.pdf", "excel_read": False})
    with pytest.raises(ToolError, match="read_excel first"):
        documents.tool_read_pdf("invoice.pdf", mode="text")
    assert "D2==B2*C2" in documents.tool_read_excel("costs.xlsx")
    assert "642.00" in documents.tool_read_pdf("invoice.pdf", mode="text")


def test_no_spreadsheet_rule_by_default(invoice, workbook, monkeypatch):
    monkeypatch.setattr(state, "turn", {"instruction": "Summarize invoice.pdf for my Excel report", "excel_read": False})
    assert "642.00" in documents.tool_read_pdf("invoice.pdf", mode="text")


def test_spreadsheet_rule_needs_read_excel_among_the_tools(invoice, workbook, monkeypatch):
    monkeypatch.setattr(state, "excel_first", True)
    monkeypatch.setattr(state, "tool_names", {"read_pdf"})
    monkeypatch.setattr(state, "turn", {"instruction": "Fill costs.xlsx from invoice.pdf", "excel_read": False})
    assert "642.00" in documents.tool_read_pdf("invoice.pdf", mode="text")


def test_edit_excel_diff_backup_and_values(workspace, workbook, ui, tmp_path, monkeypatch):
    monkeypatch.setattr(backups, "BACKUP_HOME", tmp_path / "backups")
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
    assert "sheets: Data (2000x3); Costs (1x3); Lookup (1x2)" in text and "[Data]" in text and "[Costs]" in text
    assert "A6=row 5" in text and "A7=" not in text  # only the first rows of the big tab
    assert len(text) < 2000
    full = documents.tool_read_excel("book.xlsx", sheet="Costs")
    assert "--- sheet Costs ---" in full and "A1=Item" in full
    assert "sheet Costs (1 rows x 3 cols); sheets: Data (2000 rows x 3 cols)" in full  # few sheets: all listed


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



@pytest.fixture
def many_sheets(workspace):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for s in range(86):
        ws = wb.create_sheet(f"{s} - Questionnaire part {s}")
        for r in range(1, 40):
            ws.append([f"Question {r} of part {s}: " + "long wording " * 8, "Yes/No", f"=IF(B{r}=\"Yes\",1,0)"])
    wb.save(workspace / "big.xlsx")
    return workspace / "big.xlsx"


def test_a_workbook_with_many_sheets_gets_a_bounded_overview(many_sheets):
    text = documents.tool_read_excel("big.xlsx")
    assert len(text) <= documents.OVERVIEW_BUDGET + 500
    assert "85 - Questionnaire part 85 (39x3)" in text  # every sheet is at least listed
    assert "[0 - Questionnaire part 0]" in text and "A2=" not in text  # one row per sheet with many sheets
    assert "…" in text  # long values cut


def test_a_small_read_stays_small_and_the_workbook_is_loaded_once(many_sheets, monkeypatch):
    loads = []
    real = documents.load_workbook
    monkeypatch.setattr(documents, "load_workbook", lambda p, **o: loads.append(o) or real(p, **o))
    documents._READ_CACHE.clear()
    one = documents.tool_read_excel("big.xlsx", sheet="1 - Questionnaire part 1", range="A4:A4")
    assert len(one) < 600 and "one of 86 sheets" in one and "A4=Question 4" in one
    documents.tool_read_excel("big.xlsx", sheet="2 - Questionnaire part 2", range="A1:A3")
    documents.tool_read_excel("big.xlsx")
    assert loads == [{"data_only": False}]  # no formula shown: the calculated values were never loaded
    documents.tool_read_excel("big.xlsx", sheet="3 - Questionnaire part 3", range="C1:C2")  # formulas
    assert loads == [{"data_only": False}, {"data_only": True}]


def test_a_changed_workbook_is_read_again(workspace, workbook, ui):
    documents._READ_CACHE.clear()
    assert "B2=12" not in documents.tool_read_excel("costs.xlsx")
    ui.answers = ["yes"]
    documents.tool_edit_excel("costs.xlsx", [{"sheet": "Costs", "cell": "B2", "value": 12}])
    assert "B2=12" in documents.tool_read_excel("costs.xlsx")


def test_restore_backup_lists_and_puts_back_a_previous_version(workspace, workbook, ui, tmp_path, monkeypatch):
    monkeypatch.setattr(backups, "BACKUP_HOME", tmp_path / "backups")
    with pytest.raises(ToolError, match="No previous version of costs.xlsx"):
        documents.tool_restore_backup("costs.xlsx")
    ui.answers = ["yes"]
    documents.tool_edit_excel("costs.xlsx", [{"sheet": "Costs", "cell": "B2", "value": 12}])  # keeps the original
    listing = documents.tool_restore_backup("costs.xlsx")
    [version] = [v["id"] for v in backups.versions("costs.xlsx")]
    assert f"version={version!r}" in listing and "as it was before the change of" in listing

    ui.answers = ["no", "keep it"]  # refused: nothing changes
    with pytest.raises(ToolError, match="rejected the restore.*keep it"):
        documents.tool_restore_backup("costs.xlsx", version)
    assert openpyxl.load_workbook(workbook)["Costs"]["B2"].value == 12

    ui.answers = ["yes"]
    result = documents.tool_restore_backup("costs.xlsx", version)
    assert "is back to its version from before the change of" in result
    assert openpyxl.load_workbook(workbook)["Costs"]["B2"].value is None  # the original again
    assert len(backups.versions("costs.xlsx")) == 2  # the edited version was kept: the restore can be undone
    with pytest.raises(ToolError, match="not a previous version"):
        documents.tool_restore_backup("costs.xlsx", "20200101-000000-costs.xlsx")


def test_backups_belong_to_their_folder_and_file(tmp_path, monkeypatch):
    monkeypatch.setattr(backups, "BACKUP_HOME", tmp_path / "backups")
    a, b = tmp_path / "a" / "project", tmp_path / "b" / "project"  # same name, different folders
    for ws in (a, b):
        ws.mkdir(parents=True)
    backups.save(a / "x.xlsx", b"old x", a)
    backups.save(a / "y.xlsx", b"old y", a)
    assert backups.folder(a) != backups.folder(b)
    assert [v["id"][16:] for v in backups.versions("x.xlsx", a)] == ["x.xlsx"]
    assert backups.versions("x.xlsx", b) == []


def test_edit_excel_needs_the_sheet_when_there_are_several(workspace, ui, tmp_path, monkeypatch):
    monkeypatch.setattr(backups, "BACKUP_HOME", tmp_path / "backups")
    wb = openpyxl.Workbook()
    wb.active.title = "Instructions"
    wb.create_sheet("Questionnaire")
    wb.save(workspace / "q.xlsx")
    with pytest.raises(ToolError, match="several sheets.*give 'sheet' for every change"):
        documents.tool_edit_excel("q.xlsx", [{"cell": "D2", "value": "Yes"}])  # would have gone to Instructions
    ui.answers = ["yes"]
    documents.tool_edit_excel("q.xlsx", [{"sheet": "Questionnaire", "cell": "D2", "value": "Yes"}])
    assert openpyxl.load_workbook(workspace / "q.xlsx")["Questionnaire"]["D2"].value == "Yes"


def test_applications_can_protect_formulas_and_the_columns(workspace, ui, tmp_path, monkeypatch):
    monkeypatch.setattr(backups, "BACKUP_HOME", tmp_path / "backups")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Q"
    ws.append(["#", "Question", "Answer"])
    ws["C3"] = "=B2"
    wb.save(workspace / "q.xlsx")
    monkeypatch.setattr(state, "excel_protect_formulas", True)
    monkeypatch.setattr(state, "excel_max_columns", {(workspace / "q.xlsx").resolve(): {"Q": 3}})
    with pytest.raises(ToolError, match="formulas may not be written"):
        documents.tool_edit_excel("q.xlsx", [{"sheet": "Q", "cell": "C2", "value": "=MID(B2,200,600)"}])
    with pytest.raises(ToolError, match="holds a formula.*may not be changed or cleared"):
        documents.tool_edit_excel("q.xlsx", [{"sheet": "Q", "cell": "C3", "value": None}])
    with pytest.raises(ToolError, match="outside the sheet's columns \\(A to C\\)"):
        documents.tool_edit_excel("q.xlsx", [{"sheet": "Q", "cell": "I2", "value": "note"}])
    ui.answers = ["yes"]
    documents.tool_edit_excel("q.xlsx", [{"sheet": "Q", "cell": "C2", "value": "Yes"}])  # a value in its column
    assert openpyxl.load_workbook(workspace / "q.xlsx")["Q"]["C2"].value == "Yes"


def test_read_pdf_visual_deepseek_uses_images(invoice, scan, monkeypatch):
    monkeypatch.setattr(documents, "uses_deepseek", lambda: True)
    monkeypatch.setattr(documents, "render_pdf_pages", lambda p, sel: [(b"png", "image/png")] * len(sel))
    blocks = documents.tool_read_pdf("scan.pdf", pages="1-2", mode="visual")
    assert blocks[0]["type"] == "text"
    assert [b["type"] for b in blocks[1:]] == ["image", "image"]
    assert "Total 642.00 EUR" in documents.tool_read_pdf("invoice.pdf", mode="text")


def test_read_pdf_visual_deepseek_without_renderer_says_how_to_install_it(invoice, scan, monkeypatch):
    monkeypatch.setattr(documents, "uses_deepseek", lambda: True)
    monkeypatch.setitem(sys.modules, "pypdfium2", None)  # makes `import pypdfium2` raise ImportError
    with pytest.raises(ToolError, match=r"codeagent\[pdf-image\]"):
        documents.tool_read_pdf("scan.pdf", mode="visual")


def test_search_pdf_gives_the_pages_and_a_snippet_of_every_match(invoice):
    out = documents.tool_search_pdf("invoice.pdf", "total")
    assert "1 match(es)" in out and "page 2:" in out and "Total 642.00 EUR" in out
    assert "page 1:" not in out and "read_pdf" in out


def test_search_pdf_ignores_case_and_line_breaks(workspace):
    make_pdf(workspace / "fr.pdf", ["Le contrat de Renassur", "assurance   habitation"])
    assert "page 1:" in documents.tool_search_pdf("fr.pdf", "RENASSUR")
    assert "page 2:" in documents.tool_search_pdf("fr.pdf", "assurance habitation")      # the spaces count as one


def test_the_search_folds_accents_without_changing_the_length():
    folded = documents._fold("Sénégalaise ÉTÉ œuvre ﬁn")
    assert folded.startswith("senegalaise ete ") and len(folded) == len("Sénégalaise ÉTÉ œuvre ﬁn")


def test_search_pdf_regex_pages_and_limits(workspace):
    make_pdf(workspace / "n.pdf", [f"Article {n} text" for n in range(1, 31)])
    out = documents.tool_search_pdf("n.pdf", r"Article \d+", regex=True)
    assert "30 match(es)" in out and "page 20:" in out and "page 21:" not in out and "10 more match(es)" in out
    assert "2 match(es)" in documents.tool_search_pdf("n.pdf", "article", pages="5-6")
    assert "page 5:" in documents.tool_search_pdf("n.pdf", "Article 5 ")
    with pytest.raises(ToolError, match="Invalid regular expression"):
        documents.tool_search_pdf("n.pdf", "(", regex=True)
    with pytest.raises(ToolError, match="empty"):
        documents.tool_search_pdf("n.pdf", "  ")


def test_search_pdf_says_when_nothing_matches_and_reads_the_text_once(invoice, monkeypatch):
    assert "0 match(es)" in documents.tool_search_pdf("invoice.pdf", "zebra") and "No match" in documents.tool_search_pdf("invoice.pdf", "zebra")
    documents._PDF_TEXT_CACHE.clear()
    reads = []
    real = documents.pdf_page_texts.__wrapped__ if hasattr(documents.pdf_page_texts, "__wrapped__") else None
    import pypdfium2
    original = pypdfium2.PdfDocument
    monkeypatch.setattr(pypdfium2, "PdfDocument", lambda *a, **k: (reads.append(1), original(*a, **k))[1])
    documents.tool_search_pdf("invoice.pdf", "invoice")
    documents.tool_search_pdf("invoice.pdf", "terms")
    assert len(reads) == 1


def test_search_pdf_lists_the_pages_without_a_text_layer(workspace, monkeypatch):
    monkeypatch.setattr(documents.ocr, "available", lambda: False)
    make_pdf(workspace / "mixed.pdf", ["Some words", ""])
    out = documents.tool_search_pdf("mixed.pdf", "words")
    assert "page 1:" in out and "1 scanned page(s) have no text yet and were NOT searched" in out and "tesseract" in out


class FakeTesseract:
    """Stands for pytesseract: reads every page as the same words, and counts the pages it was asked to read."""
    class TesseractError(Exception):
        pass
    pages_read = 0

    @classmethod
    def get_tesseract_version(cls):
        return "5.0"

    @classmethod
    def image_to_string(cls, image, lang=None):
        cls.pages_read += 1
        return "Le   delai de\nprescription est de deux ans"


@pytest.fixture
def fake_ocr(tmp_path, monkeypatch):
    import sys
    FakeTesseract.pages_read = 0
    monkeypatch.setitem(sys.modules, "pytesseract", FakeTesseract)
    monkeypatch.setattr(documents.ocr, "OCR_HOME", tmp_path / "ocr")
    documents._PDF_TEXT_CACHE.clear()
    return FakeTesseract


def test_search_pdf_reads_scanned_pages_with_ocr_and_keeps_the_text(workspace, fake_ocr):
    make_pdf(workspace / "mixed.pdf", ["Some words", "", ""])
    out = documents.tool_search_pdf("mixed.pdf", "prescription")
    assert "page 2:" in out and "page 3:" in out and "2 scanned page(s) were searched through OCR" in out
    assert fake_ocr.pages_read == 2
    documents.tool_search_pdf("mixed.pdf", "deux ans")  # the second search reads nothing again
    assert fake_ocr.pages_read == 2


def test_search_pdf_does_not_ocr_more_pages_than_one_call_may(workspace, fake_ocr, monkeypatch):
    monkeypatch.setattr(documents, "OCR_MAX_PAGES_PER_CALL", 2)
    make_pdf(workspace / "scans.pdf", ["", "", "", ""])
    out = documents.tool_search_pdf("scans.pdf", "prescription")
    assert fake_ocr.pages_read == 0 and "4 scanned page(s) have no text yet and were NOT searched" in out and "coding-agent --ocr" in out
    out = documents.tool_search_pdf("scans.pdf", "prescription", pages="1-2")  # a small range is read
    assert fake_ocr.pages_read == 2 and "page 1:" in out and "page 2:" in out


def test_read_pdf_text_mode_gives_the_ocr_text_of_a_scanned_page(workspace, fake_ocr):
    make_pdf(workspace / "mixed.pdf", ["Some words", ""])
    out = documents.tool_read_pdf("mixed.pdf", mode="text")
    assert "Some words" in out and "page 2 (read by OCR" in out and "Le delai de prescription est de deux ans" in out


def test_search_pdf_matches_plain_apostrophes_and_quotes_against_typographic_ones(workspace, monkeypatch):
    make_pdf(workspace / "typo.pdf", ["x"])
    page = "Selon l\u2019article 28, l\u2019assur\u00e9 doit \u00ab agir \u00bb \u2014 vite"
    monkeypatch.setattr(documents, "pdf_page_texts", lambda p: [page])
    for query in ("l'assure doit", "l\u2019assur\u00e9 doit", 'doit " agir "', 'agir " - vite'):
        assert "1 match(es)" in documents.tool_search_pdf("typo.pdf", query), query
    assert "page 1: Selon l\u2019article 28, l\u2019assur\u00e9 doit" in documents.tool_search_pdf("typo.pdf", "l'assure doit")  # the snippet keeps the original characters


def test_page_text_comes_from_pdfium_so_a_font_pypdf_mis_decodes_cannot_garble_it(workspace, monkeypatch):
    make_pdf(workspace / "invoice2.pdf", ["Article 15 Aggravation du risque", "Article 16 Obligations"])
    import pypdf
    monkeypatch.setattr(pypdf.PdfReader, "__init__", lambda *a, **k: (_ for _ in ()).throw(AssertionError("pypdf must not read the text")))
    documents._PDF_TEXT_CACHE.clear()
    assert documents.pdf_page_texts(workspace / "invoice2.pdf") == ["Article 15 Aggravation du risque", "Article 16 Obligations"]


def test_visual_mode_is_refused_for_a_scan_that_ocr_can_read_and_allowed_when_it_cannot(scan, fake_ocr, monkeypatch):
    monkeypatch.setattr(documents, "uses_deepseek", lambda: False)
    with pytest.raises(ToolError, match=r"read by OCR and their text is ready: read them with mode='text'"):
        documents.tool_read_pdf("scan.pdf", pages="1-2", mode="visual")
    assert fake_ocr.pages_read == 2                                   # read once, now cached
    assert "prescription" in documents.tool_read_pdf("scan.pdf", pages="1-2", mode="text")
    assert fake_ocr.pages_read == 2                                   # the text mode did not read them again
    monkeypatch.setattr(documents.ocr, "available", lambda: False)    # no tesseract: the images are the only way
    assert documents.tool_read_pdf("scan.pdf", pages="3", mode="visual")[1]["type"] == "document"


def test_visual_mode_is_allowed_when_ocr_finds_no_text_on_the_page(scan, fake_ocr, monkeypatch):
    monkeypatch.setattr(documents, "uses_deepseek", lambda: False)
    monkeypatch.setattr(fake_ocr, "image_to_string", classmethod(lambda cls, image, lang=None: "  "))  # a drawing: nothing to read
    assert documents.tool_read_pdf("scan.pdf", pages="1", mode="visual")[1]["type"] == "document"


def test_text_quality_tells_text_from_the_ocr_of_handwriting():
    assert documents.text_quality("The total of the invoice is 642.00 EUR, payable in thirty days") >= 0.6
    assert documents.text_quality("قَالَ الشَّيْخُ رَحِمَهُ اللَّهُ تَعَالَى فِي كِتَابِهِ") >= 0.6      # vowelled Arabic
    assert documents.text_quality("oer Crotill ee * re tain ys 7 — eee ae ; 7 ich oe ee ’ a 2 4) » wi") < 0.5
    assert documents.text_quality("Invoice INV-31 Sensors 12 x 45.50") >= 0.6 and documents.text_quality("TXA025 2-200 lux 0-45 °C 12%") >= 0.6  # numbers, codes
    assert documents.text_quality("Section sur les heures ........................ 131") >= 0.6      # a table of contents
    assert not documents.has_usable_text("") and not documents.has_usable_text("a 2 4) » wi , ; ~")


def test_a_page_with_a_garbage_text_layer_can_be_read_visually_and_text_mode_warns(workspace, monkeypatch):
    monkeypatch.setattr(documents, "uses_deepseek", lambda: False)
    make_pdf(workspace / "journal.pdf", ["& 7 vA - 3 4 es ee a 2 eee 4 > wi iret a ae ee ee ee ee AT a Be * a se > - a are ee", "Total 642.00 EUR on this page"])
    text = documents.tool_read_pdf("journal.pdf", pages="1", mode="text")
    assert "looks unreliable" in text and "mode visual" in text
    assert "unreliable" not in documents.tool_read_pdf("journal.pdf", pages="2", mode="text")
    assert documents.tool_read_pdf("journal.pdf", pages="1", mode="visual")[1]["type"] == "document"    # not refused
    with pytest.raises(ToolError, match="mode='text'"):                                                  # a real page still is
        documents.tool_read_pdf("journal.pdf", pages="2", mode="visual")


def test_ocr_noise_from_handwriting_does_not_block_the_images(scan, fake_ocr, monkeypatch):
    monkeypatch.setattr(documents, "uses_deepseek", lambda: False)
    monkeypatch.setattr(fake_ocr, "image_to_string", classmethod(lambda cls, image, lang=None: "& 7 vA - 3 4 es ee a 2 eee 4 > wi iret a ae ee ee ee ee AT a Be * a se > - a are ee"))
    assert documents.tool_read_pdf("scan.pdf", pages="1", mode="visual")[1]["type"] == "document"
    assert "OCR found only noise" in documents.tool_read_pdf("scan.pdf", pages="1", mode="text")
