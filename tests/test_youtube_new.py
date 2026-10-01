import json

import pytest

from ingest import appearances, youtube, youtube_new

MEMBERS = [
    {"id": "65703", "name": "Remigijus Žemaitaitis", "surname": "Žemaitaitis"},
    {"id": "7252", "name": "Algirdas Sysas", "surname": "Sysas"},
    {"id": "47855", "name": "Aidas Gedvilas", "surname": "Gedvilas"},
    {"id": "47856", "name": "Martynas Gedvilas", "surname": "Gedvilas"},
    {"id": "90001", "name": "Simonas Kairys", "surname": "Kairys"},
]


def guessed(title, description="", shows=None):
    video = {"video_id": "aaaaaaaaaaa", "title": title, "description": description}
    return appearances.classify(video, MEMBERS, shows)


# ------------------------------------------------------------------- guesses


@pytest.mark.parametrize(
    "title",
    [
        "Žemaitaitis per OpTV",
        "TIESIOGIAI SU REMIGIJUMI ŽEMAITAIČIU",
        "Mokesčiai | Remigijus Žemaitaitis",
    ],
)
def test_a_title_naming_a_guest_fills_them_in(title):
    entry = guessed(title)
    assert entry["appearing"] == ["Remigijus Žemaitaitis"]
    assert entry["appearing_mp_ids"] == ["65703"]
    assert entry["status"].startswith("names who appears")


def test_a_guest_is_found_in_the_description_in_another_case():
    entry = guessed(
        '"Ačiū, kad atėjote"',
        "Andrius Užkalnis kalbina seimo narį, socialdemokratą Algirdą Sysą.",
    )
    assert entry["appearing"] == ["Algirdas Sysas"]
    assert entry["status"] == "names who appears (full name)"


def test_a_cue_before_a_dash_still_counts():
    entry = guessed("Kaip atrodo Seimo nario darbas? Svečiuose - Simonas Kairys")
    assert entry["appearing"] == ["Simonas Kairys"]


def test_a_title_opening_with_a_name_is_a_weaker_sign():
    entry = guessed("Žemaitaitis išklojo, ką iš tikrųjų mano slidžiu klausimu")
    assert entry["status"] == "described as speaking (weaker)"
    assert entry["appearing"] == ["Remigijus Žemaitaitis"]


def test_a_mere_mention_fills_in_nobody():
    entry = guessed("Kas vyksta Seime", "Kalbama ir apie Žemaitaičio pareiškimus.")
    assert (entry["status"], entry["appearing"]) == ("MPs only mentioned", [])
    assert entry["evidence"][0]["mp"] == "Remigijus Žemaitaitis"


def test_with_alone_in_a_description_is_no_sign():
    entry = guessed("Savaitės apžvalga", "Ginčas su Sysu dėl biudžeto tęsiasi.")
    assert entry["appearing"] == []


def test_a_shared_surname_needs_the_given_name():
    assert guessed("Gedvilas per OpTV") is None
    assert guessed("Aidas Gedvilas per OpTV")["appearing"] == ["Aidas Gedvilas"]


def test_a_clip_takes_the_guests_of_the_full_show_it_links():
    show = {"appearing": ["Remigijus Žemaitaitis"]}
    entry = guessed(
        "Viskas Vėgėlės rankose?",
        "visa laida yra čia: https://www.youtube.com/live/NSeDyvRZe-M",
        {"NSeDyvRZe-M": show},
    )
    assert entry["status"] == "clip: its full show names who appears"
    assert entry["appearing"] == ["Remigijus Žemaitaitis"]
    assert entry["full_shows"] == ["NSeDyvRZe-M"]
    unknown = guessed(
        "Viskas Vėgėlės rankose?", "visa laida: https://youtu.be/NSeDyvRZe-M"
    )
    assert unknown["status"] == "clip: full show linked, nobody named as appearing"


def test_a_video_naming_no_mp_is_no_candidate():
    assert guessed("Receptai: naminiai makaronai") is None


