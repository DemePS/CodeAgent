"""OCR of scanned PDF pages: pypdfium2 draws the page, pytesseract (the tesseract program) reads it.

The text is kept on disk (config.OCR_HOME), one file per PDF version, so a page is read once: later searches, other sessions
and other processes reuse it. Pages are numbered from 1, like everywhere in the PDF tools.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .common import ToolError
from .config import OCR_DPI, OCR_HOME, OCR_LANGS


def _store(p: Path) -> Path:
    stat = p.stat()
    key = hashlib.sha1(f"{p.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()[:20]  # a changed file is a new entry
    return OCR_HOME / f"{p.stem[:40]}-{key}.json"


def cached(p: Path) -> dict[int, str]:
    """The pages of this PDF already read: {page: text}."""
    try:
        return {int(n): text for n, text in json.loads(_store(p).read_text(encoding="utf-8")).items()}
    except (OSError, ValueError):
        return {}


def _save(p: Path, pages: dict[int, str]) -> None:
    path = _store(p)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")  # written whole, then moved: another process never reads half a file
    tmp.write_text(json.dumps({str(n): t for n, t in sorted(pages.items())}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def available() -> bool:
    """Is the tesseract program installed?"""
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def missing_message() -> str:
    return ("OCR needs the tesseract program and its language packs on this machine "
            f"(Debian/Ubuntu: apt install tesseract-ocr tesseract-ocr-fra tesseract-ocr-ara tesseract-ocr-eng; languages used: {OCR_LANGS}).")


def read_pages(p: Path, pages: list[int], progress=None) -> dict[int, str]:
    """The text of these pages, read from the cache or by OCR (new pages are saved at once). progress(done, total) is called after each page."""
    import pypdfium2 as pdfium
    import pytesseract

    have = cached(p)
    todo = [n for n in pages if n not in have]
    if todo:
        try:
            pytesseract.get_tesseract_version()
        except Exception:
            raise ToolError(missing_message())
        document = pdfium.PdfDocument(str(p))
        for i, n in enumerate(todo, 1):
            try:
                image = document.get_page(n - 1).render(scale=OCR_DPI / 72).to_pil()
                have[n] = " ".join(pytesseract.image_to_string(image, lang=OCR_LANGS).split())
            except pytesseract.TesseractError as e:
                raise ToolError(f"tesseract failed on page {n} of {p.name} (languages {OCR_LANGS}): {str(e)[:200]}")
            if i % 5 == 0 or i == len(todo):
                _save(p, have)  # a long run keeps what it has read if it is stopped
            if progress:
                progress(i, len(todo))
    return {n: have[n] for n in pages}
