"""Složka ``uzitecne/`` – vegetace / srázy 104 / skály 201.2 k prohlížení.

Bez-KP dřív nechávalo jen raw ``base/vegetation.*`` a tick DXF; filtrací
(min-size, occupancy, dense-contour, …) zmizely kandidáty. Tady je export
**použité** (co šlo do auto .omap) vs **vyhozené** (s polem ``duvod``).

Layout ve ZIPu i v ``output/``::

    uzitecne/
      README.txt
      pouzite/vegetace.*  srazy_104.*  skaly_201.*
      vyhozene/vegetace.* srazy_104.*  skaly_201.*
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.pipeline.crs_5514 import write_prj

UZITECNE_DIR_NAME = "uzitecne"
POUZITE = "pouzite"
VYHOZENE = "vyhozene"

_SHP_SIDE = (".shp", ".shx", ".dbf", ".prj", ".cpg")

UZITECNE_README = """Užitečné vektory – vegetace, srázy, skály
========================================
Prohlížení a ruční import v OOM / QGIS (EPSG:5514).

pouzite/   … co zůstalo v auto .omap po filtrech
vyhozene/  … kandidáti zahození (atribut duvod = důvod filtru)

Vrstvy
------
vegetace.*   polygony porostů (cls, code = 401/406/408/410)
srazy_104.*  zemní srázy (linie ISOM 104)
skaly_201.*  skalní plochy (polygony 201.2 / 206)

Důvody ve vyhozene (příklady): min_plocha, zamotany, nizky_schod,
deprese, budova, prekryv_104, occupancy, husté_vrstevnice.

