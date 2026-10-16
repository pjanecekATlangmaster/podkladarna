# Lokální vývoj Podkladárny – workflow bez NAS

## Princip

1. **pytest** – API a upload (sekundy, bez Dockeru)
2. **docker compose dev** – plný stack s PDAL + GDAL lokálně
3. **smoke_e2e.py** – end-to-end proti běžící instanci
4. **GitHub Actions `test.yml`** – pytest na každý push
5. **Teprve pak** `./deploy-nas.sh` na Synology

---

## 1. Rychlé testy API (doporučeno před každým commitem)

```powershell
cd C:\Users\PetrJanecek\.cursor\projects\podkladarna
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -v
```

Ověří: health, presety, multipart upload, log jobu, chybové stavy.

---

## Testovací data (`testdata/`)

V repu jsou malé soubory pro celé kolečko (~2,5 MB):

- `DMR5G.laz`, `DMP1G.laz` (lokální test), `Zabaged.zip` — produkce stahuje DMP OK z openzu

```powershell
.\scripts\dev.ps1 up           # terminál 1 – Docker dev
.\scripts\dev.ps1 e2e-upload   # terminál 2 – jen upload
.\scripts\dev.ps1 e2e          # celá pipeline + čekání na ZIP (~5–30 min)
```

Nebo ručně:

```powershell
python scripts/smoke_e2e.py --wait-minutes 45
```

---

## 2. Lokální Docker (plná pipeline)

Vyžaduje Docker Desktop (WSL2).

Image (`Dockerfile`) bere GDAL z **conda-forge** (`gdal` + `libgdal-core`) — stejný
stack jako `ogr2ogr` / `gdalwarp`. C++ CLI včetně `gdal_translate` (GeoTIFF náhledy)
je v `libgdal-core`; build ověří `command -v gdal_translate`. **Neinstalovat** apt
`gdal-bin` vedle conda (dvojí PROJ/GDAL). Po změně Dockerfile u deploye:
`docker compose build` (lokální smoke loop bez Dockeru beze změny).

```powershell
docker compose -f docker-compose.dev.yml up --build
```

- http://localhost:8672
- změny v `app/`, `web/`, `configs/` → **automatický reload**
- data v `./data/`

Smoke test proti běžící instanci:

```powershell
python scripts/smoke_e2e.py --fake              # jen API upload
python scripts/smoke_e2e.py --wait-minutes 45   # testdata/ → celé kolečko
```

---

## 3. Skript `scripts/dev.ps1`

**Checkouty:** Petr smokuje z `C:\Users\PetrJanecek\.cursor\projects\podkladarna`.
Agenti editují jen worktree `C:\Users\PetrJanecek\.cursor\projects\podkladarna-agent`
(stejný git, agent worktree) — neukládat do Petrova live stromu,
jinak `--reload` / file-watch zabije běžící job.

```powershell
.\scripts\dev.ps1 test        # pytest (~2 s)
.\scripts\dev.ps1 run         # uvicorn bez --reload (smoke / dlouhé joby)
.\scripts\dev.ps1 run-reload  # uvicorn s --reload (jen krátký UI vývoj)
.\scripts\dev.ps1 up          # docker compose dev
.\scripts\dev.ps1 smoke       # fake upload (API)
.\scripts\dev.ps1 e2e-upload  # testdata → upload
.\scripts\dev.ps1 e2e         # testdata → celá pipeline
.\scripts\dev.ps1 all         # pytest → docker → e2e
```

### Důležité: `--reload` zabíjí běžící joby

`run` **nepoužívá** `--reload`. Dřív reload restartoval uvicorn při každé změně
v `app/` / `web/` / `configs/` (uložení v editoru, agent, pytest) → běžící job
skončil jako *„Přerušeno restartem serveru…“*.

Pro lokální smoke / dlouhé joby:

```powershell
.\scripts\dev.ps1 run
```

