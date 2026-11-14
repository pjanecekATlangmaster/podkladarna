"""Rozhraní poskytovatele plateb pro prodej map na doméně v režimu ``paid``.

Poskytovatel (prodejce na účet typu Paddle / Stripe Managed Payments, nebo platební brána) je za
tenkým rozhraním :class:`PaymentProvider`, aby šel vyměnit bez zásahu do toku objednávky
(``app.orders``). Volání na API poskytovatele patří jen do jeho adaptéru v tomto balíčku.

Zatím existuje jen testovací :mod:`app.payments.fake` (bez skutečné platby, bez tajemství
v repozitáři). Výběr ``PAYMENTS_PROVIDER`` v ``.env``; prázdné = prodej vypnutý.
Návrh: ``/mnt/project-files/evropa-data/platby-navrh.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

PAYMENT_EVENT_KINDS = ("paid", "refunded", "failed")


class PaymentError(Exception):
    """Chyba při komunikaci s poskytovatelem (založení platby se nepovedlo)."""


class WebhookError(PaymentError):
    """Webhook s neplatným podpisem nebo obsahem – nikdy ho nezpracovat."""


@dataclass(frozen=True)
class CheckoutSession:
    """Založená platba: kam přesměrovat zákazníka a pod jakým ID ji vede poskytovatel."""

    url: str
    provider_ref: str


@dataclass(frozen=True)
class PaymentEvent:
    """Ověřená událost od poskytovatele (už po kontrole podpisu)."""

    event_id: str  # jedinečné ID události u poskytovatele (idempotence)
    kind: str  # jedna z PAYMENT_EVENT_KINDS
    order_id: str  # naše ID objednávky (poskytovateli předané při založení platby)
    provider_ref: str = ""  # ID platby / checkoutu u poskytovatele
    amount_cents: int | None = None
    currency: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)


class PaymentProvider:
    """Společné rozhraní adaptérů. Adaptér nedrží stav objednávek, jen mluví s poskytovatelem."""

    name = "base"

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
        raise NotImplementedError

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> PaymentEvent | None:
        """Ověří podpis a vrátí událost; ``None`` = událost, která nás nezajímá. Neplatná → WebhookError."""
        raise NotImplementedError

    def receipt_url(self, provider_ref: str) -> str | None:
        """Odkaz na doklad u poskytovatele (online doklad pro zákazníka), pokud ho poskytovatel má."""
        return None


def provider_names() -> tuple[str, ...]:
    return ("fake",)


def get_provider(name: str | None = None) -> PaymentProvider | None:
    """Nakonfigurovaný poskytovatel, nebo ``None`` = prodej vypnutý (výchozí)."""
    if name is None:
        from app.settings import PAYMENTS_PROVIDER

        name = PAYMENTS_PROVIDER
    name = (name or "").strip().lower()
    if not name:
        return None
    if name == "fake":
        from app.payments.fake import FakeProvider

        return FakeProvider()
    raise PaymentError(f"Neznámý poskytovatel plateb: {name!r}")


def payments_enabled() -> bool:
    try:
        return get_provider() is not None
    except PaymentError:
        return False
