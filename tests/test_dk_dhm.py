"""Dánsko: výšky DHM z WCS Datafordeleru – dlaždice, API klíč jen z prostředí a nikdy v chybách, nabídka země."""

import io
import urllib.error
import urllib.parse

import pytest

from app import countries, source_status
from app.pipeline import elevation_raster, reference_dk

KEY = "TESTKEY0123456789abcdef"


def _query(url):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


def test_urls_tile_window_at_1m_with_placeholder_key(monkeypatch):
    monkeypatch.setenv(reference_dk.DK_API_KEY_ENV, KEY)
    urls = elevation_raster.dk_dhm_urls("dsm", (723000.0, 6186000.0, 725500.0, 6187000.0))
    assert len(urls) == 2  # 2,5 km + okraj → dvě dlaždice nejvýš 2 km
    q = _query(urls[0])
    x0, y0, x1, y1 = map(float, q["BBOX"][0].split(","))
    assert q["COVERAGE"] == ["dhm_overflade"] and q["VERSION"] == ["1.0.0"] and q["FORMAT"] == ["GTiff"]
    assert q["CRS"] == q["RESPONSE_CRS"] == ["EPSG:25832"]
    assert int(q["WIDTH"][0]) == x1 - x0 and int(q["HEIGHT"][0]) == y1 - y0
    assert all(KEY not in u and reference_dk.KEY_PLACEHOLDER in u for u in urls)  # klíč až při dotazu


def test_with_key_and_redact(monkeypatch):
    monkeypatch.delenv(reference_dk.DK_API_KEY_ENV, raising=False)
    assert reference_dk.with_key("https://example.org/x") == "https://example.org/x"
    with pytest.raises(RuntimeError, match=reference_dk.DK_API_KEY_ENV):
        reference_dk.with_key(reference_dk.DK_ELEVATION_PROBE)
    monkeypatch.setenv(reference_dk.DK_API_KEY_ENV, f" {KEY}\n")
    assert f"apikey={KEY}&" in reference_dk.with_key(reference_dk.DK_ELEVATION_PROBE)
    assert reference_dk.redact(f"chyba u ?apikey={KEY}") == "chyba u ?apikey=***"


def test_download_retries_401_and_never_leaks_key(tmp_path, monkeypatch):
    monkeypatch.setenv(reference_dk.DK_API_KEY_ENV, KEY)
    seen = []

    def fake(url, dest, *, log=None, **_kw):
        seen.append(url)
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(elevation_raster, "_download", fake)
    url = elevation_raster.dk_dhm_urls("dtm", (723000.0, 6186000.0, 723500.0, 6186500.0))[0]
    with pytest.raises(RuntimeError) as err:
        elevation_raster._dk_download(url, tmp_path / "t.tif", wait_s=0)
    assert len(seen) == elevation_raster.DK_RETRIES + 1 and KEY in seen[0]
    assert str(err.value) == "Datafordeler DHM: HTTP 401"
    assert err.value.__cause__ is None and err.value.__suppress_context__  # traceback bez HTTPError s URL


def test_download_bad_response_message_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv(reference_dk.DK_API_KEY_ENV, KEY)

    def fake(url, dest, *, log=None, **_kw):
        raise RuntimeError(f"WCS nevrátil GeoTIFF (text/xml): <a href='{url}'/>")

    monkeypatch.setattr(elevation_raster, "_download", fake)
    url = elevation_raster.dk_dhm_urls("dtm", (723000.0, 6186000.0, 723500.0, 6186500.0))[0]
    with pytest.raises(RuntimeError) as err:
        elevation_raster._dk_download(url, tmp_path / "t.tif", wait_s=0)
    assert KEY not in str(err.value) and "***" in str(err.value)


@pytest.mark.parametrize("key, ready", [("", False), ("  ", False), (KEY, True)])
def test_denmark_offered_only_with_key(monkeypatch, key, ready):
    monkeypatch.setenv(reference_dk.DK_API_KEY_ENV, key)
    dk = countries.load_countries()["DK"]
    assert dk.elevation == "dk_dhm" and dk.crs == "EPSG:25832" and not dk.cz_sources
    assert countries.elevation_ready(dk) is ready
    assert countries.elevation_ready(countries.load_countries()["CZ"])  # Česko beze změny


def test_probe_fills_key_only_for_request(monkeypatch):
    monkeypatch.setattr(source_status, "PROBE_RETRY_WAIT_S", 0)
    monkeypatch.setenv(reference_dk.DK_API_KEY_ENV, KEY)
    probe = next(p for p in source_status.PROBES if p.key == "dk_elevation")
    assert probe.countries == ("DK",) and probe.fatal and KEY not in probe.urls[0]
    opened = []

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b""

    monkeypatch.setattr(source_status.urllib.request, "urlopen", lambda req, timeout: opened.append(req.full_url) or Resp())
    assert source_status.check_probe(probe)["ok"]
    assert KEY in opened[0]
    monkeypatch.delenv(reference_dk.DK_API_KEY_ENV)
    result = source_status.check_probe(probe)
    assert not result["ok"] and reference_dk.DK_API_KEY_ENV in result["detail"]


def test_ortho_tiles_cover_grid_with_placeholder_key(monkeypatch):
    monkeypatch.setenv(reference_dk.DK_API_KEY_ENV, KEY)
    grid = (574000.0, 6223000.0, 577000.0, 6224000.0)
    tiles = reference_dk.ortho_tile_urls(grid, 6000, 2000, tile_px=4000)
    assert [(w, h) for _u, _b, w, h in tiles] == [(4000, 2000), (2000, 2000)]
    assert tiles[0][1] == (574000.0, 6223000.0, 576000.0, 6224000.0) and tiles[1][1][2] == 577000.0
    q = _query(tiles[0][0])
    assert q["LAYERS"] == ["orto_foraar"] and q["CRS"] == ["EPSG:25832"] and q["WIDTH"] == ["4000"]
    assert all(KEY not in u for u, *_r in tiles)


def test_direct_ortho_failure_logged_without_key(tmp_path, monkeypatch):
    from app.pipeline import open_paved

    monkeypatch.setenv(reference_dk.DK_API_KEY_ENV, KEY)

    def boom(dest, grid, width, height, wkt):
        raise RuntimeError(f"GDAL: cannot open https://wms.datafordeler.dk/?apikey={KEY}")

    monkeypatch.setattr(reference_dk, "fetch_ortho", boom)
    lines = []
    out = open_paved._fetch_direct_ortho(
        "DK", tmp_path / "ortofoto.tif", tmp_path / "ortofoto.json", {}, (0, 0, 10, 10), 20, 20, log=lines.append
    )
    assert out is None and lines and KEY not in lines[0] and "***" in lines[0]
    assert not list(tmp_path.iterdir())  # žádný dočasný soubor ani metadata
