"""Seimas structured record for term 10: harvest from the source, load offline.

    uv run python -m ingest.seimas harvest   # warm the cache; serial and polite
    uv run python -m ingest.seimas verify    # open-data XML vs the Seimas vote pages
    uv run python -m ingest.seimas load      # cache -> Postgres
    uv run python -m ingest.seimas report    # reports/phase1.md

The harvest is resumable: every response is cached, so a restart re-reads what
is already on disk at no cost to the source. Run one harvest at a time; the
delay floor is enforced per process. verify, load and report never touch the
network.
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
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import psycopg
from lxml import html as LH

from .config import (
    ALTERNATIVE_CHOICES,
    DATA_DIR,
    DATABASE_URL,
    ROOT,
    TERM10_VOTE_FIRST,
    TERM10_VOTE_LAST,
    TERM_ID,
    VOTE_CHOICES,
    service_url,
)
from .db import ensure_schema
from .http import fetch
from .names import declined_forms

VILNIUS = ZoneInfo("Europe/Vilnius")
PORTAL = "https://www.lrs.lt/sip/portal.show?p_r=35727&p_k=1"
RECENT = timedelta(days=14)  # listings this fresh may still change at the source
EDGE_MISSES = 25  # consecutive ids without results that mark the end of the series
STATUS = DATA_DIR / "seimas_harvest.json"
VERIFY = DATA_DIR / "seimas_verify.json"
REPORT = ROOT / "reports" / "phase1.md"

# Marks in the Už / Prieš / Susil. columns of a Seimas vote page, per choice.
MARKS = {
    "uz": (True, False, False),
    "pries": (False, True, False),
    "susilaike": (False, False, True),
    "nebalsavo": (False, False, False),
    "a": (True, False, False),  # alternative votes: column A
    "b": (False, True, False),  # column B
}


# -------------------------------------------------------------------- urls


def terms_url() -> str:
    return service_url("ad_seimo_kadencijos")


def members_url() -> str:
    return service_url("ad_seimo_nariai", {"kadencijos_id": TERM_ID})


def sessions_url() -> str:
    return service_url("ad_seimo_sesijos", {"kadencijos_id": TERM_ID})


def sittings_url(session_id: str | int) -> str:
    return service_url("ad_seimo_posedziai", {"sesijos_id": session_id})


def agenda_url(sitting_id: str | int) -> str:
    return service_url("ad_sp_darbotvarke", {"posedzio_id": sitting_id})


def vote_url(vote_id: int) -> str:
    return service_url("ad_sp_balsavimo_rezultatai", {"balsavimo_id": vote_id})


def sitting_page_url(sitting_id: str | int) -> str:
    return f"{PORTAL}&p_a=sale_fakt_pos&p_fakt_pos_id={sitting_id}"


def vote_page_url(vote_id: int) -> str:
    return f"{PORTAL}&p_a=sale_bals&p_bals_id={vote_id}"


# ----------------------------------------------------------------- parsing


def clean(value: str | None) -> str:
    return " ".join((value or "").split())


def day(value: str | None) -> date | None:
    value = clean(value)
    return date.fromisoformat(value[:10]) if value else None


def local_time(value: str | None) -> datetime | None:
    value = clean(value)
    if not value:
        return None
    fmt = "%Y-%m-%d %H:%M:%S" if value.count(":") == 2 else "%Y-%m-%d %H:%M"
    return datetime.strptime(value, fmt).replace(tzinfo=VILNIUS)


def clock(value: str | None):
    value = clean(value)
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    return None


def attr(element: ET.Element | None, name: str) -> str | None:
    return (clean(element.get(name)) or None) if element is not None else None


def has_results(body: str | None) -> bool:
    return body is not None and "BendriBalsavimoRezultatai" in body


BILL = re.compile(r"\b([XIV]+P-\d+(?:\(\d+\))?(?:ES)?)")
DASH = re.compile(r"\s*[-–—]\s*")
SEAT = re.compile(r"(.+?)\s*\(Nr\.\s*(\d+)\)\s*apygardoje")


def bill_number(*texts: str | None) -> str | None:
    for text in texts:
        match = BILL.search(text or "")
        if match:
            return match.group(1)
    return None


# Printed on a vote page whose individual rows do not add up to its totals. In
# term 10 this happens only where a member who voted is listed twice; `verify`
# checks that on every page, and the XML totals equal the individual choices.
PROTOCOL_NOTICE = "neatitinka suminių rezultatų"

# The portal sometimes answers with its "page not found" page and status 200.
# Cached, it would read as a sitting without agenda lines, so it is rejected.
PAGE_NOT_FOUND = "Puslapis nerastas"


def portal_page(body: str) -> bool:
    return PAGE_NOT_FOUND not in body


def parse_vote_page(page: str) -> dict:
    """Question, motion, result and as-of-vote fractions from a Seimas vote page.

    An alternative vote ("alternatyvus balsavimas") sets motion A against motion B.
    Its table's columns are A, B and Susil., and the XML records A as "Už" and B
    as "Prieš"; those must never be read as for and against.
    """
    doc = LH.fromstring(page)
    text = clean(" ".join(doc.itertext()))
    hrefs = [a.get("href") or "" for a in doc.iter("a")]

    def first_id(param: str) -> int | None:
        for href in hrefs:
            match = re.search(rf"[?&]{param}=(-?\d+)", href)
            if match:
                return int(match.group(1))
        return None

    def grab(pattern: str) -> tuple[str, ...] | None:
        match = re.search(pattern, text)
        return match.groups() if match else None

    question = grab(
        r"Darbotvarkės klausimas\s*(.*?)\s*"
        r"(?:\(\s*dokumento tekstas|Pranešėjas|Formuluotė)"
    )
    question = re.sub(r"\s+;", ";", question[0]) if question else None
    if not question or re.fullmatch(r"\(\s*Nr\.\s*\)", question):
        question = None
    motions = grab(r"Formuluotė A:\s*(.*?)\s*Formuluotė B:\s*(.*?)\s*Balsavimo laikas:")
    formulation = None if motions else grab(r"Formuluotė:\s*(.*?)\s*Balsavimo laikas:")
    options = r"A\s*-\s*(\d+)\s*,\s*B" if motions else r"už\s*-\s*(\d+)\s*,\s*prieš"
    tally = grab(
        rf"Balsavimo rezultatai:\s*{options}\s*-\s*(\d+)\s*,"
        r"\s*susilaikė\s*-\s*(\d+)\s*,?\s*([^.]*?)\s*\."
    )
    at = grab(r"Balsavimo laikas:\s*(\d{1,2}:\d{2}(?::\d{2})?)")
    voted = grab(r"Balsavo Seimo narių:\s*(\d+)\s*iš\s*(\d+)")

    # A member who changes fraction is listed under both, with the same marks, on
    # every vote that day; a term's first sitting also lists the outgoing members.
    # Rows are merged per member. Where they name different fractions, the
    # member's fraction at that vote is unknown and left blank.
    listed: dict[int, list[tuple[str | None, tuple[bool, ...]]]] = defaultdict(list)
    for tr in doc.iter("tr"):
        cells = tr.findall("td")
        link = cells[0].find(".//a") if len(cells) == 5 else None
        match = (
            re.search(r"p_asm_id=(\d+)", link.get("href") or "")
            if link is not None
            else None
        )
        if match:
            marks = tuple(bool(clean(td.text_content())) for td in cells[2:])
            fraction = clean(cells[1].text_content()) or None
            listed[int(match.group(1))].append((fraction, marks))
    rows: dict[int, tuple[str | None, tuple[bool, ...]]] = {}
    for mp_id, entries in listed.items():
        fractions = {fraction for fraction, _ in entries}
        marks = tuple(any(m[k] for _, m in entries) for k in range(3))
        rows[mp_id] = (fractions.pop() if len(fractions) == 1 else None, marks)

    return {
        "sitting_id": first_id("p_fakt_pos_id"),
        "agenda_item_id": first_id("p_kl_stad_id"),
        "agenda_line_id": first_id("p_svarst_kl_stad_id"),
        "kind": "alternative" if motions else "standard",
        "question": question,
        "formulation": (formulation[0] or None) if formulation else None,
        "formulation_a": motions[0] if motions else None,
        "formulation_b": motions[1] if motions else None,
        "time": clock(at[0]) if at else None,
        "voted": (int(voted[0]), int(voted[1])) if voted else None,
        "tally": tuple(int(n) for n in tally[:3]) if tally else None,
        "result": (tally[3] or None) if tally else None,
        "rows": rows,
        "listed_twice": {
            mp_id: [fraction for fraction, _ in entries]
            for mp_id, entries in listed.items()
            if len(entries) > 1
        },
        "protocol_notice": PROTOCOL_NOTICE in text,
    }


def choice_code(printed: str, kind: str | None) -> str | None:
    """The stored choice for an XML kaip_balsavo value, given the kind of vote."""
    return (ALTERNATIVE_CHOICES if kind == "alternative" else VOTE_CHOICES).get(printed)


def sitting_lines(page: str) -> list[tuple[int, str]]:
    """Agenda lines of a sitting page, in order, keyed by p_svarst_kl_stad_id."""
    seen: set[int] = set()
    lines = []
    for a in LH.fromstring(page).iter("a"):
        match = re.search(r"[?&]p_svarst_kl_stad_id=(-?\d+)", a.get("href") or "")
        if match and int(match.group(1)) not in seen:
            seen.add(int(match.group(1)))
            lines.append((int(match.group(1)), clean(a.text_content())))
    return lines


# ------------------------------------------------------------------ harvest


class Harvest:
    def __init__(self) -> None:
        self.requests = 0
        self.failed: list[str] = []
        self.started = time.monotonic()
        self.today = datetime.now(VILNIUS).date()

    def get(
        self, url: str, *, suffix: str = ".xml", force: bool = False, valid=None
    ) -> str | None:
        if not force:
            body = fetch(url, suffix=suffix, offline=True, valid=valid)
            if body is not None:
                return body
        self.requests += 1
        body = fetch(url, suffix=suffix, force=True, valid=valid)
        if body is None:
            self.failed.append(url)
        return body

    def need(self, url: str, *, force: bool = False) -> ET.Element:
        body = self.get(url, force=force)
        if body is None:
            raise SystemExit(f"required listing unavailable, stopping: {url}")
        return ET.fromstring(body)

    def recent(self, d: date | None) -> bool:
        return d is None or self.today - d <= RECENT

    def status(self, phase: str, **extra) -> None:
        state = {
            "phase": phase,
            "requests": self.requests,
            "failed": len(self.failed),
            "elapsed_min": round((time.monotonic() - self.started) / 60, 1),
            "updated_at": datetime.now(VILNIUS).isoformat(timespec="seconds"),
            **extra,
        }
        STATUS.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
        print(json.dumps(state, ensure_ascii=False), flush=True)


def harvest(args: argparse.Namespace) -> None:
    lock = (DATA_DIR / "seimas_harvest.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise SystemExit("another Seimas harvest is running") from exc

    h = Harvest()
    # Listings that change while the term runs are refreshed once per harvest.
    h.need(terms_url(), force=True)
    h.need(members_url(), force=True)
    sittings: list[tuple[str, date | None]] = []
    for s in h.need(sessions_url(), force=True).iter("SeimoSesija"):
        ended = day(s.get("data_iki"))
        listing = h.need(sittings_url(s.get("sesijos_id")), force=h.recent(ended))
        sittings += [
            (p.get("posėdžio_id"), day(p.get("pradžia")))
            for p in listing.iter("SeimoPosėdis")
        ]
    h.status("sittings", sittings=len(sittings))

    for n, (sitting_id, held) in enumerate(sittings, 1):
        h.get(agenda_url(sitting_id), force=h.recent(held))
        h.get(
            sitting_page_url(sitting_id),
            suffix=".html",
            force=h.recent(held),
            valid=portal_page,
        )
        if n % 50 == 0 or n == len(sittings):
            h.status("agendas", sitting=n, of=len(sittings))

    # Cached "no results" responses past the end go stale as the term runs, so
    # the edge is always re-probed with fresh requests.
    last, misses, probe = TERM10_VOTE_LAST, 0, TERM10_VOTE_LAST - 1
    while misses < EDGE_MISSES:
        if has_results(h.get(vote_url(probe), force=True)):
            last, misses = probe, 0
        else:
            misses += 1
        probe -= 1

    ids = range(TERM10_VOTE_FIRST, last - 1, -1)
    gaps: list[int] = []
    for n, vote_id in enumerate(ids, 1):
        body = h.get(vote_url(vote_id))
        if not has_results(body):
            gaps.append(vote_id)
        else:
            try:
                results = ET.fromstring(body).find(".//BendriBalsavimoRezultatai")
            except ET.ParseError:
                fresh = h.get(vote_url(vote_id), force=True) or "<x/>"
                results = ET.fromstring(fresh).find(".//BendriBalsavimoRezultatai")
            if results is not None and (
                int(results.get("balsavo") or 0) > 0 or args.all_pages
            ):
                voted = day(results.get("balsavimo_laikas"))
                h.get(
                    vote_page_url(vote_id),
                    suffix=".html",
                    force=h.recent(voted),
                    valid=portal_page,
                )
        if n % 100 == 0 or n == len(ids):
            h.status("votes", vote=n, of=len(ids), last_id=last, gaps=len(gaps))

    h.status("done", last_id=last, gaps=gaps[:50], failed_urls=h.failed[:50])


# ------------------------------------------------------------ cache reading


@dataclass
class Record:
    terms: list[dict] = field(default_factory=list)
    sessions: list[dict] = field(default_factory=list)
    mps: list[dict] = field(default_factory=list)
    fraction_names: dict[int, dict[str, date]] = field(default_factory=dict)
    memberships: list[tuple] = field(default_factory=list)
    positions: list[tuple] = field(default_factory=list)
    sittings: list[dict] = field(default_factory=list)
    agenda: list[dict] = field(default_factory=list)
    lines: list[tuple] = field(default_factory=list)
    notes: Counter = field(default_factory=Counter)
    roster_as_of: str | None = None


def cached_xml(url: str) -> ET.Element:
    body = fetch(url, offline=True)
    if body is None:
        raise SystemExit(f"not in the cache, run `harvest` first: {url}")
    return ET.fromstring(body)


def read_members(root: ET.Element, rec: Record) -> None:
    for m in root.iter("SeimoNarys"):
        mp_id = int(m.get("asmens_id"))
        first, last = clean(m.get("vardas")), clean(m.get("pavardė"))
        how = clean(m.get("išrinkimo_būdas"))
        seat = SEAT.fullmatch(how)
        via = "constituency" if seat else "list" if how == "Pagal sąrašą" else None
        if via is None:
            rec.notes[f"unrecognised išrinkimo_būdas {how!r}"] += 1
        served = clean(m.get("kadencijų_skaičius"))
        party = DASH.sub("-", clean(m.get("iškėlusi_partija"))) or None
        rec.mps.append(
            {
                "id": mp_id,
                "term_id": TERM_ID,
                "first_name": first,
                "last_name": last,
                "name_variants": sorted(declined_forms(first) | declined_forms(last)),
                "gender": clean(m.get("lytis")) or None,
                "nominated_by": party,
                "elected_via": via,
                "constituency": seat.group(1) if seat else None,
                "constituency_no": int(seat.group(2)) if seat else None,
                "terms_served": int(served) if served.isdigit() else None,
                "date_from": day(m.get("data_nuo")),
                "date_to": day(m.get("data_iki")),
                "source_url": clean(m.get("biografijos_nuoroda")) or None,
            }
        )
        for p in m.findall("Pareigos"):
            if p.get("parlamentinės_grupės_id"):
                continue  # friendship and interest groups: out of scope
            role = clean(p.get("pareigos"))
            body_id = (
                int(p.get("padalinio_id")) if clean(p.get("padalinio_id")) else None
            )
            body = clean(p.get("padalinio_pavadinimas"))
            since, until = day(p.get("data_nuo")), day(p.get("data_iki"))
            # The ROLE marks fraction membership ('Frakcijos narys', 'narė',
            # 'seniūnas' ...). Body names miss the mixed group, and the role's
            # grammatical gender varies, so neither is a safe test on its own.
            if role.startswith("Frakcijos") and body_id:
                names = rec.fraction_names.setdefault(body_id, {})
                names[body] = max(names.get(body, since), since)
                rec.memberships.append((mp_id, body_id, role, since, until))
            else:
                rec.positions.append(
                    (mp_id, body_id, body or "Seimas", role, since, until)
                )


def read_sitting(p: ET.Element, session_id: int, rec: Record) -> None:
    sitting_id = int(p.get("posėdžio_id"))
    rec.sittings.append(
        {
            "id": sitting_id,
            "session_id": session_id,
            "term_id": TERM_ID,
            "number": clean(p.get("numeris")) or None,
            "kind": clean(p.get("tipas")) or None,
            "starts_at": local_time(p.get("pradžia")),
            "ends_at": local_time(p.get("pabaiga")),
            "stenogram_url": attr(p.find("Stenograma"), "stenogramos_nuoroda"),
            "protocol_url": attr(p.find("Protokolas"), "protokolo_nuoroda"),
            "page_url": sitting_page_url(sitting_id),
        }
    )
    agenda = fetch(agenda_url(sitting_id), offline=True)
    if agenda is None:
        rec.notes["sittings whose agenda is not cached"] += 1
    else:
        for q in ET.fromstring(agenda).iter("DarbotvarkėsKlausimas"):
            title = clean(q.get("pavadinimas"))
            stages = q.findall("KlausimoStadija")
            if not stages:
                rec.notes["agenda questions without a stage id"] += 1
            for stage in stages:
                rec.agenda.append(
                    {
                        "id": int(stage.get("darbotvarkės_klausimo_id")),
                        "sitting_id": sitting_id,
                        "number": clean(q.get("numeris")) or None,
                        "title": title,
                        "stage": attr(stage, "pavadinimas"),
                        "bill_number": bill_number(title),
                        "document_url": attr(stage, "dokumento_nuoroda"),
                        "planned_from": clock(q.get("laikas_nuo")),
                        "planned_to": clock(q.get("laikas_iki")),
                        "rapporteurs": [
                            clean(r.get("asmuo"))
                            for r in q.findall("KlausimoPranešėjas")
                            if clean(r.get("asmuo"))
                        ],
                    }
                )
    page = fetch(
        sitting_page_url(sitting_id), suffix=".html", offline=True, valid=portal_page
    )
    if rec.sittings[-1]["starts_at"] is None:
        # The page of a sitting not yet held shows the sitting in progress.
        rec.notes["sittings without a start time (sitting page ignored)"] += 1
    elif page is None:
        rec.notes["sittings whose sitting page is not cached"] += 1
    else:
        for ordinal, (line_id, title) in enumerate(sitting_lines(page), 1):
            rec.lines.append((line_id, sitting_id, ordinal, title))


def read_record() -> Record:
    rec = Record()
    for t in cached_xml(terms_url()).iter("SeimoKadencija"):
        rec.terms.append(
            {
                "id": int(t.get("kadencijos_id")),
                "label": clean(t.get("pavadinimas")),
                "date_from": day(t.get("data_nuo")),
                "date_to": day(t.get("data_iki")),
            }
        )
    members = cached_xml(members_url())
    rec.roster_as_of = members.get("suformavimo_laikas")
    read_members(members, rec)
    for s in cached_xml(sessions_url()).iter("SeimoSesija"):
        session_id = int(s.get("sesijos_id"))
        rec.sessions.append(
            {
                "id": session_id,
                "term_id": TERM_ID,
                "number": int(s.get("numeris"))
                if clean(s.get("numeris")).isdigit()
                else None,
                "label": clean(s.get("pavadinimas")),
                "date_from": day(s.get("data_nuo")),
                "date_to": day(s.get("data_iki")),
            }
        )
        for p in cached_xml(sittings_url(session_id)).iter("SeimoPosėdis"):
            read_sitting(p, session_id, rec)
    return rec


def vote_range_end() -> int:
    """The last vote id the latest harvest established, else the configured one."""
    try:
        return int(json.loads(STATUS.read_text("utf-8"))["last_id"])
    except (OSError, KeyError, TypeError, ValueError):
        return TERM10_VOTE_LAST


def iter_votes(holes: dict[str, list[int]] | None = None):
    """(vote, XML rows, parsed vote page or None) for every cached term-10 vote.

    Walks the whole range the harvest established, because the series has holes,
    then continues until EDGE_MISSES consecutive ids have no results. Ids inside
    the range without a vote are recorded in `holes`, keyed by kind.
    """
    holes = {} if holes is None else holes
    end = vote_range_end()
    vote_id, misses = TERM10_VOTE_FIRST, 0
    while vote_id >= end or misses < EDGE_MISSES:
        body = fetch(vote_url(vote_id), offline=True)
        if not has_results(body):
            if vote_id >= end:
                kind = "not cached" if body is None else "no results"
                holes.setdefault(kind, []).append(vote_id)
            misses += 1
            vote_id -= 1
            continue
        misses = 0
        root = ET.fromstring(body)
        g = root.find(".//BendriBalsavimoRezultatai")
        vote = {
            "id": vote_id,
            "voted_at": local_time(g.get("balsavimo_laikas")),
            "total_for": int(g.get("už") or 0),
            "total_against": int(g.get("prieš") or 0),
            "total_abstain": int(g.get("susilaikė") or 0),
            "total_voted": int(g.get("balsavo") or 0),
            "total_members": int(g.get("viso") or 0),
        }
        rows = [
            (
                int(r.get("asmens_id")),
                clean(r.get("frakcija")) or None,
                clean(r.get("kaip_balsavo")),
            )
            for r in root.iter("IndividualusBalsavimoRezultatas")
        ]
        page = None
        if vote["total_voted"] > 0:
            html_body = fetch(
                vote_page_url(vote_id), suffix=".html", offline=True, valid=portal_page
            )
            page = parse_vote_page(html_body) if html_body else None
        yield vote, rows, page
        vote_id -= 1


def sittings_by_day(sittings: list[dict]) -> dict[date, list[tuple[datetime, int]]]:
    index: dict[date, list[tuple[datetime, int]]] = defaultdict(list)
    for s in sittings:
        if s["starts_at"]:
            index[s["starts_at"].date()].append((s["starts_at"], s["id"]))
    return index


def sitting_at(when: datetime, index: dict) -> int | None:
    """The sitting in progress at `when`: the latest already started that day,
    else one opening within 30 minutes (the chamber is sometimes called early)."""
    today = index.get(when.date(), [])
    started = [(t, i) for t, i in today if t <= when]
    if started:
        return max(started)[1]
    soon = [(t, i) for t, i in today if t <= when + timedelta(minutes=30)]
    return min(soon)[1] if soon else None


# ------------------------------------------------------------------- verify


def verify(args: argparse.Namespace) -> None:
    index = sittings_by_day(read_record().sittings)
    counts: Counter = Counter()
    fraction_patterns: Counter = Counter()
    examples: dict[str, list] = defaultdict(list)

    def example(kind: str, item) -> None:
        if len(examples[kind]) < 15:
            examples[kind].append(item)

    holes: dict[str, list[int]] = {}
    for vote, rows, page in iter_votes(holes):
        vid = vote["id"]
        counts["vote ids"] += 1
        tally = (vote["total_for"], vote["total_against"], vote["total_abstain"])
        if sum(tally) != vote["total_voted"]:
            counts["XML: for + against + abstain != balsavo"] += 1
            example("xml tally", vid)
        if sum(1 for _, _, c in rows if c) != vote["total_voted"]:
            counts["XML: non-empty choices != balsavo"] += 1
            example("xml cast", vid)
        if len(rows) != vote["total_members"]:
            counts["XML: rows != viso"] += 1
            example("xml rows", vid)
        unknown = sorted({c for _, _, c in rows if c not in VOTE_CHOICES})
        if unknown:
            counts["XML: unknown kaip_balsavo value"] += 1
            example("unknown choice", {"vote": vid, "values": unknown})
        if vote["total_voted"] == 0:
            counts["votes without individual results"] += 1
            continue
        counts["votes with individual results"] += 1
        if page is None:
            counts["vote page missing from cache"] += 1
            example("page missing", vid)
            continue
        if not page["rows"]:
            counts["vote page without an individual table"] += 1
            example("page without table", vid)
            continue
        if page["kind"] == "alternative":
            counts["alternative votes (motion A against motion B)"] += 1
        if page["tally"] != tally:
            counts["page tally != XML tally"] += 1
            example("tally", {"vote": vid, "page": page["tally"], "xml": tally})
        twice = page["listed_twice"]
        if page["protocol_notice"]:
            counts["pages noting a mismatch with the protocol"] += 1
            if not any(any(page["rows"][m][1]) for m in twice):
                counts["  ... of which no voting member is listed twice"] += 1
                example("notice", vid)
        if page["voted"] != (vote["total_voted"], vote["total_members"]):
            if twice:
                counts["page voted/members != XML, page lists a member twice"] += 1
            else:
                counts["page voted/members != XML"] += 1
                example("voted", {"vote": vid, "page": page["voted"]})
        if page["time"] != vote["voted_at"].time():
            counts["page time != XML time"] += 1
            example(
                "time",
                {"vote": vid, "page": str(page["time"]), "xml": str(vote["voted_at"])},
            )
        if page["question"] is None and page["agenda_line_id"] is None:
            counts["vote page with neither question nor agenda line"] += 1
            example("unlinked", vid)
        window = sitting_at(vote["voted_at"], index)
        counts[
            "sitting by time window == page sitting"
            if window == page["sitting_id"]
            else "sitting by time window != page sitting"
        ] += 1
        if len(page["rows"]) != len(rows):
            counts["row count differs between page and XML"] += 1
            example(
                "row count", {"vote": vid, "page": len(page["rows"]), "xml": len(rows)}
            )
        for mp_id, xml_fraction, choice in rows:
            on_page = page["rows"].get(mp_id)
            if on_page is None:
                counts["XML rows absent from page"] += 1
                continue
            counts["rows compared"] += 1
            page_fraction, marks = on_page
            if MARKS.get(choice_code(choice, page["kind"])) != marks:
                counts["rows: choice disagrees"] += 1
                example(
                    "choice", {"vote": vid, "mp": mp_id, "xml": choice, "page": marks}
                )
            if page_fraction is None and mp_id in twice:
                counts["rows: listed under two fractions (fraction unknown)"] += 1
            elif xml_fraction != page_fraction:
                counts["rows: fraction disagrees"] += 1
                fraction_patterns[
                    f"{xml_fraction or '(blank)'} -> {page_fraction or '(blank)'}"
                ] += 1

    for kind, ids in holes.items():
        counts[f"vote ids in the harvested range: {kind}"] = len(ids)
        examples[f"ids {kind}"] = ids[:50]
    result = {
        "checked_at": datetime.now(VILNIUS).isoformat(timespec="seconds"),
        "counts": dict(counts),
        "fraction_disagreements_xml_to_page": dict(fraction_patterns.most_common(40)),
        "examples": examples,
    }
    VERIFY.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    for key, value in counts.items():
        print(f"  {value:>9,}  {key}")
    if fraction_patterns:
        print("\n  fraction disagreements, XML -> page:")
        for pattern, n in fraction_patterns.most_common(12):
            print(f"  {n:>9,}  {pattern}")
    print(f"\nwritten {VERIFY}")


# --------------------------------------------------------------------- load


# Sittings and MPs are upserted, never cleared: corpus chunks reference them. The
# rest is deleted, not truncated: TRUNCATE locks the tables against reads until the
# load commits, and the app, a chat question or the update page, would wait on it.
# After DELETE, readers keep the old record until the new one is committed.
CLEAR_SQL = (
    "DELETE FROM mp_votes",
    "DELETE FROM votes",
    "DELETE FROM agenda_lines",
    "DELETE FROM agenda_items",
    "DELETE FROM mp_positions",
    "DELETE FROM mp_fractions",
)
TERMS_SQL = """
    INSERT INTO terms (id, label, date_from, date_to)
    VALUES (%(id)s, %(label)s, %(date_from)s, %(date_to)s)
    ON CONFLICT (id) DO UPDATE SET label = EXCLUDED.label,
        date_from = EXCLUDED.date_from, date_to = EXCLUDED.date_to
