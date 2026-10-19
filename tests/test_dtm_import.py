"""DTM ploty v pipeline: příprava, objekty do mapy a priorita před OSM."""

from __future__ import annotations

import json

import app.pipeline.dtm_import as di
import app.pipeline.dtm_sources as ds
from app.pipeline.dtm_fences import Fence

X0, Y0 = -744000.0, -1047000.0
BBOX = (14.405, 50.046, 14.419, 50.054)


def _pkg():
    return ds.DtmPackage(
        url="https://x/VS_CZ010_MOMC_500143_20260930_ZPS_KR.jvf.zip",
        kraj="CZ010",
        unit_kind="MOMC",
        unit_code=500143,
        date="20260930",
        set_name="ZPS_KR",
        fmt="jvf",
    )


def _patch_sources(monkeypatch, tmp_path, fences):
    monkeypatch.setattr(ds, "find_packages_for_bbox", lambda bbox, **k: [_pkg()])
    monkeypatch.setattr(ds, "download_package", lambda pkg, **k: tmp_path / pkg.filename)
    monkeypatch.setattr(di.dtm_fences, "load_fences", lambda path, **k: fences)
    # souřadnice výřezu → pro test je jedno, filtr obdélníkem se obejde
    monkeypatch.setattr(di.dtm_fences, "fences_in_bounds", lambda fs, b: fs)


def test_prepare_writes_merged_fences(tmp_path, monkeypatch):
    fences = [
        Fence(((X0, Y0), (X0 + 10, Y0)), "2", 0, False),
        Fence(((X0 + 10, Y0), (X0 + 10, Y0 + 8)), "4", 0, True),  # roh → spojí se
        Fence(((X0 + 50, Y0), (X0 + 60, Y0)), "5", 0, False),  # živý plot
        Fence(((X0 + 90, Y0), (X0 + 99, Y0)), "2", -1, False),  # pod povrchem
    ]
    _patch_sources(monkeypatch, tmp_path, fences)
    logs: list[str] = []
    out = di.prepare_dtm_fences(tmp_path / "work", BBOX, log=logs.append)
    assert out == di.fences_path(tmp_path / "work")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["source_pieces"] == 4
    codes = sorted(f["code"] for f in data["fences"])
    assert codes == ["416", "518"]  # podzemní plot vynechán, dva úseky 518 → jedna čára
    assert any("napojených čar" in m for m in logs)
    assert len(di.load_fence_lines(tmp_path / "work")) == 2


def test_prepare_returns_none_without_packages(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "find_packages_for_bbox", lambda bbox, **k: [])
    logs: list[str] = []
    assert di.prepare_dtm_fences(tmp_path, BBOX, log=logs.append) is None
    assert any("nejsou datové sady" in m for m in logs)
    assert not di.fences_path(tmp_path).exists()


def test_prefetch_swallows_errors_and_returns_none(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("DMVS nedostupné")

    monkeypatch.setattr(di, "prepare_dtm_fences", boom)
    logs: list[str] = []
    pre = di.start_dtm_prefetch(tmp_path, BBOX, log=logs.append)
    assert pre.result() is None  # job pokračuje bez DTM
    assert any("přeskočeny" in m and "DMVS nedostupné" in m for m in logs)


def _write_fences(work_dir, lines):
    p = di.fences_path(work_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"fences": [{"code": c, "coords": [list(pt) for pt in l]} for c, l in lines]}),
        encoding="utf-8",
    )


def test_build_parts_sprint_only_with_codes(tmp_path):
    from app.pipeline.oom_symbol_map import symbol_index_for_code

    _write_fences(
        tmp_path,
        [("518", [(X0, Y0), (X0 + 20, Y0)]), ("416", [(X0, Y0 + 5), (X0 + 20, Y0 + 5)])],
    )
    kw = dict(ref_x=X0, ref_y=Y0, grivation_deg=0.0)
    parts = di.build_dtm_fence_parts(tmp_path, preset_id="sprint_4000", scale=4000, **kw)
    assert len(parts) == 1 and parts[0].count == 2
    xml = parts[0].objects_xml
    assert f'symbol="{symbol_index_for_code("sprint_4000", 4000, "518")}"' in xml
    assert f'symbol="{symbol_index_for_code("sprint_4000", 4000, "416")}"' in xml
    # les / MTBO a oblast bez DTM: nic
    assert di.build_dtm_fence_parts(tmp_path, preset_id="forest_10000", scale=10000, **kw) == []
    assert di.build_dtm_fence_parts(tmp_path / "jinde", preset_id="sprint_4000", scale=4000, **kw) == []


def test_subtract_dtm_from_osm_fences(tmp_path):
    _write_fences(tmp_path, [("518", [(0.0, 0.0), (100.0, 0.0)])])
    buf = di.dtm_fence_buffer(tmp_path)
    assert buf is not None

    def osm(coords, kind="fence"):
        return {"type": "Feature", "properties": {"kind": kind}, "geometry": {"type": "LineString", "coordinates": coords}}

    covered = osm([[10, 1], [90, 1]])  # 1 m od plotu z DTM → celý pryč
    partial = osm([[50, 1], [200, 1]])  # polovina u DTM, zbytek 100 m zůstane
    far = osm([[0, 50], [100, 50]])  # daleko → beze změny
    wall = osm([[10, 1], [90, 1]], kind="wall")  # jiný druh → beze změny
    out = di.subtract_dtm_from_osm_fences([covered, partial, far, wall], buf)
    assert far in out and wall in out
    assert covered not in out
    cut = [f for f in out if f is not far and f is not wall]
    assert len(cut) == 1
    xs = [p[0] for p in cut[0]["geometry"]["coordinates"]]
    assert min(xs) >= 100 - 3.0 - 1e-6 and max(xs) == 200  # začíná až za pásem DTM
    # bez DTM se nic nemění
    assert di.subtract_dtm_from_osm_fences([covered], None) == [covered]
