"""Find the MP a user means, however the name is written.

Users write surnames in any grammatical case, with or without diacritics, with
or without a given name: "Žemaitaičio", "zemaitaitis", "Remigijus Žemaitaitis".
Every surname part must appear in one of its declined forms; a given name, when
present, narrows the match. When a surname alone fits several MPs, all of them
are returned so the caller can ask which one was meant.

A text that is only a name must not be matched on its surname alone when its
other words are no form of the MP's names: "Artūras Visockas" is not Paulius
Visockas, and "Vaida Alekna" is not Virgilijus Alekna. `other_words` says so.
"""

from __future__ import annotations

from dataclasses import dataclass

from ingest.names import declined_forms, normal


@dataclass(frozen=True)
class Candidate:
    id: int
    full_name: str
    given_name_matched: bool
    # The text has a word that is no form of this MP's surname or given names, nor
    # a given name's initial. Where the text is only a name, it names someone else.
    other_words: bool = False


def rank_mps(words: list[str], people) -> list[Candidate]:
    """people: (id, first_name, last_name, full_name). Folded words as typed."""
    typed = set(words)
    found = []
    for person_id, first, last, full_name in people:
        surname_parts = normal(last).split()
        if not all(typed & declined_forms(part) for part in surname_parts):
            continue
        given = bool(typed & declined_forms(first))
        initials = {part[0] for part in normal(first).split()}
        other = bool(typed - declined_forms(last) - declined_forms(first) - initials)
        found.append(Candidate(person_id, full_name, given, other))
    if any(c.given_name_matched for c in found):
        found = [c for c in found if c.given_name_matched]
    return found


def find_mps(conn, text: str) -> list[Candidate]:
    words = normal(text).split()
    people = conn.execute(
        "SELECT id, first_name, last_name, full_name FROM mps "
        "WHERE name_variants && %s",
        (words,),
    ).fetchall()
    return rank_mps(words, people)
