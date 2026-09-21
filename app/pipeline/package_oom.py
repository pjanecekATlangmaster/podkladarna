from __future__ import annotations

import json
import re
import unicodedata
import zipfile
from pathlib import Path

from app.pipeline.karttapullautin_dxf import collect_dxf_for_zip
from app.guide_text import ZIP_ABOUT_TXT
from app.pipeline.aopk_trees import build_aopk_tree_parts, load_aopk_tree_points
from app.pipeline.contours_gdal import build_gdal_contour_parts
from app.pipeline.osm_paths import (
    PATH_SOURCE_MIXED,
    PATH_SOURCE_OSM,
    PATH_SOURCE_ZABAGED,
    ZABAGED_OMIT_WHEN_OSM_POWER,
    build_osm_feature_parts,
    build_osm_path_parts,
    load_osm_path_lines,
    osm_features_have_power_lines,
    resolve_path_source,
)
from app.pipeline.build_oom_map import write_oom_map
from app.pipeline.crs_5514 import projected_to_wgs84
from app.pipeline.fetch_openzu import crop_bounds_5514
from app.pipeline.geom_clip import expand
from app.pipeline.georef import projected_center_from_raster
from app.pipeline.oom_georef import oom_north_angles
from app.pipeline.oom_import import (
    OomObjectPart,
    _path_object,
    build_dxf_object_part,
    build_zabaged_object_parts,
)
from app.pipeline.oom_coords import projected_to_map_coord
from app.pipeline.oom_layers import collect_oom_templates
from app.pipeline.oom_symbol_map import symbol_index_for_code
from app.pipeline.open_land_subtract import collect_kp401_subtract_wkbs
from app.pipeline.reference_layers import reference_metadata
from app.pipeline.ruian_buildings import (
    ZABAGED_OMIT_BUILDING_LAYERS,
    write_ruian_buildings_shapefile,
)
from app.pipeline.vegetation_gdal import build_vegetation_parts
from app.settings import APP_VERSION

DOPLNKY_OSM_README = """OSM budovy (ruční import)
========================
Soubor OSM_budovy.shp ve složce osm/ – v OOM dialog přiřazení symbolu.
Všem polygonům dej 521 (les/sprint) nebo 526 (MTBO).

Výchozí budovy v .omap jsou z OSM; toto SHP je pro ruční import.
"""

OSM_FOLDER_README = """OSM – vrstvy pro ruční skládání mapy
====================================
Stejný účel jako složka zabaged/: vyber SHP a importuj do OOM
(File → Importovat…) s přiřazením symbolu.

Objekty už jsou i v .omap; tady je máš jako zdroj pro volné poskládání.
Včetně OSM_budovy.shp (stejný zdroj jako auto budovy v .omap).

Doporučené symboly jsou v README.txt uvnitř jobu (osm_paths/manual/).

Souřadnice: EPSG:5514 (S-JTSK).
"""

DOPLNKY_ZABAGED_README = """ZABAGED / RÚIAN budovy
=====================
Vrstvy Budova*, Kulna*, StavebniObjektZakryty, Hrad, Zamek a RUIAN_budovy.shp
jsou ve složce zabaged/ mezi ostatními SHP. Výchozí budovy v .omap jsou z OSM;
RÚIAN a ZABAGED budovy jsou podklad pro ruční import.

V OOM: File → Importovat… → symbol 521 (les/sprint) nebo 526 (MTBO).
"""

# Zpětná kompatibilita testů / starších odkazů.
DOPLNKY_README = (
    "Budovy pro ruční import jsou u svých zdrojů:\n"
    "  osm/OSM_budovy.shp (mezi ostatními OSM vrstvami)\n"
    "  zabaged/ (vrstvy Budova* atd. mezi ostatními SHP)\n"
    "\n"
    + DOPLNKY_OSM_README
    + "\n"
    + DOPLNKY_ZABAGED_README
)

