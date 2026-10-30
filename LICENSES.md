# Licence a zdroje

## Software (MIT)

Podkladárna (tento repozitář – web, API, pipeline) je uvolněná pod **MIT**.
Viz [LICENSE](./LICENSE). MIT se vztahuje na kód; **nevyžaduje** uvedení
zdroje na mapách, které službou vytvoříte.

## Výstup jobu (CC BY 4.0)

PNG, `.omap`, DXF a shapefile z jobu jsou pod
[Creative Commons Uveďte původ 4.0](https://creativecommons.org/licenses/by/4.0/deed.cs).
Při dalším použití mapy (tisk, závod, publikace) **uveďte zdroj**, např.:

**Podklad: Podkladárna · ČÚZK · OSM, [rok]**

Výstupy vznikají z cizích dat a nástrojů — ty mají vlastní podmínky níže.
ČÚZK data jsou sama o sobě CC BY 4.0; citace ČÚZK je tedy povinná i bez
licence výstupu Podkladárny.

## Data ČÚZK (CC BY 4.0)

LiDAR a polohopis se stahují jako otevřená data Zeměměřického úřadu / ČÚZK:

- [DMR 5G](https://openzu.cuzk.gov.cz/opendata/DMR5G/epsg-5514/) — digitální model reliéfu
- [DMP OK](https://openzu.cuzk.gov.cz/opendata/DMPOK-LAZ/epsg-5514/) — digitální model povrchu (obrazová korelace)
- [ZABAGED®](https://geoportal.cuzk.gov.cz/) — polohopis (ArcGIS REST / Geoprohlížeč)
- [RÚIAN / INSPIRE](https://ags.cuzk.gov.cz/arcgis/rest/services/RUIAN/MapServer) — budovy (`StavebniObjekt`) jako SHP podklad ve `zabaged/`
- Ortofoto ČÚZK ([WMS ORTOFOTO](https://ags.cuzk.gov.cz/arcgis1/services/ORTOFOTO/MapServer/WMSServer)) — podkladová vrstva v `references/`
  a podklad pro rozlišení louky / drsného terénu a zeleně
- Katastrální mapa (WMS ČÚZK, [services.cuzk.gov.cz](https://services.cuzk.gov.cz/wms/local-km-wms.asp)) — podkladová vrstva v `references/`

Licence: [Creative Commons Uveďte původ 4.0](https://creativecommons.org/licenses/by/4.0/deed.cs).

Podmínky ČÚZK: [Podmínky poskytování prostorových dat](https://www.cuzk.gov.cz/Predpisy/Podminky-poskytovani-prostor-dat-a-sitovych-sluzeb/Podminky-poskytovani-prostorovych-dat-CUZK.aspx).

Citace na tiskových výstupech: **ČÚZK, [rok]** (rok = aktuálnost použitých dat).

## DTM kraje (DMVS)

Zaměřený polohopis (chodníky, silnice, budovy, schodiště, ploty, mosty…) se bere z
**digitální technické mapy kraje**, kterou vede kraj a zpřístupňuje ji ČÚZK v Informačním
systému DMVS (veřejné datové sady `ZPS`). Veřejná data jsou dostupná zdarma a bez přihlášení
(„bez jakýchkoliv podmínek“); informační systém DMVS byl vybudován z IROP (spolufinancováno
Evropskou unií).

U těchto datových sad není výslovná licence (např. CC BY) uvedena a podmínky poskytování dat
ČÚZK (CC BY 4.0, „ČÚZK, [rok]“) DTM kraje výslovně neřeší. Využití dat ve výstupech Podkladárny
je ale potvrzeno. Zdroj se uvádí v duchu těchto podmínek: **DTM kraje · ČÚZK / DMVS, [rok]**.

- [DMVS – poskytování dat](https://cuzk.gov.cz/DMVS/Poskytovani-dat.aspx)
- [Projekt Vybudování IS DMVS ČR (IROP)](https://cuzk.gov.cz/O-resortu/Strukturalni-fondy/Projekty-CUZK-z-IROP/Projekt-Vybudovani-IS-DMVS-CR.aspx)

## AOPK památné stromy (CC BY 4.0)

[Památné stromy – jedinci](https://gis.nature.cz/arcgis/rest/services/PamatneStromy/PamatneStromy/MapServer)
(Agentura ochrany přírody a krajiny ČR). Licence CC BY 4.0; při šíření uveďte AOPK ČR.

## Inspirace (Karttapullautin)

Podkladárna byla volně inspirována
[Karttapullautinem](https://github.com/karttapullautin/karttapullautin)
a pak šla vlastní cestou (vegetace / srázy / DEM).

## Sady symbolů

- **ISOM 2017-2 (Revize 6)** a **ISSprOM 2019-2 (Revize 6)** (`configs/oom/*_cz.omap`)
  vycházejí z oficiálních OCAD sad symbolů (`configs/oom/ocad_src/`, otevřených
  a uložených v OpenOrienteering Mapperu). Základní názvy symbolů jsou podle oficiálních
  českých překladů specifikací (ČSOS), názvy podsymbolů (minimální velikosti, varianty,
  obrysy) jsou odvozené; skript `scripts/build_symbol_sets.py` je
  vygeneruje, odstraní skryté symboly a přejmenuje symboly.
- **ISMTBOM 2022** (MTBO, `configs/oom/ISMTBOM_2022_*_cz.omap`) vychází stejně z oficiální OCAD
  sady (`configs/oom/ocad_src/ISMTBOM_2022_*.omap`); skryté symboly zůstávají jako v OCADu.
  Starší sady OpenOrienteering Mapperu (`configs/oom/ISMTBOM_*.omap`, `ISOM_2017-2_*.omap`,
  [GPL-3.0](https://github.com/OpenOrienteering/mapper)) se už nepoužívají.
- TODO Petr: potvrdit podmínky šíření počeštěné odvozeniny OCAD sad.

## Mapa výřezu

- [OpenStreetMap](https://www.openstreetmap.org/copyright) — dlaždice, ODbL
- [Leaflet](https://leafletjs.com/) — BSD-2-Clause, vendored v `web/static/leaflet/`

Záložní zdroj dlaždic: [CARTO](https://carto.com/) Voyager (data OSM).

## Nástroje v kontejneru

- [PDAL](https://pdal.io/) — BSD
- [GDAL](https://gdal.org/) — MIT/X
- [OpenOrienteering Mapper](https://www.openorienteering.org/) **CLI** — **GPL-3.0**
  (Docker image: pin `mfbehrens/oo-mapper` větev `cli` @
  `6dc1fd72`, upstream PR
  [#2523](https://github.com/OpenOrienteering/mapper/pull/2523);
  build recept je multi-stage `Dockerfile`)

### GPL nabídka zdrojů (Mapper v image)

Distribuce binárky Mapperu v Docker image podléhá GPL-3.0. Odpovídající
zdroj je veřejný git pin výše; recept sestavení je v `Dockerfile`
(stage `mapper-builder`). Na vyžádání (Issues / e-mail maintainerovi)
poskytneme tarball zdroje k danému pinu nebo odkaz na clone + SHA.

Lokální Windows tip (`:8672`) Mapper CLI do Dockeru nepotřebuje —
georef bez CLI padá na Pillow @ 300 DPI-eq (viz `DEV.md`).

## Cílový software (GUI u uživatele)

- [OpenOrienteering Mapper](https://www.openorienteering.org/) — GPL-3.0
  (editace `.omap`; stock 0.9.6 **není** headless CLI pro georef PNG)
