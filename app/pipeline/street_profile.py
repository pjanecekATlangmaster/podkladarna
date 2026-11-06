"""Ulice se stáním jako celek (les a MTBO): o ploše ulice rozhoduje profil přesahu stání za kresbu čáry.

Místo slévání jednotlivých kusů stání s ulicí (dřívější ``osm_paths.merge_parking_into_road``) se podél každé osy
kreslené silnice vedou kolmé řezy po ``STEP_M`` a na každé straně se změří, o kolik povrch stání přesahuje kresbu
čáry. Povrch stání = pruhy stání z OSM (kandidáti slití) + parkoviště z DTM; plocha silnic z DTM (obsahuje i pruhy
stání, které OSM nemá, ale také zálivy a trychtýře křižovatek) se bere jen tam, kde stání na téže straně potvrdí
stání do ``CONFIRM_M`` podél osy.

Strana se kreslí rozšířená, jen když přesah drží aspoň ``T_MM`` mm mapy (rozšíření musí být v měřítku vidět) souvisle
aspoň ``L_MM`` mm mapy; mezery do ``G_MM`` mm se zacelí, slabší přesah (od ``HYST`` · T) navazující na rozšíření ho
prodlouží. Rozšířený úsek = pás kresby + pruh stání konstantní šířky (medián přesahu), konce kolmé. Stání pod kresbou
a krátká stání pohltí čára. Povrch dál než ``EMAX_M`` za kresbou je parkoviště nebo náměstí: řez je neznámý
a parkoviště zůstává samostatnou plochou sítě jako dosud.

Petr 2026-11-06 (varianta A, vlákno „ulice se stáním jako celek“; měření a výřezy ``/mnt/project-files/ulice-stani/``).
"""

from __future__ import annotations

import json
from pathlib import Path

# Les a MTBO kreslí ulici se stáním podle profilu; False = slévání po kusech stání (3.12.4 – 3.16.x).
STREET_PROFILE = True
T_MM = 0.3  # přesah, od kterého se rozšíření kreslí (1:10 000 = 3 m)
L_MM = 5.0  # nejkratší rozšířený úsek
G_MM = 4.0  # mezera mezi rozšířenými úseky, která se zacelí
HYST = 0.6  # slabší přesah (od HYST · T) navazující na rozšířený úsek ho prodlouží
CONFIRM_M = 20.0  # plocha silnic z DTM se bere jen do tolika m podél osy od potvrzeného stání
EMAX_M = 7.0  # povrch dál než tolik m za kresbou = parkoviště / náměstí, ne pruh stání
STEP_M = 1.0
JUNCTION_M = 3.0  # řezy u konců a křížení jiných silnic (+ tolik m) jsou neznámé, převezmou třídu sousedů
END_M = 12.0  # kolem konců řetězu (uzly sítě) je povrch rozšířený zaoblením křižovatky – neznámé
IQR_MAX_M = 2.0  # pruh stání má stálou šířku; rozšíření s větším rozptylem přesahu (trychtýř, ostrůvek) se nekreslí
ROUND_M = 0.5


def enabled(preset_id: str) -> bool:
    return STREET_PROFILE and not preset_id.startswith("sprint")


def _load_dtm(work_dir: Path, names) -> list:
    """Plochy z ``dtm/<name>.json`` (shapely Polygon); při ``dtm_disabled`` prázdný seznam."""
    from shapely.geometry import Polygon

    from app.pipeline.dtm_import import dtm_dir

    out = []
    for name in names:
        p = dtm_dir(work_dir) / f"{name}.json"
        if not p.is_file():
            continue
        try:
            rings = json.loads(p.read_text(encoding="utf-8")).get("polygons") or []
        except (OSError, ValueError):
            continue
        for r in rings:
            try:
                g = Polygon(r[0], r[1:]).buffer(0)
            except (ValueError, TypeError, IndexError):
                continue
            if not g.is_empty:
                out.append(g)
    return out


