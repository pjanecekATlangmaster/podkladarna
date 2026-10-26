from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipeline.oom_coords import projected_to_map_coord
from app.pipeline.oom_import import feature_props
from app.pipeline.oom_symbol_map import oom_code_for_vectorconf_rule, symbol_index_for_code
from app.pipeline.oom_vectorconf import load_vectorconf, match_feature


def test_feature_props_uses_getfield_not_items():
    class _Defn:
        def __init__(self, name: str) -> None:
            self._name = name

        def GetName(self) -> str:
            return self._name

    class _Feature:
        def __init__(self, fields: list[tuple[str, object]]) -> None:
            self._fields = fields

        def GetFieldCount(self) -> int:
            return len(self._fields)

        def GetFieldDefnRef(self, i: int) -> _Defn:
            return _Defn(self._fields[i][0])

        def GetField(self, i: int) -> object:
            return self._fields[i][1]

    feat = _Feature([("druhbud", "ano"), ("typulice_p", "ulice sjízdná v sídle")])
    props = feature_props(feat, layer_name="Cesta")
    assert props["druhbud"] == "ano"
    assert props["vrstva"] == "Cesta"


def test_projected_to_map_coord_at_ref():
    x, y = projected_to_map_coord(
        100.0,
        200.0,
        ref_x=100.0,
        ref_y=200.0,
        scale=4000,
        grivation_deg=13.0,
    )
    assert x == 0
    assert y == 0


def test_projected_to_map_coord_east_with_grivation():
    # 1 m východně, grivation 0 → 250 nativních jednotek na ose X
    x0, y0 = projected_to_map_coord(
        1.0, 0.0, ref_x=0.0, ref_y=0.0, scale=4000, grivation_deg=0.0
    )
    assert x0 == 250
    assert y0 == 0
    # Se grivací se osa mapy natočí – Y už není 0
    x1, y1 = projected_to_map_coord(
        1.0, 0.0, ref_x=0.0, ref_y=0.0, scale=4000, grivation_deg=13.0
    )
    assert x1 != 250 or y1 != 0
    assert abs(x1) > 200


def test_vectorconf_match_building():
    rules = load_vectorconf("zabaged.txt")
    rule = match_feature({"druhbud": "ano"}, rules)
    assert rule is not None
    assert rule.symbol_name == "building"
    assert rule.kp_code == "526"


def test_oom_code_building_maps_to_521():
    code = oom_code_for_vectorconf_rule(
        "building",
        "526",
        "Budova",
        preset_id="sprint_2m",
        scale=4000,
    )
    assert code == "521"
    # Kód generátoru 521 → OCAD 521.0 v ISSprOM_2019_4000_cz.omap (id = pořadí v sadě).
    from app.pipeline.oom_symbol_map import _code_to_index
    from app.pipeline.oom_symbols import symbol_set_path

    expected = _code_to_index(symbol_set_path("sprint_2m", 4000))["521.0"]
    assert symbol_index_for_code("sprint_2m", 4000, code) == expected


def test_oom_code_building_mtbo_maps_to_526():
    """ISMTBOM budova je 526 (Black 70 %), ne ISOM 521."""
    code = oom_code_for_vectorconf_rule(
        "building",
        "526",
        "Budova",
        preset_id="mtbo_10000",
        scale=10000,
    )
    assert code == "526"
    assert symbol_index_for_code("mtbo_10000", 10000, code) is not None
    # 521 v ISMTBOM je zeď – nesmí se použít pro budovu.
    assert code != "521"


def test_oom_code_road_mtbo_maps_to_502():
    code = oom_code_for_vectorconf_rule(
        "road-path",
        "503",
        "SilniceDalnice",
        preset_id="mtbo_10000",
        scale=10000,
    )
    assert code == "502"
    assert symbol_index_for_code("mtbo_10000", 10000, code) is not None


def test_oom_code_path_mtbo_uses_riding_series():
    track = oom_code_for_vectorconf_rule(
        "road-path",
        "505",
        "Cesta",
        preset_id="mtbo_10000",
        scale=10000,
    )
    path = oom_code_for_vectorconf_rule(
        "road-path",
        "506",
        "Pesina",
        preset_id="mtbo_10000",
        scale=10000,
    )
    assert track == "833"
    assert path == "834"
    assert symbol_index_for_code("mtbo_10000", 10000, track) is not None
    assert symbol_index_for_code("mtbo_10000", 10000, path) is not None