OUTPUT_ZIP_NAME = "podkladarna_output.zip"
OOM_ZIP_NAME = "podkladarna_oom.zip"  # legacy – starší joby
OOM_MAP_NAME = "podkladarna.omap"
# Zdroje cest × disciplíny (počet disciplín závisí na měřítku).
# V .omap jen OSM cesty; ZABAGED cesty zůstávají ve zabaged/ pro ruční import.
OOM_PATH_VARIANTS: tuple[tuple[str, str], ...] = (
    ("cesty_osm", PATH_SOURCE_OSM),
)
OOM_DISCIPLINE_ORDER: tuple[str, ...] = ("sprint", "les", "mtbo")
MAP_SCALES: tuple[int, ...] = (4000, 7500, 10000, 15000)
# Povolené ekvidistance (m) podle zvoleného měřítka.
CONTOURS_BY_SCALE: dict[int, tuple[float, ...]] = {
    4000: (2.0, 2.5, 5.0),
    7500: (2.0, 2.5, 5.0),
    10000: (5.0,),
    15000: (5.0,),
}
DEFAULT_CONTOUR_BY_SCALE: dict[int, float] = {
    4000: 2.5,
    7500: 5.0,
    10000: 5.0,
    15000: 5.0,
}
# Ořez DXF/srazů z širšího LiDAR cropu: nechat kousek za hranicí, ať u kraje
# nechybí stub symbolu. OSM/ZABAGED/AOPK se neořezávají – radši přesahují.
CLIP_MARGIN_M = 25.0
# Louka / parková zeleň pod KP vegetací – jinak 401 překryje hustníky z LiDARu.
_ZABAGED_UNDER_VEGETATION = frozenset(
    {
        "TrvalyTravniPorost",
        "UdrzovanaZelen",
    }
)
# Zpevněné „Ostatní plocha v sídlech“ / parkoviště jako nejspodnější podklad
# (pod loukami, vegetací i silnicemi) – jinak 529 přemaluje Ulice/Cesta.
_ZABAGED_BASE_PAVED = frozenset(
    {
        "OstatniPlochaVSidlech",
        "ParkovisteOdpocivka",
    }
)
# Obdělávaná půda z OSM (412) taky pod KP – hustníky zůstanou navrch.
_OSM_UNDER_VEGETATION_MARK = "(412)"
# Dvory v budovách (oliva) až navrch – překryjí detaily uvnitř dvorů.
_COURTYARD_OLIVE_MARK = "dvory (oliva)"


def map_scale_from_scalefactor(scalefactor: float) -> int:
    return int(round(float(scalefactor) * 10000))


def parse_map_scale(raw: object) -> int | None:
    """Vrátí jedno z MAP_SCALES, nebo None."""
    if raw is None or raw == "":
        return None
    try:
        if isinstance(raw, str):
            text = raw.strip().lower().replace(" ", "")
            if text.startswith("1:"):
                text = text[2:]
            value = int(round(float(text.replace(",", "."))))
        else:
            value = int(round(float(raw)))
    except (TypeError, ValueError):
        return None
    return value if value in MAP_SCALES else None


def parse_contour_interval(raw: object) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        if isinstance(raw, str):
            value = float(raw.strip().replace(",", ".").replace("m", ""))
        else:
            value = float(raw)
    except (TypeError, ValueError):
        return None
    # Normalizuj 2.50 → 2.5 apod.
    for known in (2.0, 2.5, 5.0):
        if abs(value - known) < 1e-6:
            return known
    return None


def allowed_contours_for_scale(map_scale: int) -> tuple[float, ...]:
    return CONTOURS_BY_SCALE.get(int(map_scale), (5.0,))


def default_contour_for_scale(map_scale: int) -> float:
    return DEFAULT_CONTOUR_BY_SCALE.get(int(map_scale), 5.0)


def _pick_preset(preferred: str, fallbacks: tuple[str, ...], presets: dict) -> str:
    if not presets or preferred in presets:
        return preferred
    for fb in fallbacks:
        if fb in presets:
            return fb
    return preferred


def map_scale_from_preset_id(preset_id: str, presets: dict | None = None) -> int:
    """Odhad měřítka ze starého preset_id (zpětná kompatibilita)."""
    presets = presets or {}
    pid = (preset_id or "").strip()
    if pid.startswith("sprint"):
        return 4000
    if pid == "forest_7500":
        return 7500
    if pid.startswith("mtbo") and "15000" in pid:
        return 15000
    if pid in presets:
        try:
            return map_scale_from_scalefactor(float(presets[pid].get("scalefactor", 1)))
        except (TypeError, ValueError):
            pass
    return 10000


