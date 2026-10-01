"""Local Apple Silicon fallback for public episodes without Lithuanian captions."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import shutil
import time
from datetime import UTC, datetime

from . import youtube

MODEL_REPO = "mlx-community/whisper-large-v3-turbo"
MODEL_REVISION = "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb"
MAX_STALE_CLAIM_SECONDS = 6 * 3600


def _claim_is_stale(claimed):
    if not claimed:
        return True
    try:
        claimed_at = datetime.fromisoformat(claimed)
    except ValueError:
        return True
    return (datetime.now(UTC) - claimed_at).total_seconds() > MAX_STALE_CLAIM_SECONDS


def _active_asr_is_available(video, args):
    if args.retry:
        return True
    claimed = video.get("asr_claimed_at")
    if not claimed:
        # Legacy workers did not write claims, but they do write asr_status.
        return False
    return _claim_is_stale(claimed)


def target_mp_ids(args):
    ids = set(getattr(args, "target_mp_ids", None) or [])
    if getattr(args, "mp_id", None):
        ids.add(args.mp_id)
    return ids


def selected_mp_ids(args):
    ids = target_mp_ids(args)
    if ids:
        return ids
    name = getattr(args, "mp_name", None)
    if not name:
        return set()
    wanted = youtube.normal(name)
    roster = youtube.read(youtube.ROOT / "roster.json", {"members": []})["members"]
    matches = {m["id"] for m in roster if wanted in youtube.normal(m["name"])}
    if not matches:
        raise ValueError(f"No current MP matches --mp-name {name!r}")
    return matches


def eligible(video, args):
    if args.video and video["video_id"] != args.video:
        return False
    if not video.get("candidate_mp_ids"):
        return False
    ids = target_mp_ids(args)
    if ids and not (ids & set(video.get("candidate_mp_ids", []))):
        return False
    if video.get("transcript_status") in {"downloaded", "generated"}:
        return False
    if video.get("live_status") in {"is_live", "is_upcoming"}:
        return False
    if video.get("transcript_status") == "asr_processing":
        return bool(args.retry) or _claim_is_stale(video.get("asr_claimed_at"))
    if video.get("asr_status") in {"downloading_audio", "transcribing"}:
        return _active_asr_is_available(video, args)
    if video.get("asr_status") == "error" and not args.retry:
        return False
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
    state = youtube.read(status_path(args), {})
    state.update(
        updated_at=youtube.now(),
        video_id=video["video_id"],
        video_stage=video.get("asr_status"),
    )
    youtube.save(status_path(args), state)


def lock_path(args):
    lane = getattr(args, "lane", "main")
    if lane == "legacy":
        return youtube.ROOT / "harvest.lock"
    return youtube.ROOT / f"asr-{lane}.lock"


def status_path(args):
    lane = getattr(args, "lane", "main")
    if lane == "main":
        return youtube.ROOT / "run.json"
    return youtube.ROOT / f"asr-{lane}.run.json"


def ordered_paths(args):
    if getattr(args, "reverse", False):
        return youtube.reverse_prioritized_video_paths()
    return youtube.prioritized_video_paths()


def claim_next(args):
    if getattr(args, "mp_name", None) and not target_mp_ids(args):
        args.target_mp_ids = sorted(selected_mp_ids(args))
    with (youtube.ROOT / "asr_queue.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for path in ordered_paths(args):
            video = youtube.read(path)
            if not eligible(video, args):
                continue
            previous_status = video.get("transcript_status")
            video["transcript_status"] = "asr_processing"
            video["asr_status"] = "claimed"
            video["asr_claimed_at"] = youtube.now()
            video["asr_lane"] = getattr(args, "lane", "main")
            if previous_status:
                video["asr_previous_transcript_status"] = previous_status
            else:
                video.pop("asr_previous_transcript_status", None)
            youtube.save(path, video)
            return path, video
    return None, None


def transcribe(args):
    os.environ.setdefault("HF_HOME", str(youtube.ROOT / "models"))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    from huggingface_hub import snapshot_download
    from yt_dlp import YoutubeDL

    # Use the pinned, inspected model snapshot; no repository code is executed.
    model_path = snapshot_download(
        MODEL_REPO,
        revision=MODEL_REVISION,
        allow_patterns=["config.json", "*.safetensors", "*.npz"],
    )
    import mlx_whisper

    count = 0
    consecutive_errors = 0
    while True:
        if args.limit and count >= args.limit:
            break
        if shutil.disk_usage(youtube.ROOT).free < 5 * 1024**3:
            print("ASR paused: less than 5 GiB free disk space", flush=True)
            return "paused_disk_space"
        path, video = claim_next(args)
        if not path:
            break
        count += 1
        vid = video["video_id"]
        audio_dir = youtube.ROOT / "audio" / vid
        audio_dir.mkdir(parents=True, exist_ok=True)
        try:
            video["asr_status"] = "downloading_audio"
            youtube.save(path, video)
            update_progress(args, video)
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
            with audio.open("rb") as stream:
                audio_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            video["asr_status"] = "transcribing"
            youtube.save(path, video)
            update_progress(args, video)
            print(f"ASR {vid}: {video['title']}", flush=True)
            started = time.monotonic()
            result = mlx_whisper.transcribe(
                str(audio),
                path_or_hf_repo=model_path,
                language="lt",
                task="transcribe",
                verbose=False,
                condition_on_previous_text=False,
                word_timestamps=True,
                hallucination_silence_threshold=2.0,
            )
            raw = [
                {
                    "text": s["text"],
                    "start": s["start"],
                    "duration": s["end"] - s["start"],
                }
                for s in result["segments"]
            ]
            folder = youtube.export_transcript(
                video,
                raw,
                provider="mlx_whisper",
                model=f"{MODEL_REPO}@{MODEL_REVISION}",
            )
            youtube.save(folder / "asr_raw.json", result)
            youtube.save(
                folder / "audio_provenance.json",
                {
                    "source_url": video["url"],
                    "audio_sha256": audio_hash,
                    "model": MODEL_REPO,
                    "model_revision": MODEL_REVISION,
                    "generated_at": youtube.now(),
                    "processing_seconds": round(time.monotonic() - started, 2),
                    "review_status": "unreviewed",
                    "speaker_attribution": "unverified",
                },
            )
            video["asr_status"] = "generated"
            video.pop("asr_claimed_at", None)
            video.pop("asr_previous_transcript_status", None)
            youtube.save(path, video)
            update_progress(args, video)
            consecutive_errors = 0
            # This is only the temporary audio created by this job. Keep the
            # source hash and timestamped transcript, avoiding an audio archive.
            audio.unlink()
        except Exception as exc:
            previous_status = video.pop("asr_previous_transcript_status", None)
            if previous_status:
                video["transcript_status"] = previous_status
            else:
                video.pop("transcript_status", None)
            video.update(asr_status="error", asr_error=type(exc).__name__)
            video.pop("asr_claimed_at", None)
            youtube.save(path, video)
            update_progress(args, video)
            print(f"ASR {vid}: {type(exc).__name__}", flush=True)
            consecutive_errors += 1
            if consecutive_errors >= 3:
                print("ASR paused after three consecutive failures", flush=True)
                youtube.report(args)
                return "paused_errors"
        youtube.report(args)
    return "finished_pass"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--retry", action="store_true")
    parser.add_argument(
        "--pending",
        action="store_true",
        help="Also transcribe untested/blocked-caption candidates from public audio",
    )
    parser.add_argument(
        "--lane",
        default="main",
        help=(
            "Independent local ASR lane name; use lane 2, 3, etc. for parallel workers"
        ),
    )
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="Consume the priority queue from the back for this ASR lane",
    )
    parser.add_argument("--mp-id", help="Only transcribe candidates for this MP ID")
    parser.add_argument(
        "--mp-name",
        help="Only transcribe candidates for a current MP whose name matches this text",
    )
    args = parser.parse_args()
    args.track_run = True
    args.target_mp_ids = sorted(selected_mp_ids(args))
    youtube.ROOT.mkdir(parents=True, exist_ok=True)
    with lock_path(args).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = {
            "pid": os.getpid(),
            "status": "running",
            "phase": "local_speech_recognition",
            "started_at": youtube.now(),
            "updated_at": youtube.now(),
            "include_pending": args.pending,
            "limit": args.limit,
            "lane": args.lane,
            "reverse": args.reverse,
            "target_mp_ids": args.target_mp_ids,
            "target_mp_name": args.mp_name,
        }
        youtube.save(status_path(args), state)
        try:
            status = transcribe(args)
        except BaseException as exc:
            state = youtube.read(status_path(args))
            state.update(
                status="interrupted_or_failed",
                error_type=type(exc).__name__,
                updated_at=youtube.now(),
            )
            youtube.save(status_path(args), state)
            raise
        state = youtube.read(status_path(args))
        state.update(status=status, updated_at=youtube.now())
        youtube.save(status_path(args), state)


if __name__ == "__main__":
    main()
