"""Opravy bezpečné pro souběh les × MTBO: vlastní dočasné složky, bez přejmenování .prj, atomické zápisy."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_zabaged_parts_use_own_stage_and_clean_up(tmp_path):
    pytest.importorskip("pyogrio")
    pytest.importorskip("shapely")
    from app.pipeline.oom_import import build_zabaged_object_parts
    from tests.test_oom_import import _water_zip

    z, x0, y0 = _water_zip(tmp_path)
    work = tmp_path / "job"
    work.mkdir()
    stale = work / "_oom_objects"  # pozůstatek starého běhu / jiného procesu nesmí překážet
    stale.mkdir()
    (stale / "x").write_text("x")
    kw = dict(
        vectorconf_name="zabaged.txt", preset_id="forest_10000", scale=10000,
        ref_x=x0, ref_y=y0, grivation_deg=0.0, work_dir=work,
    )  # fmt: skip
    first = build_zabaged_object_parts(z, **kw)
    second = build_zabaged_object_parts(z, **kw)
    assert [p.objects_xml for p in first] == [p.objects_xml for p in second]
    assert (stale / "x").is_file()
    assert [d.name for d in work.iterdir()] == ["_oom_objects"]


def test_vege_rows_keep_prj_in_place(tmp_path, monkeypatch):
    pytest.importorskip("shapely")
    import sys
    import types

    import numpy as np
    import pyogrio.raw as pr
    from shapely.geometry import box

    from app.pipeline.vegetation_gdal import _iter_vege_rows

    shp = tmp_path / "vegetation.shp"
    pr.write(
        str(shp), geometry=np.array([box(0, 0, 5, 5).wkb], dtype=object), field_data=[], fields=[],
        geometry_type="Polygon", crs="EPSG:5514", driver="ESRI Shapefile",
    )  # fmt: skip
    prj = shp.with_suffix(".prj")
    assert prj.is_file()
    seen: dict = {}

    def fake_open(path):  # OGR otevře kopii bez .prj, originál zůstane celý
        seen["dir"] = Path(path).parent
        seen["files"] = sorted(p.suffix for p in Path(path).parent.iterdir())
        seen["prj_in_place"] = prj.is_file() and not shp.with_suffix(".prj.aside").exists()
        return None  # dál se čte přes pyogrio

    osgeo = types.ModuleType("osgeo")
    osgeo.ogr = types.SimpleNamespace(Open=fake_open)
    monkeypatch.setitem(sys.modules, "osgeo", osgeo)
    rows = list(_iter_vege_rows(shp))
    assert len(rows) == 1
    assert ".prj" not in seen["files"] and ".shp" in seen["files"] and seen["prj_in_place"]
    assert not seen["dir"].exists() and prj.is_file() and not shp.with_suffix(".prj.aside").exists()


@pytest.mark.parametrize("as_bytes", [False, True])
def test_atomic_write_replaces_without_leftovers(tmp_path, as_bytes):
    from app.fsutil import atomic_write_bytes, atomic_write_text

    target = tmp_path / "c.json"
    target.write_text("old")
    if as_bytes:
        atomic_write_bytes(target, "nový".encode())
    else:
        atomic_write_text(target, "nový")
    assert target.read_text(encoding="utf-8") == "nový"
    assert [p.name for p in tmp_path.iterdir()] == ["c.json"]
    assert isinstance(target, Path)
