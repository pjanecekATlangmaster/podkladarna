"""Překlad technického logu jobu pro zobrazení na webu v jiném jazyce než češtině (Omapic).

Log jobu se dál zapisuje česky (``db.append_log``, ``log.txt``): web z něj parsuje průběh, ``source_gate`` výpadky
a stará data i testy počítají s češtinou. Překládá se až při čtení (``GET /api/jobs/{id}/log``) podle jazyka
stránky: čeština vrací řádek beze změny, jiný jazyk dostane anglický text v ``line`` a původní řádek v ``src``.

Pravidla jsou šablony celého řádku (bez odsazení a předpony ``[OSM] `` / ``- ``): ``{název}`` v české šabloně
zachytí libovolný text, v anglické se dosadí; filtr ``{název|t}`` přeloží hodnotu slovníkem ``TERMS``,
``{název|list}`` seznam oddělený `` · ``, ``{název|clist}`` seznam oddělený čárkou, ``{název|p}`` nahradí známé
fráze (``PHRASES``). Řádek, na který žádné pravidlo nesedí, zůstane česky (nic se neztratí); řádky s příkazy
(``> gdalwarp …``) se nepřekládají. Pokrytí na vzorových lozích měří ``tests/test_log_i18n.py``.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Callable

# Kroky průběhu (``job_progress``, hlavička „[3/17] Vrstevnice · 0:18“ a „Plán (…): …“) a úlohy na pozadí.
TERMS: dict[str, str] = {
    # kroky
    "kopie LAZ z předchozího jobu": "LAZ copy from the previous job",
    "stažení LiDAR": "LiDAR download",
    "stažení ZABAGED": "ZABAGED download",
    "mřížka mapy": "map grid",
    "příprava LiDAR": "LiDAR preparation",
    "výškové modely": "elevation models",
    "příprava ZABAGED": "ZABAGED preparation",
    "vrstevnice": "contours",
    "vegetace": "vegetation",
    "srázy": "cliffs",
    "kupky a ďolíky": "knolls and depressions",
    "OSM pěšiny a objekty": "OSM paths and objects",
    "stínování reliéfu": "relief shading",
    "referenční podklady": "reference layers",
    "RÚIAN a AOPK": "RÚIAN and AOPK",
    "DTM kraje": "regional DTM",
    "polohopis a vegetace": "planimetry and vegetation",
    "silnice a cesty": "roads and paths",
    "ploty, budovy a oliva": "fences, buildings and olive",
    "slučování zdrojů a odsun od cest": "merging sources and shifting away from paths",
    "zjednodušení a zápis mapy": "simplification and writing the map",
    "převod do OCAD": "conversion to OCAD",
    "náhled mapy": "map preview",
    "balení ZIP": "packing the ZIP",
    "sestavení mapy a ZIP": "building the map and ZIP",
    "balení výstupu": "packing the output",
    # úlohy na pozadí (``background_task``)
    "Podklady ortofoto, mapy a stínování": "Orthophoto, maps and hillshade",
    "Ortofoto": "Orthophoto",
    "Ortofoto pro ulice sprintu": "Orthophoto for sprint streets",
    "Ortofoto pro vegetaci": "Orthophoto for vegetation",
    "Katastrální mapa": "Cadastral map",
    "Katastr vektorově": "Cadastre (vector)",
    "Snímky Sentinel-2": "Sentinel-2 images",
    "Podklad OpenStreetMap": "OpenStreetMap base layer",
    "Mapa ZTM": "ZTM map",
    "Náhled DMP OK": "DMP OK preview",
    "Stínování reliéfu (podklady)": "Relief shading (base layers)",
    "Stínování reliéfu (DMR 5G)": "Relief shading (DMR 5G)",
    "Podklad DTM kraje – plochy (ZPS)": "Regional DTM base layer – areas (ZPS)",
    "Podklad DTM kraje – linie a body (ZPS)": "Regional DTM base layer – lines and points (ZPS)",
    # důvody přeskočení kroku (``progress.skip(..., reason=…)``)
    "online šablony": "online templates",
    "chybí georef šablona": "georeferenced template missing",
    "mimo Česko": "outside Czechia",
    "výšky z rastru": "elevation from raster",
    "chybí zdroj": "source missing",
    "kresba bez DTM": "drawing without DTM",
    "kresba z OSM a ZABAGED": "drawn from OSM and ZABAGED",
    "kresba z OSM": "drawn from OSM",
    "z předchozího jobu": "from the previous job",
    "vypnuto": "turned off",
    "Data OpenStreetMap": "OpenStreetMap data",
    "Data DTM kraje": "Regional DTM data",
    "Straßenbefahrung a ALKIS (GDI Berlin)": "Straßenbefahrung and ALKIS (GDI Berlin)",
    "Data ALKIS (katastr země)": "ALKIS data (state cadastre)",
    # obsah technických map („Kresba z …“) a kategorie ploch („[BGT] BGT budovy: 12 ploch“)
    "budovy": "buildings",
    "silnice": "roads",
    "silnice a cesty": "roads and paths",
    "chodníky": "sidewalks",
    "parkoviště": "car parks",
    "parkování": "parking",
    "ploty": "fences",
    "soukromé pozemky": "private plots",
    "schody": "steps",
    "schodiště": "stairs",
    "mosty": "bridges",
    "zahrady": "gardens",
    "vozovky": "carriageways",
    "uliční prostor": "street space",
    "pěší plochy": "pedestrian areas",
    "pěší plochy a náměstí": "pedestrian areas and squares",
    "zeleň": "greenery",
    "zeleň a parky": "greenery and parks",
    "zeleň v ulicích": "street greenery",
    "udržovaná zeleň": "maintained greenery",
    "dvory a zahrady": "courtyards and gardens",
    "náměstí": "squares",
    "náměstí a pěší zóny": "squares and pedestrian zones",
    "cyklostezky": "cycle paths",
    "smíšené plochy": "mixed areas",
    "ostrůvky": "traffic islands",
    "dělicí ostrůvky": "dividing islands",
    "ostrůvky a ostatní zpevněné plochy": "traffic islands and other paved areas",
    "ostatní zpevněné plochy": "other paved areas",
    "zpevněné plochy": "paved areas",
    "zpevněná náměstí": "paved squares",
    "trávníky": "lawns",
    "vinice a sady": "vineyards and orchards",
    "jiný kryt (les, louka, pole)": "other cover (forest, meadow, field)",
    "jiný kryt (les, vřes, louka, pole)": "other cover (forest, heath, meadow, field)",
    "chodníky a pěší plochy": "sidewalks and pedestrian areas",
    "chodníky a pěší zóny": "sidewalks and pedestrian zones",
    "chodníky a smíšené plochy": "sidewalks and mixed areas",
    "chodníky, pěší zóny a náměstí": "sidewalks, pedestrian zones and squares",
    "vozovky z okrajů silnic": "carriageways from road edges",
    "vozovky podle šířky silnic": "carriageways from road widths",
    "nezpevněné pásy v ulici": "unpaved strips in the street",
    "parkování v ulici": "on-street parking",
    "vozovky a chodníky (Brusel z UrbIS, i parkování v ulici)": "carriageways and sidewalks (Brussels from UrbIS, incl. on-street parking)",
    "trávníky (Helsinky, Espoo)": "lawns (Helsinki, Espoo)",
    # volby v hlavičce „Přijato“
    "les": "forest",
    "ano": "yes",
    "ne": "no",
    "vždy": "always",
    "adaptivní": "adaptive",
    "méně vyhlazení": "less smoothing",
    "základní ve sprintu": "basic in sprint",
    "žádné": "none",
    "všechny": "all",
    "stažené": "downloaded",
    "online": "online",
    "jen PNG": "PNG only",
    # kroky, které hlásí přeskočení / selhání
    "Zeleň z ortofota": "Green from orthophoto",
    "Ulice z katastru": "Streets from the cadastre",
    "Parcely komunikací z katastru": "Road parcels from the cadastre",
    "Chodníky u ulic z katastru": "Sidewalks along cadastre streets",
    "Ortofoto 401/403": "Orthophoto 401/403",
    "Stínování reliéfu": "Relief shading",
    "Stínování z WMS": "Shading from WMS",
    "Stínování z DEM": "Shading from the DEM",
    "Stínování z DEM (náhradní)": "Shading from the DEM (fallback)",
    "Vegetace": "Vegetation",
    "Vegetace (hustota bodů)": "Vegetation (point density)",
    "Vegetace: CLI polygonize": "Vegetation: CLI polygonize",
    "Silnice: měření napojení": "Roads: connection measurement",
    "Přílivová zóna": "Intertidal zone",
}

# Ordered substring replacements for free text inside a line (source descriptions, notes).
PHRASES: tuple[tuple[str, str], ...] = (
    ("mračna PNOA-LiDAR", "PNOA-LiDAR point clouds"),
    ("klasifikovaného mračna LiDAR", "classified LiDAR point cloud"),
    ("mračna LiDAR", "LiDAR point clouds"),
    ("mračno LiDAR", "LiDAR point cloud"),
    ("vlády Navarry", "Government of Navarre"),
    ("baskické vlády", "Basque Government"),
    ("skotské vlády", "Scottish Government"),
    ("velšské vlády", "Welsh Government"),
    ("Valencijského společenství", "Valencian Community"),
    ("Kastilie a Leónu", "Castile and León"),
    ("Katalánska", "Catalonia"),
    ("Andalusie", "Andalusia"),
    ("celostátní mozaika LiDARu, Nový Zéland", "national LiDAR mosaic, New Zealand"),
    ("vlastních dat zadavatele", "the requester's own data"),
    ("výška porostu", "canopy height"),
    ("porost = menší ze zimního lidaru a letních snímků", "canopy = lower of winter LiDAR and summer imagery"),
    ("povrch z letních snímků", "surface from summer imagery"),
    ("povrch ze snímků", "surface from imagery"),
    ("terén a povrch", "terrain and surface"),
    ("bodů/m²", "points/m²"),
    ("Berlín", "Berlin"),
    ("Šlesvicko-Holštýnsko", "Schleswig-Holstein"),
    ("Vídeň", "Vienna"),
    ("Paříž", "Paris"),
    ("Flandry", "Flanders"),
    ("Obrázek OpenStreetMap", "OpenStreetMap image"),
    ("menší z výšek zimního DOMN (ALS) a letního nDOMN (snímky)",
     "lower of the winter DOM (ALS) and summer nDOM (imagery) heights"),
    ("nejvyšší body vegetace z mračna", "highest vegetation points from the point cloud"),
    ("buněk zvýšeno", "of cells raised"),
    ("vegetace/srázy/skály pouzite+vyhozene", "vegetation/cliffs/rocks used+discarded"),
    ("vegetace/srázy/skály v_plne_mape+vyrazene_filtry", "vegetation/cliffs/rocks in_full_map+filtered_out"),
    ("tam se kreslí jen z OSM, bez místní velkoměřítkové mapy; terén a porost tam nemusí být "
     "(národní výšková data končí na hranici).",
     "there it is drawn from OSM only, without a local large-scale map; terrain and vegetation may be missing "
     "(national elevation data end at the border)."),
    ("tam se kreslí jen z OSM, bez místní velkoměřítkové mapy.",
     "there it is drawn from OSM only, without a local large-scale map."),
    ("tam se kreslí jen z OSM (bez terénu z LiDARu, ZABAGED a DTM).",
     "there it is drawn from OSM only (without LiDAR terrain, ZABAGED and DTM)."),
    ("národní mapa", "national map"),
    ("jako šablony", "as templates"),
    ("Šablony ortofota, mapy a katastru budou chybět.", "Orthophoto, map and cadastre templates will be missing."),
    ("bez osy", "without axis"),
    ("z okrajů", "from edges"),
    ("uzlů EPT", "EPT nodes"),
    ("mil. bodů ve výřezu", "million points in the area"),
    ("Brusel", "Brussels"),
    ("Helsinky", "Helsinki"),
    ("katastr země", "state cadastre"),
    (" – sprint ho kreslí jako ulici v celé šířce", " – sprint draws it as a street in full width"),
    (" a ", " and "),
)

# Pravidla: (česká šablona celého řádku, anglická šablona). První shoda vyhrává, pořadí = od konkrétních k obecným.
RULES: tuple[tuple[str, str], ...] = (
    # --- hlavička jobu (main.py) ---
    ("Přijato: preset {p} · 1:{s} · ekvidistance {e} m · země {c|country} · mapy les + MTBO",
     "Accepted: preset {p} · 1:{s} · contour interval {e} m · country {c} · maps forest + MTBO"),
    ("Přijato: preset {p} · 1:{s} · ekvidistance {e} m · země {c|country} · mapa {m|t}",
     "Accepted: preset {p} · 1:{s} · contour interval {e} m · country {c} · map {m}"),
    ("Přijato: preset {p} · 1:{s} · ekvidistance {e} m · země {c|country}",
     "Accepted: preset {p} · 1:{s} · contour interval {e} m · country {c}"),
    ("Přijato: preset {p} · 1:{s} · ekvidistance {e} m · listy SM5 {l}{r}",
     "Accepted: preset {p} · 1:{s} · contour interval {e} m · SM5 sheets {l}{r|accepted_tail}"),
    ("Terén: vegetace {v} m · tvar vegetace {t} · vrstevnice {c|t} · kresba sprint {s}/les+MTBO {f} · srázy {k} · "
     "pomocné vrstevnice {h|t}",
     "Terrain: vegetation {v} m · vegetation shape {t} · contours {c} · drawing sprint {s}/forest+MTBO {f} · "
     "cliffs {k} · form lines {h}"),
    ("OSM: objekty {o|t} · ploty a zdi vždy · footway jako chodník {f|t}",
     "OSM: objects {o} · fences and walls always · footway as sidewalk {f}"),
    ("Sídla: residential 501 {r|yn} · ostatní plocha {o}",
     "Settlements: residential 501 {r} · other area {o}"),
    ("Výstup: {z|t} · podklady {r|t} (Sentinel-2 {s|t}) · georef GeoTIFF {g|t} · vynucené obnovení {f|t} · "
     "privátní {p|t}{rest|output_tail}",
     "Output: {z} · reference layers {r} (Sentinel-2 {s}) · georef GeoTIFF {g} · forced refresh {f} · "
     "private {p}{rest}"),
    ("{p} % výřezu leží mimo {c|country}: {rest|p}", "{p} % of the area lies outside {c}: {rest}"),
    ("Job ve fronte – pozice {n}.", "Job queued – position {n}."),
    # --- stav zdrojů na startu a konci jobu (run_job.down_source_lines, resolve_source_issues) ---
    ("Zdroj {l|cat} při poslední kontrole neodpovídal ({d|cat}); mapa se kreslí z OSM a ZABAGED.",
     "Source {l} did not respond at the last check ({d}); the map is drawn from OSM and ZABAGED."),
    ("Zdroj {l|cat} má plánovanou odstávku ({d|cat}); mapa se kreslí z OSM a ZABAGED.",
     "Source {l} has a planned outage ({d}); the map is drawn from OSM and ZABAGED."),
    ("Zdroj {l|cat} při poslední kontrole neodpovídal ({d|cat}): {i|cat} Ověří se při stahování.",
     "Source {l} did not respond at the last check ({d}): {i} It will be checked during the download."),
    ("Zdroj {l|cat} má plánovanou odstávku ({d|cat}): {i|cat} Ověří se při stahování.",
     "Source {l} has a planned outage ({d}): {i} It will be checked during the download."),
    ("Zdroj {l|cat} při poslední kontrole neodpovídal ({d|cat}): {i|cat}",
     "Source {l} did not respond at the last check ({d}): {i}"),
    ("Zdroj {l|cat} má plánovanou odstávku ({d|cat}): {i|cat}", "Source {l} has a planned outage ({d}): {i}"),
    ("Zdroj {l|cat} byl při kontrole na startu dočasně nedostupný, data se nakonec stáhla "
     "(záložní zrcadlo, opakovaný pokus nebo mezipaměť).",
     "Source {l} was temporarily unavailable at the start check, but the data were downloaded in the end "
     "(backup mirror, retry or cache)."),
    ("Job selhal na výpadku zdroje {l|cat}; do limitů se nepočítá.",
     "The job failed due to an outage of {l}; it does not count towards the limits."),
    # --- konec jobu, e-maily, chyby (job_worker, paid_flow) ---
    ("CHYBA: Na serveru došlo místo na disku – uvolněte místo v /data (cache, staré joby).",
     "ERROR: The server ran out of disk space – free up space in /data (cache, old jobs)."),
    ("CHYBA odeslání kopie e-mailu o chybě: {e}", "ERROR sending the copy of the failure e-mail: {e}"),
    ("CHYBA odeslání e-mailu o chybě: {e}", "ERROR sending the failure e-mail: {e}"),
    ("CHYBA odeslání e-mailu: {e}", "ERROR sending the e-mail: {e}"),
    ("CHYBA: privátní job bez download tokenu.", "ERROR: private job without a download token."),
    ("CHYBA: {e|cat}", "ERROR: {e}"),
    ("Privátní job bez e-mailu – odkaz neodeslán.", "Private job without an e-mail – link not sent."),
    ("E-mail s odkazem odeslán na {a}.", "E-mail with the link sent to {a}."),
    ("E-mail o chybě odeslán na {a}.", "Failure e-mail sent to {a}."),
    ("E-mail s náhledem objednávky odeslán na {a}.", "E-mail with the order preview sent to {a}."),
    ("Mezivýsledky objednávky smazány ({m} MB).", "Intermediate order results deleted ({m} MB)."),
    ("Vlastní výšková data zadavatele smazána.", "The requester's own elevation data deleted."),
    # --- obecné hlášky o selhání kroků (⚠ se odřízne jako předpona) ---
    ("Stažení {l|p} selhalo ({e}), pokus {a} za {w} s …", "Download of {l} failed ({e}), attempt {a} in {w} s …"),
    ("{x|p}: kontrola novější verze selhala ({e}), použita data z cache",
     "{x}: check for a newer version failed ({e}), cached data used"),
    ("{x|p}: stažení novější verze selhalo ({e}), použita data z cache",
     "{x}: download of a newer version failed ({e}), cached data used"),
    ("{x|p}: obnova selhala ({e}), beru starší kopii z cache", "{x}: refresh failed ({e}), using an older cached copy"),
    ("Sentinel-2 nedostupný ({e}) – beze změny", "Sentinel-2 unavailable ({e}) – unchanged"),
    ("Vegetace: zimní snímek Sentinel-2 nedostupný ({e}) – zrnitost vypnutá",
     "Vegetation: winter Sentinel-2 image unavailable ({e}) – texture turned off"),
    ("Herní prvky: ortofoto / CHM nedostupné ({e})", "Playground equipment: orthophoto / CHM unavailable ({e})"),
    ("Ortofoto: výřez je příliš velký ({d} px), vynecháno", "Orthophoto: the area is too large ({d} px), skipped"),
    ("Geoid EGM08-REDNAP se nestáhl ({e}): výšky zůstanou elipsoidické (o ~50 m vyšší).",
     "Geoid EGM08-REDNAP was not downloaded ({e}): heights stay ellipsoidal (~50 m higher)."),
    ("Kvazigeoid SK se nestáhl ({e}): výšky zůstanou elipsoidické (o ~40–46 m vyšší).",
     "SK quasigeoid was not downloaded ({e}): heights stay ellipsoidal (~40–46 m higher)."),
    ("Zpevněné plochy z ortofota: ořez o kresbu přeskočen ({e})",
     "Paved areas from orthophoto: trimming to the drawing skipped ({e})"),
    # --- průběh (job_progress, job_worker, run_job) ---
    ("Start jobu {id} · preset {p}", "Job {id} started · preset {p}"),
    ("Plán ({n} kroků): {s|list}", "Plan ({n} steps): {s}"),
    ("[{i}/{n}] {s|step} – nedostupné ({r|p})", "[{i}/{n}] {s} – unavailable ({r})"),
    ("[{i}/{n}] {s|step} – nedostupné", "[{i}/{n}] {s} – unavailable"),
    ("[{i}/{n}] {s|step} – přeskočeno ({r|t})", "[{i}/{n}] {s} – skipped ({r})"),
    ("[{i}/{n}] {s|step} – přeskočeno", "[{i}/{n}] {s} – skipped"),
    ("[{i}/{n}] {s|step} – převzato z předchozího jobu", "[{i}/{n}] {s} – taken over from the previous job"),
    ("[{i}/{n}] {s|step} · {t}", "[{i}/{n}] {s} · {t}"),
    ("[{i}/{n}] {s|step}", "[{i}/{n}] {s}"),
    ("Krok {s|step} přeskočen ({r})", "Step {s} skipped ({r})"),
    ("Na pozadí: {b|bg} · běží", "Background: {b} · running"),
    ("Na pozadí: {b|bg} · hotovo za {t}", "Background: {b} · done in {t}"),
    ("Na pozadí: {b|bg} · z mezipaměti", "Background: {b} · from cache"),
    ("Na pozadí: {b|bg} · nedostupné", "Background: {b} · unavailable"),
    ("Na pozadí: {b|bg} · selhalo{rest}", "Background: {b} · failed{rest}"),
    ("Hotovo za {t}, s chybami ({n}).", "Done in {t}, with errors ({n})."),
    ("Hotovo za {t}.", "Done in {t}."),
    ("Dokončeno s chybami ({n}):", "Completed with errors ({n}):"),
    ("Shrnutí pro uživatele: {s}", "Summary for the user: {s}"),
    ("Status: {s}", "Status: {s}"),
    ("Země výřezu: {c|country} – kresba z {d|p}, výšky z {h|p}.",
     "Country of the area: {c} – drawn from {d}, elevation from {h}."),
    ("Kresba: z OSM (technická mapa pro toto území není).", "Drawing: from OSM (no technical map for this area)."),
    ("Kresba: z OSM (technická mapa země zatím není zapojená).",
     "Drawing: from OSM (the country's technical map is not connected yet)."),
    ("Kresba z {s}: ano – {s2} ({c|clist}) se stahuje na pozadí, do mapy jde při sestavení.",
     "Drawing from {s}: yes – {s2} ({c}) is downloaded in the background and added when the map is built."),
    ("Kresba z {s}: ne – mapa se kreslí z OSM.", "Drawing from {s}: no – the map is drawn from OSM."),
    ("Start stahování OSM na pozadí; výsledek se převezme v kroku OSM.",
     "OSM download started in the background; the result is used in the OSM step."),
    ("Start přípravy {s} na pozadí.", "Preparation of {s} started in the background."),
    ("Čekám na stažení OSM z pozadí…", "Waiting for the background OSM download…"),
    ("Čekám na {s} z pozadí…", "Waiting for {s} from the background…"),
    ("Sonda výřezu: zdroje pokrývají celý výřez.", "Area check: the sources cover the whole area."),
    ("Sonda výřezu: Ve výřezu není technická mapa ({s}), budovy, silnice a plochy se kreslí z OSM.",
     "Area check: there is no technical map ({s}) in the area; buildings, roads and areas are drawn from OSM."),
    ("Data výřezu: Výška porostu je z leteckých snímků, ne z laseru: zeleň v lese bude jen základní.",
     "Area data: canopy height comes from aerial imagery, not laser: forest green will be basic only."),
    # --- mřížka a výšky ---
    ("Kanonická mřížka {rest}", "Canonical grid {rest}"),
    ("Stahuji výšková data ({s|p}): terén a povrch jako rastr…",
     "Downloading elevation data ({s}): terrain and surface as raster…"),
    ("Terén z rastru: {p}% buněk s daty, rozlišení {r} m", "Terrain from raster: {p}% of cells with data, resolution {r} m"),
    ("Výšky převedeny z elipsoidických na národní systém (kvazigeoid {r} m).",
     "Heights converted from ellipsoidal to the national system (quasigeoid {r} m)."),
    ("Povrch = {a} + menší z výšek zimního {b} (ALS) a letního {c} (snímky)",
     "Surface = {a} + lower of the winter {b} (ALS) and summer {c} (imagery) heights"),
    ("Povrch = {rest|p}", "Surface = {rest}"),
    ("Mračno LiDAR {y} (vegetace, COPC do {d} m): {n} dlaždic, staženo {m} MB",
     "LiDAR point cloud {y} (vegetation, COPC to {d} m): {n} tiles, {m} MB downloaded"),
    ("{p|p}: {n} uzlů EPT, {m} mil. bodů ve výřezu, {s} MB",
     "{p}: {n} EPT nodes, {m} million points in the area, {s} MB"),
    # --- vrstevnice ---
    ("Vrstevnice GDAL: interval {i} m (pomocné 103: {f}), DEM {d} m, smooth {s} m, Chaikin {c}, min_délka {m} m, stitch {t} m",
     "Contours GDAL: interval {i} m (form lines 103: {f}), DEM {d} m, smooth {s} m, Chaikin {c}, min_length {m} m, stitch {t} m"),
    ("Vrstevnice: sdílený DMR dem_filled ({p}) – stejný zdroj jako hillshade (ne DMP)",
     "Contours: shared DTM dem_filled ({p}) – same source as the hillshade (not DSM)"),
    ("Vyhlazuji DEM (plynulejší vrstevnice)…", "Smoothing the DEM (smoother contours)…"),
    ("Adaptivně vyhlazuji DEM pro vrstevnice (rovina víc, členitý terén jako dosud)…",
     "Adaptively smoothing the DEM for contours (more on flat ground, rugged terrain as before)…"),
    ("Vrstevnice adaptivně: vyhlazeno {a} % plochy (plně {b} %), σ' {s} m, rovina {r} mm, detail {d} m, zástavba {z}, zeleň beze změny {g}",
     "Adaptive contours: {a} % of the area smoothed (fully {b} %), σ' {s} m, flat {r} mm, detail {d} m, built-up {z}, green unchanged {g}"),
    ("Kreslím vrstevnice z DEM (ekvidistance do mapy)…", "Drawing contours from the DEM (map contour interval)…"),
    ("Varování: adaptivní vyhlazení vrstevnic selhalo ({e}); vrstevnice se kreslí s menším vyhlazením (původní).",
     "Warning: adaptive contour smoothing failed ({e}); contours are drawn with less smoothing (original)."),
    ("Spádnice: {n} na {m} uzavřených vrstevnicích prohlubní", "Slope lines: {n} on {m} closed depression contours"),
    ("Importuji vrstevnice do OOM: {rest}", "Importing contours into OOM: {rest}"),
    ("QA vrstevnice↔hillshade: OK – společný DMR dem_filled ({f}), ne DMP",
     "QA contours↔hillshade: OK – shared DTM dem_filled ({f}), not DSM"),
    # --- vegetace ---
    ("Vegetace (hustota bodů): chybí dem_filled.tif nebo LAZ – přeskočeno",
     "Vegetation (point density): dem_filled.tif or LAZ missing – skipped"),
    ("Klasifikuji vegetaci z CHM (podle výšky porostu)…", "Classifying vegetation from the CHM (by canopy height)…"),
    ("Vegetace: drobné plošky vyplněny okolím ({n} px)", "Vegetation: small patches filled from surroundings ({n} px)"),
    ("Vegetace: {n} polygonů → vegetation.shp ({r})", "Vegetation: {n} polygons → vegetation.shp ({r})"),
    ("Vegetace: {n} polygonu -> vegetation.shp ({r})", "Vegetation: {n} polygons -> vegetation.shp ({r})"),
    ("Převádím třídy vegetace na polygony (vrstvy do mapy)…", "Converting vegetation classes to polygons (map layers)…"),
    ("Vegetace: zeleň ze zrnitosti porostu – {r}", "Vegetation: green from canopy texture – {r}"),
    ("Vegetace: zeleň z podílu nízkého patra – {r}", "Vegetation: green from the share of the low layer – {r}"),
    ("Vegetace: zrnitost by zazelenala {p} % lesa – zeleň z podílu nízkého patra (pojistka)",
     "Vegetation: texture would make {p} % of the forest green – green from the share of the low layer (safeguard)"),
    ("Vegetace: zimní výšky – v lese z OSM nízký porost jako bílý les ({p} % výřezu)",
     "Vegetation: winter heights – low vegetation in OSM forest as white forest ({p} % of the area)"),
    ("Výška vegetace: {f} {d} px ({r} m/px)", "Vegetation height: {f} {d} px ({r} m/px)"),
    # --- srázy, kupky, balvany ---
    ("Citlivost {s}: {rest}", "Sensitivity {s}: {rest}"),
    ("Hledám srázy na DEM (drop≥{a} m, skála≥{b} m / sklon≥{c}; krátké ticky a plochy řeší merge v OOM)…",
     "Searching for cliffs on the DEM (drop≥{a} m, rock≥{b} m / slope≥{c}; short ticks and areas are merged in OOM)…"),
    ("Srázy DEM: {a} zemních + {b} skalních ticků (zem drop≥{c} m / skála≥{d} m, major≥{e} m, sklon≥{f}; body 204 z šumu DEM nevznikají) → temp/",
     "DEM cliffs: {a} earth + {b} rock ticks (earth drop≥{c} m / rock≥{d} m, major≥{e} m, slope≥{f}; no 204 points from DEM noise) → temp/"),
    ("Hledám kupky, ďolíky a jámy na DEM (jen výrazné)…", "Searching for knolls, depressions and pits on the DEM (distinct only)…"),
    ("Kupky/ďolíky DEM: {a} kupek 109 (≥{b} m), {c} ďolíků 111, {d} jam 112 (≥{e} m) → temp/",
     "DEM knolls/depressions: {a} knolls 109 (≥{b} m), {c} depressions 111, {d} pits 112 (≥{e} m) → temp/"),
    ("Kupky a ďolíky: vypnuto ve formuláři", "Knolls and depressions: turned off in the form"),
    ("Hledám balvany na DEM (jen výrazné)…", "Searching for boulders on the DEM (distinct only)…"),
    ("Balvany DEM: {n} kandidátů 204 ({r}) → temp/", "DEM boulders: {n} candidates 204 ({r}) → temp/"),
    # --- OSM ---
    ("OSM Overpass ({s}): {n} prvků", "OSM Overpass ({s}): {n} elements"),
    ("OSM cache zásah ({n} prvků, stáří {h} h) – Overpass přeskakuji",
     "OSM cache hit ({n} elements, {h} h old) – skipping Overpass"),
    ("OSM dlaždice {n}…", "OSM tiles {n}…"),
    ("OSM: převzato z pozadí ({n} prvků)", "OSM: taken from the background ({n} elements)"),
    ("OSM cesty: {n} rozdělených úseků spojeno do souvislých linií",
     "OSM paths: {n} split sections joined into continuous lines"),
    ("OSM cesty (mix): {n} ponecháno ({k}), {d} duplicit/krátkých (z toho {o} OSM×OSM širší>užší), {s} přeskočeno (tag)",
     "OSM paths (mix): {n} kept ({k}), {d} duplicates/short (of which {o} OSM×OSM wider>narrower), {s} skipped (tag)"),
    ("OSM cesty (mix): {n} ponecháno ({k}), {d} duplicit/krátkých, {s} přeskočeno (tag)",
     "OSM paths (mix): {n} kept ({k}), {d} duplicates/short, {s} skipped (tag)"),
    ("OSM cesty (jen OSM): {n} ponecháno (bez ZABAGED/OSM×OSM dedup, {k} krátkých)",
     "OSM paths (OSM only): {n} kept (without ZABAGED/OSM×OSM dedup, {k} short)"),
    ("OSM cesty (jen OSM): {n} ponecháno (bez ZABAGED/OSM×OSM dedup)",
     "OSM paths (OSM only): {n} kept (without ZABAGED/OSM×OSM dedup)"),
    ("OSM objekty: {n} ({k})", "OSM objects: {n} ({k})"),
    ("OSM pěší zóny: {n} koridorů z paralelních linií sloučeno do plochy {c}",
     "OSM pedestrian zones: {n} corridors from parallel lines merged into area {c}"),
    ("Ukládám ruční OSM vrstvy do shapefile (import do mapy)…", "Saving manual OSM layers to shapefile (map import)…"),
    ("OSM ruční vrstvy: {n} SHP → {p}", "OSM manual layers: {n} SHP → {p}"),
    ("OSM budovy→SHP: {n} polygonů → {p}", "OSM buildings→SHP: {n} polygons → {p}"),
    # --- stínování a podklady ---
    ("Počítám stínování reliéfu z {f} (azimut {a}°, výška {h}°)…",
     "Computing relief shading from {f} (azimuth {a}°, altitude {h}°)…"),
    ("Stínování z DEM: {f} ({d} px)", "Shading from the DEM: {f} ({d} px)"),
    ("Stínování: {f} {d} px ({r} m/px)", "Shading: {f} {d} px ({r} m/px)"),
    ("Sentinel-2: {n} kandidátů za {d} dní do {t}", "Sentinel-2: {n} candidates in {d} days up to {t}"),
    ("Sentinel-2 {t}: mraky / sníh / bez dat ve výřezu {p} %", "Sentinel-2 {t}: clouds / snow / no data in the area {p} %"),
    ("Sentinel-2 letní snímek {t} (NDVI {r} m)", "Sentinel-2 summer image {t} (NDVI {r} m)"),
    ("Stahuji podklady místních služeb (zapnuté „Stáhnout referenční šablony předem“)…",
     "Downloading local service layers (“Download reference templates in advance” is on)…"),
    ("Podklady staženy předem: {n} rastrů, online zůstalo {o} šablon (z {t})",
     "Layers downloaded in advance: {n} rasters, {o} templates left online (of {t})"),
    ("Online šablony podkladů: {n} (stáhnou se až v Mapperu)", "Online reference templates: {n} (downloaded in Mapper)"),
    ("OpenStreetMap: vlastní render {d} px ({r} m/px), data {a} s, kresba {b} s",
     "OpenStreetMap: own render {d} px ({r} m/px), data {a} s, drawing {b} s"),
    ("OpenStreetMap: obrázek se nekreslí (chybí knihovna pycairo)",
     "OpenStreetMap: image not drawn (pycairo library missing)"),
    ("OpenStreetMap: obrázek se nepodařilo nakreslit (chybí data OSM)",
     "OpenStreetMap: the image could not be drawn (OSM data missing)"),
    ("Podklady: z licenčních důvodů nepřibaleno: {s|p} – vysvětlení v {f}, v .omap zůstává online šablona",
     "Layers: not included for licensing reasons: {s} – explained in {f}, the .omap keeps an online template"),
    ("Sestavuji mapu OOM (vrstevnice, zeleň a polohopis do .omap)…",
     "Building the OOM map (contours, green and planimetry into .omap)…"),
    # --- zpevněné plochy, ulice, stromy, herní prvky (mimo Česko) ---
    ("ortofoto země {d} px za {s} s", "country orthophoto {d} px in {s} s"),
    ("přeskočeno ({e})", "skipped ({e})"),
    ("Zpevněné plochy z ortofota: přeskočeno ({e})", "Paved areas from orthophoto: skipped ({e})"),
    ("ortofoto země není k dispozici – beze změny", "country orthophoto not available – unchanged"),
    ("Zpevněné plochy z ortofota: {n} kusů, {a} m² místo {b} (Sentinel-2 {t})",
     "Paved areas from orthophoto: {n} pieces, {a} m² instead of {b} (Sentinel-2 {t})"),
    ("Zpevněné plochy z ortofota ({x}): {n} ploch po ořezu o kresbu",
     "Paved areas from orthophoto ({x}): {n} areas after trimming to the drawing"),
    ("Zpevněné plochy z ortofota: bez ortofota země nebo letního snímku – beze změny",
     "Paved areas from orthophoto: no country orthophoto or summer image – unchanged"),
    ("Ulice z katastru ({x}): {n} ploch", "Streets from the cadastre ({x}): {n} areas"),
    ("Chodníky podél ulic z katastru ({x}): {n} nekresleno", "Sidewalks along cadastre streets ({x}): {n} not drawn"),
    ("Stromy na zpevněné ploše: bez ortofota přeskočeno", "Trees on paved areas: skipped without orthophoto"),
    ("Aleje ({x}): {n} linií, nekreslených kvůli zeleni / bílé {a}, bez stromů {b}, kreslených {c}, bodů {d} ({r})",
     "Tree rows ({x}): {n} lines, not drawn due to green / white {a}, without trees {b}, drawn {c}, points {d} ({r})"),
    ("Herní prvky: {n} hřišť (soukromá vynechána {p})", "Playground equipment: {n} playgrounds (private skipped {p})"),
    ("Herní prvky: {n} hřišť, křížky OSM {a}, z výšky a ortofota {b}, záložní {c} ({rest|playground})",
     "Playground equipment: {n} playgrounds, OSM crosses {a}, from heights and orthophoto {b}, fallback {c} ({rest})"),
    ("Silnice: napojení v křižovatkách {p} % ({a} z {b} kreslených ramen, {c} děr, {d} končí na ploše, "
     "{e} z {f} ústí proti pravidlu, {g} křižovatek OSM)",
     "Roads: junction connections {p} % ({a} of {b} drawn arms, {c} gaps, {d} end on an area, "
     "{e} of {f} mouths against the rule, {g} OSM junctions)"),
    ("Přílivová zóna: přeskočeno (u moře chybí výšky)", "Intertidal zone: skipped (no heights by the sea)"),
    ("Přílivová zóna: přeskočeno ({e})", "Intertidal zone: skipped ({e})"),
    ("Přílivová zóna: žádná (pod {h} m u moře není holá rovina)",
     "Intertidal zone: none (no bare flat land below {h} m by the sea)"),
    ("Přílivová zóna: {n} ploch, {a} ha pod {h} m (výška pobřeží z DTM) jako písčitá půda",
     "Intertidal zone: {n} areas, {a} ha below {h} m (coast height from DTM) as sandy ground"),
    # --- technické mapy zemí („[BGT] BGT budovy: 12 ploch“) ---
    ("{s}: {n} objektů z {k} vrstev, z toho UrbIS (Brusel) {u} ({rest})",
     "{s}: {n} objects from {k} layers, of which UrbIS (Brussels) {u} ({rest})"),
    ("{s}: {n} objektů z {k} vrstev ({rest})", "{s}: {n} objects from {k} layers ({rest})"),
    ("{s}: {n} aktuálních objektů z {k} dlaždic ({rest})", "{s}: {n} current objects from {k} tiles ({rest})"),
    ("{s}: {n} objektů z {k} dlaždic ({rest})", "{s}: {n} objects from {k} tiles ({rest})"),
    ("{s}: {n} objektů z {k} sad ({rest})", "{s}: {n} objects from {k} datasets ({rest})"),
    ("{s} ploty: {n} úseků → {c} napojených čar, {f} oplocení k vyplnění; {rest|fence_tail}: {o}",
     "{s} fences: {n} segments → {c} connected lines, {f} enclosures to fill; {rest}: {o}"),
    ("{s|src} {w|tp}: {n} ploch{rest|p}", "{s} {w}: {n} areas{rest}"),
    # --- skládání mapy (package_oom, preview) ---
    ("OOM: typ mapy – {rest|maptype}", "OOM: map type – {rest}"),
    ("OOM: {f} hotovo za {t}", "OOM: {f} done in {t}"),
    ("OOM: zjednodušení tvaru ({m} mm): {a} → {b} souřadnic", "OOM: shape simplification ({m} mm): {a} → {b} coordinates"),
    ("OOM: čistá verze – vynecháno {a} ploch zeleně z výšek, {b} ploch otevřeného terénu pod {c} ha (zůstává {d}), {e} objektů z DEM",
     "OOM: clean version – omitted {a} green areas from heights, {b} open-land areas below {c} ha ({d} kept), {e} objects from DEM"),
    ("OOM OCD: přeskočeno ({f}) – není PODKLADARNA_MAPPER_CONVERT / Mapper CLI",
     "OOM OCD: skipped ({f}) – no PODKLADARNA_MAPPER_CONVERT / Mapper CLI"),
    ("OOM OCD: šablony opraveny {n}× ({f})", "OOM OCD: templates fixed {n}× ({f})"),
    ("OOM OCD: {f} za {t}", "OOM OCD: {f} in {t}"),
    ("OOM georef: přeskočeno (output_georef vypnuto) – jen webový náhled.",
     "OOM georef: skipped (output_georef off) – web preview only."),
    ("OOM georef: pracovní PNG odstraněno, dodává se GeoTIFF ({f})",
     "OOM georef: working PNG removed, GeoTIFF delivered ({f})"),
    ("OOM georef: ZIP náhledů → {f} ({s}, {n} souborů)", "OOM georef: preview ZIP → {f} ({s}, {n} files)"),
    ("OOM georef: hotovo {n} variant ({rest})", "OOM georef: done, {n} variants ({rest})"),
    ("OOM georef: GeoTIFF → {f} ({e}, dlaždice + přehledky)", "OOM georef: GeoTIFF → {f} ({e}, tiles + overviews)"),
    ("OOM georef: Mapper {f} ({d} DPI, s grivací)", "OOM georef: Mapper {f} ({d} DPI, with grivation)"),
    ("OOM web: Mapper {f} ({d} DPI, s grivací)", "OOM web: Mapper {f} ({d} DPI, with grivation)"),
    ("OOM náhled: Pillow render preview.png ({d}, {n} objektů, bez deklinace)",
     "OOM preview: Pillow render preview.png ({d}, {n} objects, without declination)"),
    ("OOM náhled ({k}): {rest|p}", "OOM preview ({k}): {rest}"),
    ("OOM náhled: za {t}", "OOM preview: in {t}"),
    ("Balím výstupní ZIP (mapy, vektory, referenční podklady – u velkých výřezů i několik minut)…",
     "Packing the output ZIP (maps, vectors, reference layers – several minutes for large areas)…"),
    ("Vykresluji georeferencované náhledy (Mapper @ {d} DPI)…", "Rendering georeferenced previews (Mapper @ {d} DPI)…"),
    ("Balím ZIP jen s georeferencovanými náhledy…", "Packing a ZIP with georeferenced previews only…"),
    ("ZIP: zabaleno za {t}", "ZIP: packed in {t}"),
    ("uzitecne/: {n} souborů ({rest|p})", "uzitecne/: {n} files ({rest})"),
    ("Výstup: {f} ({n} souborů)", "Output: {f} ({n} files)"),
    ("Výstup: {f} ({m} MB)", "Output: {f} ({m} MB)"),
    # obecné „OOM: <soubor>.omap (preset, měřítko, zdroj)“ a velikost staženého souboru jsou bez češtiny
    # --- obecné řádky se jménem souboru / zdroje na začátku (až nakonec) ---
    ("{x|t} selhalo ({e}) – zkouším WMS…", "{x} failed ({e}) – trying WMS…"),
    ("{x|t}: přeskočeno ({e})", "{x}: skipped ({e})"),
    ("{x|t}: selhalo ({e})", "{x}: failed ({e})"),
    ("{x|t} selhalo ({e})", "{x} failed ({e})"),
    ("{f|p}: {n} dlaždic (okno výřezu)", "{f}: {n} tiles (area window)"),
    ("{f|p}: {n} dlaždic (okno výřezu, {r})", "{f}: {n} tiles (area window, {r})"),
    ("{f|p}: {n} dlaždic, {m} MB", "{f}: {n} tiles, {m} MB"),
    ("{f|p}: {n} dlaždic (z mezipaměti {c})", "{f}: {n} tiles ({c} from cache)"),
    ("{f|p}: dlaždice uloženy do cache", "{f}: tiles saved to cache"),
    ("{f|p}: {m} MB převedeno do cache", "{f}: {m} MB converted into cache"),
    ("{f|p}: {m} MB za {s} s", "{f}: {m} MB in {s} s"),
    ("{f|p}: přechodná chyba služby, pokus {n}", "{f}: temporary service error, attempt {n}"),
    ("{f|p}: mozaika k {r}", "{f}: mosaic as of {r}"),
    ("{f|p}: {m} MB ({k})", "{f}: {m} MB ({k})"),
    ("{f|p}: {m} MB", "{f}: {m} MB"),
)

# Delší řádky (traceback, výpis nástroje) se nepřekládají: šablony s několika ``.+?`` mají na nich
# kvadratickou cenu (15 000 znaků ≈ 6 s) a takové řádky stejně nejsou česká hláška pro uživatele.
MAX_LINE_CHARS = 1000

_PREFIX_RE = re.compile(r"^(\s*)((?:\[[A-Za-zÀ-ž][^\]\n]{0,23}\] )?)((?:- |⚠ )?)(.*)$", re.S)
_FIELD_RE = re.compile(r"\{(\w+)(?:\|(\w+))?\}")
_CZ_CHARS = re.compile(r"[ěščřžýáíéůúňťďĚŠČŘŽÝÁÍÉŮÚŇŤĎ]")


def _term(value: str) -> str:
    if value in TERMS:
        return TERMS[value]
    low = value[:1].lower() + value[1:]
    if low in TERMS:
        out = TERMS[low]
        return out[:1].upper() + out[1:]
    return value


def _phrases(value: str) -> str:
    for cs, en in PHRASES:
        value = value.replace(cs, en)
    return value


def _step(value: str) -> str:
    """Krok průběhu; „data BGT“ → „BGT data“."""
    out = _term(value)
    if out != value:
        return out[:1].upper() + out[1:] if value[:1].isupper() else out
    m = re.fullmatch(r"([Dd])ata (.+)", value)
    if m:
        return f"{m.group(2)} data"
    return value


def _bg(value: str) -> str:
    """Úloha na pozadí; „Data BGT (PDOK)“ → „BGT data (PDOK)“."""
    out = _term(value)
    if out != value:
        return out
    m = re.fullmatch(r"Data (\S+)( \(.+\))?", value)
    if m:
        return f"{m.group(1)} data{_phrases(m.group(2) or '')}"
    return _phrases(value)


@lru_cache(maxsize=1)
def _country_names() -> dict[str, str]:
    try:
        from app.countries import load_countries

        names: dict[str, str] = {}
        for c in load_countries().values():
            cs, en = c.name("cs"), c.name("en")
            names[cs] = en
            if cs.count(" – ") == en.count(" – "):
                names.update(zip(cs.split(" – "), en.split(" – ")))
        return names
    except Exception:  # noqa: BLE001 – překlad logu nesmí shodit API
        return {}


def _country(value: str) -> str:
    names = _country_names()
    if value in names:
        return names[value]
    return " – ".join(names.get(part, _phrases(part)) for part in value.split(" – "))


@lru_cache(maxsize=4)
def _reverse_catalog_map(lang: str = "en") -> dict[str, str]:
    """Česká hodnota katalogu bez proměnných → anglická (názvy a dopady zdrojů, hlášky API)."""
    try:
        from app import i18n

        cs, en = i18n.catalog("cs"), i18n.catalog(lang)
    except Exception:  # noqa: BLE001
        return {}
    return {v: en[k] for k, v in cs.items() if "{" not in v and k in en and en[k] != v}


def _reverse_catalog(value: str) -> str | None:
    return _reverse_catalog_map().get(value)


def _tp(value: str) -> str:
    """Název kategorie s volitelnou závorkou v původním jazyce: „chodníky (Trottoir)“."""
    m = re.fullmatch(r"(.+?)( \(.+\))", value)
    if m and m.group(1) in TERMS:
        return TERMS[m.group(1)] + _phrases(m.group(2))
    return _term(value)


_TAILS: dict[str, tuple[tuple[str, str], ...]] = {
    "accepted_tail": ((" · mapy les + MTBO", " · maps forest + MTBO"), (" · mapa les", " · map forest"),
                      (" · mapa MTBO", " · map MTBO")),
    "output_tail": ((" · API klient ", " · API client "), (" · ukázka (generuje server)", " · sample (generated by the server)")),
    "maptype": (("jen les (vybráno v GUI)", "forest only (selected in the GUI)"),
                ("jen MTBO (vybráno v GUI)", "MTBO only (selected in the GUI)"),
                ("les + MTBO (vybráno v GUI)", "forest + MTBO (selected in the GUI)"),
                ("vybráno v GUI", "selected in the GUI"), ("jen ", "only "), ("les", "forest")),
    "fence_tail": (("soukromé pozemky (GRB je nevede) jako oliva", "private plots (not in GRB) as olive"),
                   ("soukromé pozemky (erf) jako oliva", "private plots (erf) as olive"),
                   ("soukromé pozemky jako oliva", "private plots as olive"),
                   ("zahrady u budov (Gartenanlage) jako oliva", "gardens by buildings (Gartenanlage) as olive"),
                   ("dvory a zadní zahrady jako oliva", "courtyards and back gardens as olive"),
                   ("soukromé dvory a zahrady (eraõu) jako oliva", "private courtyards and gardens (eraõu) as olive"),
                   (" jako oliva", " as olive")),
    "playground": (("OSM bodů v hřištích", "OSM points in playgrounds"), ("soukromá vynechána", "private skipped"),
                   ("výška", "height"), ("ortofoto", "orthophoto"), ("slunce z výřezu", "sun from the area")),
}


# Předpony řádků z úloh na pozadí („[Zpevněné plochy] …“).
TAGS: dict[str, str] = {"[Zpevněné plochy] ": "[Paved areas] "}


def _split_top(value: str, sep: str = ", ") -> list[str]:
    """Rozdělí seznam podle ``sep`` mimo závorky."""
    out, depth, cur, i = [], 0, "", 0
    while i < len(value):
        ch = value[i]
        if depth == 0 and value.startswith(sep, i):
            out.append(cur)
            cur, i = "", i + len(sep)
            continue
        depth += ch == "("
        depth -= ch == ")"
        cur += ch
        i += 1
    out.append(cur)
    return out


def _item(value: str) -> str:
    """Položka výčtu; „vozovky a chodníky“ po částech."""
    out = _tp(value)
    if out != value:
        return out
    parts = value.split(" a ")
    if len(parts) > 1 and all(p in TERMS for p in parts):
        return " and ".join(TERMS[p] for p in parts)
    return _phrases(value)


def _filter(name: str | None) -> Callable[[str], str]:
    if not name:
        return lambda v: v
    if name == "t":
        return _term
    if name == "step":
        return _step
    if name == "bg":
        return _bg
    if name == "list":
        return lambda v: " · ".join(_step(x) for x in v.split(" · "))
    if name == "clist":
        return lambda v: ", ".join(_item(x) for x in _split_top(v))
    if name == "p":
        return _phrases
    if name == "cat":
        return lambda v: _reverse_catalog(v) or _phrases(v)
    if name == "country":
        return _country
    if name == "tp":
        return _tp
    if name == "yn":
        return lambda v: "/".join(_term(x) for x in v.split("/"))
    if name == "src":
        return lambda v: v
    tails = _TAILS[name]

    def apply(v: str) -> str:
        for cs, en in tails:
            v = v.replace(cs, en)
        return v

    return apply


def _compile(cs: str) -> tuple[re.Pattern[str], dict[str, str | None]]:
    parts: list[str] = []
    filters: dict[str, str | None] = {}
    pos = 0
    for m in _FIELD_RE.finditer(cs):
        parts.append(re.escape(cs[pos:m.start()]))
        name, filt = m.group(1), m.group(2)
        filters[name] = filt
        if filt == "src":
            parts.append(rf"(?P<{name}>[A-Z][A-Za-z0-9ßäöüÄÖÜ+ ]*?)")
        elif filt == "tp":
            parts.append(rf"(?P<{name}>[a-zěščřžýáíéůúňťď][^:]*?)")
        elif name.startswith("rest"):
            parts.append(rf"(?P<{name}>.*?)")
        else:
            parts.append(rf"(?P<{name}>.+?)")
        pos = m.end()
    parts.append(re.escape(cs[pos:]))
    return re.compile("".join(parts), re.S), filters


@lru_cache(maxsize=1)
def _compiled() -> tuple[tuple[re.Pattern[str], dict[str, str | None], str], ...]:
    return tuple((*_compile(cs), en) for cs, en in RULES)


def _render(template: str, values: dict[str, str]) -> str:
    return _FIELD_RE.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def translate_body(body: str) -> str | None:
    """Přeloží řádek bez odsazení a předpony; None = žádné pravidlo."""
    for pattern, filters, en in _compiled():
        m = pattern.fullmatch(body)
        if m is None:
            continue
        values = {k: _filter(filters.get(k))(v) for k, v in m.groupdict().items()}
        return _render(en, values)
    return None


@lru_cache(maxsize=8192)
def translate_line(line: str, lang: str) -> str:
    """Řádek logu v jazyce ``lang``; čeština a nepřeložitelné řádky beze změny."""
    if lang == "cs" or not line or not line.strip() or len(line) > MAX_LINE_CHARS:
        return line
    m = _PREFIX_RE.match(line)
    if m is None:
        return line
    indent, tag, bullet, body = m.groups()
    if body.startswith("> "):
        return line
    out = translate_body(body)
    return line if out is None else f"{indent}{TAGS.get(tag, tag)}{bullet}{out}"


def is_translated(line: str, lang: str = "en") -> bool:
    """Pro měření pokrytí: řádek po překladu neobsahuje češtinu (nebo ji neměl)."""
    out = translate_line(line, lang)
    return out != line or not _CZ_CHARS.search(line)


def localize_log_lines(lines: list[dict], lang: str) -> list[dict]:
    """Řádky z ``db.get_logs`` pro web: v češtině beze změny, jinak ``line`` přeložený a ``src`` původní."""
    if lang == "cs":
        return lines
    out = []
    for row in lines:
        text = row.get("line") or ""
        translated = translate_line(text, lang)
        out.append({**row, "line": translated, "src": text} if translated != text else {**row, "src": text})
    return out
