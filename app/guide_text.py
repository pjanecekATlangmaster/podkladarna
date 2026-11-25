"""Srozumitelný popis služby pro web a ZIP."""

import re

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
  <li>Les a MTBO ve dvou verzích: <code>*-les.omap</code> / <code>*-mtbo.omap</code> je plná kresba, <code>*_novege.omap</code> verze bez vegetace jen s jistými daty (bez zeleně z výšek, luk a pasek pod 0,3 ha a objektů z DEM; volba „Verze bez vegetace“ ji umí i bez žluté z výšek); obě i jako <code>.ocd</code></li>
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
  project. {{t:web.brand}} then went its own way (its own vegetation, cliffs and DEM)
  towards editable base maps directly in
  <a href="https://www.openorienteering.org/" target="_blank" rel="noopener">OpenOrienteering Mapper</a>.
  The program was written with the help of AI, but the generation itself runs the old way,
  with clearly defined algorithms.
</p>
<p>
  For the area you select, {{t:web.brand}} downloads the national airborne LiDAR or elevation models
  (terrain and surface), surveyed features from the country's <strong>large-scale technical map</strong>
  where available (in Czechia the regional digital technical map DTM, elsewhere e.g. BGT in the Netherlands,
  AV in Switzerland or FMZK in Vienna), additions from OSM (paths, areas, buildings…) and, in Czechia,
  ZABAGED and other national sources.
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
  The primary output is always a ZIP with editable vectors (contours, vegetation, OSM, national
  layers of the country – in Czechia ZABAGED, RÚIAN and AOPK –, cliff DXF, reference orthophoto…).
  A georeferenced GeoTIFF in the ZIP is optional (off by default).
</p>
<p>
  <strong>The map is georeferenced in the national coordinate system of the country</strong>
  (S-JTSK in Czechia, RD New in the Netherlands, LV95 in Switzerland …), including
  magnetic declination and grid convergence for the centre of the area.
  Templates (cadastre, hillshade…) and vectors therefore line up in OOM / OCAD
  without manual alignment and can go straight under your own template.
</p>
<p>
  <strong>Vegetation shows the height and density of growth from LiDAR (surface model), not runnability.</strong>
  Green and open areas show where the forest is tall, low or absent; how fast you can run
  through it (undergrowth, visibility) cannot be read from the data. Use it as an overview;
  the vegetation is finished in the terrain.
</p>
<p>
  The data sources cover the countries shown on the map; each map is built from the data of the national
  providers of its country, and the ZIP lists the credit to print (<code>ABOUT_OMAPIC.txt</code>).
</p>
<details class="about-zip">
  <summary><strong>The ZIP contains, among other things</strong></summary>
<ul>
  <li><code>*-sprint.omap</code> / <code>*-forest.omap</code> / <code>*-mtbo.omap</code> (+ <code>.ocd</code> of the same name), named after the project and scale; paths from OSM; <code>.omap</code> for OOM, <code>.ocd</code> for OCAD (templates: orthophoto, base map, cadastre, hillshade … depending on the country, and OSM)</li>
  <li>Forest and MTBO in two versions: <code>*-forest.omap</code> / <code>*-mtbo.omap</code> is the full drawing, <code>*_novege.omap</code> the version without vegetation with certain data only (no vegetation from elevation, meadows or clearings under 0.3 ha or DEM objects; the “Version without vegetation” option can also drop all yellow from elevation); both also as <code>.ocd</code></li>
  <li>cliff DXF, GDAL contours (<code>contours_gdal.*</code>), vegetation / cliffs / knolls in <code>base/</code>, OSM buildings in the .omap, OSM shapefiles in <code>osm/</code>, in Czechia also ZABAGED, RÚIAN/ZABAGED buildings in <code>zabaged/</code> and AOPK memorial trees, guide <code>README_OOM.txt</code></li>
</ul>
</details>
<details class="about-evolution">
  <summary><strong>How {{t:web.brand}} evolved (ZABAGED → OSM → regional DTM)</strong></summary>
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
  <strong>A request first:</strong> {{t:web.brand}} runs on a single shared server, so every
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
  <code>*-sprint.omap</code> / <code>*-forest.omap</code> / <code>*-mtbo.omap</code> in OOM
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
  {{t:web.brand}} is an experiment, so feedback is welcome next to a finished map.
