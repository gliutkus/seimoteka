"""Bill texts on the term-10 agenda, split by article.

    uv run python -m ingest.bills harvest   # network; shares the Seimas lock
    uv run python -m ingest.bills stats     # offline: parsing report
    uv run python -m ingest.bills load      # offline: documents + chunks

Every agenda stage links the bill document (TAP) it considers, one version per
link: XVP-238 as submitted, XVP-238(2) as a committee revised it. e-seimas serves
each as Word-exported HTML under rs/legalact/TAP/<guid>/. A revision is a new
document with a new link, so a cached version is never fetched again, and each
version is loaded as a document of its own: a vote is on one version.

The HTML has no structure beyond bold runs, so structure is read from what a
bill's text always has: a header down to the place line ("Vilnius"), at times a
preamble, the bill's own articles, then signature lines, who submits the bill
("Teikia") and any annex ("priedas"). The bill's own articles are numbered 1, 2,
3 ... in order. An amendment quotes the wording it inserts, often with article
headings of its own, bold and numbered as the law it amends ("„86-1 straipsnis.
..."), and a law restated in full is quoted with its articles numbered from 1: a
heading inside quotes is never the bill's own. In the one bill in twenty whose
quotes do not balance, where a quote was closed with a mark other than “ or ”, a
second reading takes a bold heading that continues the sequence as the bill's
next article. A chunk is a section, split at paragraphs when long, and every
chunk opens with its bill's title and number and its section heading.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field

import psycopg
from lxml import html as LH
from psycopg.types.json import Jsonb

from .config import DATA_DIR, DATABASE_URL, TERM_ID
from .db import ensure_schema
from .http import _cache_path, fetch
from .stenograms import text_url
from .text import Paragraph, group_blocks, join_superscripts, paragraphs

STATUS = DATA_DIR / "seimas_bills.json"
LOCK = DATA_DIR / "seimas_harvest.lock"  # one network job against the Seimas at a time
MAX_CHARS = 1500
TITLE_CHARS = 200  # a longer title is cut in chunk headings; it keeps its number
HEADER_LINES = 15  # the place line closes the header within this many paragraphs
ARTICLE = re.compile(r"^(?P<number>\d+(?:-\d+)?)\.?\s+straipsnis\.", re.IGNORECASE)
PLACE = re.compile(r"(?:^|\s)Vilnius$")  # alone, or after the date: "... Nr. Vilnius"
SIGNATURE = re.compile(
    r"^(?:Seimo Pirminink(?:as|ė)|Respublikos Prezident(?:as|ė)"
    r"|Skelbiu šį Lietuvos Respublikos Seimo priimtą \w+\.?)$"
)
ANNEX = re.compile(r"(?:^|\s)priedas$", re.IGNORECASE)
ANNEX_CHARS = 80  # an annex marker is a short line of its own
OPEN_QUOTE, CLOSE_QUOTES = "„", ("“", "”")
PREAMBLE, TEXT, SUBMITTED, APPENDIX = "Preambulė", "Tekstas", "Teikia", "Priedas"

# One row per bill document: its number and title as the latest stage printed
# them, the day it was first considered, and every stage that considered it.
DOCUMENTS_SQL = """
SELECT a.document_url,
       (array_agg(a.bill_number ORDER BY s.starts_at DESC NULLS LAST))[1],
       (array_agg(a.title ORDER BY s.starts_at DESC NULLS LAST))[1],
       min((s.starts_at AT TIME ZONE 'Europe/Vilnius')::date),
       jsonb_agg(jsonb_build_object(
           'stage', a.stage, 'agenda_item_id', a.id, 'sitting_id', a.sitting_id,
           'day', (s.starts_at AT TIME ZONE 'Europe/Vilnius')::date
       ) ORDER BY s.starts_at)
