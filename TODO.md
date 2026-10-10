# TODO

Odložené nápady a otevřené body (ne priorita teď). Nejnovější nahoře.

## Výběr území obecným polygonem

Teď jen obdélník – obdélník jde napříč celou aplikací (výběr listů SM5, ořez
LiDARu, rastry DEM/vegetace/hillshade, WMS podklady, ořez vektorů, ohraničení
v .omap, cache, odhad času). Polygon jde vždy převést na obalový obdélník.

Přínos: hlavně čistší výstup (žádné objekty v nezajímavých místech, méně
objektů pro OCAD) a limit plochy by šel počítat z polygonu (protáhlá území).
Zrychlení jen částečné – ořez listu čte vždy celý list; zrychlí se kroky po
bodech (sloučení mračna, DEM, vegetace) úměrně vynechané ploše. Odhad: polygon
na 60 % obdélníku 6×6 km ušetří ~8 min z 55 (~15 %).

1. fáze – polygon jako maska (~2–3 dny):
   - kreslení polygonu v mapě (vrcholy, úchyty jako u obdélníku), obdélník
     zůstává výchozí;
   - API: polygon v options, `bbox_wgs84` = obalový obdélník (zpětná
     kompatibilita);
   - PDAL ořez mračna polygonem (`filters.crop` s WKT);
   - ořez vektorů a objektů .omap polygonem (dnes `clip_ring` /
     `clip_polyline` jen na obdélník), ohraničení AOI v .omap jako polygon;
   - limit: plocha polygonu ≤ 36 km² a obalový obdélník ≤ ~60 km² (rastry).
2. fáze (podle potřeby): maskování rastrů, výběr jen nutných listů SM5,
   cache klíčovaná polygonem.

## Ze zpětné vazby mapaře (2026-10-08)

- Volba „Obsah mapy“: jen podklady + vrstevnice + budovy (+ cesty) vs. plná
  automatická mapa; plošné symboly (louky, zpevněné) mapař nepoužije.
- Vyhlazení schodovitých okrajů ploch z rastru.
- Vlastní šablona mapového klíče (nahrát .omap; import jde podle kódů značek)
  a výchozí ISSprOM 2019-2 rev. 6 (chybí např. trojúhelníky víceúrovňových
  staveb). Uživatel hledá úplnou sadu pro OOM.
  - Rozbor šablony ze školení kartografů (Mapovani_Zderaz.omap, 2026-10):
    ISOM 2017-2 1:15000 česky, 138 kódů shodných s naší; kódy jako „101.0“
    a přečíslované podvarianty (513.100, 520.4, 501.1, 203.101) → při použití
    normalizovat „.0“ + tabulka přemapování; chybí 206, 301, 412.1, 203.1/2.
    Navíc pomocné značky 1.x Ortofoto, 2.x LAS reliéf, 3.x pomocné – vhodné
    pro nejisté výstupy generátoru. Licence/šíření nejasné (asi ČSOS).
- OSM `natural=bare_rock` (např. [way/905065542](https://www.openstreetmap.org/way/905065542),
  50.0404N 14.3653E) mapovat na značku 214 – holá skála. HOTOVO (neotestováno
  na ostrém jobu): `bare_rock` jde jako 214 do .omap (sprint i les; MTBO
  214 nemá). Značku 206 z OSM nemapujeme, `scree` zůstává jen podklad.
  Zbývá ověřit na reálné mapě u way/905065542 a zvážit `scree`.
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
    zahrady, parkoviště, hřiště, bare_rock…) → při záloze chybí tyto objekty,
    a dlaždice se přesto zacachuje jako úplná;
  - HTTP 400 při > 50 000 uzlech (husté centrum) se neřeší – dlaždici půlit;
  - selhaná zrcadla si pamatovat v rámci jobu (neopakovat u každé dlaždice);
  - zalogovat, že záloha je neúplná.
  - Overpass s API klíčem (NextGIS, FairwayMapper, Geofabrik, Tracestrack,
    Overspan…) brzy prozkoumat; URL s klíčem číst z proměnné prostředí
    (např. `PODKLADARNA_OVERPASS_URL`) a zkoušet jako první, klíč nedávat do repa.

- Krok DEM z DMR 5G trval na NAS u 6×6 km 9 min (lokálně stejná pipeline
  21 s, u 5,4 km² na NAS 6 s) – zjistit příčinu při dalším velkém jobu.
