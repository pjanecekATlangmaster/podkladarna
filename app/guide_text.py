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
  polohopis ZABAGED a doplňky z OSM (cesty, plochy, budovy…) i další zdroje.
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
  Georeferencované PNG/TIFF do ZIPu je volitelné (ve výchozím stavu vypnuto).
</p>
<p>V ZIPu je mimo jiné:</p>
<ul>
  <li><code>*-sprint.omap</code> / <code>*-les.omap</code> / <code>*-mtbo.omap</code> (+ stejnojmenné <code>.ocd</code>) – podle názvu projektu a měřítka; cesty z OSM; <code>.omap</code> v OOM, <code>.ocd</code> v OCAD (ortofoto, OSM, ZTM, katastr, DMP OK, hillshade, reliéf)</li>
  <li>DXF srázy, vrstevnice GDAL (<code>contours_gdal.*</code>), vegetace / srázy / kupky ve <code>base/</code>, ZABAGED, budovy z OSM v .omap, RÚIAN/ZABAGED budovy ve složce <code>zabaged/</code>, OSM SHP ve složce <code>osm/</code>, památné stromy AOPK, návod <code>README_OOM.txt</code></li>
</ul>
<h2>Jak na to</h2>
<p>
  Nakreslete obdélník (max cca 36&nbsp;km², např. 6×6&nbsp;km), vyberte
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

Kvalita podkladu
----------------
Hlavní výstup jsou vektory pro OOM (.omap; cesty, plochy, budovy, vrstevnice…)
a stejnojmenné .ocd (OCD12) pro OCAD.
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
- osm/                 … OSM shapefile vrstvy pro ruční skládání (cesty, posedy, studny, budovy, …)
- zabaged/             … polohopis ZABAGED (shapefile včetně budov + RUIAN_budovy.shp; výchozí budovy v .omap jsou z OSM)
                       … Ostatní plocha v sídlech jako OstatniPlochaVSidlech_mensi / _stredni / _velke (podle velikosti; prázdné pásmo chybí)
- references/          … ortofoto, OSM, ZTM, katastr, náhled DMP OK, hillshade (jen pro kreslení, ne do tisku)
- README_OOM.txt       … podrobný postup v OOM
- metadata.json        … měřítko, preset, CRS, epochy LiDAR / režim DMP

Co s tím
--------
1. Nainstalujte OOM (openorienteering.org).
2. Rozbalte ZIP. Dvojklik na vybraný *-sprint.omap / *-les.omap / *-mtbo.omap (OOM)
   nebo stejnojmenný *.ocd (OCAD), případně File → Open.
3. Importujte DXF a SHP dle README_OOM.txt a přiřaďte symboliku ISOM/ISSOM.
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
