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
    "fr_pdv": TechnicalMap(
        key="fr_pdv",
        short="PDV",
        step="data Plan de voirie",
        background="Plan de voirie (Ville de Paris)",
        content="vozovky, chodníky, smíšené plochy, schodiště, ostrůvky, zeleň",
        module="app.pipeline.pdv_paris",
    ),
}


def foreign(key: str) -> TechnicalMap | None:
    return FOREIGN.get(key)
