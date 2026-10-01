"""Download new Seimas data: votes, then the stenograms of new sittings.

    uv run python -m ingest.update run                     # now, in this process
    uv run python -m ingest.update run --allow-unlimited   # embed without a limit
    uv run python -m ingest.update embed                   # only missing embeddings
    uv run python -m ingest.update status

The page "Papildyti žinių bazę" (kb_page.py, shown with KB_UPDATES=1) runs the
same steps in the background with one button:

1. download new votes, sittings and agendas (ingest.seimas harvest);
2. compare every vote with its page (ingest.seimas verify): a disagreement the
   checks behind the loaded data did not have stops the update, and the database
   is not changed;
3. load the vote record, in one transaction (ingest.seimas load);
4. download stenograms, add the new sittings' ones and reload recent ones that
   changed (ingest.stenograms load --new), then refresh the search statistics;
5. embed the passages without vectors, unless the OpenRouter key has no credit
   limit and nobody confirmed: until then they are found by their words only.

One update runs at a time. Every run asks lrs.lt again for its recent listings,
so a new one may start only an hour after the last successful one. The job is
data/seimas_update.json, and the output of a background run data/seimas_update.log.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import subprocess
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
import requests

from . import config, progress, seimas, stenograms

JOB = config.DATA_DIR / "seimas_update.json"
LOG = config.DATA_DIR / "seimas_update.log"
LOCK = config.DATA_DIR / "seimas_update.lock"
# The verify counts behind the vote record in the database.
ACCEPTED = config.DATA_DIR / "seimas_verify_accepted.json"
RUNNING = ("queued", "running")
STEPS = (
    "votes_download",
    "votes_check",
    "votes_load",
    "stenograms_download",
    "stenograms_load",
    "search_stats",
    "embedding",
)
UNCHANGED_BEFORE = ("", "votes_download", "votes_check", "votes_load")
COOLDOWN = timedelta(hours=1)
STALE_START = 300  # seconds a queued update may wait for its worker to report in
# Verify counts that must not grow: a member's choice or a tally that differs from
# the vote page, or a vote whose page is missing or has no table of members.
BLOCKING = (
    "rows: choice disagrees",
    "page tally != XML tally",
    "XML rows absent from page",
    "XML: unknown kaip_balsavo value",
    "vote page missing from cache",
    "vote page without an individual table",
)
RECORD_SQL = """
    SELECT count(*), count(*) FILTER (WHERE has_individual_results),
           (SELECT count(*) FROM mp_votes), max(voted_at),
           (SELECT value FROM dataset_meta WHERE key = 'loaded_at')
    FROM votes
