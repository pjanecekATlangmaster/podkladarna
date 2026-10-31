"""Adaptivní vyhlazení DEM pro vrstevnice (volba jobu ``contour_smooth``).

Vyhlazuje se výškový model, ne čáry: vrstevnice jedné spojité plochy se
nekříží a změna podél jedné vrstevnice vychází sama z pole vah.

    Z = Z_base + w · clip(Z_strong − Z_base, ±0,3·I)

- ``Z_base`` = dnešní ``contours/dem_smooth.tif`` (beze změny, čtou ho i srázy),
- ``Z_strong`` = Gauss σ' nad ``Z_base`` (normalizovaná konvoluce, nodata se
  nerozteče – rybníky po ``gdal_fillnodata -md 24`` zůstávají −9999),
- ``w = w_zone · (1 − w_detail)``:
  ``w_zone`` = rovina podle rozestupu vrstevnic na papíře (rozmazaná krát
  místní, malé hodnoty přesně 0), ``w_detail`` ostře podle |Z_strong − Z_base| v metrech
  (mělké kupky, valy a žlaby se nemažou).

Kde ``w = 0`` (členitý terén), zůstane pixel bit po bitu ``Z_base``.
Návrh a oponentura: /mnt/project-files/vrstevnice-vyhlazeni/.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

DEM_CONTOUR_NAME = "dem_contour.tif"

# Pod tuto váhu se nevyhlazuje vůbec (Gauss má ocas přes celý výřez).
_W_ZERO = 0.02
_SPRINT_SF_MAX = 0.55


@dataclass(frozen=True)
class AdaptiveParams:
    sigma_strong_m: float  # σ' silného vyhlazení nad dem_smooth
    flat_lo_mm: float  # rozestup vrstevnic na papíře: pod tím w_flat = 0
    flat_hi_mm: float  # nad tím w_flat = 1
    slope_sigma_m: float  # průměrování sklonu před výpočtem rozestupu
    zone_sigma_m: float  # rozmazání zónové váhy (plynulý přechod podél čáry)
    detail_lo_m: float  # |Z_strong − Z_base| do této výšky se vyhladí plně
    detail_hi_m: float  # od této výšky se tvar nechá jako dosud
    cap_frac_interval: float  # strop změny výšky (× ekvidistance)


def adaptive_params(scalefactor: float) -> AdaptiveParams:
    """Les / MTBO a sprint (oponentura §2 a §4)."""
    sf = float(scalefactor) if scalefactor else 1.0
    if sf <= _SPRINT_SF_MAX:
        return AdaptiveParams(
            sigma_strong_m=3.0,
            flat_lo_mm=1.0,
            flat_hi_mm=2.0,
            slope_sigma_m=4.0,
            zone_sigma_m=8.0,
            detail_lo_m=0.4,
            detail_hi_m=0.9,
            cap_frac_interval=0.3,
        )
    return AdaptiveParams(
        sigma_strong_m=7.0 * max(sf, 1.0),
        flat_lo_mm=2.0,
        flat_hi_mm=5.0,
        slope_sigma_m=10.0 * max(sf, 1.0),
        zone_sigma_m=20.0 * max(sf, 1.0),
        detail_lo_m=0.5,
        detail_hi_m=1.2,
        cap_frac_interval=0.3,
    )


def _smoothstep(x, lo: float, hi: float):
    import numpy as np

    t = np.clip((x - lo) / max(hi - lo, 1e-9), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _masked_gauss(values, valid, sigma_px: float):
    """Gauss jen přes platné pixely (normalizovaná konvoluce)."""
    import numpy as np
    from scipy.ndimage import gaussian_filter

    num = gaussian_filter(np.where(valid, values, 0.0).astype(np.float64), sigma_px, mode="nearest")
    den = gaussian_filter(valid.astype(np.float64), sigma_px, mode="nearest")
    out = np.where(den > 1e-6, num / np.maximum(den, 1e-6), 0.0)
    return out


def adaptive_contour_array(
    z_base,
    valid,
    *,
    cell_m: float,
    interval_m: float,
    scale: int,
    params: AdaptiveParams,
):
    """(Z, w) – Z je float32, mimo ``valid`` a kde w = 0 přesně ``z_base``."""
    import numpy as np

    z_base = np.asarray(z_base, dtype=np.float32)
    valid = np.asarray(valid, dtype=bool)
    cell = max(float(cell_m), 1e-6)
    interval = max(float(interval_m), 0.1)
    if not valid.any():
        return z_base.copy(), np.zeros(z_base.shape, dtype=np.float32)

    z_strong = _masked_gauss(z_base, valid, params.sigma_strong_m / cell)
    delta = np.where(valid, z_strong - z_base.astype(np.float64), 0.0)

    # Rozestup vrstevnic na papíře (mm) z průměru |sklonu| v okně. Průměr velikosti
    # (ne sklon průměru): u okraje výřezu by Gauss s "nearest" svah zploštil
    # a členitý terén (sklony na obě strany) by vypadal jako rovina.
    gy, gx = np.gradient(z_base.astype(np.float64), cell)
    grad = np.hypot(gx, gy)
    del gx, gy
    # Pixely u nodata mají nesmyslný gradient (soused −9999) – do průměru nejdou.
    from scipy.ndimage import binary_erosion

    grad_ok = binary_erosion(valid, iterations=1, border_value=1)
    grad = _masked_gauss(grad, grad_ok, params.slope_sigma_m / cell)
    del grad_ok
    spacing_mm = interval / np.maximum(grad, 1e-6) / float(scale) * 1000.0
    del grad
    w_flat = _smoothstep(spacing_mm, params.flat_lo_mm, params.flat_hi_mm)
    del spacing_mm

    # Zónová váha rozmazaná (plynulý přechod), detail až potom a ostře.
    # Rozmazání × místní rovina: plynulý přechod, ale strmý svah (i úzký náspový
    # svah v rovině) zůstane přesně na nule, malá plošinka ve svahu skoro taky.
    w_zone = _masked_gauss(w_flat, valid, params.zone_sigma_m / cell) * w_flat
    del w_flat
    w_zone = np.where(w_zone < _W_ZERO, 0.0, np.minimum(w_zone, 1.0))
    w_detail = _smoothstep(np.abs(delta), params.detail_lo_m, params.detail_hi_m)
    w = np.where(valid, w_zone * (1.0 - w_detail), 0.0)
    del w_zone, w_detail
    w = np.where(w < _W_ZERO, 0.0, w)

    cap = params.cap_frac_interval * interval
    change = (w * np.clip(delta, -cap, cap)).astype(np.float32)
    out = np.where(w > 0.0, z_base + change, z_base).astype(np.float32)
    return out, w.astype(np.float32)


def write_adaptive_contour_dem(
    dem_smooth: Path,
    dest: Path,
    *,
    interval_m: float,
    scale: int,
    scalefactor: float,
    log=None,
) -> dict:
    """``dem_smooth.tif`` → ``dem_contour.tif``; vrací souhrn pro contour_meta."""
    import numpy as np
    from osgeo import gdal

    params = adaptive_params(scalefactor)
    src = gdal.Open(str(dem_smooth))
    if src is None:
        raise RuntimeError(f"Nelze otevřít DEM: {dem_smooth}")
    gt = src.GetGeoTransform()
    band = src.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    z = band.ReadAsArray().astype(np.float32)
    valid = np.isfinite(z)
    if nodata is not None:
        valid &= z != np.float32(nodata)
    cell_m = (abs(gt[1]) + abs(gt[5])) / 2.0
    out, w = adaptive_contour_array(
        z, valid, cell_m=cell_m, interval_m=interval_m, scale=scale, params=params
    )
    driver = gdal.GetDriverByName("GTiff")
    dest.unlink(missing_ok=True)
    dst = driver.Create(
        str(dest), src.RasterXSize, src.RasterYSize, 1, gdal.GDT_Float32,
        options=["COMPRESS=DEFLATE", "TILED=YES"],
    )
    dst.SetGeoTransform(gt)
    dst.SetProjection(src.GetProjection())
    out_band = dst.GetRasterBand(1)
    if nodata is not None:
        out_band.SetNoDataValue(nodata)
    out_band.WriteArray(out)
    out_band.FlushCache()
    dst = None
    src = None
    n_valid = int(valid.sum()) or 1
    summary = {
        "mode": "adaptive",
        "params": asdict(params),
        "share_smoothed": round(float((w > 0).sum()) / n_valid, 4),
        "share_full": round(float((w >= 0.98).sum()) / n_valid, 4),
    }
    if log:
        log(
            "Vrstevnice adaptivně: vyhlazeno "
            f"{summary['share_smoothed'] * 100:.0f} % plochy "
            f"(plně {summary['share_full'] * 100:.0f} %), σ' {params.sigma_strong_m:g} m, "
            f"rovina {params.flat_lo_mm:g}–{params.flat_hi_mm:g} mm, "
            f"detail {params.detail_lo_m:g}–{params.detail_hi_m:g} m"
        )
    return summary
