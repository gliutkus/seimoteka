# Phase 1 reconciliation report

Generated 2026-09-16 23:57 by `uv run python -m ingest.seimas report` from Postgres and the offline verification in `data/seimas_verify.json`. Regenerate; do not edit.

## Rows loaded

| table | rows |
| --- | --- |
| mps | 148 |
| mp_fractions | 199 |
| mp_positions | 603 |
| fractions | 7 |
| sessions | 7 |
| sittings | 185 |
| agenda_items | 5128 |
| agenda_lines | 4380 |
| votes | 5391 |
| mp_votes | 516708 |

## Mandates

| people on the term roster | open mandates | list | constituency | never in a fraction |
| --- | --- | --- | --- | --- |
| 148 | 140 | 74 | 74 | 4 |

## Fractions

| abbrev | name | current members | members at any time |
| --- | --- | --- | --- |
| LSDPF | Lietuvos socialdemokratų partijos frakcija | 52 | 53 |
| TS-LKDF | Tėvynės sąjungos-Lietuvos krikščionių demokratų frakcija | 28 | 30 |
| NAF | „Nemuno aušros“ frakcija | 18 | 20 |
| DFVL | Demokratų frakcija „Vardan Lietuvos“ | 13 | 17 |
| LSF | Liberalų sąjūdžio frakcija | 11 | 12 |
| LVŽ-KŠSF | Lietuvos valstiečių, žaliųjų ir Krikščioniškų šeimų sąjungos frakcija | 9 | 11 |
| MSNG | Mišri Seimo narių grupė | 8 | 9 |

## Sittings

| session | sittings | with stenogram link | agenda stages |
| --- | --- | --- | --- |
| 1 eilinė | 18 | 18 | 438 |
| 2 eilinė | 50 | 49 | 1317 |
| neeilinė | 2 | 2 | 7 |
| 3 eilinė | 45 | 45 | 1434 |
| 4 eilinė | 58 | 58 | 1825 |
| neeilinė | 2 | 2 | 9 |
| 5 eilinė | 10 | 4 | 98 |

## Votes

| vote ids | with results | without results | first | last |
| --- | --- | --- | --- | --- |
| 5391 | 3669 | 1722 | 2024-11-14 | 2026-09-15 |

## What each vote was about (votes with results)

| votes | % question | % motion | % stage link | % stage or line | % some subject | % bill | % sitting |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 3669 | 80.2 | 95.5 | 95.0 | 99.8 | 100.0 | 98.0 | 100.0 |

## Votes with results but no agenda link (newest 10)

| id | day | subject |
| --- | --- | --- |
| -55003 | 2025-03-20 | dėl pasiūlymo išbraukti iš darbotvarkės projektą Nr. XVP-166 |
| -55002 | 2025-03-20 | dėl pasiūlymo išbraukti iš darbotvarkės projektą Nr. XVP-196(2) |
| -55001 | 2025-03-20 | dėl pasiūlymo išbraukti iš darbotvarkės Rinkimų kodekso pakeitimo projektus (dar |
| -55000 | 2025-03-20 | dėl pasiūlymo įrašyti į darbotvarkę projektą Nr. XVP-217 |
| -54821 | 2025-01-14 | dėl pasiūlymo įrašyti į darbotvarkę projektus Nr. XVP-102 ir Nr. XVP-103 |
| -54820 | 2025-01-14 | dėl pasiūlymo išbraukti iš darbotvarkės projektą Nr. XVP-44(2) |
| -54819 | 2025-01-14 | dėl pasiūlymo įrašyti į darbotvarkę projektą Nr. XVP-92 |

## Bill number shapes

| shape | votes |
| --- | --- |
| XVP-N(N) | 1984 |
| XVP-N | 1313 |
| XIVP-N(N) | 246 |
| XIVP-N | 51 |

## Kinds of vote

| kind | votes | no result printed | no motion printed |
| --- | --- | --- | --- |
| standard | 3506 | 17 | 3 |
| alternative | 163 | 1 | 0 |

## Published results

| result | votes |
| --- | --- |
| pritarta | 2753 |
| nepritarta | 696 |
| pritarta formuluotei A | 88 |
| pritarta formuluotei B | 72 |
| sprendimas nepriimtas | 42 |
| (none) | 18 |

## Individual choices

| choice | rows | % | no fraction then |
| --- | --- | --- | --- |
| uz | 249178 | 48.2 | 1615 |
| nebalsavo | 177333 | 34.3 | 1955 |
| pries | 40851 | 7.9 | 280 |
| susilaike | 34873 | 6.7 | 171 |
| a | 7380 | 1.4 | 25 |
| b | 7093 | 1.4 | 25 |

## Record currency

| key | value |
| --- | --- |
| embedding_model | intfloat/multilingual-e5-large@1024 |
| loaded_at | 2026-09-16T23:56:31+03:00 |
| roster_generated_at | 2026-09-16 23:49:46 |
| scope | term 10 |
| votes_first_id | -54444 |
| votes_last_at | 2026-09-15T15:05:13+03:00 |
| votes_last_id | -60059 |

## Verification against the Seimas vote pages

Checked 2026-09-16T23:55:53+03:00. Every vote with individual results is compared row by row, by `asmens_id`, with the page the Seimas publishes for it.

| check | count |
| --- | --- |
| alternative votes (motion A against motion B) | 163 |
| page voted/members != XML, page lists a member twice | 348 |
| pages noting a mismatch with the protocol | 165 |
| row count differs between page and XML | 3 |
| rows compared | 516708 |
| rows: fraction disagrees | 179 |
| rows: listed under two fractions (fraction unknown) | 405 |
| sitting by time window != page sitting | 28 |
| sitting by time window == page sitting | 3641 |
| vote ids | 5391 |
| vote ids in the harvested range: no results | 225 |
| votes with individual results | 3669 |
| votes without individual results | 1722 |


### Fraction label, open-data XML -> vote page

| pattern | rows |
| --- | --- |
| (blank) -> LSF | 50 |
| (blank) -> DFVL | 46 |
| (blank) -> TS-LKDF | 44 |
| (blank) -> LVŽ-KŠSF | 26 |
| (blank) -> LSDPF | 13 |

## Corroboration by the stenograms

Checked 2026-09-14T02:30:53. A vote is corroborated when its sitting's stenogram announces exactly its tally (voted, for, against, abstain). `uv run python -m ingest.tallies` regenerates this.

| check | count |
| --- | --- |
| announcements that could not be read | 270 |
| announcements with no matching vote | 18 |
| corroborated by the stenogram | 3270 |
| not found in the stenogram | 377 |
| registrations announced | 227 |
| sittings | 176 |
| votes with results | 3647 |
| votes without individual results | 1702 |
