from uuid import uuid4

import psycopg
import pytest
from langchain.messages import AIMessage

from ingest.config import DATABASE_URL
from ingest.names import declined_forms
from rag import query


class Structured:
    """Stands in for a chat model asked for structured output: each call returns
    the next scripted reply, with the usage a real call reports."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def with_structured_output(self, schema, include_raw=False, **kwargs):
        return self

    def invoke(self, messages):
        self.prompts.append(messages)
        raw = AIMessage(
            content="",
            usage_metadata={"input_tokens": 40, "output_tokens": 8, "total_tokens": 48},
            response_metadata={"cost": 0.0002},
        )
        return {"raw": raw, "parsed": self.replies.pop(0), "parsing_error": None}


def test_a_plan_routes_the_question_and_splits_its_parts():
    parts = ["Ką Rima Baškienė sakė apie PVM?", " ", "Kaip ji balsavo dėl PVM?"]
    model = Structured(query.Plan(route="both", parts=parts))
    history = [{"role": "user", "content": "Labas"}]
    plan, usage = query.plan(model, "Ką ji sakė apie PVM ir kaip balsavo?", history)
    assert plan.route == "both"
    assert plan.parts == ["Ką Rima Baškienė sakė apie PVM?", "Kaip ji balsavo dėl PVM?"]
    assert usage == {
        "model_calls": 1,
        "input_tokens": 40,
        "output_tokens": 8,
        "cost_usd": 0.0002,
    }
    assert "Earlier in the conversation:\nuser: Labas" in model.prompts[0][1].content


def test_a_plan_that_does_not_parse_is_unsure_and_keeps_the_question_whole():
    plan, _ = query.plan(Structured(None), "Kas naujo Seime?")
    assert (plan.route, plan.parts) == ("unsure", ["Kas naujo Seime?"])


def test_rewrites_differ_from_the_query_and_from_each_other():
    model = Structured(
        query.Rewrites(
            queries=[
                "PVM lengvatos",
                "  pvm   LENGVATOS ",
                "",
                "pridėtinės vertės mokesčio lengvatos",
                "lengvatinis pridėtinės vertės mokesčio tarifas",
                "PVM lengvatos maisto produktams",
                "ketvirta",
            ]
        )
    )
    rewrites, usage = query.rewrite(model, "PVM lengvatos", ["stenogram"])
    assert rewrites == [
        "pridėtinės vertės mokesčio lengvatos",
        "lengvatinis pridėtinės vertės mokesčio tarifas",
        "PVM lengvatos maisto produktams",
    ]
    assert usage["model_calls"] == 1
    assert "sitting stenograms" in model.prompts[0][0].content
    assert "programmes" not in model.prompts[0][0].content


@pytest.fixture
def db():
    try:
        conn = psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("Postgres not reachable")
    schema = f"test_{uuid4().hex[:8]}"
    try:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"CREATE TABLE {schema}.mps (LIKE public.mps INCLUDING ALL)")
        conn.execute(f"SET search_path TO {schema}, public")
        for mp_id, first, last in ((1, "Rima", "Baškienė"), (2, "Aidas", "Gedvilas")):
            forms = sorted(declined_forms(first) | declined_forms(last))
            conn.execute(
                "INSERT INTO mps (id, term_id, first_name, last_name, name_variants, "
                "date_from) VALUES (%s, 10, %s, %s, %s, '2024-11-14')",
                (mp_id, first, last, forms),
            )
        yield conn
    finally:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.close()


def test_a_rewrite_that_brings_in_a_person_is_dropped(db):
    candidates = [
        "Baškienės pasisakymai apie ligonines",  # the query's own MP
        "Aido Gedvilo pasisakymai apie ligonines",  # another MP
        "ligoninių tinklo pertvarka",
    ]
    assert query.kept("Baškienė apie ligonines", candidates, db) == [
        "Baškienės pasisakymai apie ligonines",
        "ligoninių tinklo pertvarka",
    ]


def test_a_vote_subject_is_rewritten_in_the_words_of_vote_titles():
    replies = query.Rewrites(queries=["Pensijų kaupimo įstatymo", "pensijų pakopos"])
    model = Structured(replies)
    rewrites, usage = query.rewrite_titles(model, "pensijų pakopos")
    assert rewrites == ["Pensijų kaupimo įstatymo"]  # not the subject again
    assert "titles and motions of votes" in model.prompts[0][0].content
    assert usage["model_calls"] == 1


def test_a_rewrite_that_gives_an_mp_another_initial_is_dropped(db):
    candidates = [
        "M. Gedvilo pasisakymai apie ligonines",  # he is Aidas Gedvilas, A.
        "A. Gedvilo pasisakymai apie ligonines",
        "kreipimasis į Gedvilą dėl ligoninių",  # "į" is a word, not an initial
    ]
    assert query.kept("Gedvilas apie ligonines", candidates, db) == candidates[1:]