def _reach(pre, x: float, y: float, nx: float, ny: float, half: float) -> float:
    """Přesah povrchu ``pre`` za kresbu (m) na jedné straně řezu; ``inf`` když povrch pokračuje dál než ``EMAX_M``."""
    from shapely.geometry import LineString, Point

    d = max(0.0, half - 1.0)
    lim = half + EMAX_M + 1.5
    if not pre.intersects(LineString([(x + nx * d, y + ny * d), (x + nx * lim, y + ny * lim)])):
        return 0.0  # paprsek povrch neprotne (většina řezů) – bez krokování
    last = None
    gap = 0.0
    while d <= lim:
        if pre.contains(Point(x + nx * d, y + ny * d)):
            last = d
            gap = 0.0
        elif d > half:
            gap += 0.25
            if gap > 1.0:
                break
        d += 0.25
    if last is None:
        return 0.0
    e = last - half
    return float("inf") if e > EMAX_M else max(0.0, e)


def _runs(flags: list) -> list:
    """Souvislé úseky stejné hodnoty: [(hodnota, od, do)]."""
    out = []
    st = 0
    for i in range(1, len(flags) + 1):
        if i == len(flags) or flags[i] != flags[st]:
            out.append((flags[st], st, i))
            st = i
    return out


def generalize_flags(flags: list, weak: list, gap_n: int, min_n: int) -> list:
    """1D generalizace tříd řezů podél osy: hystereze (``weak`` prodlouží rozšíření), neznámé (None) převezmou
    souseda, mezery do ``gap_n`` vzorků se zacelí, rozšíření kratší než ``min_n`` vzorků odpadne."""
    f = list(flags)
    changed = True
    while changed:
        changed = False
        for i in range(len(f)):
            if f[i] is False and weak[i] and ((i > 0 and f[i - 1]) or (i + 1 < len(f) and f[i + 1])):
                f[i] = True
                changed = True
    for i in range(len(f)):
        if f[i] is None and i > 0:
            f[i] = f[i - 1]
    for i in range(len(f) - 1, -1, -1):
        if f[i] is None:
            f[i] = f[i + 1] if i + 1 < len(f) and f[i + 1] is not None else False
    R = _runs(f)
    for k, (v, a, b) in enumerate(R):
        if not v and 0 < k < len(R) - 1 and b - a <= gap_n:
            for i in range(a, b):
                f[i] = True
    for v, a, b in _runs(f):
        if v and b - a < min_n:
            for i in range(a, b):
                f[i] = False
    return f


def _chains(roads: list) -> list:
    """Řetězy os stejné šířky kresby spojené přes prosté uzly: [(LineString, půlka kresby)]."""
    from shapely.ops import linemerge, unary_union

    by_half: dict[float, list] = {}
    for line, half in roads:
        by_half.setdefault(round(half, 3), []).append(line)
    out = []
    for half, lines in by_half.items():
        u = unary_union(lines)
        m = linemerge(u) if u.geom_type == "MultiLineString" else u
        out += [(c, half) for c in getattr(m, "geoms", [m]) if c.geom_type == "LineString" and c.length > 0]
    return out


