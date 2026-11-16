# Zdroje dat po zemích

> Generováno z `configs/data_sources.yaml` skriptem `scripts/data_sources_doc.py` – needitovat ručně.
> Změnu zapiš do registru a dokument přegeneruj (`tests/test_data_sources.py` hlídá, že sedí).
> Kontrola jiným modelem: prompt v `docs/zdroje-dat-kontrola-prompt.md`.

Licence: 🟢 public = otevřená licence včetně komerčního použití (může vyžadovat citaci) · 🟠 omezený = podmínky, schválení, poplatek nebo nekomerční · ⚪ nejasná = licence neuvedena nebo nenalezena.
Přístup: volný = bez registrace; ⚠ e-mail / API klíč / účet / smlouva = je potřeba něco navíc.
Ověřeno: datum a způsob ověření licence; „neověřeno“ = převzato z podkladů projektu, na stránce poskytovatele neověřeno.

## Přehled zemí

| Země | Stav v Podkladárně | Výšky | Technická mapa | Zdrojů | S poznámkou / omezením |
|---|---|---|---|---|---|
| CZ Česko | live | `cz_openzu` | `cz_dtm` | 7 | 5 |
| SK Slovensko | off | `sk_ugkk` | `–` | 1 | 1 |
| NL Nizozemsko | beta | `nl_ahn` | `nl_bgt` | 4 | 3 |
| CH Švýcarsko | beta | `ch_swisstopo` | `ch_av` | 4 | 4 |
| AT Rakousko | beta | `at_bev` | `at_fmzk` | 3 | 3 |
| DE Německo – Berlín | beta | `de_berlin` | `de_sb` | 3 | 3 |
| FR Francie | beta | `fr_lidarhd` | `fr_pdv` | 4 | 3 |
| EE Estonsko | beta | `ee_maaamet` | `ee_etak` | 4 | 3 |
| IT Itálie (Trentino) | beta | `it_trentino` | `–` | 3 | 2 |
| BE Belgie (Flandry) | beta | `be_dhmv` | `be_grb` | 3 | 3 |
| FI Finsko | beta | `fi_mml` | `fi_hel` | 4 | 4 |
| DK Dánsko | kandidát | `–` | `–` | 1 | 1 |
| ES Španělsko | kandidát | `–` | `–` | 1 | 1 |
| LV Lotyšsko | kandidát | `–` | `–` | 1 | 1 |
| NO Norsko | kandidát | `–` | `–` | 1 | 1 |
| PL Polsko | kandidát | `–` | `–` | 1 | 1 |
| SE Švédsko | kandidát | `–` | `–` | 1 | 1 |
| SI Slovinsko | kandidát | `–` | `–` | 1 | 1 |
| UK Spojené království | kandidát | `–` | `–` | 1 | 1 |

## Všechny země (`*`)

| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |
|---|---|---|---|---|---|---|
| vektory, budovy | **OpenStreetMap přes Overpass API (6 zrcadel, záloha OSM API /map)**<br>OpenStreetMap contributors; zrcadla overpass-api.de, overpass.openstreetmap.fr, maps.mail.ru, private.coffee<br><https://overpass-api.de/api/interpreter> · [info](https://www.openstreetmap.org/copyright) | 🟢 public – [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/)<br>citace: „© OpenStreetMap contributors“ | volný | napojeno (kresba)<br>sonda `osm` | • Veřejná instance overpass-api.de žádá nejvýš ~10 000 dotazů a ~1 GB za den; při růstu provozu vlastní instance nebo placené zrcadlo (PODKLADARNA_OVERPASS_URLS).<br>• Zrcadla jsou nestabilní: maps.mail.ru 38 s nebo 504, private.coffee a kumi.systems vracely 500 (komentář v osm_paths.py).<br>• Záloha OSM API /map (api.openstreetmap.org) je určená hlavně pro editory; podmínky API usage policy pro čtení dat ověřit.<br>• ODbL: prodávaný .omap je odvozená databáze, kupující ji smí šířit dál (evropa-data-prehled kap. 5).<br>• Kvalita a úplnost OSM se liší po zemích i obcích (moře jen jako pobřežní čára, zahrady, hřiště bez surface). | 2026-11-16 overpass-api.de (limity); ODbL podle osm.org/copyright |
| mapa | **Dlaždice OSM (openstreetmap.fr, .org, .de) a OSM WMS terrestris**<br>OSM France, OSMF, FOSSGIS, terrestris<br><https://a.tile.openstreetmap.fr/osmfr/> · [info](https://operations.osmfoundation.org/policies/tiles/) | 🟠 omezený – [data ODbL, dlaždice podle podmínek provozovatele](https://operations.osmfoundation.org/policies/tiles/)<br>citace: „© OpenStreetMap contributors (terrestris: © OpenStreetMap contributors, © Natural Earth Data, © GEBCO, © terrestris)“ | volný | napojeno (šablona) | • Tile policy OSMF zakazuje na tile.openstreetmap.org předstahování a offline použití; obrázek OSM do ZIPu (reference_layers) je z pohledu této politiky problém – ověřit, zda se tile.openstreetmap.org reálně používá jen jako online šablona, případně ho z offline větve vyřadit.<br>• Vyžaduje vlastní User-Agent (generické knihovny blokuje).<br>• openstreetmap.de občas vrací 404 na okrajové dlaždice (GDAL padá) – komentář v kódu. | 2026-11-16 tile policy OSMF; terrestris produktová stránka (závazné podmínky nenalezeny) |
| družicové snímky | **Sentinel-2 L2A (Earth Search STAC na AWS)**<br>ESA / Copernicus, katalog Element 84<br><https://earth-search.aws.element84.com/v1/search> · [info](https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice) | 🟢 public – [Copernicus Sentinel data legal notice (volné použití)](https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice)<br>citace: „Contains modified Copernicus Sentinel data [rok]“ | volný | napojeno (šablona)<br>sonda `sentinel` | • Rozlišení 10 m, jen orientační šablona. | 2026-11-16 legal notice Copernicus (podmínky katalogu Element 84 neověřeny) |
| výšky (DTM/DSM) | **FABDEM / FathomDEM 30 m**<br>Fathom<br><https://www.fathom.global/> | 🟠 omezený – [CC BY-NC-SA 4.0 (nekomerční)](https://creativecommons.org/licenses/by-nc-sa/4.0/) | volný | nevhodné (výšky) | • Nekomerční licence – při prodeji map nelze; 30 m na vrstevnice nestačí. | neověřeno (evropa-data-prehled kap. 4) |
| výšky (DTM/DSM) | **Copernicus DEM GLO-30 (COG na AWS)**<br>ESA / Copernicus<br><https://copernicus-dem-30m.s3.amazonaws.com/> · [info](https://spacedata.copernicus.eu/) | 🟢 public – [zdarma i komerčně, citace](https://spacedata.copernicus.eu/)<br>citace: „© DLR e.V. 2010–2014 and © Airbus Defence and Space GmbH 2014–2018 provided under COPERNICUS by the European Union and ESA“ | volný | nevhodné (výšky) | • 30 m DSM (ne DTM) – na OB vrstevnice nepoužitelné. | neověřeno |

