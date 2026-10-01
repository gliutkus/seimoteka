"""Seimoteka: questions about the Lithuanian Seimas, answered from its record.

    uv run streamlit run app.py --server.address 127.0.0.1
    KB_UPDATES=1 uv run streamlit run app.py --server.address 127.0.0.1

Each answer is shown with what it was built from: how the question was routed and
split, the tool calls as they run, vote tables, the passages retrieval found with
the phrasings that found them and how they ranked, and links to the primary
documents. With KB_UPDATES=1 a second page adds YouTube interviews (kb_page.py).
"""

from __future__ import annotations

import json
import time

import pandas as pd
import psycopg
import streamlit as st

from ingest import config
from rag import agent as bot
from rag.validation import inline_code, literal

CHOICES = {
    "uz": "už",
    "pries": "prieš",
    "susilaike": "susilaikė",
    "nebalsavo": "nebalsavo",
    "a": "už formuluotę A",
    "b": "už formuluotę B",
}
TOOLS = {
    "voting_record": "Seimo nario balsavimai",
    "find_votes": "Balsavimai pagal klausimą",
    "voting_agreement": "Dviejų Seimo narių balsavimų sutapimas",
    "fraction_cohesion": "Frakcijos vieningumas",
    "programme_votes": "Partijos programa ir jos iškeltų Seimo narių balsavimai",
    "search_documents": "Paieška dokumentuose",
}
DOC_TYPES = {
    "stenogram": "Posėdžio stenograma",
    "programme": "Rinkimų programa",
    "vtek": "VTEK sprendimas",
    "ethics": "Etikos ir procedūrų komisijos išvada",
    "bill": "Teisės akto projektas",
    "youtube_interview": "YouTube interviu",
}
ROUTES = {
    "votes": "balsavimų įrankiai",
    "documents": "dokumentų paieška",
    "both": "balsavimų įrankiai ir dokumentų paieška",
    "unsure": "visi įrankiai",
}


@st.cache_resource
def connection() -> psycopg.Connection:
    return psycopg.connect(config.DATABASE_URL, autocommit=True)


def session_model():
    if "model" not in st.session_state:
        st.session_state.model = bot.build_model()
    return st.session_state.model


def history() -> list[dict]:
    """Earlier turns, as the model reads them. A turn that failed is left out, and
    so is one a screen refused: its question would reach the model after all."""
    messages = []
    for turn in st.session_state.turns:
        refused = any(e["type"] == "guard" for e in turn.get("events", []))
        if turn["answer"] and not turn.get("error") and not refused:
            messages.append({"role": "user", "content": turn["question"]})
            messages.append({"role": "assistant", "content": turn["answer"]})
    return messages


# ------------------------------------------------------------------ results


def motion(vote: dict) -> str | None:
    if vote["kind"] == "alternative":
        return f"A: {vote['formulation_a']} / B: {vote['formulation_b']}"
    return vote["formulation"]


def link(label: str) -> st.column_config.LinkColumn:
    return st.column_config.LinkColumn("Šaltinis", display_text=label)


def found_by(indices: list[int]) -> str:
    return ", ".join("užklausa" if i == 0 else f"{i} formuluotė" for i in indices)


def show_passages(passages: list[dict]) -> None:
    frame = pd.DataFrame(
        {
            "Dokumentas": DOC_TYPES.get(p["doc_type"], p["doc_type"]),
            "Pavadinimas": p["title"],
            "Data": p["doc_date"],
            "Kalbėtojas": p["speaker_name"],
            "Rasta pagal": found_by(p.get("found_by", [0])),
            "BM25 vieta": p["lexical_rank"],
            "Vektorinė vieta": p["dense_rank"],
            "RRF balas": p["score"],
            "Šaltinis": p["source_url"],
        }
        for p in passages
    )
    st.dataframe(frame, hide_index=True, column_config={"Šaltinis": link("atverti")})
    for p in passages:
        label = f"{p['title']} · {p['speaker_name'] or p['section'] or ''}"
        with st.expander(literal(label)):
            st.text(p["content"])


