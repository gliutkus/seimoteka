-- Seimas accountability RAG: one Postgres for the structured record (tool
-- calls) and the chunk store (retrieval). Term 10 scope; see PHASE1.md.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- -------------------------------------------------------------- dimensions

CREATE TABLE IF NOT EXISTS terms (
    id          INTEGER PRIMARY KEY,          -- kadencijos_id, e.g. 10
    label       TEXT NOT NULL,                -- '2024–2028 metų kadencija'
    date_from   DATE,
    date_to     DATE
);

CREATE TABLE IF NOT EXISTS sessions (
    id          BIGINT PRIMARY KEY,           -- sesijos_id
    term_id     INTEGER NOT NULL REFERENCES terms(id),
    number      INTEGER,                      -- running session number
    label       TEXT NOT NULL,                -- '3 eilinė', 'neeilinė'
    date_from   DATE,
    date_to     DATE                          -- NULL while the session is open
);

CREATE TABLE IF NOT EXISTS mps (
    id              BIGINT PRIMARY KEY,       -- asmens_id; one row per person (term-10 scope)
    term_id         INTEGER NOT NULL REFERENCES terms(id),
    first_name      TEXT NOT NULL,
    last_name       TEXT NOT NULL,
    full_name       TEXT GENERATED ALWAYS AS (first_name || ' ' || last_name) STORED,
    -- Diacritic-folded case forms of every name part (ingest.names), so both
    -- "Žemaitaičio" and "zemaitaicio" resolve to Žemaitaitis.
    name_variants   TEXT[] NOT NULL DEFAULT '{}',
    gender          CHAR(1),                  -- 'V' / 'M' as published
    nominated_by    TEXT,                     -- iškėlusi_partija, dash-normalised
    elected_via     TEXT CHECK (elected_via IN ('list', 'constituency')),
    constituency    TEXT,
    constituency_no INTEGER,
    terms_served    INTEGER,                  -- kadencijų_skaičius, including this one
    date_from       DATE NOT NULL,            -- mandate
    date_to         DATE,                     -- NULL while the mandate is open
    source_url      TEXT
);

