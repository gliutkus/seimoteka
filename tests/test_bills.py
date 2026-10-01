from ingest import bills

# Shaped like the e-seimas HTML of a revised amending bill: a header down to the
# place line, bold article headings, the wording an article inserts in quotes
# (with an article heading of its own), a superscript article number, signature
# lines and who submits the bill.
AMENDMENT = """<html><head><style>p.MsoNormal {margin:0in}</style></head><body>
<div class="WordSection1">
<p class="MsoNormal"><b>projektas Nr. XVP-238 (2)</b></p>
<p class="MsoNormal"><b>LIETUVOS RESPUBLIKOS</b></p>
<p class="MsoNormal"><b>GAMTINIŲ DUJŲ ĮSTATYMO NR. VIII-1973 31, 37<sup>1</sup>
  STRAIPSNIŲ PAKEITIMO</b></p>
<p class="MsoNormal"><b>ĮSTATYMAS</b></p>
<p class="MsoNormal">2025 m. d. Nr.</p>
<p class="MsoNormal">Vilnius</p>
<p class="MsoNormal"><b>1 straipsnis. 31 straipsnio pakeitimas</b></p>
<p class="MsoNormal">Pakeisti 31 straipsnį ir jį išdėstyti taip:</p>
<p class="MsoNormal">„31 straipsnis. Operatoriaus vykdoma tinklo plėtra</p>
<p class="MsoNormal">1. Operatorius kas dvejus metus rengia planą.</p>
<p class="MsoNormal">32 straipsnis. Tinklo plėtros planas</p>
<p class="MsoNormal">Planas skelbiamas viešai.“</p>
<p class="MsoNormal"><b>2</b><span> straipsnis. 37<sup>1</sup> straipsnio
  pakeitimas</span></p>
<p class="MsoNormal">Pakeisti 37<sup>1</sup> straipsnį.</p>
<p class="MsoNormal">Skelbiu šį Lietuvos Respublikos Seimo priimtą įstatymą.</p>
<p class="MsoNormal">Respublikos Prezidentas</p>
<p class="MsoNormal">Teikia</p>
<p class="MsoNormal">Seimo narys Vardenis Pavardenis</p>
</div></body></html>"""

RESOLUTION = """<html><body>
<p><b>Projektas</b></p>
<p><b>LIETUVOS RESPUBLIKOS SEIMAS</b></p>
<p><b>NUTARIMAS</b></p>
<p>2026 m. d. Nr.</p>
<p>Vilnius</p>
<p>Lietuvos Respublikos Seimas, vadovaudamasis Seimo statuto 19 straipsniu,
  nutaria:</p>
<p><b>1</b> straipsnis.</p>
<p>Patvirtinti biudžetą (pridedama).</p>
<p>Seimo Pirmininkas</p>
<p>Teikia</p>
<p>Komiteto pirmininkas Vardenis Pavardenis</p>
<p>Lietuvos Respublikos Seimo nutarimo</p>
<p>priedas</p>
<p>BIUDŽETAS</p>
<p>Įplaukos</p>
<p>4 728 800</p>
</body></html>"""

TITLE = (
    "Gamtinių dujų įstatymo Nr. VIII-1973 31, 37-1 straipsnių pakeitimo "
    "įstatymo projektas (Nr. XVP-238(2))"
)


def test_articles_open_at_their_headings_but_not_inside_quoted_wording():
    sections = bills.parse(AMENDMENT)
    assert [s.heading for s in sections] == [
        "1 straipsnis. 31 straipsnio pakeitimas",
        "2 straipsnis. 37-1 straipsnio pakeitimas",
        "Teikia",
    ]
    assert sections[0].paragraphs[-2:] == [
        "32 straipsnis. Tinklo plėtros planas",
        "Planas skelbiamas viešai.“",
    ]
    assert sections[1].paragraphs == ["Pakeisti 37-1 straipsnį."]
    assert sections[2].paragraphs == ["Seimo narys Vardenis Pavardenis"]


def test_a_preamble_and_an_annex_are_sections_and_signatures_are_dropped():
    sections = bills.parse(RESOLUTION)
    assert [(s.heading, s.paragraphs) for s in sections] == [
        (
            "Preambulė",
            [
                "Lietuvos Respublikos Seimas, vadovaudamasis Seimo statuto 19 "
                "straipsniu, nutaria:"
            ],
        ),
        ("1 straipsnis.", ["Patvirtinti biudžetą (pridedama)."]),
        (
            "Teikia",
            [
                "Komiteto pirmininkas Vardenis Pavardenis",
                "Lietuvos Respublikos Seimo nutarimo",
            ],
        ),
        ("Priedas", ["priedas", "BIUDŽETAS", "Įplaukos", "4 728 800"]),
    ]


