from ingest import tallies


def read(text: str):
    return tallies.announcements(text)


def test_announcements_in_either_word_order():
    numbers_first = (
        "Balsavo 127 Seimo nariai: 90 – už, 14 – prieš, 23 susilaikė. Pritarta."
    )
    words_first = "Balsavo 122 Seimo nariai: už – 83, prieš – 14, susilaikė – 25."
    assert read(numbers_first)[0] == [(127, 90, 14, 23)]
    assert read(words_first)[0] == [(122, 83, 14, 25)]


def test_unstated_parts_are_zero_only_when_the_rest_adds_up():
    assert read("Balsavo 60 Seimo narių: už – 60, prieš ir susilaikiusių nėra.")[0] == [
        (60, 60, 0, 0)
    ]
    assert read("Balsavo 1 Seimo narys: už – 1.")[0] == [(1, 1, 0, 0)]
    assert read("Balsavo 70 Seimo narių: už – 60.") == ([], 1, 0)  # never guessed


def test_registrations_are_counted_separately():
    text = (
        "Registruojamės. Užsiregistravo 128 Seimo nariai. "
        "Užsiregistravo 71 Seimo narys."
    )
    assert read(text) == ([], 0, 2)


def test_each_announcement_corroborates_one_vote():
    votes = [(-1, (127, 90, 14, 23)), (-2, (127, 90, 14, 23)), (-3, (50, 50, 0, 0))]
    announced = [(127, 90, 14, 23), (10, 10, 0, 0)]
    assert tallies.match_tallies(votes, announced) == {
        "corroborated": [-1],
        "unannounced": [-2, -3],
        "announced_without_vote": 1,
    }


def test_chairs_who_omit_the_members_or_name_the_motion():
    short = "Balsavo 102: už – 71, prieš – 14, susilaikė 17. Pasiūlymui pritarta."
    motion = "Balsavo 107 Seimo nariai: už pertrauką – 86, prieš – 19, susilaikė 2."
    members = "Balsavo 106 Seimo nariai: 14 Seimo narių – už, 73 – prieš, susilaikė 19."
    assert read(short)[0] == [(102, 71, 14, 17)]
    assert read(motion)[0] == [(107, 86, 19, 2)]
    assert read(members)[0] == [(106, 14, 73, 19)]


def test_a_vote_announced_with_its_registration_count():
    text = (
        "Užsiregistravo 83 Seimo nariai, balsavo 82: už – 73, prieš – 3, "
        "susilaikė 6. Po pateikimo yra pritarta."
    )
    assert read(text) == ([(82, 73, 3, 6)], 0, 0)  # a vote, not a registration


def test_announcements_that_everyone_who_voted_voted_for():
    assert read("Balsavo 98 Seimo nariai, visi – už. Pritarta.")[0] == [(98, 98, 0, 0)]
    none_against = "Balsavo 60 Seimo narių, prieš ir susilaikiusių nebuvo."
    assert read(none_against)[0] == [(60, 60, 0, 0)]
    typo = "Balsavo 90 Seimo narių: už – 80, prieš – 5, sulaikė 5."
    assert read(typo)[0] == [(90, 80, 5, 5)]
