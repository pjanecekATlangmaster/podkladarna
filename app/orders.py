"""Objednávky map na doméně v režimu ``paid`` (bez účtů, identita = ověřený e-mail).

Tok (návrh ``platby-navrh.md`` kap. 5): zákazník vyplní formulář s e-mailem → objednávka
``awaiting_confirmation`` (job ještě neexistuje, zadání je uložené v objednávce) → klik na odkaz
v e-mailu → ``confirmed`` (vznikne a zařadí se job) → job doběhne → ``preview_ready`` (náhled,
ukázka a odkaz na nákup) → platba u poskytovatele → ``paid`` (odkaz ke stažení plné mapy).
Selhání jobu → ``failed``; propadlá nezaplacená → ``expired``; vrácení peněz → ``refunded``.

Tokeny (potvrzovací, náhledový, stahovací) jsou náhodné a v databázi jen jako SHA-256; čitelný
token existuje jen v odkazu v e-mailu. Náhledový a stahovací token se vydávají (rotují) ve chvíli,
kdy se posílá příslušný e-mail. E-mail se drží čitelně jen po dobu platnosti odkazů, pak zůstane
jen HMAC značka (stejná sůl jako ``job_origin``).

Schéma: nové tabulky ``orders`` a ``payment_events`` (aditivní ``CREATE TABLE IF NOT EXISTS``;
starší image je ignoruje, rollback bezpečný).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import logging

from app import db
from app import settings as app_settings

logger = logging.getLogger("podkladarna.orders")

STATUS_AWAITING = "awaiting_confirmation"
STATUS_CONFIRMING = "confirming"  # krátký mezistav při potvrzení (brání dvojímu založení jobu)
STATUS_CONFIRMED = "confirmed"
STATUS_PREVIEW = "preview_ready"
STATUS_PAID = "paid"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"
STATUS_REFUNDED = "refunded"

# Rozpracované nezaplacené objednávky (limit na e-mail).
OPEN_UNPAID = (STATUS_AWAITING, STATUS_CONFIRMING, STATUS_CONFIRMED, STATUS_PREVIEW)

TOKEN_KINDS = ("confirm", "preview", "download")
# Zpožděná platba k objednávce s rozběhnutým checkoutem se ještě přijme (hodiny po konci náhledu).
PAYMENT_GRACE_HOURS = 24
# Mezistav „potvrzuje se“ trvá jen po dobu založení jobu; starší = pád procesu → vrátit k potvrzení.
CONFIRMING_STALE_MINUTES = 15
# Platba, kterou nejde doručit (jiná částka, propadlá objednávka, druhá platba): zůstane jen záznam
# v payment_events s tímto druhem a varování v logu – peníze vrátit ručně u poskytovatele.
REFUND_KINDS = {
    "amount": "paid:refund_amount_mismatch",
    "expired": "paid:refund_order_expired",
    "duplicate": "paid:refund_duplicate",
}


class OrderError(Exception):
    """Objednávku nejde založit / potvrdit (zpráva je pro log, ne pro uživatele)."""


class OrderLimitError(OrderError):
    """E-mail už má rozpracovanou nezaplacenou objednávku."""


def init_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            site TEXT,
            lang TEXT,
            email TEXT,
            email_tag TEXT,
            job_id TEXT,
            job_request_json TEXT,
            price_cents INTEGER NOT NULL,
            currency TEXT NOT NULL,
            provider TEXT,
            provider_ref TEXT,
            confirm_hash TEXT,
            confirm_expires_at TEXT,
            confirmed_at TEXT,
            preview_hash TEXT,
            preview_expires_at TEXT,
            download_hash TEXT,
            download_expires_at TEXT,
            paid_at TEXT
        )
        """
    )
    for col in ("confirm_hash", "preview_hash", "download_hash"):
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_{col} ON orders({col}) "
            f"WHERE {col} IS NOT NULL"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_email_tag ON orders(email_tag)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_job ON orders(job_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payment_events (
            event_id TEXT PRIMARY KEY,
            provider TEXT,
            order_id TEXT,
            kind TEXT,
            received_at TEXT NOT NULL
        )
        """
    )


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def normalize_email(email: str) -> str:
    return (email or "").strip()


def email_tag(email: str) -> str | None:
    return db._origin_tag("email", normalize_email(email))


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("confirm_hash", "preview_hash", "download_hash"):
        out.pop(key, None)
    raw = out.pop("job_request_json", None)
    try:
        out["job_request"] = json.loads(raw) if raw else None
    except ValueError:
        out["job_request"] = None
    return out


def get_order(order_id: str) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    order = _row(row)
    if order is None:
        raise KeyError(order_id)
    return order


def order_for_job(job_id: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE job_id = ? ORDER BY created_at DESC LIMIT 1", (job_id,)
        ).fetchone()
    return _row(row)


def count_open_unpaid(email: str, *, now: datetime | None = None) -> int:
    """Rozpracované nezaplacené objednávky e-mailu (propadlé potvrzení a náhled se nepočítají)."""
    tag = email_tag(email)
    if not tag:
        return 0
    now_s = _iso(_now(now))
    marks = ",".join("?" for _ in OPEN_UNPAID)
    with db.connect() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) FROM orders
            WHERE email_tag = ? AND status IN ({marks})
              AND NOT (status = ? AND COALESCE(confirm_expires_at, '') <= ?)
              AND NOT (status = ? AND COALESCE(preview_expires_at, '') <= ?)
            """,
            (tag, *OPEN_UNPAID, STATUS_AWAITING, now_s, STATUS_PREVIEW, now_s),
        ).fetchone()
    return int(row[0]) if row else 0


