"""Doplnění symbolů z novější sady (OCAD export → ``.omap``) do OOM sady symbolů.

Pravidlo: pokud sada, kterou generátor používá (OOM), kód symbolu už má – i bez
přípony ``.0`` (``401.0`` ≙ ``401``) – ponechá se původní symbol. Jen symboly,
které v původní sadě nejsou (např. ``512.300`` plocha průchodná v nižší úrovni),
se přidají včetně svého kódu. Barvy přidaných symbolů se přemapují na barvy
původní sady (stejný název, jinak stejné / nejbližší CMYK), takže se nemění
pořadí vykreslování.

Nepoužívá se pro ISMTBOM – ten má v nové sadě jiné číslování.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.pipeline.oom_symbols import (
    _next_symbol_id,
    _color_refs_in_symbol,
    _parse_color_entries,
    _remap_colors_in_xml,
    _top_level_symbols,
)

# Typ 8 = textové symboly (900–902), v mapách se nepoužívají.
SKIP_TYPES = frozenset({"8"})
# Slova bez významu pro porovnání názvů (OCAD sada píše „footprint“ → nově jen „3 m“,
# české přípony „dvojitá linie“ apod.).
_NAME_STOP = frozenset(
    {"footprint", "dvojitá", "linie", "line", "the", "of", "a", "and", "with", "or", "mm", "m"}
)


@dataclass
class MergeReport:
    added: list[tuple[str, str]] = field(default_factory=list)  # (kód, název)
    skipped_existing: int = 0
    twins: list[tuple[str, str, str]] = field(default_factory=list)  # (nový kód, název, starý kód)
    collisions: list[tuple[str, str, str, str]] = field(default_factory=list)  # nový kód/název, starý kód/název
    skipped_type: int = 0
    color_map: dict[int, int] = field(default_factory=dict)
    color_inexact: list[str] = field(default_factory=list)
    color_added: list[str] = field(default_factory=list)


def normalize_code(code: str) -> str:
    """``401.0`` → ``401`` (OCAD export dává hlavním symbolům ``.0``)."""
    return code[:-2] if code.endswith(".0") else code


def name_tokens(name: str) -> frozenset[str]:
    """Významová slova názvu symbolu (bez „footprint“, „dvojitá linie“ apod.)."""
    name = name.replace("&amp;", "&").replace("&gt;", ">")
    words = re.findall(r"[a-zá-ž]+|\d+(?:\.\d+)?", name.lower())
    return frozenset(w for w in words if w not in _NAME_STOP)


def _series(code: str) -> str:
    return code.split(".")[0]


def _attr(attrs: str, name: str) -> str | None:
    m = re.search(rf'\b{name}="([^"]*)"', attrs)
    return m.group(1) if m else None


def _cmyk(color_xml: str) -> tuple[float, float, float, float]:
    vals = []
    for key in "cmyk":
        m = re.match(r"<color\b[^>]*\b%s=\"([\d.]+)\"" % key, color_xml)
        vals.append(float(m.group(1)) if m else 0.0)
    return tuple(vals)  # type: ignore[return-value]


def _color_name(color_xml: str) -> str:
    m = re.search(r'\bname="([^"]*)"', color_xml)
    return (m.group(1) if m else "").strip().lower()


# Nejbližší barva základní sady se použije jen do této vzdálenosti (součet čtverců
# rozdílů CMYK); jinak se barva přidá jako nová (pod ostatní).
NEAR_COLOR_DIST2 = 0.02


def _dist2(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b))


def _plain_color_xml(priority: int, name: str, cmyk: tuple[float, ...]) -> str:
    """Nová barva bez spotcolor odkazů (ty jsou v každé sadě jinak číslované)."""
    c, m, y, k = cmyk
    r, g, b = (1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)
    esc = name.replace("&", "&amp;").replace('"', "&quot;")
    return (
        f'<color priority="{priority}" name="{esc}" c="{c:g}" m="{m:g}" y="{y:g}" '
        f'k="{k:g}" opacity="1"><cmyk method="custom"/>'
        f'<rgb method="cmyk" r="{r:.3g}" g="{g:.3g}" b="{b:.3g}"/></color>'
    )


class _ColorMapper:
    """Priorita barvy v donoru → priorita v základní sadě (příp. nově přidaná)."""

    def __init__(self, base_colors_xml: str, donor_colors_xml: str, report: MergeReport):
        self.base = _parse_color_entries(base_colors_xml)
        self.donor = dict(_parse_color_entries(donor_colors_xml))
        self.report = report
        self.next_pri = max((p for p, _ in self.base), default=-1) + 1
        self.appended: list[str] = []
        self.map: dict[int, int] = {}

    def resolve(self, pri: int) -> int:
        if pri in self.map:
            return self.map[pri]
        xml = self.donor.get(pri)
        if xml is None:
            self.map[pri] = pri
            return pri
        name, cmyk = _color_name(xml), _cmyk(xml)
        same_name = [b for b, bx in self.base if _color_name(bx) == name]
        exact = [b for b, bx in self.base if _cmyk(bx) == cmyk]
        if same_name:
            out = same_name[0]
        elif exact:
            out = exact[0]
        else:
            near = min(self.base, key=lambda e: _dist2(_cmyk(e[1]), cmyk))
            if _dist2(_cmyk(near[1]), cmyk) <= NEAR_COLOR_DIST2:
                out = near[0]
                self.report.color_inexact.append(
                    f"{pri} '{name}' → {out} '{_color_name(near[1])}'"
                )
            else:
                out = self.next_pri
                self.next_pri += 1
                self.appended.append(_plain_color_xml(out, name, cmyk))
                self.report.color_added.append(f"{out} '{name}'")
        self.map[pri] = out
        return out


def build_color_map(
    base_colors_xml: str, donor_colors_xml: str, report: MergeReport | None = None
) -> dict[int, int]:
    """Priorita barvy v donoru → priorita barvy v základní sadě (všechny barvy)."""
    mapper = _ColorMapper(base_colors_xml, donor_colors_xml, report or MergeReport())
    return {pri: mapper.resolve(pri) for pri in mapper.donor}


def merge_missing_symbols(
    base_text: str,
    donor_text: str,
    equivalents: dict[str, str] | None = None,
) -> tuple[str, MergeReport]:
    """Vrátí ``base_text`` s doplněnými symboly z ``donor_text`` + report.

    ``equivalents``: ručně určená dvojčata ``{nový kód: starý kód}`` pro případy,
    kdy se názvy liší (nový „Paved footpath, 4 m“ ≙ starý „…footprint: 4 m“).
    """
    equivalents = equivalents or {}
    report = MergeReport()
    base_colors = re.search(r"<colors\s+count=\"\d+\"[^>]*>.*?</colors>", base_text, re.S)
    donor_colors = re.search(r"<colors\s+count=\"\d+\"[^>]*>.*?</colors>", donor_text, re.S)
    base_syms = re.search(r"<symbols\s+count=\"\d+\"[^>]*>.*?</symbols>", base_text, re.S)
    donor_syms = re.search(r"<symbols\s+count=\"\d+\"[^>]*>.*?</symbols>", donor_text, re.S)
    if not (base_colors and donor_colors and base_syms and donor_syms):
        raise ValueError("Neplatná sada symbolů (chybí <colors> nebo <symbols>)")

    base_symbols_xml = base_syms.group(0)
    donor_symbols_xml = donor_syms.group(0)
    mapper = _ColorMapper(base_colors.group(0), donor_colors.group(0), report)

    base_names: dict[str, str] = {}
    for _s, _e, a in _top_level_symbols(base_symbols_xml):
        if _attr(a, "code"):
            base_names[_attr(a, "code")] = _attr(a, "name") or ""  # type: ignore[index]
    base_codes = set(base_names)
    base_tokens: dict[str, list[tuple[frozenset[str], str]]] = {}
    for bc, bn in base_names.items():
        base_tokens.setdefault(_series(bc), []).append((name_tokens(bn), bc))

    next_id = _next_symbol_id(base_symbols_xml)
    id_map: dict[str, str] = {}  # id v donoru → nové id (kvůli <part symbol=>)
    pending: list[str] = []

    for start, end, attrs in _top_level_symbols(donor_symbols_xml):
        code = _attr(attrs, "code") or ""
        did = _attr(attrs, "id")
        if not code:
            continue
        if _attr(attrs, "type") in SKIP_TYPES:
            report.skipped_type += 1
            continue
        nm = _attr(attrs, "name") or ""
        # Stejný význam v téže číselné řadě (532.1 „3 m“ ≙ 532.8 „footprint 3 m“).
        twin = equivalents.get(code) or next(
            (bc for toks, bc in base_tokens.get(_series(code), []) if toks == name_tokens(nm)),
            None,
        )
        if twin is not None:
            report.twins.append((code, nm, twin))
            continue
        same_code = normalize_code(code) if normalize_code(code) in base_codes else (code if code in base_codes else None)
        if same_code is not None:
            report.skipped_existing += 1
            if name_tokens(base_names[same_code]) != name_tokens(nm):
                report.collisions.append((code, nm, same_code, base_names[same_code]))
            continue
        xml = donor_symbols_xml[start:end]
        xml = re.sub(r'\s+is_hidden="true"', "", xml, count=1)
        for pri in _color_refs_in_symbol(xml):
            mapper.resolve(pri)
        xml = _remap_colors_in_xml(xml, mapper.map)
        new_id = str(next_id)
        next_id += 1
        xml = re.sub(r'\bid="\d+"', f'id="{new_id}"', xml, count=1)
        if did is not None:
            id_map[did] = new_id
        pending.append(xml)
        report.added.append((code, _attr(attrs, "name") or ""))
    report.color_map = dict(mapper.map)

    def _part_repl(m: re.Match[str]) -> str:
        return f'part symbol="{id_map.get(m.group(1), m.group(1))}"'

    new_symbols = [re.sub(r'part symbol="(\d+)"', _part_repl, x) for x in pending]
    if not new_symbols:
        return base_text, report

    merged_symbols = re.sub(
        r"</symbols>\s*$", "".join(new_symbols) + "\n</symbols>", base_symbols_xml, count=1
    )
    total = len(_top_level_symbols(merged_symbols))
    merged_symbols = re.sub(
        r'<symbols\s+count="\d+"', f'<symbols count="{total}"', merged_symbols, count=1
    )
    out = base_text.replace(base_symbols_xml, merged_symbols, 1)
    if mapper.appended:
        base_colors_xml = base_colors.group(0)
        merged_colors = re.sub(
            r"</colors>\s*$", "".join(mapper.appended) + "\n</colors>", base_colors_xml, count=1
        )
        n = len(_parse_color_entries(merged_colors))
        merged_colors = re.sub(
            r'<colors\s+count="\d+"', f'<colors count="{n}"', merged_colors, count=1
        )
        out = out.replace(base_colors_xml, merged_colors, 1)
    return out, report

__all__ = ["MergeReport", "merge_missing_symbols", "normalize_code", "build_color_map"]
