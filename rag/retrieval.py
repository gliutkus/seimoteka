"""Hybrid retrieval over the corpus: BM25 and vector similarity, fused.

    uv run python -m rag.retrieval stats      # after every corpus load
    uv run python -m rag.retrieval search "pridėtinės vertės mokesčio lengvata"

One SQL query. The lexical side scores chunks with BM25 over `chunks.tsv`
(Lithuanian stemming), using the document frequencies `stats` stores in
`lexeme_stats`. The dense side ranks chunks by cosine distance to a query
embedding, when one is given and chunks carry embeddings. Both sides apply the
same metadata filters, and their ranks are fused by reciprocal rank fusion:
score = sum of 1 / (60 + rank). RRF uses ranks only, so the two sides' scores
need no calibration against each other.

A chunk matches if it contains any of the query's lexemes, weighted by rarity: a
question is not a conjunction of its words. The Lithuanian configuration has no
stop-word list, so lexemes present in more than a quarter of all chunks (ir,
apie, su ...) are dropped from the query; they barely move BM25 and would make
nearly every chunk a candidate.

People never go through full-text search: the stemmer folds the surname Balsys
and the verb balsuoti together. Resolve them with rag.people and filter on
`speaker` (who said it) or `subject` (whom a finding is about).
"""

from __future__ import annotations

import argparse
import json
from datetime import date

import psycopg

from ingest.config import DATABASE_URL

from .tools import resolve, rows_of

RRF_K = 60
DEPTH = 50  # candidates each side contributes to the fusion
MAX_DF = 0.25  # lexemes in a larger share of chunks are dropped from the query
K1, B = 1.2, 0.75
BROWSE_DAYS = 7  # BM25 term-frequency saturation and length normalisation

FILTERS = """
      (%(types)s::text[] IS NULL OR c.doc_type = ANY(%(types)s))
  AND (%(since)s::date IS NULL OR c.doc_date >= %(since)s)
  AND (%(until)s::date IS NULL OR c.doc_date <= %(until)s)
  AND (%(speaker)s::bigint IS NULL OR c.speaker_mp_id = %(speaker)s)
  AND (%(subject)s::bigint IS NULL OR EXISTS (
          SELECT 1 FROM document_subjects s
          WHERE s.document_id = d.id AND s.mp_id = %(subject)s))
  AND (%(sitting)s::bigint IS NULL OR c.sitting_id = %(sitting)s)
  AND (%(party)s::text IS NULL OR (c.doc_type = 'programme' AND c.section = %(party)s))
  AND (%(bill)s::text IS NULL OR c.bill_number = %(bill)s
       OR (strpos(%(bill)s, '(') = 0 AND c.bill_number LIKE %(bill)s || '(%%'))
"""

# Lexemes are quoted for tsquery input, doubling embedded quotes and backslashes.
SEARCH_SQL = rf"""
WITH totals AS (
    SELECT chunks::float AS n, avg_lexemes AS avglen FROM corpus_stats
), terms AS (
    SELECT s.word, ln(1 + (totals.n - s.ndoc + 0.5) / (s.ndoc + 0.5)) AS idf
    FROM totals,
         unnest(tsvector_to_array(to_tsvector('lithuanian', %(text)s))) AS w(word)
         JOIN lexeme_stats s ON s.word = w.word
    WHERE s.ndoc <= %(max_df)s * totals.n
), query AS (
    SELECT string_agg(
               '''' || replace(replace(word, '\', '\\'), '''', '''''') || '''',
               ' | '
           )::tsquery AS q
    FROM terms
), lexical AS (
    SELECT id, row_number() OVER (ORDER BY score DESC, id) AS rank
    FROM (
        SELECT c.id,
               sum(t.idf * u.tf * ({K1} + 1) / (u.tf + {K1}
                   * (1 - {B} + {B} * length(c.tsv) / totals.avglen))) AS score
        FROM query, totals, chunks c
        JOIN documents d ON d.id = c.document_id
        CROSS JOIN LATERAL (
            SELECT lexeme, array_length(positions, 1) AS tf FROM unnest(c.tsv)
        ) u
        JOIN terms t ON t.word = u.lexeme
        WHERE c.tsv @@ query.q AND {FILTERS}
        GROUP BY c.id
        ORDER BY score DESC, c.id
        LIMIT %(depth)s
    ) best
), dense AS (
    SELECT id, row_number() OVER (ORDER BY distance, id) AS rank
    FROM (
        SELECT c.id, c.embedding <=> %(vector)s::vector AS distance
        FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE %(vector)s::vector IS NOT NULL AND c.embedding IS NOT NULL
          AND {FILTERS}
        ORDER BY distance, c.id
        LIMIT %(depth)s
    ) nearest
), fused AS (
    SELECT id, l.rank AS lexical_rank, v.rank AS dense_rank,
           coalesce(1.0 / (%(rrf_k)s + l.rank), 0)
         + coalesce(1.0 / (%(rrf_k)s + v.rank), 0) AS score
    FROM lexical l FULL JOIN dense v USING (id)
)
SELECT c.id AS chunk_id, d.doc_type, d.title, c.doc_date,
       coalesce(c.source_url, d.source_url) AS source_url,
       c.speaker_name, c.fraction, c.section, c.section_at, c.bill_number,
       c.content, f.score, f.lexical_rank, f.dense_rank
FROM fused f
JOIN chunks c ON c.id = f.id
JOIN documents d ON d.id = c.document_id
ORDER BY f.score DESC, c.id
LIMIT %(k)s
"""