CREATE INDEX IF NOT EXISTS mps_lastname_trgm ON mps USING gin (last_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS mps_name_variants ON mps USING gin (name_variants);

-- Fractions, including Mišri Seimo narių grupė (the mixed group), which the
-- roster models as a body whose members hold 'Frakcijos narys' roles.
CREATE TABLE IF NOT EXISTS fractions (
    id       BIGINT PRIMARY KEY,              -- padalinio_id
    term_id  INTEGER NOT NULL REFERENCES terms(id),
    name     TEXT NOT NULL,                   -- whitespace-normalised
    -- Abbreviation printed on vote records (LSDPF, TS-LKDF, MSNG ...), derived
    -- at load from which members carried it while in the fraction.
    abbrev   TEXT UNIQUE
);

-- Membership is temporal: members change fractions mid-term.
CREATE TABLE IF NOT EXISTS mp_fractions (
    mp_id        BIGINT NOT NULL REFERENCES mps(id),
    fraction_id  BIGINT NOT NULL REFERENCES fractions(id),
    role         TEXT NOT NULL,               -- raw and gendered: narys / narė / seniūnas ...
    date_from    DATE NOT NULL,
    date_to      DATE,                        -- inclusive; NULL = current
    PRIMARY KEY (mp_id, fraction_id, date_from)
);

CREATE INDEX IF NOT EXISTS mp_fractions_fraction ON mp_fractions (fraction_id);

-- Committees, commissions, the board, and chamber posts such as Speaker.
CREATE TABLE IF NOT EXISTS mp_positions (
    mp_id      BIGINT NOT NULL REFERENCES mps(id),
    body_id    BIGINT,                        -- padalinio_id; NULL for chamber posts
    body_name  TEXT NOT NULL,                 -- 'Seimas' for chamber posts
    role       TEXT NOT NULL,
    date_from  DATE NOT NULL,
    date_to    DATE,
    PRIMARY KEY (mp_id, body_name, role, date_from)
);

-- ---------------------------------------------------------------- sittings

CREATE TABLE IF NOT EXISTS sittings (
    id             BIGINT PRIMARY KEY,        -- posėdžio_id
    session_id     BIGINT NOT NULL REFERENCES sessions(id),
    term_id        INTEGER NOT NULL REFERENCES terms(id),
    number         TEXT,
    kind           TEXT,                      -- Rytinis / Vakarinis / Nenumatytas posėdis
    starts_at      TIMESTAMPTZ,
    ends_at        TIMESTAMPTZ,
    stenogram_url  TEXT,
    protocol_url   TEXT,
    page_url       TEXT NOT NULL
);

-- One row per agenda question STAGE, as published by ad_sp_darbotvarke.
-- Times are planned; the chamber routinely runs past them.
CREATE TABLE IF NOT EXISTS agenda_items (
    id            BIGINT PRIMARY KEY,         -- darbotvarkės_klausimo_id
    sitting_id    BIGINT NOT NULL REFERENCES sittings(id),
    number        TEXT,                       -- as printed, e.g. '1-4.'
    title         TEXT NOT NULL,
    stage         TEXT,                       -- Pateikimas / Svarstymas / Priėmimas, or NULL
    bill_number   TEXT,
    document_url  TEXT,
    planned_from  TIME,
    planned_to    TIME,
    rapporteurs   TEXT[] NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS agenda_items_sitting ON agenda_items (sitting_id);
CREATE INDEX IF NOT EXISTS agenda_items_bill ON agenda_items (bill_number);
CREATE INDEX IF NOT EXISTS agenda_items_title_trgm ON agenda_items USING gin (title gin_trgm_ops);

-- The sitting page numbers its agenda lines in a second id space. Some vote
-- pages point only here, typically procedural votes with no question title.
CREATE TABLE IF NOT EXISTS agenda_lines (
    id          BIGINT PRIMARY KEY,           -- p_svarst_kl_stad_id
    sitting_id  BIGINT NOT NULL REFERENCES sittings(id),
    ordinal     INTEGER NOT NULL,
    title       TEXT NOT NULL
);

-- ------------------------------------------------------------------- votes

CREATE TABLE IF NOT EXISTS votes (
    id               BIGINT PRIMARY KEY,      -- balsavimo_id (negative, descending)
    term_id          INTEGER NOT NULL REFERENCES terms(id),
    sitting_id       BIGINT REFERENCES sittings(id),
    agenda_item_id   BIGINT REFERENCES agenda_items(id),  -- vote page p_kl_stad_id
    agenda_line_id   BIGINT REFERENCES agenda_lines(id),  -- vote page p_svarst_kl_stad_id
    voted_at         TIMESTAMPTZ NOT NULL,
    question         TEXT,                    -- as printed on the vote page; may be empty
    formulation      TEXT,                    -- 'Formuluotė': what the motion actually was
    -- An alternative vote sets motion A against motion B: total_for counts A,
    -- total_against counts B, and choices are 'a' / 'b'. NULL: page not read.
    kind             TEXT CHECK (kind IN ('standard', 'alternative')),
    formulation_a    TEXT,
    formulation_b    TEXT,
    bill_number      TEXT,
    result           TEXT,                    -- as published: pritarta, nepritarta ...
    total_for        INTEGER NOT NULL,
    total_against    INTEGER NOT NULL,
    total_abstain    INTEGER NOT NULL,
    total_voted      INTEGER NOT NULL,        -- balsavo = for + against + abstain
    total_members    INTEGER NOT NULL,        -- viso
    -- About a third of vote ids carry no individual results (balsavo = 0 and
    -- every choice empty). They have no mp_votes rows; exclude them everywhere.
    has_individual_results BOOLEAN GENERATED ALWAYS AS (total_voted > 0) STORED,
    source_url       TEXT NOT NULL,           -- open-data XML
    page_url         TEXT NOT NULL            -- the Seimas page a citation links to
);

CREATE INDEX IF NOT EXISTS votes_voted_at ON votes (voted_at);
CREATE INDEX IF NOT EXISTS votes_sitting ON votes (sitting_id);
CREATE INDEX IF NOT EXISTS votes_bill ON votes (bill_number);
CREATE INDEX IF NOT EXISTS votes_question_trgm ON votes USING gin (question gin_trgm_ops);

CREATE TABLE IF NOT EXISTS mp_votes (
    vote_id          BIGINT NOT NULL REFERENCES votes(id) ON DELETE CASCADE,
    mp_id            BIGINT NOT NULL REFERENCES mps(id),
    -- A blank on the Seimas vote page means the member did not vote. The page
    -- does not distinguish absent from present-but-not-voting, so neither do
    -- we: 'nebalsavo', never 'nedalyvavo'. Abstention is an affirmative act.
    choice           TEXT NOT NULL
        CHECK (choice IN ('uz', 'pries', 'susilaike', 'nebalsavo', 'a', 'b')),
    -- As of the vote, from the vote page. The open-data XML's frakcija gives
    -- current membership, not membership at the vote, so it is not used.
    -- NULL = no fraction at that moment (e.g. while serving as Speaker).
    fraction_abbrev  TEXT,
    PRIMARY KEY (vote_id, mp_id)
);

CREATE INDEX IF NOT EXISTS mp_votes_mp ON mp_votes (mp_id);
CREATE INDEX IF NOT EXISTS mp_votes_fraction ON mp_votes (fraction_abbrev);

-- Provenance for the answer layer: what the record is current to.
CREATE TABLE IF NOT EXISTS dataset_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------ corpus

CREATE TABLE IF NOT EXISTS documents (
    id          BIGSERIAL PRIMARY KEY,
    doc_type    TEXT NOT NULL,   -- stenogram | programme | vtek | ethics | bill | youtube_interview
    title       TEXT,
    doc_date    DATE,
    term_id     INTEGER,
    source_url  TEXT NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{}',  -- source facts, e.g. appeal and annulment
    fetched_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (source_url)
);

-- The MPs a finding or decision is about: every member its title names, so a
-- joint finding belongs to each of them.
CREATE TABLE IF NOT EXISTS document_subjects (
    document_id  BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    mp_id        BIGINT NOT NULL REFERENCES mps(id),
    PRIMARY KEY (document_id, mp_id)
);

CREATE INDEX IF NOT EXISTS document_subjects_mp ON document_subjects (mp_id);

CREATE TABLE IF NOT EXISTS chunks (
    id            BIGSERIAL PRIMARY KEY,
    document_id   BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal       INTEGER NOT NULL,
    content       TEXT NOT NULL,
    -- speaker-turn metadata: this is what makes "what did X say about Y" work
    speaker_name  TEXT,
    speaker_mp_id BIGINT REFERENCES mps(id),
    fraction      TEXT,
    doc_type      TEXT,
    doc_date      DATE,
    source_url    TEXT,
    sitting_id    BIGINT REFERENCES sittings(id),   -- stenograms: the sitting
    section       TEXT,        -- the heading a chunk falls under: agenda item, party, article
    section_at    TIMESTAMPTZ, -- when that section started, where the source says
    bill_number   TEXT,
    -- This Postgres ships a Lithuanian Snowball stemmer, verified to fold case
    -- endings and stem alternations (Žemaitaičio -> žemaitait, mokesčių ->
    -- mokest), so no external lemmatiser is needed. It also stems the surname
    -- Balsys and the verb balsuoti to one lexeme: resolve people through
    -- mps.name_variants, never through full-text rank.
    tsv           tsvector GENERATED ALWAYS AS (to_tsvector('lithuanian', content)) STORED,
    embedding     vector(1024),
    UNIQUE (document_id, ordinal)
);

CREATE INDEX IF NOT EXISTS chunks_tsv ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_speaker ON chunks (speaker_mp_id);
CREATE INDEX IF NOT EXISTS chunks_doc_type ON chunks (doc_type);
CREATE INDEX IF NOT EXISTS chunks_date ON chunks (doc_date);

-- Upgrades for databases created from an earlier version of this file.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS sitting_id BIGINT REFERENCES sittings(id);
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS section TEXT;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS section_at TIMESTAMPTZ;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS bill_number TEXT;
CREATE INDEX IF NOT EXISTS chunks_sitting ON chunks (sitting_id);
CREATE INDEX IF NOT EXISTS chunks_bill ON chunks (bill_number);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}';
-- documents.subject_mp_id held a single MP; a joint finding names several.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'documents' AND column_name = 'subject_mp_id'
    ) THEN
        INSERT INTO document_subjects (document_id, mp_id)
        SELECT id, subject_mp_id FROM documents WHERE subject_mp_id IS NOT NULL
        ON CONFLICT DO NOTHING;
        ALTER TABLE documents DROP COLUMN subject_mp_id;
    END IF;
