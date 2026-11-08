"""Papildyti žinių bazę: the app's page for adding new Seimas data and YouTube
interviews.

app.py shows it when KB_UPDATES=1. The work runs in background processes
(ingest/update.py, ingest/interviews.py), so this page only starts the steps,
shows their progress and results, and records who each voice is.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import streamlit as st

from ingest import config, interviews, update, youtube_new
from ingest.seimas import VILNIUS
from rag import embed
from rag.validation import literal

STATES = {
    "queued": "Laukia eilėje",
    "downloading": "Atsisiunčiama",
    "transcribing": "Atpažįstama kalba",
    "separating": "Atskiriami balsai",
    "review": "Laukia, kol nurodysite, kas kalba",
    "loading": "Įkeliama į žinių bazę",
    "loaded": "Žinių bazėje",
    "failed": "Nepavyko",
}
STAGES = {
    "reading the video's page": "skaitomas vaizdo įrašo puslapis",
    "downloading the audio": "atsisiunčiamas garso įrašas",
    "converting the audio": "paruošiamas garso įrašas",
    "waiting for another interview to finish": "laukiama, kol baigsis kitas interviu",
    "waiting for another YouTube download to finish": "laukiama, kol baigsis kitas "
    "YouTube atsisiuntimas",
    "writing passages": "įrašomos ištraukos",
    "embedding passages": "kuriami ištraukų vektoriai",
    "refreshing search statistics": "atnaujinama paieškos statistika",
}
INTERVIEW_STEPS = ("downloading", "transcribing", "separating")
INTERVIEW_LOAD = ("loading",)
BACKGROUND = (
    "Darbas vyksta fone: galite uždaryti šį puslapį arba klausti pokalbių robotą, "
    "o grįžę matysite, kiek padaryta."
)
MESSAGES = {
    "not a YouTube link": "Tai ne YouTube nuoroda.",
    "the link names no video": "Nuorodoje nėra vaizdo įrašo.",
    "name at least one person who speaks in the video": "Nurodykite bent vieną "
    "kalbantį žmogų.",
    "this video is already being processed": "Šis vaizdo įrašas jau apdorojamas.",
    "this video is already in the knowledge base; remove it first": "Šis vaizdo "
    "įrašas jau yra žinių bazėje; pirmiausia jį pašalinkite.",
    "an update is already running": "Atnaujinimas jau vyksta.",
    "a search for new videos is already running": "Naujų vaizdo įrašų paieška jau "
    "vyksta.",
    "the last search finished less than an hour ago": "Nuo paskutinės paieškos "
    "praėjo mažiau nei valanda.",
    "a running or loaded job cannot be marked": "Apdorojamo ar jau įkelto įrašo "
    "pažymėti negalima.",
    "the audio is gone; start the video again": "Garso įrašo nebėra; pradėkite šį "
    "vaizdo įrašą iš naujo.",
    "only a job under review can be separated again": "Iš naujo atskirti galima tik "
    "peržiūrai paruoštą įrašą.",
    "the last update finished less than an hour ago": "Nuo paskutinio atnaujinimo "
    "praėjo mažiau nei valanda.",
    "only a job under review can be loaded": "Įkelti galima tik peržiūrėtą įrašą: "
    "šis jau įkeliamas arba pasikeitė. Atnaujinkite puslapį.",
    "voices can be assigned only while they are reviewed": "Balsus priskirti galima "
    "tik peržiūros metu. Atnaujinkite puslapį.",
    "a running job cannot be removed; wait until it finishes": "Apdorojamo įrašo "
    "pašalinti negalima; palaukite, kol baigsis.",
    "a running or loaded job cannot be deleted": "Apdorojamo ar jau įkelto įrašo "
    "ištrinti negalima.",
}
UPDATE_STEPS = {
    "": "Ruošiamasi",
    "votes_download": "Atsisiunčiami nauji balsavimai",
    "votes_check": "Balsavimai tikrinami su balsavimų puslapiais",
    "votes_load": "Balsavimai įkeliami į duomenų bazę",
    "stenograms_download": "Atsisiunčiamos stenogramos",
    "stenograms_load": "Įkeliamos naujų posėdžių stenogramos",
    "search_stats": "Atnaujinama paieškos statistika",
    "embedding": "Kuriami ištraukų vektoriai",
}
PHASES = {
    "agendas": "posėdžių darbotvarkės",
    "votes": "balsavimai",
    "stenograms": "stenogramos",
}
PROBLEMS = {
    "rows: choice disagrees": "Seimo nario balsas nesutampa su balsavimo puslapiu",
    "page tally != XML tally": "balsų skaičius nesutampa su balsavimo puslapiu",
    "XML rows absent from page": "Seimo nario nėra balsavimo puslapyje",
    "XML: unknown kaip_balsavo value": "nežinoma balso reikšmė",
    "vote page missing from cache": "nepavyko atsisiųsti balsavimo puslapio",
    "vote page without an individual table": "balsavimo puslapyje nėra narių balsų",
}
SKIPPED = {
    "no key": "nenustatytas OpenRouter raktas",
    "no credit limit": "OpenRouter raktas neturi išlaidų ribos",
    "limit unknown": "nepavyko patikrinti OpenRouter rakto išlaidų ribos",
}
ARCHIVE_STATES = {
    "todo": "Laukia peržiūros",
    "no_mp": "Seimo nario nėra",
    "unusable": "Nuorašas netinkamas",
    **STATES,
}
ARCHIVE_FILTERS = {
    "Laukia peržiūros": ("todo",),
    "Pradėti, dar neįkelti": (*interviews.RUNNING, *interviews.WAITING),
    "Žinių bazėje": ("loaded",),
    "Seimo nario nėra": ("no_mp",),
    "Nuorašas netinkamas": ("unusable",),
    "Visi": None,
}
EVIDENCE = {
    "names who appears (full name)": "pavadinime ar aprašyme nurodytas vardu ir "
    "pavarde",
    "names who appears (surname only one MP has)": "nurodytas pavarde",
    "clip: its full show names who appears": "ištrauka; visoje laidoje nurodytas",
    "described as speaking (weaker)": "aprašyme minimas kaip kalbantis (silpnesnis "
    "požymis)",
    "MPs only mentioned": "Seimo nariai tik paminėti",
    "clip: full show linked, nobody named as appearing": "ištrauka; dalyvis "
    "nenurodytas",
    "clip: its full show's guest is not on the roster": "ištrauka; svečias ne Seimo "
    "narys",
}
NOBODY = "Nė vienas iš išvardytų: palikti be kalbėtojo"
VOICES_HELP = (
    "Nurodžius skaičių, kalba skirstoma į tiek balsų. Nežinant, skaičius "
    "spėjamas, ir panašūs balsai kartais susilieja į vieną."
)
CLIP_SECONDS = 12


def lithuanian(message: str) -> str:
    return MESSAGES.get(message, message)


def person_label(person: dict) -> str:
    return person["name"] + (" (Seimo narys)" if person["mp_id"] else "")


@st.cache_data(ttl=600, show_spinner=False)
def key_has_limit() -> bool | None:
    """Whether the OpenRouter key has a credit limit; None when it cannot be told."""
    if not config.OPENROUTER_API_KEY:
        return None
    try:
        return embed.key_limit(config.OPENROUTER_API_KEY) is not None
    except Exception:  # the provider: ask again on the next load
        return None


@st.cache_data(max_entries=48, show_spinner=False)
def clip(path: str, start: float, end: float, modified: float) -> bytes:
    return interviews.clip_bytes(Path(path), start, end)


def count(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def moment(value: str | None) -> str:
    if not value:
        return "nežinoma"
    return f"{datetime.fromisoformat(value).astimezone(VILNIUS):%Y-%m-%d %H:%M}"


def render(connection) -> None:
    st.title("Papildyti žinių bazę")
    seimas_data(connection)
    st.subheader("YouTube interviu")
    st.caption(
        "Įklijuokite YouTube nuorodą ir nurodykite, kas vaizdo įraše kalba. Garso "
        "įrašas atsisiunčiamas, kalba atpažįstama ir suskirstoma pagal balsus šiame "
        "kompiuteryje. Tada pasiklausote kiekvieno balso ir nurodote, kas tai: tik "
        "jūsų priskirti balsai tampa kalbėtojais. Įkeltą interviu pokalbių robotas "
        "cituoja su nuoroda į tą vaizdo įrašo vietą. Įrašas rodo, kas pasakyta, bet "
        "ne tai, ar pasakyta tiesa."
    )
    members = interviews.roster(connection())
    jobs = interviews.all_jobs()
    if any(job["state"] in interviews.RUNNING for job in jobs):
        progress_panel()
    waiting = [j for j in jobs if j["state"] in interviews.WAITING]
    if waiting:
        st.subheader(f"Laukia jūsų: {len(waiting)}")
        for job in waiting:
            show_job(job, members)
    archive_section(members)
    with st.expander("Kitas vaizdo įrašas pagal nuorodą"):
        start_form(members)
    done = [j for j in jobs if j["state"] == "loaded"]
    if done:
        st.subheader(f"Žinių bazėje: {len(done)} interviu")
        for job in done:
            show_job(job, members)


def seimas_data(connection) -> None:
    st.subheader("Seimo duomenys")
    st.caption(
        "Atsisiunčia iš lrs.lt naujus balsavimus, posėdžius ir stenogramas. Kiekvienas "
        "balsavimas patikrinamas su jo puslapiu: radus naujų neatitikimų, duomenų bazė "
        "nekeičiama. Atnaujinti galima kartą per valandą."
    )
    job = update.current()
    if job and job["state"] in update.RUNNING:
        # The record is not read while it is being replaced; the progress says it all.
        update_progress()
        return
    record = update.record(connection())
    st.markdown(
        f"Paskutinis balsavimas: **{moment(record['last_vote_at'])}** · balsavimų "
        f"{count(record['votes'])}, Seimo narių balsų {count(record['choices'])} · "
        f"įkelta {moment(record['loaded_at'])}"
    )
    if job:
        show_update(job)
    unlimited = key_has_limit() is False
    allow = unlimited and st.checkbox(
        "OpenRouter raktas neturi išlaidų ribos; vis tiek kurti naujų ištraukų "
        "vektorius (tai kainuoja mažiau nei centą)",
        key="update-unlimited",
    )
    wait = update.minutes_to_wait(job)
    if st.button(
        "Atsisiųsti naujus Seimo duomenis",
        type="primary",
        disabled=bool(wait),
        key="update-start",
    ):
        begin_update("update", allow)
    if wait:
        st.caption(f"Vėl atnaujinti bus galima po {wait} min.")
    missing = update.missing_vectors(connection())
    if missing:
        st.warning(
            f"Ištraukų be vektorių: {count(missing)}. Kol jų nėra, šios ištraukos "
            "randamos tik pagal žodžius."
        )
        if st.button("Sukurti trūkstamus vektorius", key="update-embed"):
            if unlimited and not allow:
                st.error(
                    "Pirmiausia patvirtinkite, kad vektoriai gali būti kuriami raktu "
                    "be išlaidų ribos."
                )
            else:
                begin_update("embed", allow)


def begin_update(mode: str, allow: bool) -> None:
    try:
        update.start(mode, allow_unlimited=allow)
    except ValueError as exc:
        st.error(lithuanian(str(exc)))
        return
    st.rerun()


@st.fragment(run_every=3)
def update_progress() -> None:
    job = update.current()
    if not job or job["state"] not in update.RUNNING:
        st.rerun()
    steps = update.STEPS if job.get("mode") == "update" else ("embedding",)
    name = job.get("step") or ""
    measured = None
    if harvested := update.harvest_progress(job):
        phase, n, of = harvested
        measured = (n, of, f"{PHASES.get(phase, phase)} {count(n)} iš {count(of)}")
    elif progress := job.get("progress"):
        measured = (progress["done"], progress["total"], amount(progress))
    step_list(job, steps, UPDATE_STEPS, name, measured)


def show_update(job: dict) -> None:
    finished = moment(job.get("finished_at"))
    if job["state"] == "failed":
        problems = (job.get("checked") or {}).get("new_problems") or {}
        if problems:
            found = "; ".join(
                f"{PROBLEMS.get(key, key)}: {count(n)}" for key, n in problems.items()
            )
            message = f"Atnaujinimas sustabdytas {finished}: {found}."
        else:
            step = job.get("failed_step") or ""
            message = (
                f"Atnaujinimas nepavyko {finished} "
                f"({UPDATE_STEPS.get(step, step).lower()}): {job.get('error')}."
            )
        if problems or job.get("failed_step", "") in update.UNCHANGED_BEFORE:
            message += " Duomenų bazė nepakeista."
        st.error(literal(message))
        return
    embedding = job.get("embedding") or {}
    if job.get("mode") == "embed":
        st.success(
            f"Vektoriai sukurti {finished}: {count(embedding.get('embedded', 0))} "
            "ištraukų."
        )
    else:
        before, after = job.get("before") or {}, job.get("after") or {}
        pages = job.get("stenograms") or {}
        checked = (job.get("checked") or {}).get("rows_compared", 0)
        st.success(
            f"Atnaujinta {finished}. Nauji balsavimai: "
            f"{count(after.get('votes', 0) - before.get('votes', 0))}, iš jų su "
            "Seimo narių balsais: "
            f"{count(after.get('with_results', 0) - before.get('with_results', 0))}. "
            f"Su balsavimų puslapiais patikrinta Seimo narių balsų: {count(checked)}, "
            "naujų neatitikimų nerasta. Naujos stenogramos: "
            f"{count(pages.get('added', 0))}, pataisytos: "
            f"{count(pages.get('replaced', 0))}. Sukurta ištraukų vektorių: "
            f"{count(embedding.get('embedded', 0))}."
        )
    if skipped := embedding.get("skipped"):
        st.caption(f"Vektoriai nekurti: {SKIPPED.get(skipped, skipped)}.")
    if embedding.get("error"):
        st.warning(literal(f"Vektorių sukurti nepavyko: {embedding['error']}"))


@st.cache_data(ttl=3600, show_spinner="Skaitomas surinktų vaizdo įrašų sąrašas…")
def cached_archive(version: int, changed: float) -> list[dict]:
    """The harvested videos. A cache is kept while this function's code is
    unchanged, so the version of harvested()'s fields is part of its key, and so
    is when the list of guesses last changed (a search for new videos)."""
    return interviews.harvested()


def archive() -> list[dict]:
    guesses = interviews.youtube.ROOT / interviews.PARTICIPANTS
    changed = guesses.stat().st_mtime if guesses.exists() else 0.0
    return cached_archive(interviews.HARVESTED_VERSION, changed)


def archive_section(members: dict[str, int]) -> None:
    st.subheader("Surinkti vaizdo įrašai")
    st.caption(
        "Vaizdo įrašai, kurių pavadinime ar aprašyme minimi Seimo nariai. Tai tik "
        "spėjimas, kuris Seimo narys kalba: tai reikia patikrinti klausantis. "
        "Paruošiant peržiūrai naudojamas jau turimas nuorašas; kur jo nėra, kalba "
        "atpažįstama šiame kompiuteryje, ir tai užtrunka ilgiau."
    )
    new_videos()
    videos = archive()
    if not videos:
        st.info("Surinktų vaizdo įrašų nėra.")
        return
    states = interviews.review_states(videos)
    tally = {}
    for state in states.values():
        tally[state] = tally.get(state, 0) + 1
    st.markdown(
        " · ".join(
            f"{ARCHIVE_STATES.get(state, state)}: **{count(n)}**"
            for state, n in sorted(tally.items(), key=lambda item: -item[1])
        )
    )
    shown, who, since, text = st.columns([1, 1, 1, 1])
    choice = shown.selectbox("Rodyti", list(ARCHIVE_FILTERS), key="archive-state")
    people = sorted({name for v in videos for name in v["candidates"]})
    mp = who.selectbox("Galimas Seimo narys", ["Visi", *people], key="archive-mp")
    start = since.date_input(
        "Paskelbta nuo", value=None, format="YYYY-MM-DD", key="archive-since"
    )
    words = text.text_input("Pavadinime yra", key="archive-text").casefold().strip()
    wanted = ARCHIVE_FILTERS[choice]
    rows = [
        v
        for v in videos
        if (wanted is None or states[v["video_id"]] in wanted)
        and (mp == "Visi" or mp in v["candidates"])
        and (start is None or (v.get("published") or "") >= start.isoformat())
        and (not words or words in v["title"].casefold())
    ]
    st.caption(f"Rodoma {count(len(rows))} iš {count(len(videos))}.")
    table = [
        {
            "Pavadinimas": v["title"],
            "Paskelbta": v.get("published") or "",
            "Kanalas": v["channel"],
            "Trukmė, min": round((v["duration"] or 0) / 60),
            "Nuorašas": "yra" if v.get("transcribed") else "bus sukurtas",
            "Galimi Seimo nariai": ", ".join(v["candidates"]) or "—",
            "Požymis": EVIDENCE.get(v["evidence"], v["evidence"]),
            "Būsena": ARCHIVE_STATES.get(states[v["video_id"]], ""),
        }
        for v in rows
    ]
    event = st.dataframe(
        table,
        hide_index=True,
        on_select="rerun",
        selection_mode="multi-row",
        # A selection is kept as row positions: once the rows change (a filter,
        # or videos prepared and gone from the list) it would point at others.
        key=f"archive-{choice}-{mp}-{start}-{words}-"
        f"{st.session_state.get('prepared', 0)}",
        height=360,
    )
    picked = [rows[i] for i in event.selection.rows if i < len(rows)]
    ready = [
        v
        for v in picked or rows
        if states[v["video_id"]] == "todo"
        and any(name in members for name in v["candidates"])
    ]
    st.caption(
        "Paruošiant garsas atsisiunčiamas ir balsai atskiriami fone, po vieną įrašą. "
        "Kalbantieji užpildomi pagal pavadinimą ir aprašymą; laidos vedėją ir kitus "
        "galėsite pridėti peržiūros metu."
    )
    first, many = st.columns(2)
    if picked:
        label = f"Paruošti pasirinktus peržiūrai ({len(ready)})"
    else:
        ready = ready[:5]
        label = f"Paruošti {len(ready)} kitus laukiančius peržiūrai"
    if first.button(label, disabled=not ready, key="archive-prepare"):
        prepare(ready, members)
    if picked and len(ready) < len(picked):
        many.caption(
            f"{len(picked) - len(ready)} pasirinkti įrašai jau pradėti, atmesti "
            "arba be galimo Seimo nario: juos paruoškite po vieną."
        )
    if len(picked) == 1:
        archive_video(picked[0], states[picked[0]["video_id"]], members)


def prepare(videos: list[dict], members: dict[str, int]) -> None:
    """Start the videos with their candidate MPs as the people who speak."""
    for video in videos:
        mps = [name for name in video["candidates"] if name in members]
        try:
            interviews.start(video["url"], interviews.participants(members, mps, []))
        except ValueError as exc:
            st.error(f"{literal(video['title'])}: {lithuanian(str(exc))}")
    st.session_state["prepared"] = st.session_state.get("prepared", 0) + 1
    st.rerun()


NEW_STEPS = {
    "roster": "Atnaujinamas Seimo narių sąrašas",
    "search": "Ieškoma naujų vaizdo įrašų",
    "describe": "Skaitomi naujų vaizdo įrašų puslapiai",
    "guess": "Spėjama, kurie Seimo nariai dalyvauja",
}
NEW_UNITS = {"searches": "paieškos", "pages": "puslapiai"}


def new_videos() -> None:
    """The button that searches YouTube for new videos, and how the last search
    went."""
    job = youtube_new.current()
    if job and job["state"] in youtube_new.RUNNING:
        new_videos_progress()
        return
    if job and job["state"] == "done":
        waiting = len(job.get("waiting") or [])
        st.success(
            f"Paieška baigta {moment(job.get('finished_at'))}: rasta naujų vaizdo "
            f"įrašų {count(job.get('found', 0))}, perskaityta puslapių "
            f"{count(job.get('described', 0))}, į sąrašą įtraukta "
            f"{count(job.get('added', 0))}."
            + (f" Dar neperskaityta: {count(waiting)}." if waiting else "")
        )
    elif job and job["state"] == "failed":
        st.error(literal(f"Paieška nepavyko: {job.get('error')}"))
    wait = youtube_new.minutes_to_wait(job)
    if st.button(
        "Ieškoti naujų vaizdo įrašų", disabled=bool(wait), key="new-videos-start"
    ):
        try:
            youtube_new.start()
        except ValueError as exc:
            st.error(lithuanian(str(exc)))
        else:
            st.rerun()
    if wait:
        st.caption(f"Vėl ieškoti bus galima po {wait} min.")
    else:
        st.caption(
            "Kiekvieno Seimo nario pokalbių YouTube ieškoma iš naujo, peržiūrimi ir "
            "naujausi OpTV įrašai; perskaitoma iki "
            f"{youtube_new.LIMIT} naujų vaizdo įrašų puslapių. Užtrunka apie 10–20 min."
        )


@st.fragment(run_every=3)
def new_videos_progress() -> None:
    job = youtube_new.current()
    if not job or job["state"] not in youtube_new.RUNNING:
        st.rerun()
    timed = job.get("steps") or {}
    running = [name for name, s in timed.items() if not s.get("finished")]
    current = running[-1] if running else ""
    measured = None
    if progress := job.get("progress"):
        done, total = progress["done"], progress["total"]
        unit = NEW_UNITS.get(progress["unit"], progress["unit"])
        measured = (done, total, f"{unit}: {count(done)} iš {count(total)}")
    elif job.get("stage"):
        measured = (None, None, STAGES.get(job["stage"], job["stage"]))
    step_list(job, youtube_new.STEPS, NEW_STEPS, current, measured)


def archive_video(video: dict, state: str, members: dict[str, int]) -> None:
    st.markdown(f"#### {literal(video['title'])}")
    watch, side = st.columns([3, 2])
    watch.video(video["url"])
    facts = [literal(video["channel"]), f"[{video['url']}]({video['url']})"]
    if video["mentioned"]:
        facts.append("paminėti: " + literal(", ".join(video["mentioned"])))
    watch.caption(" · ".join(f for f in facts if f))
    with side:
        archive_actions(video, state, members)


def archive_actions(video: dict, state: str, members: dict[str, int]) -> None:
    vid = video["video_id"]
    if state == "no_mp":
        st.info("Pažymėta, kad šiame įraše nekalba nė vienas Seimo narys.")
        if st.button("Atšaukti žymą", key=f"unmark-{vid}"):
            interviews.unmark_no_mp(vid)
            st.rerun()
        return
    if state == "unusable":
        st.warning(
            "Valant nuorašą jis buvo atmestas (pvz., kalba ne lietuvių arba kalbos "
            "atpažinimas nepatikimas), todėl šio įrašo įkelti negalima."
        )
        return
    if state != "todo":
        st.info(
            f"Šis įrašas jau pradėtas: {ARCHIVE_STATES.get(state, state).lower()}. "
            "Jį rasite aukščiau."
        )
        return
    st.markdown(
        "Pažiūrėkite pradžią ir nurodykite, kas kalba. Seimo nariai užpildyti pagal "
        "pavadinimą ir aprašymą: pašalinkite tuos, kurie nekalba."
    )
    candidates = [name for name in video["candidates"] if name in members]
    with st.form(f"archive-start-{vid}"):
        mps = st.multiselect(
            "Kalbantys Seimo nariai",
            list(members),
            default=candidates,
            placeholder="Pasirinkite iš Seimo narių sąrašo",
        )
        others = st.text_area(
            "Kiti kalbantys žmonės, po vieną eilutėje",
            placeholder="Pavyzdžiui, laidos vedėjas",
            height=90,
        )
        voices = st.number_input(
            "Kiek skirtingų balsų girdisi (0, jei nežinote)",
            min_value=0,
            max_value=10,
            value=0,
            step=1,
            help=VOICES_HELP,
        )
        submitted = st.form_submit_button("Pradėti peržiūrą", type="primary")
    if submitted:
        begin_interview(video["url"], members, mps, others, voices)
    note = st.text_input(
        "Pastaba (neprivaloma)", key=f"no-mp-note-{vid}", placeholder="Kas kalba"
    )
    if st.button("Šiame įraše nekalba nė vienas Seimo narys", key=f"no-mp-{vid}"):
        try:
            interviews.mark_no_mp(vid, note)
        except ValueError as exc:
            st.error(lithuanian(str(exc)))
            return
        st.rerun()


def begin_interview(
    link: str, members: dict[str, int], mps: list[str], others: str, voices: int
) -> None:
    try:
        people = interviews.participants(members, mps, others.splitlines())
        job = interviews.start(link, people, voices=int(voices) or None)
    except ValueError as exc:
        st.error(lithuanian(str(exc)))
        return
    st.success(f"Pradėta: vaizdo įrašas {job['video_id']} apdorojamas fone.")
    st.rerun()


def start_form(members: dict[str, int]) -> None:
    with st.form("new-interview"):
        link = st.text_input(
            "YouTube nuoroda", placeholder="https://www.youtube.com/watch?v=…"
        )
        mps = st.multiselect(
            "Kalbantys Seimo nariai",
            list(members),
            placeholder="Pasirinkite iš Seimo narių sąrašo",
        )
        others = st.text_area(
            "Kiti kalbantys žmonės, po vieną eilutėje",
            placeholder="Pavyzdžiui, laidos vedėjas",
            height=90,
        )
        voices = st.number_input(
            "Kiek skirtingų balsų girdisi (0, jei nežinote)",
            min_value=0,
            max_value=10,
            value=0,
            step=1,
        )
        submitted = st.form_submit_button("Pradėti", type="primary")
    if submitted:
        begin_interview(link, members, mps, others, voices)


@st.fragment(run_every=3)
def progress_panel() -> None:
    running = [j for j in interviews.all_jobs() if j["state"] in interviews.RUNNING]
    if not running:
        st.rerun()
    st.subheader("Apdorojama")
    for job in running:
        st.markdown(f"**{literal(job.get('title') or job['url'])}**")
        if job["state"] == "queued":
            st.caption(STATES["queued"])
            continue
        steps = INTERVIEW_LOAD if job["state"] == "loading" else INTERVIEW_STEPS
        stage = job.get("stage") or ""
        measured = None
        if progress := job.get("progress"):
            measured = (progress["done"], progress["total"], amount(progress))
        elif stage:
            measured = (None, None, STAGES.get(stage, stage))
        step_list(job, steps, STATES, job["state"], measured)


def duration(seconds: float) -> str:
    whole = max(int(seconds), 0)
    hours, minutes, rest = whole // 3600, whole // 60 % 60, whole % 60
    if hours:
        return f"{hours} val. {minutes} min"
    if minutes:
        return f"{minutes} min {rest} s"
    return f"{rest} s"


def since(value: str) -> float:
    return (datetime.now(VILNIUS) - datetime.fromisoformat(value)).total_seconds()


def amount(progress: dict) -> str:
    """How far a step has got, in its own unit."""
    done, total, unit = progress["done"], progress["total"], progress["unit"]
    if unit == "bytes":
        return f"{done / 1e6:.1f} iš {total / 1e6:.1f} MB".replace(".", ",")
    if unit == "frames":  # Whisper's 10 ms frames: minutes of the recording
        return f"{done / 6000:.0f} iš {total / 6000:.0f} min įrašo"
    if unit == "passages":
        return f"{count(done)} iš {count(total)} ištraukų"
    return f"{100 * done / max(total, 1):.0f} %"


def step_list(
    job: dict,
    steps: tuple[str, ...],
    labels: dict[str, str],
    current: str,
    measured: tuple | None,
) -> None:
    """Each step: done with its time, running with what it has done and an
    estimate of what is left, or still to come."""
    timed = job.get("steps") or {}
    position = steps.index(current) if current in steps else -1
    for index, name in enumerate(steps):
        label = labels.get(name, name)
        step = timed.get(name) or {}
        if index < position and not step.get("finished"):  # a job from before timing
            st.markdown(f"✅ {label}")
        elif name != current and step.get("finished"):
            took = (
                datetime.fromisoformat(step["finished"])
                - datetime.fromisoformat(step["started"])
            ).total_seconds()
            note = ""
            if name == "transcribing" and (job.get("transcript") or {}).get("reused"):
                note = " · panaudotas jau turimas tekstas"
            st.markdown(f"✅ {label} · {duration(took)}{note}")
        elif name == current:
            elapsed = since(step["started"]) if step.get("started") else None
            done, total, detail = measured or (None, None, "")
            parts = [detail] if detail else []
            if elapsed is not None:
                parts.append(f"praėjo {duration(elapsed)}")
            share = done / total if done is not None and total else None
            if share and elapsed and 0.02 < share < 1 and elapsed >= 15:
                parts.append(f"liko apie {duration(elapsed * (1 - share) / share)}")
            st.markdown(f"⏳ **{label}**")
            if share is not None:
                st.progress(min(share, 1.0), text=" · ".join(parts))
            else:
                st.caption(" · ".join(parts) or "vyksta")
        else:
            st.markdown(f":gray[○ {label}]")
    started = [s["started"] for s in timed.values() if s.get("started")]
    if started:
        st.caption(f"Iš viso praėjo {duration(since(min(started)))}. {BACKGROUND}")


def show_job(job: dict, members: dict[str, int]) -> None:
    state = STATES.get(job["state"], job["state"])
    with st.expander(
        literal(f"{state}: {job.get('title') or job['video_id']}"),
        expanded=job["state"] in interviews.WAITING,
    ):
        facts = [f"[{job['url']}]({job['url']})"]
        if job.get("channel"):
            facts.append(literal(job["channel"]))
        if job.get("published"):
            facts.append(f"paskelbta {job['published']}")
        st.markdown(" · ".join(facts))
        people = ", ".join(person_label(p) for p in job["participants"])
        st.caption(f"Kalba: {literal(people)}")
        if job.get("error"):
            st.error(literal(job["error"]))
        if job["state"] == "review":
            review(job, members)
        elif job["state"] == "loaded":
            loaded(job)
        elif job["state"] == "failed":
            failed(job)


def review(job: dict, members: dict[str, int]) -> None:
    vid = job["video_id"]
    options = [NOBODY] + [person_label(p) for p in job["participants"]]
    assignment = job.get("assignment") or {}
    wav = interviews.job_dir(vid) / "audio.wav"
    # Widgets keep what was chosen for this key; a new round of review starts clean.
    key = f"{vid}-{job.get('round', 0)}"
    if (job.get("transcript") or {}).get("reused"):
        st.caption("Panaudotas jau turimas šio vaizdo įrašo kalbos atpažinimo tekstas.")
    if not wav.exists():
        st.info(
            "Garso įrašo šiame kompiuteryje nebėra: klausykitės vaizdo įraše, "
            "spustelėję laiką prie ištraukos."
        )
    if len(job.get("voices") or []) > len(job["participants"]):
        st.caption(
            "Balsų daugiau nei kalbančiųjų: tas pats žmogus galėjo būti išskaidytas "
            "į kelis balsus. Kiekvienam jo balsui pasirinkite tą patį žmogų arba "
            "atskirkite balsus iš naujo, nurodę jų skaičių."
        )
    add_person(job, members)
    unlimited = key_has_limit() is False
    with st.form(f"assign-{vid}"):
        st.markdown(
            "Pasiklausykite kiekvieno balso ir nurodykite, kas tai. Tam pačiam "
            "žmogui gali tekti keli balsai; balsą, kurio neatpažįstate, palikite be "
            "kalbėtojo."
        )
        choices = {}
        for voice in job.get("voices") or []:
            minutes, seconds = divmod(voice["seconds"], 60)
            st.markdown(
                f"**Balsas {voice['label']}** · {minutes} min {seconds} s "
                f"({voice['share']:.0%} kalbos) · pirmą kartą "
                f"{interviews.clock(voice['first'])}"
            )
            for sample in voice["samples"][:3]:
                at = interviews.watch_url(vid, sample["start"])
                st.caption(
                    f"[{interviews.clock(sample['start'])}]({at}) · "
                    f"{literal(sample['text'])}"
                )
                if wav.exists():
                    end = min(sample["end"], sample["start"] + CLIP_SECONDS)
                    audio = clip(str(wav), sample["start"], end, wav.stat().st_mtime)
                    st.audio(audio, format="audio/wav")
            current = assignment.get(str(voice["voice"]))
            choices[str(voice["voice"])] = st.selectbox(
                f"Kas kalba balsu {voice['label']}?",
                range(len(options)),
                index=0 if current is None else current + 1,
                format_func=options.__getitem__,
                key=f"voice-{key}-{voice['voice']}",
            )
        if job.get("unplaced_seconds"):
            st.caption(
                f"{job['unplaced_seconds']} s kalbos, kur balsai persidengia arba "
                "nepriskirti jokiam balsui, liks be kalbėtojo."
            )
        listened = st.checkbox(
            "Pasiklausiau ir patvirtinu, kas kalba kiekvienu balsu",
            key=f"listened-{key}",
        )
        allow = unlimited and st.checkbox(
            "OpenRouter raktas neturi išlaidų ribos; vis tiek sukurti ištraukų "
            "vektorius (tai kainuoja mažiau nei centą)",
            key=f"unlimited-{vid}",
        )
        submitted = st.form_submit_button("Įkelti į žinių bazę", type="primary")
    with st.form(f"separate-{key}"):
        st.markdown(
            "Balsai susilieję ar išskaidyti per smulkiai? Atskirkite juos iš naujo: "
            "naudojamas tas pats nuorašas"
            + (
                " ir garso įrašas."
                if wav.exists()
                else ", o garso įrašas atsisiunčiamas iš naujo."
            )
        )
        again = st.number_input(
            "Kiek skirtingų balsų girdisi",
            min_value=1,
            max_value=10,
            # what was found, within the allowed range: a split can find more than 10
            value=min(max(len(job.get("voices") or []), len(job["participants"])), 10),
            step=1,
            help=VOICES_HELP,
        )
        if st.form_submit_button("Atskirti balsus iš naujo"):
            try:
                interviews.separate_again(vid, int(again))
            except ValueError as exc:
                st.error(lithuanian(str(exc)))
            else:
                st.rerun()
    if st.button("Ištrinti šį darbą", key=f"forget-{vid}"):
        attempt(interviews.forget, vid)
    if not submitted:
        return
    if not listened:
        st.error("Pirmiausia patvirtinkite, kas kalba kiekvienu balsu.")
        return
    if unlimited and not allow:
        st.error(
            "Patvirtinkite, kad vektoriai gali būti kuriami raktu be išlaidų ribos."
        )
        return
    try:
        interviews.queue_load(
            vid,
            {voice: None if i == 0 else i - 1 for voice, i in choices.items()},
            allow_unlimited=allow,
        )
    except ValueError as exc:
        st.error(lithuanian(str(exc)))
        return
    st.rerun()


def add_person(job: dict, members: dict[str, int]) -> None:
    """Name one more speaker, such as a host, before assigning the voices."""
    with st.form(f"add-person-{job['video_id']}", clear_on_submit=True):
        name = st.text_input(
            "Trūksta kalbančiojo? Įrašykite jo vardą ir pavardę",
            placeholder="Pavyzdžiui, laidos vedėjas; Seimo narį rašykite kaip sąraše",
        )
        if st.form_submit_button("Pridėti") and name.strip():
            try:
                interviews.add_participant(job, members, " ".join(name.split()))
            except ValueError as exc:
                st.error(lithuanian(str(exc)))
            else:
                st.rerun()


def loaded(job: dict) -> None:
    people = job["participants"]
    assignment = job.get("assignment") or {}
    lines = [
        f"- Balsas {voice['label']}: "
        + (
            person_label(people[assignment[str(voice["voice"])]])
            if assignment.get(str(voice["voice"])) is not None
            else "be kalbėtojo"
        )
        for voice in job.get("voices") or []
    ]
    st.success(f"Žinių bazėje: {job.get('chunks', 0)} ištraukų.")
    st.markdown("\n".join(lines))
    if st.button("Pašalinti iš žinių bazės", key=f"remove-{job['video_id']}"):
        with st.spinner("Šalinama…"):
            attempt(interviews.remove, job["video_id"])


def failed(job: dict) -> None:
    retry, forget = st.columns(2)
    if retry.button("Bandyti dar kartą", key=f"retry-{job['video_id']}"):
        attempt(
            interviews.start,
            job["url"],
            job["participants"],
            voices=job.get("voices_expected"),
        )
    if forget.button("Ištrinti", key=f"forget-{job['video_id']}"):
        attempt(interviews.forget, job["video_id"])


def attempt(action, *args, **kwargs) -> None:
    """Run a job action and show the page again, or say in Lithuanian why the
    job's state did not allow it (another tab or a worker moved it meanwhile)."""
    try:
        action(*args, **kwargs)
    except ValueError as exc:
        st.error(lithuanian(str(exc)))
        return
    st.rerun()
