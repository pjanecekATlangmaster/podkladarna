from __future__ import annotations

from pathlib import Path

from app.pipeline.build_oom_map import build_oom_map_xml, write_oom_map
from app.pipeline.oom_layers import OomTemplate
from app.pipeline.georef import PgwGeoref, read_pgw
from app.pipeline.reference_layers import (
    HILLSHADE_VARIANTS,
    _pick_osm_zoom,
    _write_osm_vrt,
    reference_metadata,
)


def test_reference_metadata():
    meta = reference_metadata()
    assert meta["hillshade_source"] == "ČÚZK DMR 5G WMS"
    assert meta["hillshade_tool"] == "WMS ImageServer"
    assert len(meta["hillshade_variants"]) == len(HILLSHADE_VARIANTS)
    assert "osm.png" in meta["map_layers"]
    assert "katastr.png" in meta["map_layers"]
    assert meta["dmpok_preview"] == "dmpok_nahled.png"


def test_pick_osm_zoom_small_bbox():
    z = _pick_osm_zoom(14.4, 50.08, 14.42, 50.09)
    assert 12 <= z <= 19


def test_osm_target_size_finer_than_template(tmp_path):
    from app.pipeline.georef import PgwGeoref
    from app.pipeline.reference_layers import _osm_target_size

    # Hrubá KP šablona (200×200 px na ~1 km) → OSM má být jemnější.
    mini_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\xc8\x00\x00\x00\xc8"
        b"\x08\x02\x00\x00\x00\xa2\x8c\x5a\x8e\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
        b"\x00\x01\x01\x01\x00\x18\xdd\x8d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    # Fix: use valid small PNG then fake extent via pgw only – size from PNG IHDR.
    # Use 4x4 PNG with pgw spanning 1000m so template is tiny vs ground.
    mini_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x04\x00\x00\x00\x04"
        b"\x08\x02\x00\x00\x00\x26\x93\x09\x29\x00\x00\x00\x12IDATx\x9cc\x60\x60"
        b"\x60\x00\x00\x00\x04\x00\x01\x5c\xcd\xff\x69\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    png = tmp_path / "t.png"
    png.write_bytes(mini_png)
    pgw = tmp_path / "t.pgw"
    # 4 px across 1000 m → template 4×4; OSM should request much larger.
    PgwGeoref(250.0, 0.0, 0.0, -250.0, 0.0, 1000.0).write(pgw)
    tw, th, mpp = _osm_target_size(png, pgw)
    assert tw >= 100 and th >= 100
    assert mpp <= 2.0
    assert max(tw, th) <= 8192


def test_ref_target_size_finer_than_kp_template(tmp_path):
    from app.pipeline.georef import PgwGeoref
    from app.pipeline.reference_layers import _ref_target_size, _osm_target_size

    mini_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x04\x00\x00\x00\x04"
        b"\x08\x02\x00\x00\x00\x26\x93\x09\x29\x00\x00\x00\x12IDATx\x9cc\x60\x60"
        b"\x60\x00\x00\x00\x04\x00\x01\x5c\xcd\xff\x69\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    png = tmp_path / "t.png"
    png.write_bytes(mini_png)
    pgw = tmp_path / "t.pgw"
    # 4 px / 1000 m = KP ~250 m/px. Orto má jít na 0,25 m/px.
    PgwGeoref(250.0, 0.0, 0.0, -250.0, 0.0, 1000.0).write(pgw)
    tw, th, mpp = _ref_target_size(png, pgw)
    osm_tw, osm_th, osm_mpp = _osm_target_size(png, pgw)
    assert mpp <= 0.25
    assert tw >= osm_tw and th >= osm_th
    assert mpp <= osm_mpp
    assert max(tw, th) <= 8192
    assert tw == 4000 and th == 4000


