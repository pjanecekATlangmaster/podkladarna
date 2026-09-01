from __future__ import annotations

from app.pipeline.fetch_zabaged import (
    drop_oversized_ostatni_plocha,
    query_layer_geojson,
    tag_features_with_layer,
)


def test_query_layer_paginates(monkeypatch):
    pages = [
        {
            "type": "FeatureCollection",
            "exceededTransferLimit": True,
            "features": [{"type": "Feature", "properties": {"i": 0}, "geometry": None}],
        },
        {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {"i": 1}, "geometry": None}],
        },
    ]

    def fake_http(url, timeout):
        offset = 0
        if "resultOffset=2000" in url:
            offset = 1
        return pages[offset]

    monkeypatch.setattr("app.pipeline.fetch_zabaged._http_json", fake_http)
    gj = query_layer_geojson("https://example.invalid/MapServer", 99, 14.4, 50.08, 14.42, 50.09)
    assert len(gj["features"]) == 2
    assert gj["features"][1]["properties"]["i"] == 1


def test_query_layer_empty(monkeypatch):
    monkeypatch.setattr(
        "app.pipeline.fetch_zabaged._http_json",
        lambda *a, **k: {"type": "FeatureCollection", "features": []},
    )
    gj = query_layer_geojson("https://example.invalid/MapServer", 99, 14.4, 50.08, 14.42, 50.09)
    assert gj["features"] == []


def test_ags_yaml_covers_prepare_layers():
    import yaml

    from app.settings import CONFIG_DIR

    prepare = yaml.safe_load((CONFIG_DIR / "zabaged_layers.yaml").read_text(encoding="utf-8"))["layers"]
    ags = yaml.safe_load((CONFIG_DIR / "zabaged_ags.yaml").read_text(encoding="utf-8"))["layers"]
    missing = [name for name in prepare if name not in ags]
    assert missing == [], missing
    extra = [name for name in ags if name not in prepare]
    assert extra == [], extra


def test_tag_features_with_layer():
    gj = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"fid_zbg": "1"}, "geometry": None},
            {"type": "Feature", "properties": None, "geometry": None},
        ],
    }
    tag_features_with_layer(gj, "TramvajovaDraha")
    assert gj["features"][0]["properties"]["vrstva"] == "TramvajovaDraha"
    assert gj["features"][0]["properties"]["fid_zbg"] == "1"
    assert gj["features"][1]["properties"]["vrstva"] == "TramvajovaDraha"


def test_vectorconf_sports_before_settlement_catchall():
    from app.settings import CONFIG_DIR

    lines = [
        ln.strip()
        for ln in (CONFIG_DIR / "zabaged.txt").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    for ln in lines:
        parts = ln.split("|")
        assert len(parts) == 3, ln
    sports = next(i for i, ln in enumerate(lines) if "typzast_p=stadión" in ln)
    catchall = next(i for i, ln in enumerate(lines) if ln.endswith("typzast_p!="))
    assert sports < catchall
    assert any("vrstva=TramvajovaDraha" in ln and ln.startswith("tramway|516|") for ln in lines)
    assert any("vrstva=ZeleznicniTrat" in ln and ln.startswith("railway|515|") for ln in lines)
    assert any("vrstva=ZeleznicniVlecka" in ln and ln.startswith("railway|515|") for ln in lines)
    assert not any("vrstva=Metro" in ln for ln in lines)
    assert not any("|509." in ln for ln in lines)
    assert any("vrstva=ParkovisteOdpocivka" in ln for ln in lines)
    garages = next(i for i, ln in enumerate(lines) if "typzast_p=skupinové garáže" in ln)
    assert garages < catchall
    assert any("vrstva=TrvalyTravniPorost" in ln and ln.startswith("farm|401|") for ln in lines)
    assert any("typ_pudy_k=OR" in ln and ln.startswith("farm|401|") for ln in lines)
    assert not any("vrstva=LesniPudaSKrovinatymPorostem" in ln for ln in lines)
    assert not any("vrstva=UdrzovanaZelen" in ln for ln in lines)
    assert not any("typ_pudy_k=UZ" in ln for ln in lines)
    assert not any("vrstva=OrnaPudaAOstatniDaleNespecifikovanePlochy" in ln for ln in lines)
    shelter = next(i for i, ln in enumerate(lines) if "podtypob_p=přístřešek" in ln)
    podtyp_all = next(i for i, ln in enumerate(lines) if ln.endswith("podtypob_p!="))
    assert shelter < podtyp_all
    assert lines[shelter].startswith("parking|529|")
    assert any(ln.startswith("road-path|503T|") and "vrstva=Most" in ln for ln in lines)
    assert any(ln.startswith("road-path|504T|") and "vrstva=Podjezd" in ln for ln in lines)
    assert not any("vrstva=Tunel" in ln and ln.startswith("blackline|") for ln in lines)
    assert not any("vrstva=Podjezd" in ln and ln.startswith("blackline|") for ln in lines)


def _vectorconf_lines(name: str) -> list[str]:
    from app.settings import CONFIG_DIR

    lines = [
        ln.strip()
        for ln in (CONFIG_DIR / name).read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    for ln in lines:
        parts = ln.split("|")
        assert len(parts) == 3, ln
    return lines


def test_forest_vectorconf_501_ochre_and_502_brown_road():
    lines = _vectorconf_lines("zabaged_forest.txt")
    assert any("vrstva=ParkovisteOdpocivka" in ln and ln.startswith("farm|401|") for ln in lines)
    assert any("vrstva=OstatniPlochaVSidlech" in ln and ln.startswith("farm|401|") for ln in lines)
    assert any("silnice!=" in ln and ln.startswith("road-path|503|") for ln in lines)
    assert any(
        "typulice_p=ulice nesjízdná v sídle" in ln and ln.startswith("road-path|503|")
        for ln in lines
    )
    assert not any("vrstva=ParkovisteOdpocivka" in ln and "|529|" in ln for ln in lines)


def test_drop_oversized_ostatni_plocha():
    gj = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"Shape_Area": 160_356}, "geometry": None},
            {"type": "Feature", "properties": {"Shape_Area": 13_727_609}, "geometry": None},
            {"type": "Feature", "properties": {"fid_zbg": "no-area"}, "geometry": None},
        ],
    }
    drop_oversized_ostatni_plocha(gj)
    areas = [f["properties"].get("Shape_Area") for f in gj["features"]]
    assert areas == [160_356, None]

