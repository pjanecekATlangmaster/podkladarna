"""Vegetace do auto .omap: CHM / hustota, ne ZABAGED louky."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.pipeline.oom_import import OomObjectPart
from app.pipeline.package_oom import _ZABAGED_UNDER_VEGETATION, prepare_oom_map


def test_zabaged_under_vegetation_layers():
    assert "TrvalyTravniPorost" in _ZABAGED_UNDER_VEGETATION
    assert "UdrzovanaZelen" in _ZABAGED_UNDER_VEGETATION


def test_omits_zabaged_meadows_from_auto_omap(tmp_path: Path):
    """Žádné ZABAGED louky v auto .omap; vegetace jen z LiDARu."""
    kp = tmp_path / "work"
    kp.mkdir()
    zabaged = tmp_path / "zabaged_clean.zip"
    zabaged.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    meadow = OomObjectPart(
        name="ZABAGED – TrvalyTravniPorost",
        objects_xml="<object/>",
        count=1,
    )
    path_part = OomObjectPart(
        name="ZABAGED – Cesta",
        objects_xml="<object/>",
        count=1,
    )
    chm_vege = OomObjectPart(
        name="Otevřený terén (CHM)",
        objects_xml="<object symbol=401/>",
        count=2,
    )

    with (
        patch(
            "app.pipeline.package_oom.build_zabaged_object_parts",
            return_value=[meadow, path_part],
        ) as zab_mock,
        patch(
            "app.pipeline.package_oom.build_vegetation_parts",
            return_value=[chm_vege],
        ) as vege_mock,
        patch(
            "app.pipeline.package_oom.build_gdal_contour_parts",
            return_value=[],
        ),
        patch(
            "app.pipeline.package_oom.build_osm_feature_parts",
            return_value=[],
        ),
        patch(
            "app.pipeline.package_oom.build_osm_path_parts",
            return_value=[],
        ),
        patch(
            "app.pipeline.package_oom.build_aoi_boundary_part",
            return_value=None,
        ),
        patch(
            "app.pipeline.package_oom.write_oom_map",
            side_effect=lambda dest, **kw: dest,
        ) as write_mock,
    ):
        out = prepare_oom_map(
            kp,
            tmp_path / "out.omap",
            map_name="out",
            scale=10000,
            preset_id="forest_10000",
            bbox_wgs84=(14.4, 50.08, 14.42, 50.09),
            zabaged_clean=zabaged,
        )

    assert out is not None
    omit = zab_mock.call_args.kwargs.get("omit_layers") or set()
    assert "TrvalyTravniPorost" in omit
    assert "UdrzovanaZelen" in omit
    vege_mock.assert_called_once()

    parts = write_mock.call_args.kwargs["object_parts"]
    names = [p.name for p in parts]
    assert "ZABAGED – TrvalyTravniPorost" not in names
    assert any("CHM" in n or "Otevřený" in n or "terén" in n for n in names)


def test_vector_sources_clipped_near_aoi(tmp_path: Path):
    """OSM/ZABAGED/AOPK do .omap jen ~1 km za AOI (VVN z OSM měla 155 km → Mapper OOM)."""
    from app.pipeline.package_oom import VECTOR_CLIP_MARGIN_M
    from app.pipeline.fetch_openzu import crop_bounds_5514

    kp = tmp_path / "work"
    kp.mkdir()
    zabaged = tmp_path / "zabaged_clean.zip"
    zabaged.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    bbox = (14.4, 50.08, 14.42, 50.09)
    with (
        patch("app.pipeline.package_oom.build_zabaged_object_parts", return_value=[]) as zab,
        patch("app.pipeline.package_oom.build_vegetation_parts", return_value=[]),
        patch("app.pipeline.package_oom.build_gdal_contour_parts", return_value=[]),
        patch("app.pipeline.package_oom.build_osm_feature_parts", return_value=[]) as feat,
        patch("app.pipeline.package_oom.build_osm_path_parts", return_value=[]) as paths,
        patch("app.pipeline.package_oom.build_aoi_boundary_part", return_value=None),
        patch("app.pipeline.package_oom.write_oom_map", side_effect=lambda dest, **kw: dest),
    ):
        prepare_oom_map(
            kp,
            tmp_path / "out.omap",
            map_name="out",
            scale=10000,
            preset_id="forest_10000",
            bbox_wgs84=bbox,
            zabaged_clean=zabaged,
        )
    xmin, ymin, xmax, ymax = crop_bounds_5514(*bbox, buffer_m=0.0)
    for mock in (zab, feat, paths):
        cb = mock.call_args.kwargs["clip_bounds"]
        assert cb is not None
        assert abs(cb[0] - (xmin - VECTOR_CLIP_MARGIN_M)) < 1e-6
        assert abs(cb[3] - (ymax + VECTOR_CLIP_MARGIN_M)) < 1e-6
