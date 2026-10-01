"""Retrieval check: does search put the passage a question was written from on top?

    uv run python -m evaluation.retrieval     # writes reports/retrieval.md

Each question written from a passage records the filters a correct tool call
would pass (`retrieval`). The question is searched with those filters, and the
passage counts as found at the first hit from the question's source whose text
contains it; matching on source and text rather than chunk id survives reloads.

The questions were written after reading their passages, so they share its
words, which favours BM25. Each also has a paraphrase that avoids those words,
which is where vector search, and rewriting the query into the record's words,
should help; both are reported. BM25 always runs; dense and hybrid join once the
chunks carry vectors. The `+ rewrites` modes need a key: the chat model rewrites
each question as the agent's searches are rewritten (rag.query.rewrite), every
phrasing is searched, and the rankings are fused.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import psycopg

from ingest.config import (
    DATABASE_URL,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    OPENROUTER_API_KEY,
    ROOT,
)
from ingest.names import normal
from rag import agent as bot
from rag import embed
from rag import query as translation

QUESTIONS = Path(__file__).with_name("questions.json")
REPORT = ROOT / "reports" / "retrieval.md"
K = 10
VARIANTS = {"question": "as written", "paraphrase": "paraphrased"}


def found_at(item: dict, hits: list[dict]) -> int | None:
    """Rank of the first hit from the item's source containing its passage."""
    passage = normal(item["passage"])
    for rank, hit in enumerate(hits, 1):
        if hit["source_url"] in item["sources"] and passage in normal(hit["content"]):
            return rank
    return None


def filters(conn, spec: dict) -> dict:
    one = bot.one_mp
    return {
        "types": spec.get("types") or bot.SEARCHED_BY_DEFAULT,
        "speaker": one(conn, spec["speaker"])[0] if "speaker" in spec else None,
        "subject": one(conn, spec["subject"])[0] if "subject" in spec else None,
        "since": date.fromisoformat(spec["since"]) if "since" in spec else None,
        "until": date.fromisoformat(spec["until"]) if "until" in spec else None,
    }


def modes(conn) -> list[str]:
    """BM25 always; dense and hybrid once the chunks carry vectors from the
    configured model and questions can be embedded; rewriting with a key."""
    configured = f"{EMBEDDING_MODEL}@{EMBEDDING_DIM}"
    vectors = (
        bool(OPENROUTER_API_KEY)
        and embed.stored_model(conn) == configured
        and conn.execute(
            "SELECT 1 FROM chunks WHERE embedding IS NOT NULL LIMIT 1"
        ).fetchone()
        is not None
    )
    available = ["bm25", "dense", "hybrid"] if vectors else ["bm25"]
    if OPENROUTER_API_KEY:
        available.append("bm25 + rewrites")
    if vectors:
        available.append("hybrid + rewrites")
    return available


def run(
    conn, items: list[dict], mode: str, variant: str, rewriter, rewritten: dict
) -> dict[str, int | None]:
    ranks = {}
    base, _, rewrites = mode.partition(" + ")
    for item in items:
        text = item[variant]
        params = filters(conn, item["retrieval"])
        phrasings = [text]
        if rewrites:
            if text not in rewritten:
                rewritten[text], _ = translation.rewrite(
                    rewriter, text, params["types"], conn
                )
            phrasings += rewritten[text]
        vectors = None if base == "bm25" else bot.query_vectors(conn, phrasings)
        queries = [""] * len(phrasings) if base == "dense" else phrasings
        hits = translation.fused_search(conn, queries, vectors, k=K, **params)
        ranks[item["id"]] = found_at(item, hits)
    return ranks


def summary(ranks: dict[str, int | None]) -> dict[str, float]:
    n = len(ranks)
    return {
        "hit@1": sum(1 for r in ranks.values() if r == 1) / n,
        "hit@5": sum(1 for r in ranks.values() if r and r <= 5) / n,
        "mrr": sum(1 / r for r in ranks.values() if r) / n,
    }


def write_report(
    items: list[dict], results: dict[tuple[str, str], dict], rewritten: dict
) -> None:
    lines = [
        "# Retrieval check",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} by `uv run python -m "
        "evaluation.retrieval`. Regenerate; do not edit.",
        "",
        f"{len(items)} questions written from a passage, each searched with the "
        f"filters a correct tool call would pass; top {K} hits. The questions as "
        "written share their passage's words; the paraphrases avoid them. In the "
        "`+ rewrites` modes the chat model also rewrites each question into the "
        "record's words, and the rankings of every phrasing are fused.",
        "",
        "| questions | mode | hit@1 | hit@5 | MRR |",
        "| --- | --- | --- | --- | --- |",
    ]
    for (variant, mode), ranks in results.items():
        s = summary(ranks)
        lines.append(
            f"| {VARIANTS[variant]} | {mode} | {s['hit@1']:.2f} | {s['hit@5']:.2f} "
            f"| {s['mrr']:.2f} |"
        )
    columns = list(results)
    header = " | ".join(f"{VARIANTS[v]} {m}" for v, m in columns)
    lines += ["", f"| question | {header} |", "| --- |" + " --- |" * len(columns)]
    for item in items:
        cells = " | ".join(str(results[c][item["id"]] or "-") for c in columns)
        lines.append(f"| {item['id']} | {cells} |")
    if rewritten:
        lines += ["", "## Rewrites", "", "| searched | rewritten as |", "| --- | --- |"]
        for text, rewrites in rewritten.items():
            lines.append(f"| {text} | {' · '.join(rewrites) or '(none kept)'} |")
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", "utf-8")


def main() -> None:
    data = json.loads(QUESTIONS.read_text("utf-8"))
    items = [item for item in data["items"] if "retrieval" in item]
    variants = [v for v in VARIANTS if all(v in item for item in items)]
    rewriter = bot.build_model() if OPENROUTER_API_KEY else None
    rewritten: dict[str, list[str]] = {}
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        available = modes(conn)
        results = {
            (variant, mode): run(conn, items, mode, variant, rewriter, rewritten)
            for variant in variants
            for mode in available
        }
    write_report(items, results, rewritten)
    for (variant, mode), ranks in results.items():
        print(VARIANTS[variant], mode, summary(ranks))
    print(f"written {REPORT}")


if __name__ == "__main__":
    main()