"""
SESSIONS_SQL = """
    INSERT INTO sessions (id, term_id, number, label, date_from, date_to)
    VALUES (%(id)s, %(term_id)s, %(number)s, %(label)s, %(date_from)s, %(date_to)s)
    ON CONFLICT (id) DO UPDATE SET number = EXCLUDED.number, label = EXCLUDED.label,
        date_from = EXCLUDED.date_from, date_to = EXCLUDED.date_to
"""
FRACTIONS_SQL = """
    INSERT INTO fractions (id, term_id, name) VALUES (%s, %s, %s)
    ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name
"""
MPS_SQL = """
    INSERT INTO mps (id, term_id, first_name, last_name, name_variants, gender,
        nominated_by, elected_via, constituency, constituency_no, terms_served,
        date_from, date_to, source_url)
    VALUES (%(id)s, %(term_id)s, %(first_name)s, %(last_name)s,
        %(name_variants)s, %(gender)s, %(nominated_by)s, %(elected_via)s,
        %(constituency)s, %(constituency_no)s, %(terms_served)s, %(date_from)s,
        %(date_to)s, %(source_url)s)
    ON CONFLICT (id) DO UPDATE SET
        first_name = EXCLUDED.first_name, last_name = EXCLUDED.last_name,
        name_variants = EXCLUDED.name_variants, gender = EXCLUDED.gender,
        nominated_by = EXCLUDED.nominated_by, elected_via = EXCLUDED.elected_via,
        constituency = EXCLUDED.constituency,
        constituency_no = EXCLUDED.constituency_no,
        terms_served = EXCLUDED.terms_served, date_from = EXCLUDED.date_from,
        date_to = EXCLUDED.date_to, source_url = EXCLUDED.source_url