`run-reload` jen když potřebujete hot-reload při krátkém UI vývoji **bez** běžícího jobu.
Docker `docker-compose.dev.yml` má `--reload` dál (mount zdrojů) – při e2e
v Dockeru neukládejte kód uprostřed jobu.

### Windows PROJ / QGIS (EPSG:5514)

QGIS `ogr2ogr` must use QGIS `proj.db`, not pip pyproj’s older copy
(`DATABASE.LAYOUT.VERSION.MINOR = 4` → GDAL expects `>= 6`).

`scripts/dev.ps1` and `app.tool_env.apply_local_gis_env()` set this automatically.
One-liner if you run tools outside the script:

```powershell
$env:PROJ_DATA = 'C:\QGIS\share\proj'; $env:PROJ_LIB = $env:PROJ_DATA
# optional: append (do not prepend) so system python stays first
$env:PATH = $env:PATH + ';C:\QGIS\bin'
gdalsrsinfo EPSG:5514   # must not mention pyproj\proj_dir
```

---

## Sady symbolů (ISOM, ISSprOM, ISMTBOM)

- **ISOM 2017-2 (Revize 6)** a **ISSprOM 2019-2 (Revize 6)** vycházejí z oficiálních
  OCAD sad (otevřených a uložených v Mapperu jako `.omap` do `configs/oom/ocad_src/`).
  Základní názvy symbolů jsou české podle oficiálních českých překladů ČSOS, názvy podsymbolů
  jsou odvozené (`review: true` v katalogu). Sady jsou
  v `configs/oom/ISOM_2017_10000_cz.omap`, `ISOM_2017_15000_cz.omap`,
  `ISSprOM_2019_4000_cz.omap`. Kódy zůstávají jako v OCAD (`101.0`, `203.100`, …),
  názvy barev se nepřekládají. Lesní presety 7500 i 10000 používají sadu 10000.
- **ISMTBOM 2022** (MTBO) je stejně z OCADu: `configs/oom/ISMTBOM_2022_10000_cz.omap`
  a `ISMTBOM_2022_15000_cz.omap` (presety `mtbo_10000` / `mtbo_15000`). Na rozdíl od ISOM/ISSprOM
  se u ní **skryté symboly nemažou** (`KEEP_HIDDEN` ve `scripts/build_symbol_sets.py`):
  zůstávají `is_hidden` jako v OCADu a `build_oom_map` při tvorbě mapy odkryje jen ty, které
  objekty skutečně používají. Kódy generátoru pro MTBO jsou dál v číslování dřívějšího ISMTBOM
  (`831`, `526`, …) a sekce `mtbo` v katalogu je překládá na kódy OCAD (`815.0`, `521.0`, …).
  Starší `configs/oom/ISMTBOM_*.omap` a `ISOM_2017-2_*.omap` presety nepoužívají (nemažou se).
- `_cz.omap` se nepíšou ručně, generuje je skript:

  ```powershell
  python scripts/build_symbol_sets.py
  ```

  Odstraní skryté symboly (`is_hidden`) a nestandardní pomocné kódy 799/999, přejmenuje
  symboly včetně vnořených částí podle katalogu a zapíše `configs/oom/symbol_catalog.yaml`.
  Opakovaný běh zachová ruční úpravy v katalogu a doplní jen nové symboly.

### Katalog `configs/oom/symbol_catalog.yaml`

Sekce `isom` (sdílí 10k i 15k) a `sprint`; pro každý symbol sady:

- `ocad_code` – kód symbolu v sadě (např. `501.0`, `505.0`),
- `cz_name` – český název (zapisuje se do `.omap`),
- `generator_codes` – kódy, které používá generátor (`osm_oom_code`, `oom_code_for_dxf`,
  vegetace, vrstevnice, …) a které se mapují na tento symbol. Jeden kód generátoru smí
  patřit jen jednomu symbolu (jinak `ValueError`); více kódů na jeden symbol je v pořádku,
