"""USA: mračno USGS 3DEP (EPT na AWS) podle indexu WESM – výběr projektů, uzly EPT, mřížka, UTM zóna, profil."""

import io
import json

import numpy as np
import pytest

from app import countries, settings, source_status
from app.pipeline import elevation_raster, reference_us
from app.pipeline.job_progress import SOURCE_ELEVATION


def _wesm(*rows):
    return {"features": [
        {"attributes": {"workunit": name, "ql": ql, "collect_end": end},
         "geometry": {"rings": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}}
        for name, ql, end, (w, s, e, n) in rows
    ]}


def test_utm_zone_from_bbox_centre():
    assert reference_us.utm_epsg(-122.25, 47.73) == 26910  # Seattle
    assert reference_us.utm_epsg(-74.1, 41.26) == 26918  # New York
    assert reference_us.utm_epsg(-157.9, 21.4) == 26904  # Havaj
    us = countries.load_countries()["US"]
    assert us.job_crs((-74.12, 41.25, -74.10, 41.27)) == "EPSG:26918"
    assert countries.load_countries()["SI"].job_crs((14.5, 46.0, 14.6, 46.1)) == "EPSG:3794"  # pevný CRS beze změny
    assert countries.load_countries()["CZ"].job_crs((14.4, 50.0, 14.5, 50.1)) == "EPSG:5514"  # Česko beze změny


def test_parse_wesm_keeps_quality_levels_newest_first():
    data = _wesm(("OLD_2012", "QL 2", 1_340_000_000_000, (0, 0, 1, 1)),
                 ("LEGACY", "Other", 1_700_000_000_000, (0, 0, 1, 1)),
                 ("SPARSE", "QL 3", 1_700_000_000_000, (0, 0, 1, 1)),
                 ("NEW_A22", "QL 1", 1_651_000_000_000, (0, 0, 1, 1)))
    assert [(w.name, w.year) for w in reference_us.parse_wesm(data)] == [("NEW_A22", 2022), ("OLD_2012", 2012)]


def test_ept_name_match_and_cached_index(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path)
    names = ["NY_SE4County_1_A22", "USGS_LPC_MD_VA_Sandy_NCR_2014_LAS_2015"]
    assert reference_us.match_ept("NY_SE4County_1_A22", names) == "NY_SE4County_1_A22"
    assert reference_us.match_ept("MD_VA_Sandy_NCR_2014", names) == "USGS_LPC_MD_VA_Sandy_NCR_2014_LAS_2015"
    assert reference_us.match_ept("MD_VA_Sandy", names) is None  # jen celé jméno projektu
    page = b"<ListBucketResult><CommonPrefixes><Prefix>A_2020/</Prefix></CommonPrefixes></ListBucketResult>"
    assert reference_us.ept_names(fetch=lambda url: page) == ["A_2020"]
    assert reference_us.ept_names(fetch=lambda url: pytest.fail("z cache")) == ["A_2020"]


def test_candidates_skip_missing_ept_and_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path)
    wesm = _wesm(("NEW_D24", "QL 1", 1_740_000_000_000, (0.0, 0.0, 1.0, 1.0)),
                 ("HALF_2018", "QL 2", 1_530_000_000_000, (0.0, 0.0, 0.5, 1.0)))
    bucket = b"<ListBucketResult><CommonPrefixes><Prefix>HALF_2018/</Prefix></CommonPrefixes></ListBucketResult>"

    def fetch(url):
        return json.dumps(wesm).encode() if "index.nationalmap.gov" in url else bucket

    cands = reference_us.candidates((0.0, 0.0, 1.0, 1.0), fetch=fetch)
    assert [name for name, _wu in cands] == ["HALF_2018"]  # nejnovější projekt v bucketu chybí
    assert reference_us.coverage_share((0.0, 0.0, 1.0, 1.0), cands) == pytest.approx(0.5)
    assert reference_us.coverage_share((0.0, 0.0, 1.0, 1.0), []) == 0.0


def _footprints(*rows):
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": name, "count": count},
         "geometry": {"type": "MultiPolygon", "coordinates": [[[[w, s], [e, s], [e, n], [w, n], [w, s]]]]}}
        for name, count, (w, s, e, n) in rows
    ]}


