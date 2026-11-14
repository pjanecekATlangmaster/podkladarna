"""Srozumitelný popis služby pro web a ZIP."""

from app.pipeline.source_meta import INDICATIVE_LABEL_CS

WEB_ABOUT_HTML = """
<p>
  Chtěl jsem vyzkoušet, jestli už jdou podklady pro orientační mapy
  skládat automaticky. Inspirací bylo
  <a href="https://mapant.net/" target="_blank" rel="noopener">mapant.net</a>
  a projekt
  <a href="https://github.com/karttapullautin/karttapullautin" target="_blank" rel="noopener">Karttapullautin</a>
  – Podkladárna pak šla vlastní cestou (vlastní vegetace / srázy / DEM)
  k editovatelným podkladům přímo v
  <a href="https://www.openorienteering.org/" target="_blank" rel="noopener">OpenOrienteering Mapperu</a>.
  Program vznikl s pomocí AI, samotné generování ale běží postaru,
  jasně danými algoritmy.
</p>
<p>
  Z výřezu na mapě si Podkladárna stáhne LiDAR (DMR&nbsp;5G + DMP&nbsp;OK),
  zaměřený polohopis z <strong>DTM kraje</strong> (kde je k dispozici),
  doplňky z OSM (cesty, plochy, budovy…), ZABAGED a další zdroje.
  Reliéf a zeleň skládá z DMR/DMP (hustota odrazů, DEM srázy, GDAL vrstevnice);
  hlavní výstup jsou vektory pro OOM (a stejnojmenné
  <code>.ocd</code> OCD12 pro OCAD) – podle měřítka.
  Data nejsou dokonalá a automatika je jen skládá dohromady:
  něco chybí, něco se překrývá a ne všechno sedí napoprvé.
  Berte to jako <em>pracovní podklad</em>, ne dokonalé zaměření
  ani hotovou mapu. V OOM a terénu s tím ještě budete mít práci.
</p>
<p>
  Na webu uvidíte PNG náhled mapy.
  Primární výstup je vždy ZIP s editovatelnými vektory (vrstevnice, zeleň, ZABAGED, OSM,
  RÚIAN podklady, AOPK, DXF srázů, referenční orto…).
  Georeferencovaný GeoTIFF do ZIPu je volitelný (ve výchozím stavu vypnuto).
</p>
<p>
  <strong>Mapa je rovnou georeferencovaná v S-JTSK (EPSG:5514)</strong> – včetně
  magnetické deklinace a grivace pro střed výřezu.
  Podklady (katastr, hillshade…) i vektory tak v OOM / OCAD sedí na
  sebe bez ručního lícování a do vlastní šablony je jde rovnou podložit.
</p>
<p>
  <strong>Vegetace je výška a hustota porostu z LiDARu (DMP&nbsp;OK), ne průběžnost.</strong>
  Zeleň a louky ukazují, kde je les vysoký, nízký nebo žádný – jak se lesem běží
  (podrost, viditelnost) z dat nepoznáme. Berte je jako přehled, kresbu vegetace
  doladí až terén.
</p>
<details class="about-zip">
  <summary><strong>V ZIPu je mimo jiné</strong></summary>
<ul>
  <li><code>*-sprint.omap</code> / <code>*-les.omap</code> / <code>*-mtbo.omap</code> (+ stejnojmenné <code>.ocd</code>) – podle názvu projektu a měřítka; cesty z OSM; <code>.omap</code> v OOM, <code>.ocd</code> v OCAD (OSM, ZTM, katastr, DTM kraje, DMP OK, hillshade, reliéf)</li>
  <li>DXF srázy, vrstevnice GDAL (<code>contours_gdal.*</code>), vegetace / srázy / kupky ve <code>base/</code>, ZABAGED, budovy z OSM v .omap, RÚIAN/ZABAGED budovy ve složce <code>zabaged/</code>, OSM SHP ve složce <code>osm/</code>, památné stromy AOPK, návod <code>README_OOM.txt</code></li>
</ul>
</details>
<details class="about-evolution">
  <summary><strong>Jak se Podkladárna vyvíjela (ZABAGED → OSM → DTM kraje)</strong></summary>
  <ol>
    <li>
      <strong>ZABAGED.</strong> Začínal jsem státním polohopisem ČÚZK. Je úplný a jednotný,
      ale pro orientační mapu příliš hrubý a pomalu se aktualizuje.
    </li>
    <li>
      <strong>OSM.</strong> Je aktuálnější a má detaily, které orientační mapa potřebuje
      (schody, ploty, lávky, chodníky), jenže kvalita se mezi místy liší. OSM tak převzalo vedení
      a ZABAGED zůstal jako doplněk.
    </li>
    <li>
      <strong>DTM kraje.</strong> Zaměřená digitální technická mapa (DMVS, ČÚZK) dává
      přesný <em>tvar</em> ploch: chodníky, silnice, parkoviště, budovy, schodiště, ploty
      i mosty s více úrovněmi. Tvar proto bereme z DTM, <em>aktuálnost</em> z OSM: co v DTM
      ještě chybí (nové stavby, čerstvé úpravy), doplní OSM. Mosty se kreslí s křížením úrovní
      a místa, kde se pod nimi dá přecházet, označí trojúhelník. Pokrytí DTM kraje postupně roste;
      kde chybí, zůstávají OSM a ZABAGED.
    </li>
    <li>
      <strong>Karttapullautin → vlastní vektory.</strong> Reliéf a vegetace původně stály
      na výstupech Karttapullautinu (KP). Dnes si vegetaci, srázy a DEM počítáme sami a
      kreslíme je rovnou jako editovatelné vektory. Tahle část je mladší a ještě na ní
      budeme pracovat.
    </li>
  </ol>
  <p>
    Sprint (1:4000) z DTM těží nejvíc. V lese a u MTBO se DTM využívá pro budovy, ploty,
    schodiště a koridor silnice (MTBO). Zpevněné plochy (silnice, parkoviště, pěší zóny) se v lese a MTBO kreslí z OSM, ZABAGED a DTM jako jedna síť.
  </p>
</details>
<h2>Jak na to</h2>
<p class="about-note">
  <strong>Prosba na začátek:</strong> Podkladárna běží na jednom sdíleném serveru, takže každý
  velký job na chvíli zdrží všechny ostatní. Generujte prosím jen to, co opravdu využijete,
  ať už na zkoušku, nebo pro práci na mapě. Na první pokus stačí menší výřez a jedno měřítko;
  celé území a další varianty má smysl pustit, až když výsledek sedí. Děkujeme.
</p>
<p>
  Nakreslete obdélník (MTBO max 25&nbsp;km², např. 5&nbsp;×&nbsp;5 nebo 7&nbsp;×&nbsp;3,5&nbsp;km; les max 15&nbsp;km²;
  sprint 1:4000 max 4&nbsp;km², např. 2&nbsp;×&nbsp;2&nbsp;km), vyberte
  <strong>měřítko</strong> (podle něj se zvolí sprint, nebo les a MTBO), případně zvolte další
  parametry a spusťte generování. Na tlačítku je hrubý odhad potřebného času.
  Stránku mezitím můžete zavřít.
  Po dokončení stáhněte ZIP, v OOM otevřete vybraný
  <code>*-sprint.omap</code> / <code>*-les.omap</code> / <code>*-mtbo.omap</code>
  (v OCAD stejnojmenný <code>.ocd</code>) a můžete začít.
</p>
<p>
  Běží to na domácím NAS, takže najednou jede jen jeden job. Z jedné sítě
  můžou současně běžet nebo čekat nejvýš <strong>2 joby</strong>, za hodinu
  <strong>10</strong>. Kdo má oba sloty plné, ve frontě ustoupí tomu, kdo
  ještě nic nespustil. Podle IP to není stoprocentní (VPN, sdílená Wi‑Fi).
  Hotové joby držíme <strong>48 hodin</strong>, ZIP si uložte u sebe.
  Pokud nechcete, aby mapu viděl nikdo jiný, zvolte privátní režim a až bude
  hotovo, přijde vám odkaz ke stažení.
</p>
<p>
  Podkladárna je experiment, proto ocením zpětnou vazbu u hotové mapy
  nebo na
  <a href="https://github.com/pjanecekATlangmaster/podkladarna/issues" target="_blank" rel="noopener">GitHub Issues</a>.
</p>
"""

