# Podkladárna – plán implementace

> Domácí webová služba (Docker na Synology NAS) pro generování orientačních podkladů
> z ČÚZK LiDAR + ZABAGED pomocí Karttapullautin.
>
> Hardware: Synology NAS, **AMD Ryzen R1600, 24 GB RAM** (amd64).
> Název: **Podkladárna** (slug: `podkladarna`).

---

## Architektura (finální – dohodnuto s „Karlem“)

```
┌─────────────────────────────────────────┐
│  podkladarna (1 kontejner)              │
│  ├─ FastAPI (web + API + static SPA)    │
│  ├─ SQLite (jobs, presets, cache index) │
│  ├─ worker thread (LiDAR exkluzivně)    │
│  ├─ pullauta (linux amd64) + PDAL+GDAL  │
│  └─ Python pipeline                     │
└─────────────────────────────────────────┘
  volumes: /data/jobs, /data/cache (SM5 LAZ)
```

- **Bez Redis** – zbytečné pro domácí NAS.
- **mem_limit:** 8–12 GB (NAS má 24 GB – dost headroom).
- **processes=2** v pullauta.ini (batch / větší oblasti).
- **TEMP_RETENTION_DAYS=7** – automatické mazání starých temp.

---

## Presety map

| Preset | Měřítko (600 DPI) | Ekvidistance | scalefactor | contour_interval | basemapinterval | formline |
|--------|-------------------|--------------|-------------|------------------|-----------------|----------|
| Sprint 2 m | 1:4000 | 2 m | 0.4 | 2 | 0.5 | 0 |
| Sprint 2,5 m | 1:4000 | 2,5 m | 0.4 | 2.5 | 0.625 | 0 |
| Lesní 1:7500 | 1:7500 | 5 m | 0.75 | 5 | 1.25 | 2 |
| Lesní 1:10000 | 1:10000 | 5 m | 1.0 | 5 | 1.25 | 2 |

Vzorec: `scalefactor = měřítko / 10000`.

### ČÚZK specifika (vždy)
- PDAL: DMR class **8 → 2** (ground)
- PDAL: DMP classes **5+6** (vegetace + budovy)
- `waterelevation` vypnuto (flat terén)
- `buildingsclass` vypnuto (budovy ze ZABAGED)
- Pořadí: **LiDAR → vektory** (vektory potřebují `temp/vegetation.pgw`)
- `vectorconf=zabaged.txt` (ČR); OSM jen preset Zahraničí (backlog)
- `savetempfolders=1`, `output_dxf=1` default

---

## Upload okénka (v1.0 i fallback po auto-download)

| # | Pole | Formát | Povinné |
|---|------|--------|---------|
| 1 | DMR 5G – model reliéfu | `.laz`/`.las` multi | ano* |
| 2 | DMP 1G / DMP OK – model povrchu | `.laz`/`.las` multi | ano* |
| 3 | ZABAGED – polohopis | `.zip` Shapefile | ne |

\* Při auto-download se stáhnou automaticky; upload zůstává jako alternativa.

---

## Auto-stahování ČÚZK (priorita – v1.1, hned po MVP)

### LiDAR – openzu (primární)
```
bbox (mapa, WGS84)
  → transform EPSG:5514
  → ArcGIS KladyMapovychListu/MapServer/24
     query bbox → MAPNOM[] (např. PRAH77)
  → download:
     https://openzu.cuzk.gov.cz/opendata/DMR5G/epsg-5514/{MAPNOM}.zip
     https://openzu.cuzk.gov.cz/opendata/DMP1G/epsg-5514/{MAPNOM}.zip
  → unzip → PDAL merge → crop bbox (+ buffer 30 m) → class fix
```

- Cache stažených listů: `/data/cache/sm5/{MAPNOM}/`
- UI: „Protíná listy: PRAH77, PRAH78 (4 listy)“ – ne velikosti v MB
- DMP OK: až bude na openzu (2027); do té doby DMP1G

### ZABAGED
- **v1.1:** upload (Geoprohlížeč) – spolehlivé
- **v2:** WFS po vrstvách, varování při limitu 1000 prvků

### OSM
- **Backlog** – preset Zahraničí; v ČR ne kombinovat s ZABAGED do PNG

---

## Fáze pipeline (stavový automat jobu)

```
prepare → lidar → vectors? → done
                ↓
         (po done, v1.5+)
         refine_vege | refine_cliffs | rerender | add_vectors
```

### Příkazy Karttapullautin
| Fáze | Příkaz |
|------|--------|
| LiDAR full | `pullauta merged.laz` |
| Vektory | `pullauta zabaged_clean.zip` |
| Re-render | `pullauta` (bez args) |
| Regenerace zeleně | `pullauta makevege` → `pullauta` |
| Regenerace srážů | `pullauta makecliffs …` → `pullauta` |
| Merge PNG (batch v2) | `pullauta pngmerge 1` |
| Merge DXF (batch v2) | `pullauta dxfmerge` |

