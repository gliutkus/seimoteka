"""Adding a YouTube interview: links, people, voices, turns, jobs, loading, the page."""

import io
import json
import sys
import wave
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from ingest import interviews, update
from ingest.config import DATABASE_URL
from rag import agent as bot

ROOT = Path(__file__).resolve().parent.parent
VIDEO = "2u6em3f1PoE"
MEMBERS = {"Aidas Gedvilas": 47855, "Remigijus Žemaitaitis": 65703}
PEOPLE = [
    {"name": "Remigijus Žemaitaitis", "mp_id": 65703},
    {"name": "Rūta Janutienė", "mp_id": None},
]
JOB = {
    "video_id": VIDEO,
    "url": f"https://www.youtube.com/watch?v={VIDEO}",
    "title": "Tai pasakodamas Žemaitaitis nebesitvardė",
    "channel": "OpTV",
    "published": "2025-11-20",
    "participants": PEOPLE,
    "assignment": {"0": 0, "1": 1, "2": None},
}


def words(*items):
    return [{"text": text, "start": start, "end": end} for text, start, end in items]


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(interviews, "JOBS", tmp_path / "interviews")
    return tmp_path / "interviews"


@pytest.fixture
def db():
    try:
        conn = psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("Postgres not reachable")
    schema = f"test_{uuid4().hex[:8]}"
    try:
        conn.execute(f"CREATE SCHEMA {schema}")
        tables = ("documents", "chunks", "lexeme_stats", "corpus_stats", "dataset_meta")
        for table in tables:
            conn.execute(
                f"CREATE TABLE {schema}.{table} (LIKE public.{table} INCLUDING ALL)"
            )
        conn.execute(f"SET search_path TO {schema}, public")
        yield conn
    finally:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.close()


# ------------------------------------------------------------------ links, people


def test_the_video_id_comes_from_every_link_form_and_nothing_else():
    for link in (
        f"https://www.youtube.com/watch?v={VIDEO}",
        f"https://youtube.com/watch?v={VIDEO}&t=42s",
        f"https://m.youtube.com/watch?feature=share&v={VIDEO}",
        f"https://youtu.be/{VIDEO}?si=abc",
        f"https://www.youtube.com/live/{VIDEO}?feature=share",
        f"https://youtube.com/shorts/{VIDEO}",
        f"  https://www.youtube.com/embed/{VIDEO}  ",
    ):
        assert interviews.video_id_of(link) == VIDEO
    for link in (
        f"https://example.com/watch?v={VIDEO}",
        "https://www.youtube.com/@OpTV_jusu_tiesa",
        "https://www.youtube.com/watch?v=short",
        "javascript:alert(1)",
        f"youtube.com/watch?v={VIDEO}",
    ):
        with pytest.raises(ValueError):
            interviews.video_id_of(link)


def test_a_passage_link_opens_the_moment_it_quotes():
    assert interviews.watch_url(VIDEO) == f"https://www.youtube.com/watch?v={VIDEO}"
    assert interviews.watch_url(VIDEO, 125.7).endswith(f"?v={VIDEO}&t=125s")


def test_people_are_mps_chosen_from_the_roster_or_typed_names():
    people = interviews.participants(
        MEMBERS, ["Remigijus Žemaitaitis"], ["Rūta  Janutienė", " "]
    )
    assert people == PEOPLE


@pytest.mark.parametrize(
    ("mps", "others"),
    [
        (["Remigijus Žemaitaitis "], []),  # not the roster's spelling
        ([], ["Aidas Gedvilas"]),  # an MP typed as someone else would lose the MP
        ([], ["<b>Vedėjas</b>"]),
        ([], ["Rūta Janutienė", "rūta janutienė"]),
        ([], []),
    ],
)
def test_people_who_could_be_misattributed_are_refused(mps, others):
    with pytest.raises(ValueError):
        interviews.participants(MEMBERS, mps, others)


# --------------------------------------------------------------------- voices


