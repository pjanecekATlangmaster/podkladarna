"""Lipsko: Stadtgrundkarte DSGK jako doplněk ALKIS (``app/pipeline/dsgk_leipzig.py``)."""

from __future__ import annotations

import io
import sqlite3
import struct
import zipfile
from pathlib import Path

import pytest
from shapely.geometry import LineString, Polygon, box

from app.pipeline import dsgk_leipzig

GEOM = '"$(GPKG_GEOMETRY_ATTRIBUT)"'


def _gp(geom) -> bytes:
    return b"GP" + bytes([0, 0x01]) + struct.pack("<i", 25833) + geom.wkb


def _gpkg(path: Path, rows: dict[str, list[tuple]]) -> Path:
    con = sqlite3.connect(path)
    for table, col, col2, _codes in dsgk_leipzig.LAYERS:
        second = f', "{col2}" integer' if col2 else ""
        con.execute(f'create table "{table}" (id integer primary key, {GEOM} blob, "{col}" integer{second})')
        for geom, *codes in rows.get(table, []):
            marks = ",".join("?" * (len(codes) + 1))
            names = f'{GEOM}, "{col}"' + (f', "{col2}"' if col2 else "")
            con.execute(f'insert into "{table}" ({names}) values ({marks})', (_gp(geom), *codes))
    con.execute('create table "GK_Gebaeude" (id integer primary key, x integer)')
    con.commit()
    con.close()
    return path


X, Y = 317_000.0, 5_690_000.0
ROWS = {
    "GK_Strasse": [(box(X, Y, X + 50, Y + 10), 1307)],
    "GK_Strassenverkehrsanlage": [(box(X, Y + 10, X + 20, Y + 15), 5315), (box(X + 20, Y + 10, X + 25, Y + 15), 1015)],
    "GK_WegPfadSteig": [(box(X, Y + 15, X + 50, Y + 18), 1103, 1000), (box(X, Y + 30, X + 50, Y + 33), 2020, 2000)],
    "GK_SonstigesBauwerkOSonstigeEinrichtung_f": [(box(X, Y + 40, X + 3, Y + 43), 1620), (box(X, Y + 50, X + 3, Y + 53), 1700)],
    "GK_SonstigesBauwerkOSonstigeEinrichtung_l": [
        (LineString([(X, Y + 60), (X + 30, Y + 60)]), 1741),
        (LineString([(X, Y + 70), (X + 30, Y + 70)]), 2020),
    ],
}


@pytest.mark.parametrize(
    ("layer", "code", "code2", "expected"),
    [
        ("GK_Strasse", 1307, None, "roads"),
        ("GK_Strassenverkehrsanlage", 1000, None, "roads"),
        ("GK_Strassenverkehrsanlage", 5315, None, "parking"),
        ("GK_Strassenverkehrsanlage", 4000, None, "sidewalks"),
        ("GK_Strassenverkehrsanlage", 1015, None, None),  # ostrůvek kreslí OSM
        ("GK_WegPfadSteig", 1103, 1000, "sidewalks"),
        ("GK_WegPfadSteig", 1110, None, "sidewalks"),
        ("GK_WegPfadSteig", 1103, 2000, None),  # nezpevněná pěšina kreslí OSM
        ("GK_WegPfadSteig", 2020, 1000, "small_paved"),
        ("GK_WegPfadSteig", 2020, None, None),
        ("GK_SonstigesBauwerkOSonstigeEinrichtung_f", 1620, None, "stairs"),
        ("GK_SonstigesBauwerkOSonstigeEinrichtung_f", 1700, None, None),
        ("GK_SonstigesBauwerkOSonstigeEinrichtung_l", 1742, None, "fences"),
        ("GK_SonstigesBauwerkOSonstigeEinrichtung_l", 2020, None, None),
    ],
)
def test_target(layer, code, code2, expected):
    assert dsgk_leipzig.target(layer, code, code2) == expected


def test_build_store_and_classify(tmp_path):
    gpkg = _gpkg(tmp_path / "dsgk.gpkg", ROWS)
    store = tmp_path / "lite.sqlite"
    # vozovka, stání, chodník, nezpevněná cesta, schody, plot; ostrůvek, zeď a zábradlí se neuloží
    assert dsgk_leipzig.build_store(gpkg, store) == 6
    feats = dsgk_leipzig.read_features(store, (X - 1, Y - 1, X + 60, Y + 80))
    out = dsgk_leipzig.classify(feats)
    assert {k: len(v) for k, v in out.items()} == {
        "roads": 1, "sidewalks": 1, "parking": 1, "small_paved": 0, "stairs": 1, "fences": 1,
    }
    assert out["fences"][0].coords[0] == (X, Y + 60)
    assert dsgk_leipzig.read_features(store, (X + 1000, Y + 1000, X + 1100, Y + 1100)) == []