def resolve_omap_job(
    map_scale: object,
    contour_interval: object = None,
    *,
    presets: dict | None = None,
    preset_id_fallback: str | None = None,
) -> dict:
    """Z měřítka + ekvidistance odvodí KP preset a seznam omap disciplín.

    Vrací dict:
      map_scale, preset_id, scalefactor, contour_interval, indexcontours,
      disciplines: [(tag, preset_id, scale), ...]

    Raises:
      ValueError: neplatné měřítko nebo nepovolená ekvidistance.
    """
    presets = presets or {}
    scale = parse_map_scale(map_scale)
    if scale is None and preset_id_fallback:
        scale = map_scale_from_preset_id(preset_id_fallback, presets)
    if scale is None:
        raise ValueError("Neplatné nebo chybějící měřítko (4000 / 7500 / 10000 / 15000).")

    allowed = allowed_contours_for_scale(scale)
    contour = parse_contour_interval(contour_interval)
    if contour is None:
        if preset_id_fallback and preset_id_fallback in presets:
            try:
                contour = parse_contour_interval(
                    presets[preset_id_fallback].get("contour_interval")
                )
            except Exception:
                contour = None
        if contour is None or contour not in allowed:
            contour = default_contour_for_scale(scale)
    if contour not in allowed:
        allowed_txt = ", ".join(
            str(int(c)) if float(c).is_integer() else str(c).replace(".", ",")
            for c in allowed
        )
        raise ValueError(
            f"Pro měřítko 1:{scale} je ekvidistance {contour} m nepovolená "
            f"(povolené: {allowed_txt} m)."
        )

    indexcontours = 5.0 * float(contour)
    scalefactor = scale / 10000.0

    if scale == 4000:
        if abs(contour - 2.5) < 1e-6:
            kp_preset = _pick_preset(
                "sprint_2_5m", ("sprint_2m", "sprint_2_5m"), presets
            )
        else:
            kp_preset = _pick_preset(
                "sprint_2m", ("sprint_2m", "sprint_2_5m"), presets
            )
        disciplines = [("sprint", kp_preset, 4000)]
    elif scale == 7500:
        forest_id = _pick_preset(
            "forest_7500", ("forest_7500", "forest_10000"), presets
        )
        mtbo_id = _pick_preset(
            "mtbo_10000", ("mtbo_10000", "mtbo_15000"), presets
        )
        kp_preset = forest_id
        disciplines = [
            ("les", forest_id, 7500),
            ("mtbo", mtbo_id, 7500),
        ]
    elif scale == 10000:
        forest_id = _pick_preset(
            "forest_10000", ("forest_10000", "forest_7500"), presets
        )
        mtbo_id = _pick_preset(
            "mtbo_10000", ("mtbo_10000", "mtbo_15000"), presets
        )
        kp_preset = forest_id
        disciplines = [
            ("les", forest_id, 10000),
            ("mtbo", mtbo_id, 10000),
        ]
    else:  # 15000
        forest_id = _pick_preset(
            "forest_10000", ("forest_10000", "forest_7500"), presets
        )
        mtbo_id = _pick_preset(
            "mtbo_15000", ("mtbo_15000", "mtbo_10000"), presets
        )
        kp_preset = mtbo_id
        disciplines = [
            ("les", forest_id, 15000),
            ("mtbo", mtbo_id, 15000),
        ]

    return {
        "map_scale": scale,
        "preset_id": kp_preset,
        "scalefactor": scalefactor,
        "contour_interval": float(contour),
        "indexcontours": indexcontours,
        "disciplines": disciplines,
    }


def resolve_discipline_presets(
    selected_preset_id: str,
    presets: dict | None = None,
    *,
    map_scale: object = None,
    contour_interval: object = None,
) -> list[tuple[str, str, int]]:
    """Vrátí [(tag, preset_id, scale), ...] podle měřítka (ne vždy všechny disciplíny)."""
    presets = presets or {}
    resolved = resolve_omap_job(
        map_scale,
        contour_interval,
        presets=presets,
        preset_id_fallback=selected_preset_id,
    )
    return list(resolved["disciplines"])


def omap_map_stem(map_name: str, *, max_len: int = 12) -> str:
    """Název mapy → krátký stem pro .omap (bez diakritiky, CamelCase, max 12).

    „Česká lípa park“ → „CeskaLipaPar“; prázdný název → „Mapa“.
    """
    raw = unicodedata.normalize("NFD", (map_name or "").strip())
    raw = "".join(c for c in raw if unicodedata.category(c) != "Mn")
    raw = re.sub(r'[<>:"/\\|?*\x00-\x1f._]+', " ", raw)
    words: list[str] = []
    for token in re.split(r"\s+", raw):
        word = re.sub(r"[^A-Za-z0-9]", "", token)
        if word:
            words.append(word)
    if not words:
        return "Mapa"
    stem = words[0] + "".join(w[:1].upper() + w[1:] for w in words[1:])
    return stem[:max_len] or "Mapa"


def omap_variant_filename(
    discipline_tag: str,
    path_tag: str = "",
    map_name: str = "",
) -> str:
    # Jediný auto zdroj cest je OSM – bez přípony cesty_*.
    del path_tag
    return f"{omap_map_stem(map_name)}-{discipline_tag}.omap"


