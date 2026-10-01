from ingest import transcripts as tr


def seg(text, start=0.0):
    return {"text": text, "start": start, "end": start + 2}


def video(vid, segments, signals=None):
    return tr.Video(
        id=vid,
        title="",
        channel="",
        version="asr-x",
        segments=segments,
        signals=signals,
    )


LITHUANIAN = (
    "gerbiami kolegos šiandien svarstome įstatymo projektą dėl mokesčių ir biudžeto "
)


def test_whisper_thresholds_are_strict_inequalities():
    assert tr.whisper_unreliable({"avg_logprob": -1.01}) == "low_confidence"
    assert tr.whisper_unreliable({"avg_logprob": -0.9, "compression_ratio": 2.41}) == (
        "repetition"
    )
    assert (
        tr.whisper_unreliable({"avg_logprob": -1.0, "compression_ratio": 2.4}) is None
    )
    assert tr.whisper_unreliable(None) is None


def test_foreign_script_flags_hallucination_not_lithuanian():
    assert tr.FOREIGN_SCRIPT.search("Ir вамūか įū세요 aiko")
    assert not tr.FOREIGN_SCRIPT.search("Ačiū už klausimą – ką žinote apie „Ignitis“?")


def test_repetition_loop_keeps_the_first_occurrence_only_for_long_runs():
    segments = [seg("jose")] * 4 + [seg("kitas")] + [seg("taip")] * 2
    assert tr.repetition_loops(segments) == {1, 2, 3}


def test_segment_rules_in_order():
    v = video(
        "a",
        [seg("Labas vakaras"), seg("Ir вам"), seg("  "), seg("tekstas"), seg("x")],
        [{"avg_logprob": -0.2}, {"avg_logprob": -0.2}, {}, {"avg_logprob": -2.0}, {}],
    )
    tr.mark_segments(v)
    assert {i: r for i, (r, _) in v.removed.items()} == {
        1: "foreign_script",
        2: "empty",
        3: "whisper_low_confidence",
    }


def test_duplicates_come_out_of_the_shorter_video_only():
    words = [f"zodis{i}" for i in range(60)]
    long = video(
        "long",
        [
            seg(" ".join(words[:30])),
            seg(" ".join(words[30:])),
            seg("unikalus tekstas ilgame įraše ir dar daugiau žodžių čia yra"),
        ],
    )
    clip = video(
        "clip",
        [
            seg(" ".join(words[10:30])),
            seg("savas komentaras apie visai kitką sakant ir dar kartą dabar"),
        ],
    )
    assert tr.duplicates([long, clip]) == {"clip": {0: "long"}}


def test_video_mostly_not_lithuanian_is_dropped():
    model = tr.CharModel(LITHUANIAN * 20)
    cutoff = model.score("gerbiami kolegos svarstome projektą") - 1.0
    lithuanian = video(
        "lt", [seg("gerbiami kolegos šiandien svarstome įstatymo projektą")] * 5
    )
    english = video(
        "en", [seg("the quick brown fox jumps over the lazy dog again")] * 5
    )
    assert tr.video_verdict(lithuanian, model, cutoff) is None
    assert tr.video_verdict(english, model, cutoff)[0] == "not_lithuanian"


def test_video_mostly_flagged_by_whisper_is_dropped():
    model = tr.CharModel(LITHUANIAN * 20)
    cutoff = -100.0  # language test passes; only the Whisper share decides
    text = "gerbiami kolegos šiandien svarstome įstatymo projektą"
    v = video("w", [seg(text)] * 4, [{"avg_logprob": -2}] * 3 + [{"avg_logprob": -0.1}])
    assert tr.video_verdict(v, model, cutoff)[0] == "unreliable"


def test_clip_left_mostly_empty_by_deduplication_is_dropped_whole():
    clip = video("clip", [seg("a" * 90), seg("b" * 10)])
    clip.removed = {0: ("duplicate", "long")}
    assert tr.mostly_copied(clip) == ("duplicate_video", "90% repeats long")
    clip.removed = {0: ("whisper_low_confidence", "")}
    assert tr.mostly_copied(clip) is None
