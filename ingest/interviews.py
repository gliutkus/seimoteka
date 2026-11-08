"""Add a YouTube interview to the knowledge base.

Paste a link and name who speaks in it; the rest runs in the background. The
video's page and audio are fetched, the speech is transcribed and the audio is
split by voice. A person then says which voice is whom, and only then are the
speaker turns loaded as passages, embedded and made searchable. The Streamlit
page "Papildyti žinių bazę" (app.py, shown with KB_UPDATES=1) drives the steps;
each also runs from the command line, from files/:

    uv run python -m ingest.interviews start LINK --mp "Name Surname" --other "Host"
    uv run python -m ingest.interviews run VIDEO_ID
    uv run python -m ingest.interviews assign VIDEO_ID A=1 B=2 C=-
    uv run python -m ingest.interviews load VIDEO_ID [--allow-unlimited]
    uv run python -m ingest.interviews remove VIDEO_ID
    uv run python -m ingest.interviews list

A job is data/youtube/interviews/<video id>/job.json. Its state moves through
queued, downloading, transcribing, separating and review, then loading and
loaded; a step that fails records its error. Every allowed move is listed in
LIFECYCLE below, and move() refuses any other (`python -m ingest.lifecycle`
draws it). A video's passages are in the knowledge base only while its job is
loaded. One job is processed at a time, and downloads take the YouTube harvest
lock, so they never overlap a harvest.

What keeps interviews inside the project's rules:
- A voice becomes a named speaker only when a person assigns it. Speech in a
  voice nobody assigned, and speech where two voices overlap, has no speaker.
  Identity is never inferred from a voice, a title or a description.
- A recording shows what was said, not whether it is true; the agent's prompt
  says so for every passage of type youtube_interview.
- The transcript is cleaned before chunking. A video ingest.transcripts already
  trimmed keeps its trimmed segments; any other loses what the same rules remove
  within one video: segments Whisper scored unreliable, mixed-script
  hallucinations and repetition loops.
- The audio is kept only until the interview is loaded, so the review page can
  play each voice; then it is deleted.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import traceback
import wave
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import psycopg
import requests
from psycopg.types.json import Jsonb

from . import config, lifecycle, progress, youtube, youtube_asr
from .text import split_long
from .transcripts import FOREIGN_SCRIPT, TRIMMED, repetition_loops, whisper_unreliable

JOBS = youtube.ROOT / "interviews"
# The harvest's guess at who appears in each transcribed video, from its title and
# description (ingest.youtube), is youtube.ROOT / PARTICIPANTS: unreviewed
# candidates, never an attribution. Videos a person checked and found no MP
# speaking in are JOBS / NO_MP: {video id: {"at", "note"}}.
PARTICIPANTS = "participants.json"
HARVESTED_VERSION = 2  # raise when harvested() gains or changes fields
NO_MP = "no_mp.json"
MODELS = youtube.ROOT / "models" / "sherpa-onnx"
DOC_TYPE = "youtube_interview"
SAMPLE_RATE = 16_000
MAX_SECONDS = 4 * 3600
MAX_AUDIO_BYTES = 250 * 1024**2
MAX_CHARS = 1500
UNKNOWN_SPEAKER = "Nenustatytas kalbėtojas"
# Passages are cut from turns, and a turn ends wherever the speaker changes. Where
# two voices overlap for a moment, the few words there have no speaker; each such
# crumb split a turn around it into scraps that matched searches by the heading
# alone. Crumbs of up to CRUMB_WORDS words are left out, so a speaker's words on
# either side join into one turn (no word gains a speaker), and passages of fewer
# than MIN_WORDS words ("Taip.") are not written.
CRUMB_WORDS = 4
MIN_WORDS = 5
LIFECYCLE = lifecycle.Lifecycle(
    "interview",
    lifecycle.moves(
        (None, "queued", "start()"),
        ("failed", "queued", "start() again, or run() from the command line"),
        (
            "review",
            "queued",
            "separate_again(), start() again, or run() from the command line",
        ),
        ("queued", "downloading", "run()"),
        ("downloading", "transcribing", "run()"),
        ("transcribing", "separating", "run()"),
        ("separating", "review", "run()"),
        (
            ("queued", "downloading", "transcribing", "separating"),
            "failed",
            "run() on an error, or refresh()",
        ),
        ("review", "loading", "queue_load(), or load() from the command line"),
        ("loaded", "loading", "load() again from the command line"),
        ("loading", "loaded", "load()"),
        ("loading", "review", "load() on an error, or refresh()"),
        ("loading", "failed", "refresh(), when the transcript is gone"),
        (("loaded", "failed"), "review", "remove()"),
        (("loaded", "review"), "failed", "remove(), when the transcript is gone"),
        (("review", "failed"), None, "forget(), or mark_no_mp()"),
    ),
    running=("queued", "downloading", "transcribing", "separating", "loading"),
    waiting=("review", "failed"),
)
RUNNING = LIFECYCLE.running
WAITING = LIFECYCLE.waiting  # for a person: to say who speaks, or to try again
STALE_START = 300  # seconds a queued job may wait for its worker to report in

# Speaker separation: pyannote segmentation-3.0 finds who is active in each window
# and WeSpeaker ResNet34 embeds each voice, both run by sherpa-onnx on the CPU.
THREADS = max(1, min(8, (os.cpu_count() or 2) - 2))
WINDOW_SHIFT = 0.5  # of the 10 s window; pyannote's 0.1 costs 5x the embeddings
CLUSTER_THRESHOLD = 0.5  # sherpa-onnx's default; a larger one merges more voices
OVERLAP_SHARE = 0.5  # a word two voices each cover this much of is overlapping speech
NEAREST_GAP = 0.5  # seconds: a word in no voice's turn joins a turn this close
UTTERANCE_GAP = 3.0  # seconds of silence that end an utterance of one voice
MODEL_FILES = {
    "segmentation": {
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
        "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
        "archive": "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
        "archive_sha256": "24615ee884c897d9d2ba09bb4d30da6b"
        "b1b15e685065962db5b02e76e4996488",
        "path": "sherpa-onnx-pyannote-segmentation-3-0/model.onnx",
        "sha256": "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079",
    },
    "embedding": {
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
        "speaker-recongition-models/wespeaker_en_voxceleb_resnet34.onnx",
        "path": "wespeaker_en_voxceleb_resnet34.onnx",
        "sha256": "5ef208a9da1453335308a6b6f4e6dfbd7e183a38b604de0a57664f45d257fe94",
    },
}

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
LINKED_VIDEO = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:live/|watch\?v=|shorts/))([\w-]{11})"
)
NAME = re.compile(r"[^\W\d_](?:[^\W\d_]|[ .'’-])*")
ROSTER_SQL = (
    "SELECT id, first_name || ' ' || last_name FROM mps ORDER BY last_name, first_name"
)
CHUNK_COLUMNS = (
    "document_id, ordinal, content, speaker_name, speaker_mp_id, fraction, doc_type, "
    "doc_date, source_url, sitting_id, section, section_at, bill_number"
)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600}:{whole // 60 % 60:02}:{whole % 60:02}"


# ------------------------------------------------------------------ links, people


def video_id_of(link: str) -> str:
    """The video id in a YouTube link: watch?v=, youtu.be/, /live/, /shorts/ or
    /embed/."""
    parsed = urlparse(link.strip())
    if parsed.scheme not in ("http", "https") or parsed.hostname not in YOUTUBE_HOSTS:
        raise ValueError("not a YouTube link")
    parts = [p for p in parsed.path.split("/") if p]
    if parsed.hostname == "youtu.be":
        candidate = parts[0] if parts else ""
    elif parts == ["watch"]:
        candidate = parse_qs(parsed.query).get("v", [""])[0]
    elif len(parts) >= 2 and parts[0] in ("live", "shorts", "embed"):
        candidate = parts[1]
    else:
        candidate = ""
    if not VIDEO_ID.fullmatch(candidate):
        raise ValueError("the link names no video")
    return candidate


def watch_url(video_id: str, seconds: float | None = None) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    return f"{url}&t={int(seconds)}s" if seconds else url


def roster(conn) -> dict[str, int]:
    """MPs of the term by full name."""
    return {name: mp_id for mp_id, name in conn.execute(ROSTER_SQL)}


def participants(
    members: dict[str, int], mps: list[str], others: list[str]
) -> list[dict]:
    """The people who speak: MPs chosen by their roster name, others by a typed name."""
    people, seen = [], set()
    folded = {name.casefold() for name in members}
    for name in mps:
        if name not in members:
            raise ValueError(f"not an MP of this term: {name}")
        people.append({"name": name, "mp_id": members[name]})
    for text in others:
        name = " ".join(text.split())
        if not name:
            continue
        if not (2 <= len(name) <= 80 and NAME.fullmatch(name)):
            raise ValueError(f"not a name: {text!r}")
        if name.casefold() in folded:
            raise ValueError(f"{name} is an MP: choose them from the list of MPs")
        people.append({"name": name, "mp_id": None})
    for person in people:
        if person["name"].casefold() in seen:
            raise ValueError(f"{person['name']} is named twice")
        seen.add(person["name"].casefold())
    if not people:
        raise ValueError("name at least one person who speaks in the video")
    return people


# ------------------------------------------------------------------------- jobs


def job_dir(video_id: str) -> Path:
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError(f"not a video id: {video_id!r}")
    return JOBS / video_id


def read_job(video_id: str) -> dict | None:
    return youtube.read(job_dir(video_id) / "job.json")


def save_job(job: dict) -> dict:
    job["updated_at"] = now()
    youtube.save(job_dir(job["video_id"]) / "job.json", job)
    return job


def update(job: dict, **fields) -> dict:
    """Write fields of a job as they are. Its state changes through move()."""
    job.update(fields)
    return save_job(job)


move = lifecycle.mover(LIFECYCLE, update)


def enter(job: dict, state: str, stage: str = "", **fields) -> dict:
    """Move a job to its next state, timing each state as a step."""
    return move(job, state, stage=stage, **progress.begin(job, state, now()), **fields)


def owned_elsewhere(job: dict) -> bool:
    """Whether another live process is working on this job."""
    pid = job.get("pid")
    return job["state"] in RUNNING and pid != os.getpid() and alive(pid)


def reporter(job: dict, unit: str):
    """A (done, total) callback that records the running step's progress."""
    return progress.throttled(lambda state: update(job, progress=state), unit)


