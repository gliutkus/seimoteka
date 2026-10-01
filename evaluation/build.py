"""Build the evaluation set: questions whose answers are checked against the record.

    uv run python -m evaluation.build      # writes evaluation/questions.json

Questions about votes, agreement and cohesion are drawn from a seeded random
sample of MPs, fractions and votes, never picked by hand, so the set treats
everyone alike. Their expected answers are computed from the database, and each
vote question names the Seimas page that shows the answer. Questions a sample
cannot produce (refusals, injection attempts, facts outside the record) are
written by hand in evaluation/manual.json and merged in.

Regenerate after a reload and review the diff: a changed expectation means the
record changed.
"""

from __future__ import annotations

import json
import random
import re
from datetime import date
from pathlib import Path

import psycopg

from ingest.config import DATABASE_URL
from rag.tools import fraction_cohesion, rows_of, voting_agreement

SEED = 20260914
HERE = Path(__file__).parent
MANUAL = HERE / "manual.json"
OUTPUT = HERE / "questions.json"
YEAR = (date(2025, 1, 1), date(2025, 12, 31))
CHOICE_WORDS = {
    "uz": "už",
    "pries": "prieš",
    "susilaike": "susilaikė",
    "nebalsavo": "nebalsavo",
    "a": "už formuluotę A",
    "b": "už formuluotę B",
}

MEMBER_VOTES = """
    SELECT v.id, (v.voted_at AT TIME ZONE 'Europe/Vilnius')::date AS day,
           coalesce(v.question, ai.title) AS subject, v.formulation_a,
           v.formulation_b, v.result, mv.choice, v.page_url
    FROM votes v
    JOIN agenda_items ai ON ai.id = v.agenda_item_id
    JOIN mp_votes mv ON mv.vote_id = v.id AND mv.mp_id = %(mp)s
    WHERE v.kind = %(kind)s AND mv.choice = ANY(%(choices)s)
      AND (%(stage)s::text IS NULL OR ai.stage = %(stage)s)
      AND v.result IS NOT NULL AND v.question IS NOT NULL
      -- the question names member, day and agenda question: it must mean one vote
      AND (SELECT count(*) FROM votes other
           JOIN mp_votes om ON om.vote_id = other.id AND om.mp_id = %(mp)s
           WHERE other.question = v.question
             AND (other.voted_at AT TIME ZONE 'Europe/Vilnius')::date
               = (v.voted_at AT TIME ZONE 'Europe/Vilnius')::date) = 1
    ORDER BY v.id
"""
UNSTATED_RESULTS = """
    SELECT id, (voted_at AT TIME ZONE 'Europe/Vilnius')::date AS day,
           to_char(voted_at AT TIME ZONE 'Europe/Vilnius', 'HH24:MI') AS at, question,
           total_for, total_against, total_abstain, page_url
    FROM votes
    WHERE has_individual_results AND kind = 'standard' AND result IS NULL
      AND question IS NOT NULL
    ORDER BY id
"""


def subject(text: str) -> str:
    """The agenda question without its trailing stage ("; priėmimas")."""
    return re.sub(r"\s*;\s*[^;]*$", "", text).strip()


def member_vote(conn, rng, pool: list, **params) -> tuple[tuple, dict]:
    """The next member in the shuffled pool with a matching vote, and one of them."""
    while pool:
        mp_id, name = pool.pop()
        rows = rows_of(conn.execute(MEMBER_VOTES, {"mp": mp_id, **params}))
        if rows:
            return (mp_id, name), rng.choice(rows)
    raise SystemExit(f"no member in the sample has a vote matching {params}")


def vote_items(conn, rng, pool: list) -> list[dict]:
    items = []
    # two of each cast choice, so reading abstention and "against" is tested too
    kinds = [("uz",), ("uz",), ("pries",), ("pries",), ("susilaike",)] * 1 + [
        ("susilaike",),
        ("nebalsavo",),
    ]
    for n, choices in enumerate(kinds, 1):
        (_, name), vote = member_vote(
            conn, rng, pool, kind="standard", stage="Priėmimas", choices=list(choices)
        )
        check = "names the member's choice and the published result; cites the vote"
        if choices == ("nebalsavo",):
            check += "; says the member cast no vote and never that they were absent"
        items.append(
            {
                "id": f"record-{n}",
                "category": "voting_record",
                "question": (
                    f"Kaip {name} balsavo {vote['day']} dėl klausimo "
                    f"„{subject(vote['subject'])}“?"
                ),
                "expected": {
                    "mp": name,
                    "vote_id": vote["id"],
                    "choice": CHOICE_WORDS[vote["choice"]],
                    "result": vote["result"],
                },
                "sources": [vote["page_url"]],
                "check": check,
            }
        )
    (_, name), vote = member_vote(
        conn, rng, pool, kind="alternative", stage=None, choices=["a", "b"]
    )
    motion = vote["formulation_a"] if vote["choice"] == "a" else vote["formulation_b"]
    items.append(
        {
            "id": "record-alternative",
            "category": "voting_record",
            "question": (
                f"Kaip {name} balsavo {vote['day']} dėl klausimo "
                f"„{subject(vote['subject'])}“?"
            ),
            "expected": {
                "mp": name,
                "vote_id": vote["id"],
                "choice": CHOICE_WORDS[vote["choice"]],
                "motion": motion,
                "result": vote["result"],
            },
            "sources": [vote["page_url"]],
            "check": (
                "says which motion the member supported and what it was; never "
                "calls the choice a vote for or against"
            ),
        }
    )
    return items