def create_order(
    *,
    email: str,
    job_request: dict[str, Any],
    site: str | None = None,
    lang: str | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], str]:
    """Založí objednávku čekající na potvrzení e-mailu; vrací (objednávka, potvrzovací token)."""
    email = normalize_email(email)
    if not email or "@" not in email:
        raise OrderError("chybí e-mail")
    limit = app_settings.MAX_UNPAID_ORDERS_PER_EMAIL
    if limit > 0 and count_open_unpaid(email, now=now) >= limit:
        raise OrderLimitError("e-mail už má rozpracovanou nezaplacenou objednávku")
    current = _now(now)
    token = _new_token()
    order_id = uuid.uuid4().hex[:16]
    hours = app_settings.ORDER_CONFIRM_HOURS if app_settings.ORDER_CONFIRM_HOURS > 0 else 24
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO orders (
                id, status, created_at, updated_at, site, lang, email, email_tag,
                job_request_json, price_cents, currency, confirm_hash, confirm_expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                STATUS_AWAITING,
                _iso(current),
                _iso(current),
                (site or "")[:253] or None,
                (lang or "")[:8] or None,
                email,
                email_tag(email),
                json.dumps(job_request),
                int(app_settings.PAID_PRICE_CENTS),
                app_settings.PAID_CURRENCY,
                _hash(token),
                _iso(current + timedelta(hours=hours)),
            ),
        )
        conn.commit()
    return get_order(order_id), token


def _find_by_token(conn: sqlite3.Connection, kind: str, token: str) -> sqlite3.Row | None:
    if kind not in TOKEN_KINDS:
        raise ValueError(kind)
    token = (token or "").strip()
    if not token:
        return None
    return conn.execute(
        f"SELECT * FROM orders WHERE {kind}_hash = ?", (_hash(token),)
    ).fetchone()


def get_order_by_token(kind: str, token: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Objednávka podle platného tokenu daného druhu; propadlý / neznámý token → None."""
    with db.connect() as conn:
        row = _find_by_token(conn, kind, token)
    order = _row(row)
    if order is None:
        return None
    expires = _parse(order.get(f"{kind}_expires_at"))
    if expires is not None and _now(now) >= expires:
        return None
    if kind == "download" and order["status"] != STATUS_PAID:
        return None
    return order


def claim_confirmation(token: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Atomicky převezme potvrzení (dvojí klik nezaloží dva joby); None = neplatný / použitý token."""
    current = _iso(_now(now))
    with db.connect() as conn:
        row = _find_by_token(conn, "confirm", token)
        if row is None:
            return None
        cur = conn.execute(
            """
            UPDATE orders SET status = ?, updated_at = ?
            WHERE id = ? AND status = ? AND confirm_expires_at > ?
            """,
            (STATUS_CONFIRMING, current, row["id"], STATUS_AWAITING, current),
        )
        conn.commit()
        if cur.rowcount != 1:
            return None
    return get_order(row["id"])


def attach_job(order_id: str, job_id: str, *, now: datetime | None = None) -> None:
    """Potvrzená objednávka dostala job; potvrzovací token už nejde použít."""
    current = _iso(_now(now))
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE orders SET status = ?, job_id = ?, confirmed_at = ?, updated_at = ?,
                confirm_hash = NULL, job_request_json = NULL
            WHERE id = ? AND status = ?
            """,
            (STATUS_CONFIRMED, job_id, current, current, order_id, STATUS_CONFIRMING),
        )
        conn.commit()


def release_confirmation(order_id: str, *, now: datetime | None = None) -> None:
    """Založení jobu po potvrzení selhalo – vrátit objednávku, ať jde odkaz použít znovu."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (STATUS_AWAITING, _iso(_now(now)), order_id, STATUS_CONFIRMING),
        )
        conn.commit()