FROM agenda_items a JOIN sittings s ON s.id = a.sitting_id
WHERE a.document_url IS NOT NULL
GROUP BY a.document_url
ORDER BY max(s.starts_at) DESC NULLS LAST, a.document_url
"""


@dataclass
class Section:
    heading: str
    paragraphs: list[str] = field(default_factory=list)


def quote_depth(depth: int, text: str) -> int:
    closes = sum(text.count(mark) for mark in CLOSE_QUOTES)
    return max(0, depth + text.count(OPEN_QUOTE) - closes)


def body(document: str) -> list[Paragraph]:
    """The paragraphs after the header, with superscript numbers joined."""
    root = LH.fromstring(document)
    join_superscripts(root)
    texts = paragraphs(root)
    start = next(
        (k + 1 for k, p in enumerate(texts[:HEADER_LINES]) if PLACE.search(p.text)), 0
    )
    return texts[start:]


def own_article(p: Paragraph, number: str, quoted: int, last: int, recover: bool):
    """Whether a heading opens the bill's own next article; see the module notes."""
    if "-" in number or (last and int(number) != last + 1):
        return False
    return not quoted or (recover and p.bold.split(" ")[0].rstrip(".") == number)


def read_sections(texts: list[Paragraph], recover: bool) -> tuple[list[Section], bool]:
    """Sections in order, and whether the articles ended inside a quote."""
    sections: list[Section] = []
    sink: list[str] | None = None  # where the next paragraph goes; None drops it
    quoted, closed, last = 0, False, 0  # closed: past the articles, at signatures
    for p in texts:
        text = p.text
        article = ARTICLE.match(text)
        if SIGNATURE.match(text):
            closed, sink = True, None
            continue
        if text.rstrip(":") == SUBMITTED or (
            closed and text.startswith(f"{SUBMITTED} ")
        ):
            rest = text[len(SUBMITTED) :].strip(" :")
            closed, section = True, Section(SUBMITTED, [rest] if rest else [])
        elif (
            not quoted
            and OPEN_QUOTE not in text
            and len(text) <= ANNEX_CHARS
            and ANNEX.search(text)
        ):
            closed, section = True, Section(APPENDIX, [text])
        elif (
            article
            and not closed
            and own_article(p, article["number"], quoted, last, recover)
        ):
            quoted, last, section = 0, int(article["number"]), Section(text)
        elif sink is None and not closed and not sections:
            section = Section(PREAMBLE, [text])
            quoted = quote_depth(quoted, text)
        else:
            if sink is not None:
                sink.append(text)
                if not closed:
                    quoted = quote_depth(quoted, text)
            continue
        sections.append(section)
        sink = section.paragraphs
    return sections, quoted > 0


def parse(document: str) -> list[Section]:
    """A bill's sections in order: preamble, articles, submitters, annexes. A bill
    with no articles, such as a declaration, has its text as one section."""
    texts = body(document)
    sections, unclosed = read_sections(texts, recover=False)
    if unclosed:
        sections, _ = read_sections(texts, recover=True)
    if not any(is_article(s) for s in sections):
        sections = [
            Section(TEXT, s.paragraphs) if s.heading == PREAMBLE else s
            for s in sections
        ]
    return sections


def heading_line(title: str, bill: str | None) -> str:
    """The bill's title, cut short when long, always with the bill's number."""
    if len(title) > TITLE_CHARS:
        title = title[:TITLE_CHARS].rsplit(" ", 1)[0] + " …"
    if bill and bill not in title:
        title = f"{title} (Nr. {bill})"
    return title