"""


def now() -> str:
    return datetime.now(seimas.VILNIUS).isoformat(timespec="seconds")


# ---------------------------------------------------------------------- job


def read() -> dict | None:
    try:
        return json.loads(JOB.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def save(job: dict) -> dict:
    job["updated_at"] = now()
    JOB.parent.mkdir(parents=True, exist_ok=True)
    temporary = JOB.with_suffix(".tmp")
    temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(JOB)  # the page never reads half a file
    return job


def update(job: dict, **fields) -> dict:
    job.update(fields)
    return save(job)


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
    """The latest update; one whose worker died, or never started, has failed."""
    job = read()
    if job is None or job["state"] not in RUNNING:
        return job
    if job.get("pid"):
        if alive(job["pid"]):
            return job
    else:
        waited = datetime.now(seimas.VILNIUS) - datetime.fromisoformat(
            job["updated_at"]
        )
        if waited.total_seconds() < STALE_START:
            return job
    return update(
        job,
        state="failed",
        failed_step=job.get("step", ""),
        step="",
        pid=None,
        finished_at=now(),
        error="the update stopped before it finished",
    )


def minutes_to_wait(job: dict | None) -> int:
    """Minutes until lrs.lt may be asked again: an hour after the last success."""
    if not job or not job.get("last_success_at"):
        return 0
    ready = datetime.fromisoformat(job["last_success_at"]) + COOLDOWN
    left = (ready - datetime.now(seimas.VILNIUS)).total_seconds()
    return max(0, math.ceil(left / 60))


def start(mode: str = "update", *, allow_unlimited: bool = False) -> dict:
    """Queue an update ("update") or the missing embeddings alone ("embed") and
    start its worker in the background."""
    job = current()
    if job and job["state"] in RUNNING:
        raise ValueError("an update is already running")
    if mode == "update" and minutes_to_wait(job):
        raise ValueError("the last update finished less than an hour ago")
    queued = save(
        {
            "mode": mode,
            "state": "queued",
            "step": "",
            "allow_unlimited": allow_unlimited,
            "created_at": now(),
            "pid": None,
            "error": None,
            "last_success_at": (job or {}).get("last_success_at"),
        }
    )
    launch(mode, allow_unlimited)
    return queued


def launch(mode: str, allow_unlimited: bool) -> None:
    """Run the update in its own process, so it outlives the page that started it."""
    command = "run" if mode == "update" else "embed"
    extra = ["--allow-unlimited"] if allow_unlimited else []
    log = LOG.open("a")
    subprocess.Popen(
        [sys.executable, "-m", "ingest.update", command, *extra],
        cwd=config.ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def harvest_progress(job: dict) -> tuple[str, int, int] | None:
    """(phase, done, of) of a download step, from its harvest's status file."""
    path = {
        "votes_download": seimas.STATUS,
        "stenograms_download": stenograms.STATUS,
    }.get(job.get("step"))
    if path is None or not job.get("step_started_at"):
        return None
    try:
        if (
            path.stat().st_mtime
            < datetime.fromisoformat(job["step_started_at"]).timestamp()
        ):
            return None
        state = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    for phase, key in (
        ("stenograms", "stenogram"),
        ("votes", "vote"),
        ("agendas", "sitting"),
    ):
        if isinstance(state.get(key), int) and isinstance(state.get("of"), int):
            return phase, state[key], state["of"]
    return None


# --------------------------------------------------------------------- steps


def record(conn) -> dict:
    """The size of the vote record, its latest vote and when it was loaded."""
    votes, with_results, choices, last, loaded = conn.execute(RECORD_SQL).fetchone()
    return {
        "votes": votes,
        "with_results": with_results,
        "choices": choices,
        "last_vote_at": last.isoformat() if last else None,
        "loaded_at": loaded,
    }


def missing_vectors(conn) -> int:
    from rag import embed

    return embed.pending(conn)[0]


def verify_counts(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text("utf-8"))["counts"]
    except (OSError, ValueError, KeyError):
        return None


def harvest_votes() -> None:
    seimas.harvest(argparse.Namespace(all_pages=False))


def check_votes() -> dict:
    """Compare every vote with its page, and name the disagreements the checks
    behind the loaded record did not have. Passing, the counts become that
    baseline."""
    baseline = verify_counts(ACCEPTED) or verify_counts(seimas.VERIFY) or {}
    seimas.verify(argparse.Namespace())
    counts = verify_counts(seimas.VERIFY) or {}
    new = {
        key: counts.get(key, 0) - baseline.get(key, 0)
        for key in BLOCKING
        if counts.get(key, 0) > baseline.get(key, 0)
    }
    if not new:
        ACCEPTED.write_text(json.dumps({"counts": counts}, indent=2), "utf-8")
    return {"rows_compared": counts.get("rows compared", 0), "new_problems": new}


def load_votes() -> None:
    seimas.load(argparse.Namespace())


def harvest_stenograms() -> None:
    stenograms.harvest(argparse.Namespace())


def refresh_search(conn) -> None:
    from rag import retrieval

    retrieval.refresh_stats(conn)


