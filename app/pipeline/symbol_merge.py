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

# Která stávající značka v OOM má být předchůdcem nově přidané značky,
# aby odpovídající symboly byly přímo u sebe (např. 501.300 přímo za 501.3).
_ANCHORS: dict[str, str] = {
    # ISSprOM 2019 – OCAD 501 schéma (light .0–.13, heavy .20–.33)
    # OCAD 501 blok se vkládá celý za poslední vegetaci před zpevněnými.
    "501.0": "419",
    "501.300": "501.3",
    "501.301": "501.3",
    "501.200": "501.100",
    "505.3": "505.2",
    "505.4": "505.2",
    "509.101": "509.1",
    "512.100": "512.1",
    "512.101": "512.1",
    "512.300": "512.2",
    "512.301": "512.2",
    "512.302": "512.2",
    "512.304": "512.2",
    "512.307": "512.2",
    "512.308": "512.2",
    "512.309": "512.2",
    "512.311": "512.2",
    "512.312": "512.2",
    "512.313": "512.2",
    "512.314": "512.2",
    "512.315": "512.2",
    "513.101": "513.1",
    "533.0": "532.9",
    # ISOM 2017
    "101.2": "101.1",
    "101.3": "101.1",
    "102.2": "102.1",
    "103.2": "103.1",
    "103.3": "103.1",
    "105.101": "105",
    "105.200": "105",
    "508.5": "508.4",
    "508.6": "508.4",
    "508.7": "508.4",
    "508.8": "508.4",
    "508.9": "508.4",
    "513.200": "513.2",
    "709.4": "709.2",
}


def _find_anchor(code: str, base_codes: list[str]) -> str:
    """Určí stávající kód symbolu, za který má být nový symbol zařazen."""
    if code in _ANCHORS and _ANCHORS[code] in base_codes:
        return _ANCHORS[code]
    prefix = _series(code)
    same_series = [c for c in base_codes if _series(c) == prefix]
    if same_series:
        return same_series[-1]
    try:
        num = int(prefix)
        prev = [c for c in base_codes if _series(c).isdigit() and int(_series(c)) < num]
        if prev:
            return prev[-1]
    except ValueError:
        pass
    return base_codes[-1] if base_codes else ""


