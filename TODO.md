# TODO

Odložené nápady a otevřené body (ne priorita teď). Nejnovější nahoře.

## Výběr území obecným polygonem

Teď jen obdélník – obdélník jde napříč celou aplikací (výběr listů SM5, ořez
LiDARu, rastry DEM/vegetace/hillshade, WMS podklady, ořez vektorů, ohraničení
v .omap, cache, odhad času). Polygon jde vždy převést na obalový obdélník.

Přínos: hlavně čistší výstup (žádné objekty v nezajímavých místech, méně
objektů pro OCAD) a limit plochy by šel počítat z polygonu (protáhlá území).
Zrychlení jen částečné – ořez listu čte vždy celý list; zrychlí se kroky po
bodech (sloučení mračna, DEM, vegetace) úměrně vynechané ploše. Dřívější odhad:
polygon na 60 % obdélníku 6×6 km ušetří ~8 min z 55 (~15 %); limit výběru je
dnes 25 km² (3.2.2), časy nově neměřeny.

1. fáze – polygon jako maska (~2–3 dny):
   - kreslení polygonu v mapě (vrcholy, úchyty jako u obdélníku), obdélník
     zůstává výchozí;
   - API: polygon v options, `bbox_wgs84` = obalový obdélník (zpětná
     kompatibilita);
   - PDAL ořez mračna polygonem (`filters.crop` s WKT);
   - ořez vektorů a objektů .omap polygonem (dnes `clip_ring` /
     `clip_polyline` jen na obdélník), ohraničení AOI v .omap jako polygon;
   - limit: plocha polygonu ≤ 25 km² (`MAX_BBOX_AREA_KM2`); limit obalového
     obdélníku pro rastry znovu určit (dřívější ~60 km² platil při limitu 36 km²).
2. fáze (podle potřeby): maskování rastrů, výběr jen nutných listů SM5,
   cache klíčovaná polygonem.

## Ze zpětné vazby mapaře (2026-10-08)

- Volba „Obsah mapy“: jen podklady + vrstevnice + budovy (+ cesty) vs. plná
  automatická mapa; plošné symboly (louky, zpevněné) mapař nepoužije.
- Vyhlazení schodovitých okrajů ploch z rastru. HOTOVO (3.1.0, volba
  `veg_smooth` pro porosty 401–410 v lese a MTBO, viz DEV.md „Volby jobu a limity“).
- Vlastní šablona mapového klíče (nahrát .omap; import jde podle kódů značek)
  a výchozí ISSprOM 2019-2 rev. 6 (chybí např. trojúhelníky víceúrovňových
  staveb). Uživatel hledá úplnou sadu pro OOM.
  - Stav (2026-10): mapový klíč je teď česká OCAD sada ISOM 2017-2 / ISSprOM 2019-2
    (`configs/oom/*_cz.omap`, viz DEV.md „Sady symbolů“); trojúhelníky víceúrovňových
    staveb a další symboly jsou v sadě. Vlastní nahrávaná šablona zůstává otevřená.
  - Vlastní šablona: nejdřív promyslet **mapování symbolů** (katalog `symbol_catalog.yaml`
    je navázaný na kódy naší sady; cizí šablona má jiné/přečíslované kódy – viz rozbor
    Mapovani_Zderaz.omap níže, normalizace „.0“ a tabulka přemapování). Bez návrhu
    mapování neimplementovat.
  - Rozbor šablony ze školení kartografů (Mapovani_Zderaz.omap, 2026-10):
    ISOM 2017-2 1:15000 česky, 138 kódů shodných s naší; kódy jako „101.0“
    a přečíslované podvarianty (513.100, 520.4, 501.1, 203.101) → při použití
    normalizovat „.0“ + tabulka přemapování; chybí 206, 301, 412.1, 203.1/2.
    Navíc pomocné značky 1.x Ortofoto, 2.x LAS reliéf, 3.x pomocné – vhodné
    pro nejisté výstupy generátoru. Licence/šíření nejasné (asi ČSOS).
- OSM `natural=bare_rock` (např. [way/905065542](https://www.openstreetmap.org/way/905065542),
  50.0404N 14.3653E) mapovat na značku 214 – holá skála. HOTOVO a ověřeno
  (Petr, 2026-10): `bare_rock` jde jako 214 do .omap (sprint i les; MTBO
  214 nemá). Značku 206 z OSM nemapujeme, `scree` zůstává jen podklad.
  Zbývá jen zvážit `scree`.
- Mapa „Spousta změn, velká kontrola ďolíky“ (job): generuje se velké
  množství samostatných maličkých zelených ploch, na ní dobře vidět – zjistit,
  která vegetační třída je tvoří, a odfiltrovat nebo sloučit podle minimální
  plochy (související s vyhlazením okrajů ploch z rastru výše).
- Výška vegetace spíš jako rastrový podklad (CHM / hustota) než hotové plochy.
- Předvolby „podklady pro mapaře“ vs. „příprava na závod (embargo)“.

## Technické

- OSM zdroje (zjištěno 2026-10-09): Overpass zrcadla jsou nespolehlivá
  (overpass-api.de ~50 % 504, maps.mail.ru pomalé, private.coffee 500 a stará
  data; .ru mrtvé, odstraněno). Záloha `api.openstreetmap.org/api/0.6/map`
  funguje spolehlivě, ale:
  - `parse_osm_api_map_xml` ignoruje relace (multipolygony: budovy s dvorky,
    zahrady, parkoviště, hřiště, bare_rock…) → při záloze tyto objekty chybí
    (dlaždice se už necachuje jako úplná a log to hlásí – HOTOVO);
  - HTTP 400 při > 50 000 uzlech (husté centrum) se neřeší – dlaždici půlit;
  - HOTOVO: selhaná zrcadla se pamatují (`_MIRROR_FAILED_AT`, `MIRROR_COOLDOWN_S`)
    a záloha se loguje jako neúplná;
  - HOTOVO: vlastní Overpass URL (např. s API klíčem) se čtou z proměnné
    prostředí `PODKLADARNA_OVERPASS_URLS` (čárkami oddělené) a zkoušejí se jako první;
    zbývá jen prozkoumat poskytovatele s klíčem (NextGIS, FairwayMapper, Geofabrik,
    Tracestrack, Overspan…), klíč nedávat do repa.

- Krok DEM z DMR 5G trval na NAS u 6×6 km 9 min (lokálně stejná pipeline
  21 s, u 5,4 km² na NAS 6 s) – zjistit příčinu při dalším velkém jobu.