def show_result(name: str, data: dict) -> None:
    if name == "voting_record":
        votes = data.get("votes", [])
        st.markdown(f"**{data.get('mp')}**: balsavimų rasta {len(votes)}")
        if votes:
            frame = pd.DataFrame(
                {
                    "Laikas": vote["voted_at"],
                    "Klausimas": vote["subject"],
                    "Stadija": vote["stage"],
                    "Formuluotė": motion(vote),
                    "Balsas": CHOICES.get(vote["choice"], vote["choice"]),
                    "Frakcija tuo metu": vote["fraction_then"],
                    "Rezultatas": vote["result"],
                    "Šaltinis": vote["source"],
                }
                for vote in votes
            )
            config_ = {"Šaltinis": link("balsavimas")}
            st.dataframe(frame, hide_index=True, column_config=config_)
    elif name == "find_votes":
        votes = data.get("votes", [])
        st.markdown(f"Balsavimų rasta: {len(votes)}")
        if votes:
            frame = pd.DataFrame(
                {
                    "Laikas": vote["voted_at"],
                    "Klausimas": vote["subject"],
                    "Stadija": vote["stage"],
                    "Formuluotė": motion(vote),
                    "Už / A": vote["total_for"],
                    "Prieš / B": vote["total_against"],
                    "Susilaikė": vote["total_abstain"],
                    "Rezultatas": vote["result"],
                    "Šaltinis": vote["source"],
                }
                for vote in votes
            )
            config_ = {"Šaltinis": link("balsavimas")}
            st.dataframe(frame, hide_index=True, column_config=config_)
    elif name == "voting_agreement":
        row = {
            "Seimo narys": data["mp_a"],
            "Kitas Seimo narys": data["mp_b"],
            "Abu balsavo": data["both_voted"],
            "Vienodai": data["same_choice"],
            "Skirtingai": data["different_choice"],
            "Balsavo tik vienas": data["only_one_voted"],
            "Sutapimas": data["agreement"],
        }
        st.dataframe(pd.DataFrame([row]), hide_index=True)
        st.caption(data["method"])
    elif name == "fraction_cohesion":
        row = {
            "Frakcija": data["fraction"],
            "Balsavimai": data["votes_with_two_or_more_voting"],
            "Vieningumas": data["cohesion"],
            "Vienbalsiai": data["unanimous"],
            "Dalyvavimas": data["participation"],
        }
        st.dataframe(pd.DataFrame([row]), hide_index=True)
        st.caption(data["method"])
    elif name == "programme_votes":
        st.markdown(
            f"**{data['party']}**: 2024 m. rinkimuose iškelta Seimo narių: "
            f"{data['mps_nominated']}"
        )
        if data.get("passages"):
            show_passages(data["passages"])
        votes = data.get("votes", [])
        found = data.get("found", len(votes))
        shown = f"; lentelėje naujausi {len(votes)}" if found > len(votes) else ""
        st.markdown(f"Balsavimų rasta: {found}{shown}")
        if votes:
            frame = pd.DataFrame(
                {
                    "Laikas": vote["voted_at"],
                    "Klausimas": vote["subject"],
                    "Stadija": vote["stage"],
                    "Sąraše": vote["listed"],
                    "Už": vote["uz"],
                    "Prieš": vote["pries"],
                    "Susilaikė": vote["susilaike"],
                    "Už A": vote["a"],
                    "Už B": vote["b"],
                    "Nebalsavo": vote["nebalsavo"],
                    "Rezultatas": vote["result"],
                    "Šaltinis": vote["source"],
                }
                for vote in votes
            )
            config_ = {"Šaltinis": link("balsavimas")}
            st.dataframe(frame, hide_index=True, column_config=config_)
        st.caption(data["method"])
    elif name == "search_documents":
        queries = data.get("queries") or []
        if len(queries) > 1:
            st.caption("Ieškota ir taip, įrašo žodžiais:")
            st.text("\n".join(f"{n}. {q}" for n, q in enumerate(queries[1:], 1)))
        passages = data.get("passages", [])
        if not passages:
            st.markdown("Ištraukų nerasta.")
            return
        show_passages(passages)
        st.caption(
            "Kiekviena formuluotė ieškoma sujungus pilnateksčio (BM25) ir vektorinio "
            "artumo vietas, o formuluočių rikiavimai sujungiami RRF balu. BM25 ir "
            "vektorinė vieta rodomos pagal pačią užklausą; tuščia vieta: ištrauka "
            "nepateko tarp tos paieškos geriausių."
        )


