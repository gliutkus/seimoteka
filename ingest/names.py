"""Lithuanian personal names: folding and declension, shared by every consumer.

Users type "Žemaitaičio"; the record says "Žemaitaitis". Retrieval, loaders and
discovery all need the same answer to "which forms can this name take", so it
lives here once.
"""

from __future__ import annotations

import re
import unicodedata


def normal(text: str) -> str:
    """Casefold, strip diacritics, keep word characters separated by spaces."""
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.findall(r"\w+", text))


def declined_forms(name: str) -> set[str]:
    """Case forms of a Lithuanian name, diacritic-folded like normal().

    Works for surnames and given names alike. The -is class alternates the stem
    consonant before an -i ending (Žemaitaitis -> Žemaitaičio, Kuodis ->
    Kuodžio), which prefix stemming misses; generating whole forms also keeps
    ordinary words out (balsavimas is not a form of Balsys).
    """
    forms: set[str] = set()
    for part in normal(name).split():
        forms.add(part)
        if part.endswith("ius"):
            stem = part[:-3]
            forms |= {stem + e for e in ("ius", "iaus", "iui", "iu", "iumi")}
        elif part.endswith("as"):
            stem = part[:-2]
            forms |= {stem + e for e in ("as", "o", "ui", "a", "u", "e")}
        elif part.endswith("ys"):
            stem = part[:-2]
            forms |= {stem + e for e in ("ys", "io", "iui", "i", "iu")}
        elif part.endswith("us"):
            stem = part[:-2]
            forms |= {stem + e for e in ("us", "aus", "ui", "u", "umi")}
        elif part.endswith("is"):
            stem = part[:-2]
            if stem.endswith("t"):
                alt = stem[:-1] + "c"
            elif stem.endswith("d"):
                alt = stem[:-1] + "dz"
            else:
                alt = stem
            forms |= {stem + "is", stem + "i"}
            forms |= {alt + e for e in ("io", "iui", "iu")}
        elif part.endswith("e"):
            stem = part[:-1]
            forms |= {stem + e for e in ("e", "es", "ei")}
        elif part.endswith("a"):
            stem = part[:-1]
            forms |= {stem + e for e in ("a", "os", "ai", "as")}
    return forms


def person_at(words: list[str], start: int, people) -> tuple[int, int] | None:
    """(id, end) of the one person whose given names and surname take the printed
    forms at words[start:end]; None when nobody, or more than one person, does.

    `words` are normal()-folded words as printed, possibly in another case and
    surrounded by other text ("nario karolio neimanto veiklos"). `people` holds
    (id, first_name, last_name). Both names must match: a surname alone would
    confuse namesakes such as Artūras and Paulius Visockas.
    """
    found = set()
    for person_id, first, last in people:
        given, family = normal(first).split(), normal(last).split()
        end = start + len(given) + len(family)
        printed_given = words[start : start + len(given)]
        printed_family = words[start + len(given) : end]
        if (
            len(printed_family) == len(family)
            and set(printed_given) <= declined_forms(first)
            and set(printed_family) <= declined_forms(last)
        ):
            found.add((person_id, end))
    return found.pop() if len(found) == 1 else None
