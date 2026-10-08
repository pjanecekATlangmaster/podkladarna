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

- Georef PNG 600 DPI je obří (6×6 km ≈ 25 000 × 24 000 px, neotevře se):
  výchozí 300 DPI, dlaždicový GeoTIFF s kompresí a náhledy, PNG jen volbou.
- Max. počet vrcholů u velkých ploch (louka přes celou mapu s tisíci vrcholy
  brzdí OCAD): rozřezat do mřížky (~250 m) / strop ~1–2 tis. vrcholů na objekt.
- Volba „Obsah mapy“: jen podklady + vrstevnice + budovy (+ cesty) vs. plná
  automatická mapa; plošné symboly (louky, zpevněné) mapař nepoužije.
- Vyhlazení schodovitých okrajů ploch z rastru.
- Vlastní šablona mapového klíče (nahrát .omap; import jde podle kódů značek)
  a výchozí ISSprOM 2019-2 rev. 6 (chybí např. trojúhelníky víceúrovňových
  staveb).
- Výška vegetace spíš jako rastrový podklad (CHM / hustota) než hotové plochy.
- Předvolby „podklady pro mapaře“ vs. „příprava na závod (embargo)“.

## Technické

- Krok DEM z DMR 5G trval na NAS u 6×6 km 9 min (lokálně stejná pipeline
  21 s, u 5,4 km² na NAS 6 s) – zjistit příčinu při dalším velkém jobu.
