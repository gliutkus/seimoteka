"""Text extraction for e-seimas documents: stenograms, protocols, bills.

e-seimas serves Word-exported HTML. Structure is in the paragraph classes, and
words are hyphenated for print with U+00AD, which silently breaks every search.
Extract per paragraph with text_content(): replacing tags with spaces instead
splits paragraph drop caps from their word ("U žsiregistravo").
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass

from lxml import etree
from lxml import html as LH


@dataclass(frozen=True)
class Paragraph:
    kind: str  # the paragraph's class: Roman, Laikas, Roman12, ...
    text: str
    bold: str  # the bold run the paragraph opens with, or ""


def clean(text: str) -> str:
    """Drop print hyphenation and normalise whitespace."""
    return " ".join(text.replace("\u00ad", "").replace("\xa0", " ").split())


def paragraphs(document) -> list[Paragraph]:
    """The non-empty paragraphs of an HTML document, given as text or parsed."""
    root = LH.fromstring(document) if isinstance(document, str) else document
    out = []
    for p in root.iter("p"):
        text = clean(p.text_content())
        if not text:
            continue
        bold = next(
            (clean(b.text_content()) for b in p.iter("b") if clean(b.text_content())),
            "",
        )
        out.append(
            Paragraph(p.get("class") or "", text, bold if text.startswith(bold) else "")
        )
    return out


ROMAN_END = re.compile(r"(?:^|\s)[IVXLC]+$")


def join_superscripts(root) -> None:
    """Article 37¹ and chapter XVIII¹ are printed 37<sup>1</sup> and
    XVIII<sup>1</sup>, which read as article 371 and chapter XVIII1. Write them
    37-1 and XVIII-1, as the Seimas's own plain text does."""
    for sup in root.iter("sup"):
        digits = clean(sup.text_content())
        previous = sup.getprevious()
        if previous is None:
            before = sup.getparent().text or ""
        else:
            before = previous.tail or previous.text_content()
        if digits.isdigit() and (before[-1:].isdigit() or ROMAN_END.search(before)):
            tail = sup.tail
            sup.clear()
            sup.text, sup.tail = f"-{digits}", tail


def split_long(text: str, limit: int) -> list[str]:
    """Split at sentence ends so no piece exceeds `limit` unless one sentence does."""
    if len(text) <= limit:
        return [text]
    out, current = [], ""
    for sentence in re.split(r"(?<=[.!?…])\s+", text):
        if current and len(current) + 1 + len(sentence) > limit:
            out.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    return out + ([current] if current else [])


BLOCK_TAGS = ("p", "li", "h1", "h2", "h3", "h4", "h5", "h6")


def blocks(root) -> list[str]:
    """Texts of leaf blocks in document order: paragraphs, list items, headings.

    A block that contains other blocks is skipped in favour of its children, so a
    paragraph inside a list item is read once.
    """
    out = []
    for element in root.iter(*BLOCK_TAGS):
        if any(d.tag in BLOCK_TAGS for d in element.iterdescendants()):
            continue
        text = clean(element.text_content())
        if re.search(r"\w", text):  # a bare "." is a placeholder, not a block
            out.append(text)
    return out


def blocks_from_html(fragment: str) -> list[str]:
    return blocks(LH.fromstring(f"<div>{fragment}</div>")) if fragment.strip() else []


def group_blocks(texts: list[str], limit: int) -> list[str]:
    """Consecutive blocks joined by newlines into pieces of at most `limit` chars."""
    pieces, current = [], []
    for text in texts:
        for part in split_long(text, limit):
            if current and len("\n".join([*current, part])) > limit:
                pieces.append("\n".join(current))
                current = []
            current.append(part)
    return pieces + (["\n".join(current)] if current else [])


WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
SAFE_XML = etree.XMLParser(resolve_entities=False, no_network=True)


def docx_blocks(data: bytes) -> list[str]:
    """Paragraph texts of a .docx, read from its XML without extra libraries."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        root = etree.fromstring(archive.read("word/document.xml"), SAFE_XML)
    out = []
    for paragraph in root.iter(f"{WORD}p"):
        text = clean("".join(run.text or "" for run in paragraph.iter(f"{WORD}t")))
        if re.search(r"\w", text):
            out.append(text)
    return out
