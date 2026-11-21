"""Anglické názvy souborů a složek v ZIPu pro zadání z anglické stránky (Omapic, Petr 2026-11-21).

Pracovní soubory jobu (``work/``) a český ZIP mají dál česká jména; mapa se použije jen při balení anglického
ZIPu (``build_oom_zip``) a v cestách šablon .omap (``collect_oom_templates``), aby .omap našel podklady.
Názvy tříd ze zdroje (``zabaged/``, finské ``MTK_*``) se nepřekládají.
"""

from __future__ import annotations

from pathlib import PurePosixPath

# Složky (kterákoli úroveň cesty).
EN_DIRS: dict[str, str] = {
    "uzitecne": "useful",
    "pouzite": "in_full_map",
    "vyhozene": "filtered_out",
    "ocad_symboly": "ocad_symbols",
}

# Kmen souboru → anglický kmen (přípona zůstává: .png/.pgw, .shp/.shx/.dbf/.prj/.cpg, .xml …).
EN_STEMS: dict[str, str] = {
    # references/
    "vyska_vegetace": "vegetation_height",
    "katastr": "cadastre",
    "katastr_bev": "cadastre_bev",
    "mapa_ztm": "ztm_basemap",
    "dtm_plochy": "dtm_areas",
    "dtm_linie": "dtm_lines",
    "dmpok_nahled": "dmpok_preview",
    "sklon_dmr5g": "slope_dmr5g",
    "ortofoto_cuzk": "orthophoto_cuzk",
    "ortofoto_archiv": "orthophoto_archive",
    # osm/
    "OSM_aleje": "OSM_tree_rows",
    "OSM_balvany": "OSM_boulders",
    "OSM_brany": "OSM_gates",
    "OSM_budovy": "OSM_buildings",
    "OSM_cesty": "OSM_paths",
    "OSM_herni_prvky": "OSM_playground_equipment",
    "OSM_hriste": "OSM_playgrounds",
    "OSM_jeskyne": "OSM_cave_entrances",
    "OSM_kasny": "OSM_fountains",
    "OSM_lampy": "OSM_lamps",
    "OSM_lanovky": "OSM_aerialways",
    "OSM_lavicky": "OSM_benches",
    "OSM_mokrad": "OSM_wetlands",
    "OSM_nastupiste": "OSM_platforms",
    "OSM_ohniste": "OSM_firepits",
    "OSM_orna": "OSM_farmland",
    "OSM_parkoviste": "OSM_parking",
    "OSM_pesi_zony": "OSM_pedestrian_areas",
    "OSM_pisek": "OSM_sand",
    "OSM_ploty": "OSM_fences",
    "OSM_pomniky": "OSM_memorials",
    "OSM_posedy": "OSM_hunting_stands",
    "OSM_prameny": "OSM_springs",
    "OSM_pristresky": "OSM_shelters",
    "OSM_residential_zbytek_mensi": "OSM_residential_rest_small",
    "OSM_residential_zbytek_stredni": "OSM_residential_rest_medium",
    "OSM_residential_zbytek_velke": "OSM_residential_rest_large",
    "OSM_residential_zbytek_ridke": "OSM_residential_rest_sparse",
    "OSM_rozcestniky": "OSM_guideposts",
    "OSM_skaly": "OSM_bare_rock",
    "OSM_skaly_linie": "OSM_cliffs",
    "OSM_sport": "OSM_pitches",
    "OSM_sport_trava": "OSM_pitches_grass",
    "OSM_stoly": "OSM_picnic_tables",
    "OSM_stromy": "OSM_landmark_trees",
    "OSM_studny": "OSM_wells",
    "OSM_sutina": "OSM_scree",
    "OSM_tabule": "OSM_info_boards",
    "OSM_tramvaje": "OSM_tram_lines",
    "OSM_vedeni": "OSM_power_lines",
    "OSM_vedeni_velke": "OSM_power_lines_major",
    "OSM_veze": "OSM_towers",
    "OSM_voda": "OSM_water",
    "OSM_vodni_toky": "OSM_waterways",
    "OSM_zahrady": "OSM_gardens",
    "OSM_zdi": "OSM_walls",
    "OSM_zeleznice": "OSM_railways",
    "OSM_zive_ploty": "OSM_hedges",
    # uzitecne/ (useful/)
    "vegetace": "vegetation",
    "srazy_104": "earth_banks_104",
    "skaly_201": "cliffs_201",
}

# Přípona online šablony ve středním zoomu (online_templates).
MEDIUM_ZOOM_CS = "_stredni"
MEDIUM_ZOOM_EN = "_medium"

# Složky s názvy ze zdroje dat – beze změny.
_KEEP_DIRS = frozenset({"zabaged"})


def localize_arcname(arcname: str, lang: str) -> str:
    """Cesta v ZIPu (``references/vyska_vegetace.png``) v jazyce ZIPu; ``cs`` beze změny."""
    if lang != "en" or not arcname:
        return arcname
    parts = arcname.split("/")
    if parts[0] in _KEEP_DIRS:
        return arcname
    dirs = [EN_DIRS.get(p, p) for p in parts[:-1]]
    name = parts[-1]
    stem, dot, suffix = name.partition(".")
    if stem in EN_STEMS:
        name = EN_STEMS[stem] + dot + suffix
    elif stem.endswith(MEDIUM_ZOOM_CS):
        # online šablona ve středním zoomu (online_templates, ``ortofoto_cuzk_stredni.xml``)
        base = stem[: -len(MEDIUM_ZOOM_CS)]
        name = EN_STEMS.get(base, base) + MEDIUM_ZOOM_EN + dot + suffix
    return "/".join([*dirs, name])


def localize_name(filename: str, lang: str) -> str:
    """Jen jméno souboru (bez složky)."""
    return PurePosixPath(localize_arcname(filename, lang)).name


class LocalizedZip:
    """Obal ``zipfile.ZipFile``: ``write`` / ``writestr`` přejmenují cílovou cestu podle jazyka ZIPu."""

    def __init__(self, zf, lang: str):
        self._zf = zf
        self.lang = lang

    def write(self, filename, arcname=None, *args, **kwargs):
        if arcname is not None:
            arcname = localize_arcname(str(arcname), self.lang)
        return self._zf.write(filename, arcname, *args, **kwargs)

    def writestr(self, zinfo_or_arcname, data, *args, **kwargs):
        if isinstance(zinfo_or_arcname, str):
            zinfo_or_arcname = localize_arcname(zinfo_or_arcname, self.lang)
        else:
            zinfo_or_arcname.filename = localize_arcname(zinfo_or_arcname.filename, self.lang)
        return self._zf.writestr(zinfo_or_arcname, data, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._zf, name)