def test_words_take_the_voice_covering_them_and_overlaps_belong_to_nobody():
    turns = [(0.0, 5.0, 0), (4.5, 9.0, 1), (12.0, 14.0, 0), (20.0, 30.0, 1)]
    result = interviews.utterances_from(
        words(
            ("Labas", 0.5, 1.0),
            ("vakaras", 1.1, 1.6),
            ("abu", 4.6, 4.9),  # both voices speak
            ("kalba", 5.2, 5.8),
            ("pauzė", 10.2, 10.4),  # in no voice's turn, and none within reach
            ("vėl", 12.2, 12.5),
            ("pabaiga", 14.2, 14.4),  # just after a turn of the same voice
            ("vienas", 20.1, 20.4),
            ("du", 25.0, 25.3),  # the same voice after a long silence
        ),
        turns,
    )
    assert result == [
        {"voice": 0, "start": 0.5, "end": 1.6, "text": "Labas vakaras"},
        {"voice": None, "start": 4.6, "end": 4.9, "text": "abu"},
        {"voice": 1, "start": 5.2, "end": 5.8, "text": "kalba"},
        {"voice": None, "start": 10.2, "end": 10.4, "text": "pauzė"},
        {"voice": 0, "start": 12.2, "end": 14.4, "text": "vėl pabaiga"},
        {"voice": 1, "start": 20.1, "end": 20.4, "text": "vienas"},
        {"voice": 1, "start": 25.0, "end": 25.3, "text": "du"},
    ]


def test_voices_are_lettered_by_talk_time_with_lines_to_recognise_them():
    utterances = [
        {
            "voice": 3,
            "start": 0.0,
            "end": 10.0,
            "text": "Laba diena, šiandien studijoje mūsų svečias yra Seimo narys.",
        },
        {
            "voice": 7,
            "start": 10.0,
            "end": 70.0,
            "text": "Ačiū, kad pakvietėte, pakalbėsime apie biudžetą ir mokesčius.",
        },
        {"voice": None, "start": 70.0, "end": 71.0, "text": "abu"},
        {
            "voice": 3,
            "start": 71.0,
            "end": 80.0,
            "text": "O kodėl balsavote prieš šį įstatymo projektą praėjusią savaitę?",
        },
    ]
    summary = interviews.voice_summary(utterances)
    assert [(v["label"], v["voice"], v["seconds"], v["share"]) for v in summary] == [
        ("A", 7, 60, 0.759),
        ("B", 3, 19, 0.241),
    ]
    assert summary[1]["first"] == 0.0
    assert [s["start"] for s in summary[1]["samples"]] == [0.0, 71.0]


def test_the_transcript_is_cleaned_like_the_harvested_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(interviews, "TRIMMED", tmp_path)
    fine = {"avg_logprob": -0.2, "compression_ratio": 1.2}
    segments = [
        {"text": " Labas vakaras", **fine},
        {"text": " šnypštimas", "avg_logprob": -1.4, "compression_ratio": 1.1},
        {"text": " Привет всем", **fine},
        {"text": " ačiū", **fine},
        {"text": " ačiū", **fine},
        {"text": " ačiū", **fine},
        {"text": "   ", **fine},
    ]
    assert interviews.kept_segments(VIDEO, "asr-1", segments) == [0, 3]
    trimmed = {
        "source_version": "asr-1",
        "dropped": None,
        "segments": [{"ordinal": 5}, {"ordinal": 0}],
    }
    (tmp_path / f"{VIDEO}.json").write_text(json.dumps(trimmed), "utf-8")
    assert interviews.kept_segments(VIDEO, "asr-1", segments) == [0, 5]
    assert interviews.kept_segments(VIDEO, "asr-2", segments) == [0, 3]


def test_review_clips_are_cut_from_the_audio(tmp_path):
    path = tmp_path / "audio.wav"
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16_000)
        out.writeframes(bytes(64_000))  # two seconds
    with wave.open(io.BytesIO(interviews.clip_bytes(path, 0.5, 1.5))) as clip:
        assert (clip.getframerate(), clip.getnframes()) == (16_000, 16_000)


# ------------------------------------------------------------------ passages


