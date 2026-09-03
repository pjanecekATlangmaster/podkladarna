from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.pipeline.karttapullautin_dxf import collect_dxf_for_zip
from app.pipeline.reference_layers import HILLSHADE_VARIANTS

# Skupiny v okně šablon OOM (atribut group=).
GROUP_REFERENCE = 1
GROUP_KP = 2

DXF_LABELS: dict[str, str] = {
    "cliffs_small.dxf": "Srázy malé (DXF)",
    "cliffs_large.dxf": "Srázy velké (DXF)",
    "dotknolls.dxf": "Knolíky (DXF)",
}


@dataclass(frozen=True)
class OomTemplate:
    kind: str  # image | ogr
    label: str
    relpath: str
    visible: bool = True
    opacity: float = 1.0
    group: int | None = None

    @property
    def filename(self) -> str:
        return Path(self.relpath).name


OOM_REFERENCE_SPECS: tuple[tuple[str, str, str, float, bool], ...] = (
    ("orthophoto", "Ortofoto ČÚZK", "references/orthophoto.png", 1.0, True),
    ("osm", "OpenStreetMap", "references/osm.png", 0.85, True),
    ("ztm", "Základní mapa ČÚZK (ZTM)", "references/mapa_ztm.png", 0.88, True),
    ("dmpok", "Náhled DMP OK", "references/dmpok_nahled.png", 0.65, False),
    *(
        (key, label, f"references/{filename}", opacity, visible)
        for key, _layer, filename, label, opacity, visible in HILLSHADE_VARIANTS
    ),
)


def collect_oom_templates(
    kp_cwd: Path,
    *,
    built_refs: dict[str, Path] | None = None,
    include_dxf: bool = True,
    include_dxf_templates: bool = False,
) -> list[OomTemplate]:
    """Šablony zdola nahoru. PNG z Karttapullautinu = kontrolní podklad.

    Vektory KP a ZABAGED jdou do .omap jako editovatelné objekty (viz oom_import).
    """
    templates: list[OomTemplate] = []

    if built_refs:
        for key, label, relpath, opacity, visible in OOM_REFERENCE_SPECS:
            path = built_refs.get(key)
            if path and path.is_file():
                templates.append(
                    OomTemplate(
                        "image",
                        label,
                        relpath,
                        visible=visible,
                        opacity=opacity,
                        group=GROUP_REFERENCE,
                    )
                )

    depr = kp_cwd / "pullautus_depr.png"
    if depr.is_file():
        templates.append(
            OomTemplate(
                "image",
                "Karttapullautin reliéf",
                "relief/pullautus_depr.png",
                visible=False,
                opacity=0.75,
                group=GROUP_KP,
            )
        )

    if (kp_cwd / "pullautus.png").is_file():
        templates.append(
            OomTemplate(
                "image",
                "Karttapullautin (PNG – kontrola)",
                "basemap/pullautus.png",
                visible=True,
                opacity=0.45,
                group=GROUP_KP,
            )
        )

    if include_dxf and include_dxf_templates:
        temp = kp_cwd / "temp"
        if temp.is_dir():
            for zip_name in sorted(collect_dxf_for_zip(temp)):
                templates.append(
                    OomTemplate(
                        "ogr",
                        DXF_LABELS.get(zip_name, zip_name),
                        f"karttapullautin/{zip_name}",
                        visible=False,
                        opacity=1.0,
                        group=GROUP_KP,
                    )
                )

    return templates
