"""Které obrázky podkladů smí do ZIPu (Petr 2026-11-16).

Prodává se vytvořená mapa (.omap), referenční obrázky v ``references/`` jsou jen doprovod. Obrázek zdroje se do
ZIPu přibalí všude, kde to licence výslovně nezakazuje. U zdrojů s ``image_in_zip: forbidden`` v registru
``configs/data_sources.yaml`` se obrázek nestáhne ani nepřibalí, v .omap na něj nevede šablona, online šablona
(načítání zobrazených dlaždic v Mapperu) zůstává a ZIP dostane textové vysvětlení s odkazem na zdroj.
Klíče podkladů (``built_refs``) přiřazuje ke zdrojům pole ``reference_keys`` (fnmatch).
"""

from __future__ import annotations

import fnmatch
from functools import lru_cache
from pathlib import Path

import yaml

REGISTRY = Path(__file__).resolve().parents[2] / "configs" / "data_sources.yaml"

ALLOWED, FORBIDDEN, UNCLEAR = "allowed", "forbidden", "unclear"
RASTER_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff"})
# Obrázky počítané z výšek jobu (stínování, výška vegetace) – vlastní výpočet, žádný cizí obrázek.
OWN_RENDER_KEYS = frozenset({"vyska_vegetace", "hillshade_dtm", "hillshade_dsm"})

NOTICE_NAME_CS = "CHYBEJICI_PODKLADY.txt"
NOTICE_NAME_EN = "MISSING_BASE_MAPS.txt"


@lru_cache(maxsize=1)
def _sources() -> tuple[dict, ...]:
    data = yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}
    return tuple(s for s in data.get("sources") or () if s.get("reference_keys"))


def source_for_key(key: str) -> dict | None:
    """Záznam registru, ze kterého podklad ``key`` vzniká (None = vlastní výpočet nebo neznámý klíč)."""
    if key in OWN_RENDER_KEYS:
        return None
    for src in _sources():
        if any(fnmatch.fnmatchcase(key, pat) for pat in src["reference_keys"]):
            return src
    return None


def image_in_zip(key: str) -> str:
    """``allowed`` / ``forbidden`` / ``unclear`` pro obrázek podkladu ``key`` (neznámý klíč = povoleno)."""
    src = source_for_key(key)
    return str(src.get("image_in_zip") or ALLOWED) if src else ALLOWED


def is_withheld(key: str) -> bool:
    """Obrázek podkladu ``key`` se do ZIPu nedává (licence zdroje to zakazuje)."""
    return image_in_zip(key) == FORBIDDEN


def _is_raster(path: Path) -> bool:
    return Path(path).suffix.lower() in RASTER_SUFFIXES


def withhold_images(built: dict[str, Path], *, log=None) -> dict[str, Path]:
    """Z ``built`` vyřadí stažené obrázky zakázaných zdrojů a smaže je i s world file (online šablony zůstávají)."""
    out: dict[str, Path] = {}
    for key, path in built.items():
        if _is_raster(path) and is_withheld(key):
            from app.pipeline.reference_layers import world_file_for

            Path(path).unlink(missing_ok=True)
            world_file_for(Path(path)).unlink(missing_ok=True)
            if log:
                log(f"Podklady: {Path(path).name} se do ZIPu nepřidává (licence zdroje)")
            continue
        out[key] = path
    return out


def withheld_raster_names(country: str) -> frozenset[str]:
    """Názvy souborů obrázků zakázaných zdrojů v ``references/`` (pojistka při balení ZIPu)."""
    from app.pipeline.online_templates import ONLINE_OSM, ONLINE_TMS_LAYERS, wms_layers_for

    names: set[str] = set()
    if is_withheld("osm"):
        names.add("osm.png")
    layers = [(ONLINE_OSM[0], ONLINE_OSM[1])]
    layers += [(k, f) for k, f, *_r in wms_layers_for(country)]
    layers += [(k, f) for k, f, *_r in ONLINE_TMS_LAYERS.get(country, ())]
    for key, filename in layers:
        if is_withheld(f"online_{key}"):
            stem = Path(filename).stem
            names.update(f"{stem}{ext}" for ext in (".png", ".jpg"))
    return frozenset(names)


