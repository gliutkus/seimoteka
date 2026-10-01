"""Party election programmes for the 2024 Seimas election, from VRK's booklet.

    uv run --group docs python -m ingest.programmes harvest   # one request
    uv run --group docs python -m ingest.programmes stats     # offline
    uv run --group docs python -m ingest.programmes load      # offline

The Central Electoral Commission printed every registered list's programme, one
page each, in its information booklet for the election (pages 8-22). A page
opens with a disclaimer, "RINKIMŲ PROGRAMA", the party name with its ballot
number and an uppercase slogan; the body is bullets, numbered points or
paragraphs. Each bullet or point is kept whole, since each is a promise on its
own. Paragraphs are rebuilt from wrapped PDF lines. Every chunk names its party,
so a promise is never retrieved without its author.
"""

from __future__ import annotations

import argparse
import io
import re
from dataclasses import dataclass, field
from datetime import date

import psycopg

from .config import DATABASE_URL, TERM_ID
from .db import ensure_schema
from .http import fetch_bytes
from .text import split_long

PDF_URL = (
    "https://www.vrk.lt/documents/10180/797805/"
    "Informacinis+leidinys+Seimo+2024+rinkimams+DGMNDT+web.pdf/"
)
PAGES = range(8, 23)
PUBLISHED = date(2024, 9, 6)  # creation date in the booklet's PDF metadata
DISCLAIMER = ("Kandidatų sąrašai gali", "mų kodekso", "mą. Aktualius")
BALLOT = re.compile(r"^(?P<name>.*?)\s*(?:Nr\.\s*)?(?P<number>\d{1,2})$")
ITEM = re.compile(r"^(?:•|\d{1,2}\.)\s+")
ENDS_SENTENCE = (".", "!", "?")
MAX_CHARS = 900


@dataclass
class Programme:
    page: int
    number: int
    party: str
    slogan: str
    # (heading, text, is_item): a heading is the line printed above a list of points
    units: list[tuple[str | None, str, bool]] = field(default_factory=list)


def mostly_upper(line: str) -> bool:
    letters = [c for c in line if c.isalpha()]
    return len(letters) >= 4 and sum(c.isupper() for c in letters) >= 0.7 * len(letters)


def body_units(lines: list[str]) -> list[tuple[str | None, str, bool]]:
    """Rebuild points and paragraphs from wrapped PDF lines."""
    units: list[tuple[str | None, str, bool]] = []
    heading, current, is_item = None, "", False

    def flush() -> None:
        nonlocal current, is_item
        if current:
            units.append((heading, current, is_item))
        current, is_item = "", False

    for k, line in enumerate(lines):
        following = lines[k + 1] if k + 1 < len(lines) else ""
        if ITEM.match(line):
            flush()
            current, is_item = ITEM.sub("", line), True
        elif current and line[:1].islower():
            current = f"{current} {line}"  # a wrapped line
        elif current and not is_item and not current.endswith(ENDS_SENTENCE):
            current = f"{current} {line}"  # a paragraph still mid-sentence
        else:
            flush()
            if ITEM.match(following) and not line.endswith(ENDS_SENTENCE):
                heading = line  # printed above the points that follow
            else:
                heading, current = None, line
    flush()
    return units


def parse_page(text: str, page: int) -> Programme:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in lines if line and not line.startswith(DISCLAIMER)]
    if "PROGRAMA" not in lines:
        raise ValueError(f"page {page}: no programme header")
    i = lines.index("PROGRAMA") + 1
    name: list[str] = []
    number = None
    while i < len(lines) and len(name) < 6:
        line = lines[i]
        ballot = BALLOT.match(line)
        if line == "Nr.":
            i += 1
        elif ballot and number is None:
            number = int(ballot["number"])
            if ballot["name"]:
                name.append(ballot["name"])
            i += 1
            if name:
                break  # the number closes the name
        elif number is not None and mostly_upper(line):
            break  # the number came first; the slogan closes the name
        else:
            name.append(line)
            i += 1
    if number is None or not name:
        raise ValueError(f"page {page}: party name or ballot number not found")
    slogan = []
    while i < len(lines) and mostly_upper(lines[i]):
        slogan.append(lines[i])
        i += 1
    return Programme(
        page, number, " ".join(name), " / ".join(slogan), body_units(lines[i:])
    )


