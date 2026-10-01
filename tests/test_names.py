from ingest.names import declined_forms, normal


def test_normal_folds_case_and_diacritics():
    assert normal("  Čmilytė-Nielsen ") == "cmilyte nielsen"


def test_stem_alternation_is_generated():
    assert {
        "zemaitaitis",
        "zemaitaicio",
        "zemaitaiciui",
        "zemaitaiti",
    } <= declined_forms("Žemaitaitis")
    assert {"kuodis", "kuodzio", "kuodziui"} <= declined_forms("Kuodis")


def test_ordinary_words_are_not_forms_of_a_name():
    assert "balsavimas" not in declined_forms("Balsys")
    assert "balsas" not in declined_forms("Balsys")
    assert "aleknaviciene" not in declined_forms("Alekna")


def test_given_names_and_compound_surnames():
    assert "remigijaus" in declined_forms("Remigijus")
    assert "igno" in declined_forms("Ignas")
    assert "agnes" in declined_forms("Agnė")
    assert {"cmilyte", "cmilytes", "nielsen"} <= declined_forms("Čmilytė-Nielsen")