def build_aoi_boundary_part(
    bbox_wgs84: tuple[float, float, float, float],
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
) -> OomObjectPart | None:
    """Fialový obdélník vybraného AOI (uvnitř = výběr, vně = přesah)."""
    # ISOM/ISSprOM 708 = Out-of-bounds boundary (purple);
    # ISMTBOM 708 je crossing point → 705 Marked route.
    code = "705" if preset_id.startswith("mtbo") else "708"
    symbol_index = symbol_index_for_code(preset_id, scale, code)
    if symbol_index is None:
        return None
    xmin, ymin, xmax, ymax = crop_bounds_5514(
        *bbox_wgs84, buffer_m=0.0
    )
    ring = [
        (xmin, ymin),
        (xmax, ymin),
        (xmax, ymax),
        (xmin, ymax),
        (xmin, ymin),
    ]
    map_coords = [
        projected_to_map_coord(
            x,
            y,
            ref_x=ref_x,
            ref_y=ref_y,
            scale=scale,
            grivation_deg=grivation_deg,
        )
        for x, y in ring
    ]
    obj = _path_object(symbol_index, map_coords)
    if not obj:
        return None
    return OomObjectPart(
        name="AOI – vybraný výřez",
        objects_xml=obj,
        count=1,
    )


def job_scale_label(options: dict | None, preset_id: str = "") -> str:
    """Text pro seznam jobů: „1:10000 · 5 m“."""
    opts = options or {}
    scale = parse_map_scale(opts.get("map_scale"))
    if scale is None and opts.get("scalefactor") is not None:
        try:
            scale = map_scale_from_scalefactor(float(opts["scalefactor"]))
            if scale not in MAP_SCALES:
                scale = None
        except (TypeError, ValueError):
            scale = None
    if scale is None and preset_id:
        scale = map_scale_from_preset_id(preset_id)
    contour = parse_contour_interval(opts.get("contour_interval"))
    if contour is None and scale is not None:
        contour = default_contour_for_scale(scale)
    if scale is None:
        return preset_id or "?"
    if contour is None:
        return f"1:{scale}"
    if float(contour).is_integer():
        c_txt = str(int(contour))
    else:
        c_txt = str(contour).replace(".", ",")
    return f"1:{scale} · {c_txt} m"


def oom_metadata(
    preset_id: str,
    preset: dict,
    options: dict,
    job_name: str = "",
    *,
    reference_layers: list[str] | None = None,
) -> dict:
    sf = float(options.get("scalefactor", preset.get("scalefactor", 1)))
    meta = {
        "name": job_name,
        "app_version": APP_VERSION,
        "preset_id": preset_id,
        "label": preset.get("label", preset_id),
        "crs": "EPSG:5514",
        "scale": map_scale_from_scalefactor(sf),
        "scalefactor": sf,
        "contour_interval_m": options.get(
            "contour_interval", preset.get("contour_interval")
        ),
        "formline": options.get("formline", preset.get("formline")),
        **reference_metadata(),
    }
    if reference_layers:
        meta["reference_layers"] = reference_layers
    meta["path_source"] = resolve_path_source(options.get("path_source"))
    return meta


def oom_readme(meta: dict) -> str:
    scale = meta.get("scale") or "?"
    label = meta.get("label") or meta.get("preset_id") or ""
    interval = meta.get("contour_interval_m")
    interval_txt = f"{interval} m" if interval is not None else "?"
    refs = meta.get("reference_layers") or []
    ref_block = ""
    if refs:
        ref_block = (
            "\nReferenční podklady (složka references/)\n"
            "----------------------------------------\n"
            + "\n".join(f"- {name}" for name in refs)
            + "\n"
        )
    return (
        "Podkladárna – balíček pro OpenOrienteering Mapper\n"
        "=================================================\n\n"
        "Nejprve přečtěte CO_JE_PODKLADARNA.txt v kořeni ZIPu.\n\n"
        f"Typ mapy: {label}\n"
        f"Měřítko: 1:{scale}\n"
        f"Ekvidistance: {interval_txt}\n"
        "Souřadnicový systém: EPSG:5514 (S-JTSK / Křovák)\n\n"
        f"Stínovaný reliéf DMR 5G (ČÚZK WMS): základní, Z10 a Z20 ve složce references/.\n"
        "Mapové podklady: OpenStreetMap, Základní topografická mapa ČR (ZTM), katastrální mapa a náhled DMP OK.\n"
        f"{ref_block}\n"
        "Doporučený postup v OOM\n"
        "-----------------------\n"
        "1. Rozbalte celý ZIP do jedné složky. Otevřete vybraný *-sprint.omap / *-les.omap / *-mtbo.omap\n"
        "   (podle měřítka; cesty z OSM).\n"
        "   Výchozí pohled: jen vektory (vrstevnice, zeleň, ZABAGED, OSM budovy, srázy, …).\n"
        "   Fialový obdélník = váš výřez; vně je jen přesah polohopisu.\n"
        "   Vrstevnice (101/102) jsou zamčené (is_protected) – odemkni v panelu symbolů.\n"
        "2. PNG podklady (OSM, KP náhled, ortofoto, hillshade, …) zapněte dle potřeby\n"
        "   v Šablony → Nastavení šablon (Template Setup); KP PNG náhledy jsou ve složce kp/.\n"
        "3. Deprese: šablona „Karttapullautin deprese“.\n"
        "4. Budovy v .omap jsou z OSM. Podklady: osm/OSM_budovy.shp, Budova* a\n"
        "   RUIAN_budovy.shp ve zabaged/. Cesty ZABAGED jsou ve zabaged/ pro ruční import.\n"
        "   KP PNG náhledy ve složce kp/; ve složce base/: vrstevnice GDAL\n"
        "   (contours_gdal.*), vrstevnice KP (contours_kp.dxf), vegetace, srázy, knolly.\n\n"
        "OCAD: soubor .omap neotevře – importujte DXF, SHP nebo georeferencované PNG+PGW.\n"
        "Nebo v OOM exportujte do formátu OCD (v8–12).\n\n"
        "Data: ČÚZK (DMR 5G, DMP OK, ZABAGED®, RÚIAN/INSPIRE, ortofoto), CC BY 4.0. "
        "AOPK památné stromy (CC BY 4.0). "
        "OSM © přispěvatelé (ODbL). Výstup jobu: CC BY 4.0 – při šíření uveďte zdroj:\n"
        "Podklad: Podkladárna · ČÚZK · OSM · Karttapullautin, [rok].\n"
        "Reliéf a vegetace: Karttapullautin (GPL-3.0).\n\n"
        "Podkladárna je experiment — zpětná vazba a připomínky:\n"
        "https://github.com/pjanecekATlangmaster/podkladarna/issues\n"
    )


