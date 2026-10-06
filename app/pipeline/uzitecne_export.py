"""Složka uzitecne/ ve ZIPu – vektory k ručnímu importu (i nezaškrtnuté v auto .omap)."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from app.pipeline.karttapullautin_dxf import collect_dxf_for_zip
from app.pipeline.residual_paved import _RESIDUAL_BANDS_DIR

logger = logging.getLogger(__name__)

UZITECNE_DIR_NAME = "_uzitecne"
UZITECNE_ZIP_PREFIX = "uzitecne"

# SHP z osm/manual (nebo budovy) → kopie do uzitecne.
_OSM_MANUAL_COPY = (
    "OSM_cesty",
    "OSM_lavicky",
    "OSM_lampy",
    "OSM_stoly",
    "OSM_ohniste",
    "OSM_tabule",
    "OSM_stromy",
    "OSM_budovy",
    "OSM_zahrady",
    "OSM_posedy",
    "OSM_ploty",
    "OSM_zdi",
    "OSM_fitness",
    "OSM_brany",
    "OSM_pomniky",
    "OSM_pristresky",
    "OSM_veze",
    "OSM_vedeni",
    "OSM_vedeni_velke",
    "OSM_zive_ploty",
    "OSM_skaly",
    "OSM_skaly_linie",
    "OSM_sutina",
)

# Herní prvky: stem z OSM_MANUAL_LAYER_SPECS nebo OSM_playground_equipment.
_ZABAGED_PATH_COPY = ("Pesina", "Cesta", "Ulice")

_SHP_SIDE = (".shp", ".shx", ".dbf", ".prj", ".cpg")

UZITECNE_README = """Užitečné vektory k ručnímu importu
=================================
Sem patří podklady, které v auto .omap nemusí být (vypnutý checkbox / knolly),
nebo alternativní zdroje (cesty ZABAGED vs OSM). Import v OOM: File → Importovat…
a přiřaď symbol.

kopecky.dxf              Kopečky KP (ISOM 109) – v auto .omap nejsou
OSM_lavicky / lampy / …  OSM nábytek (i když checkbox vypnutý)
OSM_stromy               Významné stromy OSM (417)
AOPK_pamatne_stromy      Památné stromy AOPK (417)
OSM_cesty                Cesty OSM
OSM_skaly / _linie / sutina  OSM skály (podklad; ne v auto .omap – LiDAR 206)
ZABAGED_Pesina/Cesta/Ulice  Cesty ZABAGED (druhý zdroj)
OSM_residential_zbytek_* Residual 501 (mensi/stredni/velke/ridke)
OstatniPlochaVSidlech_*  Zpevněné plochy ZABAGED podle velikosti
OSM_dvory_oliva          Dvory uvnitř budov (520) – i při vypnuté olivě