</p>
"""

WEB_ABOUT_HTML_BY_LANG = {"cs": WEB_ABOUT_HTML, "en": WEB_ABOUT_HTML_EN}


# Česky na mezinárodní doméně (Omapic, Petr 2026-11-19): obsah jako WEB_ABOUT_HTML_EN, bez Podkladárny a ČÚZK jako jediného zdroje.
WEB_ABOUT_HTML_CS_INTL = """
<p>
  Chtěl jsem zjistit, jestli se už dají podklady pro orientační mapy skládat automaticky.
  Inspirací byl
  <a href="https://mapant.net/" target="_blank" rel="noopener">mapant.net</a>
  a projekt
  <a href="https://github.com/karttapullautin/karttapullautin" target="_blank" rel="noopener">Karttapullautin</a>.
  {{t:web.brand}} pak šel vlastní cestou (vlastní vegetace, srázy a DEM)
  k editovatelným podkladům přímo v
  <a href="https://www.openorienteering.org/" target="_blank" rel="noopener">OpenOrienteering Mapperu</a>.
  Program vznikl s pomocí AI, samotné generování ale běží postaru,
  podle jasně daných algoritmů.
</p>
<p>
  Pro zvolený výřez {{t:web.brand}} stáhne národní letecký LiDAR nebo výškové modely
  (terénu a povrchu), zaměřené objekty z <strong>technické mapy velkého měřítka</strong> dané země,
  kde je k dispozici (v Česku DTM kraje, jinde např. BGT v Nizozemsku,
  AV ve Švýcarsku nebo FMZK ve Vídni), doplňky z OSM (cesty, plochy, budovy…) a v Česku
  ZABAGED a další národní zdroje.
  Reliéf a vegetace vznikají z modelů terénu a povrchu (hustota odrazů, srázy z DEM,
  vrstevnice GDAL); hlavním výstupem jsou vektory pro OOM (a stejnojmenný soubor <code>.ocd</code>
  OCD12 pro OCAD) podle měřítka.
  Data nejsou dokonalá a automatika je jen skládá:
  něco chybí, něco se překrývá a ne všechno sedne napoprvé.
  Výsledek berte jako <em>pracovní podklad</em>, ne přesné zaměření
  ani hotovou mapu. Práce v OOM a v terénu vás ještě čeká.
</p>
<p>
  Web ukazuje náhled mapy v PNG.
  Hlavním výstupem je vždy ZIP s editovatelnými vektory (vrstevnice, vegetace, OSM, národní
  vrstvy země – v Česku ZABAGED, RÚIAN a AOPK –, DXF srázů, referenční ortofoto…).
  Georeferencovaný GeoTIFF v ZIPu je volitelný (výchozí vypnutý).
</p>
<p>
  <strong>Mapa je rovnou georeferencovaná v národním souřadnicovém systému země</strong>
  (v Česku S-JTSK, v Nizozemsku RD New, ve Švýcarsku LV95 …) včetně
  magnetické deklinace a grivace pro střed výřezu.
  Šablony (katastr, stínování…) a vektory proto v OOM / OCADu sedí
  bez ručního lícování a můžete je rovnou podložit vlastní šablonou.
</p>
<p>
  <strong>Vegetace ukazuje výšku a hustotu porostu z LiDARu (model povrchu), ne průběžnost.</strong>
  Zelená a otevřené plochy ukazují, kde je les vysoký, nízký nebo kde chybí; jak rychle se jím
  dá běžet (podrost, viditelnost), z dat vyčíst nejde. Berte ji jako přehled,
  vegetace se dokončuje v terénu.
</p>
<p>
  Zdroje dat pokrývají země vyznačené na mapě; každá mapa vzniká z dat národních
  poskytovatelů své země a ZIP uvádí citaci, kterou na mapu uvést (úvodní textový soubor).
</p>
<details class="about-zip">
  <summary><strong>ZIP obsahuje mimo jiné</strong></summary>
<ul>
  <li><code>*-sprint.omap</code> / <code>*-les.omap</code> (les) / <code>*-mtbo.omap</code> (+ stejnojmenný <code>.ocd</code>), pojmenované podle projektu a měřítka; cesty z OSM; <code>.omap</code> pro OOM, <code>.ocd</code> pro OCAD (šablony: ortofoto, základní mapa, katastr, stínování … podle země, a OSM)</li>
  <li>Les a MTBO ve dvou verzích: <code>*-les.omap</code> / <code>*-mtbo.omap</code> je plná kresba, <code>*_novege.omap</code> verze bez vegetace jen s jistými daty (bez zeleně z výšek, luk a pasek pod 0,3 ha a objektů z DEM; volba „Verze bez vegetace“ ji umí i bez žluté z výšek); obě i jako <code>.ocd</code></li>
  <li>DXF srázů, vrstevnice GDAL (<code>contours_gdal.*</code>), vegetace / srázy / kupky v <code>base/</code>, budovy OSM v .omap, shapefily OSM v <code>osm/</code>, v Česku i ZABAGED, budovy RÚIAN/ZABAGED v <code>zabaged/</code> a památné stromy AOPK, návod <code>README_OOM.txt</code></li>
