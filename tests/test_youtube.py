import argparse
import json

import pytest

from ingest import youtube


def test_current_roster_excludes_departed_and_future_members():
    xml = """<SeimoInformacija>
    <SeimoNarys asmens_id="1" vardas="A" pavardė="B"
      data_nuo="2024-11-14" data_iki="" />
    <SeimoNarys asmens_id="2" vardas="C" pavardė="D"
      data_nuo="2024-11-14" data_iki="2025-01-01" />
    <SeimoNarys asmens_id="3" vardas="E" pavardė="F"
      data_nuo="2027-01-01" data_iki="" />
    </SeimoInformacija>"""
    assert [m["id"] for m in youtube.current_members(xml, "2026-09-11")] == ["1"]


def test_discovery_is_not_speaker_attribution():
    members = [{"id": "1", "surname": "Žemaitaitis"}]
    assert youtube.candidate_matches("Pokalbis apie Žemaitaitį", members) == ["1"]
    segment = youtube.validated_segments(
        [{"text": "Pokalbis apie Žemaitaitį", "start": 1, "duration": 2}]
    )[0]
    assert segment["speaker_mp_id"] is None
    assert segment["end"] == 3


def test_compound_surname_short_form_is_a_candidate():
    members = [{"id": "1", "surname": "Čmilytė-Nielsen"}]
    assert youtube.candidate_matches("Pokalbis su V. Čmilyte", members) == ["1"]


@pytest.mark.parametrize(
    "start,duration", [(-1, 1), (1, -1), (float("nan"), 1), (1, float("inf"))]
)
def test_rejects_bad_timestamps(start, duration):
    with pytest.raises(ValueError):
        youtube.validated_segments(
            [{"text": "x", "start": start, "duration": duration}]
        )


def test_empty_captions_are_not_successful():
    with pytest.raises(ValueError):
        youtube.validated_segments([])


def test_rounding_carries_to_next_minute():
    assert youtube.timestamp(59.9999) == "00:01:00.000"


def test_successful_import_is_not_refetched(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube, "ROOT", tmp_path)
    youtube.save(
        tmp_path / "videos" / "abcdefghijk.json",
        {
            "video_id": "abcdefghijk",
            "candidate_mp_ids": ["1"],
            "transcript_status": "downloaded",
            "transcript_path": "transcripts/abcdefghijk/version/transcript.json",
            "url": "https://www.youtube.com/watch?v=abcdefghijk",
        },
    )
    from youtube_transcript_api import YouTubeTranscriptApi

    def fail(*args, **kwargs):
        pytest.fail("Successful transcript must not be fetched twice")

    monkeypatch.setattr(YouTubeTranscriptApi, "list", fail)
    youtube.captions(argparse.Namespace(limit=10, retry=False))


def test_coverage_keeps_zero_result_mps(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube, "ROOT", tmp_path)
    youtube.save(tmp_path / "roster.json", {"members": [{"id": "1", "name": "A B"}]})
    youtube.report(None)
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["coverage"] == [
        {"mp_id": "1", "name": "A B", "candidate_videos": 0, "transcripts": 0}
    ]


def test_provider_block_preserves_remaining_queue(tmp_path, monkeypatch):
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import RequestBlocked

    monkeypatch.setattr(youtube, "ROOT", tmp_path)
    for vid in ["abcdefghijk", "bcdefghijkl"]:
        youtube.save(
            tmp_path / "videos" / f"{vid}.json",
            {
                "video_id": vid,
                "candidate_mp_ids": ["1"],
            },
        )

    def blocked(self, vid):
        raise RequestBlocked(vid)

    monkeypatch.setattr(YouTubeTranscriptApi, "list", blocked)
    assert youtube.captions(argparse.Namespace(limit=10, retry=False)) is True
    assert (
        youtube.read(tmp_path / "videos/abcdefghijk.json")["transcript_status"]
        == "blocked"
    )
    assert "transcript_status" not in youtube.read(tmp_path / "videos/bcdefghijkl.json")


def test_caption_export_preserves_text_and_unknown_speakers(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from youtube_transcript_api import YouTubeTranscriptApi

    monkeypatch.setattr(youtube, "ROOT", tmp_path)
    video = {
        "video_id": "abcdefghijk",
        "candidate_mp_ids": ["1"],
        "title": "Test",
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
    }
    youtube.save(tmp_path / "videos/abcdefghijk.json", video)
    raw = [{"text": "Lietuviškas tekstas", "start": 12.5, "duration": 2.25}]
    track = SimpleNamespace(
        language_code="lt",
        is_generated=True,
        fetch=lambda: SimpleNamespace(to_raw_data=lambda: raw),
    )

    class Tracks(list):
        def find_transcript(self, languages):
            assert languages == ["lt"]
            return track

    monkeypatch.setattr(YouTubeTranscriptApi, "list", lambda self, vid: Tracks([track]))
    assert youtube.captions(argparse.Namespace(limit=10, retry=False)) is False
    stored = youtube.read(tmp_path / "videos/abcdefghijk.json")
    artifact = tmp_path / stored["transcript_path"]
    transcript = youtube.read(artifact)
    assert transcript["segments"][0]["speaker_mp_id"] is None
    assert transcript["segments"][0]["text"] == raw[0]["text"]
    assert "00:00:12.500 --> 00:00:14.750" in artifact.with_suffix(".vtt").read_text()
    assert artifact.with_suffix(".txt").exists()


def test_priority_queue_rotates_mps_and_keeps_other_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube, "ROOT", tmp_path)
    youtube.save(
        tmp_path / "roster.json",
        {
            "members": [
                {"id": "1", "name": "First Person"},
                {"id": "2", "name": "Second Person"},
            ]
        },
    )
    for vid, title, duration in [
        ("a", "First Person interview", 2000),
        ("b", "First Person second interview", 2000),
        ("c", "Second Person interview", 2000),
        ("d", "Another candidate", 300),
        ("e", "First Person and Second Person", 2000),
    ]:
        youtube.save(
            tmp_path / "videos" / f"{vid}.json",
            {
                "title": title,
                "duration": duration,
            },
        )
    result = [p.stem for p in youtube.prioritized_video_paths()]
    assert result == ["a", "c", "b", "e", "d"]


