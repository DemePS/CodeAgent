"""The full-text index of the library: search_library ranks pages by the words of the query."""

import pytest

from coding_agent import index, ocr
from coding_agent.tools import documents

from test_documents import FakeTesseract, make_pdf  # noqa: F401 -- the stand-in for tesseract and the minimal PDF writer


@pytest.fixture(autouse=True)
def homes(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "INDEX_HOME", tmp_path / "index")
    monkeypatch.setattr(ocr, "OCR_HOME", tmp_path / "ocr")
    documents._PDF_TEXT_CACHE.clear()


def test_words_keep_the_distinctive_ones_folded_and_without_plural_endings():
    assert index.words("L'assuré est tenu de donner avis à l'entreprise d'assurances") == ["assure", "tenu", "donner", "avis", "entreprise", "assurance"]
    assert index.words("the of and")  # only common words: still a query, not an empty one


def test_a_sentence_finds_the_page_that_has_most_of_its_words(workspace):
    make_pdf(workspace / "code.pdf", ["Les actions derivant du contrat sont prescrites par deux ans", "Le sinistre est declare", "Autre chose"])
    out = documents.tool_search_library("quel est le delai de prescription des actions du contrat ?")
    assert "code.pdf page 1" in out and "page 2" not in out.split("page 1")[0]
    assert "words used:" in out and "prescription" in out


def test_all_words_come_before_any_word_and_the_document_filter_narrows(workspace):
    make_pdf(workspace / "a.pdf", ["sinistre declaration delai", "sinistre seulement"])
    make_pdf(workspace / "b.pdf", ["sinistre declaration delai"])
    out = documents.tool_search_library("sinistre declaration delai")
    assert out.index("a.pdf page 1 (all words)") < out.index("a.pdf page 2 (any word)")
    only_b = documents.tool_search_library("sinistre declaration delai", document="b.pdf")
    assert "b.pdf page 1" in only_b and "a.pdf" not in only_b


def test_apostrophes_accents_and_plurals_are_ignored(workspace, monkeypatch):
    make_pdf(workspace / "typo.pdf", ["x"])
    monkeypatch.setattr(documents, "pdf_page_texts", lambda p: ["Les actions de l’assuré se prescrivent"])
    assert "typo.pdf page 1" in documents.tool_search_library("l'assure prescrivent action")


def test_an_unchanged_file_is_not_read_again_and_a_changed_one_is(workspace, monkeypatch):
    make_pdf(workspace / "a.pdf", ["alpha beta"])
    assert index.refresh(workspace) == [workspace / "a.pdf"]
    assert index.refresh(workspace) == []
    make_pdf(workspace / "a.pdf", ["alpha beta gamma delta"])
    assert index.refresh(workspace) == [workspace / "a.pdf"]
    (workspace / "a.pdf").unlink()
    index.refresh(workspace)
    assert "No PDF is indexed" in documents.tool_search_library("alpha")


def test_scans_are_reported_until_they_are_indexed_with_ocr(workspace, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "pytesseract", FakeTesseract)
    make_pdf(workspace / "mixed.pdf", ["Some words", "", ""])
    out = documents.tool_search_library("prescription")
    assert "2 scanned page(s)" in out and "coding-agent --index" in out
    assert index.refresh(workspace, ocr_scans=True) == [workspace / "mixed.pdf"]
    out = documents.tool_search_library("prescription deux ans")
    assert "mixed.pdf page 2" in out and "mixed.pdf page 3" in out and "scanned page(s)" not in out
