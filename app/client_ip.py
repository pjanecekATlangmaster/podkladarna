from __future__ import annotations

from fastapi import Request

from app.settings import TRUST_PROXY_HEADERS


def client_ip(request: Request) -> str:
    """Vrátí IP klienta (za reverse proxy z X-Forwarded-For / CF-Connecting-IP)."""
    if TRUST_PROXY_HEADERS:
        cf = (request.headers.get("cf-connecting-ip") or "").strip()
        if cf:
            return cf
        xff = (request.headers.get("x-forwarded-for") or "").strip()
        if xff:
            return xff.split(",")[0].strip()
        xri = (request.headers.get("x-real-ip") or "").strip()
        if xri:
            return xri
    if request.client and request.client.host:
        return request.client.host
    return "unknown"