def chunks_of(programme: Programme, limit: int = MAX_CHARS) -> list[str]:
    head = (
        f"{programme.party} (sąrašo Nr. {programme.number}), "
        "2024 m. Seimo rinkimų programa"
    )
    if programme.slogan:
        head += f" „{programme.slogan}“"
    groups: list[tuple[str | None, list[str]]] = []
    for heading, text, is_item in programme.units:
        for piece in split_long(text, limit):
            line = f"• {piece}" if is_item else piece
            if (
                groups
                and groups[-1][0] == heading
                and len("\n".join([*groups[-1][1], line])) <= limit
            ):
                groups[-1][1].append(line)
            else:
                groups.append((heading, [line]))
    return [
        f"{head}.\n"
        + (f"{heading.rstrip(':')}:\n" if heading else "")
        + "\n".join(lines)
        for heading, lines in groups
    ]


def read_programmes() -> list[Programme]:
    from pypdf import PdfReader  # only this module needs it

    data = fetch_bytes(PDF_URL, suffix=".pdf", offline=True)
    if data is None:
        raise SystemExit("the VRK booklet is not cached; run `harvest` first")
    reader = PdfReader(io.BytesIO(data))
    return [parse_page(reader.pages[n - 1].extract_text() or "", n) for n in PAGES]


def harvest(args: argparse.Namespace) -> None:
    data = fetch_bytes(PDF_URL, suffix=".pdf")
    print(f"{len(data):,} bytes" if data else "request failed")


def stats(args: argparse.Namespace) -> None:
    for p in read_programmes():
        items = sum(1 for _, _, is_item in p.units if is_item)
        print(
            f"  p{p.page:>2} Nr.{p.number:>2} {p.party[:48]:<48} "
            f"units {len(p.units):>3} (points {items:>2}) "
            f"chunks {len(chunks_of(p)):>2}  slogan: {p.slogan[:30]}"
        )


def load(args: argparse.Namespace) -> None:
    programmes = read_programmes()
    ensure_schema()
    rows = []
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        embedded = cur.execute(
            "SELECT count(*) FROM chunks "
            "WHERE doc_type = 'programme' AND embedding IS NOT NULL"
        ).fetchone()[0]
        if embedded and not args.force:
            raise SystemExit(
                f"{embedded} programme chunks carry embeddings; use --force"
            )
        cur.execute("DELETE FROM documents WHERE doc_type = 'programme'")
        for p in programmes:
            source = f"{PDF_URL}#page={p.page}"
            doc_id = cur.execute(
                "INSERT INTO documents "
                "(doc_type, title, doc_date, term_id, source_url) "
                "VALUES ('programme', %s, %s, %s, %s) RETURNING id",
                (
                    f"{p.party} — 2024 m. Seimo rinkimų programa",
                    PUBLISHED,
                    TERM_ID,
                    source,
                ),
            ).fetchone()[0]
            for ordinal, text in enumerate(chunks_of(p)):
                rows.append(
                    (doc_id, ordinal, text, "programme", PUBLISHED, source, p.party)
                )
        columns = (
            "document_id, ordinal, content, doc_type, doc_date, source_url, section"
        )
        with cur.copy(f"COPY chunks ({columns}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)
    print(f"loaded {len(programmes)} programmes, {len(rows)} chunks")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("harvest", help="fetch the VRK booklet").set_defaults(func=harvest)
    sub.add_parser("stats", help="parse and summarise").set_defaults(func=stats)
    p = sub.add_parser("load", help="rebuild programme documents and chunks")
    p.add_argument(
        "--force", action="store_true", help="rebuild even if chunks are embedded"
    )
    p.set_defaults(func=load)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
