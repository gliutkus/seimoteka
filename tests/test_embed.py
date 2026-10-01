import json
from uuid import uuid4

import httpx2
import openai._base_client
import psycopg
import pytest
from openai import APIStatusError, APITimeoutError

from ingest.config import DATABASE_URL
from rag import embed


@pytest.fixture
def db():
    try:
        conn = psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("Postgres not reachable")
    schema = f"test_{uuid4().hex[:8]}"
    try:
        conn.execute(f"CREATE SCHEMA {schema}")
        for table in ("documents", "chunks", "dataset_meta"):
            conn.execute(
                f"CREATE TABLE {schema}.{table} (LIKE public.{table} INCLUDING ALL)"
            )
        conn.execute(f"SET search_path TO {schema}, public")
        conn.execute(
            "INSERT INTO documents (id, doc_type, title, source_url) "
            "VALUES (1, 'stenogram', 'Posėdis', 'https://example.test/1')"
        )
        for i in range(1, 6):
            add_chunk(conn, i)
        yield conn
    finally:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.close()


def add_chunk(conn, i: int) -> None:
    conn.execute(
        "INSERT INTO chunks (id, document_id, ordinal, content, doc_type) "
        "VALUES (%s, 1, %s, %s, 'stenogram')",
        (i, i, f"Kalba numeris {i}"),
    )


class FakeEmbeddings:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, texts, *, model, dims, key):
        self.calls.append(list(texts))
        return [[1.0] + [0.0] * (dims - 1) for _ in texts], 10 * len(texts)


def test_the_column_dimension_is_read_from_the_schema(db):
    declared = db.execute(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
    ).fetchone()[0]
    assert declared == f"vector({embed.column_dims(db)})"


def test_every_chunk_is_embedded_in_batches_and_the_model_recorded(db):
    fake = FakeEmbeddings()
    result = embed.embed_corpus(
        db, model="m", dims=embed.column_dims(db), key="k", batch=2, embedder=fake
    )
    assert result == {
        "embedded": 5,
        "tokens": 50,
        "model": f"m@{embed.column_dims(db)}",
    }
    assert [len(call) for call in fake.calls] == [2, 2, 1]
    assert embed.pending(db)[0] == 0
    assert embed.stored_model(db) == f"m@{embed.column_dims(db)}"
    rerun = embed.embed_corpus(
        db, model="m", dims=embed.column_dims(db), key="k", embedder=fake
    )
    assert rerun["embedded"] == 0


def test_vectors_from_two_models_are_never_mixed(db):
    embed.embed_corpus(
        db, model="m", dims=embed.column_dims(db), key="k", embedder=FakeEmbeddings()
    )
    add_chunk(db, 6)
    with pytest.raises(SystemExit, match="already carry"):
        embed.embed_corpus(
            db,
            model="other",
            dims=embed.column_dims(db),
            key="k",
            embedder=FakeEmbeddings(),
        )


def test_a_dimension_the_column_cannot_hold_is_refused(db):
    fake = FakeEmbeddings()
    with pytest.raises(SystemExit, match=f"holds {embed.column_dims(db)} dimensions"):
        embed.embed_corpus(
            db, model="m", dims=embed.column_dims(db) + 16, key="k", embedder=fake
        )
    assert fake.calls == []


def test_longer_vectors_are_truncated_and_renormalised():
    assert embed.fit([3.0, 4.0, 12.0], 2) == pytest.approx([0.6, 0.8])
    assert embed.fit([0.5, 0.5], 2) == [0.5, 0.5]
    with pytest.raises(RuntimeError):
        embed.fit([1.0], 2)


def test_instruction_tuned_models_get_a_query_instruction():
    fake = FakeEmbeddings()
    embed.embed_query(
        "mokesčiai", model="qwen/qwen3-embedding-8b", dims=4, embedder=fake
    )
    embed.embed_query(
        "mokesčiai", model="openai/text-embedding-3-large", dims=4, embedder=fake
    )
    assert fake.calls[0][0].endswith("Query: mokesčiai")
    assert fake.calls[1] == ["mokesčiai"]


