from datetime import datetime
from xml.etree import ElementTree as ET

from ingest import seimas
from ingest.config import VOTE_CHOICES

VOTE_PAGE = """<html><body>
<div><a href="portal.show?p_a=sale_fakt_pos&p_fakt_pos_id=-502067">Rytinis</a></div>
<h1>Balsavimo rezultatai (2025-09-10, rytinis posėdis)</h1>
<div>Darbotvarkės klausimas
 <a href="portal.show?p_a=sale_klaus_stadija&p_svarst_kl_stad_id=-53194"
   >Seimo nutarimo „Dėl X“ projektas (Nr. XVP-717)</a>; pateikimas
 (<a href="#">dokumento tekstas</a>,
  <a href="portal.show?p_a=sale_klaus_detaliau&p_kl_stad_id=-97712"
   >detali informacija</a>)
</div>
<div><b>Formuluotė:</b> dėl pritarimo po pateikimo</div>
<div><b>Balsavimo laikas:</b> 10:26:57</div>
<div><b>Balsavo Seimo narių:</b> 127 iš 141.</div>
<div><b>Balsavimo rezultatai:</b> už - 90, prieš - 14, susilaikė - 23, pritarta.</div>
<table>
<tr><th>Seimo narys(-ė)</th><th>Frakcija</th>
    <th>Už</th><th>Prieš</th><th>Susil.</th></tr>
<tr><td><a href="portal.show?p_a=498&p_asm_id=79162">Alekna Virgilijus</a></td>
    <td>LSF</td><td></td><td></td><td>+</td></tr>
<tr><td><a href="portal.show?p_a=498&p_asm_id=79165">Ąžuolas Valius</a></td>
    <td>LVŽ-KŠSF</td><td></td><td></td><td></td></tr>
<tr><td><a href="portal.show?p_a=498&p_asm_id=7193">Olekas Juozas</a></td>
    <td></td><td>+</td><td></td><td></td></tr>
</table></body></html>"""


def test_vote_page_links_the_vote_to_its_agenda_stage():
    page = seimas.parse_vote_page(VOTE_PAGE)
    assert page["sitting_id"] == -502067
    assert page["agenda_item_id"] == -97712
    assert page["agenda_line_id"] == -53194
    assert page["question"] == (
        "Seimo nutarimo „Dėl X“ projektas (Nr. XVP-717); pateikimas"
    )
    assert page["formulation"] == "dėl pritarimo po pateikimo"
    assert page["voted"] == (127, 141)
    assert page["tally"] == (90, 14, 23)
    assert page["result"] == "pritarta"
    assert page["time"].isoformat() == "10:26:57"


def test_vote_page_rows_carry_marks_and_as_of_vote_fraction():
    rows = seimas.parse_vote_page(VOTE_PAGE)["rows"]
    assert rows[79162] == ("LSF", (False, False, True))
    # fraction labels contain Lithuanian capitals; a blank row did not vote
    assert rows[79165] == ("LVŽ-KŠSF", (False, False, False))
    # no fraction at the time of the vote is a real state, not a parse failure
    assert rows[7193] == (None, (True, False, False))


def test_procedural_vote_page_with_empty_question():
    page = VOTE_PAGE.replace(
        "Seimo nutarimo „Dėl X“ projektas (Nr. XVP-717)</a>; pateikimas",
        "</a>(Nr. )",
    )
    assert seimas.parse_vote_page(page)["question"] is None


def test_abstention_and_non_voting_are_different_choices():
    assert seimas.MARKS[VOTE_CHOICES["Susilaikė"]] == (False, False, True)
    assert VOTE_CHOICES[""] == "nebalsavo"
    assert seimas.MARKS["nebalsavo"] == (False, False, False)


