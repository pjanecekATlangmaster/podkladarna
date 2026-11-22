"""Stálezelený porost ze zimního NDVI Sentinel-2 (zimní výšky ``green: leafoff``)."""
from datetime import date

import numpy as np
import pytest

from app.pipeline import sentinel2 as s2
from app.pipeline import winter_evergreen as we

WKT_UTM31 = 'PROJCS["WGS 84 / UTM zone 31N",GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",0],PARAMETER["central_meridian",3],PARAMETER["scale_factor",0.9996],PARAMETER["false_easting",500000],PARAMETER["false_northing",0],UNIT["metre",1]]'
GT = (600000.0, 1.0, 0.0, 5650000.0, 0.0, -1.0)


def test_winter_windows():
    assert we.winter_windows(date(2026, 11, 22)) == [
        (date(2025, 12, 1), date(2026, 2, 28)), (date(2024, 12, 1), date(2025, 2, 28)),
        (date(2023, 12, 1), date(2024, 2, 28))]
    # uprostřed zimy: letošní zima jen do dneška
    assert we.winter_windows(date(2026, 1, 10))[0] == (date(2025, 12, 1), date(2026, 1, 10))


def _scene(day):
    return s2.Scene(f"S2_{day}", day, 5.0, {"scl": f"scl_{day}", "red": f"red_{day}", "nir": f"nir_{day}"}, 0.0)


@pytest.mark.parametrize(
    "scl, nir, expected",
    [
        # levá půlka jehličí (NDVI 0,8), pravá holé buky (NDVI 0,56) → maska jen vlevo
        (4, (900, 350), (True, False)),
        # sníh / mraky ve snímku → bez masky (kresba jako dřív)
        (11, (900, 350), None),
    ],
)
def test_evergreen_mask(scl, nir, expected):
    day = date(2026, 2, 1)

    def read_band(href, res, alg):
        if href.startswith("scl"):
            return np.full((5, 5), scl, dtype=np.uint8)
        if href.startswith("red"):
            return np.full((10, 10), 100, dtype=np.uint16)
        out = np.full((10, 10), nir[1], dtype=np.uint16)
        out[:, :5] = nir[0]
        return out

    mask, got_day = we.evergreen_mask(GT, (100, 100), WKT_UTM31, today=date(2026, 3, 1),
                                      search=lambda *a, **k: [_scene(day)], read_band=read_band)
    if expected is None:
        assert mask is None and got_day is None
    else:
        assert mask.shape == (100, 100) and got_day == day
        assert bool(mask[50, 10]) is expected[0] and bool(mask[50, 90]) is expected[1]
