"""Andalusie a Katalánsko: profily, hranice, sondy a výšky z regionálních COG."""

import pytest

from app import countries, source_status
from app.pipeline import elevation_raster as er
from app.pipeline import online_templates, open_paved, reference_es


@pytest.mark.parametrize(
    "code,key,crs,probe",
    [("ES-AN", "es_an", "EPSG:25830", "an_elevation"), ("ES-CT", "es_ct", "EPSG:25831", "ct_elevation")],
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
    er.cog_windows(["https://x/a.tif"], (0, 0, 10, 10), "EPSG:25830", tmp_path, "t", gdal_config={"GDAL_HTTP_AUTH": "BASIC"})
    cmd = seen[0]
    i = cmd.index("GDAL_HTTP_AUTH")
    assert cmd[i - 1] == "--config" and cmd[i + 1] == "BASIC" and cmd.index("GDAL_HTTP_AUTH") < cmd.index("-projwin")
