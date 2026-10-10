import zipfile

import pytest

from app.pipeline.oom_symbols import _OCAD_SYMBOL_SETS, ocad_symbol_set_path
from app.pipeline.package_oom import _write_ocad_symbol_sets


def test_every_key_has_existing_ocad_file():
    for (disc, scale), name in _OCAD_SYMBOL_SETS.items():
        path = ocad_symbol_set_path(disc, scale)
        assert path is not None and path.name == name, (disc, scale)
        assert path.read_bytes()[:2] == b"\xad\x0c"  # OCAD hlavička


def test_unknown_key_is_none():
    assert ocad_symbol_set_path("sprint", 10000) is None
    assert ocad_symbol_set_path("xyz", 4000) is None


@pytest.mark.parametrize(
    "scale, tags, expected",
    [
        (4000, ["sprint"], {"ISSprOM_2019_4000.ocd"}),
        (7500, ["les", "mtbo"], {"ISOM_2017_10000.ocd", "ISMTBOM_2022_7500.ocd"}),
        (15000, ["les", "mtbo"], {"ISOM_2017_15000.ocd", "ISMTBOM_2022_15000.ocd"}),
    ],
)
def test_zip_gets_matching_sets(tmp_path, scale, tags, expected):
    omaps = []
    for tag in tags:
        p = tmp_path / f"mapa-{tag}.omap"
        p.write_text("x")
        omaps.append(p)
    zpath = tmp_path / "o.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        _write_ocad_symbol_sets(zf, omaps, {"scale": scale})
    with zipfile.ZipFile(zpath) as zf:
        names = {n.removeprefix("ocad_symboly/") for n in zf.namelist()}
    assert names == expected
