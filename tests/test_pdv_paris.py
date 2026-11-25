"""Paříž (Plan de voirie) jako obdoba DTM a výšky LiDAR HD: třídění, dotaz na obálku, dlaždice WMS-R."""

import urllib.parse

import pytest

from app.pipeline import elevation_raster, pdv_paris


def _feat(x0: float, y0: float, w: float = 10.0) -> dict:
    ring = [[x0, y0], [x0 + w, y0], [x0 + w, y0 + w], [x0, y0 + w], [x0, y0]]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}, "properties": {}}


def test_classify_paris_like_dtm():
    data = {
        "plan-de-voirie-chaussees": [_feat(0, 0, 50)],
        "plan-de-voirie-trottoirs-emprises": [_feat(0, 50, 50)],
        "plan-de-voirie-aires-mixtes-vehicules-et-pietons": [_feat(100, 0, 30)],
        "plan-de-voirie-voies-en-escalier": [_feat(60, 40, 3)],
        "plan-de-voirie-ilots-directionnels": [_feat(60, 0)],
        "plan-de-voirie-emprises-espaces-verts": [_feat(200, 0)],
    }
    counts = {k: len(v) for k, v in pdv_paris.classify(data).items()}
    assert counts["roads"] == 1 and counts["sidewalks"] == 2 and counts["stairs"] == 1 and counts["small_paved"] == 1
    assert counts["green"] == 1 and counts["buildings"] == 0  # budovy Plan de voirie nevede, kreslí OSM
    assert counts["public"] == 4 and counts["yards"] == 0


def test_fetch_all_datasets_in_bbox():
    calls = []

    def fake(url):
        calls.append(url)
        return {"features": [_feat(0, 0), {"type": "Feature", "geometry": None}]}, 5

    data, nbytes = pdv_paris.fetch_datasets((2.348, 48.855, 2.356, 48.859), fetch=fake)
    assert set(data) == set(pdv_paris.DATASETS) and all(len(v) == 1 for v in data.values())  # bez geometrie pryč
    assert nbytes == 5 * len(pdv_paris.DATASETS)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(calls[0]).query)
    assert query["where"] == ["in_bbox(geo_shape,48.855000,2.348000,48.859000,2.356000)"]  # jih, západ, sever, východ
    assert query["limit"] == ["-1"] and "/exports/geojson" in calls[0]


def test_lidarhd_tiles_at_half_metre():
    urls = elevation_raster.fr_lidarhd_urls("dtm", (652000.0, 6861000.0, 655000.0, 6862000.0))
    assert len(urls) == 2  # 3,04 km → dvě dlaždice do 2 km
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]).query)
    x0, y0, x1, y1 = map(float, q["BBOX"][0].split(","))
    assert int(q["WIDTH"][0]) == round((x1 - x0) / 0.5) <= 5010 and int(q["HEIGHT"][0]) == round((y1 - y0) / 0.5)
    assert q["CRS"] == ["EPSG:2154"] and "MNT" in q["LAYERS"][0] and q["FORMAT"] == ["image/geotiff"]
    assert "MNS" in elevation_raster.fr_lidarhd_urls("dsm", (652000.0, 6861000.0, 652500.0, 6861500.0))[0]


def test_lidarhd_download_retries_transient_errors(monkeypatch, tmp_path):
    import urllib.error

    calls = []

    def flaky(url, dest, log=None):
        calls.append(url)
        if len(calls) < 3:
            raise urllib.error.HTTPError(url, 502, "Bad Gateway", {}, None)
        return dest

    monkeypatch.setattr(elevation_raster, "_download", flaky)
    assert elevation_raster._download_retry("u", tmp_path / "t.tif", wait_s=0) == tmp_path / "t.tif"
    assert len(calls) == 3

    def missing(url, dest, log=None):
        calls.append(url)
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)

    monkeypatch.setattr(elevation_raster, "_download", missing)
    calls.clear()
    with pytest.raises(urllib.error.HTTPError):
        elevation_raster._download_retry("u", tmp_path / "t.tif", wait_s=0)
    assert len(calls) == 1  # chyba požadavku se neopakuje

    # GUGiK 2026-11-20: výpadek databáze MapServeru vrací 400 s ExceptionReportem – opakovat, pak jasná hláška.
    import io

    body = (b'<ows:ExceptionReport><ows:Exception exceptionCode=""><ows:ExceptionText>msPostGISLayerOpen(): '
            b'Query error. Database connection failed.</ows:ExceptionText></ows:Exception></ows:ExceptionReport>')

    def db_down(url, dest, log=None):
        calls.append(url)
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(body))

    monkeypatch.setattr(elevation_raster, "_download", db_down)
    calls.clear()
    with pytest.raises(RuntimeError, match="výpadek na své straně.*Database connection failed"):
        elevation_raster._download_retry("u", tmp_path / "t.tif", wait_s=0)
    assert len(calls) == 4



