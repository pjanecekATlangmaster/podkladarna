import io
import zipfile

import shapefile

from app.pipeline import osm_paths as op


def _zip(tmp_path, polys):
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON)
    w.field("vrstva", "C")
    for ring in polys:
        w.poly([ring])
        w.record("OstatniPlochaVSidlech")
    w.close()
    path = tmp_path / "zabaged_clean.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for ext, buf in (("shp", shp), ("shx", shx), ("dbf", dbf)):
            zf.writestr(f"OstatniPlochaVSidlech.{ext}", buf.getvalue())
    return path


def _sq(x, y, a):
    return [(x, y), (x, y + a), (x + a, y + a), (x + a, y), (x, y)]


def test_zabaged_other_area_joins_paved_network_in_forest_only(tmp_path):
    archive = _zip(tmp_path, [_sq(0, 0, 30), _sq(100, 0, 300)])  # druhá 9 ha nad limit
    assert op.with_zabaged_paved_areas([], "forest_10000") == []  # mimo sestavení mapy beze změny
    with op.zabaged_paved_areas(archive, 50_000.0):
        assert op.zabaged_paved_active("forest_10000") and not op.zabaged_paved_active("sprint_4000")
        feats = op.with_zabaged_paved_areas([], "mtbo_10000")
        assert op.with_zabaged_paved_areas([], "sprint_4000") == []
    assert [f["properties"]["kind"] for f in feats] == ["pedestrian_area"]
    assert feats[0]["properties"]["source"] == "zabaged"
    assert not op.zabaged_paved_active("forest_10000")
    # Vrstva se nekreslí (volba „žádné“) → nic.
    with op.zabaged_paved_areas(archive, None):
        assert op.with_zabaged_paved_areas([], "forest_10000") == []
    # Úzký výběžek ZABAGED plochy se neotevírá (nemá čáru, která by ho kreslila).
    assert op.open_narrow_walk_areas(feats, "forest_10000", 10000) == feats
