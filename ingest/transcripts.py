"""Trim YouTube transcripts before chunking.

Removes what speech recognition got wrong and what another video already
contains, without touching the harvested transcripts: it writes a trimmed copy
of every video plus a log of each removed segment and the rule that removed it.

    uv run python -m ingest.transcripts

Output: data/youtube/trimmed/<video id>.json and reports/transcript_trim.md.

Rules, in order. Each was measured on this corpus before it was adopted.

1. A video whose text is mostly not Lithuanian, or mostly flagged by Whisper, is
   dropped whole. These are English, Polish and Ukrainian interviews run through
   Lithuanian recognition; the output is not a transcript of anything.
2. A segment Whisper itself scored as unreliable is dropped: avg_logprob < -1.0
   or compression_ratio > 2.4, Whisper's own fallback thresholds. Every sampled
   segment just past either cut was garbage; just inside was mostly real speech.
3. A segment containing Cyrillic, Arabic, Hebrew, Greek or CJK characters is
   dropped: in these transcripts that is mixed-script hallucination.
4. In a run of three or more identical consecutive segments, only the first stays.
5. A segment is dropped when 80% of its words repeat a longer video. OpTV
   publishes clips cut from its streams, so the same speech appears twice; the
   longest carrier keeps it. A video left with under a fifth of its text is
   dropped whole instead of being kept as orphan fragments.

Rule 1 uses a character-trigram model of clean parliamentary Lithuanian. It is
never applied to single segments, where it also flags ordinary colloquial speech.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from lxml import html as LH

from .config import DATA_DIR, ROOT
from .http import fetch
from .names import normal
from .seimas import VILNIUS, read_record

YOUTUBE = DATA_DIR / "youtube"
TRIMMED = YOUTUBE / "trimmed"
REPORT = ROOT / "reports" / "transcript_trim.md"

MAX_LOGPROB = -1.0
MAX_COMPRESSION = 2.4
NOT_LITHUANIAN_SHARE = 0.20  # the non-Lithuanian videos measured >= 24%; next was 16%
UNRELIABLE_SHARE = 0.50
MIN_SCORED_CHARS = 25
LOOP_RUN = 3
SHINGLE = 10
DUPLICATE_SHARE = 0.8
DUPLICATE_VIDEO_SHARE = 0.8  # mostly-copied clips leave orphan fragments
MAX_CARRIERS = 15  # passages found in more videos than this are generic, not copies
FOREIGN_SCRIPT = re.compile("[Ͱ-ϿЀ-ӿ֐-׿؀-ۿ぀-ヿ一-鿿가-힯]")
REFERENCE_STENOGRAM = (
    "https://e-seimas.lrs.lt/rs/legalact/TAK/f5da17b08e6411f0b351ee31aa0a26b8/"
)


def text_of(segment: dict) -> str:
    return segment.get("text") or ""


# ----------------------------------------------------------- language model


def fold_chars(text: str) -> str:
    text = re.sub(r"\s+", " ", text.lower().replace("­", ""))
    return re.sub(r"[^a-ząčęėįšųūž ]", "#", text)


class CharModel:
    """Character-trigram model of Lithuanian, scored in bits per character."""

    def __init__(self, reference: str) -> None:
        text = fold_chars(reference)
        self.tri = Counter(text[i : i + 3] for i in range(len(text) - 2))
        self.bi = Counter(text[i : i + 2] for i in range(len(text) - 1))
        self.alphabet = len(set(text))

    def score(self, text: str) -> float:
        t = fold_chars(text)
        if len(t) < 3:
            return 0.0
        logs = [
            math.log2(
                (self.tri[t[i : i + 3]] + 0.1)
                / (self.bi[t[i : i + 2]] + 0.1 * self.alphabet)
            )
            for i in range(len(t) - 2)
        ]
        return sum(logs) / len(logs)


def reference() -> tuple[str, list[str]]:
    """Clean Lithuanian: one cached stenogram plus every agenda title."""
    page = fetch(REFERENCE_STENOGRAM, suffix=".html", offline=True)
    if page is None:
        raise SystemExit("reference stenogram not cached; run the Seimas harvest first")
    paragraphs = [p.text_content() for p in LH.fromstring(page).iter("p")]
    titles = {a["title"] for a in read_record().agenda}
    training = " ".join(paragraphs) + " " + " ".join(titles)
    return training, [p for p in paragraphs if len(p) > 60]


# ------------------------------------------------------------------ videos


@dataclass
class Video:
    id: str
    title: str
    channel: str
    version: str
    segments: list[dict]
    signals: list[dict] | None  # Whisper scores, aligned with segments by index
    removed: dict[int, tuple[str, str]] = field(default_factory=dict)
    dropped: tuple[str, str] | None = None


def load_videos(root: Path = YOUTUBE) -> list[Video]:
    videos = []
    for vdir in sorted((root / "transcripts").iterdir()):
        versions = sorted(v for v in vdir.iterdir() if (v / "transcript.json").exists())
        if not versions:
            continue
        # published captions win over local speech recognition
        version = next(
            (v for v in versions if not v.name.startswith("asr")), versions[0]
        )
        transcript = json.loads((version / "transcript.json").read_text("utf-8"))
        segments = list(transcript.get("segments") or [])
        signals = None
        raw = version / "asr_raw.json"
        if raw.exists():
            raw_segments = json.loads(raw.read_text("utf-8")).get("segments") or []
            if len(raw_segments) == len(segments):
                signals = raw_segments
        meta_path = root / "videos" / f"{vdir.name}.json"
        meta = json.loads(meta_path.read_text("utf-8")) if meta_path.exists() else {}
        videos.append(
            Video(
                id=transcript.get("video_id") or vdir.name,
                title=meta.get("title") or transcript.get("title") or "",
                channel=meta.get("channel") or "",
                version=version.name,
                segments=segments,
                signals=signals,
            )
        )
    return videos


# ------------------------------------------------------------------- rules


def whisper_unreliable(signal: dict | None) -> str | None:
    if not signal:
        return None
    if (signal.get("avg_logprob") or 0.0) < MAX_LOGPROB:
        return "low_confidence"
    if (signal.get("compression_ratio") or 0.0) > MAX_COMPRESSION:
        return "repetition"
    return None


def video_verdict(
    video: Video, model: CharModel, cutoff: float
) -> tuple[str, str] | None:
    total = sum(len(text_of(s)) for s in video.segments)
    if not total:
        return ("empty", "no text")
    alien = sum(
        len(t)
        for s in video.segments
        if len(t := text_of(s)) >= MIN_SCORED_CHARS
        and (FOREIGN_SCRIPT.search(t) or model.score(t) < cutoff)
    )
    if alien / total >= NOT_LITHUANIAN_SHARE:
        return ("not_lithuanian", f"{alien / total:.0%} of text is not Lithuanian")
    signals = video.signals or [None] * len(video.segments)
    unreliable = sum(
        len(text_of(s))
        for s, sig in zip(video.segments, signals, strict=True)
        if whisper_unreliable(sig)
    )
    if unreliable / total >= UNRELIABLE_SHARE:
        return ("unreliable", f"Whisper flags {unreliable / total:.0%} of text")
    return None


def repetition_loops(segments: list[dict]) -> set[int]:
    """Indexes of repeats in runs of LOOP_RUN+ identical consecutive segments."""
    loops: set[int] = set()
    start = 0
    for i in range(1, len(segments) + 1):
        same = i < len(segments) and normal(text_of(segments[i])) == normal(
            text_of(segments[start])
        )
        if same:
            continue
        if i - start >= LOOP_RUN and normal(text_of(segments[start])):
            loops.update(range(start + 1, i))
        start = i
    return loops


def mark_segments(video: Video) -> None:
    loops = repetition_loops(video.segments)
    for i, segment in enumerate(video.segments):
        text = text_of(segment)
        signal = video.signals[i] if video.signals else None
        if not text.strip():
            video.removed[i] = ("empty", "")
        elif reason := whisper_unreliable(signal):
            video.removed[i] = (f"whisper_{reason}", "")
        elif FOREIGN_SCRIPT.search(text):
            video.removed[i] = ("foreign_script", "")
        elif i in loops:
            video.removed[i] = ("repetition_loop", "")


def duplicates(videos: list[Video]) -> dict[str, dict[int, str]]:
    """Per video, segments mostly repeating a longer video -> that video's id."""
    words = {
        v.id: [
            (w, i) for i, s in enumerate(v.segments) for w in normal(text_of(s)).split()
        ]
        for v in videos
    }
    size = {vid: len(seq) for vid, seq in words.items()}
    shingles = {
        vid: [
            hash(" ".join(w for w, _ in seq[i : i + SHINGLE]))
            for i in range(len(seq) - SHINGLE + 1)
        ]
        for vid, seq in words.items()
    }
    spread = Counter()
    for hs in shingles.values():
        spread.update(set(hs))
    carriers: dict[int, list[str]] = defaultdict(list)
    for vid, hs in shingles.items():
        for h in set(hs):
            if 2 <= spread[h] <= MAX_CARRIERS:
                carriers[h].append(vid)
    keeper = {h: max(vs, key=lambda v: (size[v], v)) for h, vs in carriers.items()}

    marks: dict[str, dict[int, str]] = {}
    for vid, hs in shingles.items():
        seq = words[vid]
        source: list[str | None] = [None] * len(seq)
        for i, h in enumerate(hs):
            owner = keeper.get(h)
            if owner is not None and owner != vid:
                for j in range(i, i + SHINGLE):
                    source[j] = owner
        per_segment: dict[int, Counter] = defaultdict(Counter)
        for (_, seg), src in zip(seq, source, strict=True):
            per_segment[seg][src] += 1
        found = {}
        for seg, counts in per_segment.items():
            copied = sum(n for src, n in counts.items() if src is not None)
            if copied / sum(counts.values()) >= DUPLICATE_SHARE:
                found[seg] = max((src for src in counts if src), key=counts.get)
        if found:
            marks[vid] = found
    return marks


