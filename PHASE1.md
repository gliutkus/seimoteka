# Phase 1 — Data

The structured term-10 record — MPs, fraction membership, positions, sessions,
sittings, agendas, votes and every individual vote — harvested from the Seimas,
loaded into Postgres, and checked against the Seimas's own published pages.

Numbers live in [reports/phase1.md](reports/phase1.md), which is generated. This
file records the method, what the data turned out to be, and what changed from
the plan written after the first reconnaissance.

## Running it

```bash
docker compose up -d db                     # Postgres 17 + pgvector; schema via initdb
uv run python -m ingest.seimas harvest      # network: warm the cache
uv run python -m ingest.seimas verify       # offline: XML vs the Seimas vote pages
uv run python -m ingest.seimas load         # offline: cache -> Postgres
uv run python -m ingest.seimas report       # offline: reports/phase1.md
```

**harvest** is the only command that touches the network. It runs serially at
the 1-second floor, identifies itself, and caches every response, so a restart
costs the source nothing. A first run is roughly 10,000 requests and a few hours;
later runs fetch only what is new. Each run refreshes the listings that change
while the term is running (roster, sessions, recent sittings and agendas) and
re-probes the end of the vote series with fresh requests — a cached "no such
vote" response for an id beyond the end would otherwise go stale and silently
truncate the record. Failed requests are not cached and are retried next run.
Cache writes are write-then-rename, so an interrupted harvest cannot leave a
truncated response behind. Progress is in `data/seimas_harvest.json`.

The portal sometimes answers with its *Puslapis nerastas* (page not found) page
and status 200. Five sitting pages were cached that way in the first harvest and
read as sittings without an agenda. `fetch` now takes a validator: a rejected
response counts as a failed request and is not cached, and a rejected body
already in the cache counts as a miss, so the next harvest fetches it again.
For those five the portal answered the same way on the second harvest too.

**verify**, **load** and **report** read only the cache and the database.
`load` is idempotent: it rebuilds the Phase 1 tables in one transaction and
upserts the dimension tables that later phases reference.

## Sources

| Source | Gives | Used for |
| --- | --- | --- |
| `p2b.ad_seimo_kadencijos` | all terms | `terms` |
| `p2b.ad_seimo_nariai?kadencijos_id` | roster with nested roles and dates | `mps`, `fractions`, `mp_fractions`, `mp_positions` |
| `p2b.ad_seimo_sesijos?kadencijos_id` | sessions | `sessions` |
| `p2b.ad_seimo_posedziai?sesijos_id` | sittings, stenogram and protocol links | `sittings` |
| `p2b.ad_sp_darbotvarke?posedzio_id` | agenda questions and their stages | `agenda_items` |
| `p2b.ad_sp_balsavimo_rezultatai?balsavimo_id` | tallies and every member's choice | `votes`, `mp_votes.choice` |
| vote page `portal.show?p_a=sale_bals&p_bals_id` | question, motion, result, agenda ids, fraction at the vote | `votes`, `mp_votes.fraction_abbrev` |
| sitting page `portal.show?p_a=sale_fakt_pos&p_fakt_pos_id` | agenda lines in a second id space | `agenda_lines` |

The XML services are the open-data API (CC BY 4.0). The two portal pages are the
Seimas's public website; they are fetched only where no service carries the same
facts — the vote page for votes with individual results, the sitting page once
per sitting.

## What the data turned out to be

### Every vote links to its agenda stage by id, from the source

No open-data service says what a vote was about, and the original plan was to
infer it by matching tallies announced in the stenogram. That is unnecessary.
The page the Seimas publishes for each vote states the agenda question, the
motion wording (*Formuluotė*), the published result (*pritarta*, *nepritarta*
...), and links to the agenda stage by the same `darbotvarkės_klausimo_id` that
`ad_sp_darbotvarke` uses. Vote `-56454` links to stage `-97712`, the *Pateikimas*
of bill XVP-717. The link is a fact published by the Seimas, not an inference,
so it can be cited.

Procedural votes — adding an item to the agenda, for instance — print an empty
question and point only at the sitting page's own numbering
(`p_svarst_kl_stad_id`). Those ids are loaded as `agenda_lines`, and the motion
wording (*"dėl pasiūlymo įrašyti į darbotvarkę projektą Nr. XVP-217"*) is kept.
The page of a sitting not yet held shows the agenda of the sitting in progress
instead, so lines are read only from sittings that have a start time.

A few pages print the tally and nothing after it — vote `-54579` reads
*"už - 91, prieš - 1, susilaikė - 0."* — and a few print no motion wording.
`votes.result` and `votes.formulation` are NULL there. Whether a motion passed
cannot be read off the tally, because the required majority depends on the kind
of act, so nothing downstream may infer a result the page does not state.

### Abstaining and not voting are different acts

