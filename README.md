# Podkladárna

Webová služba, která z otevřených dat vygeneruje podklad pro kreslení orientační mapy (sprint, les, MTBO).
Výstupem je jeden ZIP pro [OpenOrienteering Mapper](https://www.openorienteering.org/apps/mapper/):
`.omap` s vrstevnicemi, vegetací, srázy, cestami, budovami a dalšími objekty, georeferencované náhledy
a podkladové vrstvy (ortofoto, stínování, OSM …).

Česká verze se jmenuje **Podkladárna**, mezinárodní verze pro další země **Omapic**; jde o tentýž kód,
jazyk a nabízené země určuje konfigurace domény.

> *English summary:* Podkladárna / Omapic generates orienteering base maps (sprint, forest, MTBO) for
> OpenOrienteering Mapper from open LiDAR, OpenStreetMap and national topographic data. Maps you create
> are licensed CC BY 4.0 – please credit the source printed in `ABOUT_OMAPIC.txt` of your ZIP.

## Zdroje dat

- **OpenStreetMap** je základ kresby (cesty, budovy, vodstvo, plochy). Kreslí se plnohodnotně i bez dalších zdrojů.
- **Výšková data (LiDAR)** dávají reliéf a výšku porostu: v Česku DMR 5G + DMP OK z ČÚZK, v dalších zemích
  národní modely terénu a povrchu.
- **Národní polohopis a technické mapy** kresbu z OSM doplňují: v Česku ZABAGED®, RÚIAN a DTM krajů (DMVS),
  v dalších zemích obdobné velkoměřítkové mapy tam, kde jsou volně dostupné.
- Přehled zdrojů, licencí a stavu po zemích: **[docs/zdroje-dat.md](./docs/zdroje-dat.md)**
  (generováno z registru `configs/data_sources.yaml`), seznam zemí: `configs/countries.yaml`.

Volně inspirováno [Karttapullautinem](https://github.com/karttapullautin/karttapullautin); pipeline jde vlastní
cestou (vegetace z hustoty LiDAR odrazů, srázy a skály z DMR, vrstevnice GDAL). Vegetace je výška a hustota
porostu, ne průběžnost – kresbu vegetace doladí mapař v terénu.

Výstup je georeferencovaný v souřadnicovém systému země (Česko S-JTSK, EPSG:5514) včetně magnetické deklinace
a grivace.

## Vstup a presety

Výřez se vybírá na mapě. Výchozí stropy plochy: MTBO 25 km², les bez MTBO 15 km² (`FOREST_MAX_BBOX_AREA_KM2`),
sprint 4 km² (`SPRINT_MAX_BBOX_AREA_KM2`).

- Sprint 1:4000 · 2 m / 2,5 m
- Lesní 1:7500 / 1:10 000 · 5 m
- MTBO 1:10 000 / 1:15 000 · 5 m

Stažená data (LiDAR, polohopis, OSM) se ukládají do sdílené cache v datové složce, ne do jednotlivých jobů.

## Sady symbolů

Lesní a sprintové mapy používají sady **ISOM 2017-2 (Revize 6)** a **ISSprOM 2019-2 (Revize 6)** vycházející
z oficiálních OCAD sad, MTBO **ISMTBOM 2022**; české i anglické názvy symbolů
(`configs/oom/`, generuje `scripts/build_symbol_sets.py`). Postup přegenerování: **[DEV.md](./DEV.md)**,
původ a licence: **[LICENSES.md](./LICENSES.md)**.

## Spuštění v Dockeru

Docker image obsahuje PDAL, GDAL a OpenOrienteering Mapper CLI.

```bash
cp .env.example .env      # upravte podle sebe (SMTP, PUBLIC_BASE_URL, …)
docker compose up -d --build
```

Otevřete http://localhost:8672. Data a cache jsou ve složce `./data` (v kontejneru `/data`).

Důležité proměnné (vzor a popis v `.env.example`):

- `PUBLIC_BASE_URL` – veřejná adresa instance (odkazy v e-mailech),
- `SMTP_*`, `FEEDBACK_TO` – e-mail pro soukromé joby a zpětnou vazbu (bez SMTP běží jen veřejné joby),
- `SITES`, `LANGS` – domény, jazyk a nabízené země (bez nastavení jen čeština a Česko),
- `COUNTRIES_BETA` – zpřístupní země ve stavu beta,
- `API_CLIENT_KEY` – klíč pro přístup přes API (hlavička `X-Api-Key`).

Provoz se dvěma instancemi (ostrá a testovací) popisuje **[DEPLOY.md](./DEPLOY.md)**
(`docker-compose.nas-ab.yml`, `update-nas-ab.sh`).

## Lokální vývoj (bez Dockeru)

Vyžaduje Python 3, PDAL a GDAL v `PATH` (na Windows proj.db z QGIS, viz DEV.md).

```bash
export PODKLADARNA_DATA=./data
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload --app-dir .
python -m pytest tests/ -q
```

Podrobný vývojový postup, testy a e2e: **[DEV.md](./DEV.md)**.

## Licence a uvedení zdroje

**Kód** je pod **[MIT](./LICENSE)** (© 2026 Petr Janeček).

**Vygenerovaná mapa** (`.omap`, `.ocd`, PNG / GeoTIFF, DXF, SHP) je pod
**[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.cs)**. Při tisku, závodě, publikaci
nebo sdílení mapy **uveďte zdroj** – přesný řádek je v textovém souboru v ZIPu, v poznámce mapy
(Mapa → Poznámky) i na stránce `/licence`:

- Česko: `Podklad: Podkladárna · ČÚZK · OSM, [rok]`
- další země (Omapic): `Base map: Omapic · <národní zdroje> · OSM, [year]`

`[rok]` je rok aktuálnosti použitých dat; nevíte-li ho, stačí rok vytvoření mapy. Použitá data mají
vlastní licence (ČÚZK CC BY 4.0, OpenStreetMap ODbL, národní poskytovatelé podle země), přehled je
v **[LICENSES.md](./LICENSES.md)** a [docs/zdroje-dat.md](./docs/zdroje-dat.md).
