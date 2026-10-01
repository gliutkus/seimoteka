"""Deterministic, SQL-backed tools over the vote record.

    uv run python -m rag.tools record Žemaitaitis --topic "pridėtinės vertės"
    uv run python -m rag.tools agreement Kasčiūnas Žemaitaitis
    uv run python -m rag.tools cohesion NAF --since 2025-01-01

Every tool works the same way for every MP and fraction, and every figure
states its definition. Only votes with individual results count: quorum
registrations carry none. "nebalsavo" means the member did not vote; the record
does not say whether they were present, so no tool treats it as absence. In an
alternative vote the choices are 'a' and 'b', for motions A and B.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from decimal import Decimal

import psycopg

from ingest.config import DATABASE_URL

from .people import find_mps

LOCAL_DAY = "(v.voted_at AT TIME ZONE 'Europe/Vilnius')::date"
DATES = f"""
  AND (%(since)s::date IS NULL OR {LOCAL_DAY} >= %(since)s)
  AND (%(until)s::date IS NULL OR {LOCAL_DAY} <= %(until)s)
"""

RECORD_SQL = f"""
SELECT v.id AS vote_id,
       to_char(v.voted_at AT TIME ZONE 'Europe/Vilnius', 'YYYY-MM-DD HH24:MI')
           AS voted_at,
       coalesce(v.question, al.title, v.formulation) AS subject,
       v.formulation, v.kind, v.formulation_a, v.formulation_b,
       v.bill_number, ai.stage, v.result,
       mv.choice, mv.fraction_abbrev AS fraction_then, v.page_url AS source
FROM mp_votes mv
JOIN votes v ON v.id = mv.vote_id
LEFT JOIN agenda_items ai ON ai.id = v.agenda_item_id
LEFT JOIN agenda_lines al ON al.id = v.agenda_line_id
WHERE mv.mp_id = %(mp)s {DATES}
  AND (%(bill)s::text IS NULL OR v.bill_number = %(bill)s)
  AND (%(topic)s::text IS NULL OR to_tsvector('lithuanian',
         concat_ws(' ', v.question, v.formulation, al.title, ai.title))
       @@ websearch_to_tsquery('lithuanian', %(topic)s))
ORDER BY v.voted_at DESC
LIMIT %(limit)s
"""

AGREEMENT_SQL = f"""
WITH pairs AS (
    SELECT a.vote_id, a.choice AS a_choice, b.choice AS b_choice
    FROM mp_votes a
    JOIN mp_votes b ON b.vote_id = a.vote_id AND b.mp_id = %(b)s
    JOIN votes v ON v.id = a.vote_id
    WHERE a.mp_id = %(a)s {DATES}
)
SELECT count(*) AS votes_listing_both,
       count(*) FILTER (WHERE a_choice <> 'nebalsavo' AND b_choice <> 'nebalsavo')
           AS both_voted,
       count(*) FILTER (WHERE a_choice <> 'nebalsavo' AND a_choice = b_choice)
           AS same_choice,
       count(*) FILTER (WHERE a_choice <> 'nebalsavo' AND b_choice <> 'nebalsavo'
                          AND a_choice <> b_choice) AS different_choice,
       count(*) FILTER (WHERE (a_choice = 'nebalsavo') <> (b_choice = 'nebalsavo'))
           AS only_one_voted
FROM pairs
"""

COHESION_SQL = f"""
WITH rows AS (
    SELECT mv.vote_id, mv.choice
    FROM mp_votes mv JOIN votes v ON v.id = mv.vote_id
    WHERE mv.fraction_abbrev = %(fraction)s {DATES}
), per_vote AS (
    SELECT vote_id,
           count(*) AS listed,
           count(*) FILTER (WHERE choice <> 'nebalsavo') AS voted,
           greatest(count(*) FILTER (WHERE choice = 'uz'),
                    count(*) FILTER (WHERE choice = 'pries'),
                    count(*) FILTER (WHERE choice = 'susilaike'),
                    count(*) FILTER (WHERE choice = 'a'),
                    count(*) FILTER (WHERE choice = 'b')) AS majority
    FROM rows GROUP BY vote_id
)
SELECT count(*) AS votes,
       count(*) FILTER (WHERE voted >= 2) AS votes_with_two_or_more_voting,
       round(avg(majority::numeric / voted) FILTER (WHERE voted >= 2), 3)
           AS cohesion,
       count(*) FILTER (WHERE voted >= 2 AND majority = voted) AS unanimous,
       round(sum(voted)::numeric / nullif(sum(listed), 0), 3) AS participation