def test_split_pixel_grid_stays_under_wms_limit():
    from app.pipeline.reference_layers import WMS_MAX_GETMAP_PX, _split_pixel_grid

    assert _split_pixel_grid(4000, 3000) == [(0, 0, 4000, 3000)]
    tiles = _split_pixel_grid(6000, 3000)
    assert tiles == [(0, 0, 4000, 3000), (4000, 0, 6000, 3000)]
    for x0, y0, x1, y1 in tiles:
        assert x1 - x0 <= WMS_MAX_GETMAP_PX
        assert y1 - y0 <= WMS_MAX_GETMAP_PX


def test_pixel_window_bounds_top_left_is_north():
    from app.pipeline.reference_layers import _pixel_window_bounds

    bounds = (0.0, 0.0, 100.0, 50.0)
    xmin, ymin, xmax, ymax = _pixel_window_bounds(bounds, 100, 50, (0, 0, 40, 10))
    assert abs(xmin - 0.0) < 1e-9
    assert abs(xmax - 40.0) < 1e-9
    assert abs(ymax - 50.0) < 1e-9
    assert abs(ymin - 40.0) < 1e-9


def test_write_osm_vrt(tmp_path):
    tile = tmp_path / "t.png"
    tile.write_bytes(b"x")
    vrt = tmp_path / "m.vrt"
    _write_osm_vrt([(tile, 10, 20)], 15, 10, 20, vrt)
    text = vrt.read_text(encoding="utf-8")
    assert "EPSG:3857" in text
    assert "SimpleSource" in text
    assert 'band="3"' in text
    assert "<ColorInterp>Red</ColorInterp>" in text
    assert text.count("<SourceBand>") == 3


def test_pgw_roundtrip(tmp_path):
    pgw = tmp_path / "a.pgw"
    PgwGeoref(0.5, 0.0, 0.0, -0.5, 100.0, 200.0).write(pgw)
    got = read_pgw(pgw)
    assert got.pixel_x == 0.5
    assert got.origin_x == 100.0


def test_osm_pgw_path(tmp_path):
    out_dir = tmp_path / "references"
    out_dir.mkdir()
    osm_pgw = (out_dir / "osm.png").with_suffix(".pgw")
    assert osm_pgw.name == "osm.pgw"


def test_build_oom_map_xml_contains_templates():
    xml = build_oom_map_xml(
        map_name="Test",
        scale=10000,
        ref_x=500000.0,
        ref_y=1200000.0,
        ref_lat=50.0,
        ref_lon=14.5,
        declination=5.15,
        grivation=13.02,
        preset_id="forest_10000",
        templates=[
            OomTemplate("image", "Ortofoto", "references/orthophoto.png"),
            OomTemplate("image", "OSM", "references/osm.png"),
        ],
    )
    assert "+proj=krovak" in xml
    assert "<parameter>5514</parameter>" in xml
    assert "references/orthophoto.png" in xml
    assert "references/osm.png" in xml
    assert 'scale="10000"' in xml
    assert "ref_point_deg" in xml
    assert 'declination="5.15"' in xml
    assert 'grivation="13.02"' in xml
    assert '<symbols count="' in xml
    assert 'code="101"' in xml


def test_write_oom_map_file(tmp_path):
    dest = tmp_path / "podkladarna.omap"
    write_oom_map(
        dest,
        map_name="Šance",
        scale=4000,
        ref_x=1.0,
        ref_y=2.0,
        ref_lat=50.0,
        ref_lon=14.5,
        preset_id="sprint_2m",
        templates=[OomTemplate("image", "OSM", "references/osm.png")],
    )
    assert dest.is_file()
    assert "Šance" in dest.read_text(encoding="utf-8")


# --- Průhledný katastr -------------------------------------------------------

_MINI_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x04\x00\x00\x00\x04"
    b"\x08\x02\x00\x00\x00\x26\x93\x09\x29\x00\x00\x00\x12IDATx\x9cc\x60\x60"
    b"\x60\x00\x00\x00\x04\x00\x01\x5c\xcd\xff\x69\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _mini_template(tmp_path: Path, extent_m: float) -> tuple[Path, Path]:
    png = tmp_path / "tpl.png"
    png.write_bytes(_MINI_PNG)
    pgw = tmp_path / "tpl.pgw"
    # 4×4 px šablona pokrývá extent_m × extent_m (levý horní roh 0, extent_m).
    PgwGeoref(extent_m / 4, 0.0, 0.0, -extent_m / 4, 0.0, extent_m).write(pgw)
    return png, pgw


