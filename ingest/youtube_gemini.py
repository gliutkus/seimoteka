"""Paid Gemini transcription lane for candidates at the back of the queue."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import mimetypes
import os
import shutil
import subprocess
import time

from . import youtube

DEFAULT_MODEL = "gemini-3.5-transcribe"
DEFAULT_CHUNK_SECONDS = 29 * 60
MAX_STALE_CLAIM_SECONDS = 6 * 3600


def eligible(video, args):
    if args.video and video["video_id"] != args.video:
        return False
    if not video.get("candidate_mp_ids"):
        return False
    if video.get("transcript_status") in {"downloaded", "generated"}:
        return False
    if video.get("live_status") in {"is_live", "is_upcoming"}:
        return False
    if (
        video.get("transcript_status") == "unavailable"
        and video.get("transcript_error") != "NoTranscriptFound"
    ):
        return False
    if video.get("gemini_status") == "error" and not args.retry:
        return False
    if video.get("transcript_status") == "gemini_processing":
        claimed = video.get("gemini_claimed_at")
        if not claimed or args.retry:
            return True
        try:
            claimed_at = youtube.datetime.fromisoformat(claimed)
            age = youtube.datetime.now(youtube.UTC) - claimed_at
        except ValueError:
            return True
        return age.total_seconds() > MAX_STALE_CLAIM_SECONDS
    if args.video:
        return True
    if video.get("transcript_error") in {"NoTranscriptFound", "TranscriptsDisabled"}:
        return True
    return bool(getattr(args, "pending", False)) and (
        not video.get("transcript_status")
        or video.get("transcript_status") == "blocked"
    )


def update_progress(args, video):
    if not getattr(args, "track_run", False):
        return
    state = youtube.read(youtube.ROOT / "gemini_run.json", {})
    state.update(
        updated_at=youtube.now(),
        video_id=video["video_id"],
        video_stage=video.get("gemini_status"),
    )
    youtube.save(youtube.ROOT / "gemini_run.json", state)


def _duration_arg(path):
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(completed.stdout.strip())


def _split_audio(audio, chunk_dir, chunk_seconds):
    chunk_dir.mkdir(parents=True, exist_ok=True)
    pattern = chunk_dir / "chunk-%05d.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(audio),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "64k",
            "-f",
            "segment",
            "-segment_time",
            str(chunk_seconds),
            "-reset_timestamps",
            "1",
            str(pattern),
        ],
        check=True,
    )
    chunks = sorted(chunk_dir.glob("chunk-*.mp3"))
    if not chunks:
        raise RuntimeError("ffmpeg did not produce any Gemini audio chunks")
    return chunks


def _seconds(value):
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    if text.endswith("s"):
        text = text[:-1]
    return float(text)


def _field(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _annotations(interaction):
    words = []
    for step in _field(interaction, "steps", []) or []:
        for content in _field(step, "content", []) or []:
            for annotation in _field(content, "annotations", []) or []:
                if _field(annotation, "type") == "word_info":
                    start = _seconds(_field(annotation, "start_offset"))
                    end = _seconds(_field(annotation, "end_offset"))
                    text = _field(annotation, "text", "")
                    if text and start is not None and end is not None and end >= start:
                        words.append(
                            {
                                "text": text,
                                "start": start,
                                "end": end,
                                "speaker": _field(annotation, "speaker"),
                            }
                        )
    return words


def _output_text(interaction):
    value = _field(interaction, "output_text")
    if callable(value):
        return value()
    if value:
        return value
    value = _field(interaction, "outputText")
    if callable(value):
        return value()
    return value or ""


def _segments_from_words(words, offset):
    if not words:
        return []
    raw = []
    group = []
    group_start = words[0]["start"]
    last_end = words[0]["end"]
    speaker = words[0].get("speaker")
    for word in words:
        text = word["text"]
        pause = word["start"] - last_end
        changed_speaker = word.get("speaker") and word.get("speaker") != speaker
        too_long = word["end"] - group_start >= 18
        sentence_end = group and group[-1].rstrip().endswith((".", "?", "!", ":"))
        if group and (changed_speaker or pause > 1.4 or too_long or sentence_end):
            raw.append(
                {
                    "text": " ".join(group),
                    "start": offset + group_start,
                    "duration": max(0.1, last_end - group_start),
                }
            )
            group = []
            group_start = word["start"]
            speaker = word.get("speaker")
        group.append(text)
        last_end = word["end"]
    if group:
        raw.append(
            {
                "text": " ".join(group),
                "start": offset + group_start,
                "duration": max(0.1, last_end - group_start),
            }
        )
    return raw


def _fallback_segments(text, offset, duration):
    paragraphs = [p.strip() for p in text.splitlines() if p.strip()]
    if not paragraphs:
        paragraphs = [text.strip()]
    paragraphs = [p for p in paragraphs if p]
    if not paragraphs:
        raise ValueError("Gemini returned an empty transcript")
    step = max(0.1, duration / len(paragraphs))
    return [
        {"text": paragraph, "start": offset + index * step, "duration": step}
        for index, paragraph in enumerate(paragraphs)
    ]


def _transcribe_chunk(client, chunk, args):
    mime_type = mimetypes.guess_type(chunk)[0] or "audio/mp3"
    uploaded = client.files.upload(file=str(chunk))
    try:
        interaction = client.interactions.create(
            model=args.model,
            input=[
                {
                    "type": "audio",
                    "uri": uploaded.uri,
                    "mime_type": getattr(uploaded, "mime_type", mime_type),
                }
            ],
            generation_config={
                "transcription_config": {
                    "language_codes": ["lt-LT"],
                    "mode": {
                        "type": "verbatim",
                        "timestamp_granularities": ["word"],
                    },
                }
            },
        )
    finally:
        delete = getattr(client.files, "delete", None)
        if delete and getattr(uploaded, "name", None):
            try:
                delete(name=uploaded.name)
            except Exception:
                pass
    return interaction


def _download_audio(video, audio_dir):
    from yt_dlp import YoutubeDL

    audio_dir.mkdir(parents=True, exist_ok=True)
    with YoutubeDL(
        {
            "format": "bestaudio/best",
            "noplaylist": True,
            "outtmpl": str(audio_dir / "audio.%(ext)s"),
            "quiet": True,
            "logger": youtube.QuietLogger(),
            "socket_timeout": 25,
            "retries": 1,
            "extractor_retries": 1,
            "fragment_retries": 1,
            "concurrent_fragment_downloads": 1,
            "sleep_interval_requests": max(1, youtube.REQUEST_DELAY),
            "max_filesize": 250 * 1024**2,
            "cachedir": str(youtube.ROOT / "extractor_cache"),
        }
    ) as ydl:
        info = ydl.extract_info(video["url"], download=True)
        audio = audio_dir / ("audio." + info["ext"])
    if not audio.exists():
        raise RuntimeError("Audio download missing or exceeded size limit")
    return audio


def transcribe(args):
    if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
        raise RuntimeError("Set GEMINI_API_KEY or GOOGLE_API_KEY before using Gemini")
    from google import genai

    client = genai.Client()
    paths = (
        youtube.reverse_prioritized_video_paths()
        if args.reverse
        else youtube.prioritized_video_paths()
    )
    count = 0
    consecutive_errors = 0
    for path in paths:
        video = youtube.read(path)
        if not eligible(video, args):
            continue
        if args.limit and count >= args.limit:
            break
        if shutil.disk_usage(youtube.ROOT).free < 5 * 1024**3:
            print("Gemini paused: less than 5 GiB free disk space", flush=True)
            return "paused_disk_space"
        count += 1
        vid = video["video_id"]
        previous_status = video.get("transcript_status")
        video.update(
            transcript_status="gemini_processing",
            gemini_status="downloading_audio",
            gemini_claimed_at=youtube.now(),
            gemini_model=args.model,
        )
        youtube.save(path, video)
        update_progress(args, video)
        audio_dir = youtube.ROOT / "audio" / f"gemini-{vid}"
        chunk_dir = audio_dir / "chunks"
        started = time.monotonic()
        try:
            audio = _download_audio(video, audio_dir)
            with audio.open("rb") as stream:
                audio_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            video["gemini_status"] = "splitting_audio"
            youtube.save(path, video)
            update_progress(args, video)
            chunks = _split_audio(audio, chunk_dir, args.chunk_seconds)
            raw = []
            raw_response = []
            offset = 0.0
            video["gemini_status"] = "transcribing"
            youtube.save(path, video)
            update_progress(args, video)
            print(f"Gemini {vid}: {video['title']}", flush=True)
            for chunk in chunks:
                duration = _duration_arg(chunk)
                interaction = _transcribe_chunk(client, chunk, args)
                words = _annotations(interaction)
                text = _output_text(interaction)
                raw_response.append(
                    {
                        "chunk": chunk.name,
                        "offset_seconds": offset,
                        "duration_seconds": duration,
                        "text": text,
                        "word_count": len(words),
                    }
                )
                raw.extend(
                    _segments_from_words(words, offset)
                    or _fallback_segments(text, offset, duration)
                )
                offset += duration
            folder = youtube.export_transcript(
                video,
                raw,
                provider="gemini_transcribe",
                model=args.model,
            )
            youtube.save(folder / "gemini_raw.json", raw_response)
            youtube.save(
                folder / "gemini_provenance.json",
                {
                    "source_url": video["url"],
                    "audio_sha256": audio_hash,
                    "model": args.model,
                    "generated_at": youtube.now(),
                    "chunk_seconds": args.chunk_seconds,
                    "chunks": len(chunks),
                    "processing_seconds": round(time.monotonic() - started, 2),
                    "review_status": "unreviewed",
                    "speaker_attribution": "unverified",
                },
            )
            video["gemini_status"] = "generated"
            video.pop("gemini_error", None)
            youtube.save(path, video)
            update_progress(args, video)
            consecutive_errors = 0
            shutil.rmtree(audio_dir, ignore_errors=True)
        except Exception as exc:
            if previous_status:
                video["transcript_status"] = previous_status
            else:
                video.pop("transcript_status", None)
            video.update(gemini_status="error", gemini_error=type(exc).__name__)
            youtube.save(path, video)
            update_progress(args, video)
            print(f"Gemini {vid}: {type(exc).__name__}", flush=True)
            consecutive_errors += 1
            if consecutive_errors >= 3:
                print("Gemini paused after three consecutive failures", flush=True)
                youtube.report(args)
                return "paused_errors"
        youtube.report(args)
    return "finished_pass"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--retry", action="store_true")
    parser.add_argument("--pending", action="store_true")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--chunk-seconds", type=int, default=DEFAULT_CHUNK_SECONDS)
    parser.add_argument("--front", dest="reverse", action="store_false")
    parser.set_defaults(reverse=True)
    args = parser.parse_args()
    args.track_run = True
    youtube.ROOT.mkdir(parents=True, exist_ok=True)
    with (youtube.ROOT / "gemini.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = {
            "pid": os.getpid(),
            "status": "running",
            "phase": "gemini_transcription",
            "started_at": youtube.now(),
            "updated_at": youtube.now(),
            "include_pending": args.pending,
            "limit": args.limit,
            "model": args.model,
            "reverse": args.reverse,
        }
        youtube.save(youtube.ROOT / "gemini_run.json", state)
        try:
            status = transcribe(args)
        except BaseException as exc:
            state = youtube.read(youtube.ROOT / "gemini_run.json")
            state.update(
                status="interrupted_or_failed",
                error_type=type(exc).__name__,
                updated_at=youtube.now(),
            )
            youtube.save(youtube.ROOT / "gemini_run.json", state)
            raise
        state = youtube.read(youtube.ROOT / "gemini_run.json")
        state.update(status=status, updated_at=youtube.now())
        youtube.save(youtube.ROOT / "gemini_run.json", state)


if __name__ == "__main__":
    main()
