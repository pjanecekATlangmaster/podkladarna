"""Škálování veta porostu z CHM (mimo Česko): syntetické město, vnořené výřezy.

Spuštění z kořene repozitáře: PYTHONPATH=. python scripts/veg_veto_scaling.py 0.5 1 2
"""
import json, math, random, sys, tempfile, time
from pathlib import Path
from shapely.geometry import Polygon, box
import app.pipeline.vegetation_gdal as vg
from app.pipeline import country_profile
from app.pipeline.vegetation_chm import CHM_SOURCE_MARK
country_profile.process_uses_cz_sources = lambda: False

def city(km2, seed=1):
    side = math.sqrt(km2) * 1000
    rnd = random.Random(seed)
    d = Path(tempfile.mkdtemp()); (d / "osm_paths").mkdir(); (d / "vegetation").mkdir()
    (d / "vegetation" / CHM_SOURCE_MARK).write_text("chm")
    feats = []
    for _ in range(int(1500 * km2)):
        x, y = rnd.uniform(0, side), rnd.uniform(0, side); w = rnd.uniform(6, 20)
        feats.append({"properties": {"kind": "building"}, "geometry": {"type": "Polygon", "coordinates": [[[x, y], [x + w, y], [x + w, y + w], [x, y + w], [x, y]]]}})
    feats.append({"properties": {"kind": "tram_line"}, "geometry": {"type": "LineString", "coordinates": [[0, side / 2], [side, side / 2 + 7]]}})
    (d / "osm_paths" / "features.geojson").write_text(json.dumps({"features": feats}))
    paths = []
    for i in range(int(side // 80) + 1):
        paths.append({"properties": {"highway": "road_3"}, "geometry": {"type": "LineString", "coordinates": [[i * 80, 0], [i * 80, side]]}})
        paths.append({"properties": {"highway": "road_3"}, "geometry": {"type": "LineString", "coordinates": [[0, i * 80], [side, i * 80]]}})
    (d / "osm_paths" / "paths_osm.geojson").write_text(json.dumps({"features": paths}))
    rows = []
    for _ in range(int(3000 * km2)):
        x, y = rnd.uniform(0, side), rnd.uniform(0, side); r = rnd.uniform(3, 15)
        rows.append(Polygon([(x + r * math.cos(a / 8 * 6.283), y + r * math.sin(a / 8 * 6.283)) for a in range(8)]).wkb)
    return d, rows

res = []
for km2 in [float(a) for a in sys.argv[1:]]:
    d, rows = city(km2)
    t0 = time.perf_counter()
    mask = vg._raster_veto_mask(d, d / "vegetation" / "vegetation.shp", True)
    t1 = time.perf_counter()
    out = [vg._clip_wkb(w, mask) for w in rows]
    t2 = time.perf_counter()
    res.append((km2, t1 - t0, t2 - t1))
    print(f"{km2:5.1f} km²: maska {t1-t0:6.2f} s, ořez {len(rows)} řádků {t2-t1:6.2f} s, celkem {t2-t0:6.2f} s", flush=True)
for (a, *ta), (b, *tb) in zip(res, res[1:]):
    print(f"exponent {a}→{b} km²: {math.log(sum(tb)/sum(ta))/math.log(b/a):.2f}")