def _noisy_image(mode: str, size=(120, 120), *, white_bg: bool = True):
    import random

    from PIL import Image

    rnd = random.Random(7)
    bg = (255, 255, 255, 255) if mode == "RGBA" else (255, 255, 255)
    im = Image.new(mode, size, bg)
    px = im.load()
    for _ in range(1500):
        x, y = rnd.randrange(size[0]), rnd.randrange(size[1])
        v = rnd.randrange(0, 120)
        px[x, y] = (v, v, v, 255) if mode == "RGBA" else (v, v, v)
    return im


def test_wms_getmap_url_transparent_flag():
    from app.pipeline.reference_layers import _wms_getmap_url

    url = _wms_getmap_url("http://x/wms", "KN", 0, 0, 10, 10, 100, 100, "image/png")
    assert "transparent=false" in url
    url = _wms_getmap_url(
        "http://x/wms", "KN", 0, 0, 10, 10, 100, 100, "image/png", transparent=True
    )
    assert "transparent=true" in url
    assert "format=image%2Fpng" in url
    assert "crs=EPSG%3A5514" in url


def test_ensure_png_alpha_fallback_white_to_transparent(tmp_path):
    from PIL import Image

    from app.pipeline.reference_layers import ensure_png_alpha

    png = tmp_path / "k.png"
    _noisy_image("RGB").save(png)
    assert ensure_png_alpha(png) == "fallback"
    out = Image.open(png)
    assert out.mode == "RGBA"
    assert out.getpixel((0, 0))[3] == 0 or out.getextrema()[3][0] == 0
    # tmavé čáry zůstávají krycí
    alphas = {p[3] for p in out.getdata() if p[0] < 120}
    assert alphas == {255}
    # skoro bílá (≥250) → průhledná, světle šedá 200 → krycí
    im = Image.new("RGB", (4, 4), (252, 251, 250))
    im.putpixel((1, 1), (200, 200, 200))
    im.save(png)
    ensure_png_alpha(png)
    out = Image.open(png)
    assert out.getpixel((0, 0))[3] == 0
    assert out.getpixel((1, 1))[3] == 255


def test_ensure_png_alpha_keeps_server_alpha(tmp_path):
    from PIL import Image

    from app.pipeline.reference_layers import ensure_png_alpha

    png = tmp_path / "k.png"
    im = Image.new("RGBA", (4, 4), (255, 255, 255, 0))
    im.putpixel((2, 2), (255, 255, 255, 255))  # bílá, ale krycí – nesmí zprůhlednět
    im.save(png)
    assert ensure_png_alpha(png) == "server"
    out = Image.open(png)
    assert out.mode == "RGBA"
    assert out.getpixel((0, 0))[3] == 0
    assert out.getpixel((2, 2))[3] == 255


def test_ensure_png_alpha_palette_with_transparency(tmp_path):
    from PIL import Image

    from app.pipeline.reference_layers import ensure_png_alpha

    png = tmp_path / "k.png"
    im = Image.new("P", (8, 8), 0)
    im.putpalette([255, 255, 255, 0, 0, 0] + [0] * 250 * 3)
    im.putpixel((3, 3), 1)
    im.save(png, transparency=0)  # index 0 = průhledný
    assert ensure_png_alpha(png) == "server"
    out = Image.open(png)
    assert out.mode == "RGBA"
    assert out.getpixel((0, 0))[3] == 0
    assert out.getpixel((3, 3)) == (0, 0, 0, 255)


