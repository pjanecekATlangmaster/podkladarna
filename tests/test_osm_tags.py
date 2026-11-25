"""Tagy OSM u objektů .omap (volba osm_tags): párování podle tvaru nad hotovou mapou."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from app.pipeline.oom_coords import projected_to_map_coord
from app.pipeline.oom_import import OomObjectPart
from app.pipeline.osm_tags import attach_osm_tags, merge_tags
from app.pipeline.source_tags import dtm_records, dtm_tags

SCALE = 10000
REF = (1000.0, 2000.0)


def _xy(x, y):
    return projected_to_map_coord(x, y, ref_x=REF[0], ref_y=REF[1], scale=SCALE, grivation_deg=0.0)


def _line(pts, symbol=3):
    body = ";".join("%d %d" % _xy(x, y) for x, y in pts) + ";"
    return f'<object type="1" symbol="{symbol}"><coords count="{len(pts)}">{body}</coords></object>'


def _point(x, y):
    return '<object type="0" symbol="5"><coords count="1">%d %d;</coords></object>' % _xy(x, y)


def _tags(obj_xml: str) -> dict:
    el = ET.fromstring(obj_xml)
    return {t.get("k"): t.text for t in el.iter("t")}


@pytest.fixture
def work(tmp_path):
    recs = [
        # Dvě OSM cesty spojené v mapě do jedné čáry: shodný surface zůstane, jiné jméno ne.
        {"id": "way/1", "tags": {"highway": "track", "surface": "gravel", "name": "A"}, "lines": [[(1000, 2000), (1100, 2000)]]},
        {"id": "way/2", "tags": {"highway": "track", "surface": "gravel", "name": "B & <C>"}, "lines": [[(1100, 2000), (1200, 2001)]]},
        {"id": "node/3", "tags": {"amenity": "bench"}, "point": (1050.0, 2030.0)},
        # Přechod vedle lavičky: lavička si vezme nejbližší bod, přechod zůstane nespárovaný.
        {"id": "node/5", "tags": {"highway": "crossing"}, "point": (1055.0, 2030.0)},
        # Souběžný chodník mimo kresbu u spojené silnice: jiná třída, tagy silnice nezkazí.
        {"id": "way/6", "tags": {"highway": "footway"}, "lines": [[(1000, 2001), (1100, 2001)]]},
        # Nenakreslená cesta daleko od objektů.
        {"id": "way/4", "tags": {"highway": "path"}, "lines": [[(1000, 2300), (1100, 2300)]]},
    ]
    (tmp_path / "osm_paths").mkdir()
    (tmp_path / "osm_paths" / "osm_tagged.json").write_text(json.dumps(recs), encoding="utf-8")
    return tmp_path


def test_tags_paired_by_shape_and_reported(work):
    road = _line([(1000, 2000), (1100, 2000), (1200, 2001)])
    bench = _point(1051, 2030.5)  # odsunutý bod
    zab = _line([(1000, 2000), (1200, 2000)])
    parts = [
        OomObjectPart("OSM cesty", road, 1),
        OomObjectPart("OSM lavička", bench, 1),
        OomObjectPart("ZABAGED – Ulice", zab, 1),
    ]
    st: dict = {}
    out = attach_osm_tags(parts, work, scale=SCALE, ref_x=REF[0], ref_y=REF[1], grivation_deg=0.0, stats=st)
    assert _tags(out[0].objects_xml) == {"src": "osm", "osm_id": "way/1;way/2", "highway": "track", "surface": "gravel"}
    assert _tags(out[1].objects_xml) == {"src": "osm", "osm_id": "node/3", "amenity": "bench"}
    assert out[2] is parts[2]  # při shodném pokrytí má přednost objekt z části téhož zdroje
    assert st["unpaired"] == {"highway=path": 1, "highway=crossing": 1, "highway=footway": 1}
    assert st["dropped_keys"] == {"name": 1}
    assert (work / "osm_tagged_report.json").is_file()


def test_missing_prepared_elements_leave_parts(tmp_path):
    parts = [OomObjectPart("OSM cesty", _line([(1000, 2000), (1100, 2000)]), 1)]
    st: dict = {}
    assert attach_osm_tags(parts, tmp_path, scale=SCALE, ref_x=0, ref_y=0, grivation_deg=0.0, stats=st) == parts
    assert st["missing"]


def test_merge_tags_escaping_and_conflicts():
    kept, dropped = merge_tags([{"a": "1", "b": "x"}, {"a": "1"}])
    assert kept == {"a": "1"} and dropped == {"b"}


def _dtm(rid, tags, pts):
    return {"id": f"dtm/{rid}", "src": "dtm", "tags": {**tags, "dtm:id": str(rid)}, "lines": [pts]}


def test_osm_and_dtm_merged_on_one_object_osm_wins_conflict(tmp_path):
    """Zeď z OSM i z DTM (po dedupu jeden objekt) nese tagy obou zdrojů; u rozdílné hodnoty vyhrává OSM."""
    (tmp_path / "osm_paths").mkdir()
    osm = [
        {"id": "way/7", "tags": {"barrier": "wall", "material": "stone"}, "lines": [[(1000, 2000), (1050, 2000)]]},
        {"id": "way/8", "tags": {"highway": "footway", "surface": "asphalt"}, "lines": [[(1000, 2100), (1060, 2100)]]},
    ]
    (tmp_path / "osm_paths" / "osm_tagged.json").write_text(json.dumps(osm), encoding="utf-8")
    dtm = [
        _dtm(11, dtm_tags("Plot", {"DruhPlotu": "4"}), [(1000, 2000.3), (1050, 2000.3)]),
        _dtm(12, {"area:highway": "footway", "surface": "concrete", "dtm:typ": "ChodnikPlocha"},
             [(1000, 2100.4), (1060, 2100.4)]),
        # Plot souběžně se silnicí: nesmí jí dát tagy plotu (jiná skupina symbolu).
        _dtm(13, dtm_tags("Plot", {"DruhPlotu": "2"}), [(1000, 2200.8), (1060, 2200.8)]),
    ]
    parts = [
        OomObjectPart("Ploty (DTM + OSM)", _line([(1000, 2000), (1050, 2000)], symbol=1), 1),
        OomObjectPart("OSM cesty", _line([(1000, 2100), (1060, 2100)], symbol=2), 1),
        OomObjectPart("OSM silnice", _line([(1000, 2200), (1060, 2200)], symbol=3), 1),
    ]
    st: dict = {}
    out = attach_osm_tags(
        parts, tmp_path, scale=SCALE, ref_x=REF[0], ref_y=REF[1], grivation_deg=0.0, stats=st,
        symbol_codes={1: "513", 2: "505", 3: "502"}, extra_records=dtm,
    )
    wall = _tags(out[0].objects_xml)
    assert wall["src"] == "osm;dtm" and wall["osm_id"] == "way/7" and wall["dtm:id"] == "11"
    assert wall["barrier"] == "wall" and wall["material"] == "stone" and wall["dtm:typ"] == "Plot"
    path = _tags(out[1].objects_xml)
    assert path["surface"] == "asphalt" and path["area:highway"] == "footway"
    assert out[2] is parts[2]
    assert st["conflicts"] == {"surface": 1}
    assert st["multi_source"] == 2
    assert st["unpaired"] == {"dtm:Plot": 1}
    assert st["sources"]["dtm"] == {"elements": 3, "paired": 2}


@pytest.mark.parametrize(
    "typ,attrs,expected",
    [
        ("Plot", {"DruhPlotu": "2"}, {"barrier": "fence", "fence_type": "chain_link"}),
        ("Plot", {"DruhPlotu": "5"}, {"barrier": "hedge"}),
        ("ChodnikPlocha", {"PrevazujiciPovrch": "3"}, {"area:highway": "footway", "surface": "paving_stones"}),
        ("ZemedelskaPlochaPlocha", {"TypZemedelskePlochy": "4"}, {"landuse": "orchard"}),
        ("BudovaPlocha", {"UrovenUmisteniObjektuZPS": "1"}, {"building": "yes", "layer": "1"}),
    ],
)
def test_dtm_tags_unified_with_osm_vocabulary(typ, attrs, expected):
    tags = dtm_tags(typ, {"ID": "5", "IDEditora": "SUBJ-1", "PopisObjektu": "n/a", **attrs})
    assert {k: v for k, v in tags.items() if not k.startswith("dtm:")} == expected
    assert tags["dtm:typ"] == typ and tags["dtm:id"] == "5"
    assert "dtm:IDEditora" not in tags and "dtm:PopisObjektu" not in tags


def test_dtm_records_from_job_packages(tmp_path):
    import zipfile

    from tests.test_jvf_reader import XML

    pkgs = tmp_path / "cache"
    pkgs.mkdir()
    for name in ("VS_X_ZPS_KR.jvf.zip", "VS_X_ZPS_OPL.jvf.zip"):
        with zipfile.ZipFile(pkgs / name, "w") as zf:
            zf.writestr("a.xml", XML)
    (tmp_path / "dtm").mkdir()
    (tmp_path / "dtm" / "fences.json").write_text(
        json.dumps({"opl_files": [str(pkgs / "VS_X_ZPS_OPL.jvf.zip")]}), encoding="utf-8"
    )
    recs = dtm_records(tmp_path, (-1000.0, -1000.0, 1000.0, 1000.0))
    typs = sorted(r["tags"]["dtm:typ"] for r in recs)
    assert "Plot" in typs  # z ZPS_KR vedle OPL; plot pod povrchem (úroveň −1) ne
    assert typs.count("Plot") == 1
    assert (pkgs / "VS_X_ZPS_KR.jvf.zip.tags2.json").is_file()
    assert dtm_records(tmp_path / "jiny", None) == []


def test_prepare_oom_map_runs_source_tags_step(tmp_path):
    """Volba osm_tags v sestavení mapy: krok tagů proběhne (bez prvků jen zapíše řádek), mapa se zapíše."""
    from unittest.mock import patch

    from app.pipeline.package_oom import prepare_oom_map

    kp = tmp_path / "work"
    kp.mkdir()
    zabaged = tmp_path / "zabaged_clean.zip"
    zabaged.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    lines: list[str] = []
    with (
        patch("app.pipeline.package_oom.build_zabaged_object_parts", return_value=[]),
        patch("app.pipeline.package_oom.build_vegetation_parts", return_value=[]),
        patch("app.pipeline.package_oom.build_gdal_contour_parts", return_value=[]),
        patch("app.pipeline.package_oom.build_osm_feature_parts", return_value=[]),
        patch("app.pipeline.package_oom.build_osm_path_parts", return_value=[]),
        patch("app.pipeline.package_oom.build_aoi_boundary_part", return_value=None),
        patch("app.pipeline.package_oom.write_oom_map", side_effect=lambda dest, **kw: dest),
    ):
        out = prepare_oom_map(
            kp, tmp_path / "out.omap", map_name="out", scale=10000, preset_id="forest_10000",
            bbox_wgs84=(14.4, 50.08, 14.42, 50.09), zabaged_clean=zabaged, osm_tags=True, log=lines.append,
        )
    assert out is not None
    assert any(ln.startswith("OSM tagy: chybí") for ln in lines)
    assert not any("tagy: přeskočeno" in ln.lower() for ln in lines)