def test_candidates_from_ept_footprints(tmp_path, monkeypatch):
    """New York: mračno města ve WESM není a projekt WESM (Sandy 2013) v bucketu chybí – rozhodne obrys mračna."""
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(reference_us, "_FOOTPRINTS", {})
    monkeypatch.setattr(reference_us, "_PENDING", {})
    cell = (-74.0, 40.7, -73.99, 40.71)  # ~0,94 km²
    km2 = 0.94e6
    wesm = _wesm(("NY_CMPG_2013", "QL 2", 1_398_000_000_000, cell),
                 ("OLD_OTHER_2009", "Other", 1_240_000_000_000, cell))
    names = {"OLD_OTHER_2009": "Other"}  # obrys WESM výřez míjí, kvalitu najde dotaz podle jména
    foot = _footprints(("NY_NewYorkCity", int(4 * km2), cell),  # mimo WESM, 4 b/m² → bere se
                       ("IA_FullState", int(1.1 * km2), cell),  # mimo WESM, řídké → ne
                       ("USGS_LPC_OLD_OTHER_2009_LAS_2012", int(9 * km2), cell),  # WESM „Other“ → ne
                       ("FAR_2020", int(9 * km2), (-100.0, 30.0, -99.9, 30.1)))  # mimo výřez
    bucket = b"<ListBucketResult><CommonPrefixes><Prefix>NY_NewYorkCity/</Prefix></CommonPrefixes></ListBucketResult>"
    calls = []

    def fetch(url):
        calls.append(url)
        if "githubusercontent" in url:
            return json.dumps(foot).encode()
        if "index.nationalmap.gov" in url:
            if "UPPER" in url:
                assert "IA_FULLSTATE" in url and "NY_NEWYORKCITY" in url
                return json.dumps({"features": [{"attributes": {"workunit": k, "ql": v}} for k, v in names.items()]}).encode()
            return json.dumps(wesm if "1%3D1" in url else _wesm()).encode()
        return bucket

    cands = reference_us.candidates(cell, fetch=fetch)
    assert [(n, wu.year) for n, wu in cands] == [("NY_NewYorkCity", 0)]
    assert reference_us.coverage_share(cell, cands) == pytest.approx(1.0)
    assert reference_us.pending_workunits(cell) == ["NY_CMPG_2013 (2014)"]  # nasnímáno, mračno zatím chybí
    n = len(calls)
    reference_us.candidates(cell, fetch=fetch)
    assert not any("githubusercontent" in u for u in calls[n:])  # obrysy z cache
    assert reference_us._name_year("GA_Central_1_2018") == 2018 and reference_us._name_year("UT_WestEast_1_B22") == 2022
    assert reference_us.ept_workunit("USGS_LPC_NY_LongIsland_Z18_2014_LAS_2015") == "NY_LongIsland_Z18_2014"


def test_ept_nodes_window_and_depth_limit():
    ept = {"bounds": [0, 0, 0, 1024, 1024, 1024], "span": 128}
    hier = {"0-0-0-0": 10, "1-0-0-0": 5, "1-1-0-0": 5, "2-0-0-0": -1, "3-0-0-0": 7}
    sub = {"2-0-0-0": 4, "3-0-0-0": 3}
    asked = []

    def fetch(url):
        asked.append(url)
        return json.dumps(sub if url.endswith("2-0-0-0.json") else hier).encode()

    # okno v levém dolním rohu; na rovníku má úroveň 3 rozestup 1024 / 8 / 128 = 1 m (nad limitem 0,3 m)
    nodes = elevation_raster.ept_nodes("B", ept, (10.0, 10.0, 100.0, 100.0), 0.0, fetch=fetch)
    assert nodes == [("0-0-0-0", 10), ("1-0-0-0", 5), ("2-0-0-0", 4), ("3-0-0-0", 3)]
    assert "B/ept-hierarchy/2-0-0-0.json" in asked  # podstrom -1 se dočetl
    north = elevation_raster.ept_nodes("B", ept, (10.0, 10.0, 100.0, 100.0), 75.0, fetch=fetch)
    assert [k for k, _c in north] == ["0-0-0-0", "1-0-0-0", "2-0-0-0"]  # cos 75° → úroveň 3 jen 0,26 m na zemi


