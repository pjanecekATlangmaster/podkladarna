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


def map_to_projected(
    mx: float,
    my: float,
    *,
    ref_x: float,
    ref_y: float,
    scale: int,
    grivation_deg: float = 0.0,
    combined_scale_factor: float = 1.0,
    map_ref_x: float = 0.0,
    map_ref_y: float = 0.0,
) -> tuple[float, float]:
    """Nativní souřadnice OOM → metry S-JTSK (inverze ``projected_to_map_coord``).

    Webový náhled PNGčko má grivaci odrotovanou (= grid north nahoru), takže
    pro PGW typicky volej s ``grivation_deg=0``.
    """
    s = combined_scale_factor * float(scale) / 1000.0
    if s <= 0:
        raise ValueError("Neplatný měřítkový faktor pro OOM transformaci")
    fac = 1000.0 / s
    rx = (mx - map_ref_x) / fac
    ry = -(my - map_ref_y) / fac
    g = math.radians(grivation_deg)
    if abs(grivation_deg) < 1e-9:
        return ref_x + rx, ref_y + ry
    # Inverze rotace +grivation: [dx,dy] = R(-g) · [rx,ry]
    cos_g = math.cos(g)
    sin_g = math.sin(g)
    dx = rx * cos_g + ry * sin_g
    dy = -rx * sin_g + ry * cos_g
    return ref_x + dx, ref_y + dy
