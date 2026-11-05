"""Sentinel-2 jako podklad: výběr snímku, maska SCL, šablony v .omap, ZIP."""

from __future__ import annotations

import io
import json
from datetime import date

import pytest

from app.pipeline import sentinel2 as s2
from app.pipeline.oom_layers import collect_oom_templates


def _scene(day: str, cloud: float = 0.0) -> s2.Scene:
    return s2.Scene(f"S2_{day}", date.fromisoformat(day), cloud, {}, 1000.0)


@pytest.mark.parametrize(
    ("days", "clear", "expected"),
    [
        # nejnovější jasný je v sezóně → jen ten
        (["2026-09-26", "2026-08-01"], {"2026-09-26", "2026-08-01"}, ["2026-09-26"]),
        # nejnovější jasný mimo sezónu → přidá nejnovější jasný letní
        (["2026-11-05", "2026-10-16", "2026-09-26"], {"2026-11-05", "2026-09-26"}, ["2026-11-05", "2026-09-26"]),
        # zatažené se přeskočí
        (["2026-11-05", "2026-10-16"], {"2026-10-16"}, ["2026-10-16"]),
        # nic jasného
        (["2026-11-05"], set(), []),
    ],
)
def test_pick_scenes(days, clear, expected):
    picked = s2.pick_scenes([_scene(d) for d in days], lambda sc: sc.day.isoformat() in clear)
    assert [p.day.isoformat() for p in picked] == expected


def test_bad_share_counts_clouds_snow_and_nodata():
    # 4 = vegetace, 5 = holá půda, 8/9 mraky, 11 sníh, 0 bez dat
    assert s2.bad_share(bytes([4, 5, 4, 5])) == 0.0
    assert s2.bad_share(bytes([4, 8, 11, 0])) == 0.75
    assert s2.bad_share(b"") == 1.0


def test_search_parses_stac_and_sorts_newest_first():
    def feat(iid, dt, cloud, href="https://x/"):
        assets = {k: {"href": href + k + ".tif"} for k in ("visual", "scl", "nir", "red", "green")}
        assets["nir"]["raster:bands"] = [{"offset": -0.1, "scale": 0.0001}]
        return {"id": iid, "properties": {"datetime": dt, "eo:cloud_cover": cloud}, "assets": assets}

    payload = {
        "features": [
            feat("old", "2026-09-26T10:00:00Z", 1.0),
            feat("new", "2026-11-05T10:00:00Z", 3.0),
            feat("s3", "2026-11-04T10:00:00Z", 0.0, href="s3://bucket/"),  # jen https
        ]
    }
    seen = {}

    def opener(req, timeout):
        seen["body"] = json.loads(req.data)
        return io.BytesIO(json.dumps(payload).encode())

    scenes = s2.search_scenes((14.0, 50.0, 14.1, 50.1), today=date(2026, 11, 5), opener=opener)
    assert [sc.item_id for sc in scenes] == ["new", "old"]
    assert scenes[0].dn_offset == pytest.approx(1000.0)
    assert seen["body"]["collections"] == ["sentinel-2-l2a"]


def test_sentinel_templates_hidden_with_date(tmp_path):
    refs = {}
    for name in ("sentinel2_2026-11-05.jpg", "sentinel2_2026-11-05_nir.jpg"):
        p = tmp_path / name
        p.write_bytes(b"x")
        refs[p.stem] = p
    tpls = collect_oom_templates(tmp_path, built_refs=refs)
    labels = {t.label: t for t in tpls}
    assert set(labels) == {"Sentinel-2 2026-11-05 pravé barvy", "Sentinel-2 2026-11-05 infračervené barvy"}
    assert all(not t.visible for t in tpls)
    assert labels["Sentinel-2 2026-11-05 pravé barvy"].relpath == "references/sentinel2_2026-11-05.jpg"


def test_info_text_has_date_and_attribution():
    text = s2.info_text([_scene("2026-11-05"), _scene("2026-09-26")])
    assert "Datum snímku: 2026-11-05 (mimo vegetační sezónu)" in text
    assert "sentinel2_2026-09-26_nir.jpg" in text
    assert "Copernicus Sentinel 2026" in text