def test_turns_name_only_assigned_voices_and_link_to_their_moment():
    utterances = [
        {"voice": 1, "start": 40.6, "end": 78.7, "text": "O ką jie jums pasakoja?"},
        {"voice": 0, "start": 78.3, "end": 139.1, "text": "Žmonės labai nusivylę."},
        {"voice": 0, "start": 139.9, "end": 188.3, "text": "Sutikau moterį."},
        {"voice": 2, "start": 201.5, "end": 201.8, "text": "jo"},
        {
            "voice": None,
            "start": 265.3,
            "end": 266.6,
            "text": "ranko galit ir dar kažką",
        },
        {
            "voice": 1,
            "start": 3725.0,
            "end": 3730.0,
            "text": "Ačiū, kad šiandien pas mus atėjote.",
        },
    ]
    rows = interviews.chunk_rows(7, JOB, utterances)
    url = f"https://www.youtube.com/watch?v={VIDEO}"
    assert [(r[1], r[3], r[4], r[8], r[10]) for r in rows] == [
        (0, "Rūta Janutienė", None, f"{url}&t=40s", "0:00:40–0:01:18"),
        (1, "Remigijus Žemaitaitis", 65703, f"{url}&t=78s", "0:01:18–0:03:08"),
        (2, None, None, f"{url}&t=201s", "0:03:21–0:04:26"),
        (3, "Rūta Janutienė", None, f"{url}&t=3725s", "1:02:05–1:02:10"),
    ]
    assert rows[1][2] == (
        "Interviu, OpTV: „Tai pasakodamas Žemaitaitis nebesitvardė“, paskelbta "
        "2025-11-20\n[0:01:18–0:03:08] Remigijus Žemaitaitis: Žmonės labai nusivylę. "
        "Sutikau moterį."
    )
    assert rows[2][2].endswith("] Nenustatytas kalbėtojas: jo ranko galit ir dar kažką")
    assert {(r[0], r[6], r[7]) for r in rows} == {
        (7, "youtube_interview", date(2025, 11, 20))
    }


def test_a_long_turn_is_split_between_utterances_within_the_limit():
    pieces = [
        {
            "voice": 0,
            "start": float(10 * i),
            "end": float(10 * i + 9),
            "text": " ".join([f"Sakinys numeris {i}."] * 20),
        }
        for i in range(5)
    ]
    [turn] = interviews.speaker_turns(pieces, {"0": 0}, PEOPLE)
    chunks = interviews.turn_chunks(turn, limit=900)
    assert len(chunks) == 3
    assert all(len(c["text"]) <= 900 for c in chunks)
    assert (chunks[0]["start"], chunks[-1]["end"]) == (0.0, 49.0)
    assert " ".join(c["text"] for c in chunks) == " ".join(p["text"] for p in pieces)


# ----------------------------------------------------------------------- jobs


def test_a_video_cannot_be_started_twice(jobs):
    job = interviews.start(f"https://youtu.be/{VIDEO}", PEOPLE, launch_worker=False)
    assert job["state"] == "queued" and (jobs / VIDEO / "job.json").exists()
    with pytest.raises(ValueError, match="already being processed"):
        interviews.start(f"https://www.youtube.com/watch?v={VIDEO}", PEOPLE)


def test_a_job_whose_worker_died_or_never_started_stops_waiting(jobs):
    job = interviews.start(f"https://youtu.be/{VIDEO}", PEOPLE, launch_worker=False)
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    (jobs / VIDEO / "job.json").write_text(json.dumps({**job, "updated_at": old}))
    [seen] = interviews.all_jobs()
    assert seen["state"] == "failed" and "stopped" in seen["error"]

    interviews.update(seen, state="separating", pid=4_194_311, error=None)
    assert interviews.all_jobs()[0]["state"] == "failed"