def show_event(event: dict) -> None:
    if event["type"] == "plan":
        route = ROUTES.get(event["route"], event["route"])
        st.markdown(f"**Klausimo analizė:** {route}")
        if len(event["parts"]) > 1:
            parts = enumerate(event["parts"], 1)
            st.text("\n".join(f"{n}. {part}" for n, part in parts))
        return
    if event["type"] == "plan_error":
        st.caption("Klausimo analizė nepavyko, todėl siūlomi visi įrankiai.")
        return
    if event["type"] == "truncated":
        st.warning(
            "Atsakymas nutrūko, nes buvo per ilgas. Visi rasti duomenys pateikti "
            "lentelėse."
        )
        return
    if event["type"] == "guard":
        st.caption("Atsakyta be kalbos modelio: klausimą sustabdė saugiklis.")
        return
    if event["type"] == "clarify":
        st.caption(
            "Pavardė tinka keliems Seimo nariams: klausimas sudarytas iš Seimo narių "
            "sąrašo, ne kalbos modelio."
        )
        return
    if event["type"] == "recite":
        st.caption(
            "Atsakyme nebuvo nuorodų į šaltinius: modelio paprašyta jį perrašyti su "
            "nuorodomis. Perrašytas atsakymas tikrinamas taip pat."
        )
        return
    if event["type"] == "validation_error":
        st.warning("Atsakymas neatitiko šaltinių patikros, todėl nerodomas.")
        return
    if event["type"] == "ungrounded":
        st.warning(
            "Nė vienas įrankis negrąžino duomenų, todėl modelio atsakymas nerodomas."
        )
        return
    label = TOOLS.get(event["name"], event["name"])
    if event["type"] == "tool_call":
        args = ", ".join(f"{key}={value}" for key, value in event["args"].items())
        st.markdown(f"**{label}** {inline_code(args)}")
    elif event["type"] == "tool_error":
        st.warning(f"{label}: {literal(str(event['content']))}")
    else:
        show_result(event["name"], event["artifact"] or {})


def sources(events: list[dict]) -> dict[str, str]:
    found: dict[str, str] = {}
    for event in events:
        if event["type"] != "tool_result":
            continue
        data = event["artifact"] or {}
        votes = data.get("votes", [])
        # fraction_cohesion reports "votes" as a count, not rows
        for vote in votes if isinstance(votes, list) else []:
            subject = vote["subject"] or "be pavadinimo"
            found.setdefault(
                vote["source"], f"Balsavimas {vote['voted_at']}: {subject}"
            )
        for p in data.get("passages", []):
            kind = DOC_TYPES.get(p["doc_type"], p["doc_type"])
            found.setdefault(p["source_url"], f"{kind}: {p['title']}")
    return found


def show_sources(events: list[dict]) -> None:
    found = sources(events)
    if found:
        with st.expander(f"Šaltiniai ({len(found)})"):
            items = (f"- [{literal(label)}]({url})" for url, label in found.items())
            st.markdown("\n".join(items))


def usage_line(usage: dict) -> str:
    if not usage:
        return ""
    return (
        f"Užklausų modeliui: {usage['model_calls']} · žetonų: "
        f"{usage['input_tokens']:,} + {usage['output_tokens']:,} · "
        f"kaina: ${usage['cost_usd']:.4f}"
    )


# -------------------------------------------------------------------- turns


def show_turn(turn: dict) -> None:
    with st.chat_message("user"):
        st.text(turn["question"])
    with st.chat_message("assistant"):
        if turn["events"]:
            with st.expander("Kaip ieškota"):
                for event in turn["events"]:
                    show_event(event)
        if turn["error"]:
            st.error(literal(turn["error"]))
        else:
            st.markdown(turn["answer"])  # linked by the agent
        show_sources(turn["events"])
        st.caption(usage_line(turn["usage"]))


