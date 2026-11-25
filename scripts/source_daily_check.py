"""Denní kontrola zdrojů dat: cloud + pohled ostré A, třídění výpadků a stručný stav pro koordinátora.

Petr 2026-11-25: denní ruční rozbor dostupnosti zdrojů nezvládá. Jednou denně (naplánovaná úloha v cloudu)
se každý zdroj z ``app.source_status.PROBES`` zkusí stejným lehkým dotazem jako na serveru (jeden dotaz na
URL, po chybě jeden opakovaný pokus; ~100 zdrojů = ~100–250 dotazů za den, žádné stahování dat) a přečte se,
jak tytéž zdroje vidí ostrá A (veřejné ``GET /api/source_status?country=…``, jen čtení uloženého stavu).
Volitelně s ``SOURCE_EVENTS_TOKEN`` i deník ostré A (odmítnuté a selhané joby).

Třídy: ``ok``, ``prechodne`` (5xx / timeout poprvé), ``planovane``, ``rate_limit`` (429), ``url`` (404 / 410),
``pristup`` (401 / 403: licence, klíč, účet), ``sit_a`` (z cloudu běží, z ostré A ne: DNS, certifikát, blokace
českých IP), ``cert`` (certifikát z obou stran), ``trvale`` (mimo provoz ≥ 3 dny za sebou), ``opakovane``
(přechodné ≥ 3 dny z posledních 7), ``jen_cloud`` (síť cloudu, ostrá A v pořádku – ignorovat).

Výstup (``--out``): ``zdroje-denni-kontrola.md`` (stav, k opravě, historie) a ``zdroje-kontrola/historie.jsonl``
(jeden řádek za den, jen zdroje mimo provoz). Známé a vyřízené výjimky jsou v ``zdroje-kontrola/zname.json``
(klíč zdroje → poznámka, ``until`` volitelně); ty se hlásí jen jako „známé“.

Česká kresba ani server se nemění, skript jen čte. Spuštění z kořene repa::

    python scripts/source_daily_check.py --out /mnt/project-files/evropa-data
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("PODKLADARNA_DATA", tempfile.mkdtemp(prefix="pkd-srccheck-"))

import hashlib  # noqa: E402
import re  # noqa: E402

from app import failed_jobs, source_status  # noqa: E402

PROD_BASE = os.environ.get("SOURCE_CHECK_PROD", "https://map.ulvi.app")
WORKERS = 6  # souběh dotazů z cloudu (různé servery, jeden dotaz na zdroj)
TRVALE_DAYS = 3
OPAK_DAYS, OPAK_WINDOW = 3, 7
HISTORY_KEEP_DAYS = 60
REPORT_HISTORY_DAYS = 14
REPORT_NAME = "zdroje-denni-kontrola.md"
STATE_DIR = "zdroje-kontrola"

# Třídy, které chtějí zásah (ostatní se jen evidují v historii).
ACTION = {
    "url": "opravné vlákno (senior): najít novou adresu / službu, ověřit licenci, upravit adaptér a přehled zemí",
    "pristup": "ověřit, zda jde o licenci / klíč / účet; když je potřeba účet nebo klíč, zeptat se Petra, jinak opravné vlákno (senior)",
    "rate_limit": "opravné vlákno (junior): menší souběh, odstup a společná pauza po 429 (vzor PR #503)",
    "sit_a": "síť ostré A (DNS, firewall) nebo zdroj blokuje české IP: ověřit z Petrova PC / testu B; síť mění jen Petr",
    "cert": "certifikát zdroje (z obou stran): neobcházet, sledovat; po 3 dnech hledat zálohu (senior)",
    "trvale": "opravné vlákno (senior): zjistit příčinu (nová URL, konec služby), případně záloha nebo vypnutí země po souhlasu Petra",
    "opakovane": "opravné vlákno (junior): retry / záloha / cache, aby přechodné výpadky nerozbíjely joby",
}
LABEL = {
    "ok": "v pořádku", "prechodne": "přechodný výpadek", "planovane": "plánovaná odstávka", "rate_limit": "rate limit (429)",
    "url": "změna URL (404/410)", "pristup": "přístup / licence (401/403)", "sit_a": "blokace sítě ostré A",
    "cert": "certifikát", "trvale": "trvalý výpadek", "opakovane": "opakované výpadky", "jen_cloud": "jen síť cloudu",
    "nezname": "bez dat",
}
NET_A_MARKERS = ("Hostname mismatch", "CERTIFICATE_VERIFY_FAILED", "certificate verify failed", "Name or service",
                 "getaddrinfo", "timed out", "Timeout", "neodpovídá", "Connection refused", "unreachable")
CERT_MARKERS = ("CERTIFICATE_VERIFY_FAILED", "certificate verify failed", "Hostname mismatch", "SSLCertVerification")


def _side(r: dict | None) -> dict | None:
    """Zjednodušený výsledek jedné strany: ok, kód, popis."""
    if r is None:
        return None
    return {"ok": bool(r.get("ok")), "code": r.get("code"), "detail": r.get("detail"),
            "planned": bool(r.get("planned")), "down_since": r.get("down_since")}


def check_cloud() -> dict[str, dict]:
    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="daily-src") as pool:
        results = list(pool.map(source_status.check_probe, source_status.PROBES))
    return {r["key"]: _side(r) for r in results}


def _get_json(url: str, headers: dict | None = None) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": "podkladarna-source-daily-check", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 – ostrá A nedostupná = bez porovnání
        print(f"ostrá A: {url} – {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def check_prod() -> tuple[dict[str, dict], str | None]:
    """Stav zdrojů z ostré A (čte uložený stav; po 3 min ho server sám obnoví jedním během)."""
    codes = sorted({c for p in source_status.PROBES for c in p.countries if c != "*"})
    out: dict[str, dict] = {}
    checked_at = None
    first = True
    for code in codes:
        data = _get_json(f"{PROD_BASE}/api/source_status?country={code}")
        if first and data is None:
            return {}, None
        first = False
        if not data:
            continue
        checked_at = data.get("checked_at") or checked_at
        for svc in data.get("services") or []:
            out.setdefault(svc["key"], _side(svc))
    return out, checked_at


def prod_events() -> dict | None:
    token = os.environ.get("SOURCE_EVENTS_TOKEN")
    if not token:
        return None
    return _get_json(f"{PROD_BASE}/api/source_events?days=7", {"Authorization": f"Bearer {token}"})


# --- nepovedené buildy (Petr 2026-11-25) ------------------------------------

BUILD_HOURS = 24
BUILD_SEEN_DAYS = 30
BUILD_HISTORY = "buildy-historie.jsonl"
BUILD_ACTION = {
    "chyba": "prověřit traceback; malá oprava = opravné vlákno (junior se zadáním od seniora), jinak senior",
    "pamet": "senior: paměť / velikost výřezu (strop území, dělení na bloky)",
    "cas": "senior: časový limit jobu (pomalý krok nebo zdroj; odhad a strop výřezu)",
    "disk": "ostrá A: místo na disku (úklid cache, viz app/disk_space.py); mazání jen se souhlasem Petra",
}
BUILD_LABEL = {
    "chyba": "chyba v kódu / datech", "pamet": "paměť", "cas": "časový limit", "disk": "disk",
    "vypadek_zdroje": "výpadek zdroje (řeší kontrola zdrojů)", "vlastni_data": "vlastní data uživatele",
    "restart": "restart serveru",
}
_SIG_NUM = re.compile(r"\d+")
_SIG_HEX = re.compile(r"\b[0-9a-f]{10,}\b")


def fetch_failed_jobs() -> tuple[list[dict], str]:
    """Selhané joby za 24 h: s tokenem všechny (i privátní), jinak jen veřejné joby domény ``PROD_BASE``."""
    token = os.environ.get("SOURCE_EVENTS_TOKEN")
    if token:
        data = _get_json(f"{PROD_BASE}/api/ops/failed_jobs?hours={BUILD_HOURS}", {"Authorization": f"Bearer {token}"})
        if data is not None:
            return data.get("jobs") or [], "všechny joby ostré A (token)"
    data = _get_json(f"{PROD_BASE}/api/jobs")
    if data is None:
        return [], "nedostupné"
    since = datetime.now(timezone.utc) - timedelta(hours=BUILD_HOURS)
    out = []
    for job in data.get("jobs") or []:
        if job.get("status") not in failed_jobs.FAILED_STATUSES:
            continue
        when = failed_jobs._parse(job.get("updated_at") or job.get("created_at"))
        if when is None or when < since:
            continue
        log = _get_json(f"{PROD_BASE}/api/jobs/{job['id']}/log") or {}
        lines = [str(x.get("src") or x.get("line") or "") for x in log.get("lines") or []]
        picked, tb = failed_jobs.extract(lines, str(job.get("name") or ""))
        opts = job.get("options") or {}
        out.append({"id": job["id"], "status": job.get("status"), "country": opts.get("country") or "CZ",
                    "preset_id": job.get("preset_id"), "source_outage": opts.get("source_outage"),
                    "user_sources": bool(opts.get("user_sources")), "sample": bool(opts.get("sample")),
                    "area_km2": job.get("area_km2"), "error": str(job.get("error") or "")[:600],
                    "log": picked, "traceback": tb})
    return out, "jen veřejné joby Omapicu (bez tokenu nejsou vidět privátní joby ani Podkladárna)"


def build_signature(job: dict) -> tuple[str, str]:
    """(kategorie, podpis příčiny): poslední řádek tracebacku, jinak první chybový řádek logu, jinak hláška."""
    tb = (job.get("traceback") or "").strip()
    sig = tb.splitlines()[-1] if tb else ""
    if not sig:
        sig = next((x for x in job.get("log") or [] if x.startswith(("CHYBA", "ERROR"))), "") or job.get("error") or "?"
    sig = _SIG_NUM.sub("#", _SIG_HEX.sub("‹id›", sig.strip()))[:220]
    text = " ".join([sig, job.get("error") or "", *(job.get("log") or [])[-5:]]).lower()
    if job.get("source_outage"):
        cat = "vypadek_zdroje"
    elif job.get("user_sources"):
        cat = "vlastni_data"
    elif "memoryerror" in text or "killed" in text or "paměť" in text or "paměti" in text:
        cat = "pamet"
    elif "no space left" in text or "místo na disku" in text:
        cat = "disk"
    elif "timeout" in text and "časový limit" in text or "překročil časový limit" in text:
        cat = "cas"
    elif "restart" in text and not tb:
        cat = "restart"
    else:
        cat = "chyba"
    return cat, sig


def group_builds(jobs: list[dict], history: list[dict]) -> list[dict]:
    seen: dict[str, int] = {}
    for day in history[-BUILD_SEEN_DAYS:]:
        for gid in (day.get("groups") or {}):
            seen[gid] = seen.get(gid, 0) + 1
    groups: dict[str, dict] = {}
    for job in jobs:
        cat, sig = build_signature(job)
        if cat == "chyba" and job.get("status") == "done_with_errors" and not job.get("traceback") \
                and not any(x.startswith(("CHYBA", "ERROR")) for x in job.get("log") or []):
            continue  # hotová mapa jen s varováním
        gid = "b-" + hashlib.sha1(f"{cat}|{sig}".encode("utf-8")).hexdigest()[:8]
        g = groups.setdefault(gid, {"id": gid, "cat": cat, "sig": sig, "jobs": [], "countries": set(),
                                    "presets": set(), "days_seen": seen.get(gid, 0), "example": job})
        g["jobs"].append(job["id"])
        g["countries"].add(job.get("country") or "?")
        g["presets"].add(job.get("preset_id") or "?")
    out = []
    for g in groups.values():
        g["countries"], g["presets"] = sorted(g["countries"]), sorted(g["presets"])
        g["new"] = g["days_seen"] == 0
        out.append(g)
    return sorted(out, key=lambda g: (g["cat"] not in BUILD_ACTION, -len(g["jobs"]), g["id"]))


def _has(text: str | None, markers) -> bool:
    return bool(text) and any(m in text for m in markers)


def classify_today(c: dict | None, a: dict | None) -> str:
    """Třída za dnešek jen z dnešních výsledků (historie se přidá v ``classify``)."""
    if a is None and c is None:
        return "nezname"
    if (a is None or a["ok"]) and (c is None or c["ok"]):
        return "ok"
    codes = {x["code"] for x in (a, c) if x and not x["ok"]}
    if 429 in codes:
        return "rate_limit"
    if a is not None and a["ok"]:
        return "jen_cloud"  # síť cloudu (proxy, americká IP, chybějící klíč); produkci to netrápí
    # Ostrá A mimo provoz (nebo bez dat z A a cloud mimo provoz).
    if any(x and not x["ok"] and x["planned"] for x in (a, c)):
        return "planovane"
    if c is not None and c["ok"]:
        return "sit_a"
    detail = " ".join(str(x["detail"]) for x in (a, c) if x and x.get("detail"))
    if codes & {404, 410}:
        return "url"
    if codes & {401, 403}:
        return "pristup"
    if _has(detail, CERT_MARKERS):
        return "cert"
    return "prechodne"


def classify(today: str, key: str, history: list[dict]) -> tuple[str, int]:
    """Doplní trvalé / opakované podle historie; vrací (třída, dní mimo provoz za sebou včetně dneška)."""
    if today in ("ok", "jen_cloud", "nezname"):
        return today, 0
    streak = 1
    for day in reversed(history):
        if key in day.get("down", {}):
            streak += 1
        else:
            break
    recent = sum(1 for day in history[-(OPAK_WINDOW - 1):] if key in day.get("down", {})) + 1
    if today in ("prechodne", "planovane", "cert") and streak >= TRVALE_DAYS:
        return "trvale", streak
    if today == "prechodne" and recent >= OPAK_DAYS:
        return "opakovane", streak
    return today, streak


def _load_history(path: Path) -> list[dict]:
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return rows


def _load_known(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    today = date.today().isoformat()
    return {k: v for k, v in data.items() if not k.startswith("_") and (not v.get("until") or v["until"] >= today)}


def _fmt_side(x: dict | None) -> str:
    if x is None:
        return "–"
    if x["ok"]:
        return "běží"
    return str(x.get("detail") or (f"HTTP {x['code']}" if x.get("code") else "mimo provoz"))[:80]


def run(out_dir: Path, dry: bool = False) -> dict:
    state_dir = out_dir / STATE_DIR
    hist_path = state_dir / "historie.jsonl"
    today = date.today().isoformat()
    history = [h for h in _load_history(hist_path) if h.get("date") != today]
    known = _load_known(state_dir / "zname.json")

    cloud = check_cloud()
    prod, prod_checked = check_prod()
    events = prod_events()
    build_hist_path = state_dir / BUILD_HISTORY
    build_history = [h for h in _load_history(build_hist_path) if h.get("date") != today]
    bjobs, bsource = fetch_failed_jobs()
    bgroups = group_builds(bjobs, build_history)
    for g in bgroups:
        g["known"] = known.get(g["id"])
    baction = [g for g in bgroups if g["cat"] in BUILD_ACTION and not g["known"]]
    probes = {p.key: p for p in source_status.PROBES}

    rows = []
    for key, p in probes.items():
        c, a = cloud.get(key), prod.get(key)
        cls, streak = classify(classify_today(c, a), key, history)
        rows.append({"key": key, "label": p.label, "countries": list(p.countries), "cls": cls, "streak": streak,
                     "cloud": c, "prod": a, "fatal": p.fatal, "known": known.get(key)})

    down_today = {r["key"]: {"cls": r["cls"], "cloud": _fmt_side(r["cloud"]), "prod": _fmt_side(r["prod"])}
                  for r in rows if r["cls"] not in ("ok", "jen_cloud", "nezname")}
    entry = {"date": today, "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
             "prod_checked_at": prod_checked, "prod_ok": bool(prod), "down": down_today}

    prev_action = {k for k, v in (history[-1]["down"] if history else {}).items() if v.get("cls") in ACTION}
    action = [r for r in rows if r["cls"] in ACTION and not r["known"]]
    new_action = [r for r in action if r["key"] not in prev_action]
    entry["action"] = [r["key"] for r in action]
    entry["new_action"] = [r["key"] for r in new_action]

    history.append(entry)
    cutoff = (date.today() - timedelta(days=HISTORY_KEEP_DAYS)).isoformat()
    history = [h for h in history if h.get("date", "") >= cutoff]
    build_history.append({"date": today, "source": bsource, "jobs": len(bjobs), "groups": {
        g["id"]: {"cat": g["cat"], "sig": g["sig"], "jobs": g["jobs"], "countries": g["countries"]} for g in bgroups}})
    build_history = [h for h in build_history if h.get("date", "") >= cutoff]
    report = build_report(entry, rows, history, events, known)
    report = report.replace("## Postup pro koordinátora", build_section(bgroups, bjobs, bsource) + "\n## Postup pro koordinátora", 1)
    if not dry:
        state_dir.mkdir(parents=True, exist_ok=True)
        hist_path.write_text("".join(json.dumps(h, ensure_ascii=False) + "\n" for h in history), encoding="utf-8")
        build_hist_path.write_text("".join(json.dumps(h, ensure_ascii=False) + "\n" for h in build_history), encoding="utf-8")
        (out_dir / REPORT_NAME).write_text(report, encoding="utf-8")
    return {"date": today, "prod_ok": bool(prod), "action": entry["action"], "new_action": entry["new_action"],
            "known_down": [r["key"] for r in rows if r["known"] and r["cls"] not in ("ok", "jen_cloud", "nezname")],
            "build_jobs": len(bjobs), "build_source": bsource,
            "build_action": [g["id"] for g in baction], "build_new": [g["id"] for g in baction if g["new"]],
            "report": report if dry else str(out_dir / REPORT_NAME)}


def _countries(r: dict) -> str:
    return ", ".join(r["countries"])


def build_section(groups: list[dict], jobs: list[dict], source: str) -> str:
    L = [f"## Nepovedené buildy ({BUILD_HOURS} h)", "", f"Zdroj: {source}; selhaných jobů {len(jobs)}.", ""]
    action = [g for g in groups if g["cat"] in BUILD_ACTION and not g.get("known")]
    other = [g for g in groups if g not in action]
    if not action:
        L += ["K opravě nic.", ""]
    else:
        L += ["| Skupina | Příčina (podpis) | Kategorie | Jobů | Země | Joby | Co udělat |", "|---|---|---|---|---|---|---|"]
        for g in action:
            new = " 🆕" if g["new"] else f" (viděno {g['days_seen']} d)"
            sig = g["sig"].replace("|", "/")
            L.append(f"| `{g['id']}`{new} | {sig} | {BUILD_LABEL[g['cat']]} | {len(g['jobs'])} | "
                     f"{', '.join(g['countries'])} | {', '.join(g['jobs'][:3])} | {BUILD_ACTION[g['cat']]} |")
        L.append("")
        L.append("Detail jobu: `GET /api/jobs/<id>/log` na ostré A (privátní joby jen s tokenem přes `/api/ops/failed_jobs`).")
        L.append("")
    if other:
        L.append("Bez zásahu: " + "; ".join(
            f"{BUILD_LABEL.get(g['cat'], g['cat'])} {len(g['jobs'])}×" + (" (známé)" if g.get("known") else "") for g in other))
        L.append("")
    return "\n".join(L)


def build_report(entry: dict, rows: list[dict], history: list[dict], events: dict | None, known: dict) -> str:
    action = [r for r in rows if r["cls"] in ACTION and not r["known"]]
    known_down = [r for r in rows if r["known"] and r["cls"] not in ("ok", "jen_cloud", "nezname")]
    passing = [r for r in rows if r["cls"] in ("prechodne", "planovane")]
    L = [
        "# Denní kontrola zdrojů dat",
        "",
        f"Poslední kontrola: {entry['checked_at']} (cloud) · ostrá A: "
        + (f"stav z {entry['prod_checked_at']}" if entry["prod_ok"] else "**nedostupná, bez porovnání**")
        + f" · zdrojů {len(rows)}",
        "",
        "Generuje `scripts/source_daily_check.py` (naplánovaná úloha v cloudu, denně). Soubor se celý přepisuje,",
        "ručně upravuj jen `zdroje-kontrola/zname.json` (vyřízené výjimky). Postup pro koordinátora je na konci.",
        "",
        "## K opravě",
        "",
    ]
    if not action:
        L += ["Nic. Všechny zdroje běží nebo jde jen o přechodný či známý výpadek.", ""]
    else:
        L += ["| Zdroj | Země | Třída | Dní za sebou | Cloud | Ostrá A | Co udělat |", "|---|---|---|---|---|---|---|"]
        for r in sorted(action, key=lambda r: (-r["fatal"], -r["streak"], r["key"])):
            new = " 🆕" if r["key"] in entry["new_action"] else ""
            L.append(f"| {r['label']} (`{r['key']}`){new} | {_countries(r)} | {LABEL[r['cls']]} | {r['streak']} | "
                     f"{_fmt_side(r['cloud'])} | {_fmt_side(r['prod'])} | {ACTION[r['cls']]} |")
        L.append("")
    if known_down:
        L += ["## Známé (nehlásit znovu)", ""]
        for r in known_down:
            L.append(f"- `{r['key']}` {r['label']}: {LABEL[r['cls']]}, {r['streak']} d – {r['known'].get('note', '')}")
        L.append("")
    if passing:
        L += ["## Dnes přechodně mimo provoz (nic nedělat)", ""]
        for r in passing:
            L.append(f"- `{r['key']}` ({_countries(r)}): {_fmt_side(r['prod']) if r['prod'] else _fmt_side(r['cloud'])}")
        L.append("")
    if events and events.get("summary"):
        hit = [g for g in events["summary"] if g.get("rejected") or g.get("job_failed")]
        if hit:
            L += ["## Dopad na joby ostré A (7 dní)", ""]
            for g in hit:
                L.append(f"- `{g['key']}`: odmítnuto {g.get('rejected', 0)}, selhalo {g.get('job_failed', 0)}")
            L.append("")
    L += [f"## Historie výpadků ({REPORT_HISTORY_DAYS} dní)", ""]
    recent = history[-REPORT_HISTORY_DAYS:]
    counts: dict[str, list[str]] = {}
    for day in recent:
        for key, v in day.get("down", {}).items():
            counts.setdefault(key, []).append(f"{day['date'][5:]} {v.get('cls')}")
    if not counts:
        L += ["Bez výpadků.", ""]
    else:
        for key, days in sorted(counts.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            L.append(f"- `{key}` {len(days)}×: " + ", ".join(days[-6:]))
        L.append("")
    L += [
        "## Postup pro koordinátora",
        "",
        "- Při startu přečti sekci „K opravě“. Pro každý řádek bez otevřeného vlákna založ opravné vlákno",
        "  (junior / senior podle sloupce „Co udělat“), do zadání dej klíč zdroje, třídu, výstup cloud vs. ostrá A",
        "  a odkaz na tento soubor. Po opravě nebo rozhodnutí přidej zdroj do `zdroje-kontrola/zname.json`",
        "  s poznámkou (a `until`, když jde o dočasnou výjimku).",
        "- Totéž pro „Nepovedené buildy“: u skupiny 🆕 nebo opakované navrhni příčinu z tracebacku a kódu; malou opravu",
        "  dej opravnému vláknu, vyřízenou skupinu (`b-…`) zapiš do `zname.json`. Uživatelské chyby, limity a výpadky",
        "  zdrojů se tu neřeší.",
        "- Petra oslov jen u věcí, které vyžadují jeho účet, e-mail, klíč, síť ostré A nebo rozhodnutí (vypnutí země).",
        "- Přechodné, plánované a „jen síť cloudu“ nehlásit. Trvalou změnu (nová URL, licence, konec služby) zapiš",
        "  do `evropa-data-prehled.md` ve stejném kroku jako opravu.",
        "",
        "Třídy: přechodný = 5xx / timeout méně než 3 dny; trvalý = 3 dny za sebou; opakované = 3 dny ze 7;",
        "blokace sítě ostré A = z cloudu běží, z ostré A ne; jen síť cloudu = z ostré A běží (ignorovat).",
        "",
    ]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=Path("/mnt/project-files/evropa-data"))
    ap.add_argument("--dry-run", action="store_true", help="nic nezapisovat, report na stdout")
    args = ap.parse_args()
    res = run(args.out, dry=args.dry_run)
    if args.dry_run:
        print(res.pop("report"))
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