- `geom` – `point` / `line` / `area` / `text` (kombinovaný symbol je `area`, jen když má
  plošnou část),
- `review: true` – název nebo přiřazení je odvozené a čeká na kontrolu; drží se jen u symbolů,
  které generátor používá (`generator_codes` neprázdné). U ostatních (varianty, např. zvětšení
  120 %, minimální velikosti) stačí odvozený název, takže `review: false`,
- `keep_reason` – proč je v sadě symbol, který generátor nepoužívá (řada 7xx, texty).

Za běhu **neexistuje implicitní překlad** (žádné „kód + .0“ ani fallback na syrový kód).
`set_code(preset, měřítko, kód_generátoru)` v `app/pipeline/oom_symbol_map.py` přeloží kód
přes katalog; neznámý kód vrátí `None`, objekt se nenakreslí a job log dostane varování
„kód generátoru bez symbolu“ (počet dotazů na symbol, ne přesně objektů). Výjimka: kód
z vectorconf (`kp`) odvozený ze souboru `zabaged*.txt`, který katalog nezná, se v
`oom_code_for_vectorconf_rule` jen přeskočí bez varování – tak se tiše zahazovaly vrstvy bez
symbolu i dřív; kódy, které generátor vyrábí, hlídá `tests/test_generator_codes_covered.py`.
Staré sady (MTBO) procházejí beze změny.

Zpevněné plochy v lese: ISOM `501.0` má okraj v symbolu, `501.1` je jen výplň a `501.2` okrajová
čára. OSM plochy kreslené jako `501.1` (hřiště, pěší zóny, nástupiště) proto dostanou navíc
čáru `501.2` po skutečné hranici plochy (`osm_paths.py`, `_paved_edge_objects`): prstenec celý
ve výřezu = uzavřená čára, prstenec protnutý výřezem = jen otevřené kusy uvnitř (podél ořezu
na výřez se okraj nekreslí). Sprint a MTBO to nedělají (sprint `501.0` je kombinovaný
a okraj `501.100` obsahuje; `501.1`/`501.2` mají v ISSprOM jiný význam).

Testy: `tests/test_symbol_catalog.py` (sady a katalog), `tests/test_set_code.py` (překlad),
`tests/test_generator_codes_covered.py` (každý kód, který generátor umí vyprodukovat, je
v katalogu a geometrie symbolu odpovídá použití).

### Změna nebo nová sada (např. ISOM 2030)

1. Otevři oficiální OCAD sadu v Mapperu a ulož jako `.omap` do `configs/oom/ocad_src/`.
2. V `scripts/build_symbol_sets.py` doplň položku do `SETS` (klíč katalogu, zdroj, výstup
   `*_cz.omap`); pro nový klíč doplň české názvy do `scripts/symbol_names_cz.py`
   (nezmapované symboly dostanou anglický název a `review: true`).
3. Spusť skript, projdi `symbol_catalog.yaml` (`review: true`) a ručně vyplň
   `generator_codes` – zkontroluj šířky a geometrii (např. starý footprint 1,4 m ≠ stejné číslo v OCAD).
4. Uprav `symbol_set_path` (`app/pipeline/oom_symbols.py`) a `catalog_key` (`app/pipeline/symbol_catalog.py`).
5. `python -m pytest tests/ -q` – výčtový test ukáže kódy generátoru bez mapování.

## 4. NAS deploy až po zelených testech

```bash
# na NAS
./deploy-nas.sh
```

---

## Ladění selhání

| Kde | Příkaz |
|-----|--------|
| pytest | `pytest tests/ -v --tb=short` |
| Docker log | `docker compose -f docker-compose.dev.yml logs -f` |
| Job log API | `curl http://127.0.0.1:8672/api/jobs/<id>/log` |
| NAS | `docker compose -f docker-compose.nas.yml logs --tail=100` |

---

## Co pytest neřeší