FROM per_vote
"""


def rows_of(cursor) -> list[dict]:
    names = [d.name for d in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor]


def plain(value):
    return float(value) if isinstance(value, Decimal) else value


def voting_record(
    conn,
    mp_id: int,
    *,
    topic: str | None = None,
    bill: str | None = None,
    since: date | None = None,
    until: date | None = None,
    limit: int = 20,
) -> list[dict]:
    """An MP's recorded choices, newest first, each with the vote's source page."""
    params = {
        "mp": mp_id,
        "topic": topic,
        "bill": bill,
        "since": since,
        "until": until,
        "limit": limit,
    }
    return rows_of(conn.execute(RECORD_SQL, params))


def voting_agreement(
    conn, mp_a: int, mp_b: int, *, since: date | None = None, until: date | None = None
) -> dict:
    """How often two MPs cast the same choice, over votes where both voted."""
    params = {"a": mp_a, "b": mp_b, "since": since, "until": until}
    result = rows_of(conn.execute(AGREEMENT_SQL, params))[0]
    both = result["both_voted"]
    result["agreement"] = round(result["same_choice"] / both, 3) if both else None
    result["method"] = (
        "Share of votes, among those where both MPs cast a choice (už, prieš, "
        "susilaikė, or motion A or B in an alternative vote), in which they cast the "
        "same one. Votes where either did not vote are counted separately and "
        "excluded from the share. Votes listing both are all the votes whose roll "
        "lists both MPs, whether or not either of them voted."
    )
    return result


def fraction_cohesion(
    conn, fraction: str, *, since: date | None = None, until: date | None = None
) -> dict:
    """How united a fraction's voting members were, by the label at each vote."""
    params = {"fraction": fraction, "since": since, "until": until}
    row = rows_of(conn.execute(COHESION_SQL, params))[0]
    result = {key: plain(value) for key, value in row.items()}
    result["fraction"] = fraction
    result["method"] = (
        "For each vote, the share of the fraction's members who voted and cast the "
        "fraction's most common choice, averaged over votes where at least two of "
        "its members voted. Membership is the fraction printed on the Seimas vote "
        "page at the time of the vote; a member the page lists under two fractions, "
        "on the day they switched, is counted in neither. Participation is the "
        "share of listed members who voted."
    )
    return result


FIND_VOTES_SQL = f"""
SELECT v.id AS vote_id,
       to_char(v.voted_at AT TIME ZONE 'Europe/Vilnius', 'YYYY-MM-DD HH24:MI')
           AS voted_at,
       coalesce(v.question, al.title, v.formulation) AS subject,
       v.formulation, v.kind, v.formulation_a, v.formulation_b,
       v.bill_number, ai.stage, v.result, v.total_for, v.total_against,
       v.total_abstain, v.total_voted, v.total_members, v.page_url AS source
FROM votes v
LEFT JOIN agenda_items ai ON ai.id = v.agenda_item_id
LEFT JOIN agenda_lines al ON al.id = v.agenda_line_id
WHERE v.has_individual_results {DATES}
  AND (%(bill)s::text IS NULL OR v.bill_number = %(bill)s)
  AND (%(topic)s::text IS NULL OR to_tsvector('lithuanian',
         concat_ws(' ', v.question, v.formulation, al.title, ai.title))
       @@ websearch_to_tsquery('lithuanian', %(topic)s))
ORDER BY v.voted_at DESC, v.id
LIMIT %(limit)s
"""


def find_votes(
    conn,
    *,
    topic: str | None = None,
    bill: str | None = None,
    since: date | None = None,
    until: date | None = None,
    limit: int = 20,
) -> list[dict]:
    """Votes with individual results by bill, topic or dates, newest first, each
    with its tally, published result and vote page."""
    params = {
        "topic": topic,
        "bill": bill,
        "since": since,
        "until": until,
        "limit": limit,
    }
    return rows_of(conn.execute(FIND_VOTES_SQL, params))


PARTY_VOTES_SQL = f"""
SELECT v.id AS vote_id,
       to_char(v.voted_at AT TIME ZONE 'Europe/Vilnius', 'YYYY-MM-DD HH24:MI')
           AS voted_at,
       coalesce(v.question, al.title, v.formulation) AS subject,
       v.formulation, v.kind, v.formulation_a, v.formulation_b,
       v.bill_number, ai.stage, v.result,
       count(*) AS listed,
       count(*) FILTER (WHERE mv.choice = 'uz') AS uz,
       count(*) FILTER (WHERE mv.choice = 'pries') AS pries,
       count(*) FILTER (WHERE mv.choice = 'susilaike') AS susilaike,
       count(*) FILTER (WHERE mv.choice = 'a') AS a,
       count(*) FILTER (WHERE mv.choice = 'b') AS b,
       count(*) FILTER (WHERE mv.choice = 'nebalsavo') AS nebalsavo,
       v.page_url AS source
FROM votes v
JOIN mp_votes mv ON mv.vote_id = v.id
JOIN mps m ON m.id = mv.mp_id
LEFT JOIN agenda_items ai ON ai.id = v.agenda_item_id
LEFT JOIN agenda_lines al ON al.id = v.agenda_line_id
WHERE m.nominated_by = %(party)s AND v.has_individual_results {DATES}
  AND to_tsvector('lithuanian',
         concat_ws(' ', v.question, v.formulation, al.title, ai.title))
      @@ websearch_to_tsquery('lithuanian', %(topic)s)
GROUP BY v.id, al.id, ai.id
ORDER BY v.voted_at DESC, v.id
LIMIT %(limit)s
"""

