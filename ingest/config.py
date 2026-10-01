"""Central configuration for the ingestion pipeline."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "raw"
SAMPLES_DIR = DATA_DIR / "samples"

for _d in (DATA_DIR, CACHE_DIR, SAMPLES_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- politeness -------------------------------------------------------------
# Change CONTACT_EMAIL to your own. Identifying yourself is the difference
# between "a researcher" and "unexplained traffic" in someone's access log.
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "set-me@example.com")
USER_AGENT = os.getenv("USER_AGENT", "seimas-rag-research-bot/0.1")
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "1.0"))  # seconds between hits

# --- Seimas -----------------------------------------------------------------
SEIMAS_BASE = "https://apps.lrs.lt/sip"

# p_kade_id / kadencijos_id. 10 = 2024-2028 term, 9 = 2020-2024.
TERM_ID = int(os.getenv("TERM_ID", "10"))

# The p2b.* XML services, as VERIFIED by `uv run recon` on 2026-09-11.
#
# A 404 from this gateway means "no procedure with this name AND argument
# signature" - a wrong parameter name 404s exactly like a missing service.
# That is why the original guesses failed: ad_sp_darbotvarke is real, it just
# takes posedzio_id, not kadencijos_id. Samples are in data/samples/.
#
# The chain is: kadencija -> sesija -> posedis -> darbotvarke.
VERIFIED_SERVICES: dict[str, dict[str, str | int]] = {
    # no arguments; lists all 10 terms
    "ad_seimo_kadencijos": {},
    # 148 members for term 10, each with nested Pareigos (fraction + committee
    # membership, with date ranges) and Kontaktai. Also answers with no args.
    "ad_seimo_nariai": {"kadencijos_id": TERM_ID},
    # 7 sessions for term 10, each with sesijos_id
    "ad_seimo_sesijos": {"kadencijos_id": TERM_ID},
    # sittings for ONE session: posedzio_id, times, and - importantly - the
    # stenogram and protocol URLs on e-seimas.lrs.lt
    "ad_seimo_posedziai": {"sesijos_id": 141},
    # agenda for ONE sitting: question titles, bill numbers, stages
    # (Pateikimas / Svarstymas / Priemimas) and rapporteurs
    "ad_sp_darbotvarke": {"posedzio_id": -502067},
    # one roll-call: tallies + per-MP choice. Carries NO question text.
    "ad_sp_balsavimo_rezultatai": {"balsavimo_id": -55000},
}

# Probed and genuinely absent (no argument spelling found one). Fractions and
# committees are NOT lost - they are derivable from ad_seimo_nariai/Pareigos.
ABSENT_SERVICES = (
    "ad_frakcijos",
    "ad_komitetai",
    "ad_seimo_narys_pareigos",
    "ad_sp_posedziai",
    "ad_sp_svarstomi_klausimai",
    "ad_sp_balsavimai",
    "ad_teises_aktai",
    "ad_sp_priimti_ta",
)

# Kept under the old name so recon.py keeps working.
CANDIDATE_SERVICES = VERIFIED_SERVICES

# --- term 10 vote id space (bisected 2026-09-11) -----------------------------
# Monotonic in time, with isolated holes: ids whose record carries no results.
TERM10_VOTE_FIRST = -54444  # 2024-11-14 15:44:15, constitutive sitting
TERM10_VOTE_LAST = -60011  # 2026-09-10 15:28:20, live edge; re-probe before harvest
# ~32% of ids are registrations/void: balsavo=0 and every kaip_balsavo empty.

# kaip_balsavo vocabulary, observed exhaustively across term-10 votes and checked
# row by row against the Seimas's own vote pages. Abstention ('Susilaikė') is an
# affirmative act and counts toward balsavo. An empty value means the member did
# not vote: the Seimas page shows a blank row and does not say whether they were
# absent, so the label is 'nebalsavo' (did not vote), never 'nedalyvavo'.
VOTE_CHOICES = {
    "Už": "uz",
    "Prieš": "pries",
    "Susilaikė": "susilaike",
    "": "nebalsavo",
}

# In an alternative vote (motion A against motion B) the XML reuses the same
# labels: "Už" is a vote for motion A and "Prieš" a vote for motion B. Verified
# row by row against the A / B / Susil. columns of the Seimas vote page. Reading
# them as for and against would misstate what a member supported.
ALTERNATIVE_CHOICES = {
    "Už": "a",
    "Prieš": "b",
    "Susilaikė": "susilaike",
    "": "nebalsavo",
}


def service_url(name: str, params: dict | None = None) -> str:
    url = f"{SEIMAS_BASE}/p2b.{name}"
    if params:
        pairs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{pairs}"
    return url


# --- database ---------------------------------------------------------------
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://seimas:seimas@localhost:5433/seimas"
)

# --- models -----------------------------------------------------------------
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-large")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))
CHAT_MODEL = os.getenv("CHAT_MODEL", "google/gemini-2.5-flash")

# --- knowledge-base updates --------------------------------------------------
# The app's page for adding YouTube interviews downloads audio and writes to the
# database, so it is shown only when KB_UPDATES=1. Keep such a server local.
KB_UPDATES = os.getenv("KB_UPDATES", "") == "1"