def _write_if_exists(zf: zipfile.ZipFile, src: Path, arcname: str) -> bool:
    if src.is_file():
        zf.write(src, arcname)
        return True
    return False


def _add_shapefiles_from_zip(
    zf: zipfile.ZipFile,
    src_zip: Path,
    dest_dir: str,
    *,
    only_layers: frozenset[str] | set[str] | None = None,
    exclude_layers: frozenset[str] | set[str] | None = None,
) -> int:
    n = 0
    keep = {".shp", ".shx", ".dbf", ".prj", ".cpg"}
    only = {name.lower() for name in only_layers} if only_layers else None
    exclude = {name.lower() for name in exclude_layers} if exclude_layers else set()
    with zipfile.ZipFile(src_zip) as src:
        for info in src.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name
            if not name or name.startswith("."):
                continue
            if Path(name).suffix.lower() not in keep:
                continue
            stem = Path(name).stem.lower()
            if only is not None and stem not in only:
                continue
            if stem in exclude:
                continue
            data = src.read(info)
            zf.writestr(f"{dest_dir}/{name}", data)
            n += 1
    return n


def _write_ostatni_band_shapefiles(
    zabaged_clean: Path,
    dest_dir: Path,
) -> list[Path]:
    """Rozdělí OstatniPlochaVSidlech do pásem mensi/stredni/velke (SHP)."""
    from app.pipeline.fetch_zabaged import (
        OSTATNI_LAYER_STEM,
        feature_area_m2,
        ostatni_band_stem,
    )

    try:
        from osgeo import ogr
    except ImportError:
        return []
    ogr.UseExceptions()

    dest_dir.mkdir(parents=True, exist_ok=True)
    # Extract source layer
    stage = dest_dir / "_ostatni_src"
    stage.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zabaged_clean) as zf:
        for info in zf.infolist():
            name = Path(info.filename).name
            if name.lower().startswith(OSTATNI_LAYER_STEM.lower() + "."):
                (stage / name).write_bytes(zf.read(info))
    src_shp = stage / f"{OSTATNI_LAYER_STEM}.shp"
    if not src_shp.is_file():
        return []
    src_ds = ogr.Open(str(src_shp))
    if not src_ds:
        return []
    src_lyr = src_ds.GetLayer(0)
    if src_lyr is None:
        return []

    buckets: dict[str, list] = {}
    for feat in src_lyr:
        geom = feat.GetGeometryRef()
        props = {feat.GetFieldDefnRef(i).GetName(): feat.GetField(i) for i in range(feat.GetFieldCount())}
        area = feature_area_m2(
            props,
            geom_area_m2=float(geom.GetArea()) if geom is not None else None,
        )
        if area is None:
            continue
        stem = ostatni_band_stem(area)
        buckets.setdefault(stem, []).append(feat.Clone())

    driver = ogr.GetDriverByName("ESRI Shapefile")
    written: list[Path] = []
    srs = src_lyr.GetSpatialRef()
    for stem, feats in buckets.items():
        if not feats:
            continue
        out_shp = dest_dir / f"{stem}.shp"
        if out_shp.exists():
            driver.DeleteDataSource(str(out_shp))
        out_ds = driver.CreateDataSource(str(out_shp))
        out_lyr = out_ds.CreateLayer(
            stem, srs=srs, geom_type=src_lyr.GetGeomType()
        )
        # copy fields from source
        defn = src_lyr.GetLayerDefn()
        for i in range(defn.GetFieldCount()):
            out_lyr.CreateField(defn.GetFieldDefn(i))
        for feat in feats:
            out_lyr.CreateFeature(feat)
        out_ds = None
        written.append(out_shp)
    src_ds = None
    return written


