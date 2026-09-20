"""Srozumitelný popis služby pro web a ZIP."""

WEB_ABOUT_HTML = """
<p>
  Chtěl jsem vyzkoušet, jestli už jdou podklady pro orientační mapy
  skládat automaticky. Inspirací bylo
  <a href="https://mapant.net/" target="_blank" rel="noopener">mapant.net</a>
  a projekt
  <a href="https://github.com/karttapullautin/karttapullautin" target="_blank" rel="noopener">Karttapullautin</a>
  – myšlenka se ale posunula od rastrového náhledu k editovatelným podkladům
  přímo v
  <a href="https://www.openorienteering.org/" target="_blank" rel="noopener">OpenOrienteering Mapperu</a>.
  Program vznikl s pomocí AI, samotné generování ale běží postaru,
  jasně danými algoritmy.
</p>
<p>
  Z výřezu na mapě si Podkladárna stáhne LiDAR (DMR&nbsp;5G + DMP&nbsp;OK),
  polohopis ZABAGED a doplňky z OSM (cesty, plochy, budovy…) i další zdroje.
  Karttapullautin připraví reliéf a zeleň jako rychlý PNG náhled; hlavní
  výstup jsou vektory ve <code>.omap</code> pro OOM – podle měřítka
  (cesty z OSM). Data nejsou dokonalá a automatika je jen skládá dohromady:
  něco chybí, něco se překrývá a ne všechno sedí napoprvé.
  Berte to jako <em>pracovní podklad</em>, ne hotovou mapu. V OOM s tím
  ještě budete kreslit.
</p>
<p>
  Na webu uvidíte PNG z Karttapullautinu, spíš rychlý náhled než finální kresbu.
  ZIP skládá editovatelné vektory z víc zdrojů (vrstevnice, zeleň, ZABAGED, OSM,
  RÚIAN podklady, AOPK, DXF srázů, referenční orto…), takže PNG a ZIP nevypadají úplně stejně.
  Výchozí je PNG + ZIP; můžete nechat jen náhled.
</p>
<p>Ve ZIPu je mimo jiné:</p>
<ul>
  <li><code>*-sprint.omap</code> / <code>*-les.omap</code> / <code>*-mtbo.omap</code> – podle názvu projektu a měřítka; cesty z OSM; otevřete v OOM (ortofoto, OSM, ZTM, katastr, DMP OK, hillshade, reliéf)</li>
  <li>DXF srázy, vrstevnice GDAL (<code>contours_gdal.*</code>) i KP (<code>contours_kp.dxf</code>) ve <code>base/</code>, ZABAGED, budovy z OSM v .omap, RÚIAN/ZABAGED budovy ve složce <code>zabaged/</code>, OSM SHP ve složce <code>osm/</code>, památné stromy AOPK, návod <code>README_OOM.txt</code></li>
</ul>
<p>
  Nakreslete obdélník (max cca 36&nbsp;km², např. 6×6&nbsp;km), vyberte <strong>měřítko</strong> a
  <strong>ekvidistanci</strong> a spusťte generování. PNG na webu je jen náhled;
  do ZIPu jdou omapy pro příslušné disciplíny. Stránku mezitím můžete zavřít.
  Po dokončení stáhněte ZIP (pokud jste ho nechali generovat),
  v OOM otevřete vybraný <code>*-sprint.omap</code> / <code>*-les.omap</code> / <code>*-mtbo.omap</code> a podle návodu doladíte symboliku.
  OCAD soubor <code>.omap</code> neotevře, DXF/SHP/PNG ano.
</p>
<p>
  Běží to na domácím NAS, takže najednou jede jen jeden job. Z jedné sítě
  můžou současně běžet nebo čekat nejvýš <strong>2 joby</strong>, za hodinu
  <strong>10</strong>. Kdo má oba sloty plné, ve frontě ustoupí tomu, kdo
  ještě nic nespustil. Podle IP to není stoprocentní (VPN, sdílená Wi‑Fi).
  Hotové joby držíme <strong>48 hodin</strong>, ZIP si uložte u sebe.
</p>
<p>
  Podkladárna je experiment — ocení
  <a href="https://github.com/pjanecekATlangmaster/podkladarna/issues" target="_blank" rel="noopener">zpětnou vazbu a připomínky (GitHub Issues)</a>.
</p>
"""