def test_oom_code_railway_mtbo_maps_to_515():
    code = oom_code_for_vectorconf_rule(
        "railway",
        "515",
        "ZeleznicniTrat",
        preset_id="mtbo_10000",
        scale=10000,
    )
    assert code == "515"
    assert symbol_index_for_code("mtbo_10000", 10000, code) is not None


def test_oom_code_railway_maps_to_509_1():
    code = oom_code_for_vectorconf_rule(
        "railway",
        "515",
        "ZeleznicniTrat",
        preset_id="sprint_2m",
        scale=4000,
    )
    assert code == "509.1"
    assert symbol_index_for_code("sprint_2m", 4000, code) is not None


def test_oom_code_railway_forest_maps_to_509():
    code = oom_code_for_vectorconf_rule(
        "railway",
        "515",
        "ZeleznicniTrat",
        preset_id="forest_10000",
        scale=10000,
    )
    assert code == "509"
    assert symbol_index_for_code("forest_10000", 10000, code) is not None


def test_oom_code_road_forest_maps_to_503():
    code = oom_code_for_vectorconf_rule(
        "road-path",
        "503",
        "SilniceDalnice",
        preset_id="forest_10000",
        scale=10000,
    )
    assert code == "503"
    assert symbol_index_for_code("forest_10000", 10000, code) is not None


def test_oom_code_ulice_sprint_maps_to_issprom_footprint():
    """Sjízdná ulice ve sprintu = ISSprOM 501.31 (heavy), ne hrana 501.1."""
    code = oom_code_for_vectorconf_rule(
        "road-path",
        "503",
        "Ulice",
        preset_id="sprint_2m",
        scale=4000,
    )
    assert code == "501.31"
    assert symbol_index_for_code("sprint_2m", 4000, code) is not None
    assert code != "501.1"


def test_oom_code_skupina_balvanu_maps_to_207():
    code = oom_code_for_vectorconf_rule(
        "blackline",
        "414",
        "SkupinaBalvanu",
        preset_id="sprint_2m",
        scale=4000,
    )
    assert code == "207"
    assert symbol_index_for_code("sprint_2m", 4000, code) is not None


def test_oom_code_parking_forest_maps_to_501_under_roads():
    """Zpevněná plocha musí mít nižší CRT index než silnice (503 @ ~117)."""
    for vrstva in ("ParkovisteOdpocivka", "OstatniPlochaVSidlech"):
        code = oom_code_for_vectorconf_rule(
            "parking",
            "529",
            vrstva,
            preset_id="forest_10000",
            scale=10000,
        )
        assert code == "501", (vrstva, code)
        idx = symbol_index_for_code("forest_10000", 10000, code)
        road_idx = symbol_index_for_code("forest_10000", 10000, "503")
        assert idx is not None and road_idx is not None
        assert idx < road_idx, (idx, road_idx)


def test_ostatni_as_403_override_available():
    """Volba ostatni_plocha_as_403: značka 403 je ve všech sadách."""
    for preset_id, scale in (
        ("sprint_2m", 4000),
        ("forest_10000", 10000),
        ("mtbo_10000", 10000),
    ):
        idx = symbol_index_for_code(preset_id, scale, "403")
        assert idx is not None, (preset_id, scale)


def test_oom_code_parking_mtbo_maps_to_501_0_under_roads():
    code = oom_code_for_vectorconf_rule(
        "parking",
        "529",
        "OstatniPlochaVSidlech",
        preset_id="mtbo_10000",
        scale=10000,
    )
    assert code == "501.0"
    idx = symbol_index_for_code("mtbo_10000", 10000, code)
    road_idx = symbol_index_for_code("mtbo_10000", 10000, "503")
    assert idx is not None and road_idx is not None
    # OCAD sada ISMTBOM 2022: pořadí v seznamu o překrytí nerozhoduje, plocha má
    # nižší barevnou prioritu (paved area 26) než ulice (street infill 18).


def test_oom_code_orchard_garden_maps_to_olive_520():
    """ZABAGED OvocnySadZahrada → oliva (KP 527 / OOM 520), ne tečkovaný sad 413."""
    for symbol_name, kp in (("farm", "413"), ("settlement", "527")):
        code = oom_code_for_vectorconf_rule(
            symbol_name,
            kp,
            "OvocnySadZahrada",
            preset_id="sprint_2m",
            scale=4000,
        )
        assert code == "520", (symbol_name, kp, code)
    assert (
        oom_code_for_vectorconf_rule(
            "settlement",
            "527",
            "OvocnySadZahrada",
            preset_id="forest_10000",
            scale=10000,
        )
        == "520"
    )
    assert symbol_index_for_code("sprint_2m", 4000, "520") is not None


