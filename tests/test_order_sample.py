"""Ukázkový výřez 4 × 4 cm pro objednávky (app.order_sample)."""

from __future__ import annotations

import random
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from app import order_sample as sample

ROOT = Path(__file__).resolve().parents[1]
# Sada symbolů s legendou (373 objektů: čáry, plochy, body, texty; 1:4 000, rozsah 306 × 358 mm).
OMAP = ROOT / "configs" / "oom" / "ISSprOM_2019_4000_cz.omap"
Q = sample._q
SALT = b"test-salt-0123456789-0123456789-xx"


def _objects(xml: bytes) -> list[ET.Element]:
    root = ET.fromstring(xml)
    return [o for part in root.iter(Q("parts")) for o in part.iter(Q("object"))]


def _extent(objects: list[ET.Element]) -> tuple[int, int]:
    pts = [(x, y) for o in objects for x, y, _f in sample._parse_coords(o.find(Q("coords")).text)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return max(xs) - min(xs), max(ys) - min(ys)


def test_crop_keeps_only_square_and_drops_templates():
    xml = OMAP.read_bytes()
    out = sample.crop_omap(xml, "sprint|4000", salt=SALT)  # 40 mm = 160 m, 1/3 rozsahu je 102 mm
    assert out is not None and out == sample.crop_omap(xml, "sprint|4000", salt=SALT)
    objects = _objects(out)
    assert 0 < len(objects) < len(_objects(xml)) // 4
    w, h = _extent(objects)
    assert w <= 40_000 + 2 and h <= 40_000 + 2
    root = ET.fromstring(out)
    assert root.find(f".//{Q('symbols')}").get("count") == "190"  # symboly zůstávají
    assert all(t.get("count") == "0" for t in root.iter(Q("templates")))
    for obj in objects:
        coords = obj.find(Q("coords"))
        assert int(coords.get("count")) == len(sample._parse_coords(coords.text))


@pytest.mark.parametrize(
    "side_um, expect_max_um",
    [
        (400_000, 102_000),  # větší než mapa (malé území): strana se půlí na ≤ 1/3 rozsahu
        (40_000_000, 102_000),
    ],
)
def test_small_area_gives_only_small_part(side_um, expect_max_um):
    xml = OMAP.read_bytes()
    out = sample.crop_omap(xml, "les|10000", salt=SALT, side=side_um)
    objects = _objects(out)
    w, h = _extent(objects)
    assert w <= expect_max_um and h <= expect_max_um
    assert len(objects) < len(_objects(xml)) // 4  # nikdy celá mapa


def test_tiny_map_gives_no_sample():
    xml = (
        f'<?xml version="1.0"?><map xmlns="{sample.OMAP_NS}" version="9">'
        '<georeferencing scale="10000"/><barrier><symbols count="0"/>'
        '<parts count="1"><part name="a"><objects count="2">'
        '<object type="0" symbol="0"><coords count="1">0 0;</coords></object>'
        '<object type="0" symbol="0"><coords count="1">20000 20000;</coords></object>'
        "</objects></part></parts></barrier></map>"
    ).encode()
    assert sample.crop_omap(xml, "les|10000", salt=SALT) is None  # 20 mm mapy: ukázka pod 10 mm


def test_place_anchored_to_grid_and_secret():
    rng = random.Random(1)
    field = [(rng.uniform(-200, 2400), rng.uniform(-200, 2400)) for _ in range(40_000)]

    def area(x0: float, y0: float) -> list[tuple[float, float]]:  # výřez 2 × 2 km kdekoli v poli
        return [(x, y) for x, y in field if x0 <= x <= x0 + 2000 and y0 <= y <= y0 + 2000]

    base = sample.choose_cell(area(0, 0), 400, "les|10000", SALT)
    assert base is not None and all(v % 400 == 0 for v in base)  # zarovnané na mřížku
    for dx, dy in [(7, 0), (0, -9), (15, 12)]:  # malý posun výřezu = stejné místo
        assert sample.choose_cell(area(dx, dy), 400, "les|10000", SALT) == base
    others = {sample.choose_cell(area(0, 0), 400, "les|10000", bytes([i]) * 32) for i in range(8)}
    assert len(others) > 1  # místo určuje tajná sůl, ne jen výřez


def test_area_clipped_to_square():
    points = "".join(  # body (symbol 1) hustě po mapě, ať je kam ukázku umístit
        f'<object type="0" symbol="1"><coords count="1">{x} {y};</coords></object>'
        for x in range(5_000, 300_000, 10_000)
        for y in range(5_000, 300_000, 10_000)
    )
    xml = (
        f'<?xml version="1.0"?><map xmlns="{sample.OMAP_NS}" version="9">'
        '<georeferencing scale="10000"/><barrier>'
        '<symbols count="2"><symbol type="4" id="0"/><symbol type="1" id="1"/></symbols>'
        '<parts count="1"><part name="a"><objects count="2">'
        '<object type="1" symbol="0"><coords count="5">'
        f"0 0;300000 0;300000 300000;0 300000;0 0 18;</coords></object>{points}"
        "</objects></part></parts></barrier></map>"
    ).encode()
    objects = _objects(sample.crop_omap(xml, "les|10000", salt=SALT))
    (obj,) = [o for o in objects if o.get("symbol") == "0"]
    assert 0 < len(objects) - 1 <= 25  # body jen ze čtverce 40 × 40 mm
    coords = sample._parse_coords(obj.find(Q("coords")).text)
    assert coords[-1][2] == 18 and len(coords) == 5  # čtverec ukázky, uzavřený prstenec
    xs = sorted({c[0] for c in coords})
    assert xs[-1] - xs[0] == 40_000


def test_sample_zip_from_job_zip(tmp_path, monkeypatch):
    monkeypatch.setenv("ORDER_SAMPLE_SECRET", "test-secret")
    job_zip = tmp_path / "podkladarna_output.zip"
    with zipfile.ZipFile(job_zip, "w") as zf:
        zf.write(OMAP, "mapa_sprint.omap")
        zf.writestr("readme.txt", "x")
    dest = sample.build_sample_zip(job_zip, tmp_path / sample.SAMPLE_ZIP_NAME, "sprint|4000")
    assert dest is not None
    with zipfile.ZipFile(dest) as zf:
        assert zf.namelist() == ["sample-mapa_sprint.omap"]
        assert _objects(zf.read("sample-mapa_sprint.omap"))
    assert sample.build_sample_zip(tmp_path / "missing.zip", tmp_path / "x.zip", "k") is None


def test_salt_file_created_outside_repo(data_dir, monkeypatch):
    monkeypatch.delenv("ORDER_SAMPLE_SECRET", raising=False)
    salt = sample.sample_salt()
    assert len(salt) >= 32 and sample.sample_salt() == salt
    assert (data_dir / "sample_salt").is_file()
