# Source catalogue

Every source below was probed on 2026-09-11 and returned what is described.
Nothing here is inferred from documentation — all of it came back over the
wire. Samples in `data/samples/`. Companion to [PHASE1.md](PHASE1.md), which
covers the vote/stenogram core in more depth.

Ranked by value to this project, not by how interesting they were to find.

---

## 1. Seimas p2b XML API — the structured core

`https://apps.lrs.lt/sip/p2b.<service>?<arg>=<value>` · no auth · CC BY 4.0

Six services, verified. The chain is `kadencija → sesija → posėdis →
darbotvarkė`. Full detail and the argument table are in [PHASE1.md](PHASE1.md).

**~3,760 substantive votes, ~250 sittings, 148 MPs for term 10.** This is the
backbone: it is the only source that is fully structured, complete, and
uniform across every MP, which is what makes the SQL tools possible and the
neutrality constraint enforceable.

The one thing it does not carry is **what any vote was about** — see PHASE1.

---

## 2. e-seimas documents — one route for the entire legal corpus

```
https://e-seimas.lrs.lt/rs/legalact/<TYPE>/<guid>/
```

This is the single most useful discovery of the session. The `portal/legalAct/`
URLs that appear throughout the Seimas data are a JSF application shell and
return no text. Rewrite them to `rs/legalact/` and you get the document body as
clean HTML. Verified for three types:

| Type | What it is | Reached from | Soft hyphens |
|---|---|---|---|
| `TAK` | stenograms, sitting protocols, Legal Department conclusions | `ad_seimo_posedziai` | **31,710 per stenogram** |
| `TAP` | bill drafts (*teisės akto projektas*) | `ad_sp_darbotvarke` → `dokumento_nuoroda` | 0 |
| `TAD` | adopted legal acts | related-documents panel | 0 |

Stenograms are ~275 KB each and are the large text corpus. Bills are small and
clean — a 7 KB resolution, article by article, which is exactly the
"chunk legal documents by article" shape Phase 2 wants.

**Related documents are the weak point.** A bill's accompanying documents —
the Legal Department's conclusion, and the explanatory note (*aiškinamasis
raštas*) for real bills — exist and are individually fetchable by the route
above, but their guids are only discoverable by rendering the JSF page, which
issues a stateful POST. Harvesting those needs a headless browser, not the
cached fetcher. The bill itself needs no such thing: its guid comes free from
the agenda.

Two search portals exist if guid discovery ever becomes the bottleneck:
`/portal/legalActSearch/lt` and `/portal/legalActProjectSearch/lt`.

---

## 3. VRK election programmes — one PDF, all parties

```
https://www.vrk.lt/documents/10180/797805/Informacinis+leidinys+Seimo+2024+rinkimams+DGMNDT+web.pdf/
```

The official VRK information publication for the 13 October 2024 Seimas
election. **35 pages, 1.2 MB, 86k characters, born-digital** (Adobe InDesign),
extracts cleanly with pypdf, and — unlike the stenograms — contains **zero soft
hyphens**.

Structure:

| Pages | Content |
|---|---|
| 1–7 | general election information |
| **8–22** | **election programmes, one party per page** |
| 23–32 | candidate lists |
| 33–35 | ballot instructions |

Each programme page is headed `RINKIMŲ PROGRAMA` + party name + list number,
then a slogan, then the promises as discrete numbered or bulleted items in the
first person future — *"Sustabdysime…"*, *"Užtikrinsime…"*, *"Remsime…"*.
That is close to ideal for the promise-vs-votes tool: each bullet is already an
atomic, attributable commitment, and the set is uniform across all 17 lists,
which keeps the tool neutral by construction.

Caveat: these are the official **one-page summaries** submitted to VRK, not the
parties' full manifestos. Uniform and primary, but shallow — expect a promise
to be matchable to a policy area rather than to a specific bill.

VRK's open-data catalogue (16 datasets on data.gov.lt) covers candidates,
results and campaign finance, but **contains no programmes** — those are only
published as documents. Single-member-district candidate programmes are 71
separate per-district PDF booklets under `vrk.lt/documents/10180/808321/…`;
low priority, since MPs vote with their fractions.

---

## 4. Seimas Ethics and Procedures Commission — the best accountability source

```
listing:  https://www.lrs.lt/sip/portal.show?p_r=38585&p_k=1&p_a=1000&p_kade_id=10
finding:  https://www.lrs.lt/sip/portal.show?p_r=38585&p_k=1&p_t=<id>
```

**49 findings for term 10**, of which **23 name a specific MP** and 26 concern
statutory or procedural questions. 11 distinct MPs named. This is the Seimas'
own body, it deals exclusively with MPs, and it is materially richer than VTEK
for this project.

Two things to handle:

- **Names in the titles are genitive.** *"Dėl Lietuvos Respublikos Seimo nario
  Remigijaus Žemaitaičio elgesio"*, *"Igno Vėgėlės"*, *"Raimondo Kuodžio"*,
  *"Simono Kairio"*. Attributing a finding to an MP means mapping genitive →
  nominative. This is the inflection problem appearing in **linking**, not just
  in user queries — a second, independent reason the `name_variants` column
  and the trigram index need to exist.
