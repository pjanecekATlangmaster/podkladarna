"""Anglické texty ZIPu pro zadání z jiné než české stránky (mezinárodní verze Omapic).

Jazyk ZIPu = jazyk stránky, ze které job přišel (``options["lang"]``, v metadatech ``lang``); bez něj česky
(starší joby, API). Katalog ``configs/i18n`` na dlouhé texty ZIPu nestačí, proto jsou tady celé – stejně jako
anglický úvod webu v ``guide_text``. Názvy souborů a složek (``base/``, ``uzitecne/``, ``vyska_vegetace.png`` …)
zůstávají, jsou to technické identifikátory; texty je vysvětlují.
"""

from __future__ import annotations

import re
from typing import Any, Callable

ZIP_LANGS = ("cs", "en")
BRAND_EN = "Omapic"
ABOUT_NAME_EN = "ABOUT_OMAPIC.txt"
INDICATIVE_LABEL_EN = "Working base map for drawing – not a precise survey."
WARNING_PREFIX_EN = "WARNING: "
NOTICE_PREFIX_EN = "NOTICE: "
DTM_OUTAGE_NOTICE_EN = (
    "The Digital Technical Map (DTM) of ČÚZK was unavailable during processing (source outage or maintenance). "
    "The map is drawn from OSM and ZABAGED; only buildings, fences, sidewalks and road areas lack the more precise "
    "shape from the DTM. You can run the job again later."
)


def zip_lang(meta_or_options: dict | None) -> str:
    """Jazyk textů ZIPu: ``cs`` jen u české stránky (i starší joby bez jazyka), jinak anglicky."""
    lang = str((meta_or_options or {}).get("lang") or "cs").strip().lower()
    return "cs" if lang == "cs" else "en"


# Česky zadaný job na mezinárodní doméně (Omapic, Petr 2026-11-19): české texty ZIPu pod značkou Omapic.
ABOUT_NAME_CS_INTL = "CO_JE_OMAPIC.txt"
_OMAPIC_CS = (
    ("CO_JE_PODKLADARNA.txt", ABOUT_NAME_CS_INTL),
    ("kdy je Podkladárna stáhla", "kdy je Omapic stáhl"),
    ("Podkladárny", "Omapicu"),
    ("Podkladárnu", "Omapic"),
    ("Podkladárně", "Omapicu"),
    ("Podkladárnou", "Omapicem"),
    ("Podkladárna", "Omapic"),
)


def intl_site_url(meta_or_options: dict | None) -> str:
    """Adresa mezinárodní domény zadání (``SITES`` s jinými zeměmi než Česko), jinak prázdné (česká doména, API)."""
    from app import i18n

    data = meta_or_options or {}
    if data.get("intl_site") and data.get("site_url"):
        return str(data["site_url"]).rstrip("/")
    site = i18n.sites().get(str(data.get("site") or "").strip().lower())
    return site.base_url.rstrip("/") if site is not None and i18n.is_intl_site(site) else ""


def cs_intl_site_url(meta_or_options: dict | None) -> str:
    """Adresa Omapicu u česky zadaného jobu z mezinárodní domény; jinak prázdné = české texty beze změny."""
    return intl_site_url(meta_or_options) if zip_lang(meta_or_options) == "cs" else ""


def omapic_cs(text: str, site_url: str = "") -> str:
    """Český text ZIPu pod značkou Omapic (název, citace s adresou webu); s prázdnou adresou beze změny."""
    if not site_url:
        return text
    for old, new in _OMAPIC_CS:
        text = text.replace(old, new)
    host = re.sub(r"^https?://", "", site_url).rstrip("/")
    head = "Podklad: " + BRAND_EN
    if host and head + " (" not in text:
        text = text.replace(head + " ·", f"{head} ({host}) ·")
    return text


def citation_en(short: str) -> str:
    """Česká citace („Podklad: Podkladárna · …, [rok]“) v anglickém textu jako „Base map: Omapic · …, [year]“."""
    text = str(short or "")
    if text.startswith("Podklad: Podkladárna"):
        text = "Base map: " + BRAND_EN + text[len("Podklad: Podkladárna"):]
    return text.replace("[rok]", "[year]")


