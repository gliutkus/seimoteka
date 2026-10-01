# Retrieval check

Generated 2026-09-14 19:17 by `uv run python -m evaluation.retrieval`. Regenerate; do not edit.

10 questions written from a passage, each searched with the filters a correct tool call would pass; top 10 hits. The questions as written share their passage's words; the paraphrases avoid them. In the `+ rewrites` modes the chat model also rewrites each question into the record's words, and the rankings of every phrasing are fused.

| questions | mode | hit@1 | hit@5 | MRR |
| --- | --- | --- | --- | --- |
| as written | bm25 | 0.80 | 1.00 | 0.90 |
| as written | dense | 0.70 | 0.80 | 0.72 |
| as written | hybrid | 0.70 | 0.90 | 0.77 |
| as written | bm25 + rewrites | 0.70 | 1.00 | 0.85 |
| as written | hybrid + rewrites | 0.60 | 0.90 | 0.73 |
| paraphrased | bm25 | 0.60 | 0.60 | 0.63 |
| paraphrased | dense | 0.60 | 0.60 | 0.63 |
| paraphrased | hybrid | 0.60 | 0.60 | 0.62 |
| paraphrased | bm25 + rewrites | 0.50 | 0.90 | 0.67 |
| paraphrased | hybrid + rewrites | 0.60 | 0.60 | 0.66 |

| question | as written bm25 | as written dense | as written hybrid | as written bm25 + rewrites | as written hybrid + rewrites | paraphrased bm25 | paraphrased dense | paraphrased hybrid | paraphrased bm25 + rewrites | paraphrased hybrid + rewrites |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| programme-pension-pillar | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 2 | 1 |
| programme-belarus | 1 | 1 | 1 | 2 | 2 | - | - | - | 3 | 10 |
| programme-security | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 |
| stenogram-question-to-pm-candidate | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 |
| stenogram-excise-arguments | 2 | 4 | 2 | 2 | 2 | - | 7 | 10 | 9 | 7 |
| stenogram-committee-report | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 |
| stenogram-lrt-director-criteria | 1 | - | - | 1 | 10 | 7 | 9 | 7 | 2 | 7 |
| stenogram-firefighters-question | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 |
| finding-ethics-committee-meeting | 2 | - | 5 | 2 | 5 | 7 | - | - | 4 | 6 |
| finding-vtek-appeal-status | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 |

## Rewrites