def test_the_vector_column_is_resized_only_while_empty(db):
    embed.set_dims(db, 8)
    assert embed.column_dims(db) == 8
    embed.embed_corpus(db, model="m", dims=8, key="k", embedder=FakeEmbeddings())
    with pytest.raises(SystemExit, match="already carry vectors"):
        embed.set_dims(db, 16)


def fake_openrouter(monkeypatch, handler):
    """Requests go to `handler` through the SDK's own HTTP client, without waiting
    between retries."""
    original = embed.OpenAI
    monkeypatch.setattr(openai._base_client.time, "sleep", lambda seconds: None)
    transport = httpx2.MockTransport(handler)
    monkeypatch.setattr(
        embed,
        "OpenAI",
        lambda **kw: original(**kw, http_client=httpx2.Client(transport=transport)),
    )


def vectors_reply():
    return {
        "object": "list",
        "model": "m",
        "data": [
            {"object": "embedding", "index": 1, "embedding": [0.0, 1.0]},
            {"object": "embedding", "index": 0, "embedding": [1.0, 0.0]},
        ],
        "usage": {"prompt_tokens": 7, "total_tokens": 7},
    }


@pytest.mark.parametrize("failure", [408, 429, 500, 503, "stalled", "dropped"])
def test_a_failed_request_is_retried_and_the_vectors_keep_the_input_order(
    monkeypatch, failure
):
    calls = []

    def handler(request):
        calls.append(request)
        assert json.loads(request.content)["encoding_format"] == "float"
        assert request.extensions["timeout"]["read"] == embed.TIMEOUT
        if len(calls) == 1:
            if failure == "stalled":
                raise httpx2.ReadTimeout("stalled", request=request)
            if failure == "dropped":
                raise httpx2.RemoteProtocolError("dropped", request=request)
            return httpx2.Response(failure, json={"error": {"message": "retry"}})
        return httpx2.Response(200, json=vectors_reply())

    fake_openrouter(monkeypatch, handler)
    assert embed.request_embeddings(["a", "b"], model="m", dims=2, key="k") == (
        [[1.0, 0.0], [0.0, 1.0]],
        7,
    )
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("reply", "error", "attempts"),
    [
        (401, APIStatusError, 1),  # a refused key: retrying cannot help
        (503, APIStatusError, 5),
        ("stalled", APITimeoutError, 5),
    ],
)
def test_retries_end_and_a_refused_key_is_not_retried(
    monkeypatch, reply, error, attempts
):
    calls = []

    def handler(request):
        calls.append(request)
        if reply == "stalled":
            raise httpx2.ReadTimeout("stalled", request=request)
        return httpx2.Response(reply, json={"error": {"message": "failed"}})

    fake_openrouter(monkeypatch, handler)
    with pytest.raises(error):
        embed.request_embeddings(["a"], model="m", dims=2, key="k")
    assert len(calls) == attempts


def test_an_error_in_a_successful_response_is_reported(monkeypatch):
    body = {"error": {"message": "No successful provider responses", "code": 502}}
    fake_openrouter(monkeypatch, lambda request: httpx2.Response(200, json=body))
    with pytest.raises(RuntimeError, match="No successful provider responses"):
        embed.request_embeddings(["a"], model="m", dims=2, key="k")


def test_every_text_gets_exactly_one_vector(monkeypatch):
    body = vectors_reply()
    body["data"][0]["index"] = 0  # two vectors for the first text, none for the second
    fake_openrouter(monkeypatch, lambda request: httpx2.Response(200, json=body))
    with pytest.raises(RuntimeError, match="invalid embedding indices"):
        embed.request_embeddings(["a", "b"], model="m", dims=2, key="k")
