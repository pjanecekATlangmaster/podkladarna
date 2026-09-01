from __future__ import annotations

import html
from pathlib import Path

from app.pipeline.crs_5514 import (
    CRS_LABEL,
    CRS_PROJ4,
    GEOGRAPHIC_CRS_PROJ4,
    projected_to_wgs84,
)
from app.pipeline.oom_georef import oom_north_angles
from app.pipeline.oom_symbols import colors_and_symbols_xml, symbol_set_path

# Zpětná kompatibilita importů z build_oom_map.
__all__ = [
    "CRS_LABEL",
    "CRS_PROJ4",
    "GEOGRAPHIC_CRS_PROJ4",
    "build_oom_map_xml",
    "projected_to_wgs84",
    "write_oom_map",
]

def _template_xml(
    idx: int,
    name: str,
    relpath: str,
    *,
    open_layer: bool = False,
    opacity: float = 1.0,
) -> str:
    open_attr = "true" if open_layer else "false"
    crs = html.escape(CRS_PROJ4)
    return f"""            <template type="TemplateImage" open="{open_attr}" name="{html.escape(name)}" path="{html.escape(relpath)}" relpath="{html.escape(relpath)}" georef="true" opacity="{opacity:.2f}">
                <crs_spec>{crs}</crs_spec>
            </template>"""


def build_oom_map_xml(
    *,
    map_name: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    ref_lat: float,
    ref_lon: float,
    declination: float,
    grivation: float,
    templates: list[tuple[str, str, bool] | tuple[str, str, bool, float]],
    preset_id: str,
) -> str:
    """.omap s referenčními šablonami a oficiální symbolikou IOF (ISSprOM / ISOM)."""
    colors_xml, symbols_xml = colors_and_symbols_xml(symbol_set_path(preset_id, scale))
    template_blocks = []
    for idx, item in enumerate(templates):
        if len(item) == 3:
            label, relpath, visible = item
            opacity = 1.0
        else:
            label, relpath, visible, opacity = item
        template_blocks.append(
            _template_xml(idx, label, relpath, open_layer=visible, opacity=opacity)
        )
    templates_xml = "\n".join(template_blocks)
    template_refs = "\n".join(
        f'                    <ref template="{i}" visible="true" opacity="1"/>'
        for i in range(len(templates))
    )
    if template_refs:
        template_refs += "\n"
    front = max(0, len(templates) - 1)
    safe_name = html.escape(map_name)
    crs = html.escape(CRS_PROJ4)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<map xmlns="http://openorienteering.org/apps/mapper/xml/v2" version="9">
    <notes>Podkladárna – {safe_name}</notes>
    <georeferencing scale="{scale}" auxiliary_scale_factor="1" declination="{declination:.2f}" grivation="{grivation:.2f}">
        <projected_crs id="EPSG">
            <spec language="PROJ.4">{crs}</spec>
            <parameter>5514</parameter>
            <ref_point x="{ref_x:.6f}" y="{ref_y:.6f}"/>
        </projected_crs>
        <geographic_crs id="Geographic coordinates">
            <spec language="PROJ.4">{GEOGRAPHIC_CRS_PROJ4}</spec>
            <ref_point_deg lat="{ref_lat:.8f}" lon="{ref_lon:.8f}"/>
        </geographic_crs>
    </georeferencing>
    {colors_xml}
    <barrier version="6" required="0.6.0">
        {symbols_xml}
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
{template_refs}                </templates>
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
    ref_lat: float,
    ref_lon: float,
    templates: list[tuple[str, str, bool] | tuple[str, str, bool, float]],
    preset_id: str,
    declination: float | None = None,
    grivation: float | None = None,
) -> Path:
    if declination is None or grivation is None:
        declination, grivation = oom_north_angles(ref_x, ref_y)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        build_oom_map_xml(
            map_name=map_name,
            scale=scale,
            ref_x=ref_x,
            ref_y=ref_y,
            ref_lat=ref_lat,
            ref_lon=ref_lon,
            declination=declination,
            grivation=grivation,
            templates=templates,
            preset_id=preset_id,
        ),
        encoding="utf-8",
    )
    return dest