def test_oom_code_dxf_cliffs_small_preset_specific():
    from app.pipeline.oom_symbol_map import oom_code_for_dxf

    assert oom_code_for_dxf("cliffs_small.dxf", preset_id="sprint_2m") == "104"
    assert oom_code_for_dxf("cliffs_small.dxf", preset_id="forest_10000") == "104"
    assert oom_code_for_dxf("cliffs_large.dxf", preset_id="forest_10000") == "104"
    assert (
        oom_code_for_dxf(
            "cliffs_small.dxf", preset_id="sprint_2m", cliff_symbol="symbol_206"
        )
        == "206"
    )
    assert (
        oom_code_for_dxf(
            "cliffs_small.dxf", preset_id="sprint_2m", cliff_symbol="off"
        )
        is None
    )
    assert (
        oom_code_for_dxf(
            "dotknolls.dxf", preset_id="sprint_2m", cliff_symbol="earth_bank"
        )
        == "109"
    )
    assert (
        oom_code_for_dxf(
            "cliffs_rock.dxf", preset_id="forest_10000", cliff_symbol="auto"
        )
        == "201"
    )
    assert (
        oom_code_for_dxf(
            "cliffs_small.dxf", preset_id="forest_10000", cliff_symbol="auto"
        )
        == "104"
    )
    assert (
        oom_code_for_dxf(
            "cliffs_rock.dxf", preset_id="forest_10000", cliff_symbol="earth_bank"
        )
        == "104"
    )
    # Legacy rock_face already removed from choices – treated as auto (no force).
    assert (
        oom_code_for_dxf(
            "cliffs_small.dxf", preset_id="sprint_2m", cliff_symbol="rock_face"
        )
        == "104"
    )


def test_orient_polyline_tags_downhill_flips_when_needed():
    from app.pipeline.oom_import import orient_polyline_tags_downhill

    pts = [(0.0, 0.0), (10.0, 0.0)]

    def to_map(x, y):
        return (int(x * 100), int(y * 100))

    def elev_lower_on_plus_y(x, y):
        # Vlevo od 0→10 (+Y) je níž → bez otočení (tagy vlevo).
        return -y

    assert (
        orient_polyline_tags_downhill(pts, elev_at=elev_lower_on_plus_y, to_map=to_map)
        == pts
    )

    def elev_lower_on_minus_y(x, y):
        return y

    assert orient_polyline_tags_downhill(
        pts, elev_at=elev_lower_on_minus_y, to_map=to_map
    ) == list(reversed(pts))


def test_path_object_dash_point_flag():
    from app.pipeline.oom_import import MAP_COORD_DASH_POINT, _path_object

    xml = _path_object(7, [(0, 0), (1000, 0, MAP_COORD_DASH_POINT), (2000, 0)])
    assert 'symbol="7"' in xml
    assert "0 0;1000 0 32;2000 0;" in xml


def test_convert_polyline_to_curves_open_xml():
    """Mapper CurveStart=1; 3 body → 2 Bézier → 7 coords s handly."""
    from app.pipeline.oom_import import (
        MAP_COORD_CURVE_START,
        _path_object,
        convert_polyline_to_curves,
    )

    # Lomená čára: (0,0) → (1000,0) → (1000,1000)
    curved = convert_polyline_to_curves([(0, 0), (1000, 0), (1000, 1000)])
    assert len(curved) == 7  # 3N-2 pro N=3
    assert curved[0][0] == 0 and curved[0][1] == 0
    assert curved[0][2] == MAP_COORD_CURVE_START
    # Druhý vrchol (původní index 1) je po handlu startu + incoming → CurveStart
    assert any(
        len(c) == 3 and c[0] == 1000 and c[1] == 0 and c[2] == MAP_COORD_CURVE_START
        for c in curved
    )
    xml = _path_object(3, curved)
    assert 'coords count="7"' in xml
    assert "0 0 1;" in xml  # CurveStart na prvním bodě
    # Handly nemají flag (jen x y)
    assert "390 0;" in xml or "391 0;" in xml  # ~0.39 * 1000


