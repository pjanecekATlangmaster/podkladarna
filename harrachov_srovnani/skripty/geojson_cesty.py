"""GeoJSON cest z hotových .omap (EPSG:5514): symbol, zdroj (OSM / ZABAGED / DTM podle prostorového překryvu
s podklady pipeline) a OSM tagy nejlépe překryté OSM cesty (Overpass dump ref/osm_overpass.json).
Výstup geojson/cesty_<mapa>.geojson (jen uvnitř obrysu reference + 50 m) a geojson/osm_cesty.geojson."""
import json, sys, zipfile, tempfile
from pathlib import Path
sys.path.insert(0, r"C:\Users\PetrJanecek\source\repos\podkladarna-harrachov")
sys.path.insert(0, str(Path(__file__).parent))
from app.tool_env import apply_local_gis_env; apply_local_gis_env()
from pyproj import Transformer
from shapely.geometry import LineString, Polygon, shape, mapping
from shapely.ops import unary_union
from shapely.strtree import STRtree
import pyogrio.raw
import shapely
from omaplib import load_objects

B = Path(r"C:\Users\PetrJanecek\source\repos\podkladarna\local_test\harrachov_srovnani")
WORK = Path(r"C:\Users\PetrJanecek\source\repos\podkladarna\local_test\harrachov_work")
OUT = B / "geojson"; OUT.mkdir(exist_ok=True)
TAGS = ("highway", "surface", "tracktype", "trail_visibility", "width", "smoothness", "source", "sac_scale", "mtb:scale",
        "footway", "sidewalk", "bicycle", "foot", "access", "name", "bridge", "tunnel", "layer", "piste:type")
CRS = {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::5514"}}
obrys = shape(json.loads((B / "ref" / "obrys.geojson").read_text(encoding="utf-8"))["features"][0]["geometry"])
zona = obrys.buffer(50)
tr = Transformer.from_crs(4326, 5514, always_xy=True).transform


def dump(name, feats):
    (OUT / name).write_text(json.dumps({"type": "FeatureCollection", "crs": CRS, "features": feats}, ensure_ascii=False),
                            encoding="utf-8")
    print(name, len(feats), flush=True)


# --- OSM cesty (všechny tagy) ---
osm_ways = []
for el in json.loads((B / "ref" / "osm_overpass.json").read_text(encoding="utf-8"))["elements"]:
    t = el.get("tags", {})
    if "highway" not in t:
        continue
    g = [n for n in (el.get("geometry") or []) if n]
    if len(g) < 2:
        continue
    ls = LineString([tr(n["lon"], n["lat"]) for n in g])
    if ls.intersects(zona):
        osm_ways.append((el["id"], t, ls))
dump("osm_cesty.geojson", [{"type": "Feature", "properties": {"osm_id": i, "delka_m": round(ls.length, 1), **t},
                            "geometry": mapping(ls)} for i, t, ls in osm_ways])
osm_tree = STRtree([ls for _, _, ls in osm_ways])

# --- ZABAGED cesty ---
zab = []
zc = WORK / "zabaged_clean.zip"
if zc.exists():
    with tempfile.TemporaryDirectory() as td:
        zipfile.ZipFile(zc).extractall(td)
        for shp in Path(td).rglob("*.shp"):
            if shp.stem in ("Cesta", "Pesina", "Ulice", "SilniceDalnice", "Lavka", "Most"):
                try:
                    _meta, _fids, wkb, _fields = pyogrio.raw.read(shp)
                except Exception as e:
                    print("ZABAGED", shp.stem, e); continue
                for g in shapely.from_wkb(wkb):
                    if g is not None and not g.is_empty and g.intersects(zona):
                        zab.append((shp.stem, g))
print("ZABAGED čar", len(zab))
zab_tree = STRtree([g for _, g in zab])

# --- DTM plochy silnic / chodníků / zpevněné ---
dtm = []
for n in ("roads", "sidewalks", "small_paved", "parking", "stairs"):
    p = WORK / "dtm" / f"{n}.json"
    if not p.exists():
        continue
    for rings in json.loads(p.read_text(encoding="utf-8")).get("polygons", []):
        try:
            g = Polygon(rings[0], rings[1:]).buffer(0)
        except Exception:
            continue
        if not g.is_empty and g.intersects(zona):
            dtm.append((n, g))
print("DTM ploch", len(dtm))
dtm_tree = STRtree([g for _, g in dtm]) if dtm else None


def frac_near(geom, tree, items, d):
    """Podíl délky geom do d m od prvků; vrátí (podíl, index nejlépe překrytého prvku)."""
    if tree is None or geom.length == 0:
        return 0.0, None
    idx = tree.query(geom.buffer(d))
    if len(idx) == 0:
        return 0.0, None
    best, bi = 0.0, None
    cover = []
    for i in idx:
        z = items[i][-1].buffer(d)
        part = geom.intersection(z).length
        cover.append(z)
        if part > best:
            best, bi = part, int(i)
    return geom.intersection(unary_union(cover)).length / geom.length, bi


PATH_CODES = ("501", "502", "503", "504", "505", "506", "507", "508", "509", "529", "532", "8")
for omap in sorted((B / "mapy").glob("Harrachov-*.omap")):
    feats = []
    for o in load_objects(omap):
        code = o["code"]
        if not code.startswith(PATH_CODES) or o["kind"] == "point":
            continue
        g = o["geom"]
        if not g.intersects(zona):
            continue
        if o["kind"] == "area":
            ln = g.boundary
            pr = {"symbol": code, "nazev_symbolu": o["name"], "typ": "plocha", "plocha_m2": round(g.area, 1)}
        else:
            ln = g
            pr = {"symbol": code, "nazev_symbolu": o["name"], "typ": "čára", "delka_m": round(g.length, 1)}
        f_osm, bi = frac_near(ln, osm_tree, osm_ways, 3.0)
        f_zab, bz = frac_near(ln, zab_tree, zab, 3.0)
        f_dtm, bd = frac_near(ln, dtm_tree, dtm, 1.0) if dtm_tree is not None else (0.0, None)
        if o["kind"] == "area" and dtm_tree is not None:
            idx = dtm_tree.query(g)
            inter = sum(g.intersection(dtm[i][1]).area for i in idx)
            f_dtm = max(f_dtm, inter / g.area if g.area else 0)
        fr = {"OSM": f_osm, "ZABAGED": f_zab, "DTM": f_dtm}
        src = max(fr, key=fr.get)
        pr.update({"zdroj": src if fr[src] >= 0.5 else "nejasný", "podil_osm": round(f_osm, 2),
                   "podil_zabaged": round(f_zab, 2), "podil_dtm": round(f_dtm, 2)})
        if bi is not None and f_osm >= 0.3:
            oid, t, _ = osm_ways[bi]
            pr["osm_id"] = oid
            for k in TAGS:
                if k in t:
                    pr[f"osm_{k}"] = t[k]
        if bz is not None and f_zab >= 0.3:
            pr["zabaged_vrstva"] = zab[bz][0]
        if bd is not None and f_dtm >= 0.3:
            pr["dtm_vrstva"] = dtm[bd][0]
        feats.append({"type": "Feature", "properties": pr, "geometry": mapping(g)})
    dump(f"cesty_{omap.stem.split('-', 1)[1].replace('-', '_')}.geojson", feats)
