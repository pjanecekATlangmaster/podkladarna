"""DXF produkty jobu v ``work/temp/`` (knolly, srázy) → názvy v ZIPu (base/)."""

from __future__ import annotations

from pathlib import Path

# Zdroj v temp/ → název v ZIPu (base/). Jediná pravda vrstevnic = GDAL
# (base/contours_gdal.*). c2g = zemní srázy (104), c_rock = skalní (201).
DXF_PRODUCTS: tuple[tuple[str, str], ...] = (
    ("dotknolls.dxf", "dotknolls.dxf"),
    ("dotdepressions.dxf", "dotdepressions.dxf"),
    ("dotpits.dxf", "dotpits.dxf"),
    ("c2g.dxf", "cliffs_small.dxf"),
    ("c_rock.dxf", "cliffs_rock.dxf"),
)


def ensure_text_dxf(
    temp_dir: Path,
    dxf_name: str,
    *,
    log: callable | None = None,
) -> Path | None:
    """Vrátí existující neprázdný DXF z ``temp/``, jinak None."""
    path = temp_dir / dxf_name
    if path.is_file() and path.stat().st_size >= 8:
        return path
    return None


def collect_dxf_for_zip(
    temp_dir: Path,
    *,
    log: callable | None = None,
    include_cliffs: bool = True,
) -> dict[str, Path]:
    """Soubory pro base/ ve výstupním ZIPu (zip_name → cesta)."""
    if not temp_dir.is_dir():
        return {}
    collected: dict[str, Path] = {}
    for src_name, zip_name in DXF_PRODUCTS:
        if not include_cliffs and zip_name.startswith("cliffs_"):
            continue
        path = ensure_text_dxf(temp_dir, src_name, log=log)
        if path:
            collected[zip_name] = path
    return collected