def test_voices_go_only_to_the_people_named(jobs):
    job = interviews.start(f"https://youtu.be/{VIDEO}", PEOPLE, launch_worker=False)
    voices = [{"voice": 0, "label": "A"}, {"voice": 2, "label": "B"}]
    job = interviews.update(job, state="review", voices=voices)
    with pytest.raises(ValueError):
        interviews.assign(job, {"1": 0})
    with pytest.raises(ValueError):
        interviews.assign(job, {"0": 2})
    assert interviews.assign(job, {"0": 1, "2": None})["assignment"] == {
        "0": 1,
        "2": None,
    }


def fake_steps(monkeypatch, info):
    monkeypatch.setattr(interviews, "fetch_info", lambda url: info)

    def download(url, folder, report=None):
        report(5, 5)
        (folder / "download.webm").write_bytes(b"audio")
        return folder / "download.webm"

    def transcribe(job, wav, report=None):
        report(900, 900)
        return heard, {"reused": True}

    def separate(wav, voices=None, progress=None):
        progress(2, 2)
        return [(0.0, 5.0, 0), (5.5, 9.0, 1)]

    monkeypatch.setattr(interviews, "download_audio", download)
    monkeypatch.setattr(
        interviews, "to_wav", lambda source, target: target.write_bytes(b"wav")
    )
    heard = words(("Labas", 0.5, 1.5), ("vakaras", 6.0, 6.5))
    monkeypatch.setattr(interviews, "transcript_words", transcribe)
    monkeypatch.setattr(interviews, "separate_voices", separate)


def test_the_worker_leaves_the_job_ready_for_review(jobs, monkeypatch):
    monkeypatch.setattr(interviews.youtube, "ROOT", jobs.parent)
    interviews.start(f"https://youtu.be/{VIDEO}", PEOPLE, launch_worker=False)
    info = {
        "title": "Pokalbis",
        "channel": "OpTV",
        "upload_date": "20251120",
        "duration": 552,
        "description": "Visa laida yra čia: https://youtube.com/live/sGcKpamctCk",
    }
    fake_steps(monkeypatch, info)
    job = interviews.run(VIDEO)
    assert job["state"] == "review", job["error"]
    assert (job["published"], job["linked_videos"], job["pid"]) == (
        "2025-11-20",
        ["sGcKpamctCk"],
        None,
    )
    assert [(v["label"], v["voice"]) for v in job["voices"]] == [("A", 0), ("B", 1)]
    assert list(job["steps"]) == ["downloading", "transcribing", "separating"]
    assert all(s["started"] <= s["finished"] for s in job["steps"].values())
    assert job["progress"] is None
    assert not (jobs / VIDEO / "download.webm").exists()
    saved = json.loads((jobs / VIDEO / "utterances.json").read_text("utf-8"))
    assert [u["text"] for u in saved] == ["Labas", "vakaras"]


def test_a_live_broadcast_is_refused(jobs, monkeypatch):
    monkeypatch.setattr(interviews.youtube, "ROOT", jobs.parent)
    interviews.start(f"https://youtu.be/{VIDEO}", PEOPLE, launch_worker=False)
    fake_steps(monkeypatch, {"live_status": "is_live"})
    job = interviews.run(VIDEO)
    assert job["state"] == "failed" and "live" in job["error"]


# -------------------------------------------------------------------- loading


def reviewed_job(jobs, utterances, assignment):
    job = interviews.start(f"https://youtu.be/{VIDEO}", PEOPLE, launch_worker=False)
    (jobs / VIDEO / "utterances.json").write_text(json.dumps(utterances), "utf-8")
    (jobs / VIDEO / "audio.wav").write_bytes(b"RIFF")
    voices = [
        {"voice": 0, "label": "A", "seconds": 61},
        {"voice": 1, "label": "B", "seconds": 38},
    ]
    job = interviews.update(
        job,
        state="review",
        title="Pokalbis",
        channel="OpTV",
        published="2025-11-20",
        voices=voices,
    )
    if assignment:
        interviews.assign(job, assignment)
    return job