def prepare_oom_map(
    kp_cwd: Path,
    dest: Path,
    *,
    map_name: str,
    scale: int,
    preset_id: str,
    bbox_wgs84: tuple[float, float, float, float],
    built_refs: dict[str, Path] | None = None,
    zabaged_clean: Path | None = None,
    vectorconf_name: str = "zabaged.txt",
    include_dxf: bool = True,
    contour_interval_m: float | None = None,
    formline: float = 0,
    indexcontours_m: float | None = None,
    cliff_symbol: str = "earth_bank",
    courtyard_olive: bool = False,
    path_source: str = PATH_SOURCE_MIXED,
    aopk_trees: Path | None = None,
    max_ostatni_m2: float | None = 50_000.0,
    ostatni_as_403: bool = False,
) -> Path | None:
    del formline
    path_source = resolve_path_source(path_source)
    west, south, east, north = bbox_wgs84
    xmin, ymin, xmax, ymax = crop_bounds_5514(west, south, east, north)
    pullautus_png = kp_cwd / "pullautus.png"
    pullautus_pgw = kp_cwd / "pullautus.pgw"
    if pullautus_png.is_file() and pullautus_pgw.is_file():
        try:
            ref_x, ref_y = projected_center_from_raster(pullautus_png, pullautus_pgw)
        except ValueError:
            ref_x = (xmin + xmax) / 2
            ref_y = (ymin + ymax) / 2
    else:
        ref_x = (xmin + xmax) / 2
        ref_y = (ymin + ymax) / 2
    ref_lat, ref_lon = projected_to_wgs84(ref_x, ref_y)
    _, grivation = oom_north_angles(ref_x, ref_y)
    # KP/LiDAR běží na širším výřezu (CROP_BUFFER_M) – DXF srazy/kameny z okraje
    # by jinak zaplavily mapu. Vektorové zdroje (OSM, ZABAGED, AOPK) necháme
    # přesahovat za AOI; uživatel je případně ořízne v OOM.
    dxf_clip_bounds = expand(
        crop_bounds_5514(west, south, east, north, buffer_m=0.0), CLIP_MARGIN_M
    )
    templates = collect_oom_templates(
        kp_cwd,
        built_refs=built_refs,
        include_dxf=include_dxf,
        include_dxf_templates=False,
    )
    if not templates:
        return None

    object_parts: list[OomObjectPart] = []
    zabaged_under: list[OomObjectPart] = []
    zabaged_base_paved: list[OomObjectPart] = []
    zabaged_rest: list[OomObjectPart] = []
    courtyard_olive_parts: list[OomObjectPart] = []
    prefer_osm_paths: list[list[tuple[float, float]]] = []
    if path_source == PATH_SOURCE_MIXED:
        prefer_osm_paths = load_osm_path_lines(kp_cwd)
    if zabaged_clean and zabaged_clean.is_file():
        omit = set(ZABAGED_OMIT_BUILDING_LAYERS)
        if osm_features_have_power_lines(kp_cwd):
            omit |= set(ZABAGED_OMIT_WHEN_OSM_POWER)
        for part in build_zabaged_object_parts(
            zabaged_clean,
            vectorconf_name=vectorconf_name,
            preset_id=preset_id,
            scale=scale,
            ref_x=ref_x,
            ref_y=ref_y,
            grivation_deg=grivation,
            work_dir=kp_cwd.parent,
            clip_bounds=None,
            courtyard_olive=False,  # oliva dvorů z OSM budov
            prefer_osm_path_lines=prefer_osm_paths or None,
            omit_path_layers=path_source == PATH_SOURCE_OSM,
            omit_layers=omit,
            max_ostatni_m2=max_ostatni_m2,
            ostatni_as_403=ostatni_as_403,
        ):
            if _COURTYARD_OLIVE_MARK in part.name:
                courtyard_olive_parts.append(part)
                continue
            layer = part.name.removeprefix("ZABAGED – ").strip()
            if layer in _ZABAGED_UNDER_VEGETATION:
                zabaged_under.append(part)
            elif layer in _ZABAGED_BASE_PAVED:
                zabaged_base_paved.append(part)
            else:
                zabaged_rest.append(part)

    # Nejspodnější podklad: zpevněné plochy ze ZABAGED (501), pak louky…
    object_parts.extend(zabaged_base_paved)
    # Louky/zeleň ze ZABAGED + OSM 412 pod KP (hustníky z LiDARu musí zůstat vidět).
    object_parts.extend(zabaged_under)
    aopk_pts = load_aopk_tree_points(aopk_trees)
    osm_feat = build_osm_feature_parts(
        kp_cwd,
        preset_id=preset_id,
        scale=scale,
        ref_x=ref_x,
        ref_y=ref_y,
        grivation_deg=grivation,
        clip_bounds=None,
        aopk_tree_points=aopk_pts or None,
        courtyard_olive=courtyard_olive,
    )
    osm_under: list[OomObjectPart] = []
    osm_feat_rest: list[OomObjectPart] = []
    for part in osm_feat:
        if _COURTYARD_OLIVE_MARK in part.name:
            courtyard_olive_parts.append(part)
            continue
        if _OSM_UNDER_VEGETATION_MARK in part.name:
            osm_under.append(part)
        else:
            osm_feat_rest.append(part)
    object_parts.extend(osm_under)
    # KP zeleň (+ 401 s odečtem ZABAGED/OSM ploch) pod vrstevnicemi.
    subtract_wkbs = collect_kp401_subtract_wkbs(
        zabaged_clean=zabaged_clean if zabaged_clean and zabaged_clean.is_file() else None,
        work_dir=kp_cwd,
    )
    object_parts.extend(
        build_vegetation_parts(
            kp_cwd,
            preset_id=preset_id,
            scale=scale,
            ref_x=ref_x,
            ref_y=ref_y,
            grivation_deg=grivation,
            subtract_wkbs=subtract_wkbs or None,
        )
    )
    object_parts.extend(
        build_gdal_contour_parts(
            kp_cwd,
            preset_id=preset_id,
            scale=scale,
            ref_x=ref_x,
            ref_y=ref_y,
            grivation_deg=grivation,
            interval_m=float(contour_interval_m or 5),
            formline=0,
            index_m=float(indexcontours_m) if indexcontours_m else None,
        )
    )
    if include_dxf:
        dxf_part = build_dxf_object_part(
            kp_cwd,
            preset_id=preset_id,
            scale=scale,
            ref_x=ref_x,
            ref_y=ref_y,
            grivation_deg=grivation,
            cliff_symbol=cliff_symbol,
            clip_bounds=dxf_clip_bounds,
        )
        if dxf_part:
            object_parts.append(dxf_part)
    object_parts.extend(zabaged_rest)
    # OSM cesty / u režimu jen-ZABAGED aspoň krátké lávky (ČÚZK je často nemá).
    osm_parts = build_osm_path_parts(
        kp_cwd,
        preset_id=preset_id,
        scale=scale,
        ref_x=ref_x,
        ref_y=ref_y,
        grivation_deg=grivation,
        clip_bounds=None,
        path_source=path_source,
    )
    if osm_parts:
        object_parts.extend(osm_parts)
    if osm_feat_rest:
        object_parts.extend(osm_feat_rest)
    if aopk_trees and aopk_trees.is_file():
        object_parts.extend(
            build_aopk_tree_parts(
                aopk_trees,
                preset_id=preset_id,
                scale=scale,
                ref_x=ref_x,
                ref_y=ref_y,
                grivation_deg=grivation,
                clip_bounds=None,
            )
        )
    # Oliva dvorů až nakonec – překryje vegetaci/OSM detaily uvnitř budov.
    object_parts.extend(courtyard_olive_parts)
    aoi_part = build_aoi_boundary_part(
        bbox_wgs84,
        preset_id=preset_id,
        scale=scale,
        ref_x=ref_x,
        ref_y=ref_y,
        grivation_deg=grivation,
    )
    if aoi_part:
        object_parts.append(aoi_part)
    return write_oom_map(
        dest,
        map_name=map_name,
        scale=scale,
        ref_x=ref_x,
        ref_y=ref_y,
        ref_lat=ref_lat,
        ref_lon=ref_lon,
        templates=templates,
        object_parts=object_parts or None,
        preset_id=preset_id,
    )


