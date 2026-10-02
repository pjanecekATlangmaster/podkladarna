"""Náhled PNG z .omap – vestavěný render a volitelný Mapper CLI."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from PIL import Image

from app.pipeline.oom_preview import (
    aoi_frame_bbox,
    find_mapper_exe,
    map_grivation_deg,
    mapper_export_argv,
    oom_preview_enabled,
    render_omap_to_png,
    undo_grivation_xy,
    write_job_oom_preview,
)

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
    assert oom_preview_enabled({"use_kp": False}) is True
    assert oom_preview_enabled({"use_kp": True}) is False
    assert oom_preview_enabled({"use_kp": True, "oom_preview": True}) is True
    assert oom_preview_enabled({"use_kp": False, "oom_preview": False}) is False


def test_env_can_disable(monkeypatch):
    monkeypatch.setenv("PODKLADARNA_OOM_PREVIEW", "0")
    assert oom_preview_enabled({"use_kp": False}) is False


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


def test_hole_stays_paper_white(tmp_path: Path):
    omap = tmp_path / "hole.omap"
    omap.write_text(_HOLE, encoding="utf-8")
    png = tmp_path / "preview.png"
    render_omap_to_png(omap, png, max_side=200)
    image = Image.open(png).convert("RGB")
    cx, cy = image.size[0] // 2, image.size[1] // 2
    assert image.getpixel((cx, cy)) == (255, 255, 255)
    assert image.getpixel((24, 24))[1] > 200


def test_mapper_cli_used_for_georef_not_web(tmp_path: Path, monkeypatch):
    """Web = Pillow; georef = Mapper CLI when configured."""
    omap = tmp_path / "mini.omap"
    omap.write_text(_MAP, encoding="utf-8")
    script = tmp_path / "fake_mapper.py"
    script.write_text(
        "import sys\n"
        "from PIL import Image\n"
        "Image.new('RGB', (8, 8), (255, 0, 0)).save(sys.argv[-1], format='PNG')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "PODKLADARNA_MAPPER_EXPORT",
        f'"{sys.executable}" "{script}" "{{omap}}" "{{png}}"',
    )
    web = tmp_path / "web.png"
    web_summary = render_omap_to_png(omap, web, engine="pillow")
    assert web_summary.startswith("xml")
    assert Image.open(web).getpixel((0, 0)) != (255, 0, 0)

    geo = tmp_path / "geo.png"
    geo_summary = render_omap_to_png(
        omap, geo, write_pgw=True, write_geotiff=False, engine="mapper", dpi=150
    )
    assert geo_summary.startswith("mapper-cli")
    assert "dpi=150" in geo_summary
    assert Image.open(geo).getpixel((0, 0)) == (255, 0, 0)
    assert geo.with_suffix(".pgw").is_file()


def test_georef_mapper_missing_raises(tmp_path: Path, monkeypatch):
    from app.pipeline.oom_preview import MapperExportError

    monkeypatch.delenv("PODKLADARNA_MAPPER_EXPORT", raising=False)
    monkeypatch.delenv("PODKLADARNA_MAPPER", raising=False)
    monkeypatch.delenv("MAPPER", raising=False)
    omap = tmp_path / "mini.omap"
    omap.write_text(_MAP, encoding="utf-8")
    with pytest.raises(MapperExportError):
        render_omap_to_png(
            omap, tmp_path / "out.png", write_pgw=True, engine="mapper", dpi=600
        )


def test_export_argv_keeps_spaces_and_dpi(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PODKLADARNA_MAPPER", str(tmp_path / "Mapper 0.9.6" / "Mapper.exe"))
    (tmp_path / "Mapper 0.9.6").mkdir()
    exe = tmp_path / "Mapper 0.9.6" / "Mapper.exe"
    exe.write_bytes(b"")
    monkeypatch.setenv(
        "PODKLADARNA_MAPPER_EXPORT",
        '"{mapper}" --cli export --full-map -i "{omap}" -o "{png}" --dpi {dpi}',
    )
    argv = mapper_export_argv(
        tmp_path / "a b.omap", tmp_path / "out png.png", dpi=600
    )
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


def test_resolve_oom_export_dpi_defaults():
    from app.pipeline.oom_preview import resolve_oom_export_dpi

    assert resolve_oom_export_dpi(None) == 600
    assert resolve_oom_export_dpi({}) == 600
    assert resolve_oom_export_dpi({"oom_export_dpi": 150}) == 150
    assert resolve_oom_export_dpi({"oom_export_dpi": "300"}) == 300
    assert resolve_oom_export_dpi({"oom_export_dpi": 9999}) == 1200


def _install_fake_mapper(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "fake_mapper.py"
    script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from PIL import Image\n"
        "png = Path(sys.argv[-1])\n"
        "Image.new('RGB', (40, 30), (200, 40, 40)).save(png, format='PNG')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "PODKLADARNA_MAPPER_EXPORT",
        f'"{sys.executable}" "{script}" "{{omap}}" "{{png}}"',
    )


def test_write_job_preview_copies_named_png(tmp_path: Path, monkeypatch):
    _install_fake_mapper(tmp_path, monkeypatch)
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
        {"use_kp": False, "oom_geotiff": False, "oom_export_dpi": 150},
        log=lines.append,
    )
    assert dest == work / "preview.png"
    assert dest.is_file()
    named = tmp_path / "out" / "preview" / "oom_preview.png"
    assert named.is_file()
    assert named.read_bytes() == dest.read_bytes()
    # Všechny varianty + PGW (Mapper CLI).
    for stem in ("Les-sprint", "Les-mtbo"):
        png = tmp_path / "out" / "preview" / f"{stem}.png"
        pgw = png.with_suffix(".pgw")
        assert png.is_file()
        assert pgw.is_file()
        vals = [float(x) for x in pgw.read_text(encoding="utf-8").strip().splitlines()[:6]]
        assert vals[0] > 0  # pixel_x
        assert vals[3] < 0  # pixel_y (north-up)
    assert any("Les-sprint" in line for line in lines)
    assert any("DPI=150" in line for line in lines)
    # Web Pillow ≠ georef Mapper PNG.
    assert named.read_bytes() != (tmp_path / "out" / "preview" / "Les-sprint.png").read_bytes()


def test_write_job_skips_georef_without_mapper(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PODKLADARNA_MAPPER_EXPORT", raising=False)
    monkeypatch.delenv("PODKLADARNA_MAPPER", raising=False)
    monkeypatch.delenv("MAPPER", raising=False)
    omap = tmp_path / "out" / "Park-les.omap"
    omap.parent.mkdir()
    omap.write_text(_MAP_GEOREF, encoding="utf-8")
    lines: list[str] = []
    dest = write_job_oom_preview(
        [omap],
        tmp_path / "work",
        tmp_path / "out",
        {"use_kp": False, "oom_geotiff": False},
        log=lines.append,
    )
    assert dest is not None and dest.is_file()
    assert (tmp_path / "out" / "preview" / "oom_preview.png").is_file()
    assert not (tmp_path / "out" / "preview" / "Park-les.png").is_file()
    assert any("Mapper CLI není nakonfigurovaný" in line for line in lines)


def test_write_job_skips_when_kp(tmp_path: Path):
    omap = tmp_path / "a.omap"
    omap.write_text(_MAP, encoding="utf-8")
    assert (
        write_job_oom_preview([omap], tmp_path, tmp_path, {"use_kp": True}) is None
    )


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
        omap, png, write_pgw=True, write_geotiff=False, max_side=200, engine="pillow"
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
    """Georef PNG (Mapper) ≠ web preview (Pillow)."""
    import math

    _install_fake_mapper(tmp_path, monkeypatch)
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
        {"use_kp": False, "oom_geotiff": False, "oom_export_dpi": 300},
    )
    assert work is not None and work.is_file()
    georef_png = tmp_path / "out" / "preview" / "Park-les.png"
    web_png = tmp_path / "out" / "preview" / "oom_preview.png"
    assert georef_png.is_file() and georef_png.with_suffix(".pgw").is_file()
    assert web_png.is_file()
    assert not web_png.with_suffix(".pgw").is_file()
    assert georef_png.read_bytes() != web_png.read_bytes()
    from app.pipeline.georef import read_pgw

    pgw = read_pgw(georef_png.with_suffix(".pgw"))
    assert pgw.pixel_x != 0 and pgw.pixel_y != 0


def test_build_georef_previews_zip(tmp_path: Path, monkeypatch):
    from app.pipeline.oom_preview import build_georef_previews_zip

    _install_fake_mapper(tmp_path, monkeypatch)
    omap_les = tmp_path / "out" / "Park-les.omap"
    omap_mtbo = tmp_path / "out" / "Park-mtbo.omap"
    omap_les.parent.mkdir()
    omap_les.write_text(_MAP_GEOREF, encoding="utf-8")
    omap_mtbo.write_text(_MAP_GEOREF, encoding="utf-8")
    write_job_oom_preview(
        [omap_les, omap_mtbo],
        tmp_path / "work",
        tmp_path / "out",
        {"use_kp": False, "oom_geotiff": False},
    )
    zpath = build_georef_previews_zip(tmp_path / "out")
    assert zpath is not None and zpath.is_file()
    with zipfile.ZipFile(zpath) as zf:
        names = set(zf.namelist())
    assert "README.txt" in names
    assert "Park-les.png" in names and "Park-les.pgw" in names
    assert "Park-mtbo.png" in names and "Park-mtbo.pgw" in names
    assert "oom_preview.png" not in names


def test_mapper_package_zip_excludes_georef_oom_png(tmp_path: Path, monkeypatch):
    """ZIP s .omap neobsahuje georef OOM PNG – ty patří jen do malého georef ZIPu."""
    from app.pipeline.oom_preview import build_georef_previews_zip
    from app.pipeline.package_oom import build_oom_zip

    _install_fake_mapper(tmp_path, monkeypatch)
    out = tmp_path / "out"
    work = tmp_path / "work"
    out.mkdir()
    work.mkdir()
    omap = out / "Park-les.omap"
    omap.write_text(_MAP_GEOREF, encoding="utf-8")
    write_job_oom_preview(
        [omap],
        work,
        out,
        {"use_kp": False, "oom_geotiff": False},
    )
    georef_zip = build_georef_previews_zip(out)
    assert georef_zip is not None
    dest = out / "podkladarna.zip"
    build_oom_zip(
        work,
        dest,
        zabaged_clean=None,
        metadata={
            "label": "test",
            "scale": 10000,
            "contour_interval_m": 5,
            "citation": "test",
            "use_kp": False,
        },
        omap_paths=[omap],
        include_png=True,
        include_dxf=False,
        include_cliffs=False,
    )
    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
    assert "Park-les.omap" in names
    assert "preview/oom_preview.png" in names
    assert "preview/Park-les.png" not in names
    assert "preview/Park-les.pgw" not in names
    with zipfile.ZipFile(georef_zip) as zf:
        geo_names = set(zf.namelist())
    assert "Park-les.png" in geo_names
    assert "Park-les.pgw" in geo_names


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
