"""A full-text index of the PDFs in a folder (the library), one row per page, searched by word with a ranking.

Why: asking the model to guess the exact words of a passage fails (a phrase rarely matches word for word). Here the pages are
ranked by how many of the query's distinctive words they contain (BM25), accents, case and typographic apostrophes are ignored,
and a word also matches its longer forms (prescription / prescriptions). The text of a page is its text layer, or the OCR text
for a scan (config.OCR_HOME). The index is one SQLite file per folder (config.INDEX_HOME); a file is read again only when its
size or date changes.
"""

from __future__ import annotations

import hashlib
import math
from concurrent.futures import ThreadPoolExecutor
import re
import sqlite3
import unicodedata
from pathlib import Path

from .config import INDEX_HOME

MAX_PARALLEL = 4  # alternatives searched at the same time
RRF_K = 60  # reciprocal rank fusion: how fast the weight of a rank falls
INDEX_VERSION = 3  # 2: page text read by pdfium; 3: plain text files (.txt, .md) are indexed too
TEXT_SUFFIXES = (".txt", ".md")
TEXT_CHUNK_CHARS = 1500       # prose is cut into records of whole paragraphs, about this long
IN_TOOL_MAX_BYTES = 25_000_000  # a search call indexes new files up to this size itself (about 13 MB/s); bigger ones wait for `coding-agent --index`
MAX_RESULTS = 30
ANY_WORD_POOL = 1500  # pages with some of the words that are re-ranked by rarity (a long library is a few thousand pages)
STOPWORDS = frozenset("""
le la les un une des de du d l et ou en au aux a à est sont se sa son ses ce cet cette ces qui que quoi dont où ne pas plus par pour sur
sous dans avec sans il elle ils elles on nous vous je tu me te lui leur leurs y fait faut doit peut etre avoir ainsi comme mais si
the a an of to and or in on at is are was were be by for with from that this these those it its as not no what which who when how
""".split())


def _path(root: Path) -> Path:
    return INDEX_HOME / f"{root.name[:30] or 'root'}-{hashlib.sha1(str(root).encode()).hexdigest()[:12]}.sqlite"


def connect(root: Path) -> sqlite3.Connection:
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    if db.execute("PRAGMA user_version").fetchone()[0] != INDEX_VERSION:  # built by an older extractor (pypdf garbled some PDFs): start again
        db.execute("DROP TABLE IF EXISTS docs")
        db.execute("DROP TABLE IF EXISTS pages")
        db.execute(f"PRAGMA user_version = {INDEX_VERSION}")
    db.execute("CREATE TABLE IF NOT EXISTS docs (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, pages INTEGER, unread INTEGER)")
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(text, path UNINDEXED, page UNINDEXED, "
               "tokenize='unicode61 remove_diacritics 2')")
    return db


def documents_under(root: Path) -> list[Path]:
    """The PDFs and the plain text files (.txt, .md) of a folder, hidden folders left out."""
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (".pdf", *TEXT_SUFFIXES)
                  and not any(part.startswith(".") for part in p.relative_to(root).parts))


def pdfs_under(root: Path) -> list[Path]:
    return [p for p in documents_under(root) if p.suffix.lower() == ".pdf"]


def is_text_file(path) -> bool:
    return Path(path).suffix.lower() in TEXT_SUFFIXES


def _stale(db: sqlite3.Connection, p: Path) -> bool:
    stat = p.stat()
    row = db.execute("SELECT size, mtime_ns FROM docs WHERE path = ?", (str(p),)).fetchone()
    return row != (stat.st_size, stat.st_mtime_ns)


_DATA_LINE = re.compile(r"^\d+\s*[|\t]")