Souřadnice: EPSG:5514 (S-JTSK). Stejné vrstvy často i v osm/ nebo zabaged/.
"""


def prepare_uzitecne_dir(
    kp_cwd: Path,
    *,
    zabaged_clean: Path | None = None,
    aopk_trees: Path | None = None,
    log=None,
) -> Path:
    """Sestaví ``kp_cwd/_uzitecne/`` (kopie + exporty). Vrací cestu ke složce."""
    dest = kp_cwd / UZITECNE_DIR_NAME
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    _copy_kopecky(kp_cwd, dest, log=log)
    _copy_osm_manual(kp_cwd, dest, log=log)
    _copy_residual_bands(kp_cwd, dest, log=log)
    _copy_zabaged_paths(zabaged_clean, dest, log=log)
    _copy_ostatni_bands(kp_cwd, dest, log=log)
    _write_aopk_shp(aopk_trees, dest, log=log)
    _write_courtyard_olive_shp(kp_cwd, dest, log=log)
    # Herní prvky – případný stem bez specifikace.
    _copy_glob_stems(kp_cwd / "osm_paths" / "manual", dest, ("OSM_playground_equipment",))

    (dest / "README.txt").write_text(UZITECNE_README, encoding="utf-8")
    if log:
        n = sum(1 for p in dest.iterdir() if p.is_file())
        log(f"uzitecne/: {n} souborů (vektory k ručnímu importu)")
    return dest


def add_uzitecne_to_zip(zf, kp_cwd: Path) -> int:
    """Zapíše obsah ``_uzitecne/`` do ZIPu pod ``uzitecne/``. Vrací počet souborů."""
    src = kp_cwd / UZITECNE_DIR_NAME
    if not src.is_dir():
        return 0
    n = 0
    for path in sorted(src.iterdir()):
        if not path.is_file():
            continue
        zf.write(path, f"{UZITECNE_ZIP_PREFIX}/{path.name}")
        n += 1
    return n


def _copy_side_files(src_shp: Path, dest_dir: Path, dest_stem: str | None = None) -> None:
    stem = dest_stem or src_shp.stem
    for suffix in _SHP_SIDE:
        side = src_shp.with_suffix(suffix)
        if side.is_file():
            shutil.copy2(side, dest_dir / f"{stem}{suffix}")


def _copy_kopecky(kp_cwd: Path, dest: Path, *, log=None) -> None:
    temp = kp_cwd / "temp"
    if not temp.is_dir():
        return
    src: Path | None = None
    collected = collect_dxf_for_zip(temp, include_cliffs=False, include_contours=False)
    src = collected.get("kopecky.dxf")
    if src is None or not src.is_file():
        # collect_dxf vyžaduje ≥8 B; fallback na surový KP soubor.
        raw = temp / "dotknolls.dxf"
        if raw.is_file():
            src = raw
    if src and src.is_file():
        shutil.copy2(src, dest / "kopecky.dxf")
        if log:
            log("uzitecne/: kopecky.dxf")


def _copy_osm_manual(kp_cwd: Path, dest: Path, *, log=None) -> None:
    manual = kp_cwd / "osm_paths" / "manual"
    budovy = kp_cwd / "osm_paths" / "budovy"
    for stem in _OSM_MANUAL_COPY:
        for base in (manual, budovy):
            shp = base / f"{stem}.shp"
            if shp.is_file():
                _copy_side_files(shp, dest)
                break
    # Herní prvky: osm_paths může použít OSM_playground_equipment nebo spec.
    for stem in ("OSM_playground_equipment", "OSM_herni_prvky"):
        shp = manual / f"{stem}.shp"
        if shp.is_file():
            _copy_side_files(shp, dest, dest_stem="OSM_herni_prvky")
            break


def _copy_glob_stems(src_dir: Path, dest: Path, stems: tuple[str, ...]) -> None:
    if not src_dir.is_dir():
        return
    for stem in stems:
        shp = src_dir / f"{stem}.shp"
        if shp.is_file():
            _copy_side_files(shp, dest)


def _copy_residual_bands(kp_cwd: Path, dest: Path, *, log=None) -> None:
    band_dir = kp_cwd / _RESIDUAL_BANDS_DIR
    if not band_dir.is_dir():
        return
    for path in band_dir.iterdir():
        if path.suffix.lower() in _SHP_SIDE:
            shutil.copy2(path, dest / path.name)


def _copy_zabaged_paths(
    zabaged_clean: Path | None, dest: Path, *, log=None
) -> None:
    if not zabaged_clean or not zabaged_clean.is_file():
        return
    import zipfile

    stage = dest / "_zabaged_paths_src"
    stage.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zabaged_clean) as zf:
            for info in zf.infolist():
                name = Path(info.filename).name
                stem = Path(name).stem
                if stem in _ZABAGED_PATH_COPY and Path(name).suffix.lower() in _SHP_SIDE:
                    (stage / name).write_bytes(zf.read(info))
        for layer in _ZABAGED_PATH_COPY:
            shp = stage / f"{layer}.shp"
            if shp.is_file():
                _copy_side_files(shp, dest, dest_stem=f"ZABAGED_{layer}")
    except (OSError, zipfile.BadZipFile) as exc:
        logger.warning("uzitecne: ZABAGED cesty: %s", exc)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _copy_ostatni_bands(kp_cwd: Path, dest: Path, *, log=None) -> None:
    band_dir = kp_cwd / "_ostatni_bands"
    if not band_dir.is_dir():
        return
    for path in band_dir.iterdir():
        if path.suffix.lower() in _SHP_SIDE and path.name.startswith(
            "OstatniPlochaVSidlech"
        ):
            shutil.copy2(path, dest / path.name)


def _write_aopk_shp(aopk_trees: Path | None, dest: Path, *, log=None) -> None:
    if not aopk_trees or not aopk_trees.is_file():
        return
    try:
        data = json.loads(aopk_trees.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    feats = list(data.get("features") or [])
    if not feats:
        return
    from app.pipeline.osm_paths import _geojson_to_shapefile

    shp = dest / "AOPK_pamatne_stromy.shp"
    if _geojson_to_shapefile(
        feats, shp, nlt="POINT", log=log, label="AOPK památné stromy"
    ):
        if log:
            log(f"uzitecne/: AOPK_pamatne_stromy ({len(feats)})")


def _write_courtyard_olive_shp(kp_cwd: Path, dest: Path, *, log=None) -> None:
    """Díry v OSM budovách → polygony olivy (520) pro ruční import."""
    from app.pipeline.osm_paths import _OSM_BUILDING_KINDS, _geojson_to_shapefile

    gj = kp_cwd / "osm_paths" / "features.geojson"
    if not gj.is_file():
        return
    try:
        data = json.loads(gj.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    hole_feats: list[dict] = []
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        kind = str(props.get("kind") or "")
        if kind not in _OSM_BUILDING_KINDS:
            continue
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates") or []
        polys: list = []
        if gtype == "Polygon":
            polys = [coords]
        elif gtype == "MultiPolygon":
            polys = list(coords)
        else:
            continue
        for poly in polys:
            if not poly or len(poly) < 2:
                continue
            for hole in poly[1:]:
                if not hole or len(hole) < 3:
                    continue
                ring = list(hole)
                if ring[0] != ring[-1]:
                    ring = ring + [ring[0]]
                hole_feats.append(
                    {
                        "type": "Feature",
                        "properties": {"kind": "courtyard_olive", "oom_code": "520"},
                        "geometry": {"type": "Polygon", "coordinates": [ring]},
                    }
                )
    if not hole_feats:
        return
    shp = dest / "OSM_dvory_oliva.shp"
    if _geojson_to_shapefile(
        hole_feats, shp, nlt="POLYGON", log=log, label="OSM dvory oliva"
    ):
        if log:
            log(f"uzitecne/: OSM_dvory_oliva ({len(hole_feats)})")
        return
    # Fallback bez ogr2ogr (lokální / CI bez GDAL).
    if _write_polygon_geojson_as_shp_pyshp(hole_feats, shp):
        if log:
            log(f"uzitecne/: OSM_dvory_oliva pyshp ({len(hole_feats)})")


def _write_polygon_geojson_as_shp_pyshp(features: list[dict], dest_shp: Path) -> bool:
    try:
        import shapefile
        from app.pipeline.crs_5514 import write_prj
    except ImportError:
        return False
    for suffix in _SHP_SIDE:
        dest_shp.with_suffix(suffix).unlink(missing_ok=True)
    n = 0
    with shapefile.Writer(str(dest_shp.with_suffix("")), shapeType=shapefile.POLYGON) as w:
        w.field("kind", "C", size=40)
        w.field("oom_code", "C", size=8)
        for feat in features:
            props = feat.get("properties") or {}
            geom = feat.get("geometry") or {}
            if geom.get("type") != "Polygon":
                continue
            coords = geom.get("coordinates") or []
            if not coords:
                continue
            rings = [[[float(x), float(y)] for x, y in ring] for ring in coords]
            if not rings or len(rings[0]) < 3:
                continue
            w.poly(rings)
            w.record(str(props.get("kind") or ""), str(props.get("oom_code") or ""))
            n += 1
    if n > 0 and dest_shp.is_file():
        write_prj(dest_shp)
        return True
    return False