## CZ – Česko

Stav `live`, CRS `EPSG:5514`. Citace v mapě: „Podklad: Podkladárna · ČÚZK · OSM, [rok]“.

| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |
|---|---|---|---|---|---|---|
| LiDAR mračno, výšky (DTM/DSM) | **DMR 5G a DMP OK (LAZ po listech SM5)**<br>ČÚZK, OpenZU<br><https://openzu.cuzk.gov.cz/opendata/> · [info](https://www.cuzk.gov.cz/Uvod/Produkty-a-sluzby/Otevrena-data/Otevrena-data-zakladni-informace.aspx) | 🟢 public – [CC BY 4.0](https://www.cuzk.gov.cz/Uvod/Produkty-a-sluzby/Otevrena-data/Otevrena-data-zakladni-informace.aspx)<br>citace: „Data ČÚZK – CC BY 4.0 (u upraveného díla popis úpravy)“ | volný | napojeno (výšky)<br>sonda `lidar` | • DMR 5G má hustotu ~0,5 m mezi body: drobné balvany a skalky pod ~1 m nevyčteme.<br>• Listy jsou velké, proto cache listů se stropem LIDAR_CACHE_MAX_GB a kontrolou neúplného LAZ (3.9.0, 3.56.1).<br>• DMP OK je z obrazové korelace (bez prostupnosti porostu jako u mračna z laseru). | 2026-11-16 stránka ČÚZK Otevřená data |
| technická mapa | **Digitální technická mapa krajů (JVF balíky ZPS) + WMS náhledy**<br>ČÚZK, IS DMVS, servery krajů<br><https://dmvs.cuzk.gov.cz/api/rest> · [info](https://dmvs.cuzk.gov.cz/) | ⚪ nejasná – [veřejné sady „bez podmínek“, konkrétní licence neuvedena](https://dmvs.cuzk.gov.cz/)<br>citace: „ČÚZK / IS DMVS“ | volný | napojeno (kresba)<br>sonda `dtm` | • Odstávky DMVS (výpadek 503 2026-11-12, plánovaná odstávka kvůli JVF DTM 1.5 2026-11-13); job pak bere balíky z cache.<br>• Neúplné obce: např. Jihlava má v ZPS_OPL jen ~8 % budov (incomplete_opl_units).<br>• Ploty s neznámým původem (ZpusobPorizeniZPS = 99) – filtr filter_dubious_fences.<br>• Balíky jsou měsíční export, čerstvý objekt z portálu v nich být nemusí. | neověřeno (převzato z dtm-kraje/dtm-vysocina-a-kraje.md, 2026-10-26) |
| vektory | **ZABAGED polohopis (ArcGIS REST)**<br>ČÚZK<br><https://ags.cuzk.gov.cz/arcgis/rest/services/ZABAGED_POLOHOPIS/MapServer> · [info](https://www.cuzk.gov.cz/Uvod/Produkty-a-sluzby/Otevrena-data/Otevrena-data-zakladni-informace.aspx) | 🟢 public – [CC BY 4.0](https://www.cuzk.gov.cz/Uvod/Produkty-a-sluzby/Otevrena-data/Otevrena-data-zakladni-informace.aspx)<br>citace: „ZABAGED® © ČÚZK – CC BY 4.0“ | volný | napojeno (kresba)<br>sonda `zabaged` | • Projekt ZABAGED postupně opouští (OSM základ, DTM doplňuje); místy zastaralé (sady, areály). | 2026-11-16 stránka ČÚZK Otevřená data |
| katastr | **RÚIAN (obce, parcely)**<br>ČÚZK<br><https://ags.cuzk.gov.cz/arcgis/rest/services/RUIAN/MapServer> · [info](https://www.cuzk.gov.cz/Uvod/Produkty-a-sluzby/Otevrena-data/Otevrena-data-zakladni-informace.aspx) | 🟢 public – [CC BY 4.0](https://www.cuzk.gov.cz/Uvod/Produkty-a-sluzby/Otevrena-data/Otevrena-data-zakladni-informace.aspx)<br>citace: „RÚIAN © ČÚZK – CC BY 4.0“ | volný | napojeno (kresba)<br>sonda `ruian` | – | 2026-11-16 stránka ČÚZK Otevřená data |
| ortofoto | **Ortofoto ČR (aktuální WMS a archiv 2003–2005)**<br>ČÚZK<br><https://ags.cuzk.gov.cz/arcgis1/services/ORTOFOTO/MapServer/WMSServer> · [info](https://www.cuzk.gov.cz/Uvod/Produkty-a-sluzby/Otevrena-data/Otevrena-data-zakladni-informace.aspx) | 🟢 public – [CC BY 4.0](https://www.cuzk.gov.cz/Uvod/Produkty-a-sluzby/Otevrena-data/Otevrena-data-zakladni-informace.aspx)<br>citace: „Ortofoto © ČÚZK – CC BY 4.0“ | volný | napojeno (kresba)<br>sonda `ortofoto` | • Používá se i ke kresbě (stromy na dlažbě, herní prvky stínem, zeleň v ulicích), ne jen jako šablona. | 2026-11-16 stránka ČÚZK Otevřená data (archivní WMS licenci zvlášť neověřeno) |
| stínování, mapa, katastr | **Stínování DMR 5G a DMP, ZTM, katastrální mapa (WMS a DXF)**<br>ČÚZK<br><https://ags.cuzk.gov.cz/arcgis2/services/dmr5g/ImageServer/WMSServer> · [info](https://www.cuzk.gov.cz/Uvod/Produkty-a-sluzby/Otevrena-data/Otevrena-data-zakladni-informace.aspx) | 🟢 public – [CC BY 4.0](https://www.cuzk.gov.cz/Uvod/Produkty-a-sluzby/Otevrena-data/Otevrena-data-zakladni-informace.aspx)<br>citace: „© ČÚZK“ | volný | napojeno (šablona)<br>sonda `katastr` | • ČÚZK 2026-11 přejmenovala službu dmp_obrazova_korelace na dmp (stará adresa 404) – adresy služeb se mění bez ohlášení. | 2026-11-16 stránka ČÚZK Otevřená data (katastrální mapa zvlášť neověřeno) |
| vektory | **Památné stromy AOPK ČR**<br>AOPK ČR<br><https://gis.nature.cz/arcgis/rest/services> · [info](https://data.nature.cz/) | 🟢 public – [CC BY 4.0](https://data.nature.cz/)<br>citace: „© AOPK ČR (CC BY 4.0)“ | volný | napojeno (kresba) | – | neověřeno (CC BY 4.0 obecně pro data AOPK podle prezentace 2020; u sady stromů na portálu neověřeno) |

