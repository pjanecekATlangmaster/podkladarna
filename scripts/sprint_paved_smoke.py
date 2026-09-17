"""Lokální smoke: sprint omap s ulicí (501.17), chodníkem (501.6) a parkovištěm (501).

Spusť: python -m scripts.sprint_paved_smoke
Otevři výstup v OOM – bez Dockeru.
"""
from __future__ import annotations

from pathlib import Path

from app.pipeline.build_oom_map import write_oom_map
from app.pipeline.oom_import import OomObjectPart, _area_object_with_holes, _path_object
from app.pipeline.oom_layers import OomTemplate
from app.pipeline.oom_symbol_map import (
    oom_code_for_vectorconf_rule,
    symbol_index_for_code,
)
from app.pipeline.osm_paths import feature_oom_code, osm_oom_code

PRESET = "sprint_2m"
SCALE = 4000
OUT = Path("tmp") / "sprint_paved_smoke.omap"


def main() -> None:
    ulice_code = oom_code_for_vectorconf_rule(
        "road-path", "503", "Ulice", preset_id=PRESET, scale=SCALE
    )
    sidewalk_code = osm_oom_code("sidewalk", PRESET)
    parking_code = feature_oom_code("parking", PRESET)
    print(f"codes: ulice={ulice_code} sidewalk={sidewalk_code} parking={parking_code}")
    assert ulice_code == "501.17", ulice_code
    assert sidewalk_code == "501.6", sidewalk_code
    assert parking_code == "501", parking_code

    si_u = symbol_index_for_code(PRESET, SCALE, ulice_code)
    si_s = symbol_index_for_code(PRESET, SCALE, sidewalk_code)
    si_p = symbol_index_for_code(PRESET, SCALE, parking_code)
    assert None not in (si_u, si_s, si_p)

    # Jednoduchá geometrie v mapových jednotkách (kolem 0,0).
    street = _path_object(
        si_u,
        [(-8000, 0), (-2000, 0), (2000, 500), (8000, 500)],
    )
    walk = _path_object(
        si_s,
        [(-8000, -1200), (8000, -1200)],
    )
    park = _area_object_with_holes(
        si_p,
        [[(-1500, 2000), (1500, 2000), (1500, 4500), (-1500, 4500), (-1500, 2000)]],
    )
    # Pro srovnání: žlutá louka přes ulici (401) – ulice musí zůstat vidět díky barvě.
    si_farm = symbol_index_for_code(PRESET, SCALE, "401")
    meadow = _area_object_with_holes(
        si_farm,
        [[(-9000, -3000), (9000, -3000), (9000, 3000), (-9000, 3000), (-9000, -3000)]],
    )

    parts = [
        OomObjectPart("ZABAGED – TrvalyTravniPorost", meadow, 1),
        OomObjectPart("RÚIAN – budovy", "", 0),  # placeholder skip empty
        OomObjectPart("ZABAGED – Ulice", street, 1),
        OomObjectPart("OSM chodník", walk, 1),
        OomObjectPart("OSM parkoviště (501 zpevněná)", park, 1),
    ]
    parts = [p for p in parts if p.count > 0 and p.objects_xml]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    # Minimální PNG šablona není potřeba – write_oom_map chce templates non-empty?
    tmpl = OomTemplate("image", "dummy", "basemap/pullautus.png", visible=False)
    # Vytvoř prázdný placeholder aby šablona dávala smysl při otevření.
    (OUT.parent / "basemap").mkdir(parents=True, exist_ok=True)
    write_oom_map(
        OUT,
        map_name="sprint-paved-smoke",
        scale=SCALE,
        ref_x=-745000.0,
        ref_y=-1045000.0,
        ref_lat=50.05,
        ref_lon=14.35,
        templates=[tmpl],
        object_parts=parts,
        preset_id=PRESET,
    )
    xml = OUT.read_text(encoding="utf-8")
    assert f'symbol="{si_u}"' in xml
    assert f'symbol="{si_p}"' in xml
    assert 'priority="13"' in xml and "Lower brown 50%" in xml
    assert 'priority="30"' in xml and "Yellow 100%" in xml
    print(f"OK wrote {OUT.resolve()}")
    print("Open in OOM: street (brown footprint) must sit ON TOP of yellow meadow.")


if __name__ == "__main__":
    main()