"""
MEMBERSHIPS_SQL = """
    INSERT INTO mp_fractions (mp_id, fraction_id, role, date_from, date_to)
    VALUES (%s, %s, %s, %s, %s)
"""
POSITIONS_SQL = """
    INSERT INTO mp_positions (mp_id, body_id, body_name, role, date_from, date_to)
    VALUES (%s, %s, %s, %s, %s, %s)
"""
SITTINGS_SQL = """
    INSERT INTO sittings (id, session_id, term_id, number, kind, starts_at,
        ends_at, stenogram_url, protocol_url, page_url)
    VALUES (%(id)s, %(session_id)s, %(term_id)s, %(number)s, %(kind)s,
        %(starts_at)s, %(ends_at)s, %(stenogram_url)s, %(protocol_url)s,
        %(page_url)s)
    ON CONFLICT (id) DO UPDATE SET session_id = EXCLUDED.session_id,
        number = EXCLUDED.number, kind = EXCLUDED.kind,
        starts_at = EXCLUDED.starts_at, ends_at = EXCLUDED.ends_at,
        stenogram_url = EXCLUDED.stenogram_url,
        protocol_url = EXCLUDED.protocol_url, page_url = EXCLUDED.page_url
"""
AGENDA_SQL = """
    INSERT INTO agenda_items (id, sitting_id, number, title, stage, bill_number,
        document_url, planned_from, planned_to, rapporteurs)
    VALUES (%(id)s, %(sitting_id)s, %(number)s, %(title)s, %(stage)s,
        %(bill_number)s, %(document_url)s, %(planned_from)s, %(planned_to)s,
        %(rapporteurs)s)
