"""Les a MTBO: jeden symbol silnice na souvislý tah („jednou linií“, Petr 2026-10-27/28).

Silnice OSM se v uzlech skládají do **tahů**: přes uzel pokračuje nejpřímější úsek (odchylka do ``MAX_TURN_DEG``), v prostém
uzlu (jen dva úseky) vždy. Symbol se po tahu spojí do běhů; běh kratší než ``MIN_RUN_MM`` mm mapy převezme symbol delšího
souseda (soused s pevným symbolem – osa rozdělené vozovky 502.0 – vyhrává vždy). Krátký tah do ``MIN_RUN_MM`` (pahýl, otočka,
slepá spojka), který se dotýká jediného jiného symbolu, se na něj jen **zúží** (účelová spojka 502 na 502.1 zůstane 502).
Šířka se tak mění jen v uzlu a jen mezi dvěma dlouhými běhy: 27 m 502.1 mezi osou Lamačovy a K Barrandovu, 24 m 502.1 u Lidlu
a kapka otočky 502.1 na 502.2 (Retest 4, 2026-10-28) zmizí. Sprint se nemění (volá se jen pro les a MTBO).
"""

from __future__ import annotations

import math

# Běh jednoho symbolu kratší než tolik mm mapy (50 m v 1:10 000, 75 m v 1:15 000) se nekreslí jiným symbolem než sousedé.
MIN_RUN_MM = 5.0
# Přes křižovatku (uzel se třemi a více úseky) tah pokračuje jen do nejpřímějšího úseku s odchylkou do tolika stupňů.
MAX_TURN_DEG = 45.0
# Konce úseků do tolika m se pokládají za týž uzel.
NODE_SNAP_M = 0.5
# Okolí křižovatky (uzel se třemi a více úseky) v mm mapy, kde se šířka z DTM neměří (řez měří rozšíření křižovatky).
JUNCTION_EXCL_MM = 1.5
# Slepá větev / slepý konec tahu se zužuje na symbol své silnice jen do této délky v mm mapy (příjezd s otočkou, pahýl);
# delší slepá silnice (např. 500 m k areálu) si drží symbol podle skutečné šířky (review Opus 2026-10-28).
DEAD_END_MAX_MM = 15.0
# Pořadí šířek: index = širší symbol; kód mimo seznam je nejužší (černá cesta). MTBO ``503`` se katalogem překládá na 502.2.
WIDTH_ORDER = ("504", "831", "815", "503", "502.2", "502", "502.1", "502.0")


def width_rank(code: str | None) -> int:
    """Pořadí šířky symbolu (vyšší = širší); neznámý kód = nejužší."""
    try:
        return WIDTH_ORDER.index(code or "") + 1
    except ValueError:
        return 0


def _length(coords) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(coords, coords[1:]))


def _end_dir(coords, at_start: bool):
    pts = coords if at_start else coords[::-1]
    x0, y0 = pts[0]
    for x, y in pts[1:]:
        d = math.hypot(x - x0, y - y0)
        if d > 0.01:
            return ((x - x0) / d, (y - y0) / d)
    return None


def _nodes(lines: list[list[tuple[float, float]]]) -> dict[tuple[float, float], list[tuple[int, bool]]]:
    """Uzel → ``[(index čáry, True = začátek)]``; konce do ``NODE_SNAP_M`` jsou jeden uzel."""
    reps: list[tuple[float, float]] = []
    nodes: dict[tuple[float, float], list[tuple[int, bool]]] = {}
    for i, ln in enumerate(lines):
        for at_start in (True, False):
            p = ln[0] if at_start else ln[-1]
            key = None
            for r in reps:
                if math.hypot(r[0] - p[0], r[1] - p[1]) <= NODE_SNAP_M:
                    key = r
                    break
            if key is None:
                key = (float(p[0]), float(p[1]))
                reps.append(key)
            nodes.setdefault(key, []).append((i, at_start))
    return nodes


def junction_zone(lines: list[list[tuple[float, float]]], radius_m: float):
    """``(okolí křižovatek – uzly se třemi a více konci – jako shapely prepared nebo None, uzly)``."""
    pts = [node for node, inc in _nodes(lines).items() if len(inc) >= 3]
    if not pts:
        return None, []
    from shapely.geometry import Point
    from shapely.ops import unary_union
    from shapely.prepared import prep

    return prep(unary_union([Point(p).buffer(radius_m) for p in pts])), pts


