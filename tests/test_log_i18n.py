"""Anglický technický log jobu na Omapicu (``app/log_i18n.py``); česky beze změny."""

from __future__ import annotations

import re

import pytest

from app import log_i18n
from app.log_i18n import localize_log_lines, translate_line

_CZ_RE = re.compile(r"[ěščřžýáíůúňťďĚŠČŘŽÝÁÍŮÚŇŤĎ]")
# Vlastní jména s českými písmeny (úřad, zdroj) nejsou nepřeložený text.
_PROPER_NAMES = ("ÚGKK", "RÚIAN", "ČÚZK")


class _CZ:
    @staticmethod
    def search(text: str):
        for name in _PROPER_NAMES:
            text = text.replace(name, "")
        return _CZ_RE.search(text)

# Výřez skutečného logu zahraničního jobu (Fontainebleau, 2026-11) a typické hlášky o zdrojích a chybách.
SAMPLE_LOG = """\
Přijato: preset forest_10000 · 1:10000 · ekvidistance 5.0 m · země Francie · mapa les
  Terén: vegetace 2.0 m · tvar vegetace 1 · vrstevnice adaptivní · kresba sprint OSM/les+MTBO OSM · srázy low/auto · pomocné vrstevnice ne
  OSM: objekty základní ve sprintu · ploty a zdi vždy · footway jako chodník ne
  Sídla: residential 501 ne/small · ostatní plocha small
  Výstup: ZIP · podklady stažené (Sentinel-2 ano) · georef GeoTIFF ne · vynucené obnovení ne · privátní ne
Job ve fronte – pozice 1.
Start jobu 254e642e5f3e · preset forest_10000
Plán (17 kroků): mřížka mapy · výškové modely · vrstevnice · vegetace · srázy · kupky a ďolíky · OSM pěšiny a objekty · stínování reliéfu · referenční podklady · data BGT · polohopis a vegetace · silnice a cesty · ploty, budovy a oliva · slučování zdrojů a odsun od cest · zjednodušení a zápis mapy · převod do OCAD · náhled mapy · balení ZIP
Země výřezu: Francie – kresba z OSM, výšky z LiDAR HD MNT a MNS (IGN, 0,5 m).
[OSM] Start stahování OSM na pozadí; výsledek se převezme v kroku OSM.
[OSM] Na pozadí: Data OpenStreetMap · běží
Kresba: z OSM (technická mapa pro toto území není).
[1/17] Mřížka mapy · 0:00
  Kanonická mřížka EPSG:2154: [670361.0,672654.0]×[6811110.0,6812853.0] 2293×1743 px @ 1 m → job_grid.json, job.pgw
[2/17] Výškové modely · 0:00
  Stahuji výšková data (LiDAR HD MNT a MNS, IGN): terén a povrch jako rastr…
[OSM] OSM Overpass (overpass.openstreetmap.fr): 242 prvků
[OSM] Na pozadí: Data OpenStreetMap · hotovo za 0:07
  Terén z rastru: 100% buněk s daty, rozlišení 1 m
[3/17] Vrstevnice · 0:18
  Vyhlazuji DEM (plynulejší vrstevnice)…
  Kreslím vrstevnice z DEM (ekvidistance do mapy)…
[4/17] Vegetace · 0:20
  Klasifikuji vegetaci z CHM (podle výšky porostu)…
  Vegetace: 7440 polygonů → vegetation.shp (CHM open<1 m, white>=6 m)
[5/17] Srázy – přeskočeno (vypnuto)
[7/17] OSM pěšiny a objekty · 0:25
  OSM: převzato z pozadí (242 prvků)
  Na pozadí: Snímky Sentinel-2 · běží
  Sentinel-2 2026-10-31: mraky / sníh / bez dat ve výřezu 0.0 %
[BGT] BGT budovy: 120 ploch
[BGT] BGT chodníky (voetpad): 40 ploch
[BGT] BGT ploty: 12 úseků → 9 napojených čar, 3 oplocení k vyplnění; soukromé pozemky (erf) jako oliva: 2
[BGT] Na pozadí: Data BGT (PDOK) · hotovo za 0:12
Kresba z GRB: ano – GRB (budovy, uliční prostor, vozovky a chodníky (Brusel z UrbIS, i parkování v ulici)) se stahuje na pozadí, do mapy jde při sestavení.
  OOM: typ mapy – jen les (vybráno v GUI)
  Silnice: napojení v křižovatkách 100.0 % (3 z 3 kreslených ramen, 0 děr, 0 končí na ploše, 0 z 1 ústí proti pravidlu, 4 křižovatek OSM)
  OOM OCD: přeskočeno (fontaineblea-les.omap) – není PODKLADARNA_MAPPER_CONVERT / Mapper CLI
  ⚠ Ulice z katastru: přeskočeno (timeout)
⚠ Zdroj Sentinel-2 (AWS) při poslední kontrole neodpovídal (neodpovídá): Šablona se snímky Sentinel-2 bude chybět.
⚠ Dokončeno s chybami (1):
  Balím výstupní ZIP (mapy, vektory, referenční podklady – u velkých výřezů i několik minut)…
  Výstup: podkladarna_output.zip (30.76 MB)
Hotovo za 2:16.
CHYBA: Na serveru došlo místo na disku – uvolněte místo v /data (cache, staré joby).
"""


