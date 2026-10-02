"""Metadata listů SM5 / epochy cache a režim DMP OK vs 1G.

ČÚZK produkty nejsou ko-temporální – job musí evidovat, co reálně použil.
``cache_downloaded_at`` ≠ datum pořízení listu (to openzu v meta zatím
neexponuje); logujeme alespoň stáří cache a produkt povrchu.
"""

from __future__ import annotations

from pathlib import Path

from app.download_cache import age_days, lidar_sheet_dir, read_meta

PRODUCT_DMR5G = "DMR5G"
PRODUCT_DMPOK = "DMPOK"
PRODUCT_DMP1G = "DMP1G"

INDICATIVE_LABEL_CS = (
    "Indikativní kreslicí podklad – není zeměměřičské zaměření."
)
CITATION_SHORT = "Podklad: Podkladárna · ČÚZK · OSM, [rok]"
# Legacy alias – KP runtime removed; citation never includes Karttapullautin.
CITATION_SHORT_NO_KP = CITATION_SHORT
CITATION_SHORT_WITH_KP = CITATION_SHORT


def citation_line(*, use_kp: bool = False) -> str:
    del use_kp  # retained for callers; always bez-KP citation
    return f"{CITATION_SHORT} — {INDICATIVE_LABEL_CS}"


def _sheet_product_meta(folder: Path, laz_name: str, default_kind: str) -> dict:
    laz = folder / laz_name
    meta = read_meta(folder) or {}
    downloaded = meta.get("downloaded_at")
    # meta.json může držet více kindů – preferuj soubor na disku.
    kind = default_kind
    if laz_name.upper().startswith("DMPOK"):
        kind = PRODUCT_DMPOK
    elif laz_name.upper().startswith("DMP1G"):
        kind = PRODUCT_DMP1G
    elif laz_name.upper().startswith("DMR"):
        kind = PRODUCT_DMR5G
    elif meta.get("kind"):
        kind = str(meta["kind"])
    present = laz.is_file() and laz.stat().st_size >= 1000
    return {
        "product": kind,
        "present": present,
        "cache_downloaded_at": downloaded if present else None,
        "cache_age_days": age_days(downloaded) if present and downloaded else None,
        "file": laz_name if present else None,
    }


def resolve_dmp_product(folder: Path) -> dict:
    """Preferuje DMP OK; 1G jen jako viditelný fallback."""
    dmpok = folder / "DMPOK.laz"
    dmp1g = folder / "DMP1G.laz"
    if dmpok.is_file() and dmpok.stat().st_size >= 1000:
        info = _sheet_product_meta(folder, "DMPOK.laz", PRODUCT_DMPOK)
        info["mode"] = "ok"
        info["degraded"] = False
        return info
    if dmp1g.is_file() and dmp1g.stat().st_size >= 1000:
        info = _sheet_product_meta(folder, "DMP1G.laz", PRODUCT_DMP1G)
        info["mode"] = "1g"
        info["degraded"] = True
        return info
    return {
        "product": None,
        "present": False,
        "mode": None,
        "degraded": False,
        "cache_downloaded_at": None,
        "cache_age_days": None,
        "file": None,
    }


def collect_lidar_source_meta(mapnoms: list[str]) -> dict:
    """Souhrn listů SM5 + DMR/DMP epochy z cache pro metadata.json / README."""
    sheets: list[dict] = []
    any_dmp1g = False
    for mapnom in mapnoms:
        name = (mapnom or "").strip().upper()
        if not name:
            continue
        folder = lidar_sheet_dir(name)
        dmr = _sheet_product_meta(folder, "DMR5G.laz", PRODUCT_DMR5G)
        dmp = resolve_dmp_product(folder)
        if dmp.get("degraded"):
            any_dmp1g = True
        sheets.append(
            {
                "mapnom": name,
                "dmr": dmr,
                "dmp": dmp,
            }
        )
    modes = {s["dmp"].get("mode") for s in sheets if s["dmp"].get("mode")}
    if modes == {"ok"}:
        dmp_mode = "ok"
    elif modes == {"1g"}:
        dmp_mode = "1g"
    elif modes:
        dmp_mode = "mixed"
    else:
        dmp_mode = None
    return {
        "sheets": sheets,
        "sheet_count": len(sheets),
        "dmp_mode": dmp_mode,
        "dmp_degraded": any_dmp1g,
        "indicative_label": INDICATIVE_LABEL_CS,
    }


def format_source_epochs_readme(source_meta: dict | None) -> str:
    """Blok do README_OOM.txt – epochy cache + varování při DMP 1G."""
    if not source_meta or not source_meta.get("sheets"):
        return ""
    lines = [
        "Zdroje LiDAR (epochy = stažení do cache, ne datum pořízení ČÚZK)",
        "----------------------------------------------------------------",
    ]
    for sheet in source_meta["sheets"]:
        mapnom = sheet.get("mapnom") or "?"
        dmr = sheet.get("dmr") or {}
        dmp = sheet.get("dmp") or {}
        dmr_day = (dmr.get("cache_downloaded_at") or "?")[:10]
        dmp_day = (dmp.get("cache_downloaded_at") or "?")[:10]
        dmp_label = "DMP OK" if dmp.get("mode") == "ok" else (
            "DMP 1G" if dmp.get("mode") == "1g" else "DMP ?"
        )
        lines.append(
            f"- {mapnom}: DMR 5G {dmr_day} · {dmp_label} {dmp_day}"
        )
    if source_meta.get("dmp_degraded"):
        lines.append(
            "VAROVÁNÍ: alespoň jeden list používá DMP 1G místo DMP OK "
            "(nižší kvalita povrchu / CHM) – není tichý fallback."
        )
    lines.append(INDICATIVE_LABEL_CS)
    return "\n".join(lines) + "\n"
