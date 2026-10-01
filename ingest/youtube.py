"""Resumable public-caption harvest. Files are staging data, not attributed RAG.

uv run --group youtube python -m ingest.youtube roster
uv run --group youtube python -m ingest.youtube discover --channel URL
uv run --group youtube python -m ingest.youtube discover --search-mps
uv run --group youtube python -m ingest.youtube captions --limit 100
uv run --group youtube python -m ingest.youtube report
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import time
import unicodedata
from collections import Counter, deque
from datetime import UTC, date, datetime
from itertools import islice
from xml.etree import ElementTree as ET

import requests

from .config import DATA_DIR, REQUEST_DELAY, SAMPLES_DIR, TERM_ID, service_url
from .http import fetch

ROOT = DATA_DIR / "youtube"


def now():
    return datetime.now(UTC).isoformat()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def current_members(xml, as_of):
    result = []
    for element in ET.fromstring(xml).iter("SeimoNarys"):
        m = element.attrib
        start, end = m.get("data_nuo"), m.get("data_iki")
        if not start or start > as_of or (end and end < as_of):
            continue
        result.append(
            {
                "id": m["asmens_id"],
                "name": m["vardas"] + " " + m["pavardė"],
                "surname": m["pavardė"],
                "mandate_from": start,
                "mandate_to": end or None,
            }
        )
    if not result:
        raise ValueError("No current MPs parsed; refusing an empty roster")
    return result


def roster(args):
    url = service_url("ad_seimo_nariai", {"kadencijos_id": TERM_ID})
    if args.cached:
        body = (SAMPLES_DIR / "ad_seimo_nariai.xml").read_text()
    else:
        body = fetch(url, force=True)
    if not body:
        raise RuntimeError("Roster refresh failed; use --cached explicitly if needed")
    members = current_members(body, args.as_of)
    save(
        ROOT / "roster.json",
        {
            "as_of": args.as_of,
            "source_url": url,
            "checked_at": now(),
            "cached_sample": args.cached,
            "members": members,
        },
    )
    print(f"Saved {len(members)} current MPs", flush=True)


def normal(text):
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.findall(r"\w+", text))


def surname_forms(surname):
    """Declined forms of a surname, diacritics already folded by normal().

    Lithuanian surnames inflect by case, and the -is/-ys classes alternate the
    stem consonant before an -i ending: Zemaitaitis -> Zemaitaicio, Kuodis ->
    Kuodzio. Generating the forms and matching whole words is both wider and
    narrower than prefix-stemming: it finds Zemaitaicio, and it stops balsavimas
    from looking like Balsys.
    """
    forms = set()
    for part in normal(surname).split():
        forms.add(part)
        if part.endswith("ius"):
            stem = part[:-3]
            forms |= {stem + e for e in ("ius", "iaus", "iui", "iu", "iumi")}
        elif part.endswith("as"):
            stem = part[:-2]
            forms |= {stem + e for e in ("as", "o", "ui", "a", "u", "e")}
        elif part.endswith("ys"):
            stem = part[:-2]
            forms |= {stem + e for e in ("ys", "io", "iui", "i", "iu")}
        elif part.endswith("us"):
            stem = part[:-2]
            forms |= {stem + e for e in ("us", "aus", "ui", "u", "umi")}
        elif part.endswith("is"):
            stem = part[:-2]
            # t -> c and d -> dz before the -i endings (c/dz here are the
            # diacritic-folded spellings of c-caron and dz-caron).
            if stem.endswith("t"):
                alt = stem[:-1] + "c"
            elif stem.endswith("d"):
                alt = stem[:-1] + "dz"
            else:
                alt = stem
            forms |= {stem + "is", stem + "i"}
            forms |= {alt + e for e in ("io", "iui", "iu")}
        elif part.endswith("e"):
            stem = part[:-1]
            forms |= {stem + e for e in ("e", "es", "ei")}
        elif part.endswith("a"):
            stem = part[:-1]
            forms |= {stem + e for e in ("a", "os", "ai", "as")}
    return forms


def candidate_matches(text, members):
    """Broad discovery only. A matched surname NEVER asserts identity."""
    words = set(normal(text).split())
    return [mp["id"] for mp in members if surname_forms(mp["surname"]) & words]


def rematch(args):
    members = read(ROOT / "roster.json")["members"]
    for path in (ROOT / "videos").glob("*.json"):
        video = read(path)
        matches = candidate_matches(
            video.get("title", "") + " " + video.get("description_excerpt", ""),
            members,
        )
        if sorted(matches) != video.get("candidate_mp_ids"):
            video["candidate_mp_ids"] = sorted(matches)
            save(path, video)


def prioritized_video_paths():
    """Long, named-guest candidates first, round-robin across the whole roster.

    This is only processing priority, never confirmation of an appearance.
    Remaining candidates are retained after the priority pass.
    """
    members = read(ROOT / "roster.json", {"members": []})["members"]
    names = [(m["id"], normal(m["name"])) for m in members]
    buckets = {mp_id: deque() for mp_id, _ in names}
    remaining = []
    for path in sorted((ROOT / "videos").glob("*.json")):
        video = read(path)
        title = " " + normal(video.get("title", "")) + " "
        matched = [mp_id for mp_id, name in names if " " + name + " " in title]
        if (video.get("duration") or 0) >= 1200 and matched:
            for mp_id in matched:
                buckets[mp_id].append(path)
        else:
            remaining.append(path)
    seen = set()
    while any(buckets.values()):
        for bucket in buckets.values():
            if bucket:
                path = bucket.popleft()
                if path not in seen:
                    seen.add(path)
                    yield path
    yield from remaining


def reverse_prioritized_video_paths():
    """Consume the same queue from the back for a second, independent worker."""
    yield from reversed(list(prioritized_video_paths()))


class QuietLogger:
    def debug(self, message):
        pass

    def warning(self, message):
        print("YouTube warning:", message, flush=True)

    def error(self, message):
        print("YouTube error:", message, flush=True)


def discover(args):
    from yt_dlp import YoutubeDL

    members = read(ROOT / "roster.json")["members"]
    targets = [(url, None) for url in args.channel]
    if args.search_mps:
        targets += [
            (f"ytsearch{args.results}:{m['name']} pokalbis", m["id"]) for m in members
        ]
    followed = getattr(args, "progress", None)  # (done, total) after each target
    opts = {
        "extract_flat": True,
        "skip_download": True,
        "quiet": True,
        "logger": QuietLogger(),
        "socket_timeout": 25,
        "retries": 1,
        "extractor_retries": 1,
        "sleep_interval_requests": max(1, REQUEST_DELAY),
        "cachedir": str(ROOT / "extractor_cache"),
        "ignoreerrors": False,
        "lazy_playlist": True,
    }
    if args.limit:
        opts["playlistend"] = args.limit
    for number, (target, searched_mp) in enumerate(targets):
        if followed:
            followed(number, len(targets))
        key = hashlib.sha256(target.encode()).hexdigest()[:20]
        checkpoint = ROOT / "discovery" / f"{key}.json"
        previous = read(checkpoint, {})
        if (
            previous.get("status") == "complete"
            and previous.get("limit") == args.limit
            and not args.refresh
        ):
            continue
        count = 0
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(target, download=False, process=False)
                if info is None:
                    raise RuntimeError("Empty discovery response")
                entries = info.get("entries", [])
                if args.limit:
                    entries = islice(entries, args.limit)
                for entry in entries:
                    if not entry or not re.fullmatch(r"[\w-]{11}", entry.get("id", "")):
                        continue
                    vid = entry["id"]
                    path = ROOT / "videos" / f"{vid}.json"
                    old = read(path, {})
                    title = entry.get("title") or old.get("title", "")
                    description = entry.get("description") or ""
                    matched = candidate_matches(title + " " + description, members)
                    evidence = old.get("discovery", [])
                    item = {"target": target, "searched_mp_id": searched_mp}
                    if item not in evidence:
                        evidence.append(item)
                    old.update(
                        {
                            "video_id": vid,
                            "url": f"https://www.youtube.com/watch?v={vid}",
                            "title": title,
                            "description_excerpt": description,
                            "channel": entry.get("channel")
                            or info.get("channel")
                            or entry.get("uploader")
                            or old.get("channel"),
                            "channel_id": entry.get("channel_id")
                            or info.get("channel_id")
                            or old.get("channel_id"),
                            "duration": entry.get("duration") or old.get("duration"),
                            "live_status": entry.get("live_status"),
                            "candidate_mp_ids": sorted(
                                set(old.get("candidate_mp_ids", [])) | set(matched)
                            ),
                            "appearance_status": old.get(
                                "appearance_status", "unverified"
                            ),
                            "speaker_attribution": "unverified",
                            "discovery": evidence,
                            "discovered_at": old.get("discovered_at", now()),
                        }
                    )
                    save(path, old)
                    count += 1
                    if count % 100 == 0:
                        print(f"{target}: {count} videos staged", flush=True)
            save(
                checkpoint,
                {
                    "target": target,
                    "mp_id": searched_mp,
                    "status": "complete",
                    "count": count,
                    "limit": args.limit,
                    "checked_at": now(),
                },
            )
            print(f"Discovery {target}: {count}", flush=True)
        except Exception as exc:
            save(
                checkpoint,
                {
                    "target": target,
                    "mp_id": searched_mp,
                    "status": "error",
                    "count": count,
                    "error_type": type(exc).__name__,
                    "checked_at": now(),
                },
            )
            print(f"Discovery stopped: {type(exc).__name__}", flush=True)
            break
    report(args)


class PoliteSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.last_request = 0.0

    def request(self, *args, **kwargs):
        time.sleep(
            max(0, max(1, REQUEST_DELAY) - (time.monotonic() - self.last_request))
        )
        kwargs.setdefault("timeout", 25)
        try:
            return super().request(*args, **kwargs)
        finally:
            self.last_request = time.monotonic()


def validated_segments(raw):
    result = []
    for ordinal, s in enumerate(raw):
        start, duration = float(s["start"]), float(s["duration"])
        if not all(math.isfinite(v) and v >= 0 for v in (start, duration)):
            raise ValueError("Invalid caption timing")
        if not isinstance(s["text"], str):
            raise ValueError("Invalid caption text")
        result.append(
            {
                "ordinal": ordinal,
                "start": start,
                "end": start + duration,
                "text": s["text"],
                "speaker_mp_id": None,
            }
        )
    if not result or not any(s["text"].strip() for s in result):
        raise ValueError("Empty caption track")
    return result


def timestamp(seconds):
    total = round(seconds * 1000)
    hours, rest = divmod(total, 3600000)
    minutes, rest = divmod(rest, 60000)
    sec, ms = divmod(rest, 1000)
    return f"{hours:02}:{minutes:02}:{sec:02}.{ms:03}"


def export_transcript(
    video,
    raw,
    *,
    language="lt",
    is_generated=True,
    provider="youtube_captions",
    model=None,
):
    vid = video["video_id"]
    segments = validated_segments(raw)
    digest = hashlib.sha256(json.dumps(raw, ensure_ascii=False).encode()).hexdigest()
    version = digest[:16] if provider == "youtube_captions" else "asr-" + digest[:16]
    folder = ROOT / "transcripts" / vid / version
    save(
        folder / "transcript.json",
        {
            "video_id": vid,
            "source_url": video["url"],
            "title": video["title"],
            "language": language,
            "is_generated": is_generated,
            "provider": provider,
            "model": model,
            "retrieved_at": now(),
            "content_sha256": digest,
            "appearance_status": "unverified",
            "speaker_attribution": "unverified",
            "segments": segments,
        },
    )
    save(folder / "raw.json", raw)
    vtt = (
        "WEBVTT\n\n"
        + "\n\n".join(
            f"{timestamp(s['start'])} --> {timestamp(s['end'])}\n{s['text']}"
            for s in segments
        )
        + "\n"
    )
    (folder / "transcript.vtt").write_text(vtt)
    (folder / "transcript.txt").write_text(
        "\n".join(f"[{timestamp(s['start'])}] {s['text']}" for s in segments) + "\n"
    )
    video.update(
        {
            "transcript_status": "downloaded"
            if provider == "youtube_captions"
            else "generated",
            "transcript_path": str((folder / "transcript.json").relative_to(ROOT)),
            "segment_count": len(segments),
            "transcript_end_seconds": max(s["end"] for s in segments),
        }
    )
    video.pop("transcript_error", None)
    print(f"Saved {vid}: {len(segments)} segments — {video['title']}", flush=True)
    return folder


def captions(args):
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import (
        NoTranscriptFound,
        RequestBlocked,
        TranscriptsDisabled,
        VideoUnavailable,
        VideoUnplayable,
    )

    api = YouTubeTranscriptApi(http_client=PoliteSession())
    attempted = 0
    stopped_on_block = False
    paths = prioritized_video_paths()
    for path in paths:
        video = read(path)
        if getattr(args, "video", None) and video["video_id"] != args.video:
            continue
        if not video.get("candidate_mp_ids"):
            continue
        if video.get("live_status") in {"is_live", "is_upcoming"}:
            continue
        status = video.get("transcript_status")
        if status in {"downloaded", "generated"} or (status and not args.retry):
            continue
        if args.limit and attempted >= args.limit:
            break
        attempted += 1
        vid = video["video_id"]
        try:
            tracks = api.list(vid)
            video["available_tracks"] = [
                {"language": t.language_code, "generated": t.is_generated}
                for t in tracks
            ]
            track = tracks.find_transcript(["lt"])
            raw = track.fetch().to_raw_data()
            export_transcript(
                video,
                raw,
                language=track.language_code,
                is_generated=track.is_generated,
            )
        except Exception as exc:
            if isinstance(exc, RequestBlocked):
                video["transcript_status"] = "blocked"
            elif isinstance(
                exc,
                (
                    NoTranscriptFound,
                    TranscriptsDisabled,
                    VideoUnavailable,
                    VideoUnplayable,
                ),
            ):
                video["transcript_status"] = "unavailable"
            else:
                video["transcript_status"] = "error"
            video["transcript_error"] = type(exc).__name__
            print(f"Caption {vid}: {type(exc).__name__}", flush=True)
            if isinstance(exc, RequestBlocked):
                video["transcript_checked_at"] = now()
                save(path, video)
                print(
                    "Stopping on provider block; remaining queue preserved", flush=True
                )
                stopped_on_block = True
                break
        video["transcript_checked_at"] = now()
        save(path, video)
        if attempted % 20 == 0:
            report(args)
    report(args)
    return stopped_on_block


def report(args):
    roster_data = read(ROOT / "roster.json", {"members": []})
    videos = [read(p) for p in sorted((ROOT / "videos").glob("*.json"))]
    candidates = [v for v in videos if v.get("candidate_mp_ids")]
    done = [
        v
        for v in candidates
        if v.get("transcript_status") in {"downloaded", "generated"}
    ]
    searches = [read(p) for p in (ROOT / "discovery").glob("*.json")]
    coverage = []
    for mp in roster_data["members"]:
        matches = [v for v in candidates if mp["id"] in v["candidate_mp_ids"]]
        coverage.append(
            {
                "mp_id": mp["id"],
                "name": mp["name"],
                "candidate_videos": len(matches),
                "transcripts": sum(
                    v.get("transcript_status") in {"downloaded", "generated"}
                    for v in matches
                ),
            }
        )
    result = {
        "updated_at": now(),
        "roster_as_of": roster_data.get("as_of"),
        "current_mps": len(coverage),
        "mps_searched": len(
            {
                s["mp_id"]
                for s in searches
                if s.get("mp_id") and s.get("status") == "complete"
            }
        ),
        "discovered_videos": len(videos),
        "candidate_videos": len(candidates),
        "saved_transcripts": len(done),
        "downloaded_transcripts": sum(
            v.get("transcript_status") == "downloaded" for v in done
        ),
        "generated_transcripts": sum(
            v.get("transcript_status") == "generated" for v in done
        ),
        "captioned_hours_approx": round(
            sum(v.get("transcript_end_seconds", 0) for v in done) / 3600, 2
        ),
        "statuses": dict(
            Counter(v.get("transcript_status", "pending") for v in candidates)
        ),
        "coverage": coverage,
        "limitation": "Candidate matches are not verified appearances or speakers. "
        "Search and caption coverage are incomplete; these are unreviewed captions.",
    }
    save(ROOT / "report.json", result)
    lines = [
        "# YouTube transcript harvest",
        "",
        f"Updated: {result['updated_at']}",
        "",
        f"Current MPs: {len(coverage)}; MPs searched: {result['mps_searched']}; "
        f"videos discovered: {len(videos)}; candidates: {len(candidates)}; "
        f"transcripts saved: {len(done)}.",
        "",
        result["limitation"],
        "",
        "## Saved transcripts",
        "",
    ]
    for v in done:
        transcript_file = ROOT / v["transcript_path"]
        label = v.get("title", v["video_id"]).replace("[", "(").replace("]", ")")
        lines.append(
            f"- {label} — [video]({v['url']}) · "
            f"[text](<{transcript_file.with_name('transcript.txt')}>) · "
            f"[JSON](<{transcript_file}>)"
        )
    lines += [
        "",
        "## Coverage by current MP",
        "",
        "Counts below are candidate matches, not verified appearances.",
        "",
        "| MP | Candidate videos | Saved transcripts |",
        "| --- | ---: | ---: |",
    ]
    lines += [
        f"| {m['name']} | {m['candidate_videos']} | {m['transcripts']} |"
        for m in coverage
    ]
    (ROOT / "INDEX.md").write_text("\n".join(lines) + "\n")
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "coverage"}, ensure_ascii=False
        ),
        flush=True,
    )


def harvest(args):
    """Run serially; persist phase and results so a stopped run is resumable."""
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / "harvest.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another harvest is running") from exc
        state = {"pid": os.getpid(), "started_at": now(), "status": "running"}

        def phase(name):
            state.update(phase=name, updated_at=now())
            save(ROOT / "run.json", state)
            print(f"Harvest phase: {name}", flush=True)

        try:
            if args.wait_pid:
                phase("waiting_for_existing_discovery")
                state["wait_pid"] = args.wait_pid
                save(ROOT / "run.json", state)
                deadline = time.monotonic() + 3600
                while True:
                    try:
                        os.kill(args.wait_pid, 0)
                    except ProcessLookupError:
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError(
                            "Existing discovery did not exit in one hour"
                        )
                    time.sleep(10)
            phase("roster")
            roster(argparse.Namespace(cached=False, as_of=date.today().isoformat()))
            phase("all_current_mp_searches")
            discover(
                argparse.Namespace(
                    channel=[], search_mps=True, results=20, limit=None, refresh=False
                )
            )
            phase("optv_archive")
            discover(
                argparse.Namespace(
                    channel=[
                        "https://www.youtube.com/@OpTV_jusu_tiesa/videos",
                        "https://www.youtube.com/@OpTV_jusu_tiesa/streams",
                    ],
                    search_mps=False,
                    results=20,
                    limit=None,
                    refresh=False,
                )
            )
            phase("captions")
            rematch(args)
            blocked = captions(argparse.Namespace(limit=0, retry=False))
            asr_status = None
            if args.asr and not blocked:
                from .youtube_asr import transcribe

                phase("local_speech_recognition")
                asr_status = transcribe(
                    argparse.Namespace(video=None, limit=0, retry=False)
                )
            state["status"] = (
                "paused_provider_block" if blocked else asr_status or "finished_pass"
            )
        except BaseException as exc:
            state.update(status="interrupted_or_failed", error_type=type(exc).__name__)
            raise
        finally:
            state["updated_at"] = now()
            save(ROOT / "run.json", state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("roster")
    p.add_argument("--as-of", default=date.today().isoformat())
    p.add_argument("--cached", action="store_true")
    p.set_defaults(func=roster)
    p = sub.add_parser("discover")
    p.add_argument("--channel", action="append", default=[])
    p.add_argument("--search-mps", action="store_true")
    p.add_argument("--results", type=int, default=20)
    p.add_argument("--limit", type=int)
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=discover)
    p = sub.add_parser("captions")
    p.add_argument("--video")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--retry", action="store_true")
    p.set_defaults(func=captions)
    p = sub.add_parser("report")
    p.set_defaults(func=report)
    p = sub.add_parser("match")
    p.set_defaults(func=rematch)
    p = sub.add_parser("harvest")
    p.add_argument("--asr", action="store_true")
    p.add_argument("--wait-pid", type=int)
    p.set_defaults(func=harvest)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