## SK – Slovensko

Stav `off`, CRS `EPSG:5514`. Citace v mapě: „Base map: Omapic · ÚGKK SR · OSM, [year]“.

| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |
|---|---|---|---|---|---|---|
| výšky (DTM/DSM) | **DMR 5.0 1 m (INSPIRE WCS)**<br>ÚGKK SR<br><https://inspirews.skgeodesy.sk/geoserver/el/ows> · [info](https://www.geoportal.sk/sk/zbgis/lls-dmr/) | 🟢 public – [CC BY 4.0](https://www.geoportal.sk/sk/zbgis/lls-dmr/)<br>citace: „DMR 5.0 © ÚGKK SR – CC BY 4.0“ | volný | vypnuto (výšky) | • Model povrchu DMP 1.0 a mračno jen exportem z portálu (mračno do 2 km² na export, DMP na e-mail) → země vypnutá (bez povrchu nezařazujeme).<br>• Servery geoportal.sk / zbgis z cloudu nedostupné (možná blokace podle země). | neověřeno (portál ÚGKK z cloudu nedostupný, převzato z evropa-data-prehled) |

## NL – Nizozemsko

Stav `beta`, CRS `EPSG:28992`. Citace v mapě: „Base map: Omapic · AHN · Kadaster · Beeldmateriaal.nl · OSM, [year]“.

| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |
|---|---|---|---|---|---|---|
| výšky (DTM/DSM) | **AHN DTM a DSM 0,5 m (PDOK WCS) + WMS stínování**<br>Rijkswaterstaat / Het Waterschapshuis (AHN), PDOK<br><https://service.pdok.nl/rws/ahn/wcs/v1_0> · [info](https://www.ahn.nl/) | 🟢 public – [CC0 1.0 (AHN1–4), novější snímkování CC BY 4.0](https://data.overheid.nl/dataset/47567-actueel-hoogtebestand-nederland--ahn--dtm)<br>citace: „Bron: AHN“ | volný | napojeno (výšky)<br>sonda `nl_ahn` | • Vegetace jen z rastru DSM − DTM; v mračnu je vegetace ve třídě 1 (nezařazené), ne zvlášť. | 2026-11-16 data.overheid.nl (AHN4 CC0); CC BY u AHN5+ jen ze sekundárního zdroje – neověřeno |
| LiDAR mračno | **AHN4–6 mračno bodů (COPC)**<br>Rijkswaterstaat / Het Waterschapshuis<br><https://www.ahn.nl/> | 🟢 public – [CC0 1.0 / CC BY 4.0](https://www.ahn.nl/)<br>citace: „Bron: AHN“ | volný | kandidát (výšky) | • Vegetace ve třídě 1 spolu s ostatním nezařazeným – porost z mračna jen s vlastní klasifikací. | neověřeno |
| technická mapa | **BGT – Basisregistratie Grootschalige Topografie (OGC API Features, WMTS)**<br>Kadaster, PDOK<br><https://api.pdok.nl/lv/bgt/ogc/v1> · [info](https://www.pdok.nl/introductie/-/article/basisregistratie-grootschalige-topografie-bgt-) | 🟢 public – [CC0 1.0](https://www.pdok.nl/introductie/-/article/basisregistratie-grootschalige-topografie-bgt-)<br>citace: „BGT – CC0 1.0“ | volný | napojeno (kresba)<br>sonda `nl_bgt` | • HEAD na API vrací 404, sonda proto dělá GET na kolekci pand. | neověřeno (převzato z countries.yaml a README NL) |
| ortofoto, katastr, mapa | **Luchtfoto (Beeldmateriaal), Kadastrale kaart, BRT TOP10NL (PDOK WMS)**<br>Beeldmateriaal Nederland, Kadaster, PDOK<br><https://service.pdok.nl/hwh/luchtfotorgb/wms/v1_0> · [info](https://www.beeldmateriaal.nl/) | 🟢 public – [CC BY 4.0](https://www.beeldmateriaal.nl/)<br>citace: „Bron: Beeldmateriaal.nl; Bron: Kadaster“ | volný | napojeno (šablona)<br>sonda `nl_luchtfoto`, `nl_kadaster` | – | neověřeno (převzato z countries.yaml) |

## CH – Švýcarsko

Stav `beta`, CRS `EPSG:2056`. Citace v mapě: „Base map: Omapic · © swisstopo · AV cantons (geodienste.ch) · OSM, [year]“.

| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |
|---|---|---|---|---|---|---|
| výšky (DTM/DSM) | **swissALTI3D (terén) a swissSURFACE3D Raster (povrch) 0,5 m, STAC + COG**<br>swisstopo<br><https://data.geo.admin.ch/api/stac/v0.9> · [info](https://www.swisstopo.admin.ch/en/terms-of-use-free-geodata-and-geoservices) | 🟢 public – [swisstopo OGD (volné i komerční použití, povinná citace)](https://www.swisstopo.admin.ch/en/terms-of-use-free-geodata-and-geoservices)<br>citace: „© swisstopo“ | volný | napojeno (výšky)<br>sonda `ch_swisstopo` | • swisstopo si vyhrazuje omezit přístup při nadměrném používání služeb (pak smlouva). | 2026-11-16 stránka swisstopo Terms of use |
| LiDAR mračno | **swissSURFACE3D mračno (LAS, ~12 b/m², třída 3 vegetace)**<br>swisstopo<br><https://data.geo.admin.ch/api/stac/v0.9> · [info](https://www.swisstopo.admin.ch/en/terms-of-use-free-geodata-and-geoservices) | 🟢 public – [swisstopo OGD](https://www.swisstopo.admin.ch/en/terms-of-use-free-geodata-and-geoservices)<br>citace: „© swisstopo“ | volný | kandidát (výšky) | • Velké soubory: 1 km² = ZIP 125 MB, rozbaleno 338 MB (nelze číst jen výřez). | 2026-11-16 stránka swisstopo Terms of use |
| technická mapa | **AV – amtliche Vermessung kantonů (WFS geodienste.ch)**<br>kantony, geodienste.ch<br><https://geodienste.ch/db/av_0/deu> · [info](https://geodienste.ch/services/av) | 🟠 omezený – [20 kantonů + FL volně („Freie Nutzung“, většinou povinná citace); JU, LU, NE, NW, OW, VD jen se schválením](https://geodienste.ch/services/av)<br>citace: „Quelle: Kantone, geodienste.ch“ | volný | napojeno (kresba)<br>sonda `ch_av` | • JU, LU, NE, NW, OW, VD vyžadují schválení (JU, LU, NE, NW i smlouvu, JU od 10 ha poplatek) → technical_map_gaps, tam jen OSM.<br>• Podmínky se liší po kantonech; GR, SG a další odkazují na vlastní kantonální podmínky.<br>• Plochy plotů AV nevede. | 2026-11-16 stránka geodienste.ch/services/av |
| ortofoto, mapa, katastr, stínování | **SWISSIMAGE, národní mapa, stínování a AV (WMS swisstopo)**<br>swisstopo<br><https://wms.geo.admin.ch/> · [info](https://www.swisstopo.admin.ch/en/terms-of-use-free-geodata-and-geoservices) | 🟢 public – [swisstopo OGD](https://www.swisstopo.admin.ch/en/terms-of-use-free-geodata-and-geoservices)<br>citace: „© swisstopo“ | volný | napojeno (šablona)<br>sonda `ch_wms` | • Dotaz 1 × 1 px vracel 500 (opravená sonda). | 2026-11-16 stránka swisstopo Terms of use |

## AT – Rakousko

Stav `beta`, CRS `EPSG:25833`. Citace v mapě: „Base map: Omapic · BEV · www.basemap.at · Stadt Wien – data.wien.gv.at · OSM, [year]“.

| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |
|---|---|---|---|---|---|---|
| výšky (DTM/DSM) | **ALS DTM a DSM 1 m, roční mozaika (COG 50 km, EPSG:3035)**<br>BEV – Bundesamt für Eich- und Vermessungswesen<br><https://data.bev.gv.at/download/ALS/> · [info](https://data.bev.gv.at/) | 🟢 public – [CC BY 4.0](https://data.bev.gv.at/)<br>citace: „Datenquelle: BEV – Bundesamt für Eich- und Vermessungswesen“ | volný | napojeno (výšky)<br>sonda `at_bev` | • Mračno bodů otevřené není → porost jen z DSM − DTM (slabší beta, falešný porost na mostech a střechách řeší veto z OSM).<br>• DTM se obnovuje v intervalu až 10 let, Stichtag mozaiky není datum letu.<br>• COG dlaždice má ~10 GB (čte se jen okno přes HTTP range); přesnost výšek ±0,5 m, ve velehorách hůř. | 2026-11-16 metadata BEV v INSPIRE Geoportal (CC BY 4.0, přístup bez omezení); stránka BEV přímo neověřena |
| technická mapa | **FMZK – Flächen-Mehrzweckkarte Stadt Wien (WFS)**<br>Stadt Wien (MA 41)<br><https://data.wien.gv.at/daten/geo> · [info](https://www.wien.gv.at/stadtentwicklung/stadtvermessung/geodaten/fmzk/) | 🟢 public – [CC BY 4.0](https://www.wien.gv.at/stadtentwicklung/stadtvermessung/geodaten/fmzk/)<br>citace: „Datenquelle: Stadt Wien – data.wien.gv.at“ | volný | napojeno (kresba)<br>sonda `at_fmzk` | • Jen Vídeň (technical_map_area).<br>• Server převádí do jiných EPSG bez posunu data MGI (~290 m vedle) → stahovat v nativním EPSG:31256 a převádět PROJ. | 2026-11-16 stránka FMZK wien.gv.at |
| ortofoto, mapa, stínování | **basemap.at (ortofoto, mapa, stínování, TMS) a Luftbild Wien (WMTS)**<br>basemap.at (spolkové země), Stadt Wien<br><https://mapsneu.wien.gv.at/basemap> · [info](https://basemap.at/en) | 🟢 public – [CC BY 4.0](https://basemap.at/en)<br>citace: „Datenquelle: basemap.at (s odkazem na www.basemap.at)“ | volný | napojeno (šablona)<br>sonda `at_basemap`, `at_luftbild` | • Citace v ZIPu a .omap uvádí „www.basemap.at“ (citace i řádek zdrojů v countries.yaml, 2026-11-16). | 2026-11-16 stránka basemap.at (OGD Austria, CC BY 4.0, „Data source: basemap.at“ s odkazem) |

## DE – Německo – Berlín

Stav `beta`, CRS `EPSG:25833`. Citace v mapě: „Base map: Omapic · Geoportal Berlin · OSM, [year]“.

| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |
|---|---|---|---|---|---|---|
| výšky (DTM/DSM) | **ATKIS DGM1 a DOM1 Berlín (ALS 2021), 1 m, XYZ po 2 km**<br>Senatsverwaltung Berlin, Geoportal Berlin (GDI Berlin)<br><https://gdi.berlin.de/data/dgm1/atom/> · [info](https://gdi.berlin.de/geonetwork/srv/api/records/b0485f06-a39e-3cc2-b5e0-be7fcd425dab) | 🟢 public – [Datenlizenz Deutschland – Zero 2.0](https://www.govdata.de/dl-de/zero-2-0)<br>citace: „nepovinná; uvádíme Geoportal Berlin“ | volný | napojeno (výšky)<br>sonda `de_berlin` | • Zrcadla katalogu (gdk.gdi-de.org, INSPIRE) mají u DGM ještě starší „Namensnennung 2.0“; platí záznam poskytovatele (Zero 2.0). Citaci „Geoportal Berlin“ uvádíme i tak – při případném návratu k BY 2.0 vyhovuje.<br>• Novější bDOM 2026 ze snímků je bez listí (les vycházel jako louka) → bere se DOM1 2021.<br>• Mračno jen v ZIPech po desítkách GB – nepoužitelné. | 2026-11-16 katalog GDI Berlin: ATKIS DGM (b0485f06…, publikováno 2026-01-29) i DOM (b0e8e83e…) uvádějí Zero 2.0 |
| technická mapa, budovy, ortofoto | **Straßenbefahrung 2014, budovy ALKIS a TrueDOP (WFS / WMS GDI Berlin)**<br>Geoportal Berlin<br><https://gdi.berlin.de/services/wfs/strassenbefahrung> · [info](https://gdi.berlin.de/) | 🟢 public – [Datenlizenz Deutschland – Zero 2.0](https://www.govdata.de/dl-de/zero-2-0)<br>citace: „nepovinná; uvádíme Geoportal Berlin“ | volný | napojeno (kresba)<br>sonda `de_sb`, `de_truedop` | • Straßenbefahrung je z roku 2014 – změny ulic po 2014 chybí. | neověřeno (licence Straßenbefahrung v katalogu nenalezena; převzato z README DE) |
| výšky (DTM/DSM), LiDAR mračno | **DGM1 a LAZ dalších spolkových zemí (NRW WCS + index.json, BY metalink, HE, SN, TH, BB …)**<br>zemské geodetické úřady<br><https://www.opengeodata.nrw.de/produkte/geobasis/hm/> · [info](https://www.govdata.de/) | 🟢 public – [dl-de/by-2.0, dl-de/zero-2.0, CC BY 4.0 (podle země)](https://www.govdata.de/)<br>citace: „podle země“ | volný | kandidát (výšky) | • Každá spolková země jiné rozhraní → až 16 adaptérů.<br>• LAZ ~70–110 MB/km² po celých 1km dlaždicích.<br>• NI, MV, SH jen DSM (bez mračna). | neověřeno (evropa-data-prehled 2026-11-14: NRW WCS a seznamy LAZ BY a NRW staženy) |

## FR – Francie

Stav `beta`, CRS `EPSG:2154`. Citace v mapě: „Base map: Omapic · IGN LiDAR HD · Ville de Paris · OSM, [year]“.

| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |
|---|---|---|---|---|---|---|
| výšky (DTM/DSM) | **LiDAR HD MNT a MNS 0,5 m (WMS-R Géoplateforme)**<br>IGN<br><https://data.geopf.fr/wms-r/wms> · [info](https://geoservices.ign.fr/lidarhd) | 🟢 public – [Licence Ouverte Etalab 2.0](https://www.etalab.gouv.fr/licence-ouverte-open-licence/)<br>citace: „© IGN“ | volný | napojeno (výšky)<br>sonda `fr_lidarhd` | • Vychází postupně, díry (Lille) → sonda pokrytí, pod 50 % výřez odmítnut; vojenské zóny ZICAD bez dat (nodata).<br>• WMS-R občas vrací 502 (až 4 pokusy); IGN zavedl limity počtu dotazů na WMS-R / WMS-V / WFS.<br>• Dlaždice 2 × 2 km ~64 MB, ~20 s. | 2026-11-16 dokumentace IGN k LiDAR HD (Etalab 2.0) přes vyhledávání; stránka geoservices.ign.fr přesměrovává na cartes.gouv.fr |
| LiDAR mračno | **LiDAR HD mračno COPC (~10–30 b/m², třídy 1–5)**<br>IGN<br><https://data.geopf.fr/wfs/ows> · [info](https://geoservices.ign.fr/lidarhd) | 🟢 public – [Licence Ouverte Etalab 2.0](https://www.etalab.gouv.fr/licence-ouverte-open-licence/)<br>citace: „© IGN“ | volný | kandidát (výšky) | • List COPC ~235 MB, ale jde číst jen výřez přes HTTP range (ověřeno 100 × 100 m). | 2026-11-16 dokumentace IGN (Etalab 2.0) |
| technická mapa | **Plan de voirie (chaussées, trottoirs, escaliers) Ville de Paris**<br>Ville de Paris – opendata.paris.fr<br><https://opendata.paris.fr/api/explore/v2.1/catalog/datasets/> · [info](https://opendata.paris.fr/explore/dataset/plan-de-voirie-chaussees/information/) | 🟢 public – [ODbL](https://opendata.paris.fr/explore/dataset/plan-de-voirie-chaussees/information/)<br>citace: „© Ville de Paris – opendata.paris.fr (ODbL)“ | volný | napojeno (kresba)<br>sonda `fr_pdv` | • Jen obec Paříž (technical_map_area); ODbL – upravená data musí zůstat pod ODbL (Podkladárna je nešíří, jen podle nich kreslí). | neověřeno (licence u sady Chaussées nenalezena; ODbL u příbuzných sad Plan de voirie) |
| ortofoto, mapa, katastr | **BD ORTHO, Plan IGN, Parcellaire Express (WMS Géoplateforme)**<br>IGN<br><https://data.geopf.fr/wms-r/wms> · [info](https://geoservices.ign.fr/) | 🟢 public – [Licence Ouverte Etalab 2.0](https://www.etalab.gouv.fr/licence-ouverte-open-licence/)<br>citace: „© IGN – Géoplateforme“ | volný | napojeno (šablona)<br>sonda `fr_ortho` | – | neověřeno (převzato z countries.yaml) |