def project_url(options: dict | None) -> str:
    """Adresa webu, kde job vznikl (``options.site`` ze ``SITES``); bez ní první mezinárodní doména ze ``SITES``."""
    from app import i18n

    base = i18n.site_base_url(str((options or {}).get("site") or ""))
    if not base:
        base = next((s.base_url for s in i18n.sites().values() if tuple(s.countries) != ("CZ",)), "")
    return (base or "").rstrip("/")


def citation_with_url(citation: str, site_url: str) -> str:
    """„Base map: Omapic · …“ → „Base map: Omapic (omapic.ulvi.app) · …“."""
    host = re.sub(r"^https?://", "", site_url or "").rstrip("/")
    head = "Base map: " + BRAND_EN
    if host and citation.startswith(head) and not citation.startswith(head + " ("):
        return f"{head} ({host}){citation[len(head):]}"
    return citation


def citation_line_en(short: str) -> str:
    return f"{citation_en(short)} – {INDICATIVE_LABEL_EN}"


def map_label_en(label: str) -> str:
    """Popisek presetu („Lesní 1:10000 · 5 m“) anglicky."""
    return (
        str(label or "")
        .replace("Lesní", "Forest")
        .replace("Les ", "Forest ")
        .replace("2,5 m", "2.5 m")
    )


ABOUT_EN_TEMPLATE = """Omapic – what is in this package
================================
{web_line}
Omapic automatically builds base maps for orienteering from airborne laser
scanning (LiDAR: terrain and surface models), the national technical map where
one exists, and OpenStreetMap. The result is a .omap file for OpenOrienteering
Mapper (OOM) and a .ocd file of the same name (OCD12) for OCAD.
The map is georeferenced in the national coordinate system of the area,
including magnetic declination and grid convergence for the centre of the
area – base maps and vectors fit on top of each other without alignment.
Vegetation is the height and density of growth from LiDAR, not runnability –
how fast you can run through the forest (undergrowth, visibility) cannot be
read from the data; vegetation is finished in the field.
The PNG preview of the map is mainly for orientation.
Neither the official data nor OSM are perfect and the automation only puts
them together – something is missing, something overlaps. This package is a
working base map, not a precise survey or a finished map; you will still work
on it in OOM and in the field.

What is inside
--------------
- *-sprint/les/mtbo.omap … by project name and scale (les = forest), open in OpenOrienteering Mapper (OOM)
- *-sprint/les/mtbo.ocd  … the same for OCAD (OCD12 via Mapper convert; lossy OK; the .omap stays)
- base/                … GDAL contours (contours_gdal.*), vegetation / cliffs / knolls
- uzitecne/            … vegetation / earth banks 104 / rocks: pouzite/ (kept) vs vyhozene/ (dropped by filters)
- osm/                 … OSM shapefile layers for manual composition (paths, buildings, rock base, …)
{zabaged_line}- references/          … base maps (orthophoto, OSM, …) as online templates (.xml) or downloaded rasters
                         (for drawing only, not for print)
- README_OOM.txt       … detailed workflow in OOM
- metadata.json        … scale, preset, CRS, LiDAR epochs

What to do with it
------------------
1. Install OOM (openorienteering.org).
2. Unzip the ZIP. Double-click the chosen *-sprint.omap / *-forest.omap / *-mtbo.omap (OOM)
   or the *.ocd of the same name (OCAD), or use File → Open.
3. Import DXF and SHP as described in README_OOM.txt and use the symbols of the map's symbol set
   (ISOM 2017-2 / ISSprOM 2019-2, MTBO: ISMTBOM).
4. Draw the map. Switch off or delete the reference layers when you are done.

OCAD: use the *.ocd from the ZIP (OCD12). OCAD does not open .omap natively –
it stays for OOM; DXF/SHP/PNG+PGW work as well.

Author and contact
------------------
Omapic is made by Petr Janeček.{web_contact}
Questions, comments and ideas for improvement are very welcome:
  e-mail:   janecek@datais.cz

Legal
-----
Omapic code: MIT. Job output (.omap, .ocd, PNG, …): CC BY 4.0.
A map made from this base (even redrawn and completed) must state on the map
or in its legend that it used Omapic: “{citation}”.
{attribution}
Loosely inspired by Karttapullautin.

"""


