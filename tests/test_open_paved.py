"""Sprint mimo Česko: zpevněná 401 z ortofota země a letního NDVI (``app.pipeline.open_paved``)."""

import json
from datetime import date

import numpy as np
import pytest
from shapely.geometry import Polygon

from app.pipeline import open_paved

GREY = (128, 128, 128)
GRASS = (60, 140, 50)


def _scene(paved_box, ndvi_paved: float, size: int = 80):
    """Mřížka 0,5 m: tráva (NDVI 0,7), uvnitř šedý obdélník ``paved_box`` (řádky, sloupce) s ``ndvi_paved``."""
    rgb = np.empty((size, size, 3), dtype=np.uint8)
    rgb[:] = GRASS
    ndvi = np.full((size, size), 0.7, dtype=np.float32)
    r0, r1, c0, c1 = paved_box
    rgb[r0:r1, c0:c1] = GREY
    ndvi[r0:r1, c0:c1] = ndvi_paved
    return rgb, ndvi


@pytest.mark.parametrize(
    ("paved_box", "ndvi_paved", "in_401", "osm", "want_min_m2", "want_max_m2"),
    [
        ((0, 80, 0, 40), 0.1, True, None, 500.0, 850.0),  # náměstí v 401: zpevněné, NDVI potvrdí
        ((0, 80, 0, 40), 0.1, True, "rail", 300.0, 600.0),  # pás trati (štěrkové lože) zůstane 401
        ((0, 80, 0, 40), 0.1, True, "beach", 0.0, 0.0),  # OSM pláž / písek: světlý písek není dlažba (Scheveningen)
        ((0, 80, 0, 40), 0.1, True, "scrub", 0.0, 0.0),  # OSM přírodní kryt: suchá tráva dun není dlažba
        ((0, 80, 0, 40), 0.6, True, None, 0.0, 0.0),  # zimní ortofoto: hnědá tráva vypadá šedě, léto ji ukáže zelenou
        ((0, 80, 0, 40), 0.1, False, None, 0.0, 0.0),  # mimo 401 se nic nemění
        ((28, 52, 28, 52), 0.25, True, None, 0.0, 0.0),  # malý kus (pod 200 m²): NDVI ≤ 0,2, 10m pixel míchá okolí
        ((28, 52, 28, 52), 0.15, True, None, 25.0, 200.0),
        ((0, 0, 0, 0), 0.1, True, None, 0.0, 0.0),  # samá tráva
    ],
)
def test_open_paved_mask(tmp_path, paved_box, ndvi_paved, in_401, osm, want_min_m2, want_max_m2):
    from app.pipeline.osm_paths import NATURAL_COVER_NAME, SAND_AREAS_NAME

    rgb, ndvi = _scene(paved_box, ndvi_paved)
    open401 = np.full(ndvi.shape, in_401)
    (tmp_path / "osm_paths").mkdir()
    rail = {"type": "Feature", "properties": {"kind": "rail_line"},
            "geometry": {"type": "LineString", "coordinates": [[10.0, -5.0], [10.0, 45.0]]}}
    beach = {"type": "Feature", "properties": {"source": "osm"},
             "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [25, 0], [25, 40], [0, 40], [0, 0]]]}}
    (tmp_path / "osm_paths" / "features.geojson").write_text(
        json.dumps({"features": [rail] if osm == "rail" else []}), encoding="utf-8")
    (tmp_path / "osm_paths" / SAND_AREAS_NAME).write_text(
        json.dumps({"features": [beach] if osm == "beach" else []}), encoding="utf-8")
    scrub = dict(beach, properties={"source": "osm", "natural_cover": True, "cover": "scrub"})
    (tmp_path / "osm_paths" / NATURAL_COVER_NAME).write_text(
        json.dumps({"features": [scrub] if osm == "scrub" else []}), encoding="utf-8")
    exclude = open_paved.exclude_zone(tmp_path)
    gt = (0.0, 0.5, 0.0, 40.0, 0.0, -0.5)
    if exclude is not None:  # jako ``_prepare``: vyloučené pixely nejsou kandidáty (tady bez GDAL)
        import shapely

        xs, ys = np.meshgrid(np.arange(80) * 0.5 + 0.25, 40.0 - (np.arange(80) * 0.5 + 0.25))
        open401 &= ~shapely.contains_xy(exclude, xs, ys)
    mask = open_paved.paved_mask(rgb, open401, ndvi, 0.5)
    open_wkbs = [Polygon([(0, 0), (40, 0), (40, 40), (0, 40)]).wkb] if in_401 else []
    polys = open_paved.fit_to_open(open_paved._mask_to_polygons(mask, gt), open_wkbs, exclude)
    area = sum(p.area for p in polys)
    assert want_min_m2 <= area <= want_max_m2


def _area_part(name, code, poly, preset, scale):
    from app.pipeline.oom_import import OomObjectPart, _geom_parts_to_objects, _wkb_parts
    from app.pipeline.oom_symbol_map import symbol_index_for_code

    sym = symbol_index_for_code(preset, scale, code)
    objs = _geom_parts_to_objects(_wkb_parts(poly.wkb)[0], sym, ref_x=0.0, ref_y=0.0, scale=scale, grivation_deg=0.0,
                                  as_area=True)
    return OomObjectPart(name=name, objects_xml="\n".join(objs), count=len(objs))


def _drawn_m2(part, scale):
    from app.pipeline.path_clearance import _COORDS_RE, _OBJECT_RE, _parse_coords, _polygon, _rings

    um2 = sum(_polygon(_rings(_parse_coords(_COORDS_RE.search(m.group(4)).group(1)))).area
              for m in _OBJECT_RE.finditer(part.objects_xml))
    return um2 / (1e6 / scale) ** 2


@pytest.mark.parametrize(
    ("cz", "preset", "strip_x", "want_501_m2", "cover", "want_drawn_m2"),
    [
        (False, "sprint_2m", 10.0, 315.0, None, 315.0),  # mimo Česko: pruh 10 × 30 m (+ 0,5 m na mřížku 401) je 501.1 nad 401
        (False, "sprint_2m", 28.0, 855.0, None, 855.0),
        (False, "sprint_2m", 29.0, 900.0, None, 900.0),  # zbytek 401 menší než kus CHM (15 m²) připadne k ploše
        # kresbu jiné části (pruh 5 × 30 m) 501.1 nepřekryje: oliva, zelená, jiná 501; žlutou ano, 501 na mostě také
        (False, "sprint_2m", 10.0, 315.0, ("Vegetace", "520"), 165.0),
        (False, "sprint_2m", 10.0, 315.0, ("Vegetace", "406"), 165.0),
        (False, "sprint_2m", 10.0, 315.0, ("Mezery", "501.1"), 165.0),
        (False, "sprint_2m", 10.0, 315.0, ("Vegetace", "401"), 315.0),
        (False, "sprint_2m", 10.0, 315.0, ("Mosty – 501", "501.1"), 315.0),
        (True, "sprint_2m", 10.0, None, None, None),  # Česko beze změny
        (False, "forest_10000", 10.0, None, None, None),  # les a MTBO beze změny
    ],
)
def test_open_paved_draws_501_over_401(tmp_path, monkeypatch, cz, preset, strip_x, want_501_m2, cover, want_drawn_m2):
    from app.pipeline import country_profile

    monkeypatch.setattr(country_profile, "process_uses_cz_sources", lambda: cz)
    row401 = Polygon([(0, 0), (30, 0), (30, 30), (0, 30)])
    strip = Polygon([(0, 0), (strip_x, 0), (strip_x, 30), (0, 30)])
    open_paved._write_result(tmp_path / open_paved.CACHE_SUBDIR, open_paved.fit_to_open([strip], [row401.wkb]),
                             {"stamp": []})
    scale = 4000 if preset.startswith("sprint") else 10000
    polys = open_paved.load_polygons(tmp_path) if open_paved.applies(preset) else []
    part = open_paved.build_open_paved_part(tmp_path, polys, preset_id=preset, scale=scale, ref_x=0.0, ref_y=0.0,
                                            grivation_deg=0.0)
    if want_501_m2 is None:
        assert part is None
        return
    assert part is not None and part.count == 1
    assert sum(p.area for p in polys) == pytest.approx(want_501_m2, abs=1.0)
    parts = [part]
    if cover:
        parts.append(_area_part(cover[0], cover[1], Polygon([(0, 0), (5, 0), (5, 30), (0, 30)]), preset, scale))
    parts = open_paved.trim_to_drawn(parts, preset_id=preset, scale=scale)
    drawn = [p for p in parts if p.name == open_paved.PART_NAME]
    assert len(drawn) == 1 and _drawn_m2(drawn[0], scale) == pytest.approx(want_drawn_m2, abs=1.0)


def test_open_paved_prepare_failure_clears_old_result(tmp_path, monkeypatch):
    """Selhání výpočtu nesmí nechat starší výsledek (nakreslil by se k jiným vstupům)."""
    from app.pipeline import country_profile
    from app.pipeline.vegetation_chm import CHM_SOURCE_MARK

    monkeypatch.setattr(country_profile, "process_uses_cz_sources", lambda: False)
    (tmp_path / "vegetation").mkdir()
    (tmp_path / "vegetation" / "vegetation.shp").write_bytes(b"placeholder")
    (tmp_path / "vegetation" / CHM_SOURCE_MARK).write_text("chm\n", encoding="utf-8")
    open_paved._write_result(tmp_path / open_paved.CACHE_SUBDIR, [Polygon([(0, 0), (9, 0), (9, 9)])], {"stamp": []})

    def boom(*_a, **_k):
        raise RuntimeError("WMS mimo provoz")

    monkeypatch.setattr(open_paved, "_prepare", boom)
    logs = []
    assert open_paved.prepare(tmp_path, (13.4, 52.5, 13.41, 52.51), preset_id="sprint_2m", log=logs.append) == []
    assert open_paved.load_polygons(tmp_path) == []
    assert any("přeskočeno" in line for line in logs)


def test_open_paved_summer_ndvi_scene_choice(tmp_path):
    """Nejnovější letní snímek s jasnou maskou SCL; bez něj nic (a výsledek se pamatuje, nehledá se znovu)."""
    pytest.importorskip("osgeo")
    from osgeo import gdal

    from app.pipeline.sentinel2 import Scene

    def scene(day, scl):
        return Scene(item_id=f"S2_{day}", day=day, tile_cloud=5.0,
                     assets={"scl": scl, "red": "red", "nir": "nir", "visual": "v", "green": "g"}, dn_offset=1000.0)

    calls = []

    def search(_bbox, *, today, days):
        calls.append((today, days))
        if today.year == 2026:
            return [scene(date(2026, 8, 20), "cloudy"), scene(date(2026, 7, 3), "clear")]
        return []

    def read_band(href, grid, mpp, _resampling):
        w = max(1, round((grid[2] - grid[0]) / mpp))
        h = max(1, round((grid[3] - grid[1]) / mpp))
        value = {"cloudy": 9, "clear": 4, "red": 300, "nir": 2700}[href]  # SCL 9 mrak, 4 vegetace
        return np.full((h, w), value, dtype=np.uint16), (grid[0], mpp, 0.0, grid[3], 0.0, -mpp)

    bounds = (0.0, 0.0, 100.0, 50.0)
    got = open_paved.fetch_summer_ndvi(tmp_path, (13.4, 52.5, 13.41, 52.51), bounds, today=date(2026, 11, 16),
                                       search=search, read_band=read_band)
    assert got is not None
    meta = json.loads((tmp_path / open_paved.CACHE_SUBDIR / open_paved.NDVI_META_NAME).read_text(encoding="utf-8"))
    assert meta["day"] == "2026-07-03"
    ndvi = gdal.Open(str(got)).ReadAsArray()
    assert ndvi.shape == (5, 10) and ndvi.mean() == pytest.approx(0.8, abs=1e-3)  # bez posunu DN
    assert calls[0] == (date(2026, 9, 10), 101)

    def search_none(_bbox, *, today, days):
        calls.append((today, days))
        return []

    empty = tmp_path / "zima"
    calls.clear()
    for _ in range(2):
        assert open_paved.fetch_summer_ndvi(empty, (13.4, 52.5, 13.41, 52.51), bounds, today=date(2026, 11, 16),
                                            search=search_none, read_band=read_band) is None
    assert len(calls) == 2  # letos a loni, podruhé z paměti jobu


def test_open_paved_ortho_source_per_country():
    bounds = (0.0, 0.0, 100.0, 100.0)
    de = open_paved.ortho_source_xml("DE", bounds)
    assert de and "truedop" in de and "<Service name=\"WMS\">" in de
    at = open_paved.ortho_source_xml("AT", bounds)
    assert at and "<Service name=\"TMS\">" in at
    assert open_paved.ortho_source_xml("CZ", bounds) is None
    assert open_paved.ortho_source_xml("SK", bounds) is None
