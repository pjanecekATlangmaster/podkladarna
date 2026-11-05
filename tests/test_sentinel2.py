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
        # nejnovější jasný v období
        (["2026-11-05", "2026-10-16"], {"2026-11-05", "2026-10-16"}, "2026-11-05"),
        # zatažené se přeskočí, druhá dlaždice téhož dne se neověřuje znovu
        (["2026-11-05", "2026-11-05", "2026-10-16"], {"2026-10-16"}, "2026-10-16"),
        # nic jasného
        (["2026-11-05"], set(), None),
    ],
)
def test_pick_newest_clear(days, clear, expected):
    calls = []

    def is_clear(sc):
        calls.append(sc.day)
        return sc.day.isoformat() in clear

    picked = s2.pick_newest_clear([_scene(d) for d in days], is_clear)
    assert (picked.day.isoformat() if picked else None) == expected
    assert len(calls) == len(set(calls))


def test_slots_last_half_year_by_three_months():
    assert s2.slot_ends(date(2026, 11, 5)) == [date(2026, 11, 5), date(2026, 8, 7), date(2026, 5, 9)]


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
    for name in ("sentinel2_2026-08-01.jpg", "sentinel2_2026-11-05.jpg"):
        p = tmp_path / name
        p.write_bytes(b"x")
        refs[p.stem] = p
    tpls = collect_oom_templates(tmp_path, built_refs=refs)
    assert [t.label for t in tpls] == ["Sentinel-2 2026-11-05", "Sentinel-2 2026-08-01"]
    assert all(not t.visible for t in tpls)
    assert tpls[0].relpath == "references/sentinel2_2026-11-05.jpg"


def test_info_text_has_date_and_attribution():
    text = s2.info_text([_scene("2026-11-05"), _scene("2025-09-26")])
    assert "sentinel2_2026-11-05.jpg – datum snímku 2026-11-05" in text
    assert "_nir" not in text
    assert "Copernicus Sentinel 2025, 2026" in text


def test_readme_lists_sentinel_with_other_references():
    from app.pipeline.package_oom import oom_readme

    info = s2.info_text([_scene("2026-11-05")])
    readme = oom_readme({"reference_layers": ["osm.png"]}, sentinel_info=info)
    refs = readme.split("Referenční podklady (složka references/)", 1)[1]
    assert "- osm.png" in refs and "sentinel2_2026-11-05.jpg" in refs and "Copernicus Sentinel 2026" in refs