def all_jobs() -> list[dict]:
    if not JOBS.exists():
        return []
    found = [youtube.read(path) for path in JOBS.glob("*/job.json")]
    return sorted(
        (refresh(job) for job in found if job),
        key=lambda j: j["created_at"],
        reverse=True,
    )


alive = lifecycle.alive


def refresh(job: dict) -> dict:
    """A job whose worker died, or never started, stops waiting for it."""
    if not lifecycle.abandoned(job, LIFECYCLE, datetime.now(UTC), STALE_START):
        return job
    reviewable = (job_dir(job["video_id"]) / "utterances.json").exists()
    back = "review" if job["state"] == "loading" and reviewable else "failed"
    return move(
        job, back, stage="", pid=None, error="the step stopped before it finished"
    )


def start(
    link: str,
    people: list[dict],
    *,
    voices: int | None = None,
    launch_worker: bool = True,
) -> dict:
    video_id = video_id_of(link)
    existing = read_job(video_id)
    if existing and refresh(existing)["state"] in RUNNING:
        raise ValueError("this video is already being processed")
    if existing and existing["state"] == "loaded":
        raise ValueError("this video is already in the knowledge base; remove it first")
    if voices is not None and not 1 <= voices <= 10:
        raise ValueError("the number of voices must be between 1 and 10")
    LIFECYCLE.check(existing and existing["state"], "queued")
    job_dir(video_id).mkdir(parents=True, exist_ok=True)
    job = save_job(
        {
            "video_id": video_id,
            "url": watch_url(video_id),
            "state": "queued",
            "stage": "",
            "participants": people,
            "voices_expected": voices,
            "created_at": now(),
            "error": None,
            "pid": None,
        }
    )
    if launch_worker:
        launch(video_id, "run")
    return job


