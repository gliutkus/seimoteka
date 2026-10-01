"""Findings of the Seimas Ethics and Procedures Commission, term 10.

    uv run python -m ingest.ethics harvest   # network; shares the Seimas lock
    uv run python -m ingest.ethics load      # offline

The commission publishes each finding ("Komisijos išvada") as a page on lrs.lt,
listed per term. A finding's title names its subjects in the genitive after
"Seimo nario / narės / narių": "Dėl ... Seimo nario Remigijaus Žemaitaičio įrašo
...", at times behind the member's post ("Seimo narės, Seimo Pirmininko
pavaduotojos Viktorijos Čmilytės-Nielsen pasisakymo"), and a joint finding names
several ("Seimo narių Vytauto Juozapaičio ir Jurgitos Sejonienės elgesio"). A
subject is resolved only when both the given name and the surname decline to the
same MP. Procedural findings, and findings that name only a post, keep no
subject: working out who held the post would be inference.
"""

from __future__ import annotations

import argparse
import fcntl
import re

import psycopg
from lxml import html as LH
from psycopg.types.json import Jsonb

from .config import DATA_DIR, DATABASE_URL, TERM_ID
from .db import ensure_schema
from .http import fetch
from .names import normal, person_at
from .seimas import portal_page
from .text import blocks, clean, group_blocks

LISTING = "https://www.lrs.lt/sip/portal.show?p_r=38585&p_k=1&p_a=1000&p_kade_id=10"
PAGE = "https://www.lrs.lt/sip/portal.show?p_r=38585&p_k=1&p_t={}"
LOCK = DATA_DIR / "seimas_harvest.lock"
DATED = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\s+Nr\.\s*(?P<number>\S+)$")
MEMBER = {"nario", "nares", "nariu"}  # "Seimo nario / narės / narių", folded
POST_WORDS = 6  # words a member's post may take before the name
MAX_CHARS = 1500


def listed(page: str) -> dict[int, str]:
    findings: dict[int, str] = {}
    for a in LH.fromstring(page).iter("a"):
        match = re.search(r"[?&]p_t=(\d+)", a.get("href") or "")
        title = clean(a.text_content())
        if match and title.startswith("Komisijos išvada"):
            findings.setdefault(int(match[1]), title)
    return findings


def parse_page(page: str) -> dict:
    box = LH.fromstring(page).get_element_by_id("document-text-container1")
    texts = blocks(box)
    title = texts[0] if texts else ""
    date = number = None
    body = texts[1:]
    for k, text in enumerate(texts):
        dated = DATED.match(text)
        if dated:
            date, number = dated["date"], dated["number"]
            body = texts[k + 1 :]
            if body and body[0] == "Vilnius":
                body = body[1:]
            break
    return {"title": title, "date": date, "number": number, "body": body}


def subjects(title: str, people) -> list[int]:
    """The MPs a finding's title names after "Seimo nario / narės / narių"."""
    words = normal(title).split()
    for i in range(len(words) - 1):
        if words[i] == "seimo" and words[i + 1] in MEMBER:
            named = names_from(words, i + 2, people)
            if named:
                return named
    return []


def names_from(words: list[str], start: int, people) -> list[int]:
    """The list of names that begins within POST_WORDS words of `start`, its names
    joined by "ir" or by commas, which normal() has already dropped."""
    for skip in range(POST_WORDS + 1):
        hit = person_at(words, start + skip, people)
        if hit:
            break
    named = []
    while hit:
        person_id, end = hit
        named.append(person_id)
        if end < len(words) and words[end] == "ir":
            end += 1
        hit = person_at(words, end, people)
    return named


def chunks_of(finding: dict, limit: int = MAX_CHARS) -> list[str]:
    header = (
        "Seimo Etikos ir procedūrų komisijos išvada"
        + (f" Nr. {finding['number']}" if finding["number"] else "")
        + (f", {finding['date']}" if finding["date"] else "")
        + f": {finding['title']}"
    )
    return [f"{header}\n{part}" for part in group_blocks(finding["body"], limit)]


def harvest(args: argparse.Namespace) -> None:
    lock = LOCK.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise SystemExit("another Seimas harvest is running; one at a time") from exc
    listing = fetch(LISTING, suffix=".html", force=True, valid=portal_page)
    if listing is None:
        raise SystemExit("findings listing unavailable")
    ids = listed(listing)
    failed = [
        pt
        for pt in ids
        if fetch(PAGE.format(pt), suffix=".html", valid=portal_page) is None
    ]
    print(f"{len(ids)} findings listed, {len(failed)} failed")


def load(args: argparse.Namespace) -> None:
    listing = fetch(LISTING, suffix=".html", offline=True, valid=portal_page)
    if listing is None:
        raise SystemExit("the findings listing is not cached; run `harvest` first")
    ensure_schema()
    rows, counts = [], {"findings": 0, "about an MP": 0, "not cached": 0}
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        embedded = cur.execute(
            "SELECT count(*) FROM chunks "
            "WHERE doc_type = 'ethics' AND embedding IS NOT NULL"
        ).fetchone()[0]
        if embedded and not args.force:
            raise SystemExit(f"{embedded} ethics chunks carry embeddings; use --force")
        people = conn.execute("SELECT id, first_name, last_name FROM mps").fetchall()
        cur.execute("DELETE FROM documents WHERE doc_type = 'ethics'")
        for pt in listed(listing):
            page = fetch(
                PAGE.format(pt), suffix=".html", offline=True, valid=portal_page
            )
            if page is None:
                counts["not cached"] += 1
                continue
            finding = parse_page(page)
            mp_ids = subjects(finding["title"], people)
            counts["findings"] += 1
            counts["about an MP"] += bool(mp_ids)
            source = PAGE.format(pt)
            doc_id = cur.execute(
                "INSERT INTO documents "
                "(doc_type, title, doc_date, term_id, source_url, metadata) "
                "VALUES ('ethics', %s, %s, %s, %s, %s) RETURNING id",
                (
                    finding["title"],
                    finding["date"],
                    TERM_ID,
                    source,
                    Jsonb({"number": finding["number"], "p_t": pt}),
                ),
            ).fetchone()[0]
            cur.executemany(
                "INSERT INTO document_subjects (document_id, mp_id) VALUES (%s, %s)",
                [(doc_id, mp_id) for mp_id in mp_ids],
            )
            for ordinal, text in enumerate(chunks_of(finding)):
                rows.append((doc_id, ordinal, text, "ethics", finding["date"], source))
        columns = "document_id, ordinal, content, doc_type, doc_date, source_url"
        with cur.copy(f"COPY chunks ({columns}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)
    print(f"{counts}; {len(rows)} chunks")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("harvest", help="fetch the findings (network)").set_defaults(
        func=harvest
    )
    p = sub.add_parser("load", help="rebuild ethics documents and chunks")
    p.add_argument(
        "--force", action="store_true", help="rebuild even if chunks are embedded"
    )
    p.set_defaults(func=load)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
