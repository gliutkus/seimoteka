# Transcript trimming

Generated 2026-09-13 23:44 by `uv run python -m ingest.transcripts`. Regenerate; do not edit. The rules and the evidence for each threshold are in `ingest/transcripts.py`.

563 videos, 14,254,260 characters. Removed **614,141 (4.3%)**, kept 13,640,119. 13 videos dropped whole. Character-model cutoff: -4.53 bits/char.

## Removed, by rule

| rule | segments | characters | % of corpus |
| --- | --- | --- | --- |
| duplicate | 5,169 | 407,877 | 2.86% |
| video: not_lithuanian | 2,979 | 117,027 | 0.82% |
| video: duplicate_video | 1,229 | 50,406 | 0.35% |
| whisper_low_confidence | 199 | 22,414 | 0.16% |
| whisper_repetition | 23 | 12,219 | 0.09% |
| repetition_loop | 496 | 4,198 | 0.03% |
| empty | 183 | 0 | 0.00% |

## Videos dropped whole

| video | channel | title | why |
| --- | --- | --- | --- |
| 0qCpzW10cFw | OpTV | Šimonytei - atpildo metas? Jankūnas ruošiasi apklausti aukas | 92% repeats 6Tb45A-_VxA |
| 3bAA0fak_G4 | OpTV | Širinskienė įspėjo dėl keisto Žemaitaičio elgesio | 91% repeats cxVSyjPeqh8 |
| 6TCQSLcBaKI | OpTV | Skvernelis praregėjo savo kailiu: kodėl komanda virto prisiplakėliais? | 93% repeats 9HN9XEinE7k |
| 8mfqsrJUnt8 | OpTV | Vėgėlė: reikia būti naiviam, kad visa tai laikytum sutapimu | 98% repeats 15Er31uf3VU |
| AV7NjBmreNA | Radio WNET | Žygimantas Pavilionis: Poles, why do you stand with atrocious communis | 24% of text is not Lithuanian |
| DSP1w25iS64 | OpTV | Žemaitaitis nenori būti grubus, bet nesiseka | 84% repeats 8JwZjltaWR4 |
| IKad8rndQys | Ukraine Media Center | (English) Andrius Kubilius, Audronius Ažubalis. Ukraine crisis media c | 41% of text is not Lithuanian |
| IrVR_B7F8lI | “Euroua Info” media group | Linas Balsys - about the threats of Astravyets Nuclear Power Plant in  | 30% of text is not Lithuanian |
| PF9z0bkn0G0 | OpTV | Žemaitaitis: skandalingosios "Riaušių anatomijos" autorės sūnui - rūst | 86% repeats 62r40A5cXgY |
| S-7pWmLFWXw | Zonta International | Remarkable Women, Powerful Stories: Jurgita Šukevičienė | 37% of text is not Lithuanian |
| eyJI_1BFctU | Leonas Glinskis | Žygimantas Pavilionis Kaišiadoryse | 24% of text is not Lithuanian |
| lBbW9OLRlFI | TVP Wilno | Nowe spojrzenie \| Prof. Vilija Targamadzė: Język polski jako egzamin p | 74% of text is not Lithuanian |
| mFR4ywDkqfY | Школа журналістики та комунікацій УКУ | Лінас Бальсис (Linas Balsys) :: Інтерв'ю :: Розмовляє Марічка Паплауск | 29% of text is not Lithuanian |

## Kept videos losing the most text

| video | channel | title | removed |
| --- | --- | --- | --- |
| CSWW_n2DTUs | OpTV | Skandalinga! Prieš Žemaitaitį mobilizuoti nepilnamečiai: tėvai skandal | 79% |
| MSKGuuZtmo4 | OpTV | Žemaitaitis vėl tėškė prognozę: Briuseliui ateina galas? #davos2026 | 78% |
| -H6RSOpM3UQ | OpTV | Žemaitaitis išklojo, ką iš tikrųjų mano slidžiu klausimu | 76% |
| 4YqfYG1wFns | OpTV | Janutienė atkapstė! Žemaitaitį apmulkinęs Karolis Žukauskas turi ką sl | 75% |
| L1E52GvbSZk | OpTV | Andrius Tapinas įsivėlė - Žemaitaitis vožė atgal #andriustapinas #remi | 75% |
| EYmJlV7HOuQ | OpTV | Žemaitaitis susirūpino: ar tikrai taip blogai, kaip pasakoja? | 73% |
| E30iOTGNKR0 | OpTV | Žemaitaitis kirto pagrindiniam valdžios propaganDONUI: žinojo apie gir | 71% |
| 5oZ8t6iZs3Q | OpTV | Žemaitaičio neužkimši: pasakė tiesiai šviesiai, kas bus su Ukraina | 70% |
| Ny8IPb6_9Z0 | OpTV | Žemaitaitis: 5 proc. buvo klaida! | 69% |
| LM_4AeFuZr8 | OpTV | Žemaitaitis atkirto dėl korupcijos Ukrainoje: maža nepasirodys | 68% |

## Samples of removed segments

### whisper_low_confidence

| where | text |
| --- | --- |
| -YVYVkMLtgs @3057s | You've been sitting on my chest |
| -YVYVkMLtgs @3063s | I went down of this mess |
| -YVYVkMLtgs @3068s | You've been drinking on my stress |

### duplicate

| where | text |
| --- | --- |
| -4jea7zFykU @60s, also in M6qfXqZviwI | tai bus blogai, yra griežtai pasakyta, netgi yra pasakyta atsiimti projektus. Tie, kurie pateikėte, atsiimėt p |
| -4jea7zFykU @72s, also in M6qfXqZviwI | Bet jūs man pasakykit, su eina darbo metu valdiško muziejaus direktorius suhebra ir nurodo visiems nedalyvauti |
| -4jea7zFykU @145s, also in M6qfXqZviwI | Ir ačiū viso geriausiai. Jeigu tu nori užsimti visuomeninę veiklą, tai tu pildai nemokamo tosų kutįšinį. |

### repetition_loop

| where | text |
| --- | --- |
| 82wawsHsjpw @8099s | Tai dabar visą laiką teisė. |
| 82wawsHsjpw @8100s | Tai dabar visą laiką teisė. |
| CeSP0eCcRas @3698s | Ačiū, kisimėsiu, Remigijai. |

### whisper_repetition

| where | text |
| --- | --- |
| -YVYVkMLtgs @3504s | Oh, oh, oh, oh, oh, oh, oh, oh, oh, oh, oh, oh, oh, oh, oh, oh, oh, oh, oh, oh, oh, oh, oh, oh, oh, oh, oh, oh |
| 2Th_RzaAiNI @3933s | Lipsyta savo pasakrasis su pasakrasis su pasakrasis su pasakrasis su pasakrasis su pasakrasis su pasakrasis su |
| 2ehDeErmkAk @481s | Ir dal别 problemas kapур nelimas electronics ExpressCLSS nakėlia 안� reinvent Occ перевскихrintųųųųųųųųųųųųųųųųų |