def strokes(lines: list[list[tuple[float, float]]]) -> list[list[int]]:
    """Tahy jako seznamy indexů čar v pořadí po tahu. V uzlu se dvěma konci tah pokračuje vždy, ve větším uzlu
    jen nejpřímější dvojicí (do ``MAX_TURN_DEG``; dvojice se vybírají od nejpřímější, každý konec nejvýš jednou)."""
    n = len(lines)
    cos_max = math.cos(math.radians(MAX_TURN_DEG))
    # partner[(i, at_start)] = (j, at_start_j)
    partner: dict[tuple[int, bool], tuple[int, bool]] = {}
    for inc in _nodes(lines).values():
        if len(inc) < 2:
            continue
        cands = []
        for a in range(len(inc)):
            for b in range(a + 1, len(inc)):
                (i, si), (j, sj) = inc[a], inc[b]
                if i == j:
                    continue
                da, db = _end_dir(lines[i], si), _end_dir(lines[j], sj)
                if da is None or db is None:
                    continue
                straight = -(da[0] * db[0] + da[1] * db[1])
                if len(inc) == 2 or straight >= cos_max:
                    cands.append((straight, inc[a], inc[b]))
        used: set[tuple[int, bool]] = set()
        for _s, ea, eb in sorted(cands, key=lambda c: -c[0]):
            if ea in used or eb in used:
                continue
            partner[ea] = eb
            partner[eb] = ea
            used.update((ea, eb))
    seen = [False] * n
    out = []
    for i in range(n):
        if seen[i]:
            continue
        # dojít na začátek tahu (nebo dokola u prstence)
        cur, at_start = i, True
        chain = []
        start = (cur, at_start)
        while True:
            seen[cur] = True
            chain.append(cur)
            nxt = partner.get((cur, not at_start))  # z druhého konce dál
            if nxt is None or seen[nxt[0]]:
                break
            cur, at_start = nxt
        # a zpět na druhou stranu od ``i``
        back = []
        cur, at_start = i, True
        while True:
            nxt = partner.get((cur, at_start))
            if nxt is None or seen[nxt[0]]:
                break
            cur, at_start = nxt[0], not nxt[1]
            seen[cur] = True
            back.append(cur)
        out.append(back[::-1] + chain)
    return out


def stroke_codes(
    lines: list[list[tuple[float, float]]],
    codes: list[str | None],
    *,
    min_run_m: float,
    fixed: frozenset[int] | set[int] = frozenset(),
    dead_end_max_m: float | None = None,
) -> list[str | None]:
    """Symboly čar po pravidle tahu (viz modul). ``fixed`` = indexy čar s pevným symbolem (osa 502.0);
    ``dead_end_max_m`` = nejdelší slepá větev / ocas, který se zužuje (výchozí 3 × ``min_run_m``)."""
    if dead_end_max_m is None:
        dead_end_max_m = 3.0 * min_run_m
    if not lines or min_run_m <= 0:
        return list(codes)
    out = list(codes)
    lens = [_length(ln) for ln in lines]
    chains = strokes(lines)
    stroke_of = {i: k for k, ch in enumerate(chains) for i in ch}
    nodes = _nodes(lines)
    for k, chain in enumerate(chains):
        _merge_short_runs(chain, out, lens, min_run_m, fixed)
        _narrow_dead_end_tails(chain, out, nodes, stroke_of, k, fixed, lens, dead_end_max_m)
    # krátké tahy (pahýl, slepá spojka) a slepé větve (otočka, odbočka k domům, visí na síti v jediném uzlu):
    # jen zúžení na symbol jediného sousedního symbolu
    for k, chain in enumerate(chains):
        if any(i in fixed for i in chain):
            continue
        own = {out[i] for i in chain}
        if len(own) != 1:
            continue
        own_code = next(iter(own))
        touching: set[str | None] = set()
        touch_nodes = 0
        touch_at_end = False  # jediný dotyk s ostatními je v koncovém uzlu tahu (slepá větev), ne uprostřed
        for inc in nodes.values():
            mine = sum(1 for i, _s in inc if stroke_of[i] == k)
            if not mine:
                continue
            others = {out[i] for i, _s in inc if stroke_of[i] != k}
            if others:
                touch_nodes += 1
                touch_at_end = mine == 1 or len(chain) == 1 and mine == 2
            touching.update(others)
        total = sum(lens[i] for i in chain)
        short = total < min_run_m
        if len(touching) == 1 and (short or (touch_nodes == 1 and touch_at_end and total <= dead_end_max_m)):
            other = next(iter(touching))
            if width_rank(other) < width_rank(own_code):
                for i in chain:
                    out[i] = other
    return out


