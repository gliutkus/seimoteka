"""Downloading new Seimas data: the steps and their order, the vote check, the
stenograms of new sittings, one update at a time, the page."""

import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from ingest import interviews, seimas, stenograms, update
from ingest.config import DATABASE_URL
from rag import embed

ROOT = Path(__file__).resolve().parent.parent
BEFORE = {"votes": 5391, "with_results": 3669, "choices": 516708}
AFTER = {"votes": 5433, "with_results": 3691, "choices": 519788}


@pytest.fixture
def files(tmp_path, monkeypatch):
    for name in ("JOB", "LOG", "LOCK", "ACCEPTED"):
        monkeypatch.setattr(update, name, tmp_path / getattr(update, name).name)
    return tmp_path


class Steps:
    """Stands in for the network, the loaders and the embedding provider, and
    records the steps an update runs."""

    def __init__(self, monkeypatch, *, problems=None, found=None, limit=5.0):
        self.calls = []
        self.missing = 0
        records = iter([BEFORE, AFTER])

        def note(name, result=None):
            def step(*args, **kwargs):
                self.calls.append(name)
                return result

            return step

        def load_new(conn):
            self.calls.append("load stenograms")
            self.missing += (found or Counter())["chunks"]
            return found or Counter()

        def embed_corpus(conn, **kwargs):
            self.calls.append("embed")
            done, self.missing = self.missing, 0
            return {"embedded": done, "tokens": 40 * done, "model": "e5@1024"}

        checked = {"rows_compared": AFTER["choices"], "new_problems": problems or {}}
        monkeypatch.setattr(update, "harvest_votes", note("harvest votes"))
        monkeypatch.setattr(update, "check_votes", note("check votes", checked))
        monkeypatch.setattr(update, "load_votes", note("load votes"))
        monkeypatch.setattr(update, "record", lambda conn: next(records))
        monkeypatch.setattr(update, "harvest_stenograms", note("harvest stenograms"))
        monkeypatch.setattr(stenograms, "load_new", load_new)
        monkeypatch.setattr(update, "refresh_search", note("refresh search"))
        monkeypatch.setattr(update, "missing_vectors", lambda conn: self.missing)
        monkeypatch.setattr(update.config, "OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setattr(embed, "key_limit", lambda key: limit)
        monkeypatch.setattr(embed, "embed_corpus", embed_corpus)


NEW_SITTING = Counter({"stenograms added": 1, "chunks": 250})


def test_an_update_checks_and_loads_votes_then_adds_stenograms_and_embeds(
    files, monkeypatch
):
    steps = Steps(monkeypatch, found=NEW_SITTING)
    job = update.run(conn=object())
    assert steps.calls == [
        "harvest votes",
        "check votes",
        "load votes",
        "harvest stenograms",
        "load stenograms",
        "refresh search",
        "embed",
    ]
    assert job["state"] == "done"
    assert (job["before"], job["after"]) == (BEFORE, AFTER)
    assert job["stenograms"] == {"added": 1, "replaced": 0, "chunks": 250}
    assert job["embedding"] == {"embedded": 250, "missing": 0}
    assert job["last_success_at"] == job["finished_at"]
    assert update.read() == job


def test_without_new_stenograms_the_search_statistics_stay(files, monkeypatch):
    steps = Steps(monkeypatch)
    job = update.run(conn=object())
    assert "refresh search" not in steps.calls
    assert "embed" not in steps.calls
    assert job["embedding"] == {"embedded": 0, "missing": 0}


def test_a_vote_that_disagrees_with_its_page_stops_the_update(files, monkeypatch):
    steps = Steps(monkeypatch, problems={"rows: choice disagrees": 2})
    job = update.run(conn=object())
    assert steps.calls == ["harvest votes", "check votes"]
    assert (job["state"], job["failed_step"]) == ("failed", "votes_check")
    assert job["checked"]["new_problems"] == {"rows: choice disagrees": 2}
    assert job["last_success_at"] is None


def test_without_a_credit_limit_passages_wait_for_confirmation(files, monkeypatch):
    steps = Steps(monkeypatch, found=NEW_SITTING, limit=None)
    job = update.run(conn=object())
    assert "embed" not in steps.calls
    assert job["state"] == "done"
    assert job["embedding"] == {
        "embedded": 0,
        "missing": 250,
        "skipped": "no credit limit",
    }
    downloaded = job["last_success_at"]

    # Confirmed, the vectors are made without downloading anything again.
    steps.calls.clear()
    job = update.run("embed", allow_unlimited=True, conn=object())
    assert steps.calls == ["embed"]
    assert job["embedding"] == {"embedded": 250, "missing": 0}
    assert job["last_success_at"] == downloaded


def test_the_check_compares_with_the_counts_behind_the_loaded_record(
    files, monkeypatch, tmp_path
):
    monkeypatch.setattr(seimas, "VERIFY", tmp_path / "seimas_verify.json")
    known = {
        "rows compared": 100,
        "row count differs between page and XML": 3,
        "rows: fraction disagrees": 5,
    }
    seimas.VERIFY.write_text(json.dumps({"counts": known}))
    results = iter(
        [
            # only counts that describe the source, not a disagreement, grew
            {
                **known,
                "rows compared": 120,
                "row count differs between page and XML": 4,
                "rows: fraction disagrees": 9,
            },
            {**known, "rows compared": 130, "rows: choice disagrees": 1},
            {**known, "rows compared": 130, "rows: choice disagrees": 1},
        ]
    )
    monkeypatch.setattr(
        seimas,
        "verify",
        lambda args: seimas.VERIFY.write_text(json.dumps({"counts": next(results)})),
    )
    assert update.check_votes() == {"rows_compared": 120, "new_problems": {}}
    stopped = {"rows_compared": 130, "new_problems": {"rows: choice disagrees": 1}}
    assert update.check_votes() == stopped
    # a check that failed is not the new baseline, so the next one stops too
    assert update.check_votes() == stopped
    accepted = json.loads(update.ACCEPTED.read_text())["counts"]
    assert accepted["rows compared"] == 120


def test_one_update_at_a_time_and_lrs_lt_asked_at_most_hourly(files, monkeypatch):
    launched = []
    monkeypatch.setattr(
        update, "launch", lambda mode, allow: launched.append((mode, allow))
    )
    job = update.start()
    assert (job["state"], launched) == ("queued", [("update", False)])
    with pytest.raises(ValueError, match="already running"):
        update.start()

    succeeded = update.now()
    update.update(job, state="done", last_success_at=succeeded)
    with pytest.raises(ValueError, match="less than an hour"):
        update.start()
    assert update.minutes_to_wait(update.current()) == 60

    # Embedding asks nothing of lrs.lt, so it need not wait.
    update.start("embed", allow_unlimited=True)
    assert launched[-1] == ("embed", True)
    assert update.read()["last_success_at"] == succeeded

    earlier = datetime.now(seimas.VILNIUS) - timedelta(minutes=61)
    update.update(update.read(), state="done", last_success_at=earlier.isoformat())
    update.start()
    assert launched[-1] == ("update", False)


def test_an_update_whose_worker_died_or_never_started_has_failed(files):
    ended = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    update.save({"state": "running", "step": "votes_load", "pid": int(ended.stdout)})
    job = update.current()
    assert (job["state"], job["failed_step"]) == ("failed", "votes_load")

    long_ago = (datetime.now(seimas.VILNIUS) - timedelta(minutes=10)).isoformat()
    update.JOB.write_text(
        json.dumps({"state": "queued", "step": "", "pid": None, "updated_at": long_ago})
    )
    assert update.current()["state"] == "failed"


def test_progress_comes_from_the_status_the_harvest_writes(files, monkeypatch):
    monkeypatch.setattr(seimas, "STATUS", files / "seimas_harvest.json")
    job = {"step": "votes_download", "step_started_at": update.now()}
    assert update.harvest_progress(job) is None
    seimas.STATUS.write_text(json.dumps({"phase": "votes", "vote": 120, "of": 5400}))
    assert update.harvest_progress(job) == ("votes", 120, 5400)
    earlier = datetime.now().timestamp() - 3600
    os.utime(seimas.STATUS, (earlier, earlier))  # left by an earlier harvest
    assert update.harvest_progress(job) is None


# ---------------------------------------------------------------- stenograms


@pytest.fixture
def db():
    try:
        conn = psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("Postgres not reachable")
    schema = f"test_{uuid4().hex[:8]}"
    try:
        conn.execute(f"CREATE SCHEMA {schema}")
        for table in ("documents", "chunks"):
            conn.execute(
                f"CREATE TABLE {schema}.{table} (LIKE public.{table} INCLUDING ALL)"
            )
        conn.execute(f"SET search_path TO {schema}, public")
        yield conn
    finally:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.close()


def sitting(number: int, day: date) -> dict:
    return {
        "id": number,
        "stenogram_url": f"https://e-seimas.lrs.lt/portal/legalAct/lt/TAK/{number:032x}",
        "starts_at": datetime(day.year, day.month, day.day, 10, tzinfo=seimas.VILNIUS),
        "kind": "rytinis",
    }


def stenogram(speech: str) -> str:
    return (
        '<html><body><p class="Laikas"><span>10.00 val.</span></p>'
        '<p class="Roman12"><span>Darbotvarkės klausimas</span></p>'
        f'<p class="Roman"><b>V. PRANCKIETIS</b> (LSF). {speech}</p></body></html>'
    )


def stored(conn, sitting: dict) -> list[tuple]:
    return conn.execute(
        "SELECT c.content, c.embedding IS NOT NULL FROM chunks c "
        "JOIN documents d ON d.id = c.document_id "
        "WHERE d.source_url = %s ORDER BY c.ordinal",
        (sitting["stenogram_url"],),
    ).fetchall()


def test_new_sittings_are_added_and_recent_changed_stenograms_reloaded(db, monkeypatch):
    today = date(2026, 9, 17)
    final = sitting(1, date(2026, 8, 20))  # older than two weeks: final
    draft = sitting(2, date(2026, 9, 15))  # recent: may still be corrected
    new = sitting(3, date(2026, 9, 17))
    unpublished = sitting(4, date(2026, 9, 17))
    vector = "[" + ",".join(["0.1"] * 1024) + "]"
    for loaded, text in ((final, "Galutinis."), (draft, "Juodraštis.")):
        [(document_id,)] = db.execute(
            "INSERT INTO documents (doc_type, title, doc_date, source_url) "
            "VALUES ('stenogram', 'Stenograma', %s, %s) RETURNING id",
            (loaded["starts_at"].date(), loaded["stenogram_url"]),
        ).fetchall()
        db.execute(
            "INSERT INTO chunks (document_id, ordinal, content, doc_type, embedding) "
            "VALUES (%s, 0, %s, 'stenogram', %s::vector)",
            (document_id, f"V. PRANCKIETIS (LSF). {text}", vector),
        )
    cache = {
        stenograms.text_url(final["stenogram_url"]): stenogram("Pakeista vėliau."),
        stenograms.text_url(draft["stenogram_url"]): stenogram("Pataisytas tekstas."),
        stenograms.text_url(new["stenogram_url"]): stenogram("Nauja kalba."),
    }
    monkeypatch.setattr(
        stenograms, "stenogram_sittings", lambda: [final, draft, new, unpublished]
    )
    monkeypatch.setattr(stenograms, "fetch", lambda url, **kwargs: cache.get(url))
    monkeypatch.setattr(stenograms, "mp_rows", lambda conn: [])

    counts = stenograms.load_new(db, today=today)
    assert counts == Counter(
        {
            "stenograms added": 1,
            "stenograms replaced": 1,
            "chunks": 2,
            "sittings whose stenogram is not cached": 1,
        }
    )
    assert stored(db, final) == [("V. PRANCKIETIS (LSF). Galutinis.", True)]
    assert stored(db, draft) == [("V. PRANCKIETIS (LSF). Pataisytas tekstas.", False)]
    assert stored(db, new) == [("V. PRANCKIETIS (LSF). Nauja kalba.", False)]

    again = stenograms.load_new(db, today=today)
    assert again == Counter({"sittings whose stenogram is not cached": 1})


# ---------------------------------------------------------------------- page


RECORD = {
    **BEFORE,
    "last_vote_at": "2026-09-15T16:29:04+03:00",
    "loaded_at": "2026-09-16T23:59:03+03:00",
}


def kb_app(root):
    import sys

    if root not in sys.path:
        sys.path.insert(0, root)
    import kb_page

    kb_page.render(lambda: None)


def test_the_page_starts_an_update_and_shows_what_it_found(files, monkeypatch):
    from streamlit.testing.v1 import AppTest

    if str(ROOT) not in sys.path:
        monkeypatch.syspath_prepend(str(ROOT))
    import kb_page

    launched = []
    missing = {"passages": 0}
    monkeypatch.setattr(
        update, "launch", lambda mode, allow: launched.append((mode, allow))
    )
    monkeypatch.setattr(update, "record", lambda conn: RECORD)
    monkeypatch.setattr(update, "missing_vectors", lambda conn: missing["passages"])
    monkeypatch.setattr(interviews, "JOBS", files / "interviews")
    monkeypatch.setattr(interviews, "roster", lambda conn: {})
    monkeypatch.setattr(kb_page, "key_has_limit", lambda: False)

    def button(app, label):
        return next(b for b in app.button if b.label == label)

    app = AppTest.from_function(kb_app, args=(str(ROOT),), default_timeout=30).run()
    assert not app.exception
    assert any(
        "2026-09-15 16:29" in m.value and "516 708" in m.value for m in app.markdown
    )
    button(app, "Atsisiųsti naujus Seimo duomenis").click().run()
    assert launched == [("update", False)]
    assert update.read()["state"] == "queued"

    update.save(
        {
            **update.read(),
            "state": "done",
            "step": "",
            "finished_at": "2026-09-17T20:05:00+03:00",
            "last_success_at": update.now(),
            "before": BEFORE,
            "after": AFTER,
            "checked": {"rows_compared": 519788, "new_problems": {}},
            "stenograms": {"added": 1, "replaced": 0, "chunks": 250},
            "embedding": {"embedded": 0, "missing": 250, "skipped": "no credit limit"},
        }
    )
    missing["passages"] = 250
    app.run()
    [done] = [s.value for s in app.success]
    assert "Nauji balsavimai: 42, iš jų su Seimo narių balsais: 22" in done
    assert "patikrinta Seimo narių balsų: 519 788" in done
    assert "Naujos stenogramos: 1" in done
    assert button(app, "Atsisiųsti naujus Seimo duomenis").disabled
    assert any("Ištraukų be vektorių: 250" in w.value for w in app.warning)

    button(app, "Sukurti trūkstamus vektorius").click().run()
    assert "Pirmiausia patvirtinkite" in app.error[0].value
    assert launched == [("update", False)]
    app.checkbox[0].check()
    button(app, "Sukurti trūkstamus vektorius").click().run()
    assert launched[-1] == ("embed", True)

    update.save(
        {
            "mode": "update",
            "state": "failed",
            "failed_step": "votes_check",
            "finished_at": "2026-09-17T21:10:00+03:00",
            "checked": {
                "rows_compared": 519788,
                "new_problems": {"rows: choice disagrees": 2},
            },
            "error": "ValueError: votes disagree with their pages",
        }
    )
    app.run()
    [stopped] = [re.sub(r"\\(.)", r"\1", e.value) for e in app.error]  # as shown
    assert "Seimo nario balsas nesutampa su balsavimo puslapiu: 2" in stopped
    assert "Duomenų bazė nepakeista" in stopped


def test_steps_are_timed_and_progress_is_throttled(monkeypatch):
    from ingest import progress

    job = {}
    job.update(progress.begin(job, "votes_download", "2026-09-23T10:00:00+03:00"))
    job.update(progress.begin(job, "votes_check", "2026-09-23T10:02:00+03:00"))
    assert job["steps"]["votes_download"] == {
        "started": "2026-09-23T10:00:00+03:00",
        "finished": "2026-09-23T10:02:00+03:00",
    }
    assert "finished" not in job["steps"]["votes_check"]
    job.update(progress.finish(job, "2026-09-23T10:05:00+03:00"))
    assert job["steps"]["votes_check"]["finished"] == "2026-09-23T10:05:00+03:00"

    saved = []
    clock = iter([100.0, 100.5, 103.0, 103.1])
    monkeypatch.setattr(progress.time, "monotonic", lambda: next(clock))
    report = progress.throttled(saved.append, "passages", every=2.0)
    for done in (1, 2, 3, 10):
        report(done, 10)
    # the first report, one 2 s later, and the last whatever its timing
    assert [s["done"] for s in saved] == [1, 3, 10]
    assert saved[-1] == {"done": 10, "total": 10, "unit": "passages"}
