"""Tok objednávky na placené doméně (app.paid_flow) s testovacím poskytovatelem plateb."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

OMAP = Path(__file__).resolve().parents[1] / "configs" / "oom" / "ISSprOM_2019_4000_cz.omap"

PAID_HOST = "map.example"
FORM = {
    "name": "Order test",
    "map_scale": "4000",
    "contour_interval": "2.5",
    "bbox": "14.40,50.08,14.42,50.09",
    "sm5_sheets": "PRAH77",
    "output_mode": "png_zip",
    "notify_email": "buyer@example.org",
}


@pytest.fixture()
def shop(client, monkeypatch):
    import app.main as main
    import app.paid_flow as paid_flow
    import app.settings as settings

    monkeypatch.setenv("SITES", f"{PAID_HOST}=en:paid:*")
    monkeypatch.setattr(settings, "PAYMENTS_PROVIDER", "fake")
    monkeypatch.setattr(settings, "PAYMENTS_FAKE_ALLOWED", True)
    monkeypatch.setattr(settings, "ORDER_MAILS_PER_IP_HOUR", 50)
    paid_flow.reset_order_mail_limits()
    monkeypatch.setattr(main, "query_sm5_sheets", lambda *a, **k: [{"mapnom": "PRAH77", "name": "Praha 7-7"}])
    monkeypatch.setattr(main, "check_create_job", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "enqueue", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "queue_position", lambda *_a, **_k: 0)
    mails: list[dict] = []
    monkeypatch.setattr(
        paid_flow,
        "send_mail",
        lambda to, subject, body, attachments=None: mails.append(
            {"to": to, "subject": subject, "body": body, "attachments": attachments}
        ),
    )
    return mails


def _link(mail: dict, pattern: str) -> str:
    match = re.search(pattern, mail["body"])
    assert match, mail["body"]
    return match.group(0)


def _finish_job(job_id: str) -> None:
    from PIL import Image

    import app.db as db
    from app.settings import JOBS_DIR

    out = JOBS_DIR / job_id / "output"
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out / "podkladarna_output.zip", "w") as zf:
        zf.write(OMAP, "order-test_sprint.omap")
    Image.new("RGB", (200, 120), (240, 240, 240)).save(out / "preview.png")
    db.update_job(job_id, status="done", phase="done")


def test_paid_order_flow(client, shop):
    import app.db as db
    import app.job_worker as job_worker
    import app.orders as orders

    host = {"host": PAID_HOST}
    page = client.get("/", headers=host)
    assert 'id="paid-note"' in page.text and "10.00 EUR" in page.text

    r = client.post("/api/jobs", data={**FORM, "output_mode": "png"}, headers=host)
    assert r.status_code == 200 and r.json()["order_pending"] is True
    assert db.list_jobs(include_private=True) == []  # job až po potvrzení e-mailu
    stale = _link(shop[0], r"https://map\.example/order/confirm/\S+").removeprefix("https://map.example")
    # Nové zadání na týž e-mail nahradí nepotvrzené (starý odkaz neplatí).
    r = client.post("/api/jobs", data={**FORM, "output_mode": "png"}, headers=host)
    assert r.status_code == 200 and client.post(stale, follow_redirects=False).status_code == 404
    confirm = _link(shop[1], r"https://map\.example/order/confirm/\S+").removeprefix("https://map.example")

    assert "<form" in client.get(confirm).text  # GET nic nemění (skenery odkazů)
    assert db.list_jobs(include_private=True) == []
    r = client.post(confirm, follow_redirects=False)
    assert r.status_code == 303
    job_id = r.headers["location"].split("job=")[1]
    job = db.get_job(job_id)
    assert job["options"]["order_id"] and job["options"]["private"]
    assert job["options"]["output_zip"] is True  # „jen PNG“ by po zaplacení nemělo co doručit
    assert client.post(confirm, follow_redirects=False).status_code == 404  # jednorázové
    assert client.post("/api/jobs", data=FORM, headers=host).status_code == 429  # 1 nezaplacená na e-mail

    _finish_job(job_id)
    token = db.get_download_token(job_id)
    assert client.get(f"/api/jobs/{job_id}/download?token={token}").status_code == 404  # zamčeno
    assert client.get(f"/d/{token}").status_code == 404
    job_worker._notify_private_job(job_id)
    job_worker._notify_private_job(job_id)  # opakování (retry po restartu) nic nepošle
    assert len(shop) == 3
    assert sorted(p.name for p in shop[2]["attachments"]) == ["order_preview.png", "sample-4x4cm.zip"]
    order_url = _link(shop[2], r"https://map\.example/order/\S+").removeprefix("https://map.example")

    order_page = client.get(order_url)
    assert order_page.status_code == 200 and "/checkout" in order_page.text
    assert "/sample.zip" in order_page.text
    assert client.get(f"{order_url}/preview.png").headers["content-type"] == "image/png"
    sample = client.get(f"{order_url}/sample.zip")
    assert sample.status_code == 200 and sample.content.startswith(b"PK")

    r = client.post(f"{order_url}/checkout", follow_redirects=False)
    assert r.status_code == 303 and "/api/payments/fake/checkout/" in r.headers["location"]
    assert client.get(r.headers["location"]).status_code == 200
    paid = client.post(r.headers["location"], follow_redirects=False)
    assert paid.status_code == 303 and paid.headers["location"] == "/order/thanks"
    assert client.get("/order/thanks").status_code == 200
    assert len(shop) == 4
    download = _link(shop[3], r"https://map\.example/order/download/\S+").removeprefix("https://map.example")
    got = client.get(download)
    assert got.status_code == 200 and got.content.startswith(b"PK")
    assert orders.order_for_job(job_id)["status"] == orders.STATUS_PAID
    assert client.get(f"/d/{token}").status_code == 200  # po zaplacení odemčeno
    # Další objednávka na týž e-mail už jde.
    assert client.post("/api/jobs", data=FORM, headers=host).json()["order_pending"] is True


def test_free_domain_and_disabled_shop_unchanged(client, shop, monkeypatch):
    import app.settings as settings

    r = client.post("/api/jobs", data=FORM)  # jiná doména než placená
    assert r.status_code == 200 and r.json().get("id")
    monkeypatch.setattr(settings, "PAYMENTS_FAKE_ALLOWED", False)  # bez pojistky se prodej nezapne
    r = client.post("/api/jobs", data={**FORM, "name": "other"}, headers={"host": PAID_HOST})
    assert r.status_code == 200 and "order_pending" not in r.json()
    assert 'id="paid-note"' not in client.get("/", headers={"host": PAID_HOST}).text
    assert client.post("/api/payments/webhook/fake", content=b"{}").status_code == 404
    assert shop == []


def test_webhook_rejects_bad_signature(client, shop):
    r = client.post("/api/payments/webhook/fake", content=b'{"x":1}', headers={"x-fake-signature": "0" * 64})
    assert r.status_code == 400


def test_order_mail_rate_limit_and_one_line_name(client, shop, monkeypatch):
    import app.settings as settings

    monkeypatch.setattr(settings, "ORDER_MAILS_PER_IP_HOUR", 2)
    host = {"host": PAID_HOST}
    for i in range(2):
        data = {**FORM, "notify_email": f"x{i}@example.org", "name": "Mapa\r\nBcc: x@example.org"}
        assert client.post("/api/jobs", data=data, headers=host).status_code == 200
    assert shop[0]["subject"].count("\n") == 0 and "Mapa Bcc" in shop[0]["subject"]
    r = client.post("/api/jobs", data={**FORM, "notify_email": "x9@example.org"}, headers=host)
    assert r.status_code == 429 and len(shop) == 2