def test_grid_ground_mean_and_surface_max():
    grid = elevation_raster._UsGrid(100.0, 200.0, 3, 2)
    x = np.array([100.2, 100.7, 100.5, 100.5, 100.5, 101.5, 102.5])
    y = np.array([199.5, 199.5, 199.5, 199.5, 199.5, 199.5, 198.5])
    z = np.array([300.0, 302.0, 320.0, 999.0, 340.0, 305.0, 310.0])
    cls = np.array([2, 2, 1, 7, 1, 1, 2], dtype=np.uint8)
    withheld = np.array([0, 0, 0, 0, 1, 0, 0], dtype=bool)
    grid.add(x, y, z, cls, withheld)
    dtm, dsm = grid.grids()
    assert dtm.shape == (2, 3) and dtm[0, 0] == pytest.approx(301.0)  # průměr bodů země
    assert dsm[0, 0] == pytest.approx(320.0)  # maximum mimo šum 7 a vyřazený bod
    assert dtm[0, 1] == elevation_raster.NODATA and dsm[0, 1] == pytest.approx(305.0)  # bez země jen povrch
    assert dtm[1, 2] == pytest.approx(310.0) and dsm[1, 0] == elevation_raster.NODATA


def test_las_arrays_reads_classification_and_withheld():
    laspy = pytest.importorskip("laspy")
    header = laspy.LasHeader(point_format=6, version="1.4")
    header.scales = [0.001, 0.001, 0.001]
    las = laspy.LasData(header)
    las.x, las.y, las.z = np.array([1.0, 2.0]), np.array([3.0, 4.0]), np.array([5.0, 6.0])
    las.classification = np.array([2, 18], dtype=np.uint8)
    buf = io.BytesIO()
    las.write(buf, do_compress=False)
    x, y, z, cls, withheld = elevation_raster._las_arrays(buf.getvalue())
    assert list(cls) == [2, 18] and not withheld.any() and z[1] == pytest.approx(6.0)


def test_united_states_profile_and_probes():
    us = countries.load_countries()["US"]
    assert us.elevation == "us_3dep" and us.crs == countries.UTM_NAD83 and not us.cz_sources
    assert us.technical_map == "none" and us.status == "beta" and "USGS" in us.citation
    assert countries.elevation_ready(us)
    assert "us_3dep" in elevation_raster.PROVIDERS and "us_3dep" in elevation_raster.COVERAGE
    probes = {p.key: p for p in source_status.PROBES if p.countries == ("US",)}
    assert set(probes) == {"us_index", "us_elevation"} and all(p.fatal for p in probes.values())
    assert probes["us_elevation"].urls == (reference_us.US_ELEVATION_PROBE,)
    assert all(source_status.probe_role(k) == SOURCE_ELEVATION for k in probes)  # brána jobu je hlídá


def test_small_dtm_gaps_filled_large_kept():
    nd = elevation_raster.NODATA
    grid = np.full((1, 12), 100.0, dtype="float32")
    grid[0, 1] = nd  # díra 1 m pod korunou
    grid[0, 3:12] = nd  # „budova“ 9 m na kraji
    out = elevation_raster.fill_small_gaps(grid, 4.0)
    assert out[0, 1] == 100.0 and out[0, 3] == 100.0 and out[0, 6] == 100.0  # do 4 m od země doplněno
    assert out[0, 7] == nd and out[0, 11] == nd  # dál zůstane bez dat (CHM 0, budova)


@pytest.mark.parametrize("code, calls, ok", [(503, 2, True), (404, 1, False)])
def test_get_retries_transient_errors_only(monkeypatch, code, calls, ok):
    import urllib.error

    seen = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"ok"

    def fake(req, timeout):
        seen.append(req.full_url)
        if len(seen) == 1:
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
        return _Resp()

    monkeypatch.setattr(reference_us.urllib.request, "urlopen", fake)
    if ok:
        assert reference_us._get("https://example.invalid/a", wait_s=0) == b"ok"
    else:
        with pytest.raises(urllib.error.HTTPError):
            reference_us._get("https://example.invalid/a", wait_s=0)
    assert len(seen) == calls
