"""Testy čistých českých OCAD sad (*_cz.omap) a katalogu symbolů."""

from __future__ import annotations

import re
from pathlib import Path
from xml.sax.saxutils import unescape

import pytest
import yaml

from app.pipeline.oom_symbols import _top_level_symbols
from app.pipeline.symbol_catalog import normalize_code

OOM = Path(__file__).resolve().parent.parent / "configs" / "oom"
CATALOG = OOM / "symbol_catalog.yaml"

# (klíč katalogu, soubor sady)
SETS = [
    ("isom", "ISOM_2017_10000_cz.omap"),
    ("isom", "ISOM_2017_15000_cz.omap"),
    ("sprint", "ISSprOM_2019_4000_cz.omap"),
]

# Přiznaně hrubá heuristika: běžná anglická slova, která se v českých názvech
# nevyskytují (záměrně bez slov shodných s češtinou: start, text, pole, most…).
_ENGLISH_WORDS = frozenset(
    {
        "the", "of", "and", "with", "without", "or", "to", "line", "lines", "area",
        "minimum", "boulder", "boulders", "cliff", "road", "path", "footpath",
        "wall", "fence", "water", "marsh", "bank", "earth", "gully", "pit",
        "contour", "ground", "rock", "stony", "sandy", "forest", "vegetation",
        "walk", "fight", "tree", "trees", "bush", "building", "tower", "stairway",
        "railway", "bridge", "tunnel", "border", "boundary", "outline", "main",
        "double", "single", "dot", "large", "small", "wide", "narrow", "paved",
        "unpaved", "open", "land", "slow", "running", "good", "visibility",
        "bounds", "control", "finish", "point", "number", "spot", "height",
        "hedge", "ruin", "canopy", "spring", "well", "hill", "depression",
        "dashed", "solid", "light", "heavy", "traffic", "level", "lower", "upper",
        "tag", "tags", "label", "index", "form", "knoll", "broken", "impassable",
        "uncrossable", "crossable", "crossing", "magnetic", "north", "black",
        "blue", "seasonal", "shallow", "dominant", "prominent", "feature",
        "gigantic", "dense", "difficult", "normal", "scattered", "rough",
        "cultivated", "orchard", "vineyard", "similar", "ride", "linear",
        "trace", "track", "vehicle", "power", "major", "pylon", "square",
        "retaining", "ruined", "passable", "face", "cave", "dangerous",
        "refreshment", "first", "aid", "post", "issue", "route", "course",
        "marked", "out", "bounds", "temporary", "construction", "closed",
        "continuing", "exchange", "registration", "mark", "edge", "step",
        "pillar", "cairn", "fodder", "rack", "ring", "man", "made",
    }
)


def _words(name: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[^\W\d_]+", name)]


def _symbols_xml(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(r'<symbols\s+count="\d+"[^>]*>.*?</symbols>', text, re.DOTALL)
    assert m, f"{path.name}: chybí <symbols>"
    return m.group(0)


def _top_codes(path: Path) -> list[str]:
    out = []
    for _s, _e, attrs in _top_level_symbols(_symbols_xml(path)):
        m = re.search(r'\bcode="([^"]*)"', attrs)
        assert m, f"{path.name}: symbol bez code"
        out.append(m.group(1))
    return out


def _top_names(path: Path) -> dict[str, str]:
    out = {}
    for _s, _e, attrs in _top_level_symbols(_symbols_xml(path)):
        code = re.search(r'\bcode="([^"]*)"', attrs).group(1)
        name = re.search(r'\bname="([^"]*)"', attrs)
        out[code] = unescape(name.group(1)) if name else ""
    return out


def _all_names(path: Path) -> list[str]:
    """Názvy všech symbolů včetně vnořených částí."""
    return [
        unescape(n) for n in re.findall(r'<symbol\b[^>]*\bname="([^"]*)"', _symbols_xml(path))
    ]


@pytest.fixture(scope="module")
def catalog() -> dict[str, list[dict]]:
    return yaml.safe_load(CATALOG.read_text(encoding="utf-8"))


@pytest.mark.parametrize("key,fname", SETS)
def test_top_level_codes_unique(key: str, fname: str) -> None:
    codes = _top_codes(OOM / fname)
    assert len(codes) == len(set(codes)), f"{fname}: duplicitní kódy nejvyšší úrovně"
    # normalizace .0 nesmí vytvořit kolizi (401.0 vs 401)
    norm = [normalize_code(c) for c in codes]
    assert len(norm) == len(set(norm)), f"{fname}: kolize po normalizaci kódu"


@pytest.mark.parametrize("key,fname", SETS)
def test_no_hidden_symbols(key: str, fname: str) -> None:
    text = (OOM / fname).read_text(encoding="utf-8")
    assert "is_hidden" not in text


@pytest.mark.parametrize("key,fname", SETS)
def test_catalog_matches_set(catalog: dict, key: str, fname: str) -> None:
    cat_codes = [e["ocad_code"] for e in catalog[key]]
    assert len(cat_codes) == len(set(cat_codes)), f"{key}: duplicitní kódy v katalogu"
    assert set(cat_codes) == set(_top_codes(OOM / fname)), fname


@pytest.mark.parametrize("key,fname", SETS)
def test_set_names_match_catalog(catalog: dict, key: str, fname: str) -> None:
    names = _top_names(OOM / fname)
    for e in catalog[key]:
        assert names[e["ocad_code"]] == e["cz_name"], e["ocad_code"]


@pytest.mark.parametrize("key,fname", SETS)
def test_part_references_valid(key: str, fname: str) -> None:
    """<part symbol="N"> musí odkazovat na existující id symbolu."""
    xml = _symbols_xml(OOM / fname)
    ids = set(re.findall(r'<symbol\b[^>]*\bid="(\d+)"', xml))
    for ref in re.findall(r'<part symbol="(\d+)"', xml):
        assert ref in ids, f"{fname}: part odkazuje na neexistující id {ref}"


@pytest.mark.parametrize("key,fname", SETS)
def test_no_english_names(key: str, fname: str) -> None:
    bad = []
    for name in _all_names(OOM / fname):
        hits = [w for w in _words(name) if w in _ENGLISH_WORDS]
        if hits:
            bad.append((name, hits))
    assert not bad, f"{fname}: anglické názvy: {bad[:10]}"


def test_catalog_names_not_english(catalog: dict) -> None:
    for key, entries in catalog.items():
        for e in entries:
            hits = [w for w in _words(e["cz_name"]) if w in _ENGLISH_WORDS]
            assert not hits, (key, e["ocad_code"], e["cz_name"], hits)


def test_isom_10k_and_15k_same_codes_and_names() -> None:
    n10 = _top_names(OOM / "ISOM_2017_10000_cz.omap")
    n15 = _top_names(OOM / "ISOM_2017_15000_cz.omap")
    assert list(n10) == list(n15)
    assert n10 == n15


def test_catalog_cz_name_nonempty(catalog: dict) -> None:
    for key, entries in catalog.items():
        for e in entries:
            assert isinstance(e["cz_name"], str) and e["cz_name"].strip(), (key, e["ocad_code"])
            assert e["geom"] in {"point", "line", "area", "text"}, (key, e["ocad_code"])
            assert isinstance(e["review"], bool), (key, e["ocad_code"])
            assert isinstance(e["generator_codes"], list), (key, e["ocad_code"])