def street_areas(work_dir: Path, roads: list, strips: list, scale: int) -> list[dict]:
    """Plochy ulic se stáním (prvky GeoJSON ``kind`` ``parking``, ``street`` True) z profilu přesahu.

    ``roads``: [(LineString, půlka kresby)] kreslených silnic (bez mostů a tunelů); ``strips``: OSM stání (shapely),
    která by se s ulicí slévala – do mapy se samostatně nedostanou, pohltí je čára nebo pruh."""
    from shapely.geometry import Point, mapping
    from shapely.ops import substring, unary_union
    from shapely.prepared import prep
    from shapely.strtree import STRtree

    from app.pipeline.osm_paths import mm_to_ground_m

    surf = list(strips) + _load_dtm(work_dir, ("parking",))
    if not surf or not roads or scale <= 0:
        return []
    S = prep(unary_union(surf))
    dtm_roads = _load_dtm(work_dir, ("roads",))
    SA = prep(unary_union(surf + dtm_roads)) if dtm_roads else None
    T = mm_to_ground_m(T_MM, scale)
    gap_n = int(mm_to_ground_m(G_MM, scale) / STEP_M)
    min_n = int(mm_to_ground_m(L_MM, scale) / STEP_M)
    conf_n = int(CONFIRM_M / STEP_M)
    inf = float("inf")

    chains = _chains(roads)
    tree = STRtree([c.buffer(h + JUNCTION_M) for c, h in chains])
    pieces = []
    for ci, (chain, half) in enumerate(chains):
        if chain.length < 2 * STEP_M:
            continue
        # uzly: konce jiných silnic u řetězu a křížení s nimi (souběžná kopie téže ulice – mixed – řetěz nezakryje)
        zone = chain.buffer(half + JUNCTION_M)
        others = [Point(chain.coords[0]).buffer(END_M), Point(chain.coords[-1]).buffer(END_M)]
        for k in tree.query(chain):
            k = int(k)
            if k == ci:
                continue
            oc, oh = chains[k]
            pts = [Point(oc.coords[0]), Point(oc.coords[-1])]
            x = oc.intersection(chain)
            pts += [g for g in getattr(x, "geoms", [x]) if g.geom_type == "Point"]
            others += [q.buffer(oh + half + JUNCTION_M) for q in pts if zone.contains(q)]
        J = prep(unary_union(others))
        n = int(chain.length // STEP_M)
        ext = {1: [], -1: []}
        normals = []
        junc = []
        for i in range(n + 1):
            t = min(chain.length, i * STEP_M)
            p0 = chain.interpolate(max(0.0, t - 0.5))
            p1 = chain.interpolate(min(chain.length, t + 0.5))
            p = chain.interpolate(t)
            dx, dy = p1.x - p0.x, p1.y - p0.y
            L = (dx * dx + dy * dy) ** 0.5 or 1.0
            junc.append(J.contains(p))
            normals.append((p.x, p.y, -dy / L, dx / L))
            for s in (1, -1):
                ext[s].append(_reach(S, p.x, p.y, normals[-1][2] * s, normals[-1][3] * s, half))
        for s in (1, -1):
            e_s = list(ext[s])
            if SA is not None:
                # plocha silnic z DTM jen u potvrzeného stání na téže straně (měří se jen tam)
                ev = [HYST * T <= e < inf for e in ext[s]]
                for i in range(len(e_s)):
                    if not ev[i] and any(ev[max(0, i - conf_n): i + conf_n + 1]):
                        px, py, nx, ny = normals[i]
                        e_s[i] = _reach(SA, px, py, nx * s, ny * s, half)
            flags = [None if junc[i] or e == inf else T <= e for i, e in enumerate(e_s)]
            weak = [HYST * T <= e < inf for e in e_s]
            for v, a, b in _runs(generalize_flags(flags, weak, gap_n, min_n)):
                if not v:
                    continue
                es = sorted(e for e in e_s[a:b] if HYST * T <= e < inf)
                if not es or es[(3 * len(es)) // 4] - es[len(es) // 4] > IQR_MAX_M:
                    continue
                w = max(T, round(es[len(es) // 2] / ROUND_M) * ROUND_M)
                sub = substring(chain, a * STEP_M, min(chain.length, b * STEP_M))
                if sub.length <= 0:
                    continue
                strip = sub.buffer(s * (half + w), single_sided=True, cap_style=2)
                pieces.append(sub.buffer(half, cap_style=2).union(strip))
    out = []
    if pieces:
        u = unary_union(pieces)
        for poly in getattr(u, "geoms", [u]):
            if poly.geom_type == "Polygon" and not poly.is_empty and poly.area > 1.0:
                out.append({"type": "Feature", "properties": {"kind": "parking", "street": True}, "geometry": mapping(poly)})
    return out
