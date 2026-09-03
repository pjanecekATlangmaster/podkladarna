from __future__ import annotations

import json
import zipfile
from pathlib import Path

from app.pipeline.build_oom_map import CRS_PROJ4
from app.pipeline.oom_layers import collect_oom_templates
from app.pipeline.package_oom import (
    build_oom_zip,
    map_scale_from_scalefactor,
    oom_metadata,
    oom_readme,
    prepare_oom_map,
)


def test_map_scale_from_scalefactor():
    assert map_scale_from_scalefactor(0.4) == 4000
    assert map_scale_from_scalefactor(0.75) == 7500
    assert map_scale_from_scalefactor(1.0) == 10000
    assert map_scale_from_scalefactor(1.5) == 15000


def test_oom_metadata_uses_interval_override():
    meta = oom_metadata(
        "forest_10000",
        {"label": "Les", "contour_interval": 5, "scalefactor": 1, "formline": 2},
        {"scalefactor": 1, "contour_interval": 2.5, "formline": 2},
    )
    assert meta["contour_interval_m"] == 2.5
    assert meta["scale"] == 10000


def test_build_oom_zip_layout(tmp_path: Path):
    kp = tmp_path / "work"
    kp.mkdir()
    (kp / "pullautus.png").write_bytes(b"png")
    (kp / "pullautus.pgw").write_text("1\n0\n0\n-1\n0\n0\n", encoding="utf-8")
    (kp / "pullautus_depr.png").write_bytes(b"depr")
    temp = kp / "temp"
    temp.mkdir()
    (temp / "out2.dxf").write_text("contours", encoding="utf-8")
    contours = kp / "contours"
    contours.mkdir()
    (contours / "contours.shp").write_bytes(b"shp")
    (contours / "contours.shx").write_bytes(b"shx")
    (temp / "contours03.dxf").write_text("huge", encoding="utf-8")

    zabaged = tmp_path / "zabaged_clean.zip"
    with zipfile.ZipFile(zabaged, "w") as zf:
        zf.writestr("nested/Cesta.shp", b"shp")
        zf.writestr("nested/Cesta.shx", b"shx")
        zf.writestr("nested/Cesta.dbf", b"dbf")

    dest = tmp_path / "out" / "podkladarna_oom.zip"
    meta = oom_metadata(
        "sprint_2m",
        {"label": "Sprint 1:4000 · 2 m", "contour_interval": 2, "scalefactor": 0.4, "formline": 0},
        {"scalefactor": 0.4, "formline": 0},
        job_name="test-job",
    )
    build_oom_zip(kp, dest, zabaged_clean=zabaged, metadata=meta)
    assert dest.is_file()

    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
    assert "README_OOM.txt" in names
    assert "CO_JE_PODKLADARNA.txt" in names
    assert "metadata.json" in names
    assert "basemap/pullautus.png" in names
    assert "basemap/pullautus.pgw" in names
    assert "relief/pullautus_depr.png" in names
    assert "contours/contours.shp" in names
    assert "karttapullautin/contours.dxf" not in names
    assert "karttapullautin/contours03.dxf" not in names
    assert "vectors/Cesta.shp" in names
    assert "vectors/Cesta.shx" in names
    payload = json.loads(zipfile.ZipFile(dest).read("metadata.json"))
    assert payload["crs"] == "EPSG:5514"
    assert payload["scale"] == 4000
    assert payload["preset_id"] == "sprint_2m"
    readme = zipfile.ZipFile(dest).read("README_OOM.txt").decode("utf-8")
    assert "EPSG:5514" in readme
    assert "1:4000" in readme
    assert "ČÚZK" in readme
    assert "WMS" in readme
    assert "podkladarna.omap" in readme
    assert oom_readme(meta).startswith("Podkladárna")


