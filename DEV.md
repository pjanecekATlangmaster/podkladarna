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
(stejný git, větev `cursor/bez-kp-pipeline-a36e`) — neukládat do Petrova live stromu,
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

Pro lokální smoke / bez-KP pipeline:

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

- PDAL merge, pullauta běh – to až `docker compose dev` + `--data-dir`
- Reverse proxy limit – testujte HTTPS zvlášť po nasazení nginx conf

---

## A/B harness (legacy)

Skript `scripts/compare_bez_kp_ab.py` zůstal pro staré artefakty; tip už KP nerunuje (`use_kp` je vždy false).


## Náhled PNG z `.omap` (bez KP)

Když job běží s `use_kp=false` (výchozí), pipeline po zápisu `.omap` dělá dvě věci:

1. **Web „Otevřít PNG“** – vždy **Pillow + XML** (`work/preview.png` / `output/preview/oom_preview.png`), **bez deklinace**. Rychlé; Mapper se nevolá.
2. **Georef ZIP** – preferuje **OpenOrienteering Mapper CLI @ 600 DPI** (`--full-map`) → `output/preview/*-{les,mtbo,sprint}.png` + `.pgw` (+ volitelně `.tif`), **s grivací**. Bez CLI buildu (typicky Windows tip se stock 0.9.6) job **explicitně** použije Pillow georef (PNG+PGW±GeoTIFF, s grivací) a zapíše to do logu – tlačítko „Stáhnout georef náhledy“ zůstane. Malý ZIP: `podkladarna_georef_previews.zip` / API `/download/georef-previews`.

**Materiálový OOM ZIP** PNG náhledy mapy **neobsahuje** (ani Pillow, ani Mapper). ČÚZK WMS `references/` zůstávají. KP `kp/pullautus*` jen když běží KP.

Zapnout OOM preview i při KP: `options.oom_preview=true` / `PODKLADARNA_OOM_PREVIEW=1`. Vypnout: `oom_preview=false` / `PODKLADARNA_OOM_PREVIEW=0`. GeoTIFF: `oom_geotiff=false` / `PODKLADARNA_OOM_GEOTIFF=0`.

### Mapper CLI (georef)

Stock Mapper **0.9.6** headless export **neumí** (otevřel by GUI). Potřeba build s CLI (upstream PR [#2523](https://github.com/OpenOrienteering/mapper/pull/2523) / `mfbehrens/oo-mapper` větev `cli`). Bez šablony job použije Pillow georef (jasný log, ne tiché „Mapper“).

```powershell
# Cesta k CLI binárce (ne stock 0.9.6, pokud nemá --cli)
$env:PODKLADARNA_MAPPER = "C:\cesta\k\Mapper.exe"
# Šablona – bez ní georef běží Pillow fallback (log), ne Mapper @ 600 DPI
$env:PODKLADARNA_MAPPER_EXPORT = '"{mapper}" --cli export --full-map -i "{omap}" -o "{png}" --dpi {dpi}'
# Volitelně timeout (s), default 600
$env:PODKLADARNA_MAPPER_TIMEOUT = "600"
```

Na Linuxu CLI build defaultně nastaví `QT_QPA_PLATFORM=offscreen`. Windows tip často nemá CLI build – georef ZIP pak vznikne Pillow fallbackem.

Vestavěný Pillow kreslí zjednodušenou symboliku. Orientace: nižší map Y nahoru. Web ořez kolem AOI (708 / 705).

```powershell
python scripts/oom_export_png.py C:\cesta\Mapa-mtbo.omap -o preview.png
.\scripts\fetch_openorienteering_mapper.ps1
```

### Default bez KP

Tip ≥1.26.0: Karttapullautin runtime odstraněn; pipeline je vždy bez KP.

---

## SMTP a privátní joby

Checkbox **„Privátní režim generování mapy…“** vyžaduje e-mail. Job se neobjeví ve veřejném `/api/jobs`, UI neukáže náhled ani ZIP. Po `done` worker pošle plain-text e-mail s odkazem `/d/{token}` (platí `PRIVATE_JOB_RETENTION_HOURS`, default 48). Expirované privátní joby maže startup/cleanup sweep (a 410 při přístupu po splatnosti).

Env (doporučeno v `.env` vedle checkoutu / compose; necommitujte hesla).
`app.settings` a `scripts/dev.ps1` `.env` načtou automaticky (nepřepisují už
nastavené proměnné). Bez `PUBLIC_BASE_URL` job doběhne, ale e-mail se neodešle.
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

Rychlý test SMTP (mockuje se v pytest; živý send):

```powershell
$env:SMTP_HOST='datais-cz.mail.protection.outlook.com'
$env:SMTP_PORT='25'
$env:SMTP_ENCRYPTION='starttls'
$env:SMTP_FROM='podkladarna@datais.cz'
$env:SMTP_FROM_NAME='OB podklady'
python -c "from app.mail import send_mail; send_mail('vas@email.cz', 'Podkladárna SMTP test', 'Funguje.')"
```