def embed_passages(conn, allow_unlimited: bool, report=None) -> dict:
    """Embed the passages without vectors; what was done, or why it was not.
    `report(done, total)` follows the embedding."""
    from rag import embed

    missing = missing_vectors(conn)
    key = config.OPENROUTER_API_KEY
    if not missing:
        return {"embedded": 0, "missing": 0}
    if not key:
        return {"embedded": 0, "missing": missing, "skipped": "no key"}
    if not allow_unlimited:
        try:
            limit = embed.key_limit(key)
        except requests.RequestException:
            return {"embedded": 0, "missing": missing, "skipped": "limit unknown"}
        if limit is None:
            return {"embedded": 0, "missing": missing, "skipped": "no credit limit"}
    result = embed.embed_corpus(
        conn,
        model=config.EMBEDDING_MODEL,
        dims=config.EMBEDDING_DIM,
        key=key,
        progress=(lambda done: report(done, missing)) if report else None,
    )
    return {"embedded": result["embedded"], "missing": missing_vectors(conn)}


def step(job: dict, name: str) -> dict:
    when = now()
    return update(
        job, step=name, step_started_at=when, **progress.begin(job, name, when)
    )


def reporter(job: dict, unit: str):
    """A (done, total) callback that records the running step's progress."""
    return progress.throttled(lambda state: update(job, progress=state), unit)


def download(job: dict, conn) -> None:
    job = step(job, "votes_download")
    update(job, before=record(conn))
    harvest_votes()
    step(job, "votes_check")
    checked = check_votes()
    update(job, checked=checked)
    if checked["new_problems"]:
        raise ValueError("votes disagree with their pages")
    step(job, "votes_load")
    load_votes()
    update(job, after=record(conn))
    step(job, "stenograms_download")
    harvest_stenograms()
    step(job, "stenograms_load")
    counts = stenograms.load_new(conn)
    added = counts["stenograms added"]
    replaced = counts["stenograms replaced"]
    update(
        job,
        stenograms={"added": added, "replaced": replaced, "chunks": counts["chunks"]},
    )
    if added or replaced:
        step(job, "search_stats")
        refresh_search(conn)


def run(mode: str = "update", *, allow_unlimited: bool = False, conn=None) -> dict:
    """Run an update ("update") or only the missing embeddings ("embed") now."""
    lock = LOCK.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise SystemExit("another update is running") from exc
    job = read() or {}
    if job.get("state") not in RUNNING:  # started from the command line
        job = {"created_at": now(), "last_success_at": job.get("last_success_at")}
    job = update(
        job,
        mode=mode,
        state="running",
        step="",
        pid=os.getpid(),
        allow_unlimited=allow_unlimited,
        started_at=now(),
        error=None,
        steps={},
        progress=None,
    )
    own = conn is None
    try:
        conn = conn or psycopg.connect(config.DATABASE_URL, autocommit=True)
        if mode == "update":
            download(job, conn)
        step(job, "embedding")
        try:
            embedding = embed_passages(conn, allow_unlimited, reporter(job, "passages"))
        except Exception as exc:  # the provider: the data itself is loaded
            if mode != "update":
                raise
            traceback.print_exc()
            embedding = {
                "embedded": 0,
                "missing": missing_vectors(conn),
                "error": f"{type(exc).__name__}: {exc}",
            }
        finished = now()
        return update(
            job,
            state="done",
            step="",
            pid=None,
            finished_at=finished,
            **progress.finish(job, finished),
            embedding=embedding,
            last_success_at=finished
            if mode == "update"
            else job.get("last_success_at"),
        )
    except (Exception, SystemExit) as exc:
        traceback.print_exc()
        failed = now()
        return update(
            job,
            state="failed",
            failed_step=job.get("step", ""),
            step="",
            pid=None,
            finished_at=failed,
            **progress.finish(job, failed),
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if own and conn is not None:
            conn.close()
        lock.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="command", required=True)
    for name, text in (
        ("run", "download new data, check it and load it"),
        ("embed", "embed the passages that have no vectors"),
    ):
        sub.add_parser(name, help=text).add_argument(
            "--allow-unlimited",
            action="store_true",
            help="embed even if the OpenRouter key has no credit limit",
        )
    sub.add_parser("status", help="show the latest update")
    args = ap.parse_args(argv)
    if args.command == "status":
        print(json.dumps(current(), ensure_ascii=False, indent=2))
        return
    mode = "update" if args.command == "run" else "embed"
    job = run(mode, allow_unlimited=args.allow_unlimited)
    print(json.dumps(job, ensure_ascii=False, indent=2))
    if job["state"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
