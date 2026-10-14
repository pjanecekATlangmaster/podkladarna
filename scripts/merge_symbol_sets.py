"""Doplní do OOM sad symbolů (configs/oom) symboly, které jsou jen v novější OCAD sadě.

Zdroj: ``configs/oom/ocad_src/*.omap`` = čisté OCAD sady (.ocd) otevřené a uložené
v Mapperu jako .omap. Výstup: ``configs/oom/merged/<stejný název>.omap``, který
``symbol_set_path`` upřednostní. Viz ``app/pipeline/symbol_merge.py``.

    python scripts/merge_symbol_sets.py
"""

from __future__ import annotations

import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.pipeline.symbol_merge import merge_missing_symbols  # noqa: E402

OOM = ROOT / "configs" / "oom"
SRC = OOM / "ocad_src"
OUT = OOM / "merged"
TEMP_SS = Path(r"D:\Temp\ss")

# (OOM sada, OCAD zdroj). ISMTBOM se nesloučí – v nové sadě má jiné číslování.
# Donor může být v D:\Temp\ss (např. 'Orienteering Map ISSprOM 2019 4000.omap')
# nebo v configs/oom/ocad_src ('ISSprOM_2019_4000.omap').
PAIRS = [
    (
        "ISOM_2017-2_10000.omap",
        ("Orienteering Map ISOM 2017 10 000.omap", "ISOM_2017_10000.omap"),
    ),
    (
        "ISOM_2017-2_15000.omap",
        ("Orienteering Map ISOM 2017 15 000.omap", "ISOM_2017_15000.omap"),
    ),
    (
        "ISSprOM_2019_4000.omap",
        ("Orienteering Map ISSprOM 2019 4000.omap", "ISSprOM_2019_4000.omap"),
    ),
]


def _find_donor(donor_names: tuple[str, ...]) -> Path | None:
    for name in donor_names:
        if TEMP_SS.is_dir() and (TEMP_SS / name).is_file():
            return TEMP_SS / name
        if (SRC / name).is_file():
            return SRC / name
    return None


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    eq_path = OOM / "merge_equivalents.json"
    overrides_path = OOM / "merge_overrides.json"
    equivalents = json.loads(eq_path.read_text(encoding="utf-8")) if eq_path.is_file() else {}
    overrides = (
        json.loads(overrides_path.read_text(encoding="utf-8")) if overrides_path.is_file() else {}
    )
    for base_name, donor_names in PAIRS:
        base = OOM / base_name
        donor = _find_donor(donor_names)
        if donor is None:
            print(f"CHYBÍ zdroj pro {base_name} (hledáno v {TEMP_SS} i {SRC})")
            return 1
        ov = overrides.get(base_name) or {}
        merged, rep = merge_missing_symbols(
            base.read_text(encoding="utf-8"),
            donor.read_text(encoding="utf-8"),
            equivalents.get(base_name) or {},
            prefer_donor=set(ov.get("prefer_donor") or ()),
            drop_base=set(ov.get("drop_base") or ()),
        )
        (OUT / base_name).write_text(merged, encoding="utf-8", newline="\n")
        print(
            f"{base_name}: +{len(rep.added)} symbolů, nahrazeno {len(rep.replaced)}, "
            f"drop OOM {len(rep.dropped)} "
            f"(dvojčat přeskočeno {len(rep.twins)}, kód už existuje {rep.skipped_existing} z toho s jiným významem {len(rep.collisions)}, text {rep.skipped_type}); "
            f"barev nepřesně {len(rep.color_inexact)}, přidáno nových {len(rep.color_added)}"
        )
        for code, name in rep.replaced:
            print(f"    ~ {code:9} {name}")
        for code, name in rep.added:
            print(f"    + {code:9} {name}")
        if rep.dropped:
            print(f"    - drop {', '.join(rep.dropped)}")
        for c, nm, oc, on in rep.collisions:
            print(f"    ! kolize {c:8} nový '{nm}' x starý {oc} '{on}'")
        for c in rep.color_inexact:
            print(f"    ~ barva {c}")
        for c in rep.color_added:
            print(f"    * nová barva {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
