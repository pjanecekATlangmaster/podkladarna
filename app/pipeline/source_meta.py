"""Metadata listů SM5 / epochy cache a režim DMP OK vs 1G.

ČÚZK produkty nejsou ko-temporální – job musí evidovat, co reálně použil.
``cache_downloaded_at`` ≠ datum pořízení listu (to openzu v meta zatím
neexponuje); logujeme alespoň stáří cache a produkt povrchu.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from app.download_cache import age_days, lidar_sheet_dir, read_meta

PRODUCT_DMR5G = "DMR5G"
PRODUCT_DMPOK = "DMPOK"
PRODUCT_DMP1G = "DMP1G"

INDICATIVE_LABEL_CS = (
    "Pracovní kreslicí podklad – není dokonalé zaměření."
)
# Bezlisté období: před 15. 5. a od 1. 11. (listnaté stromy v DMP OK chybí).
LEAFLESS_UNTIL = (5, 15)
LEAFLESS_FROM_MONTH = 11
_GPS_EPOCH = _dt.date(1980, 1, 6)
_GPS_SAMPLE_POINTS = 50_000
CITATION_SHORT = "Podklad: Podkladárna · ČÚZK · OSM, [rok]"


def citation_line() -> str:
    return f"{CITATION_SHORT} – {INDICATIVE_LABEL_CS}"


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


def dmp_acquired_on(laz_path: Path) -> str | None:
    """Datum náletu (ISO) z GPS času bodů LAZ; None, když ho nelze spolehlivě určit.

    Platí jen pro „adjusted standard“ GPS čas (global encoding bit 0);
    laspy je volitelný – bez něj nebo při jakékoli chybě vrací None.
    """
    try:
        import laspy
        from laspy.header import GpsTimeType

        with laspy.open(str(laz_path)) as reader:
            if reader.header.global_encoding.gps_time_type != GpsTimeType.STANDARD:
                return None
            points = reader.read_points(_GPS_SAMPLE_POINTS)
            times = sorted(float(t) for t in points.gps_time)
        if not times:
            return None
        median = times[len(times) // 2]
        day = _GPS_EPOCH + _dt.timedelta(days=int((median + 1e9) // 86400))
        return day.isoformat()
    except Exception:
        return None


def is_leafless_date(iso: str | None) -> bool:
    """True, když datum náletu spadá do bezlistého období."""
    try:
        day = _dt.date.fromisoformat(str(iso)[:10])
    except (TypeError, ValueError):
        return False
    if day.month >= LEAFLESS_FROM_MONTH:
        return True
    return (day.month, day.day) < LEAFLESS_UNTIL


def resolve_dmp_product(folder: Path) -> dict:
    """Preferuje DMP OK; 1G jen jako viditelný fallback."""
    dmpok = folder / "DMPOK.laz"
    dmp1g = folder / "DMP1G.laz"
    if dmpok.is_file() and dmpok.stat().st_size >= 1000:
        info = _sheet_product_meta(folder, "DMPOK.laz", PRODUCT_DMPOK)
        info["mode"] = "ok"
        info["degraded"] = False
        info["acquired_on"] = dmp_acquired_on(dmpok)
        info["leafless"] = is_leafless_date(info["acquired_on"])
        return info
    if dmp1g.is_file() and dmp1g.stat().st_size >= 1000:
        info = _sheet_product_meta(folder, "DMP1G.laz", PRODUCT_DMP1G)
        info["mode"] = "1g"
        info["degraded"] = True
        info["acquired_on"] = None
        info["leafless"] = False
        return info
    return {
        "product": None,
        "present": False,
        "mode": None,
        "degraded": False,
        "acquired_on": None,
        "leafless": False,
        "cache_downloaded_at": None,
        "cache_age_days": None,
        "file": None,
    }


def collect_lidar_source_meta(mapnoms: list[str]) -> dict:
    """Souhrn listů SM5 + DMR/DMP epochy z cache pro metadata.json / README."""
    sheets: list[dict] = []
    any_dmp1g = False
    leafless_sheets: list[dict] = []
    for mapnom in mapnoms:
        name = (mapnom or "").strip().upper()
        if not name:
            continue
        folder = lidar_sheet_dir(name)
        dmr = _sheet_product_meta(folder, "DMR5G.laz", PRODUCT_DMR5G)
        dmp = resolve_dmp_product(folder)
        if dmp.get("degraded"):
            any_dmp1g = True
        if dmp.get("leafless"):
            leafless_sheets.append(
                {"mapnom": name, "acquired_on": dmp.get("acquired_on")}
            )
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
        "dmp_leafless": bool(leafless_sheets),
        "dmp_leafless_sheets": leafless_sheets,
        "indicative_label": INDICATIVE_LABEL_CS,
    }


def format_source_epochs_readme(source_meta: dict | None) -> str:
    """Blok do README_OOM.txt – epochy cache + varování při DMP 1G."""
    if not source_meta or not source_meta.get("sheets"):
        return ""
    lines = [
        "Výšková data ČÚZK (letecké laserové skenování: DMR 5G terén, DMP OK stromy a budovy)",
        "Datum = kdy je Podkladárna stáhla; datum náletu DMP OK (kdy ČÚZK skenoval) je v závorce.",
        "------------------------------------------------------------------------------------",
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
        flown = f" (nálet {dmp['acquired_on']})" if dmp.get("acquired_on") else ""
        lines.append(
            f"- {mapnom}: DMR 5G {dmr_day} · {dmp_label} {dmp_day}{flown}"
        )
    if source_meta.get("dmp_degraded"):
        lines.append(
            "VAROVÁNÍ: pro část území nebyla dostupná přesná data o výšce stromů "
            "a budov (DMP OK), použila se starší hrubší náhrada DMP 1G. Porosty "
            "mohou být méně přesné; doporučujeme mapu za pár dní vygenerovat znovu."
        )
    leafless = source_meta.get("dmp_leafless_sheets") or []
    if source_meta.get("dmp_leafless") and leafless:
        part = (
            "pro toto území"
            if len(leafless) >= len(source_meta["sheets"])
            else "pro část území (listy "
            + ", ".join(str(x.get("mapnom")) for x in leafless)
            + ")"
        )
        dates = ", ".join(
            sorted({str(x["acquired_on"]) for x in leafless if x.get("acquired_on")})
        )
        lines.append(
            f"VAROVÁNÍ: DMP OK {part} ČÚZK nalétal v bezlistém období "
            f"(datum {dates}). Listnaté stromy pak v datech chybí, takže "
            "v listnatém lese mohou být chybně louky / paseky (403) a porosty "
            "401–410 jsou tam nespolehlivé. Ověřte v terénu nebo na ortofotu."
        )
    lines.append(INDICATIVE_LABEL_CS)
    return "\n".join(lines) + "\n"
