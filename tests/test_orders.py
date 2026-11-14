"""Objednávky placené domény (app.orders) a testovací poskytovatel plateb (app.payments.fake)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

T0 = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
REQUEST = {"name": "Test", "preset_id": "sprint_2m", "options": {"bbox_wgs84": [14.0, 50.0, 14.01, 50.01]}}


@pytest.fixture()
def orders(data_dir):
    import app.orders as orders

    return orders


def _paid_event(order_id: str, ref: str = "cs_1", event_id: str = "evt_1", kind: str = "paid"):
    from app.payments import PaymentEvent

    return PaymentEvent(event_id=event_id, kind=kind, order_id=order_id, provider_ref=ref)


def _to_preview(orders, email="a@example.org", now=T0):
    order, confirm = orders.create_order(email=email, job_request=REQUEST, site="map.example", now=now)
    assert orders.claim_confirmation(confirm, now=now)
    orders.attach_job(order["id"], "job123", now=now)
    preview = orders.issue_preview_token(order["id"], now=now)
    return order, confirm, preview


def test_tables_created_additively(data_dir):
    from app.settings import DB_PATH

    with sqlite3.connect(DB_PATH) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"orders", "payment_events", "jobs", "job_origin"} <= names


def test_lifecycle_tokens_hashed_and_rotated(orders, data_dir):
    order, confirm, preview = _to_preview(orders)
    from app.settings import DB_PATH

    with sqlite3.connect(DB_PATH) as conn:
        dump = " ".join(str(v) for row in conn.execute("SELECT * FROM orders") for v in row)
    assert confirm not in dump and preview not in dump  # v DB jen hashe
    assert orders.get_order_by_token("confirm", confirm, now=T0) is None  # potvrzení jednorázové
    got = orders.get_order_by_token("preview", preview, now=T0)
    assert got["status"] == orders.STATUS_PREVIEW and got["job_id"] == "job123"
    assert got["job_request"] is None  # zadání se po založení jobu nedrží
    orders.set_checkout(order["id"], "fake", "cs_1", now=T0)
    paid, download = orders.apply_payment_event(_paid_event(order["id"]), "fake", now=T0)
    assert paid["status"] == orders.STATUS_PAID and download
    assert orders.is_paid(paid, now=T0)
    assert orders.get_order_by_token("download", download, now=T0)["id"] == order["id"]
    assert orders.get_order_by_token("preview", download, now=T0) is None  # tokeny nejsou zaměnitelné


def test_double_confirm_creates_one_job(orders):
    order, confirm = orders.create_order(email="b@example.org", job_request=REQUEST, now=T0)
    assert orders.claim_confirmation(confirm, now=T0)["job_request"] == REQUEST
    assert orders.claim_confirmation(confirm, now=T0) is None
    orders.release_confirmation(order["id"], now=T0)  # založení jobu selhalo → jde znovu
    assert orders.claim_confirmation(confirm, now=T0) is not None


def test_confirm_link_expires(orders):
    _order, confirm = orders.create_order(email="c@example.org", job_request=REQUEST, now=T0)
    assert orders.claim_confirmation(confirm, now=T0 + timedelta(hours=25)) is None


def test_one_open_unpaid_order_per_email(orders):
    orders.create_order(email="d@example.org", job_request=REQUEST, now=T0)
    with pytest.raises(orders.OrderLimitError):
        orders.create_order(email=" d@example.org ", job_request=REQUEST, now=T0)
    orders.create_order(email="e@example.org", job_request=REQUEST, now=T0)  # jiný e-mail
    # Propadlé potvrzení limit neblokuje.
    orders.create_order(email="d@example.org", job_request=REQUEST, now=T0 + timedelta(hours=25))


def test_paid_order_does_not_block_next(orders):
    order, _c, _p = _to_preview(orders, email="f@example.org")
    orders.apply_payment_event(_paid_event(order["id"]), "fake", now=T0)
    orders.create_order(email="f@example.org", job_request=REQUEST, now=T0)


def test_payment_event_idempotent_and_provider_checked(orders):
    order, _c, _p = _to_preview(orders, email="g@example.org")
    orders.set_checkout(order["id"], "fake", "cs_1", now=T0)
    _o, first = orders.apply_payment_event(_paid_event(order["id"]), "fake", now=T0)
    _o, again = orders.apply_payment_event(_paid_event(order["id"]), "fake", now=T0)
    assert first and again is None
    other, _c, _p = _to_preview(orders, email="h@example.org")
    orders.set_checkout(other["id"], "fake", "cs_2", now=T0)
    got, token = orders.apply_payment_event(_paid_event(other["id"], event_id="evt_x"), "jiny", now=T0)
    assert got is None and token is None
    unknown, token = orders.apply_payment_event(_paid_event("neexistuje", event_id="evt_y"), "fake", now=T0)
    assert unknown is None and token is None


def test_refund_revokes_download(orders):
    order, _c, _p = _to_preview(orders, email="i@example.org")
    _o, download = orders.apply_payment_event(_paid_event(order["id"]), "fake", now=T0)
    refunded, _t = orders.apply_payment_event(
        _paid_event(order["id"], event_id="evt_r", kind="refunded"), "fake", now=T0
    )
    assert refunded["status"] == orders.STATUS_REFUNDED
    assert orders.get_order_by_token("download", download, now=T0) is None


def test_purge_expires_unpaid_and_scrubs_email(orders):
    unpaid, _c, preview = _to_preview(orders, email="j@example.org")
    paid, _c, _p = _to_preview(orders, email="k@example.org")
    orders.apply_payment_event(_paid_event(paid["id"]), "fake", now=T0)
    late = T0 + timedelta(days=8)
    assert orders.purge_orders(now=late)["expired"] == 1
    gone = orders.get_order(unpaid["id"])
    assert gone["status"] == orders.STATUS_EXPIRED and gone["email"] is None and gone["email_tag"]
    assert orders.get_order(paid["id"])["email"] == "k@example.org"  # stažení platí 30 dní
    orders.purge_orders(now=T0 + timedelta(days=31))
    assert orders.get_order(paid["id"])["email"] is None


def test_late_payment_within_grace_is_delivered(orders):
    order, _c, _p = _to_preview(orders, email="l@example.org")
    orders.set_checkout(order["id"], "fake", "cs_1", now=T0)
    after = T0 + timedelta(days=7, hours=1)
    orders.purge_orders(now=after)  # rozběhnutý checkout: ještě nepropadne
    assert orders.get_order(order["id"])["status"] == orders.STATUS_PREVIEW
    paid, token = orders.apply_payment_event(_paid_event(order["id"]), "fake", now=after)
    assert paid["status"] == orders.STATUS_PAID and token


def test_fake_provider_signature():
    from app.payments import WebhookError, get_provider
    from app.payments import fake

    provider = get_provider("fake")
    session = provider.create_checkout(
        order_id="o1",
        amount_cents=1000,
        currency="EUR",
        email=None,
        description="map",
        success_url="https://x/ok",
        cancel_url="https://x/no",
    )
    assert session.url.startswith(fake.CHECKOUT_PATH) and session.provider_ref
    body = fake.event_body("o1", session.provider_ref)
    event = provider.parse_webhook(body, {"X-Fake-Signature": fake.sign(body)})
    assert event.kind == "paid" and event.order_id == "o1"
    with pytest.raises(WebhookError):
        provider.parse_webhook(body, {"X-Fake-Signature": "0" * 64})
    with pytest.raises(WebhookError):
        provider.parse_webhook(body + b" ", {"X-Fake-Signature": fake.sign(body)})


def test_payments_off_by_default(data_dir):
    from app.payments import get_provider, payments_enabled

    assert get_provider("") is None
    assert payments_enabled() is False
