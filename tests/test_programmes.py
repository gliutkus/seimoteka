from ingest import programmes

DISCLAIMER = """Kandidatų sąrašai gali pasikeisti Vyriausiajai rinkimų komisijai
mų kodekso reikalavimus neatitinkančius kandidatus arba panaikinus kandidatų
mą. Aktualius kandidatų sąrašus rasite www.vrk.lt ir balsavimo dieną."""

NUMBER_FIRST = f"""7
{DISCLAIMER}
Nr. 
RINKIMŲ  
PROGRAMA
1
Tautos ir teisingumo sąjunga 
(centristai, tautininkai) 
KELKIS, LIETUVA!
1. Partijų finansavimo, rinkimų sistemos reforma. Politikų atsakomybė piliečiams.
2. KGB bendradarbių sąrašai vieši. Renkami 
teisėjai, prokurorai."""

BULLETS = f"""8
{DISCLAIMER}
Nr. 
RINKIMŲ  
PROGRAMA
Nacionalinis susivienijimas 2
UŽ LIETUVĄ LIETUVIŠKĄ AMŽIAMS!
Užtikrinsime lietuviškos Lietuvos saugumą ir išlikimą
• Sustabdysime masinę imigraciją
• Remsime šeimas
Pertvarkysime valstybę tarnauti kiekvienam piliečiui
• Užtikrinsime pareigūnų atsakomybę už neteisingus 
sprendimus"""

PARAGRAPHS = f"""15
{DISCLAIMER}
RINKIMŲ  
PROGRAMA
Partija  
„Laisvė ir teisingumas“ Nr. 10
UŽ LIETUVĄ BE BAIMĖS!
Partija „Laisvė ir teisingumas“ telkia visuomenę už žmogaus 
gerovę, sveikatą. Atstovaujame centro dešiniųjų
rinkėjams.
Mūsų programa – Lietuvos išsaugojimas."""


def test_ballot_number_before_the_name_and_numbered_points():
    p = programmes.parse_page(NUMBER_FIRST, 8)
    assert (p.number, p.party, p.slogan) == (
        1,
        "Tautos ir teisingumo sąjunga (centristai, tautininkai)",
        "KELKIS, LIETUVA!",
    )
    assert p.units == [
        (
            None,
            "Partijų finansavimo, rinkimų sistemos reforma. "
            "Politikų atsakomybė piliečiams.",
            True,
        ),
        (None, "KGB bendradarbių sąrašai vieši. Renkami teisėjai, prokurorai.", True),
    ]


def test_bullets_keep_their_heading_and_wrapped_lines():
    p = programmes.parse_page(BULLETS, 9)
    assert (p.number, p.party) == (2, "Nacionalinis susivienijimas")
    first, second = (
        "Užtikrinsime lietuviškos Lietuvos saugumą ir išlikimą",
        ("Pertvarkysime valstybę tarnauti kiekvienam piliečiui"),
    )
    assert p.units == [
        (first, "Sustabdysime masinę imigraciją", True),
        (first, "Remsime šeimas", True),
        (second, "Užtikrinsime pareigūnų atsakomybę už neteisingus sprendimus", True),
    ]


def test_inline_ballot_number_and_paragraphs():
    p = programmes.parse_page(PARAGRAPHS, 16)
    assert (p.number, p.party, p.slogan) == (
        10,
        "Partija „Laisvė ir teisingumas“",
        "UŽ LIETUVĄ BE BAIMĖS!",
    )
    assert [text for _, text, _ in p.units] == [
        "Partija „Laisvė ir teisingumas“ telkia visuomenę už žmogaus gerovę, sveikatą. "
        "Atstovaujame centro dešiniųjų rinkėjams.",
        "Mūsų programa – Lietuvos išsaugojimas.",
    ]


def test_chunks_name_their_party_and_keep_points_whole():
    chunks = programmes.chunks_of(programmes.parse_page(BULLETS, 9))
    assert len(chunks) == 2  # one per heading
    assert all(
        c.startswith("Nacionalinis susivienijimas (sąrašo Nr. 2)") for c in chunks
    )
    assert "\nUžtikrinsime lietuviškos Lietuvos saugumą ir išlikimą:\n" in chunks[0]
    assert "• Sustabdysime masinę imigraciją\n• Remsime šeimas" in chunks[0]


TWO_LINE_HEADER = f"""18
{DISCLAIMER}
Nr. 
RINKIMŲ  
PROGRAMA
Tėvynės sąjunga-Lietuvos
krikščionys demokratai13
GINTI IR AUGINTI
PROGRAMA LIETUVOS ATEIČIAI
Drauge įveikėme daug iššūkių.
Pasisakome už:
• šeimą
• taiką"""


def test_two_line_slogan_and_a_heading_that_ends_in_a_colon():
    p = programmes.parse_page(TWO_LINE_HEADER, 19)
    assert (p.number, p.party) == (
        13,
        "Tėvynės sąjunga-Lietuvos krikščionys demokratai",
    )
    assert p.slogan == "GINTI IR AUGINTI / PROGRAMA LIETUVOS ATEIČIAI"
    assert p.units == [
        (None, "Drauge įveikėme daug iššūkių.", False),
        ("Pasisakome už:", "šeimą", True),
        ("Pasisakome už:", "taiką", True),
    ]
    chunk = programmes.chunks_of(p)[-1]
    assert "\nPasisakome už:\n• šeimą\n• taiką" in chunk