def test_convert_polyline_to_curves_closed_flag():
    from app.pipeline.oom_import import (
        MAP_COORD_CLOSE_POINT,
        MAP_COORD_HOLE_POINT,
        convert_polyline_to_curves,
    )

    ring = [(0, 0), (1000, 0), (1000, 1000), (0, 1000)]
    curved = convert_polyline_to_curves(ring, closed=True)
    assert curved[0][2] == 1  # CurveStart
    last = curved[-1]
    assert last[0] == 0 and last[1] == 0
    assert last[2] == MAP_COORD_CLOSE_POINT | MAP_COORD_HOLE_POINT


def test_geom_parts_as_curves_only_when_requested():
    from app.pipeline.oom_import import _geom_parts_to_objects

    parts = [("line", [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)], False)]
    plain = _geom_parts_to_objects(
        parts,
        1,
        ref_x=0,
        ref_y=0,
        scale=10000,
        grivation_deg=0,
    )
    curved = _geom_parts_to_objects(
        parts,
        1,
        ref_x=0,
        ref_y=0,
        scale=10000,
        grivation_deg=0,
        as_curves=True,
    )
    assert " 1;" not in plain[0]  # žádný CurveStart flag
    assert " 1;" in curved[0]
    assert 'coords count="3"' in plain[0]
    assert 'coords count="7"' in curved[0]
def test_load_cliff_dem_prefers_shared_dem_over_contour_smooth(tmp_path, monkeypatch):
    """contours/ má jen dem_smooth; dense-contour potřebuje dem/dem_filled."""
    from app.pipeline import oom_import as oi

    dem_dir = tmp_path / "dem"
    cont = tmp_path / "contours"
    dem_dir.mkdir()
    cont.mkdir()
    filled = dem_dir / "dem_filled.tif"
    smooth = cont / "dem_smooth.tif"
    filled.write_bytes(b"filled")
    smooth.write_bytes(b"smooth")

    opened: list[object] = []

    class _FakeElev:
        def __init__(self, path):
            opened.append(path)
            self.path = path

        def __call__(self, x: float, y: float):
            return 0.0

    monkeypatch.setattr(oi, "_DemElev", _FakeElev)
    oi._dem_cache.clear()
    oi._dem_cache_dir = None
    elev = oi._load_cliff_dem(tmp_path)
    assert elev is not None
    assert elev.path == filled
    assert opened[0] == filled
    # Smooth v contours/ se nemá preferovat před nehlazeným dem/.
    assert smooth not in opened

def _water_zip(tmp_path):
    """ZABAGED zip: jezero 100 × 50 m + tři toky (uvnitř / z poloviny / mimo)."""
    import zipfile

    import numpy as np
    import pyogrio.raw as pr
    from shapely.geometry import LineString, box

    def write(name, geoms, gtype):
        arr = np.array([g.wkb for g in geoms], dtype=object)
        pr.write(
            str(tmp_path / f"{name}.shp"),
            geometry=arr,
            field_data=[],
            fields=[],
            geometry_type=gtype,
            crs="EPSG:5514",
            driver="ESRI Shapefile",
        )

    x0, y0 = -744000.0, -1047000.0
    write("VodniPlocha", [box(x0, y0, x0 + 100, y0 + 50)], "Polygon")
    write(
        "VodniTok",
        [
            LineString([(x0 + 10, y0 + 25), (x0 + 90, y0 + 25)]),
            LineString([(x0 + 50, y0 + 25), (x0 + 50, y0 + 200)]),
            LineString([(x0 + 500, y0 + 500), (x0 + 600, y0 + 500)]),
        ],
        "LineString",
    )
    z = tmp_path / "z.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for f in tmp_path.iterdir():
            if f.suffix in {".shp", ".shx", ".dbf", ".prj", ".cpg"}:
                zf.write(f, f.name)
    return z, x0, y0


def _count_waterway_objects(tmp_path, preset, scale, flag):
    import tempfile

    from app.pipeline.oom_import import build_zabaged_object_parts
    from app.pipeline.oom_symbol_map import symbol_index_for_code

    z, x0, y0 = _water_zip(tmp_path)
    parts = build_zabaged_object_parts(
        z,
        vectorconf_name="zabaged.txt",
        preset_id=preset,
        scale=scale,
        ref_x=x0,
        ref_y=y0,
        grivation_deg=0.0,
        work_dir=Path(tempfile.mkdtemp()),
        suppress_waterway_in_water=flag,
    )
    idx = symbol_index_for_code(preset, scale, "306")
    xml = "\n".join(p.objects_xml for p in parts if "VodniTok" in p.name)
    return xml.count(f'symbol="{idx}"')


