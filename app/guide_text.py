"""Srozumitelný popis služby pro web a ZIP."""

WEB_ABOUT_HTML = """
<p>
  Chtěl jsem vyzkoušet, jestli už jdou podklady pro orientační mapy
  skládat automaticky. Program vznikl s pomocí AI, samotné generování
  ale běží postaru, jasně danými algoritmy. Stáhnou se data ČÚZK,
  Karttapullautin je zpracuje a složí se balíček do
  <a href="https://www.openorienteering.org/" target="_blank" rel="noopener">OpenOrienteering Mapper</a>.
</p>
<p>
  Z výřezu na mapě si Podkladárna stáhne LiDAR (DMR 5G + DMP OK) a polohopis
  ZABAGED. Výsledek je jen tak dobrý, jaká jsou data: něco chybí, něco je
  dvakrát (třeba silnice na mostě) a automatika to vždycky nerozsoudí.
  Berte to jako <em>pracovní podklad</em>, ne hotovou mapu. V OOM s tím
  ještě budete kreslit.
</p>
<p>
  Na webu uvidíte PNG z Karttapullautinu, spíš rychlý náhled než finální kresbu.
  ZIP skládá editovatelné vektory z víc zdrojů (vrstevnice, zeleň, ZABAGED, OSM,
  DXF srázů, referenční orto…), takže PNG a ZIP nevypadají úplně stejně.
  Výchozí je PNG + ZIP; můžete nechat jen náhled.
</p>
<p>Ve ZIPu je mimo jiné:</p>
<ul>
  <li><code>podkladarna.omap</code> – otevřete v OOM (ortofoto, OSM, ZTM, katastr, DMP OK, hillshade, reliéf)</li>
  <li>DXF srázy, shapefile vrstevnic (GDAL) a ZABAGED, návod <code>README_OOM.txt</code></li>
</ul>
<p>
  Nakreslete obdélník (max 5 × 5&nbsp;km), vyberte typ mapy a spusťte generování.
  Sprint bývá hotový za 2–6 minut, lesní mapa trvá déle. Stránku mezitím
  můžete zavřít. Po dokončení stáhněte ZIP (pokud jste ho nechali generovat),
  v OOM otevřete <code>podkladarna.omap</code> a podle návodu doladíte symboliku.
  OCAD soubor <code>.omap</code> neotevře, DXF/SHP/PNG ano.
</p>
<p>
  Běží to na domácím NAS, takže najednou jede jen jeden job. Z jedné sítě
  můžou současně běžet nebo čekat nejvýš <strong>2 joby</strong>, za hodinu
  <strong>10</strong>. Kdo má oba sloty plné, ve frontě ustoupí tomu, kdo
  ještě nic nespustil. Podle IP to není stoprocentní (VPN, sdílená Wi‑Fi).
  Hotové joby držíme <strong>48 hodin</strong>, ZIP si uložte u sebe.
</p>
"""

ZIP_ABOUT_TXT = """Podkladárna – co je v tomto balíčku
==================================

Tento ZIP vygenerovala služba Podkladárna (LiDAR + ZABAGED → podklad pro orientační mapu).

Proč Podkladárna
----------------
Chtěl jsem vyzkoušet, jestli už jdou podklady pro orientační mapy skládat
automaticky. Program vznikl s pomocí AI, samotné generování ale běží postaru,
jasně danými algoritmy.

Kvalita podkladu
----------------
Výsledek je jen tak dobrý, jaká jsou data. V LiDARu, ZABAGEDu i OSM něco chybí,
něco je dvakrát a automatika to vždycky nerozsoudí. Tento balíček je pracovní
podklad, ne hotová mapa; v OOM s ním ještě budete kreslit.

PNG z Karttapullautinu na webu je hlavně náhled – editovatelné vektory a .omap jsou zde ve ZIPu
a skládají se z více zdrojů (ne 1:1 s PNG).

Co je uvnitř
------------
- podkladarna.omap     … otevřete v OpenOrienteering Mapper (OOM)
- basemap/             … reliéf a vegetace z LiDARu (Karttapullautin)
- karttapullautin/     … srázy a knolíky (DXF)
- contours/            … vrstevnice z PDAL/GDAL (shapefile)
- osm_paths/           … pěšiny z OpenStreetMap (ODbL) – geojson + shapefile OSM_cesty (po dedupu i do KP PNG); na sprintu i chodníky (footway=sidewalk) a v OOM přednost před ZABAGED Pesina/Cesta; studny/prameny, mokřad, jeskyně, hřiště/sportoviště, zahrady (520 oliva); volitelně lavičky / lampy / herní prvky; při prioritě OSM (hlavně sprint) ploty/zdi/brány/…
- zabaged/             … polohopis ZABAGED (shapefile)
- references/          … ortofoto, OSM, ZTM, katastr, náhled DMP OK, hillshade (jen pro kreslení, ne do tisku)
- README_OOM.txt       … podrobný postup v OOM
- metadata.json        … měřítko, preset, CRS

Co s tím
--------
1. Nainstalujte OOM (openorienteering.org).
2. Rozbalte ZIP. Dvojklik na podkladarna.omap nebo File → Open.
3. Importujte DXF a SHP dle README_OOM.txt a přiřaďte symboliku ISOM/ISSOM.
4. Kreslete mapu. Referenční vrstvy po dokončení vypněte nebo smažte.

OCAD neotevře .omap – použijte DXF, SHP nebo georeferencované PNG+PGW,
případně export z OOM do OCD (v8–12).

Právní
------
Data ČÚZK (DMR 5G, DMP OK, ZABAGED®, ortofoto) – licence CC BY 4.0.
Při šíření mapy uveďte: „Zdroj: ČÚZK, [rok]“. OSM © přispěvatelé (ODbL).
Reliéf: Karttapullautin (GPL-3.0).

"""