## EE – Estonsko

Stav `beta`, CRS `EPSG:3301`. Citace v mapě: „Base map: Omapic · Maa- ja Ruumiamet · OSM, [year]“.

| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |
|---|---|---|---|---|---|---|
| výšky (DTM/DSM) | **DTM 1 m (listy 5 km) a DSM 1 m (listy 1 km), GeoTIFF**<br>Maa- ja Ruumiamet<br><https://geoportaal.maaruum.ee/index.php> · [info](https://geoportaal.maaamet.ee/opendata-licence) | 🟢 public – [otevřená licence Maa- ja Ruumiamet (i komerčně, citace se zdrojem a datem dat)](https://geoportaal.maaamet.ee/opendata-licence)<br>citace: „Maa- ja Ruumiamet [rok dat]“ | volný | napojeno (výšky)<br>sonda `ee_elevation` | • List DTM ~75 MB (čte se jen okno přes /vsicurl); MTBO 25 km² odhad ~240 MB výšek. | 2026-11-16 stránka licence Maa-amet (geoportaal.maaamet.ee) přes vyhledávání; aktuální verze textu bez data |
| LiDAR mračno | **LAZ 1.4 mračna po 1 km (víc ročníků)**<br>Maa- ja Ruumiamet<br><https://geoportaal.maaamet.ee/eng/Download-3D-data-p837.html> · [info](https://geoportaal.maaamet.ee/opendata-licence) | 🟢 public – [otevřená licence Maa- ja Ruumiamet](https://geoportaal.maaamet.ee/opendata-licence)<br>citace: „Maa- ja Ruumiamet [rok dat]“ | volný | kandidát (výšky) | • Celé listy 1 km, objem změřit před napojením. | neověřeno |
| technická mapa | **ETAK – Eesti topograafia andmekogu (WFS)**<br>Maa- ja Ruumiamet<br><https://gsavalik.envir.ee/geoserver/etak/ows> · [info](https://geoportaal.maaamet.ee/opendata-licence) | 🟢 public – [otevřená licence Maa- ja Ruumiamet](https://geoportaal.maaamet.ee/opendata-licence)<br>citace: „Maa- ja Ruumiamet [rok dat]“ | volný | napojeno (kresba)<br>sonda `ee_etak` | • EPSG:3301 má osy sever–východ: WMS 1.3.0 vracela prázdné šablony, dokud se nepřehodily osy (opraveno). | 2026-11-16 stránka licence Maa-amet přes vyhledávání |
| ortofoto, mapa, katastr | **Ortofoto EESTIFOTO, põhikaart, katastr (WMS)**<br>Maa- ja Ruumiamet<br><https://kaart.maaamet.ee/wms/fotokaart> · [info](https://geoportaal.maaamet.ee/opendata-licence) | 🟢 public – [otevřená licence Maa- ja Ruumiamet](https://geoportaal.maaamet.ee/opendata-licence)<br>citace: „Maa- ja Ruumiamet [rok dat]“ | volný | napojeno (šablona)<br>sonda `ee_ortho` | – | 2026-11-16 stránka licence Maa-amet přes vyhledávání |

## IT – Itálie (Trentino)

Stav `beta`, CRS `EPSG:25832`. Citace v mapě: „Base map: Omapic · Provincia autonoma di Trento · OSM, [year]“.

| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |
|---|---|---|---|---|---|---|
| výšky (DTM/DSM) | **LiDAR PAT 2014/2018 – DTM a DSM first 0,5 m (dlaždice 500 m, služba merge STEM)**<br>Provincia autonoma di Trento (SIAT)<br><https://siat.provincia.tn.it/stem/services> · [info](https://www.provincia.tn.it/Argomenti/Ambiente/Geologia) | 🟢 public – [CC BY 4.0](https://www.provincia.tn.it/Argomenti/Ambiente/Geologia)<br>citace: „© Provincia autonoma di Trento“ | volný | napojeno (výšky)<br>sonda `it_elevation` | • Díry ~320 km² (5 % provincie, největší Piné / Baselga ~100 km²).<br>• Největší objem z bet: ~15 MB/km² (les 3,1 km² = 40 dlaždic, 84 MB); asynchronní služba merge.<br>• Data 2014 – novější změny porostu chybí. | neověřeno (u sady 2014/2018 licence v metadatech nenalezena; CC BY 4.0 uvádějí jiné LiDAR sady PAT) |
| ortofoto, mapa | **Ortofoto a Carta Tecnica Provinciale (WMS SIAT)**<br>Provincia autonoma di Trento<br><https://siat.provincia.tn.it/geoserver/stem/wms> · [info](https://siat.provincia.tn.it/) | 🟢 public – [CC BY 4.0](https://siat.provincia.tn.it/)<br>citace: „© Provincia autonoma di Trento“ | volný | napojeno (šablona)<br>sonda `it_ortho` | – | neověřeno (převzato z countries.yaml) |
| výšky (DTM/DSM) | **Jižní Tyrolsko DTM a DSM 2,5 m (0,5 m jen sídla), WCS**<br>Provincia autonoma di Bolzano<br><https://geoservices9.civis.bz.it/geoserver/p_bz-Elevation/ows> · [info](https://geokatalog.buergernetz.bz.it/) | ⚪ nejasná – [neověřeno](https://geokatalog.buergernetz.bz.it/) | volný | kandidát (výšky) | • Les jen 2,5 m (porosty hrubě); mimo Trentino Petr celou Itálii zatím nechce (2026-11-15). | neověřeno |

## BE – Belgie (Flandry)

Stav `beta`, CRS `EPSG:31370`. Citace v mapě: „Base map: Omapic · Digitaal Vlaanderen · OSM, [year]“.

| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |
|---|---|---|---|---|---|---|
| výšky (DTM/DSM) | **DHMV II DTM a DSM 1 m (WCS, LiDAR 2013–2015)**<br>Digitaal Vlaanderen<br><https://geo.api.vlaanderen.be/DHMV/wcs> · [info](https://www.vlaanderen.be/datavindplaats/catalogus/wmts-digitaal-hoogtemodel-vlaanderen) | 🟢 public – [Modellicentie Gratis Hergebruik (podle countries.yaml); WMTS DHMV vede „Gebruiksrecht geografische webdiensten“](https://www.vlaanderen.be/digitaal-vlaanderen/onze-diensten-en-platformen/open-data/voorwaarden-voor-het-hergebruik-van-overheidsinformatie/modellicentie-gratis-hergebruik)<br>citace: „© Digitaal Vlaanderen“ | volný | napojeno (výšky)<br>sonda `be_elevation` | • LiDAR 2013–2015 bez listí (zima): porost listnáčů vychází nižší, mladé porosty po 2015 chybí.<br>• Mračno nejspíš po mapových listech – automatický přístup bez účtu neověřen. | neověřeno (licence přímo u sady DHMV II nenalezena) |
| technická mapa | **GRB – Grootschalig Referentie Bestand (WFS)**<br>Digitaal Vlaanderen<br><https://geo.api.vlaanderen.be/GRB/wfs> · [info](https://www.vlaanderen.be/digitaal-vlaanderen/onze-diensten-en-platformen/basiskaart-vlaanderen-grb/grb-webdiensten) | 🟢 public – [Gratis Open Data Licentie Vlaanderen v1.02](https://assets.vlaanderen.be/image/upload/v1681976168/GRB_open_data_licentie_fwbc3o.pdf)<br>citace: „Bron: Grootschalig Referentie Bestand Vlaanderen, Digitaal Vlaanderen“ | volný | napojeno (kresba)<br>sonda `be_grb` | • Citace v countries.yaml má pro GRB vlastní řádek „Bron: Grootschalig Referentie Bestand Vlaanderen, Digitaal Vlaanderen (Gratis Open Data Licentie Vlaanderen v1.02)“ (2026-11-16).<br>• Brusel GRB nemá (technical_map_gaps, jen OSM); Valonsko jiné zdroje (PICC), nezapnuto. | 2026-11-16 licence GRB (PDF Digitaal Vlaanderen, čl. 4: citace podle poskytovatele) a stránka GRB (předepsaná citace) |
| ortofoto, mapa, katastr | **Ortofoto OMW, GRB basiskaart, katastr Adpf (WMS)**<br>Digitaal Vlaanderen<br><https://geo.api.vlaanderen.be/OMW/wms> · [info](https://www.vlaanderen.be/digitaal-vlaanderen/onze-diensten-en-platformen/open-data/voorwaarden-voor-het-hergebruik-van-overheidsinformatie) | 🟢 public – [Modellicentie Gratis Hergebruik](https://www.vlaanderen.be/digitaal-vlaanderen/onze-diensten-en-platformen/open-data/voorwaarden-voor-het-hergebruik-van-overheidsinformatie/modellicentie-gratis-hergebruik)<br>citace: „© Digitaal Vlaanderen“ | volný | napojeno (šablona)<br>sonda `be_ortho` | • WMS vyžaduje prázdný parametr STYLES. | neověřeno (převzato z countries.yaml) |

## FI – Finsko

Stav `beta`, CRS `EPSG:3067`. Citace v mapě: „Base map: Omapic · MML · Metsäkeskus · Helsingin kaupunki · OSM, [year]“.

| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |
|---|---|---|---|---|---|---|
| výšky (DTM/DSM) | **Korkeusmalli 2 m (zrcadlo Funet, listy 6 km)**<br>Maanmittauslaitos (MML), zrcadlo Funet (CSC)<br><https://mirrors.nic.funet.fi/index/geodata/mml/dem2m/> · [info](https://www.maanmittauslaitos.fi/en/opendata-licence-cc40) | 🟢 public – [CC BY 4.0 (NLS open data licence)](https://www.maanmittauslaitos.fi/en/opendata-licence-cc40)<br>citace: „© Maanmittauslaitos“ | volný | napojeno (výšky)<br>sonda `fi_elevation` | • Přímá rozhraní MML bez klíče vrací 401 → bere se zrcadlo Funet, které není oficiální služba MML; při jeho výpadku je potřeba bezplatný API klíč MML.<br>• Jen 2 m; husté mračno 5 b/m² je placené (min. 26,69 € za objednávku, silná autentizace), otevřené jen 0,5 b/m². | 2026-11-16 stránka produktu MML (licence CC 4.0) přes vyhledávání |
| výška porostu | **Latvusmalli – výška porostu 1 m (listy)**<br>Suomen metsäkeskus<br><https://avoin.metsakeskus.fi/aineistot/Latvusmalli/> · [info](https://www.metsakeskus.fi/) | 🟢 public – [CC BY 4.0](https://www.metsakeskus.fi/)<br>citace: „© Suomen metsäkeskus“ | volný | napojeno (výšky)<br>sonda `fi_chm` | • Metsäkeskus mění distribuci otevřených dat (memo 2025-11) – adresa avoin.metsakeskus.fi se může změnit.<br>• DSM se skládá jako DTM + latvusmalli (ne z mračna). | neověřeno (CC BY 4.0 obecně pro otevřená data Metsäkeskus; u latvusmalli zvlášť nenalezeno; distribuce se 2025–26 mění) |
| technická mapa, ortofoto, mapa | **YLRE, budovy, ortofoto a kantakartta Helsinek (WFS / WMS kartta.hel.fi)**<br>Helsingin kaupunki<br><https://kartta.hel.fi/ws/geoserver/avoindata/wfs> · [info](https://hri.fi/data/en/dataset/helsingin-kantakartta) | 🟢 public – [CC BY 4.0](https://hri.fi/data/en/dataset/helsingin-kantakartta)<br>citace: „© Helsingin kaupunki“ | volný | napojeno (kresba)<br>sonda `fi_ylre` | • Jen Helsinky (technical_map_area).<br>• Ortofoto Helsinek se při stahování občas nestáhne („Requested rectangle bounds is not contained…“), ručně ano; od 3.55.0 jeden opakovaný pokus. | 2026-11-16 HRI (kantakartta a další sady CC BY 4.0); YLRE zvlášť neověřeno |
| ortofoto, mapa | **Ortokuva a peruskartta MML (TMS zrcadlo kartat.kapsi.fi)**<br>Maanmittauslaitos, zrcadlo Kapsi<br><https://tiles.kartat.kapsi.fi/ortokuva> · [info](https://www.maanmittauslaitos.fi/en/opendata-licence-cc40) | 🟢 public – [CC BY 4.0](https://www.maanmittauslaitos.fi/en/opendata-licence-cc40)<br>citace: „© Maanmittauslaitos, via kartat.kapsi.fi“ | volný | napojeno (šablona)<br>sonda `fi_ortho` | • Neoficiální zrcadlo (spolek Kapsi), bez záruky dostupnosti. | neověřeno (licence MML; podmínky zrcadla Kapsi neověřeny) |

