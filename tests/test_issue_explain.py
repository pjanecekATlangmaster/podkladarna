"""Chyby jobu v řeči uživatele: kategorie, dopad na mapu a doporučený postup."""

from __future__ import annotations

import pytest

from app.pipeline import issue_explain as ie


@pytest.mark.parametrize(
    "text, category, advice, what",
    [
        ("OSM: 2 z 9 dlaždic se nepodařilo stáhnout z žádného zdroje", ie.SEVERE, ie.RETRY, "část cest"),
        ("OSM: přeskočeno (timeout)", ie.SEVERE, ie.RETRY, "cesty a objekty z OpenStreetMap"),
        ("OSM 501: stahování na pozadí se nespustilo (x)", ie.SEVERE, ie.RETRY, "zpevněné plochy"),
        ("DTM: příprava na pozadí se nespustila (x)", ie.SEVERE, ie.RETRY, "Digitální technické mapy"),
        ("RÚIAN budovy: přeskočeno (HTTP 500)", ie.SEVERE, ie.RETRY, "budovy z RÚIAN"),
        ("Srázy: chybí DEM (dem_filled.tif) – přeskočeno", ie.SEVERE, ie.REFRESH, "srázy (chybí výškový model)"),
        ("Vegetace: nevznikla (hustota bodů ani CHM)", ie.SEVERE, ie.REFRESH, "porosty"),
        ("Kupky a ďolíky: přeskočeno (KeyError)", ie.SEVERE, ie.CONTACT, "kupky a ďolíky"),
        ("OOM: MTBO 1:10000 nevytvořeno (boom)", ie.SEVERE, ie.CONTACT, "mapa MTBO 1:10000"),
        ("Výstup: žádný ZIP ani PNG", ie.SEVERE, ie.CONTACT, "výsledný soubor"),
        (
            "[podklady] DTM kraje – plochy (ZPS): přeskočeno (WMS HTTP 503)",
            ie.MISSING, ie.RETRY, "DTM kraje – plochy (ZPS)",
        ),
        ("[podklady] Ortofoto: přeskočeno (HTTP 503)", ie.MISSING, ie.RETRY, "ortofoto"),
        ("Katastr KÚ 123456: přeskočeno (x)", ie.MISSING, ie.RETRY, "katastrální mapa"),
        ("Stínování: WMS ČÚZK nevrátil žádnou vrstvu", ie.MISSING, ie.RETRY, "stínování"),
        ("OOM OCD: přeskočeno (les.omap): err", ie.MISSING, ie.REPORT, "OCAD"),
        ("OOM: proces mapy spadl, chybějící mapy se dostaví sériově.", ie.OTHER, ie.NONE, "souběžný"),
        ("Něco úplně nového: selhalo", ie.OTHER, ie.REPORT, ""),
    ],
)
def test_explain_issue(text, category, advice, what):
    got = ie.explain_issue(text)
    assert (got.category, got.advice) == (category, advice)
    assert what in got.what
    assert got.technical == text


def test_summary_only_missing_layers_says_map_is_fine():
    text = ie.user_summary(
        [
            "[podklady] DTM kraje – plochy (ZPS): přeskočeno (503)",
            "[podklady] DTM kraje – linie a body (ZPS): přeskočeno (503)",
        ]
    )
    assert text.startswith("Mapa je v pořádku, chybí jen některé podkladové vrstvy: DTM kraje – plochy")
    assert ie.ADVICE_TEXT[ie.RETRY] in text
    assert "503" not in text and ie.DETAILS_HINT in text


def test_summary_severe_picks_strongest_advice():
    text = ie.user_summary(
        ["Ortofoto: přeskočeno (503)", "OSM: přeskočeno (x)", "Srázy: přeskočeno (ValueError)"]
    )
    assert text.startswith("Mapa je hotová, ale je neúplná – chybí: cesty a objekty z OpenStreetMap, srázy.")
    assert "Chybí také podkladové vrstvy: ortofoto." in text
    assert ie.ADVICE_TEXT[ie.CONTACT] in text and ie.ADVICE_TEXT[ie.RETRY] not in text
    assert len(text) <= ie.SUMMARY_MAX_CHARS


@pytest.mark.parametrize(
    "technical, expected",
    [
        ("Job překročil časový limit 90 min – ukončeno, fronta pokračuje.", "trval příliš dlouho"),
        ("Přerušeno restartem serveru (uvicorn reload / pád procesu)", "restart serveru"),
        ("ZABAGED (polohopis) není k dispozici – bez něj nelze dokončit mapu.", "dočasný výpadek"),
        ("HTTPSConnectionPool: Max retries exceeded", "dočasný výpadek"),
        ("Po ořezu nezůstaly žádné DMR (ground) body – výřez mimo listy?", "LiDAR"),
        ("Pipeline skončila s kódem -9", "paměť"),
        ("KeyError: 'geometry'", "chyba Podkladárny"),
    ],
)
def test_failure_summary(technical, expected):
    text = ie.failure_summary(technical)
    assert text.startswith("Mapu se nepodařilo vytvořit.")
    assert expected in text