# Anglický úvod pro web (stejné bloky a třídy jako WEB_ABOUT_HTML).
WEB_ABOUT_HTML_EN = """
<p>
  I wanted to find out whether base maps for orienteering can already be put together
  automatically. The inspiration was
  <a href="https://mapant.net/" target="_blank" rel="noopener">mapant.net</a>
  and the
  <a href="https://github.com/karttapullautin/karttapullautin" target="_blank" rel="noopener">Karttapullautin</a>
  project. Podkladárna then went its own way (its own vegetation, cliffs and DEM)
  towards editable base maps directly in
  <a href="https://www.openorienteering.org/" target="_blank" rel="noopener">OpenOrienteering Mapper</a>.
  The program was written with the help of AI, but the generation itself runs the old way,
  with clearly defined algorithms.
</p>
<p>
  For the area you select, Podkladárna downloads LiDAR (DMR&nbsp;5G + DMP&nbsp;OK),
  surveyed features from the <strong>regional digital technical map (DTM)</strong> where available,
  additions from OSM (paths, areas, buildings…), ZABAGED and other sources.
  Relief and vegetation are built from the terrain and surface models (return density, DEM cliffs,
  GDAL contours); the main output is vectors for OOM (and an <code>.ocd</code> OCD12 file
  of the same name for OCAD), depending on the scale.
  The data are not perfect and the automation only combines them:
  something is missing, something overlaps and not everything fits the first time.
  Treat the result as a <em>working base map</em>, not a precise survey
  or a finished map. You will still have work to do in OOM and in the terrain.
</p>
<p>
  The website shows a PNG preview of the map.
  The primary output is always a ZIP with editable vectors (contours, vegetation, ZABAGED, OSM,
  RÚIAN layers, AOPK, cliff DXF, reference orthophoto…).
  A georeferenced GeoTIFF in the ZIP is optional (off by default).
</p>
<p>
  <strong>The map is georeferenced in S-JTSK (EPSG:5514)</strong>, including
  magnetic declination and grid convergence for the centre of the area.
  Templates (cadastre, hillshade…) and vectors therefore line up in OOM / OCAD
  without manual alignment and can go straight under your own template.
</p>
<p>
  <strong>Vegetation shows the height and density of growth from LiDAR (DMP&nbsp;OK), not runnability.</strong>
  Green and open areas show where the forest is tall, low or absent; how fast you can run
  through it (undergrowth, visibility) cannot be read from the data. Use it as an overview;
  the vegetation is finished in the terrain.
</p>
<p>
  The data sources currently cover the Czech Republic.
</p>
<details class="about-zip">
  <summary><strong>The ZIP contains, among other things</strong></summary>
<ul>
  <li><code>*-sprint.omap</code> / <code>*-les.omap</code> (forest) / <code>*-mtbo.omap</code> (+ <code>.ocd</code> of the same name), named after the project and scale; paths from OSM; <code>.omap</code> for OOM, <code>.ocd</code> for OCAD (templates OSM, ZTM, cadastre, regional DTM, DMP OK, hillshade, relief)</li>
  <li>cliff DXF, GDAL contours (<code>contours_gdal.*</code>), vegetation / cliffs / knolls in <code>base/</code>, ZABAGED, OSM buildings in the .omap, RÚIAN/ZABAGED buildings in <code>zabaged/</code>, OSM shapefiles in <code>osm/</code>, AOPK memorial trees, guide <code>README_OOM.txt</code> (in Czech)</li>
</ul>
</details>
<details class="about-evolution">
  <summary><strong>How Podkladárna evolved (ZABAGED → OSM → regional DTM)</strong></summary>
  <ol>
    <li>
      <strong>ZABAGED.</strong> I started with the national topographic database of ČÚZK. It is complete
      and consistent, but too coarse for an orienteering map and slow to update.
    </li>
    <li>
      <strong>OSM.</strong> It is more up to date and has the details an orienteering map needs
      (steps, fences, footbridges, sidewalks), but its quality varies from place to place. OSM took the lead
      and ZABAGED remained as a supplement.
    </li>
    <li>
      <strong>Regional DTM.</strong> The surveyed digital technical map (DMVS, ČÚZK) gives the
      precise <em>shape</em> of areas: sidewalks, roads, car parks, buildings, stairs, fences
      and multi-level bridges. The shape therefore comes from the DTM and <em>currency</em> from OSM:
      what the DTM is still missing (new buildings, recent changes) is added from OSM. Bridges are drawn with
      crossing levels, and places where you can pass underneath are marked with a triangle. DTM coverage
      keeps growing; where it is missing, OSM and ZABAGED remain.
    </li>
    <li>
      <strong>Karttapullautin → own vectors.</strong> Relief and vegetation were originally based
      on Karttapullautin (KP) outputs. Today we compute vegetation, cliffs and the DEM ourselves and
      draw them directly as editable vectors. This part is younger and we will keep working on it.
    </li>
  </ol>
  <p>
    Sprint (1:4000) benefits most from the DTM. In forest and MTBO maps the DTM is used for buildings,
    fences, stairs and the road corridor (MTBO). Paved areas (roads, car parks, pedestrian zones) in forest
    and MTBO maps are drawn from OSM, ZABAGED and the DTM as one network.
  </p>
</details>
<h2>How to use it</h2>
<p class="about-note">
  <strong>A request first:</strong> Podkladárna runs on a single shared server, so every
  large job delays everyone else for a while. Please generate only what you will really use,
  whether as a try-out or for work on a map. A smaller area and one scale are enough for a first try;
  the whole area and other variants are worth running once the result looks right. Thank you.
</p>
<p>
  Draw a rectangle (MTBO up to 25&nbsp;km², e.g. 5&nbsp;×&nbsp;5 or 7&nbsp;×&nbsp;3.5&nbsp;km; forest up to 15&nbsp;km²;
  sprint 1:4000 up to 4&nbsp;km², e.g. 2&nbsp;×&nbsp;2&nbsp;km), choose the
  <strong>scale</strong> (it decides between sprint, or forest and MTBO), set other
  options if you like and start generating. The button shows a rough estimate of the time needed.
  You can close the page in the meantime.
  When it is done, download the ZIP, open the chosen
  <code>*-sprint.omap</code> / <code>*-les.omap</code> / <code>*-mtbo.omap</code> in OOM
  (or the <code>.ocd</code> of the same name in OCAD) and start mapping.
</p>
<p>
  Only one job runs at a time. From one network, at most <strong>2 jobs</strong> can run
  or wait at once, and <strong>10</strong> per hour. Whoever has both slots full gives way
  in the queue to someone who has not started anything yet. Going by IP address is not perfect
  (VPN, shared Wi‑Fi). Finished jobs are kept for <strong>48 hours</strong>, so save the ZIP.
  If you do not want anyone else to see the map, choose private mode and you will receive
  a download link when it is done.
</p>
<p>
  Podkladárna is an experiment, so feedback is welcome next to a finished map
  or on
  <a href="https://github.com/pjanecekATlangmaster/podkladarna/issues" target="_blank" rel="noopener">GitHub Issues</a>.
</p>
"""