Raw tick DXF a vegetation.shp jsou dál i ve složce base/ (před finální
filtrací u srázů). Tahle složka ukazuje výsledek filtrů.
"""


def uzitecne_root(kp_cwd: Path) -> Path:
    return Path(kp_cwd) / UZITECNE_DIR_NAME


def ensure_uzitecne_dirs(kp_cwd: Path) -> tuple[Path, Path, Path]:
    root = uzitecne_root(kp_cwd)
    pouzite = root / POUZITE
    vyhozene = root / VYHOZENE
    pouzite.mkdir(parents=True, exist_ok=True)
    vyhozene.mkdir(parents=True, exist_ok=True)
    return root, pouzite, vyhozene


def _clear_stem(dest_dir: Path, stem: str) -> None:
    for suffix in _SHP_SIDE:
        (dest_dir / f"{stem}{suffix}").unlink(missing_ok=True)


def _copy_shp_stem(src_shp: Path, dest_dir: Path, dest_stem: str) -> bool:
    if not src_shp.is_file():
        return False
    dest_dir.mkdir(parents=True, exist_ok=True)
    _clear_stem(dest_dir, dest_stem)
    for suffix in _SHP_SIDE:
        side = src_shp.with_suffix(suffix)
        if side.is_file():
            shutil.copy2(side, dest_dir / f"{dest_stem}{suffix}")
    return (dest_dir / f"{dest_stem}.shp").is_file()


def write_line_shapefile(
    dest_shp: Path,
    lines: list[list[tuple[float, float]]],
    *,
    reasons: list[str] | None = None,
) -> Path | None:
    """Zapíše linie (S-JTSK). ``reasons`` → pole ``duvod`` (stejná délka)."""
    if not lines:
        return None
    try:
        import shapefile
    except ImportError:
        return None
    dest_shp = Path(dest_shp)
    dest_shp.parent.mkdir(parents=True, exist_ok=True)
    _clear_stem(dest_shp.parent, dest_shp.stem)
    with_reason = reasons is not None
    if with_reason and len(reasons) != len(lines):
        reasons = list(reasons) + [""] * max(0, len(lines) - len(reasons or []))
        reasons = reasons[: len(lines)]
    with shapefile.Writer(str(dest_shp.with_suffix("")), shapeType=shapefile.POLYLINE) as w:
        w.field("oom_code", "C", size=8)
        if with_reason:
            w.field("duvod", "C", size=40)
        for i, pts in enumerate(lines):
            if len(pts) < 2:
                continue
            ring = [[float(x), float(y)] for x, y in pts]
            w.line([ring])
            if with_reason:
                w.record("104", str(reasons[i] or "")[:40])
            else:
                w.record("104")
    if not dest_shp.is_file():
        return None
    write_prj(dest_shp)
    return dest_shp


def write_polygon_shapefile(
    dest_shp: Path,
    rings: list[list[tuple[float, float]]],
    *,
    oom_code: str = "201.2",
    reasons: list[str] | None = None,
    extra_fields: list[tuple[str, str, str]] | None = None,
) -> Path | None:
    """Zapíše polygony (jeden ring = jedna plocha). ``reasons`` → ``duvod``."""
    if not rings:
        return None
    try:
        import shapefile
    except ImportError:
        return None
    dest_shp = Path(dest_shp)
    dest_shp.parent.mkdir(parents=True, exist_ok=True)
    _clear_stem(dest_shp.parent, dest_shp.stem)
    with_reason = reasons is not None
    if with_reason and len(reasons) != len(rings):
        reasons = list(reasons) + [""] * max(0, len(rings) - len(reasons or []))
        reasons = reasons[: len(rings)]
    with shapefile.Writer(str(dest_shp.with_suffix("")), shapeType=shapefile.POLYGON) as w:
        w.field("oom_code", "C", size=8)
        if with_reason:
            w.field("duvod", "C", size=40)
        for field_name, field_type, _ in extra_fields or []:
            w.field(field_name, field_type, size=16 if field_type == "C" else 10)
        n = 0
        for i, ring in enumerate(rings):
            if len(ring) < 3:
                continue
            coords = [[float(x), float(y)] for x, y in ring]
            if coords[0] != coords[-1]:
                coords = coords + [coords[0]]
            w.poly([coords])
            rec: list = [str(oom_code)[:8]]
            if with_reason:
                rec.append(str(reasons[i] or "")[:40])
            w.record(*rec)
            n += 1
    if n <= 0 or not dest_shp.is_file():
        _clear_stem(dest_shp.parent, dest_shp.stem)
        return None
    write_prj(dest_shp)
    return dest_shp


def write_vegetation_discarded_shp(
    dest_shp: Path,
    features: list[tuple[int, str, object]],
    *,
    reason: str = "min_plocha",
) -> Path | None:
    """``features`` = (cls, code, shapely geom) zahozené při min-size/simplify."""
    if not features:
        return None
    try:
        import shapefile
    except ImportError:
        return None
    dest_shp = Path(dest_shp)
    dest_shp.parent.mkdir(parents=True, exist_ok=True)
    _clear_stem(dest_shp.parent, dest_shp.stem)
    n = 0
    with shapefile.Writer(str(dest_shp.with_suffix("")), shapeType=shapefile.POLYGON) as w:
        w.field("cls", "N", size=10)
        w.field("code", "C", size=8)
        w.field("duvod", "C", size=40)
        for cls, code, geom in features:
            try:
                if geom is None or geom.is_empty:
                    continue
                if geom.geom_type == "MultiPolygon":
                    polys = list(geom.geoms)
                elif geom.geom_type == "Polygon":
                    polys = [geom]
                else:
                    continue
                for poly in polys:
                    if poly.is_empty or float(poly.area) <= 0:
                        continue
                    exterior = [[float(x), float(y)] for x, y in poly.exterior.coords]
                    parts = [exterior]
                    for hole in poly.interiors:
                        parts.append([[float(x), float(y)] for x, y in hole.coords])
                    w.poly(parts)
                    w.record(int(cls), str(code)[:8], str(reason)[:40])
                    n += 1
            except Exception:
                continue
    if n <= 0 or not dest_shp.is_file():
        _clear_stem(dest_shp.parent, dest_shp.stem)
        return None
    write_prj(dest_shp)
    return dest_shp


def write_cliff_inspection_vectors(
    kp_cwd: Path,
    *,
    used_earth: list[list[tuple[float, float]]] | None = None,
    used_rocks: list[list[tuple[float, float]]] | None = None,
    discarded_earth: list[tuple[list[tuple[float, float]], str]] | None = None,
    discarded_rocks: list[tuple[list[tuple[float, float]], str]] | None = None,
    rock_code: str = "201.2",
    log=None,
) -> Path:
    """Zapíše srázy/skály do ``uzitecne/pouzite|vyhozene/``."""
    _root, pouzite, vyhozene = ensure_uzitecne_dirs(kp_cwd)
    used_earth = used_earth or []
    used_rocks = used_rocks or []
    discarded_earth = discarded_earth or []
    discarded_rocks = discarded_rocks or []

    write_line_shapefile(pouzite / "srazy_104.shp", used_earth)
    write_polygon_shapefile(
        pouzite / "skaly_201.shp", used_rocks, oom_code=rock_code
    )
    if discarded_earth:
        write_line_shapefile(
            vyhozene / "srazy_104.shp",
            [pts for pts, _ in discarded_earth],
            reasons=[reason for _, reason in discarded_earth],
        )
    if discarded_rocks:
        write_polygon_shapefile(
            vyhozene / "skaly_201.shp",
            [ring for ring, _ in discarded_rocks],
            oom_code=rock_code,
            reasons=[reason for _, reason in discarded_rocks],
        )
    if log:
        log(
            "uzitecne/: srázy/skály "
            f"použité {len(used_earth)}+{len(used_rocks)}, "
            f"vyhozené {len(discarded_earth)}+{len(discarded_rocks)}"
        )
    return _root


def finalize_uzitecne_vectors(kp_cwd: Path, *, log=None) -> Path:
    """Doplní vegetaci do pouzite/, README; připraví složku před ZIP/output."""
    root, pouzite, vyhozene = ensure_uzitecne_dirs(kp_cwd)
    vege = Path(kp_cwd) / "vegetation" / "vegetation.shp"
    if vege.is_file():
        _copy_shp_stem(vege, pouzite, "vegetace")
    # Vegetace vyhozené: vegetation_chm může zapsat vegetation/vyhozene_vegetace.shp
    discarded_vege = Path(kp_cwd) / "vegetation" / "vyhozene_vegetace.shp"
    if discarded_vege.is_file():
        _copy_shp_stem(discarded_vege, vyhozene, "vegetace")
    (root / "README.txt").write_text(UZITECNE_README, encoding="utf-8")
    if log:
        n_files = sum(1 for p in root.rglob("*") if p.is_file())
        log(f"uzitecne/: {n_files} souborů (vegetace/srázy/skály pouzite+vyhozene)")
    return root


def add_uzitecne_to_zip(zf, kp_cwd: Path) -> int:
    """Zapíše ``uzitecne/`` do ZIPu (rekurzivně). Vrací počet souborů."""
    src = uzitecne_root(kp_cwd)
    if not src.is_dir():
        return 0
    n = 0
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src).as_posix()
        zf.write(path, f"{UZITECNE_DIR_NAME}/{rel}")
        n += 1
    return n


def copy_uzitecne_to_output(kp_cwd: Path, output_dir: Path) -> int:
    """Zrcadlí ``uzitecne/`` do výstupního adresáře jobu."""
    src = uzitecne_root(kp_cwd)
    if not src.is_dir():
        return 0
    dest = Path(output_dir) / UZITECNE_DIR_NAME
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest)
    return sum(1 for p in dest.rglob("*") if p.is_file())


def dropped_by_identity(
    before: list,
    after: list,
) -> list:
    """Vrátí prvky z ``before``, které nejsou v ``after`` (porovnání id)."""
    kept = {id(x) for x in after}
    return [x for x in before if id(x) not in kept]
