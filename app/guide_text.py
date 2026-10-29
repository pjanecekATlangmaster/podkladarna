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
  Podklady (ortofoto, katastr, hillshade…) i vektory tak v OOM / OCAD sedí na
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
  <li><code>*-sprint.omap</code> / <code>*-les.omap</code> / <code>*-mtbo.omap</code> (+ stejnojmenné <code>.ocd</code>) – podle názvu projektu a měřítka; cesty z OSM; <code>.omap</code> v OOM, <code>.ocd</code> v OCAD (ortofoto, OSM, ZTM, katastr, DTM kraje, DMP OK, hillshade, reliéf)</li>
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
    schodiště a koridor silnice (MTBO). Zpevněné plochy teprve přijdou.
  </p>
</details>
<h2>Jak na to</h2>
<p>
  Nakreslete obdélník (max cca 25&nbsp;km², např. 5&nbsp;×&nbsp;5&nbsp;km), vyberte
  <strong>mapový klíč</strong>, <strong>měřítko</strong>, případně zvolte další
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
- references/          … ortofoto, OSM, ZTM, katastr, DTM kraje (zaměřená situace), náhled DMP OK, hillshade (jen pro kreslení, ne do tisku)
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

Právní
------
Kód Podkladárny: MIT. Výstup jobu (PNG, .omap, …): CC BY 4.0 –
při šíření uveďte: „Podklad: Podkladárna · ČÚZK · OSM, [rok]“.
Data ČÚZK (DMR 5G, DMP OK, ZABAGED®, RÚIAN/INSPIRE, ortofoto) – CC BY 4.0.
AOPK památné stromy (CC BY 4.0). OSM © přispěvatelé (ODbL).
Volně inspirováno Karttapullautinem.

"""
