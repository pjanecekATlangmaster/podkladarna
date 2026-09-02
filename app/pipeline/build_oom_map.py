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
from app.pipeline.oom_import import OomObjectPart
from app.pipeline.oom_layers import OomTemplate
from app.pipeline.oom_symbols import colors_and_symbols_xml, symbol_set_path

__all__ = [
    "CRS_LABEL",
    "CRS_PROJ4",
    "GEOGRAPHIC_CRS_PROJ4",
    "build_oom_map_xml",
    "projected_to_wgs84",
    "write_oom_map",
]


def _template_xml(tmpl: OomTemplate) -> str:
    open_attr = "true" if tmpl.visible else "false"
    fname = html.escape(tmpl.filename)
    relpath = html.escape(tmpl.relpath)
    group_attr = (
        f' group="{tmpl.group}"' if tmpl.group is not None else ""
    )
    if tmpl.kind == "ogr":
        return (
            f'            <template type="OgrTemplate" open="{open_attr}"'
            f' name="{fname}" path="{relpath}" relpath="{relpath}"'
            f' georef="true"{group_attr}/>'
        )
    crs = html.escape(CRS_PROJ4)
    return (
        f'            <template type="TemplateImage" open="{open_attr}"'
        f' name="{fname}" path="{relpath}" relpath="{relpath}"'
        f' georef="true"{group_attr}>\n'
        f"                <crs_spec>{crs}</crs_spec>\n"
        f"            </template>"
    )


def _parts_xml(parts: list[OomObjectPart]) -> tuple[str, int]:
    if not parts:
        return (
            '        <parts count="1" current="0">\n'
            '            <part name="Mapa">\n'
            '                <objects count="0"/>\n'
            "            </part>\n"
            "        </parts>",
            0,
        )
    blocks: list[str] = []
    total = 0
    for part in parts:
        total += part.count
        if part.objects_xml:
            blocks.append(
                f'            <part name="{html.escape(part.name)}">\n'
                f'                <objects count="{part.count}">\n'
                f"{part.objects_xml}\n"
                "                </objects>\n"
                "            </part>"
            )
        else:
            blocks.append(
                f'            <part name="{html.escape(part.name)}">\n'
                f'                <objects count="0"/>\n'
                "            </part>"
            )
    current = len(parts) - 1
    return (
        f'        <parts count="{len(parts)}" current="{current}">\n'
        + "\n".join(blocks)
        + "\n        </parts>",
        total,
    )


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
    templates: list[OomTemplate],
    object_parts: list[OomObjectPart] | None = None,
    preset_id: str,
) -> str:
    """.omap s kontrolním PNG a editovatelnými objekty mapy."""
    colors_xml, symbols_xml = colors_and_symbols_xml(symbol_set_path(preset_id, scale))
    templates_xml = "\n".join(_template_xml(t) for t in templates)
    template_refs = "\n".join(
        f'                    <ref template="{i}" visible="{"true" if t.visible else "false"}"'
        f' opacity="{t.opacity:.2f}"/>'
        for i, t in enumerate(templates)
    )
    if template_refs:
        template_refs += "\n"
    parts_xml, _ = _parts_xml(object_parts or [])
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
{parts_xml}
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
    templates: list[OomTemplate],
    preset_id: str,
    object_parts: list[OomObjectPart] | None = None,
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
            object_parts=object_parts,
            preset_id=preset_id,
        ),
        encoding="utf-8",
    )
    return dest
