"""read_word and read_powerpoint: text and tables in order, slides in numeric order with notes, long documents by parts, refusals."""

import zipfile

import pytest

from seedagent.common import ToolError
from seedagent.config import MAX_TOOL_OUTPUT_CHARS
from seedagent.tools import TOOL_HANDLERS, office

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


# --- the library index and the redirecting refusal ------------------------------------------------------------------------
@pytest.fixture
def index_home(tmp_path, monkeypatch):
    """The index goes to a temporary folder, never to ~/.coding-agent/index."""
    from seedagent import index
    monkeypatch.setattr(index, "INDEX_HOME", tmp_path / "index")


def test_word_and_powerpoint_files_are_in_the_search_index(workspace, index_home):
    from seedagent import index
    docx(workspace / "contrat.docx", ["Article 1 objet du contrat", "Article 2 delai de prescription quinze ans"], table=[["Clause", "Duree"], ["Garantie", "dix ans"]])
    pptx(workspace / "deck.pptx", [["Budget 2027"], ["Charges", "Loyer annuel 38 400"]], notes={2: ["verifier le bail commercial"]})
    errors: dict = {}
    index.refresh(workspace, errors=errors)
    assert not errors
    word = index.search([workspace], "prescription quinze")["hits"]
    assert word and word[0][0].endswith("contrat.docx")
    slide = index.search([workspace], "bail commercial")["hits"]                      # a note counts, and the slide number is the page
    assert slide and slide[0][0].endswith("deck.pptx") and slide[0][1] == 2


def test_a_word_hit_gives_the_character_to_reopen_it_with(workspace, index_home):
    from seedagent import index
    lines = [f"paragraphe {i:04d} " + "mot " * 40 for i in range(200)] + ["PASSAGE UNIQUE zebulon quartz"]
    docx(workspace / "long.docx", lines)
    index.refresh(workspace)
    hit = index.search([workspace], "zebulon quartz")["hits"][0]
    assert "zebulon" in office.tool_read_word("long.docx", start=hit[1])


def test_one_slide_can_be_read_alone(workspace):
    pptx(workspace / "d.pptx", [["One"], ["Two"], ["Three"]])
    assert office.tool_read_powerpoint("d.pptx", slide=2) == "--- slide 2 ---\nTwo"
    with pytest.raises(ToolError, match="no slide 9"):
        office.tool_read_powerpoint("d.pptx", slide=9)


def test_read_file_names_the_tool_for_a_file_that_is_not_text(workspace):
    from seedagent.tools.files import tool_read_file
    docx(workspace / "a.docx", ["x"])
    pptx(workspace / "a.pptx", [["x"]])
    (workspace / "a.pdf").write_bytes(b"%PDF-1.4\x00\xff\x81\x8d")
    (workspace / "old.doc").write_bytes(b"\x00\xd0\xcf\x11\x81\x8d")
    for name, tool in (("a.docx", "read_word"), ("a.pptx", "read_powerpoint"), ("a.pdf", "read_pdf")):
        with pytest.raises(ToolError, match=tool):
            tool_read_file(name)
    with pytest.raises(ToolError, match="save it as .docx"):
        tool_read_file("old.doc")
