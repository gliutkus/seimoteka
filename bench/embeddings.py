"""Embedding model benchmark on real Lithuanian retrieval queries.

    uv run python -m bench.embeddings                 # dry run: nothing spent
    uv run python -m bench.embeddings --run           # embed and score every candidate
    uv run python -m bench.embeddings --run --models qwen/qwen3-embedding-8b

The corpus is built from local data only: the speaker turns of one cached
stenogram, every distinct term-10 agenda title, and ~45-second windows from a
fixed sample of trimmed YouTube transcripts. Queries and their relevance rules are in
bench/embedding_queries.json. The dry run prints what each rule selects so the
ground truth can be reviewed before anything is spent; its only network call is
OpenRouter's public price list.

Embeddings go through OpenRouter. --run refuses a key without a credit limit and
caches every vector under data/embed_bench/, so re-scoring costs nothing.
Results are written to reports/embeddings.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests
from lxml import html as LH

from ingest.config import DATA_DIR, OPENROUTER_API_KEY, OPENROUTER_BASE_URL, ROOT
from ingest.http import fetch
from ingest.names import normal
from ingest.seimas import read_record

BENCH = DATA_DIR / "embed_bench"
QUERIES = Path(__file__).with_name("embedding_queries.json")
REPORT = ROOT / "reports" / "embeddings.md"

STENOGRAM = "https://e-seimas.lrs.lt/rs/legalact/TAK/f5da17b08e6411f0b351ee31aa0a26b8/"
SPEAKER = re.compile(
    r"^((?:[A-ZĄČĘĖĮŠŲŪŽ]\.\s*)+[A-ZĄČĘĖĮŠŲŪŽ][A-ZĄČĘĖĮŠŲŪŽ\-]+(?:\s*\([^)]*\))?"
    r"|PIRMININK(?:AS|Ė)(?:\s*\([^)]*\))?)\s*\.\s*"
)
TIME_HEADING = re.compile(r"^\d{1,2}\.\d{2} val\.")
MAX_CHARS = 1500
ANCHOR_VIDEOS = [  # videos seen to discuss the YouTube query topics
    "--7cRChlPEQ",
    "-4jea7zFykU",
    "0ThWKEa2wpM",
    "-8863r7rqbI",
    "-Ds2n3WomPg",
    "-4sYUkw8pb8",
    "-g1bW_Tr8pE",
    "-70tdYdi9pQ",
    "81UqhF3CBeU",  # second-pillar pensions; -8863r7rqbI's passage was a copy
]
DISTRACTOR_VIDEOS = 40
WINDOW_SECONDS = 45
CHARS_PER_TOKEN = 3.2  # rough for Lithuanian; the run records real usage
# One pass over the MVP corpus, estimated 2026-09-13: stenograms ~25M chars
# (32,116 sitting minutes x ~740 chars/min measured on sitting -502067),
# trimmed transcripts 13.6M chars, titles and smaller documents ~1M; ~3.2 chars/token.
MVP_TOKENS = 12_400_000
SHORTER_DIMS = (3072, 2000, 1536, 1024, 768)
QWEN_QUERY = "Instruct: Given a question, retrieve passages that answer it\nQuery: "


@dataclass(frozen=True)
class Candidate:
    model: str
    query_prefix: str = ""
    doc_prefix: str = ""
    # Trained so that a renormalised prefix of the vector is itself an embedding.
    matryoshka: bool = False


CANDIDATES = [
    Candidate("openai/text-embedding-3-large", matryoshka=True),
    Candidate("openai/text-embedding-3-small", matryoshka=True),
    Candidate("google/gemini-embedding-001", matryoshka=True),
    Candidate("google/gemini-embedding-2"),
    Candidate("qwen/qwen3-embedding-8b", query_prefix=QWEN_QUERY, matryoshka=True),
    Candidate("qwen/qwen3-embedding-4b", query_prefix=QWEN_QUERY, matryoshka=True),
    Candidate("baai/bge-m3"),
    Candidate(
        "intfloat/multilingual-e5-large", query_prefix="query: ", doc_prefix="passage: "
    ),
    Candidate("voyageai/voyage-4-large"),
    Candidate("voyageai/voyage-4"),
    Candidate("mistralai/mistral-embed-2312"),
]


@dataclass(frozen=True)
class Doc:
    key: str
    source: str
    text: str


# ------------------------------------------------------------------ corpus


def split_long(text: str, limit: int = MAX_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, current = [], ""
    for sentence in re.split(r"(?<=[.!?…])\s+", text):
        if current and len(current) + len(sentence) + 1 > limit:
            out.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    return out + ([current] if current else [])


def stenogram_docs() -> list[Doc]:
    raw = fetch(STENOGRAM, suffix=".html", offline=True)
    if raw is None:
        raise SystemExit(f"stenogram not in the cache: {STENOGRAM}")
    turns: list[tuple[str, list[str]]] = []
    for p in LH.fromstring(raw).iter("p"):
        # text_content() per paragraph keeps drop caps joined to their word
        text = " ".join(p.text_content().replace("­", "").replace("\xa0", " ").split())
        if not text or TIME_HEADING.match(text):
            continue
        opener = SPEAKER.match(text)
        if opener:
            turns.append((opener.group(1), [text[opener.end() :]]))
        elif turns:
            turns[-1][1].append(text)
    docs = []
    for t, (speaker, paragraphs) in enumerate(turns):
        pieces, current = [], ""
        for paragraph in paragraphs:
            for piece in split_long(paragraph):
                if current and len(current) + len(piece) + 1 > MAX_CHARS:
                    pieces.append(current)
                    current = piece
                else:
                    current = f"{current} {piece}".strip()
        pieces += [current] if current else []
        docs += [
            Doc(f"steno:{t}:{i}", "steno", f"{speaker}. {x}")
            for i, x in enumerate(pieces)
        ]
    return docs


def agenda_docs() -> list[Doc]:
    first: dict[str, int] = {}
    for item in read_record().agenda:
        first[item["title"]] = min(first.get(item["title"], 0), item["id"])
    return [
        Doc(f"agenda:{i}", "agenda", t)
        for t, i in sorted(first.items(), key=lambda x: x[1])
    ]


def transcript_windows(transcript: dict) -> list[str]:
    out, buffer, start = [], [], None
    for seg in transcript.get("segments") or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = seg["start"] if start is None else start
        buffer.append(text)
        if seg["end"] - start >= WINDOW_SECONDS:
            out.append(" ".join(buffer))
            buffer, start = [], None
    return out + ([" ".join(buffer)] if buffer else [])


def youtube_docs() -> list[Doc]:
    """Windows from trimmed transcripts: the same text Phase 2 will chunk."""
    root = DATA_DIR / "youtube" / "trimmed"
    if not root.exists():
        raise SystemExit(
            "no trimmed transcripts; run `uv run python -m ingest.transcripts`"
        )
    trimmed = {
        p.stem: json.loads(p.read_text("utf-8")) for p in sorted(root.glob("*.json"))
    }
    usable = sorted(v for v, t in trimmed.items() if not t["dropped"] and t["segments"])
    missing = [v for v in ANCHOR_VIDEOS if v not in usable]
    if missing:
        print(f"warning: anchor videos without usable transcripts: {missing}")
    others = [v for v in usable if v not in ANCHOR_VIDEOS]
    chosen = [v for v in ANCHOR_VIDEOS if v in usable]
    chosen += random.Random(20260913).sample(others, DISTRACTOR_VIDEOS)
    return [
        Doc(f"yt:{video}:{i}", "youtube", text)
        for video in chosen
        for i, text in enumerate(transcript_windows(trimmed[video]))
    ]


def build_corpus() -> list[Doc]:
    # One empty text fails a whole batch at OpenAI-compatible endpoints.
    docs = agenda_docs() + stenogram_docs() + youtube_docs()
    return [doc for doc in docs if doc.text.strip()]


def relevant(query: dict, docs: list[Doc]) -> set[str]:
    """Documents of the query's source matching any clause of all-substrings."""
    keys = set()
    for doc in docs:
        if doc.source != query["source"]:
            continue
        folded = normal(doc.text)
        if any(
            all(s in folded for s in clause) for clause in query["match"]
        ) and not any(s in folded for s in query.get("exclude", [])):
            keys.add(doc.key)
    return keys


