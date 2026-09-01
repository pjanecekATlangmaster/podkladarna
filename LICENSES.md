# Licence a zdroje

Podkladárna (tento repozitář) je uvolněná pod **MIT**. Viz [LICENSE](./LICENSE).

Výstupy jobů (PNG, DXF, shapefile) vznikají z cizích dat a nástrojů — ty mají
vlastní podmínky. Při dalším šíření mapy je potřeba uvést zejména ČÚZK.

## Data ČÚZK (CC BY 4.0)

LiDAR a polohopis se stahují jako otevřená data Zeměměřického úřadu / ČÚZK:

- [DMR 5G](https://openzu.cuzk.gov.cz/opendata/DMR5G/epsg-5514/) — digitální model reliéfu
- [DMP 1G](https://openzu.cuzk.gov.cz/opendata/DMP1G/epsg-5514/) — digitální model povrchu
- [ZABAGED®](https://geoportal.cuzk.gov.cz/) — polohopis (ArcGIS REST / Geoprohlížeč)

Licence: [Creative Commons Uveďte původ 4.0](https://creativecommons.org/licenses/by/4.0/deed.cs).

Podmínky ČÚZK: [Podmínky poskytování prostorových dat](https://www.cuzk.gov.cz/Predpisy/Podminky-poskytovani-prostor-dat-a-sitovych-sluzeb/Podminky-poskytovani-prostorovych-dat-CUZK.aspx).

Citace na tiskových výstupech: **ČÚZK, [rok]** (rok = aktuálnost použitých dat).

## Karttapullautin (GPL-3.0)

Orientační reliéf, vegetace, vrstevnice a srázy kreslí
[Karttapullautin](https://github.com/karttapullautin/karttapullautin)
([GPL-3.0](https://github.com/karttapullautin/karttapullautin/blob/master/LICENSE)).
Docker image obsahuje binárku `pullauta`; zdroj je u upstreamu.

## Mapa výřezu

- [OpenStreetMap](https://www.openstreetmap.org/copyright) — dlaždice, ODbL
- [Leaflet](https://leafletjs.com/) — BSD-2-Clause, vendored v `web/static/leaflet/`

Záložní zdroj dlaždic: [CARTO](https://carto.com/) Voyager (data OSM).

## Nástroje v kontejneru

- [PDAL](https://pdal.io/) — BSD
- [GDAL](https://gdal.org/) — MIT/X

## Cílový software (není součástí Podkladárny)

- [OpenOrienteering Mapper](https://www.openorienteering.org/) — GPL-3.0
