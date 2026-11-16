"""Vlastní render obrázku OSM (osm_render): mřížka šablony, pruhy, barvy vrstev, bez dat nic."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

pytest.importorskip("cairo")
pytest.importorskip("shapely")

from app.pipeline import osm_render  # noqa: E402
from app.pipeline.georef import read_pgw  # noqa: E402
from app.pipeline.reference_layers import _write_pgw_for_extent  # noqa: E402

# výřez ~300 × 200 m na Vinohradech (lon/lat), šablona v souřadnicích jobu
BBOX = (14.4370, 50.0740, 14.4412, 50.0758)


def _template(tmp_path: Path) -> tuple[Path, Path]:
    from pyproj import Transformer

    from app.pipeline.crs_5514 import CRS_PROJ4

    tr = Transformer.from_crs("EPSG:4326", CRS_PROJ4, always_xy=True)
    xs, ys = tr.transform([BBOX[0], BBOX[2]] * 2, [BBOX[1], BBOX[1], BBOX[3], BBOX[3]])
    tpl = tmp_path / "job.png"
    w, h = int((max(xs) - min(xs)) / 2), int((max(ys) - min(ys)) / 2)
    Image.new("RGB", (w, h)).save(tpl)
    _write_pgw_for_extent(tpl.with_suffix(".pgw"), min(xs), min(ys), max(xs), max(ys), w, h)
    return tpl, tpl.with_suffix(".pgw")


def _square(lon: float, lat: float, d: float) -> list[dict]:
    pts = [(lon - d, lat - d), (lon + d, lat - d), (lon + d, lat + d), (lon - d, lat + d), (lon - d, lat - d)]
    return [{"lon": x, "lat": y} for x, y in pts]


ELEMENTS = [
    {"type": "way", "tags": {"landuse": "forest"}, "geometry": _square(14.4378, 50.0749, 0.0005)},
    {"type": "way", "tags": {"natural": "water"}, "geometry": _square(14.4404, 50.0749, 0.0004)},
    {"type": "way", "tags": {"building": "yes"}, "geometry": _square(14.4391, 50.0752, 0.0001)},
    {"type": "way", "tags": {"highway": "primary", "name": "Korunní"},
     "geometry": [{"lon": 14.4370, "lat": 50.0744}, {"lon": 14.4412, "lat": 50.0744}]},
]


def _pixel(png: Path, pgw: Path, lon: float, lat: float) -> tuple[int, int, int]:
    from pyproj import Transformer

    from app.pipeline.crs_5514 import CRS_PROJ4

    x, y = Transformer.from_crs("EPSG:4326", CRS_PROJ4, always_xy=True).transform(lon, lat)
    g = read_pgw(pgw)
    col = int((x - (g.origin_x - g.pixel_x / 2)) / g.pixel_x)
    row = int((y - (g.origin_y - g.pixel_y / 2)) / g.pixel_y)
    return Image.open(png).convert("RGB").getpixel((col, row))


@pytest.mark.parametrize(
    "lon, lat, color",
    [
        (14.4378, 50.0749, "#9fca8c"),  # les
        (14.4404, 50.0749, "#aad3df"),  # voda
        (14.4391, 50.0752, "#d9d0c9"),  # budova
        (14.4380, 50.0744, "#fbcb84"),  # silnice primary
        (14.4391, 50.0756, osm_render.BACKGROUND),  # nic
    ],
)
def test_render_layers_in_template_grid(tmp_path, monkeypatch, lon, lat, color):
    monkeypatch.setattr(osm_render, "STRIP_ROWS", 37)  # více pruhů – napojení proudového PNG
    tpl, pgw = _template(tmp_path)
    png, out_pgw = tmp_path / "osm.png", tmp_path / "osm.pgw"
    assert osm_render.render_osm_png(BBOX, tpl, pgw, png, out_pgw, elements=ELEMENTS)
    from app.pipeline.reference_layers import _osm_target_size

    assert Image.open(png).size == _osm_target_size(tpl, pgw)[:2]
    want = tuple(int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    assert max(abs(a - b) for a, b in zip(_pixel(png, out_pgw, lon, lat), want)) <= 3


def test_no_data_no_image(tmp_path, monkeypatch):
    tpl, pgw = _template(tmp_path)
    asked = []
    monkeypatch.setattr(osm_render, "fetch_osm_elements", lambda bbox, **_k: asked.append(bbox))
    assert not osm_render.render_osm_png(BBOX, tpl, pgw, tmp_path / "osm.png", tmp_path / "osm.pgw")
    assert not (tmp_path / "osm.png").exists()
    # dotaz pokryje celý (v lon/lat pootočený) extent šablony, ne jen výřez jobu
    w, s, e, n = asked[0]
    assert w < BBOX[0] and s < BBOX[1] and e > BBOX[2] and n > BBOX[3]


def test_online_osm_only_without_rendered_image(tmp_path):
    from app.pipeline.run_job import _drop_online_osm, _online_only_keys

    assert "osm" in _online_only_keys({})
    assert "osm" not in _online_only_keys({"osm": tmp_path / "osm.png"})
    for name in ("osm.png", "osm.vrt", "osm_stredni.vrt", "osm_dlazdice.xml"):
        (tmp_path / name).write_text("osm_dlazdice.xml" if name.endswith(".vrt") else "x", encoding="utf-8")
    built = {"osm": tmp_path / "osm.png", "online_osm": tmp_path / "osm.vrt",
             "online_osm_stredni": tmp_path / "osm_stredni.vrt"}
    assert set(_drop_online_osm(built)) == {"osm"}
    assert not any((tmp_path / n).exists() for n in ("osm.vrt", "osm_stredni.vrt", "osm_dlazdice.xml"))
