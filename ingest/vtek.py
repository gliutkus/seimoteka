"""VTEK decisions about term-10 MPs, from the commission's public register.

    uv run python -m ingest.vtek harvest   # network: pinreg.vtek.lt
    uv run python -m ingest.vtek load      # offline

The Chief Official Ethics Commission publishes its decisions through a public
JSON API behind pinreg.vtek.lt. The register covers officials in general, so only
decisions whose subject's full name is exactly a term-10 MP's are kept; a surname
alone would attribute a stranger's decision to an MP. Decisions VTEK has
anonymised stay anonymous.

Some decisions publish their full text only as an attached document, served by
the register's own /parsisiusti endpoint. A decision can be appealed, and
annulled by a court, after it is published, so the harvest refreshes every
decision it keeps, and every chunk states both flags: a decision is never
retrieved without its legal status.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict

import psycopg
from psycopg.types.json import Jsonb

from .config import DATABASE_URL
from .db import ensure_schema
from .http import fetch, fetch_bytes
from .names import normal
from .text import blocks_from_html, docx_blocks, group_blocks

API = "https://pinreg.vtek.lt/external/viesi-vtek-sprendimai"
INDEX = (
    f"{API}?puslapioNr=1&irasuSkaiciusPuslapyje=500"
    "&rusiuojamiStulpeliai=PRIEMIMO_DATA&rusiavimoKryptis=DESC"
)
PAGE = "https://pinreg.vtek.lt/app/vtek-sprendimai/{}"
MAX_CHARS = 1500


def attachment_url(decision_id: int) -> str:
    return f"{API}/{decision_id}/parsisiusti"


def subject_matches(rows: list[dict], people) -> list[tuple[dict, int]]:
    """Register rows whose subject's full name belongs to exactly one MP."""
    by_name = defaultdict(list)
    for person_id, first, last in people:
        by_name[normal(f"{first} {last}")].append(person_id)
    matches = []
    for row in rows:
        ids = by_name.get(normal(f"{row['vardas']} {row['pavarde']}"), [])
        if len(ids) == 1:
            matches.append((row, ids[0]))
    return matches


def yes_no(flag: bool) -> str:
    return "taip" if flag else "ne"


def published_text(decision: dict) -> list[str]:
    return blocks_from_html(decision.get("sprendimas") or "")


def needs_attachment(decision: dict) -> bool:
    return not published_text(decision) and bool(decision.get("dokumentai"))


def chunks_of(
    decision: dict, full_text: list[str] | None = None, limit: int = MAX_CHARS
) -> list[str]:
    header = (
        f"VTEK sprendimas Nr. {decision['numeris']}, "
        f"priimtas {decision['priemimoData']}, "
        f"dėl asmens {decision['vardas']} {decision['pavarde']} "
        f"({decision['teisesAktasPavadinimas']}). "
        f"Apskųstas teismui: {yes_no(decision['apskustasTeismui'])}. "
        "Panaikintas teismo sprendimu: "
        f"{yes_no(decision['panaikintasTeismoSprendimu'])}."
    )
    operative = blocks_from_html(decision.get("rezoliucineDalis") or "")
    parts = [f"Rezoliucinė dalis:\n{p}" for p in group_blocks(operative, limit)]
    parts += group_blocks(published_text(decision) or (full_text or []), limit)
    dissent = blocks_from_html(decision.get("atskirojiNuomone") or "")
    parts += [f"Atskiroji nuomonė:\n{p}" for p in group_blocks(dissent, limit)]
    return [f"{header}\n{part}" for part in parts]


def mp_people(conn) -> list[tuple]:
    return conn.execute("SELECT id, first_name, last_name FROM mps").fetchall()


def harvest(args: argparse.Namespace) -> None:
    index = fetch(INDEX, suffix=".json", force=True)
    if index is None:
        raise SystemExit("VTEK register unavailable")
    rows = json.loads(index)["content"]
    with psycopg.connect(DATABASE_URL) as conn:
        matches = subject_matches(rows, mp_people(conn))
    failed = []
    for row, _ in matches:
        body = fetch(f"{API}/{row['id']}", suffix=".json", force=True)
        if body is None:
            failed.append(str(row["id"]))
        elif needs_attachment(json.loads(body)):
            if (
                fetch_bytes(attachment_url(row["id"]), suffix=".docx", force=True)
                is None
            ):
                failed.append(f"{row['id']} (attachment)")
    print(
        f"{len(rows)} decisions, {len(matches)} about "
        f"term-10 MPs, failed: {failed or 'none'}"
    )


def load(args: argparse.Namespace) -> None:
    index = fetch(INDEX, suffix=".json", offline=True)
    if index is None:
        raise SystemExit("the VTEK register is not cached; run `harvest` first")
    ensure_schema()
    rows, missing = [], 0
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        embedded = cur.execute(
            "SELECT count(*) FROM chunks "
            "WHERE doc_type = 'vtek' AND embedding IS NOT NULL"
        ).fetchone()[0]
        if embedded and not args.force:
            raise SystemExit(f"{embedded} VTEK chunks carry embeddings; use --force")
        matches = subject_matches(json.loads(index)["content"], mp_people(conn))
        cur.execute("DELETE FROM documents WHERE doc_type = 'vtek'")
        for row, mp_id in matches:
            body = fetch(f"{API}/{row['id']}", suffix=".json", offline=True)
            if body is None:
                missing += 1
                continue
            d = json.loads(body)
            full_text, text_from = None, "register"
            if needs_attachment(d):
                data = fetch_bytes(
                    attachment_url(d["id"]), suffix=".docx", offline=True
                )
                full_text = docx_blocks(data) if data and data[:2] == b"PK" else None
                text_from = "attachment" if full_text else "operative part only"
            source = PAGE.format(d["id"])
            metadata = {
                "number": d["numeris"],
                "law": d["teisesAktasPavadinimas"],
                "appealed_to_court": d["apskustasTeismui"],
                "annulled_by_court": d["panaikintasTeismoSprendimu"],
                "dissenting_opinion": d["yraAtskirojiNuomone"],
                "commissioners_votes": d["balsavimoRezultatai"],
                "full_text_from": text_from,
                "api_url": f"{API}/{d['id']}",
                "subject_matched_by": "exact full name",
            }
            doc_id = cur.execute(
                "INSERT INTO documents "
                "(doc_type, title, doc_date, source_url, metadata) "
                "VALUES ('vtek', %s, %s, %s, %s) RETURNING id",
                (
                    f"VTEK sprendimas {d['numeris']} "
                    f"(asmuo: {d['vardas']} {d['pavarde']})",
                    d["priemimoData"],
                    source,
                    Jsonb(metadata),
                ),
            ).fetchone()[0]
            cur.execute(
                "INSERT INTO document_subjects (document_id, mp_id) VALUES (%s, %s)",
                (doc_id, mp_id),
            )
            for ordinal, text in enumerate(chunks_of(d, full_text)):
                rows.append((doc_id, ordinal, text, "vtek", d["priemimoData"], source))
        columns = "document_id, ordinal, content, doc_type, doc_date, source_url"
        with cur.copy(f"COPY chunks ({columns}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)
    print(
        f"loaded {len(matches) - missing} decisions, {len(rows)} chunks; "
        f"{missing} not cached"
    )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("harvest", help="refresh the register (network)").set_defaults(
        func=harvest
    )
    p = sub.add_parser("load", help="rebuild VTEK documents and chunks")
    p.add_argument(
        "--force", action="store_true", help="rebuild even if chunks are embedded"
    )
    p.set_defaults(func=load)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