def test_fetch_cuzk_wms_png_transparent_requests_alpha_and_fallback(tmp_path, monkeypatch):
    import io

    from PIL import Image

    from app.pipeline import reference_layers as rl

    png, pgw = _mini_template(tmp_path, 100.0)  # 100 m / 0,25 = 400 px, jeden GetMap
    buf = io.BytesIO()
    _noisy_image("RGB").save(buf, "PNG")  # server TRANSPARENT ignoruje: bílé pozadí
    urls: list[str] = []

    def fake_get(url, timeout=120):
        urls.append(url)
        return buf.getvalue()

    monkeypatch.setattr(rl, "_http_get_bytes", fake_get)
    dest = tmp_path / "katastr.png"
    ok = rl.fetch_cuzk_wms_png(
        rl.KM_WMS,
        rl.KM_LAYER,
        (0.0, 0.0, 100.0, 100.0),
        png,
        pgw,
        dest,
        dest.with_suffix(".pgw"),
        label="Katastr",
        transparent=True,
    )
    assert ok
    assert len(urls) == 1
    assert "transparent=true" in urls[0]
    assert "layers=KN" in urls[0]
    out = Image.open(dest)
    assert out.mode == "RGBA"
    assert out.getextrema()[3][0] == 0
    assert dest.with_suffix(".pgw").is_file()


def test_fetch_cuzk_wms_png_default_stays_opaque(tmp_path, monkeypatch):
    import io

    from PIL import Image

    from app.pipeline import reference_layers as rl

    png, pgw = _mini_template(tmp_path, 100.0)
    buf = io.BytesIO()
    _noisy_image("RGB").save(buf, "PNG")
    urls: list[str] = []
    monkeypatch.setattr(
        rl, "_http_get_bytes", lambda url, timeout=120: (urls.append(url), buf.getvalue())[1]
    )
    dest = tmp_path / "mapa_ztm.png"
    assert rl.fetch_cuzk_wms_png(
        rl.ZTM_WMS, "0", (0.0, 0.0, 100.0, 100.0), png, pgw, dest,
        dest.with_suffix(".pgw"), label="ZTM",
    )
    assert "transparent=false" in urls[0]
    assert Image.open(dest).mode == "RGB"


def test_mosaic_wms_tiles_keeps_alpha(tmp_path):
    """gdalwarp mozaika průhledných dlaždic zachová alfa (-dstalpha)."""
    import pytest
    from PIL import Image

    from app.pipeline import reference_layers as rl

    try:
        rl._gdal_tool("gdalwarp")
    except RuntimeError:
        pytest.skip("gdalwarp není k dispozici")
    X0, Y0 = -743000.0, -1043000.0  # Praha (S-JTSK; (0,0) není v Křováku platné)
    import random

    rnd = random.Random(3)
    tiles = []
    for i, x0 in enumerate((0, 120)):
        # paletové PNG s průhlednou barvou 0 – tak je posílá ČÚZK KN
        im = Image.new("P", (120, 120), 0)
        im.putpalette([255, 255, 255] + [0, 0, 0] * 255)
        for _ in range(1500):
            im.putpixel((rnd.randrange(120), rnd.randrange(120)), 1)
        im.putpixel((10, 10), 1)
        p = tmp_path / f"t{i}.png"
        im.save(p, transparency=0)
        rl._write_pgw_for_extent(
            p.with_suffix(".pgw"), X0 + x0, Y0, X0 + x0 + 120, Y0 + 120, 120, 120
        )
        tiles.append((p, x0, 0, x0 + 120, 120))
    dest = tmp_path / "m.png"
    ok = rl._mosaic_wms_tiles(
        tiles, (X0, Y0, X0 + 240.0, Y0 + 120.0), dest, dest.with_suffix(".pgw"), 240, 120,
        transparent=True,
    )
    assert ok
    out = Image.open(dest)
    assert out.mode == "RGBA"
    assert out.getextrema()[3] == (0, 255)
    # (world file = roh pixelu → GDAL může posunout o půl pixelu; ověřuj okolí)
    left = out.crop((0, 0, 120, 120)).getextrema()[3]
    right = out.crop((120, 0, 240, 120)).getextrema()[3]
    assert left == (0, 255) and right == (0, 255)


