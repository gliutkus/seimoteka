"""Find new YouTube videos of current MPs for the review page.

    uv run --group youtube python -m ingest.youtube_new run [--limit 150]
    uv run --group youtube python -m ingest.youtube_new status

The page "Papildyti žinių bazę" runs the same steps in the background with the
button "Ieškoti naujų vaizdo įrašų":

1. roster: the current MPs, from lrs.lt (ingest.youtube roster);
2. search: for each MP, YouTube's top results for "<name> pokalbis" searched
   again, and the latest uploads of the channels harvested whole (ingest.youtube
   discover). YouTube ranks search results by relevance, not date, so a new
   upload outside an MP's top results is missed until it ranks higher; the
   channels' uploads are listed newest first;
3. describe: the page of each video found for the first time, for its full
   description and the date it was published, and of the full shows its
   description links to. At most `limit` pages a run, videos whose title names
   an MP first; the rest wait for the next run;
4. guess: ingest.appearances guesses which MPs take part in each described video,
   and the ones naming an MP are added to participants.json. Entries already
   there are kept as they are.

Nothing is transcribed here: the review page transcribes a video when it is
prepared. Every request goes through the harvest's delay, and the run holds the
YouTube harvest lock, so it never overlaps a harvest or an interview download.
One run at a time, at most once an hour. The job is data/youtube/new_videos.json.
"""

from __future__ import annotations

import argparse
import fcntl
import math
import os
import subprocess
import sys
import time
import traceback
from datetime import UTC, datetime, timedelta

from . import appearances, config, progress, youtube

JOB = youtube.ROOT / "new_videos.json"
LOG = youtube.ROOT / "new_videos.log"
LOCK = youtube.ROOT / "harvest.lock"
PARTICIPANTS = youtube.ROOT / "participants.json"
RUNNING = ("queued", "running")
STEPS = ("roster", "search", "describe", "guess")
COOLDOWN = timedelta(hours=1)
STALE_START = 300  # seconds a queued run may wait for its worker to report in
LIMIT = 150  # video pages fetched a run
CHANNELS = (
    "https://www.youtube.com/@OpTV_jusu_tiesa/videos",
    "https://www.youtube.com/@OpTV_jusu_tiesa/streams",
)
CHANNEL_LATEST = 30  # uploads looked at on each channel
RESULTS = 20  # search results for each MP


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------- job


def read() -> dict | None:
    return youtube.read(JOB)


def update(job: dict, **fields) -> dict:
    job.update(fields, updated_at=now())
    youtube.save(JOB, job)
    return job


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def current() -> dict | None:
    """The latest run; one whose worker died, or never started, has failed."""
    job = read()
    if job is None or job["state"] not in RUNNING:
        return job
    if alive(job.get("pid")):
        return job
    waited = datetime.now(UTC) - datetime.fromisoformat(job["updated_at"])
    if not job.get("pid") and waited.total_seconds() < STALE_START:
        return job
    return update(
        job,
        state="failed",
        pid=None,
        finished_at=now(),
        error="the search stopped before it finished",
        **progress.finish(job, now()),
    )


def minutes_to_wait(job: dict | None) -> int:
    if not job or not job.get("last_success_at"):
        return 0
    ready = datetime.fromisoformat(job["last_success_at"]) + COOLDOWN
    left = (ready - datetime.now(UTC)).total_seconds()
    return max(0, math.ceil(left / 60))


def start(*, launch_worker: bool = True) -> dict:
    job = current()
    if job and job["state"] in RUNNING:
        raise ValueError("a search for new videos is already running")
    if minutes_to_wait(job):
        raise ValueError("the last search finished less than an hour ago")
    queued = update(
        {
            "state": "queued",
            "created_at": now(),
            "pid": None,
            "error": None,
            "steps": {},
            "progress": None,
            "last_success_at": (job or {}).get("last_success_at"),
            "waiting": (job or {}).get("waiting") or [],
        }
    )
    if launch_worker:
        with LOG.open("a") as log:
            subprocess.Popen(
                [sys.executable, "-m", "ingest.youtube_new", "run"],
                cwd=config.ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    return queued


def step(job: dict, name: str) -> dict:
    return update(job, **progress.begin(job, name, now()))


def reporter(job: dict, unit: str):
    return progress.throttled(lambda state: update(job, progress=state), unit)


# ------------------------------------------------------------------ steps


def fetch_page(video_id: str) -> dict:
    """A video's page as the harvest saves it in descriptions/."""
    from yt_dlp import YoutubeDL

    options = {
        "skip_download": True,
        "quiet": True,
        "logger": youtube.QuietLogger(),
        "socket_timeout": 25,
        "retries": 1,
        "ignore_no_formats_error": True,
    }
    url = f"https://www.youtube.com/watch?v={video_id}"
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False, process=False)
    return {
        "video_id": video_id,
        "url": url,
        "title": info.get("title") or "",
        "channel": info.get("channel") or info.get("uploader") or "",
        "description": info.get("description") or "",
        "duration": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "fetched_at": now(),
    }


