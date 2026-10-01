from datetime import date, time

from ingest import seimas, stenograms

DOC = """<html><body>
<p class="MsoHeader"> </p>
<p class="MsoTitle">Stenogramą galima rasti</p>
<p class="MsoNormal"><b>2025 m. rugsėjo 10 d.</b></p>
<p class="Pirmininkai">Pirmininkauja Seimo Pirmininkas S. SKVERNELIS</p>
<p class="Roman"><b>PIRMININKAS (S. SKVERNELIS).</b> Ger&shy;bia&shy;mi
  ko&shy;le&shy;gos.</p>
<p class="Laikas"><span>10.05 val.</span></p>
<p class="Roman12"><span>Sei&shy;mo nu&shy;ta&shy;ri&shy;mo „Dėl X“ pro&shy;jek&shy;tas
  Nr. XVP-717 (pa&shy;tei&shy;ki&shy;mas)</span></p>
<p class="Roman"><span> </span></p>
<p class="Roman"><b><span>PIRMININKAS</span></b><span> (J. OLEKAS, LSDPF*).
  Kvie&shy;čiu.</span></p>
<p class="Roman"><b>V. PRANCKIETIS</b> (LSF). Pir&shy;ma pa&shy;strai&shy;pa.</p>
<p class="Roman">An&shy;tra, <span>U</span><span>žsiregistravo</span> 128.</p>
<p class="Roman"><b>PIRMININKAS.</b> Ačiū.</p>
<p class="Priemimas">Šio nutarimo priėmimas</p>
<p class="Roman"><b>V. ČMILYTĖ-NIELSEN</b> (LSF). Dėkoju.</p>
<p class="Roman"><b>M. JAKUBAUSKIENĖ.</b> Ministrė kalba.</p>
<p class="MsoNormal">* Santrumpų reikšmės: LSDPF – Lietuvos socialdemokratų partijos
  frakcija; LSF – Liberalų sąjūdžio frakcija.</p>
</body></html>"""


def test_turns_follow_the_markup():
    turns, legend = stenograms.parse(DOC)
    assert [(t.marker, t.name, t.fraction, t.chair) for t in turns] == [
        ("PIRMININKAS (S. SKVERNELIS)", "S. SKVERNELIS", None, True),
        ("PIRMININKAS (J. OLEKAS, LSDPF)", "J. OLEKAS", "LSDPF", True),
        ("V. PRANCKIETIS (LSF)", "V. PRANCKIETIS", "LSF", False),
        # a bare PIRMININKAS is the chair in office
        ("PIRMININKAS", "J. OLEKAS", "LSDPF", True),
        ("V. ČMILYTĖ-NIELSEN (LSF)", "V. ČMILYTĖ-NIELSEN", "LSF", False),
        ("M. JAKUBAUSKIENĖ", "M. JAKUBAUSKIENĖ", None, False),
    ]
    assert legend == {
        "LSDPF": "Lietuvos socialdemokratų partijos frakcija",
        "LSF": "Liberalų sąjūdžio frakcija",
    }


def test_sections_times_hyphens_and_drop_caps():
    turns, _ = stenograms.parse(DOC)
    title = "Seimo nutarimo „Dėl X“ projektas Nr. XVP-717 (pateikimas)"
    assert turns[0].section is None
    assert (turns[1].section, turns[1].section_time) == (title, time(10, 5))
    assert turns[2].paragraphs == ["Pirma pastraipa.", "Antra, Užsiregistravo 128."]
    assert turns[4].section == f"{title} — Šio nutarimo priėmimas"
    assert seimas.bill_number(turns[1].section) == "XVP-717"


def test_long_turns_split_and_every_chunk_names_its_speaker():
    turn = stenograms.Turn(
        "V. PRANCKIETIS (LSF)",
        "V. PRANCKIETIS",
        "LSF",
        False,
        None,
        None,
        ["Sakinys. " * 100, "Kitas. " * 100, "Trumpas."],
    )
    chunks = stenograms.chunks_of(turn, limit=500)
    assert len(chunks) > 3
    assert all(c.startswith("V. PRANCKIETIS (LSF). ") for c in chunks)
    assert all(len(c) <= 500 + len("V. PRANCKIETIS (LSF). ") for c in chunks)


def test_speakers_resolve_by_initial_and_surname_within_the_mandate():
    since = date(2024, 11, 14)
    index = stenograms.speaker_index(
        [
            (1, "Viktoras", "Pranckietis", since, None),
            (2, "Viktorija", "Čmilytė-Nielsen", since, None),
            (3, "Rimas Jonas", "Jankūnas", since, None),
            (4, "Aidas", "Gedvilas", since, None),
            (5, "Martynas", "Gedvilas", since, None),
            (6, "Vilija", "Blinkevičiūtė", since, since),
        ]
    )
    day = date(2025, 9, 10)
    assert stenograms.resolve("V. PRANCKIETIS", day, index) == 1
    assert stenograms.resolve("V. ČMILYTĖ-NIELSEN", day, index) == 2
    assert stenograms.resolve("R. J. JANKŪNAS", day, index) == 3
    assert stenograms.resolve("M. GEDVILAS", day, index) == 5
    assert stenograms.resolve("V. BLINKEVIČIŪTĖ", day, index) is None  # mandate over
    assert stenograms.resolve("M. JAKUBAUSKIENĖ", day, index) is None  # not an MP
    assert stenograms.resolve(None, day, index) is None


def test_text_url_and_phase1_loader_keeps_sittings():
    portal = (
        "https://e-seimas.lrs.lt/portal/legalAct/lt/TAK/"
        "f5da17b08e6411f0b351ee31aa0a26b8"
    )
    assert stenograms.text_url(portal) == (
        "https://e-seimas.lrs.lt/rs/legalact/TAK/f5da17b08e6411f0b351ee31aa0a26b8/"
    )
    assert not any("sittings" in sql for sql in seimas.CLEAR_SQL)
    assert not any("TRUNCATE" in sql for sql in seimas.CLEAR_SQL)
    assert "ON CONFLICT (id) DO UPDATE" in seimas.SITTINGS_SQL