def test_reverse_priority_queue_starts_from_the_back(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube, "ROOT", tmp_path)
    youtube.save(
        tmp_path / "roster.json",
        {"members": [{"id": "1", "name": "First Person"}]},
    )
    for vid, title, duration in [
        ("a", "First Person interview", 2000),
        ("b", "Another candidate", 300),
    ]:
        youtube.save(
            tmp_path / "videos" / f"{vid}.json", {"title": title, "duration": duration}
        )
    assert [p.stem for p in youtube.reverse_prioritized_video_paths()] == ["b", "a"]


def test_pending_asr_is_explicit_and_does_not_retry_success():
    from ingest.youtube_asr import eligible

    video = {"video_id": "abcdefghijk", "candidate_mp_ids": ["1"]}
    args = argparse.Namespace(video=None, retry=False, pending=False)
    assert not eligible(video, args)
    args.pending = True
    assert eligible(video, args)
    video["transcript_status"] = "generated"
    assert not eligible(video, args)
    video["transcript_status"] = "downloaded"
    assert not eligible(video, args)


def test_pending_asr_keeps_inaccessible_videos_out():
    from ingest.youtube_asr import eligible

    args = argparse.Namespace(video=None, retry=False, pending=True)
    video = {
        "video_id": "abcdefghijk",
        "candidate_mp_ids": ["1"],
        "transcript_status": "unavailable",
        "transcript_error": "VideoUnplayable",
    }
    assert not eligible(video, args)
    video["transcript_error"] = "NoTranscriptFound"
    assert eligible(video, args)
    video["live_status"] = "is_live"
    assert not eligible(video, args)


def test_asr_can_target_one_mp_id():
    from ingest.youtube_asr import eligible

    args = argparse.Namespace(
        video=None,
        retry=False,
        pending=True,
        mp_id="65703",
        target_mp_ids=["65703"],
    )
    assert eligible({"video_id": "a", "candidate_mp_ids": ["65703"]}, args)
    assert not eligible({"video_id": "b", "candidate_mp_ids": ["other"]}, args)


def test_asr_active_claims_are_skipped_until_stale():
    from ingest.youtube_asr import eligible

    args = argparse.Namespace(video=None, retry=False, pending=True)
    video = {
        "video_id": "abcdefghijk",
        "candidate_mp_ids": ["1"],
        "transcript_status": "asr_processing",
        "asr_claimed_at": youtube.now(),
    }
    assert not eligible(video, args)
    video["asr_claimed_at"] = "bad timestamp"
    assert eligible(video, args)
    args.retry = True
    video["asr_claimed_at"] = youtube.now()
    assert eligible(video, args)


def test_legacy_asr_transcribing_without_claim_is_still_active():
    from ingest.youtube_asr import eligible

    args = argparse.Namespace(video=None, retry=False, pending=True)
    video = {
        "video_id": "abcdefghijk",
        "candidate_mp_ids": ["1"],
        "asr_status": "transcribing",
    }
    assert not eligible(video, args)
    args.retry = True
    assert eligible(video, args)


def test_gemini_claims_skip_success_and_allow_stale_retry():
    from ingest.youtube_gemini import eligible

    args = argparse.Namespace(video=None, retry=False, pending=True)
    video = {"video_id": "abcdefghijk", "candidate_mp_ids": ["1"]}
    assert eligible(video, args)
    video["transcript_status"] = "gemini_processing"
    video["gemini_claimed_at"] = "bad timestamp"
    assert eligible(video, args)
    video["transcript_status"] = "generated"
    assert not eligible(video, args)


def test_stem_alternating_surnames_are_found_in_every_case():
    """Zemaitaitis -> Zemaitaicio is the inflection the project brief calls out."""
    members = [{"id": "1", "surname": "Žemaitaitis"}, {"id": "2", "surname": "Kuodis"}]
    for form in (
        "Žemaitaitis",
        "Žemaitaičio",
        "Žemaitaičiui",
        "Žemaitaitį",
        "Žemaitaičiu",
    ):
        assert "1" in youtube.candidate_matches(f"Interviu su {form}", members), form
    for form in ("Kuodis", "Kuodžio", "Kuodžiui", "Kuodį"):
        assert "2" in youtube.candidate_matches(f"Interviu su {form}", members), form


def test_common_words_do_not_become_candidates():
    """A parliamentary corpus is full of balsavimas; Balsys is a surname."""
    members = [
        {"id": "1", "surname": "Balsys"},
        {"id": "2", "surname": "Alekna"},
        {"id": "3", "surname": "Ąžuolas"},
    ]
    for text in (
        "Skandalingas balsavimas Seime",
        "Seimas balsavo už biudžetą",
        "Kalbame apie balsą ir balsavimo teisę",
    ):
        assert youtube.candidate_matches(text, members) == [], text
    # a different person whose surname merely starts the same way
    assert youtube.candidate_matches("Aleknavičienė apie mokesčius", members) == []
    # the real surname still matches, in its declined forms
    assert youtube.candidate_matches("Pokalbis su Balsiu", members) == ["1"]
    assert youtube.candidate_matches("Aleknos pozicija", members) == ["2"]