def _text_records(p: Path) -> list[tuple[str, int]]:
    """A text file as (text, line where the record starts).
    A file of numbered records (`66|6|text`, `1<TAB>Sahih<TAB>...`: a verse or a hadith per line) gives one record per line, so the line number
    points at the passage. Prose gives its paragraphs (separated by blank lines), grouped up to TEXT_CHUNK_CHARS; a longer paragraph stands alone.
    Comment lines (# ...) of a data file are left out; in a .md file they are headings and stay."""
    with open(p, "rb") as handle:
        if b"\0" in handle.read(4096):  # a binary file with a text name: nothing to search
            return []
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    keep_hash = p.suffix.lower() == ".md"
    content = [(n, line.strip()) for n, line in enumerate(lines, 1) if line.strip() and (keep_hash or not line.lstrip().startswith("#"))]
    if not content:
        return []
    if sum(1 for _, line in content if _DATA_LINE.match(line)) >= 0.8 * len(content):
        return [(line, n) for n, line in content]
    paragraphs, current, start = [], [], 0
    for n, raw in enumerate(lines, 1):
        text = raw.strip()
        if not text or (not keep_hash and text.startswith("#")):
            if current and not text:
                paragraphs.append((" ".join(current), start))
                current = []
            continue
        if not current:
            start = n
        current.append(text)
    if current:
        paragraphs.append((" ".join(current), start))
    records, buffer, first, size = [], [], 0, 0
    for text, line in paragraphs:
        if buffer and size + len(text) > TEXT_CHUNK_CHARS:
            records.append((" ".join(buffer), first))
            buffer, size = [], 0
        if not buffer:
            first = line
        buffer.append(text)
        size += len(text) + 1
    if buffer:
        records.append((" ".join(buffer), first))
    return records


def _index_text_file(db: sqlite3.Connection, p: Path) -> tuple[int, int]:
    records = _text_records(p)
    stat = p.stat()
    with db:
        db.execute("DELETE FROM pages WHERE path = ?", (str(p),))
        db.executemany("INSERT INTO pages (text, path, page) VALUES (?, ?, ?)", [(text, str(p), line) for text, line in records])
        db.execute("INSERT OR REPLACE INTO docs VALUES (?, ?, ?, ?, ?)", (str(p), stat.st_size, stat.st_mtime_ns, len(records), 0))
    return len(records), 0


def index_document(db: sqlite3.Connection, p: Path, ocr_scans: bool = False, progress=None) -> tuple[int, int]:
    """(pages, scanned pages left without text): the text layer, plus the OCR text of the scans when it exists or ocr_scans is set.
    A plain text file gives records instead of pages (the number kept is the line where the record starts)."""
    from . import ocr
    from .tools.documents import pdf_page_texts

    if is_text_file(p):
        return _index_text_file(db, p)
    texts = pdf_page_texts(p)
    blank = [n for n, t in enumerate(texts, 1) if not t]
    done = ocr.cached(p)
    todo = [n for n in blank if n not in done]
    if todo and ocr_scans:
        ocr.read_pages(p, todo, progress=progress)
        done = ocr.cached(p)
    rows, unread = [], 0
    for n, text in enumerate(texts, 1):
        text = text or done.get(n, "")
        if not text and n in blank and n not in done:
            unread += 1
        if text:
            rows.append((text, str(p), n))
    stat = p.stat()
    with db:
        db.execute("DELETE FROM pages WHERE path = ?", (str(p),))
        db.executemany("INSERT INTO pages (text, path, page) VALUES (?, ?, ?)", rows)
        db.execute("INSERT OR REPLACE INTO docs VALUES (?, ?, ?, ?, ?)", (str(p), stat.st_size, stat.st_mtime_ns, len(texts), unread))
    return len(texts), unread


def refresh(root: Path, ocr_scans: bool = False, progress=None, log=None, max_bytes: int | None = None,
            errors: dict | None = None) -> list[Path]:
    """Index the PDFs and text files of a folder that are new or changed (and forget the ones that are gone). Returns the files read.
    max_bytes: leave bigger files for later (a search call must not spend a minute indexing a 12 MB file): see pending().
    errors: filled with {path: why} for the files that could not be read (no permission, password-protected, damaged): one such file
    must not stop the search of all the others."""
    db = connect(root)
    try:
        files = documents_under(root)
        known = {row[0] for row in db.execute("SELECT path FROM docs")}
        with db:
            for gone in known - {str(p) for p in files}:
                db.execute("DELETE FROM pages WHERE path = ?", (gone,))
                db.execute("DELETE FROM docs WHERE path = ?", (gone,))
        read = []
        for p in files:
            row = db.execute("SELECT unread FROM docs WHERE path = ?", (str(p),)).fetchone()
            if _stale(db, p) or (ocr_scans and row and row[0]):
                if max_bytes is not None and p.stat().st_size > max_bytes and _stale(db, p):
                    continue
                if log:
                    log(f"indexing {p.name}")
                try:
                    index_document(db, p, ocr_scans=ocr_scans, progress=progress)
                except Exception as e:  # noqa: BLE001 -- any failure of one file, whatever its kind
                    if errors is not None:
                        errors[p] = f"{type(e).__name__}: {e}"[:160]
                    continue
                read.append(p)
        return read
    finally:
        db.close()