def test_every_chunk_opens_with_its_bill_and_section():
    pieces = bills.chunks_of(bills.parse(AMENDMENT), TITLE, "XVP-238(2)", limit=300)
    first = [text for heading, text in pieces if heading.startswith("1 straipsnis")]
    assert len(first) == 2  # a long article is split at paragraphs
    prefix = f"{TITLE}\n1 straipsnis. 31 straipsnio pakeitimas\n"
    assert all(text.startswith(prefix) for text in first)
    assert pieces[-1] == ("Teikia", f"{TITLE}\nTeikia\nSeimo narys Vardenis Pavardenis")


def test_a_long_title_is_cut_but_keeps_its_bill_number():
    title = "Įstatymo " + "labai ilgas pavadinimas " * 20 + "projektas"
    line = bills.heading_line(title, "XVP-9(3)")
    assert len(line) < bills.TITLE_CHARS + 20
    assert line.endswith("… (Nr. XVP-9(3))")
    assert bills.heading_line(TITLE, "XVP-238(2)") == TITLE


def test_a_bill_link_reads_its_text_from_the_same_route_as_a_stenogram():
    portal = "https://e-seimas.lrs.lt/portal/legalAct/lt/TAP/a7005027314711f0"
    assert bills.text_url(portal) == (
        "https://e-seimas.lrs.lt/rs/legalact/TAP/a7005027314711f0/"
    )


# A law restated in full is quoted with its own articles, bold and numbered from
# 1, so its article 2 carries the number the bill's own next article will have.
RESTATED = """<html><body>
<p>2026 m. d. Nr. Vilnius</p>
<p><b>1. Straipsnis. Tyrimų įstatymo nauja redakcija</b></p>
<p>Pakeisti Tyrimų įstatymą ir jį išdėstyti taip:</p>
<p>„LIETUVOS RESPUBLIKOS TYRIMŲ ĮSTATYMAS</p>
<p><b>1 straipsnis. Įstatymo paskirtis</b></p>
<p>Įstatymas nustato tyrimų tvarką.</p>
<p><b>2 straipsnis. Sąvokos</b></p>
<p>Tyrimas – veikla.“</p>
<p><b>2</b> straipsnis. Įstatymo įsigaliojimas</p>
<p>Šis įstatymas įsigalioja 2027 m. sausio 1 d.</p>
<p>Seimo Pirmininkas</p>
<p>Teikia Seimo nariai</p>
</body></html>"""

# The quote is closed with a straight mark, so it never closes for the parser.
UNCLOSED = """<html><body>
<p>Vilnius</p>
<p><b>1 straipsnis. 5 straipsnio pakeitimas</b></p>
<p>Pakeisti 5 straipsnį ir jį išdėstyti taip:</p>
<p>„5. Registras tvarkomas elektroniniu būdu."</p>
<p><b>2 straipsnis. Įstatymo įsigaliojimas</b></p>
<p>Šis įstatymas įsigalioja 2027 m. sausio 1 d.</p>
</body></html>"""

DECLARATION = """<html><body>
<p><b>REZOLIUCIJA</b></p>
<p>2026 m. d. Nr.</p>
<p>Vilnius</p>
<p>Lietuvos Respublikos Seimas,</p>
<p>primindamas bendrą istoriją,</p>
<p><b>sveikina</b> draugišką valstybę.</p>
</body></html>"""


def test_the_bills_own_articles_are_numbered_in_order_outside_quotes():
    sections = bills.parse(RESTATED)
    assert [s.heading for s in sections] == [
        "1. Straipsnis. Tyrimų įstatymo nauja redakcija",
        "2 straipsnis. Įstatymo įsigaliojimas",
        "Teikia",
    ]
    assert "2 straipsnis. Sąvokos" in sections[0].paragraphs
    assert sections[2].paragraphs == ["Seimo nariai"]


def test_a_quote_left_open_is_read_again_by_the_article_sequence():
    headings = [s.heading for s in bills.parse(UNCLOSED)]
    assert headings == [
        "1 straipsnis. 5 straipsnio pakeitimas",
        "2 straipsnis. Įstatymo įsigaliojimas",
    ]


def test_a_bill_without_articles_is_one_text():
    assert [(s.heading, s.paragraphs) for s in bills.parse(DECLARATION)] == [
        (
            "Tekstas",
            [
                "Lietuvos Respublikos Seimas,",
                "primindamas bendrą istoriją,",
                "sveikina draugišką valstybę.",
            ],
        )
    ]


def test_superscript_article_and_chapter_numbers_are_written_with_a_hyphen():
    from lxml import html as LH

    from ingest.text import clean, join_superscripts

    root = LH.fromstring(
        "<div><p>37<sup>1</sup> straipsnis, XVIII<sup>2</sup> skyrius, "
        "5 m<sup>2</sup></p></div>"
    )
    join_superscripts(root)
    assert clean(root.text_content()) == "37-1 straipsnis, XVIII-2 skyrius, 5 m2"