PARTY_VOTES_METHOD = (
    "For each vote with individual results whose question or motion matches the "
    "subject: how many of the MPs the party nominated in the 2024 election the roll "
    "lists, and what each of them cast, whichever fraction they sat in at the time. "
    "It describes those MPs' votes, not a party position, and it does not say "
    "whether any promise was kept."
)


def programme_parties(conn) -> list[str]:
    """The parties whose 2024 election programme is loaded, by list name."""
    return [
        party
        for (party,) in conn.execute(
            "SELECT DISTINCT section FROM chunks "
            "WHERE doc_type = 'programme' AND section IS NOT NULL ORDER BY 1"
        )
    ]


def party_votes(
    conn,
    party: str,
    *,
    topic: str,
    since: date | None = None,
    until: date | None = None,
    limit: int = 15,
) -> dict:
    """How the MPs a party nominated voted on a subject: the newest `limit` votes
    one by one, how many votes were found, and figures over all of them."""
    params = {
        "party": party,
        "topic": topic,
        "since": since,
        "until": until,
        "limit": None,  # every vote, so the figures count them all
    }
    votes = rows_of(conn.execute(PARTY_VOTES_SQL, params))
    nominated = conn.execute(
        "SELECT count(*) FROM mps WHERE nominated_by = %s", (party,)
    ).fetchone()[0]
    return {
        "party": party,
        "mps_nominated": nominated,
        "found": len(votes),
        "votes": votes[:limit],
        "summary": vote_summary(votes),
        "method": PARTY_VOTES_METHOD,
    }


CAST = ("uz", "pries", "susilaike", "a", "b")
SUMMARY_DEFINITION = (
    "Counted over every vote found, not only the votes listed. most_cast: for each "
    "choice, the number of votes in which it was the choice most of the party's "
    "MPs who voted cast; tied, the votes in which two choices were cast equally "
    "often. most_cast_no_vote: the votes in which more of the party's listed MPs "
    "cast no vote than voted; each of these is also counted in most_cast, by the "
    "choice of those who did vote. adoption_votes: the final adoption votes."
)


def adopts(vote: dict) -> bool:
    """Whether a vote is a final adoption vote: its motion reads "dėl šio įstatymo
    priėmimo" (or nutarimo, kodekso ...)."""
    motion = vote["formulation"] or ""
    return motion.startswith("dėl šio ") and motion.endswith(" priėmimo")


def adoption_first(votes: list[dict], n: int) -> list[dict]:
    """At most n of the votes: final adoption votes first, then the rest, each
    group in the order given."""
    return ([v for v in votes if adopts(v)] + [v for v in votes if not adopts(v)])[:n]


def vote_summary(votes: list[dict]) -> dict:
    """Figures over every party vote found (party_votes rows), so that a statement
    about all of them rests on all of them rather than on the few an answer cites."""
    most_cast = dict.fromkeys((*CAST, "tied"), 0)
    no_vote = 0
    for vote in votes:
        (second, _), (first, choice) = sorted((vote[c], c) for c in CAST)[-2:]
        if first:
            most_cast[choice if first > second else "tied"] += 1
        no_vote += vote["nebalsavo"] > vote["listed"] - vote["nebalsavo"]
    return {
        "votes": len(votes),
        "adoption_votes": sum(1 for vote in votes if adopts(vote)),
        "most_cast": most_cast,
        "most_cast_no_vote": no_vote,
        "definition": SUMMARY_DEFINITION,
    }


def resolve(conn, name: str) -> int:
    candidates = find_mps(conn, name)
    if len(candidates) != 1:
        options = ", ".join(c.full_name for c in candidates) or "none"
        raise SystemExit(f"'{name}' does not identify one MP (candidates: {options})")
    return candidates[0].id


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("tool", choices=["record", "agreement", "cohesion"])
    ap.add_argument("subjects", nargs="+", help="MP names or a fraction abbreviation")
    ap.add_argument("--topic")
    ap.add_argument("--bill")
    ap.add_argument("--since", type=date.fromisoformat)
    ap.add_argument("--until", type=date.fromisoformat)
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args(argv)
    span = {"since": args.since, "until": args.until}
    with psycopg.connect(DATABASE_URL) as conn:
        if args.tool == "record":
            mp_id = resolve(conn, args.subjects[0])
            out = voting_record(
                conn, mp_id, topic=args.topic, bill=args.bill, limit=args.limit, **span
            )
        elif args.tool == "agreement":
            a, b = (resolve(conn, name) for name in args.subjects[:2])
            out = voting_agreement(conn, a, b, **span)
        else:
            out = fraction_cohesion(conn, args.subjects[0], **span)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
