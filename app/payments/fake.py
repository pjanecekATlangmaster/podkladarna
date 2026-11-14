"""Testovací poskytovatel plateb (bez peněz) pro vývoj, testy a test B.

Platba je odkaz na vlastní stránku instance; webhook podepisuje HMAC-SHA256 tajemstvím
z ``PAYMENTS_FAKE_SECRET``, nebo náhodným tajemstvím procesu (stačí, protože falešnou platbu
i webhook vyřizuje týž proces). Žádné síťové volání, žádné tajemství v repozitáři.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Mapping
from urllib.parse import quote

from app.payments import (
    PAYMENT_EVENT_KINDS,
    CheckoutSession,
    PaymentEvent,
    PaymentProvider,
    WebhookError,
)

SIGNATURE_HEADER = "x-fake-signature"
# Cesta falešné platební stránky (obsluhu doplní tok objednávky v app.main).
CHECKOUT_PATH = "/api/payments/fake/checkout"

_PROCESS_SECRET = secrets.token_bytes(32)


def _secret() -> bytes:
    env = os.environ.get("PAYMENTS_FAKE_SECRET", "")
    return env.encode("utf-8") if env else _PROCESS_SECRET


def sign(body: bytes) -> str:
    return hmac.new(_secret(), body, hashlib.sha256).hexdigest()


def event_body(order_id: str, provider_ref: str, kind: str = "paid", **extra: Any) -> bytes:
    """Tělo webhooku falešné platby (tak, jak by ho poslal poskytovatel)."""
    payload = {
        "id": f"evt_fake_{secrets.token_hex(8)}",
        "type": kind,
        "order_id": order_id,
        "provider_ref": provider_ref,
        "created": int(time.time()),
        **extra,
    }
    return json.dumps(payload, sort_keys=True).encode("utf-8")


class FakeProvider(PaymentProvider):
    name = "fake"

    def create_checkout(
        self,
        *,
        order_id: str,
        amount_cents: int,
        currency: str,
        email: str | None,
        description: str,
        success_url: str,
        cancel_url: str,
        lang: str | None = None,
    ) -> CheckoutSession:
        ref = f"cs_fake_{secrets.token_hex(12)}"
        url = f"{CHECKOUT_PATH}/{ref}?order={quote(order_id)}"
        return CheckoutSession(url=url, provider_ref=ref)

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> PaymentEvent | None:
        got = ""
        for key, value in headers.items():
            if key.lower() == SIGNATURE_HEADER:
                got = str(value).strip()
                break
        if not got or not hmac.compare_digest(got, sign(body)):
            raise WebhookError("neplatný podpis webhooku")
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise WebhookError("neplatné tělo webhooku") from exc
        if not isinstance(data, dict):
            raise WebhookError("neplatné tělo webhooku")
        kind = str(data.get("type") or "")
        if kind not in PAYMENT_EVENT_KINDS:
            return None
        event_id = str(data.get("id") or "")
        order_id = str(data.get("order_id") or "")
        if not event_id or not order_id:
            raise WebhookError("webhook bez ID události nebo objednávky")
        amount = data.get("amount_cents")
        return PaymentEvent(
            event_id=event_id,
            kind=kind,
            order_id=order_id,
            provider_ref=str(data.get("provider_ref") or ""),
            amount_cents=int(amount) if isinstance(amount, int) else None,
            currency=str(data["currency"]) if data.get("currency") else None,
            raw=data,
        )

    def receipt_url(self, provider_ref: str) -> str | None:
        return None