</ul>
</details>
<details class="about-evolution">
  <summary><strong>Jak se {{t:web.brand}} vyvíjel (ZABAGED → OSM → DTM kraje)</strong></summary>
  <ol>
    <li>
      <strong>ZABAGED.</strong> Začínal jsem s národní topografickou databází ČÚZK. Je úplná
      a jednotná, ale na orientační mapu příliš hrubá a pomalu se aktualizuje.
    </li>
    <li>
      <strong>OSM.</strong> Je aktuálnější a má detaily, které orientační mapa potřebuje
      (schody, ploty, lávky, chodníky), kvalita se ale místo od místa liší. OSM převzalo vedení
      a ZABAGED zůstal jako doplněk.
    </li>
    <li>
      <strong>DTM kraje.</strong> Zaměřená digitální technická mapa (DMVS, ČÚZK) dává
      přesný <em>tvar</em> ploch: chodníků, silnic, parkovišť, budov, schodišť, plotů
      a víceúrovňových mostů. Tvar proto přichází z DTM a <em>aktuálnost</em> z OSM:
      co v DTM ještě chybí (nové budovy, čerstvé změny), doplní OSM. Mosty se kreslí
      s úrovněmi křížení a místa, kudy se dá projít pod nimi, jsou označená trojúhelníkem. Pokrytí DTM
      stále roste; kde chybí, zůstává OSM a ZABAGED.
    </li>
    <li>
      <strong>Karttapullautin → vlastní vektory.</strong> Reliéf a vegetace původně vycházely
      z výstupů Karttapullautinu (KP). Dnes vegetaci, srázy a DEM počítáme sami a
      kreslíme je rovnou jako editovatelné vektory. Tahle část je mladší a dál na ní pracujeme.
    </li>
  </ol>
  <p>
    Nejvíc z DTM těží sprint (1:4000). V mapách pro les a MTBO se DTM používá pro budovy,
    ploty, schodiště a koridor silnice (MTBO). Zpevněné plochy (silnice, parkoviště, pěší zóny) v lese
    a MTBO se kreslí z OSM, ZABAGED a DTM jako jedna síť.
  </p>
</details>
<h2>Jak na to</h2>
<p class="about-note">
  <strong>Nejdřív prosba:</strong> {{t:web.brand}} běží na jednom sdíleném serveru, takže každý
  velký job na chvíli zdrží všechny ostatní. Generujte prosím jen to, co opravdu použijete,
  ať na zkoušku, nebo pro práci na mapě. Na první pokus stačí menší výřez a jedno měřítko;
  celé území a další varianty má smysl pouštět, až výsledek vypadá dobře. Děkuji.
</p>
<p>
  Nakreslete obdélník (MTBO do 25&nbsp;km², např. 5&nbsp;×&nbsp;5 nebo 7&nbsp;×&nbsp;3,5&nbsp;km; les do 15&nbsp;km²;
  sprint 1:4000 do 4&nbsp;km², např. 2&nbsp;×&nbsp;2&nbsp;km), zvolte
  <strong>měřítko</strong> (rozhoduje mezi sprintem, nebo lesem a MTBO), případně nastavte další
  volby a spusťte generování. Tlačítko ukazuje hrubý odhad potřebného času.
  Stránku můžete mezitím zavřít.
  Až bude hotovo, stáhněte ZIP, otevřete zvolený
  <code>*-sprint.omap</code> / <code>*-les.omap</code> / <code>*-mtbo.omap</code> v OOM
  (nebo stejnojmenný <code>.ocd</code> v OCADu) a začněte mapovat.
</p>
<p>
  Najednou běží jen jeden job. Z jedné sítě mohou běžet nebo čekat nejvýš <strong>2 joby</strong>
  naráz a <strong>10</strong> za hodinu. Kdo má obě místa obsazená, dá ve frontě přednost
  tomu, kdo ještě nic nespustil. Rozlišení podle IP adresy není dokonalé
  (VPN, sdílená Wi‑Fi). Hotové joby se uchovávají <strong>48 hodin</strong>, ZIP si proto uložte.
  Pokud nechcete, aby mapu viděl někdo další, zvolte soukromý režim a odkaz ke stažení
  dostanete, až bude hotovo.
</p>
<p>
  {{t:web.brand}} je experiment, proto ocením zpětnou vazbu u hotové mapy.
