"""Náhled PNG z .omap – vestavěný render a volitelný Mapper CLI."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

from app.pipeline.oom_preview import (
    find_mapper_exe,
    mapper_export_argv,
    oom_preview_enabled,
    render_omap_to_png,
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


def test_hole_stays_paper_white(tmp_path: Path):
    omap = tmp_path / "hole.omap"
    omap.write_text(_HOLE, encoding="utf-8")
    png = tmp_path / "preview.png"
    render_omap_to_png(omap, png, max_side=200)
    image = Image.open(png).convert("RGB")
    cx, cy = image.size[0] // 2, image.size[1] // 2
    assert image.getpixel((cx, cy)) == (255, 255, 255)
    assert image.getpixel((24, 24))[1] > 200


def test_mapper_cli_template_overrides_xml(tmp_path: Path, monkeypatch):
    omap = tmp_path / "mini.omap"
    omap.write_text(_MAP, encoding="utf-8")
    script = tmp_path / "fake_mapper.py"
    script.write_text(
        "import sys\n"
        "from PIL import Image\n"
        "Image.new('RGB', (8, 8), (255, 0, 0)).save(sys.argv[1], format='PNG')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "PODKLADARNA_MAPPER_EXPORT",
        f'"{sys.executable}" "{script}" "{{png}}" "{{omap}}"',
    )
    png = tmp_path / "out.png"
    summary = render_omap_to_png(omap, png)
    assert summary == "mapper-cli"
    assert Image.open(png).getpixel((0, 0)) == (255, 0, 0)


def test_export_argv_keeps_spaces(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PODKLADARNA_MAPPER", str(tmp_path / "Mapper 0.9.6" / "Mapper.exe"))
    (tmp_path / "Mapper 0.9.6").mkdir()
    exe = tmp_path / "Mapper 0.9.6" / "Mapper.exe"
    exe.write_bytes(b"")
    monkeypatch.setenv(
        "PODKLADARNA_MAPPER_EXPORT",
        '"{mapper}" --export "{png}" "{omap}"',
    )
    argv = mapper_export_argv(tmp_path / "a b.omap", tmp_path / "out png.png")
    assert argv is not None
    assert argv[0] == str(exe)
    assert argv[1:] == ["--export", str(tmp_path / "out png.png"), str(tmp_path / "a b.omap")]


def test_write_job_preview_copies_named_png(tmp_path: Path):
    omap = tmp_path / "out" / "Les-sprint.omap"
    omap.parent.mkdir()
    omap.write_text(_MAP, encoding="utf-8")
    other = tmp_path / "out" / "Les-mtbo.omap"
    other.write_text(_HOLE, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    lines: list[str] = []
    dest = write_job_oom_preview(
        [other, omap],
        work,
        tmp_path / "out",
        {"use_kp": False},
        log=lines.append,
    )
    assert dest == work / "preview.png"
    assert dest.is_file()
    named = tmp_path / "out" / "preview" / "oom_preview.png"
    assert named.is_file()
    assert named.read_bytes() == dest.read_bytes()
    assert any("Les-sprint.omap" in line for line in lines)


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
