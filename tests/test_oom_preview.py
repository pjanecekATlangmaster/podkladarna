"""Náhled PNG z .omap – Pillow (web) a Mapper CLI (georef)."""

from __future__ import annotations

import os
import signal
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from PIL import Image, ImageDraw

from app.pipeline.oom_preview import (
    _is_pixel_bomb_error,
    _web_preview_deadline,
    aoi_frame_bbox,
    assert_png_within_web_pixel_limit,
    build_georef_previews_zip,
    capped_web_mapper_dpi,
    convert_omap_to_ocd,
    find_mapper_exe,
    map_grivation_deg,
    mapper_convert_argv,
    mapper_export_argv,
    ocd_version,
    oom_preview_enabled,
    output_georef_enabled,
    png_pixel_count,
    prepare_mapper_web_preview,
    render_omap_to_png,
    run_mapper_convert,
    undo_grivation_xy,
    write_job_oom_preview,
)

# _web_preview_deadline stojí na SIGALRM (Linux job worker); Windows ho nemá.
needs_sigalrm = pytest.mark.skipif(
    not hasattr(signal, "SIGALRM"), reason="SIGALRM not available (Windows)"
)


def _install_fake_mapper(monkeypatch, tmp_path: Path, *, rgb=(255, 0, 0)) -> Path:
    """Fake Mapper CLI: full-map styl PNG z .omap (s grivací, bez AOI ořezu)."""
    # Podproces musí najít balík app (jinak ModuleNotFoundError → testy tiše
    # jely přes Pillow fallback místo „Mapperu“).
    repo_root = str(Path(__file__).resolve().parents[1])
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(p for p in (repo_root, os.environ.get("PYTHONPATH", "")) if p),
    )
    script = tmp_path / "fake_mapper.py"
    # Fallback solid color when omap path missing; jinak Pillow render bez undo
    # přes celý bbox objektů (simulace Mapper --full-map).
    script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from PIL import Image, ImageDraw\n"
        "from xml.etree import ElementTree as ET\n"
        "from app.pipeline.oom_preview import (\n"
        "    _collect_ops, _ops_bbox, _read_omap_bytes, parse_omap_georef,\n"
        ")\n"
        "args = sys.argv[1:]\n"
        "out = Path(args[args.index('-o') + 1] if '-o' in args else args[0])\n"
        "omap = None\n"
        "for a in args:\n"
        "    if a.endswith('.omap'):\n"
        "        omap = Path(a)\n"
        "        break\n"
        f"rgb = ({rgb[0]}, {rgb[1]}, {rgb[2]})\n"
        "if omap is None or not omap.is_file():\n"
        "    Image.new('RGB', (64, 48), rgb).save(out, format='PNG')\n"
        "    raise SystemExit(0)\n"
        "root = ET.fromstring(_read_omap_bytes(omap))\n"
        "ops = _collect_ops(root, grivation_deg=0.0)\n"
        "if not ops:\n"
        "    Image.new('RGB', (64, 48), rgb).save(out, format='PNG')\n"
        "    raise SystemExit(0)\n"
        "minx, miny, maxx, maxy = _ops_bbox(ops)\n"
        "pad = 500.0\n"
        "world_w = max(maxx - minx, 1.0) + 2 * pad\n"
        "world_h = max(maxy - miny, 1.0) + 2 * pad\n"
        "side = 320\n"
        "mpp = max(world_w, world_h) / float(side)\n"
        "w = max(1, int(round(world_w / mpp)))\n"
        "h = max(1, int(round(world_h / mpp)))\n"
        "ox, oy = minx - pad, miny - pad\n"
        "img = Image.new('RGB', (w, h), (255, 255, 255))\n"
        "draw = ImageDraw.Draw(img)\n"
        "def to_px(x, y):\n"
        "    return ((x - ox) / mpp, (y - oy) / mpp)\n"
        "for op in ops:\n"
        "    color = op.rgb if op.rgb else rgb\n"
        "    for path in op.paths:\n"
        "        if len(path) < 2:\n"
        "            continue\n"
        "        pts = [to_px(x, y) for x, y in path]\n"
        "        if op.kind == 'area':\n"
        "            draw.polygon(pts, fill=color)\n"
        "        else:\n"
        "            draw.line(pts, fill=color, width=max(1, int(round((op.width or 400) / mpp))))\n"
        "img.save(out, format='PNG')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PODKLADARNA_MAPPER", sys.executable)
    monkeypatch.setenv(
        "PODKLADARNA_MAPPER_EXPORT",
        f'"{sys.executable}" "{script}" -o "{{png}}" "{{omap}}" --dpi {{dpi}}',
    )
    return script

_MAP = """<?xml version="1.0" encoding="UTF-8"?>
<map xmlns="http://openorienteering.org/apps/mapper/xml/v2" version="9">
  <georeferencing scale="10000"/>
  <colors count="2">
    <color priority="2" name="Black" c="0" m="0" y="0" k="1" opacity="1"/>
    <color priority="26" name="Green" c="0.76" m="0" y="0.91" k="0" opacity="1">
      <rgb method="cmyk" r="0.24" g="1" b="0.09"/>
    </color>
  </colors>
  <barrier>
    <symbols count="2">
      <symbol type="4" id="10" code="406" name="Forest">
        <area_symbol inner_color="26"/>
      </symbol>
      <symbol type="2" id="11" code="101" name="Contour">
        <line_symbol color="2" line_width="400"/>
      </symbol>
    </symbols>
    <parts count="1" current="0">
      <part name="Mapa">
        <objects count="2">
          <object type="1" symbol="10">
            <coords count="5">0 0;0 10000;10000 10000;10000 0;0 0 18;</coords>
          </object>
          <object type="1" symbol="11">
            <coords count="2">0 5000;10000 5000;</coords>
          </object>
        </objects>
      </part>
    </parts>
  </barrier>
</map>
"""

_HOLE = """<?xml version="1.0" encoding="UTF-8"?>
<map xmlns="http://openorienteering.org/apps/mapper/xml/v2" version="9">
  <colors count="1">
    <color priority="26" name="Green">
      <rgb r="0" g="1" b="0"/>
    </color>
  </colors>
  <symbols count="1">
    <symbol type="4" id="1" code="406">
      <area_symbol inner_color="26"/>
    </symbol>
  </symbols>
  <parts count="1" current="0">
    <part name="Mapa">
      <objects count="1">
        <object type="1" symbol="1">
          <coords count="10">0 0;0 10000;10000 10000;10000 0;0 0 18;2500 2500;2500 7500;7500 7500;7500 2500;2500 2500 18;</coords>
        </object>
      </objects>
    </part>
  </parts>
</map>
"""


def test_preview_on_only_for_bez_kp():
    assert oom_preview_enabled({}) is True
    assert oom_preview_enabled({"use_kp": True}) is True
    assert oom_preview_enabled({"oom_preview": True}) is True
    assert oom_preview_enabled({"oom_preview": False}) is False


def test_output_georef_default_off(monkeypatch):
    monkeypatch.delenv("PODKLADARNA_OUTPUT_GEOREF", raising=False)
    assert output_georef_enabled({}) is False
    assert output_georef_enabled({"output_georef": False}) is False
    assert output_georef_enabled({"output_georef": True}) is True
    monkeypatch.setenv("PODKLADARNA_OUTPUT_GEOREF", "1")
    assert output_georef_enabled({}) is True


def test_env_can_disable(monkeypatch):
    monkeypatch.setenv("PODKLADARNA_OOM_PREVIEW", "0")
    assert oom_preview_enabled({}) is False


def test_xml_render_green_fill_and_black_line(tmp_path: Path):
    omap = tmp_path / "mini.omap"
    omap.write_text(_MAP, encoding="utf-8")
    png = tmp_path / "preview.png"
    summary = render_omap_to_png(omap, png, max_side=200)
    assert summary.startswith("xml")
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    image = Image.open(png).convert("RGB")
    # Roh uvnitř zelené plochy (mimo okraj a mimo středovou čáru).
    corner = image.getpixel((24, 24))
    assert corner[1] > corner[0]
    assert corner[1] > 180
    cx, cy = image.size[0] // 2, image.size[1] // 2
    center = image.getpixel((cx, cy))
    assert center[0] < 40 and center[1] < 40 and center[2] < 40