def test_czech_is_unchanged():
    rows = [{"id": 1, "line": "[3/17] Vrstevnice · 0:18", "at": "x"}]
    assert localize_log_lines(rows, "cs") is rows
    for line in SAMPLE_LOG.splitlines():
        assert translate_line(line, "cs") == line


def test_sample_log_has_no_czech_left():
    left = [translate_line(line, "en") for line in SAMPLE_LOG.splitlines()]
    assert [line for line in left if _CZ.search(line)] == []


@pytest.mark.parametrize(
    "line, expected",
    [
        ("[3/17] Vrstevnice · 0:18", "[3/17] Contours · 0:18"),
        ("Plán (3 kroků): mřížka mapy · data BGT · balení ZIP", "Plan (3 steps): map grid · BGT data · packing the ZIP"),
        ("[OSM] Na pozadí: Data OpenStreetMap · hotovo za 0:07", "[OSM] Background: OpenStreetMap data · done in 0:07"),
        ("Hotovo za 2:16, s chybami (2).", "Done in 2:16, with errors (2)."),
        ("  ⚠ Ulice z katastru: přeskočeno (timeout)", "  ⚠ Streets from the cadastre: skipped (timeout)"),
        ("[BGT] BGT chodníky (voetpad): 40 ploch", "[BGT] BGT sidewalks (voetpad): 40 areas"),
        ("Země výřezu: Francie – kresba z OSM, výšky z LiDAR HD MNT a MNS (IGN, 0,5 m).",
         "Country of the area: France – drawn from OSM, elevation from LiDAR HD MNT and MNS (IGN, 0,5 m)."),
        # Slovinsko (job c2a285e54bf3): zdroj výšek, zimní snímek, šablony OCD, souhrn chyb.
        ("    Slovinsko CLSS 546_151 (15.03.2024–30.04.2024): 11 MB převedeno do cache",
         "    Slovenia CLSS 546_151 (15.03.2024–30.04.2024): 11 MB converted into cache"),
        ("  - Slovinsko: CLSS (GURS) nedostupné (HTTPError: HTTP Error 429: Too Many Requests), výšky ze staršího "
         "mračna ARSO 2011–2015", "  - Slovenia: CLSS (GURS) unavailable (HTTPError: HTTP Error 429: Too Many Requests), "
         "elevation from the older ARSO 2011–2015 point cloud"),
        ("  Vegetace: stálezelený porost ze zimního snímku Sentinel-2 2026-02-28 (27 % výřezu)",
         "  Vegetation: evergreen canopy from the winter Sentinel-2 image 2026-02-28 (27 % of the area)"),
        ("  Vrstevnice ve vodě: 1 linií ořezáno na břehu nebo vynecháno (OSM vodní plochy)",
         "  Contours in water: 1 lines clipped at the shore or omitted (OSM water areas)"),
        ("  OOM OCD: šablony opraveny 6× (a.ocd), beze změny: vegetation_height.png",
         "  OOM OCD: templates fixed 6× (a.ocd), unchanged: vegetation_height.png"),
        # Příkazy a neznámé řádky zůstávají (nic se neztratí).
        ("  > gdal_contour -a elev -i 5.0 dem.tif contours.shp", "  > gdal_contour -a elev -i 5.0 dem.tif contours.shp"),
        ("  Úplně nová hláška bez pravidla", "  Úplně nová hláška bez pravidla"),
    ],
)
def test_translate_line(line, expected):
    assert translate_line(line, "en") == expected