# ----------------------------------------------------------------- scoring


def normalise(vectors: np.ndarray) -> np.ndarray:
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def score(
    q: np.ndarray, d: np.ndarray, queries: list[dict], docs: list[Doc], rel
) -> list:
    rows = []
    for qi, query in enumerate(queries):
        top = [docs[i].key for i in np.argsort(-(d @ q[qi]))[:10]]
        relset = rel[query["id"]]
        first = next((rank for rank, key in enumerate(top, 1) if key in relset), None)
        rows.append(
            {
                "id": query["id"],
                "source": query["source"],
                "mrr10": 1 / first if first else 0.0,
                "hit5": float(bool(first and first <= 5)),
                "p10": sum(key in relset for key in top) / 10,
            }
        )
    return rows


# --------------------------------------------------------------- embedding


def price_list() -> dict[str, float]:
    try:
        r = requests.get(f"{OPENROUTER_BASE_URL}/embeddings/models", timeout=30)
        r.raise_for_status()
        return {m["id"]: float(m["pricing"]["prompt"]) for m in r.json()["data"]}
    except (requests.RequestException, KeyError, ValueError):
        return {}


def check_key(key: str, allow_unlimited: bool) -> None:
    r = requests.get(
        f"{OPENROUTER_BASE_URL}/key",
        headers={"Authorization": f"Bearer {key}"},
        timeout=30,
    )
    r.raise_for_status()
    info = r.json().get("data", {})
    print(
        f"key: limit={info.get('limit')} remaining={info.get('limit_remaining')} "
        f"reset={info.get('limit_reset')} usage={info.get('usage')}"
    )
    if info.get("limit") is None and not allow_unlimited:
        raise SystemExit("this key has no credit limit; set one on openrouter.ai first")