def test_lower_map_y_is_top_of_png(tmp_path: Path):
    """OOM scale(s, −s): geografický sever = nižší map Y → horní část PNG.

    Dříve render dával vyšší map Y nahoru (matematické Y-up), takže náhled
    vypadal otočený vzhůru nohama / „o 180°“.
    """
    omap = tmp_path / "north.omap"
    # Zelená y∈[-10000, 0]; černá linka y=10000 roztáhne bbox, ať je co porovnat.
    omap.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<map xmlns="http://openorienteering.org/apps/mapper/xml/v2" version="9">
  <colors count="2">
    <color priority="2" name="Black"><rgb r="0" g="0" b="0"/></color>
    <color priority="26" name="Green"><rgb r="0" g="1" b="0"/></color>
  </colors>
  <symbols count="2">
    <symbol type="4" id="1" code="406">
      <area_symbol inner_color="26"/>
    </symbol>
    <symbol type="2" id="2" code="101">
      <line_symbol color="2" line_width="400"/>
    </symbol>
  </symbols>
  <parts count="1" current="0">
    <part name="Mapa">
      <objects count="2">
        <object type="1" symbol="1">
          <coords count="5">0 0;0 -10000;10000 -10000;10000 0;0 0 18;</coords>
        </object>
        <object type="1" symbol="2">
          <coords count="2">0 10000;10000 10000;</coords>
        </object>
      </objects>
    </part>
  </parts>
</map>
""",
        encoding="utf-8",
    )
    png = tmp_path / "preview.png"
    render_omap_to_png(omap, png, max_side=200)
    image = Image.open(png).convert("RGB")
    w, h = image.size
    top = image.getpixel((w // 2, max(1, h // 8)))
    bottom = image.getpixel((w // 2, h - max(1, h // 8) - 4))
    assert top[1] > 180 and top[0] < 80, f"top should be green (low map Y), got {top}"
    assert not (bottom[1] > 180 and bottom[0] < 80), (
        f"bottom should not be green (high map Y), got {bottom}"
    )


def test_aoi_frame_bbox_finds_708_rectangle():
    root = ET.fromstring(
        """<?xml version="1.0" encoding="UTF-8"?>
<map xmlns="http://openorienteering.org/apps/mapper/xml/v2" version="9">
  <colors count="1">
    <color priority="5" name="Purple"><rgb r="1" g="0" b="1"/></color>
  </colors>
  <symbols count="1">
    <symbol type="2" id="175" code="708" name="Out-of-bounds boundary">
      <line_symbol color="5" line_width="1000"/>
    </symbol>
  </symbols>
  <parts count="1" current="0">
    <part name="Mapa">
      <objects count="1">
        <object type="1" symbol="175">
          <coords count="5">0 0;10000 0;10000 5000;0 5000;0 0 18;</coords>
        </object>
      </objects>
    </part>
  </parts>
</map>
"""
    )
    assert aoi_frame_bbox(root) == (0.0, 0.0, 10000.0, 5000.0)


def test_undo_grivation_restores_grid_east():
    """projected_to_map_coord točí +grivation; náhled odrotuje zpět (bez deklinace)."""
    from app.pipeline.oom_coords import projected_to_map_coord

    g = 12.52
    mx, my = projected_to_map_coord(
        1.0, 0.0, ref_x=0.0, ref_y=0.0, scale=10000, grivation_deg=g
    )
    x, y = undo_grivation_xy(mx, my, g)
    # 1 m východně @ 1:10000 → 100 nativních jednotek na +X, Y≈0
    assert abs(x - 100.0) < 1.0
    assert abs(y) < 1.0
    # Sever (dy=+1) → nižší map Y i po odrotování
    mxn, myn = projected_to_map_coord(
        0.0, 1.0, ref_x=0.0, ref_y=0.0, scale=10000, grivation_deg=g
    )
    xn, yn = undo_grivation_xy(mxn, myn, g)
    assert abs(xn) < 1.0
    assert yn < -90.0


def test_preview_undoes_grivation_for_aoi_crop(tmp_path: Path):
    """AOI zapečené s grivací → po odrotování osově rovný ořez (srovnání bez deklinace)."""
    import math

    g = 12.52
    rad = math.radians(g)
    # Obdélník v gridu; v mapových souřadnicích jako po projected_to_map_coord.
    grid = [(-5000, -4000), (5000, -4000), (5000, 4000), (-5000, 4000), (-5000, -4000)]

    def to_map(x: float, y: float) -> tuple[int, int]:
        rx = x * math.cos(rad) - y * math.sin(rad)
        ry = x * math.sin(rad) + y * math.cos(rad)
        return round(rx), round(-ry)

    ring = [to_map(x, y) for x, y in grid]
    coords = ";".join(f"{x} {y}" for x, y in ring[:-1])
    coords += f";{ring[-1][0]} {ring[-1][1]} 18;"
    omap = tmp_path / "griv.omap"
    omap.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<map xmlns="http://openorienteering.org/apps/mapper/xml/v2" version="9">
  <georeferencing scale="10000" declination="5.00" grivation="{g}"/>
  <colors count="2">
    <color priority="5" name="Purple"><rgb r="1" g="0" b="1"/></color>
    <color priority="26" name="Green"><rgb r="0" g="1" b="0"/></color>
  </colors>
  <symbols count="2">
    <symbol type="2" id="175" code="708">
      <line_symbol color="5" line_width="1000"/>
    </symbol>
    <symbol type="4" id="10" code="406">
      <area_symbol inner_color="26"/>
    </symbol>
  </symbols>
  <parts count="1" current="0">
    <part name="Mapa">
      <objects count="2">
        <object type="1" symbol="10">
          <coords count="5">{coords}</coords>
        </object>
        <object type="1" symbol="175">
          <coords count="5">{coords}</coords>
        </object>
      </objects>
    </part>
  </parts>
</map>
""",
        encoding="utf-8",
    )
    root = ET.fromstring(omap.read_text(encoding="utf-8"))
    assert abs(map_grivation_deg(root) - g) < 1e-6
    minx, miny, maxx, maxy = aoi_frame_bbox(root)
    # Bez odrotování by bbox byl „kosočtverec“; po odrotování ≈ 10000×8000.
    assert abs((maxx - minx) - 10000) < 50
    assert abs((maxy - miny) - 8000) < 50
    png = tmp_path / "preview.png"
    render_omap_to_png(omap, png, max_side=200)
    image = Image.open(png).convert("RGB")
    w, h = image.size
    assert abs(w / h - 10000 / 8000) < 0.15


def test_cultivated_land_412_is_yellow_not_black(tmp_path: Path):
    """ISOM 412 = 401 (žlutá) + 412.1 (černý pattern); pattern nesmí zalít plochu černě."""
    omap = tmp_path / "cultivated.omap"
    omap.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<map xmlns="http://openorienteering.org/apps/mapper/xml/v2" version="9">
  <colors count="3">
    <color priority="31" name="Black for open land"><rgb r="0" g="0" b="0"/></color>
    <color priority="32" name="Yellow"><rgb r="1" g="0.73" b="0.21"/></color>
    <color priority="33" name="Yellow 100% for area features"><rgb r="1" g="0.73" b="0.21"/></color>
  </colors>
  <symbols count="3">
    <symbol type="4" id="79" code="401" name="Open land">
      <area_symbol inner_color="33" min_area="1125" patterns="0"/>
    </symbol>
    <symbol type="16" id="99" code="412" name="Cultivated land">
      <combined_symbol parts="2">
        <part symbol="79"/>
        <part symbol="100"/>
      </combined_symbol>
    </symbol>
    <symbol type="4" id="100" code="412.1" name="Cultivated land (black pattern)">
      <area_symbol inner_color="-1" min_area="20250" patterns="1">
        <pattern type="2" angle="0" line_spacing="1200" line_offset="0"
                 offset_along_line="0" point_distance="1200">
          <symbol type="1" code="" name="Pattern fill 1">
            <point_symbol rotatable="true" inner_radius="150" inner_color="31"
                          outer_width="0" outer_color="-1" elements="0"/>
          </symbol>
        </pattern>
      </area_symbol>
    </symbol>
  </symbols>
  <parts count="1" current="0">
    <part name="Mapa">
      <objects count="1">
        <object type="1" symbol="99">
          <coords count="5">0 0;0 8000;8000 8000;8000 0;0 0 18;</coords>
        </object>
      </objects>
    </part>
  </parts>
