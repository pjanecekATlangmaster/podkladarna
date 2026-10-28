# CLAUDE.md — Podkladárna

## Co je tento projekt

Podkladárna je webová služba pro generování orientačních podkladů z ČÚZK LiDAR (DMR 5G + DMP OK) a ZABAGED. Výstupem je ZIP pro OpenOrienteering Mapper (`podkladarna_output.zip`).

- Backend: Python / FastAPI, vstupní bod `app.main:app`
- Frontend: statické soubory ve `web/`
- Geoprocessing: PDAL, GDAL, vlastní pipeline pro vegetaci, srázy a DEM
- Kontejnerizace: Docker / Docker Compose
- Testy: pytest v `tests/`, end-to-end smoke test `scripts/smoke_e2e.py`
- CI: GitHub Actions — `test.yml` (testy), `docker.yml` (build a push image do GHCR)

## Načítání kontextu

- Při navazování zjisti `git status` a rozsah změn; rozliš vlastní změny od cizí rozpracované práce.
- Začni konkrétním zadáním a cíleným hledáním symbolů (Grep), čti jen potřebné části souborů.
- `DEV.md`, `DEPLOY.md`, `TODO.md` čti jen pro konkrétní otázku, ne preventivně. Již načtený obsah nečti znovu.
- Nečti celé datové soubory, rozsáhlé logy ani celý repozitář; použij souhrn, výběr nebo skript.
- Úspora kontextu neruší bezpečnostní a doménová pravidla; chybějící důkazy cíleně doplň.

## Pravidla pro agenta