def build_oom_zip(
    kp_cwd: Path,
    dest_zip: Path,
    *,
    zabaged_clean: Path | None,
    metadata: dict,
    reference_dir: Path | None = None,
    omap_paths: list[Path] | None = None,
    include_zabaged_archive: bool = False,
    include_png: bool = True,
    include_dxf: bool = True,
    include_cliffs: bool = True,
    ruian_buildings: Path | None = None,
) -> Path:
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    if dest_zip.exists():
        dest_zip.unlink()

    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("CO_JE_PODKLADARNA.txt", ZIP_ABOUT_TXT)
        if omap_paths:
            for omap_path in omap_paths:
                if omap_path.is_file():
                    zf.write(omap_path, omap_path.name)
        zf.writestr("README_OOM.txt", oom_readme(metadata))
        zf.writestr(
            "metadata.json",
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        )
        for name in ("pullautus.png", "pullautus.pgw"):
            if include_png:
                _write_if_exists(zf, kp_cwd / name, f"kp/{name}")
        for name in ("pullautus_depr.png", "pullautus_depr.pgw"):
            if include_png:
                _write_if_exists(zf, kp_cwd / name, f"kp/{name}")
        if reference_dir and reference_dir.is_dir():
            for png in sorted(reference_dir.glob("*.png")):
                zf.write(png, f"references/{png.name}")
                pgw = png.with_suffix(".pgw")
                if pgw.is_file():
                    zf.write(pgw, f"references/{pgw.name}")
        temp = kp_cwd / "temp"
        if include_dxf and temp.is_dir():
            for zip_name, src in sorted(
                collect_dxf_for_zip(temp, include_cliffs=include_cliffs).items()
            ):
                zf.write(src, f"base/{zip_name}")
        contours_dir = kp_cwd / "contours"
        if contours_dir.is_dir():
            for path in sorted(contours_dir.iterdir()):
                if path.is_file() and path.suffix.lower() in {
                    ".shp",
                    ".shx",
                    ".dbf",
                    ".prj",
                    ".cpg",
                }:
                    # Odlišit od KP DXF (contours_kp.dxf).
                    stem = path.stem.lower()
                    if stem == "contours":
                        arc = f"base/contours_gdal{path.suffix.lower()}"
                    else:
                        arc = f"base/{path.name}"
                    zf.write(path, arc)
        vege_dir = kp_cwd / "vegetation"
        if vege_dir.is_dir():
            for path in sorted(vege_dir.iterdir()):
                if path.is_file() and path.suffix.lower() in {
                    ".shp",
                    ".shx",
                    ".dbf",
                    ".prj",
                    ".cpg",
                }:
                    zf.write(path, f"base/{path.name}")
        osm_dir = kp_cwd / "osm_paths"
        # SHP pro ruční skládání (jako zabaged/) – primární obsah osm/.
        manual_dir = osm_dir / "manual"
        if manual_dir.is_dir():
            for path in sorted(manual_dir.iterdir()):
                if path.name == "README.txt":
                    zf.write(path, "osm/README.txt")
                    continue
                if path.is_file() and path.suffix.lower() in {
                    ".shp",
                    ".shx",
                    ".dbf",
                    ".prj",
                    ".cpg",
                }:
                    zf.write(path, f"osm/{path.name}")
        else:
            zf.writestr("osm/README.txt", OSM_FOLDER_README)
        # Budovy: OSM_budovy.shp z osm_paths/budovy/ → přímo do osm/ (ne podsložka).
        budovy_shp = osm_dir / "budovy"
        if budovy_shp.is_dir():
            for path in sorted(budovy_shp.iterdir()):
                if path.is_file() and path.suffix.lower() in {
                    ".shp",
                    ".shx",
                    ".dbf",
                    ".prj",
                    ".cpg",
                }:
                    zf.write(path, f"osm/{path.name}")
        # osm_kp.zip = vstup KP; pokud chybí manual OSM_cesty, rozbal jako fallback.
        osm_kp = kp_cwd / "osm_kp.zip"
        if osm_kp.is_file() and not (manual_dir / "OSM_cesty.shp").is_file():
            _add_shapefiles_from_zip(zf, osm_kp, "osm")
        if zabaged_clean and zabaged_clean.is_file():
            from app.pipeline.fetch_zabaged import OSTATNI_LAYER_STEM
            from app.pipeline.crs_5514 import write_prj

            # Monolitickou Ostatní rozdělíme do pásem; prázdná pásma přeskočíme.
            _add_shapefiles_from_zip(
                zf,
                zabaged_clean,
                "zabaged",
                exclude_layers={OSTATNI_LAYER_STEM},
            )
            band_dir = kp_cwd / "_ostatni_bands"
            for shp in _write_ostatni_band_shapefiles(zabaged_clean, band_dir):
                write_prj(shp)
                for side in shp.parent.glob(shp.stem + ".*"):
                    if side.suffix.lower() in {".shp", ".shx", ".dbf", ".prj", ".cpg"}:
                        zf.write(side, f"zabaged/{side.name}")
            if include_zabaged_archive:
                zf.write(zabaged_clean, "zabaged_clean.zip")
        # RÚIAN budovy jako SHP do zabaged/ (ne do auto .omap – tam je OSM).
        if ruian_buildings and ruian_buildings.is_file():
            ruian_dir = kp_cwd / "ruian_shp"
            write_ruian_buildings_shapefile(ruian_buildings, ruian_dir)
            if ruian_dir.is_dir():
                for path in sorted(ruian_dir.iterdir()):
                    if path.is_file() and path.suffix.lower() in {
                        ".shp",
                        ".shx",
                        ".dbf",
                        ".prj",
                        ".cpg",
                    }:
                        zf.write(path, f"zabaged/{path.name}")

    return dest_zip