def test_a_reviewed_interview_is_loaded_embedded_and_removed(db, jobs):
    # The id comes from the sequence the loader uses too: a fixed id collides with
    # the loader's on a fresh database, whose sequence starts at 1.
    [stenogram] = db.execute(
        "INSERT INTO documents (doc_type, title, source_url) "
        "VALUES ('stenogram', 'Posėdis', 'https://example.test/1') RETURNING id"
    ).fetchone()
    db.execute(
        "INSERT INTO chunks (document_id, ordinal, content, doc_type) "
        "VALUES (%s, 0, 'Kalba posėdyje', 'stenogram')",
        (stenogram,),
    )
    utterances = [
        {
            "voice": 1,
            "start": 40.6,
            "end": 78.7,
            "text": "O ką jie sako apie mokesčius?",
        },
        {
            "voice": 0,
            "start": 78.3,
            "end": 139.1,
            "text": "Žmonės labai nusivylę tais mokesčiais.",
        },
        {
            "voice": None,
            "start": 139.5,
            "end": 144.0,
            "text": "abu kalba kartu ir nieko nesuprasi",
        },
    ]
    reviewed_job(jobs, utterances, {"0": 0, "1": 1})
    batches = []

    def embedder(texts, *, model, dims, key):
        batches.append(len(texts))
        return [[1.0] + [0.0] * (dims - 1) for _ in texts], 7 * len(texts)

    loaded = interviews.load(VIDEO, conn=db, embedder=embedder)
    assert loaded["state"] == "loaded", loaded["error"]
    assert (loaded["chunks"], batches, loaded["embedding_tokens"]) == (3, [3], 21)
    url = f"https://www.youtube.com/watch?v={VIDEO}"
    rows = db.execute(
        "SELECT speaker_name, speaker_mp_id, source_url, embedding IS NOT NULL "
        "FROM chunks WHERE doc_type = 'youtube_interview' ORDER BY ordinal"
    ).fetchall()
    assert rows == [
        ("Rūta Janutienė", None, f"{url}&t=40s", True),
        ("Remigijus Žemaitaitis", 65703, f"{url}&t=78s", True),
        (None, None, f"{url}&t=139s", True),
    ]
    other = db.execute("SELECT embedding FROM chunks WHERE doc_type = 'stenogram'")
    assert other.fetchone()[0] is None  # only the interview's passages are embedded
    document = db.execute(
        "SELECT title, doc_date, metadata->'voices' FROM documents "
        "WHERE doc_type = 'youtube_interview'"
    ).fetchone()
    assert document == (
        "OpTV: Pokalbis",
        date(2025, 11, 20),
        [
            {"label": "A", "seconds": 61, "speaker": "Remigijus Žemaitaitis"},
            {"label": "B", "seconds": 38, "speaker": "Rūta Janutienė"},
        ],
    )
    assert db.execute("SELECT chunks FROM corpus_stats").fetchone()[0] == 4
    assert not (jobs / VIDEO / "audio.wav").exists()

    interviews.update(loaded, state="review")
    interviews.load(VIDEO, conn=db, embedder=embedder)
    count = "SELECT count(*) FROM chunks WHERE doc_type = 'youtube_interview'"
    assert db.execute(count).fetchone()[0] == 3  # replaced, not added

    assert interviews.remove(VIDEO, conn=db)["state"] == "review"
    assert db.execute(count).fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
    assert db.execute("SELECT chunks FROM corpus_stats").fetchone()[0] == 1


def test_loading_waits_until_someone_says_who_each_voice_is(db, jobs):
    utterances = [{"voice": 0, "start": 0.0, "end": 1.0, "text": "Labas"}]
    reviewed_job(jobs, utterances, None)
    job = interviews.load(VIDEO, conn=db, embedder=lambda *a, **kw: ([], 0))
    assert job["state"] == "review" and "who each voice is" in job["error"]
    assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 0


# ---------------------------------------------------------------------- agent


def test_interviews_are_searched_by_default_and_can_be_asked_for():
    assert "youtube_interview" in bot.SEARCHED_BY_DEFAULT
    args = bot.SearchArgs(query="mokesčiai", doc_types=["youtube_interview"])
    assert args.doc_types == ["youtube_interview"]
    assert "interview passage" in bot.PROMPT