def pending(root: Path) -> list[Path]:
    """The documents of a folder that are not in the index (yet) or changed since: they are not searched."""
    db = connect(root)
    try:
        return [p for p in documents_under(root) if _stale(db, p)]
    finally:
        db.close()


def words(query: str) -> list[str]:
    """The distinctive words of a query: folded, no short or common words, plural endings cut, each once."""
    from .tools.documents import _fold
    found = []
    for w in re.findall(r"\w+", _fold(query)):
        if len(w) < 3 or w in STOPWORDS:
            continue
        if len(w) >= 5 and w[-1] in "sx":
            w = w[:-1]
        if w not in found:
            found.append(w)
    if not found:  # a query made only of common words: use them all rather than nothing
        found = [w for w in re.findall(r"\w+", _fold(query)) if w not in found]
    return found


def _idf(db: sqlite3.Connection, terms: list[str]) -> dict[str, float]:
    """How rare each word is in this folder: a word on every page (CIMA in the CIMA code) says little, a rare one says a lot."""
    total = max(1, db.execute("SELECT COUNT(*) FROM pages").fetchone()[0])
    weights = {}
    for t in terms:
        df = db.execute("SELECT COUNT(*) FROM pages WHERE pages MATCH ?", ('"' + t.replace('"', "") + '"*',)).fetchone()[0]
        weights[t] = math.log(1 + total / (1 + df))
    return weights


_COMBINING = re.compile("[\u0300-\u036f]")
_TYPOGRAPHIC = str.maketrans({"\u2019": "'", "\u2018": "'", "\u00a0": " "})


def _postings(db: sqlite3.Connection, terms: list[str], document: str | None = None) -> tuple[dict[str, float], dict[int, float]]:
    """(the rarity of each word, the summed rarity of the words each page has), read from the inverted index: no page text is fetched.
    A word's rarity counts every page of the folder (a filter on the document does not change how rare a word is)."""
    total = max(1, db.execute("SELECT COUNT(*) FROM pages").fetchone()[0])
    weights: dict[str, float] = {}
    postings: dict[str, list[int]] = {}
    for t in terms:
        match = '"' + t.replace('"', "") + '"*'
        if document:  # the path is fetched only to filter on it
            rows = db.execute("SELECT rowid, path FROM pages WHERE pages MATCH ?", (match,)).fetchall()
            weights[t] = math.log(1 + total / (1 + len(rows)))
            postings[t] = [rid for rid, path in rows if document in path]
        else:
            rows = [r[0] for r in db.execute("SELECT rowid FROM pages WHERE pages MATCH ?", (match,))]
            weights[t] = math.log(1 + total / (1 + len(rows)))
            postings[t] = rows
    coverage: dict[int, float] = {}
    for t, rids in postings.items():
        for rid in rids:
            coverage[rid] = coverage.get(rid, 0.0) + weights[t]
    return weights, coverage


def _coverage(text: str, weights: dict[str, float]) -> float:
    """The summed rarity of the query's words that the page has (prefix match, like the index)."""
    folded = _COMBINING.sub("", unicodedata.normalize("NFKD", text.translate(_TYPOGRAPHIC))).lower()  # whole text at once: fast enough for ~100 pages
    tokens = set(re.findall(r"\w+", folded))
    return sum(w for t, w in weights.items() if any(tok.startswith(t) for tok in tokens))


def alternatives(query: str) -> list[str]:
    """The phrasings of a query: alternatives separated by | (like grep -e a -e b); a part with no usable word is dropped."""
    parts = [part.strip() for part in query.split("|")]
    return [part for part in parts if words(part)] or [query]


