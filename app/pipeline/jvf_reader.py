"""Proudová čtečka JVF DTM (``*.jvf.xml`` v ZIPu) – vybrané typy objektů.

JVF je XML (JVFDTM 1.4.x, GML 3.2) s jmennými prostory typu ``objtyp``/``plotli``/``atr``
(URI = prefix). Soubory mají desítky až stovky MB, proto se čte ``iterparse``em
a po zpracování každého záznamu se paměť uvolňuje.

Jeden objekt = ``<objtyp:Plot>`` s mnoha ``<…:ZaznamObjektu>``; každý záznam nese
atributy (``atr:*``) a geometrii (``gml:LineString`` / ``gml:Polygon`` / ``gml:Point``,
``posList`` po trojicích x y z v EPSG:5514).
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

_OBJTYP = "objtyp"


@dataclass
class JvfRecord:
    type_name: str  # např. Plot, Zed, ChodnikPlocha
    attrs: dict[str, str] = field(default_factory=dict)
    geom_type: str = ""  # LineString | Polygon | Point
    # LineString: [(x, y[, z]), …]; Polygon: [outer, hole, …]; Point: [(x, y[, z])]
    coords: list = field(default_factory=list)

    @property
    def level(self) -> int:
        """UrovenUmisteniObjektuZPS: −1 pod povrchem, 0 na povrchu, 1+ nad povrchem."""
        try:
            return int(float(self.attrs.get("UrovenUmisteniObjektuZPS", "0") or 0))
        except ValueError:
            return 0


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _pos_list(text: str, dim: int) -> list[tuple[float, ...]]:
    vals = [float(v) for v in text.split()]
    dim = dim if dim in (2, 3) else 3
    return [tuple(vals[i : i + dim]) for i in range(0, len(vals) - dim + 1, dim)]


def _geometry(zaznam: ET.Element) -> tuple[str, list]:
    """Geometrie záznamu (první nalezená); ('', []) když chybí nebo je nepodporovaná."""
    for el in zaznam.iter():
        name = _local(el.tag)
        if name == "LineString":
            dim = int(el.get("srsDimension") or 3)
            pl = next((c for c in el.iter() if _local(c.tag) == "posList"), None)
            return "LineString", _pos_list(pl.text or "", dim) if pl is not None else []
        if name == "Polygon":
            dim = int(el.get("srsDimension") or 3)
            rings = []
            for ring_parent in el:
                for pl in ring_parent.iter():
                    if _local(pl.tag) == "posList":
                        rings.append(_pos_list(pl.text or "", dim))
            return "Polygon", rings
        if name == "Point":
            dim = int(el.get("srsDimension") or 3)
            pos = next((c for c in el.iter() if _local(c.tag) == "pos"), None)
            return "Point", _pos_list(pos.text or "", dim) if pos is not None else []
    return "", []


def _attributes(zaznam: ET.Element) -> dict[str, str]:
    """Listové prvky z ``AtributyObjektu`` (bez prefixů); text bez bílých znaků."""
    out: dict[str, str] = {}
    for attrs in zaznam.iter():
        if _local(attrs.tag) != "AtributyObjektu":
            continue
        for el in attrs.iter():
            if len(el) == 0 and el is not attrs:
                out[_local(el.tag)] = (el.text or "").strip()
        break
    return out


def _open_xml(source: str | Path | io.BufferedIOBase):
    """Vrátí (souborový objekt, uzavírač) pro .zip s jedním .xml, nebo samotný .xml."""
    if isinstance(source, (str, Path)) and str(source).lower().endswith(".zip"):
        zf = zipfile.ZipFile(source)
        names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if not names:
            zf.close()
            raise ValueError(f"ZIP {source} neobsahuje .xml")
        fh = zf.open(names[0])
        return fh, lambda: (fh.close(), zf.close())
    if isinstance(source, (str, Path)):
        fh = open(source, "rb")
        return fh, fh.close
    return source, lambda: None


def iter_records(
    source: str | Path | io.BufferedIOBase,
    object_types: set[str] | frozenset[str],
) -> Iterator[JvfRecord]:
    """Záznamy vybraných typů objektů (``objtyp:<Typ>``), po jednom, ve streamu."""
    fh, close = _open_xml(source)
    try:
        current: str | None = None
        data_el: ET.Element | None = None
        seen = 0
        for event, el in ET.iterparse(fh, events=("start", "end")):
            name = _local(el.tag)
            is_type = el.tag.startswith("{" + _OBJTYP + "}")
            if event == "start":
                if is_type and name == "Data":
                    data_el = el
                elif is_type and name in object_types:
                    current = name
                continue
            if name == "ZaznamObjektu":
                if current is not None:
                    gtype, coords = _geometry(el)
                    yield JvfRecord(current, _attributes(el), gtype, coords)
                el.clear()
                seen += 1
                if data_el is not None and seen % 2000 == 0:
                    data_el.clear()  # průběžně uvolnit i prázdné záznamy (paměť u stovek MB)
            elif is_type and name == current:
                current = None
    finally:
        close()
