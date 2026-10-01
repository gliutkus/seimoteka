"""Corroborate the vote record with the stenograms' spoken announcements.

    uv run python -m ingest.tallies    # offline; writes data/seimas_tallies.json

After an electronic vote the chair reads the result into the record: "Balsavo 127
Seimo nariai: 90 – už, 14 – prieš, 23 susilaikė." The stenogram is a separate
primary document from the vote database, so a vote whose tally is announced
verbatim in its own sitting's stenogram is corroborated by a second source.
Registrations are announced too ("Užsiregistravo 128 Seimo nariai"), which lets
the count of quorum registrations be checked against the votes that carry no
individual results.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime

import psycopg

from .config import DATA_DIR, DATABASE_URL

OUTPUT = DATA_DIR / "seimas_tallies.json"
DASH = r"\s*[–—-]\s*"
MAYBE_DASH = r"\s*(?:[–—-]\s*)?"  # "23 susilaikė" is printed without one
MEMBERS = r"(?:\s+Seimo\s+nar\w+)?"  # "Balsavo 102:" and "14 Seimo narių – už"
WORDS = r"(?:\s+\w+){0,2}?"  # "už pertrauką – 86"
# Chairs differ: "Balsavo 127 Seimo nariai: 90 – už ..." and "Balsavo 102: už – 71 ...".
# A match counts only if its parts add up to the number who voted.
ANNOUNCED = re.compile(
    rf"\b[Bb]alsavo\s+(?P<voted>\d+){MEMBERS}\s*:?(?P<rest>[^.]{{0,200}})"
)
# "Užsiregistravo 83 Seimo nariai, balsavo 82: ..." reports a vote, not a quorum
# registration, so a registration count followed by "balsavo" is not counted.
REGISTERED = re.compile(
    r"\bUžsiregistravo\s+(?P<count>\d+)\s+Seimo\s+nar\w*\b(?!\s*,?\s*balsavo)"
)
ABSTAIN = r"su(?:si)?laik\w*"  # the record also spells it "sulaikė"
# Stated outright that no one voted against or abstained.
UNANIMOUS = re.compile(
    r"\bvisi\s*(?:[–—-]\s*)?už\b|\bprieš\s+ir\s+susilaikiusių\s+(?:nebuvo|nėra)",
    re.I,
)
PARTS = {
    "for": re.compile(
        rf"(?:(?P<n>\d+){MEMBERS}{DASH}už\b|\bUž{WORDS}{MAYBE_DASH}(?P<m>\d+))", re.I
    ),
    "against": re.compile(
        rf"(?:(?P<n>\d+){MEMBERS}{DASH}prieš\b|\bprieš{WORDS}{MAYBE_DASH}(?P<m>\d+))",
        re.I,
    ),
    "abstain": re.compile(
        rf"(?:(?P<n>\d+){MAYBE_DASH}{ABSTAIN}|\b{ABSTAIN}{MAYBE_DASH}(?P<m>\d+))",
        re.I,
    ),
}


def parse_announcement(voted: int, rest: str) -> tuple[int, int, int, int] | None:
    """(voted, for, against, abstain) from the words after "Balsavo N Seimo nariai",
    or None when the parts cannot be read or do not add up."""
    found = {}
    for part, pattern in PARTS.items():
        match = pattern.search(rest)
        if match:
            found[part] = int(match["n"] or match["m"])
    if not found and UNANIMOUS.search(rest):
        return voted, voted, 0, 0  # "visi – už", "prieš ir susilaikiusių nebuvo"
    missing = [p for p in PARTS if p not in found]
    # "prieš ir susilaikiusių nėra": whatever is not stated is zero, if the rest adds up
    if missing and sum(found.values()) == voted:
        found.update(dict.fromkeys(missing, 0))
    if len(found) < 3 or sum(found.values()) != voted:
        return None
    return voted, found["for"], found["against"], found["abstain"]


def announcements(text: str) -> tuple[list[tuple[int, int, int, int]], int, int]:
    """Tallies announced in `text`, how many announcements could not be read, and
    how many registrations were announced."""
    tallies, unreadable = [], 0
    for match in ANNOUNCED.finditer(text):
        tally = parse_announcement(int(match["voted"]), match["rest"])
        if tally:
            tallies.append(tally)
        else:
            unreadable += 1
    return tallies, unreadable, len(REGISTERED.findall(text))


def match_tallies(votes: list[tuple[int, tuple]], announced: list[tuple]) -> dict:
    """Pair each vote with an announcement of the same tally, each used once."""
    pool = Counter(announced)
    corroborated, unannounced = [], []
    for vote_id, tally in votes:
        if pool[tally] > 0:
            pool[tally] -= 1
            corroborated.append(vote_id)
        else:
            unannounced.append(vote_id)
    return {
        "corroborated": corroborated,
        "unannounced": unannounced,
        "announced_without_vote": sum(pool.values()),
    }


def main() -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        text_by_sitting: dict[int, list[str]] = defaultdict(list)
        for sitting_id, content in conn.execute(
            "SELECT sitting_id, content FROM chunks "
            "WHERE doc_type = 'stenogram' ORDER BY sitting_id, ordinal"
        ):
            text_by_sitting[sitting_id].append(content)
        votes_by_sitting: dict[int, list] = defaultdict(list)
        registrations: Counter = Counter()
        for (
            sitting_id,
            vote_id,
            has_results,
            voted,
            for_,
            against,
            abstain,
        ) in conn.execute(
            "SELECT sitting_id, id, has_individual_results, total_voted, total_for, "
            "total_against, total_abstain FROM votes "
            "WHERE sitting_id = ANY(%s) ORDER BY id DESC",
            (list(text_by_sitting),),
        ):
            if has_results:
                votes_by_sitting[sitting_id].append(
                    (vote_id, (voted, for_, against, abstain))
                )
            else:
                registrations[sitting_id] += 1

    totals: Counter = Counter()
    sittings = {}
    for sitting_id, texts in text_by_sitting.items():
        tallies, unreadable, registered = announcements("\n".join(texts))
        result = match_tallies(votes_by_sitting[sitting_id], tallies)
        totals["sittings"] += 1
        totals["votes with results"] += len(votes_by_sitting[sitting_id])
        totals["corroborated by the stenogram"] += len(result["corroborated"])
        totals["not found in the stenogram"] += len(result["unannounced"])
        totals["announcements with no matching vote"] += result[
            "announced_without_vote"
        ]
        totals["announcements that could not be read"] += unreadable
        totals["registrations announced"] += registered
        totals["votes without individual results"] += registrations[sitting_id]
        sittings[sitting_id] = {
            **result,
            "unreadable": unreadable,
            "registered": registered,
            "zero_tally_votes": registrations[sitting_id],
        }
    report = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "totals": dict(totals),
        "sittings": {str(k): v for k, v in sittings.items()},
    }
    OUTPUT.write_text(json.dumps(report, indent=2), "utf-8")
    for key, value in totals.items():
        print(f"  {value:>7,}  {key}")
    print(f"written {OUTPUT}")


if __name__ == "__main__":
    main()