- PDAL merge / celá pipeline – to až `docker compose dev` + `--data-dir`
- Reverse proxy limit – testujte HTTPS zvlášť po nasazení nginx conf

---

## Náhled PNG z `.omap`

Po zápisu `.omap` pipeline dělá dvě věci:

1. **Web „Otevřít PNG“** – vždy **Pillow + XML** (`work/preview.png` / `output/preview/oom_preview.png`), **bez deklinace**. Rychlé; Mapper se nevolá.
2. **Georef ZIP** – preferuje **OpenOrienteering Mapper CLI @ 300 DPI** (`--full-map`, konstanta `GEOREF_MAPPER_DPI`) → pracovní `output/preview/*-{les,mtbo,sprint}.png` + `.pgw`, z nich **GeoTIFF** `.tif` (dlaždice 512, DEFLATE+PREDICTOR=2, BIGTIFF=IF_SAFER, interní přehledky 2/4/8/16), **s grivací**. Obří PNG se po web náhledu maže a do ZIPu nejde; zůstane jen když GeoTIFF nevznikne (bez GDAL / `oom_geotiff=0`). Bez CLI buildu (typicky Windows tip se stock 0.9.6) job **explicitně** použije Pillow georef @ **300 DPI-eq** papíru (`map_per_px = 25400/DPI`, cap 10 000 px; override `PODKLADARNA_GEOREF_PILLOW_DPI`) → GeoTIFF, s grivací, a zapíše to do logu – tlačítko „Stáhnout georef náhledy“ zůstane. Malý ZIP: `podkladarna_georef_previews.zip` / API `/download/georef-previews`.

**Materiálový OOM ZIP** PNG náhledy mapy **neobsahuje** (ani Pillow, ani Mapper). ČÚZK WMS `references/` zůstávají.

Zapnout/vypnout OOM preview: `options.oom_preview=true|false` / `PODKLADARNA_OOM_PREVIEW=1|0`. GeoTIFF: `oom_geotiff=false` / `PODKLADARNA_OOM_GEOTIFF=0`.

### Mapper CLI (georef)