ABOUT_CS_INTL_TEMPLATE = """Omapic – co je v tomto balíčku
==============================
{web_line}
Omapic automaticky skládá podklady pro orientační mapy z leteckého laserového
skenování (LiDAR: modely terénu a povrchu), z národní technické mapy, kde
existuje, a z OpenStreetMap. Výsledkem je soubor .omap pro OpenOrienteering
Mapper (OOM) a stejnojmenný soubor .ocd (OCD12) pro OCAD.
Mapa je georeferencovaná v národním souřadnicovém systému výřezu včetně
magnetické deklinace a grivace pro střed výřezu – podklady i vektory sedí
na sebe bez lícování.
Vegetace je výška a hustota porostu z LiDARu, ne průběžnost – jak rychle se
lesem běží (podrost, viditelnost), z dat vyčíst nejde; vegetace se dokončuje
v terénu.
PNG náhled mapy je hlavně orientační.
Úřední data ani OSM nejsou dokonalá a automatika je jen skládá dohromady –
něco chybí, něco se překrývá. Tento balíček je pracovní podklad, ne přesné
zaměření ani hotová mapa; v OOM a v terénu s ním ještě budete pracovat.

Co je uvnitř
------------
- *-sprint/les/mtbo.omap … podle názvu projektu a měřítka, otevřete v OpenOrienteering Mapper (OOM)
- *-sprint/les/mtbo.ocd  … totéž pro OCAD (OCD12 přes Mapper convert; lossy OK; .omap zůstává)
- base/                … vrstevnice GDAL (contours_gdal.*), vegetace / srázy / kupky
- uzitecne/            … vegetace / srázy 104 / skály: pouzite/ (ponechané) vs vyhozene/ (vyřazené filtry)
- osm/                 … vrstvy OSM jako shapefily pro ruční skládání (cesty, budovy, skály podklad, …)
{zabaged_line}- references/          … podklady (ortofoto, OSM, …) jako online šablony (.xml) nebo stažené rastry
                         (jen pro kreslení, ne do tisku)
- README_OOM.txt       … podrobný postup v OOM
- metadata.json        … měřítko, preset, CRS, epochy LiDARu

Co s tím
--------
1. Nainstalujte OOM (openorienteering.org).
2. Rozbalte ZIP. Dvojklik na vybraný *-sprint.omap / *-les.omap / *-mtbo.omap (OOM)
   nebo stejnojmenný *.ocd (OCAD), případně File → Open.
3. Importujte DXF a SHP podle README_OOM.txt a použijte symboly sady v mapě
   (ISOM 2017-2 / ISSprOM 2019-2, MTBO: ISMTBOM).
4. Kreslete mapu. Referenční vrstvy po dokončení vypněte nebo smažte.

OCAD: použijte *.ocd ze ZIPu (OCD12). Soubor .omap OCAD nativně neotevře –
zůstává pro OOM; DXF/SHP/PNG+PGW fungují také.

Autor a kontakt
---------------
Omapic vytváří Petr Janeček.{web_contact}
Dotazy, připomínky i nápady na vylepšení jsou vítané:
  e-mail:   janecek@datais.cz

Právní
------
Kód Omapicu: MIT. Výstup jobu (.omap, .ocd, PNG, …): CC BY 4.0.
Mapa z tohoto podkladu (i překreslená a doplněná) musí na mapě nebo
v legendě uvést, že využila Omapic: „{citation}“.
{attribution}
Volně inspirováno Karttapullautinem.

"""


def about_txt_cs_intl(citation: str, attribution: tuple[str, ...], *, cz: bool, site_url: str) -> str:
    """CO_JE_OMAPIC.txt: česká obdoba ABOUT_OMAPIC.txt pro česky zadaný job na mezinárodní doméně."""
    zabaged_line = (
        "- zabaged/             … polohopis ZABAGED (shapefily včetně budov + RUIAN_budovy.shp)\n" if cz else ""
    )
    return ABOUT_CS_INTL_TEMPLATE.format(
        web_line=f"Vytvořeno v Omapicu: {site_url}\n" if site_url else "",
        web_contact=f" Web: {site_url}" if site_url else "",
        citation=omapic_cs(citation, site_url),
        attribution="\n".join(attribution or ()),
        zabaged_line=zabaged_line,
    )