def mostly_copied(video: Video) -> tuple[str, str] | None:
    """Drop a clip whose remainder after deduplication is only fragments."""
    size = sum(len(text_of(s)) for s in video.segments)
    copied: Counter = Counter()
    for i, (reason, source) in video.removed.items():
        if reason == "duplicate":
            copied[source] += len(text_of(video.segments[i]))
    if not size or sum(copied.values()) / size < DUPLICATE_VIDEO_SHARE:
        return None
    source, _ = copied.most_common(1)[0]
    return ("duplicate_video", f"{sum(copied.values()) / size:.0%} repeats {source}")


# ------------------------------------------------------------------ output


def write_trimmed(video: Video, trimmed_at: str) -> None:
    doc = {
        "video_id": video.id,
        "title": video.title,
        "channel": video.channel,
        "source_version": video.version,
        "trimmed_at": trimmed_at,
        "dropped": dict(zip(("reason", "detail"), video.dropped, strict=True))
        if video.dropped
        else None,
        "segments": []
        if video.dropped
        else [s for i, s in enumerate(video.segments) if i not in video.removed],
        "removed": [
            {
                "index": i,
                "start": video.segments[i].get("start"),
                "end": video.segments[i].get("end"),
                "reason": reason,
                "detail": detail,
                "text": text_of(video.segments[i]),
            }
            for i, (reason, detail) in sorted(video.removed.items())
        ],
    }
    path = TRIMMED / f"{video.id}.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), "utf-8")