ROSTER = """<SeimoInformacija><SeimoKadencija kadencijos_id="10">
<SeimoNarys asmens_id="1" vardas="Agnė" pavardė="Testaitė" lytis="M"
  data_nuo="2024-11-14" data_iki="" kadencijų_skaičius="2" biografijos_nuoroda="u"
  iškėlusi_partija="Tėvynės sąjunga – Lietuvos krikščionys demokratai"
  išrinkimo_būdas=" Nalšios pietinė (Nr. 53) apygardoje">
  <Pareigos padalinio_id="1022" pareigos="Frakcijos narė" data_nuo="2024-11-14"
    data_iki="2026-04-13"
    padalinio_pavadinimas="Tėvynės sąjungos-Lietuvos krikščionių demokratų frakcija"/>
  <Pareigos padalinio_id="47" padalinio_pavadinimas="Mišri Seimo narių grupė"
    pareigos="Frakcijos narė" data_nuo="2026-04-14" data_iki=""/>
  <Pareigos padalinio_id="870" padalinio_pavadinimas="Liberalų  sąjūdžio frakcija"
    pareigos="Frakcijos seniūnė" data_nuo="2024-11-14" data_iki="2024-12-01"/>
  <Pareigos padalinio_id="876" padalinio_pavadinimas="Audito komitetas"
    pareigos="Komiteto narė" data_nuo="2024-11-19" data_iki=""/>
  <Pareigos padalinio_id="" padalinio_pavadinimas=""
    pareigos="Seimo Pirmininko pavaduotoja" data_nuo="2024-11-14" data_iki=""/>
  <Pareigos parlamentinės_grupės_id="32" parlamentinės_grupės_pavadinimas="Grupė"
    pareigos="Narė" data_nuo="2024-12-05" data_iki=""/>
</SeimoNarys>
<SeimoNarys asmens_id="2" vardas="Jonas" pavardė="Sąrašaitis" lytis="V"
  data_nuo="2024-11-14" data_iki="2025-01-01" kadencijų_skaičius="1"
  biografijos_nuoroda="u" iškėlusi_partija="Išsikėlė pats"
  išrinkimo_būdas="Pagal sąrašą"/>
</SeimoKadencija></SeimoInformacija>"""


def test_roster_membership_follows_the_role_not_the_body_name():
    rec = seimas.Record()
    seimas.read_members(ET.fromstring(ROSTER), rec)
    fractions = {(mp, fid, role) for mp, fid, role, _, _ in rec.memberships}
    # feminine roles and the mixed group both count as fraction membership
    assert fractions == {
        (1, 1022, "Frakcijos narė"),
        (1, 47, "Frakcijos narė"),
        (1, 870, "Frakcijos seniūnė"),
    }
    assert rec.fraction_names[870] == {
        "Liberalų sąjūdžio frakcija": datetime(2024, 11, 14).date()
    }
    positions = {(body, role) for _, _, body, role, _, _ in rec.positions}
    assert positions == {
        ("Audito komitetas", "Komiteto narė"),
        ("Seimas", "Seimo Pirmininko pavaduotoja"),
    }


def test_roster_member_fields_are_normalised():
    rec = seimas.Record()
    seimas.read_members(ET.fromstring(ROSTER), rec)
    agne, jonas = rec.mps
    assert agne["nominated_by"] == "Tėvynės sąjunga-Lietuvos krikščionys demokratai"
    assert (agne["elected_via"], agne["constituency"], agne["constituency_no"]) == (
        "constituency",
        "Nalšios pietinė",
        53,
    )
    assert {"testaite", "testaites", "agnes"} <= set(agne["name_variants"])
    assert (jonas["elected_via"], jonas["constituency"]) == ("list", None)
    assert jonas["date_to"].isoformat() == "2025-01-01"
    assert not rec.notes


def test_bill_numbers():
    assert seimas.bill_number("projektas (Nr. XVP-1(2))") == "XVP-1(2)"
    assert seimas.bill_number(None, "dėl įrašymo projektą Nr. XVP-217") == "XVP-217"
    assert seimas.bill_number("Seimo narių pareiškimai") is None


def test_sitting_by_time_window():
    t = lambda h, m: datetime(2025, 9, 10, h, m, tzinfo=seimas.VILNIUS)  # noqa: E731
    index = seimas.sittings_by_day(
        [{"id": 1, "starts_at": t(10, 0)}, {"id": 2, "starts_at": t(14, 0)}]
    )
    assert seimas.sitting_at(t(13, 59), index) == 1
    assert seimas.sitting_at(t(14, 5), index) == 2
    assert seimas.sitting_at(t(9, 40), index) == 1  # chamber opens a little early
    assert (
        seimas.sitting_at(datetime(2025, 9, 11, 11, 0, tzinfo=seimas.VILNIUS), index)
        is None
    )


def test_sitting_page_lines_are_ordered_and_unique():
    page = """<html><body>
      <a href="x?p_svarst_kl_stad_id=-2">Antras</a>
      <a href="x?p_svarst_kl_stad_id=-3">Trečias</a>
      <a href="x?p_svarst_kl_stad_id=-2">Antras</a>
    </body></html>"""
    assert seimas.sitting_lines(page) == [(-2, "Antras"), (-3, "Trečias")]


