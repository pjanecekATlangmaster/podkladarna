from __future__ import annotations

from app.pipeline.fetch_zabaged import query_layer_geojson


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