def test_waterway_under_water_area_not_drawn_in_sprint(tmp_path):
    pytest.importorskip("pyogrio")
    pytest.importorskip("shapely")
    # bez potlačení všechny tři toky; s ním tok celý pod vodou zmizí,
    # druhý se zkrátí na část mimo jezero, třetí zůstane
    assert _count_waterway_objects(tmp_path, "sprint_4000", 4000, False) == 3
    assert _count_waterway_objects(tmp_path, "sprint_4000", 4000, True) == 2


def test_waterway_flag_off_keeps_forest_unchanged(tmp_path):
    pytest.importorskip("pyogrio")
    pytest.importorskip("shapely")
    # les / MTBO se potlačení neúčastní (výchozí False)
    assert _count_waterway_objects(tmp_path, "forest_10000", 10000, False) == 3

def test_zabaged_area_under_osm_area_is_omitted(tmp_path):
    """Priorita OSM: ZABAGED plocha pod / menší než OSM plocha se vynechá."""
    pytest.importorskip("pyogrio")
    pytest.importorskip("shapely")
    import tempfile
    import zipfile

    import numpy as np
    import pyogrio.raw as pr
    from shapely.geometry import box

    from app.pipeline.oom_import import build_zabaged_object_parts
    from app.pipeline.oom_symbol_map import symbol_index_for_code

    x0, y0 = -744000.0, -1047000.0
    zabaged = [
        box(x0 + 10, y0 + 10, x0 + 30, y0 + 30),    # celé pod OSM plochou → pryč
        box(x0 + 80, y0 + 10, x0 + 140, y0 + 30),   # z 1/3 pod OSM plochou → zůstane
        box(x0 + 500, y0 + 10, x0 + 520, y0 + 30),  # mimo OSM plochu → zůstane
    ]
    arr = np.array([g.wkb for g in zabaged], dtype=object)
    pr.write(
        str(tmp_path / "VodniPlocha.shp"),
        geometry=arr,
        field_data=[],
        fields=[],
        geometry_type="Polygon",
        crs="EPSG:5514",
        driver="ESRI Shapefile",
    )
    z = tmp_path / "z.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for f in tmp_path.iterdir():
            if f.suffix in {".shp", ".shx", ".dbf", ".prj", ".cpg"}:
                zf.write(f, f.name)
    osm_water = box(x0, y0, x0 + 100, y0 + 50)

    def count(priority):
        parts = build_zabaged_object_parts(
            z,
            vectorconf_name="zabaged.txt",
            preset_id="sprint_4000",
            scale=4000,
            ref_x=x0,
            ref_y=y0,
            grivation_deg=0.0,
            work_dir=Path(tempfile.mkdtemp()),
            osm_priority_areas=priority,
        )
        idx = symbol_index_for_code("sprint_4000", 4000, "301")
        xml = "\n".join(p.objects_xml for p in parts if "VodniPlocha" in p.name)
        return xml.count(f'symbol="{idx}"')

    assert count(None) == 3
    assert count({"VodniPlocha": osm_water}) == 2


def test_osm_priority_area_geometries_by_kind(tmp_path):
    import app.pipeline.osm_paths as op

    d = tmp_path / "osm_paths"
    d.mkdir()

    def poly(kind, x):
        return {
            "type": "Feature",
            "properties": {"kind": kind},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[x, 0], [x + 10, 0], [x + 10, 10], [x, 10], [x, 0]]],
            },
        }

    (d / "features.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [poly("water_body", 0), poly("parking", 50), poly("building", 90)],
            }
        ),
        encoding="utf-8",
    )
    got = op.osm_priority_area_geometries(tmp_path)
    assert set(got) == {"VodniPlocha", "ParkovisteOdpocivka"}
    assert abs(got["VodniPlocha"].area - 100.0) < 1e-9
    assert op.osm_priority_area_geometries(tmp_path / "neni") == {}


