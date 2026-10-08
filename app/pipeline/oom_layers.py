from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.pipeline.reference_layers import HILLSHADE_VARIANTS, orthophoto_items

# Skupiny v okně šablon OOM (atribut group=).
GROUP_REFERENCE = 1
GROUP_OSM = 2


@dataclass(frozen=True)
class OomTemplate:
    kind: str  # image | ogr
    label: str
    relpath: str
    visible: bool = True
    opacity: float = 1.0
    group: int | None = None
    # open= v .omap: šablona je v seznamu podkladů (i když visible=false).
    loaded: bool = True

    @property
    def filename(self) -> str:
        return Path(self.relpath).name


OOM_REFERENCE_SPECS: tuple[tuple[str, str, str, float, bool], ...] = (
    # Všechny PNG vypnuté – výchozí pohled = jen vektorové objekty mapy.
    ("orthophoto", "Ortofoto ČÚZK", "references/orthophoto.jpg", 1.0, False),
    ("ztm", "Základní mapa ČÚZK (ZTM)", "references/mapa_ztm.png", 0.88, False),
    ("katastr", "Katastrální mapa", "references/katastr.png", 0.9, False),
    ("dmpok", "Náhled DMP OK", "references/dmpok_nahled.png", 0.65, False),
    *(
        (key, label, f"references/{filename}", opacity, False)
        for key, _layer, filename, label, opacity, _visible in HILLSHADE_VARIANTS
    ),
)

OOM_OSM_REF_SPEC = (
    "osm",
    "OpenStreetMap",
    "references/osm.png",
    0.55,
    False,
)


def collect_oom_templates(
    work_dir: Path,
    *,
    built_refs: dict[str, Path] | None = None,
) -> list[OomTemplate]:
    """Referenční šablony zdola nahoru (všechny pod mapou).

    Vektory (LiDAR, ZABAGED, OSM) jdou do .omap jako editovatelné objekty
    (viz oom_import), ne jako šablony – kromě katastru, ten je jen podklad.
    """
    del work_dir
    templates: list[OomTemplate] = []

    if built_refs:
        for key, label, relpath, opacity, visible in OOM_REFERENCE_SPECS:
            if key == "orthophoto":
                # JPEG dlaždice (orthophoto.jpg nebo orthophoto_rXcY.jpg) = každá šablona zvlášť.
                tiles = [
                    (k, p) for k, p in orthophoto_items(built_refs) if p.is_file()
                ]
                for tkey, tpath in tiles:
                    tlabel = label if len(tiles) == 1 else f"{label} {tkey[len('orthophoto_'):]}"
                    templates.append(
                        OomTemplate(
                            "image",
                            tlabel,
                            f"references/{tpath.name}",
                            visible=visible,
                            opacity=opacity,
                            group=GROUP_REFERENCE,
                        )
                    )
                continue
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
        # Vektorový katastr (GeoPackage, EPSG:5514) jako podklad nad rastry –
        # jde zapnout/vypnout a nastavit průhlednost, do mapy nezasahuje.
        km_vector = built_refs.get("katastr_vector")
        if km_vector and km_vector.is_file():
            templates.append(
                OomTemplate(
                    "ogr",
                    "Katastrální mapa (křivky)",
                    f"references/{km_vector.name}",
                    visible=False,
                    opacity=1.0,
                    group=GROUP_REFERENCE,
                )
            )
        key, label, relpath, opacity, visible = OOM_OSM_REF_SPEC
        path = built_refs.get(key)
        if path and path.is_file():
            templates.append(
                OomTemplate(
                    "image",
                    label,
                    relpath,
                    visible=visible,
                    opacity=opacity,
                    group=GROUP_OSM,
                )
            )

    return templates
