"""Lichtenštejnsko: profil země nad výškami swisstopo, hranice a sondy sdílené se Švýcarskem."""

import pytest

from app import countries, source_status
from app.pipeline import elevation_raster as er
from app.pipeline import online_templates, open_paved


def test_li_profile_and_probes():
    li = countries.load_countries()["LI"]
    assert li.elevation == "ch_swisstopo" and li.crs == "EPSG:2056" and not li.cz_sources
    assert li.status == "beta" and "swisstopo" in li.citation
    assert "ch_swisstopo" in er.PROVIDERS
    for key in ("ch_swisstopo", "ch_wms"):
        probe = next(p for p in source_status.PROBES if p.key == key)
        assert "LI" in probe.countries and "CH" in probe.countries
    layers = online_templates.wms_layers_for("LI")
    assert layers and all(layer[0] != "ch_av" for layer in layers)  # švýcarská úřední mapa LI nepokrývá
    assert open_paved.ORTHO_KEYS["LI"] == "ch_swissimage"


@pytest.mark.parametrize(
    "bbox,code",
    [
        ((9.515, 47.135, 9.525, 47.145), "LI"),  # Vaduz
        ((9.600, 47.095, 9.612, 47.103), "LI"),  # Malbun
        ((9.470, 47.160, 9.480, 47.170), "CH"),  # Buchs SG
        ((9.590, 47.230, 9.600, 47.240), "AT"),  # Feldkirch
    ],
)
def test_li_boundary(bbox, code):
    all_c = countries.load_countries()
    best = max(("LI", "CH", "AT"), key=lambda c: countries.bbox_share(bbox, all_c[c]))
    assert best == code
