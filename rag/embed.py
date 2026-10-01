"""Embed the corpus through OpenRouter, and embed questions for retrieval.

    uv run python -m rag.embed          # dry run: chunks to embed, tokens, cost
    uv run python -m rag.embed --run    # embed every chunk that has no vector

The model and dimension are EMBEDDING_MODEL and EMBEDDING_DIM. The dimension must
match the `chunks.embedding` column, which is checked before anything is sent,
and a key without a credit limit is refused. Vectors are written batch by batch,
so an interrupted run resumes where it stopped; `dataset_meta` records the model,
so vectors from two models never end up in one index.

A model that returns more dimensions than the column holds is truncated and
renormalised, which is only sound for models trained for it (text-embedding-3,
for one); `bench/embeddings.py` measures exactly this truncation.
"""

from __future__ import annotations

import argparse
import math

import psycopg
import requests
from openai import OpenAI

from ingest.config import (
    DATABASE_URL,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
)

BATCH = 64
CHARS_PER_TOKEN = 3.2  # rough for Lithuanian; the run reports real usage
TIMEOUT = 60  # seconds; a stalled request is retried rather than waited on
META_KEY = "embedding_model"
META_SQL = """
    INSERT INTO dataset_meta (key, value) VALUES (%s, %s)
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
"""
# Instruction-tuned embedding models expect questions to say what they are for.
QUERY_INSTRUCTIONS = {
    "qwen/": "Instruct: Given a question, retrieve passages that answer it\nQuery: ",
}


def fit(vector: list[float], dims: int) -> list[float]:
    """The vector cut to `dims` and renormalised, or unchanged if it fits."""
    if len(vector) < dims:
        raise RuntimeError(f"the model returned {len(vector)} dimensions, not {dims}")
    if len(vector) == dims:
        return vector
    head = vector[:dims]
    norm = math.sqrt(sum(x * x for x in head)) or 1.0
    return [x / norm for x in head]


def request_embeddings(
    texts: list[str], *, model: str, dims: int, key: str
) -> tuple[list[list[float]], int]:
    """Vectors for `texts` in order, and the prompt tokens billed. The OpenAI SDK
    retries timeouts, dropped connections and retryable statuses with backoff:
    one stalled request must not end a job of hundreds."""
    with OpenAI(
        api_key=key, base_url=OPENROUTER_BASE_URL, timeout=TIMEOUT, max_retries=4
    ) as client:
        response = client.embeddings.create(
            model=model, input=texts, encoding_format="float"
        )
    if not response.data:
        # OpenRouter can answer 200 with an error in the body instead of vectors.
        error = (response.model_extra or {}).get("error")
        raise RuntimeError(f"{model}: no embeddings returned: {error}")
    data = sorted(response.data, key=lambda item: item.index)
    if [item.index for item in data] != list(range(len(texts))):
        raise RuntimeError(f"{model}: invalid embedding indices for {len(texts)} texts")
    tokens = response.usage.prompt_tokens if response.usage else 0
    return [fit(item.embedding, dims) for item in data], int(tokens or 0)


def as_vector(values: list[float]) -> str:
    return "[" + ",".join(map(str, values)) + "]"


def column_dims(conn) -> int | None:
    row = conn.execute(
        "SELECT atttypmod FROM pg_attribute "
        "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
    ).fetchone()
    return row[0] if row and row[0] > 0 else None


def stored_model(conn) -> str | None:
    row = conn.execute(
        "SELECT value FROM dataset_meta WHERE key = %s", (META_KEY,)
    ).fetchone()
    return row[0] if row else None


def pending(conn) -> tuple[int, int]:
    """Chunks without a vector, and their total characters."""
    return conn.execute(
        "SELECT count(*), coalesce(sum(length(content)), 0) FROM chunks "
        "WHERE embedding IS NULL"
    ).fetchone()


