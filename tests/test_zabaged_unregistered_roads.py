"""ZABAGED Silnice neevidovaná: potvrzení DTM / ortofotem a čtení os pozemních komunikací z DTM sady DI – bez sítě."""

from __future__ import annotations

import io
import zipfile

import numpy as np
import pytest
from shapely.geometry import LineString, box

from app.pipeline import dtm_import
from app.pipeline import zabaged_unregistered_roads as zur

LINE = [(0.0, 0.0), (100.0, 0.0)]  # 100 m


def _band(x0: float, x1: float):
    """Predikát „bod v pásu x0..x1“ (plocha silnice z DTM bez bufferu)."""
    return zur.zone_predicate(box(x0, -3, x1, 3), distance_m=0.0)


def _ortho(x0: float, x1: float):
    """Ortofoto vypadá zpevněně jen mezi x0 a x1."""
    return lambda pts, tang: (pts[:, 0] >= x0) & (pts[:, 0] <= x1)


def _len(pieces):
    return sum(LineString(p).length for p in pieces)


@pytest.mark.parametrize(
    "in_dtm, ortho, kept_m, dtm_m, ortho_m",
    [
        (_band(-1, 101), None, 100, 100, 0),  # celá čára potvrzená plochou / osou DTM
        (_band(-1, 50.5), None, 50, 50, 0),  # zbytek bez ortofota se nekreslí
        (None, _ortho(-1, 101), 100, 0, 100),  # bez DTM rozhodne ortofoto
        (_band(-1, 40.5), _ortho(38, 101), 100, 40, 60),  # DTM + ortofoto navazují
        (_band(-1, 30.5), _ortho(60, 101), 70, 30, 40),  # mezera 30 m mezi potvrzeními se nezacelí (30 + 40 m)
        (None, None, 0, 0, 0),  # nic nepotvrzuje → vynecháno
        (_band(-1, 10.5), None, 0, 0, 0),  # potvrzený úsek pod 20 m se nekreslí
    ],
)
def test_confirm_line_sources(in_dtm, ortho, kept_m, dtm_m, ortho_m):
    pieces, st = zur.confirm_line(LINE, in_dtm, ortho)
    assert _len(pieces) == pytest.approx(kept_m, abs=3)
    assert st["dtm"] == pytest.approx(dtm_m, abs=3)
    assert st["ortho"] == pytest.approx(ortho_m, abs=3)
    assert st["dropped"] == pytest.approx(100 - kept_m, abs=3)


@pytest.mark.parametrize("length_m", [2.0, 9.0, 16.0])
def test_confirm_line_shorter_than_ortho_window(length_m):
    """Čára kratší než okno ortofota nespadne (dřív ValueError broadcast, job Zvoles) a pod 20 m se nekreslí."""
    pieces, st = zur.confirm_line([(0.0, 0.0), (length_m, 0.0)], None, _ortho(-1, 101))
    assert pieces == []
    assert st["dropped"] == pytest.approx(length_m, abs=zur.SAMPLE_M)


def test_confirm_line_closes_short_gap_and_keeps_one_piece():
    # DTM pokrývá 0–40 a 52–100 (mezera 12 m < 15 m) → jeden souvislý kus
    both = lambda xs, ys: _band(-1, 40.5)(xs, ys) | _band(52, 101)(xs, ys)  # noqa: E731
    pieces, st = zur.confirm_line(LINE, both)
    assert len(pieces) == 1
    assert _len(pieces) == pytest.approx(100, abs=3)


def test_ortho_paved_fn_gray_strip_vs_green():
    gt = (0.0, 0.5, 0.0, 10.0, 0.0, -0.5)  # 0,5 m / px, horní hrana y = 10
    rgb = np.zeros((20, 200, 3), dtype=np.uint8)
    rgb[:] = (40, 160, 40)  # tráva
    rgb[6:14, 0:100] = (120, 120, 120)  # šedý pás podél y ≈ 5 pro x < 50 m (px 0,5 m → x do 50)
    fn = zur.ortho_paved_fn(rgb, gt)
    pts = np.array([[20.0, 5.0], [80.0, 5.0]])
    tang = np.array([[1.0, 0.0], [1.0, 0.0]])
    assert fn(pts, tang).tolist() == [True, False]


def _di_zip(oznaceni: str = "III/1234") -> io.BytesIO:
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<objtyp:JVFDTM xmlns:objtyp="objtyp" xmlns:osa="osa" xmlns:cmn="cmn" xmlns:atr="atr"
    xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:xlink="http://www.w3.org/1999/xlink">
  <objtyp:DataJVFDTM>
    <objtyp:Data>
      <objtyp:OsaPozemniKomunikace>
        <osa:ZaznamyObjektu>
          <osa:ZaznamObjektu>
            <osa:AtributyObjektu>
              <atr:SpolecneAtributyVsechObjektu><atr:ID>531893833434</atr:ID></atr:SpolecneAtributyVsechObjektu>
              <atr:OznaceniKomunikace>{oznaceni}</atr:OznaceniKomunikace>
              <atr:KategoriePozemniKomunikace>5</atr:KategoriePozemniKomunikace>
            </osa:AtributyObjektu>
            <osa:GeometrieObjektu>
              <gml:curveProperty xlink:type="simple">
                <gml:LineString srsName="EPSG:5514" srsDimension="3" gml:id="a">
                  <gml:posList>-100.5 -200.25 10.0 -90.5 -200.25 10.5 -80.0 -190.0 11.0</gml:posList>
                </gml:LineString>
              </gml:curveProperty>
            </osa:GeometrieObjektu>
          </osa:ZaznamObjektu>
        </osa:ZaznamyObjektu>
      </objtyp:OsaPozemniKomunikace>
    </objtyp:Data>
  </objtyp:DataJVFDTM>
</objtyp:JVFDTM>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("di.xml", xml)
    buf.seek(0)
    return buf


def test_load_road_axes_from_di_snippet(tmp_path):
    path = tmp_path / "VS_CZ010_MOMC_1_20261031_DI.jvf.zip"
    path.write_bytes(_di_zip().getvalue())
    axes = dtm_import.load_road_axes(path)
    assert len(axes) == 1
    ax = axes[0]
    assert (ax["id"], ax["oznaceni"], ax["kategorie"]) == ("531893833434", "III/1234", "5")
    assert ax["coords"] == [(-100.5, -200.25), (-90.5, -200.25), (-80.0, -190.0)]