- **The distribution is skewed**: 12 of the 23 named findings concern one MP.
  That is a property of the record. Report it faithfully when asked about that
  MP; never rank MPs by finding count. A "who has the most findings" tool would
  be exactly the targeting the project forbids, and it is not hard to imagine
  someone asking for it.

The document pages are ~95 KB of portal chrome around the text — extract the
content container, don't take the whole page. No soft hyphens.

---

## 5. VTEK decisions — a real API, but thin for this project

```
index:   https://pinreg.vtek.lt/external/viesi-vtek-sprendimai?puslapioNr=1&irasuSkaiciusPuslapyje=500&rusiuojamiStulpeliai=PRIEMIMO_DATA&rusiavimoKryptis=DESC
detail:  https://pinreg.vtek.lt/external/viesi-vtek-sprendimai/<id>
```

A clean public Spring Boot JSON API, no auth. The **entire register is 396
decisions and comes back in a single request**; each detail record is ~60 KB.

Fields worth knowing: `sprendimas` (full reasoned decision), `rezoliucineDalis`
(operative part), `balsavimoRezultatai` (how each commissioner voted),
`teisesAktasPavadinimas`, and critically **`apskustasTeismui`** (appealed to
court) and **`panaikintasTeismoSprendimu`** (annulled by court).

> Never present a decision as a standing finding without checking
> `panaikintasTeismoSprendimu`. An annulled decision reported as live is a
> factual error about a named living person — the precise failure this project
> is built to avoid.

Two limits, both material:

- **Yield is low.** Joining all 396 against the 148 term‑10 MPs gives **exactly
  2 exact matches** (Ruginienė KS‑2026/69, Žemaitaitis KS‑2026/39). Worth
  ingesting because it is cheap and official, not because it is broad.
- **Older decisions are anonymised.** 215 of 396 have `vardas`/`pavardė` =
  `"Nebeskelbiama"`. VTEK redacts after a retention period; respect that and do
  not try to re-identify.

**A name-matching warning, from the data.** Two decisions concern *Artūras
Visockas*; term 10 contains an MP named *Paulius Visockas*. Surname matching
would attribute a different person's ethics decision to a sitting MP. Match on
full name, and prefer an explicit reviewed mapping over any fuzzy join. Trigram
similarity is the right tool for *retrieval*; it is the wrong tool for
*attribution*.

---

## 6. Constitutional Court — low priority

`https://lrkt.lt/lt/teismo-aktai/paieska/135` — web search form only. No API,
no RSS, no bulk download, no URL pagination.

Most rulings concern whether a law is constitutional, not the conduct of a
named MP, so the fit with this project's questions is weak. The one genuinely
relevant subset is **impeachment conclusions** (*išvada* on whether an MP
breached the Constitution or their oath), which are few and historically
significant. Treat as a short hand-curated list if it comes up at all; do not
build a scraper.

---

## 7. PINREG interest declarations — closed

Private-interest declarations would have been a strong, uniform, per-MP source.
They are not publicly available: every `/external/` path other than
`viesi-vtek-sprendimai` returns `401 Full authentication is required`. That is
an access-control decision by VTEK and the project should treat it as closed
rather than look for a way around it. Revisit only if VTEK grants credentials.

---

## Required code changes

**`ingest/http.py` corrupts binary responses.** It uses `resp.text` and
`path.write_text()`, so the VRK PDF came back as 1,150,074 characters of mojibake
instead of 1,198,462 bytes. Any PDF source needs a binary path:

```python
def fetch_bytes(url, *, suffix=".pdf", force=False, timeout=30) -> bytes | None:
    # same cache + delay + User-Agent discipline, but resp.content / write_bytes
```

Keep the cache key, the delay floor and the contact header identical — this is a
second body type, not a second fetcher.

**Text normalisation belongs in one place**, applied to every HTML source
before chunking:

```python
text = html.unescape(strip_tags(raw)).replace("­", "").replace("\xa0", " ")
```

U+00AD alone is the difference between a stenogram corpus that retrieves and one
that silently never matches anything.

**Add `pypdf`** to the ingest group when the programmes are loaded
(`uv add --group rag pypdf`, or a new `docs` group).

## Suggested document types

Extends the `documents.doc_type` vocabulary already in the schema:

```
stenogram    TAK via ad_seimo_posedziai       ~250 docs, large, soft-hyphenated
protocol     TAK via ad_seimo_posedziai       ~250 docs
bill         TAP via ad_sp_darbotvarke        one per agenda item with a document
act          TAD via related documents        requires JSF rendering
legal_note   TAK, Legal Department conclusion  requires JSF rendering
programme    VRK PDF, pages 8-22              17 party programmes
ethics       Seimas Ethics Commission          49 findings, 23 MP-named
vtek         VTEK decisions API                396 total, 2 MP-relevant
```

## Politeness ledger

All fetches went through the cached, rate-limited client. Session total across
both passes: **~180 requests, 4.5 MB cached**, no source hit faster than once
per second, every request carrying the contact address. The VTEK register was
pulled in one request rather than 20 by raising the page size — prefer that
shape wherever an API allows it.
