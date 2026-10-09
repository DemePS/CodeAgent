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


def test_a_page_with_a_rare_word_beats_a_page_that_repeats_a_common_one(workspace):
    pages = ["cima " * 5, "conference " + "mot " * 30, "cima autre", "cima encore"] + [f"texte banal numero {i}" for i in range(8)]  # the page with the rare word is long
    make_pdf(workspace / "code.pdf", pages)
    index.refresh(workspace)
    db = index.connect(workspace)
    plain = [row[0] for row in db.execute('SELECT page FROM pages WHERE pages MATCH \'"cima"* OR "conference"*\' ORDER BY bm25(pages)')]
    db.close()
    assert plain[0] == 1, "premise: plain BM25 puts the page that repeats the common word first"  # otherwise this test proves nothing
    out = documents.tool_search_library("cima conference")
    assert out.index("code.pdf page 2") < out.index("code.pdf page 1")  # the rarity ordering puts the page with the rare word first


def test_a_rare_word_is_found_even_when_plain_bm25_ranks_its_page_far_down(workspace):
    # 100 short pages with the common word, and one very long page with both words: BM25 puts the long page last (100th of 101)
    pages = [f"cima texte {i}" for i in range(100)] + [f"texte banal {i}" for i in range(199)] + ["cima " + "mot " * 800 + "xylophone"]
    make_pdf(workspace / "big.pdf", pages)
    out = documents.tool_search_library("cima xylophone introuvable", max_results=3)  # no page has all three words: the any-word stage decides
    assert "big.pdf page 300" in out.split("Open the pages")[0]


def test_a_negative_or_zero_number_of_results_means_the_default(workspace):
    make_pdf(workspace / "a.pdf", [f"mot numero {i}" for i in range(12)])
    index.refresh(workspace)
    for limit in (0, -3, None):
        assert len(index.search([workspace], "mot", limit=limit)["hits"]) == 10
