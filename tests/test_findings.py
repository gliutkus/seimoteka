from lxml import html as LH

from ingest import ethics, vtek
from ingest.names import normal, person_at
from ingest.text import blocks, group_blocks

PEOPLE = [
    (1, "Remigijus", "Žemaitaitis"),
    (2, "Paulius", "Visockas"),
    (3, "Dalia", "Asanavičiūtė-Gružauskienė"),
    (4, "Rimas Jonas", "Jankūnas"),
    (5, "Karolis", "Neimantas"),
    (6, "Viktorija", "Čmilytė-Nielsen"),
    (7, "Vytautas", "Juozapaitis"),
    (8, "Jurgita", "Sejonienė"),
]


def test_blocks_read_list_items_and_skip_containers():
    root = LH.fromstring(
        "<div><p>Pirma</p><ol><li><p>Punktas</p></li><li>Antras</li></ol></div>"
    )
    assert blocks(root) == ["Pirma", "Punktas", "Antras"]
    placeholder = LH.fromstring("<div><p>.</p><p>Tekstas.</p></div>")
    assert blocks(placeholder) == ["Tekstas."]
    assert group_blocks(["a" * 6, "b" * 6, "c" * 6], limit=13) == [
        "aaaaaa\nbbbbbb",
        "cccccc",
    ]


def test_people_match_only_when_both_names_decline_to_the_same_person():
    def named(text: str) -> int | None:
        hit = person_at(normal(text).split(), 0, PEOPLE)
        return hit[0] if hit else None

    assert named("Remigijaus Žemaitaičio įrašo") == 1
    assert named("Karolio Neimanto veiklos") == 5
    assert named("Dalios Asanavičiūtės-Gružauskienės") == 3
    assert named("Rimo Jono Jankūno") == 4
    assert named("Viktorijos Čmilytės-Nielsen pasisakymo") == 6
    assert named("Artūro Visocko") is None  # a namesake, not the MP
    words = normal("Seimo nario Karolio Neimanto").split()
    assert person_at(words, 2, PEOPLE) == (5, 4)  # the name ends before word 4


def test_vtek_keeps_exact_name_matches_and_states_legal_status():
    rows = [
        {"id": 1, "vardas": "Remigijus", "pavarde": "Žemaitaitis"},
        {"id": 2, "vardas": "Artūras", "pavarde": "Visockas"},
        {"id": 3, "vardas": "Nebeskelbiama", "pavarde": "Nebeskelbiama"},
    ]
    assert [(r["id"], mp) for r, mp in vtek.subject_matches(rows, PEOPLE)] == [(1, 1)]
    decision = {
        "numeris": "KS-2026/69",
        "priemimoData": "2026-06-09",
        "vardas": "Inga",
        "pavarde": "Ruginienė",
        "teisesAktasPavadinimas": "Viešųjų ir privačių interesų derinimo įstatymas",
        "apskustasTeismui": True,
        "panaikintasTeismoSprendimu": False,
        "rezoliucineDalis": (
            "<p>n u s p r e n d ė:</p><ol><li>Pripažinti, kad pažeidė.</li></ol>"
        ),
        "sprendimas": "<p>SPRENDIMAS</p><p>Aplinkybės.</p>",
        "atskirojiNuomone": None,
    }
    chunks = vtek.chunks_of(decision)
    assert all(
        "Apskųstas teismui: taip. Panaikintas teismo sprendimu: ne." in c
        for c in chunks
    )
    assert (
        "Rezoliucinė dalis:\nn u s p r e n d ė:\nPripažinti, kad pažeidė." in chunks[0]
    )


PAGE = """<html><body><div id="document-text-container1">
<h3>Komisijos išvada „Dėl Lietuvos Respublikos Seimo nario
  Remigijaus Žemaitaičio įrašo"</h3>
<p>LIETUVOS RESPUBLIKOS SEIMO</p><p>ETIKOS IR PROCEDŪRŲ KOMISIJA</p><p>IŠVADA</p>
<p>2026-04-22 Nr. 101-I-31</p><p>Vilnius</p>
<p>Komisija nustatė:</p><p>• pirma aplinkybė;</p>
<p>1. Seimo narys pažeidė kodeksą.</p>
</div></body></html>"""


def test_ethics_page_parses_title_number_body_and_subject():
    finding = ethics.parse_page(PAGE)
    assert finding["title"].startswith("Komisijos išvada „Dėl")
    assert (finding["date"], finding["number"]) == ("2026-04-22", "101-I-31")
    assert finding["body"] == [
        "Komisija nustatė:",
        "• pirma aplinkybė;",
        "1. Seimo narys pažeidė kodeksą.",
    ]
    assert ethics.subjects(finding["title"], PEOPLE) == [1]
    assert (
        ethics.subjects("Komisijos išvada „Dėl Seimo statuto 92 str. taikymo“", PEOPLE)
        == []
    )
    assert ethics.chunks_of(finding)[0].startswith(
        "Seimo Etikos ir procedūrų komisijos išvada "
        "Nr. 101-I-31, 2026-04-22: Komisijos išvada"
    )


def test_findings_name_members_behind_their_post_and_together():
    # The first two titles are published term-10 findings that were once left
    # without a subject, which hid them from a search for either member.
    title = "Komisijos išvada „Dėl Lietuvos Respublikos Seimo {}“"
    cases = [
        ("narės, Seimo Pirmininko pavaduotojos Viktorijos Čmilytės-Nielsen", [6]),
        ("narių Vytauto Juozapaičio ir Jurgitos Sejonienės elgesio", [7, 8]),
        ("nario Remigijaus Žemaitaičio, Karolio Neimanto pasisakymų", [1, 5]),
        ("Audito komiteto pirmininko veiksmų", []),  # a post names nobody
        ("narių teisės steigti įmonę", []),
        ("nario Artūro Visocko elgesio", []),  # a namesake of an MP
    ]
    for rest, expected in cases:
        assert ethics.subjects(title.format(rest), PEOPLE) == expected, rest


def test_docx_text_is_read_from_its_xml():
    import io
    import zipfile

    from ingest.text import docx_blocks

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>VYRIAUSIOJI TARNYBINĖS </w:t></w:r>"
        "<w:r><w:t>ETIKOS KOMISIJA</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>.</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Aplinkybės.</w:t></w:r></w:p></w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    assert docx_blocks(buffer.getvalue()) == [
        "VYRIAUSIOJI TARNYBINĖS ETIKOS KOMISIJA",
        "Aplinkybės.",
    ]


def test_vtek_takes_the_reasoning_from_the_attachment_when_the_register_has_none():
    decision = {
        "numeris": "KS-1",
        "priemimoData": "2026-06-09",
        "vardas": "Inga",
        "pavarde": "Ruginienė",
        "teisesAktasPavadinimas": "Įstatymas",
        "apskustasTeismui": True,
        "panaikintasTeismoSprendimu": False,
        "rezoliucineDalis": "<p>Pripažinti.</p>",
        "sprendimas": "<p>.</p>",
        "dokumentai": [{"id": 1}],
        "atskirojiNuomone": None,
    }
    assert vtek.needs_attachment(decision)
    chunks = vtek.chunks_of(decision, ["Motyvai."])
    assert chunks[-1].endswith("\nMotyvai.")
    assert "Apskųstas teismui: taip." in chunks[-1]