# --- Ortofoto 0,25 m/px, dlaždice, JPEG --------------------------------------


def test_ortho_target_size_is_quarter_meter_for_big_aoi(tmp_path):
    from app.pipeline.reference_layers import _ref_target_size, _ortho_target_size

    png, pgw = _mini_template(tmp_path, 6000.0)  # 6×6 km
    tw, th, mpp = _ortho_target_size(png, pgw)
    assert (tw, th, mpp) == (24000, 24000, 0.25)
    # ostatní vrstvy zůstávají pod stropem 8192 px (jemnost ~1 m/px)
    rw, rh, rmpp = _ref_target_size(png, pgw)
    assert max(rw, rh) <= 8192
    assert rmpp > 0.5


def test_ortho_target_size_non_square(tmp_path):
    from app.pipeline.reference_layers import _ortho_target_size

    png = tmp_path / "t.png"
    png.write_bytes(_MINI_PNG)
    pgw = tmp_path / "t.pgw"
    PgwGeoref(500.0, 0.0, 0.0, -250.0, 100.0, 1100.0).write(pgw)  # 2000 × 1000 m
    assert _ortho_target_size(png, pgw) == (8000, 4000, 0.25)


def test_split_ortho_tiles_limits_and_coverage():
    from app.pipeline.reference_layers import ORTHO_MAX_TILE_PX, _split_ortho_tiles

    assert ORTHO_MAX_TILE_PX == 8192
    one = _split_ortho_tiles(8192, 5000)
    assert one == [(0, 0, 0, 0, 8192, 5000)]
    tiles = _split_ortho_tiles(24000, 26000)
    cols = {c for _r, c, *_ in tiles}
    rows = {r for r, *_ in tiles}
    assert len(cols) == 3 and len(rows) == 4 and len(tiles) == 12
    covered = 0
    for r, c, x0, y0, x1, y1 in tiles:
        assert 0 < x1 - x0 <= 8192
        assert 0 < y1 - y0 <= 8192
        covered += (x1 - x0) * (y1 - y0)
    assert covered == 24000 * 26000
    # sousední dlaždice na sebe navazují bez mezery a přesahu
    by_pos = {(r, c): (x0, y0, x1, y1) for r, c, x0, y0, x1, y1 in tiles}
    assert by_pos[(0, 0)][2] == by_pos[(0, 1)][0]
    assert by_pos[(0, 0)][3] == by_pos[(1, 0)][1]
    assert by_pos[(3, 2)][2] == 24000 and by_pos[(3, 2)][3] == 26000