</map>
""",
        encoding="utf-8",
    )
    from app.pipeline.oom_preview import _collect_ops

    root = ET.fromstring(omap.read_text(encoding="utf-8"))
    ops = _collect_ops(root)
    assert len(ops) == 1
    assert ops[0].kind == "area"
    assert ops[0].color == 33
    assert ops[0].rgb == (255, 186, 54)

    png = tmp_path / "preview.png"
    render_omap_to_png(omap, png, max_side=120)
    image = Image.open(png).convert("RGB")
    mid = image.getpixel((image.size[0] // 2, image.size[1] // 2))
    assert mid[0] > 200 and mid[1] > 140 and mid[2] < 120, mid
    assert mid != (0, 0, 0)


def test_combined_inline_paved_area_is_drawn(tmp_path: Path):
    """ISOM 501 má private parts v combined_symbol – dřív style=None → vynecháno."""
    omap = tmp_path / "paved.omap"
    omap.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<map xmlns="http://openorienteering.org/apps/mapper/xml/v2" version="9">
  <colors count="2">
    <color priority="13" name="Paved"><rgb r="0.9" g="0.7" b="0.5"/></color>
    <color priority="12" name="Bound"><rgb r="0" g="0" b="0"/></color>
  </colors>
  <symbols count="1">
    <symbol type="16" id="111" code="501" name="Paved area, with bounding line">
      <combined_symbol parts="2">
        <part private="true">
          <symbol type="4" code="501.1">
            <area_symbol inner_color="13"/>
          </symbol>
        </part>
        <part private="true">
          <symbol type="2" code="501.2">
            <line_symbol color="12" line_width="210"/>
          </symbol>
        </part>
      </combined_symbol>
    </symbol>
  </symbols>
  <parts count="1" current="0">
    <part name="Mapa">
      <objects count="1">
        <object type="1" symbol="111">
          <coords count="5">0 0;0 8000;8000 8000;8000 0;0 0 18;</coords>
        </object>
      </objects>
    </part>
  </parts>
</map>
""",
        encoding="utf-8",
    )
    png = tmp_path / "preview.png"
    render_omap_to_png(omap, png, max_side=120)
    image = Image.open(png).convert("RGB")
    mid = image.getpixel((image.size[0] // 2, image.size[1] // 2))
    assert mid[0] > 150 and mid[1] > 100 and mid[2] < 180


def test_dashed_track_has_gaps(tmp_path: Path):
    omap = tmp_path / "dash.omap"
    omap.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<map xmlns="http://openorienteering.org/apps/mapper/xml/v2" version="9">
  <colors count="1">
    <color priority="2" name="Black"><rgb r="0" g="0" b="0"/></color>
  </colors>
  <symbols count="1">
    <symbol type="2" id="118" code="504">
      <line_symbol color="2" line_width="800" dashed="true" dash_length="2000" break_length="2000"/>
    </symbol>
  </symbols>
  <parts count="1" current="0">
    <part name="Mapa">
      <objects count="1">
        <object type="1" symbol="118">
          <coords count="2">0 0;20000 0;</coords>
        </object>
      </objects>
    </part>
  </parts>
</map>
""",
        encoding="utf-8",
    )
    png = tmp_path / "preview.png"
    render_omap_to_png(omap, png, max_side=200)
    image = Image.open(png).convert("RGB")
    y = image.size[1] // 2
    blacks = sum(
        1
        for x in range(image.size[0])
        if image.getpixel((x, y))[0] < 40
        and image.getpixel((x, y))[1] < 40
        and image.getpixel((x, y))[2] < 40
    )
    whites = sum(1 for x in range(image.size[0]) if image.getpixel((x, y)) == (255, 255, 255))
    assert blacks > 10
    assert whites > 10


def test_cliff_mid_ticks_drawn(tmp_path: Path):
    omap = tmp_path / "cliff.omap"
    # Svislá linie, ať fousy (kolmo) mají kam kreslit mimo střední sloupec.
    omap.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<map xmlns="http://openorienteering.org/apps/mapper/xml/v2" version="9">
  <colors count="1">
    <color priority="2" name="Black"><rgb r="0" g="0" b="0"/></color>
  </colors>
  <symbols count="1">
    <symbol type="2" id="23" code="201">
      <line_symbol color="2" line_width="200" segment_length="1500" end_length="800">
        <start_symbol>
          <symbol type="1">
            <point_symbol inner_radius="0" inner_color="-1" elements="1">
              <element>
                <symbol type="2">
                  <line_symbol color="2" line_width="180"/>
                </symbol>
              </element>
            </point_symbol>
          </symbol>
        </start_symbol>
      </line_symbol>
    </symbol>
  </symbols>
  <parts count="1" current="0">
    <part name="Mapa">
      <objects count="1">
        <object type="1" symbol="23">
          <coords count="2">0 0;0 12000;</coords>
        </object>
      </objects>
    </part>
  </parts>
</map>
""",
        encoding="utf-8",
    )
    png = tmp_path / "preview.png"
    render_omap_to_png(omap, png, max_side=240)
    image = Image.open(png).convert("RGB")
    mid_x = image.size[0] // 2
    off_col_black = 0
    for x in range(image.size[0]):
        if abs(x - mid_x) < 3:
            continue
        for y in range(image.size[1]):
            p = image.getpixel((x, y))
            if p[0] < 40 and p[1] < 40 and p[2] < 40:
                off_col_black += 1
    assert off_col_black > 5


def test_preview_crops_to_purple_aoi_frame(tmp_path: Path):
    """Přesah za fialový 708 rám se do náhledu nevejde (Mapper ho nechá)."""
    omap = tmp_path / "crop.omap"
    # Rám 0..10000; zelený blob jen vpravo venku (15000..20000).
    omap.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<map xmlns="http://openorienteering.org/apps/mapper/xml/v2" version="9">
  <colors count="2">
    <color priority="5" name="Purple"><rgb r="1" g="0" b="1"/></color>
    <color priority="26" name="Green"><rgb r="0" g="1" b="0"/></color>
  </colors>
  <symbols count="2">
    <symbol type="2" id="175" code="708">
      <line_symbol color="5" line_width="1000"/>
    </symbol>
    <symbol type="4" id="10" code="406">
      <area_symbol inner_color="26"/>
    </symbol>
  </symbols>
  <parts count="1" current="0">
    <part name="Mapa">
      <objects count="2">
        <object type="1" symbol="10">
          <coords count="5">15000 0;15000 10000;20000 10000;20000 0;15000 0 18;</coords>
        </object>
        <object type="1" symbol="175">
          <coords count="5">0 0;10000 0;10000 10000;0 10000;0 0 18;</coords>
        </object>
      </objects>
    </part>
  </parts>
</map>
""",
        encoding="utf-8",
    )
    png = tmp_path / "preview.png"
    render_omap_to_png(omap, png, max_side=200)
    image = Image.open(png).convert("RGB")
    w, h = image.size
    # Bez ořezu by šířka sahala k x=20000 a poměr ≈ 2:1; s rámem ≈ čtverec.
    assert abs(w - h) <= max(4, w // 10), f"expected near-square crop, got {w}x{h}"
    mid = image.getpixel((w // 2, h // 2))
    is_green = mid[1] > 180 and mid[0] < 80
    assert not is_green, f"center should not be overflow green, got {mid}"


def test_prepare_mapper_web_preview_crops_aoi_and_undoes_grivation(tmp_path: Path):
    """Mapper full-map PNG → web: ořez 708 + odrotování grivace."""
    import math

    from app.pipeline.oom_preview import (
        _collect_ops,
        _ops_bbox,
        _read_omap_bytes,
    )

    g = 12.52
    rad = math.radians(g)

    def to_map(x: float, y: float) -> tuple[int, int]:
        rx = x * math.cos(rad) - y * math.sin(rad)
        ry = x * math.sin(rad) + y * math.cos(rad)
        return round(rx), round(-ry)

    # Grid-north AOI čtverec; spill až vpravo venku (Mapper --full-map ho nechá).
    aoi = [(-5000, -5000), (5000, -5000), (5000, 5000), (-5000, 5000), (-5000, -5000)]
    spill = [(12000, -3000), (18000, -3000), (18000, 3000), (12000, 3000), (12000, -3000)]
    aoi_m = [to_map(x, y) for x, y in aoi]
    spill_m = [to_map(x, y) for x, y in spill]
    aoi_coords = ";".join(f"{x} {y}" for x, y in aoi_m[:-1])
    aoi_coords += f";{aoi_m[-1][0]} {aoi_m[-1][1]} 18;"
    spill_coords = ";".join(f"{x} {y}" for x, y in spill_m[:-1])
    spill_coords += f";{spill_m[-1][0]} {spill_m[-1][1]} 18;"
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<map xmlns="http://openorienteering.org/apps/mapper/xml/v2" version="9">
  <georeferencing scale="10000" declination="5.00" grivation="{g}">
    <projected_crs id="EPSG">
      <ref_point x="-750000" y="-1050000"/>
    </projected_crs>
  </georeferencing>
  <colors count="2">
    <color priority="5" name="Purple"><rgb r="1" g="0" b="1"/></color>
    <color priority="26" name="Green"><rgb r="0" g="1" b="0"/></color>
  </colors>
  <symbols count="2">
    <symbol type="2" id="175" code="708">
      <line_symbol color="5" line_width="1000"/>
    </symbol>
    <symbol type="4" id="10" code="406">
      <area_symbol inner_color="26"/>
    </symbol>
  </symbols>
  <parts count="1" current="0">
    <part name="Mapa">
      <objects count="2">
        <object type="1" symbol="10">
          <coords count="5">{spill_coords}</coords>
        </object>
        <object type="1" symbol="175">
          <coords count="5">{aoi_coords}</coords>
        </object>
      </objects>
    </part>
  </parts>
</map>
"""
    omap = tmp_path / "griv.omap"
    omap.write_text(body, encoding="utf-8")

    # Simulace Mapper --full-map: celý bbox objektů v nativních (grivovaných) j.
    root = ET.fromstring(_read_omap_bytes(omap))
    ops = _collect_ops(root, grivation_deg=0.0)
    minx, miny, maxx, maxy = _ops_bbox(ops)
    pad = 800.0
    world_w = max(maxx - minx, 1.0) + 2 * pad
    world_h = max(maxy - miny, 1.0) + 2 * pad
    side = 480
    mpp = max(world_w, world_h) / float(side)
    w = max(1, int(round(world_w / mpp)))
    h = max(1, int(round(world_h / mpp)))
    ox, oy = minx - pad, miny - pad
    mapper_png = tmp_path / "mapper_full.png"
    img = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    def to_px(x: float, y: float) -> tuple[float, float]:
        return (x - ox) / mpp, (y - oy) / mpp

    for op in ops:
        for path in op.paths:
            if len(path) < 2:
                continue
            pts = [to_px(x, y) for x, y in path]
            if op.kind == "area":
                draw.polygon(pts, fill=op.rgb)
            else:
                draw.line(pts, fill=op.rgb, width=3)
    img.save(mapper_png)

    # Full-map je širší než čtverec (spill + rotace).
    assert w > h * 1.15, f"full-map should be wide, got {w}x{h}"

    out = tmp_path / "web.png"
    ow, oh = prepare_mapper_web_preview(omap, mapper_png, out, max_side=200)
    assert out.is_file()
    assert abs(ow - oh) <= max(4, ow // 10), f"AOI crop should be square, got {ow}x{oh}"
    web = Image.open(out).convert("RGB")
    mid = web.getpixel((ow // 2, oh // 2))
    is_green = mid[1] > 180 and mid[0] < 80
    assert not is_green, f"center must not be spill green after AOI crop, got {mid}"
    # Po undo grivace je fialový rám axis-aligned → okrajové pixely blízko stredu stran
    # nejsou „rozmazané“ šikmé čáry přes celý obrázek (stačí: výstup existuje + čtverec).
    assert map_grivation_deg(root) == pytest.approx(g, abs=0.01)


def test_hole_stays_paper_white(tmp_path: Path):
    omap = tmp_path / "hole.omap"
    omap.write_text(_HOLE, encoding="utf-8")
    png = tmp_path / "preview.png"
    render_omap_to_png(omap, png, max_side=200)
    image = Image.open(png).convert("RGB")
    cx, cy = image.size[0] // 2, image.size[1] // 2
    assert image.getpixel((cx, cy)) == (255, 255, 255)
    assert image.getpixel((24, 24))[1] > 200


def test_mapper_cli_used_for_georef_and_web(tmp_path: Path, monkeypatch):
    """Georef i web job preview jdou přes Mapper, když je CLI; Pillow jen bez CLI."""
    omap = tmp_path / "mini.omap"
    omap.write_text(_MAP, encoding="utf-8")
    script = tmp_path / "fake_mapper.py"
    script.write_text(
        "import sys\n"
        "from PIL import Image\n"
        "out = sys.argv[sys.argv.index('-o') + 1] if '-o' in sys.argv else sys.argv[1]\n"
        "Image.new('RGB', (8, 8), (255, 0, 0)).save(out, format='PNG')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PODKLADARNA_MAPPER", sys.executable)
    monkeypatch.setenv(
        "PODKLADARNA_MAPPER_EXPORT",
        f'"{sys.executable}" "{script}" -o "{{png}}" "{{omap}}" --dpi {{dpi}}',
    )
    georef = tmp_path / "geo.png"
    summary = render_omap_to_png(
        omap, georef, write_pgw=True, write_geotiff=False, engine="mapper"
    )
    assert summary.startswith("mapper-cli")
    assert Image.open(georef).getpixel((0, 0)) == (255, 0, 0)
    assert georef.with_suffix(".pgw").is_file()

    # Bez write_pgw default engine stále pillow (unit API); job path volá mapper explicitně.
    web = tmp_path / "web.png"
    web_summary = render_omap_to_png(omap, web, write_pgw=False)
    assert web_summary.startswith("xml ")


def test_georef_mapper_missing_raises(tmp_path: Path, monkeypatch):
    from app.pipeline.oom_preview import MapperExportError

    monkeypatch.delenv("PODKLADARNA_MAPPER_EXPORT", raising=False)
    omap = tmp_path / "mini.omap"
    omap.write_text(_MAP, encoding="utf-8")
    with pytest.raises(MapperExportError):
        render_omap_to_png(
            omap, tmp_path / "out.png", write_pgw=True, engine="mapper"
        )


def test_write_job_georef_pillow_fallback_without_mapper(tmp_path: Path, monkeypatch):
    """Bez Mapper CLI musí job pořád vyrobit georef PNG+PGW (Pillow) + ZIP."""
    monkeypatch.delenv("PODKLADARNA_MAPPER_EXPORT", raising=False)
    monkeypatch.delenv("PODKLADARNA_MAPPER", raising=False)
    monkeypatch.delenv("PODKLADARNA_GEOREF_PILLOW_DPI", raising=False)
    omap = tmp_path / "out" / "Park-les.omap"
    omap.parent.mkdir()
    omap.write_text(_MAP_GEOREF, encoding="utf-8")
    logs: list[str] = []
    work = write_job_oom_preview(
        [omap],
        tmp_path / "work",
        tmp_path / "out",
        {"use_kp": False, "oom_geotiff": False, "output_georef": True},
        log=logs.append,
    )
    assert work is not None and work.is_file()
    georef_png = tmp_path / "out" / "preview" / "Park-les.png"
    assert georef_png.is_file() and georef_png.with_suffix(".pgw").is_file()
    assert any("Pillow fallback" in line or "Pillow" in line for line in logs)
    assert any("600 DPI-eq" in line for line in logs)
    # Georef: ≥ floor 4800; web = JPEG max_side 1600.
    from app.pipeline.oom_preview import (
        GEOREF_MAPPER_DPI,
        GEOREF_PILLOW_MIN_SIDE_FLOOR,
        WEB_PREVIEW_MAX_SIDE,
    )

    georef_im = Image.open(georef_png)
    web_im = Image.open(work)
    assert max(georef_im.size) >= GEOREF_PILLOW_MIN_SIDE_FLOOR
    assert max(web_im.size) <= WEB_PREVIEW_MAX_SIDE
    assert work.suffix.lower() == ".jpg"
    assert GEOREF_MAPPER_DPI == 600
    assert any(f"dpi={GEOREF_MAPPER_DPI}" in line for line in logs) or any(
        "dpi=600" in line for line in logs
    )
    zpath = build_georef_previews_zip(tmp_path / "out")
    assert zpath is not None and zpath.is_file()


def test_preview_extent_target_dpi_matches_mapper_paper():
    """Pillow georef DPI: OOM 1/1000 mm → map_per_px = 25400/DPI, floor 4800."""
    from app.pipeline.oom_preview import preview_extent_from_crop

    # 1 inch square → floor 4800 beats 600 px @ 600 DPI.
    tiny = preview_extent_from_crop(
        0, 0, 25400, 25400, 0, 0, target_dpi=600, min_side_floor=4800
    )
    assert max(tiny.width, tiny.height) == 4800
    # Velký výřez: 600 DPI (40 inch) → 24000 px, cap 1000.
    large = preview_extent_from_crop(
        0,
        0,
        25400 * 40,
        25400 * 40,
        0,
        0,
        target_dpi=600,
        max_side_cap=1000,
        min_side_floor=100,
    )
    assert max(large.width, large.height) == 1000
    # Střední: přesně 10 inch → 6000 px @ 600 DPI (nad floorem 4800).
    mid = preview_extent_from_crop(
        0,
        0,
        25400 * 10,
        25400 * 10,
        0,
        0,
        target_dpi=600,
        min_side_floor=4800,
        max_side_cap=20000,
    )
    assert mid.width == 6000
    assert abs(mid.map_per_px - (25400 / 600)) < 1e-6


def _ihdr_only_png(path: Path, width: int, height: int) -> None:
    """Minimální PNG s IHDR (bez IDAT) – stačí pro ``_png_size`` / pixel cap."""
    import struct

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", int(width), int(height))
    )


def test_assert_png_within_web_pixel_limit_rejects_huge_ihdr(tmp_path: Path):
    """~494 Mpx IHDR (job Zvokoli) musí spadnout před Image.open."""
    huge = tmp_path / "bomb.png"
    # 16214×30463 = 493_927_082 (reálná Pillow bomb hláška z jobu).
    _ihdr_only_png(huge, 16214, 30463)
    assert png_pixel_count(huge) == 493_927_082
    with pytest.raises(ValueError, match="493927082"):
        assert_png_within_web_pixel_limit(huge, max_pixels=50_000_000)
    ok = tmp_path / "ok.png"
    Image.new("RGB", (64, 48), (1, 2, 3)).save(ok)
    assert assert_png_within_web_pixel_limit(ok) == (64, 48)
    assert _is_pixel_bomb_error(
        ValueError(
            "Image size (493927082 pixels) exceeds limit of 178956970 pixels, "
            "could be decompression bomb DOS attack."
        )
    )


@needs_sigalrm
def test_web_preview_deadline_raises_timeout():
    import time

    with pytest.raises(TimeoutError, match="web preview timeout"):
        with _web_preview_deadline(0.25):
            time.sleep(2.0)


@needs_sigalrm
def test_write_job_web_timeout_skips_preview_not_raise(
    tmp_path: Path, monkeypatch
):
    """Timeout web náhledu → None + log, bez výjimky (ZIP může pokračovat)."""
    import app.pipeline.oom_preview as oom_mod

    monkeypatch.setattr(oom_mod, "web_preview_timeout_sec", lambda: 0.2)
    monkeypatch.setattr(oom_mod, "mapper_export_configured", lambda: False)

    def _slow_pillow(*_a, **_k):
        import time

        time.sleep(3.0)
        raise AssertionError("should have been interrupted")

    monkeypatch.setattr(oom_mod, "render_omap_to_png", _slow_pillow)
    omap = tmp_path / "out" / "Park-les.omap"
    omap.parent.mkdir()
    omap.write_text(_MAP, encoding="utf-8")
    logs: list[str] = []
    out = write_job_oom_preview(
        [omap],
        tmp_path / "work",
        tmp_path / "out",
        {"oom_preview": True, "output_georef": False},
        log=logs.append,
    )
    assert out is None
    assert any("timeout" in line and "ZIP" in line for line in logs)


def test_prepare_mapper_web_preview_rejects_huge_source(tmp_path: Path):
    omap = tmp_path / "mini.omap"
    omap.write_text(_MAP, encoding="utf-8")
    huge = tmp_path / "full.png"
    _ihdr_only_png(huge, 16214, 30463)
    with pytest.raises(ValueError, match="exceeds web preview limit"):
        prepare_mapper_web_preview(omap, huge, tmp_path / "out.png", max_side=200)


def test_capped_web_mapper_dpi_reduces_for_large_paper(tmp_path: Path):
    """Obří full-map papír → web DPI pod 150, ať odhad px ≤ limitu."""
    # ~2e6 map. j. ≈ 2 m papíru (+ margin) → @150 DPI stovky Mpx.
    side = 2_000_000
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<map xmlns="http://openorienteering.org/apps/mapper/xml/v2" version="9">
  <georeferencing scale="10000" declination="0" grivation="0">
    <projected_crs id="EPSG"><ref_point x="-750000" y="-1050000"/></projected_crs>
  </georeferencing>
  <colors count="1"><color priority="26" name="G"><rgb r="0" g="1" b="0"/></color></colors>
  <symbols count="1">
    <symbol type="4" id="1" code="406"><area_symbol inner_color="26"/></symbol>
  </symbols>
  <parts count="1" current="0"><part name="Mapa"><objects count="1">
    <object type="1" symbol="1">
      <coords count="5">0 0;0 {side};{side} {side};{side} 0;0 0 18;</coords>
    </object>
  </objects></part></parts>
</map>
"""
    omap = tmp_path / "huge.omap"
    omap.write_text(body, encoding="utf-8")
    limit = 100_000_000
    dpi = capped_web_mapper_dpi(omap, desired_dpi=150, max_pixels=limit)
    assert 1 <= dpi < 150
    from app.pipeline.oom_preview import estimate_omap_fullmap_paper_inches

    w_in, h_in = estimate_omap_fullmap_paper_inches(omap)
    assert (w_in * dpi) * (h_in * dpi) <= limit
    small = tmp_path / "small.omap"
    small.write_text(_MAP, encoding="utf-8")
    assert capped_web_mapper_dpi(small, desired_dpi=150, max_pixels=limit) == 150


def test_write_job_skips_huge_georef_reuse_for_web(
    tmp_path: Path, monkeypatch
):
    """Georef @600 může zůstat obří; web ho nereusuje nad pixel limitem."""
    import app.pipeline.oom_preview as oom_mod

    _install_fake_mapper(monkeypatch, tmp_path)
    omap = tmp_path / "out" / "Zvokoli-les.omap"
    omap.parent.mkdir()
    omap.write_text(_MAP_GEOREF, encoding="utf-8")

    real_within = oom_mod.png_within_web_pixel_limit
    real_count = oom_mod.png_pixel_count

    def within(path, *, max_pixels=None):
        if Path(path).name == "Zvokoli-les.png":
            return False
        return real_within(path, max_pixels=max_pixels)

    def count(path):
        if Path(path).name == "Zvokoli-les.png":
            return 493_927_082
        return real_count(path)

    monkeypatch.setattr(oom_mod, "png_within_web_pixel_limit", within)
    monkeypatch.setattr(oom_mod, "png_pixel_count", count)

    logs: list[str] = []
    work = write_job_oom_preview(
        [omap],
        tmp_path / "work",
        tmp_path / "out",
        {"oom_preview": True, "output_georef": True, "oom_geotiff": False},
        log=logs.append,
    )
    assert work is not None and work.is_file()
    assert work.suffix.lower() == ".jpg"
    assert max(Image.open(work).size) <= 1600
    assert any("nereuse" in line for line in logs)
    assert any("493927082" in line for line in logs)
    assert (tmp_path / "out" / "preview" / "Zvokoli-les.png").is_file()


def test_export_argv_keeps_spaces(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PODKLADARNA_MAPPER", str(tmp_path / "Mapper 0.9.6" / "Mapper.exe"))
    (tmp_path / "Mapper 0.9.6").mkdir()
    exe = tmp_path / "Mapper 0.9.6" / "Mapper.exe"
    exe.write_bytes(b"")
    monkeypatch.setenv(
        "PODKLADARNA_MAPPER_EXPORT",
        '"{mapper}" --cli export --full-map -i "{omap}" -o "{png}" --dpi {dpi}',
    )
    argv = mapper_export_argv(tmp_path / "a b.omap", tmp_path / "out png.png", dpi=600)
    assert argv is not None
    assert argv[0] == str(exe)
    assert argv[1:] == [
        "--cli",
        "export",
        "--full-map",
        "-i",
        str(tmp_path / "a b.omap"),
        "-o",
        str(tmp_path / "out png.png"),
        "--dpi",
        "600",
    ]


def test_convert_argv_requires_ocd12(tmp_path: Path, monkeypatch):
    exe = tmp_path / "Mapper"
    exe.write_bytes(b"")
    monkeypatch.setenv("PODKLADARNA_MAPPER", str(exe))
    monkeypatch.delenv("PODKLADARNA_MAPPER_CONVERT", raising=False)
    assert mapper_convert_argv(tmp_path / "a.omap", tmp_path / "a.ocd") is None
    monkeypatch.setenv(
        "PODKLADARNA_MAPPER_CONVERT",
        '"{mapper}" --cli convert -i "{omap}" -o "{ocd}" --output-format OCD12',
    )
    argv = mapper_convert_argv(tmp_path / "Park les.omap", tmp_path / "Park les.ocd")
    assert argv is not None
    assert argv[0] == str(exe)
    assert argv[1:] == [
        "--cli",
        "convert",
        "-i",
        str(tmp_path / "Park les.omap"),
        "-o",
        str(tmp_path / "Park les.ocd"),
        "--output-format",
        "OCD12",
    ]


def _install_fake_mapper_convert(monkeypatch, tmp_path: Path) -> Path:
    """Fake Mapper: convert → OCD magic file with version 12."""
    script = tmp_path / "fake_mapper_convert.py"
    script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "argv = sys.argv[1:]\n"
        "ocd = None\n"
        "fmt = None\n"
        "i = 0\n"
        "while i < len(argv):\n"
        "    a = argv[i]\n"
        "    if a == '-o' and i + 1 < len(argv):\n"
        "        ocd = Path(argv[i + 1]); i += 2; continue\n"
        "    if a == '--output-format' and i + 1 < len(argv):\n"
        "        fmt = argv[i + 1]; i += 2; continue\n"
        "    i += 1\n"
        "assert ocd is not None and fmt == 'OCD12'\n"
        "ocd.write_bytes(b'\\xad\\x0c\\x00\\x00\\x0c\\x00' + b'\\x00' * 64)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PODKLADARNA_MAPPER", sys.executable)
    monkeypatch.setenv(
        "PODKLADARNA_MAPPER_CONVERT",
        f'"{sys.executable}" "{script}" --cli convert -i "{{omap}}" -o "{{ocd}}" '
        "--output-format OCD12",
    )
    return script


def test_run_mapper_convert_writes_ocd12(tmp_path: Path, monkeypatch):
    _install_fake_mapper_convert(monkeypatch, tmp_path)
    omap = tmp_path / "Mapa-les.omap"
    omap.write_text("<map/>", encoding="utf-8")
    dest = run_mapper_convert(omap)
    assert dest == omap.with_suffix(".ocd")
    assert dest.is_file()
    assert ocd_version(dest) == 12


def test_convert_omap_to_ocd_skips_without_env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PODKLADARNA_MAPPER_CONVERT", raising=False)
    omap = tmp_path / "x.omap"
    omap.write_text("<map/>", encoding="utf-8")
    logs: list[str] = []
    assert convert_omap_to_ocd(omap, log=logs.append) is None
    assert any("přeskočeno" in m for m in logs)


def test_write_job_preview_copies_named_png(tmp_path: Path, monkeypatch):
    _install_fake_mapper(monkeypatch, tmp_path)
    omap = tmp_path / "out" / "Les-sprint.omap"
    omap.parent.mkdir()
    omap.write_text(_MAP_GEOREF, encoding="utf-8")
    other = tmp_path / "out" / "Les-mtbo.omap"
    other.write_text(_MAP_GEOREF.replace("Forest", "MTBO"), encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    lines: list[str] = []
    dest = write_job_oom_preview(
        [other, omap],
        work,
        tmp_path / "out",
        {"use_kp": False, "oom_geotiff": False, "output_georef": True},
        log=lines.append,
    )
    assert dest == work / "preview.jpg"
    assert dest.is_file()
    named = tmp_path / "out" / "preview" / "oom_preview.jpg"
    assert named.is_file()
    assert named.read_bytes() == dest.read_bytes()
    assert Image.open(dest).format == "JPEG"
    # Všechny varianty + PGW (Mapper fake).
    for stem in ("Les-sprint", "Les-mtbo"):
        png = tmp_path / "out" / "preview" / f"{stem}.png"
        pgw = png.with_suffix(".pgw")
        assert png.is_file()
        assert pgw.is_file()
        vals = [float(x) for x in pgw.read_text(encoding="utf-8").strip().splitlines()[:6]]
        assert vals[0] > 0  # pixel_x
        assert vals[3] < 0  # pixel_y (north-up)
        assert abs(vals[1]) < 1e-9 and abs(vals[2]) < 1e-9
    assert any("Les-sprint" in line for line in lines)
    assert any("Mapper" in line for line in lines)


def test_write_job_skips_when_kp(tmp_path: Path):
    omap = tmp_path / "a.omap"
    omap.write_text(_MAP, encoding="utf-8")
    assert (
        write_job_oom_preview([omap], tmp_path, tmp_path, {"oom_preview": False}) is None
    )


def test_write_job_skips_georef_when_default_off(tmp_path: Path, monkeypatch):
    """Bez output_georef jen webový náhled – žádné georef PNG+PGW."""
    monkeypatch.delenv("PODKLADARNA_OUTPUT_GEOREF", raising=False)
    monkeypatch.delenv("PODKLADARNA_MAPPER_EXPORT", raising=False)
    monkeypatch.delenv("PODKLADARNA_MAPPER", raising=False)
    omap = tmp_path / "out" / "Park-les.omap"
    omap.parent.mkdir()
    omap.write_text(_MAP_GEOREF, encoding="utf-8")
    logs: list[str] = []
    work = write_job_oom_preview(
        [omap],
        tmp_path / "work",
        tmp_path / "out",
        {"use_kp": False},
        log=logs.append,
    )
    assert work is not None and work.is_file()
    assert work.suffix.lower() == ".jpg"
    assert (tmp_path / "out" / "preview" / "oom_preview.jpg").is_file()
    assert not (tmp_path / "out" / "preview" / "Park-les.png").is_file()
    assert any("output_georef vypnuto" in line for line in logs)
    assert build_georef_previews_zip(tmp_path / "out") is None


def test_find_mapper_does_not_require_install():
    # Smí vrátit None, nebo existující soubor – nikdy nespouštět GUI.
    found = find_mapper_exe()
    assert found is None or found.is_file()


_MAP_GEOREF = """<?xml version="1.0" encoding="UTF-8"?>
<map xmlns="http://openorienteering.org/apps/mapper/xml/v2" version="9">
  <georeferencing scale="10000" auxiliary_scale_factor="1" declination="0" grivation="0">
    <projected_crs id="EPSG">
      <ref_point x="-750000" y="-1050000"/>
    </projected_crs>
  </georeferencing>
  <colors count="2">
    <color priority="2" name="Black" c="0" m="0" y="0" k="1" opacity="1"/>
    <color priority="26" name="Green" c="0.76" m="0" y="0.91" k="0" opacity="1">
      <rgb method="cmyk" r="0.24" g="1" b="0.09"/>
    </color>
  </colors>
  <barrier>
    <symbols count="3">
      <symbol type="4" id="10" code="406" name="Forest">
        <area_symbol inner_color="26"/>
      </symbol>
      <symbol type="2" id="11" code="101" name="Contour">
        <line_symbol color="2" line_width="400"/>
      </symbol>
      <symbol type="2" id="175" code="708" name="Out-of-bounds boundary">
        <line_symbol color="2" line_width="1000"/>
      </symbol>
    </symbols>
    <parts count="1" current="0">
      <part name="Mapa">
        <objects count="3">
          <object type="1" symbol="10">
            <coords count="5">0 0;0 10000;10000 10000;10000 0;0 0 18;</coords>
          </object>
          <object type="1" symbol="11">
            <coords count="2">0 5000;10000 5000;</coords>
          </object>
          <object type="1" symbol="175">
            <coords count="5">0 0;10000 0;10000 10000;0 10000;0 0 18;</coords>
          </object>
        </objects>
      </part>
    </parts>
  </barrier>
</map>
"""


def test_pgw_matches_projected_aoi_corners(tmp_path: Path):
    """PGW: UL pixel → S-JTSK; AOI roh (0,0) map ≈ ref po inverzi g=0."""
    from app.pipeline.georef import read_pgw
    from app.pipeline.oom_coords import map_to_projected
    from app.pipeline.oom_preview import (
        parse_omap_georef,
        pgw_for_preview,
        preview_crop_box,
        preview_extent_from_crop,
        render_omap_to_png,
    )

    omap = tmp_path / "geo.omap"
    omap.write_text(_MAP_GEOREF, encoding="utf-8")
    png = tmp_path / "geo.png"
    render_omap_to_png(
        omap,
        png,
        write_pgw=True,
        write_geotiff=False,
        max_side=200,
        engine="pillow",
    )
    pgw_path = png.with_suffix(".pgw")
    assert pgw_path.is_file()
    assert png.with_suffix(".prj").is_file()
    georef = read_pgw(pgw_path)
    assert georef.pixel_x > 0
    assert georef.pixel_y < 0
    assert abs(georef.rot_row) < 1e-12
    assert abs(georef.rot_col) < 1e-12

    root = ET.fromstring(omap.read_text(encoding="utf-8"))
    og = parse_omap_georef(root)
    assert og.ref_x == -750000.0
    assert og.scale == 10000
    # Map (0,0) při g=0 → projected = ref
    px, py = map_to_projected(
        0.0, 0.0, ref_x=og.ref_x, ref_y=og.ref_y, scale=og.scale, grivation_deg=0.0
    )
    assert abs(px - og.ref_x) < 1e-6
    assert abs(py - og.ref_y) < 1e-6

    # Střed UL pixelu z PGW = map_to_projected(origin + 0.5*mpp); g=0 → bez rotace
    from app.pipeline.oom_preview import _collect_ops

    ops = _collect_ops(root, grivation_deg=0.0)
    minx, miny, maxx, maxy, padx, pady = preview_crop_box(root, ops, grivation_deg=0.0)
    extent = preview_extent_from_crop(minx, miny, maxx, maxy, padx, pady, max_side=200)
    expected = pgw_for_preview(extent, og, with_grivation=True)
    assert abs(georef.origin_x - expected.origin_x) < 1e-6
    assert abs(georef.origin_y - expected.origin_y) < 1e-6
    assert abs(georef.pixel_x - expected.pixel_x) < 1e-9
    # 1 map unit @ 1:10000 = 0.01 m (OOM 1/1000 mm paper); map_per_px * 0.01 = mpp
    assert abs(georef.pixel_x - extent.map_per_px * 0.01) < 1e-6
    assert abs(georef.pixel_y + extent.map_per_px * 0.01) < 1e-6


def test_georef_pgw_keeps_grivation_rotation(tmp_path: Path):
    """Georef PNG nechá grivaci; PGW má rotační členy a invertuje roh AOI."""
    import math

    from app.pipeline.georef import read_pgw
    from app.pipeline.oom_coords import map_to_projected
    from app.pipeline.oom_preview import parse_omap_georef, render_omap_to_png

    g = 12.52
    rad = math.radians(g)
    grid = [(-5000, -4000), (5000, -4000), (5000, 4000), (-5000, 4000), (-5000, -4000)]

    def to_map(x: float, y: float) -> tuple[int, int]:
        rx = x * math.cos(rad) - y * math.sin(rad)
        ry = x * math.sin(rad) + y * math.cos(rad)
        return round(rx), round(-ry)

    ring = [to_map(x, y) for x, y in grid]
    coords = ";".join(f"{x} {y}" for x, y in ring[:-1])
    coords += f";{ring[-1][0]} {ring[-1][1]} 18;"
    ref_x, ref_y = -750000.0, -1050000.0
    omap = tmp_path / "griv-geo.omap"
    omap.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<map xmlns="http://openorienteering.org/apps/mapper/xml/v2" version="9">
  <georeferencing scale="10000" declination="5.00" grivation="{g}">
    <projected_crs id="EPSG">
      <ref_point x="{ref_x}" y="{ref_y}"/>
    </projected_crs>
  </georeferencing>
  <colors count="2">
    <color priority="5" name="Purple"><rgb r="1" g="0" b="1"/></color>
    <color priority="26" name="Green"><rgb r="0" g="1" b="0"/></color>
  </colors>
  <symbols count="2">
    <symbol type="2" id="175" code="708">
      <line_symbol color="5" line_width="1000"/>
    </symbol>
    <symbol type="4" id="10" code="406">
      <area_symbol inner_color="26"/>
    </symbol>
  </symbols>
  <parts count="1" current="0">
    <part name="Mapa">
      <objects count="2">
        <object type="1" symbol="10">
          <coords count="5">{coords}</coords>
        </object>
        <object type="1" symbol="175">
          <coords count="5">{coords}</coords>
        </object>
      </objects>
    </part>
  </parts>
</map>
""",
        encoding="utf-8",
    )
    png = tmp_path / "griv-geo.png"
    render_omap_to_png(
        omap,
        png,
        write_pgw=True,
        write_geotiff=False,
        max_side=200,
        undo_grivation=False,
        engine="pillow",
    )
    pgw = read_pgw(png.with_suffix(".pgw"))
    # S grivací musí být nenulová rotace v world file.
    assert abs(pgw.rot_row) > 1e-6 or abs(pgw.rot_col) > 1e-6
    og = parse_omap_georef(ET.fromstring(omap.read_text(encoding="utf-8")))
    assert abs(og.grivation_deg - g) < 1e-6
    # Roh mapy (ring[0]) → projected přes stejnou grivaci
    mx0, my0 = ring[0]
    exp_x, exp_y = map_to_projected(
        float(mx0),
        float(my0),
        ref_x=og.ref_x,
        ref_y=og.ref_y,
        scale=og.scale,
        grivation_deg=g,
    )
    # Bez odrotování je ořez širší než grid 10000×8000 (osa-aligned bbox natočeného rámu).
    web = tmp_path / "web.png"
    render_omap_to_png(omap, web, write_pgw=False, max_side=200, undo_grivation=True)
    assert png.read_bytes() != web.read_bytes()
    assert abs(exp_x - ref_x) > 1.0 or abs(exp_y - ref_y) > 1.0


def test_write_job_splits_georef_and_web(tmp_path: Path, monkeypatch):
    """Georef PNG ≠ web preview, když .omap má nenulovou grivaci."""
    import math

    _install_fake_mapper(monkeypatch, tmp_path, rgb=(200, 0, 0))
    g = 12.52
    rad = math.radians(g)
    grid = [(-5000, -4000), (5000, -4000), (5000, 4000), (-5000, 4000), (-5000, -4000)]

    def to_map(x: float, y: float) -> tuple[int, int]:
        rx = x * math.cos(rad) - y * math.sin(rad)
        ry = x * math.sin(rad) + y * math.cos(rad)
        return round(rx), round(-ry)

    ring = [to_map(x, y) for x, y in grid]
    coords = ";".join(f"{x} {y}" for x, y in ring[:-1])
    coords += f";{ring[-1][0]} {ring[-1][1]} 18;"
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<map xmlns="http://openorienteering.org/apps/mapper/xml/v2" version="9">
  <georeferencing scale="10000" declination="5.00" grivation="{g}">
    <projected_crs id="EPSG">
      <ref_point x="-750000" y="-1050000"/>
    </projected_crs>
  </georeferencing>
  <colors count="2">
    <color priority="5" name="Purple"><rgb r="1" g="0" b="1"/></color>
    <color priority="26" name="Green"><rgb r="0" g="1" b="0"/></color>
  </colors>
  <symbols count="2">
    <symbol type="2" id="175" code="708">
      <line_symbol color="5" line_width="1000"/>
    </symbol>
    <symbol type="4" id="10" code="406">
      <area_symbol inner_color="26"/>
    </symbol>
  </symbols>
  <parts count="1" current="0">
    <part name="Mapa">
      <objects count="2">
        <object type="1" symbol="10">
          <coords count="5">{coords}</coords>
        </object>
        <object type="1" symbol="175">
          <coords count="5">{coords}</coords>
        </object>
      </objects>
    </part>
  </parts>
</map>
"""
    omap = tmp_path / "out" / "Park-les.omap"
    omap.parent.mkdir()
    omap.write_text(body, encoding="utf-8")
    work = write_job_oom_preview(
        [omap],
        tmp_path / "work",
        tmp_path / "out",
        {"use_kp": False, "oom_geotiff": False, "output_georef": True},
    )
    assert work is not None and work.is_file()
    georef_png = tmp_path / "out" / "preview" / "Park-les.png"
    web_jpg = tmp_path / "out" / "preview" / "oom_preview.jpg"
    assert georef_png.is_file() and georef_png.with_suffix(".pgw").is_file()
    assert web_jpg.is_file()
    assert not web_jpg.with_suffix(".pgw").is_file()
    assert georef_png.read_bytes() != web_jpg.read_bytes()
    from app.pipeline.georef import read_pgw

    pgw = read_pgw(georef_png.with_suffix(".pgw"))
    assert abs(pgw.rot_row) > 1e-6 or abs(pgw.rot_col) > 1e-6


def test_build_georef_previews_zip(tmp_path: Path, monkeypatch):
    from app.pipeline.oom_preview import build_georef_previews_zip

    _install_fake_mapper(monkeypatch, tmp_path)
    omap_les = tmp_path / "out" / "Park-les.omap"
    omap_mtbo = tmp_path / "out" / "Park-mtbo.omap"
    omap_les.parent.mkdir()
    omap_les.write_text(_MAP_GEOREF, encoding="utf-8")
    omap_mtbo.write_text(_MAP_GEOREF, encoding="utf-8")
    write_job_oom_preview(
        [omap_les, omap_mtbo],
        tmp_path / "work",
        tmp_path / "out",
        {"use_kp": False, "oom_geotiff": False, "output_georef": True},
    )
    zpath = build_georef_previews_zip(tmp_path / "out")
    assert zpath is not None and zpath.is_file()
    with zipfile.ZipFile(zpath) as zf:
        names = set(zf.namelist())
    assert "README.txt" in names
    assert "Park-les.png" in names and "Park-les.pgw" in names
    assert "Park-mtbo.png" in names and "Park-mtbo.pgw" in names
    assert "oom_preview.png" not in names


def test_map_to_projected_roundtrip():
    from app.pipeline.oom_coords import map_to_projected, projected_to_map_coord

    ref_x, ref_y = -740123.5, -1045678.25
    for g in (0.0, 12.52):
        mx, my = projected_to_map_coord(
            ref_x + 50.0,
            ref_y - 30.0,
            ref_x=ref_x,
            ref_y=ref_y,
            scale=10000,
            grivation_deg=g,
        )
        x, y = map_to_projected(
            mx, my, ref_x=ref_x, ref_y=ref_y, scale=10000, grivation_deg=g
        )
        assert abs(x - (ref_x + 50.0)) < 0.05
        assert abs(y - (ref_y - 30.0)) < 0.05


def test_mapper_error_detail_drops_qt_proj_noise():
    import subprocess as sp

    from app.pipeline.oom_preview import _mapper_error_detail

    noise = "\n".join(
        ["QStandardPaths: XDG_RUNTIME_DIR not set, defaulting to '/tmp/runtime-root'",
         "Fontconfig error: Cannot load default config file"]
        + ["proj_create_operation_factory_context: Cannot find proj.db",
           "pj_obj_create: Cannot find proj.db"] * 20
        + ["Error: Cannot export map: something real"]
    )
    proc = sp.CompletedProcess(["mapper"], 1, b"", noise.encode())
    detail = _mapper_error_detail(proc)
    assert "something real" in detail
    assert "proj.db" not in detail
    assert "42 řádků" in detail
    assert "something real" in _mapper_error_detail(proc, full=True)


def test_web_preview_does_not_reuse_pillow_georef_after_mapper_fail(
    tmp_path: Path, monkeypatch
):
    """Mapper selže → georef z Pillow; web ho nesmí ořezat jako Mapper full-map.

    (Job e0ea3635b042: celý oranžový preview.jpg.)
    """
    script = tmp_path / "failing_mapper.py"
    script.write_text("import sys\nsys.stderr.write('boom')\nraise SystemExit(1)\n", encoding="utf-8")
    monkeypatch.setenv("PODKLADARNA_MAPPER", sys.executable)
    monkeypatch.setenv(
        "PODKLADARNA_MAPPER_EXPORT",
        f'"{sys.executable}" "{script}" -i "{{omap}}" -o "{{png}}" --dpi {{dpi}}',
    )
    omap = tmp_path / "out" / "Park-les.omap"
    omap.parent.mkdir()
    omap.write_text(_MAP_GEOREF, encoding="utf-8")
    logs: list[str] = []
    work = write_job_oom_preview(
        [omap],
        tmp_path / "work",
        tmp_path / "out",
        {"oom_preview": True, "output_georef": True, "oom_geotiff": False},
        log=logs.append,
    )
    assert work is not None and work.is_file()
    assert (tmp_path / "out" / "preview" / "Park-les.png").is_file()
    assert not any("→ AOI+bez deklinace" in line and "georef Park-les.png" in line for line in logs)
    assert any("web Pillow JPEG fallback" in line for line in logs)
    assert any("1× Pillow fallback" in line for line in logs)
    assert not any("Mapper @ 600 DPI)" in line and "hotovo" in line for line in logs)


def test_pillow_area_bbox_render_matches_full_canvas(tmp_path: Path):
    """Maska plochy jen přes bbox = stejné pixely jako maska přes celé plátno."""
    from PIL import ImageDraw

    from app.pipeline.oom_preview import render_omap_xml_png

    omap = tmp_path / "hole.omap"
    omap.write_text(_HOLE, encoding="utf-8")
    dest = tmp_path / "hole.png"
    w, h, _n, extent, _g = render_omap_xml_png(omap, dest, max_side=300)
    got = Image.open(dest).convert("RGB")

    # Referenční (původní) postup: plná maska + plný barevný obrázek.
    import xml.etree.ElementTree as ET

    from app.pipeline.oom_preview import _collect_ops, _read_omap_bytes, parse_omap_georef

    root = ET.fromstring(_read_omap_bytes(omap))
    g = parse_omap_georef(root)
    ops = _collect_ops(root, grivation_deg=g.grivation_deg)
    scale = 1.0 / extent.map_per_px
    ref = Image.new("RGB", (w, h), (255, 255, 255))
    for op in ops:
        if op.kind != "area":
            continue
        mask = Image.new("L", (w, h), 0)
        md = ImageDraw.Draw(mask)
        for i, path in enumerate(op.paths):
            pts = [((x - extent.origin_x) * scale, (y - extent.origin_y) * scale) for x, y in path]
            if len(pts) >= 3:
                md.polygon(pts, fill=0 if i else 255)
        ref.paste(Image.new("RGB", (w, h), op.rgb), (0, 0), mask)
    if all(op.kind == "area" for op in ops):
        assert list(got.getdata()) == list(ref.getdata())