def launch(video_id: str, command: str, *extra: str) -> None:
    """Run a step in its own process, so it outlives the page that started it. The
    worker records its own pid; the page never writes the job after launching."""
    log = (job_dir(video_id) / f"{command}.log").open("a")
    subprocess.Popen(
        [sys.executable, "-m", "ingest.interviews", command, video_id, *extra],
        cwd=config.ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


@contextmanager
def held(path: Path, job: dict, waiting: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            update(job, stage=waiting)
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield


# ------------------------------------------------------------ fetch and transcribe


def ydl_options(**extra) -> dict:
    """The harvester's yt-dlp settings: one retry, bounded timeouts, the delay floor."""
    return {
        "quiet": True,
        "logger": youtube.QuietLogger(),
        "noplaylist": True,
        "socket_timeout": 25,
        "retries": 1,
        "extractor_retries": 1,
        "sleep_interval_requests": max(1, youtube.REQUEST_DELAY),
        "cachedir": str(youtube.ROOT / "extractor_cache"),
        **extra,
    }


def describe(video_id: str, info: dict) -> dict:
    """What the job keeps from the video's page; refuses what cannot be processed."""
    if info.get("live_status") in ("is_live", "is_upcoming"):
        raise ValueError("the video is live or not yet broadcast")
    duration = int(info.get("duration") or 0)
    if duration > MAX_SECONDS:
        raise ValueError(
            f"the video lasts {duration // 60} min; the limit is {MAX_SECONDS // 60}"
        )
    uploaded = info.get("upload_date") or ""
    published = (
        f"{uploaded[:4]}-{uploaded[4:6]}-{uploaded[6:]}" if len(uploaded) == 8 else None
    )
    description = info.get("description") or ""
    linked = sorted(set(LINKED_VIDEO.findall(description)) - {video_id})
    return {
        "title": info.get("title") or video_id,
        "channel": info.get("channel") or info.get("uploader") or "",
        "published": published,
        "duration": duration,
        "description": description[:5000],
        "linked_videos": linked,
    }


def fetch_info(url: str) -> dict:
    from yt_dlp import YoutubeDL

    with YoutubeDL(
        ydl_options(skip_download=True, ignore_no_formats_error=True)
    ) as ydl:
        return ydl.extract_info(url, download=False, process=False)


def download_audio(url: str, folder: Path, report=None) -> Path:
    """The video's best audio; `report(done, total)` follows the bytes."""
    from yt_dlp import YoutubeDL

    def hook(state: dict) -> None:
        total = state.get("total_bytes") or state.get("total_bytes_estimate")
        if report and total and state.get("status") == "downloading":
            report(min(state.get("downloaded_bytes") or 0, total), total)

    for old in folder.glob("download.*"):
        old.unlink()
    options = ydl_options(
        format="bestaudio/best",
        outtmpl=str(folder / "download.%(ext)s"),
        fragment_retries=1,
        concurrent_fragment_downloads=1,
        max_filesize=MAX_AUDIO_BYTES,
        progress_hooks=[hook],
    )
    with YoutubeDL(options) as ydl:
        ydl.extract_info(url, download=True)
    files = [p for p in folder.glob("download.*") if not p.name.endswith(".part")]
    if not files:
        raise RuntimeError("the audio did not download, or exceeded the size limit")
    return files[0]


def to_wav(source: Path, target: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(source),
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16", str(target)],
        check=True,
    )  # fmt: skip


def existing_transcript(video_id: str) -> Path | None:
    """A Whisper transcript of the video with word timings, from the harvest or an
    earlier job."""
    folder = youtube.ROOT / "transcripts" / video_id
    if not folder.exists():
        return None
    versions = sorted(v for v in folder.glob("asr-*") if (v / "asr_raw.json").exists())
    return versions[0] if versions else None


def whisper(job: dict, wav: Path, report=None) -> Path:
    """Transcribe with the harvest's pinned local model, saved as the harvest saves
    a transcript. `report(done, total)` follows the audio transcribed, in frames of
    10 ms."""
    os.environ.setdefault("HF_HOME", str(youtube.ROOT / "models"))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    import mlx_whisper
    from huggingface_hub import snapshot_download

    if report:
        follow_whisper(report)

    model_path = snapshot_download(
        youtube_asr.MODEL_REPO,
        revision=youtube_asr.MODEL_REVISION,
        allow_patterns=["config.json", "*.safetensors", "*.npz"],
    )
    started = time.monotonic()
    result = mlx_whisper.transcribe(
        str(wav),
        path_or_hf_repo=model_path,
        language="lt",
        task="transcribe",
        verbose=False,
        condition_on_previous_text=False,
        word_timestamps=True,
        hallucination_silence_threshold=2.0,
    )
    raw = [
        {"text": s["text"], "start": s["start"], "duration": s["end"] - s["start"]}
        for s in result["segments"]
    ]
    video = {
        "video_id": job["video_id"],
        "url": job["url"],
        "title": job.get("title", ""),
    }
    model = f"{youtube_asr.MODEL_REPO}@{youtube_asr.MODEL_REVISION}"
    folder = youtube.export_transcript(video, raw, provider="mlx_whisper", model=model)
    youtube.save(folder / "asr_raw.json", result)
    youtube.save(
        folder / "audio_provenance.json",
        {
            "source_url": job["url"],
            "audio_sha256": sha256(wav),
            "model": youtube_asr.MODEL_REPO,
            "model_revision": youtube_asr.MODEL_REVISION,
            "generated_at": now(),
            "processing_seconds": round(time.monotonic() - started, 2),
            "review_status": "unreviewed",
            "speaker_attribution": "unverified",
        },
    )
    return folder


def follow_whisper(report) -> None:
    """Have mlx_whisper's progress bar report to `report(done, total)`. The
    library has no progress callback, only a tqdm bar over the audio's frames, which
    its transcribe module reaches as `tqdm.tqdm`."""
    from types import SimpleNamespace

    import tqdm

    class Bar(tqdm.tqdm):
        def update(self, n=1):
            shown = super().update(n)
            if self.total:
                report(self.n, self.total)
            return shown

    module = sys.modules["mlx_whisper.transcribe"]
    module.tqdm = SimpleNamespace(tqdm=Bar)


def kept_segments(video_id: str, version: str, segments: list[dict]) -> list[int]:
    """Indexes of the segments chunking may use (see the module docstring)."""
    trimmed = youtube.read(TRIMMED / f"{video_id}.json")
    if trimmed and trimmed.get("source_version") == version:
        if trimmed.get("dropped"):
            reason = trimmed["dropped"]
            raise ValueError(
                f"trimming dropped this transcript: {reason['reason']} "
                f"({reason['detail']})"
            )
        return sorted(s["ordinal"] for s in trimmed["segments"])
    loops = repetition_loops(segments)
    return [
        i
        for i, s in enumerate(segments)
        if (s.get("text") or "").strip()
        and not whisper_unreliable(s)
        and not FOREIGN_SCRIPT.search(s.get("text") or "")
        and i not in loops
    ]


def transcript_words(job: dict, wav: Path, report=None) -> tuple[list[dict], dict]:
    """Timed words of the cleaned transcript, and where the transcript came from."""
    version = existing_transcript(job["video_id"])
    reused = version is not None
    if version is None:
        version = whisper(job, wav, report)
    raw = json.loads((version / "asr_raw.json").read_text("utf-8"))
    segments = raw.get("segments") or []
    kept = kept_segments(job["video_id"], version.name, segments)
    words = [
        {"text": w["word"].strip(), "start": float(w["start"]), "end": float(w["end"])}
        for i in kept
        for w in segments[i].get("words") or []
        if w.get("word", "").strip() and w["end"] >= w["start"]
    ]
    if not words:
        raise ValueError("the transcript has no usable speech")
    source = {
        "version": version.name,
        "reused": reused,
        "model": f"{youtube_asr.MODEL_REPO}@{youtube_asr.MODEL_REVISION}",
        "segments": len(segments),
        "segments_kept": len(kept),
    }
    return words, source


# --------------------------------------------------------------------- voices


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_models() -> dict[str, Path]:
    """The two speaker models, downloaded once from sherpa-onnx's releases and
    checked against their SHA-256."""
    paths = {}
    for name, spec in MODEL_FILES.items():
        path = MODELS / spec["path"]
        if not (path.exists() and sha256(path) == spec["sha256"]):
            fetch_model(spec)
        paths[name] = path
    return paths


def fetch_model(spec: dict) -> None:
    MODELS.mkdir(parents=True, exist_ok=True)
    target = MODELS / (spec.get("archive") or spec["path"])
    part = target.with_name(target.name + ".part")
    headers = {"User-Agent": config.USER_AGENT}
    with requests.get(spec["url"], stream=True, timeout=60, headers=headers) as r:
        r.raise_for_status()
        with part.open("wb") as out:
            for block in r.iter_content(1 << 20):
                out.write(block)
    if sha256(part) != spec.get("archive_sha256", spec["sha256"]):
        part.unlink()
        raise RuntimeError(f"{target.name}: checksum does not match")
    part.replace(target)
    if "archive" in spec:
        with tarfile.open(target) as tar:
            tar.extract(tar.getmember(spec["path"]), MODELS, filter="data")
        if sha256(MODELS / spec["path"]) != spec["sha256"]:
            raise RuntimeError(f"{spec['path']}: checksum does not match")


def read_wav(path: Path):
    import numpy as np

    with wave.open(str(path)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (
            SAMPLE_RATE,
            1,
            2,
        ):
            raise ValueError(f"{path.name} is not 16 kHz mono 16-bit audio")
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def separate_voices(
    wav: Path, *, voices: int | None = None, progress=None
) -> list[tuple[float, float, int]]:
    """(start, end, voice) turns, sorted by start. Turns of two voices may overlap."""
    import sherpa_onnx

    models = ensure_models()
    settings = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(models["segmentation"]), window_shift_ratio=WINDOW_SHIFT
            ),
            num_threads=THREADS,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(models["embedding"]), num_threads=THREADS
        ),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=voices or -1, threshold=CLUSTER_THRESHOLD
        ),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not settings.validate():
        raise RuntimeError("the speaker separation settings are invalid")
    diarizer = sherpa_onnx.OfflineSpeakerDiarization(settings)

    def report(done, total):
        if progress:
            progress(int(done), int(total))
        return 0

    result = diarizer.process(read_wav(wav), callback=report).sort_by_start_time()
    return [(float(r.start), float(r.end), int(r.speaker)) for r in result]