def test_lidarhd_download_bare_400_is_service_fault(monkeypatch, tmp_path):
    """WMS-R IGN 2026-11-21 (job ec7893e3175a): 400 bez ServiceException se u Francie opakuje a po vyčerpání pokusů
    hlásí výpadek služby; skutečně chybný dotaz (ServiceException) se neopakuje a hláška nese důvod."""
    import io
    import urllib.error

    calls = []

    def bare_then_ok(url, dest, log=None):
        calls.append(url)
        if len(calls) < 2:
            raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(b""))
        return dest

    monkeypatch.setattr(elevation_raster, "_download", bare_then_ok)
    assert elevation_raster._download_retry("u", tmp_path / "t.tif", wait_s=0, retry_bare_400=True) == tmp_path / "t.tif"
    assert len(calls) == 2

    def bare(url, dest, log=None):
        calls.append(url)
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(b"<html>gateway</html>"))

    monkeypatch.setattr(elevation_raster, "_download", bare)
    calls.clear()
    with pytest.raises(RuntimeError, match="výšková služba má výpadek na své straně.*HTTP 400"):
        elevation_raster._download_retry("u", tmp_path / "t.tif", wait_s=0, retry_bare_400=True)
    assert len(calls) == 4

    body = (b'<ServiceExceptionReport version="1.3.0" xmlns="http://www.opengis.net/ogc"><ServiceException '
            b'code="InvalidParameterValue">WIDTH query parameter exceed the limit (5010)</ServiceException>'
            b'</ServiceExceptionReport>')

    def too_wide(url, dest, log=None):
        calls.append(url)
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(body))

    monkeypatch.setattr(elevation_raster, "_download", too_wide)
    calls.clear()
    with pytest.raises(urllib.error.HTTPError, match="WIDTH query parameter exceed the limit"):
        elevation_raster._download_retry("u", tmp_path / "t.tif", wait_s=0, retry_bare_400=True)
    assert len(calls) == 1


def test_lidarhd_download_unknown_layer_is_service_fault(monkeypatch, tmp_path):
    """WMS-R IGN 2026-11-24 (dva joby Omapicu): 400 „Layer … unknown“ u pevné vrstvy je krátká chyba služby –
    opakuje se a po vyčerpání pokusů je to výpadek služby, ne „výřez na moři“. Bez ``fault_re`` beze změny."""
    import io
    import urllib.error

    body = (b'<ServiceExceptionReport version="1.3.0" xmlns="http://www.opengis.net/ogc"><ServiceException '
            b'code="LayerNotDefined">Layer IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93 unknown'
            b'</ServiceException></ServiceExceptionReport>')
    calls = []

    def unknown(url, dest, log=None):
        calls.append(url)
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(body))

    monkeypatch.setattr(elevation_raster, "_download", unknown)
    with pytest.raises(RuntimeError, match="výšková služba má výpadek na své straně.*Layer IGNF_LIDAR-HD_MNT.*unknown"):
        elevation_raster._download_retry("u", tmp_path / "t.tif", wait_s=0, retry_bare_400=True,
                                         fault_re=elevation_raster.FR_FAULT_RE)
    assert len(calls) == 4

    calls.clear()
    with pytest.raises(urllib.error.HTTPError, match="unknown"):
        elevation_raster._download_retry("u", tmp_path / "t.tif", wait_s=0, retry_bare_400=True)
    assert len(calls) == 1