"""
LINES_SQL = """
    INSERT INTO agenda_lines (id, sitting_id, ordinal, title)
    VALUES (%s, %s, %s, %s)
"""
VOTES_SQL = """
    INSERT INTO votes (id, term_id, sitting_id, agenda_item_id, agenda_line_id,
        voted_at, question, formulation, kind, formulation_a, formulation_b,
        bill_number, result, total_for, total_against, total_abstain,
        total_voted, total_members, source_url, page_url)
    VALUES (%(id)s, %(term_id)s, %(sitting_id)s, %(agenda_item_id)s,
        %(agenda_line_id)s, %(voted_at)s, %(question)s, %(formulation)s,
        %(kind)s, %(formulation_a)s, %(formulation_b)s, %(bill_number)s,
        %(result)s, %(total_for)s, %(total_against)s, %(total_abstain)s,
        %(total_voted)s, %(total_members)s, %(source_url)s, %(page_url)s)
"""
META_SQL = """
    INSERT INTO dataset_meta (key, value) VALUES (%s, %s)
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
"""

# Which fraction does each vote-page abbreviation denote? Whichever fraction the
# members carrying it belonged to on the day of the vote. Derived, never typed.
ABBREVIATIONS = """
    WITH seen AS (
        SELECT mv.fraction_abbrev AS abbrev, mf.fraction_id, count(*) AS n
        FROM mp_votes mv
        JOIN votes v ON v.id = mv.vote_id
        JOIN mp_fractions mf
          ON mf.mp_id = mv.mp_id
         AND (v.voted_at AT TIME ZONE 'Europe/Vilnius')::date
             BETWEEN mf.date_from AND coalesce(mf.date_to, 'infinity')
        WHERE mv.fraction_abbrev IS NOT NULL
        GROUP BY 1, 2
    )
    SELECT DISTINCT ON (abbrev) abbrev, fraction_id, n,
        n::float / sum(n) OVER (PARTITION BY abbrev) AS share
    FROM seen
    ORDER BY abbrev, n DESC
