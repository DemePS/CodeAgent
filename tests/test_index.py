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


def test_a_text_file_is_searched_and_the_result_gives_the_line_to_read(workspace):
    lines = ["# Quran -- header comment", "1|1|In the name of Allah, the Entirely Merciful.", "66|6|Fire whose fuel is people and stones, over which are [appointed] angels, harsh and severe",
             "67|1|Blessed is He in whose hand is dominion."]
    (workspace / "quran.txt").write_text("\n".join(lines), encoding="utf-8")
    out = documents.tool_search_library("angels severe")
    assert "quran.txt line 3" in out and "[angels]" in out and "read_file" in out and "start_line=N" in out
    assert "header comment" not in out  # comment lines of a data file are not indexed


def test_prose_is_cut_into_paragraph_records_and_numbered_data_gets_one_record_per_line(workspace, monkeypatch):
    monkeypatch.setattr(index, "TEXT_CHUNK_CHARS", 60)
    (workspace / "notes.txt").write_text("alpha one\nbeta two\n\n" + "gamma " * 30 + "\n\ndelta four", encoding="utf-8")
    records = index._text_records(workspace / "notes.txt")
    assert [line for _, line in records] == [1, 4, 6] and records[0][0] == "alpha one beta two" and records[2][0] == "delta four"
    (workspace / "data.txt").write_text("# comment\n1|1|first verse\n1|2|second verse\n1|3|third verse", encoding="utf-8")
    assert index._text_records(workspace / "data.txt") == [("1|1|first verse", 2), ("1|2|second verse", 3), ("1|3|third verse", 4)]


def test_a_file_too_big_for_a_search_waits_for_the_indexing_command(workspace, monkeypatch):
    (workspace / "big.txt").write_text("1\\tSahih\\tRevelation\\tNarrated 'Umar: actions are judged by intentions\\n" * 5, encoding="utf-8")
    monkeypatch.setattr(index, "IN_TOOL_MAX_BYTES", 50)
    out = documents.tool_search_library("intentions")
    assert "No PDF is indexed" in out or "Not searched yet" in out
    assert [p.name for p in index.pending(workspace)] == ["big.txt"]
    index.refresh(workspace)  # `coding-agent --index`: no size limit
    assert "big.txt line 1" in documents.tool_search_library("intentions") and index.pending(workspace) == []


def test_pdfs_and_text_files_are_searched_together(workspace):
    make_pdf(workspace / "code.pdf", ["Les anges sont des creatures"])
    (workspace / "quran.txt").write_text("66|6|over which are angels harsh and severe", encoding="utf-8")
    out = documents.tool_search_library("angels anges")
    assert "code.pdf page 1" in out and "quran.txt line 1" in out


def test_one_unreadable_file_does_not_stop_the_search_of_the_others(workspace):
    import os
    (workspace / "a_locked.txt").write_text("1|1|secretword", encoding="utf-8")
    (workspace / "a_locked.txt").chmod(0)
    (workspace / "z_good.txt").write_text("1|1|zebra stripes", encoding="utf-8")
    try:
        if os.access(workspace / "a_locked.txt", os.R_OK):  # running as root: the permission cannot be refused
            pytest.skip("file permissions are not enforced for this user")
        out = documents.tool_search_library("zebra")
        assert "z_good.txt line 1" in out and "a_locked.txt" in out and "Could not be read" in out
    finally:
        (workspace / "a_locked.txt").chmod(0o644)


def test_a_binary_file_with_a_text_name_is_not_indexed(workspace):
    (workspace / "blob.txt").write_bytes(b"abc\x00def" + bytes(range(256)) * 20)
    assert index._text_records(workspace / "blob.txt") == []


def test_alternatives_separated_by_a_bar_are_searched_together_and_the_page_found_by_several_comes_first(workspace):
    (workspace / "quran.txt").write_text(
        "2|1|the angels bow down\n2|2|a verse about the malaika and the angels together\n2|3|a verse about gardens and rivers\n2|4|the malaika alone", encoding="utf-8")
    out = documents.tool_search_library("angels | malaika")
    assert "alternatives: angels | malaika" in out
    first = [line for line in out.splitlines() if "quran.txt line" in line]
    assert "line 2 " in first[0] and "2 of 2 alternatives" in first[0]  # found by both: first
    assert any("line 1 " in line for line in first) and any("line 4 " in line for line in first) and not any("line 3 " in line for line in first)


def test_a_query_without_a_bar_is_searched_as_before_and_empty_alternatives_are_ignored(workspace):
    (workspace / "a.txt").write_text("1|1|alpha beta\n1|2|gamma", encoding="utf-8")
    single = index.search([workspace], "alpha")
    assert index.alternatives("alpha") == ["alpha"] and index.alternatives("alpha | | ") == ["alpha"]
    assert index.search([workspace], "alpha | | ")["hits"] == single["hits"]
    assert "alternatives:" not in documents.tool_search_library("alpha")