Stock Mapper **0.9.6** headless export **neumí** (otevřel by GUI). Potřeba build s CLI (upstream PR [#2523](https://github.com/OpenOrienteering/mapper/pull/2523) / `mfbehrens/oo-mapper` větev `cli`). Bez šablony job použije Pillow georef (jasný log, ne tiché „Mapper“).

```powershell
# Cesta k CLI binárce (ne stock 0.9.6, pokud nemá --cli)
$env:PODKLADARNA_MAPPER = "C:\cesta\k\Mapper.exe"
# Šablona – bez ní georef běží Pillow fallback (log), ne Mapper @ 300 DPI
$env:PODKLADARNA_MAPPER_EXPORT = '"{mapper}" --cli export --full-map -i "{omap}" -o "{png}" --dpi {dpi}'
# .omap → .ocd OCD12 do ZIPu (bez CONVERT se .ocd přeskočí; bez OCD12 flagu default v9)
$env:PODKLADARNA_MAPPER_CONVERT = '"{mapper}" --cli convert -i "{omap}" -o "{ocd}" --output-format OCD12'
# Volitelně timeout (s), default 600
$env:PODKLADARNA_MAPPER_TIMEOUT = "600"
```

Bez CLI: Pillow georef cílí **300 DPI papíru** (`map_per_px = 25400/DPI`), nejméně delší strana **4800 px** (3× starý cap), max **10 000 px**. Override: `PODKLADARNA_GEOREF_PILLOW_DPI`.

Na Linuxu CLI build defaultně nastaví `QT_QPA_PLATFORM=offscreen`. **Docker image** (tip ≥1.26.1) Mapper CLI už obsahuje (`/opt/mapper/bin/Mapper`, pin `mfbehrens/oo-mapper` `cli` @ `6dc1fd72`) a nastaví `PODKLADARNA_MAPPER` + `PODKLADARNA_MAPPER_EXPORT` + `PODKLADARNA_MAPPER_CONVERT` — viz `DEPLOY.md` / `Dockerfile`. Windows tip (`:8672`) bez CLI buildu dál padá na Pillow georef @ 300 DPI-eq (floor 4800) a bez `.ocd`; Docker změna je pro budoucí NAS/ostrý image, ne nutně lokální tip.

Vestavěný Pillow kreslí zjednodušenou symboliku. Orientace: nižší map Y nahoru. Web ořez kolem AOI (708 / 705).

```powershell
python scripts/oom_export_png.py C:\cesta\Mapa-mtbo.omap -o preview.png
.\scripts\fetch_openorienteering_mapper.ps1
```

## SMTP a privátní joby

Checkbox **„Privátní režim generování mapy…“** vyžaduje e-mail. Job se neobjeví ve veřejném `/api/jobs`, UI neukáže náhled ani ZIP. Po `done` worker pošle plain-text e-mail s odkazem `/d/{token}` (platí `PRIVATE_JOB_RETENTION_HOURS`, default 48). Expirované privátní joby maže startup/cleanup sweep (a 410 při přístupu po splatnosti).

Env (doporučeno v `.env` vedle checkoutu / compose; necommitujte hesla).
`app.settings` a `scripts/dev.ps1` `.env` načtou automaticky (nepřepisují
**neprázdné** proměnné; prázdný compose inject se z `/data/.env` doplní).
Na NAS `docker-compose.nas.yml` předává `SMTP_*` /
`PUBLIC_BASE_URL` / `FEEDBACK_TO` z host `.env` do kontejneru; alternativně
stačí `/data/.env` uvnitř volume. Od **2.2.12** Docker image při prázdném
injectu doplní NAS default (`datais-cz.mail.protection.outlook.com` +
`https://podkladarna.kibos.link`) — tip/lokál bez `/.dockerenv` ne.
Bez `SMTP_HOST` (a bez Docker defaultu) selže privátní mail i
`POST /api/feedback` (502). Bez `PUBLIC_BASE_URL` job doběhne, ale e-mail
s odkazem se neodešle. Health: `mail_configured` v `/api/health`.
Po restartu tipu se u `done` privátních jobů bez „E-mail s odkazem odeslán“ mail
zkusí znovu (`retry_missed_private_mails`).

| Proměnná | Význam | Příklad |
|----------|--------|---------|
| `SMTP_HOST` | SMTP server | `datais-cz.mail.protection.outlook.com` |
| `SMTP_PORT` | Port | `25` |
| `SMTP_ENCRYPTION` | `starttls` / `ssl` / `none` | `starttls` |
| `SMTP_USER` / `SMTP_PASSWORD` | Auth (prázdné = IP relay) | |
| `SMTP_FROM` | From adresa | `podkladarna@datais.cz` |
| `SMTP_FROM_NAME` | From jméno | `OB podklady` |
| `PUBLIC_BASE_URL` | Absolutní URL instance (bez `/`) | `https://podkladarna.example` |
| `PRIVATE_JOB_RETENTION_HOURS` | Platnost odkazu | `48` |
| `FEEDBACK_TO` / `OWNER_EMAIL` | Příjemce zpětné vazby z webu | `janecek@datais.cz` |
| `MAX_FEEDBACK_PER_IP_HOUR` | Rate limit zpětné vazby / IP | `5` |

Rychlý test SMTP (mockuje se v pytest; živý send):

```powershell
$env:SMTP_HOST='datais-cz.mail.protection.outlook.com'
$env:SMTP_PORT='25'
$env:SMTP_ENCRYPTION='starttls'
$env:SMTP_FROM='podkladarna@datais.cz'
$env:SMTP_FROM_NAME='OB podklady'
python -c "from app.mail import send_mail; send_mail('vas@email.cz', 'Podkladárna SMTP test', 'Funguje.')"
```

