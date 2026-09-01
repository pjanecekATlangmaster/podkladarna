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
- **JOB_RETENTION_DAYS=30** – mazání celých hotových/selhaných jobů (DB + disk).
- **TEMP_RETENTION_DAYS=7** – *zatím jen env proměnná, neimplementováno* – viz backlog níže.

### Backlog – úklid disk u jobů (odloženo)

> **2026-08:** Neimplementovat dřív, než bude jasné, jak reálně probíhají iterace (v1.5).

Struktura jobu na disku: `input/` (LAZ + ZABAGED) → `work/lidar/` (PDAL, `merged.laz`) → `work/temp/` (KP rastry, největší) → `output/` (ZIP).

| Co ponechat | Proč |
|-------------|------|
| `input/` | znovu bez uploadu; jediný zdroj pravdy |
| `work/lidar/merged.laz` | opakovat KP bez PDAL |
| `work/zabaged_clean.zip` | vektorová fáze bez re-clean |
| `work/temp/` | rychlé iterace parametrů KP (`savetempfolders`) – **největší** |
| `output/` | finální produkt; pro iteraci nepotřeba |

**Aktuální chování:** každý nový běh v `run_job.py` smaže `work/temp/` a jede znovu od `merged.laz`. Checkbox „Uložit temp“ nechá temp po dokončení na disku; do ZIP jdou z temp jen DXF.

**Rozhodnutí:** samostatné mazání `work/temp/` (podle `TEMP_RETENTION_DAYS`) nebo selektivní úklid `work/` u hotových jobů **odložit** – až uvidíme reálný workflow iterací (Regenerovat / Re-render / Expert panel).

**Kandidátní implementace později:**
- [ ] Po X dnech u `status=done`: smazat jen `work/temp/`, ponechat `input/` + `output/` + `merged.laz`
- [ ] Volba v UI: „Udržet temp pro iterace“ vs. „Uvolnit místo po stažení“
- [ ] v1.5 iterace: nemazat temp mezi refine/rerender běhy stejného jobu

---

## Presety map

| Preset | Měřítko (600 DPI) | Ekvidistance | scalefactor | contour_interval | basemapinterval | formline |
|--------|-------------------|--------------|-------------|------------------|-----------------|----------|
| Sprint 2 m | 1:4000 | 2 m | 0.4 | 2 | 0.5 | 0 |
| Sprint 2,5 m | 1:4000 | 2,5 m | 0.4 | 2.5 | 0.625 | 0 |
| Lesní 1:7500 | 1:7500 | 5 m | 0.75 | 5 | 1.25 | 2 |
| Lesní 1:10000 | 1:10000 | 5 m | 1.0 | 5 | 1.25 | 2 |
| MTBO 1:10000 | 1:10000 | 5 m | 1.0 | 5 | 1.25 | 2 |
| MTBO 1:15000 | 1:15000 | 5 m | 1.5 | 5 | 1.25 | 2 |

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
     https://openzu.cuzk.gov.cz/opendata/DMPOK-LAZ/epsg-5514/{MAPNOM}.zip
     (fallback: DMP1G, pokud DMP OK na listu ještě není)
  → unzip → PDAL merge → crop bbox (+ buffer 30 m) → class fix