# ----------------------------------------------------------------------- page


def kb_app(root):
    import sys

    if root not in sys.path:
        sys.path.insert(0, root)
    import kb_page

    kb_page.render(lambda: None)


def test_the_page_starts_a_job_and_records_who_each_voice_is(jobs, monkeypatch):
    from streamlit.testing.v1 import AppTest

    if str(ROOT) not in sys.path:
        monkeypatch.syspath_prepend(str(ROOT))
    import kb_page

    launched = []
    monkeypatch.setattr(
        interviews,
        "launch",
        lambda video_id, command, *extra: launched.append((video_id, command, extra)),
    )
    monkeypatch.setattr(interviews, "roster", lambda conn: MEMBERS)
    monkeypatch.setattr(kb_page, "key_has_limit", lambda: True)
    # No harvested archive: the only form is the one for a pasted link.
    monkeypatch.setattr(interviews.youtube, "ROOT", jobs.parent)
    kb_page.cached_archive.clear()
    monkeypatch.setattr(kb_page.youtube_new, "JOB", jobs / "new_videos.json")
    # The page opens with the Seimas update, which reads the database and its job.
    monkeypatch.setattr(update, "JOB", jobs / "seimas_update.json")
    monkeypatch.setattr(update, "missing_vectors", lambda conn: 0)
    monkeypatch.setattr(
        update,
        "record",
        lambda conn: (
            dict.fromkeys(
                ("votes", "with_results", "choices", "last_vote_at", "loaded_at"), None
            )
            | {"votes": 0, "choices": 0}
        ),
    )

    app = AppTest.from_function(kb_app, args=(str(ROOT),), default_timeout=30).run()
    assert not app.exception
    app.text_input[0].input("https://example.com/video")
    app.text_area[0].input("Rūta Janutienė")
    next(b for b in app.button if b.label == "Pradėti").click().run()
    assert [e.value for e in app.error] == ["Tai ne YouTube nuoroda."]
    assert launched == []

    app.text_input[0].input(f"https://youtu.be/{VIDEO}")
    app.multiselect[0].select("Remigijus Žemaitaitis")
    next(b for b in app.button if b.label == "Pradėti").click().run()
    assert launched == [(VIDEO, "run", ())]
    job = interviews.read_job(VIDEO)
    assert job["participants"] == [
        {"name": "Remigijus Žemaitaitis", "mp_id": 65703},
        {"name": "Rūta Janutienė", "mp_id": None},
    ]

    voices = [
        {"voice": 0, "label": "A", "seconds": 435, "share": 0.79, "first": 5.0,
         "samples": [{"start": 78.3, "end": 139.1, "text": "Žmonės nusivylę."}]},
        {"voice": 2, "label": "B", "seconds": 105, "share": 0.19, "first": 40.6,
         "samples": [{"start": 40.6, "end": 78.7, "text": "O ką jie sako?"}]},
    ]  # fmt: skip
    interviews.update(job, state="review", title="Pokalbis", voices=voices, pid=None)
    app.run()
    assert [s.label for s in app.selectbox] == [
        "Kas kalba balsu A?",
        "Kas kalba balsu B?",
    ]
    app.selectbox[0].set_value(1)
    app.selectbox[1].set_value(2)
    load = next(b for b in app.button if b.label == "Įkelti į žinių bazę")
    load.click().run()
    assert "Pirmiausia patvirtinkite" in app.error[0].value
    assert launched == [(VIDEO, "run", ())]

    app.selectbox[0].set_value(1)
    app.selectbox[1].set_value(2)
    app.checkbox[0].check()
    next(b for b in app.button if b.label == "Įkelti į žinių bazę").click().run()
    job = interviews.read_job(VIDEO)
    assert job["assignment"] == {"0": 0, "2": 1}
    assert job["state"] == "loading"
    assert launched[-1] == (VIDEO, "load", ())


# ------------------------------------------------------------ harvested archive