def test_zabaged_street_clipped_by_osm_road_band_in_forest(tmp_path):
    """Les (mixed): ZABAGED Ulice se ořízne o kresbu OSM silnice (``dtm_cover``); sprint beze změny."""
    pytest.importorskip("pyogrio")
    pytest.importorskip("shapely")
    import tempfile
    import zipfile

    import numpy as np
    import pyogrio.raw as pr
    from shapely.geometry import LineString, box

    from app.pipeline.oom_import import build_zabaged_object_parts

    x0, y0 = -744000.0, -1047000.0
    arr = np.array([LineString([(x0, y0), (x0 + 300, y0)]).wkb], dtype=object)
    pr.write(
        str(tmp_path / "Ulice.shp"),
        geometry=arr,
        field_data=[],
        fields=[],
        geometry_type="LineString",
        crs="EPSG:5514",
        driver="ESRI Shapefile",
    )
    z = tmp_path / "z.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for f in tmp_path.iterdir():
            if f.suffix in {".shp", ".shx", ".dbf", ".prj", ".cpg"}:
                zf.write(f, f.name)
    band = box(x0 + 100, y0 - 6, x0 + 200, y0 + 6)  # OSM silnice přes prostřední třetinu

    def count(preset, scale, cover, osm=None, roads=None):
        parts = build_zabaged_object_parts(
            z,
            vectorconf_name="zabaged.txt",
            preset_id=preset,
            scale=scale,
            ref_x=x0,
            ref_y=y0,
            grivation_deg=0.0,
            work_dir=Path(tempfile.mkdtemp()),
            dtm_cover=cover,
            prefer_osm_path_lines=osm,
            prefer_osm_road_lines=roads,
        )
        return sum(p.count for p in parts if "Ulice" in p.name)

    assert count("forest_10000", 10000, None) == 1
    assert count("forest_10000", 10000, band) == 2
    assert count("sprint_4000", 4000, band) == 2  # sprint: ořez o DTM kryt platí taky (Vlachovická way/664421805)
    # mixed = OSM + detaily ze ZABAGED: ulici, kterou má OSM, ZABAGED nekreslí (les / MTBO); sprint beze změny.
    osm = [[(x0, y0 + 2), (x0 + 300, y0 + 2)]]
    assert count("forest_10000", 10000, None, osm) == 0
    assert count("sprint_4000", 4000, None, osm) == 1
    # OSM chodník vedle ulice (sám se nekreslí) ulici nenahradí: dedup ulic jen proti OSM silnicím (50.02916, 14.36073).
    assert count("forest_10000", 10000, None, osm, roads=[]) == 1
    assert count("forest_10000", 10000, None, osm, roads=osm) == 0


def _zabaged_line_zip(tmp_path, layer, lines):
    """ZIP s jednou vrstvou čar ZABAGED (EPSG:5514) pro ``build_zabaged_object_parts``."""
    import zipfile

    import numpy as np
    import pyogrio.raw as pr
    from shapely.geometry import LineString

    arr = np.array([LineString(c).wkb for c in lines], dtype=object)
    pr.write(
        str(tmp_path / f"{layer}.shp"),
        geometry=arr,
        field_data=[],
        fields=[],
        geometry_type="LineString",
        crs="EPSG:5514",
        driver="ESRI Shapefile",
    )
    z = tmp_path / "z.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for f in tmp_path.iterdir():
            if f.suffix in {".shp", ".shx", ".dbf", ".prj", ".cpg"}:
                zf.write(f, f.name)
    return z


def _zabaged_line_lengths(z, layer, preset, scale, x0, y0, osm):
    """Délky (m) čar vrstvy ``layer`` po dedupu proti OSM, z map souřadnic zpět."""
    import re
    import tempfile

    from app.pipeline.oom_import import build_zabaged_object_parts

    parts = build_zabaged_object_parts(
        z,
        vectorconf_name="zabaged.txt",
        preset_id=preset,
        scale=scale,
        ref_x=x0,
        ref_y=y0,
        grivation_deg=0.0,
        work_dir=Path(tempfile.mkdtemp()),
        prefer_osm_path_lines=osm,
    )
    out = []
    for p in parts:
        if layer not in p.name:
            continue
        for m in re.finditer(r"<coords[^>]*>(.*?)</coords>", p.objects_xml, re.DOTALL):
            pts = [tuple(int(v) for v in c.split()[:2]) for c in m.group(1).split(";") if c.strip()]
            n = sum(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 for a, b in zip(pts, pts[1:]))
            out.append(n / 1000.0 * scale / 1000.0)
    return out