END $$;
ALTER TABLE votes ADD COLUMN IF NOT EXISTS kind TEXT;
ALTER TABLE votes DROP CONSTRAINT IF EXISTS votes_kind_check;
ALTER TABLE votes ADD CONSTRAINT votes_kind_check
    CHECK (kind IN ('standard', 'alternative'));
ALTER TABLE votes ADD COLUMN IF NOT EXISTS formulation_a TEXT;
ALTER TABLE votes ADD COLUMN IF NOT EXISTS formulation_b TEXT;
ALTER TABLE mp_votes DROP CONSTRAINT IF EXISTS mp_votes_choice_check;
ALTER TABLE mp_votes ADD CONSTRAINT mp_votes_choice_check
    CHECK (choice IN ('uz', 'pries', 'susilaike', 'nebalsavo', 'a', 'b'));

-- Embeddings: intfloat/multilingual-e5-large, 1024 dimensions, chosen on real
-- Lithuanian queries (reports/embeddings.md). There is no HNSW index: at this
-- corpus's size an exact scan is fast and stays exact under the metadata filters
-- every search applies. `uv run python -m rag.embed --set-dims N` resizes the
-- column while it is empty.

-- ------------------------------------------------------------ observability

CREATE TABLE IF NOT EXISTS query_log (
    id                BIGSERIAL PRIMARY KEY,
    asked_at          TIMESTAMPTZ DEFAULT now(),
    question          TEXT NOT NULL,
    translated        TEXT,
    retrieval_mode    TEXT,          -- dense | bm25 | hybrid
    chunk_ids         BIGINT[],
    tools_called      TEXT[],
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    cost_usd          NUMERIC(10, 6),
    latency_ms        INTEGER
);

-- BM25 statistics for the lexical side of retrieval (rag/retrieval.py), rebuilt
-- after each corpus load by `uv run python -m rag.retrieval stats`.
CREATE TABLE IF NOT EXISTS lexeme_stats (
    word TEXT PRIMARY KEY,
    ndoc INTEGER NOT NULL                 -- chunks containing the lexeme
);
CREATE TABLE IF NOT EXISTS corpus_stats (
    id           BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),  -- a single row
    chunks       INTEGER NOT NULL,
    avg_lexemes  DOUBLE PRECISION NOT NULL,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