def chunks_of(
    sections: list[Section], title: str, bill: str | None, limit: int = MAX_CHARS
) -> list[tuple[str, str]]:
    """(section heading, chunk text) for every piece of every section."""
    head = heading_line(title, bill)
    out = []
    for section in sections:
        prefix = f"{head}\n{section.heading}\n"
        room = max(limit - len(prefix), limit // 2)
        for piece in group_blocks(section.paragraphs, room) or [""]:
            out.append((section.heading, (prefix + piece).rstrip()))
    return out


def is_article(section: Section) -> bool:
    return bool(ARTICLE.match(section.heading))


def bill_documents(conn) -> list[tuple]:
    """(url, bill number, title, first day, stages) per bill document, the most
    recently considered first."""
    return conn.execute(DOCUMENTS_SQL).fetchall()


# ------------------------------------------------------------------ harvest


def harvest(args: argparse.Namespace) -> None:
    lock = LOCK.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise SystemExit("another Seimas harvest is running; one at a time") from exc
    with psycopg.connect(DATABASE_URL) as conn:
        urls = [row[0] for row in bill_documents(conn)]
    made, failed, started = 0, [], time.monotonic()
    for n, url in enumerate(urls, 1):
        target = text_url(url)
        if not _cache_path(target, ".html").exists():
            made += 1
            if fetch(target, suffix=".html", timeout=60) is None:
                failed.append(target)
        if n % 50 == 0 or n == len(urls):
            state = {
                "document": n,
                "of": len(urls),
                "requests": made,
                "failed": len(failed),
                "elapsed_min": round((time.monotonic() - started) / 60, 1),
                "failed_urls": failed[:20],
            }
            STATUS.write_text(json.dumps(state, indent=2), "utf-8")
            print(json.dumps(state), flush=True)


# ------------------------------------------------------------ stats, load


def stats(args: argparse.Namespace) -> None:
    counts, lengths, without = Counter(), [], []
    with psycopg.connect(DATABASE_URL) as conn:
        documents = bill_documents(conn)
    for url, bill, title, _, _ in documents:
        document = fetch(text_url(url), suffix=".html", offline=True)
        if document is None:
            counts["documents not cached"] += 1
            continue
        sections = parse(document)
        header = [p.text for p in paragraphs(document)[:HEADER_LINES]]
        counts["documents"] += 1
        counts["no place line in the header"] += not any(map(PLACE.search, header))
        counts["quotes left open (second reading)"] += read_sections(
            body(document), recover=False
        )[1]
        if any(is_article(s) for s in sections):
            counts["with articles"] += 1
        else:
            counts["without articles"] += 1
            without.append(bill)
        for s in sections:
            kind = "article" if is_article(s) else s.heading
            counts[f"sections: {kind}"] += 1
        lengths += [len(text) for _, text in chunks_of(sections, title, bill)]
    counts["chunks"] = len(lengths)
    for key, value in sorted(counts.items()):
        print(f"  {value:>7,}  {key}")
    if lengths:
        print(
            f"  chunk length: mean {sum(lengths) // len(lengths)}, max {max(lengths)}"
        )
    print("  without articles, first 15:", without[:15])


CHUNK_COLUMNS = (
    "document_id, ordinal, content, doc_type, doc_date, source_url, section, "
    "bill_number"
)


def load(args: argparse.Namespace) -> None:
    ensure_schema()
    counts, rows = Counter(), []
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        embedded = cur.execute(
            "SELECT count(*) FROM chunks WHERE doc_type = 'bill' "
            "AND embedding IS NOT NULL"
        ).fetchone()[0]
        if embedded and not args.force:
            raise SystemExit(
                f"{embedded} bill chunks carry embeddings; "
                "--force rebuilds and drops them"
            )
        documents = bill_documents(conn)
        cur.execute("DELETE FROM documents WHERE doc_type = 'bill'")
        for url, bill, title, day, stages in documents:
            document = fetch(text_url(url), suffix=".html", offline=True)
            if document is None:
                counts["documents not cached"] += 1
                continue
            sections = parse(document)
            if not sections:
                counts["documents with no text"] += 1
                continue
            metadata = {
                "bill_number": bill,
                "text_url": text_url(url),
                "stages": stages,
            }
            doc_id = cur.execute(
                "INSERT INTO documents "
                "(doc_type, title, doc_date, term_id, source_url, metadata) "
                "VALUES ('bill', %s, %s, %s, %s, %s) RETURNING id",
                (title, day, TERM_ID, url, Jsonb(metadata)),
            ).fetchone()[0]
            counts["documents"] += 1
            counts["with articles"] += any(is_article(s) for s in sections)
            pieces = chunks_of(sections, title, bill)
            for ordinal, (heading, text) in enumerate(pieces):
                rows.append((doc_id, ordinal, text, "bill", day, url, heading, bill))
        with cur.copy(f"COPY chunks ({CHUNK_COLUMNS}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)
    counts["chunks"] = len(rows)
    for key, value in counts.items():
        print(f"  {value:>7,}  {key}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("harvest", help="fetch bill texts (network)").set_defaults(
        func=harvest
    )
    sub.add_parser("stats", help="parse cached bills and report").set_defaults(
        func=stats
    )
    p = sub.add_parser("load", help="rebuild bill documents and chunks")
    p.add_argument(
        "--force", action="store_true", help="rebuild even if chunks are embedded"
    )
    p.set_defaults(func=load)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