### Dílčí běhy (v2)
- `contoursonly=1` / `vegeonly=1` / `cliffsonly=1` (vzájemně exkluzivní)

---

## Verze a rozsah

### v1.0 – MVP (několik hodin + testování)
**Cíl:** upload → preset → pipeline → download ZIP

- [ ] Repozitář `podkladarna/`
- [ ] Dockerfile (amd64): pullauta + PDAL + GDAL + Python + FastAPI
- [ ] Refaktor `sance/prepare_sance.py` → `pipeline/prepare_lidar.py`, `prepare_zabaged.py`
- [ ] `pipeline/ini_builder.py` + `configs/presets.yaml`
- [ ] `pipeline/run_job.py` – orchestrace prepare → lidar → vectors
- [ ] SQLite: jobs, log, stav fáze
- [ ] Worker thread (1 LiDAR job naráz)
- [ ] Web: formulář (preset, 3 upload okénka, checkboxy výstupů)
- [ ] API: CRUD jobs, start, log (polling/SSE), download ZIP
- [ ] docker-compose.yml pro Synology
- [ ] Smoke test: složka Šance (upload) → stejný výstup jako `run_sance.bat`

**Checkboxy default ON:** vrstevnice DXF, basemap DXF, srázy/knolly, PNG, ZABAGED clean, vektory do PNG (pokud ZIP).

**Parametry „Upravit“ (10 polí):** contour_interval, basemapinterval, scalefactor, formline, smoothing, greenshades, processes, depression_length, savetempfolders, vectorconf.

### v1.1 – Auto-stahování (priorita uživatele)
- [ ] `pipeline/fetch_openzu.py` – bbox → MAPNOM → download → cache
- [ ] Leaflet mapa + bbox v UI
- [ ] Režim: ◉ obdélník na mapě / ○ upload
- [ ] PDAL crop na bbox
- [ ] Odhad času v UI (ne velikost stažení)

### v1.5 – Iterace (KarttaGUI light)
- [ ] Po done: Regenerovat zeleň, Re-render, Přidat vektory později
- [ ] Expert panel – všechny INI skupiny (Contours / Vegetation / Cliffs / Processing / Optional)
- [ ] Uložené vlastní presety (JSON v SQLite)
- [ ] Náhled PNG v job detailu

### v2 – Rozšíření
- [ ] WFS ZABAGED (malé oblasti, limit 1000 varování)
- [ ] Batch: více SM5 + pngmerge / dxfmerge
- [ ] contoursonly / vegeonly / cliffsonly
- [ ] OSM preset (Caorle / zahraničí)
- [ ] DMP OK místo DMP1G

---

## Struktura repozitáře

```
podkladarna/
  IMPLEMENTATION_PLAN.md      ← tento soubor
  docker-compose.yml
  Dockerfile
  app/
    main.py                   # FastAPI
    db.py                     # SQLite
    worker.py
    pipeline/
      prepare_lidar.py
      prepare_zabaged.py
      fetch_openzu.py         # v1.1
      ini_builder.py
      run_job.py
    web/static/
      index.html
      app.js
  configs/
    presets.yaml
    param_schema.yaml         # v1.5 Expert
    zabaged.txt
    zabaged_layers.yaml
```

---

## API

```
POST   /api/jobs              # nový job
GET    /api/jobs
GET    /api/jobs/{id}
POST   /api/jobs/{id}/start   # v1.0 upload; v1.1 bbox nebo upload
GET    /api/jobs/{id}/log
GET    /api/jobs/{id}/download
POST   /api/jobs/{id}/vectors # v1.5 – přidat ZABAGED později
POST   /api/jobs/{id}/refine/vegetation   # v1.5
POST   /api/jobs/{id}/rerender            # v1.5
DELETE /api/jobs/{id}
```

---

## Synology – checklist pro Petra

- [ ] Container Manager, Docker Compose
- [ ] Platforma: **linux/amd64**
- [ ] Volume: `./jobs`, `./cache`
- [ ] Port **8672**
- [ ] mem_limit: 8192m–12288m (volitelné, NAS má 24 GB)

---

## Co jsme vyhodili (dohoda s Karlem)

- Redis / Celery / multi-container
- Parita KarttaGUI v první verzi
- WFS a OSM v MVP
- Velikosti stahování v UI
- Multi-tenant / účty

---

## Reference

- Karttapullautin: https://github.com/karttapullautin/karttapullautin
- KarttaGUI inspirace: https://github.com/GyDomonkos/Karttapullautin
- ČÚZK open data: https://openzu.cuzk.gov.cz/opendata/DMR5G/epsg-5514/
- SM5 grid: https://ags.cuzk.gov.cz/arcgis/rest/services/KladyMapovychListu/MapServer/24
- Lokální wiki: `wiki/WIKI.md`, workflow Šance: `sance/prepare_sance.py`

---

*Poslední aktualizace: 2026-08-31*