def pending(found: set[str]) -> list[str]:
    """Videos found for the first time and not yet described, those whose title
    or search snippet names an MP first."""
    waiting = []
    for video_id in found:
        if (youtube.ROOT / "descriptions" / f"{video_id}.json").exists():
            continue
        video = youtube.read(youtube.ROOT / "videos" / f"{video_id}.json") or {}
        waiting.append((not video.get("candidate_mp_ids"), video_id))
    return [video_id for _, video_id in sorted(waiting)]


def describe(
    video_ids: list[str], limit: int, report=None, fetch=fetch_page
) -> list[str]:
    """Save the pages of up to `limit` videos, and of the full shows their
    descriptions link to; returns the videos fetched."""
    queue, done = list(video_ids), []
    while queue and len(done) < limit:
        video_id = queue.pop(0)
        path = youtube.ROOT / "descriptions" / f"{video_id}.json"
        if path.exists():
            continue
        if done:
            time.sleep(max(1, config.REQUEST_DELAY))
        try:
            page = fetch(video_id)
        except Exception as exc:  # a private, removed or blocked video
            print(f"{video_id}: {type(exc).__name__}: {exc}", flush=True)
            page = {"video_id": video_id, "error": type(exc).__name__}
        youtube.save(path, page)
        done.append(video_id)
        if report:
            report(len(done), min(limit, len(done) + len(queue)))
        for linked in appearances.LINKED.findall(page.get("description") or ""):
            if linked != video_id and linked not in queue:
                queue.insert(0, linked)
    return done


def guess(video_ids: list[str], members: list[dict]) -> int:
    """Add participants entries for described videos that name an MP; returns
    how many were added. Existing entries are never replaced."""
    known = youtube.read(PARTICIPANTS) or {}
    added = 0
    for video_id in video_ids:
        if video_id in known:
            continue
        page = youtube.read(youtube.ROOT / "descriptions" / f"{video_id}.json")
        if not page or page.get("error"):
            continue
        shows = {}
        for linked in appearances.LINKED.findall(page.get("description") or ""):
            show = youtube.read(youtube.ROOT / "descriptions" / f"{linked}.json")
            if linked in known:
                shows[linked] = known[linked]
            elif show and not show.get("error"):
                shows[linked] = appearances.classify(show, members) or {}
        entry = appearances.classify(page, members, shows)
        if entry:
            known[video_id] = entry
            added += 1
    youtube.save(PARTICIPANTS, known)
    return added


def run(limit: int = LIMIT, *, search=None, fetch=fetch_page) -> dict:
    """Search, describe and guess now, in this process."""
    youtube.ROOT.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as lock:
        job = read() or {}
        if job.get("state") not in RUNNING:  # started from the command line
            job = {
                "created_at": now(),
                "last_success_at": job.get("last_success_at"),
                "waiting": job.get("waiting") or [],
            }
        job = update(job, state="running", pid=os.getpid(), error=None, steps={})
        try:
            update(job, stage="waiting for another YouTube download to finish")
            fcntl.flock(lock, fcntl.LOCK_EX)
            update(job, stage="")
            before = {p.stem for p in (youtube.ROOT / "videos").glob("*.json")}
            step(job, "roster")
            youtube.roster(
                argparse.Namespace(
                    cached=False, as_of=datetime.now(UTC).date().isoformat()
                )
            )
            members = youtube.read(youtube.ROOT / "roster.json")["members"]
            step(job, "search")
            (search or searched)(reporter(job, "searches"))
            found = {p.stem for p in (youtube.ROOT / "videos").glob("*.json")} - before
            waiting = sorted(set(job.get("waiting") or []) | found)
            step(job, "describe")
            queue = pending(set(waiting))
            fetched = describe(queue, limit, reporter(job, "pages"), fetch)
            step(job, "guess")
            # full shows fetched for a clip are candidates of their own too
            added = guess(list(dict.fromkeys(waiting + fetched)), members)
            left = pending(set(waiting))
            finished = now()
            return update(
                job,
                state="done",
                pid=None,
                finished_at=finished,
                last_success_at=finished,
                found=len(found),
                described=len(fetched),
                added=added,
                waiting=left,
                **progress.finish(job, finished),
            )
        except BaseException as exc:
            traceback.print_exc()
            failed = now()
            update(
                job,
                state="failed",
                pid=None,
                finished_at=failed,
                error=f"{type(exc).__name__}: {exc}",
                **progress.finish(job, failed),
            )
            if not isinstance(exc, Exception):
                raise
            return job


def searched(report) -> None:
    """Each MP's newest search results, then the channels' latest uploads."""
    youtube.discover(
        argparse.Namespace(
            channel=[],
            search_mps=True,
            results=RESULTS,
            limit=None,
            refresh=True,
            progress=report,
        )
    )
    youtube.discover(
        argparse.Namespace(
            channel=list(CHANNELS),
            search_mps=False,
            results=RESULTS,
            limit=CHANNEL_LATEST,
            refresh=True,
        )
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    go = sub.add_parser("run", help="search for new videos now")
    go.add_argument("--limit", type=int, default=LIMIT, help="video pages to fetch")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    if args.command == "run":
        job = run(args.limit)
        print(
            {k: job.get(k) for k in ("state", "found", "described", "added", "error")}
        )
        print(f"waiting to be described: {len(job.get('waiting') or [])}")
    else:
        print(current())


if __name__ == "__main__":
    main()
