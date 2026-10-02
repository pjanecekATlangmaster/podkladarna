from __future__ import annotations

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
    assert symbol_index_for_code("sprint_2m", 4000, code) == 141


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
    """Sjízdná ulice ve sprintu = ISSprOM 501.19 (heavy), ne hrana 501.1."""
    code = oom_code_for_vectorconf_rule(
        "road-path",
        "503",
        "Ulice",
        preset_id="sprint_2m",
        scale=4000,
    )
    assert code == "501.19"
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
    assert idx < road_idx, (idx, road_idx)


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
    """Bez KP: contours/ má jen dem_smooth; dense-contour potřebuje dem/dem_filled."""
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
