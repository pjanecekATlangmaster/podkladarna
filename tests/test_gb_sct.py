"""Skotsko: dlaždice LiDARu ve mřížce OS z výpisu S3 bucketu (index v cache), pořadí fází a profil země."""

import pytest

from app import countries, settings, source_status
from app.pipeline import elevation_raster as er
from app.pipeline import reference_gb


@pytest.mark.parametrize(
    "ref,box",
    [
        ("NT27", (320000.0, 670000.0, 330000.0, 680000.0)),  # 10 km (Phase 1–2)
        ("NT27NE", (325000.0, 675000.0, 330000.0, 680000.0)),  # 5 km (Phase 3–6)
        ("NG2104", (121000.0, 804000.0, 122000.0, 805000.0)),  # 1 km (NLP; roh ověřen gdalinfo)
        ("HY20", (320000.0, 1000000.0, 330000.0, 1010000.0)),  # Orkney
        ("NT2", None),
        ("XX27", None),
        ("NT", None),
    ],
)
def test_os_ref_bounds(ref, box):
    assert er.os_ref_bounds(ref) == box


def _listing(keys, token=None):
    body = "".join(f"<Contents><Key>{k}</Key><Size>1</Size></Contents>" for k in keys)
    nxt = f"<NextContinuationToken>{token}</NextContinuationToken>" if token else ""
    return f"<ListBucketResult>{body}{nxt}</ListBucketResult>"


def test_index_cached_and_newer_phase_last(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path)
    calls = []

    def fetch(url):
        calls.append(url)
        if "prefix=lidar%2Fphase-1%2Fdtm" in url:
            return _listing(["lidar/phase-1/dtm/27700/gridded/NT27_1M_DTM_PHASE1.tif"])
        if "prefix=lidar%2Fnational-lidar-programme%2Fdtm" in url and "continuation-token" not in url:
            return _listing(["lidar/national-lidar-programme/dtm/27700/gridded/NT2173_50cm_DTM_ScotlandNationalLiDAR.tif"], "T1")
        if "continuation-token=T1" in url:
            return _listing(["lidar/national-lidar-programme/dtm/27700/gridded/NT9999_50cm_DTM_ScotlandNationalLiDAR.tif",
                             "lidar/national-lidar-programme/dtm/27700/gridded/readme.txt"])
        return _listing([])

    index = er.sct_index(fetch=fetch)
    assert len(index["national-lidar-programme"]["dtm"]) == 2  # druhá stránka výpisu, ne-TIF vynechán
    assert er.sct_index(fetch=lambda u: pytest.fail("index z cache")) == index
    tiles = er.sct_tiles("dtm", (321000.0, 673000.0, 321500.0, 673500.0), index)
    assert [f for f, _, _ in tiles] == ["phase-1", "national-lidar-programme"]  # novější poslední = ve VRT vyhrává
    assert er.sct_tiles("dtm", (500000.0, 500000.0, 501000.0, 501000.0), index) == []


def test_index_empty_listing_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path)
    with pytest.raises(RuntimeError):
        er.sct_index(fetch=lambda u: _listing([]))
    assert not (tmp_path / "gb_sct" / "index.json").exists()


def test_no_hes_datasets():
    assert not any(f.startswith("hes") for f, _ in er.SCT_DATASETS)  # HES zčásti NCGL


def test_scotland_profile_and_probe():
    sct = countries.load_countries()["GB-SCT"]
    assert sct.elevation == "gb_sct" and sct.crs == "EPSG:27700" and not sct.cz_sources
    assert "Scottish Government" in sct.citation and "gb_sct" in er.COVERAGE
    assert sct.green == "plain"  # bez zrnitosti: les na reálné mapě převážně bílý
    probe = next(p for p in source_status.PROBES if p.key == "sct_elevation")
    assert probe.countries == ("GB-SCT",) and probe.fatal and probe.urls == (reference_gb.SCT_ELEVATION_PROBE,)


def test_valid_mask_counts_only_data(tmp_path):
    """Pokrytí podle platných pixelů, ne obdélníku dlaždice (Phase 1 bývá ve čtverci jen zčásti)."""
    import shutil

    import numpy as np

    pytest.importorskip("osgeo")
    if not shutil.which("gdalwarp"):
        pytest.skip("gdalwarp není k dispozici")
    arr = np.full((100, 100), -9999.0, dtype="float32")
    arr[:, :25] = 300.0  # jen levá čtvrtina má data
    path = tmp_path / "t.tif"
    er._write_tif(path, arr, 320000.0, 671000.0, "EPSG:27700")
    share = er.sct_valid_mask([str(path)], (320000.0, 670900.0, 320100.0, 671000.0), cell_m=5.0).mean()
    assert abs(share - 0.25) < 0.05
