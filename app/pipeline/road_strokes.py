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
) -> list[str | None]:
    """Symboly čar po pravidle tahu (viz modul). ``fixed`` = indexy čar s pevným symbolem (osa 502.0)."""
    if not lines or min_run_m <= 0:
        return list(codes)
    out = list(codes)
    lens = [_length(ln) for ln in lines]
    chains = strokes(lines)
    stroke_of = {i: k for k, ch in enumerate(chains) for i in ch}
    for chain in chains:
        _merge_short_runs(chain, out, lens, min_run_m, fixed)
    # krátké tahy (pahýl, otočka, slepá spojka): jen zúžení na symbol jediného sousedního tahu
    nodes = _nodes(lines)
    for k, chain in enumerate(chains):
        if sum(lens[i] for i in chain) >= min_run_m or any(i in fixed for i in chain):
            continue
        own = {out[i] for i in chain}
        if len(own) != 1:
            continue
        own_code = next(iter(own))
        touching: set[str | None] = set()
        for inc in nodes.values():
            if any(stroke_of[i] == k for i, _s in inc):
                touching.update(out[i] for i, _s in inc if stroke_of[i] != k)
        if len(touching) == 1:
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
