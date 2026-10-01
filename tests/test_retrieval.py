from datetime import date
from uuid import uuid4

import psycopg
import pytest

from ingest.config import DATABASE_URL
from rag import embed, query, retrieval


@pytest.fixture
def db():
    try:
        conn = psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("Postgres not reachable")
    schema = f"test_{uuid4().hex[:8]}"
    try:
        conn.execute(f"CREATE SCHEMA {schema}")
        tables = ("documents", "document_subjects", "chunks")
        for table in (*tables, "lexeme_stats", "corpus_stats"):
            conn.execute(
                f"CREATE TABLE {schema}.{table} (LIKE public.{table} INCLUDING ALL)"
            )
        conn.execute(f"SET search_path TO {schema}, public")
        yield conn
    finally:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.close()


def axis(i: int, dims: int) -> list[float]:
    vector = [0.0] * dims
    vector[i] = 1.0
    return vector


# id, document, text, date, speaker. Chunk i is embedded on axis i - 1. "ir" is in
# every chunk, "apie" and "su" in three of eight: all too common to search on.
CHUNKS = [
    (1, 1, "Siūlome mažinti pridėtinės vertės mokestį maistui ir vaistams.", 7),
    (2, 1, "Kalbėjome apie gynybos finansavimą ir kariuomenę.", 8),
    (3, 2, "Mokesčių lengvatos šeimoms su vaikais ir senjorams.", None),
    (4, 1, "Pritariu kolegai ir dėkoju už klausimą apie švietimą.", 8),
    (5, 1, "Diskusija apie sveikatos apsaugą ir ligonines.", 9),
    (6, 1, "Kultūros ministerija ir teatrai su savivaldybėmis.", 9),
    (7, 1, "Energetikos projektai ir elektros kainos.", 7),
    (8, 2, "Žemės ūkis ir kaimo plėtra su ūkininkais.", None),
]
DAYS = {1: date(2025, 3, 20), 2: date(2024, 9, 6)}


def seed(conn):
    for doc_id, doc_type in ((1, "stenogram"), (2, "programme")):
        conn.execute(
            "INSERT INTO documents (id, doc_type, title, source_url) "
            "VALUES (%s, %s, 'Dokumentas', %s)",
            (doc_id, doc_type, f"https://example.test/{doc_id}"),
        )
    for chunk_id, doc_id, text, speaker in CHUNKS:
        conn.execute(
            "INSERT INTO chunks (id, document_id, ordinal, content, doc_type, "
            "doc_date, speaker_mp_id, embedding) "
            "SELECT %s, id, %s, %s, doc_type, %s, %s, %s::vector "
            "FROM documents WHERE id = %s",
            (
                chunk_id,
                chunk_id,
                text,
                DAYS[doc_id],
                speaker,
                str(axis(chunk_id - 1, embed.column_dims(conn))),
                doc_id,
            ),
        )
    assert retrieval.refresh_stats(conn) == len(CHUNKS)


def ids(hits: list[dict]) -> list[int]:
    return [hit["chunk_id"] for hit in hits]


def test_statistics_count_chunks_per_lexeme(db):
    seed(db)
    ndoc = dict(db.execute("SELECT word, ndoc FROM lexeme_stats").fetchall())
    assert (ndoc["ir"], ndoc["mokest"], ndoc["gynyb"]) == (8, 2, 1)


def test_lexical_side_stems_and_needs_only_some_words(db):
    seed(db)
    hits = retrieval.search(db, "kodėl mokesčių niekas nemažina")
    assert sorted(ids(hits)) == [1, 3]
    assert all(hit["dense_rank"] is None for hit in hits)
    assert hits[0]["source_url"].startswith("https://example.test/")


def test_words_too_common_to_discriminate_are_not_searched(db):
    seed(db)
    assert retrieval.search(db, "ir apie su") == []


def test_ranks_are_fused(db):
    seed(db)
    hits = retrieval.search(db, "gynybos", vector=axis(0, embed.column_dims(db)))
    # chunk 2 is the only lexical match and second by vector; chunk 1 is nearest
    assert ids(hits)[:3] == [2, 1, 3]
    assert (hits[0]["lexical_rank"], hits[0]["dense_rank"]) == (1, 2)
    assert hits[0]["score"] == pytest.approx(1 / 61 + 1 / 62, abs=1e-5)


def test_filters_apply_to_both_sides(db):
    seed(db)
    hits = retrieval.search(
        db, "mokesčių", vector=axis(2, embed.column_dims(db)), types=["stenogram"]
    )
    assert 3 not in ids(hits)
    assert ids(retrieval.search(db, "mokesčių", since=date(2025, 1, 1))) == [1]
    assert ids(retrieval.search(db, "mokesčių", speaker=7)) == [1]


def test_a_subject_filter_finds_every_document_about_that_member(db):
    seed(db)
    # document 1 is about members 7 and 9 jointly; document 2 about member 9 alone
    db.execute(
        "INSERT INTO document_subjects (document_id, mp_id) "
        "VALUES (1, 7), (1, 9), (2, 9)"
    )
    assert ids(retrieval.search(db, "mokesčių", subject=7)) == [1]
    assert sorted(ids(retrieval.search(db, "mokesčių", subject=9))) == [1, 3]
    assert retrieval.search(db, "mokesčių", subject=8) == []


def test_a_party_filter_keeps_only_that_partys_programme(db):
    seed(db)
    # chunks 3 and 8 are programme passages; chunk 1, a speech, also matches "mokesčių"
    db.execute("UPDATE chunks SET section = 'Vardenių partija' WHERE id = 3")
    db.execute("UPDATE chunks SET section = 'Pavardenių partija' WHERE id = 8")
    assert ids(retrieval.search(db, "mokesčių", party="Vardenių partija")) == [3]
    assert retrieval.search(db, "mokesčių", party="Pavardenių partija") == []
    assert sorted(ids(retrieval.search(db, "mokesčių"))) == [1, 3]


def test_phrasings_are_fused_and_a_chunk_several_of_them_find_rises(db):
    seed(db)
    hits = query.fused_search(db, ["mokesčių", "vaistams maistui"], k=5)
    assert ids(hits) == [1, 3]  # chunk 1 is found by both phrasings
    assert (hits[0]["found_by"], hits[1]["found_by"]) == ([0, 1], [0])
    # a chunk only a reformulation finds counts, without the query's own ranks
    hits = query.fused_search(db, ["gynybos", "vaistams"], k=5)
    assert ids(hits) == [1, 2]  # equal fused scores: the lower id first
    assert (hits[0]["found_by"], hits[0]["lexical_rank"]) == ([1], None)
    assert (hits[1]["found_by"], hits[1]["lexical_rank"]) == ([0], 1)


def test_search_refuses_to_run_without_statistics(db):
    with pytest.raises(RuntimeError, match="statistics"):
        retrieval.search(db, "mokesčiai")


def test_a_fixed_speaker_and_day_return_their_passages_when_no_word_matches(db):
    seed(db)
    day = DAYS[1]
    hits = retrieval.search(db, "zuikiai ir vilkai", speaker=8, since=day, until=day)
    assert ids(hits) == [2, 4]
    assert all(h["lexical_rank"] is None for h in hits)
    assert retrieval.search(db, "zuikiai ir vilkai", speaker=8) == []  # no span