ZIP_ABOUT_TXT = """Podkladárna – co je v tomto balíčku
==================================

Tento ZIP vygenerovala služba Podkladárna (LiDAR + ZABAGED + OSM → podklad pro OOM).

Proč Podkladárna
----------------
Chtěl jsem vyzkoušet, jestli už jdou podklady pro orientační mapy skládat
automaticky. Inspirací bylo mapant.net a Karttapullautin; myšlenka se ale
posunula k editovatelným podkladům v OpenOrienteering Mapperu, ne jen
k rastrovému náhledu. Program vznikl s pomocí AI, samotné generování ale
běží postaru, jasně danými algoritmy.

Kvalita podkladu
----------------
Hlavní výstup jsou vektory ve .omap (cesty, plochy, budovy, vrstevnice…);
PNG z Karttapullautinu je hlavně náhled. Data ČÚZK i OSM nejsou dokonalá
a automatika je jen skládá dohromady – něco chybí, něco se překrývá.
Tento balíček je pracovní podklad, ne hotová mapa; v OOM s ním ještě
budete kreslit.

PNG a ZIP nevypadají 1:1 – editovatelné vrstvy se skládají z více zdrojů.

Co je uvnitř
------------
- *-sprint/les/mtbo.omap … podle názvu projektu a měřítka (cesty OSM), otevřete v OpenOrienteering Mapper (OOM)
- kp/                  … PNG náhledy Karttapullautin (zeleň + deprese)
- base/                … vrstevnice GDAL (contours_gdal.*), vrstevnice KP (contours_kp.dxf), vegetace, srázy/knolly
- osm/                 … OSM shapefile vrstvy pro ruční skládání (cesty, posedy, studny, budovy, …)
- zabaged/             … polohopis ZABAGED (shapefile včetně budov + RUIAN_budovy.shp; výchozí budovy v .omap jsou z OSM)
- references/          … ortofoto, OSM, ZTM, katastr, náhled DMP OK, hillshade (jen pro kreslení, ne do tisku)
- README_OOM.txt       … podrobný postup v OOM
- metadata.json        … měřítko, preset, CRS

Co s tím
--------
1. Nainstalujte OOM (openorienteering.org).
2. Rozbalte ZIP. Dvojklik na vybraný *-sprint.omap / *-les.omap / *-mtbo.omap nebo File → Open.
3. Importujte DXF a SHP dle README_OOM.txt a přiřaďte symboliku ISOM/ISSOM.
4. Kreslete mapu. Referenční vrstvy po dokončení vypněte nebo smažte.

OCAD neotevře .omap – použijte DXF, SHP nebo georeferencované PNG+PGW,
případně export z OOM do OCD (v8–12).

Autor a kontakt
---------------
Autorem Podkladárny je Petr Janeček. Služba běží na
https://podkladarna.kibos.link

Budu vděčný za jakékoli dotazy, připomínky i podněty k vylepšení —
napište mi nebo zavolejte:
  e-mail:   janecek@datais.cz
  telefon:  733 575 541

Technické hlášení chyb můžete také založit na GitHubu:
https://github.com/pjanecekATlangmaster/podkladarna/issues

Právní
------
Kód Podkladárny: MIT. Výstup jobu (PNG, .omap, …): CC BY 4.0 –
při šíření uveďte: „Podklad: Podkladárna · ČÚZK · OSM · Karttapullautin, [rok]“.
Data ČÚZK (DMR 5G, DMP OK, ZABAGED®, RÚIAN/INSPIRE, ortofoto) – CC BY 4.0.
AOPK památné stromy (CC BY 4.0). OSM © přispěvatelé (ODbL).
Reliéf: Karttapullautin (GPL-3.0).

"""