def about_txt_en(citation: str, attribution: tuple[str, ...], *, cz: bool, site_url: str = "") -> str:
    """ABOUT_OMAPIC.txt (anglická obdoba CO_JE_PODKLADARNA.txt)."""
    zabaged_line = (
        "- zabaged/             … Czech topographic data ZABAGED (shapefiles incl. buildings + RUIAN_budovy.shp)\n"
        if cz
        else ""
    )
    return ABOUT_EN_TEMPLATE.format(
        web_line=f"Made with Omapic: {site_url}\n" if site_url else "",
        web_contact=f" Web: {site_url}" if site_url else "",
        citation=citation_with_url(citation_en(citation), site_url),
        attribution="\n".join(attribution or ()),
        zabaged_line=zabaged_line,
    )


def osm_folder_readme_en(describe_code: Callable[[str], str], crs: str) -> str:
    return f"""OSM – layers for manual map composition
=======================================
Same purpose as the zabaged/ folder (Czech jobs): pick a SHP and import it into OOM
(File → Import…) with a symbol assignment.

Most objects are in the .omap as well; here you have them as a source for free composition.
Including OSM_budovy.shp (buildings, the same source as the automatic buildings in the .omap).

OSM rocks: OSM_skaly (natural=bare_rock) goes into the automatic .omap as bare rock {describe_code("214")}
(MTBO does not have it, there it stays a base layer). OSM_skaly_linie / OSM_sutina are base layers only.
Other rocks in the output are computed from LiDAR/DEM ({describe_code("206")}).

Residential remainder: OSM_residential_zbytek_mensi / _stredni / _velke (smaller / medium / large)
and _ridke (sparsely mapped – not in the automatic .omap). Symbol {describe_code("501")}.

Symbol codes are the codes of the OCAD sets ISOM / ISSprOM (MTBO has its own numbering).

Coordinates: {crs}.
"""


UZITECNE_README_EN = """Useful vectors – vegetation, earth banks, rocks
===============================================
For viewing and manual import in OOM / QGIS (coordinate system of the job).

pouzite/   … what stayed in the automatic .omap after filtering
vyhozene/  … dropped candidates (attribute duvod = filter reason)

Layers
------
vegetace.*   vegetation polygons (cls, code = 401/406/408/410)
srazy_104.*  earth banks (ISOM 104 lines)
skaly_201.*  rock areas (polygons 201.2 / 206)

Reasons in vyhozene (examples): mala_plocha (small area), kompaktni_flek (compact patch),
min_plocha (minimum area), zamotany (tangled), nizky_schod (low step), deprese (depression),
budova (building), prekryv_104 (overlaps 104), occupancy, husté_vrstevnice (dense contours).

Raw tick DXF and vegetation.shp are also in the base/ folder (before the final
cliff filtering). This folder shows the result of the filters.
"""

GEOREF_PREVIEWS_README_EN = (
    "Omapic – georeferenced map previews\n"
    "===================================\n\n"
    "Each variant (forest / mtbo / sprint by scale) is one file:\n"
    "  *.tif  – GeoTIFF in the job's coordinate system, rotated for magnetic north\n"
    "           (Mapper CLI @ 300 DPI; without CLI Pillow @ 300 DPI-eq).\n"
    "           Tiled, losslessly compressed (DEFLATE) with internal overviews –\n"
    "           QGIS and OCAD open it straight away at any zoom.\n\n"
    "Open the *.tif in QGIS or insert it as a background map in OOM / OCAD.\n"
    "It is not a print map – only a georeferenced preview.\n"
    "If the GeoTIFF could not be made (GDAL missing), PNG + PGW are there instead.\n"
)


