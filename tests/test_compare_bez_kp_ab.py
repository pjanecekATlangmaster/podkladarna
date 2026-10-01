"""Tests for scripts/compare_bez_kp_ab.py A/B harness helpers."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.pipeline.cliffs_dem import write_cliff_ticks_dxf
from app.pipeline.preview import write_solid_gray_png
from scripts.compare_bez_kp_ab import (
    cliff_file_stats,
    cliffs_stats,
    compare_ab,
    find_cliff_paths,
    find_preview_assets,
    find_vegetation_shp,
    format_report,
    parse_dxf_line_segments,
    presence_matrix,
    preview_stats,
    relative_diff,
    resolve_side_root,
    segment_length_m,
    summarize_side,
)


def test_relative_diff_basic():
    assert relative_diff(100.0, 110.0) == pytest.approx(0.1)
    assert relative_diff(0.0, 0.0) is None
    assert relative_diff(0.0, 5.0) == float("inf")
    assert math.isinf(relative_diff(0.0, -1.0) or 0.0)


def test_segment_length_m():
    assert segment_length_m((0.0, 0.0), (3.0, 4.0)) == pytest.approx(5.0)


def test_parse_dxf_line_segments(tmp_path: Path):
    ticks = [
        ((0.0, 0.0), (10.0, 0.0)),
        ((0.0, 0.0), (0.0, 5.0)),
    ]
    dxf = write_cliff_ticks_dxf(ticks, tmp_path / "c2g.dxf")
    assert dxf is not None
    segs = parse_dxf_line_segments(dxf)
    assert len(segs) == 2
    assert segs[0] == ((0.0, 0.0), (10.0, 0.0))
    stats = cliff_file_stats(dxf)
    assert stats["present"] is True
    assert stats["segment_count"] == 2
    assert stats["total_length_m"] == pytest.approx(15.0)


def test_find_artifacts_in_zip_layout(tmp_path: Path):
    base = tmp_path / "base"
    base.mkdir()
    vege = base / "vegetation.shp"
    vege.write_bytes(b"not-a-real-shp")
    (base / "cliffs_small.dxf").write_text("0\nEOF\n", encoding="utf-8")
    preview_dir = tmp_path / "preview"
    preview_dir.mkdir()
    write_solid_gray_png(preview_dir / "preview.png", 8, 6, gray=120)
    kp = tmp_path / "kp"
    kp.mkdir()
    write_solid_gray_png(kp / "pullautus.png", 8, 6, gray=90)

    assert find_vegetation_shp(tmp_path) == vege
    cliffs = find_cliff_paths(tmp_path)
    assert cliffs["small"] is not None
    assert cliffs["large"] is None
    assets = find_preview_assets(tmp_path)
    assert assets["preview"] is not None
    assert assets["pullautus"] is not None
    presence = presence_matrix(tmp_path)
    assert presence["vegetation.shp"] is True
    assert presence["preview.png"] is True
    assert presence["cliffs_large.dxf"] is False


def test_resolve_side_root_prefers_output(tmp_path: Path):
    job = tmp_path / "job"
    out = job / "output"
    out.mkdir(parents=True)
    (out / "base").mkdir()
    (out / "base" / "vegetation.shp").write_bytes(b"x")
    assert resolve_side_root(job) == out.resolve()
    assert resolve_side_root(out) == out.resolve()


def test_preview_stats_dims(tmp_path: Path):
    write_solid_gray_png(tmp_path / "preview.png", 16, 12, gray=100)
    stats = preview_stats(tmp_path)
    assert stats["preview_present"] is True
    assert stats["preferred"]["width"] == 16
    assert stats["preferred"]["height"] == 12


def test_cliffs_stats_from_temp(tmp_path: Path):
    temp = tmp_path / "temp"
    temp.mkdir()
    write_cliff_ticks_dxf(
        [((1.0, 1.0), (4.0, 5.0))],  # length 5
        temp / "c2g.dxf",
    )
    write_cliff_ticks_dxf(
        [((0.0, 0.0), (2.0, 0.0)), ((0.0, 0.0), (0.0, 2.0))],  # 2+2
        temp / "c3g.dxf",
    )
    stats = cliffs_stats(tmp_path)
    assert stats["any_present"] is True
    assert stats["segment_count"] == 3
    assert stats["total_length_m"] == pytest.approx(9.0)


def test_compare_ab_report(tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    for root, use_kp, n_ticks, gray in (
        (a, True, 4, 80),
        (b, False, 2, 160),
    ):
        root.mkdir()
        (root / "metadata.json").write_text(
            json.dumps({"use_kp": use_kp, "name": root.name, "app_version": "1.17.1"}),
            encoding="utf-8",
        )
        base = root / "base"
        base.mkdir()
        ticks = [((float(i), 0.0), (float(i) + 1.0, 0.0)) for i in range(n_ticks)]
        write_cliff_ticks_dxf(ticks, base / "cliffs_small.dxf")
        if use_kp:
            kp = root / "kp"
            kp.mkdir()
            write_solid_gray_png(kp / "pullautus.png", 10, 10, gray=gray)
        else:
            prev = root / "preview"
            prev.mkdir()
            write_solid_gray_png(prev / "preview.png", 10, 10, gray=gray)

    report = compare_ab(a, b, label_a="KP", label_b="bez-KP")
    assert report["a"]["use_kp"] is True
    assert report["b"]["use_kp"] is False
    assert report["diffs"]["cliffs_segment_count"]["a"] == 4
    assert report["diffs"]["cliffs_segment_count"]["b"] == 2
    assert report["diffs"]["cliffs_segment_count"]["delta"] == -2
    assert "preview.png" in report["presence_mismatch"]
    text = format_report(report)
    assert "A/B: KP vs bez-KP" in text
    assert "Presence" in text
    assert "Cliffs" in text


def test_vegetation_stats_from_shp(tmp_path: Path):
    import numpy as np
    import pyogrio.raw as pyogrio_raw
    from shapely import wkb as shapely_wkb
    from shapely.geometry import box

    from scripts.compare_bez_kp_ab import vegetation_stats

    g1 = box(0, 0, 10, 10)  # 100 m²
    g2 = box(0, 0, 5, 4)  # 20 m²
    geoms = np.array(
        [shapely_wkb.dumps(g1), shapely_wkb.dumps(g2)],
        dtype=object,
    )
    shp = tmp_path / "vegetation.shp"
    pyogrio_raw.write(
        str(shp),
        geoms,
        [np.array([1, 2], dtype=np.int32), np.array(["401", "406"])],
        ["cls", "code"],
        layer="vegetation",
        driver="ESRI Shapefile",
        geometry_type="Polygon",
        crs="EPSG:5514",
    )
    stats = vegetation_stats(shp)
    assert stats["present"] is True
    assert stats["feature_count"] == 2
    assert stats["area_m2_total"] == pytest.approx(120.0)
    assert stats["area_by_code"]["401"] == pytest.approx(100.0)
    assert stats["area_by_code"]["406"] == pytest.approx(20.0)
    assert stats["count_by_cls"]["1"] == 1