# ---------------------------------------------------------------- the run


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube, "ROOT", tmp_path)
    for name in ("JOB", "LOCK", "PARTICIPANTS"):
        path = getattr(youtube_new, name)
        monkeypatch.setattr(youtube_new, name, tmp_path / path.name)
    monkeypatch.setattr(youtube_new.config, "REQUEST_DELAY", 0)
    monkeypatch.setattr(youtube_new.time, "sleep", lambda seconds: None)
    youtube.save(tmp_path / "roster.json", {"members": MEMBERS})
    monkeypatch.setattr(youtube, "roster", lambda args: None)
    return tmp_path


def video(root, video_id, title, candidates=()):
    youtube.save(
        root / "videos" / f"{video_id}.json",
        {"video_id": video_id, "title": title, "candidate_mp_ids": list(candidates)},
    )


PAGES = {
    "newguest001": {
        "title": "Algirdas Sysas per OpTV",
        "description": "",
        "upload_date": "20260920",
    },
    "newclip0002": {
        "title": "Trumpai",
        "description": "Visa laida yra čia: https://youtu.be/fullshow003",
        "upload_date": "20260921",
    },
    "fullshow003": {
        "title": "Tiesiogiai su Remigijumi Žemaitaičiu",
        "description": "",
        "upload_date": "20260921",
    },
    "cooking0004": {
        "title": "Naminiai makaronai",
        "description": "",
        "upload_date": "20260919",
    },
}


def fetch(video_id):
    if video_id not in PAGES:
        raise RuntimeError("private video")
    return {"video_id": video_id, **PAGES[video_id]}


def test_a_run_describes_new_videos_and_adds_those_naming_an_mp(root):
    video(root, "oldvideo000", "Senas")  # found before: left alone
    youtube.save(root / "participants.json", {"kept0000000": {"appearing": ["X"]}})

    def search(report):
        report(0, 1)
        video(root, "newguest001", "Algirdas Sysas per OpTV", ["7252"])
        video(root, "newclip0002", "Trumpai")
        video(root, "cooking0004", "Naminiai makaronai")
        video(root, "gone0000005", "Pašalintas")

    job = youtube_new.run(limit=10, search=search, fetch=fetch)
    assert job["state"] == "done", job.get("error")
    assert (job["found"], job["added"]) == (4, 3)
    assert list(job["steps"]) == ["roster", "search", "describe", "guess"]
    known = json.loads((root / "participants.json").read_text())
    assert known["kept0000000"] == {"appearing": ["X"]}  # never replaced
    assert known["newguest001"]["appearing"] == ["Algirdas Sysas"]
    assert known["fullshow003"]["appearing"] == ["Remigijus Žemaitaitis"]
    assert known["newclip0002"]["appearing"] == ["Remigijus Žemaitaitis"]
    assert "cooking0004" not in known and "oldvideo000" not in known
    page = json.loads((root / "descriptions" / "newguest001.json").read_text())
    assert page["upload_date"] == "20260920"
    assert json.loads((root / "descriptions" / "gone0000005.json").read_text()) == {
        "video_id": "gone0000005",
        "error": "RuntimeError",
    }


def test_pages_beyond_the_limit_wait_for_the_next_run(root, monkeypatch):
    def search(report):
        video(root, "newguest001", "Algirdas Sysas per OpTV", ["7252"])
        video(root, "cooking0004", "Naminiai makaronai")

    job = youtube_new.run(limit=1, search=search, fetch=fetch)
    # the video whose title names an MP is described first
    assert (job["described"], job["waiting"]) == (1, ["cooking0004"])

    monkeypatch.setattr(youtube_new, "minutes_to_wait", lambda job: 0)
    job = youtube_new.run(limit=1, search=lambda report: None, fetch=fetch)
    assert (job["described"], job["waiting"]) == (1, [])


def test_one_search_at_a_time_and_at_most_hourly(root):
    youtube_new.start(launch_worker=False)
    with pytest.raises(ValueError, match="already running"):
        youtube_new.start(launch_worker=False)
    youtube_new.update(
        youtube_new.read(), state="done", last_success_at=youtube_new.now()
    )
    with pytest.raises(ValueError, match="less than an hour"):
        youtube_new.start(launch_worker=False)