@dataclass
class MergeReport:
    added: list[tuple[str, str]] = field(default_factory=list)  # (kód, název)
    replaced: list[tuple[str, str]] = field(default_factory=list)  # donor nahradil OOM
    dropped: list[str] = field(default_factory=list)  # OOM kódy odstraněné ze sady
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
    """Priorita barvy v donoru → priorita v základní sadě (příp. nově přidaná).

    Nové barvy se nelepí na konec CRT (pod žlutou by zmizely ulice), ale
    rezervují se a při ``finalize_new_colors`` se vloží těsně nad Yellow 100%.
    """

    def __init__(self, base_colors_xml: str, donor_colors_xml: str, report: MergeReport):
        self.base = _parse_color_entries(base_colors_xml)
        self.donor = dict(_parse_color_entries(donor_colors_xml))
        self.report = report
        # Dočasné ID pro nové barvy (mimo rozsah base) → po finalize skutečná priorita.
        self._temp_pri = max((p for p, _ in self.base), default=-1) + 1000
        self.pending_new: list[tuple[int, str, tuple[float, ...]]] = []  # temp_pri, name, cmyk
        self.appended: list[str] = []  # po finalize: XML nových barev (už ne na konec)
        self.map: dict[int, int] = {}
        self._shift_map: dict[int, int] = {}  # staré base priority → po vložení nových

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
                out = self._temp_pri
                self._temp_pri += 1
                self.pending_new.append((out, name, cmyk))
                self.report.color_added.append(f"(pending) '{name}'")
        self.map[pri] = out
        return out

    def finalize_new_colors(self, colors_xml: str) -> tuple[str, dict[int, int]]:
        """Vloží pending barvy těsně nad Yellow 100%; vrátí colors XML + remap priorit."""
        if not self.pending_new:
            return colors_xml, {}
        yellow = next(
            (p for p, x in self.base if "yellow 100%" in _color_name(x)),
            None,
        )
        insert_at = yellow if yellow is not None else max((p for p, _ in self.base), default=0) + 1
        n = len(self.pending_new)
        # Base priority p → p (pokud p < insert_at) nebo p+n (pokud p >= insert_at).
        shift = {p: (p if p < insert_at else p + n) for p, _ in self.base}
        self._shift_map = shift
        # Temp priority → finální slot těsně nad (původní) žlutou.
        temp_to_final = {
            temp: insert_at + i for i, (temp, _name, _cmyk) in enumerate(self.pending_new)
        }
        # Donor map: existující base přes shift, nové přes temp_to_final.
        for donor_pri, mapped in list(self.map.items()):
            if mapped in temp_to_final:
                self.map[donor_pri] = temp_to_final[mapped]
            elif mapped in shift:
                self.map[donor_pri] = shift[mapped]
        new_entries: list[tuple[int, str]] = []
        for p, xml in self.base:
            new_pri = shift[p]
            new_entries.append(
                (new_pri, re.sub(r'\bpriority="\d+"', f'priority="{new_pri}"', xml, count=1))
            )
        for i, (_temp, name, cmyk) in enumerate(self.pending_new):
            new_pri = insert_at + i
            new_entries.append((new_pri, _plain_color_xml(new_pri, name, cmyk)))
            self.report.color_added.append(f"{new_pri} '{name}'")
        # Odstraň pending řádky z reportu.
        self.report.color_added = [x for x in self.report.color_added if not x.startswith("(pending)")]
        new_entries.sort(key=lambda e: e[0])
        body = "\n".join(x for _, x in new_entries)
        out = re.sub(
            r"(<colors\s+count=\"\d+\"[^>]*>).*</colors>",
            rf'\1\n{body}\n</colors>',
            colors_xml,
            count=1,
            flags=re.S,
        )
        out = re.sub(
            r'<colors\s+count="\d+"',
            f'<colors count="{len(new_entries)}"',
            out,
            count=1,
        )
        self.appended = []  # už jsou ve colors_xml
        # Remap pro symboly, které ještě mají temp / staré base priority.
        remap = dict(shift)
        remap.update(temp_to_final)
        return out, remap


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
    *,
    prefer_donor: set[str] | frozenset[str] | None = None,
    drop_base: set[str] | frozenset[str] | None = None,
) -> tuple[str, MergeReport]:
    """Vrátí ``base_text`` s doplněnými symboly z ``donor_text`` + report.

    ``equivalents``: ručně určená dvojčata ``{nový kód: starý kód}`` pro případy,
    kdy se názvy liší (nový „Paved footpath, 4 m“ ≙ starý „…footprint: 4 m“).

    ``prefer_donor``: kódy, u kterých má ikona/definice z donoru (OCAD) přednost
    před OOM – existující OOM symbol se nahradí. ``drop_base``: další OOM kódy
    k odstranění (např. staré footprinty 501.6–501.19 po přechodu na OCAD schéma).
    """
    equivalents = equivalents or {}
    prefer_donor = set(prefer_donor or ())
    drop_base = set(drop_base or ())
    for code in prefer_donor:
        drop_base.add(code)
        if code.endswith(".0"):
            drop_base.add(normalize_code(code))
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

    base_symbols_slices = _top_level_symbols(base_symbols_xml)
    base_names: dict[str, str] = {}
    base_codes_order: list[str] = []
    for _s, _e, a in base_symbols_slices:
        c = _attr(a, "code")
        if c:
            base_names[c] = _attr(a, "name") or ""
            base_codes_order.append(c)
    base_codes = set(base_names)
    base_tokens: dict[str, list[tuple[frozenset[str], str]]] = {}
    for bc, bn in base_names.items():
        base_tokens.setdefault(_series(bc), []).append((name_tokens(bn), bc))

    kept_codes_order = [c for c in base_codes_order if c not in drop_base]

    next_id = _next_symbol_id(base_symbols_xml)
    id_map: dict[str, str] = {}  # id v donoru → nové id (kvůli <part symbol=>)
    pending: list[tuple[str, str]] = []  # (code, xml)

    for start, end, attrs in _top_level_symbols(donor_symbols_xml):
        code = _attr(attrs, "code") or ""
        did = _attr(attrs, "id")
        if not code:
            continue
        if _attr(attrs, "type") in SKIP_TYPES:
            report.skipped_type += 1
            continue
        nm = _attr(attrs, "name") or ""
        force = code in prefer_donor
        if not force:
            # Stejný význam v téže číselné řadě (532.1 „3 m“ ≙ 532.8 „footprint 3 m“).
            twin = equivalents.get(code) or next(
                (
                    bc
                    for toks, bc in base_tokens.get(_series(code), [])
                    if toks == name_tokens(nm)
                ),
                None,
            )
            if twin is not None:
                report.twins.append((code, nm, twin))
                continue
            same_code = (
                normalize_code(code)
                if normalize_code(code) in base_codes
                else (code if code in base_codes else None)
            )
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
        pending.append((code, xml))
        if force and (code in base_codes or normalize_code(code) in base_codes):
            report.replaced.append((code, nm))
        else:
            report.added.append((code, nm))

    def _part_repl(m: re.Match[str]) -> str:
        return f'part symbol="{id_map.get(m.group(1), m.group(1))}"'

    new_symbols = [(code, re.sub(r'part symbol="(\d+)"', _part_repl, x)) for code, x in pending]
    if not new_symbols and not drop_base:
        return base_text, report

    prefer_syms = [(c, x) for c, x in new_symbols if c in prefer_donor]
    other_syms = [(c, x) for c, x in new_symbols if c not in prefer_donor]
    # Prefer-donor blok v pořadí z donoru (OCAD 501.0, .1, .10, …).
    insert_after: dict[str, list[str]] = {}
    if prefer_syms:
        root = _find_anchor(prefer_syms[0][0], kept_codes_order or base_codes_order)
        insert_after[root] = [x for _, x in prefer_syms]
    for code, sym_xml in other_syms:
        anchor = _find_anchor(code, kept_codes_order or base_codes_order)
        insert_after.setdefault(anchor, []).append(sym_xml)

    ordered_syms: list[str] = []
    for s, e, attrs in base_symbols_slices:
        bc = _attr(attrs, "code") or ""
        if bc in drop_base:
            report.dropped.append(bc)
            continue
        ordered_syms.append(base_symbols_xml[s:e])
        if bc in insert_after:
            ordered_syms.extend(insert_after.pop(bc))
    for leftover in insert_after.values():
        ordered_syms.extend(leftover)

    # Mapper očekává, že pořadí <symbol> odpovídá ID od 0. Po finálním
    # seřazení proto všechny symboly přečíslujeme a přemapujeme odkazy
    # na jejich části (<part symbol="...">).
    id_translation: dict[str, str] = {}
    for new_id, sym_xml in enumerate(ordered_syms):
        old_id = re.search(r'\bid="(\d+)"', sym_xml)
        if old_id:
            id_translation[old_id.group(1)] = str(new_id)

    ordered_syms = [
        re.sub(
            r'\bid="(\d+)"',
            lambda m: f'id="{id_translation[m.group(1)]}"',
            sym_xml,
            count=1,
        )
        for sym_xml in ordered_syms
    ]
    ordered_syms = [
        re.sub(
            r'part symbol="(\d+)"',
            lambda m: f'part symbol="{id_translation.get(m.group(1), m.group(1))}"',
            sym_xml,
        )
        for sym_xml in ordered_syms
    ]

    # Obalující tag <symbols ...> zachováme a tělo nahradíme seřazenými symboly
    prefix_m = re.match(r"<symbols\s+count=\"\d+\"[^>]*>\s*", base_symbols_xml)
    syms_prefix = prefix_m.group(0) if prefix_m else '<symbols count="0">\n'
    merged_symbols = syms_prefix + "".join(ordered_syms) + "\n</symbols>"
    total = len(_top_level_symbols(merged_symbols))
    merged_symbols = re.sub(
        r'<symbols\s+count="\d+"', f'<symbols count="{total}"', merged_symbols, count=1
    )
    # Nové barvy nad Yellow 100% + posun CRT; přemapovat odkazy ve všech symbolech.
    merged_colors, color_remap = mapper.finalize_new_colors(base_colors.group(0))
    report.color_map = dict(mapper.map)
    if color_remap:
        ordered_syms = [_remap_colors_in_xml(s, color_remap) for s in ordered_syms]
        # ID symbolů už jsou finální 0..n-1 – znovu složit tělo.
        merged_symbols = syms_prefix + "".join(ordered_syms) + "\n</symbols>"
        merged_symbols = re.sub(
            r'<symbols\s+count="\d+"', f'<symbols count="{total}"', merged_symbols, count=1
        )
    out = base_text.replace(base_symbols_xml, merged_symbols, 1)
    if merged_colors != base_colors.group(0):
        out = out.replace(base_colors.group(0), merged_colors, 1)
    return out, report

__all__ = ["MergeReport", "merge_missing_symbols", "normalize_code", "build_color_map"]