def test_progress_lines_keep_czech_source():
    """Web čte průběh z ``src`` (PROGRESS_LINE_RE v app.js je česky)."""
    rows = [{"id": 1, "line": "[1/2] Mřížka mapy · 0:00", "at": "x"}, {"id": 2, "line": "  > gdalwarp a b", "at": "x"}]
    out = localize_log_lines(rows, "en")
    assert out[0] == {"id": 1, "line": "[1/2] Map grid · 0:00", "at": "x", "src": "[1/2] Mřížka mapy · 0:00"}
    assert out[1]["line"] == out[1]["src"] == "  > gdalwarp a b"


def test_lines_written_by_code_are_translated():
    """Řádky skládané kódem (technické mapy, zdroje na startu): změna české věty se tu projeví."""
    from app.pipeline.job_progress import MAP_PHASES, TECHNICAL_MAP_STEPS
    from app.pipeline.run_job import down_source_lines, technical_map_line
    from app.pipeline.technical_maps import FOREIGN

    lines = [technical_map_line(key, on) for key in FOREIGN for on in (True, False)]
    lines.append(technical_map_line("xx", True))
    svc = {"label": "OpenStreetMap (Overpass)", "detail": "neodpovídá", "impact": "x", "planned": False}
    lines += [text for _, text in down_source_lines([{**svc, "needs": "osm"}], dtm_needed=True)]
    lines += [f"Plán (9 kroků): {' · '.join((*MAP_PHASES, *TECHNICAL_MAP_STEPS.values()))}"]
    lines += [f"Na pozadí: {spec.background} · běží" for spec in FOREIGN.values()]
    untranslated = [line for line in lines if translate_line(line, "en") == line]
    assert untranslated == []
    assert [translate_line(line, "en") for line in lines if _CZ.search(translate_line(line, "en"))] == []


def test_api_log_follows_page_language(client):
    import app.db as db

    job = db.create_job("log", "forest_10000", {})
    db.append_log(job["id"], "[1/2] Mřížka mapy · 0:00")
    db.append_log(job["id"], "Hotovo za 0:05.")

    cs = client.get(f"/api/jobs/{job['id']}/log").json()["lines"]
    assert [row["line"] for row in cs] == ["[1/2] Mřížka mapy · 0:00", "Hotovo za 0:05."]
    assert all("src" not in row for row in cs)

    en = client.get(f"/api/jobs/{job['id']}/log?lang=en").json()["lines"]
    assert [row["line"] for row in en] == ["[1/2] Map grid · 0:00", "Done in 0:05."]
    assert [row["src"] for row in en] == ["[1/2] Mřížka mapy · 0:00", "Hotovo za 0:05."]


def test_rules_compile_with_unique_fields():
    assert len(log_i18n._compiled()) == len(log_i18n.RULES)


def test_long_lines_are_not_translated_quickly():
    """Dlouhý řádek (traceback) zůstane beze změny a nestojí sekundy (šablony s více ``.+?``)."""
    import time

    line = "x: " * 5000
    translate_line.cache_clear()
    start = time.monotonic()
    assert translate_line(line, "en") == line
    assert time.monotonic() - start < 0.5
