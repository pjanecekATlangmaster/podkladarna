"""Vygeneruje čisté české OCAD sady symbolů a katalog symbolů.

Zdroj: ``configs/oom/ocad_src/*.omap`` (OCAD Revize 6 otevřené a uložené v Mapperu).
Výstup:
  * ``configs/oom/ISOM_2017_10000_cz.omap``, ``ISOM_2017_15000_cz.omap``,
    ``ISSprOM_2019_4000_cz.omap`` – bez skrytých symbolů, s českými názvy symbolů
    (včetně vnořených částí); kódy, id a názvy barev zůstávají jako v OCAD;
  * ``configs/oom/symbol_catalog.yaml`` – kostra katalogu (kód OCAD, český název,
    kódy generátoru, geometrie, příznak ke kontrole).

Při opakovaném běhu se z existujícího katalogu zachovají ručně upravené
``cz_name``, ``generator_codes``, ``review`` a ``keep_reason``; doplní se jen
nové symboly (s prázdnými ``generator_codes`` – mapování se dělá ručně, runtime
nikdy nepoužívá pravidlo „kód + .0“).

    python scripts/build_symbol_sets.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from xml.sax.saxutils import unescape

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app.pipeline.oom_symbols import (  # noqa: E402
    _top_level_symbols,
    drop_symbols_by_id,
    hidden_symbol_ids,
)
from app.pipeline.symbol_catalog import normalize_code  # noqa: E402
from symbol_names_cz import NAMES  # noqa: E402

OOM = ROOT / "configs" / "oom"
SRC = OOM / "ocad_src"
CATALOG_PATH = OOM / "symbol_catalog.yaml"

# (klíč katalogu, zdroj, výstup)
SETS = [
    ("isom", "ISOM_2017_10000.omap", "ISOM_2017_10000_cz.omap"),
    ("isom", "ISOM_2017_15000.omap", "ISOM_2017_15000_cz.omap"),
    ("sprint", "ISSprOM_2019_4000.omap", "ISSprOM_2019_4000_cz.omap"),
    ("mtbo", "ISMTBOM_2022_10000.omap", "ISMTBOM_2022_10000_cz.omap"),
    ("mtbo", "ISMTBOM_2022_15000.omap", "ISMTBOM_2022_15000_cz.omap"),
]

# Sady, u nichž se skryté symboly (is_hidden="true") neodstraňují všechny: zůstanou jen ty, jejichž kód
# generátor používá (``generator_codes`` v katalogu); při tvorbě mapy se odkryjí ty, které generátor
# skutečně použil (build_oom_map). Ostatní skryté symboly se z MTBO sady odstraní.
KEEP_HIDDEN_USED_ONLY = frozenset({"mtbo"})

# Pomocné symboly mimo normu (pokud by ve zdroji byly, smažou se).
_NON_STANDARD_CODES = frozenset({"799", "799.0", "999", "999.0"})

_GEOM = {"1": "point", "2": "line", "4": "area", "8": "text"}

# Přípony pomocných vnořených částí (původně česky bez diakritiky z OCAD exportu).
_PART_SUFFIX = {
    "hlavni linie": "hlavní linie",
    "dvojité linie": "dvojitá linie",
    "dvojita linie": "dvojitá linie",
    "orámování": "orámování",
    "oramovani": "orámování",
}

_SYMBOLS_RE = re.compile(r'<symbols\s+count="\d+"[^>]*>.*?</symbols>', re.DOTALL)
_OPEN_TAG_RE = re.compile(r"<symbol\b([^>]*?)(/?)>")


def _attr(attrs: str, name: str) -> str | None:
    m = re.search(rf'\b{name}="([^"]*)"', attrs)
    return m.group(1) if m else None


def _quote(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


def _top_level_info(symbols_xml: str) -> list[tuple[str, str, str]]:
    """[(code, geom, english_name)] top-level symbolů v pořadí souboru.

    ``geom`` = point / line / area / text. Kombinovaný symbol (type 16) je
    plocha, jen když některá jeho část je plocha (type 4); jinak čára
    (např. 502, 508.x, 509, 532, 501.1x ve sprintu = čára s okraji).
    """
    tops = _top_level_symbols(symbols_xml)
    id_type = {_attr(a, "id"): _attr(a, "type") for _s, _e, a in tops if _attr(a, "id")}
    out = []
    for s, e, attrs in tops:
        typ = _attr(attrs, "type") or ""
        if typ == "16":
            seg = symbols_xml[s:e]
            part_types = set(re.findall(r'<symbol\b[^>]*\btype="(\d+)"', seg[1:]))
            part_types |= {
                id_type.get(r, "") for r in re.findall(r'<part symbol="(\d+)"', seg)
            }
            typ = "4" if "4" in part_types else "2"
        out.append(
            (
                _attr(attrs, "code") or "",
                _GEOM.get(typ, "area"),
                unescape(_attr(attrs, "name") or "").strip(),
            )
        )
    return out


def clean_symbols(symbols_xml: str, keep_codes: frozenset[str] | set[str] = frozenset()) -> str:
    """Odstraní skryté symboly (kromě kódů ``keep_codes``) a nestandardní pomocné kódy."""
    drop = {sid for sid, code in hidden_symbol_ids(symbols_xml).items() if code not in keep_codes}
    for _s, _e, attrs in _top_level_symbols(symbols_xml):
        if _attr(attrs, "code") in _NON_STANDARD_CODES:
            sid = _attr(attrs, "id")
            if sid:
                drop.add(sid)
    return drop_symbols_by_id(symbols_xml, drop)


def rename_symbols(symbols_xml: str, cz_names: dict[str, str]) -> str:
    """Přejmenuje top-level i vnořené symboly na české názvy z katalogu."""
    top_starts = {s for s, _e, _a in _top_level_symbols(symbols_xml)}
    # Kód rodiče pro každý vnořený symbol (pro příponu „- hlavní linie“ apod.).
    parent_of: dict[int, str] = {}
    for s, e, attrs in _top_level_symbols(symbols_xml):
        pc = _attr(attrs, "code") or ""
        for m in _OPEN_TAG_RE.finditer(symbols_xml, s + 1, e):
            parent_of[m.start()] = pc

    def repl(m: re.Match[str]) -> str:
        attrs, selfclose = m.group(1), m.group(2)
        old = _attr(attrs, "name")
        if old is None:
            return m.group(0)
        code = _attr(attrs, "code") or ""
        old_plain = unescape(old).strip()
        if m.start() in top_starts:
            new = cz_names[code]
        else:
            sfx = re.match(r"^.*? - (.+)$", old_plain)
            parent = parent_of.get(m.start(), "")
            base_code = code if code in cz_names else parent
            if sfx and sfx.group(1).strip().lower() in {k.lower() for k in _PART_SUFFIX}:
                tail = _PART_SUFFIX[
                    next(k for k in _PART_SUFFIX if k.lower() == sfx.group(1).strip().lower())
                ]
                new = f"{cz_names[base_code]} – {tail}"
            elif code and code in cz_names:
                new = cz_names[code]
            elif not code:
                return m.group(0)  # pomocný element bez kódu (už česky)
            else:
                raise ValueError(f"Nelze přejmenovat vnořený symbol {code!r}: {old_plain!r}")
        new_attrs = attrs.replace(f'name="{old}"', f'name="{_quote(new)}"', 1)
        return f"<symbol{new_attrs}{selfclose}>"

    return _OPEN_TAG_RE.sub(repl, symbols_xml)


def _norm_en(name: str) -> str:
    return re.sub(r"\s+", " ", name.lower().replace("&amp;", "&")).strip()


def mtbo_names(
    info: list[tuple[str, str, str]],
    others: dict[str, list[tuple[str, str, str]]],
) -> dict[str, tuple[str, bool]]:
    """České názvy ISMTBOM: vlastní ``NAMES['mtbo']``, jinak shoda s ISOM/ISSprOM.

    Převzetí z jiné sady jen při shodě kódu, geometrie i anglického názvu
    (číslování MTBO se v části man-made liší, shoda samotného kódu nestačí).
    """
    names = dict(NAMES["mtbo"])
    for code, geom, en in info:
        if code in names:
            continue
        for key, other in others.items():
            if any(
                c == code and g == geom and _norm_en(e) == _norm_en(en)
                for c, g, e in other
            ) and code in NAMES[key]:
                names[code] = NAMES[key][code]
                break
        else:
            raise SystemExit(f"MTBO {code} ({en}): chybí český název v symbol_names_cz.py")
    return names


def build_catalog_entries(
    key: str,
    info: list[tuple[str, str, str]],
    existing: dict[str, dict],
    names: dict[str, tuple[str, bool]] | None = None,
) -> list[dict]:
    names = names if names is not None else NAMES[key]
    entries: list[dict] = []
    for code, typ, en_name in info:
        prev = existing.get(code, {})
        if code in names:
            auto_name, auto_review = names[code]
        else:
            auto_name, auto_review = en_name, True  # nový symbol: věrný přepis, zkontrolovat
        entry = {
            "ocad_code": code,
            "cz_name": prev.get("cz_name") or auto_name,
            # Kódy generátoru se doplňují ručně (věcně, ne pravidlem „kód bez .0“).
            "generator_codes": list(prev.get("generator_codes") or []),
            "geom": typ,
            "review": bool(prev["review"]) if "review" in prev else auto_review,
        }
        keep = prev.get("keep_reason") or _keep_reason(code, typ)
        if keep:
            entry["keep_reason"] = keep
        entries.append(entry)
    return entries


def _keep_reason(code: str, typ: str) -> str:
    base = normalize_code(code).split(".")[0]
    if code in ("102.3", "603.1"):
        return "popisek/text; generátor nepoužívá, ponecháno pro editaci v Mapperu"
    if typ == "text":
        return "textový symbol; generátor nepoužívá, ponecháno pro editaci v Mapperu"
    if base.startswith("7"):
        return "řada 7xx (tratě); generátor nepoužívá, ponecháno pro editaci v Mapperu"
    return ""


def _dump_catalog(catalog: dict[str, list[dict]]) -> str:
    lines = [
        "# Katalog symbolů čistých OCAD sad (*_cz.omap). Generuje scripts/build_symbol_sets.py;",
        "# ruční úpravy cz_name / generator_codes / review / keep_reason se při dalším běhu zachovají.",
        "# generator_codes: kódy používané generátorem, které se mapují na tento symbol.",
        "# Předvyplněné „kód bez .0“ je jen návrh – runtime pravidlo +.0 nikdy nepoužívá.",
    ]
    for key, entries in catalog.items():
        lines.append(f"{key}:")
        for e in entries:
            lines.append(f"  - ocad_code: {json.dumps(e['ocad_code'])}")
            lines.append(f"    cz_name: {json.dumps(e['cz_name'], ensure_ascii=False)}")
            codes = ", ".join(json.dumps(c) for c in e["generator_codes"])
            lines.append(f"    generator_codes: [{codes}]")
            lines.append(f"    geom: {e['geom']}")
            lines.append(f"    review: {'true' if e['review'] else 'false'}")
            if e.get("keep_reason"):
                lines.append(f"    keep_reason: {json.dumps(e['keep_reason'], ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def main() -> int:
    existing_all: dict[str, dict[str, dict]] = {}
    if CATALOG_PATH.is_file():
        raw = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8")) or {}
        for key, entries in raw.items():
            existing_all[key] = {str(e["ocad_code"]): e for e in entries}

    # Kódy, které generátor používá (mají ``generator_codes``): skrytý symbol s takovým kódem se nemaže.
    used_codes = {
        key: {code for code, e in entries.items() if e.get("generator_codes")}
        for key, entries in existing_all.items()
        if key in KEEP_HIDDEN_USED_ONLY
    }

    # 1) kostra katalogu (sdílená 10k/15k ISOM) + kontrola shody sad
    infos: dict[str, list[tuple[str, str, str]]] = {}
    sources: dict[str, str] = {}
    for key, src_name, _out in SETS:
        text = (SRC / src_name).read_text(encoding="utf-8", newline="")
        symbols = clean_symbols(_SYMBOLS_RE.search(text).group(0), used_codes.get(key, frozenset()))
        info = _top_level_info(symbols)
        if key in infos:
            if [c for c, _t, _n in info] != [c for c, _t, _n in infos[key]]:
                raise SystemExit(f"Sady {key} nemají shodné kódy: {src_name}")
        else:
            infos[key] = info
        sources[src_name] = text

    catalog = {}
    for key, info in infos.items():
        names = (
            mtbo_names(info, {k: v for k, v in infos.items() if k != "mtbo"})
            if key == "mtbo"
            else None
        )
        catalog[key] = build_catalog_entries(key, info, existing_all.get(key, {}), names)
    CATALOG_PATH.write_text(_dump_catalog(catalog), encoding="utf-8", newline="\n")

    # 2) čisté sady s českými názvy
    for key, src_name, out_name in SETS:
        text = sources[src_name]
        cz_names = {e["ocad_code"]: e["cz_name"] for e in catalog[key]}
        m = _SYMBOLS_RE.search(text)
        before = len(_top_level_symbols(m.group(0)))
        symbols = rename_symbols(clean_symbols(m.group(0), used_codes.get(key, frozenset())), cz_names)
        after = len(_top_level_symbols(symbols))
        out = text[: m.start()] + symbols + text[m.end() :]
        (OOM / out_name).write_text(out, encoding="utf-8", newline="\n")
        print(f"{out_name}: symbolů {before} -> {after}")
    for key, entries in catalog.items():
        print(f"{key}: v katalogu {len(entries)}, review=true {sum(e['review'] for e in entries)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