def test_zabaged_street_clips_parallel_section_to_osm(tmp_path):
    """Les: souběžný úsek ZABAGED (4,5 m od OSM) se ořízne po úsecích; sprint zůstává celý."""
    pytest.importorskip("pyogrio")
    pytest.importorskip("shapely")
    x0, y0 = -744000.0, -1047000.0
    z = _zabaged_line_zip(tmp_path, "Ulice", [[(x0, y0), (x0 + 300, y0)]])
    osm = [[(x0 + 75, y0 + 4.5), (x0 + 225, y0 + 4.5)]]
    forest = _zabaged_line_lengths(z, "Ulice", "forest_10000", 10000, x0, y0, osm)
    assert len(forest) == 2
    assert all(60 <= v <= 80 for v in forest)
    assert len(_zabaged_line_lengths(z, "Ulice", "sprint_4000", 4000, x0, y0, osm)) == 1


def test_zabaged_path_crossing_osm_not_shortened(tmp_path):
    """Kolmé křížení OSM čáry ZABAGED cestu nezkrátí."""
    pytest.importorskip("pyogrio")
    pytest.importorskip("shapely")
    x0, y0 = -744000.0, -1047000.0
    z = _zabaged_line_zip(tmp_path, "Cesta", [[(x0 + 150, y0 - 100), (x0 + 150, y0 + 100)]])
    osm = [[(x0, y0), (x0 + 300, y0)]]
    lengths = _zabaged_line_lengths(z, "Cesta", "forest_10000", 10000, x0, y0, osm)
    assert len(lengths) == 1
    assert lengths[0] == pytest.approx(200, abs=1)


def test_zabaged_remnant_after_parallel_clip_dropped(tmp_path):
    """Zbytek kratší než 15 m vzniklý ořezem souběžného úseku zmizí; dlouhý zbytek zůstane."""
    pytest.importorskip("pyogrio")
    pytest.importorskip("shapely")
    x0, y0 = -744000.0, -1047000.0
    z = _zabaged_line_zip(tmp_path, "Ulice", [[(x0, y0), (x0 + 300, y0)]])
    osm = [[(x0 + 10, y0 + 4.5), (x0 + 190, y0 + 4.5)]]  # souběh od 10 m: zbytek vlevo je kratší než 15 m
    lengths = _zabaged_line_lengths(z, "Ulice", "forest_10000", 10000, x0, y0, osm)
    assert len(lengths) == 1 and lengths[0] == pytest.approx(106, abs=4)
    osm = [[(x0 + 40, y0 + 4.5), (x0 + 220, y0 + 4.5)]]  # zbytek vlevo ~ 36 m zůstane
    lengths = sorted(_zabaged_line_lengths(z, "Ulice", "forest_10000", 10000, x0, y0, osm))
    assert len(lengths) == 2 and lengths[0] == pytest.approx(36, abs=4)


def test_zabaged_street_not_drawn_across_paved_area_in_forest(tmp_path):
    """Les / MTBO: ZABAGED Ulice přes zpevněnou plochu (parkoviště, slité podélné stání) se uvnitř nekreslí, jen
    napojení na hranu – jako OSM silnice (``clip_road_in_paved``); Pesina končí na hraně plochy."""
    pytest.importorskip("pyogrio")
    pytest.importorskip("shapely")
    import tempfile

    from shapely.geometry import box

    from app.pipeline.oom_import import build_zabaged_object_parts

    x0, y0 = -744000.0, -1047000.0
    z = _zabaged_line_zip(tmp_path, "Ulice", [[(x0, y0), (x0 + 300, y0)]])
    paved = box(x0 + 100, y0 - 10, x0 + 200, y0 + 10)

    def count(clip):
        parts = build_zabaged_object_parts(
            z,
            vectorconf_name="zabaged.txt",
            preset_id="forest_10000",
            scale=10000,
            ref_x=x0,
            ref_y=y0,
            grivation_deg=0.0,
            work_dir=Path(tempfile.mkdtemp()),
            paved_clip=clip,
        )
        return sum(p.count for p in parts if "Ulice" in p.name)

    assert count(None) == 1
    assert count((paved, paved)) == 2  # ulice před a za plochou, uvnitř ne