def test_fetch_orthophoto_wms_tiles_and_world_files(tmp_path, monkeypatch):
    """4000×... px = jedna dlaždice; velké AOI = mřížka s navazujícími bounds."""
    from app.pipeline import reference_layers as rl

    calls = []

    def fake_dl(wms_url, layer, bounds, dest, dest_pgw, *, width, height,
                image_format, transparent=False, log=None):
        calls.append((bounds, dest, dest_pgw, width, height, image_format))
        dest.write_bytes(b"\xff\xd8" + b"x" * 600)
        rl._write_pgw_for_extent(dest_pgw, *bounds, width, height)
        return True

    monkeypatch.setattr(rl, "_download_wms_raster", fake_dl)
    out = tmp_path / "references"

    # malé AOI 1 km → 4000×4000 px, jeden soubor orthophoto.jpg
    png, pgw = _mini_template(tmp_path, 1000.0)
    built = rl.fetch_orthophoto_wms((0.0, 0.0, 1000.0, 1000.0), png, pgw, out)
    assert list(built) == ["orthophoto"]
    assert built["orthophoto"].name == "orthophoto.jpg"
    assert calls[0][5] == "image/jpeg"
    assert calls[0][2].name == "orthophoto.jgw"

    # 6×6 km → 24000 px → 3×3 dlaždice po 8000 px
    calls.clear()
    png, pgw = _mini_template(tmp_path, 6000.0)
    bounds = (1000.0, 2000.0, 7000.0, 8000.0)
    built = rl.fetch_orthophoto_wms(bounds, png, pgw, out)
    assert "orthophoto" not in built and (out / "orthophoto.jpg").exists() is False
    assert sorted(built) == [f"orthophoto_r{r}c{c}" for r in range(3) for c in range(3)]
    assert len(calls) == 9
    for b, dest, dest_pgw, w, h, _fmt in calls:
        assert w <= 8192 and h <= 8192
        assert dest_pgw.suffix == ".jgw" and dest.suffix == ".jpg"
        georef = read_pgw(dest_pgw)
        assert abs(georef.pixel_x - 0.25) < 1e-9
        assert abs(georef.pixel_y + 0.25) < 1e-9
        assert abs(georef.origin_x - b[0]) < 1e-9
        assert abs(georef.origin_y - b[3]) < 1e-9
        assert abs((b[2] - b[0]) - w * 0.25) < 1e-6
    by_name = {c[1].stem: c[0] for c in calls}
    r0c0, r0c1, r1c0 = by_name["orthophoto_r0c0"], by_name["orthophoto_r0c1"], by_name["orthophoto_r1c0"]
    assert abs(r0c0[0] - 1000.0) < 1e-9 and abs(r0c0[3] - 8000.0) < 1e-9
    assert abs(r0c0[2] - r0c1[0]) < 1e-9  # sousedé na sebe navazují
    assert abs(r0c0[1] - r1c0[3]) < 1e-9
    assert abs(by_name["orthophoto_r2c2"][2] - 7000.0) < 1e-9
    assert abs(by_name["orthophoto_r2c2"][1] - 2000.0) < 1e-9


def test_fetch_orthophoto_wms_failure_removes_partial(tmp_path, monkeypatch):
    from app.pipeline import reference_layers as rl

    n = {"i": 0}

    def fake_dl(wms_url, layer, bounds, dest, dest_pgw, *, width, height,
                image_format, transparent=False, log=None):
        n["i"] += 1
        if n["i"] == 3:
            return False
        dest.write_bytes(b"\xff\xd8" + b"x" * 600)
        dest_pgw.write_text("0.25\n0\n0\n-0.25\n0\n0\n")
        return True

    monkeypatch.setattr(rl, "_download_wms_raster", fake_dl)
    png, pgw = _mini_template(tmp_path, 3000.0)
    out = tmp_path / "references"
    try:
        rl.fetch_orthophoto_wms((0.0, 0.0, 3000.0, 3000.0), png, pgw, out)
        raise AssertionError("měla vyletět výjimka")
    except RuntimeError:
        pass
    assert list(out.glob("orthophoto*")) == []


def test_download_wms_raster_jpeg_single_tile_stays_jpeg(tmp_path, monkeypatch):
    """Jedna GetMap: JPEG se uloží beze změny (bez PNG), vedle něj .jgw."""
    import io

    from PIL import Image

    from app.pipeline import reference_layers as rl

    buf = io.BytesIO()
    _noisy_image("RGB", (200, 150)).save(buf, "JPEG", quality=90)
    raw = buf.getvalue()
    monkeypatch.setattr(rl, "_http_get_bytes", lambda url, timeout=120: raw)
    dest = tmp_path / "orthophoto.jpg"
    jgw = dest.with_suffix(".jgw")
    assert rl._download_wms_raster(
        rl.ORTOFOTO_WMS, "0", (0.0, 0.0, 50.0, 37.5), dest, jgw,
        width=200, height=150, image_format="image/jpeg",
    )
    assert dest.read_bytes() == raw
    assert Image.open(dest).format == "JPEG"
    assert not list(tmp_path.glob("*.png"))
    g = read_pgw(jgw)
    assert (g.pixel_x, g.pixel_y, g.origin_x, g.origin_y) == (0.25, -0.25, 0.0, 37.5)