def agreement_items(conn, pool: list) -> list[dict]:
    # Up to the last vote loaded: new votes would change the figures.
    until = conn.execute(
        "SELECT max((voted_at AT TIME ZONE 'Europe/Vilnius')::date) FROM votes"
    ).fetchone()[0]
    items = []
    for n in range(1, 4):
        (a_id, a_name), (b_id, b_name) = pool.pop(), pool.pop()
        figures = voting_agreement(conn, a_id, b_id, until=until)
        items.append(
            {
                "id": f"agreement-{n}",
                "category": "agreement",
                "question": (
                    f"Kaip dažnai {a_name} ir {b_name} balsavo vienodai iki "
                    f"{until:%Y-%m-%d} imtinai?"
                ),
                "expected": {
                    key: figures[key]
                    for key in ("agreement", "both_voted", "same_choice")
                },
                "sources": [],
                "check": (
                    "gives the share of votes where both voted and chose the same, "
                    "how many votes it rests on, and the definition"
                ),
            }
        )
    return items


def cohesion_items(conn, rng) -> list[dict]:
    fractions = conn.execute(
        "SELECT abbrev, name FROM fractions WHERE abbrev IS NOT NULL ORDER BY abbrev"
    ).fetchall()
    items = []
    for abbrev, name in rng.sample(fractions, 3):
        figures = fraction_cohesion(conn, abbrev, since=YEAR[0], until=YEAR[1])
        items.append(
            {
                "id": f"cohesion-{abbrev}",
                "category": "cohesion",
                "question": f"Kaip vieningai 2025 metais balsavo {name}?",
                "expected": {
                    key: figures[key]
                    for key in (
                        "cohesion",
                        "participation",
                        "votes_with_two_or_more_voting",
                    )
                },
                "sources": [],
                "check": "gives cohesion and participation with their definitions",
            }
        )
    return items


def ambiguity_and_result_items(conn, rng) -> list[dict]:
    shared = conn.execute(
        "SELECT last_name, array_agg(full_name ORDER BY full_name) FROM mps "
        "GROUP BY last_name HAVING count(*) > 1 ORDER BY last_name"
    ).fetchall()
    surname, names = rng.choice(shared)
    unstated = rng.choice(rows_of(conn.execute(UNSTATED_RESULTS)))
    return [
        {
            "id": "ambiguous-name",
            "category": "clarification",
            "question": f"Kaip {surname} balsavo dėl 2026 metų valstybės biudžeto?",
            "expected": {"asks_which": names},
            "sources": [],
            "check": "asks which member is meant, naming both; states no vote",
        },
        {
            "id": "unstated-result",
            "category": "vote",
            "question": (
                f"Ar {unstated['day']} {unstated['at']} balsavime buvo pritarta "
                f"klausimui „{subject(unstated['question'])}“?"
            ),
            "expected": {
                "vote_id": unstated["id"],
                "result": None,
                "tally": [
                    unstated["total_for"],
                    unstated["total_against"],
                    unstated["total_abstain"],
                ],
            },
            "sources": [unstated["page_url"]],
            "check": (
                "reports the tally and that the vote page states no result; does "
                "not infer whether the motion passed"
            ),
        },
    ]


def build(conn) -> dict:
    rng = random.Random(SEED)
    pool = conn.execute(
        "SELECT id, full_name FROM mps WHERE date_to IS NULL ORDER BY id"
    ).fetchall()
    rng.shuffle(pool)
    items = [
        *vote_items(conn, rng, pool),
        *agreement_items(conn, pool),
        *cohesion_items(conn, rng),
        *ambiguity_and_result_items(conn, rng),
        *json.loads(MANUAL.read_text("utf-8")),
    ]
    record = dict(conn.execute("SELECT key, value FROM dataset_meta").fetchall())
    return {
        "seed": SEED,
        "record": {key: record.get(key) for key in ("loaded_at", "votes_last_id")},
        "items": items,
    }


def main() -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        result = build(conn)
    OUTPUT.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", "utf-8"
    )
    print(f"{len(result['items'])} questions written to {OUTPUT}")


if __name__ == "__main__":
    main()
