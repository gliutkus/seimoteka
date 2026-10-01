from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest

from ingest.config import DATABASE_URL
from ingest.names import normal
from rag import tools
from rag.people import rank_mps

PEOPLE = [
    (1, "Remigijus", "Žemaitaitis", "Remigijus Žemaitaitis"),
    (2, "Aidas", "Gedvilas", "Aidas Gedvilas"),
    (3, "Martynas", "Gedvilas", "Martynas Gedvilas"),
    (4, "Viktorija", "Čmilytė-Nielsen", "Viktorija Čmilytė-Nielsen"),
]


def names(text: str) -> list[str]:
    return [c.full_name for c in rank_mps(normal(text).split(), PEOPLE)]


def test_mps_are_found_in_any_case_and_ambiguity_is_kept():
    assert names("kaip balsavo Žemaitaičio frakcija") == ["Remigijus Žemaitaitis"]
    assert names("zemaitaitis") == ["Remigijus Žemaitaitis"]
    assert names("Gedvilas") == ["Aidas Gedvilas", "Martynas Gedvilas"]
    assert names("Martyno Gedvilo balsai") == ["Martynas Gedvilas"]
    assert names("Čmilytės-Nielsen") == ["Viktorija Čmilytė-Nielsen"]
    assert names("kaip balsavo seimas") == []


@pytest.fixture
def db():
    try:
        conn = psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("Postgres not reachable")
    schema = f"test_{uuid4().hex[:8]}"
    try:
        conn.execute(f"CREATE SCHEMA {schema}")
        for table in ("mps", "votes", "mp_votes", "agenda_items", "agenda_lines"):
            conn.execute(
                f"CREATE TABLE {schema}.{table} "
                f"(LIKE public.{table} INCLUDING DEFAULTS INCLUDING GENERATED)"
            )
        conn.execute(f"SET search_path TO {schema}, public")
        yield conn
    finally:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.close()


VOTE_SQL = (
    "INSERT INTO votes (id, term_id, voted_at, question, total_voted, total_for, "
    "total_against, total_abstain, total_members, source_url, page_url) "
    "VALUES (%s, 10, %s, 'Klausimas', %s, %s, %s, %s, 141, 'x', 'y')"
)
CHOICE_SQL = (
    "INSERT INTO mp_votes (vote_id, mp_id, choice, fraction_abbrev) "
    "VALUES (%s, %s, %s, 'X')"
)


def seed(conn):
    at = datetime(2025, 9, 10, 10, 0, tzinfo=UTC)
    for vote_id, tally in ((-1, (3, 2, 1, 0)), (-2, (2, 2, 0, 0)), (-3, (1, 1, 0, 0))):
        conn.execute(VOTE_SQL, (vote_id, at, *tally))
    choices = {  # members A, B, C of fraction X
        -1: ("uz", "uz", "pries"),
        -2: ("uz", "nebalsavo", "uz"),
        -3: ("uz", "nebalsavo", "nebalsavo"),
    }
    for vote_id, row in choices.items():
        for mp_id, choice in enumerate(row, 1):
            conn.execute(CHOICE_SQL, (vote_id, mp_id, choice))


def test_agreement_counts_only_votes_where_both_voted(db):
    seed(db)
    result = tools.voting_agreement(db, 1, 3)
    assert result["both_voted"] == 2
    assert result["same_choice"] == 1
    assert result["only_one_voted"] == 1
    assert result["agreement"] == 0.5


def test_cohesion_and_participation(db):
    seed(db)
    result = tools.fraction_cohesion(db, "X")
    # vote -1: 2 of 3 cast the majority choice; vote -2: 2 of 2; vote -3 has a
    # single voter and is left out of the average
    assert result["votes_with_two_or_more_voting"] == 2
    assert result["cohesion"] == pytest.approx((2 / 3 + 1) / 2, abs=0.001)
    assert result["unanimous"] == 1
    assert result["participation"] == pytest.approx(6 / 9, abs=0.001)


def test_record_lists_every_choice_including_not_voting(db):
    seed(db)
    assert [r["choice"] for r in tools.voting_record(db, 2)] == [
        "uz",
        "nebalsavo",
        "nebalsavo",
    ]


def test_alternative_votes_count_motions_a_and_b_as_choices(db):
    db.execute(VOTE_SQL, (-4, datetime(2025, 9, 11, 10, 0, tzinfo=UTC), 3, 2, 1, 0))
    db.execute(
        "UPDATE votes SET kind = 'alternative', formulation_a = 'grąžinti', "
        "formulation_b = 'atmesti' WHERE id = -4"
    )
    for mp_id, choice in enumerate(("a", "a", "b"), 1):
        db.execute(CHOICE_SQL, (-4, mp_id, choice))
    assert tools.fraction_cohesion(db, "X")["cohesion"] == pytest.approx(
        2 / 3, abs=0.001
    )
    assert tools.voting_agreement(db, 1, 2)["agreement"] == 1.0
    assert tools.voting_agreement(db, 1, 3)["agreement"] == 0.0
    [row] = tools.voting_record(db, 3)
    assert (row["choice"], row["kind"], row["formulation_b"]) == (
        "b",
        "alternative",
        "atmesti",
    )


def test_votes_are_found_without_a_member(db):
    seed(db)
    found = tools.find_votes(db, topic="klausimas")
    assert sorted(r["vote_id"] for r in found) == [-3, -2, -1]
    by_id = {r["vote_id"]: r for r in found}
    assert (by_id[-1]["total_for"], by_id[-1]["total_against"]) == (2, 1)
    assert tools.find_votes(db, bill="XVP-9") == []
