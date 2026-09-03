from __future__ import annotations

from pathlib import Path

from app.pipeline.prepare_lidar import run_cmd
from app.settings import PULLAUTA_BIN

# Zdroj v temp/ → název v ZIPu (karttapullautin/). Vrstevnice jdou z GDAL, ne z KP.
DXF_PRODUCTS: tuple[tuple[str, str], ...] = (
    ("dotknolls.dxf", "dotknolls.dxf"),
    ("c1g.dxf", "cliffs_small.dxf"),
    ("c2g.dxf", "cliffs_large.dxf"),
    ("c3.dxf", "cliffs_small.dxf"),
    ("c2.dxf", "cliffs_large.dxf"),
)

# Mezivýstupy – do ZIPu nepatří (contours03 = 0,3 m, obrovský).
DXF_SKIP_NAMES = frozenset(
    {"contours03.dxf", "out.dxf", "out2.dxf", "basemap.dxf"}
)


def _bin_path(temp_dir: Path, dxf_name: str) -> Path:
    return temp_dir / f"{dxf_name}.bin"


def ensure_text_dxf(
    temp_dir: Path,
    dxf_name: str,
    *,
    log: callable | None = None,
) -> Path | None:
    """Vrátí textový DXF – buď existující, nebo převedený z .dxf.bin."""
    path = temp_dir / dxf_name
    if path.is_file() and path.stat().st_size >= 8:
        return path
    bin_path = _bin_path(temp_dir, dxf_name)
    if not bin_path.is_file():
        return None
    run_cmd([PULLAUTA_BIN, "bin2dxf", str(bin_path), str(path)], log=log)
    if path.is_file() and path.stat().st_size >= 8:
        return path
    return None


def collect_dxf_for_zip(temp_dir: Path, *, log: callable | None = None) -> dict[str, Path]:
    """Soubory pro karttapullautin/ ve výstupním ZIPu (zip_name → cesta)."""
    if not temp_dir.is_dir():
        return {}
    collected: dict[str, Path] = {}
    for src_name, zip_name in DXF_PRODUCTS:
        if zip_name in collected:
            continue
        path = ensure_text_dxf(temp_dir, src_name, log=log)
        if path:
            collected[zip_name] = path
    return collected


def prune_heavy_intermediate_dxf(temp_dir: Path, *, log: callable | None = None) -> None:
    """Smaže z temp/ obří nebo zbytečné DXF (úspora místa po běhu KP)."""
    if not temp_dir.is_dir():
        return
    for name in DXF_SKIP_NAMES:
        path = temp_dir / name
        if path.is_file():
            size_mb = path.stat().st_size / 1e6
            path.unlink()
            if log:
                log(f"Odstraněn nepotřebný {name} ({size_mb:.1f} MB)")
        bin_path = _bin_path(temp_dir, name)
        if bin_path.is_file():
            bin_path.unlink(missing_ok=True)
