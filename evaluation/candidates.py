"""Seeded passages to write evaluation questions from.

    uv run python -m evaluation.candidates

A question about what was said, promised or found needs a person to read a
passage and ask something it answers. Which passages get read is not chosen by
hand: this prints a seeded random sample for each document type (speeches by
MPs, programme sections, findings), with the chunk id and source link to record
beside the question written from it in evaluation/manual.json.
"""

from __future__ import annotations

import psycopg

from ingest.config import DATABASE_URL

from .build import SEED

SAMPLES = {"stenogram": 5, "programme": 3, "ethics": 1, "vtek": 1}
PASSAGES = """
    SELECT c.id, d.title, c.doc_date, c.speaker_name, c.section,
           coalesce(c.source_url, d.source_url) AS source_url, c.content
    FROM chunks c JOIN documents d ON d.id = c.document_id
    WHERE c.doc_type = %(type)s
      AND (%(type)s <> 'stenogram'
           OR (c.speaker_mp_id IS NOT NULL AND length(c.content) >= 400))
    ORDER BY md5(c.id::text || %(seed)s::text)
    LIMIT %(n)s
"""


def main() -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        for doc_type, n in SAMPLES.items():
            rows = conn.execute(
                PASSAGES, {"type": doc_type, "seed": SEED, "n": n}
            ).fetchall()
            print(f"\n=== {doc_type}: {len(rows)} of {n}")
            for chunk_id, title, day, speaker, section, url, content in rows:
                print(f"\n--- chunk {chunk_id} · {day} · {speaker or '-'} · {title}")
                print(f"    section: {section or '-'}\n    source: {url}\n")
                print(content)


if __name__ == "__main__":
    main()
