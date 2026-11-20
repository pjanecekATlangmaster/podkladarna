"""Interní statistika původu jobů (tabulka job_origin) a souhrn použití (usage_totals) – jen pro čtení.

Na NAS (ostrá A; test B = kontejner podkladarna-test):
    sudo docker exec podkladarna python -m app.origin_stats
    sudo docker exec podkladarna python -m app.origin_stats 2026-11-01 2026-11-05
Bez data = dnešek (UTC); jedno datum = ten den; dvě data = období včetně obou dnů.
Soukromé joby podle e-mailu (limit MAX_PRIVATE_JOBS_PER_EMAIL_DAY, zákaz BANNED_EMAILS):
    docker exec podkladarna python -m app.origin_stats --emails            # nejčastější značky e-mailů
    docker exec podkladarna python -m app.origin_stats --email <adresa>    # počty a joby jednoho e-mailu
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
    if args[:1] == ["--emails"]:
        _email_top(int(args[1]) if len(args) > 1 else 20)
    elif args[:1] == ["--email"] and len(args) > 1:
        _email_detail(args[1])
    else:
        _report(*args[:2])


_PRIVATE_WEB = "o.private = 1 AND o.api_client = 0 AND o.email_tag IS NOT NULL"


def _kind(r) -> str:
    """Druh jobu pro výpis; API s názvem klienta (starší řádky bez sloupce / bez názvu = „default“)."""
    if r["api_client"]:
        name = r["api_client_name"] if "api_client_name" in r.keys() else None
        return f"API {name or 'default'}"
    return "privátní" if r["private"] else "veřejný"


def _ro() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _since(hours: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).isoformat()


def _email_top(limit: int = 20) -> None:
    """Značky e-mailů s nejvíce soukromými joby (24 h, 30 dní, celkem v job_origin) – bez čitelných adres."""
    con = _ro()
    rows = con.execute(
        f"""
        SELECT o.email_tag AS tag,
               SUM(o.created_at >= ?) AS d1, SUM(o.created_at >= ?) AS d30, COUNT(*) AS total,
               COUNT(DISTINCT o.ip_tag) AS ips, MAX(o.created_at) AS last
        FROM job_origin o WHERE {_PRIVATE_WEB}
        GROUP BY o.email_tag ORDER BY d1 DESC, d30 DESC, total DESC LIMIT ?
        """,
        (_since(24), _since(24 * 30), limit),
    ).fetchall()
    print("Soukromé joby podle e-mailu (značka = HMAC, stejná značka = stejný e-mail)")
    print("značka        | 24 h | 30 dní | celkem | různých IP | poslední (UTC)")
    for r in rows:
        print(f"{r['tag']:<13} | {r['d1']:>4} | {r['d30']:>6} | {r['total']:>6} | {r['ips']:>10} | "
              f"{(r['last'] or '')[:16].replace('T', ' ')}")


def _email_detail(email: str) -> None:
    """Počty a seznam soukromých jobů jednoho e-mailu (i starší řádky se značkou z nenormalizované adresy)."""
    from app import db
    from app.rate_limit import is_banned_email

    tags = sorted({t for t in (db.email_limit_tag(email), db._origin_tag("email", email)) if t})
    if not tags:
        print("Neplatný e-mail.")
        return
    con = _ro()
    marks = ",".join("?" * len(tags))
    rows = con.execute(
        f"""
        SELECT o.*, j.name AS name, j.status AS live_status FROM job_origin o LEFT JOIN jobs j ON j.id = o.job_id
        WHERE {_PRIVATE_WEB} AND o.email_tag IN ({marks}) ORDER BY o.created_at
        """,
        tags,
    ).fetchall()
    d1 = sum(1 for r in rows if r["created_at"] >= _since(24))
    d30 = sum(1 for r in rows if r["created_at"] >= _since(24 * 30))
    print(f"Značky: {', '.join(tags)}; zablokovaný (BANNED_EMAILS): {'ano' if is_banned_email(email) else 'ne'}")
    print(f"Soukromé joby: 24 h {d1}, 30 dní {d30}, celkem {len(rows)}, různých IP {len({r['ip_tag'] for r in rows})}")
    for r in rows:
        print("  " + " | ".join([
            r["created_at"][:16].replace("T", " "),
            r["job_id"][:8],
            r["live_status"] or r["status"] or "?",
            r["preset_id"] or "-",
            r["map_types"] or "-",
            "IP " + (r["ip_tag"] or "?"),
            (r["name"] or "")[:30],
        ]))


def _report(d_from: str | None = None, d_to: str | None = None) -> None:
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    d_from = d_from or today
    d_to = d_to or d_from

    con = _ro()
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
        _kind(r) for r in rows
    )
    print("Podle druhu: " + ", ".join(f"{k} {v}" for k, v in kinds.most_common()))
    if rows and "site" in rows[0].keys():
        sites = collections.Counter(
            f"{r['site'] or 'původní doména'} ({r['lang'] or 'cs'})" for r in rows
        )
        print("Podle domény: " + ", ".join(f"{k} {v}" for k, v in sites.most_common()))
    if rows and "country" in rows[0].keys():
        by_country = collections.Counter((r["country"] or "CZ") for r in rows)
        print("Podle země: " + ", ".join(f"{k} {v}" for k, v in by_country.most_common()))
    print(f"Různých IP: {len({r['ip_tag'] for r in rows})}, "
          f"sítí (/24): {len({r['ip_net_tag'] for r in rows})}, "
          f"e-mailů (privátní): {len({r['email_tag'] for r in rows if r['email_tag']})}")

    by_ip = collections.defaultdict(list)
    for r in rows:
        by_ip[(r["ip_tag"] or "?", r["ip_net_tag"] or "?")].append(r)
    for (ip, net), js in sorted(by_ip.items(), key=lambda kv: -len(kv[1])):
        print(f"\nIP {ip} (síť {net}): {len(js)} jobů")
        for r in js:
            kind = _kind(r)
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
