"""Seimas plenary stenograms: harvest, split into speaker turns, chunk, load.

    uv run python -m ingest.stenograms harvest   # network; shares the Seimas lock
    uv run python -m ingest.stenograms stats     # offline: parsing and resolution
    uv run python -m ingest.stenograms load      # offline: documents + chunks
    uv run python -m ingest.stenograms load --new  # add new sittings, keep the rest

Structure comes from the markup e-seimas serves. `Laikas` paragraphs give the
time a section started, `Roman12` its agenda title and `Priemimas` a stage
within it. A turn opens with a bold marker: `V. PRANCKIETIS (LSF).` or
`PIRMININKAS (J. OLEKAS, LSDPF*).`; a bare `PIRMININKAS.` is whoever last took
the chair. Each stenogram ends with the Seimas's own legend of fraction
abbreviations, which `stats` checks against `fractions.abbrev`.

A chunk is a speaker turn, split at paragraph and then sentence boundaries when
long, and every chunk repeats its speaker marker. Speakers resolve to an MP by
first initial and surname within the mandate, which is unambiguous in term 10.
Ministers and guests who are not MPs keep their name and no MP id.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as clock_time

import psycopg

from .config import DATA_DIR, DATABASE_URL, TERM_ID
from .db import ensure_schema
from .http import _cache_path, fetch
from .names import normal
from .seimas import VILNIUS, bill_number, read_record
from .text import paragraphs, split_long

STATUS = DATA_DIR / "seimas_stenograms.json"
LOCK = DATA_DIR / "seimas_harvest.lock"  # one network job against the Seimas at a time
RECENT = timedelta(days=14)
MAX_CHARS = 1500
UPPER = "A-ZĄČĘĖĮŠŲŪŽ"
CHAIR = re.compile(
    r"^PIRMININK(?:AS|Ė)\s*(?:\(\s*(?P<name>[^,)]+?)\s*"
    r"(?:,\s*(?P<fraction>[^)*]+?)\s*\*?\s*)?\))?\s*\.\s*"
)
SPEAKER = re.compile(
    rf"^(?P<name>(?:[{UPPER}]\.\s*)+[{UPPER}][{UPPER}\-]*[{UPPER}])\s*"
    r"(?:\(\s*(?P<fraction>[^)*]+?)\s*\*?\s*\))?\s*\.\s*"
)
INITIALS = re.compile(rf"((?:[{UPPER}]\.\s*)+)(.+)")
TIME = re.compile(r"^(\d{1,2})\.(\d{2}) val\.$")
LEGEND = re.compile(r"^\*\s*Santrumpų reikšmės:\s*(?P<body>.+)$")
FRONT_MATTER = {"MsoTitle", "MsoHeader", "MsoNormal", "Pirmininkai"}


def text_url(portal_url: str) -> str:
    """Document text for an e-seimas portal link ending in /<type>/<guid>: TAK for
    a stenogram, TAP for a bill."""
    kind, guid = portal_url.split("?")[0].rstrip("/").rsplit("/", 2)[1:]
    return f"https://e-seimas.lrs.lt/rs/legalact/{kind}/{guid}/"


@dataclass
class Turn:
    marker: str  # as printed, without the closing period or footnote star
    name: str | None  # "V. PRANCKIETIS"; for a bare PIRMININKAS, the chair in office
    fraction: str | None  # abbreviation printed with the name, as of this sitting
    chair: bool
    section: str | None
    section_time: clock_time | None
    paragraphs: list[str] = field(default_factory=list)


def parse_legend(body: str) -> dict[str, str]:
    legend = {}
    for item in body.rstrip(".").split(";"):
        abbrev, _, name = item.partition("–")
        if name.strip():
            legend[abbrev.strip()] = name.strip()
    return legend


def parse(document: str) -> tuple[list[Turn], dict[str, str]]:
    turns: list[Turn] = []
    legend: dict[str, str] = {}
    section, section_time, previous_kind = None, None, ""
    chair_name, chair_fraction = None, None
    started = False
    for p in paragraphs(document):
        kind, previous_kind = p.kind, previous_kind if p.kind else previous_kind
        legend_line = LEGEND.match(p.text)
        heading_time = TIME.match(p.text)
        if legend_line:
            legend = parse_legend(legend_line["body"])
        elif kind == "Laikas" and heading_time:
            started = True
            section, section_time = (
                None,
                clock_time(int(heading_time[1]), int(heading_time[2])),
            )
        elif kind == "Roman12":
            started = True
            if previous_kind != "Laikas":
                section_time = None
            section = p.text
        elif not started and kind in FRONT_MATTER:
            pass
        elif kind == "Priemimas":
            base = (section or "").split(" — ")[0]
            section = f"{base} — {p.text}" if base else p.text
        elif p.bold and (opener := CHAIR.match(p.text) or SPEAKER.match(p.text)):
            started = True
            is_chair = opener.re is CHAIR
            name, fraction = opener["name"], opener["fraction"]
            if is_chair and name:
                chair_name, chair_fraction = name, fraction
            elif is_chair:
                name, fraction = chair_name, chair_fraction
            marker = p.text[: opener.end()].strip().rstrip(".").replace("*", "")
            marker = re.sub(r"\s+([,)])", r"\1", re.sub(r"\(\s+", "(", marker)).strip()
            rest = p.text[opener.end() :].strip()
            turns.append(
                Turn(
                    marker,
                    name,
                    fraction and fraction.strip(),
                    is_chair,
                    section,
                    section_time,
                    [rest] if rest else [],
                )
            )
        elif turns:
            turns[-1].paragraphs.append(p.text)
        previous_kind = kind
    return turns, legend


def chunks_of(turn: Turn, limit: int = MAX_CHARS) -> list[str]:
    pieces, current = [], ""
    for paragraph in turn.paragraphs:
        for piece in split_long(paragraph, limit):
            if current and len(current) + 1 + len(piece) > limit:
                pieces.append(current)
                current = piece
            else:
                current = f"{current} {piece}".strip()
    if current:
        pieces.append(current)
    return [f"{turn.marker}. {piece}" for piece in pieces]


def speaker_index(rows) -> dict[tuple[str, str], list[tuple[int, date, date | None]]]:
    index = defaultdict(list)
    for mp_id, first_name, last_name, since, until in rows:
        index[(normal(first_name)[:1], normal(last_name))].append((mp_id, since, until))
    return index


def resolve(name: str | None, day: date, index) -> int | None:
    """The MP a printed name denotes on `day`, or None when there is not exactly one."""
    match = INITIALS.fullmatch(name or "")
    if not match:
        return None
    serving = [
        mp_id
        for mp_id, since, until in index.get(
            (normal(match[1])[:1], normal(match[2])), []
        )
        if since <= day and (until is None or day <= until)
    ]
    return serving[0] if len(serving) == 1 else None


def stenogram_sittings() -> list[dict]:
    sittings = [
        s for s in read_record().sittings if s["stenogram_url"] and s["starts_at"]
    ]
    return sorted(sittings, key=lambda s: s["starts_at"])


def mp_rows(conn) -> list[tuple]:
    return conn.execute(
        "SELECT id, first_name, last_name, date_from, date_to FROM mps"
    ).fetchall()


# ------------------------------------------------------------------ harvest


def harvest(args: argparse.Namespace) -> None:
    lock = LOCK.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise SystemExit("another Seimas harvest is running; one at a time") from exc
    today = datetime.now(VILNIUS).date()
    sittings = stenogram_sittings()
    made, failed, started = 0, [], time.monotonic()
    for n, s in enumerate(sittings, 1):
        url = text_url(s["stenogram_url"])
        # recent stenograms are published as drafts first and corrected later
        force = today - s["starts_at"].date() <= RECENT
        if force or not _cache_path(url, ".html").exists():
            made += 1
        # A stenogram runs to megabytes and e-seimas can take over 30 s to send
        # one; waiting longer is gentler on it than failing and asking again.
        if fetch(url, suffix=".html", force=force, timeout=90) is None:
            failed.append(url)
        if n % 20 == 0 or n == len(sittings):
            state = {
                "stenogram": n,
                "of": len(sittings),
                "requests": made,
                "failed": len(failed),
                "elapsed_min": round((time.monotonic() - started) / 60, 1),
                "failed_urls": failed[:20],
            }
            STATUS.write_text(json.dumps(state, indent=2), "utf-8")
            print(json.dumps(state), flush=True)


# ------------------------------------------------------------ stats, load


def stats(args: argparse.Namespace) -> None:
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            index = speaker_index(mp_rows(conn))
            known = {a for (a,) in conn.execute("SELECT abbrev FROM fractions")} - {
                None
            }
    except psycopg.OperationalError:
        index, known = {}, set()
    counts, unresolved, lengths, abbrevs = Counter(), Counter(), [], set()
    for s in stenogram_sittings():
        body = fetch(text_url(s["stenogram_url"]), suffix=".html", offline=True)
        if body is None:
            counts["sittings whose stenogram is not cached"] += 1
            continue
        turns, legend = parse(body)
        counts["stenograms"] += 1
        abbrevs |= set(legend)
        for turn in turns:
            counts["chair turns" if turn.chair else "speaker turns"] += 1
            if turn.section is None:
                counts["turns before any titled section"] += 1
            if index:
                if resolve(turn.name, s["starts_at"].date(), index):
                    counts["turns resolved to an MP"] += 1
                else:
                    unresolved[turn.name or turn.marker] += 1
            lengths += [len(c) for c in chunks_of(turn)]
    counts["chunks"] = len(lengths)
    for key, value in counts.items():
        print(f"  {value:>7,}  {key}")
    if lengths:
        print(
            f"  chunk length: mean {sum(lengths) // len(lengths)}, max {max(lengths)}"
        )
    print(
        "  unresolved speakers (not MPs, or not uniquely):", unresolved.most_common(12)
    )
    if known:
        print(
            "  legend abbreviations missing from fractions.abbrev:",
            sorted(abbrevs - known),
        )


CHUNK_COLUMNS = (
    "document_id, ordinal, content, speaker_name, speaker_mp_id, fraction, doc_type, "
    "doc_date, source_url, sitting_id, section, section_at, bill_number"
)


def title_of(sitting: dict) -> str:
    day = sitting["starts_at"].date()
    return f"Seimo posėdžio stenograma, {day:%Y-%m-%d}, {sitting['kind'] or 'posėdis'}"


def sitting_chunks(
    document_id: int | None,
    sitting: dict,
    turns: list[Turn],
    index,
    counts: Counter,
    start: int = 0,
) -> list[tuple]:
    """Chunk rows (CHUNK_COLUMNS) for one sitting's speaker turns, numbered from
    `start`."""
    day = sitting["starts_at"].date()
    rows = []
    for turn in turns:
        mp_id = resolve(turn.name, day, index)
        counts["turns resolved to an MP" if mp_id else "turns by others"] += 1
        at = (
            datetime.combine(day, turn.section_time, tzinfo=VILNIUS)
            if turn.section_time
            else None
        )
        for text in chunks_of(turn):
            rows.append(
                (
                    document_id,
                    start + len(rows),
                    text,
                    turn.name or turn.marker,
                    mp_id,
                    turn.fraction,
                    "stenogram",
                    day,
                    sitting["stenogram_url"],
                    sitting["id"],
                    turn.section,
                    at,
                    bill_number(turn.section),
                )
            )
    return rows


def cached_turns(sitting: dict, counts: Counter) -> list[Turn]:
    body = fetch(text_url(sitting["stenogram_url"]), suffix=".html", offline=True)
    if body is None:
        counts["sittings whose stenogram is not cached"] += 1
        return []
    turns, _ = parse(body)
    if not turns:
        counts["stenograms with no speaker turns"] += 1
    return turns


def insert_document(cur, sitting: dict) -> int | None:
    """The new stenogram document's id; None when another sitting shares it."""
    inserted = cur.execute(
        "INSERT INTO documents "
        "(doc_type, title, doc_date, term_id, source_url) "
        "VALUES ('stenogram', %s, %s, %s, %s) "
        "ON CONFLICT (source_url) DO NOTHING RETURNING id",
        (
            title_of(sitting),
            sitting["starts_at"].date(),
            TERM_ID,
            sitting["stenogram_url"],
        ),
    ).fetchone()
    return inserted[0] if inserted else None