def archive(root, monkeypatch):
    """A harvest of two videos: one naming an MP as appearing, one dropped."""
    monkeypatch.setattr(interviews.youtube, "ROOT", root)
    monkeypatch.setattr(interviews, "TRIMMED", root / "trimmed")
    guesses = {
        "aaaaaaaaaaa": {"status": "MPs only mentioned", "appearing": [], "evidence": [
            {"mp": "Aidas Gedvilas", "role": "said"}]},
        VIDEO: {"status": "names who appears (full name)",
                "appearing": ["Remigijus Žemaitaitis"], "evidence": [
                    {"mp": "Remigijus Žemaitaitis", "role": "appears"}]},
    }  # fmt: skip
    interviews.youtube.save(root / "participants.json", guesses)
    for vid, title in ((VIDEO, "Pokalbis"), ("aaaaaaaaaaa", "Anglų kalba")):
        interviews.youtube.save(
            root / "videos" / f"{vid}.json",
            {"title": title, "channel": "OpTV", "duration": 600},
        )
    interviews.youtube.save(
        root / "descriptions" / f"{VIDEO}.json", {"upload_date": "20251120"}
    )
    interviews.youtube.save(
        root / "trimmed" / "aaaaaaaaaaa.json", {"dropped": {"reason": "language"}}
    )


def test_the_archive_lists_candidates_first_and_where_each_stands(jobs, monkeypatch):
    archive(jobs.parent, monkeypatch)
    videos = interviews.harvested()
    assert [
        (v["video_id"], v["candidates"], v["mentioned"], v["published"]) for v in videos
    ] == [
        (VIDEO, ["Remigijus Žemaitaitis"], [], "2025-11-20"),
        ("aaaaaaaaaaa", [], ["Aidas Gedvilas"], None),
    ]
    assert interviews.review_states(videos) == {
        VIDEO: "todo",
        "aaaaaaaaaaa": "unusable",
    }
    interviews.start(f"https://youtu.be/{VIDEO}", PEOPLE, launch_worker=False)
    assert interviews.review_states(videos)[VIDEO] == "queued"


def test_a_video_without_an_mp_is_recorded_and_can_be_unmarked(jobs, monkeypatch):
    archive(jobs.parent, monkeypatch)
    videos = interviews.harvested()
    interviews.start(f"https://youtu.be/{VIDEO}", PEOPLE, launch_worker=False)
    interviews.update(interviews.read_job(VIDEO), state="review")
    interviews.mark_no_mp(VIDEO, "  kalba tik žurnalistai ")
    marked = json.loads((jobs / "no_mp.json").read_text("utf-8"))
    assert marked[VIDEO]["note"] == "kalba tik žurnalistai" and marked[VIDEO]["at"]
    assert interviews.read_job(VIDEO) is None  # the review job is dropped with it
    assert interviews.review_states(videos)[VIDEO] == "no_mp"
    interviews.unmark_no_mp(VIDEO)
    assert interviews.review_states(videos)[VIDEO] == "todo"


def test_a_loaded_video_cannot_be_marked_as_having_no_mp(jobs, monkeypatch):
    archive(jobs.parent, monkeypatch)
    interviews.start(f"https://youtu.be/{VIDEO}", PEOPLE, launch_worker=False)
    interviews.update(interviews.read_job(VIDEO), state="loaded")
    with pytest.raises(ValueError, match="cannot be marked"):
        interviews.mark_no_mp(VIDEO)


def test_a_job_under_review_can_be_separated_again(jobs, monkeypatch):
    interviews.start(f"https://youtu.be/{VIDEO}", PEOPLE, launch_worker=False)
    job = interviews.update(
        interviews.read_job(VIDEO),
        state="review",
        voices=[{"voice": 0, "label": "A"}],
        assignment={"0": 0},
        reviewed_at="2026-09-23T10:00:00+00:00",
    )
    # without its audio too: the worker downloads it again
    job = interviews.separate_again(VIDEO, 2, launch_worker=False)
    assert job["round"] == 1
    assert (job["state"], job["voices_expected"], job["assignment"]) == (
        "queued",
        2,
        {},
    )
    assert job["reviewed_at"] is None
    with pytest.raises(ValueError, match="only a job under review"):
        interviews.separate_again(VIDEO, 2, launch_worker=False)


