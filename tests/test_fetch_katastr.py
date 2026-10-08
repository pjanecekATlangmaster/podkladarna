"""Vektorový katastr (KM-KU-DXF) jako podklad."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

import app.pipeline.fetch_katastr as fk


def _dxf_with_line_and_point(x0: float, y0: float) -> str:
    """Minimální ASCII DXF: jedna LINE (vrstva 21900) a jeden POINT (18)."""
    return "\n".join(
        [
            "0", "SECTION", "2", "ENTITIES",
            "0", "LINE", "8", "21900",
            "10", f"{x0}", "20", f"{y0}", "30", "0",
            "11", f"{x0 + 100}", "21", f"{y0 + 50}", "31", "0",
            "0", "POINT", "8", "18",
            "10", f"{x0 + 10}", "20", f"{y0 + 10}", "30", "0",
            "0", "ENDSEC", "0", "EOF", "",
        ]
    )


def _zip_bytes(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, text)
    return buf.getvalue()


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path / "cache")
    return tmp_path


def test_ku_codes_for_bbox_dedups_and_sorts(monkeypatch):
    monkeypatch.setattr(
        fk,
        "query_layer_geojson",
        lambda *_a, **_k: {
            "features": [
                {"properties": {"kod": 750590}},
                {"properties": {"kod": "728837"}},
                {"properties": {"kod": 750590}},
                {"properties": {"kod": None}},
            ]
        },
    )
    assert fk.ku_codes_for_bbox((14.3, 50.0, 14.4, 50.1)) == [728837, 750590]


def test_fetch_ku_dxf_downloads_once_then_uses_cache(cache_root, monkeypatch):
    calls: list[str] = []

    def fake_download(url: str, dest: Path) -> None:
        calls.append(url)
        dest.write_bytes(_zip_bytes("750573.dxf", _dxf_with_line_and_point(-748000, -1048000)))

    monkeypatch.setattr(fk, "_http_download", fake_download)
    first = fk.fetch_ku_dxf(750573)
    second = fk.fetch_ku_dxf(750573)
    assert first == second and first is not None and first.is_file()
    assert calls == ["https://services.cuzk.cz/dxf/ku/750573.zip"]
    assert not (first.parent / "750573.zip").exists()


def test_fetch_ku_dxf_bad_zip_is_skipped(cache_root, monkeypatch):
    monkeypatch.setattr(fk, "_http_download", lambda _u, dest: dest.write_bytes(b"not a zip"))
    logs: list[str] = []
    assert fk.fetch_ku_dxf(1, log=logs.append) is None
    assert logs and "přeskočeno" in logs[0]


def test_build_katastr_vectors_lines_only_in_5514(cache_root, monkeypatch):
    try:
        fk.find_tool("ogr2ogr")
    except RuntimeError:
        pytest.skip("ogr2ogr není k dispozici")
    pyogrio = pytest.importorskip("pyogrio")

    bbox = (14.35, 50.03, 14.36, 50.04)
    xmin, ymin, _xmax, _ymax = fk.crop_bounds_5514(*bbox, buffer_m=0.0)
    monkeypatch.setattr(fk, "ku_codes_for_bbox", lambda _b: [750573])
    monkeypatch.setattr(
        fk,
        "_http_download",
        lambda _u, dest: dest.write_bytes(
            _zip_bytes("750573.dxf", _dxf_with_line_and_point(xmin + 50, ymin + 50))
        ),
    )
    built = fk.build_katastr_vectors(bbox, cache_root / "refs")
    gpkg = built["katastr_vector"]
    assert gpkg.name == "katastr.gpkg" and built["katastr_dxf"].is_file()
    info = pyogrio.read_info(gpkg)
    assert info["features"] == 1  # jen linie, bod značky vynechán
    assert "5514" in (info["crs"] or "")


def test_katastr_vector_is_hidden_ogr_template(tmp_path):
    """Katastr v .omap = vektorový podklad (OgrTemplate), ne objekty mapy."""
    from app.pipeline.build_oom_map import _template_xml
    from app.pipeline.oom_layers import collect_oom_templates

    gpkg = tmp_path / "katastr.gpkg"
    gpkg.write_bytes(b"x")
    templates = collect_oom_templates(tmp_path, built_refs={"katastr_vector": gpkg})
    km = [t for t in templates if t.relpath == "references/katastr.gpkg"]
    assert len(km) == 1 and km[0].kind == "ogr" and km[0].visible is False
    xml = _template_xml(km[0])
    assert 'type="OgrTemplate"' in xml and 'georef="true"' in xml
