"""Portugalsko: LiDAR DGT z Centro de Dados – přihlášení, hledání, cache dlaždic, nabídka jen s účtem a sonda."""

import io
import json

import pytest

from app import countries, source_status
from app.pipeline import elevation_raster, reference_pt


class _Resp(io.BytesIO):
    def __init__(self, data: bytes, url: str):
        super().__init__(data)
        self._url = url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _item(kind: str, num: str, version: str = "03") -> dict:
    col = reference_pt.PT_COLLECTIONS[kind]
    return {
        "id": f"{col}-{num}-07-2024",
        "properties": {"version": version},
        "assets": {"data": {"href": f"{reference_pt.PT_API}/download/{kind}{num}"}},
    }


def test_tile_key_and_search_body():
    assert reference_pt.tile_key("MDS-50cm-111194-07-2024") == "111194-07-2024"
    assert reference_pt.tile_key("MDT-50cm-205466-07-2025") == "205466-07-2025"
    assert reference_pt.tile_key("../MDT-50cm-1-07-2024") is None
    body = reference_pt.search_body("MDT-50cm", (-9.15, 38.70, -9.14, 38.71))
    intersects, coll = body["filter"]["args"]
    assert body["filter-lang"] == "cql2-json" and coll["args"][1] == "MDT-50cm"
    ring = intersects["args"][1]["coordinates"][0]
    assert ring[0] == ring[-1] == [-9.15, 38.70] and len(ring) == 5


