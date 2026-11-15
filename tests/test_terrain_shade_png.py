import numpy as np
import pytest

from app.pipeline import terrain_shade_png as ts
from app.pipeline.georef import read_pgw
from app.pipeline.oom_layers import FOREIGN_VEGETATION_LABEL, collect_oom_templates


@pytest.mark.parametrize(
    "sign, bright",
    [
        (1.0, True),  # terén stoupá k jihovýchodu: svah obrácený k severozápadu, ke slunci (azimut 315°)
        (-1.0, False),  # svah k jihovýchodu je ve stínu
    ],
)
def test_hillshade_lit_from_northwest(sign, bright):
    rows, cols = np.mgrid[0:20, 0:20].astype(np.float32)  # řádky k jihu, sloupce k východu
    gray, valid = ts.hillshade(sign * (rows + cols), 1.0, -1.0)
    flat, _ = ts.hillshade(np.zeros((20, 20), np.float32), 1.0, -1.0)
    assert valid.all()
    assert bool(gray.mean() > flat.mean()) is bright


def test_terrain_shades_png_georef_and_templates(tmp_path, monkeypatch):
    """Z DTM i DSM jobu vznikne PNG s .pgw (0,5 m → 1 m/px, NoData průhledné) a skrytá šablona v .omap."""
    elev = np.add.outer(np.arange(8, dtype=np.float32), np.arange(8, dtype=np.float32))
    elev[0, 0] = -9999.0
    gt = (380000.0, 0.5, 0.0, 6680000.0, 0.0, -0.5)
    dem_dir = tmp_path / "dem"
    dem_dir.mkdir()
    for _key, src, *_rest in ts.TERRAIN_SHADES:
        (dem_dir / src).write_bytes(b"x")
    monkeypatch.setattr(ts, "read_float32_geotiff", lambda path, log=None: (elev.copy(), gt, -9999.0))
    built = ts.write_terrain_shades(dem_dir, tmp_path / "references")
    assert set(built) == {"hillshade_dtm", "hillshade_dsm"}
    from PIL import Image

    img = Image.open(built["hillshade_dtm"])
    assert img.size == (4, 4) and img.mode == "L"  # blok 2×2 s jednou NoData buňkou je platný
    pgw = read_pgw(built["hillshade_dsm"].with_suffix(".pgw"))
    assert (pgw.origin_x, pgw.origin_y, pgw.pixel_x, pgw.pixel_y) == (380000.5, 6679999.5, 1.0, -1.0)

    labels = {t.relpath: t for t in collect_oom_templates(tmp_path, built_refs=built)}
    assert {"references/hillshade_dtm.png", "references/hillshade_dsm.png"} <= set(labels)
    assert not labels["references/hillshade_dsm.png"].visible

    # Chybějící rastr (job bez DSM) se přeskočí, prázdný rastr také.
    (dem_dir / "dsm_filled.tif").unlink()
    monkeypatch.setattr(
        ts, "read_float32_geotiff", lambda path, log=None: (np.full((4, 4), np.nan, np.float32), gt, None)
    )
    assert ts.write_terrain_shades(dem_dir, tmp_path / "prazdne") == {}


@pytest.mark.parametrize("country, foreign_label", [("CZ", False), ("FI", True)])
def test_vegetation_height_label_by_country(tmp_path, monkeypatch, country, foreign_label):
    from app.pipeline.country_profile import JOB_COUNTRY_ENV

    monkeypatch.setenv(JOB_COUNTRY_ENV, country)
    png = tmp_path / "vyska_vegetace.png"
    png.write_bytes(b"x")
    (veg,) = [t for t in collect_oom_templates(tmp_path, built_refs={"vyska_vegetace": png})]
    assert (veg.label == FOREIGN_VEGETATION_LABEL) is foreign_label
