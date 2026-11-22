"""Obdoby české DTM v cizích zemích (``technical_map`` země v ``configs/countries.yaml``).

Každý zdroj zapíše do ``work_dir/dtm`` stejné soubory jako příprava DTM ČÚZK (``dtm_import.prepare_dtm_fences``),
takže kresba budov, silnic, chodníků, schodů a plotů jde v Česku i jinde stejnou cestou. Tady jsou jen popisky
(krok průběhu, log, úloha na pozadí) a modul s funkcí ``prepare(work_dir, bbox_wgs84, *, log, force)``.
Modul se importuje až při spuštění přípravy (``prepare_fn``), aby import průběhu jobu zůstal lehký.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class TechnicalMap:
    key: str  # hodnota ``technical_map`` v countries.yaml
    short: str  # zkratka do logu („BGT“, „AV“)
    step: str  # krok průběhu jobu místo „DTM kraje“ (web ho překládá, ``web.js.step.*``)
    background: str  # název úlohy „Na pozadí: …“
    content: str  # co se z ní kreslí (řádek logu „Kresba z …“)
    module: str  # modul s funkcí ``prepare``

    def prepare_fn(self) -> Callable:
        return getattr(importlib.import_module(self.module), "prepare")


FOREIGN: dict[str, TechnicalMap] = {
    "nl_bgt": TechnicalMap(
        key="nl_bgt",
        short="BGT",
        step="data BGT",
        background="Data BGT (PDOK)",
        content="budovy, silnice, chodníky, parkoviště, ploty, soukromé pozemky",
        module="app.pipeline.bgt_nl",
    ),
    "ch_av": TechnicalMap(
        key="ch_av",
        short="AV",
        step="data AV",
        background="Data AV (geodienste.ch)",
        content="budovy, silnice, chodníky, schody, mosty, zahrady",
        module="app.pipeline.av_ch",
    ),
    "at_fmzk": TechnicalMap(
        key="at_fmzk",
        short="FMZK",
        step="data FMZK",
        background="Data FMZK (Stadt Wien)",
        content="budovy, vozovky, chodníky, parkování, schody, dvory a zahrady",
        module="app.pipeline.fmzk_at",
    ),
    "de_sb": TechnicalMap(
        key="de_sb",
        short="SB",
        step="data Straßenbefahrung",
        background="Straßenbefahrung a ALKIS (GDI Berlin)",
        content="budovy, vozovky, chodníky, náměstí, parkování, schody, cyklostezky",
        module="app.pipeline.sb_berlin",
    ),
    "de_alkis": TechnicalMap(
        key="de_alkis",
        short="ALKIS",
        step="data ALKIS",
        background="Data ALKIS (katastr země)",
        content="budovy, uliční prostor, náměstí a pěší zóny, parkoviště",
        module="app.pipeline.alkis_de",
    ),
    "fr_pdv": TechnicalMap(
        key="fr_pdv",
        short="PDV",
        step="data Plan de voirie",
        background="Plan de voirie (Ville de Paris)",
        content="vozovky, chodníky, smíšené plochy, schodiště, ostrůvky, zeleň",
        module="app.pipeline.pdv_paris",
    ),
    "fr_bdtopo": TechnicalMap(
        key="fr_bdtopo",
        short="BD TOPO",
        step="data BD TOPO",
        background="Data BD TOPO (IGN)",
        content="budovy, vozovky podle šířky silnic",
        module="app.pipeline.bdtopo_fr",
    ),
    "ee_etak": TechnicalMap(
        key="ee_etak",
        short="ETAK",
        step="data ETAK",
        background="Data ETAK (Maa- ja Ruumiamet)",
        content="budovy, uliční prostor, pěší plochy, parkoviště, zeleň, dvory a zahrady, ploty",
        module="app.pipeline.etak_ee",
    ),
    "be_grb": TechnicalMap(
        key="be_grb",
        short="GRB",
        step="data GRB",
        background="Data GRB (Digitaal Vlaanderen)",
        content="budovy, uliční prostor, vozovky a chodníky (Brusel z UrbIS, i parkování v ulici)",
        module="app.pipeline.grb_be",
    ),
    "dk_geodanmark": TechnicalMap(
        key="dk_geodanmark",
        short="GeoDanmark",
        step="data GeoDanmark",
        background="Data GeoDanmark (Datafordeler)",
        content="budovy, vozovky z okrajů silnic, parkoviště, ploty",
        module="app.pipeline.geodanmark_dk",
    ),
    "sk_zbgis": TechnicalMap(
        key="sk_zbgis",
        short="ZBGIS",
        step="data ZBGIS",
        background="Data ZBGIS (ÚGKK SR)",
        content="budovy, vozovky podle šířky silnic",
        module="app.pipeline.zbgis_sk",
    ),
    "fi_hel": TechnicalMap(
        key="fi_hel",
        short="YLRE",
        step="data YLRE",
        background="Data YLRE (Helsingin kaupunki)",
        content="budovy, uliční prostor, vozovky, chodníky, parkoviště, schody, trávníky",
        module="app.pipeline.ylre_fi",
    ),
    "us_city": TechnicalMap(
        key="us_city",
        short="Planimetrics",
        step="data Planimetrics",
        background="Planimetrics (NYC, Washington DC)",
        content="budovy, vozovky, chodníky, pěší plochy, parkoviště, ostrůvky",
        module="app.pipeline.city_us",
    ),
}


def foreign(key: str) -> TechnicalMap | None:
    return FOREIGN.get(key)