def _merge_short_runs(chain: list[int], out: list[str | None], lens: list[float], min_run_m: float, fixed) -> None:
    """Na jednom tahu: běhy stejného symbolu; krátký běh převezme symbol souseda (pevný vyhrává, jinak delší)."""
    while True:
        runs: list[list[int]] = []
        for i in chain:
            if runs and out[runs[-1][-1]] == out[i]:
                runs[-1].append(i)
            else:
                runs.append([i])
        if len(runs) < 2:
            return
        run_len = [sum(lens[i] for i in r) for r in runs]
        run_fixed = [any(i in fixed for i in r) for r in runs]
        short = [k for k in range(len(runs)) if run_len[k] < min_run_m and not run_fixed[k]]
        if not short:
            return
        k = min(short, key=lambda q: run_len[q])
        left = k - 1 if k > 0 else None
        right = k + 1 if k < len(runs) - 1 else None
        if left is not None and right is not None:
            if run_fixed[left] != run_fixed[right]:
                tgt = left if run_fixed[left] else right
            else:
                tgt = left if run_len[left] >= run_len[right] else right
        else:
            tgt = left if left is not None else right
        code = out[runs[tgt][0]]
        for i in runs[k]:
            out[i] = code


def _narrow_dead_end_tails(chain: list[int], out, nodes, stroke_of, k: int, fixed, lens, max_m: float) -> None:
    """Slepý konec tahu (od posledního uzlu s jiným tahem k volnému konci: příjezd k parkovišti s otočkou) není nikdy
    širší než úsek před ním – jen se zužuje (kapka 502.1 na 502.2, Retest 4 M4)."""
    if len(chain) < 2:
        return
    touch = {node for node, inc in nodes.items() if any(stroke_of[i] != k for i, _s in inc) and any(stroke_of[i] == k for i, _s in inc)}
    # uzly tahu v pořadí: hranice mezi sousedními čarami (a konce)
    def _shared(a: int, b: int):
        for node, inc in nodes.items():
            ids = {i for i, _s in inc}
            if a in ids and b in ids:
                return node
        return None

    def _outer(last: int, prev: int):
        """Vnější uzel poslední čáry ocasu (ten, který nesdílí s předchozí čárou)."""
        for node, inc in nodes.items():
            ids = [i for i, _s in inc]
            if last in ids and prev not in ids:
                return node
        return None

    for seq in (chain, chain[::-1]):
        if _outer(seq[-1], seq[-2]) in touch:
            continue  # konec tahu je v T na jiné silnici: průjezdný úsek, ne slepý konec
        # od konce dozadu: čáry, jejichž vnější hranice (směrem ke konci) nemá jiný tah
        tail: list[int] = []
        broke = False
        for pos in range(len(seq) - 1, 0, -1):
            tail.append(seq[pos])
            if _shared(seq[pos - 1], seq[pos]) in touch:
                broke = True
                break
        if not broke:
            # žádný vnitřní uzel s jiným tahem: slepá větev je celý tah za prvním úsekem, visí-li na síti jeho vnějším koncem
            if _outer(seq[0], seq[1]) not in touch:
                tail = []
        if not tail or any(i in fixed for i in tail):
            continue
        before = out[seq[len(seq) - 1 - len(tail)]]
        # jen širší konec slepé větve (od volného konce) do ``max_m``; delší široký konec si šířku drží
        wide = []
        for i in tail:  # ``tail`` je od volného konce dovnitř
            if width_rank(out[i]) <= width_rank(before):
                break
            wide.append(i)
        if wide and sum(lens[i] for i in wide) <= max_m:
            for i in wide:
                out[i] = before