def ask(raw: str) -> None:
    try:
        question = bot.clean_question(raw)
    except ValueError as exc:
        st.error(str(exc))
        return
    turn = {"question": question, "answer": "", "events": [], "usage": {}}
    turn["error"] = None
    with st.chat_message("user"):
        st.text(question)
    with st.chat_message("assistant"):
        started = time.monotonic()
        status = st.status("Ieškoma Seimo įraše…", expanded=True)
        answer_box = st.empty()  # below the status: the answer as it is written
        with status:
            try:
                model = session_model()
                for event in bot.turn(
                    connection(), history(), question, model=model, stream_answers=True
                ):
                    if event["type"] == "preview":
                        answer_box.markdown(event["text"])
                    elif event["type"] == "answer":
                        turn["answer"], turn["usage"] = event["text"], event["usage"]
                        answer_box.markdown(turn["answer"])
                    else:
                        turn["events"].append(event)
                        show_event(event)
                status.update(label="Paieška baigta", state="complete", expanded=False)
            except Exception as exc:  # the model provider, the network, the database
                answer_box.empty()
                turn["answer"] = ""
                turn["error"] = f"Nepavyko atsakyti ({type(exc).__name__}): {exc}"
                status.update(label="Nepavyko", state="error")
        if turn["error"]:
            st.error(literal(turn["error"]))
        else:
            ms = round((time.monotonic() - started) * 1000)
            bot.log_turn(connection(), question, turn["events"], turn["usage"], ms)
        show_sources(turn["events"])
        st.caption(usage_line(turn["usage"]))
    st.session_state.turns.append(turn)


def markdown_export(turns: list[dict]) -> str:
    parts = ["# Seimoteka\n"]
    for turn in turns:
        parts.append(f"## {turn['question']}\n\n{turn['error'] or turn['answer']}\n")
        found = sources(turn["events"])
        if found:
            links = "\n".join(f"- [{label}]({url})" for url, label in found.items())
            parts.append(f"Šaltiniai:\n\n{links}\n")
    return "\n".join(parts)


def sidebar(turns: list[dict]) -> None:
    keys = ("model_calls", "input_tokens", "output_tokens", "cost_usd")
    totals = {key: sum(turn["usage"].get(key, 0) for turn in turns) for key in keys}
    with st.sidebar:
        st.header("Seimoteka")
        st.caption(
            "Atsakymai iš oficialių šaltinių (Seimo balsavimų, posėdžių stenogramų, "
            "teisės aktų projektų, 2024 m. rinkimų programų, Etikos ir procedūrų "
            "komisijos išvadų ir VTEK sprendimų) ir peržiūrėtų YouTube interviu, su "
            "nuorodomis į juos."
        )
        st.metric("Užklausų modeliui", totals["model_calls"])
        st.metric(
            "Žetonų (įvestis + išvestis)",
            f"{totals['input_tokens']:,} + {totals['output_tokens']:,}",
        )
        st.metric("Kaina", f"${totals['cost_usd']:.4f}")
        st.caption(f"Modelis: {config.CHAT_MODEL}")
        export = [
            {
                "question": turn["question"],
                "answer": turn["answer"],
                "error": turn["error"],
                "usage": turn["usage"],
                "sources": sources(turn["events"]),
            }
            for turn in turns
        ]
        st.download_button(
            "Atsisiųsti pokalbį (JSON)",
            json.dumps(export, ensure_ascii=False, indent=2, default=str),
            file_name="seimoteka.json",
            mime="application/json",
            disabled=not turns,
        )
        st.download_button(
            "Atsisiųsti pokalbį (Markdown)",
            markdown_export(turns),
            file_name="seimoteka.md",
            mime="text/markdown",
            disabled=not turns,
        )
        if st.button("Naujas pokalbis", disabled=not turns):
            st.session_state.turns = []
            st.rerun()


def chat_page() -> None:
    st.session_state.setdefault("turns", [])
    st.title("Seimoteka")
    st.caption(
        "Klauskite, kaip balsavo Seimo nariai ir frakcijos, ką jie sakė posėdžiuose ir "
        "interviu, ką numato teisės aktų projektai, ką partijos žadėjo rinkimuose ir "
        "kokios išvados priimtos dėl Seimo narių."
    )
    for past in st.session_state.turns:
        show_turn(past)
    if raw := st.chat_input("Jūsų klausimas", max_chars=bot.MAX_QUESTION):
        ask(raw)
    sidebar(st.session_state.turns)


def knowledge_base_page() -> None:
    import kb_page  # imported only where KB_UPDATES shows the page

    kb_page.render(connection)


st.set_page_config(page_title="Seimoteka", page_icon="🏛️", layout="wide")
pages = [st.Page(chat_page, title="Klausti", icon="💬", default=True)]
if config.KB_UPDATES:
    pages.append(
        st.Page(
            knowledge_base_page,
            title="Papildyti žinių bazę",
            icon="➕",
            url_path="papildyti",
        )
    )
st.navigation(pages, position="top" if len(pages) > 1 else "hidden").run()
