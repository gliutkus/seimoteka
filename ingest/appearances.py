"""Which MPs a YouTube video's title and description name as taking part.

This is a guess for a person to check, never an attribution: the review page
fills in the MPs guessed here, and the person listening decides who speaks.
It reads the conventions of Lithuanian talk-show metadata: a title segment that
is only a name ("Žemaitaitis per OpTV", "Agnė Širinskienė"), a name beside words
such as "tiesiogiai su", "svečiuose", "kalbina" or "interviu", and clips whose
description links the full show ("Visa laida yra čia: <link>"), which names its
guests. A name beside a verb of saying ("sako", "teigia") is a weaker sign that
the MP speaks; a name anywhere else is only a mention.

MPs are found by their full name in any case, or by a surname in any case when
no other MP has it. A surname two MPs share (Gedvilas) is never enough.
"""

from __future__ import annotations

import re

from .names import declined_forms, normal, person_at

# Words that sit beside the name of someone taking part (diacritic-folded). In a
# description "su" (with) alone says little, so there it counts only after
# "pokalbis", "interviu" or "tiesiogiai"; in a title it counts alone.
APPEARING = {
    "svecias", "sveciai", "sveciuose", "svecia", "dalyvauja", "studijoje",
    "tiesiogiai", "kalbina", "interviu", "pokalbis", "pokalbyje", "pokalbio",
}  # fmt: skip
TITLE_APPEARING = APPEARING | {"su", "optv", "per"}
# Words that introduce the guests: every name after them in the sentence takes
# part ("kalbina seimo narį, socialdemokratą Algirdą Sysą").
INTRODUCING = {"kalbina", "sveciuose", "dalyvauja", "studijoje"}
WITH = re.compile(r"\b(?:pokalbis|pokalbyje|interviu|tiesiogiai) su$")
# Verbs of saying: the MP is described as speaking, perhaps elsewhere.
SAYING = {
    "sako", "teigia", "pareiske", "kalba", "prabilo", "demaskuoja", "perspeja",
    "sukerta", "siulo", "kviecia", "aiskina", "papasakojo", "pasakoja",
    "atskleidzia", "kritikuoja", "ragina", "suskaiciavo", "isitikines", "klausia",
    "pasakes", "priduria", "tvirtina",
}  # fmt: skip
# Words a title segment may hold besides a name and still be only that name.
FILLER = {"per", "optv", "tiesiogiai", "su", "ir", "interviu", "pokalbis", "live"}
WINDOW = 3  # words on either side of a name that are read for cues
TITLE_SEGMENTS = re.compile(r"[|:–—\-\"“”„«»!?()]+")
SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
LINKED = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:live/|watch\?v=|shorts/))([\w-]{11})"
)
RANK = {"appears": 2, "said": 1, "mentioned": 0}


def people_index(members: list[dict]) -> tuple[list[tuple], dict[str, str]]:
    """(id, given, surname) for full names, and each surname form that only one
    MP's surname takes, mapped to that MP."""
    people, owners = [], {}
    for mp in members:
        surname = mp["surname"]
        given = (
            mp["name"][: -len(surname)].strip() if mp["name"].endswith(surname) else ""
        )
        people.append((mp["id"], given, surname))
        for form in declined_forms(surname):
            owners.setdefault(form, set()).add(mp["id"])
    unique = {form: next(iter(ids)) for form, ids in owners.items() if len(ids) == 1}
    return people, unique


def mentions(text: str, people: list[tuple], unique: dict) -> list[tuple]:
    """(mp id, first word, end word, "full name" | "surname") in folded words."""
    words = normal(text).split()
    found, start = [], 0
    while start < len(words):
        hit = person_at(words, start, people)
        if hit:
            found.append((hit[0], start, hit[1], "full name"))
            start = hit[1]
        elif words[start] in unique:
            found.append((unique[words[start]], start, start + 1, "surname"))
            start += 1
        else:
            start += 1
    return found