</p>
"""


_CS_CRS_HTML = "<strong>Mapa je rovnou georeferencovaná v S-JTSK (EPSG:5514)</strong>"
_CS_CRS_HTML_INTL = (
    "<strong>Mapa je rovnou georeferencovaná v souřadnicovém systému země</strong>\n"
    "  (v Česku S-JTSK, v Nizozemsku RD New, ve Švýcarsku LV95 …)"
)


def web_about_html(lang: str, *, intl: bool = False) -> str:
    """Úvod v jazyce stránky; bez překladu anglicky (pak česky).

    ``intl``: doména i pro jiné země – česky obsah jako v angličtině (název domény, bez Podkladárny
    a S-JTSK jako jediného systému), bez odkazů na GitHub."""
    if intl and lang == "cs":
        return strip_github_links(WEB_ABOUT_HTML_CS_INTL)
    html = WEB_ABOUT_HTML_BY_LANG.get(lang) or WEB_ABOUT_HTML_BY_LANG.get("en") or WEB_ABOUT_HTML
    return strip_github_links(html.replace(_CS_CRS_HTML, _CS_CRS_HTML_INTL)) if intl else html


_GITHUB_ISSUES_RE = re.compile(r"\s*nebo na\s*<a [^>]*github\.com[^>]*>GitHub Issues</a>")
_GITHUB_SOURCE_RE = re.compile(r"\s*Zdrojový kód:\s*<a [^>]*github\.com[^>]*>[^<]*</a>\.")
_GITHUB_LINK_RE = re.compile(r'<a [^>]*href="https://[^"]*github\.(?:com|io)[^"]*"[^>]*>([^<]*)</a>')


def strip_github_links(html: str) -> str:
    """Mezinárodní verze nikde neodkazuje na GitHub (Petr 2026-11-19): bez věty o zdrojovém kódu a Issues,
    ostatní odkazy na GitHub jen jako text."""
    html = _GITHUB_SOURCE_RE.sub("", _GITHUB_ISSUES_RE.sub("", html))
    return _GITHUB_LINK_RE.sub(r"\1", html)

_ZIP_ABOUT_TEMPLATE = f"""Podkladárna – co je v tomto balíčku
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
Mapa je georeferencovaná v {{crs}} včetně magnetické deklinace
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
- uzitecne/             … vegetace / srázy 104 / skály: v_plne_mape/ vs vyrazene_filtry/ (po filtrech)
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
{{web_url}}

Budu vděčný za jakékoli dotazy, připomínky i podněty k vylepšení.
Napište mi nebo zavolejte:
  e-mail:   janecek@datais.cz
  telefon:  733 575 541

{{github_block}}Podpora vývoje
--------------
Podkladárna je zdarma. Pokud vám pomohla a chcete podpořit další vývoj
(hlavně placené AI nástroje), ocením dobrovolný příspěvek v jakékoli výši:
{{web_url}}/podpora

Právní
------
Kód Podkladárny: MIT. Výstup jobu (.omap, .ocd, PNG, …): CC BY 4.0.
Mapa z tohoto podkladu (i překreslená a doplněná) musí na mapě nebo
v legendě uvést, že využila Podkladárnu: „{{citation}}“.
{{attribution}}
Volně inspirováno Karttapullautinem.

"""

CZ_CITATION = "Podklad: Podkladárna · ČÚZK · OSM, [rok]"
CZ_WEB_URL = "https://podkladarna.kibos.link"
CZ_ATTRIBUTION = (
    "Data ČÚZK (DMR 5G, DMP OK, ZABAGED®, RÚIAN/INSPIRE, DTM, ortofoto) – CC BY 4.0.",
    "AOPK památné stromy (CC BY 4.0). OSM © přispěvatelé (ODbL).",
)


_ZIP_GITHUB_BLOCK = """Technické hlášení chyb můžete také založit na GitHubu:
https://github.com/pjanecekATlangmaster/podkladarna/issues

"""


def zip_about_txt(
    citation: str = CZ_CITATION,
    attribution: tuple[str, ...] = CZ_ATTRIBUTION,
    github: bool = True,
    crs: str = "S-JTSK (EPSG:5514)",
    web_url: str = CZ_WEB_URL,
) -> str:
    """CO_JE_PODKLADARNA.txt s citací zdrojů podle země výřezu (licence místních dat).

    ``github``: odkaz na GitHub Issues jen u jobů v Česku (mezinárodní verze ho neukazuje)."""
    return _ZIP_ABOUT_TEMPLATE.format(
        citation=citation,
        attribution="\n".join(attribution or CZ_ATTRIBUTION),
        github_block=_ZIP_GITHUB_BLOCK if github else "",
        crs=crs,
        web_url=(web_url or CZ_WEB_URL).rstrip("/"),
    )


ZIP_ABOUT_TXT = zip_about_txt()

