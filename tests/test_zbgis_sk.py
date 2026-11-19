"""Slovensko: budovy a šířky silnic z INSPIRE ZBGIS (čtení výřezu z GeoPackage, cache s obnovou po 30 dnech)."""

import io
import sqlite3
import struct
import zipfile

import pytest
from shapely.geometry import LineString, Point, box

from app import disk_space, settings
from app.pipeline import zbgis_sk


def _gp(geom) -> bytes:
    """Geometrie GeoPackage: hlavička „GP“, verze 0, příznaky (malý endian, bez obálky), SRID + WKB."""
    return b"GP" + bytes([0, 0x01]) + struct.pack("<i", zbgis_sk.SOURCE_SRID) + geom.wkb


def _table(con, name: str, columns: str, rows: list[tuple]) -> None:
    con.execute(f'create table "{name}" (fid integer primary key, geometry blob, {columns})')
    con.execute(f'create virtual table "rtree_{name}_geometry" using rtree(id, minx, maxx, miny, maxy)')
    for fid, (geom, *values) in enumerate(rows, start=1):
        marks = ",".join("?" * (len(values) + 2))
        con.execute(f'insert into "{name}" values ({marks})', (fid, _gp(geom), *values))
        x0, y0, x1, y1 = geom.bounds
        con.execute(f'insert into "rtree_{name}_geometry" values (?,?,?,?,?)', (fid, x0, x1, y0, y1))


def _bu(path):
    cond = "http://inspire.ec.europa.eu/codelist/ConditionOfConstructionValue/"
    con = sqlite3.connect(path)
    _table(con, "Building", "conditionOfConstruction text, localId text", [
        (box(19.0, 48.0, 19.0001, 48.0001), cond + "functional", "a"),
        (box(19.0002, 48.0, 19.0003, 48.0001), None, "b"),
        (box(19.0004, 48.0, 19.0005, 48.0001), cond + "demolished", "c"),  # zbořená, ne
        (box(19.0006, 48.0, 19.0007, 48.0001), cond + "declined", "d"),  # chátrající, ne
        (Point(19.0008, 48.00005), None, "e"),  # bod, ne
        (box(19.5, 48.5, 19.5001, 48.5001), None, "f"),  # mimo výřez
    ])
    con.commit()
    con.close()


def _tn(path):
    form = "http://inspire.ec.europa.eu/codelist/FormOfWayValue/"
    con = sqlite3.connect(path)
    _table(con, "RoadLink", "id text", [
        (LineString([(0, 0), (100, 0)]), "RoadLink.1"),
        (LineString([(0, 50), (100, 50)]), "RoadLink.2"),  # polní cesta
        (LineString([(0, 90), (100, 90)]), "RoadLink.3"),  # bez šířky
    ])
    con.execute('create table "RoadWidth" (networkRef text, width real)')
    con.executemany('insert into "RoadWidth" values (?, ?)', [("RoadLink.1", 6.0), ("RoadLink.2", 3.0)])
    con.execute('create table "FormOfWay" (networkRef text, formOfWay text)')
    con.executemany('insert into "FormOfWay" values (?, ?)', [("RoadLink.1", form + "singleCarriageway"), ("RoadLink.2", form + "tractorRoad")])
    con.commit()
    con.close()


def test_buildings_in_bbox_without_ruins_and_points(tmp_path):
    _bu(tmp_path / "bu.gpkg")
    feats = zbgis_sk.read_buildings(tmp_path / "bu.gpkg", (18.99, 47.99, 19.01, 48.01))
    assert sorted(f["properties"]["id"] for f in feats) == ["a", "b"]


def test_roads_with_width_become_carriageway(tmp_path):
    _tn(tmp_path / "tn.gpkg")
    zbgis_sk._add_indexes(tmp_path / "tn.gpkg")
    roads = zbgis_sk.read_roads(tmp_path / "tn.gpkg", (-1, -1, 101, 101))
    assert {(f["properties"]["width"], f["properties"]["form"]) for f in roads} == {
        (6.0, "singleCarriageway"), (3.0, "tractorRoad"), (None, ""),
    }
    polys = zbgis_sk.road_polygons(roads)  # polní cesta a osa bez šířky se nekreslí
    assert len(polys) == 1
    xs = [p[0] for p in polys[0][0]]
    ys = [p[1] for p in polys[0][0]]
    assert (min(xs), max(xs), min(ys), max(ys)) == pytest.approx((0, 100, -3, 3))


def _zip_with(member: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member, b"gpkg")
    return buf.getvalue()


@pytest.mark.parametrize(
    "later_days, remote, downloads",
    [
        (1, None, 1),  # do 30 dní bez dotazu na server
        (31, {"last_modified": "A", "size": 1}, 1),  # stejné vydání: nestahuje se znovu
        (31, {"last_modified": "B", "size": 2}, 2),  # novější vydání
        (31, OSError("down"), 1),  # server nedostupný: data z cache
    ],
)
def test_dataset_cache_checks_new_release_monthly(tmp_path, monkeypatch, later_days, remote, downloads):
    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    url, member = zbgis_sk.DATASETS["bu"]
    fetched = []

    def download(u, dest):
        fetched.append(u)
        dest.write_bytes(_zip_with(member))
        return dest.stat().st_size

    def head(_u):
        if isinstance(state["remote"], Exception):
            raise state["remote"]
        return state["remote"]

    state = {"remote": {"last_modified": "A", "size": 1}}
    t0 = 1_000_000.0
    path = zbgis_sk.ensure_dataset("bu", head=head, download=download, now=t0)
    assert path == tmp_path / zbgis_sk.CACHE_SUBDIR / "bu" / member and path.read_bytes() == b"gpkg"
    state["remote"] = remote if remote is not None else pytest.fail
    assert zbgis_sk.ensure_dataset("bu", head=head, download=download, now=t0 + later_days * 86400) == path
    assert fetched == [url] * downloads
    assert not list(path.parent.glob("*.part")) and not list(path.parent.glob("*.tmp"))


def test_zbgis_cache_is_regular_aoi_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    roots = dict(disk_space.aoi_cache_roots())
    assert roots[tmp_path / zbgis_sk.CACHE_SUBDIR] == settings.SK_ZBGIS_CACHE_MAX_AGE_DAYS > 0