def test_download_wms_raster_jpeg_mosaic(tmp_path, monkeypatch):
    """Více GetMap → mozaika přes gdal do JPEG (q90) + .jgw, bez PNG."""
    import pytest
    from PIL import Image

    from app.pipeline import reference_layers as rl

    try:
        rl._gdal_tool("gdalwarp")
        rl._gdal_tool("gdal_translate")
    except RuntimeError:
        pytest.skip("GDAL není k dispozici")
    monkeypatch.setattr(rl, "WMS_MAX_GETMAP_PX", 100)
    monkeypatch.setattr(
        rl,
        "_split_pixel_grid",
        lambda w, h, max_px=100: [(0, 0, 100, 60), (100, 0, 160, 60)],
    )
    import io

    def fake_get(url, timeout=120):
        buf = io.BytesIO()
        w = 100 if "bbox=0.0%2C" in url or "bbox=0," in url else 60
        _noisy_image("RGB", (w, 60)).save(buf, "JPEG")
        return buf.getvalue().ljust(600, b"\0")

    monkeypatch.setattr(rl, "_http_get_bytes", fake_get)
    dest = tmp_path / "orthophoto_r0c0.jpg"
    jgw = dest.with_suffix(".jgw")
    assert rl._download_wms_raster(
        rl.ORTOFOTO_WMS, "0", (-743000.0, -1043000.0, -742960.0, -1042985.0), dest, jgw,
        width=160, height=60, image_format="image/jpeg",
    )
    im = Image.open(dest)
    assert im.format == "JPEG" and im.size == (160, 60)
    assert not list(tmp_path.glob("*.png"))
    assert not list(tmp_path.glob("*.xml"))
    assert read_pgw(jgw).pixel_x == 0.25


# --- Cache a šablony s více dlaždicemi ----------------------------------------


def _fake_jpg(path: Path) -> Path:
    path.write_bytes(b"\xff\xd8" + b"j" * 600)
    path.with_suffix(".jgw").write_text("0.25\n0\n0\n-0.25\n0\n0\n")
    return path


def _fake_png(path: Path) -> Path:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"p" * 600)
    path.with_suffix(".pgw").write_text("1\n0\n0\n-1\n0\n0\n")
    return path


def test_references_cache_roundtrip_with_ortho_tiles(tmp_path):
    from app.pipeline import reference_layers as rl

    work = tmp_path / "work"
    work.mkdir()
    built = {
        "orthophoto_r0c0": _fake_jpg(work / "orthophoto_r0c0.jpg"),
        "orthophoto_r0c1": _fake_jpg(work / "orthophoto_r0c1.jpg"),
        "osm": _fake_png(work / "osm.png"),
        "katastr": _fake_png(work / "katastr.png"),
    }
    cache = tmp_path / "cache"
    rl._store_references_cache(
        cache, work, built, bbox_wgs84=(14.0, 50.0, 14.1, 50.1),
        ref_wh=(100, 100), osm_wh=(100, 100),
    )
    for name in ("orthophoto_r0c0.jpg", "orthophoto_r0c0.jgw", "orthophoto_r0c1.jpg",
                 "orthophoto_r0c1.jgw", "osm.png", "osm.pgw", "katastr.png"):
        assert (cache / name).is_file(), name
    out = tmp_path / "job" / "references"
    got = rl._try_load_references_cache(cache, out)
    assert got is not None
    assert sorted(got) == ["katastr", "orthophoto_r0c0", "orthophoto_r0c1", "osm"]
    assert (out / "orthophoto_r0c1.jgw").is_file()
    assert (out / "osm.pgw").is_file()
    assert [k for k, _p in rl.orthophoto_items(got)] == ["orthophoto_r0c0", "orthophoto_r0c1"]