"""


def unique(rows: list, key, label: str, notes: Counter) -> list:
    seen, out = set(), []
    for row in rows:
        k = key(row)
        if k in seen:
            notes[label] += 1
            continue
        seen.add(k)
        out.append(row)
    return out


def load(args: argparse.Namespace) -> None:
    ensure_schema()
    rec = read_record()
    notes = Counter(rec.notes)
    index = sittings_by_day(rec.sittings)
    sitting_ids = {s["id"] for s in rec.sittings}
    stages = unique(rec.agenda, lambda a: a["id"], "duplicate agenda stage ids", notes)
    agenda = {a["id"]: a for a in stages}
    lines = unique(rec.lines, lambda r: r[0], "duplicate agenda line ids", notes)
    line_ids = {r[0] for r in lines}
    mp_ids = {m["id"] for m in rec.mps}

    votes, mp_votes = [], []
    holes: dict[str, list[int]] = {}
    for vote, rows, page in iter_votes(holes):
        page = page or {}
        item = agenda.get(page.get("agenda_item_id"))
        if page.get("agenda_item_id") and item is None:
            notes["vote pages naming an agenda stage absent from the agenda feed"] += 1
        line_id = page.get("agenda_line_id")
        if line_id is not None and line_id not in line_ids:
            notes["vote pages naming an agenda line absent from sitting pages"] += 1
            line_id = None
        sitting_id = page.get("sitting_id")
        if sitting_id not in sitting_ids:
            sitting_id = sitting_at(vote["voted_at"], index)
        question, formulation = page.get("question"), page.get("formulation")
        bill = bill_number(question, formulation) or (item or {}).get("bill_number")
        votes.append(
            {
                **vote,
                "term_id": TERM_ID,
                "sitting_id": sitting_id,
                "agenda_item_id": item["id"] if item else None,
                "agenda_line_id": line_id,
                "question": question,
                "formulation": formulation,
                "kind": page.get("kind"),
                "formulation_a": page.get("formulation_a"),
                "formulation_b": page.get("formulation_b"),
                "bill_number": bill,
                "result": page.get("result"),
                "source_url": vote_url(vote["id"]),
                "page_url": vote_page_url(vote["id"]),
            }
        )
        if vote["total_voted"] == 0:
            continue
        page_rows = page.get("rows") or {}
        if not page_rows:
            notes["votes with results but no parsed vote page (fractions NULL)"] += 1
        for mp_id, _, choice in rows:
            if choice not in VOTE_CHOICES:
                raise SystemExit(
                    f"unknown kaip_balsavo {choice!r} on vote {vote['id']}; "
                    "inspect before loading"
                )
            if mp_id not in mp_ids:
                notes["vote rows whose asmens_id is not on the roster"] += 1
                continue
            fraction = page_rows[mp_id][0] if mp_id in page_rows else None
            if fraction is None and mp_id in page.get("listed_twice", {}):
                notes["rows listing a member under two fractions (NULL)"] += 1
            code = choice_code(choice, page.get("kind"))
            mp_votes.append((vote["id"], mp_id, code, fraction))

    for kind, ids in holes.items():
        notes[f"vote ids in the harvested range: {kind}"] = len(ids)
    memberships = unique(
        rec.memberships, lambda r: (r[0], r[1], r[3]), "duplicate memberships", notes
    )
    positions = unique(
        rec.positions, lambda r: (r[0], r[2], r[3], r[4]), "duplicate positions", notes
    )
    mp_votes = unique(mp_votes, lambda r: (r[0], r[1]), "duplicate vote rows", notes)
    names = {fid: max(seen, key=seen.get) for fid, seen in rec.fraction_names.items()}
    for fid, seen in rec.fraction_names.items():
        if len(seen) > 1:
            notes[f"fraction {fid} published under {len(seen)} names; latest kept"] += 1

    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        for sql in CLEAR_SQL:
            cur.execute(sql)
        cur.executemany(TERMS_SQL, rec.terms)
        cur.executemany(SESSIONS_SQL, rec.sessions)
        cur.execute("UPDATE fractions SET abbrev = NULL")
        cur.executemany(FRACTIONS_SQL, [(fid, TERM_ID, n) for fid, n in names.items()])
        cur.executemany(MPS_SQL, rec.mps)
        cur.executemany(MEMBERSHIPS_SQL, memberships)
        cur.executemany(POSITIONS_SQL, positions)
        cur.executemany(SITTINGS_SQL, rec.sittings)
        cur.executemany(AGENDA_SQL, list(agenda.values()))
        cur.executemany(LINES_SQL, lines)
        cur.executemany(VOTES_SQL, votes)
        copy_sql = "COPY mp_votes (vote_id, mp_id, choice, fraction_abbrev) FROM STDIN"
        with cur.copy(copy_sql) as copy:
            for row in mp_votes:
                copy.write_row(row)

        cur.execute(ABBREVIATIONS)
        assigned: dict[int, str] = {}
        for abbrev, fraction_id, n, share in cur.fetchall():
            if share < 0.95 or fraction_id in assigned:
                notes[
                    f"abbreviation {abbrev} unassigned ({share:.1%} of {n} rows)"
                ] += 1
                continue
            assigned[fraction_id] = abbrev
            cur.execute(
                "UPDATE fractions SET abbrev = %s WHERE id = %s", (abbrev, fraction_id)
            )

        last = votes[-1] if votes else None
        meta = {
            "scope": f"term {TERM_ID}",
            "roster_generated_at": rec.roster_as_of or "",
            "votes_first_id": str(TERM10_VOTE_FIRST),
            "votes_last_id": str(last["id"]) if last else "",
            "votes_last_at": last["voted_at"].isoformat() if last else "",
            "loaded_at": datetime.now(VILNIUS).isoformat(timespec="seconds"),
        }
        cur.executemany(META_SQL, list(meta.items()))

    print(
        f"loaded {len(rec.mps)} MPs, {len(memberships)} fraction memberships, "
        f"{len(positions)} positions, {len(rec.sittings)} sittings, "
        f"{len(agenda)} agenda stages, {len(lines)} agenda lines, "
        f"{len(votes):,} votes, {len(mp_votes):,} vote rows"
    )
    print(f"fraction abbreviations assigned: {assigned}")
    for note, n in notes.most_common():
        print(f"  note: {n:>6,}  {note}")


# ------------------------------------------------------------------- report


REPORT_QUERIES = {
    "Rows loaded": """
        SELECT t AS "table", n AS "rows" FROM (
            SELECT 'mps' t, count(*) n FROM mps UNION ALL
            SELECT 'mp_fractions', count(*) FROM mp_fractions UNION ALL
            SELECT 'mp_positions', count(*) FROM mp_positions UNION ALL
            SELECT 'fractions', count(*) FROM fractions UNION ALL
            SELECT 'sessions', count(*) FROM sessions UNION ALL
            SELECT 'sittings', count(*) FROM sittings UNION ALL
            SELECT 'agenda_items', count(*) FROM agenda_items UNION ALL
            SELECT 'agenda_lines', count(*) FROM agenda_lines UNION ALL
            SELECT 'votes', count(*) FROM votes UNION ALL
            SELECT 'mp_votes', count(*) FROM mp_votes
        ) x""",
    "Mandates": """
        SELECT count(*) AS "people on the term roster",
            count(*) FILTER (WHERE date_to IS NULL) AS "open mandates",
            count(*) FILTER (WHERE elected_via = 'list') AS "list",
            count(*) FILTER (WHERE elected_via = 'constituency') AS "constituency",
            count(*) FILTER (WHERE NOT EXISTS (
                SELECT 1 FROM mp_fractions f WHERE f.mp_id = mps.id
            )) AS "never in a fraction"
        FROM mps""",
    "Fractions": """
        SELECT f.abbrev, f.name,
            count(DISTINCT mf.mp_id) FILTER (
                WHERE mf.date_to IS NULL AND m.date_to IS NULL
            ) AS "current members",
            count(DISTINCT mf.mp_id) AS "members at any time"
        FROM fractions f
        LEFT JOIN mp_fractions mf ON mf.fraction_id = f.id
        LEFT JOIN mps m ON m.id = mf.mp_id
        GROUP BY f.id ORDER BY 3 DESC""",
    "Sittings": """
        SELECT s.label AS session, count(p.id) AS sittings,
            count(p.stenogram_url) AS "with stenogram link",
            (SELECT count(*) FROM agenda_items a
               JOIN sittings q ON q.id = a.sitting_id
              WHERE q.session_id = s.id) AS "agenda stages"
        FROM sessions s LEFT JOIN sittings p ON p.session_id = s.id
        GROUP BY s.id ORDER BY s.date_from""",
    "Votes": """
        SELECT count(*) AS "vote ids",
            count(*) FILTER (WHERE has_individual_results) AS "with results",
            count(*) FILTER (WHERE NOT has_individual_results) AS "without results",
            min(voted_at AT TIME ZONE 'Europe/Vilnius')::date AS first,
            max(voted_at AT TIME ZONE 'Europe/Vilnius')::date AS last
        FROM votes""",
    "What each vote was about (votes with results)": """
        WITH v AS (SELECT * FROM votes WHERE has_individual_results)
        SELECT count(*) AS votes,
            round(100.0 * count(question) / nullif(count(*), 0), 1) AS "% question",
            round(100.0 * count(formulation) / nullif(count(*), 0), 1) AS "% motion",
            round(100.0 * count(agenda_item_id) / nullif(count(*), 0), 1)
                AS "% stage link",
            round(100.0 * count(coalesce(agenda_item_id, agenda_line_id))
                / nullif(count(*), 0), 1) AS "% stage or line",
            round(100.0 * count(coalesce(question, formulation, agenda_line_id::text))
                / nullif(count(*), 0), 1) AS "% some subject",
            round(100.0 * count(bill_number) / nullif(count(*), 0), 1) AS "% bill",
            round(100.0 * count(sitting_id) / nullif(count(*), 0), 1) AS "% sitting"
        FROM v""",
    "Votes with results but no agenda link (newest 10)": """
        SELECT id, (voted_at AT TIME ZONE 'Europe/Vilnius')::date AS day,
            left(coalesce(question, formulation, '(nothing printed)'), 80) AS subject
        FROM votes
        WHERE has_individual_results
          AND agenda_item_id IS NULL AND agenda_line_id IS NULL
        ORDER BY id LIMIT 10""",
    "Bill number shapes": """
        SELECT regexp_replace(bill_number, '[0-9]+', 'N', 'g') AS shape,
            count(*) AS votes
        FROM votes WHERE bill_number IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 10""",
    "Kinds of vote": """
        SELECT coalesce(kind, '(vote page not read)') AS kind, count(*) AS votes,
            count(*) FILTER (WHERE result IS NULL) AS "no result printed",
            count(*) FILTER (WHERE formulation IS NULL AND formulation_a IS NULL)
                AS "no motion printed"
        FROM votes WHERE has_individual_results
        GROUP BY 1 ORDER BY 2 DESC""",
    "Published results": """
        SELECT coalesce(result, '(none)') AS result, count(*) AS votes
        FROM votes WHERE has_individual_results
        GROUP BY 1 ORDER BY 2 DESC LIMIT 12""",
    "Individual choices": """
        SELECT choice, count(*) AS rows,
            round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS "%",
            count(*) FILTER (WHERE fraction_abbrev IS NULL) AS "no fraction then"
        FROM mp_votes GROUP BY 1 ORDER BY 2 DESC""",
    "Record currency": "SELECT key, value FROM dataset_meta ORDER BY key",
}


def markdown_table(columns: list[str], rows: list[tuple]) -> str:
    def cell(value) -> str:
        return "" if value is None else str(value).replace("|", "\\|")

    out = ["| " + " | ".join(columns) + " |", "|" + " --- |" * len(columns)]
    out += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(out) + "\n"


def report(args: argparse.Namespace) -> None:
    parts = [
        "# Phase 1 reconciliation report\n",
        f"Generated {datetime.now(VILNIUS):%Y-%m-%d %H:%M} by "
        "`uv run python -m ingest.seimas report` from Postgres and the offline "
        "verification in `data/seimas_verify.json`. Regenerate; do not edit.\n",
    ]
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        for title, sql in REPORT_QUERIES.items():
            cur.execute(sql)
            columns = [d.name for d in cur.description]
            parts.append(f"## {title}\n\n" + markdown_table(columns, cur.fetchall()))
    if VERIFY.exists():
        v = json.loads(VERIFY.read_text("utf-8"))
        parts.append(
            "## Verification against the Seimas vote pages\n\n"
            f"Checked {v['checked_at']}. Every vote with individual results is "
            "compared row by row, by `asmens_id`, with the page the Seimas "
            "publishes for it.\n\n"
            + markdown_table(["check", "count"], sorted(v["counts"].items()))
        )
        patterns = v["fraction_disagreements_xml_to_page"]
        if patterns:
            parts.append(
                "\n### Fraction label, open-data XML -> vote page\n\n"
                + markdown_table(["pattern", "rows"], list(patterns.items()))
            )
    tallies_file = DATA_DIR / "seimas_tallies.json"
    if tallies_file.exists():
        t = json.loads(tallies_file.read_text("utf-8"))
        parts.append(
            "## Corroboration by the stenograms\n\n"
            f"Checked {t['checked_at']}. A vote is corroborated when its sitting's "
            "stenogram announces exactly its tally (voted, for, against, abstain). "
            "`uv run python -m ingest.tallies` regenerates this.\n\n"
            + markdown_table(["check", "count"], sorted(t["totals"].items()))
        )
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text("\n".join(parts), "utf-8")
    print(f"written {REPORT}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("harvest", help="warm the cache from the source (network)")
    p.add_argument(
        "--all-pages",
        action="store_true",
        help="also fetch vote pages for votes without individual results",
    )
    p.set_defaults(func=harvest)
    for name, func, text in (
        ("verify", verify, "compare the XML with the Seimas vote pages (offline)"),
        ("load", load, "load the cache into Postgres (offline)"),
        ("report", report, "write reports/phase1.md from Postgres"),
    ):
        sub.add_parser(name, help=text).set_defaults(func=func)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