`kaip_balsavo` takes exactly `Už`, `Prieš`, `Susilaikė` or an empty value.
Abstention counts toward `balsavo`; an empty value does not. On the Seimas vote
page an empty value is a blank row, and the page does not distinguish a member
who was absent from one who was present and did not vote. The stored label is
therefore `nebalsavo` (did not vote), never `nedalyvavo` (did not take part):
claiming absence would assert something the record does not show about a named
person. `verify` compares every member's choice on every vote with the page:
on the complete harvest, 513,628 rows and no disagreement (516,708 after the
update of 2026-09-16, still none). Three votes
(`-59162`, `-55934`, `-56830`) were also checked by eye in a browser.

### In an alternative vote, `Už` and `Prieš` mean motion A and motion B

When the Seimas chooses between two motions, the vote is *alternatyvus*. Vote
`-56958` set *"už pasiūlymą grąžinti šį projektą iniciatoriams tobulinti"*
(return the bill to its initiators) against *"už pasiūlymą jį atmesti"* (reject
it). Its page prints *Formuluotė A* and *Formuluotė B*, the tally *"A - 39, B -
59, susilaikė - 0, pritarta formuluotei B"*, and table columns *A*, *B* and
*Susil.* The XML carries the same vote with the ordinary labels: 39 `Už` and 59
`Prieš`. Checked member by member against the page, `Už` is a vote for motion A
and `Prieš` a vote for motion B. Read as for and against, the record would say
39 members supported a bill that every one of the 98 voting members wanted
returned or rejected.

The chamber uses the same convention aloud. Before vote `-56768` the chair
proposed *"už – Ekonomikos komitetas, o prieš – Aplinkos apsaugos komitetas"*
and then announced *"už, tai yra už Ekonomikos komitetą, – 30, prieš – 87"*;
the XML records 30 `Už` and 87 `Prieš`, and the page *"pritarta formuluotei B"*.
Because announcements use *už* and *prieš* for motions A and B as well, the
stenogram tally check needs no special case for these votes.

These votes are stored with `kind = 'alternative'`, both motions in
`formulation_a` / `formulation_b`, `total_for` / `total_against` counting A and B,
and member choices `a` / `b`. Agreement and cohesion treat `a` and `b` as choices
like any other. The tally parser had skipped every one of these pages until the
pattern was found, which is how it surfaced: they were the votes without a
result.

### The XML's fraction field is not the fraction at the time of the vote

On vote `-56454` (10 September 2025) the XML gives Juozas Olekas no fraction; the
vote page gives `LSDPF`. The roster puts his LSDPF membership from 2024-11-14 to
2025-09-10, the day of that vote, after which he became Speaker, a post held
outside any fraction. The XML field evidently reflects membership when the data
is generated, not when the vote was cast. Two other members on the same vote
show the same pattern. The fraction stored on `mp_votes` comes from the vote
page; the XML value is compared in `verify` and discarded. Getting this wrong
would move votes between fractions retroactively and corrupt every cohesion
figure.

Fraction abbreviations (`LSDPF`, `TS-LKDF`, `MSNG` ...) are not typed in
anywhere. `load` derives which fraction each abbreviation denotes from which
fraction its members belonged to on the day of each vote, and reports any
abbreviation that does not map cleanly.

### On the day a member changes fraction, vote pages list them twice

Membership has inclusive dates at both ends, so on the day a member switches
fraction the roster puts them in both. The vote pages do the same: every vote
that day lists the member once under each fraction, with identical marks. On
14 January 2025 a member moving from NAF to DFVL appears under both on all 24
votes. Neither source says which fraction applied at a given vote, so the rows
are merged and `mp_votes.fraction_abbrev` is NULL for that day; cohesion counts
the member in neither fraction.

These pages carry the note *"Individualūs balsavimo rezultatai, gauti
elektroninėmis priemonėmis, neatitinka suminių rezultatų, įrašytų protokole"*
(the individual electronic results do not match the totals in the protocol).
`verify` shows that every page with the note lists a member who voted twice,
so the page counts that vote twice (*"Balsavo Seimo narių: 109 iš 142"* against
108 of 141 in the XML), and that on every one of these votes the XML totals
equal the individual choices. The note describes the page's own double count,
not a disputed result.

The first sitting of the term lists the outgoing Seimas as well: vote `-54448`
has 293 rows for 214 people against 137 in the XML. Keeping the last row per
member had silently dropped the marks of 21, 22 and 26 members on that day's
three votes.

### A third of vote ids are quorum registrations

Ids with `balsavo = 0` and every choice empty are registrations. In sitting
`-502067` the stenogram records *"Registruojamės. Užsiregistravo 128 Seimo
nariai"* at the time of `-56453` and *"Užsiregistravo 71 Seimo narys"* at the
time of `-56458`. Their vote pages carry no question and no table. They are kept
in `votes`, flagged by `has_individual_results = false`, and have no `mp_votes`
rows; every statistic must exclude them. The service publishes no registration
counts or registered members, so attendance cannot be computed from it. Not
every such id is an announced registration: that sitting has three and its
stenogram announces two.