def search(roots: list[Path], query: str, document: str | None = None, limit: int = 10) -> dict:
    """Like _search_one, for a query that may hold alternatives separated by | ("angel | malaika | ange"): every alternative is searched on its
    own and the rankings are merged (reciprocal rank fusion), so a page found by several alternatives comes first."""
    phrasings = alternatives(query)
    if len(phrasings) == 1:
        return _search_one(roots, phrasings[0], document, limit)
    limit = int(limit or 10)
    limit = 10 if limit < 1 else min(limit, MAX_RESULTS)
    for root in roots:
        connect(root).close()  # the schema is checked once, here, not by every thread
    # One thread per alternative (at most 4): each opens its own read-only connection, and SQLite lets go of the GIL while it runs a query.
    # (asyncio would not help: nothing here waits on the network; it is all SQLite calls and Python work.)
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(phrasings))) as pool:
        results = list(pool.map(lambda phrasing: _search_one(roots, phrasing, document, limit), phrasings))  # same order as the alternatives
    score: dict[tuple, float] = {}
    best: dict[tuple, tuple] = {}
    found_by: dict[tuple, int] = {}
    for result in results:
        for rank, (path, page, snippet, mode) in enumerate(result["hits"]):
            key = (path, page)
            score[key] = score.get(key, 0.0) + 1.0 / (RRF_K + rank)
            found_by[key] = found_by.get(key, 0) + 1
            if key not in best or (mode == "all words" and best[key][3] != "all words"):
                best[key] = (path, page, snippet, mode)
    ordered = sorted(score, key=lambda k: (-score[k], k))[:limit]
    hits = []
    for key in ordered:
        path, page, snippet, mode = best[key]
        hits.append((path, page, snippet, f"{mode}, {found_by[key]} of {len(phrasings)} alternatives"))
    words_used = []
    for result in results:
        words_used += [w for w in result["words"] if w not in words_used]
    unread: dict = {}
    for result in results:
        unread.update(result["unread"])
    return {"words": words_used, "phrasings": phrasings, "hits": hits, "unread": unread, "files": results[0]["files"]}


def _search_one(roots: list[Path], query: str, document: str | None = None, limit: int = 10) -> dict:
    """{"words": [...], "hits": [(path, page, snippet, "all words" | "any word")], "unread": {path: scanned pages not in the index}, "files": n}

    Pages with all the words come first (best BM25 first). Then the pages with some of them, ranked by the summed rarity of the
    words they have (so a page with a rare word beats a page that repeats a common one), ties by BM25."""
    terms = words(query)
    limit = int(limit or 10)
    limit = 10 if limit < 1 else min(limit, MAX_RESULTS)  # nothing or a negative number: the default
    out = {"words": terms, "hits": [], "unread": {}, "files": 0}
    if not terms:
        return out
    match_and = " AND ".join(f'"{t}"*' for t in terms)
    match_or = " OR ".join(f'"{t}"*' for t in terms)
    seen = set()
    for root in roots:
        db = connect(root)
        try:
            out["files"] += db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
            for path, n in db.execute("SELECT path, unread FROM docs WHERE unread > 0"):
                out["unread"][path] = n
            for mode, match in (("all words", match_and), ("any word", match_or)):
                if mode == "any word" and (len(terms) == 1 or len(out["hits"]) >= limit):
                    break
                if mode == "all words":
                    sql = ("SELECT path, page, snippet(pages, 0, '[', ']', ' ... ', 40), bm25(pages) FROM pages WHERE pages MATCH ?"
                           + (" AND path LIKE ?" if document else "") + " ORDER BY bm25(pages) LIMIT ?")
                    rows = db.execute(sql, [match] + ([f"%{document}%"] if document else []) + [limit * 3]).fetchall()
                else:
                    # the pages with some of the words, the richest in rare words first (read from the postings), then by BM25
                    # Ranked from the index alone: the rarity of the words each page has (the postings) and BM25 for every matching page are
                    # cheap (a few ms); snippet() re-reads the text and costs about 0.25 ms a page, so it is computed for the pages kept only.
                    _, coverage = _postings(db, terms, document)
                    bm = dict(db.execute("SELECT rowid, bm25(pages) FROM pages WHERE pages MATCH ?", (match,)))
                    ranked = sorted((rid for rid in coverage if rid in bm), key=lambda rid: (-coverage[rid], bm[rid]))[:limit * 3]
                    marks = ",".join("?" * len(ranked))
                    snippets = {rid: (path, page, snippet) for path, page, snippet, rid in db.execute(
                        "SELECT path, page, snippet(pages, 0, '[', ']', ' ... ', 40), rowid FROM pages "
                        f"WHERE pages MATCH ? AND rowid IN ({marks})", [match, *ranked])} if ranked else {}
                    rows = [(*snippets[rid], 0.0) for rid in ranked if rid in snippets]
                for path, page, snippet, *_ in rows:
                    if (path, page) not in seen and len(out["hits"]) < limit:
                        seen.add((path, page))
                        out["hits"].append((path, page, snippet, mode))
        finally:
            db.close()
    return out
