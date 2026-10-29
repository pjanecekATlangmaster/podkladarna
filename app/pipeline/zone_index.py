"""Výřez velké zóny (plochy pro celou mapu) v okolí jedné čáry pro rychlý ořez.

Ořez OSM čar o zóny z DTM (``dtm_sidewalks.clip_outside``, ``end_at_edge``, ``steps_keep``) počítal rozdíl / průnik
každé krátké čáry proti zóně celého výřezu; ve sprintu 11,6 km² to bylo 84 % skládání mapy (job cf5b78724d57,
2026-10-29). Zóna se jednou za mapu rozdělí do volného čtyřstromu (kvadranty se překrývají o ``OVERLAP_M``) a čára
dostane jen ``clip_by_rect`` nejmenšího uzlu, který celý obsahuje její obálku + okraj. Uvnitř obdélníku zůstávají
vrcholy zóny beze změny a nové hrany na okraji obdélníku leží dál než ``margin`` od čáry, takže výsledek ořezu je
stejný jako proti celé zóně (liší se nejvýš zaokrouhlením ~1e-13 m)."""

from __future__ import annotations

from collections import OrderedDict

# Menší zóny se ořezávají přímo (index by stál víc, než ušetří).
MIN_COORDS = 20_000
# Uzel s nejvýš tolika vrcholy se dál nedělí.
LEAF_COORDS = 4_000
# Překryv sousedních kvadrantů (m): čára s obálkou + okrajem do této velikosti se vejde do jednoho kvadrantu.
OVERLAP_M = 150.0
MAX_DEPTH = 12
# Několik posledních zón (les, MTBO a sprint jich v jedné mapě mají kolem deseti); starší se uvolní.
_MEMO_SIZE = 16
_MEMO: OrderedDict[int, tuple[object, "_Node | None"]] = OrderedDict()


class _Node:
    __slots__ = ("rect", "geom", "children", "depth")

    def __init__(self, rect: tuple[float, float, float, float], geom, depth: int) -> None:
        self.rect = rect
        self.geom = geom
        self.children: list[_Node] | None = None
        self.depth = depth

    def _split(self) -> list[_Node]:
        import shapely

        if self.children is None:
            x0, y0, x1, y1 = self.rect
            if (
                self.depth >= MAX_DEPTH
                or shapely.get_num_coordinates(self.geom) <= LEAF_COORDS
                or min(x1 - x0, y1 - y0) <= 4 * OVERLAP_M
            ):
                self.children = []
            else:
                mx, my = (x0 + x1) / 2, (y0 + y1) / 2
                rects = [
                    (x0, y0, min(x1, mx + OVERLAP_M), min(y1, my + OVERLAP_M)),
                    (max(x0, mx - OVERLAP_M), y0, x1, min(y1, my + OVERLAP_M)),
                    (x0, max(y0, my - OVERLAP_M), min(x1, mx + OVERLAP_M), y1),
                    (max(x0, mx - OVERLAP_M), max(y0, my - OVERLAP_M), x1, y1),
                ]
                self.children = [_Node(r, shapely.clip_by_rect(self.geom, *r), self.depth + 1) for r in rects]
        return self.children


def _contains(rect, bounds) -> bool:
    return rect[0] <= bounds[0] and rect[1] <= bounds[1] and rect[2] >= bounds[2] and rect[3] >= bounds[3]


def _root(zone) -> _Node | None:
    """Kořen indexu zóny (memo podle identity objektu), None pro malou / prázdnou zónu."""
    key = id(zone)
    hit = _MEMO.get(key)
    if hit is not None and hit[0] is zone:
        _MEMO.move_to_end(key)
        return hit[1]
    import shapely

    root = None
    if not zone.is_empty and shapely.get_num_coordinates(zone) >= MIN_COORDS:
        x0, y0, x1, y1 = zone.bounds
        root = _Node((x0 - 1.0, y0 - 1.0, x1 + 1.0, y1 + 1.0), zone, 0)
    _MEMO[key] = (zone, root)  # drží odkaz na zónu, takže id nemůže připadnout jinému objektu
    while len(_MEMO) > _MEMO_SIZE:
        _MEMO.popitem(last=False)
    return root


def local_zone(zone, geom, margin: float):
    """Část ``zone`` v obálce ``geom`` rozšířené o ``margin`` m (``zone`` beze změny u malé zóny / mimo obálku)."""
    root = _root(zone)
    if root is None:
        return zone
    gx0, gy0, gx1, gy1 = geom.bounds
    rx0, ry0, rx1, ry1 = root.rect
    # Mimo kořen (obálka zóny + 1 m) zóna nic nemá, obdélník se tam smí zkrátit bez nových hran.
    bounds = (max(rx0, gx0 - margin), max(ry0, gy0 - margin), min(rx1, gx1 + margin), min(ry1, gy1 + margin))
    if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        return zone.__class__()  # čára daleko od zóny: prázdná zóna, ořez nechá čáru celou
    import shapely

    node = root
    while True:
        child = next((c for c in node._split() if _contains(c.rect, bounds)), None)
        if child is None:
            break
        node = child
    return shapely.clip_by_rect(node.geom, *bounds)


def clear() -> None:
    """Uvolní zapamatované zóny (konec mapy)."""
    _MEMO.clear()