def utterances_from(
    words: list[dict], turns: list[tuple[float, float, int]]
) -> list[dict]:
    """Consecutive words of one voice, with their times.

    A word takes the voice whose turns cover most of it. When a second voice also
    covers at least OVERLAP_SHARE of it, or no turn is within NEAREST_GAP, its
    voice is None: overlapping or unplaced speech is attributed to nobody.
    """
    ordered = sorted(turns)
    utterances: list[dict] = []
    active: list[tuple[float, float, int]] = []
    upcoming = 0
    for word in sorted(words, key=lambda w: (w["start"], w["end"])):
        start, end = word["start"], word["end"]
        while upcoming < len(ordered) and ordered[upcoming][0] < end + NEAREST_GAP:
            active.append(ordered[upcoming])
            upcoming += 1
        active = [t for t in active if t[1] > start - NEAREST_GAP]
        cover: dict[int, float] = defaultdict(float)
        for t_start, t_end, voice in active:
            cover[voice] += max(0.0, min(t_end, end) - max(t_start, start))
        ranked = sorted(cover.items(), key=lambda kv: -kv[1])
        duration = max(end - start, 0.01)
        if ranked and ranked[0][1] > 0:
            overlapping = len(ranked) > 1 and ranked[1][1] >= OVERLAP_SHARE * duration
            voice = None if overlapping else ranked[0][0]
        else:
            near = [
                t
                for t in active
                if t[0] - NEAREST_GAP <= end and start <= t[1] + NEAREST_GAP
            ]
            voices = {t[2] for t in near}
            voice = voices.pop() if len(voices) == 1 else None
        last = utterances[-1] if utterances else None
        if last and last["voice"] == voice and start - last["end"] <= UTTERANCE_GAP:
            last["end"] = end
            last["text"] += " " + word["text"]
        else:
            utterances.append(
                {"voice": voice, "start": start, "end": end, "text": word["text"]}
            )
    for u in utterances:
        u["start"], u["end"] = round(u["start"], 2), round(u["end"], 2)
    return utterances


