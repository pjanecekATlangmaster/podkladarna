"""Čtečka JVF DTM – syntetický vzorek se stejnou strukturou jako reálné balíky."""

from __future__ import annotations

import io
import zipfile

from app.pipeline.jvf_reader import iter_records

XML = """<?xml version="1.0" encoding="utf-8"?>
<objtyp:JVFDTM xmlns:objtyp="objtyp" xmlns:plotli="plotli" xmlns:budpol="budpol"
    xmlns:cmn="cmn" xmlns:atr="atr" xmlns:gml="http://www.opengis.net/gml/3.2"
    xmlns:xlink="http://www.w3.org/1999/xlink">
  <objtyp:DataJVFDTM>
    <cmn:VerzeJVFDTM>1.4.3</cmn:VerzeJVFDTM>
    <objtyp:Data>
      <objtyp:Plot>
        <plotli:ObjektovyTypNazev code_base="0100000162" code_suffix="02">plot</plotli:ObjektovyTypNazev>
        <plotli:ZaznamyObjektu>
          <plotli:ZaznamObjektu>
            <plotli:AtributyObjektu>
              <atr:SpolecneAtributyVsechObjektu><atr:ID>1</atr:ID></atr:SpolecneAtributyVsechObjektu>
              <atr:SpolecneAtributyObjektuZPS>
                <atr:UrovenUmisteniObjektuZPS>0</atr:UrovenUmisteniObjektuZPS>
              </atr:SpolecneAtributyObjektuZPS>
              <atr:DruhPlotu>2</atr:DruhPlotu>
              <atr:HraniceJinehoObjektu>false</atr:HraniceJinehoObjektu>
            </plotli:AtributyObjektu>
            <plotli:GeometrieObjektu>
              <gml:curveProperty xlink:type="simple">
                <gml:LineString srsName="EPSG:5514" srsDimension="3" gml:id="a">
                  <gml:posList>-100.5 -200.25 10.0 -90.5 -200.25 10.5</gml:posList>
                </gml:LineString>
              </gml:curveProperty>
            </plotli:GeometrieObjektu>
          </plotli:ZaznamObjektu>
          <plotli:ZaznamObjektu>
            <plotli:AtributyObjektu>
              <atr:SpolecneAtributyObjektuZPS>
                <atr:UrovenUmisteniObjektuZPS>-1</atr:UrovenUmisteniObjektuZPS>
              </atr:SpolecneAtributyObjektuZPS>
              <atr:DruhPlotu>5</atr:DruhPlotu>
            </plotli:AtributyObjektu>
            <plotli:GeometrieObjektu>
              <gml:curveProperty>
                <gml:LineString srsName="EPSG:5514" srsDimension="2">
                  <gml:posList>0 0 1 1 2 0</gml:posList>
                </gml:LineString>
              </gml:curveProperty>
            </plotli:GeometrieObjektu>
          </plotli:ZaznamObjektu>
        </plotli:ZaznamyObjektu>
      </objtyp:Plot>
      <objtyp:BudovaPlocha>
        <budpol:ZaznamyObjektu>
          <budpol:ZaznamObjektu>
            <budpol:AtributyObjektu><atr:ID>9</atr:ID></budpol:AtributyObjektu>
            <budpol:GeometrieObjektu>
              <gml:surfaceProperty>
                <gml:Polygon srsName="EPSG:5514" srsDimension="2">
                  <gml:exterior><gml:LinearRing>
                    <gml:posList>0 0 10 0 10 10 0 10 0 0</gml:posList>
                  </gml:LinearRing></gml:exterior>
                  <gml:interior><gml:LinearRing>
                    <gml:posList>2 2 4 2 4 4 2 4 2 2</gml:posList>
                  </gml:LinearRing></gml:interior>
                </gml:Polygon>
              </gml:surfaceProperty>
            </budpol:GeometrieObjektu>
          </budpol:ZaznamObjektu>
        </budpol:ZaznamyObjektu>
      </objtyp:BudovaPlocha>
    </objtyp:Data>
  </objtyp:DataJVFDTM>
</objtyp:JVFDTM>
"""


def _zip_bytes() -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("VS_CZ010_MOMC_500143_20260930_ZPS_KR.jvf.xml", XML)
    buf.seek(0)
    return buf


def test_reads_only_requested_types_with_attributes_and_geometry(tmp_path):
    recs = list(iter_records(io.BytesIO(XML.encode("utf-8")), {"Plot"}))
    assert [r.type_name for r in recs] == ["Plot", "Plot"]
    first, second = recs
    assert first.geom_type == "LineString"
    assert first.coords == [(-100.5, -200.25, 10.0), (-90.5, -200.25, 10.5)]
    assert first.attrs["DruhPlotu"] == "2"
    assert first.attrs["HraniceJinehoObjektu"] == "false"
    assert first.level == 0
    # 2D geometrie a záporná úroveň (pod povrchem)
    assert second.coords == [(0.0, 0.0), (1.0, 1.0), (2.0, 0.0)]
    assert second.level == -1


def test_reads_polygon_with_hole_and_ignores_other_types():
    recs = list(iter_records(io.BytesIO(XML.encode("utf-8")), {"BudovaPlocha"}))
    assert len(recs) == 1
    r = recs[0]
    assert r.geom_type == "Polygon"
    assert len(r.coords) == 2  # vnější obrys + díra
    assert r.coords[0][0] == (0.0, 0.0) and r.coords[1][0] == (2.0, 2.0)
    assert list(iter_records(io.BytesIO(XML.encode("utf-8")), {"Neexistuje"})) == []


def test_reads_from_zip_and_xml_files(tmp_path):
    zpath = tmp_path / "VS_test.jvf.zip"
    zpath.write_bytes(_zip_bytes().getvalue())
    assert len(list(iter_records(zpath, {"Plot"}))) == 2
    xpath = tmp_path / "VS_test.jvf.xml"
    xpath.write_text(XML, encoding="utf-8")
    assert len(list(iter_records(xpath, {"Plot", "BudovaPlocha"}))) == 3