def test_resolve_zip_url():
    html = '<a href="2026_10/">x</a> <a href="DSGK2026mH_GPKG25833_7838_NON_TILED.zip">z</a> <a href="?C=N">'
    assert dsgk_leipzig.resolve_zip_url(lambda _u: html).endswith("/DSGK2026mH_GPKG25833_7838_NON_TILED.zip")
    assert dsgk_leipzig.resolve_zip_url(lambda _u: "<html></html>").endswith("/" + dsgk_leipzig.DEFAULT_ZIP)

    def boom(_u):
        raise OSError("offline")

    assert dsgk_leipzig.resolve_zip_url(boom).endswith("/" + dsgk_leipzig.DEFAULT_ZIP)


def test_ensure_store_downloads_once_and_rechecks(tmp_path, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    gpkg = _gpkg(tmp_path / "src.gpkg", ROWS)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.write(gpkg, "QGIS/GPKG25833/Datenauszug_Stadtgrundkarte.gpkg")
    calls = {"download": 0, "head": 0}

    def download(_url, dest):
        calls["download"] += 1
        Path(dest).write_bytes(buf.getvalue())
        return len(buf.getvalue())

    def head(_url):
        calls["head"] += 1
        return {"last_modified": "Wed, 04 Nov 2026 13:12:17 GMT", "size": 123}

    listing = lambda _u: ""  # noqa: E731
    store = dsgk_leipzig.ensure_store(head=head, download=download, listing=listing, now=1000.0)
    assert store.is_file() and calls == {"download": 1, "head": 1}
    assert not list(store.parent.glob("*.part"))
    dsgk_leipzig.ensure_store(head=head, download=download, listing=listing, now=2000.0)
    assert calls == {"download": 1, "head": 1}  # do 30 dní bez dotazu
    later = 1000.0 + 31 * 86400
    dsgk_leipzig.ensure_store(head=head, download=download, listing=listing, now=later)
    assert calls == {"download": 1, "head": 2}  # stejná verze: nestahuje

    def head_down(_url):
        raise OSError("offline")

    much_later = later + 31 * 86400
    assert dsgk_leipzig.ensure_store(head=head_down, download=download, listing=listing, now=much_later) == store

    def head_new(_url):
        return {"last_modified": "Thu, 03 Dec 2026 10:00:00 GMT", "size": 456}

    def download_broken(_url, dest):
        Path(dest).write_bytes(b"not a zip")
        return 9

    newest = much_later + 31 * 86400
    assert dsgk_leipzig.ensure_store(head=head_new, download=download_broken, listing=listing, now=newest) == store
    assert store.is_file() and not list(store.parent.glob("*.part"))
    calls["head"] = 0
    dsgk_leipzig.ensure_store(head=head, download=download, listing=listing, now=newest + 3600)
    assert calls["head"] == 0  # další pokus až za den, ne v každém jobu


def test_augment_replaces_alkis_inside_city(tmp_path):
    from pyproj import Transformer

    from app.pipeline.crs_5514 import CRS_LABEL

    gpkg = _gpkg(tmp_path / "dsgk.gpkg", ROWS)
    store = tmp_path / "lite.sqlite"
    dsgk_leipzig.build_store(gpkg, store)
    t = Transformer.from_crs(dsgk_leipzig.SOURCE_CRS, CRS_LABEL, always_xy=True)
    bounds = t.transform_bounds(X - 200, Y - 200, X + 200, Y + 200)
    inside = Polygon(box(*t.transform_bounds(X + 100, Y + 100, X + 150, Y + 150)).exterior.coords)
    alkis = {
        "sidewalks": [[[list(p) for p in inside.exterior.coords]]],
        "parking": [],
        "public": [[[list(p) for p in inside.exterior.coords]]],
        "buildings": [],
    }
    out = dsgk_leipzig.augment(alkis, bounds, CRS_LABEL, store_fn=lambda log=None: store)
    assert len(out["sidewalks"]) == 1  # náměstí ALKIS v Lipsku pryč, chodník DSGK přidán
    assert len(out["roads"]) == 1 and len(out["stairs"]) == 1 and len(out["fences"]) == 1
    assert len(out["public"]) == 1 + 3  # původní ALKIS + vozovka, chodník, stání
    assert dsgk_leipzig.intersects((12.37, 51.33, 12.38, 51.34))
    assert not dsgk_leipzig.intersects((13.73, 51.04, 13.74, 51.05))  # Drážďany


def test_alkis_keeps_running_when_dsgk_fails(monkeypatch):
    from app.pipeline import alkis_de, dsgk_leipzig as mod

    def boom(*_a, **_k):
        raise OSError("server down")

    monkeypatch.setattr(mod, "augment", boom)
    logs: list[str] = []
    data = {"sidewalks": [1]}
    assert alkis_de._with_leipzig(data, (12.37, 51.33, 12.38, 51.34), (0, 0, 1, 1), "EPSG:5514", logs.append) is data
    assert logs and logs[0].startswith("⚠ DSGK Lipsko")