@pytest.mark.parametrize(
    "env, ready",
    [({}, False), ({"PT_DGT_USER": "u"}, False), ({"PT_DGT_USER": "u", "PT_DGT_PASSWORD": "p"}, True)],
)
def test_portugal_offered_only_with_account(monkeypatch, env, ready):
    monkeypatch.delenv("PT_DGT_USER", raising=False)
    monkeypatch.delenv("PT_DGT_PASSWORD", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    pt = countries.load_countries()["PT"]
    assert pt.elevation == "pt_dgt" and pt.crs == "EPSG:3763" and not pt.cz_sources and pt.technical_map == "none"
    assert "Direção-Geral do Território" in pt.citation
    assert countries.elevation_ready(pt) is ready


def test_login_posts_form_and_needs_session_cookie(monkeypatch):
    monkeypatch.setenv("PT_DGT_USER", "user@example.org")
    monkeypatch.setenv("PT_DGT_PASSWORD", "tajne")
    s = reference_pt.CddSession("test")
    sent = []
    action = "https://auth.cdd.dgterritorio.gov.pt/realms/dgterritorio/login-actions/authenticate?a=1&amp;b=2"

    def fake_open(url, data=None, headers=None, timeout=None):
        sent.append((url, data))
        if data is None:
            return _Resp(f'<form id="kc-form-login" action="{action}" method="post">'.encode(), url)
        from http.cookiejar import Cookie

        s.jar.set_cookie(Cookie(0, "connect.sid", "x", None, False, "cdd.dgterritorio.gov.pt", True, False, "/",
                                True, True, None, False, None, None, {}))
        return _Resp(b"ok", f"{reference_pt.PT_CDD}/dgt-fe/downloads")

    monkeypatch.setattr(s, "_open", fake_open)
    s.login()
    assert sent[1][0].endswith("?a=1&b=2") and b"username=user%40example.org" in sent[1][1]


def test_login_without_account_fails_without_secrets(monkeypatch):
    monkeypatch.delenv("PT_DGT_USER", raising=False)
    monkeypatch.delenv("PT_DGT_PASSWORD", raising=False)
    with pytest.raises(reference_pt.PtAuthError):
        reference_pt.CddSession("test").login()


def test_search_follows_next_pages(monkeypatch):
    s = reference_pt.CddSession("test")
    pages = [
        {"features": [{"id": "a"}], "links": [{"rel": "next", "body": {"token": "t2"}}]},
        {"data": {"features": [{"id": "b"}], "links": [{"rel": "self"}]}},
    ]
    bodies = []

    def fake_open(url, data=None, headers=None, timeout=None):
        bodies.append(json.loads(data))
        return _Resp(json.dumps(pages[len(bodies) - 1]).encode(), url)

    monkeypatch.setattr(s, "_open", fake_open)
    assert [f["id"] for f in s.search("MDS-50cm", (0, 0, 1, 1))] == ["a", "b"]
    assert bodies[1] == {"token": "t2"}


def test_fetch_tile_rejects_non_tiff(tmp_path, monkeypatch):
    class _S:
        def open_download(self, href):
            return _Resp(b"<html>login</html>", href)

    monkeypatch.setattr(elevation_raster, "run_cmd", lambda *a, **k: pytest.fail("bez převodu"))
    with pytest.raises(RuntimeError, match="GeoTIFF"):
        elevation_raster._pt_fetch_tile(_S(), "dtm", _item("dtm", "111194"), tmp_path / "t" / "dtm_v03.tif")
    assert not any((tmp_path / "t").iterdir())


def test_provider_downloads_missing_tiles_and_builds_vrt(tmp_path, monkeypatch):
    monkeypatch.setattr(elevation_raster.settings, "CACHE_DIR", tmp_path / "cache")
    cached = elevation_raster.pt_tile_dir("111194-07-2024") / "dtm_v03.tif"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"x")

    class _S:
        def __init__(self, _ua):
            pass

        def login(self):
            pass

        def search(self, collection, bbox):
            assert -10 < bbox[0] < bbox[2] < -6 and 36 < bbox[1] < bbox[3] < 43
            kind = "dtm" if collection == "MDT-50cm" else "dsm"
            return [_item(kind, "111194"), _item(kind, "111195")]

    fetched, cmds = [], []

    def fake_fetch(session, kind, item, dest, log=None):
        fetched.append(item["id"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")
        return dest

    monkeypatch.setattr(reference_pt, "CddSession", _S)
    monkeypatch.setattr(elevation_raster, "_pt_fetch_tile", fake_fetch)
    monkeypatch.setattr(elevation_raster, "run_cmd", lambda cmd, **_kw: cmds.append(cmd) or open(cmd[-3], "w").close())
    monkeypatch.setattr(elevation_raster, "_gdal_tool", lambda name: name)
    monkeypatch.setattr(elevation_raster, "_buffered", lambda b, crs, buf: (-88000.0, -106000.0, -87000.0, -105000.0))
    out = elevation_raster.PROVIDERS["pt_dgt"]((0, 0, 1, 1), tmp_path)
    assert out.dtm.name == "pt_dgt_dtm.vrt" and out.dsm.name == "pt_dgt_dsm.vrt"
    assert sorted(fetched) == ["MDS-50cm-111194-07-2024", "MDS-50cm-111195-07-2024", "MDT-50cm-111195-07-2024"]
    assert all("EPSG:3763" in c for c in cmds) and len(cmds) == 2


def test_provider_without_account_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv("PT_DGT_USER", raising=False)
    monkeypatch.delenv("PT_DGT_PASSWORD", raising=False)
    monkeypatch.setattr(elevation_raster, "_buffered", lambda b, crs, buf: (-88000.0, -106000.0, -87000.0, -105000.0))
    with pytest.raises(elevation_raster.ElevationUnavailable, match="PT_DGT_USER"):
        elevation_raster.PROVIDERS["pt_dgt"]((0, 0, 1, 1), tmp_path)


def test_probe_registered_for_portugal():
    probe = next(p for p in source_status.PROBES if p.key == "pt_elevation")
    assert probe.countries == ("PT",) and probe.fatal and probe.urls == (reference_pt.PT_ELEVATION_PROBE,)
    assert probe.urls[0].endswith("/dgt-be/v1/collections/MDT-50cm")
