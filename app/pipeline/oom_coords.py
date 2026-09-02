from __future__ import annotations

import math


def projected_to_map_coord(
    x: float,
    y: float,
    *,
    ref_x: float,
    ref_y: float,
    scale: int,
    grivation_deg: float,
    combined_scale_factor: float = 1.0,
    map_ref_x: float = 0.0,
    map_ref_y: float = 0.0,
) -> tuple[int, int]:
    """Metre S-JTSK → nativní souřadnice OOM (stejná transformace jako Mapper).

    OOM mapuje projected → map jako: posun k ref, rotace +grivation, scale(s, −s).
    Bez rotace sedí objekty proti PNG v grid north, ale v Mapperu jsou natočené.
    """
    s = combined_scale_factor * float(scale) / 1000.0
    if s <= 0:
        raise ValueError("Neplatný měřítkový faktor pro OOM transformaci")
    g = math.radians(grivation_deg)
    dx = x - ref_x
    dy = y - ref_y
    rx = dx * math.cos(g) - dy * math.sin(g)
    ry = dx * math.sin(g) + dy * math.cos(g)
    fac = 1000.0 / s
    mx = rx * fac + map_ref_x
    my = -ry * fac + map_ref_y
    return round(mx), round(my)