def embed_corpus(
    conn,
    *,
    model: str,
    dims: int,
    key: str,
    batch: int = BATCH,
    embedder=request_embeddings,
    document_id: int | None = None,
    progress=None,
) -> dict:
    """Embed every chunk without a vector, committing batch by batch; with
    `document_id`, only that document's chunks. `progress(done)` is called after
    each batch with the number of chunks embedded so far."""
    column = column_dims(conn)
    if column != dims:
        raise SystemExit(
            f"chunks.embedding holds {column} dimensions but EMBEDDING_DIM is {dims}; "
            "migrate the column before embedding"
        )
    label = f"{model}@{dims}"
    previous = stored_model(conn)
    embedded = conn.execute(
        "SELECT 1 FROM chunks WHERE embedding IS NOT NULL LIMIT 1"
    ).fetchone()
    if previous not in (None, label) and embedded:
        raise SystemExit(
            f"chunks already carry {previous} vectors; clear them before "
            f"embedding with {label}"
        )
    conn.execute(META_SQL, (META_KEY, label))
    done = tokens = 0
    select = (
        "SELECT id, content FROM chunks WHERE embedding IS NULL "
        "AND (%s::bigint IS NULL OR document_id = %s) ORDER BY id LIMIT %s"
    )
    while rows := conn.execute(select, (document_id, document_id, batch)).fetchall():
        vectors, used = embedder(
            [content for _, content in rows], model=model, dims=dims, key=key
        )
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                "UPDATE chunks SET embedding = %s::vector WHERE id = %s",
                [
                    (as_vector(vector), chunk_id)
                    for (chunk_id, _), vector in zip(rows, vectors, strict=True)
                ],
            )
        done, tokens = done + len(rows), tokens + used
        if progress:
            progress(done)
        if done % (batch * 25) < batch:
            print(f"  {done:,} chunks, {tokens:,} tokens", flush=True)
    return {"embedded": done, "tokens": tokens, "model": label}


def set_dims(conn, dims: int) -> None:
    """Resize chunks.embedding to `dims` dimensions. Only while no chunk carries a
    vector: vectors of one size cannot be converted to another.

    There is deliberately no HNSW index. At this corpus's size an exact scan is
    fast, and it stays exact under the metadata filters every search applies,
    where a filtered HNSW scan can return fewer than the requested rows."""
    embedded = conn.execute(
        "SELECT 1 FROM chunks WHERE embedding IS NOT NULL LIMIT 1"
    ).fetchone()
    if embedded:
        raise SystemExit("chunks already carry vectors; clear them before resizing")
    conn.execute(f"ALTER TABLE chunks ALTER COLUMN embedding TYPE vector({int(dims)})")


def embed_queries(
    texts: list[str],
    *,
    model: str = EMBEDDING_MODEL,
    dims: int = EMBEDDING_DIM,
    key: str = OPENROUTER_API_KEY,
    embedder=request_embeddings,
) -> list[list[float]]:
    """Vectors for search queries, in order, from one request."""
    prefix = next((p for m, p in QUERY_INSTRUCTIONS.items() if model.startswith(m)), "")
    vectors, _ = embedder(
        [prefix + text for text in texts], model=model, dims=dims, key=key
    )
    return vectors


def embed_query(text: str, **kwargs) -> list[float]:
    [vector] = embed_queries([text], **kwargs)
    return vector


def key_limit(key: str) -> float | None:
    r = requests.get(
        f"{OPENROUTER_BASE_URL}/key",
        headers={"Authorization": f"Bearer {key}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("data", {}).get("limit")


def price_per_token(model: str) -> float | None:
    try:
        r = requests.get(f"{OPENROUTER_BASE_URL}/embeddings/models", timeout=30)
        r.raise_for_status()
        prices = {m["id"]: float(m["pricing"]["prompt"]) for m in r.json()["data"]}
    except (requests.RequestException, KeyError, ValueError):
        return None
    return prices.get(model)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--run", action="store_true", help="embed; otherwise estimate")
    ap.add_argument(
        "--allow-unlimited",
        action="store_true",
        help="accept a key without a credit limit",
    )
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--set-dims", type=int, help="resize the empty vector column")
    args = ap.parse_args(argv)
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        if args.set_dims:
            set_dims(conn, args.set_dims)
            print(f"chunks.embedding is now vector({args.set_dims})")
            return
        count, chars = pending(conn)
        tokens = round(chars / CHARS_PER_TOKEN)
        price = price_per_token(EMBEDDING_MODEL)
        cost = f"about ${tokens * price:.2f}" if price is not None else "price unknown"
        print(
            f"{count:,} chunks without a vector, ~{tokens:,} tokens with "
            f"{EMBEDDING_MODEL}@{EMBEDDING_DIM}: {cost}"
        )
        if not args.run:
            return
        if not OPENROUTER_API_KEY:
            raise SystemExit("OPENROUTER_API_KEY is not set")
        if key_limit(OPENROUTER_API_KEY) is None and not args.allow_unlimited:
            raise SystemExit("this key has no credit limit; set one on openrouter.ai")
        result = embed_corpus(
            conn,
            model=EMBEDDING_MODEL,
            dims=EMBEDDING_DIM,
            key=OPENROUTER_API_KEY,
            batch=args.batch,
        )
    print(f"embedded {result['embedded']:,} chunks, {result['tokens']:,} tokens")


if __name__ == "__main__":
    main()