def test_vote_walk_crosses_holes_longer_than_the_edge_rule(monkeypatch):
    first = seimas.TERM10_VOTE_FIRST
    present = {first, first - 40}  # a 39-id hole inside the harvested range

    def fake_fetch(url, *, suffix=".xml", offline=False, **kwargs):
        vote_id = int(url.rsplit("=", 1)[1])
        if vote_id in present:
            return (
                f'<SeimoInformacija><SeimoNariųBalsavimas balsavimo_id="{vote_id}">'
                '<BendriBalsavimoRezultatai balsavimo_laikas="2024-11-14 15:44:15" '
                'balsavo="0" viso="141" už="0" prieš="0" susilaikė="0"/>'
                "</SeimoNariųBalsavimas></SeimoInformacija>"
            )
        return "<SeimoInformacija/>" if first - 40 < vote_id < first else None

    monkeypatch.setattr(seimas, "fetch", fake_fetch)
    monkeypatch.setattr(seimas, "vote_range_end", lambda: first - 40)
    holes = {}
    assert [v["id"] for v, _, _ in seimas.iter_votes(holes)] == [first, first - 40]
    assert len(holes["no results"]) == 39


ALTERNATIVE_PAGE = """<html><body>
<div><a href="x?p_a=sale_fakt_pos&p_fakt_pos_id=-502088">Posėdis</a></div>
<div>Darbotvarkės klausimas <a href="x?p_svarst_kl_stad_id=-53680">PVM įstatymo
  projektas (Nr. XVP-800)</a>; pateikimas
  (<a href="x?p_a=sale_klaus_detaliau&p_kl_stad_id=-98321">detali informacija</a>)</div>
<div>Formuluotė A: už pasiūlymą grąžinti šį projektą iniciatoriams tobulinti</div>
<div>Formuluotė B: už pasiūlymą jį atmesti</div>
<div>Balsavimo laikas: 16:26:41</div>
<div>Balsavo Seimo narių: 98 iš 141.</div>
<div>Balsavimo rezultatai: A - 39, B - 59, susilaikė - 0, pritarta formuluotei B.</div>
<table>
<tr><th>Seimo narys(-ė)</th><th>Frakcija</th><th>A</th><th>B</th><th>Susil.</th></tr>
<tr><td><a href="x?p_asm_id=65703">Žemaitaitis Remigijus</a></td>
    <td>NAF</td><td></td><td>+</td><td></td></tr>
</table></body></html>"""


def test_alternative_votes_are_motion_a_against_motion_b():
    page = seimas.parse_vote_page(ALTERNATIVE_PAGE)
    assert page["kind"] == "alternative"
    assert page["formulation"] is None
    assert (
        page["formulation_a"]
        == "už pasiūlymą grąžinti šį projektą iniciatoriams tobulinti"
    )
    assert page["formulation_b"] == "už pasiūlymą jį atmesti"
    assert (page["tally"], page["result"]) == ((39, 59, 0), "pritarta formuluotei B")
    assert page["rows"][65703] == ("NAF", (False, True, False))
    # the XML's "Prieš" on this vote is a vote for motion B, never "against"
    assert seimas.choice_code("Prieš", "alternative") == "b"
    assert seimas.choice_code("Prieš", "standard") == "pries"
    assert seimas.MARKS[seimas.choice_code("Prieš", "alternative")] == (
        False,
        True,
        False,
    )


TWICE_PAGE = """<html><body>
<div>Balsavimo laikas: 11:02:10</div>
<div>Balsavo Seimo narių: 3 iš 4.</div>
<div>Balsavimo rezultatai: už - 2, prieš - 0, susilaikė - 0, pritarta.
Individualūs balsavimo rezultatai, gauti elektroninėmis priemonėmis, neatitinka
suminių rezultatų, įrašytų protokole.</div>
<table>
<tr><th>Seimo narys(-ė)</th><th>Frakcija</th><th>Už</th><th>Prieš</th><th>Susil.</th>
</tr>
<tr><td><a href="x?p_asm_id=1">Pirmas Narys</a></td><td>NAF</td>
    <td>+</td><td></td><td></td></tr>
<tr><td><a href="x?p_asm_id=1">Pirmas Narys</a></td><td>DFVL</td>
    <td>+</td><td></td><td></td></tr>
<tr><td><a href="x?p_asm_id=2">Antras Narys</a></td><td>LSF</td>
    <td>+</td><td></td><td></td></tr>
<tr><td><a href="x?p_asm_id=2">Antras Narys</a></td><td>LSF</td>
    <td></td><td></td><td></td></tr>
</table></body></html>"""


def test_members_listed_twice_are_merged():
    page = seimas.parse_vote_page(TWICE_PAGE)
    # listed under both fractions on the day of a switch: fraction unknown
    assert page["rows"][1] == (None, (True, False, False))
    # listed twice on a term's first sitting, marked on one row only
    assert page["rows"][2] == ("LSF", (True, False, False))
    assert page["listed_twice"] == {1: ["NAF", "DFVL"], 2: ["LSF", "LSF"]}
    assert page["protocol_notice"]
    assert page["tally"] == (2, 0, 0)


def test_the_portals_not_found_page_is_rejected():
    assert not seimas.portal_page("<h1>Puslapis nerastas</h1>")
    assert seimas.portal_page(TWICE_PAGE)