def post_embeddings(model: str, texts: list[str], key: str) -> tuple[list, int]:
    for attempt in range(5):
        r = requests.post(
            f"{OPENROUTER_BASE_URL}/embeddings",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "input": texts},
            timeout=180,
        )
        body = (
            r.json()
            if r.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        if r.status_code == 200 and "data" in body:
            data = sorted(body["data"], key=lambda item: item["index"])
            if len(data) != len(texts):
                raise RuntimeError(
                    f"{model}: {len(data)} vectors for {len(texts)} inputs"
                )
            usage = body.get("usage") or {}
            return [item["embedding"] for item in data], int(
                usage.get("prompt_tokens") or 0
            )
        if r.status_code in (408, 429, 500, 502, 503, 504) and attempt < 4:
            time.sleep(2 ** (attempt + 1))
            continue
        raise RuntimeError(f"{model}: HTTP {r.status_code} {r.text[:300]}")
    raise RuntimeError(f"{model}: gave up after retries")


class VectorCache:
    """Vectors by sha256 of the exact text sent, one file per model."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.path = BENCH / (re.sub(r"[^\w.-]", "_", model) + ".npz")
        self.vectors: dict[str, np.ndarray] = {}
        if self.path.exists():
            saved = np.load(self.path)
            self.vectors = dict(
                zip(saved["keys"].tolist(), saved["vectors"], strict=True)
            )

    def save(self) -> None:
        BENCH.mkdir(parents=True, exist_ok=True)
        np.savez(
            self.path,
            keys=np.array(list(self.vectors)),
            vectors=np.stack(list(self.vectors.values())),
        )

    def embed(
        self, texts: list[str], key: str, batch: int = 64
    ) -> tuple[np.ndarray, int]:
        hashes = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]
        todo = [
            (h, t)
            for h, t in dict(zip(hashes, texts, strict=True)).items()
            if h not in self.vectors
        ]
        spent = 0
        for n, i in enumerate(range(0, len(todo), batch), 1):
            part = todo[i : i + batch]
            vectors, tokens = post_embeddings(self.model, [t for _, t in part], key)
            spent += tokens
            for (h, _), v in zip(part, vectors, strict=True):
                self.vectors[h] = np.asarray(v, dtype=np.float16)
            if n % 10 == 0:
                self.save()
        if todo:
            self.save()
        return np.stack([self.vectors[h] for h in hashes]).astype(np.float32), spent


# ------------------------------------------------------------------ report


def mean(rows: list[dict], field: str, source: str | None = None) -> float:
    chosen = [r[field] for r in rows if source is None or r["source"] == source]
    return sum(chosen) / len(chosen) if chosen else 0.0


def write_report(docs, queries, rel, results, failures, prices, spent) -> None:
    sources = ["agenda", "steno", "youtube"]
    out = [
        "# Embedding benchmark",
        "",
        "Generated by `uv run python -m bench.embeddings --run`. Dense retrieval only,",
        "over one mixed corpus; every query searches every document. With 24 queries a",
        "difference of a few hundredths in MRR is noise, not a ranking.",
        "",
        "## Corpus and queries",
        "",
        "| source | documents | characters | queries |",
        "| --- | --- | --- | --- |",
    ]
    for s in sources:
        ds = [d for d in docs if d.source == s]
        qs = [q for q in queries if q["source"] == s]
        out.append(
            f"| {s} | {len(ds)} | {sum(len(d.text) for d in ds):,} | {len(qs)} |"
        )
    out += ["", "| query | source | relevant documents |", "| --- | --- | --- |"]
    out += [f"| {q['text']} | {q['source']} | {len(rel[q['id']])} |" for q in queries]
    out += [
        "",
        "## Results",
        "",
        "HNSW: `vector` indexes up to 2,000 dimensions, `halfvec` up to 4,000.",
        "The last column prices one pass over the MVP corpus",
        f"(~{MVP_TOKENS / 1e6:.1f}M tokens).",
        "",
        "| model | dims | HNSW | MB per 100k chunks | MRR@10 | Hit@5 | P@10 "
        "| agenda | steno | youtube | $ per 1M tokens | ~$ MVP corpus |",
        "|" + " --- |" * 12,
    ]
    for r in sorted(results, key=lambda x: -mean(x["rows"], "mrr10")):
        dims, rows = r["dims"], r["rows"]
        hnsw = "vector" if dims <= 2000 else "halfvec" if dims <= 4000 else "none"
        cut = "" if dims == r["native"] else " (cut)"
        price = prices.get(r["model"])
        cost = "? | ?"
        if price is not None:
            cost = f"{price * 1e6:.3f} | {price * MVP_TOKENS:.2f}"
        by_source = " | ".join(f"{mean(rows, 'mrr10', s):.3f}" for s in sources)
        out.append(
            f"| {r['model']} | {dims}{cut} | {hnsw} | {(2 * dims + 8) / 10:,.0f} "
            f"| {mean(rows, 'mrr10'):.3f} | {mean(rows, 'hit5'):.2f} "
            f"| {mean(rows, 'p10'):.2f} | {by_source} | {cost} |"
        )
    natives = [r for r in results if r["dims"] == r["native"]]
    if natives:
        out += ["", "## MRR@10 per query (native dimensions)", ""]
        out.append(
            "| query | " + " | ".join(r["model"].split("/")[1] for r in natives) + " |"
        )
        out.append("|" + " --- |" * (len(natives) + 1))
        for qi, q in enumerate(queries):
            cells = " | ".join(f"{r['rows'][qi]['mrr10']:.2f}" for r in natives)
            out.append(f"| {q['id']} | {cells} |")
    if failures:
        out += ["", "## Failed", ""] + [f"- `{m}`: {e}" for m, e in failures.items()]
    out += ["", f"Tokens billed during this run: {spent:,}.", ""]
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text("\n".join(out), "utf-8")
    print(f"written {REPORT}")


# -------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--run", action="store_true", help="embed and score (spends money)")
    ap.add_argument("--models", nargs="*", help="subset of candidate model ids")
    ap.add_argument(
        "--allow-unlimited",
        action="store_true",
        help="accept a key without a credit limit",
    )
    args = ap.parse_args(argv)

    docs = build_corpus()
    queries = json.loads(QUERIES.read_text("utf-8"))
    rel = {q["id"]: relevant(q, docs) for q in queries}
    text = {d.key: d.text for d in docs}
    for s in ("agenda", "steno", "youtube"):
        ds = [d for d in docs if d.source == s]
        print(
            f"{s:<8} {len(ds):>6,} documents {sum(len(d.text) for d in ds):>12,} chars"
        )
    for q in queries:
        keys = sorted(rel[q["id"]])
        flag = (
            "   <- NOTHING RELEVANT"
            if not keys
            else "   <- broad"
            if len(keys) > 60
            else ""
        )
        print(f"\n[{q['id']}] {q['text']} -> {len(keys)} relevant{flag}")
        for key in keys[:3]:
            print(f"      {key:<20} {text[key][:140]}")

    prices = price_list()
    chosen = [c for c in CANDIDATES if not args.models or c.model in args.models]
    chars = sum(len(d.text) for d in docs)
    print(f"\n~{chars / CHARS_PER_TOKEN:,.0f} tokens per model for this corpus")
    for c in chosen:
        price = prices.get(c.model)
        cost = (
            f"~${chars / CHARS_PER_TOKEN * price:.3f}"
            if price is not None
            else "not listed"
        )
        print(f"  {c.model:<36} {cost}")
    if not args.run:
        print("\ndry run: nothing embedded. Review the relevance above, then --run.")
        return
    if any(not rel[q["id"]] for q in queries):
        raise SystemExit("some queries have no relevant documents; fix the rules first")
    if not OPENROUTER_API_KEY:
        raise SystemExit("OPENROUTER_API_KEY is not set in .env")
    check_key(OPENROUTER_API_KEY, args.allow_unlimited)

    results, failures, spent = [], {}, 0
    for c in chosen:
        cache = VectorCache(c.model)
        try:
            d, used_d = cache.embed(
                [c.doc_prefix + doc.text for doc in docs], OPENROUTER_API_KEY
            )
            q, used_q = cache.embed(
                [c.query_prefix + x["text"] for x in queries], OPENROUTER_API_KEY
            )
        except RuntimeError as exc:
            failures[c.model] = str(exc)
            print(f"  FAILED {exc}")
            continue
        spent += used_d + used_q
        native = d.shape[1]
        dims = [native] + [x for x in SHORTER_DIMS if x < native] * c.matryoshka
        for k in dims:
            rows = score(normalise(q[:, :k]), normalise(d[:, :k]), queries, docs, rel)
            results.append(
                {"model": c.model, "dims": k, "native": native, "rows": rows}
            )
            print(f"  {c.model:<36} {k:>5}d  MRR@10 {mean(rows, 'mrr10'):.3f}")
    write_report(docs, queries, rel, results, failures, prices, spent)


if __name__ == "__main__":
    main()
