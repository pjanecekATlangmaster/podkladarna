"""Ukázkový výřez 4 × 4 cm pro objednávky (app.order_sample)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from app import order_sample as sample

ROOT = Path(__file__).resolve().parents[1]
# Sada symbolů s legendou (373 objektů: čáry, plochy, body, texty) jako skutečná mapa.
OMAP = ROOT / "configs" / "oom" / "ISSprOM_2019_4000_cz.omap"
Q = sample._q


def _objects(xml: bytes) -> list[ET.Element]:
    root = ET.fromstring(xml)
    return [o for part in root.iter(Q("parts")) for o in part.iter(Q("object"))]


def test_crop_keeps_only_square_and_drops_templates():
    xml = OMAP.read_bytes()
    seed = sample.sample_seed({"bbox_wgs84": [14.0, 50.0, 14.01, 50.01], "map_scale": 4000}, "sprint_2m")
    side = 20_000  # menší než legenda, ať se čáry a plochy opravdu ořezávají
    out = sample.crop_omap(xml, seed, side=side)
    assert out is not None and out == sample.crop_omap(xml, seed, side=side)  # stejné území = stejné místo
    objects = _objects(out)
    assert 0 < len(objects) < len(_objects(xml))
    pts = [(x, y) for o in objects for x, y, _f in sample._parse_coords(o.find(Q("coords")).text)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    assert max(xs) - min(xs) <= side + 2 and max(ys) - min(ys) <= side + 2
    root = ET.fromstring(out)
    assert root.find(f".//{Q('symbols')}").get("count") == "190"  # symboly zůstávají
    assert all(t.get("count") == "0" for t in root.iter(Q("templates")))
    for obj in objects:  # počet souřadnic sedí, plochy mají uzavřené prstence
        coords = obj.find(Q("coords"))
        parsed = sample._parse_coords(coords.text)
        assert int(coords.get("count")) == len(parsed)


def test_area_clipped_to_square():
    area_sym = next(k for k, v in sample._symbol_types(ET.fromstring(OMAP.read_bytes())).items() if v == 4)
    xml = (
        f'<?xml version="1.0"?><map xmlns="{sample.OMAP_NS}" version="9"><barrier>'
        f'<symbols count="1"><symbol type="4" id="{area_sym}"/></symbols>'
        '<parts count="1"><part name="a"><objects count="1">'
        f'<object type="1" symbol="{area_sym}"><coords count="5">'
        "0 0;100000 0;100000 100000;0 100000;0 0 18;</coords></object>"
        "</objects></part></parts></barrier></map>"
    ).encode()
    out = sample.crop_omap(xml, "seed", side=40_000)
    (obj,) = _objects(out)
    coords = sample._parse_coords(obj.find(Q("coords")).text)
    assert coords[-1][2] == 18 and len(coords) == 5  # čtverec ukázky, uzavřený prstenec
    xs = sorted({c[0] for c in coords})
    assert xs[-1] - xs[0] == 40_000


def test_sample_zip_from_job_zip(tmp_path):
    job_zip = tmp_path / "podkladarna_output.zip"
    with zipfile.ZipFile(job_zip, "w") as zf:
        zf.write(OMAP, "mapa_sprint.omap")
        zf.writestr("readme.txt", "x")
    dest = sample.build_sample_zip(job_zip, tmp_path / sample.SAMPLE_ZIP_NAME, "seed")
    assert dest is not None
    with zipfile.ZipFile(dest) as zf:
        assert zf.namelist() == ["sample-mapa_sprint.omap"]
        assert _objects(zf.read("sample-mapa_sprint.omap"))
    assert sample.build_sample_zip(tmp_path / "missing.zip", tmp_path / "x.zip", "seed") is None
