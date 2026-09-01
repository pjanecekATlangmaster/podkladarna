from __future__ import annotations

import html
from pathlib import Path

CRS_SPEC = "EPSG:5514"


def _template_xml(
    idx: int,
    name: str,
    relpath: str,
    *,
    open_layer: bool = False,
    opacity: float = 1.0,
) -> str:
    open_attr = "true" if open_layer else "false"
    return f"""            <template type="TemplateImage" open="{open_attr}" name="{html.escape(name)}" path="{html.escape(relpath)}" relpath="{html.escape(relpath)}" georef="true" opacity="{opacity:.2f}">
                <crs_spec>{CRS_SPEC}</crs_spec>
            </template>"""


def build_oom_map_xml(
    *,
    map_name: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    templates: list[tuple[str, str, bool]],
) -> str:
    """Minimální .omap s referenčními šablonami (bez objektů mapy)."""
    template_blocks = []
    for idx, (label, relpath, visible) in enumerate(templates):
        template_blocks.append(_template_xml(idx, label, relpath, open_layer=visible))
    templates_xml = "\n".join(template_blocks)
    front = max(0, len(templates) - 1)
    safe_name = html.escape(map_name)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<map xmlns="http://openorienteering.org/apps/mapper/xml/v2" version="9">
    <notes>Podkladárna – {safe_name}</notes>
    <georeferencing scale="{scale}" auxiliary_scale_factor="1" declination="0" grivation="0">
        <projected_crs id="EPSG">
            <spec language="PROJ.4">EPSG:5514</spec>
            <parameter>EPSG:5514</parameter>
            <ref_point x="{ref_x:.3f}" y="{ref_y:.3f}"/>
        </projected_crs>
    </georeferencing>
    <colors count="0"/>
    <barrier version="6" required="0.6.0">
        <symbols count="0"/>
        <parts count="1" current="0">
            <part name="Default">
                <objects count="0"/>
            </part>
        </parts>
        <templates count="{len(templates)}" first_front_template="{front}">
{templates_xml}
            <defaults meters_per_pixel="0" dpi="0" scale="0"/>
        </templates>
        <view>
            <grid color="#646464" display="0" alignment="0" additional_rotation="0" unit="1" h_spacing="500" v_spacing="500" h_offset="0" v_offset="0" snapping_enabled="true"/>
            <map_view zoom="1" position_x="0" position_y="0">
                <map opacity="1" visible="true"/>
                <templates count="{len(templates)}">
{"".join(f'                    <ref template="{i}" visible="true" opacity="1"/>\n' for i in range(len(templates)))}                </templates>
            </map_view>
        </view>
    </barrier>
</map>
"""


def write_oom_map(
    dest: Path,
    *,
    map_name: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    templates: list[tuple[str, str, bool]],
) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        build_oom_map_xml(
            map_name=map_name,
            scale=scale,
            ref_x=ref_x,
            ref_y=ref_y,
            templates=templates,
        ),
        encoding="utf-8",
    )
    return dest
