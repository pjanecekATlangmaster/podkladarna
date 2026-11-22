"""Andalusie, Katalánsko a Valencie: profily, hranice, sondy a výšky z regionálních rastrů."""

import pytest

from app import countries, source_status
from app.pipeline import elevation_raster as er
from app.pipeline import online_templates, open_paved, reference_es


@pytest.mark.parametrize(
    "code,key,crs,probe",
    [
        ("ES-AN", "es_an", "EPSG:25830", "an_elevation"),
        ("ES-CT", "es_ct", "EPSG:25831", "ct_elevation"),
        ("ES-VC", "es_vc", "EPSG:25830", "vc_elevation"),
    ],
)
def test_profile_and_probe(code, key, crs, probe):
    c = countries.load_countries()[code]
    assert c.elevation == key and c.crs == crs and not c.cz_sources and c.status == "beta"
    assert key in er.PROVIDERS and key not in er.COVERAGE
    p = next(p for p in source_status.PROBES if p.key == probe)
    assert p.countries == (code,) and p.fatal and p.method == "HEAD"
    assert code in next(p for p in source_status.PROBES if p.key == "es_ortho").countries
    assert [w[0] for w in online_templates.wms_layers_for(code)] == ["es_ortho"]
    assert open_paved.ORTHO_KEYS[code] == "es_ortho"


@pytest.mark.parametrize(
    "bbox,code",
    [
        ((-6.00, 37.38, -5.98, 37.40), "ES-AN"),  # Sevilla
        ((-3.43, 37.05, -3.42, 37.06), "ES-AN"),  # Sierra Nevada
        ((2.16, 41.38, 2.18, 41.40), "ES-CT"),  # Barcelona
        ((1.975, 42.455, 1.985, 42.465), "ES-CT"),  # Llívia (enkláva ve Francii; FR ji kryje také, ES-CT je v pořadí dřív)
        ((-1.65, 42.80, -1.63, 42.82), "ES-NC"),  # Pamplona
        ((-0.775, 38.625, -0.755, 38.640), "ES-VC"),  # Biar (WRE 2027)
        ((-0.39, 39.46, -0.37, 39.48), "ES-VC"),  # Valencia
        ((-1.20, 40.05, -1.18, 40.07), "ES-VC"),  # Rincón de Ademuz (exkláva)
    ],
)
def test_boundary(bbox, code, monkeypatch):
    # Skutečný výběr aplikace (pořadí v countries.yaml rozhoduje při shodě), všechny beta země na doméně se vším.
    monkeypatch.setenv("COUNTRIES_BETA", "*")
    site = type("Site", (), {"countries": ("*",)})()
    assert countries.check_bbox(bbox, site)[0].code == code


@pytest.mark.parametrize(
    "key,crs,urls,auth",
    [
        ("es_an", "EPSG:25830", (reference_es.AN_DTM_URL, reference_es.AN_DSM_URL), True),
        ("es_ct", "EPSG:25831", (reference_es.CT_DTM_URL, reference_es.CT_DSM_URL), False),
    ],
)
def test_provider_reads_both_cogs(tmp_path, monkeypatch, key, crs, urls, auth):
    calls = []

    def fake(u, window, c, out_dir, prefix, **kw):
        calls.append((u, c, prefix, kw))
        return tmp_path / f"{prefix}.vrt"

    monkeypatch.setattr(er, "cog_windows", fake)
    res = er.PROVIDERS[key]((0, 0, 1, 1), tmp_path)
    assert [c[0] for c in calls] == [[urls[0]], [urls[1]]]
    assert all(c[1] == crs and c[3].get("nodata") is None for c in calls)  # NoData podle souboru (Andalusie MDT 255)
    cfg = calls[0][3].get("gdal_config")
    assert (cfg == reference_es.AN_GDAL_CONFIG) if auth else not cfg
    assert res.dtm.name == f"{key}_dtm.vrt" and res.dsm.name == f"{key}_dsm.vrt"


@pytest.mark.parametrize("key", ["es_an", "es_ct"])
def test_provider_outside_raises(tmp_path, monkeypatch, key):
    monkeypatch.setattr(er, "cog_windows", lambda *a, **k: None)
    with pytest.raises(er.ElevationUnavailable):
        er.PROVIDERS[key]((0, 0, 1, 1), tmp_path)


