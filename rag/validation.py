"""Mechanical checks of an answer against what the turn's tools returned.

A tool that ran is not evidence: an empty search or an empty list of votes
supports nothing. Evidence is the rows a successful tool result returned, and
only their provenance fields (a vote's `source`, a passage's `source_url`) make a
link citable. A URL inside retrieved text, a tool's notes, an earlier turn or the
artifact the UI shows cites nothing. An answer linking anything else is withheld,
not repaired: guessing which source a wrong link meant would be inference.

These checks are about links, not claims. They cannot show that a cited passage
supports the sentence beside it.

Around the answer the app prints text that no check covers: the arguments the
model gave a tool, the errors tools return, document titles. `literal` and
`inline_code` make it show as written: no image loads from it, and no link hides
behind its words.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from markdown_it import MarkdownIt

MARKDOWN = MarkdownIt("commonmark")
# Rows whose provenance field makes a link citable, by tool: (list, field).
SOURCES = {
    "voting_record": (("votes", "source"),),
    "find_votes": (("votes", "source"),),
    "programme_votes": (("votes", "source"), ("promises", "source_url")),
    "search_documents": (("passages", "source_url"),),
}
# Figures computed over the roll-call record, which has no page to link. A count
# of zero is a finding too, unlike an empty search.
FIGURES = {
    "voting_agreement": ("votes_listing_both", "both_voted", "same_choice"),
    "fraction_cohesion": ("votes", "votes_with_two_or_more_voting", "unanimous"),
}
# Raw HTML, images and code are never needed, and an image loads on its own.
UNSUPPORTED = {"html_block", "html_inline", "image", "fence", "code_block"}
# Streamlit renders these as links even when they are not Markdown links.
BARE_URL = re.compile(r"https?://|www\.", re.IGNORECASE)
# ASCII punctuation: Markdown prints any of it as written when it is escaped.
PUNCTUATION = re.compile(r"([!-/:-@\[-`{-~])")


def literal(text: str) -> str:
    """Text for Markdown that shows as written: every punctuation mark escaped, so
    none of it makes an image, a link, HTML or formatting. A URL in it stays
    visible as itself, though Streamlit still makes it clickable."""
    return PUNCTUATION.sub(r"\\\1", text)


def inline_code(text: str) -> str:
    """Text as inline Markdown code, which shows it as written: the fence is longer
    than any run of backticks inside, and no line break can end the paragraph."""
    fence = "`" * (max(map(len, re.findall(r"`+", text)), default=0) + 1)
    return f"{fence} {' '.join(text.split())} {fence}"


@dataclass
class Evidence:
    """What the successful tool results of one turn can support."""

    urls: set[str] = field(default_factory=set)
    figures: bool = False

    @property
    def usable(self) -> bool:
        return bool(self.urls) or self.figures

    def add(self, tool: str, content: str) -> None:
        """Takes in one successful tool result, as the model read it. Content
        that is not the tool's JSON, and rows without a source, add nothing; a
        passage or programme row must also carry its text."""
        try:
            body = json.loads(content)
        except (TypeError, ValueError):
            return
        if not isinstance(body, dict):
            return
        if tool in FIGURES:
            counts = [body.get(key) for key in FIGURES[tool]]
            self.figures |= all(type(n) is int and n >= 0 for n in counts)
            return
        for rows, source in SOURCES.get(tool, ()):
            for row in body.get(rows) or []:
                if not isinstance(row, dict):
                    continue
                url = row.get(source)
                if not isinstance(url, str) or not url.startswith(
                    ("https://", "http://")
                ):
                    continue
                if source == "source_url" and not str(row.get("content") or "").strip():
                    continue
                self.urls.add(MARKDOWN.normalizeLink(url))

    def check(self, answer: str) -> str | None:
        """Why the answer cannot be shown, or None when it can: every link must be
        a returned source, no URL may stand bare, and an answer must cite at least
        one source unless all it had were computed figures."""
        if not self.usable:
            return "no_evidence"
        if not answer.strip():
            return "empty_answer"
        cited = False
        for block in MARKDOWN.parse(answer):
            if block.type in UNSUPPORTED:
                return "unsupported_format"
            in_link = False
            for token in block.children or []:
                if token.type in UNSUPPORTED:
                    return "unsupported_format"
                if token.type == "link_open":
                    if token.attrGet("href") not in self.urls:
                        return "unknown_citation"
                    cited = in_link = True
                elif token.type == "link_close":
                    in_link = False
                elif not in_link and BARE_URL.search(token.content):
                    return "bare_url"
        if self.urls and not cited:
            return "missing_citation"
        return None
