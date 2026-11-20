"""Report odchylek naší kresby od referenčních map z Liveloxu.

python report.py <results.json> <out.md> [<history.jsonl>]

results.json = {oblast: {"ref": {...references.yaml}, "job": id, "version": "...", "m": výstup measure.py | "error": "..."}}
Prahy jsou hrubé: hlásí jen výrazné odchylky (zeleň místo bílého lesa, mnohem hustší vrstevnice …), ne ladění detailů.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

# (metrika, popis, absolutní práh v p. b., poměrové meze, min. podíl v referenci pro poměr)
AREA_CHECKS = {
    "les": [
        ("white", "bílý les", 0.15, (0.6, 1.6), 0.10),
        ("green", "zeleň 406–410", 0.15, (0.5, 2.0), 0.05),
        ("open", "otevřený prostor 401/403", 0.10, (0.4, 2.5), 0.05),
        ("water", "voda a bažiny", 0.05, (0.3, 3.0), 0.02),
    ],
    "sprint": [
        ("grey", "budovy", 0.10, (0.5, 2.0), 0.05),
        ("paved", "zpevněné 501", 0.10, (0.5, 2.0), 0.05),
        ("open", "otevřený prostor 401/403", 0.10, (0.4, 2.5), 0.05),
        ("green", "zeleň", 0.10, (0.4, 2.5), 0.05),
        ("olive", "oliva 520", 0.08, (0.3, 3.0), 0.03),
    ],
}
CONTOUR_RATIO = (0.6, 1.6)   # délka vrstevnic na km² naše / reference (jen les)
F1_GREEN_MIN = 0.25          # pod tím zeleň leží jinde než v referenci
DRIFT_PP = 0.05              # změna proti minulému běhu, která se hlásí i bez překročení prahů


def check(kind: str, m: dict) -> list[str]:
    o, r = m["ours"], m["ref"]
    out = []
    for key, label, abs_pp, (lo, hi), min_ref in AREA_CHECKS[kind]:
        d = o[key] - r[key]
        ratio = o[key] / r[key] if r[key] > 0 else None
        if abs(d) > abs_pp or (r[key] >= min_ref and ratio is not None and not lo <= ratio <= hi):
            out.append(f"{label}: naše {o[key]:.0%}, reference {r[key]:.0%}")
    if kind == "les":
        rc, oc = r["contour_km_per_km2"], o["contour_km_per_km2"]
        if rc > 0 and not CONTOUR_RATIO[0] <= oc / rc <= CONTOUR_RATIO[1]:
            out.append(f"vrstevnice: naše {oc:.0f} km/km², reference {rc:.0f} km/km² ({oc / rc:.1f}×)")
        if m.get("f1_green") is not None and r["green"] >= 0.05 and m["f1_green"] < F1_GREEN_MIN:
            out.append(f"zeleň leží jinde než v referenci (F1 {m['f1_green']:.2f})")
    return out


def drift(m: dict, prev: dict | None) -> list[str]:
    if not prev:
        return []
    keys = ("white", "green", "open", "water", "grey", "paved")
    return [f"{k}: {prev['ours'][k]:.0%} → {m['ours'][k]:.0%} proti minulému běhu"
            for k in keys if abs(m["ours"][k] - prev["ours"][k]) > DRIFT_PP]


def _last(history: Path | None) -> dict:
    last = {}
    if history and history.exists():
        for line in history.read_text().splitlines():
            try:
                e = json.loads(line); last[e["id"]] = e
            except (ValueError, KeyError):
                pass
    return last


def main(results_p: Path, out_md: Path, history: Path | None) -> int:
    res = json.loads(results_p.read_text())
    prev = _last(history)
    today = date.today().isoformat()
    rows, details, flagged = [], [], 0
    for key, e in res.items():
        ref = e["ref"]
        if "error" in e:
            rows.append(f"| {key} | {ref['country']} | {ref['kind']} | ⚠ | job selhal: {e['error']} |")
            flagged += 1
            continue
        m = e["m"]
        issues = check(ref["kind"], m)
        # odchylky, které Petr přijal jako známé omezení dat (references.yaml `accepted`), se nehlásí červeně
        acc = ref.get("accepted") or {}
        known = [i for i in issues if i.split(":")[0] in acc]
        issues = [i for i in issues if i not in known]
        dr = drift(m, (prev.get(key) or {}).get("m"))
        flag = "🔴" if issues else ("🟡" if dr else ("⚪" if known else "🟢"))
        flagged += bool(issues)
        o, r = m["ours"], m["ref"]
        if ref["kind"] == "les":
            summary = (f"bílá {o['white']:.0%}/{r['white']:.0%}, zeleň {o['green']:.0%}/{r['green']:.0%}, "
                       f"otevřené {o['open']:.0%}/{r['open']:.0%}, vrstevnice {o['contour_km_per_km2']:.0f}/{r['contour_km_per_km2']:.0f} km/km², "
                       f"F1 zeleň {m['f1_green']}")
        else:
            summary = (f"budovy {o['grey']:.0%}/{r['grey']:.0%}, 501 {o['paved']:.0%}/{r['paved']:.0%}, "
                       f"otevřené {o['open']:.0%}/{r['open']:.0%}, zeleň {o['green']:.0%}/{r['green']:.0%}, oliva {o['olive']:.0%}/{r['olive']:.0%}")
        rows.append(f"| {key} | {ref['country']} | {ref['kind']} | {flag} | {summary} |")
        if issues or dr or known:
            parts = issues + dr + [f"{i} (známé: {acc[i.split(':')[0]]})" for i in known]
            details.append(f"**{key}** ({ref['terrain']}; {ref['data']}): " + "; ".join(parts))
        if history:
            with history.open("a") as f:
                f.write(json.dumps({"id": key, "date": today, "version": e.get("version"), "m": m}) + "\n")
    md = [f"# Kontrola kvality proti Liveloxu – {today}", "",
          f"Verze {next((e.get('version') for e in res.values() if e.get('version')), '?')}, oblastí {len(res)}, "
          f"s výraznou odchylkou {flagged}. Hodnoty naše / reference (podíl plochy mapy).", "",
          "| Oblast | Země | Druh | Stav | Naše / reference |", "|---|---|---|---|---|", *rows, ""]
    if details:
        md += ["## Odchylky", "", *[f"- {d}" for d in details], ""]
    md += ["🔴 výrazná odchylka od reference, 🟡 změna proti minulému běhu, ⚪ jen známé odchylky, 🟢 v mezích.",
           "Reference jsou mapy mapařů z Liveloxu (jiný rok, jiný mapař); slouží jen k odhalení hrubých chyb."]
    out_md.write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return flagged


if __name__ == "__main__":
    a = sys.argv[1:]
    main(Path(a[0]), Path(a[1]), Path(a[2]) if len(a) > 2 else None)