## DK – Dánsko

| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |
|---|---|---|---|---|---|---|
| výšky (DTM/DSM), LiDAR mračno, ortofoto | **DHM 0,4 m DTM/DSM + mračno, ortofoto (Dataforsyningen)**<br>Klimadatastyrelsen<br><https://dataforsyningen.dk/> | 🟢 public – [frie geografiske data (komerčně ano, citace)](https://dataforsyningen.dk/)<br>citace: „Klimadatastyrelsen“ | ⚠ API klíč | kandidát (výšky) | • Vyžaduje bezplatný token (registrace účtu). | neověřeno (služba odpovídá, GetCoverage bez tokenu 403) |

## ES – Španělsko

| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |
|---|---|---|---|---|---|---|
| výšky (DTM/DSM), LiDAR mračno | **PNOA LiDAR, MDT02/05 (IDEE WCS jen 5 m int16)**<br>IGN / CNIG<br><https://centrodedescargas.cnig.es/> · [info](https://www.ign.es/) | 🟢 public – [CC BY 4.0](https://www.ign.es/)<br>citace: „© Instituto Geográfico Nacional“ | volný | kandidát (výšky) | • WCS jen 5 m v celých metrech (int16) – na vrstevnice nevhodné; LAZ jen z Centro de Descargas bez API (z cloudu nedostupné).<br>• Catastro zakazuje automatické / hromadné stahování. | neověřeno |

## LV – Lotyšsko

| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |
|---|---|---|---|---|---|---|
| LiDAR mračno, výšky (DTM/DSM) | **Klasifikované mračno 1,5–4 b/m², DTM**<br>LĢIA<br><https://www.lgia.gov.lv/> | 🟢 public – [CC BY 4.0](https://www.lgia.gov.lv/)<br>citace: „© LĢIA“ | volný | kandidát (výšky) | • Nízká hustota bodů (1,5–4 b/m²). | neověřeno |

## NO – Norsko

| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |
|---|---|---|---|---|---|---|
| výšky (DTM/DSM) | **DTM 1 m (Geonorge WCS), mračna na hoydedata.no**<br>Kartverket<br><https://hoydedata.no/> · [info](https://www.geonorge.no/) | 🟢 public – [CC BY 4.0](https://www.geonorge.no/)<br>citace: „© Kartverket“ | volný | kandidát (výšky) | • Mračna jen ručně; ortofoto Norge i bilder není otevřené ke stažení. | neověřeno |

## PL – Polsko

| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |
|---|---|---|---|---|---|---|
| výšky (DTM/DSM), LiDAR mračno, vektory, ortofoto | **NMT 1 m (WCS), LAZ ~4 b/m² (města ~20), BDOT10k, ortofoto**<br>GUGiK<br><https://mapy.geoportal.gov.pl/wss/service/PZGIK/NMT/GRID1/WCS/DigitalTerrainModelFormatTIFF> · [info](https://www.geoportal.gov.pl/) | 🟢 public – [bezplatně k libovolnému využití (zákon 2020)](https://www.geoportal.gov.pl/)<br>citace: „GUGiK“ | volný | kandidát (výšky) | • Ortofoto WMS jen v EPSG:2176–2180; EGiB (katastr) zakazuje hromadné stahování.<br>• Nízká hustota mračna mimo města (~4 b/m²). | neověřeno (evropa-data-prehled 2026-11-14: WCS DTM 1 m stažen) |

## SE – Švédsko

| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |
|---|---|---|---|---|---|---|
| výšky (DTM/DSM), LiDAR mračno | **Markhöjdmodell 1 m COG, Laserdata skog COPC (STAC)**<br>Lantmäteriet<br><https://api.lantmateriet.se/stac-hojd/v1> · [info](https://www.lantmateriet.se/) | 🟢 public – [CC BY 4.0](https://www.lantmateriet.se/)<br>citace: „© Lantmäteriet“ | ⚠ účet | kandidát (výšky) | • Stahování jen s bezplatným účtem Geotorget (Basic auth).<br>• GDPR: příjemce mračna je správcem osobních údajů. | neověřeno (evropa-data-prehled: licence v STAC CC BY 4.0, data 401 bez účtu) |

## SI – Slovinsko

| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |
|---|---|---|---|---|---|---|
| LiDAR mračno, výšky (DTM/DSM), ortofoto | **CLSS 2023–2025 (mračno, DMR, DMP, ortofoto po 1 km)**<br>GURS<br><https://clss.si/> | ⚪ nejasná – [neověřeno (≈ CC BY 4.0, stránka licenci neuvádí)](https://clss.si/) | volný | kandidát (výšky) | • Max. 10 listů naráz, bez API; z cloudu nedostupné. | neověřeno |

## UK – Spojené království

| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |
|---|---|---|---|---|---|---|
| výšky (DTM/DSM), LiDAR mračno, vektory | **EA LiDAR composite DTM/DSM 1 m (WCS) + National LiDAR Programme; OS OpenMap Local**<br>Environment Agency, Ordnance Survey<br><https://environment.data.gov.uk/> · [info](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) | 🟢 public – [Open Government Licence v3](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)<br>citace: „Contains Environment Agency information © Environment Agency“ | volný | kandidát (výšky) | • Otevřené ortofoto není (APGB jen veřejný sektor); jen Anglie, Skotsko a Wales částečně.<br>• OS MasterMap (obdoba DTM) placená. | neověřeno (evropa-data-prehled: WCS DTM 1 m stažen) |
