# Lokální vývoj Podkladárny – workflow bez NAS

## Princip

1. **pytest** – API a upload (sekundy, bez Dockeru)
2. **docker compose dev** – plný stack s PDAL + pullauta lokálně
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

## A/B harness: KP vs `use_kp=false` (vegetace / srázy / náhled)

Po dvou jobech na **stejné AOI** (jeden s KP zapnutým, druhý odškrtnutým) porovnej hotové výstupy — deskriptivní stats, ne subjektivní pass:

```powershell
cd C:\Users\PetrJanecek\.cursor\projects\podkladarna
python scripts/compare_bez_kp_ab.py --help

python scripts/compare_bez_kp_ab.py `
  --a "data\jobs\<kp_job>\output" `
  --b "data\jobs\<bez_kp_job>\output" `
  --label-a KP --label-b bez-KP

# strojový JSON + uložený text
python scripts/compare_bez_kp_ab.py --a path\a --b path\b --json -o tmp\ab_report.txt
```

Vstupy: rozbalený ZIP, `output/`, nebo job root (`output/` + `work/`). Report: presence vege/cliffs/preview, feature count + plochy vegetace, počet/délka srázových ticků, rozměry náhledu. Analogie ke `scripts/compare_contours_oom.py` (to generuje srovnávací `.omap`; tento skript jen měří hotové artefakty).

---

## Náhled PNG z `.omap` (bez KP)

Když job běží s `use_kp=false`, hillshade compose se do webu nedává. Po zápisu `.omap` pipeline uloží `work/preview.png` (web `/preview.png`, ZIP `preview/preview.png`) a kopii `output/preview/oom_preview.png`. ČÚZK WMS reference v ZIPu zůstávají. Zapnout i při KP: `options.oom_preview=true` nebo `PODKLADARNA_OOM_PREVIEW=1`. Vypnout: `oom_preview=false` nebo `PODKLADARNA_OOM_PREVIEW=0`.

Stock **OpenOrienteering Mapper 0.9.6** export do PNG umí jen z dialogu File → Export. Samotné `Mapper.exe` se proto nespouští – otevřelo by GUI. Vestavěný náhled čte XML `.omap` a kreslí plochy/linie/body barvami symbolů (ne plná symbolika Mapperu).

`powershell
python scripts/oom_export_png.py C:\cesta\Mapa-mtbo.omap -o preview.png
.\scripts\fetch_openorienteering_mapper.ps1
`

