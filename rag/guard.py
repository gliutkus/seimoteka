"""Deterministic screens in front of the model.

The system prompt already tells the model to ignore instructions inside
questions and documents, and never to judge or rank people. These screens make
the two most predictable attempts fail the same way every time, before any model
call: telling the assistant to drop its rules, and asking it to name the best or
worst MP, fraction or party. They match diacritic-folded text (`names.normal`)
in English and Lithuanian, and are deliberately narrow: a question that merely
mentions rules, or uses a superlative about something the record measures,
passes through to the model.

Before matching, full-width and other compatibility characters are folded,
invisible ones dropped, Cyrillic and Greek letters drawn like Latin ones read as
those, and runs of whitespace read as one space, so "Ｉｇｎｏｒｅ", "іgnоrе" typed
with Cyrillic letters, a zero-width space or a line break does not slip past. The
question itself is not changed: a Russian question still reaches the model in
Russian. An instruction in an earlier user turn of the history is refused too:
sent along with a new question, it would reach the model. What the
assistant said and what the tools returned are not screened: a speech quoting
such words is material to report (the prompt says so, and rag.validation keeps
its links from being cited). A quoted instruction in the question itself is
still refused, since a screen cannot tell a quotation from an instruction.

The screens know English and Lithuanian phrasings only. The same attempt in
another language, or spelled out letter by letter, reaches the model, where the
prompt and the answer checks of rag.validation still hold.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ingest.names import normal

# Unicode's default-ignorable code points, which are drawn as nothing. Each still
# splits a word for a regular expression ("ign\u034fore") or hides inside a name.
INVISIBLE = re.compile(
    "[\u00ad\u034f\u061c\u115f-\u1160\u17b4-\u17b5\u180b-\u180f\u200b-\u200f"
    "\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8"
    "\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)
# Cyrillic and Greek letters drawn like Latin ones, read as the Latin letter: the
# Cyrillic capitals and small letters, then the Greek ones (tests/test_guard.py
# checks every pair).
LOOKALIKES = str.maketrans(
    "АВЕЅІЈКМНОРСТХУҮҺԚԜӀаеѕіјорсхуүһԁԛԝӏΑΒΕΖΗΙΚΜΝΟΡΤΥΧαικνορυχγ",
    "abesijkmhopctxyyhqwiaesijopcxyyhdqwlabezhikmnoptyxaikvopuxy",
)

EN_INSTRUCTIONS = re.compile(
    r"\b(?:ignore|disregard|forget|override|bypass) (?:\w+ ){0,2}?"
    r"(?:your|all|any|previous|prior|above|earlier|system|these|those)"
    r" (?:\w+ ){0,2}?"
    r"(?:instructions?|rules?|prompts?|guidelines?)\b"
    r"|\b(?:system prompt|developer mode|jailbreak|you are now"
    r"|pretend (?:to be|you are))\b"
)
LT_INSTRUCTIONS = re.compile(
    r"\b(?:ignoruok|pamirsk|nepaisyk|apeik)(?:ite)? (?:\w+ ){0,2}?"
    r"(?:tavo|savo|jusu|visas|visus|visu|ankstesn\w*|sias|siuos|sistemos)"
    r" (?:\w+ ){0,2}?"
    r"(?:instrukcij|nurodym|taisykl|apribojim)\w*"
    r"|\b(?:nuo siol tu|dabar tu esi|apsimesk\w*"
    # the assistant's own system instructions, not "švietimo sistemos nurodymai"
    r"|(?:tavo|savo|jusu|parodyk\w*|atskleisk\w*|pakartok\w*)"
    r" sistemos (?:instrukcij|nurodym)\w*)\b"
)
EN_SUPERLATIVE = (
    r"(?:worst|best|dumbest|smartest"
    r"|most (?:corrupt|dishonest|honest|incompetent|useless))"
)
EN_PEOPLE = (
    r"(?:mps?|members? of (?:the )?(?:seimas|parliament)|politicians?"
    r"|part(?:y|ies)|fractions?|ministers?)"
)
LT_SUPERLATIVE = (
    r"(?:blogiaus|prasciaus|geriaus|kvailiaus|korumpuociaus|nesaziningiaus"
    r"|sazingiaus|protingiaus)\w*"
)
# "politika" (policy) folds like "politiką" (a politician, accusative): left out.
LT_PEOPLE = (
    r"(?:seimo nar\w*|parlamentar\w*|politik(?:as|ai|u|o|ams|uose)"
    r"|frakcij\w*|partij\w*|ministr\w*)"
)


def either_order(superlative: str, people: str) -> re.Pattern:
    near = r" (?:\w+ ){0,4}?"
    return re.compile(
        rf"\b{superlative}{near}{people}\b|\b{people}{near}{superlative}\b"
    )


SCREENS = (
    ("instructions", "en", EN_INSTRUCTIONS),
    ("instructions", "lt", LT_INSTRUCTIONS),
    ("judgement", "en", either_order(EN_SUPERLATIVE, EN_PEOPLE)),
    ("judgement", "lt", either_order(LT_SUPERLATIVE, LT_PEOPLE)),
)
REPLIES = {
    ("instructions", "en"): (
        "I don't change my rules. I answer only about the work of the Seimas, and "
        "only from its official record: votes, sitting stenograms, election "
        "programmes, Ethics and Procedures Commission findings and VTEK decisions."
    ),
    ("instructions", "lt"): (
        "Savo taisyklių nekeičiu. Atsakau tik apie Seimo darbą ir tik remdamasis "
        "oficialiais šaltiniais: balsavimais, posėdžių stenogramomis, rinkimų "
        "programomis, Etikos ir procedūrų komisijos išvadomis ir VTEK sprendimais."
    ),
    ("judgement", "en"): (
        "I don't rate or rank MPs, fractions or parties. I can show what the "
        "record says about them: how they voted, what they said in the chamber, "
        "what they promised in their programmes and which findings concern them, "
        "with links to the sources."
    ),
    ("judgement", "lt"): (
        "Seimo narių, frakcijų ar partijų nevertinu ir nereitinguoju. Galiu "
        "parodyti, ką apie juos rodo įrašas: kaip jie balsavo, ką sakė "
        "posėdžiuose, ką žadėjo rinkimų programose ir kokios išvados dėl jų "
        "priimtos, su nuorodomis į šaltinius."
    ),
}


@dataclass(frozen=True)
class Refusal:
    reason: str  # instructions | judgement
    language: str  # en | lt
    text: str


def visible(text: str) -> str:
    """The text without format characters (Cf) and the other characters Unicode
    draws as nothing."""
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return INVISIBLE.sub("", text)


def screen(question: str) -> Refusal | None:
    """A fixed refusal if the question trips a screen, else None."""
    # Decomposed first, so that "ё" or "ί" is a letter the look-alikes know plus
    # an accent, which normal() then drops.
    question = unicodedata.normalize("NFKD", question)
    question = visible(question).translate(LOOKALIKES)
    folded = " ".join(normal(question).split())
    for reason, language, pattern in SCREENS:
        if pattern.search(folded):
            return Refusal(reason, language, REPLIES[reason, language])
    return None


def conversation(question: str, history: list[dict]) -> Refusal | None:
    """Screen the current question and earlier user turns before any model call.

    Assistant text and retrieved quotations are not user instructions. This
    remains a narrow deterministic screen, not a general jailbreak classifier.
    """
    refusal = screen(question)
    if refusal:
        return refusal
    for message in history:
        if message.get("role") == "user":
            refusal = screen(str(message.get("content", "")))
            if refusal and refusal.reason == "instructions":
                return refusal
    return None