```

- Cache stažených listů: `/data/cache/sm5/{MAPNOM}/`
- UI: „Protíná listy: PRAH77, PRAH78 (4 listy)“ – ne velikosti v MB
- DMP OK: primární model povrchu (obrazová korelace, 0,2 m); DMP 1G jen záloha

### ZABAGED
- **v1.1:** stejný bbox jako LiDAR → ArcGIS REST `ZABAGED_POLOHOPIS` (stránkování, shapefile ZIP)
- Ruční ZIP z Geoprohlížeče zůstává jako přepsání
- Celostátní GPKG z ATOM (~6 GB) se nestahuje

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

**Výstup v1.0 (aktuální):** `podkladarna_output.zip` – PNG + PGW, `temp/*.dxf`, `zabaged_clean.zip` (ruční import do OOM).

### v1.5 – Balíček pro OpenOrienteering Mapper
**Cíl:** ke stažení nabídnout ZIP připravený tak, aby šel v OOM otevřít bez hledání souborů.

- [x] **`podkladarna_oom.zip`** (druhé tlačítko „ZIP pro OOM“)
- [x] Pevná struktura složek s **relativními cesty** uvnitř archivu:

```
podkladarna_oom/
  README_OOM.txt              # krátký návod (pořadí importu, měřítko, CRS)
  metadata.json               # preset, contour_interval, scalefactor, EPSG:5514
  basemap/
    pullautus.png
    pullautus.pgw
  relief/                     # volitelně pullautus_depr.*
    pullautus_depr.png
    pullautus_depr.pgw
  karttapullautin/
    contours.dxf              # nebo temp/contours.dxf, cliffs.dxf, …
    basemap.dxf
    …
  references/                 # v1.6 – referenční podklady (jen pro OOM, ne do finální mapy)
    orthophoto.png + .pgw     # ČÚZK WMS
    osm.png + .pgw            # OpenStreetMap dlaždice
    hillshade_dmr5g.png + .pgw  # GDAL hillshade z DMR 5G (az 315°, alt 45°)
  podkladarna.omap            # v1.6 – minimální OOM soubor s načtenými šablonami
  vectors/
    Budova.shp + .dbf/.shx/.prj …
    Cesta.shp
    …                           # rozbalené z zabaged_clean.zip
```

- [x] `README_OOM.txt`: doporučený postup (georeferencovaný PNG jako podklad → DXF vrstevnice jako template → SHP vektory)
- [x] Referenční podklady: ortofoto ČÚZK, OSM, hillshade DMR 5G (315°/45°)
- [x] Základní `podkladarna.omap` s georeferencovanými šablonami
- [x] V UI: surový ZIP i „ZIP pro OOM“

**Poznámka:** `.omap` zatím obsahuje rasterové šablony; DXF/SHP a plná symbolika ISOM jsou stále v2.

### v2 – Soubor `.omap` s provázanými vrstvami
**Cíl:** po rozbalení ZIPu (nebo přímo ke stažení) **hotový `.omap`**, kde jsou cesty relativní vůči umístění souboru mapy.

- [x] Generátor `pipeline/build_oom_map.py` – základní `.omap` s raster šablonami
- [ ] V `.omap` odkazovat relativně i na DXF/SHP vrstvy
- [ ] Ke stažení **`podkladarna_oom_bundle.zip`**: `.omap` + všechny datové soubory ve stejném stromu
- [ ] Uživatel: rozbalit → dvojklik / OOM „Open“ → mapa s načtenými podklady
- [ ] Ošetřit měřítko mapy a CRS (EPSG:5514 / S-JTSK) podle presetu
- [ ] Smoke test: otevření v aktuální verzi OOM na Windows

**Omezení / rizika (v2):**
- Formát `.omap` se může lišit mezi verzemi OOM – generovat pro cílovou verzi (např. 0.6.x+)
- Ne vše jde do `.omap` automaticky (symbolika ZABAGED, barvy ISOM) – minimum: podklad + vrstevnice + vektory jako editovatelné vrstvy
- Fallback vždy ponechat: `podkladarna_oom.zip` bez `.omap` (v1.5)

### Auto-stahování ČÚZK (v1.1)

- [x] `pipeline/fetch_openzu.py` – bbox → MAPNOM → download → cache
- [x] Leaflet mapa + bbox v UI
- [x] Režim: ◉ obdélník na mapě / ○ upload
- [x] PDAL crop na bbox
- [x] Odhad času v UI (ne velikost stažení)

### v1.5 – Iterace (KarttaGUI light)
- [ ] Po done: Regenerovat zeleň, Re-render, Přidat vektory později
- [ ] Expert panel – všechny INI skupiny (Contours / Vegetation / Cliffs / Processing / Optional)
- [ ] Uložené vlastní presety (JSON v SQLite)
- [x] Náhled PNG v job detailu
- [x] **OOM balíček** – viz sekce výše (`podkladarna_oom.zip`)
- [ ] **Úklid disk** – až po ověření iterací; viz backlog „úklid disk u jobů“ v Architektuře

### v2 – Rozšíření
- [ ] **Generování `.omap`** s relativními cestami – viz sekce výše
- [x] WFS/AGS ZABAGED (malé oblasti; AGS stránkování 2000)
- [ ] Batch: více SM5 + pngmerge / dxfmerge
- [ ] contoursonly / vegeonly / cliffsonly
- [ ] OSM preset (Caorle / zahraničí)
- [x] DMP OK místo DMP1G (primární zdroj, fallback na DMP 1G)

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
      build_oom_map.py          # v2 – generátor .omap
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
GET    /api/jobs/{id}/download              # podkladarna_output.zip (v1.0)
GET    /api/jobs/{id}/download/oom          # v1.5 – podkladarna_oom.zip
GET    /api/jobs/{id}/download/oom-bundle   # v2 – .omap + data
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

*Poslední aktualizace: 2026-09-01 (v1.5 ZIP pro OOM)*
