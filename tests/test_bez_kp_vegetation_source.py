"""Bez-KP: vegetace z CHM, ne ze ZABAGED luk."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from app.pipeline.oom_import import OomObjectPart
from app.pipeline.package_oom import (
    _ZABAGED_UNDER_VEGETATION,
    prepare_oom_map,
)


def _mini_png_pgw(kp: Path) -> None:
    mini_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x02\x00\x00\x00\x02"
        b"\x08\x02\x00\x00\x00\xfd\xd4\x9a\x73\x00\x00\x00\x12IDATx\x9cc\x60\x60"
        b"\x60\x00\x00\x00\x04\x00\x01\x5c\xcd\xff\x69\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    (kp / "pullautus.png").write_bytes(mini_png)
    (kp / "pullautus.pgw").write_text(
        "0.5\n0\n0\n-0.5\n-700000\n-1050000\n",
        encoding="utf-8",
    )


def test_zabaged_under_vegetation_layers_are_meadows():
    assert "TrvalyTravniPorost" in _ZABAGED_UNDER_VEGETATION
    assert "UdrzovanaZelen" in _ZABAGED_UNDER_VEGETATION


def test_bez_kp_omits_zabaged_meadows_and_skips_open_land_subtract(tmp_path: Path):
    """use_kp=false → žádné ZABAGED louky v auto .omap; žádný open_land_subtract."""
    kp = tmp_path / "work"
    kp.mkdir()
    _mini_png_pgw(kp)
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
            "app.pipeline.package_oom.collect_kp401_subtract_wkbs",
            return_value=[b"fake-wkb"],
        ) as sub_mock,
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
            map_name="bez-kp",
            scale=10000,
            preset_id="forest_10000",
            bbox_wgs84=(14.4, 50.08, 14.42, 50.09),
            zabaged_clean=zabaged,
            use_kp=False,
        )

    assert out is not None
    # Louky předány do omit_layers – build_zabaged je už nevrací; i kdyby
    # mock vrátil meadow, nesmí skončit v object_parts.
    omit = zab_mock.call_args.kwargs.get("omit_layers") or set()
    assert "TrvalyTravniPorost" in omit
    assert "UdrzovanaZelen" in omit
    sub_mock.assert_not_called()
    vege_mock.assert_called_once()
    assert vege_mock.call_args.kwargs.get("subtract_wkbs") is None

    parts = write_mock.call_args.kwargs["object_parts"]
    names = [p.name for p in parts]
    assert "ZABAGED – TrvalyTravniPorost" not in names
    assert any("CHM" in n or "Otevřený" in n or "terén" in n for n in names)


def test_with_kp_still_subtracts_and_keeps_meadow_underlay(tmp_path: Path):
    kp = tmp_path / "work"
    kp.mkdir()
    _mini_png_pgw(kp)
    zabaged = tmp_path / "zabaged_clean.zip"
    zabaged.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    meadow = OomObjectPart(
        name="ZABAGED – TrvalyTravniPorost",
        objects_xml="<object/>",
        count=1,
    )

    with (
        patch(
            "app.pipeline.package_oom.build_zabaged_object_parts",
            return_value=[meadow],
        ) as zab_mock,
        patch(
            "app.pipeline.package_oom.collect_kp401_subtract_wkbs",
            return_value=[b"wkb"],
        ) as sub_mock,
        patch(
            "app.pipeline.package_oom.build_vegetation_parts",
            return_value=[],
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
        prepare_oom_map(
            kp,
            tmp_path / "kp.omap",
            map_name="kp",
            scale=10000,
            preset_id="forest_10000",
            bbox_wgs84=(14.4, 50.08, 14.42, 50.09),
            zabaged_clean=zabaged,
            use_kp=True,
        )

    omit = zab_mock.call_args.kwargs.get("omit_layers") or set()
    assert "TrvalyTravniPorost" not in omit
    sub_mock.assert_called_once()
    assert vege_mock.call_args.kwargs.get("subtract_wkbs") == [b"wkb"]
    names = [p.name for p in write_mock.call_args.kwargs["object_parts"]]
    assert "ZABAGED – TrvalyTravniPorost" in names