def test_lidarhd_provider_retries_bare_400(monkeypatch, tmp_path):
    """Provider Francie stahuje s ``retry_bare_400``; ostatní země beze změny (výchozí False)."""
    seen = []
    monkeypatch.setattr(elevation_raster, "_download_retry",
                        lambda url, dest, log=None, **kw: seen.append(
                            kw.get("retry_bare_400") and kw.get("fault_re") is elevation_raster.FR_FAULT_RE) or dest)
    monkeypatch.setattr(elevation_raster, "run_cmd", lambda *a, **k: None)
    monkeypatch.setattr(elevation_raster, "_gdal_tool", lambda name: name)
    monkeypatch.setattr(elevation_raster, "_bounds_in", lambda b, *_a: b)
    elevation_raster.fr_lidarhd_provider((652000.0, 6861000.0, 652500.0, 6861500.0), tmp_path)
    assert seen and all(seen)


def _tile(x0: float, y0: float, mnt: bool = True, mns: bool = True) -> dict:
    feat = _feat(x0, y0, 1.0)
    feat["properties"] = {"url_mnt": "a" if mnt else None, "url_mns": "b" if mns else None}
    return feat


@pytest.mark.parametrize(
    "tiles, share",
    [
        ([_tile(0, 0), _tile(1, 0)], 1.0),  # celý výřez pokrytý
        ([_tile(0, 0)], 0.5),  # polovina (díra jako Lille)
        ([_tile(0, 0), _tile(1, 0, mns=False)], 0.5),  # dlaždice bez MNS se nepočítá
        ([], 0.0),
    ],
)
def test_lidarhd_coverage_share(tiles, share):
    urls = []

    def fake(url):
        urls.append(url)
        return {"features": tiles}

    assert elevation_raster.fr_lidarhd_coverage((0.0, 0.0, 2.0, 1.0), fetch=fake) == pytest.approx(share)
    bbox = urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]).query)["BBOX"][0]
    assert bbox.startswith("0.000000,0.000000,1.000000,2.000000,")  # jih, západ, sever, východ (EPSG::4326)


def test_elevation_coverage_never_blocks_on_error(monkeypatch):
    def broken(_bbox):
        raise OSError("WFS nedostupná")

    monkeypatch.setitem(elevation_raster.COVERAGE, "fr_lidarhd", broken)
    monkeypatch.setattr(elevation_raster, "_COVERAGE_CACHE", {})
    assert elevation_raster.elevation_coverage("fr_lidarhd", (3.0, 50.6, 3.1, 50.65)) is None
    assert elevation_raster.elevation_coverage("nl_ahn", (5.0, 52.0, 5.1, 52.1)) is None  # celoplošný zdroj


@pytest.mark.parametrize(
    "bbox, source",
    [
        ((2.350, 48.855, 2.362, 48.861), "fr_pdv"),  # Marais
        ((4.825, 45.754, 4.840, 45.762), "fr_bdtopo"),  # Lyon: Plan de voirie jen Paříž, jinde BD TOPO
    ],
)
def test_pdv_only_in_paris(bbox, source):
    from app.pipeline import country_profile

    assert country_profile.technical_map_source({"country": "FR", "bbox_wgs84": list(bbox)}) == source


def test_api_rejects_area_without_lidarhd(client, monkeypatch):
    import app.main as main

    monkeypatch.setenv("COUNTRIES_BETA", "FR")
    monkeypatch.setenv("SITES", "testserver=cs:free:*")  # beta jen na mezinárodní doméně
    monkeypatch.setattr(main, "check_create_job", lambda *_a, **_k: None)
    monkeypatch.setitem(elevation_raster.COVERAGE, "fr_lidarhd", lambda _bbox: 0.1)
    monkeypatch.setattr(elevation_raster, "_COVERAGE_CACHE", {})
    bbox = "3.050,50.625,3.070,50.640"  # Lille
    body = client.get("/api/sheets", params={"bbox": bbox}).json()
    assert body["country"] == "FR" and body["too_large"] and body["too_large_reason"] == "elevation"
    assert body["elevation_share"] == 0.1 and "10 %" in body["hint"]
    r = client.post("/api/jobs", data={"preset_id": "sprint_2m", "output_mode": "png_zip", "name": "lille", "bbox": bbox})
    assert r.status_code == 400 and "LiDAR HD" in r.text