def test_cog_windows_passes_gdal_config(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(er, "run_cmd", lambda cmd, log=None: seen.append(cmd))
    monkeypatch.setattr(er, "_gdal_tool", lambda name: name)  # CI bez GDAL
    er.cog_windows(["https://x/a.tif"], (0, 0, 10, 10), "EPSG:25830", tmp_path, "t", gdal_config={"GDAL_HTTP_AUTH": "BASIC"})
    cmd = seen[0]
    i = cmd.index("GDAL_HTTP_AUTH")
    assert cmd[i - 1] == "--config" and cmd[i + 1] == "BASIC" and cmd.index("GDAL_HTTP_AUTH") < cmd.index("-projwin")


def test_andalusia_green_like_portugal():
    # Livelox La Breña 2026-11-22: výchozí zeleň 85 % lesa proti 15 % u mapaře (pobřežní borovice jako PT)
    all_c = countries.load_countries()
    an, pt, ct = all_c["ES-AN"], all_c["PT"], all_c["ES-CT"]
    assert (an.green, an.green_share, an.green_low_ratio) == (pt.green, pt.green_share, pt.green_low_ratio) == ("understory", 0.25, 0.45)
    assert ct.green != "understory" and ct.green_share == 0.0


def _vc_wfs(features):
    import json

    return json.dumps({"type": "FeatureCollection", "features": features})


def test_vc_index_and_tiles(tmp_path, monkeypatch):
    monkeypatch.setattr(er.settings, "CACHE_DIR", tmp_path)
    sheet = lambda x0, link: {  # noqa: E731
        "type": "Feature",
        "properties": {"hoja": "0846", "link_tif": link},
        "geometry": {"type": "Polygon", "coordinates": [[[x0, 0], [x0 + 100, 0], [x0 + 100, 50], [x0, 50], [x0, 0]]]},
    }
    seen = []

    def fetch(url):
        seen.append(url)
        kind = "mdt" if "/0302" in url else "mds"
        return _vc_wfs([sheet(0, f"https://x/{kind}_a.tif"), sheet(1000, f"https://x/{kind}_b.tif")])

    idx = er.vc_index(fetch=fetch)
    assert len(seen) == 2 * len(reference_es.VC_PROVINCES)
    assert all("srsName=EPSG%3A25830" in u and "typeNames=distribucion_descargas" in u for u in seen)
    assert idx["dtm"][0] == [[0, 0, 100, 50], "https://x/mdt_a.tif"]
    assert er.vc_tiles("dtm", (10, 10, 20, 20), idx) == ["https://x/mdt_a.tif"] * len(reference_es.VC_PROVINCES)
    assert er.vc_tiles("dsm", (500, 10, 600, 20), idx) == []
    # podruhé z cache
    assert er.vc_index(fetch=lambda u: (_ for _ in ()).throw(AssertionError(u))) == idx


def test_vc_provider(tmp_path, monkeypatch):
    idx = {"dtm": [[[0, 0, 10, 10], "https://x/mdt.tif"]], "dsm": [[[0, 0, 10, 10], "https://x/mds.tif"]]}
    monkeypatch.setattr(er, "vc_index", lambda: idx)
    monkeypatch.setattr(er, "_buffered", lambda b, crs, m: (1, 1, 2, 2))
    from app.pipeline import strip_tiff

    def fake_fetch(url, window, dest, log=None):
        dest.write_bytes(b"x")
        return dest

    monkeypatch.setattr(strip_tiff, "fetch_window", fake_fetch)
    calls = []

    def fake(u, window, c, out_dir, prefix, **kw):
        calls.append(([url_of[p] for p in u], c, kw))
        return tmp_path / f"{prefix}.vrt"

    url_of = {str(tmp_path / "es_vc_dtm_src0.tif"): "https://x/mdt.tif", str(tmp_path / "es_vc_dsm_src0.tif"): "https://x/mds.tif"}

    monkeypatch.setattr(er, "cog_windows", fake)
    res = er.PROVIDERS["es_vc"]((0, 0, 1, 1), tmp_path)
    assert [c[0] for c in calls] == [["https://x/mdt.tif"], ["https://x/mds.tif"]]
    assert all(c[1] == "EPSG:25830" for c in calls)
    assert res.dtm.name == "es_vc_dtm.vrt" and res.dsm.name == "es_vc_dsm.vrt"
    assert not list(tmp_path.glob("es_vc_*_src*.tif"))  # řídké kopie po výřezu smazané
    monkeypatch.setattr(er, "_buffered", lambda b, crs, m: (50, 50, 60, 60))
    with pytest.raises(er.ElevationUnavailable):
        er.PROVIDERS["es_vc"]((0, 0, 1, 1), tmp_path)


def test_strip_tiff_window_matches_source(tmp_path):
    """Řídká kopie s hlavičkou a řádky okna čte v okně totéž co celý soubor (strip po 1 řádku, LZW, IFD na konci)."""
    np = pytest.importorskip("numpy")
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin
    from rasterio.windows import from_bounds

    from app.pipeline import strip_tiff

    src = tmp_path / "list.tif"
    data = np.random.default_rng(1).uniform(200, 900, (300, 400)).astype("float32")  # nestlačitelné: řádky mimo hlavičku
    with rasterio.open(src, "w", driver="GTiff", width=400, height=300, count=1, dtype="float32", crs="EPSG:25830",
                       transform=from_origin(700000, 4280000, 1, 1), compress="lzw", blockysize=1, nodata=-9999) as ds:
        ds.write(data, 1)
    blob = src.read_bytes()
    fetched = []

    def fetch(url, start, end, sink=None):
        fetched.append(end - start + 1)
        if sink is not None:
            sink.seek(start)
            sink.write(blob[start : end + 1])
            return b"", len(blob)
        return blob[start : end + 1], len(blob)

    win = (700050, 4279850, 700150, 4279900)
    out = strip_tiff.fetch_window("https://x/list.tif", win, tmp_path / "okno.tif", fetch=fetch)
    with rasterio.open(out) as ds:
        got = ds.read(1, window=from_bounds(*win, ds.transform))
    assert np.array_equal(got, data[100:150, 50:150])
    assert len(fetched) >= 2 and sum(fetched) < len(blob) / 2
    assert strip_tiff.fetch_window("https://x/list.tif", (0, 0, 10, 10), tmp_path / "mimo.tif", fetch=fetch) is None