def test_references_cache_legacy_png_orthophoto_is_miss(tmp_path):
    """Stará cache (orthophoto.png, bez ref_format) se nepoužije – katastr by byl nepruhledný."""
    from app.download_cache import write_meta
    from app.pipeline import reference_layers as rl

    cache = tmp_path / "cache"
    cache.mkdir()
    _fake_png(cache / "orthophoto.png")
    _fake_png(cache / "katastr.png")
    write_meta(cache, kind="references")  # bez ref_format
    assert rl._try_load_references_cache(cache, tmp_path / "out") is None


def test_references_cache_store_drops_legacy_ortho_png(tmp_path):
    from app.pipeline import reference_layers as rl

    cache = tmp_path / "cache"
    cache.mkdir()
    _fake_png(cache / "orthophoto.png")
    _fake_jpg(cache / "orthophoto_r1c1.jpg")  # dlaždice z jiné mřížky
    work = tmp_path / "work"
    work.mkdir()
    built = {"orthophoto": _fake_jpg(work / "orthophoto.jpg"), "osm": _fake_png(work / "osm.png")}
    rl._store_references_cache(
        cache, work, built, bbox_wgs84=(1, 2, 3, 4), ref_wh=(1, 1), osm_wh=(1, 1)
    )
    names = {p.name for p in cache.iterdir()}
    assert "orthophoto.png" not in names and "orthophoto.pgw" not in names
    assert "orthophoto_r1c1.jpg" not in names and "orthophoto_r1c1.jgw" not in names
    assert {"orthophoto.jpg", "orthophoto.jgw", "osm.png"} <= names
    got = rl._try_load_references_cache(cache, tmp_path / "out")
    assert got is not None and sorted(got) == ["orthophoto", "osm"]


def test_world_file_for_and_list_reference_rasters(tmp_path):
    from app.pipeline.reference_layers import list_reference_rasters, world_file_for

    assert world_file_for(Path("a/x.png")).name == "x.pgw"
    assert world_file_for(Path("a/x.jpg")).name == "x.jgw"
    for n in ("b.png", "a.jpg", "a.jgw", "b.pgw", "c.txt"):
        (tmp_path / n).write_bytes(b"x")
    assert [p.name for p in list_reference_rasters(tmp_path)] == ["a.jpg", "b.png"]


def test_collect_oom_templates_ortho_tiles_each_a_template(tmp_path):
    from app.pipeline.oom_layers import collect_oom_templates

    refs = tmp_path / "references"
    refs.mkdir()
    built = {}
    for name in ("orthophoto_r1c0", "orthophoto_r0c1", "orthophoto_r0c0"):
        built[name] = _fake_jpg(refs / f"{name}.jpg")
    built["katastr"] = _fake_png(refs / "katastr.png")
    templates = collect_oom_templates(tmp_path, built_refs=built)
    rel = [t.relpath for t in templates]
    assert rel == [
        "references/orthophoto_r0c0.jpg",
        "references/orthophoto_r0c1.jpg",
        "references/orthophoto_r1c0.jpg",
        "references/katastr.png",
    ]
    assert templates[0].label.startswith("Ortofoto ČÚZK r0c0")
    assert all(t.kind == "image" and t.visible is False for t in templates)
    xml = build_oom_map_xml(
        map_name="T", scale=10000, ref_x=1.0, ref_y=2.0, ref_lat=50.0, ref_lon=14.5,
        declination=0.0, grivation=0.0, preset_id="forest_10000", templates=templates,
    )
    assert 'name="orthophoto_r1c0.jpg" path="references/orthophoto_r1c0.jpg"' in xml
    assert '<templates count="4"' in xml


def test_collect_oom_templates_single_ortho_jpg(tmp_path):
    from app.pipeline.oom_layers import collect_oom_templates

    refs = tmp_path / "references"
    refs.mkdir()
    templates = collect_oom_templates(
        tmp_path, built_refs={"orthophoto": _fake_jpg(refs / "orthophoto.jpg")}
    )
    assert len(templates) == 1
    assert templates[0].relpath == "references/orthophoto.jpg"
    assert templates[0].label == "Ortofoto ČÚZK"

