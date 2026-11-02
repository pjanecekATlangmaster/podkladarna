"""Testy inverzní zpevněné plochy (residential − všechny OSM objekty)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipeline.residual_paved import (
    RESIDUAL_SPARSE_STEM,
    _PART_NAME,
    _RESIDUAL_BANDS_DIR,
    _subjects_and_mask_wkbs,
    build_residual_paved_parts,
    path_mask_half_width_m,
    residual_band_stem,
    residual_max_m2,
)


def _sq_wgs(lat0, lon0, dlat, dlon):
    """Uzavřený ring v lat/lon (přibližně čtverec)."""
    return [
        {"lat": lat0, "lon": lon0},
        {"lat": lat0, "lon": lon0 + dlon},
        {"lat": lat0 + dlat, "lon": lon0 + dlon},
        {"lat": lat0 + dlat, "lon": lon0},
        {"lat": lat0, "lon": lon0},
    ]


def _write_cache(work: Path, elements: list[dict]) -> None:
    dest = work / "osm_paths"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "residual_osm_all.json").write_text(
        json.dumps({"elements": elements}),
        encoding="utf-8",
    )


def test_path_mask_half_width_has_outline_reserve():
    assert path_mask_half_width_m("road_3") > path_mask_half_width_m("path")
    assert path_mask_half_width_m("road_2") >= 2.5 + 0.8
    assert path_mask_half_width_m("sidewalk") >= 0.7 + 0.8
    assert path_mask_half_width_m("footway") < path_mask_half_width_m("sidewalk")


def test_school_polygon_is_in_mask_not_subject():
    """way/365640538 amenity=school musí jít do masky, ne do subject."""
    residential = {
        "type": "way",
        "id": 1,
        "tags": {"landuse": "residential"},
        "geometry": _sq_wgs(50.10, 14.56, 0.002, 0.003),
    }
    school = {
        "type": "way",
        "id": 365640538,
        "tags": {"amenity": "school"},
        "geometry": _sq_wgs(50.1005, 14.561, 0.0004, 0.0005),
    }
    subjects, mask = _subjects_and_mask_wkbs([residential, school])
    assert len(subjects) == 1
    assert len(mask) >= 1
    from shapely import from_wkb

    assert from_wkb(mask[0]).area < from_wkb(subjects[0]).area


def test_residual_cuts_school_out_of_residential(tmp_path: Path):
    residential = {
        "type": "way",
        "id": 1,
        "tags": {"landuse": "residential"},
        "geometry": _sq_wgs(50.10, 14.56, 0.002, 0.003),
    }
    # Škola + velká budova pokrývají většinu – zbytek pod prahem 25 %.
    school = {
        "type": "way",
        "id": 365640538,
        "tags": {"amenity": "school"},
        "geometry": _sq_wgs(50.1002, 14.5605, 0.0012, 0.0015),
    }
    building = {
        "type": "way",
        "id": 2,
        "tags": {"building": "yes"},
        "geometry": _sq_wgs(50.1001, 14.5601, 0.0018, 0.0028),
    }
    _write_cache(tmp_path, [residential, school, building])
    parts = build_residual_paved_parts(
        tmp_path,
        preset_id="sprint_2m",
        scale=4000,
        ref_x=0.0,
        ref_y=0.0,
        grivation_deg=0.0,
        max_piece_m2=float("inf"),
    )
    assert len(parts) == 1
    assert parts[0].name == _PART_NAME
    assert parts[0].count >= 1
    assert 'symbol="' in parts[0].objects_xml


def test_highway_line_goes_to_mask_as_buffer():
    road = {
        "type": "way",
        "id": 2,
        "tags": {"highway": "residential"},
        "geometry": [
            {"lat": 50.10, "lon": 14.56},
            {"lat": 50.101, "lon": 14.562},
        ],
    }
    subjects, mask = _subjects_and_mask_wkbs([road])
    assert subjects == []
    assert len(mask) == 1


def test_residual_off_for_forest_preset(tmp_path: Path):
    _write_cache(
        tmp_path,
        [
            {
                "type": "way",
                "id": 1,
                "tags": {"landuse": "residential"},
                "geometry": _sq_wgs(50.0, 14.0, 0.001, 0.001),
            }
        ],
    )
    assert (
        build_residual_paved_parts(
            tmp_path,
            preset_id="forest_10000",
            scale=10000,
            ref_x=0.0,
            ref_y=0.0,
            grivation_deg=0.0,
        )
        == []
    )


def test_residual_empty_without_residential(tmp_path: Path):
    _write_cache(
        tmp_path,
        [
            {
                "type": "way",
                "id": 365640538,
                "tags": {"amenity": "school"},
                "geometry": _sq_wgs(50.0, 14.0, 0.001, 0.001),
            }
        ],
    )
    assert (
        build_residual_paved_parts(
            tmp_path,
            preset_id="sprint_2m",
            scale=4000,
            ref_x=0.0,
            ref_y=0.0,
            grivation_deg=0.0,
        )
        == []
    )


def test_sparse_residential_is_skipped(tmp_path: Path):
    """Residential skoro bez OSM uvnitř → přeskočit (jinak přikryje podklad)."""
    # Velký residential, uvnitř jen malá budova → zbytek >> 25 %.
    residential = {
        "type": "way",
        "id": 1275344912,
        "tags": {"landuse": "residential"},
        "geometry": _sq_wgs(50.10, 14.56, 0.003, 0.004),
    }
    tiny = {
        "type": "way",
        "id": 99,
        "tags": {"building": "yes"},
        "geometry": _sq_wgs(50.101, 14.561, 0.00005, 0.00005),
    }
    _write_cache(tmp_path, [residential, tiny])
    assert (
        build_residual_paved_parts(
            tmp_path,
            preset_id="sprint_2m",
            scale=4000,
            ref_x=0.0,
            ref_y=0.0,
            grivation_deg=0.0,
        )
        == []
    )
    sparse_shp = tmp_path / _RESIDUAL_BANDS_DIR / f"{RESIDUAL_SPARSE_STEM}.shp"
    assert sparse_shp.is_file()


def test_dense_residential_is_kept(tmp_path: Path):
    """Residential hustě vyplněný OSM maskou → zbytek pod prahem, kreslit."""
    residential = {
        "type": "way",
        "id": 1275344910,
        "tags": {"landuse": "residential"},
        "geometry": _sq_wgs(50.10, 14.56, 0.001, 0.001),
    }
    # Budova pokrývá většinu plochy.
    building = {
        "type": "way",
        "id": 100,
        "tags": {"building": "yes"},
        "geometry": _sq_wgs(50.10005, 14.56005, 0.0009, 0.0009),
    }
    _write_cache(tmp_path, [residential, building])
    parts = build_residual_paved_parts(
        tmp_path,
        preset_id="sprint_2m",
        scale=4000,
        ref_x=0.0,
        ref_y=0.0,
        grivation_deg=0.0,
        max_piece_m2=float("inf"),
    )
    assert len(parts) == 1
    assert parts[0].count >= 1


def test_residual_size_filter_and_band_shp(tmp_path: Path):
    """Do .omap jen ≤ max; SHP má i větší pásmo."""
    assert residual_max_m2("small") == 500.0
    assert residual_band_stem(100.0).endswith("_mensi")
    assert residual_band_stem(1000.0).endswith("_stredni")
    assert residual_band_stem(5000.0).endswith("_velke")

    # Malý residential, budova nechá zbytek ~16 % (~1200 m²) → střední pásmo.
    residential = {
        "type": "way",
        "id": 1,
        "tags": {"landuse": "residential"},
        "geometry": _sq_wgs(50.10, 14.56, 0.001, 0.001),
    }
    building = {
        "type": "way",
        "id": 2,
        "tags": {"building": "yes"},
        "geometry": _sq_wgs(50.10005, 14.56005, 0.0009, 0.0009),
    }
    _write_cache(tmp_path, [residential, building])
    # small (≤500) → do omap nic, ale SHP stredni ano
    assert (
        build_residual_paved_parts(
            tmp_path,
            preset_id="sprint_2m",
            scale=4000,
            ref_x=0.0,
            ref_y=0.0,
            grivation_deg=0.0,
            max_piece_m2=500.0,
        )
        == []
    )
    band_dir = tmp_path / _RESIDUAL_BANDS_DIR
    assert (band_dir / "OSM_residential_zbytek_stredni.shp").is_file()
    # medium → už v omap
    parts = build_residual_paved_parts(
        tmp_path,
        preset_id="sprint_2m",
        scale=4000,
        ref_x=0.0,
        ref_y=0.0,
        grivation_deg=0.0,
        max_piece_m2=2000.0,
    )
    assert len(parts) == 1
    assert parts[0].count >= 1


def test_residual_soft_timeout_skips_remaining(tmp_path: Path, monkeypatch):
    """Po soft timeoutu se zbývající subjecty přeskočí; job log dostane varování."""
    residential_a = {
        "type": "way",
        "id": 1,
        "tags": {"landuse": "residential"},
        "geometry": _sq_wgs(50.10, 14.56, 0.001, 0.001),
    }
    residential_b = {
        "type": "way",
        "id": 2,
        "tags": {"landuse": "residential"},
        "geometry": _sq_wgs(50.102, 14.562, 0.001, 0.001),
    }
    building = {
        "type": "way",
        "id": 3,
        "tags": {"building": "yes"},
        "geometry": _sq_wgs(50.1002, 14.5602, 0.0003, 0.0003),
    }
    _write_cache(tmp_path, [residential_a, residential_b, building])

    msgs: list[str] = []
    clock = {"t": 1000.0}

    def fake_mono():
        return clock["t"]

    from app.pipeline import residual_paved as rp

    real_diff = rp._difference_one_subject

    def slow_diff(subject, mask_index):
        clock["t"] += 10.0
        return real_diff(subject, mask_index)

    monkeypatch.setattr(rp, "_difference_one_subject", slow_diff)
    monkeypatch.setattr(rp.time, "monotonic", fake_mono)

    parts = build_residual_paved_parts(
        tmp_path,
        preset_id="sprint_2m",
        scale=4000,
        ref_x=0.0,
        ref_y=0.0,
        grivation_deg=0.0,
        max_piece_m2=float("inf"),
        write_shapefiles=False,
        log=msgs.append,
        soft_timeout_s=5.0,
    )
    assert any("soft timeout" in m for m in msgs)
    assert isinstance(parts, list)


def test_package_oom_gates_residual_call():
    """V package_oom je residual volaný jen uvnitř if residual_paved."""
    import inspect

    from app.pipeline import package_oom as po

    src = inspect.getsource(po._prepare_oom_map)
    assert "if residual_paved:" in src
    assert "vždy spočítat" not in src
    assert "max_piece_m2=max_residual_m2 if residual_paved else 0.0" not in src

def test_residual_prefetch_waits_for_main_osm_and_build_reuses_cache(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import Future

    import app.pipeline.residual_paved as rp

    order: list[str] = []
    calls: list[int] = []
    gate = threading.Event()
    main = Future()  # hlavní OSM ještě neskončil

    def fake_fetch(bbox):
        order.append("dump")
        calls.append(1)
        return [{"type": "way", "id": 1, "tags": {}, "geometry": []}]

    monkeypatch.setattr(rp, "fetch_all_osm_elements", fake_fetch)
    fut = rp.start_residual_osm_prefetch(
        tmp_path, (14.39, 49.99, 14.41, 50.01), after=main
    )
    assert not gate.wait(0.2) and order == []  # čeká na hlavní OSM
    main.set_result([])
    assert len(fut.result(5)) == 1
    # build si vezme cache / hotový výsledek, nestahuje podruhé
    els = rp._load_or_fetch_all_osm(tmp_path, (14.39, 49.99, 14.41, 50.01))
    assert len(els) == 1
    assert len(calls) == 1


def test_residual_prefetch_failure_falls_back_to_normal_fetch(tmp_path, monkeypatch):
    import app.pipeline.residual_paved as rp

    state = {"n": 0}

    def flaky(bbox):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("síť")
        return [{"type": "way", "id": 2, "tags": {}, "geometry": []}]

    monkeypatch.setattr(rp, "fetch_all_osm_elements", flaky)
    fut = rp.start_residual_osm_prefetch(tmp_path, (14.39, 49.99, 14.41, 50.01))
    with pytest.raises(RuntimeError):
        fut.result(5)
    els = rp._load_or_fetch_all_osm(tmp_path, (14.39, 49.99, 14.41, 50.01))
    assert len(els) == 1 and state["n"] == 2


# --- Ověření kusů ortofotem a CHM (residual_verify, Petr 2026-11-02) ---


def _evidence(green_rgb, chm_val, size=100):
    """Syntetický podklad 0,5 m na čtverci (0..50 m): ortofoto jedné barvy a CHM jedné výšky."""
    import numpy as np

    from app.pipeline.residual_verify import Evidence, Raster

    rgb = np.zeros((size, size, 3), dtype=np.uint8)
    rgb[...] = green_rgb
    chm = np.full((size, size), chm_val, dtype=np.float32)
    return Evidence(ortho=Raster(rgb, 0.0, 50.0, 0.5, 0.5), chm=Raster(chm, 0.0, 50.0, 0.5, 0.5))


@pytest.mark.parametrize(
    "rgb, chm, paved",
    [
        ((110, 110, 115), 0.0, True),  # šedý asfalt, nízko → zpevněné
        ((70, 130, 50), 0.0, False),  # trávník
        ((110, 110, 115), 8.0, False),  # stromy / nezmapovaná budova
        ((70, 130, 50), 8.0, False),
    ],
)
def test_piece_verified_by_green_and_height(rgb, chm, paved):
    from shapely.geometry import box

    from app.pipeline.residual_verify import piece_is_paved

    assert piece_is_paved(_evidence(rgb, chm), box(10, 10, 30, 20)) is paved


def test_exg_calibrated_between_roads_and_grass():
    import numpy as np
    from shapely.geometry import box

    from app.pipeline.residual_verify import EXG_DEFAULT, Raster, calibrate_exg

    rgb = np.zeros((100, 100, 3), dtype=np.uint8)
    rgb[:, :50] = (100, 100, 100)  # silnice (x 0..25 m)
    rgb[:, 50:] = (100, 104, 100)  # vybledlý trávník, ExG ~0,03: práh se sníží (zvýšit ho kalibrace nesmí)
    r = Raster(rgb, 0.0, 50.0, 0.5, 0.5)
    thr, _ = calibrate_exg(r, [box(1, 1, 24, 49)], [box(26, 1, 49, 49)])
    assert 0.0 < thr < EXG_DEFAULT
    thr2, note = calibrate_exg(r, [box(1, 1, 2, 2)], [box(26, 1, 49, 49)])  # málo vzorků silnic
    assert thr2 == EXG_DEFAULT and "málo" in note


@pytest.mark.parametrize(
    "dtm_share, complete",
    [(0.9, True), (0.2, False), (0.0, False)],
)
def test_dtm_complete_by_building_cover(dtm_share, complete):
    from shapely.geometry import box

    from app.pipeline.residual_verify import dtm_complete

    subject = box(0, 0, 100, 100)
    osm = [box(10, 10, 30, 30)]  # 400 m²
    dtm = [box(10, 10, 10 + 20 * dtm_share, 30)] if dtm_share else []
    assert dtm_complete(subject, osm, dtm) is complete


def test_sparse_residential_drawn_when_pieces_verified(tmp_path: Path, monkeypatch):
    """S ortofotem a CHM se řídké residential nezahodí celé: šedý (zpevněný) kus se kreslí, zelený ne."""
    from app.pipeline import residual_verify as rv

    residential = {
        "type": "way",
        "id": 1,
        "tags": {"landuse": "residential"},
        "geometry": _sq_wgs(50.10, 14.56, 0.0002, 0.0002),
    }
    tiny = {"type": "way", "id": 2, "tags": {"building": "yes"}, "geometry": _sq_wgs(50.10005, 14.56005, 0.00002, 0.00002)}
    _write_cache(tmp_path, [residential, tiny])
    verdict = {"paved": True}
    monkeypatch.setattr(rv, "load_evidence", lambda *a, **k: rv.Evidence(ortho=object()))
    monkeypatch.setattr(rv, "piece_is_paved", lambda ev, geom: verdict["paved"])
    kw = dict(preset_id="sprint_2m", scale=4000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0, max_piece_m2=float("inf"))
    assert len(build_residual_paved_parts(tmp_path, **kw)) == 1
    verdict["paved"] = False
    assert build_residual_paved_parts(tmp_path, **kw) == []
    assert (tmp_path / _RESIDUAL_BANDS_DIR / f"{RESIDUAL_SPARSE_STEM}.shp").is_file()