def test_zabaged_road_bridge_yields_to_osm_bridge(tmp_path):
    """Les / MTBO: silniční most ze ZABAGED (vrstva Most) souběžný s OSM silnicí / mostem se nekreslí; sprint ano."""
    pytest.importorskip("pyogrio")
    pytest.importorskip("shapely")
    x0, y0 = -744000.0, -1047000.0
    z = _zabaged_line_zip(tmp_path, "Most", [[(x0, y0), (x0 + 250, y0)]])
    osm = [[(x0 - 20, y0 + 1.0), (x0 + 270, y0 + 1.0)]]
    assert len(_zabaged_line_lengths(z, "Most", "sprint_4000", 4000, x0, y0, None)) == 1
    assert len(_zabaged_line_lengths(z, "Most", "mtbo_10000", 10000, x0, y0, None)) == 1
    assert _zabaged_line_lengths(z, "Most", "mtbo_10000", 10000, x0, y0, osm) == []
    assert _zabaged_line_lengths(z, "Most", "forest_10000", 10000, x0, y0, osm) == []
    assert len(_zabaged_line_lengths(z, "Most", "sprint_4000", 4000, x0, y0, osm)) == 1


def test_zabaged_tunnel_clipped_by_dtm_cover_in_forest(tmp_path):
    """Les / MTBO: ZABAGED Tunel (silniční) se uvnitř DTM koridoru nekreslí; sprint beze změny."""
    pytest.importorskip("pyogrio")
    pytest.importorskip("shapely")
    import tempfile

    from shapely.geometry import box

    from app.pipeline.oom_import import build_zabaged_object_parts

    x0, y0 = -744000.0, -1047000.0
    z = _zabaged_line_zip(tmp_path, "Tunel", [[(x0, y0), (x0 + 35, y0)]])  # krátký podchod
    cover = box(x0 + 10, y0 - 10, x0 + 25, y0 + 10)

    def count(preset, scale, zip_path=None):
        parts = build_zabaged_object_parts(
            zip_path or z,
            vectorconf_name="zabaged.txt",
            preset_id=preset,
            scale=scale,
            ref_x=x0,
            ref_y=y0,
            grivation_deg=0.0,
            work_dir=Path(tempfile.mkdtemp()),
            dtm_cover=cover,
        )
        return sum(p.count for p in parts if "Tunel" in p.name)

    assert count("forest_10000", 10000) == 2  # před a za koridorem, uvnitř ne
    assert count("sprint_4000", 4000) == 1
    # dlouhý tunel (i tramvaj pod povrchem bez kódu železnice) se v lese nekreslí, ve sprintu ano
    long_dir = tmp_path / "long"
    long_dir.mkdir()
    z_long = _zabaged_line_zip(long_dir, "Tunel", [[(x0, y0 + 50), (x0 + 300, y0 + 50)]])
    assert count("forest_10000", 10000, z_long) == 0
    assert count("sprint_4000", 4000, z_long) == 1
    cover = None  # bez DTM (volba OSM + ZABAGED): dlouhý tunel se v lese nekreslí ani tak
    assert count("forest_10000", 10000, z_long) == 0
    assert count("forest_10000", 10000) == 1


def test_drop_cover_stubs_keeps_long_and_far_pieces():
    from shapely.geometry import box

    from app.pipeline.oom_import import _drop_cover_stubs

    cover = box(0, 0, 100, 10)
    stub = [(50, 12), (50, 18)]  # 6 m, konec 2 m od plochy
    far = [(50, 30), (50, 36)]  # krátký, ale daleko
    long = [(50, 12), (50, 40)]
    assert _drop_cover_stubs([stub, far, long], cover, 10000) == [far, long]


def test_sprint_zabaged_road_bridge_on_osm_path_dropped(tmp_path):
    """Sprint: ZABAGED Most na OSM cestě, která v OSM most není (cyklostezka nad pěším tunelem way/160570380), se nekreslí."""
    pytest.importorskip("pyogrio")
    pytest.importorskip("shapely")
    import tempfile

    from app.pipeline.oom_import import build_zabaged_object_parts

    x0, y0 = -744000.0, -1047000.0
    z = _zabaged_line_zip(tmp_path, "Most", [[(x0, y0), (x0 + 8, y0)], [(x0, y0 + 50), (x0 + 8, y0 + 50)]])

    def count(ground):
        parts = build_zabaged_object_parts(
            z,
            vectorconf_name="zabaged.txt",
            preset_id="sprint_4000",
            scale=4000,
            ref_x=x0,
            ref_y=y0,
            grivation_deg=0.0,
            work_dir=Path(tempfile.mkdtemp()),
            prefer_osm_path_lines=ground,
            osm_ground_lines=ground,
        )
        return sum(p.count for p in parts if "Most" in p.name)

    assert count(None) == 2
    assert count([[(x0 - 50, y0 + 1), (x0 + 60, y0 + 1)]]) == 1  # druhý most OSM cesta nemá