def cancel_unconfirmed(order_id: str, *, now: datetime | None = None) -> None:
    """Potvrzovací e-mail nešel odeslat – objednávku zrušit, ať neblokuje limit na e-mail."""
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE orders SET status = ?, updated_at = ?, email = NULL, job_request_json = NULL,
                confirm_hash = NULL
            WHERE id = ? AND status = ?
            """,
            (STATUS_EXPIRED, _iso(_now(now)), order_id, STATUS_AWAITING),
        )
        conn.commit()


def issue_preview_token(order_id: str, *, now: datetime | None = None) -> str | None:
    """Job doběhl: vydá náhledový token (náhled, ukázka, nákup); None = objednávka v jiném stavu."""
    current = _now(now)
    hours = app_settings.UNPAID_ORDER_RETENTION_HOURS
    if hours <= 0:
        hours = 168
    token = _new_token()
    with db.connect() as conn:
        cur = conn.execute(
            """
            UPDATE orders SET status = ?, preview_hash = ?, preview_expires_at = ?, updated_at = ?
            WHERE id = ? AND status IN (?, ?)
            """,
            (
                STATUS_PREVIEW,
                _hash(token),
                _iso(current + timedelta(hours=hours)),
                _iso(current),
                order_id,
                STATUS_CONFIRMED,
                STATUS_PREVIEW,
            ),
        )
        conn.commit()
    return token if cur.rowcount == 1 else None


def mark_failed(order_id: str, *, now: datetime | None = None) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE id = ? AND status IN (?, ?)",
            (STATUS_FAILED, _iso(_now(now)), order_id, STATUS_CONFIRMED, STATUS_CONFIRMING),
        )
        conn.commit()


def set_checkout(order_id: str, provider: str, provider_ref: str, *, now: datetime | None = None) -> None:
    """Zapamatuje si založenou platbu u poskytovatele (poslední checkout objednávky)."""
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE orders SET provider = ?, provider_ref = ?, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            (provider, provider_ref, _iso(_now(now)), order_id, STATUS_PREVIEW),
        )
        conn.commit()


def apply_payment_event(
    event: Any, provider: str, *, now: datetime | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    """Zpracuje ověřenou událost poskytovatele (idempotentně podle ``event_id``).

    Vrací (objednávka, nový stahovací token). Token jen při prvním zaplacení – podle něj se pošle
    e-mail s plnou mapou; opakovaná / neznámá událost vrátí token None.
    """
    current = _now(now)
    days = app_settings.PAID_DOWNLOAD_DAYS if app_settings.PAID_DOWNLOAD_DAYS > 0 else 30
    token: str | None = None
    with db.connect() as conn:
        try:
            conn.execute(
                "INSERT INTO payment_events (event_id, provider, order_id, kind, received_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"{provider}:{event.event_id}", provider, event.order_id, event.kind, _iso(current)),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (event.order_id,)).fetchone()
            return _row(row), None
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (event.order_id,)).fetchone()
        if row is None or (row["provider"] and row["provider"] != provider):
            conn.commit()
            return None, None
        refund: str | None = None
        if event.kind == "paid":
            amount = getattr(event, "amount_cents", None)
            currency = str(getattr(event, "currency", None) or "").upper()
            if (amount is not None and int(amount) != int(row["price_cents"])) or (
                currency and currency != str(row["currency"] or "").upper()
            ):
                refund = "amount"
            elif row["status"] == STATUS_EXPIRED:
                # Po lhůtě (i s rozběhnutou platbou) už e-mail ani job nemusí existovat.
                refund = "expired"
            elif row["status"] in (STATUS_PAID, STATUS_REFUNDED):
                refund = "duplicate"
            if refund:
                conn.execute(
                    "UPDATE payment_events SET kind = ? WHERE event_id = ?",
                    (REFUND_KINDS[refund], f"{provider}:{event.event_id}"),
                )
            elif row["status"] == STATUS_PREVIEW:
                token = _new_token()
                conn.execute(
                    """
                    UPDATE orders SET status = ?, paid_at = ?, download_hash = ?,
                        download_expires_at = ?, updated_at = ?,
                        provider = COALESCE(provider, ?),
                        provider_ref = COALESCE(NULLIF(?, ''), provider_ref)
                    WHERE id = ?
                    """,
                    (
                        STATUS_PAID,
                        _iso(current),
                        _hash(token),
                        _iso(current + timedelta(days=days)),
                        _iso(current),
                        provider,
                        event.provider_ref,
                        row["id"],
                    ),
                )
        elif event.kind == "refunded" and row["status"] == STATUS_PAID:
            conn.execute(
                "UPDATE orders SET status = ?, download_hash = NULL, updated_at = ? WHERE id = ?",
                (STATUS_REFUNDED, _iso(current), row["id"]),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (event.order_id,)).fetchone()
    if refund:
        logger.warning(
            "Payment %s:%s for order %s needs a manual refund (%s)",
            provider,
            event.event_id,
            event.order_id,
            REFUND_KINDS[refund],
        )
        if row is not None and row["job_id"]:
            db.append_log(
                str(row["job_id"]),
                f"⚠ Platba {event.event_id} ({REFUND_KINDS[refund]}) – nedoručuje se, vrátit ručně u poskytovatele.",
            )
    return _row(row), token


def is_paid(order: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
    if not order or order.get("status") != STATUS_PAID:
        return False
    expires = _parse(order.get("download_expires_at"))
    return expires is None or _now(now) < expires


def job_expiry(order: dict[str, Any] | None) -> datetime | None:
    """Do kdy držet job objednávky (náhled do konce lhůty + rezerva na platbu, plná mapa po dobu odkazu)."""
    if not order:
        return None
    status = order.get("status")
    if status == STATUS_PAID:
        return _parse(order.get("download_expires_at"))
    if status == STATUS_PREVIEW:
        expires = _parse(order.get("preview_expires_at"))
        return expires + timedelta(hours=PAYMENT_GRACE_HOURS) if expires else None
    updated = _parse(order.get("updated_at")) or _parse(order.get("created_at"))
    if updated is None:
        return None
    if status in (STATUS_CONFIRMED, STATUS_CONFIRMING):
        # Job se počítá; pojistka pro uvízlý job (úklid jobu jinak neproběhne nikdy).
        hours = app_settings.UNPAID_ORDER_RETENTION_HOURS if app_settings.UNPAID_ORDER_RETENTION_HOURS > 0 else 168
        return updated + timedelta(hours=hours)
    if status == STATUS_FAILED:
        hours = app_settings.PRIVATE_JOB_RETENTION_HOURS if app_settings.PRIVATE_JOB_RETENTION_HOURS > 0 else 48
        return updated + timedelta(hours=hours)
    return updated  # expired, refunded: hned


def purge_orders(*, now: datetime | None = None) -> dict[str, int]:
    """Úklid: propadlé nezaplacené → ``expired``; po konci platnosti odkazů smazat čitelný e-mail.

    Joby a jejich výstupy maže úklid jobů; tady jen stav objednávky a osobní údaje.
    """
    current = _iso(_now(now))
    # Rozběhnutá platba (checkout založený před koncem platnosti) může dorazit se zpožděním:
    # takovou objednávku nechat ještě PAYMENT_GRACE_HOURS, ať jde plnou mapu doručit.
    grace = _iso(_now(now) - timedelta(hours=PAYMENT_GRACE_HOURS))
    stale = _iso(_now(now) - timedelta(minutes=CONFIRMING_STALE_MINUTES))
    with db.connect() as conn:
        # Pád mezi potvrzením a založením jobu: odkaz z e-mailu jde použít znovu (blokoval by limit).
        released = conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE status = ? AND updated_at <= ?",
            (STATUS_AWAITING, current, STATUS_CONFIRMING, stale),
        ).rowcount
        # Potvrzená objednávka, jejíž job zmizel (ruční smazání, úklid) – už se nedokončí.
        orphaned = conn.execute(
            """
            UPDATE orders SET status = ?, updated_at = ?
            WHERE status = ? AND (job_id IS NULL OR job_id NOT IN (SELECT id FROM jobs))
              AND updated_at <= ?
            """,
            (STATUS_FAILED, current, STATUS_CONFIRMED, stale),
        ).rowcount
        expired = conn.execute(
            """
            UPDATE orders SET status = ?, updated_at = ?, email = NULL, job_request_json = NULL,
                confirm_hash = NULL, preview_hash = NULL
            WHERE (status = ? AND confirm_expires_at <= ?)
               OR (status = ? AND preview_expires_at <= ?
                   AND (provider_ref IS NULL OR preview_expires_at <= ?))
            """,
            (STATUS_EXPIRED, current, STATUS_AWAITING, current, STATUS_PREVIEW, current, grace),
        ).rowcount
        scrubbed = conn.execute(
            """
            UPDATE orders SET email = NULL, download_hash = NULL, updated_at = ?
            WHERE email IS NOT NULL AND (
                (status = ? AND download_expires_at <= ?)
                OR status IN (?, ?)
            )
            """,
            (current, STATUS_PAID, current, STATUS_FAILED, STATUS_REFUNDED),
        ).rowcount
        conn.commit()
    return {
        "expired": int(expired),
        "scrubbed": int(scrubbed),
        "released": int(released),
        "orphaned": int(orphaned),
    }