def voice_summary(utterances: list[dict], samples: int = 4) -> list[dict]:
    """Each voice's talk time, first appearance and sample lines, lettered from the
    voice that talks most."""
    by_voice: dict[int, list[dict]] = defaultdict(list)
    for u in utterances:
        if u["voice"] is not None:
            by_voice[u["voice"]].append(u)
    total = sum(u["end"] - u["start"] for us in by_voice.values() for u in us) or 1.0
    summary = []
    for voice, spoken in by_voice.items():
        seconds = sum(u["end"] - u["start"] for u in spoken)
        candidates = [u for u in spoken if len(u["text"]) >= 40] or spoken
        step = max(1, -(-len(candidates) // samples))
        picks = [
            max(candidates[i : i + step], key=lambda u: u["end"] - u["start"])
            for i in range(0, len(candidates), step)
        ]
        summary.append(
            {
                "voice": voice,
                "seconds": round(seconds),
                "share": round(seconds / total, 3),
                "first": spoken[0]["start"],
                "utterances": len(spoken),
                "samples": [
                    {"start": u["start"], "end": u["end"], "text": u["text"][:300]}
                    for u in picks
                ],
            }
        )
    summary.sort(key=lambda v: (-v["seconds"], v["voice"]))
    for n, entry in enumerate(summary):
        entry["label"] = chr(ord("A") + n) if n < 26 else f"V{n + 1}"
    return summary


# ------------------------------------------------------------------- the worker


def run(video_id: str) -> dict:
    """Fetch, transcribe and separate one job's video, leaving it ready for review."""
    job = read_job(video_id)
    if job is None:
        raise SystemExit(f"no job for {video_id}")
    job = refresh(job)
    if owned_elsewhere(job):
        raise SystemExit(f"{video_id}: this video is already being processed")
    if job["state"] == "loaded":
        raise SystemExit(f"{video_id}: this video is in the knowledge base; remove it")
    if job["state"] != "queued":  # run again from the command line
        job = move(job, "queued", stage="", steps={}, progress=None)
    update(job, pid=os.getpid(), error=None)
    folder = job_dir(video_id)
    try:
        with held(JOBS / "worker.lock", job, "waiting for another interview to finish"):
            harvest_lock = youtube.ROOT / "harvest.lock"
            with held(
                harvest_lock, job, "waiting for another YouTube download to finish"
            ):
                enter(job, "downloading", "reading the video's page")
                update(job, **describe(video_id, fetch_info(job["url"])))
                if not (folder / "audio.wav").exists():
                    update(job, stage="downloading the audio")
                    audio = download_audio(job["url"], folder, reporter(job, "bytes"))
                    update(job, stage="converting the audio", progress=None)
                    to_wav(audio, folder / "audio.wav")
                    audio.unlink(missing_ok=True)
            enter(job, "transcribing")
            words, source = transcript_words(
                job, folder / "audio.wav", reporter(job, "frames")
            )
            enter(job, "separating", transcript=source)
            turns = separate_voices(
                folder / "audio.wav",
                voices=job.get("voices_expected"),
                progress=reporter(job, "steps"),
            )
            utterances = utterances_from(words, turns)
            youtube.save(folder / "utterances.json", utterances)
            unplaced = sum(
                u["end"] - u["start"] for u in utterances if u["voice"] is None
            )
            move(
                job,
                "review",
                stage="",
                pid=None,
                **progress.finish(job, now()),
                voices=voice_summary(utterances),
                unplaced_seconds=round(unplaced),
                assignment={},
            )
    except Exception as exc:
        traceback.print_exc()
        move(
            job,
            "failed",
            stage="",
            pid=None,
            **progress.finish(job, now()),
            error=f"{type(exc).__name__}: {exc}",
        )
    return job


def separate_again(video_id: str, voices: int, *, launch_worker: bool = True) -> dict:
    """Run a job under review again with a given number of voices. Its transcript
    is reused, and its audio too while it is kept (it is deleted once loaded, and
    then downloaded again); the assignment of voices starts over."""
    job = read_job(video_id)
    if job is None or refresh(job)["state"] != "review":
        raise ValueError("only a job under review can be separated again")
    if not 1 <= voices <= 10:
        raise ValueError("the number of voices must be between 1 and 10")
    job = move(
        job,
        "queued",
        stage="",
        voices_expected=voices,
        voices=None,
        assignment={},
        round=job.get("round", 0) + 1,
        reviewed_at=None,
        error=None,
        pid=None,
        steps={},
        progress=None,
    )
    if launch_worker:
        launch(video_id, "run")
    return job


def add_participant(job: dict, members: dict[str, int], name: str) -> dict:
    """Name one more person who speaks, such as a host, while the voices are
    reviewed: an MP by their roster name, anyone else as typed."""
    if job["state"] != "review":
        raise ValueError("people can be added only while the voices are reviewed")
    is_mp = name in members
    people = participants(
        members,
        [p["name"] for p in job["participants"] if p["mp_id"]]
        + ([name] if is_mp else []),
        [p["name"] for p in job["participants"] if not p["mp_id"]]
        + ([] if is_mp else [name]),
    )
    # participants() puts MPs first; keep the old order, so assigned indexes hold.
    known = {p["name"] for p in job["participants"]}
    added = [p for p in people if p["name"] not in known]
    return update(job, participants=[*job["participants"], *added])


def assign(job: dict, assignment: dict[str, int | None]) -> dict:
    """Record who each voice is: a participant's index, or None for nobody."""
    if job["state"] != "review":
        raise ValueError("voices can be assigned only while they are reviewed")
    voices = {str(v["voice"]) for v in job.get("voices") or []}
    clean: dict[str, int | None] = {}
    for voice, who in assignment.items():
        if str(voice) not in voices:
            raise ValueError(f"there is no voice {voice}")
        if who is not None and not 0 <= who < len(job["participants"]):
            raise ValueError(f"there is no participant {who}")
        clean[str(voice)] = who
    return update(job, assignment=clean, reviewed_at=now())


def queue_load(
    video_id: str,
    assignment: dict[str, int | None],
    *,
    allow_unlimited: bool = False,
    launch_worker: bool = True,
) -> dict:
    """Record who each voice is and load the passages in the background. The job
    is read again first, so a second click, or a page left open, cannot start a
    second load."""
    job = read_job(video_id)
    if job is None or refresh(job)["state"] != "review":
        raise ValueError("only a job under review can be loaded")
    job = move(assign(job, assignment), "loading", stage="", error=None)
    if launch_worker:
        launch(video_id, "load", *(["--allow-unlimited"] if allow_unlimited else []))
    return job


# ----------------------------------------------------------- harvested archive


def harvested() -> list[dict]:
    """The videos the harvest transcribed, with the MPs their metadata names as
    appearing and the date each was published: those naming an MP first, newest
    first. It reads every video's files, so the
    page caches it; review_states() says where each stands."""
    guesses = youtube.read(youtube.ROOT / PARTICIPANTS) or {}
    videos = []
    for video_id, guess in guesses.items():
        meta = youtube.read(youtube.ROOT / "videos" / f"{video_id}.json") or {}
        page = youtube.read(youtube.ROOT / "descriptions" / f"{video_id}.json") or {}
        trimmed = youtube.read(TRIMMED / f"{video_id}.json") or {}
        appearing = guess.get("appearing") or []
        uploaded = page.get("upload_date") or ""
        videos.append(
            {
                "video_id": video_id,
                "url": watch_url(video_id),
                "title": meta.get("title") or page.get("title") or video_id,
                "channel": meta.get("channel") or page.get("channel") or "",
                "published": (
                    f"{uploaded[:4]}-{uploaded[4:6]}-{uploaded[6:8]}"
                    if len(uploaded) == 8
                    else None
                ),
                "duration": meta.get("duration") or page.get("duration"),
                "evidence": guess.get("status") or "",
                "candidates": appearing,
                "mentioned": sorted(
                    {e["mp"] for e in guess.get("evidence") or []} - set(appearing)
                ),
                "transcribed": existing_transcript(video_id) is not None,
                "unusable": bool(trimmed.get("dropped")),
            }
        )
    # Candidates first, newest first within them; undated videos last.
    videos.sort(key=lambda v: v["published"] or "", reverse=True)
    return sorted(videos, key=lambda v: not v["candidates"])


def review_states(videos: list[dict]) -> dict[str, str]:
    """Where each video stands: "todo", its job's state, "no_mp" when a person
    found no MP speaking in it, or "unusable" when trimming dropped its transcript."""
    jobs = {job["video_id"]: job["state"] for job in all_jobs()}
    no_mp = youtube.read(JOBS / NO_MP) or {}
    states = {}
    for video in videos:
        vid = video["video_id"]
        if vid in jobs:
            states[vid] = jobs[vid]
        elif vid in no_mp:
            states[vid] = "no_mp"
        elif video["unusable"]:
            states[vid] = "unusable"
        else:
            states[vid] = "todo"
    return states


def mark_no_mp(video_id: str, note: str = "") -> None:
    """Record that a person checked the video and found no MP speaking in it."""
    job = read_job(video_id)
    if job and not LIFECYCLE.allows(refresh(job)["state"], None):
        raise ValueError("a running or loaded job cannot be marked")
    marked = youtube.read(JOBS / NO_MP) or {}
    marked[video_id] = {"at": now(), "note": note.strip()}
    youtube.save(JOBS / NO_MP, marked)
    if job:
        forget(video_id)


def unmark_no_mp(video_id: str) -> None:
    marked = youtube.read(JOBS / NO_MP) or {}
    if marked.pop(video_id, None) is not None:
        youtube.save(JOBS / NO_MP, marked)


# --------------------------------------------------------------------- loading


def speaker_turns(
    utterances: list[dict], assignment: dict, people: list[dict]
) -> list[dict]:
    """Utterances merged into turns of one assigned person, or of nobody."""
    turns: list[dict] = []
    for u in utterances:
        who = assignment.get(str(u["voice"])) if u["voice"] is not None else None
        if turns and turns[-1]["who"] == who:
            turns[-1]["end"] = u["end"]
            turns[-1]["pieces"].append(u)
        else:
            person = people[who] if who is not None else None
            turns.append(
                {
                    "who": who,
                    "person": person,
                    "start": u["start"],
                    "end": u["end"],
                    "pieces": [u],
                }
            )
    return turns


def turn_chunks(turn: dict, limit: int = MAX_CHARS) -> list[dict]:
    """A turn's text in pieces of at most `limit` characters, split between
    utterances or, inside a long one, at sentence ends; each with its times."""
    chunks: list[dict] = []
    current: dict | None = None
    for piece in turn["pieces"]:
        for text in split_long(piece["text"], limit):
            if current and len(current["text"]) + 1 + len(text) > limit:
                chunks.append(current)
                current = None
            if current is None:
                current = {"start": piece["start"], "end": piece["end"], "text": text}
            else:
                current["text"] += " " + text
                current["end"] = piece["end"]
    if current:
        chunks.append(current)
    return chunks


def heading(job: dict) -> str:
    published = f", paskelbta {job['published']}" if job.get("published") else ""
    channel = job.get("channel") or "YouTube"
    return f"Interviu, {channel}: „{job.get('title') or job['video_id']}“{published}"


def chunk_rows(document_id: int, job: dict, utterances: list[dict]) -> list[tuple]:
    published = date.fromisoformat(job["published"]) if job.get("published") else None
    rows: list[tuple] = []
    kept = [
        u
        for u in utterances
        if u["voice"] is not None or len(u["text"].split()) > CRUMB_WORDS
    ]
    turns = speaker_turns(kept, job.get("assignment") or {}, job["participants"])
    for turn in turns:
        person = turn["person"]
        for piece in turn_chunks(turn):
            if len(piece["text"].split()) < MIN_WORDS:
                continue
            span = f"{clock(piece['start'])}–{clock(piece['end'])}"
            speaker = person["name"] if person else UNKNOWN_SPEAKER
            rows.append(
                (
                    document_id,
                    len(rows),
                    f"{heading(job)}\n[{span}] {speaker}: {piece['text']}",
                    person["name"] if person else None,
                    person["mp_id"] if person else None,
                    None,
                    DOC_TYPE,
                    published,
                    watch_url(job["video_id"], piece["start"]),
                    None,
                    span,
                    None,
                    None,
                )
            )
    return rows


def document_metadata(job: dict) -> dict:
    people = job["participants"]
    assignment = job.get("assignment") or {}
    return {
        "video_id": job["video_id"],
        "channel": job.get("channel"),
        "published": job.get("published"),
        "duration": job.get("duration"),
        "participants": people,
        "voices": [
            {
                "label": v["label"],
                "seconds": v["seconds"],
                "speaker": people[assignment[str(v["voice"])]]["name"]
                if assignment.get(str(v["voice"])) is not None
                else None,
            }
            for v in job.get("voices") or []
        ],
        "unplaced_seconds": job.get("unplaced_seconds"),
        "transcript": job.get("transcript"),
        "speaker_separation": {
            "segmentation": MODEL_FILES["segmentation"]["path"],
            "embedding": MODEL_FILES["embedding"]["path"],
            "window_shift": WINDOW_SHIFT,
            "threshold": CLUSTER_THRESHOLD,
        },
        "attribution": "voices assigned by a person; unassigned or overlapping "
        "speech has no speaker",
        "reviewed_at": job.get("reviewed_at"),
        "linked_videos": job.get("linked_videos") or [],
    }


def delete_document(conn, url: str) -> None:
    """Remove a video's interview and its passages, without relying on a cascade."""
    conn.execute(
        "DELETE FROM chunks WHERE document_id IN "
        "(SELECT id FROM documents WHERE doc_type = %s AND source_url = %s)",
        (DOC_TYPE, url),
    )
    conn.execute(
        "DELETE FROM documents WHERE doc_type = %s AND source_url = %s", (DOC_TYPE, url)
    )


def write_document(conn, job: dict, utterances: list[dict]) -> tuple[int, int]:
    """Replace this video's document and passages in one transaction."""
    published = date.fromisoformat(job["published"]) if job.get("published") else None
    title = f"{job.get('channel') or 'YouTube'}: {job.get('title') or job['video_id']}"
    with conn.transaction():
        delete_document(conn, job["url"])
        document_id = conn.execute(
            "INSERT INTO documents "
            "(doc_type, title, doc_date, term_id, source_url, metadata) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                DOC_TYPE,
                title,
                published,
                config.TERM_ID,
                job["url"],
                Jsonb(document_metadata(job)),
            ),
        ).fetchone()[0]
        rows = chunk_rows(document_id, job, utterances)
        with (
            conn.cursor() as cur,
            cur.copy(f"COPY chunks ({CHUNK_COLUMNS}) FROM STDIN") as copy,
        ):
            for row in rows:
                copy.write_row(row)
    return document_id, len(rows)


def load(
    video_id: str, *, allow_unlimited: bool = False, conn=None, embedder=None
) -> dict:
    """Load a reviewed job's passages, embed them and refresh the BM25 statistics."""
    from rag import embed, retrieval

    job = read_job(video_id)
    if job is None:
        raise SystemExit(f"no job for {video_id}")
    job = refresh(job)
    if owned_elsewhere(job):
        raise ValueError("this video is already being processed")
    if not LIFECYCLE.allows(job["state"], "loading"):
        raise ValueError("only a job under review can be loaded")
    update(job, pid=os.getpid(), error=None, steps={})
    enter(job, "loading", "writing passages")
    own = conn is None
    written = False
    try:
        utterances = youtube.read(job_dir(video_id) / "utterances.json")
        if not utterances:
            raise ValueError("there is no separated transcript; run the job first")
        if not job.get("assignment"):
            raise ValueError("say who each voice is first")
        key = config.OPENROUTER_API_KEY
        options = {}
        if embedder is not None:
            options["embedder"] = embedder
        else:
            if not key:
                raise RuntimeError("OPENROUTER_API_KEY is not set")
            if not allow_unlimited and embed.key_limit(key) is None:
                raise RuntimeError(
                    "the OpenRouter key has no credit limit; confirm to embed anyway"
                )
        conn = conn or psycopg.connect(config.DATABASE_URL, autocommit=True)
        document_id, count = write_document(conn, job, utterances)
        written = True
        update(job, stage="embedding passages")
        report = reporter(job, "passages")
        result = embed.embed_corpus(
            conn,
            model=config.EMBEDDING_MODEL,
            dims=config.EMBEDDING_DIM,
            key=key,
            document_id=document_id,
            progress=lambda done: report(done, count),
            **options,
        )
        update(job, stage="refreshing search statistics", progress=None)
        retrieval.refresh_stats(conn)
        (job_dir(video_id) / "audio.wav").unlink(missing_ok=True)
        return move(
            job,
            "loaded",
            stage="",
            pid=None,
            **progress.finish(job, now()),
            document_id=document_id,
            chunks=count,
            embedding_tokens=result["tokens"],
            loaded_at=now(),
        )
    except Exception as exc:
        traceback.print_exc()
        error = f"{type(exc).__name__}: {exc}"
        if written:  # back under review, the passages leave the knowledge base
            try:
                with conn.transaction():
                    delete_document(conn, job["url"])
                retrieval.refresh_stats(conn)
            except Exception as undo:
                traceback.print_exc()
                error += (
                    f"; its passages could not be taken out ({type(undo).__name__}):"
                    " load it again or remove it"
                )
        return move(
            job,
            "review",
            stage="",
            pid=None,
            **progress.finish(job, now()),
            error=error,
        )
    finally:
        if own and conn is not None:
            conn.close()


def remove(video_id: str, conn=None) -> dict | None:
    """Take a video's passages out of the knowledge base; its review stays."""
    from rag import retrieval

    job = read_job(video_id)
    if job and (refresh(job)["state"] in RUNNING):
        raise ValueError("a running job cannot be removed; wait until it finishes")
    own = conn is None
    conn = conn or psycopg.connect(config.DATABASE_URL, autocommit=True)
    try:
        with conn.transaction():
            delete_document(conn, watch_url(video_id))
        retrieval.refresh_stats(conn)
    finally:
        if own:
            conn.close()
    job = read_job(video_id)
    if job is None:
        return None
    reviewable = (job_dir(video_id) / "utterances.json").exists()
    # Taken out, it is reviewed from scratch: the old assignment is not offered.
    return move(
        job,
        "review" if reviewable else "failed",
        document_id=None,
        chunks=None,
        assignment={},
        reviewed_at=None,
        round=job.get("round", 0) + 1,
    )


def forget(video_id: str) -> None:
    """Delete a job that is neither running nor loaded, with its files."""
    job = read_job(video_id)
    if job and not LIFECYCLE.allows(refresh(job)["state"], None):
        raise ValueError("a running or loaded job cannot be deleted")
    shutil.rmtree(job_dir(video_id), ignore_errors=True)


def clip_bytes(wav: Path, start: float, end: float) -> bytes:
    """The audio between `start` and `end` as a WAV file, for the review page."""
    with wave.open(str(wav)) as source:
        rate = source.getframerate()
        source.setpos(min(int(start * rate), source.getnframes()))
        frames = source.readframes(max(0, int((end - start) * rate)))
        params = source.getparams()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setparams(params)
        out.writeframes(frames)
    return buffer.getvalue()


# ------------------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="command", required=True)
    begin = sub.add_parser("start", help="create a job and run it in the background")
    begin.add_argument("link")
    begin.add_argument(
        "--mp", action="append", default=[], help="an MP who speaks, as on the roster"
    )
    begin.add_argument(
        "--other", action="append", default=[], help="anyone else who speaks"
    )
    begin.add_argument("--voices", type=int, help="how many voices, if known")
    sub.add_parser("run", help="process a job now").add_argument("video_id")
    pairs = sub.add_parser("assign", help="say who each voice is: A=1 B=2 C=-")
    pairs.add_argument("video_id")
    pairs.add_argument("pairs", nargs="+")
    loading = sub.add_parser("load", help="load a reviewed job")
    loading.add_argument("video_id")
    loading.add_argument(
        "--allow-unlimited",
        action="store_true",
        help="accept a key without a credit limit",
    )
    sub.add_parser(
        "remove", help="take a video out of the knowledge base"
    ).add_argument("video_id")
    sub.add_parser("list", help="show the jobs")
    args = ap.parse_args(argv)

    if args.command == "start":
        with psycopg.connect(config.DATABASE_URL) as conn:
            people = participants(roster(conn), args.mp, args.other)
        job = start(args.link, people, voices=args.voices)
        print(f"started {job['video_id']}; follow it with the list command")
    elif args.command == "run":
        job = run(args.video_id)
        print(f"{args.video_id}: {job['state']} {job.get('error') or ''}")
    elif args.command == "assign":
        job = read_job(args.video_id)
        if job is None:
            raise SystemExit(f"no job for {args.video_id}")
        labels = {v["label"]: str(v["voice"]) for v in job.get("voices") or []}
        wanted = {}
        for pair in args.pairs:
            label, _, who = pair.partition("=")
            if label not in labels:
                raise SystemExit(f"no voice {label}; voices: {', '.join(labels)}")
            wanted[labels[label]] = None if who in ("-", "") else int(who) - 1
        try:
            assign(job, wanted)
        except ValueError as exc:
            raise SystemExit(f"{args.video_id}: {exc}") from exc
    elif args.command == "load":
        try:
            job = load(args.video_id, allow_unlimited=args.allow_unlimited)
        except ValueError as exc:
            raise SystemExit(f"{args.video_id}: {exc}") from exc
        print(f"{args.video_id}: {job['state']} {job.get('error') or ''}")
    elif args.command == "remove":
        try:
            remove(args.video_id)
        except ValueError as exc:
            raise SystemExit(f"{args.video_id}: {exc}") from exc
    else:
        for job in all_jobs():
            stage, title = job.get("stage") or "", job.get("title") or job["url"]
            print(f"{job['video_id']}  {job['state']:<12} {stage}  {title}")
            for n, person in enumerate(job["participants"], 1):
                print(f"    {n}. {person['name']}{' (MP)' if person['mp_id'] else ''}")
            for voice in job.get("voices") or []:
                share = f"{voice['share']:.0%}"
                print(f"    voice {voice['label']}: {voice['seconds']} s, {share}")
            if job.get("error"):
                print(f"    error: {job['error']}")


if __name__ == "__main__":
    main()
