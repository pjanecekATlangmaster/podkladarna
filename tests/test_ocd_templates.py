"""Šablony v .ocd z Mapper CLI convert: poloha z world file, průhlednost z pohledu, relativní cesta."""

from __future__ import annotations

import math
import struct
from pathlib import Path

import pytest
from PIL import Image

from app.pipeline.build_oom_map import write_oom_map
from app.pipeline.ocd_templates import _parse_omap, fix_ocd_templates, read_ocd_strings
from app.pipeline.oom_layers import OomTemplate

REF_X, REF_Y = -745123.4, -1046789.6


def _fake_ocd(path: Path, strings: list[tuple[int, str]]) -> None:
    """Minimální OCD12: hlavička, jeden blok indexu řetězců, řetězce jako Mapper (nula na konci)."""
    header = bytearray(48)
    struct.pack_into("<HBBHBB", header, 0, 0x0CAD, 0, 0, 12, 0, 0)
    block_pos = 48
    struct.pack_into("<I", header, 32, block_pos)
    block = bytearray(4 + 256 * 16)
    body = bytearray()
    pos = block_pos + len(block)
    for i, (stype, text) in enumerate(strings):
        raw = text.encode("utf-8") + b"\0"
        struct.pack_into("<IIiI", block, 4 + i * 16, pos + len(body), len(raw), stype, 0)
        body += raw
    path.write_bytes(bytes(header + block + body))


def _mapper_import_corners(s8: str, scale: int, griv: float) -> list[tuple[float, float]]:
    """Rohy obrázku v S-JTSK podle ``OcdFileImport::importTemplate`` + ``Template`` transformace."""
    p = {part[0]: part[1:] for part in s8.split("\t")[1:]}
    tx, ty = float(p["x"]), -float(p["y"])
    rot = math.radians(float(p["a"]))
    u, v = float(p["u"]), float(p["v"])
    w, h = 400, 200
    out = []
    for px, py in ((-w / 2, -h / 2), (w / 2, -h / 2), (-w / 2, h / 2)):
        c, s = math.cos(-rot), math.sin(-rot)
        mx = u * c * px - v * s * py + tx
        my = u * s * px + v * c * py + ty
        sx, sy = mx * scale / 1000.0, -my * scale / 1000.0
        g = math.radians(griv)
        out.append((REF_X + sx * math.cos(g) + sy * math.sin(g), REF_Y - sx * math.sin(g) + sy * math.cos(g)))
    return out


@pytest.mark.parametrize("grivation", [0.0, 2.5])
def test_fix_ocd_templates_matches_world_file(tmp_path: Path, grivation: float) -> None:
    out = tmp_path / "output"
    refs = tmp_path / "work" / "references"
    refs.mkdir(parents=True)
    Image.new("RGB", (400, 200), "white").save(refs / "orthophoto_r0c1.jpg")
    pix = 0.125
    left, top = REF_X - 10.0, REF_Y + 30.0
    (refs / "orthophoto_r0c1.jgw").write_text(
        f"{pix}\n0\n0\n{-pix}\n{left + pix / 2}\n{top - pix / 2}\n", encoding="ascii"
    )
    omap = write_oom_map(
        out / "mapa-les.omap",
        map_name="t",
        scale=10000,
        ref_x=REF_X,
        ref_y=REF_Y,
        ref_lat=50.0,
        ref_lon=14.4,
        templates=[
            OomTemplate("image", "Ortofoto", "references/orthophoto_r0c1.jpg", visible=False, opacity=1.0),
            OomTemplate("gdal", "Online", "references/online.xml", visible=False),
        ],
        preset_id="les_10k",
        declination=grivation + 1.0,
        grivation=grivation,
    )
    ocd = omap.with_suffix(".ocd")
    _fake_ocd(
        ocd,
        [
            (1039, f"\tm10000\tg50\tr1\tx{round(REF_X)}\ty{round(REF_Y)}\ta{grivation}"),
            # Tvar z Mapper CLI: absolutní cesta, nenačtená šablona, d100.
            (8, "/data/jobs/x/output/references/orthophoto_r0c1.jpg\ts0\tr1\tu1\tv1\tx0\ty0\ta0\tb0\td100"),
        ],
    )

    res = fix_ocd_templates(ocd, omap, reference_dirs=[refs])

    assert res.fixed == 1
    (s8,) = read_ocd_strings(ocd, 8)
    assert s8.startswith("references\\orthophoto_r0c1.jpg\ts0\tr1\t")
    assert s8.endswith("\td0")
    corners = _mapper_import_corners(s8, 10000, grivation)
    expect = [(left, top), (left + 400 * pix, top), (left, top - 200 * pix)]
    for (e, n), (ee, en) in zip(corners, expect):
        assert e == pytest.approx(ee, abs=0.01)
        assert n == pytest.approx(en, abs=0.01)
    # Řetězec 1039 beze změny.
    assert read_ocd_strings(ocd, 1039)[0].startswith("\tm10000")


def test_fix_ocd_templates_missing_image_left_untouched(tmp_path: Path) -> None:
    omap = write_oom_map(
        tmp_path / "m.omap", map_name="t", scale=4000, ref_x=REF_X, ref_y=REF_Y,
        ref_lat=50.0, ref_lon=14.4,
        templates=[OomTemplate("image", "OSM", "references/osm.png", visible=True, opacity=0.55)],
        preset_id="sprint_4k", declination=4.0, grivation=3.0,
    )
    georef, templates = _parse_omap(omap)
    assert georef.scale == 4000 and templates[0].visible and templates[0].opacity == pytest.approx(0.55)
    ocd = tmp_path / "m.ocd"
    old = "/x/references/osm.png\ts0\tr1\tu1\tv1\tx0\ty0\ta0\tb0\td100"
    _fake_ocd(ocd, [(8, old)])
    res = fix_ocd_templates(ocd, omap)
    assert res.fixed == 0 and res.skipped == ("osm.png",)
    assert read_ocd_strings(ocd, 8) == [old]
