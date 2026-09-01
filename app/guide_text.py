"""Srozumitelný popis služby pro web a ZIP."""

WEB_ABOUT_HTML = """
<p>
  <strong>Podkladárna</strong> je domácí služba pro orientační mapy: z výřezu na mapě
  stáhne data ČÚZK (LiDAR DMR 5G + DMP OK a polohopis ZABAGED), zpracuje je programem
  Karttapullautin a připraví balíček pro
  <a href="https://www.openorienteering.org/" target="_blank" rel="noopener">OpenOrienteering Mapper</a>.
</p>
<p><strong>Co dostanete ve ZIPu:</strong></p>
<ul>
  <li><code>podkladarna.omap</code> – mapový soubor s podklady (ortofoto, OSM, hillshade, reliéf)</li>
  <li>DXF vrstevnice a srázy, shapefile ZABAGED, návod <code>README_OOM.txt</code></li>
</ul>
<p><strong>Jak na to:</strong> nakreslete obdélník (max 5×5&nbsp;km), vyberte typ mapy, spusťte generování.
  Sprint obvykle 2–6 minut, lesní mapa déle; stránku mezitím můžete zavřít.
  Po dokončení stáhněte ZIP. V OOM otevřete <code>podkladarna.omap</code> a dle návodu
  doladíte symboliku. Do OCADu jde import DXF/SHP/PNG, ne přímo <code>.omap</code>.</p>
<p><strong>Proč fronta a limity:</strong> server zvládne jen jedno generování najednou a běží na domácím NAS.
  Z jedné sítě (IP) mohou současně běžet nebo čekat nejvýše <strong>2 joby</strong>,
  za hodinu nejvýše <strong>10</strong>. Kdo už oba sloty využil, ustoupí ve frontě tomu,
  kdo ještě ne – férovější sdílení pro více mapmakerů. Ověření podle IP není spolehlivé
  (VPN, sdílená Wi‑Fi).</p>
<p><strong>Uchování:</strong> hotové joby na serveru držíme <strong>48 hodin</strong> – ZIP si uložte lokálně.</p>
"""

ZIP_ABOUT_TXT = """Podkladárna – co je v tomto balíčku
==================================

Tento ZIP vygenerovala služba Podkladárna (LiDAR + ZABAGED → podklad pro orientační mapu).

Co je uvnitř
------------
- podkladarna.omap     … otevřete v OpenOrienteering Mapper (OOM)
- basemap/             … reliéf a vegetace z LiDARu (Karttapullautin)
- karttapullautin/     … DXF vrstevnice a srázy
- vectors/             … polohopis ZABAGED (shapefile)
- references/          … ortofoto, OSM, hillshade (jen pro kreslení, ne do tisku)
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
