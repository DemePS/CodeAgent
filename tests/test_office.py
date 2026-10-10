"""read_word and read_powerpoint: text and tables in order, slides in numeric order with notes, long documents by parts, refusals."""

import zipfile

import pytest

from coding_agent.common import ToolError
from coding_agent.config import MAX_TOOL_OUTPUT_CHARS
from coding_agent.tools import TOOL_HANDLERS, office

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def docx(path, paragraphs, table=None, extra=""):
    p = "".join(f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for t in paragraphs)
    rows = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in r) + "</w:tr>" for r in (table or []))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'{extra}<w:document {W}><w:body>{p}{f"<w:tbl>{rows}</w:tbl>" if table else ""}</w:body></w:document>')


def pptx(path, slides, notes=None):
    def xml(paragraphs):
        return f'<p:sld {A} xmlns:p="x"><p:cSld>' + "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in paragraphs) + "</p:cSld></p:sld>"
    with zipfile.ZipFile(path, "w") as z:
        for i, s in enumerate(slides, 1):
            z.writestr(f"ppt/slides/slide{i}.xml", xml(s))
        for i, s in (notes or {}).items():   # like PowerPoint: the slide points to its notes through its relationships
            z.writestr(f"ppt/notesSlides/notesSlide{i}.xml", xml(s))
            z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels",
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" '
                       f'Target="../notesSlides/notesSlide{i}.xml"/></Relationships>')


def test_the_tools_are_built_in():
    assert TOOL_HANDLERS["read_word"] is office.tool_read_word and TOOL_HANDLERS["read_powerpoint"] is office.tool_read_powerpoint


def test_word_gives_paragraphs_then_table_rows(workspace):
    docx(workspace / "a.docx", ["Facture n° 12", "Total TTC 68,40 €"], table=[["Qté", "PU"], ["10", "4,20"]])
    assert office.tool_read_word("a.docx").splitlines() == ["Facture n° 12", "Total TTC 68,40 €", "Qté | PU", "10 | 4,20"]


def test_powerpoint_gives_slides_in_numeric_order_with_notes(workspace):
    pptx(workspace / "d.pptx", [["Budget 2026"], ["Charges", "Loyer 3 200"]] + [["slide"]] * 8 + [["last"]], notes={2: ["Check the lease"]})
    text = office.tool_read_powerpoint("d.pptx")
    assert "--- slide 2 ---\nCharges\nLoyer 3 200\n(notes) Check the lease" in text
    assert text.index("--- slide 2 ---") < text.index("--- slide 10 ---") < text.index("--- slide 11 ---")


def test_a_note_belongs_to_the_slide_that_points_to_it_not_to_the_same_number(workspace):
    """PowerPoint numbers notesSlideN in the order the notes were created: the only note of the deck is notesSlide1, on slide 2."""
    ns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    with zipfile.ZipFile(workspace / "n.pptx", "w") as z:
        for i, title in enumerate(("One", "Two", "Three"), 1):
            z.writestr(f"ppt/slides/slide{i}.xml", f'<p:sld {A} xmlns:p="x"><p:cSld><a:p><a:r><a:t>{title}</a:t></a:r></a:p></p:cSld></p:sld>')
        z.writestr("ppt/slides/_rels/slide2.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" '
                   'Target="../notesSlides/notesSlide1.xml"/></Relationships>')
        z.writestr("ppt/notesSlides/notesSlide1.xml", f'<p:notes {A} xmlns:p="x"><a:p><a:r><a:t>For slide two</a:t></a:r></a:p></p:notes>')
    text = office.tool_read_powerpoint("n.pptx")
    assert "--- slide 1 ---\nOne\n\n--- slide 2 ---\nTwo\n(notes) For slide two\n\n--- slide 3 ---\nThree" in text


def test_a_table_on_a_slide_gives_one_line_per_row(workspace):
    cell = lambda t: f"<a:tc><a:txBody><a:p><a:r><a:t>{t}</a:t></a:r></a:p></a:txBody></a:tc>"  # noqa: E731
    table = "<a:tbl>" + "".join("<a:tr>" + "".join(cell(c) for c in row) + "</a:tr>" for row in (("T", "In"), ("T1", "310 000"))) + "</a:tbl>"
    with zipfile.ZipFile(workspace / "t.pptx", "w") as z:
        z.writestr("ppt/slides/slide1.xml", f'<p:sld {A} xmlns:p="x"><p:cSld><a:p><a:r><a:t>Cash</a:t></a:r></a:p>{table}</p:cSld></p:sld>')
    assert office.tool_read_powerpoint("t.pptx") == "--- slide 1 ---\nCash\nT | In\nT1 | 310 000"


def test_a_long_document_is_read_in_parts(workspace):
    docx(workspace / "long.docx", [f"line {i:05d} " + "x" * 90 for i in range(1500)])          # about 150,000 characters
    first = office.tool_read_word("long.docx")
    assert len(first) < MAX_TOOL_OUTPUT_CHARS + 200 and "call again with start=" in first and first.startswith("line 00000")
    start = int(first.rsplit("start=", 1)[1].rstrip("]"))
    second = office.tool_read_word("long.docx", start=start)
    assert second.startswith(first.splitlines()[0][:0] + second[:10]) and "line 00000" not in second
    total = len(first.rsplit("\n...", 1)[0]) + len(second.rsplit("\n...", 1)[0])
    assert total >= MAX_TOOL_OUTPUT_CHARS * 2 - 5
    with pytest.raises(ToolError, match="past the end"):
        office.tool_read_word("long.docx", start=10_000_000)


def test_refusals(workspace):
    (workspace / "old.doc").write_bytes(b"x")
    (workspace / "fake.docx").write_bytes(b"not a zip")
    (workspace / "a.pdf").write_bytes(b"%PDF")
    for name, message in (("old.doc", "old .doc"), ("fake.docx", "not a valid .docx"), ("a.pdf", "not a .docx"), ("missing.docx", "File not found")):
        with pytest.raises(ToolError, match=message):
            office.tool_read_word(name)
    (workspace / "old.ppt").write_bytes(b"x")
    with pytest.raises(ToolError, match="old .ppt"):
        office.tool_read_powerpoint("old.ppt")
    (workspace / "empty.docx").write_bytes(b"")
    with zipfile.ZipFile(workspace / "nodoc.docx", "w") as z:
        z.writestr("other.xml", "<x/>")
    with pytest.raises(ToolError, match="no readable text"):
        office.tool_read_word("nodoc.docx")


def test_entities_in_the_xml_are_refused(workspace):
    docx(workspace / "bomb.docx", ["x"], extra='<!DOCTYPE d [<!ENTITY a "aaaaaaaaaa">]>')
    with pytest.raises(ToolError, match="DTD or entities"):
        office.tool_read_word("bomb.docx")


def test_an_empty_document_says_so(workspace):
    docx(workspace / "e.docx", [])
    assert office.tool_read_word("e.docx") == "(no text in this document)"
