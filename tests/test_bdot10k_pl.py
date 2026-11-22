"""Polsko: BDOT10k po okresech jako technická mapa (``app/pipeline/bdot10k_pl.py``)."""

from __future__ import annotations

import io
import sqlite3
import struct
import urllib.parse
import zipfile
from pathlib import Path

import pytest
from shapely.geometry import LineString, box

from app.pipeline import bdot10k_pl

X, Y = 567_000.0, 244_000.0


def _gp(geom) -> bytes:
    return b"GP" + bytes([0, 0x01]) + struct.pack("<i", 2180) + geom.wkb


def _layer(path: Path, table: str, columns: list[str], rows: list[tuple]) -> Path:
    con = sqlite3.connect(path)
    cols = ", ".join(f'"{c}"' for c in columns)
    con.execute(f'create table "{table}" (id integer primary key, geometria blob, LOKALNYID text, {cols})')
    con.execute(f'create virtual table "rtree_{table}_geometria" using rtree(id, minx, maxx, miny, maxy)')
    for i, (geom, lid, *vals) in enumerate(rows, start=1):
        marks = ",".join("?" * (len(vals) + 3))
        con.execute(f'insert into "{table}" (id, geometria, LOKALNYID, {cols}) values ({marks})', (i, _gp(geom), lid, *vals))
        x0, y0, x1, y1 = geom.bounds
        con.execute(f'insert into "rtree_{table}_geometria" values (?,?,?,?,?)', (i, x0, x1, y0, y1))
    con.commit()
    con.close()
    return path


def _county(folder: Path, prefix: str = "") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    _layer(folder / f"{prefix}OT_BUBD_A.gpkg", "OT_BUBD_A", ["KATEGORIAISTNIENIA"], [
        (box(X, Y, X + 10, Y + 10), "b1", "eksploatowany"),
        (box(X + 20, Y, X + 30, Y + 10), "b2", "zniszczony"),
    ])
    _layer(folder / f"{prefix}OT_SKJZ_L.gpkg", "OT_SKJZ_L",
           ["SZEROKOSCNAWIERZCHNI", "MATERIALNAWIERZCHNI", "POLOZENIE", "KATEGORIAISTNIENIA"], [
        (LineString([(X, Y + 50), (X + 100, Y + 50)]), "r1", 6.0, "masa bitumiczna", "na powierzchni gruntu", "eksploatowany"),
        (LineString([(X, Y + 80), (X + 100, Y + 80)]), "r2", None, "grunt naturalny", "na powierzchni gruntu", "eksploatowany"),
        (LineString([(X, Y + 90), (X + 100, Y + 90)]), "r3", 7.0, "beton", "pod powierzchnią gruntu", "eksploatowany"),
    ])
    _layer(folder / f"{prefix}OT_PTPL_A.gpkg", "OT_PTPL_A", ["MATERIALNAWIERZCHNI"], [
        (box(X + 200, Y, X + 240, Y + 40), "p1", "kostka kamienna"),
        (box(X + 300, Y, X + 340, Y + 40), "p2", "grunt naturalny"),
    ])
    return folder


def test_parse_uldk():
    teryt, geom = bdot10k_pl.parse_uldk("0\n1261|SRID=2180;POLYGON((0 0,10 0,10 10,0 10,0 0))\n")
    assert teryt == "1261" and geom.area == 100
    assert bdot10k_pl.parse_uldk("-1 brak wyników\n") is None
    assert bdot10k_pl.parse_uldk("") is None


def test_counties_for_covers_bbox_across_border():
    west = "0\n1261|SRID=2180;POLYGON((0 0,100 0,100 100,0 100,0 0))"
    east = "0\n1219|SRID=2180;POLYGON((100 0,200 0,200 100,100 100,100 0))"
    calls = []

    def fetch(url):
        x = float(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["xy"][0].split(",")[0])
        calls.append(x)
        return west if x < 100 else east

    assert sorted(bdot10k_pl.counties_for((10, 10, 190, 90), fetch)) == ["1219", "1261"]
    assert len(calls) == 2


def test_read_county_filters(tmp_path):
    data = bdot10k_pl.read_county(_county(tmp_path), (X - 10, Y - 10, X + 400, Y + 200))
    assert [f["properties"]["id"] for f in data["buildings"]] == ["b1"]  # zničená budova pryč
    assert [f["properties"]["id"] for f in data["roads"]] == ["r1"]  # nezpevněná a podzemní pryč
    assert [f["properties"]["id"] for f in data["squares"]] == ["p1"]
    assert bdot10k_pl.read_county(tmp_path, (X + 5000, Y + 5000, X + 5100, Y + 5100)) == {"buildings": [], "roads": [], "squares": []}


def test_merge_dedups_border_objects():
    f = {"type": "Feature", "geometry": None, "properties": {"id": "x"}}
    out = bdot10k_pl.merge([{"buildings": [f], "roads": [], "squares": []}, {"buildings": [dict(f)], "roads": [], "squares": []}])
    assert len(out["buildings"]) == 1


def test_ensure_county_installs_and_rechecks(tmp_path, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path / "cache")
    src = _county(tmp_path / "src", prefix="PL.PZGiK.283.BDOT10k.1261__")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in src.iterdir():
            zf.write(p, p.name)
    calls = {"download": 0, "head": 0}

    def download(url, dest):
        assert url.endswith("/12/1261_GPKG.zip")
        calls["download"] += 1
        Path(dest).write_bytes(buf.getvalue())
        return len(buf.getvalue())

    def head(_url):
        calls["head"] += 1
        return {"last_modified": "Sat, 03 Oct 2026 01:30:26 GMT", "size": 1}

    folder = bdot10k_pl.ensure_county("1261", head=head, download=download, now=1000.0)
    assert all((folder / f"{layer}.gpkg").is_file() for layer in bdot10k_pl.LAYERS)
    assert not list(folder.glob("*.part"))
    bdot10k_pl.ensure_county("1261", head=head, download=download, now=2000.0)
    assert calls == {"download": 1, "head": 1}
    later = 1000.0 + 31 * 86400

    def broken(_url, dest):
        Path(dest).write_bytes(b"not a zip")
        return 9

    def head_new(_url):
        return {"last_modified": "Sun, 01 Nov 2026 00:00:00 GMT", "size": 2}

    assert bdot10k_pl.ensure_county("1261", head=head_new, download=broken, now=later) == folder  # stará data zůstanou
    assert (folder / "OT_BUBD_A.gpkg").stat().st_size > 0


def test_prepare_writes_dtm_files(tmp_path):
    county = _county(tmp_path / "1261")
    logs: list[str] = []
    bbox = (19.93, 50.05, 19.95, 50.07)  # Kraków; obálka se přepočte, testovací objekty leží u (567 000, 244 000)
    out = bdot10k_pl.prepare(
        tmp_path / "work", bbox, log=logs.append,
        counties_fn=lambda _box: ["1261"], county_fn=lambda teryt, log=None: county,
    )
    assert out is not None and out.is_file()
    assert any("BDOT10k (okresy 1261)" in line for line in logs)


def test_prepare_outside_poland(tmp_path):
    assert bdot10k_pl.prepare(tmp_path, (19.93, 50.05, 19.95, 50.07), counties_fn=lambda _b: []) is None


@pytest.mark.parametrize("teryt, url", [("1261", "/12/1261_GPKG.zip"), ("0201", "/02/0201_GPKG.zip")])
def test_package_url(teryt, url):
    assert bdot10k_pl.package_url(teryt).endswith(url)