def write_chunks(cur, rows: list[tuple]) -> None:
    with cur.copy(f"COPY chunks ({CHUNK_COLUMNS}) FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)


def load(args: argparse.Namespace) -> None:
    ensure_schema()
    if args.new:
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            counts = load_new(conn)
        for key, value in counts.items():
            print(f"  {value:>7,}  {key}")
        return
    counts, rows = Counter(), []
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        embedded = cur.execute(
            "SELECT count(*) FROM chunks "
            "WHERE doc_type = 'stenogram' AND embedding IS NOT NULL"
        ).fetchone()[0]
        if embedded and not args.force:
            raise SystemExit(
                f"{embedded} stenogram chunks carry embeddings; "
                "--force rebuilds and drops them, --new adds new sittings only"
            )
        index = speaker_index(mp_rows(conn))
        cur.execute("DELETE FROM documents WHERE doc_type = 'stenogram'")
        for s in stenogram_sittings():
            turns = cached_turns(s, counts)
            if not turns:
                continue
            document_id = insert_document(cur, s)
            if document_id is None:
                counts["stenograms shared by two sittings (kept once)"] += 1
                continue
            counts["stenograms"] += 1
            rows += sitting_chunks(document_id, s, turns, index, counts, len(rows))
        write_chunks(cur, rows)
    counts["chunks"] = len(rows)
    for key, value in counts.items():
        print(f"  {value:>7,}  {key}")


def load_new(conn, *, today: date | None = None) -> Counter:
    """Add the stenograms of sittings not loaded yet, and reload a recent one
    whose text changed: e-seimas publishes drafts first and corrects them. Every
    other stenogram keeps its chunks and their embeddings. One transaction, so a
    search never sees half a stenogram. `conn` is in autocommit mode."""
    today = today or datetime.now(VILNIUS).date()
    counts = Counter()
    index = speaker_index(mp_rows(conn))
    loaded = dict(
        conn.execute(
            "SELECT source_url, id FROM documents WHERE doc_type = 'stenogram'"
        ).fetchall()
    )
    seen = set()
    with conn.transaction(), conn.cursor() as cur:
        for s in stenogram_sittings():
            url = s["stenogram_url"]
            if url in seen:
                continue  # a stenogram shared by two sittings
            seen.add(url)
            old_id = loaded.get(url)
            if old_id is not None and today - s["starts_at"].date() > RECENT:
                continue
            turns = cached_turns(s, counts)
            if not turns:
                continue
            rows = sitting_chunks(None, s, turns, index, Counter())
            if old_id is not None:
                stored = [
                    content
                    for (content,) in cur.execute(
                        "SELECT content FROM chunks WHERE document_id = %s "
                        "ORDER BY ordinal",
                        (old_id,),
                    )
                ]
                if stored == [row[2] for row in rows]:
                    continue
                cur.execute("DELETE FROM chunks WHERE document_id = %s", (old_id,))
                cur.execute("DELETE FROM documents WHERE id = %s", (old_id,))
                counts["stenograms replaced"] += 1
            else:
                counts["stenograms added"] += 1
            document_id = insert_document(cur, s)
            if document_id is None:
                raise SystemExit(f"{url} is loaded as another kind of document")
            write_chunks(cur, [(document_id, *row[1:]) for row in rows])
            counts["chunks"] += len(rows)
    return counts


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("harvest", help="fetch stenogram texts (network)").set_defaults(
        func=harvest
    )
    sub.add_parser("stats", help="parse cached stenograms and report").set_defaults(
        func=stats
    )
    p = sub.add_parser("load", help="rebuild stenogram documents and chunks")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--force", action="store_true", help="rebuild even if chunks are embedded"
    )
    mode.add_argument(
        "--new",
        action="store_true",
        help="only add new sittings and reload recent ones that changed",
    )
    p.set_defaults(func=load)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
