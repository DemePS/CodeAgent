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
import re
import sqlite3
import unicodedata
from pathlib import Path

from .config import INDEX_HOME

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
    db.execute("CREATE TABLE IF NOT EXISTS docs (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, pages INTEGER, unread INTEGER)")
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(text, path UNINDEXED, page UNINDEXED, "
               "tokenize='unicode61 remove_diacritics 2')")
    return db


def pdfs_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.pdf") if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts))


def _stale(db: sqlite3.Connection, p: Path) -> bool:
    stat = p.stat()
    row = db.execute("SELECT size, mtime_ns FROM docs WHERE path = ?", (str(p),)).fetchone()
    return row != (stat.st_size, stat.st_mtime_ns)


def index_document(db: sqlite3.Connection, p: Path, ocr_scans: bool = False, progress=None) -> tuple[int, int]:
    """(pages, scanned pages left without text): the text layer, plus the OCR text of the scans when it exists or ocr_scans is set."""
    from . import ocr
    from .tools.documents import pdf_page_texts

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


def refresh(root: Path, ocr_scans: bool = False, progress=None, log=None) -> list[Path]:
    """Index the PDFs of a folder that are new or changed (and forget the ones that are gone). Returns the files read."""
    db = connect(root)
    try:
        files = pdfs_under(root)
        known = {row[0] for row in db.execute("SELECT path FROM docs")}
        with db:
            for gone in known - {str(p) for p in files}:
                db.execute("DELETE FROM pages WHERE path = ?", (gone,))
                db.execute("DELETE FROM docs WHERE path = ?", (gone,))
        read = []
        for p in files:
            row = db.execute("SELECT unread FROM docs WHERE path = ?", (str(p),)).fetchone()
            if _stale(db, p) or (ocr_scans and row and row[0]):
                if log:
                    log(f"indexing {p.name}")
                index_document(db, p, ocr_scans=ocr_scans, progress=progress)
                read.append(p)
        return read
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


def _coverage(text: str, weights: dict[str, float]) -> float:
    """The summed rarity of the query's words that the page has (prefix match, like the index)."""
    folded = _COMBINING.sub("", unicodedata.normalize("NFKD", text.translate(_TYPOGRAPHIC))).lower()  # whole text at once: fast enough for ~100 pages
    tokens = set(re.findall(r"\w+", folded))
    return sum(w for t, w in weights.items() if any(tok.startswith(t) for tok in tokens))


def search(roots: list[Path], query: str, document: str | None = None, limit: int = 10) -> dict:
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
            weights = None  # the rarity of the words is only needed to order the pages that have some of them
            for mode, match in (("all words", match_and), ("any word", match_or)):
                if mode == "any word" and (len(terms) == 1 or len(out["hits"]) >= limit):
                    break
                sql = ("SELECT path, page, text, snippet(pages, 0, '[', ']', ' ... ', 40), bm25(pages) FROM pages WHERE pages MATCH ?"
                       + (" AND path LIKE ?" if document else "") + " ORDER BY bm25(pages) LIMIT ?")
                args = [match] + ([f"%{document}%"] if document else []) + [ANY_WORD_POOL if mode == "any word" else limit * 3]
                rows = db.execute(sql, args).fetchall()  # (path, page, text, snippet, bm25)
                if mode == "any word":
                    weights = weights or _idf(db, terms)
                    rows.sort(key=lambda r: (-_coverage(r[2], weights), r[4]))
                for path, page, _, snippet, _ in rows:
                    if (path, page) not in seen and len(out["hits"]) < limit:
                        seen.add((path, page))
                        out["hits"].append((path, page, snippet, mode))
        finally:
            db.close()
    return out
