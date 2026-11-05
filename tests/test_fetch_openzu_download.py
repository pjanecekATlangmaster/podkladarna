"""Stažení listu z OpenZU: useknutý / vadný ZIP se stáhne znovu (job 046c4604c4cf)."""

from __future__ import annotations

import io
import zipfile

import pytest

from app.pipeline import fetch_openzu as fo


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("UJAN08.laz", b"L" * 5000)
    return buf.getvalue()


@pytest.mark.parametrize(
    "bad_payloads, ok",
    [
        ([b"<html>chyba</html>" * 100], True),  # stránka s chybou místo ZIP, druhý pokus OK
        ([_zip_bytes()[:3000], b"x" * 2000], True),  # useknutý ZIP, pak smetí, třetí pokus OK
        ([b"y" * 2000] * fo.DOWNLOAD_ATTEMPTS, False),  # všechny pokusy vadné → FetchError
    ],
)
def test_download_retries_bad_zip(tmp_path, monkeypatch, bad_payloads, ok):
    payloads = list(bad_payloads) + [_zip_bytes()]
    calls = []

    def fake_download(_url, dest):
        dest.write_bytes(payloads[len(calls)])
        calls.append(dest)
        return None

    monkeypatch.setattr(fo, "_http_download", fake_download)
    monkeypatch.setattr(fo.time, "sleep", lambda _s: None)
    zpath, laz = tmp_path / "DMPOK.zip", tmp_path / "DMPOK.laz"
    if ok:
        fo._download_and_extract("u", zpath, laz, "DMPOK UJAN08", None)
        assert laz.stat().st_size == 5000
        assert len(calls) == len(bad_payloads) + 1
    else:
        with pytest.raises(fo.FetchError):
            fo._download_and_extract("u", zpath, laz, "DMPOK UJAN08", None)
        assert len(calls) == fo.DOWNLOAD_ATTEMPTS
        assert not laz.exists() and not zpath.exists()


def test_http_download_rejects_truncated_body(tmp_path, monkeypatch):
    class Resp(io.BytesIO):
        status = 200
        headers = {"Content-Length": "5000", "Last-Modified": "x"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(fo.urllib.request, "urlopen", lambda *a, **k: Resp(b"z" * 3000))
    dest = tmp_path / "a.zip"
    with pytest.raises(fo.FetchError, match="Neúplné"):
        fo._http_download("https://example.invalid/a.zip", dest)
    assert not dest.exists()
    assert not (tmp_path / "a.zip.part").exists()