def _applies(src: dict, country: str) -> bool:
    return "*" in src.get("countries", ()) or country in src.get("countries", ())


def withheld_sources(country: str) -> list[str]:
    """Id zdrojů země ``country``, jejichž obrázek se při stažených podkladech do ZIPu nepřidá."""
    return [
        s["id"]
        for s in _sources()
        if s.get("image_in_zip") == FORBIDDEN and s.get("status") == "used" and _applies(s, country)
    ]


def _by_id(source_id: str) -> dict | None:
    return next((s for s in _sources() if s["id"] == source_id), None)


def _notice(src: dict, lang: str) -> dict:
    """Texty vysvětlení zdroje (``zip_notice`` v registru) v jazyce ZIPu."""
    texts = src.get("zip_notice") or {}
    return texts.get("cs" if lang == "cs" else "en") or {}


def _name(source_id: str, lang: str) -> str:
    src = _by_id(source_id) or {}
    return str(_notice(src, lang).get("name") or src.get("name") or source_id)


def notice_name(lang: str) -> str:
    return NOTICE_NAME_CS if lang == "cs" else NOTICE_NAME_EN


def notice_text(source_ids: list[str], lang: str) -> str:
    """Vysvětlení do ZIPu (``references/CHYBEJICI_PODKLADY.txt`` / ``MISSING_BASE_MAPS.txt``)."""
    en = lang != "cs"
    if en:
        lines = [
            "Base maps not included in this ZIP",
            "==================================",
            "",
            "The licence of the following sources does not allow including their images in a downloadable",
            "package, so they are not in the references/ folder. Your map (.omap) is not affected. Where the",
            "source allows it, the .omap contains an online template instead: switch it on in Mapper",
            "(Templates → Template setup) and the tiles are loaded from the internet while you work.",
            "",
        ]
    else:
        lines = [
            "Podklady, které v tomto ZIPu chybí",
            "==================================",
            "",
            "Licence následujících zdrojů nedovoluje přibalit jejich obrázek do staženého balíčku, proto nejsou",
            "ve složce references/. Samotné mapy (.omap) se to netýká. Kde to zdroj dovoluje, je v .omap místo",
            "obrázku online šablona: zapněte ji v Mapperu (Šablony → Nastavení šablon) a dlaždice se načtou",
            "z internetu za běhu.",
            "",
        ]
    for sid in source_ids:
        src = _by_id(sid)
        if not src:
            continue
        lic = src.get("license") or {}
        text = _notice(src, lang)
        lines.append(f"- {text.get('name') or src.get('name')}")
        if text.get("why"):
            lines.append(f"  {'Why' if en else 'Proč'}: {text['why']}")
        if text.get("online"):
            lines.append(f"  {'Online' if en else 'Online'}: {text['online']}")
        lines.append(f"  {'Source' if en else 'Zdroj'}: {src.get('info') or src.get('url')}")
        if lic.get("url") and lic["url"] != (src.get("info") or src.get("url")):
            lines.append(f"  {'Terms' if en else 'Podmínky'}: {lic['url']}")
        lines.append("")
    return "\n".join(lines)


def notice_lines(source_ids: list[str], lang: str) -> list[str]:
    """Krátký blok do README_OOM.txt (prázdný bez vynechaných zdrojů)."""
    names = [_name(s, lang) for s in source_ids]
    if not names:
        return []
    if lang != "cs":
        return [f"Not included for licence reasons (see references/{NOTICE_NAME_EN}): " + "; ".join(names)]
    return [f"Z licenčních důvodů nepřibaleno (viz references/{NOTICE_NAME_CS}): " + "; ".join(names)]


def withheld_log_line(source_ids: list[str], lang: str) -> str:
    """Řádek do logu jobu (česky, web ho ukazuje v detailu jobu)."""
    names = "; ".join(_name(s, "cs") for s in source_ids)
    return (
        f"z licenčních důvodů nepřibaleno: {names} – vysvětlení v references/{notice_name(lang)}, "
        "v .omap zůstává online šablona"
    )