# A question's words need not appear in the speech it asks about. When nothing
# matches but the speaker and a span of at most a week are fixed, that member's
# passages in the span come back in document order, unranked. Without the span
# they would be arbitrary, so the search then stays empty.
BROWSE_SQL = f"""
SELECT c.id AS chunk_id, d.doc_type, d.title, c.doc_date,
       coalesce(c.source_url, d.source_url) AS source_url,
       c.speaker_name, c.fraction, c.section, c.section_at, c.bill_number,
       c.content, 0 AS score, NULL::bigint AS lexical_rank,
       NULL::bigint AS dense_rank
FROM chunks c JOIN documents d ON d.id = c.document_id
WHERE {FILTERS}
ORDER BY c.doc_date, c.document_id, c.ordinal
LIMIT %(k)s
"""

# DELETE, not TRUNCATE: TRUNCATE would lock lexeme_stats until the refresh commits,
# and every search in the meantime would wait on it.
STATS_SQL = (
    "DELETE FROM lexeme_stats",
    "INSERT INTO lexeme_stats (word, ndoc) "
    "SELECT word, ndoc FROM ts_stat('SELECT tsv FROM chunks')",
    "INSERT INTO corpus_stats (id, chunks, avg_lexemes) "
    "SELECT true, count(*), coalesce(avg(length(tsv)), 0) FROM chunks "
    "ON CONFLICT (id) DO UPDATE SET chunks = EXCLUDED.chunks, "
    "avg_lexemes = EXCLUDED.avg_lexemes, refreshed_at = now()",
)


def refresh_stats(conn) -> int:
    """Recompute the BM25 document frequencies over all chunks; returns their count."""
    with conn.transaction():
        for sql in STATS_SQL:
            conn.execute(sql)
    return conn.execute("SELECT chunks FROM corpus_stats").fetchone()[0]


def search(
    conn,
    text: str,
    *,
    vector: list[float] | None = None,
    types: list[str] | None = None,
    since: date | None = None,
    until: date | None = None,
    speaker: int | None = None,
    subject: int | None = None,
    sitting: int | None = None,
    bill: str | None = None,
    party: str | None = None,
    k: int = 8,
    depth: int = DEPTH,
    max_df: float = MAX_DF,
) -> list[dict]:
    """The k best chunks for `text`, each with its source URL and both side ranks.

    Without `vector` only the lexical side runs. A rank of None means the chunk
    was not among that side's `depth` best candidates.
    """
    if conn.execute("SELECT 1 FROM corpus_stats").fetchone() is None:
        raise RuntimeError(
            "no corpus statistics; run `uv run python -m rag.retrieval stats`"
        )
    params = {
        "text": text,
        "vector": None if vector is None else "[" + ",".join(map(str, vector)) + "]",
        "types": types,
        "since": since,
        "until": until,
        "speaker": speaker,
        "subject": subject,
        "sitting": sitting,
        "bill": bill,
        "party": party,
        "k": k,
        "depth": depth,
        "max_df": max_df,
        "rrf_k": RRF_K,
    }
    hits = rows_of(conn.execute(SEARCH_SQL, params))
    narrow = since and until and (until - since).days <= BROWSE_DAYS
    if not hits and speaker is not None and narrow:
        hits = rows_of(conn.execute(BROWSE_SQL, params))
    return [{**row, "score": round(float(row["score"]), 5)} for row in hits]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats", help="recompute BM25 statistics after a corpus load")
    p = sub.add_parser("search", help="print the best chunks for a query")
    p.add_argument("text")
    p.add_argument("--type", action="append", dest="types", help="repeatable")
    p.add_argument("--since", type=date.fromisoformat)
    p.add_argument("--until", type=date.fromisoformat)
    p.add_argument("--speaker", help="the MP who spoke, by name")
    p.add_argument("--k", type=int, default=8)
    args = ap.parse_args(argv)
    with psycopg.connect(DATABASE_URL) as conn:
        if args.cmd == "stats":
            print(f"statistics over {refresh_stats(conn):,} chunks")
            return
        speaker = resolve(conn, args.speaker) if args.speaker else None
        hits = search(
            conn,
            args.text,
            types=args.types,
            since=args.since,
            until=args.until,
            speaker=speaker,
            k=args.k,
        )
    for hit in hits:
        hit["content"] = hit["content"][:300]
    print(json.dumps(hits, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
