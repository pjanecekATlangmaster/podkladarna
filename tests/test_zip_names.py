"""Anglické názvy souborů a složek v anglickém ZIPu (zip_names); český ZIP beze změny."""

import pytest

from app.pipeline.zip_names import EN_STEMS, localize_arcname


@pytest.mark.parametrize(
    "arc, lang, expected",
    [
        ("references/vyska_vegetace.png", "cs", "references/vyska_vegetace.png"),
        ("osm/OSM_budovy.shp", "cs", "osm/OSM_budovy.shp"),
        ("uzitecne/pouzite/srazy_104.shp", "cs", "uzitecne/pouzite/srazy_104.shp"),
        ("references/vyska_vegetace.pgw", "en", "references/vegetation_height.pgw"),
        ("references/katastr.gpkg", "en", "references/cadastre.gpkg"),
        ("references/ortofoto_cuzk_stredni.xml", "en", "references/orthophoto_cuzk_medium.xml"),
        ("references/ortho_pnoa_stredni.xml", "en", "references/ortho_pnoa_medium.xml"),
        ("references/hillshade_dtm.png", "en", "references/hillshade_dtm.png"),
        ("osm/OSM_budovy.shx", "en", "osm/OSM_buildings.shx"),
        ("osm/OSM_residential_zbytek_stredni.dbf", "en", "osm/OSM_residential_rest_medium.dbf"),
        ("osm/README.txt", "en", "osm/README.txt"),
        ("uzitecne/vyhozene/vegetace.shp", "en", "useful/filtered_out/vegetation.shp"),
        ("ocad_symboly/ISOM_2017_10000.ocd", "en", "ocad_symbols/ISOM_2017_10000.ocd"),
        ("zabaged/Cesta.shp", "en", "zabaged/Cesta.shp"),
    ],
)
def test_localize_arcname(arc, lang, expected):
    assert localize_arcname(arc, lang) == expected


def test_all_osm_layers_have_english_name():
    """Nová OSM vrstva pro ruční skládání potřebuje anglický název (jinak by v anglickém ZIPu zůstala česky)."""
    from app.pipeline.osm_paths import OSM_MANUAL_LAYER_SPECS
    from app.pipeline.residual_paved import RESIDUAL_SHP_BANDS, RESIDUAL_SPARSE_STEM

    english_already = {"OSM_fitness"}
    stems = {spec[0] for spec in OSM_MANUAL_LAYER_SPECS.values() if spec[0].startswith("OSM_")}
    stems |= {band[0] for band in RESIDUAL_SHP_BANDS} | {RESIDUAL_SPARSE_STEM, "OSM_budovy", "OSM_cesty"}
    assert sorted(stems - english_already - set(EN_STEMS)) == []


def test_omap_templates_follow_zip_names(tmp_path, monkeypatch):
    """Šablony .omap v anglickém jobu míří na anglické názvy v ZIPu, v českém beze změny."""
    from app.pipeline.country_profile import JOB_LANG_ENV
    from app.pipeline.oom_layers import collect_oom_templates

    png = tmp_path / "vyska_vegetace.png"
    png.write_bytes(b"x")
    monkeypatch.setenv(JOB_LANG_ENV, "cs")
    (cs,) = collect_oom_templates(tmp_path, built_refs={"vyska_vegetace": png})
    monkeypatch.setenv(JOB_LANG_ENV, "en")
    (en,) = collect_oom_templates(tmp_path, built_refs={"vyska_vegetace": png})
    assert cs.relpath == "references/vyska_vegetace.png"
    assert en.relpath == "references/vegetation_height.png" and en.filename == "vegetation_height.png"
