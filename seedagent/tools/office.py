"""read_word, read_powerpoint: the text of Word (.docx) and PowerPoint (.pptx) files.

A .docx or .pptx is a zip of XML files, so the text is read with the standard library: no extra dependency. Word gives its
paragraphs in order and the rows of its tables as "cell | cell"; PowerPoint gives its slides in order, with the speaker notes.
Only text is read: a picture inside the document is not (export the page as an image or a PDF). A long document is read in
parts with `start`, the way a long page is read with `read_pdf`'s page ranges.
"""

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile

from ..common import ToolError, resolve_readable
from ..config import MAX_TOOL_OUTPUT_CHARS

WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/main"
MAX_PART_BYTES = 100_000_000  # one XML part, uncompressed: a zip bomb is refused, not expanded


def _open(path: str, extension: str, old: str) -> tuple[str, zipfile.ZipFile]:
    p = resolve_readable(path)
    if not p.is_file():
        raise ToolError(f"File not found: {path}")
    suffix = p.suffix.lower()
    if suffix == old:
        raise ToolError(f"{path} is an old {old} file: save it as {extension} (or PDF) first.")
    if suffix != extension:
        raise ToolError(f"{path} is not a {extension} file.")
    try:
        return path, zipfile.ZipFile(p)
    except zipfile.BadZipFile:
        raise ToolError(f"{path} is not a valid {extension} file.")


def _xml(z: zipfile.ZipFile, name: str, path: str):
    try:
        info = z.getinfo(name)
    except KeyError:
        raise ToolError(f"{path} has no readable text ({name} is missing).")
    if info.file_size > MAX_PART_BYTES:
        raise ToolError(f"{path}: {name} is {info.file_size:,} bytes once unpacked; it is not read.")
    data = z.read(name)
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:  # entity tricks have no place in an Office file
        raise ToolError(f"{path}: {name} holds a DTD or entities; it is not read.")
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise ToolError(f"{path}: {name} cannot be read.")


def _window(text: str, start: int, empty: str) -> str:
    """`MAX_TOOL_OUTPUT_CHARS` characters of the text from `start`, and how to go on when there is more."""
    if not text.strip():
        return empty
    start = max(0, int(start or 0))
    if start >= len(text):
        raise ToolError(f"start={start} is past the end of the text ({len(text):,} characters).")
    part = text[start:start + MAX_TOOL_OUTPUT_CHARS]
    end = start + len(part)
    if end < len(text):
        part += f"\n... [{len(text) - end:,} more characters: call again with start={end}]"
    return part


def _words(element, namespace: str, tag: str) -> str:
    return "".join(t.text or "" for t in element.iter(f"{{{namespace}}}{tag}"))


def word_text(path: str) -> str:
    """The whole text of a .docx, as read_word gives it (an offset into it is a `start` of read_word)."""
    path, z = _open(path, ".docx", ".doc")
    with z:
        body = _xml(z, "word/document.xml", path).find(f"{{{WORD}}}body")
    lines = []
    for block in body if body is not None else []:
        if block.tag == f"{{{WORD}}}p":
            lines.append(_words(block, WORD, "t").strip())
        elif block.tag == f"{{{WORD}}}tbl":
            for row in block.iter(f"{{{WORD}}}tr"):
                lines.append(" | ".join(_words(cell, WORD, "t").strip() for cell in row.iter(f"{{{WORD}}}tc")))
    return "\n".join(line for line in lines if line)


def tool_read_word(path: str, start: int = 0) -> str:
    return _window(word_text(path), start, "(no text in this document)")


def _slide_text(root) -> str:
    """The text of a slide or of its notes, in document order; a table gives one line per row, "cell | cell"."""
    lines: list[str] = []

    def walk(element):
        if element.tag == f"{{{DRAWING}}}tbl":
            for row in element.iter(f"{{{DRAWING}}}tr"):
                cells = [" ".join(_words(p, DRAWING, "t").strip() for p in cell.iter(f"{{{DRAWING}}}p")).strip()
                         for cell in row.iter(f"{{{DRAWING}}}tc")]
                if any(cells):
                    lines.append(" | ".join(cells))
        elif element.tag == f"{{{DRAWING}}}p":
            text = _words(element, DRAWING, "t").strip()
            if text:
                lines.append(text)
        else:
            for child in element:
                walk(child)

    walk(root)
    return "\n".join(lines)


def _notes_of(z: zipfile.ZipFile, slide: str) -> str | None:
    """The notes part of a slide, found through the slide's relationships: notesSlideN is numbered in the order the notes were
    created, which is not the slide's number."""
    rels = re.sub(r"ppt/slides/(slide\d+\.xml)$", r"ppt/slides/_rels/\1.rels", slide)
    try:
        root = ET.fromstring(z.read(rels))
    except (KeyError, ET.ParseError):
        return None
    for rel in root:
        if rel.get("Type", "").endswith("/notesSlide"):
            target = rel.get("Target", "")
            name = posixpath.normpath(posixpath.join("ppt/slides", target)) if not target.startswith("/") else target.lstrip("/")
            return name if name in z.namelist() else None
    return None


def powerpoint_slides(path: str) -> list[tuple[int, str]]:
    """(slide number, its text and its speaker notes) for every slide, in order."""
    path, z = _open(path, ".pptx", ".ppt")
    with z:
        number = lambda n: int(re.search(r"(\d+)\.xml$", n).group(1))  # noqa: E731
        slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=number)
        out = []
        for name in slides:
            text = _slide_text(_xml(z, name, path))
            notes_name = _notes_of(z, name)
            note = _slide_text(_xml(z, notes_name, path)) if notes_name else ""
            out.append((number(name), text + (f"\n(notes) {note}" if note else "")))
    return out


def tool_read_powerpoint(path: str, start: int = 0, slide: int | None = None) -> str:
    slides = powerpoint_slides(path)
    if slide is not None:
        for number, text in slides:
            if number == int(slide):
                return _window(f"--- slide {number} ---\n{text}", start, "(this slide has no text)")
        raise ToolError(f"{path} has {len(slides)} slide(s): there is no slide {slide}.")
    return _window("\n\n".join(f"--- slide {n} ---\n{t}" for n, t in slides), start, "(no slides in this presentation)")