| searched | rewritten as |
| --- | --- |
| Ką Lietuvos socialdemokratų partija 2024 m. rinkimų programoje žadėjo dėl antrosios pensijų pakopos? | Lietuvos socialdemokratų partijos 2024 m. Seimo rinkimų programos nuostatos dėl pensijų kaupimo II pakopos · Lietuvos socialdemokratų partijos 2024 m. Seimo rinkimų programos įsipareigojimai dėl papildomo savanoriško pensijų kaupimo · Lietuvos socialdemokratų partijos 2024 m. Seimo rinkimų programos pozicija dėl pensijų sistemos antrosios pakopos reformos |
| Ką apie santykius su Baltarusija 2024 m. rinkimų programoje rašė Lietuvos lenkų rinkimų akcija-Krikščioniškų šeimų sąjunga? | Lietuvos lenkų rinkimų akcija-Krikščioniškų šeimų sąjunga 2024 m. Seimo rinkimų programos nuostatos dėl santykių su Baltarusijos Respublika · Lietuvos lenkų rinkimų akcija-Krikščioniškų šeimų sąjunga 2024 m. Seimo rinkimų programoje: pozicija Baltarusijos atžvilgiu · Lietuvos lenkų rinkimų akcija-Krikščioniškų šeimų sąjunga 2024 m. rinkimų programa ir Lietuvos Respublikos bei Baltarusijos Respublikos dvišaliai santykiai |
| Ką apie saugumą 2024 m. rinkimų programoje rašė Lietuvos žaliųjų partija? | Lietuvos žaliųjų partijos 2024 metų rinkimų programos nuostatos dėl nacionalinio saugumo stiprinimo ir gynybos politikos plėtojimo · Lietuvos žaliųjų partijos 2024 metų rinkimų programos pozicija dėl krašto apsaugos ir visuomenės saugumo užtikrinimo priemonių · Lietuvos žaliųjų partijos 2024 metų rinkimų programos įsipareigojimai dėl Lietuvos Respublikos saugumo architektūros tobulinimo ir tarptautinio bendradarbiavimo gynybos srityje |
| Ko Aistė Gedvilienė klausė kandidato į Ministrus Pirmininkus per 2024-11-19 rytinį posėdį? | Aistės Gedvilienės klausimai kandidatui į Ministro Pirmininko pareigas 2024 m. lapkričio 19 d. rytiniame posėdyje · Klausimai, kuriuos Aistė Gedvilienė pateikė kandidatui į Ministro Pirmininko pareigas 2024 m. lapkričio 19 d. rytinio posėdžio metu · Aistės Gedvilienės užduoti klausimai kandidatui į Ministro Pirmininko pareigas 2024 m. lapkričio 19 d. Seimo posėdyje |
| Kokius argumentus prieš Akcizų įstatymo pakeitimo projektą Nr. XVP-855(2) 2025-12-09 pateikė Simonas Gentvilas? | Simono Gentvilo argumentai dėl Akcizų įstatymo Nr. XVP-855(2) pakeitimo projekto, pateikti 2025 m. gruodžio 9 d. Seimo posėdyje · Diskusijos dėl Akcizų įstatymo Nr. XVP-855(2) pakeitimo projekto 2025 m. gruodžio 9 d. Seimo posėdyje: Simono Gentvilo pasisakymai · Seimo nario Simono Gentvilo pozicija dėl Akcizų įstatymo Nr. XVP-855(2) pakeitimo projekto, išsakyta 2025 m. gruodžio 9 d. |
| Ką Daiva Žebelienė 2024-12-19 pranešė apie Valstybės valdymo ir savivaldybių komiteto sprendimą dėl projekto Nr. XVP-44(2)? | Daiva Žebelienė 2024-12-19 pranešimas Valstybės valdymo ir savivaldybių komiteto sprendimas projektas Nr. XVP-44(2) · Daiva Žebelienė 2024-12-19 kalba Valstybės valdymo ir savivaldybių komiteto nutarimas dėl įstatymo projekto Nr. XVP-44(2) · Daiva Žebelienė 2024-12-19 pasisakymas Valstybės valdymo ir savivaldybių komiteto išvada dėl teisės akto projekto Nr. XVP-44(2) |
| Ką Mindaugas Lingė 2025-12-16 kalbėjo apie LRT generalinio direktoriaus atrankos kriterijus? | Mindaugo Lingės pasisakymas dėl Lietuvos nacionalinio radijo ir televizijos generalinio direktoriaus atrankos kriterijų 2025 m. gruodžio 16 d. · Mindaugas Lingė: LRT generalinio direktoriaus atrankos kriterijai (2025-12-16) |
| Ko Valius Ąžuolas klausė pristatant Priešgaisrinės saugos įstatymo pakeitimo projektą Nr. XVP-1039 2026-06-02? | Klausimai, pateikti Seimo nariui Valiui Ąžuolui, pristatant Priešgaisrinės saugos įstatymo pakeitimo projektą Nr. XVP-1039, 2026 m. birželio 2 d. · Diskusijos dėl Priešgaisrinės saugos įstatymo pakeitimo projekto Nr. XVP-1039, kuriose dalyvavo Seimo narys Valius Ąžuolas, 2026 m. birželio 2 d. · Seimo nario Valiaus Ąžuolo pasisakymai ir klausimai, susiję su Priešgaisrinės saugos įstatymo pakeitimo projektu Nr. XVP-1039, 2026 m. birželio 2 d. |
| Dėl ko Seimo Etikos ir procedūrų komisija 2026-02-11 priėmė išvadą Nr. 101-I-8? | Seimo Etikos ir procedūrų komisijos 2026-02-11 išvada Nr. 101-I-8 · Seimo Etikos ir procedūrų komisijos išvados Nr. 101-I-8 priėmimo aplinkybės · Išvados Nr. 101-I-8, priimtos Seimo Etikos ir procedūrų komisijos 2026-02-11, turinys |
| Dėl ko priimtas VTEK sprendimas Nr. KS-2026/39 ir ar jis apskųstas teismui? | VTEK sprendimas Nr. KS-2026/39 · VTEK sprendimo Nr. KS-2026/39 apskundimas teismui |
| Ar socialdemokratai rinkimuose siūlė leisti žmonėms pasiimti pinigus iš pensijų fondų? | Lietuvos socialdemokratų partijos 2024 metų Seimo rinkimų programos nuostatos dėl pensijų fondų lėšų išėmimo galimybės · Pensijų anuitetų išmokėjimo tvarkos pakeitimai Lietuvos socialdemokratų partijos 2024 metų Seimo rinkimų programoje · Lietuvos socialdemokratų partijos rinkimų programos pasiūlymai dėl pensijų kaupimo sistemos reformos ir lėšų prieinamumo |
| Ar kuri nors partija žadėjo atkurti draugiškus santykius su baltarusiais? | Ar kuri nors politinė partija savo 2024 m. Seimo rinkimų programoje numatė atkurti diplomatinius santykius su Baltarusijos Respublika? · Ar politinės partijos 2024 m. Seimo rinkimų programose yra nuostatų dėl santykių su Baltarusijos Respublika atkūrimo? · Kokių priemonių, susijusių su santykių su Baltarusijos Respublika atkūrimu, yra numatyta politinių partijų 2024 m. Seimo rinkimų programose? |
| Ką žalieji siūlė dėl šalies apsaugos nuo pasaulinių pavojų? | Lietuvos Respublikos apsaugos stiprinimas nuo globaliųjų grėsmių Lietuvos valstiečių ir žaliųjų sąjungos (LVŽS) 2024 m. Seimo rinkimų programoje · Nacionalinio saugumo užtikrinimas ir atsparumo didinimas pasauliniams iššūkiams Lietuvos valstiečių ir žaliųjų sąjungos (LVŽS) 2024 m. Seimo rinkimų programoje · Lietuvos Respublikos pasirengimas ir atsakas į globalias krizes Lietuvos valstiečių ir žaliųjų sąjungos (LVŽS) 2024 m. Seimo rinkimų programoje |
| Ką Gedvilienė priekaištavo naujosios valdžios formuotojui dėl pasirinktų sąjungininkų? | Seimo narės Gedvilienės priekaištai dėl koalicijos partnerių pasirinkimo · Seimo narės Gedvilienės pasisakymai dėl valdančiosios koalicijos sudėties · Seimo narės Gedvilienės kritika formuojant valdančiąją daugumą |
| Kodėl Gentvilas nepritarė degalų mokesčio lengvatai? | Gentvilo pasisakymas dėl degalų mokesčio lengvatos nepritarimo · Gentvilo pozicija dėl degalų akcizo lengvatos nesuteikimo · Diskusijos dėl degalų mokesčio lengvatos ir Gentvilo nuomonė |
| Kaip Žebelienė apibūdino komiteto nuomonę apie valdininkų darbo pataisas? | Kaip Seimo narė Žebelienė apibūdino komiteto nuomonę dėl valstybės tarnautojų darbo santykių reguliavimo įstatymo pataisų? · Seimo narės Žebelienės pasisakymas dėl komiteto pozicijos valstybės tarnybos įstatymo pakeitimų klausimu · Žebelienės nuomonė apie komiteto išvadas dėl valstybės tarnybos reguliavimo tobulinimo |
| Už ką visuomeninio transliuotojo vadovo kandidatams Lingė siūlė pridėti taškų? | Visuomeninio transliuotojo vadovo kandidatų vertinimas Lingė · Lingės siūlymai dėl visuomeninio transliuotojo vadovo kandidatų atrankos · Lingės pasisakymai dėl papildomų balų skyrimo visuomeninio transliuotojo vadovo kandidatams |
| Ar Ąžuolas klausė, ar skirtingoms gaisrinėms tarnyboms bus taikomi tie patys reikalavimai? | Ar Seimo narys Ąžuolas Bučys teiravosi, ar priešgaisrinės apsaugos ir gelbėjimo tarnyboms bus nustatyti vienodi reikalavimai? · Ar Seimo narys Ąžuolas Bučys kėlė klausimą dėl vienodų reikalavimų taikymo skirtingoms priešgaisrinės apsaugos ir gelbėjimo tarnyboms? |
| Kas kreipėsi į etikos sargus dėl to, kaip buvo nagrinėjami jo siūlymai? | Asmuo, pateikęs kreipimąsi į Seimo Etikos ir procedūrų komisiją dėl jo siūlymų nagrinėjimo tvarkos. · Kreipimasis į Seimo Etikos ir procedūrų komisiją dėl siūlymų nagrinėjimo procedūros. |
| Ar Žemaitaitis ginčijo teisme tarnybinės etikos komisijos nutarimą? | Seimo nario Remigijaus Žemaitaičio skundas teismui dėl Vyriausiosios tarnybinės etikos komisijos nutarimo · Remigijaus Žemaitaičio ginčijimas teisme Vyriausiosios tarnybinės etikos komisijos nutarimo · Byla teisme dėl Vyriausiosios tarnybinės etikos komisijos nutarimo, susijusio su Seimo nariu Remigijumi Žemaitaičiu |