def table(columns: list[str], rows: list[tuple]) -> str:
    out = ["| " + " | ".join(columns) + " |", "|" + " --- |" * len(columns)]
    out += [
        "| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |" for r in rows
    ]
    return "\n".join(out) + "\n"


def write_report(videos: list[Video], cutoff: float) -> None:
    total = sum(len(text_of(s)) for v in videos for s in v.segments)
    chars, segments = Counter(), Counter()
    samples: dict[str, list] = defaultdict(list)
    losing = []
    for v in videos:
        size = sum(len(text_of(s)) for s in v.segments)
        if v.dropped:
            chars[f"video: {v.dropped[0]}"] += size
            segments[f"video: {v.dropped[0]}"] += len(v.segments)
            continue
        lost = 0
        for i, (reason, detail) in v.removed.items():
            text = text_of(v.segments[i])
            chars[reason] += len(text)
            segments[reason] += 1
            lost += len(text)
            if len(samples[reason]) < 3 and len(text.strip()) > 20:
                where = f"{v.id} @{int(v.segments[i].get('start') or 0)}s"
                if detail:
                    where += f", also in {detail}"
                samples[reason].append((where, text.strip()[:110]))
        if size:
            losing.append((lost / size, v))
    removed = sum(chars.values())
    dropped = [v for v in videos if v.dropped]
    parts = [
        "# Transcript trimming\n",
        f"Generated {datetime.now(VILNIUS):%Y-%m-%d %H:%M} by "
        "`uv run python -m ingest.transcripts`. Regenerate; do not edit. The rules "
        "and the evidence for each threshold are in `ingest/transcripts.py`.\n",
        f"{len(videos)} videos, {total:,} characters. Removed **{removed:,} "
        f"({removed / total:.1%})**, kept {total - removed:,}. {len(dropped)} videos "
        f"dropped whole. Character-model cutoff: {cutoff:.2f} bits/char.\n",
        "## Removed, by rule\n",
        table(
            ["rule", "segments", "characters", "% of corpus"],
            [
                (r, f"{segments[r]:,}", f"{n:,}", f"{n / total:.2%}")
                for r, n in chars.most_common()
            ],
        ),
        "## Videos dropped whole\n",
        table(
            ["video", "channel", "title", "why"],
            [(v.id, v.channel, v.title[:70], v.dropped[1]) for v in dropped],
        ),
        "## Kept videos losing the most text\n",
        table(
            ["video", "channel", "title", "removed"],
            [
                (v.id, v.channel, v.title[:70], f"{share:.0%}")
                for share, v in sorted(losing, key=lambda x: -x[0])[:10]
            ],
        ),
        "## Samples of removed segments\n",
    ]
    for reason, rows in samples.items():
        if not rows:
            continue
        parts.append(f"### {reason}\n\n" + table(["where", "text"], rows))
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text("\n".join(parts), "utf-8")


def main(argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args(argv)
    videos = load_videos()
    training, calibration = reference()
    model = CharModel(training)
    scores = sorted(model.score(p) for p in calibration)
    cutoff = scores[len(scores) // 100] - 1.0

    for video in videos:
        video.dropped = video_verdict(video, model, cutoff)
        if not video.dropped:
            mark_segments(video)
    by_id = {v.id: v for v in videos}
    for vid, found in duplicates([v for v in videos if not v.dropped]).items():
        for index, source in found.items():
            by_id[vid].removed.setdefault(index, ("duplicate", source))
    for video in videos:
        if not video.dropped:
            video.dropped = mostly_copied(video)

    TRIMMED.mkdir(parents=True, exist_ok=True)
    trimmed_at = datetime.now(VILNIUS).isoformat(timespec="seconds")
    for video in videos:
        write_trimmed(video, trimmed_at)
    write_report(videos, cutoff)
    print(REPORT.read_text("utf-8").split("## Kept videos")[0])


if __name__ == "__main__":
    main()