def source_epochs_en(source_meta: dict | None) -> str:
    """Anglická obdoba ``source_meta.format_source_epochs_readme`` (Česko: listy LiDARu ČÚZK)."""
    if not source_meta or not source_meta.get("sheets"):
        return ""
    lines = [
        "ČÚZK elevation data (airborne laser scanning: DMR 5G terrain, DMP OK trees and buildings)",
        "Date = when Omapic downloaded them; the DMP OK flight date (when ČÚZK scanned) is in brackets.",
        "-------------------------------------------------------------------------------------------",
    ]
    for sheet in source_meta["sheets"]:
        dmr = sheet.get("dmr") or {}
        dmp = sheet.get("dmp") or {}
        dmp_label = {"ok": "DMP OK", "1g": "DMP 1G"}.get(dmp.get("mode"), "DMP ?")
        flown = f" (flown {dmp['acquired_on']})" if dmp.get("acquired_on") else ""
        lines.append(
            f"- {sheet.get('mapnom') or '?'}: DMR 5G {(dmr.get('cache_downloaded_at') or '?')[:10]} · "
            f"{dmp_label} {(dmp.get('cache_downloaded_at') or '?')[:10]}{flown}"
        )
    if source_meta.get("dmp_degraded"):
        lines.append(
            WARNING_PREFIX_EN + "precise tree and building heights (DMP OK) were not available for part of the area, "
            "the older coarser DMP 1G was used instead. Vegetation may be less accurate; we recommend generating "
            "the map again in a few days."
        )
    leafless = source_meta.get("dmp_leafless_sheets") or []
    if source_meta.get("dmp_leafless") and leafless:
        part = (
            "for this area"
            if len(leafless) >= len(source_meta["sheets"])
            else "for part of the area (sheets " + ", ".join(str(x.get("mapnom")) for x in leafless) + ")"
        )
        dates = ", ".join(sorted({str(x["acquired_on"]) for x in leafless if x.get("acquired_on")}))
        lines.append(
            f"{WARNING_PREFIX_EN}DMP OK {part} was flown by ČÚZK in the leafless season (date {dates}). "
            "Deciduous trees are then missing in the data, so deciduous forest may wrongly show open land / "
            "clearings (403) and vegetation 401–410 is unreliable there. Check in the field or on the orthophoto."
        )
    lines.append(INDICATIVE_LABEL_EN)
    return "\n".join(lines) + "\n"


_DTM_MAPS_EN = {"sprint": "sprint", "les": "forest", "mtbo": "MTBO"}