- Komunikuj česky se správnou diakritikou; commit zprávy také česky (prefix `docs:`, `fix:`, `feat:` apod.).
- Před úpravou si přečti relevantní soubory a krátce řekni plán; u drobnosti stačí jedna věta.
- Zachovávej stávající strukturu projektu, styl kódu a pojmenování. Dělej malé, cílené diffy.
- Nedělej rozsáhlý refaktoring bez výslovného souhlasu. Nesouvisející problémy neopravuj, jen je stručně zmiň, jsou-li podstatné.
- Nikdy necommituj tajemství, přihlašovací údaje ani lokální data.
- Před commitem spusť relevantní testy (viz „Vývoj a testy"); celou sadu před PR.
- Při změnách kódu a opravách chyb zvyš `APP_VERSION` v `app/settings.py` jednou za dokončenou logickou změnu (viz „Verze a Novinky"); u dokumentace ne.

### Jen se souhlasem Petra

- Spouštět `./deploy-nas.sh` nebo `./update-nas.sh` na ostré NAS; do produkce na NAS nesahat vůbec.
- Mazat data, joby, cache nebo Docker image.
- Měnit DNS, router, Synology reverse proxy, certifikáty nebo firewall.
- Měnit produkční `.env`, SMTP údaje nebo secrets (ani `.env` lokálně nepřepisovat).
- Měnit Dockerfile, `docker-compose*.yml`, deploy skripty ani GitHub Actions bez vysvětlení dopadu.
- Publikovat novou Docker image bez úspěšných testů.
- Měnit schéma SQLite databáze nebo formát uložených jobů bez migračního plánu.

## Dělba práce (agenti)

Agenti jsou v `.claude/agents/` (každý má `maxTurns` a nízký/střední `effort`). Pouhá existence agenta není důvod ho spustit. Výchozí tok: **jeden implementátor → cílené testy → stručné předání → commit**.

### Hlavní agent

- Zvol jednu cestu: úkol vyřeš sám, nebo ho deleguj jednomu implementátorovi. Neimplementuj paralelně totéž, co jsi delegoval.
- Před delegováním načti jen kontext nutný pro přesné zadání; nedělej analýzu, kterou subagent zopakuje.
- Výsledek ověř přiměřeně riziku (shrnutí, rozsah diffu, doložené testy), neopakuj automaticky celý postup subagenta.
- Verzi (`APP_VERSION`) zvyšuje jednou za logickou změnu; subagent jen na výslovné zadání.

### Role

- `junior-dev` (Sonnet) — výchozí implementátor: opravy, menší funkce, testy, UI. Testuje sám, takže `test-runner` k němu nepatří.
- `test-runner` (Haiku) — jen když testy neběžely u implementátora, nebo na samostatný testovací úkol.
- `opus-advisor` — jedna konkrétní návrhová nejistota (pipeline, API, formát jobů, geometrie), ne automatický plánovač.
- `opus-expert` — pouze DIAGNÓZA prokázaného algoritmického problému (nemá Edit/Write); opravu provede `junior-dev`.
- `opus-reviewer` — rizikové změny (pipeline, API, formát jobů, ZIP/cesty, souběh) nebo výslovně vyžádané druhé oči. Drobnosti a dokumentace bez review.

### Rozhodovací pravidla a rozpočet

- Drobnost, dokumentace, běžné UI, lokální oprava: bez Opusu.
- Nejasný algoritmus: krátká konzultace NEBO expertova diagnóza → implementátor; ne obojí automaticky.
- Za úkol nejvýše jedno volání Opus role, jen při prokázané nejistotě. Poradce/expert + následný reviewer v jednom úkolu jen s výslovným souhlasem Petra (nebo je-li předem schválen v zadání). Nutné review se nesmí tiše přeskočit; bez něj změnu neoznačuj za hotovou.
- Zadání Opus roli obsahuje konkrétní soubory/funkce, reprodukci a otázku; agent nezjišťuje kontext plošným čtením.
- Po dvou neúspěšných opravných iteracích stejného problému zastav a předej zjištění, hypotézy a překážku; další pokus jen s novým schváleným postupem.
- Subagenti nespouštějí další subagenty a necommitují, pokud to zadání výslovně nevyžaduje.
- Souběžní writeři nesmějí měnit stejné soubory.

### Předání

- Zadání obsahuje cíl, relevantní cesty, omezení, dosavadní zjištění a podmínku dokončení; ne celou historii ani kopie velkých souborů.
- Výstup subagenta do ~10 bodů: co se změnilo, výsledek testů, rizika. Rozsáhlé výstupy do souboru, vracej souhrn.
- Úsporné pokyny nejsou důvod zamlčet zásadní nález.

## Struktura repozitáře

- `app/` — FastAPI backend, business logika, pipeline
- `web/` — frontend, statické soubory a UI
- `configs/` — konfigurace aplikace
- `scripts/` — vývojové, testovací a pomocné skripty
- `tests/` — pytest testy
- `testdata/` — malá testovací data pro lokální e2e test
- `deploy/` — konfigurace pro Synology / nginx
- `Dockerfile` — produkční image včetně PDAL, GDAL a Mapper CLI
- `docker-compose.yml` — obecný compose
- `docker-compose.dev.yml` — lokální vývojový stack
- `docker-compose.nas.yml` — produkční compose pro Synology NAS
- `deploy-nas.sh` — první deploy / šetrný restart na NAS
- `update-nas.sh` — aktualizace image z GHCR na NAS
- `nas-lib.sh` — sdílené funkce pro NAS deploy
- `.github/workflows/` — GitHub Actions (`test.yml`, `docker.yml`)
- `DEV.md` — podrobný vývojový workflow; sekce „Ladění na testovací verzi B a dohledání DTM objektu podle ID“ (pt.kibos.link, `data-test\jobs`, cache a stahování DTM balíků)
- `DEPLOY.md` — podrobný postup nasazení a provozu na NAS

## Vývoj a testy

Předpoklady: Windows + PowerShell, nainstalovaný QGIS v `C:\QGIS` (kvůli `proj.db`, viz níže).

### Testování — cíleně, celá sada až před PR

Z kořene repozitáře (závislosti: `python -m pip install -r requirements.txt -r requirements-dev.txt`, instaluj jen na žádost):

```powershell
python -m pytest tests/test_x.py -k "název" --tb=short -q   # při vývoji
python -m pytest tests/ --tb=short -q                       # celá sada: před PR / na zadání
```

- Začni nejmenším relevantním testem nebo výběrem `-k`; celý soubor, pokud odpovídá rozsahu změny. Soubory jsou pojmenované po modulech (`test_vegetation_*`, `test_cliff*`, `test_oom_*`, `test_api.py`, `test_worker.py` …).
- Jeden vlastník běhu testů; ostatní přebírají příkaz a výsledek. Testy opakuj jen po změně relevantního kódu nebo při konkrétní pochybnosti.
- Reportuj souhrn a relevantní traceback, ne celé logy. Při chybě prostředí uveď příčinu, neopakuj stejný příkaz beze změny podmínek.
- E2E a Docker (`dev.ps1 up`, `dev.ps1 e2e`) jen u změn pipeline, Dockerfile nebo závislostí.
- Sada nemá bobtnat: jedno pravidlo kresby = jeden test. Nový případ téhož pravidla přidej jako řádek do existující tabulky (`pytest.mark.parametrize`), ne jako novou funkci. Když Petr pravidlo změní nebo zruší, test uprav nebo smaž, nepřidávej další vedle.
- Scénářové testy kresby ověřují vlastnosti (nakresleno / vynecháno, symbol, napojení, přibližná plocha), ne přesné souřadnice, které se mění s laděním.

### Pomocné příkazy

```powershell
.\scripts\dev.ps1 test         # pytest
.\scripts\dev.ps1 run          # uvicorn bez reload, vhodné pro smoke / dlouhé joby
.\scripts\dev.ps1 run-reload   # uvicorn s reload, jen krátký UI vývoj bez běžícího jobu
.\scripts\dev.ps1 up           # docker compose dev
.\scripts\dev.ps1 smoke        # fake upload přes API
.\scripts\dev.ps1 e2e-upload   # upload testdat
.\scripts\dev.ps1 e2e          # celá pipeline s testdaty
.\scripts\dev.ps1 all          # pytest + docker + e2e
```

### Lokální Docker

```powershell
docker compose -f docker-compose.dev.yml up --build
```

Lokální adresa: `http://localhost:8672`

Při e2e testu v Dockeru neukládej změny kódu uprostřed běžícího jobu, protože dev compose používá automatický reload.

### Důležité prostředí PROJ / GDAL

Pro QGIS nástroje a EPSG:5514 je nutné použít QGIS `proj.db`, nikoli starší `proj.db` z pip pyproj. Skript `scripts/dev.ps1` i `app.tool_env.apply_local_gis_env()` to nastavují automaticky.

Pokud se nástroje spouštějí mimo skript:

```powershell
$env:PROJ_DATA = 'C:\QGIS\share\proj'
$env:PROJ_LIB = $env:PROJ_DATA
$env:PATH = $env:PATH + ';C:\QGIS\bin'
```

### Diagnostika

```powershell
# pytest podrobněji
python -m pytest tests/ -v --tb=short

# lokální Docker log
docker compose -f docker-compose.dev.yml logs -f

# log jobu přes API
curl http://127.0.0.1:8672/api/jobs/<id>/log
```

```bash
# log na NAS
docker compose -f docker-compose.nas.yml logs --tail=100
```

## Verze a Novinky

- Verze aplikace je `APP_VERSION` v `app/settings.py` (formát `MAJOR.MINOR.PATCH`).
- Při každé změně kódu včetně oprav chyb zvyš verzi (patch, např. `2.8.1` → `2.8.2`). Čistě dokumentační úpravy (CLAUDE.md, DEV.md, DEPLOY.md apod.) verzi nezvyšují.
- U velké změny zvyš minor o jedna a patch vynuluj (`2.8.2` → `2.9.0`). Zda jde o velkou změnu, posuzuje agent odhadem (nová funkce, změna výstupu pipeline, výrazné zrychlení apod.).
- Box „Co je nového" na webu: `configs/whats_new.yaml` je generovaný CI z git logu — neupravuj ho ručně. Ruční milníky jsou v `configs/whats_new_milestones.yaml`; zapisuj sem jen významné změny z posledního období, ne drobnosti.

## Ostré prostředí

- Veřejná adresa: `https://podkladarna.kibos.link`
- Hosting: Synology NAS
- Provoz: Internet/Cloudflare → router → Synology DSM reverse proxy → `127.0.0.1:8672` → Docker kontejner
- Interní port kontejneru: `8672`
- Docker image: `ghcr.io/pjanecekatlangmaster/podkladarna:latest`
- NAS složka: `/volume1/docker/podkladarna/`
- Perzistentní data: `./data` na NAS, uvnitř kontejneru `/data`

## Release proces

Agent dělá kroky 1–7, nasazení na NAS (od kroku 8) dělá Petr.

1. Vytvoř feature větev z aktuálního `master`.
2. Proveď změny, zvyš `APP_VERSION` (kromě čistě dokumentačních změn) a lokálně spusť `python -m pytest tests/ -v`.
3. Pro změny pipeline, Dockerfile nebo závislostí navíc `.\scripts\dev.ps1 up` a `.\scripts\dev.ps1 e2e`. U změn s vizuálním dopadem na mapu nabídni Petrovi lokální otočku s `.omap` (viz „Ověření novinky").
4. Commitni změny a pushni větev do GitHubu.
5. Vytvoř Pull Request do `master`.
6. Po merge do `master` workflow `docker.yml` sestaví a publikuje `ghcr.io/pjanecekatlangmaster/podkladarna:latest`.
7. Zkontroluj, že Actions workflow je zelené.
8. (Petr) Na NAS spusť aktualizaci:

```bash
cd /volume1/docker/podkladarna
sudo ./update-nas.sh
```

9. (Petr) Ověř funkčnost, očekávaný výsledek je `200`:

```bash
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8672/
```

## Kde se co spouští (Docker a NAS)

- **Vývojový stroj (Windows):** tady je repozitář. `dev.ps1 up` / `dev.ps1 e2e` staví image z lokálního pracovního stromu (`docker-compose.dev.yml`) a zkouší tedy kód z aktuální větve. Je to volitelná kontrola před mergem; nutná je jen u změn pipeline, Dockerfile nebo závislostí (viz „Release proces").
- **NAS:** repozitář se tam nestahuje. Běží jen hotový image `ghcr.io/pjanecekatlangmaster/podkladarna:latest` sestavený workflow `docker.yml` po mergu do `master` (kopíruje se `configs/`, `app/`, `web/`). Na NAS se proto nikdy nezkouší kód z větve ani se tam nespouští `dev.ps1`; po `update-nas.sh` se jen ověřuje nasazený image (health `200`, testovací job, log jobu, stažený ZIP).
- Agent proto při předávání pokynů „co pustit" vždy rozlišuje: před mergem = vývojový stroj, po mergu = NAS (jen `update-nas.sh` + ruční ověření podle `DEPLOY.md`).

## Ověření novinky: lokální otočka, nebo až po nasazení

U změn, jejichž výsledek se pozná jen na mapě (pipeline, symboly, geometrie, vegetace, srázy, DEM), se Petra před mergem zeptej, zda chce:

- **lokální otočku** — agent na vývojovém stroji z testdat (případně z dat zadaných Petrem) vygeneruje `.omap` / ZIP, uloží ho do session složky mimo git a předá cestu; Petr ho otevře v Mapperu a zkontroluje. Může ušetřit hodně času, než se chyba dostane do produkce;
- **ověření až po nasazení** — Petr změnu zkontroluje na ostrém podle `DEPLOY.md`.

Neptej se u drobností, dokumentace a změn bez vizuálního dopadu. Lokální otočka je výchozí doporučení u větších změn výstupu; spouštěj ji až po souhlasu, protože pipeline může trvat dlouho (e2e a Docker viz „Vývoj a testy").

## Nasazení na NAS (dělá Petr)

`update-nas.sh` ve výchozím režimu:

- synchronizuje `docker-compose.nas.yml` z GitHubu,
- čeká na dokončení GitHub Actions workflow,
- porovnává digest lokálního image a GHCR,
- při novém digestu zastaví kontejner, stáhne nový image a spustí jej,
- nepřepisuje host `.env`.

Užitečné varianty:

```bash
./update-nas.sh --no-wait       # nečekat na GitHub Actions
./update-nas.sh --force         # vynutit pull / recreate
./update-nas.sh v1.0.0          # nasadit konkrétní tag
./update-nas.sh --keep-image    # ponechat starý image pro rollback
```

Ruční postup:

```bash
cd /volume1/docker/podkladarna
docker compose -f docker-compose.nas.yml pull
docker compose -f docker-compose.nas.yml down
docker compose -f docker-compose.nas.yml up -d
```

Ruční test po deployi (job s testovacími daty, preset např. Sprint, stažení ZIP a otevření v OpenOrienteering Mapperu, u SMTP e-mail s odkazem) je popsán v `DEPLOY.md`.

## Důležité poznatky (DTM, MTBO, test B)

- **Testovací B verze** (`https://pt.kibos.link`, data `\\192.168.10.11\docker\podkladarna\data-test\jobs\`) se aktualizuje z `master`
  (`:latest` → `update-nas-ab.sh test`, cron jen když neběží job). „Push do testu“ = commit + push do `master`. Jak dohledat job, bbox a DTM balíky: `DEV.md`.
- **Zpevněný povrch v lese a MTBO je jedna plocha** (2.37.0, `app/pipeline/paved_surface.py`, `UNIFIED_PAVED`; návrh a diagnóza
  Barrandov 2026-10-26): koridor silnice, velká parkoviště a (MTBO) široké chodníky z DTM, OSM plochy a ZABAGED `OstatniPlochaVSidlech`
  i `ParkovisteOdpocivka` jdou jako prvky (kind `paved_dtm` / `parking` / `pedestrian_area`) do `build_paved_network`; výplň 501.1 i okraj
  (les 501.2, MTBO 501.100 – 501.2 je v ISMTBOM pozůstatek) vznikají z téže cache (`paved_network_feats`). Okraj kreslí až
  `build_paved_edge_parts` v `package_oom` nad hotovými čarami (`path_clearance.drawn_path_bands`): vynechá se v ústí = půlka výplně
  symbolu silnice (`symbol_fill_width_mm`, okraj končí na okrajové lince 502.x) a tam, kde čára vede souběžně s okrajem blíž než
  `LINE_MIN_GAP_MM`; u souběžné silnice se výplň dotáhne k vnější hraně její okrajové linky (`road_fill_strips`, 2.39.6), kus okraje
  pod 0,8 mm mezi ústími se nekreslí (`EDGE_PIECE_MIN_MM`). Plocha silnic z DTM se kreslí jen u **složitých křižovatek** (2.38.0, `app/pipeline/junction_areas.py`: shluk uzlů
  OSM silnic od třídy residential do 30 m s ostrůvkem ≥ 20 m² v ploše silnic z DTM, kruhovým objezdem nebo ≥ 4 jednosměrnými pásy od
  tertiary; Petr: jednoduché křižovatky čarami, napojení dělá OOMapper); jinde je silnice čára podle šířky. Od 2.45.0 (Petr 2026-10-28,
  ošklivé přechody plochy na čáru kolem objezdu na Lamačově) se ani ty nekreslí: `junction_areas.ROAD_AREAS = False` = les a MTBO
  kreslí silnice jen čarou (`corridor_geometry_for` vrací None); `True` vrátí plochy křižovatek. **Rozdělená vozovka**
  (`app/pipeline/dual_carriageway.py`): dva protisměrné jednosměrné pásy téže silnice s osami do 1,31 mm mapy souvisle ≥ 100 m kreslí
  jeden symbol 502.0 po střední ose (příprava OSM ukládá `osm_paths/oneway_roads.geojson`; starší joby bez něj kreslí pásy zvlášť).
  Kusy čar téhož symbolu se spojují i přes křižovatky (`_join_through_junctions`, nejpřímější pokračování do 45°). Metriky otoček
  (vynechané úseky okraje, dvojitý okraj, úlomky) jsou ve scratchpadu vlákna, mapy v `local_test\otocka_D2x_*`. Záložní režimy:
  `UNIFIED_PAVED = False` (2.35.4), `JUNCTION_AREAS = False` (koridor podle šířky).
- **MTBO používá DTM** (`MTBO_USE_DTM = True` v `app/pipeline/dtm_import.py` od 2.33.0, `mtbo_dtm_off`): koridor silnice (jen do 10 m od osy
  silnice), chodníky od 0,5 mm, parkoviště od 300 m², schodiště, budovy, ploty a šířky cest z DTM – vše s prahy čitelnosti v měřítku. `False` vrátí kreslení z ZABAGED / OSM.
  LES bere z DTM budovy, ploty (včetně výplně oplocení olivou 520), šířky cest a široké pěší plochy, sprint všechno.
  Od 2.34.0 les jako MTBO kreslí i koridor silnice a parkoviště od 300 m² z DTM (`FOREST_DTM_AREAS`, `dtm_area_preset`; les 501.1 + okraj 501.2,
  parkoviště z DTM v lese i MTBO 501.1 + 501.2). Plochy z DTM se v lese i MTBO generalizují podle mm mapy (`dtm_sidewalks.generalize_area`:
  zálivy do 0,4 mm, výběžky do 0,3 mm, díry pod 1 mm², kusy pod 0,5 mm²; ne do budov).
  Koridor je jen jízdní pás bez chodníků (`CORRIDOR_WITH_SIDEWALKS = False`, Petr 2026-10-24 po srovnání variant), nikde užší než kresba
  silnice 502 v presetu (`corridor_min_drawn_m`); okraj se u napojení čáry silnice přeruší (`corridor_junction_omit`). Les pak nebere
  široké pěší plochy z DTM do 8 m od silnice. Záloha „silnice jen čárou“: `MTBO_ROAD_CORRIDOR = False`.
  Ploty v lese a MTBO: výplň olivou se hledá až po dedupu a propojení mezer (`fence_gap_connectors(same_line=True)` uzavře i mezeru v jedné čáře, `merge_fill_rings`).
- **Dedup chodníků v lese a MTBO je jen z OSM:** chodník i pěšina bez tagu chodníku (`SIDEWALK_DEDUP_BASES`, v lese i cyklostezka) souběžná se silnicí nebo OSM zpevněnou plochou se zahodí (celý při ≥ 70 % délky v pásu,
  jinak jen souvislé úseky ≥ 40 m). Pás se počítá ze skutečných šířek symbolů v sadě (`sidewalk_road_zone_geometry`: půlka silnice – v lese podle symbolu zvoleného šířkou z DTM – / okraje 501.2
  + půlka chodníku + `LINE_MIN_GAP_MM`, v metrech podle měřítka), ne pevných 12 m. Chodník ≥ 70 % v pásu se zahodí celý jen bez souvislého
  zbytku ≥ 30 m mimo pás (`SIDEWALK_KEEP_REST_M`, way/1451823252). Od 2.38.2 (Petr 2026-10-26) se chodník zahazuje i podél silnice, která je
  jen v DTM (plocha silnic z DTM dál než 8 m od každé OSM silnice, `dtm_only_road_geometry`; v mixed ji kreslí ZABAGED).
- **Koridor pěší zóny ze dvou souběžných linií** (`merge_parallel_pedestrian_to_areas`) vzniká jen ze zpevněných čar (OSM `surface`), ne z mostů / tunelů,
  ne přes budovy a ne z rozbíhajících se čar (průměrná šířka ≤ 12 m).
- **Zpevněné plochy v lese a MTBO jsou síť** (`build_paved_network` při sestavení mapy): OSM parkoviště, pěší zóny, area:highway, nástupiště
  a hřiště se sloučí (mezera do 3,5 m), ne přes pás silnice a ne přes plochy na mostě / v tunelu (`structure` z přípravy OSM). Silnice přes
  plochu se uvnitř nekreslí (`clip_road_in_paved`, jen průchod ≥ 6 m a dál než 2 m od hrany), menší cesty se na ploše přeruší. V lese a MTBO (výplň 501.1 + okraj 501.2)
  se okraj vynechá tam, kde by splýval se silnicí (`paved_edge_omit_zone`); parkoviště je v lese 501.1 + 501.2. Úzké parkoviště (pruh do 4,5 m), které z ≥ 60 % leží
  pod kresbou silnice + 1 m nebo k ní přiléhá dlouhou stranou (podélné stání), se s úsekem ulice slije do jedné plochy 501.1 v šířce kresby silnice s okrajem 501.2 (`merge_parking_into_road`);
  silnice se tam nekreslí. Široká parkoviště zůstávají samostatně. Navazující kusy čar se stejným symbolem se spojí (`join_line_pieces`).
  Krátká účelová komunikace (service do 60 m) ze silnice 502 na zpevněnou plochu / budovu se kreslí 502, pokud se kresba vejde mezi budovy (`service_link_indexes`); krátká service šířku z DTM nebere. Pěší plochy
  z OSM se kreslí jen tam, kde jsou široké aspoň jako kresba silnice 502 (`open_narrow_walk_areas`); užší výběžky kreslí čára cesty (DTM plochy ne – nemají čáru). Koridor ze dvou souběžných čar
  má v přípravě uloženou osu (`corridor_axes.geojson`), les a MTBO ji kreslí tam, kde úzký koridor odpadne (sprint ne).
- **Les: symbol cesty podle skutečné šířky z DTM** (`app/pipeline/dtm_widths.py`, `uses_dtm_widths`): kolmé řezy ploch `roads.json` /
  `sidewalks.json` podél OSM čáry (medián). Silnice ≥ 9 m → 502.1, ≥ 5 m → 502 (užší podle tagů), zpevněná pěšina ≥ 2,5 m → 505; pěší plochy
  z DTM širší než 0,5 mm mapy se kreslí jako zpevněná plocha a napojí do sítě. MTBO jen se zapnutým `MTBO_USE_DTM`, sprint beze změny.
- **Tunely v lese a MTBO:** OSM cesta v tunelu / s `layer < 0` delší než 40 m (`TUNNEL_MAX_DRAW_M`) se nekreslí, krátký podchod ano.
  Železnice a tramvaj ze ZABAGED se nekreslí v okolí 4 m od OSM kolejí v tunelu (`rail_tunnels.geojson` z přípravy OSM, `ZABAGED_RAIL_LAYERS`).
- **Malé zalomené schody v lese a MTBO** (OSM, `small_complex_steps`): navazující čáry schodů tvaru „L“ / „C“ s rozsahem do 15 m (kratší strana
  otočeného obdélníku ≥ 2 m) se nekreslí. Složitá schodiště z DTM les nekreslí už dřív (`is_complex_stairs`, sprint je kreslí plochou).
- **Plochy pod olivou v lese a MTBO** (Petr 2026-10-25, `paved_fill_outside_olive`): výplň 501.1 se o kreslenou olivu do 5 000 m² ořízne (`PAVED_FILL_OLIVE_MAX_M2`; velké areály jako ateliéry Barrandov ne), plocha
  uvnitř olivy (vstup může být povolený) zůstane jen okrajem 501.2 a oliva se nevyřízne. ZABAGED `OstatniPlochaVSidlech` se v lese a MTBO
  kreslí v síti zpevněných ploch (`zabaged_paved_areas`, `with_zabaged_paved_areas`, kind `pedestrian_area`, zdroj `zabaged`), ne 501.0
  s pevným obrysem – silnice se v ní ořízne a okraj se u silnice přeruší (Sedlčany). Smíšená stezka zasahující čárou do kresby silnice
  nebo koridoru se nekreslí (`bike_road_zone`).
- **Dětská hřiště z OSM se z DTM zeleně nevystřihují** (`DTM_GREEN_KEEP_KINDS`); parkoviště a pěší zóny ano.
- **Generalizace v lese a MTBO (2.46.3, Petr Retest 6 2026-10-28, kiosek u Lidlu):** budova pod 0,25 mm² mapy (`BUILDING_MIN_AREA_MM2`,
  25 m² v 1:10 000) je bod 521.3 „Budova (minimální velikost)“ (`small_buildings_as_points`); plochy sítě zpevněných ploch
  (OSM i ZABAGED, každá zvlášť) jdou přes `dtm_sidewalks.generalize_area` jako plochy z DTM (`paved_surface.generalize_paved_feats`,
  zálivy do 0,6 mm, výběžky do 0,5 mm, rohy zaoblené ~0,25 mm; budovy aspoň minimální velikosti zacelení nepřekročí). Krátké
  schodiště z DTM (pod 10 m) do 2 m od OSM schodů se nekreslí, OSM schody zůstanou celé (`short_stairs_with_osm_steps`).
- **Skály a balvany v lese a MTBO jen jisté** (Petr 2026-10-27, srovnání Harrachov a Vyšší Brod E1–E3): balvany 204 z DEM
  (`app/pipeline/boulders_dem.py`, `dotboulders.dxf`): vyvýšenina 1,2–3 m nad průměrem protilehlých bodů ve 3 m ve všech čtyřech
  směrech (násep / hrana jen napříč neprojde), rozestup 6 m; při sestavení se zahodí do 2 m od skalní plochy, 6 m od kupky, 25 m od
  čar cest / vody / plotů a 12 m od budov a vodních ploch (`filter_boulder_points`). Přesnost nových bodů proti ručním mapám 0,85–1,0.
  Drobné balvany a skalky pod ~1 m z DMR 5G nevyčteme (přesnost ~0,45); zahozené samostatné skalní linie mají jen 0,46–0,67, proto se dál nekreslí. Sprint beze změny.
- **Sprint podle srovnání s DS26 Barrandov** (Petr 2026-10-26, 2.36.0): plot ležící z ≥ 80 % uvnitř kreslené olivy dál než 1,5 m
  od okraje se nekreslí (`drop_fences_inside_olive`, dělicí ploty zahrad); brány a sloupky (`gate`, `wicket_gate`, `lift_gate`,
  `bollard`) nejsou 531 (`sprint_skips_barrier_point`); čáry cest se uvnitř budov (> 1 m od obrysu) nekreslí a OSM `covered=yes` /
  `tunnel=building_passage` je v budově průchod 522 vyříznutý z budovy (`sprint_building_passages`, `covered_passages.geojson`).
- **Sprint podle srovnání s Českým Krumlovem** (Petr 2026-10-26, 2.40.0, data `local_test\cesky_krumlov`): budovy 521 ze všech zdrojů se
  slučují do bloků, mezery pod 0,4 mm mapy bez cesty se zavřou, dvůr bez cesty se zaleje vždy a kreslené plochy (oliva, 501, louka)
  se o něj oříznou (`sprint_merge_buildings`; ISSprOM 521, Petr 2026-10-28 na podnět mapaře A. Hájka, 2.46.5). Ulice: parcely RÚIAN se způsobem využití komunikace (15–17) minus budovy minus zeleň z ortofota, jen kusy se
  zpevněnou OSM čárou ≥ 10 m, jako 501 pod ostatními objekty (`street_parcels.py`, `SPRINT_STREET_PARCELS`; ortofoto a maska zeleně jsou
  společný modul `orthophoto.py` s vláknem ortofota: ExG > 0,02 a sytost > 0,12). `amenity=bus_station` je parkoviště 501; silnice se ve
  sprintu uvnitř OSM parkoviště nekreslí (`sprint_parking_zone`). Kupky / prohlubně / jámy ve sprintu jen mimo zástavbu (`sprint_knolls_outside_town`,
  artefakty DMR 5G u domů). Srázy ve sprintu jen se skutečným skokem (`SPRINT_STEP_SIDE_RATIO`: strany sondy nejvýš 0,5 × sklon schodu)
  a ZABAGED `StupenSraz` jen do 3 m od ticků z DEM (`dem_step_zone`; Petr: cizí data náš algoritmus jen potvrzují).
- **Napojení silnic v lese a MTBO** (Petr 2026-10-27, job 4486336340e5 bez DTM): osa rozdělené vozovky se nepřemosťuje přes ostrůvek
  kruhového objezdu a končí na prstenci (`roundabout_islands`); užší OSM silnice se u uzlu rozvětvení se širší neořezává jako souběžná
  (`_fork_dists`, `FORK_RUN_MAX_M` 40 m, konec užší na širší a křížení v uzlu `FORK_END_RUN_MAX_M` 20 m); parkoviště z ≥ 80 % pod kresbou silnice se slije
  s ulicí i když je širší než pruh 4,5 m (`PARKING_UNDER_ROAD_SHARE`); silnice kreslená symbolem s výplní (MTBO `503` → 502.2,
  `is_major_road_code`) nekončí na okraji jiné silnice jako černá cesta.
  Osa rozdělené vozovky pokračuje přes obousměrný úsek za sloučením pásů až do křižovatky (do 60 m, `_extend_to_continuation`,
  Lamačova × K Barrandovu 2.43.5); jinak by mezi 502.0 a křižovatkou zůstal užší kus 502.1.
  Rampa odbočující z pruhu rozdělené vozovky (kreslené jednou osou) se po ořezu souběhu dotáhne po OSM čáře do kresby hlavní
  silnice a kolmo k její ose, i když OSM čára osu neprotne (`road_ends._into_band`, K Barrandovu 2.46.3, Petr Retest 6 2026-10-28).
  Krátké rozvětvení ze dvou jednosměrných ramen téže ulice (do 60 m, konce do 1,31 mm mapy od sebe, ve vrcholu do 60°) se kreslí
  jednou čarou po středu ramen (`fork_links`, Lipského u zastávky na Lamačově, Petr 2026-10-27 „jednou linií“, 2.43.6).
  Odbočovací rampa (nepojmenovaná jednosměrka do 80 m, oba konce do 50 m od křížení silnic, které spojuje, a míjí ho ≥ 5 m) se
  nekreslí – trojúhelník vedle křižovatky (`slip_lane_zone`, U Náhonu × K Barrandovu, Petr 2026-10-28: celý úsek jedním způsobem, 2.45.0).
- **MTBO koridor silnice** drží hystereze: k širokým kusům (≥ 9 m, ≥ 50 m) se připojí navazující úseky ≥ 5 m do 60 m (`CORRIDOR_BRIDGE_M`),
  jinak se silnice kreslila střídavě plochou a čárou.
- **Ploty z DTM (`filter_dubious_fences`):** plot s neznámým původem (`ZpusobPorizeniZPS = 99`) + `HraniceJinehoObjektu` se zahodí, když není u soukromé
  plochy. V menších obcích (např. Dobřichovice) má `ZPS_OPL` jen zlomek zahrad a budov (5 400 plotů typu 99, 47 zahrad), proto se jako důkaz berou i čáry
  `HraniceStavby` / `HraniceBudovy` z `ZPS_KR` (`load_construction_borders`, cache `.cons1.json`): plot, který se jich dotkne koncem nebo po nich vede, se
  ponechá. Plot na obrysu budovy (≤ 0,5 m) se nekreslí – budova má přednost (`cut_fences_by_buildings`, budovy = DTM + OSM bez protějšku). Budovy z DTM jsou
  v těchto obcích řídké, tvar budov je převážně z OSM.
- **Podezřelé ploty na hraně olivy:** filtr běží při přípravě DTM (na začátku jobu, ještě bez ZABAGED a OSM), proto `fences.json` ukládá i vynechané
  úseky (`dubious`) a při kreslení (`build_dtm_fence_parts`) se vrátí ty, které z ≥ 80 % délky leží do 0,5 m od okraje kreslené olivy
  (`fences_on_olive_edge`, `_load_dubious_fences`) – hranice soukromého pozemku. Dobřichovice: z 2 905 vynechaných se vrátí 1 505. Starší joby bez `dubious` se chovají jako dřív.
- **Propojení mezer mezi ploty** (`fence_gap_connectors`, sprint a les): dva volné konce téhož druhu plotu do 6 m, které na sebe míří (≤ 35°), se spojí
  čárou, pokud spojka leží do 1,5 m od kreslené olivy (OSM zahrada, oplocení z DTM, ZABAGED `ArealUceloveZastavby` / `OvocnySadZahrada` / `Hrbitov` – tak je oliva
  ve sprintu hlavně kreslená, `configs/zabaged.txt` „settlement“) a neprochází budovou, cestou ani jiným plotem. Bez olivy se nespojuje nic.
- **DTM mimo Prahu:** `dtm_sources` bere balíky z celostátního API DMVS pro všech 14 krajů (Vysočina ověřena 2026-10-26). Některé obce mají
  ZPS_OPL neúplné (Jihlava: 932 ploch budov proti 11 661 `BudovaDefinicniBod` v ZPS_KR); u obce s podílem pod 0,5 se plochy z jejího OPL
  neberou (`incomplete_opl_units`, klíč `incomplete_opl` ve `fences.json`), ploty z KR zůstávají a plochy kreslí OSM.
- **ID objektu z portálu DTM** je `<atr:ID>` v JVF (12–13 číslic); ID mohou být i v sadě **DI** (dopravní infrastruktura), kterou pipeline nepoužívá.
  Portál ukazuje živá data, balíky jsou měsíční export – čerstvý objekt v balíku být nemusí.
- **Zdroj kresby v GUI** (Petr 2026-10-27, 2.43.0): jedna volba „DTM“ (`dtm`: `auto` podle presetu = sprint z DTM, les + MTBO
  z OSM + ZABAGED; `1` DTM všude; `0` bez DTM). OSM + ZABAGED se kreslí vždy tam, kde DTM není. Volby jobu `dtm_sprint` / `dtm_forest` (API je přijímá dál),
  `prepare_oom_map(use_dtm=…)`, podrobnosti v `DEV.md`. Lokální otočky s DTM volají `prepare_oom_map` s výchozím `use_dtm=True`.
- **Mixed je jediný režim v GUI** (Petr 2026-10-23): OSM je hlavní, ZABAGED (postupně opouštíme) a DTM (vtahujeme) kresbu doplňují.
  Příprava OSM (sdílená se sprintem) ale v `paths_mixed` zahazuje OSM silnice proti ZABAGED Ulice; les a MTBO je proto při kreslení vracejí
  z `paths_osm` (`mixed_osm_roads_missing`, bez souběžných užších – OSM×OSM dedup platí). ZABAGED Ulice / SilniceDalnice v lese a MTBO
  ustoupí OSM (dedup celé čáry jako Pěšina / Cesta) a zbytek se ořízne o kresbu OSM silnic (`mixed_osm_road_band`). Sprint beze změny.
  Dedup chodníků, spojky a pás kresby 502 berou silnice z `paths_osm`.
  Úzký koridor ze dvou čar, který odpadne jako plocha, kreslí obě původní čáry, jsou-li čitelné (`corridor_axis_features`), jinak osu.
- **Odsun objektů od cest v lese a MTBO** (`app/pipeline/path_clearance.py`) je pozdní krok `prepare_oom_map` nad hotovými objekty:
  osy cest bere z mapy (OSM i ZABAGED, 502–508, 532, MTBO 8xx), šířky ze sady symbolů. Budovy a body se posunou celé, oliva
  se ořízne o pás cesty (0,05 mm), zdi/ploty po bodech (0,2 mm) jen bez přiblížení k jiné cestě; max. 0,9 mm na mapě. Ploty se
  v lese a MTBO spojují, otáčejí čárkami k olivě a uzavřené se plní olivou jako ve sprintu. Lokální otočky dřív běžely s
  `path_source="osm"` – výchozí v produkci je `mixed` (část cest ze ZABAGED), ověřuj i v něm.
  **Srázy** (104, 201; ZABAGED StupenSraz i DEM, Petr 2026-10-26 Barrandov): sráz podél cesty se posune po bodech (až
  `CLIFF_MAX_SHIFT_MM` 1,5 mm; odstup o délku čárek navíc jen když cesta leží na straně čárek = vpravo od směru),
  křížení cesty a část na ploše 501 se vyřízne, drobky pod 0,5 mm pryč (`_clear_cliff`). Otočky s `include_dxf=False`
  srázy z DEM nemají, jen ZABAGED.

## Konfigurace a tajemství

- Lokálně vycházej z `.env.example`; skutečný `.env` necommituj.
- Na NAS je `.env` vedle `docker-compose.nas.yml`.
- `GHCR_OWNER=pjanecekatlangmaster` musí být lowercase.
- SMTP je nutné pro privátní joby a zpětnou vazbu.
- Důležité proměnné: `SMTP_HOST`, `SMTP_PORT`, `SMTP_ENCRYPTION`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_FROM_NAME`, `PUBLIC_BASE_URL`, `FEEDBACK_TO`, `PRIVATE_JOB_RETENTION_HOURS`.
- Zdravotní stav mailu lze ověřit přes `/api/health`, pole `mail_configured`.
