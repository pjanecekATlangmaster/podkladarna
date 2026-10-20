"""DTM ploty v pipeline: příprava, objekty do mapy a priorita před OSM."""

from __future__ import annotations

import json

import pytest

import app.pipeline.dtm_import as di
import app.pipeline.dtm_sources as ds
from app.pipeline.dtm_fences import Fence


@pytest.fixture(autouse=True)
def _mtbo_dtm_on(monkeypatch):
    monkeypatch.setattr(di, "MTBO_USE_DTM", True)  # MTBO má DTM ve výchozím stavu vypnuté

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
    monkeypatch.setattr(ds, "download_packages", lambda pkgs, **k: {p.filename: tmp_path / p.filename for p in pkgs})
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
    # les / MTBO: stejné spojené ploty, ale symboly presetu (516 / 522, živý plot 416), bez výplně
    forest = di.build_dtm_fence_parts(tmp_path, preset_id="forest_10000", scale=10000, **kw)
    assert len(forest) == 1 and forest[0].count == 2
    assert f'symbol="{symbol_index_for_code("forest_10000", 10000, "516")}"' in forest[0].objects_xml
    mtbo = di.build_dtm_fence_parts(tmp_path, preset_id="mtbo_10000", scale=10000, **kw)
    assert len(mtbo) == 1 and mtbo[0].count >= 1
    assert f'symbol="{symbol_index_for_code("mtbo_10000", 10000, "522")}"' in mtbo[0].objects_xml
    # krátké ploty (zahradní oplocení) se v lese / MTBO nekreslí
    _write_fences(tmp_path, [("518", [(X0, Y0), (X0 + 10, Y0)])])
    assert di.build_dtm_fence_parts(tmp_path, preset_id="forest_10000", scale=10000, **kw) == []
    assert di.build_dtm_fence_parts(tmp_path, preset_id="sprint_4000", scale=4000, **kw) != []
    # oblast bez DTM: nic
    assert di.build_dtm_fence_parts(tmp_path / "jinde", preset_id="sprint_4000", scale=4000, **kw) == []
    assert di.build_dtm_fence_parts(tmp_path / "jinde", preset_id="forest_10000", scale=10000, **kw) == []


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

def test_prepare_orients_fences_with_opl_areas(tmp_path, monkeypatch):
    opl = ds.DtmPackage(
        url="https://x/VS_CZ010_MOMC_500143_20260930_ZPS_OPL.jvf.zip",
        kraj="CZ010", unit_kind="MOMC", unit_code=500143, date="20260930", set_name="ZPS_OPL", fmt="jvf",
    )
    monkeypatch.setattr(ds, "find_packages_for_bbox", lambda bbox, **k: [_pkg(), opl])
    monkeypatch.setattr(ds, "download_packages", lambda pkgs, **k: {p.filename: tmp_path / p.filename for p in pkgs})
    # plot na východ; oliva (zahrada) je nalevo (sever) → čárky vpravo by mířily ke chodníku → otočit
    fences = [Fence(((X0, Y0), (X0 + 60, Y0)), "2", 0, False)]
    monkeypatch.setattr(di.dtm_fences, "load_fences", lambda path, **k: fences)
    monkeypatch.setattr(di.dtm_fences, "fences_in_bounds", lambda fs, b: fs)

    def rect(x0, y0, x1, y1):
        return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]

    areas = {
        "olive": [rect(X0 - 5, Y0 + 0.1, X0 + 65, Y0 + 10)],
        "public": [rect(X0 - 5, Y0 - 10, X0 + 65, Y0 - 0.1)],
    }
    monkeypatch.setattr(di.dtm_fences, "load_orientation_areas", lambda path, **k: areas)
    out = di.prepare_dtm_fences(tmp_path / "work", BBOX)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["oriented"] is True
    coords = data["fences"][0]["coords"]
    # otočeno k olivě a odsunuto od chodníku o FENCE_SETBACK_M k olivě (sever)
    sb = di.dtm_fences.FENCE_SETBACK_M
    assert coords[0] == [X0 + 60, Y0 + sb] and coords[-1] == [X0, Y0 + sb]

    # chyba orientace nesmí shodit plot
    def boom(path, **k):
        raise ValueError("poškozené OPL")

    monkeypatch.setattr(di.dtm_fences, "load_orientation_areas", boom)
    logs: list[str] = []
    out = di.prepare_dtm_fences(tmp_path / "work2", BBOX, log=logs.append)
    assert json.loads(out.read_text(encoding="utf-8"))["oriented"] is False
    assert any("plochy z OPL nelze načíst" in m for m in logs)


def _write_unified_fixture(work, dtm_fences_list, osm_fences):
    (work / "dtm").mkdir(parents=True)
    (work / "dtm" / "fences.json").write_text(
        json.dumps({"fences": [{"code": "518", "coords": [list(p) for p in ln]} for ln in dtm_fences_list], "fills": [], "opl_files": []}),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir(parents=True)
    feats = [
        {"type": "Feature", "properties": {"kind": "fence"}, "geometry": {"type": "LineString", "coordinates": [list(p) for p in ln]}}
        for ln in osm_fences
    ]
    (work / "osm_paths" / "features.geojson").write_text(json.dumps({"features": feats}), encoding="utf-8")


KW = dict(preset_id="sprint_2_5m", scale=4000, ref_x=X0, ref_y=Y0, grivation_deg=0.0)


def test_unified_fences_draw_osm_and_dtm_in_one_part_and_drop_inner_osm_line(tmp_path):
    ring = [(X0, Y0), (X0 + 40, Y0), (X0 + 40, Y0 + 40), (X0, Y0 + 40), (X0, Y0)]  # uzavřený OSM plot
    inner = [(X0 + 10, Y0 + 10), (X0 + 25, Y0 + 10)]  # osamělá čára uvnitř
    dtm = [(X0 + 200, Y0), (X0 + 230, Y0)]
    _write_unified_fixture(tmp_path, [dtm], [ring, inner])
    parts = di.build_dtm_fence_parts(tmp_path, **KW)
    assert [p.name for p in parts] == ["Ploty (DTM + OSM)"]
    assert parts[0].count == 2  # DTM čára + OSM oplocení; vnitřní OSM čára zahozena


def test_unified_fences_join_osm_ways_and_skip_without_dtm(tmp_path):
    a = [(X0, Y0), (X0 + 20, Y0)]
    b = [(X0 + 20, Y0), (X0 + 20, Y0 + 20)]  # navazuje na a
    _write_unified_fixture(tmp_path, [], [a, b])
    parts = di.build_dtm_fence_parts(tmp_path, **KW)
    assert parts and parts[0].count == 1  # dva OSM ways spojené do jednoho plotu
    # bez dat DTM se nekreslí nic (ploty z OSM kreslí build_osm_feature_parts)
    assert di.build_dtm_fence_parts(tmp_path / "jiny", **KW) == []
    assert not di.dtm_owns_fences(tmp_path / "jiny")


def test_osm_feature_parts_skip_fences_when_dtm_owns_them(tmp_path):
    from app.pipeline import osm_paths as o

    fence = [(X0, Y0), (X0 + 20, Y0)]
    _write_unified_fixture(tmp_path, [[(X0 + 200, Y0), (X0 + 230, Y0)]], [fence])
    parts = o.build_osm_feature_parts(tmp_path, **KW)
    assert all(p.name != "OSM ploty" for p in parts)
    (tmp_path / "dtm" / "fences.json").unlink()  # bez DTM kreslí OSM ploty původní cestou
    parts = o.build_osm_feature_parts(tmp_path, **KW)
    assert any(p.name == "OSM ploty" for p in parts)