WEB_ABOUT_HTML_BY_LANG = {"cs": WEB_ABOUT_HTML, "en": WEB_ABOUT_HTML_EN}


def web_about_html(lang: str) -> str:
    """Úvod v jazyce stránky; bez překladu anglicky (pak česky)."""
    return WEB_ABOUT_HTML_BY_LANG.get(lang) or WEB_ABOUT_HTML_BY_LANG.get("en") or WEB_ABOUT_HTML

ZIP_ABOUT_TXT = f"""Podkladárna – co je v tomto balíčku
==================================

Tento ZIP vygenerovala služba Podkladárna (LiDAR + ZABAGED + OSM → podklad pro OOM).

{INDICATIVE_LABEL_CS}

Proč Podkladárna
----------------
Chtěl jsem vyzkoušet, jestli už jdou podklady pro orientační mapy skládat
automaticky. Inspirací bylo mapant.net a Karttapullautin; Podkladárna pak
šla vlastní cestou k editovatelným podkladům v OpenOrienteering Mapperu
(vlastní vegetace / srázy / DEM), ne jen k rastrovému náhledu.
Program vznikl s pomocí AI, samotné generování ale běží postaru, jasně
danými algoritmy.

Odkud co bereme (vývoj)
-----------------------
ZABAGED (státní polohopis, úplný, ale hrubý) → OSM (aktuálnější, s detaily) →
DTM kraje (zaměřený tvar ploch: chodníky, silnice, parkoviště, budovy, schodiště,
ploty, mosty ve více úrovních). Tvar bereme z DTM, aktuálnost z OSM; kde DTM chybí,
zůstávají OSM a ZABAGED. Reliéf a vegetace dřív stály na Karttapullautinu, dnes jsou
to vlastní vektory (tahle část je mladší a ještě na ní pracujeme).

Kvalita podkladu
----------------
Hlavní výstup jsou vektory pro OOM (.omap; cesty, plochy, budovy, vrstevnice…)
a stejnojmenné .ocd (OCD12) pro OCAD.
Mapa je georeferencovaná v S-JTSK (EPSG:5514) včetně magnetické deklinace
a grivace pro střed výřezu – podklady i vektory sedí na sebe bez lícování.
Vegetace je výška a hustota porostu z LiDARu (DMP OK), ne průběžnost – jak
se lesem běží (podrost, viditelnost), z dat nepoznáme; kresbu vegetace doladí terén.
PNG náhled mapy je hlavně orientační.
Data ČÚZK i OSM nejsou dokonalá a automatika je jen skládá dohromady –
něco chybí, něco se překrývá. Tento balíček je pracovní podklad,
ne dokonalé zaměření ani hotová mapa; v OOM a terénu s ním ještě budete mít práci.

Co je uvnitř
------------
- *-sprint/les/mtbo.omap … podle názvu projektu a měřítka (cesty OSM), otevřete v OpenOrienteering Mapper (OOM)
- *-sprint/les/mtbo.ocd  … totéž pro OCAD (OCD12 přes Mapper convert; lossy OK; .omap zůstává)
- base/                … vrstevnice GDAL (contours_gdal.*), vegetace/srázy/kupky
- uzitecne/             … vegetace / srázy 104 / skály: pouzite/ vs vyhozene/ (po filtrech)
- osm/                 … OSM shapefile vrstvy pro ruční skládání (cesty, posedy, studny, budovy, skály podklad, …)
- zabaged/             … polohopis ZABAGED (shapefile včetně budov + RUIAN_budovy.shp; výchozí budovy v .omap jsou z OSM)
                       … Ostatní plocha v sídlech jako OstatniPlochaVSidlech_mensi / _stredni / _velke (podle velikosti; prázdné pásmo chybí)
- references/          … ortofoto ČÚZK, OSM, ZTM, katastr, DTM kraje (zaměřená situace), náhled DMP OK, hillshade – výchozí online šablony (.xml), nebo stažené rastry (jen pro kreslení, ne do tisku)
- README_OOM.txt       … podrobný postup v OOM
- metadata.json        … měřítko, preset, CRS, epochy LiDAR / režim DMP

Co s tím
--------
1. Nainstalujte OOM (openorienteering.org).
2. Rozbalte ZIP. Dvojklik na vybraný *-sprint.omap / *-les.omap / *-mtbo.omap (OOM)
   nebo stejnojmenný *.ocd (OCAD), případně File → Open.
3. Importujte DXF a SHP dle README_OOM.txt a použijte symboly sady v mapě (ISOM 2017-2 / ISSprOM 2019-2 s českými názvy, MTBO: ISMTBOM).
4. Kreslete mapu. Referenční vrstvy po dokončení vypněte nebo smažte.

OCAD: použijte *.ocd z ZIPu (OCD12). Soubor .omap nativně neotevře –
zůstává pro OOM; DXF/SHP/PNG+PGW dál fungují.

Autor a kontakt
---------------
Autorem Podkladárny je Petr Janeček. Služba běží na
https://podkladarna.kibos.link

Budu vděčný za jakékoli dotazy, připomínky i podněty k vylepšení.
Napište mi nebo zavolejte:
  e-mail:   janecek@datais.cz
  telefon:  733 575 541

Technické hlášení chyb můžete také založit na GitHubu:
https://github.com/pjanecekATlangmaster/podkladarna/issues

Podpora vývoje
--------------
Podkladárna je zdarma. Pokud vám pomohla a chcete podpořit další vývoj
(hlavně placené AI nástroje), ocením dobrovolný příspěvek v jakékoli výši:
https://podkladarna.kibos.link/podpora

Právní
------
Kód Podkladárny: MIT. Výstup jobu (PNG, .omap, …): CC BY 4.0 –
při šíření uveďte: „Podklad: Podkladárna · ČÚZK · OSM, [rok]“.
Data ČÚZK (DMR 5G, DMP OK, ZABAGED®, RÚIAN/INSPIRE, ortofoto) – CC BY 4.0.
AOPK památné stromy (CC BY 4.0). OSM © přispěvatelé (ODbL).
Volně inspirováno Karttapullautinem.

"""