def test_the_worker_reuses_audio_it_already_has(jobs, monkeypatch):
    monkeypatch.setattr(interviews.youtube, "ROOT", jobs.parent)
    interviews.start(f"https://youtu.be/{VIDEO}", PEOPLE, launch_worker=False)
    fake_steps(monkeypatch, {"title": "Pokalbis", "duration": 552})
    (jobs / VIDEO / "audio.wav").write_bytes(b"RIFF")

    def no_download(*args, **kwargs):
        raise AssertionError("the audio was downloaded again")

    monkeypatch.setattr(interviews, "download_audio", no_download)
    job = interviews.run(VIDEO)
    assert job["state"] == "review", job["error"]


def test_overlap_crumbs_and_scraps_do_not_become_passages():
    job = {
        "video_id": VIDEO,
        "title": "Pokalbis",
        "channel": "OpTV",
        "participants": PEOPLE,
        "assignment": {"0": 0, "1": 1},
    }
    said = [
        (0, 0.0, 9.0, "Pirmiausia noriu pasakyti apie mokesčius"),
        (None, 9.0, 9.5, "taip taip"),  # two voices at once
        (0, 9.5, 20.0, "ir apie tai, kaip jie keisis kitais metais"),
        (1, 20.0, 21.0, "Aišku."),
        (None, 21.0, 30.0, "kažkas kalba kartu ir niekas nežino kas"),
    ]
    said = [dict(zip(("voice", "start", "end", "text"), u, strict=True)) for u in said]
    rows = interviews.chunk_rows(1, job, said)
    texts = [row[2].split("\n", 1)[1] for row in rows]
    assert texts == [
        "[0:00:00–0:00:20] Remigijus Žemaitaitis: Pirmiausia noriu pasakyti apie "
        "mokesčius ir apie tai, kaip jie keisis kitais metais",
        "[0:00:21–0:00:30] Nenustatytas kalbėtojas: kažkas kalba kartu ir niekas "
        "nežino kas",
    ]
    assert [row[3] for row in rows] == ["Remigijus Žemaitaitis", None]


def test_a_person_can_be_added_while_the_voices_are_reviewed(jobs):
    interviews.start(f"https://youtu.be/{VIDEO}", PEOPLE, launch_worker=False)
    job = interviews.update(interviews.read_job(VIDEO), state="review")
    job = interviews.add_participant(job, MEMBERS, "Aidas Gedvilas")
    job = interviews.add_participant(job, MEMBERS, "Laidos Vedėja")
    assert job["participants"] == [
        *PEOPLE,  # the old order stays, so assigned indexes still hold
        {"name": "Aidas Gedvilas", "mp_id": 47855},
        {"name": "Laidos Vedėja", "mp_id": None},
    ]
    with pytest.raises(ValueError, match="named twice"):
        interviews.add_participant(job, MEMBERS, "Laidos Vedėja")


def test_an_interview_taken_out_is_reviewed_from_scratch(jobs, monkeypatch):
    interviews.start(f"https://youtu.be/{VIDEO}", PEOPLE, launch_worker=False)
    (jobs / VIDEO / "utterances.json").write_text("[]", "utf-8")
    interviews.update(
        interviews.read_job(VIDEO),
        state="loaded",
        assignment={"0": 0},
        reviewed_at="2026-09-23T09:41:08+00:00",
    )
    monkeypatch.setattr(interviews, "delete_document", lambda conn, url: None)
    monkeypatch.setattr(interviews.psycopg, "connect", lambda *a, **k: FakeConn())
    from rag import retrieval

    monkeypatch.setattr(retrieval, "refresh_stats", lambda conn: 0)
    job = interviews.remove(VIDEO)
    assert (job["state"], job["assignment"], job["reviewed_at"], job["round"]) == (
        "review",
        {},
        None,
        1,
    )


class FakeConn:
    def transaction(self):
        from contextlib import nullcontext

        return nullcontext()

    def close(self):
        pass