def _join_en(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def dtm_lines_en(summary: dict[str, Any] | None) -> dict[str, list[str]]:
    """Anglické věty o DTM z údajů souhrnu (``lines``, ``warn_lines``, ``notice_lines`` jako v souhrnu, bez prefixů).

    České věty souhrnu se nepřekládají slovo od slova; skládají se znovu z počtů a stavů."""
    out: dict[str, list[str]] = {"lines": [], "warn_lines": [], "notice_lines": []}
    if not summary or not summary.get("lines"):
        return out
    state = summary.get("state")
    maps = summary.get("maps") or {}
    used = [_DTM_MAPS_EN.get(k, k) for k, v in maps.items() if v] if isinstance(maps, dict) else []
    required = bool(summary.get("warn_lines"))
    if state in {"used", "weak"}:
        counts = summary.get("counts") or {}
        names = {"ploty": "fences", "budovy": "buildings", "chodníky": "sidewalks", "silnice": "roads", "parkoviště": "parking"}
        detail = ", ".join(f"{names.get(k, k)} {n}" for k, n in counts.items() if n)
        head = "ČÚZK Digital Technical Map (DTM): used" + (f" for {_join_en(used)}" if used else "")
        out["lines"].append(head + (f" ({detail})." if detail else "."))
        problems: list[str] = []
        missing = summary.get("missing") or []
        total = summary.get("units_total") or 0
        if missing and total:
            problems.append(
                f"DTM covers only {total - len(missing)} of {total} municipalities of the area "
                f"(missing: {', '.join(missing)}); elsewhere the map is drawn from OSM and ZABAGED."
            )
        incomplete = summary.get("incomplete") or []
        if incomplete:
            problems.append(
                f"DTM areas are incomplete in {', '.join(incomplete)}; buildings and sidewalks there come from OSM."
            )
        out["lines"] += problems
        if required:
            out["warn_lines"] += problems
    elif state == "failed":
        out["lines"].append(DTM_OUTAGE_NOTICE_EN)
        if summary.get("notice_lines"):
            out["notice_lines"].append(DTM_OUTAGE_NOTICE_EN)
        elif required:
            out["warn_lines"].append(DTM_OUTAGE_NOTICE_EN)
    elif state == "pending":
        out["lines"].append("DTM data were still being prepared; the map is drawn from OSM and ZABAGED.")
    else:
        out["lines"].append("DTM was not used for this map (switched off or no DTM data in the area).")
    return out


def dtm_readme_en(summary: dict[str, Any] | None) -> str:
    """Anglický blok o DTM do README_OOM.txt."""
    parts = dtm_lines_en(summary)
    if not parts["lines"]:
        return ""
    warn, notice = set(parts["warn_lines"]), set(parts["notice_lines"])
    lines = [
        (WARNING_PREFIX_EN if line in warn else NOTICE_PREFIX_EN if line in notice else "") + line
        for line in parts["lines"]
    ]
    title = "Digital Technical Map (DTM)"
    return "\n".join([title, "-" * len(title), *lines])


def dtm_notice_en(summary: dict[str, Any] | None) -> list[str]:
    return [DTM_OUTAGE_NOTICE_EN] if summary and summary.get("notice_lines") else []


def oom_readme_en(
    meta: dict,
    *,
    sentinel_info: str,
    code: Callable[[str], str],
    crs_text: str,
    data_sources: str,
    wms_lines: Callable[[str], list[str]],
) -> str:
    """README_OOM.txt anglicky; stejné bloky jako české ``package_oom.oom_readme``."""
    country = meta.get("country") or "CZ"
    cz = country == "CZ"
    scale = meta.get("scale") or "?"
    label = map_label_en(meta.get("label") or meta.get("preset_id") or "")
    interval = meta.get("contour_interval_m")
    interval_txt = f"{interval} m" if interval is not None else "?"
    if meta.get("output_georef"):
        georef_note = (
            "   Georeferenced map previews are in the preview/ folder "
            f"(GeoTIFF in {meta.get('crs') or 'EPSG:5514'}, 300 DPI, with overviews; Mapper, otherwise Pillow).\n"
        )
    else:
        georef_note = (
            "   Georeferenced map previews (GeoTIFF, Mapper @ 300 DPI) are not in this ZIP –\n"
            "   switch on the georeferenced raster option when generating,\n"
            "   or download them separately on the website (“Download georef previews”)\n"
            "   if the job made them.\n"
        )
    epochs = source_epochs_en(meta.get("lidar_sources"))
    epochs = "\n" + epochs + "\n" if epochs else ""
    dtm_block = dtm_readme_en(meta.get("dtm")) if cz else ""
    if dtm_block:
        epochs += "\n" + dtm_block + "\n"
    notice_block = "".join(f"{NOTICE_PREFIX_EN}{line}\n\n" for line in (dtm_notice_en(meta.get("dtm")) if cz else []))
    from app import data_notes

    notice_block += "".join(f"{NOTICE_PREFIX_EN}{line}\n\n" for line in data_notes.meta_lines(meta, "en"))
    refs = meta.get("reference_layers") or []
    ref_block = ""
    if refs or sentinel_info:
        ref_block = (
            "\nReference base maps (folder references/)\n"
            "----------------------------------------\n" + "".join(f"- {name}\n" for name in refs)
        )
        if sentinel_info:
            ref_block += "\n" + sentinel_info_en(sentinel_info).strip() + "\n"
    from app.pipeline.zip_images import notice_lines

    withheld = notice_lines(meta.get("withheld_images") or [], "en")
    if withheld:
        ref_block += "\n" + "\n".join(withheld) + "\n"
    online = any(str(name).endswith((".xml", ".vrt")) for name in refs)
    if online and not cz:
        refs_intro = (
            "Base maps are online templates (.xml / .vrt files in the references/ folder, GDAL WMS) of the local\n"
            "services of the area's country (and OpenStreetMap when the image was not drawn), each at maximum and medium zoom (_stredni, 4× coarser).\n"
            "They are switched off in the OOM template list; once switched on they download from the internet.\n"
            "Vegetation height (vyska_vegetace.png) and terrain and surface hillshade (hillshade_dtm.png,\n"
            "hillshade_dsm.png) are from the job's own elevation models. OCAD does not open .xml files –\n"
            "connect the base maps as WMS:\n" + "\n".join(wms_lines(country)) + "\n"
            + ("The OpenStreetMap image (osm.png) is our own rendering of OSM data (© OpenStreetMap contributors).\n"
               if "osm.png" in refs else "")
        )
    elif online:
        refs_intro = (
            "Base maps are online templates (.xml / .vrt files in the references/ folder, GDAL WMS): ČÚZK orthophoto,\n"
            "archive orthophoto 2003–2005, ZTM topographic map, cadastre, regional DTM, DMP OK preview, DMR 5G\n"
            "hillshade and slope (basic, Z10, Z20 hillshade) and OpenStreetMap (without osm.png), each at maximum and medium zoom\n"
            "(_stredni, 4× coarser). They are switched off in the OOM template list; once switched on they download\n"
            "from the internet (maximum zoom takes a while). Offline base maps: tick “Download reference templates\n"
            "in advance” when generating.\n"
            "OCAD does not open .xml files – connect the base maps as WMS (Background map → Online map):\n"
            + "\n".join(wms_lines(country))
            + "\n"
            + ("The OpenStreetMap image (osm.png) is our own rendering of OSM data (© OpenStreetMap contributors).\n"
               if "osm.png" in refs else "")
        )
    elif cz:
        refs_intro = (
            "DMR 5G hillshade (ČÚZK WMS): basic, Z10 and Z20 in the references/ folder, DMR 5G slope\n"
            "(sklon_dmr5g.png: grey flat, yellow to brown steep slope).\n"
            "Base maps: ČÚZK orthophoto, "
            + ("OpenStreetMap (osm.png, own rendering of OSM data), " if "osm.png" in refs else "")
            + "ZTM topographic map, cadastral map and DMP OK preview\n"
            "(downloaded rasters, they work offline and in OCAD). Vegetation height (vyska_vegetace.png,\n"
            "DMP OK − DMR 5G: yellow bushes, orange thicket, green forest, below 0.5 m transparent). Online template\n"
            "(.xml): archive orthophoto 2003–2005.\n"
        )
    else:
        refs_intro = (
            "Base maps are downloaded rasters in the references/ folder (they work offline and in OCAD).\n"
            "Vegetation height (vyska_vegetace.png) and terrain and surface hillshade (hillshade_dtm.png,\n"
            "hillshade_dsm.png) are from the job's own elevation models.\n"
            + ("The OpenStreetMap image (osm.png) is our own rendering of OSM data (© OpenStreetMap contributors).\n"
               if "osm.png" in refs else "")
        )
    buildings = (
        "3. Buildings in the .omap come from the regional DTM (where available), completed by OSM. Sources:\n"
        "   osm/OSM_budovy.shp, Budova* and RUIAN_budovy.shp in zabaged/. ZABAGED paths are in zabaged/ for manual import.\n"
        if cz
        else "3. Buildings in the .omap come from the technical map of the country (where available), completed by\n"
        "   OSM. Source: osm/OSM_budovy.shp.\n"
    )
    steps = (
        "2. Switch on base maps (orthophoto, OSM, hillshade, …) as needed\n"
        "   in Templates → Template setup.\n"
        f"{buildings}"
        "   In the base/ folder: GDAL contours (contours_gdal.* = the single source of truth, terrain only), "
        "vegetation from LiDAR return density (vegetation.*, fallback CHM), "
        f"earth banks ({code('104')}) / rock ({code('206')}) "
        f"and optional knolls ({code('109')}).\n"
        "   uzitecne/pouzite vs uzitecne/vyhozene: the same layers after filters "
        "(min-size, occupancy, dense contours, …) – SHP for viewing.\n"
        f"{georef_note}"
        "   The web PNG preview (Pillow) is not part of the material package.\n\n"
    )
    if cz:
        relief = (
            "Relief and vegetation: DMR 5G / DMP OK (ČÚZK) – our own DEM. "
            "Vegetation from LiDAR return density (fallback: CHM height), not from ZABAGED meadows. "
            "Cliffs: rock vs. earth by step slope.\n\n"
        )
    else:
        relief = (
            "Relief and vegetation: terrain and surface model of the area's country (see Data) – our own DEM. "
            "Vegetation from surface height above terrain (CHM). Cliffs: rock vs. earth by step slope.\n\n"
        )
    citation = citation_line_en(meta.get("citation_short") or "")
    return (
        "Omapic – package for OpenOrienteering Mapper\n"
        "============================================\n\n"
        f"Read {ABOUT_NAME_EN} in the root of the ZIP first.\n\n"
        f"{INDICATIVE_LABEL_EN}\n\n"
        f"{notice_block}"
        f"Map type: {label}\n"
        f"Scale: 1:{scale}\n"
        f"Contour interval: {interval_txt}\n"
        f"Coordinate system: {crs_text}\n"
        f"{epochs}\n"
        f"{refs_intro}"
        f"{ref_block}\n"
        "Recommended workflow in OOM\n"
        "---------------------------\n"
        "1. Unzip the whole ZIP into one folder. Open the chosen *-sprint.omap / *-forest.omap / *-mtbo.omap\n"
        "   (by scale; les = forest; paths from OSM) – or the *.ocd of the same name in OCAD.\n"
        "   Default view: vectors only (contours, vegetation, OSM buildings, cliffs, …).\n"
        "   Purple rectangle = your area; outside it there is only an overlap of the map content.\n"
        f"   Contours ({code('101')}/{code('102')}): Chaikin → DP simplify (~0.08 mm) → Bézier; locked (is_protected)\n"
        "   – unlock in the symbol panel; optionally Convert to curves in OOM.\n"
        f"{steps}"
        "OCAD: open the *.ocd of the same name (OCD12 from Mapper convert; lossy OK).\n"
        "   The original OCAD symbol set for the given standard is in the ocad_symboly/ folder\n"
        "   (ISOM / ISSprOM / ISMTBOM) – you can use it in OCAD to replace symbols\n"
        "   (symbol codes match).\n"
        "OCAD does not open the .omap file natively – it stays for OOM; or use DXF/SHP/PNG+PGW.\n\n"
        f"{data_sources} Job output: CC BY 4.0 – a map made from this base must credit Omapic:\n"
        f"{citation}\n"
        f"{relief}"
        "Omapic is an experiment, so feedback and comments are very welcome.\n"
    )


_SENTINEL_SCENE_RE = re.compile(
    r"^- (?P<file>\S+) – datum snímku (?P<day>[\d-]+) \((?P<item>[^,]+), oblačnost celé dlaždice (?P<cloud>[\d.]+) %\)$"
)
_SENTINEL_YEAR_RE = re.compile(r"Copernicus Sentinel (?P<years>[\d, ]+) \(ESA")


def sentinel_info_en(text: str) -> str:
    """Anglický popis snímků Sentinel-2 z českého ``sentinel2.txt`` (soubor píše job, jazyk zná až balení)."""
    if not text.strip():
        return ""
    scenes = [m.groupdict() for line in text.splitlines() if (m := _SENTINEL_SCENE_RE.match(line.strip()))]
    years = _SENTINEL_YEAR_RE.search(text)
    lines = ["Sentinel-2 L2A – true-colour satellite images (base layer for mappers, not used for drawing)", ""]
    lines += [
        f"- {s['file']} – image date {s['day']} ({s['item']}, cloud cover of the whole tile {s['cloud']} %)"
        for s in scenes
    ]
    lines += [
        "",
        "Resolution 10 m/px (resampled), world file .jgw. The newest cloud-free image of the area and older ones",
        "a few months apart, if clear. Useful for judging recent changes (clear-cuts, windthrow, new buildings,",
        "paths) after the orthophoto and LiDAR date; details under ~20 m are not visible.",
        "Source: Copernicus Sentinel-2 L2A via Element84 earth-search (AWS Open Data).",
        f"Contains modified Copernicus Sentinel data {years.group('years').strip() if years else ''} (ESA / EU Copernicus).",
    ]
    return "\n".join(lines) + "\n"