def role(
    words: list[str],
    start: int,
    end: int,
    cues: set[str] = APPEARING,
    lead: list[str] | None = None,
) -> str:
    """How a name is used, from the words around it; `lead` holds words just
    before its text, such as the title segment before a dash."""
    context, offset = (lead or []) + words, len(lead or [])
    before = context[max(0, offset + start - WINDOW) : offset + start]
    near = set(before + words[end : end + WINDOW])
    if near & cues or WITH.search(" ".join(before)):
        return "appears"
    if set(context[: offset + start]) & INTRODUCING:
        return "appears"
    if near & SAYING:
        return "said"
    return "mentioned"


def evidence(video: dict, people: list[tuple], unique: dict) -> list[dict]:
    """What the title and description say about each MP they name."""
    found = []
    title = video.get("title") or ""
    segments = [s for s in TITLE_SEGMENTS.split(title) if normal(s)]
    for index, segment in enumerate(segments):
        words = normal(segment).split()
        for mp, start, end, match in mentions(segment, people, unique):
            rest = set(words[:start] + words[end:])
            if rest <= FILLER:
                kind, where = "appears", "title segment"
            else:
                lead = normal(segments[index - 1]).split() if index else []
                kind = role(words, start, end, TITLE_APPEARING, lead)
                where = "title"
                if kind == "mentioned" and index == 0 and start == 0:
                    # A title opening with the name makes the MP its subject:
                    # "Žemaitaitis išklojo, ką...". A weaker sign than a guest.
                    kind = "said"
            found.append((mp, kind, match, where, segment.strip()))
    for sentence in SENTENCES.split(video.get("description") or ""):
        words = normal(sentence).split()
        for mp, start, end, match in mentions(sentence, people, unique):
            found.append((mp, role(words, start, end), match, "description", sentence))
    return [
        {"mp_id": mp, "role": kind, "match": match, "where": where, "text": text[:200]}
        for mp, kind, match, where, text in found
    ]


def classify(
    video: dict, members: list[dict], full_shows: dict[str, dict] | None = None
) -> dict | None:
    """The participants entry for one video: its status, the MPs to fill in and
    the evidence; None when it names no MP at all. `full_shows` holds entries
    already made for the videos a clip's description links to."""
    people, unique = people_index(members)
    names = {mp["id"]: mp["name"] for mp in members}
    seen = evidence(video, people, unique)
    for item in seen:
        item["mp"] = names[item.pop("mp_id")]
    best: dict[str, str] = {}
    for item in seen:
        if RANK[item["role"]] > RANK.get(best.get(item["mp"]), -1):
            best[item["mp"]] = item["role"]
    ids = {name: mp_id for mp_id, name in names.items()}
    linked = [
        vid
        for vid in dict.fromkeys(LINKED.findall(video.get("description") or ""))
        if vid != video.get("video_id")
    ]
    appearing = [name for name, kind in best.items() if kind == "appears"]
    if appearing:
        full = any(
            i["match"] == "full name" and i["mp"] in appearing
            for i in seen
            if i["role"] == "appears"
        )
        status = (
            "names who appears (full name)"
            if full
            else "names who appears (surname only one MP has)"
        )
    else:
        inherited = [
            name
            for vid in linked
            for name in ((full_shows or {}).get(vid) or {}).get("appearing") or []
        ]
        appearing = list(dict.fromkeys(inherited))
        saying = [name for name, kind in best.items() if kind == "said"]
        if appearing:
            status = "clip: its full show names who appears"
        elif saying:
            appearing = saying
            status = "described as speaking (weaker)"
        elif linked:
            status = "clip: full show linked, nobody named as appearing"
        elif best:
            status = "MPs only mentioned"
        else:
            return None
    return {
        "status": status,
        "appearing": appearing,
        "appearing_mp_ids": [ids[name] for name in appearing],
        "full_shows": linked,
        "evidence": seen,
    }
