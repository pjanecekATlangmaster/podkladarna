"""Berlín (Straßenbefahrung + ALKIS) jako obdoba DTM a výšky DGM1 / DOM1: třídění, stránkování, dlaždice."""

from app.pipeline import elevation_raster, sb_berlin


def _feat(x0: float, y0: float, w: float = 10.0, *, fid: str, **props) -> dict:
    ring = [[x0, y0], [x0 + w, y0], [x0 + w, y0 + w], [x0, y0 + w], [x0, y0]]
    return {"type": "Feature", "id": fid, "geometry": {"type": "MultiPolygon", "coordinates": [[ring]]}, "properties": props}


def test_classify_berlin_like_dtm():
    data = {
        "cm_fahrbahn": [_feat(0, 0, 50, fid="f1")],
        "cl_gehweg": [_feat(0, 50, 50, fid="g1")],
        "ci_oeffentlicher_platz": [_feat(100, 0, 30, fid="p1")],
        "ck_parkflaeche": [_feat(60, 0, fid="k1")],
        "ch_radweg": [_feat(60, 20, fid="r1")],
        "cc_treppe": [_feat(60, 40, 3, fid="t1")],
        "ce_gruenflaeche": [_feat(200, 0, fid="z1")],
        "gebaeude": [
            _feat(0, 100, 20, fid="b1", bezeich="AX_Gebaeude", ofl=None),
            _feat(30, 100, 5, fid="b2", bezeich="AX_Bauteil", ofl=None),  # část budovy ne
            _feat(40, 100, 20, fid="b3", bezeich="AX_Gebaeude", ofl=1200),  # pod zemí ne
        ],
    }
    counts = {k: len(v) for k, v in sb_berlin.classify(data).items()}
    assert counts["roads"] == 1 and counts["sidewalks"] == 2 and counts["parking"] == 1 and counts["stairs"] == 1
    assert counts["small_paved"] == 1 and counts["green"] == 1 and counts["buildings"] == 1
    assert counts["public"] == 5 and counts["yards"] == 0


def test_fetch_pages_and_dedup(monkeypatch):
    monkeypatch.setattr(sb_berlin, "PAGE_SIZE", 2)
    calls = []

    def fake(url):
        calls.append(url)
        start = int(url.rsplit("STARTINDEX=", 1)[1])
        feats = [_feat(i, 0, fid=f"cl_gehweg.{i}") for i in range(start, min(start + 2, 3))]
        return {"features": feats}, 7

    data, nbytes = sb_berlin.fetch_layers((0, 0, 900, 400), [(sb_berlin.SB_WFS, "cl_gehweg")], tile_m=500, fetch=fake)
    assert len(data["cl_gehweg"]) == 3  # 2 dlaždice × 2 stránky, objekty podle id jen jednou
    assert len(calls) == 4 and nbytes == 28
    assert "strassenbefahrung%3Acl_gehweg" in calls[0] and "EPSG%3A%3A25833" in calls[0]


def test_berlin_elevation_tiles():
    assert elevation_raster.berlin_tiles((389500.0, 5818500.0, 391200.0, 5819900.0)) == [(388, 5818), (390, 5818)]
    assert elevation_raster.berlin_tile_url("dtm", 390, 5818).endswith("/dgm1/atom/DGM1_390_5818.zip")
    assert elevation_raster.berlin_tile_url("dsm", 390, 5818).endswith("/dom/atom/DOM1_390_5818.zip")