Registrations are assigned to a sitting by time, since they have no page to
state it. `verify` measures that heuristic against the page-stated sitting of
every other vote.

### Fraction membership is temporal, marked by role, and includes the mixed group

The roster nests roles with date ranges. A role beginning `Frakcijos` —
*narys*, *narė*, *seniūnas*, *seniūnė*, *seniūno pavaduotojas* — marks fraction
membership. Matching the role rather than the body name matters twice: role
strings are grammatically gendered, so a masculine-only match drops every woman;
and *Mišri Seimo narių grupė*, the mixed group, is a body without *frakcija* in
its name whose members hold `Frakcijos narys` roles.

The first plan counted four sitting members as having no fraction. They are in
the mixed group. The only people never in a fraction are the four elected who
gave up their mandate on the first day. A Speaker holds no fraction while in
office, which is a real state and appears as a NULL fraction on their votes.

### Roster fields need normalising before anything groups by them

- `išrinkimo_būdas` is either `Pagal sąrašą` or `" <name> (Nr. N) apygardoje"`
  with a leading space; it is split into `elected_via`, `constituency`,
  `constituency_no`.
- `iškėlusi_partija` spells Tėvynės sąjunga–Lietuvos krikščionys demokratai with a
  hyphen and with a spaced en dash; dashes are normalised, merging 30 + 1.
- One fraction name contains a double space. All names are whitespace-normalised.
- `name_variants` holds the diacritic-folded case forms of every name part
  (`ingest/names.py`), so *Žemaitaičio* and *zemaitaicio* both resolve.

### Lithuanian full-text search needs no external lemmatiser

This Postgres ships a `lithuanian` text-search configuration. It folds case
endings and stem alternations: *Žemaitaičio* → `žemaitait`, *mokesčių* →
`mokest`, *įstatymų* → `įstatym`. `chunks.tsv` uses it, and the planned stanza
lemmatisation is dropped. Two caveats carry into retrieval. It stems the surname
*Balsys* and the verb *balsuoti* to the same lexeme, so people must be resolved
through `mps.name_variants`, never through full-text rank. And it does not fold
definite adjective endings (*antroji* → `antroj`, *antrąją* → `antrąj`), which
dense retrieval has to cover.

## Changes from the first plan

| First plan | Now | Why |
| --- | --- | --- |
| Link votes to agenda items by matching stenogram tallies | Take the stage id from the Seimas vote page | The source publishes the link |
| Empty choice = `nedalyvavo` | `nebalsavo` | The record shows no vote, not absence |
| Choices `uz` / `pries` / `susilaike` | Plus `a` / `b`, and `votes.kind` | Alternative votes reuse `Už` / `Prieš` for motions A and B |
| `mp_votes.fraction` from the XML | From the vote page | The XML gives current membership |
| One vote-page row per member | Rows merged per member; fraction NULL where they differ | Members are listed twice on the day they switch |
| Four non-attached members with no fraction | Members of the mixed group | Fraction membership is marked by role |
| `mps.fraction` column | `mp_fractions` with date ranges | Membership changes during the term |
| Lemmatise with stanza for BM25 | Postgres `lithuanian` configuration | Verified to handle Lithuanian inflection |
| `votes.question NOT NULL` | Nullable, plus motion wording and agenda ids | Procedural votes print no question |
| Any cached 200 response is data | Portal pages are validated | The portal serves "page not found" with status 200 |

## Known limits

- Term 10 only. `mps` is keyed by person, so a multi-term record needs a
  person/mandate split.
- Registrations carry no per-member data; attendance is not available.
- A member's fraction is unknown for the votes held on the day they switched.
- Some vote pages state no result or no motion wording; those fields are NULL
  and no result is inferred from a tally.
- The portal serves *Puslapis nerastas* for five sitting pages: the sitting of
  2024-11-14 at 14:31, the morning sittings of 13 and 14 January 2025, and both
  sittings of 20 March 2025. Seven procedural votes there, whose pages point
  only at a sitting-page line, have no agenda link.
- Parliamentary friendship and interest groups are not loaded.
- Published votes are treated as immutable once they carry results. Listings
  younger than 14 days are refreshed on each harvest.
- Stenograms, bills, programmes, ethics findings and VTEK decisions are not
  loaded here; they are the Phase 2 corpus.

## Carried into Phase 2

- Stenogram text needs U+00AD removed, HTML entities unescaped, and paragraph
  drop caps rejoined: the source splits the first letter from its word
  (*"U žsiregistravo"*, *"P rašom"*).
- Speaker turns are marked like `V. PRANCKIETIS ( LSF ) .` and
  `PIRMININKAS (J. OLEKAS , LSDPF * ).`; the fraction abbreviations match
  `fractions.abbrev`.
- `sittings.stenogram_url` is the citation link; fetch text from
  `https://e-seimas.lrs.lt/rs/legalact/TAK/<guid>/`.
- Stenograms announce alternative votes in the same words as ordinary ones,
  *už* for motion A and *prieš* for motion B.
