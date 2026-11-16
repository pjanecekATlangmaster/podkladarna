"""Vygeneruje docs/zdroje-dat.md z registru configs/data_sources.yaml.

Spuštění z kořene repozitáře:  python scripts/data_sources_doc.py
Kontrola bez zápisu (CI / test): python scripts/data_sources_doc.py --check
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "configs" / "data_sources.yaml"
COUNTRIES = ROOT / "configs" / "countries.yaml"
DOC = ROOT / "docs" / "zdroje-dat.md"

KINDS = {
    "elevation": "výšky (DTM/DSM)",
    "point_cloud": "LiDAR mračno",
    "canopy": "výška porostu",
    "technical_map": "technická mapa",
    "vectors": "vektory",
    "buildings": "budovy",
    "orthophoto": "ortofoto",
    "map": "mapa",
    "cadastre": "katastr",
    "hillshade": "stínování",
    "imagery": "družicové snímky",
}
LICENSE_CLASS = {"public": "🟢 public", "restricted": "🟠 omezený", "unknown": "⚪ nejasná"}
ACCESS = {
    "free": "volný",
    "email": "⚠ e-mail",
    "api_key": "⚠ API klíč",
    "account": "⚠ účet",
    "contract": "⚠ smlouva / poplatek",
}
STATUS = {"used": "napojeno", "off": "vypnuto", "candidate": "kandidát", "rejected": "nevhodné"}
# Názvy zemí mimo countries.yaml (kandidáti).
EXTRA_NAMES = {
    "PL": "Polsko", "UK": "Spojené království", "DK": "Dánsko", "SE": "Švédsko", "NO": "Norsko",
    "ES": "Španělsko", "SI": "Slovinsko", "LV": "Lotyšsko",
}


def load() -> tuple[list[dict], dict]:
    sources = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))["sources"]
    countries = yaml.safe_load(COUNTRIES.read_text(encoding="utf-8"))
    return sources, countries


def probes_of(s: dict) -> list[str]:
    """Klíče sond stavu zdrojů u záznamu (`probe` je řetězec nebo seznam)."""
    p = s.get("probe")
    return [] if not p else [p] if isinstance(p, str) else list(p)


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _row(s: dict) -> str:
    lic = s["license"]
    lic_text = f"{LICENSE_CLASS[lic['class']]} – [{_cell(lic['name'])}]({lic['url']})"
    if lic.get("attribution"):
        lic_text += f"<br>citace: „{_cell(lic['attribution'])}“"
    source = f"**{_cell(s['name'])}**<br>{_cell(s['provider'])}<br><{s['url']}>"
    if s.get("info") and s["info"] != s["url"]:
        source += f" · [info]({s['info']})"
    status = STATUS[s["status"]] + f" ({s['role']})"
    if probes_of(s):
        status += "<br>sonda " + ", ".join(f"`{p}`" for p in probes_of(s))
    issues = "<br>".join(f"• {_cell(i)}" for i in s.get("issues") or []) or "–"
    kinds = ", ".join(KINDS[k] for k in s["kinds"])
    return (
        f"| {kinds} | {source} | {lic_text} | {ACCESS[s['access']]} | {status} | {issues} | "
        f"{_cell(s['checked'])} |"
    )


HEADER = (
    "| Druh dat | Zdroj a server | Licence | Přístup | Stav | Problémy a poznámky | Ověřeno |\n"
    "|---|---|---|---|---|---|---|"
)


def render() -> str:
    sources, countries = load()
    out = [
        "# Zdroje dat po zemích",
        "",
        "> Generováno z `configs/data_sources.yaml` skriptem `scripts/data_sources_doc.py` – needitovat ručně.",
        "> Změnu zapiš do registru a dokument přegeneruj (`tests/test_data_sources.py` hlídá, že sedí).",
        "> Kontrola jiným modelem: prompt v `docs/zdroje-dat-kontrola-prompt.md`.",
        "",
        "Licence: 🟢 public = otevřená licence včetně komerčního použití (může vyžadovat citaci) · "
        "🟠 omezený = podmínky, schválení, poplatek nebo nekomerční · ⚪ nejasná = licence neuvedena nebo nenalezena.",
        "Přístup: volný = bez registrace; ⚠ e-mail / API klíč / účet / smlouva = je potřeba něco navíc.",
        "Ověřeno: datum a způsob ověření licence; „neověřeno“ = převzato z podkladů projektu, na stránce "
        "poskytovatele neověřeno.",
        "",
        "## Přehled zemí",
        "",
        "| Země | Stav v Podkladárně | Výšky | Technická mapa | Zdrojů | S poznámkou / omezením |",
        "|---|---|---|---|---|---|",
    ]
    order = list(countries)
    for code in sorted({c for s in sources for c in s["countries"] if c != "*" and c not in countries}):
        order.append(code)
    for code in order:
        own = [s for s in sources if code in s["countries"]]
        if not own:
            continue
        if code in countries:
            c = countries[code]
            name = c["name"]["cs"]
            status = "off" if c["status"] is False else c["status"]  # YAML čte off jako false
            elev, tech = c.get("elevation", "–"), c.get("technical_map", "–")
        else:
            name, status, elev, tech = EXTRA_NAMES.get(code, code), "kandidát", "–", "–"
        bad = sum(1 for s in own if s.get("issues") or s["license"]["class"] != "public"
                  or s["access"] != "free")
        out.append(f"| {code} {name} | {status} | `{elev}` | `{tech}` | {len(own)} | {bad} |")
    out += ["", "## Všechny země (`*`)", "", HEADER]
    out += [_row(s) for s in sources if "*" in s["countries"]]
    for code in order:
        own = [s for s in sources if code in s["countries"]]
        if not own:
            continue
        name = countries[code]["name"]["cs"] if code in countries else EXTRA_NAMES.get(code, code)
        out += ["", f"## {code} – {name}", ""]
        if code in countries:
            c = countries[code]
            note = f"Stav `{'off' if c['status'] is False else c['status']}`, CRS `{c.get('crs', '–')}`."
            if c.get("citation"):
                note += f" Citace v mapě: „{c['citation']}“."
            out += [note, ""]
        out += [HEADER] + [_row(s) for s in own]
    out.append("")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        if not DOC.exists() or DOC.read_text(encoding="utf-8") != text:
            print("docs/zdroje-dat.md neodpovídá registru – spusť python scripts/data_sources_doc.py")
            return 1
        return 0
    DOC.parent.mkdir(parents=True, exist_ok=True)
    DOC.write_text(text, encoding="utf-8", newline="\n")
    print(f"zapsáno {DOC.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
