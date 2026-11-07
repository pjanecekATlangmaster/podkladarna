"""Interní statistika původu jobů (tabulka job_origin) a souhrn použití (usage_totals) – jen pro čtení.

Na NAS (ostrá A; test B = kontejner podkladarna-test):
    sudo docker exec podkladarna python -m app.origin_stats
    sudo docker exec podkladarna python -m app.origin_stats 2026-11-01 2026-11-05
Bez data = dnešek (UTC); jedno datum = ten den; dvě data = období včetně obou dnů.
Joby ještě nesmazané úklidem doplní stav a název z tabulky jobs.
IP, síť (/24) a e-mail jsou jen neprůhledné značky: stejná značka = stejný zdroj.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import sqlite3
import sys

from app.settings import DB_PATH


def main(args: list[str]) -> None:
    _report(*args[:2])


def _report(d_from: str | None = None, d_to: str | None = None) -> None:
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    d_from = d_from or today
    d_to = d_to or d_from

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT o.*, j.name AS name, j.status AS live_status
        FROM job_origin o LEFT JOIN jobs j ON j.id = o.job_id
        WHERE o.created_at >= ? AND o.created_at < date(?, '+1 day')
        ORDER BY o.created_at
        """,
        (d_from, d_to),
    ).fetchall()

    totals = {
        r["key"]: r for r in con.execute("SELECT key, value, since FROM usage_totals")
    } if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'usage_totals'"
    ).fetchone() else {}
    if totals:
        print("Celkem (patička webu): " + ", ".join(
            f"{k} {totals[k]['value']:.0f} (od {totals[k]['since'][:10]})" for k in sorted(totals)
        ))

    print(f"Joby {d_from} – {d_to} (UTC): {len(rows)}")
    kinds = collections.Counter(
        "API" if r["api_client"] else ("privátní" if r["private"] else "veřejný") for r in rows
    )
    print("Podle druhu: " + ", ".join(f"{k} {v}" for k, v in kinds.most_common()))
    print(f"Různých IP: {len({r['ip_tag'] for r in rows})}, "
          f"sítí (/24): {len({r['ip_net_tag'] for r in rows})}, "
          f"e-mailů (privátní): {len({r['email_tag'] for r in rows if r['email_tag']})}")

    by_ip = collections.defaultdict(list)
    for r in rows:
        by_ip[(r["ip_tag"] or "?", r["ip_net_tag"] or "?")].append(r)
    for (ip, net), js in sorted(by_ip.items(), key=lambda kv: -len(kv[1])):
        print(f"\nIP {ip} (síť {net}): {len(js)} jobů")
        for r in js:
            kind = "API" if r["api_client"] else ("privátní" if r["private"] else "veřejný")
            b = json.loads(r["bbox_json"]) if r["bbox_json"] else None
            center = f"{(b[1] + b[3]) / 2:.3f},{(b[0] + b[2]) / 2:.3f}" if b else "-"
            print("  " + " | ".join([
                r["created_at"][:16].replace("T", " "),
                r["job_id"][:8],
                r["live_status"] or r["status"] or "?",
                r["preset_id"] or "-",
                r["map_types"] or "-",
                kind,
                ("e-mail " + r["email_tag"]) if r["email_tag"] else "-",
                center,
                (r["name"] or "")[:30],
            ]))


if __name__ == "__main__":
    main(sys.argv[1:])