def test_prepare_oom_map_minimal(tmp_path):
    kp = tmp_path / "work"
    kp.mkdir()
    # 2×2 px PNG – stačí pro projected_center_from_raster
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
    dest = tmp_path / "podkladarna.omap"
    out = prepare_oom_map(
        kp,
        dest,
        map_name="test",
        scale=4000,
        preset_id="sprint_2m",
        bbox_wgs84=(14.4, 50.08, 14.42, 50.09),
        built_refs=None,
    )
    assert out == dest
    xml = dest.read_text(encoding="utf-8")
    assert "basemap/pullautus.png" in xml
    assert "+proj=krovak" in xml
    assert CRS_PROJ4 in xml
    assert "<geographic_crs" in xml
    assert "ref_point_deg" in xml
    assert 'declination="' in xml
    assert 'grivation="' in xml
    assert 'grivation="0.00"' not in xml
    assert "<ref_point x=\"-699999.500000\" y=\"-1050000.500000\"/>" in xml
    assert 'name="pullautus.png"' in xml
    assert 'type="TemplateImage"' in xml
    assert '<symbols count="' in xml
    assert '<line_symbol' in xml
    assert 'parts count="1"' in xml
    assert 'first_front_template="0"' in xml


def test_collect_oom_templates_with_refs(tmp_path):
    kp = tmp_path / "work"
    refs = kp / "references"
    refs.mkdir(parents=True)
    (refs / "hillshade_dmr5g.png").write_bytes(b"x")
    (kp / "pullautus.png").write_bytes(b"x")
    built = {"hillshade": refs / "hillshade_dmr5g.png"}
    templates = collect_oom_templates(kp, built_refs=built)
    assert len(templates) == 2
    assert templates[0].relpath == "references/hillshade_dmr5g.png"
    assert templates[0].visible is False
    assert templates[1].relpath == "basemap/pullautus.png"
    assert templates[1].visible is True
    assert templates[1].opacity == 0.65


def test_collect_oom_templates_includes_hidden_dxf(tmp_path):
    kp = tmp_path / "work"
    kp.mkdir()
    (kp / "pullautus.png").write_bytes(b"x")
    temp = kp / "temp"
    temp.mkdir()
    (temp / "c1g.dxf").write_text("0\nSECTION\n", encoding="utf-8")
    templates = collect_oom_templates(kp, include_dxf_templates=True)
    dxf = [t for t in templates if t.kind == "ogr"]
    assert len(dxf) == 1
    assert dxf[0].relpath == "karttapullautin/cliffs_small.dxf"
    assert dxf[0].visible is False


def test_collect_oom_templates_png_is_control_overlay(tmp_path):
    kp = tmp_path / "work"
    kp.mkdir()
    (kp / "pullautus.png").write_bytes(b"x")
    templates = collect_oom_templates(kp)
    png = [t for t in templates if t.relpath == "basemap/pullautus.png"]
    assert len(png) == 1
    assert png[0].opacity == 0.65
    assert png[0].visible is True
    assert all(t.kind != "ogr" for t in templates)


def test_first_front_puts_kp_above_map():
    from app.pipeline.oom_layers import OomTemplate, first_front_template_index

    templates = [
        OomTemplate("image", "ortho", "references/orthophoto.png", visible=False),
        OomTemplate("image", "kp", "basemap/pullautus.png", visible=True, opacity=0.65),
    ]
    assert first_front_template_index(templates) == 1


def test_oom_map_shows_all_objects_in_one_part(tmp_path):
    from app.pipeline.build_oom_map import write_oom_map
    from app.pipeline.oom_import import OomObjectPart
    from app.pipeline.oom_layers import OomTemplate

    dest = tmp_path / "m.omap"
    dummy = (
        '            <object type="1" symbol="0">\n'
        '                <coords count="2">0 0;1 1;</coords>\n'
        "            </object>"
    )
    write_oom_map(
        dest,
        map_name="t",
        scale=10000,
        ref_x=0,
        ref_y=0,
        ref_lat=50,
        ref_lon=14,
        templates=[OomTemplate("image", "png", "basemap/pullautus.png")],
        preset_id="forest_10000",
        object_parts=[
            OomObjectPart("Vrstevnice", dummy, 1),
            OomObjectPart("OSM pěšiny", dummy, 1),
        ],
    )
    xml = dest.read_text(encoding="utf-8")
    assert 'parts count="1"' in xml
    assert 'part name="Mapa"' in xml
    assert 'objects count="2"' in xml
    assert 'first_front_template="0"' in xml
