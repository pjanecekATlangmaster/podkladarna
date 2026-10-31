"""Adaptivní vyhlazení DEM pro vrstevnice (volba jobu ``contour_smooth``).

Vyhlazuje se výškový model, ne čáry: vrstevnice jedné spojité plochy se
nekříží a změna podél jedné vrstevnice vychází sama z pole vah.

    Z = Z_base + w · clip(Z_strong − Z_base, ±0,3·I)

- ``Z_base`` = dnešní ``contours/dem_smooth.tif`` (beze změny, čtou ho i srázy),
- ``Z_strong`` = Gauss σ' nad ``Z_base`` (normalizovaná konvoluce, nodata se
  nerozteče – rybníky po ``gdal_fillnodata -md 24`` zůstávají −9999),
- zástavba (ZABAGED budovy + okolí, parkoviště, areály) se vyhladí i v mírném
  svahu; ve sprintu zeleň (louky, parky, sady ze ZABAGED) zůstane beze změny,
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
    urban_lo_mm: float = 1.0  # zástavba: pod tímto rozestupem (strmě) nevyhlazovat
    urban_hi_mm: float = 2.0
    urban_buffer_m: float = 10.0  # okolí budov (DMR u zdí má schody a artefakty)
    urban_sigma_m: float = 8.0  # rozmazání masky zástavby / zeleně
    urban_detail_add_m: float = 1.0  # v zástavbě chránit až vyšší tvary
    urban_sigma_strong_m: float = 12.0  # σ' v zástavbě (silnější než v rovině)
    protect_buffer_m: float = 8.0  # náspy a zářezy drah, valy – beze změny
    keep_green: bool = False  # sprint: louky a parky beze změny


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
            urban_lo_mm=0.6,
            urban_hi_mm=1.0,
            urban_buffer_m=6.0,
            urban_sigma_m=4.0,
            urban_detail_add_m=0.5,
            urban_sigma_strong_m=6.0,
            protect_buffer_m=6.0,
            keep_green=True,
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


def _pct(share) -> str:
    return "–" if share is None else f"{share * 100:.0f} %"


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
    urban=None,
    green=None,
    protect=None,
):
    """(Z, w) – Z je float32, mimo ``valid`` a kde w = 0 přesně ``z_base``.

    ``urban`` / ``green`` (bool rastry na stejné mřížce, volitelné): zástavba
    se vyhladí i ve svahu do ``urban_hi_mm`` rozestupu, zeleň (sprint: louky,
    parky) se nevyhlazuje vůbec (``params.keep_green``), ``protect`` (náspy
    a zářezy drah, valy) také ne.
    """
    import numpy as np

    z_base = np.asarray(z_base, dtype=np.float32)
    valid = np.asarray(valid, dtype=bool)
    cell = max(float(cell_m), 1e-6)
    interval = max(float(interval_m), 0.1)
    if not valid.any():
        return z_base.copy(), np.zeros(z_base.shape, dtype=np.float32)

    z_strong = _masked_gauss(z_base, valid, params.sigma_strong_m / cell)
    delta = np.where(valid, z_strong - z_base.astype(np.float64), 0.0)
    del z_strong

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
    u = None
    if urban is not None and np.any(urban):
        # Zástavba: maska budov (s okolím) a zpevněných ploch, rozmazaná; i v mírném
        # svahu, ale ne ve strmém (areál na svahu nesmí srovnat terén).
        u = _masked_gauss(np.asarray(urban, dtype=np.float64), valid, params.urban_sigma_m / cell)
        u = np.minimum(u * 2.0, 1.0) * _smoothstep(spacing_mm, params.urban_lo_mm, params.urban_hi_mm)
        w_flat = np.maximum(w_flat, u)
        # V zástavbě silnější vyhlazení (Petr: tvar mezi domy není klíčový).
        z_urban = _masked_gauss(z_base, valid, params.urban_sigma_strong_m / cell)
        delta = delta * (1.0 - u) + np.where(valid, z_urban - z_base.astype(np.float64), 0.0) * u
        del z_urban
    del spacing_mm

    # Zónová váha rozmazaná (plynulý přechod), detail až potom a ostře.
    # Rozmazání × místní rovina: plynulý přechod, ale strmý svah (i úzký náspový
    # svah v rovině) zůstane přesně na nule, malá plošinka ve svahu skoro taky.
    w_zone = _masked_gauss(w_flat, valid, params.zone_sigma_m / cell) * w_flat
    del w_flat
    w_zone = np.where(w_zone < _W_ZERO, 0.0, np.minimum(w_zone, 1.0))
    if params.keep_green and green is not None and np.any(green):
        g = _masked_gauss(np.asarray(green, dtype=np.float64), valid, params.urban_sigma_m / cell)
        w_zone = w_zone * (1.0 - np.minimum(g * 2.0, 1.0))
        w_zone = np.where(w_zone < _W_ZERO, 0.0, w_zone)
        del g
    detail_lo, detail_hi = params.detail_lo_m, params.detail_hi_m
    if u is not None:
        # V zástavbě jsou drobné „tvary“ hlavně artefakty DMR u domů a zdí.
        detail_lo = detail_lo + u * params.urban_detail_add_m
        detail_hi = detail_hi + u * params.urban_detail_add_m
    t = np.clip((np.abs(delta) - detail_lo) / np.maximum(detail_hi - detail_lo, 1e-9), 0.0, 1.0)
    w_detail = t * t * (3.0 - 2.0 * t)
    del t, u
    w = np.where(valid, w_zone * (1.0 - w_detail), 0.0)
    del w_zone, w_detail
    if protect is not None and np.any(protect):
        pr = _masked_gauss(np.asarray(protect, dtype=np.float64), valid, params.urban_sigma_m / 2.0 / cell)
        w = w * (1.0 - np.minimum(pr * 2.0, 1.0))
        del pr
    w = np.where(w < _W_ZERO, 0.0, w)

    cap = params.cap_frac_interval * interval
    change = (w * np.clip(delta, -cap, cap)).astype(np.float32)
    out = np.where(w > 0.0, z_base + change, z_base).astype(np.float32)
    return out, w.astype(np.float32)


# ZABAGED (zabaged_clean.zip, hotový před krokem vrstevnic – deterministické).
URBAN_BUILDING_LAYERS = ("BudovaJednotlivaNeboBlokBudov",)
URBAN_AREA_LAYERS = ("ParkovisteOdpocivka", "OstatniPlochaVSidlech", "ArealUceloveZastavby")
GREEN_LAYERS = ("TrvalyTravniPorost", "UdrzovanaZelen", "OvocnySadZahrada")
PROTECT_LAYERS = ("ZeleznicniTrat", "ZeleznicniVlecka", "HradbaVal")


def _rasterize_layers(zabaged_zip: Path, layers, shape, gt, srs_wkt: str):
    import numpy as np
    from osgeo import gdal, ogr

    out = np.zeros(shape, dtype=bool)
    mem = gdal.GetDriverByName("MEM").Create("", shape[1], shape[0], 1, gdal.GDT_Byte)
    mem.SetGeoTransform(gt)
    if srs_wkt:
        mem.SetProjection(srs_wkt)
    for name in layers:
        ds = ogr.Open(f"/vsizip/{Path(zabaged_zip).as_posix()}/{name}.shp")
        if ds is None:
            continue
        band = mem.GetRasterBand(1)
        band.Fill(0)
        gdal.RasterizeLayer(mem, [1], ds.GetLayer(0), burn_values=[1], options=["ALL_TOUCHED=TRUE"])
        out |= band.ReadAsArray().astype(bool)
        ds = None
    return out


def zabaged_masks(zabaged_zip: Path | None, shape, gt, srs_wkt: str, params: AdaptiveParams):
    """(zástavba, zeleň, ochrana) jako bool rastry na mřížce DEM; bez ZABAGED None."""
    import numpy as np
    from scipy.ndimage import distance_transform_edt

    if zabaged_zip is None or not Path(zabaged_zip).is_file():
        return None, None, None
    cell = (abs(gt[1]) + abs(gt[5])) / 2.0
    buildings = _rasterize_layers(zabaged_zip, URBAN_BUILDING_LAYERS, shape, gt, srs_wkt)
    near_buildings = (
        distance_transform_edt(~buildings) * cell <= params.urban_buffer_m
        if buildings.any()
        else buildings
    )
    urban = near_buildings | _rasterize_layers(zabaged_zip, URBAN_AREA_LAYERS, shape, gt, srs_wkt)
    green = None
    if params.keep_green:
        green = _rasterize_layers(zabaged_zip, GREEN_LAYERS, shape, gt, srs_wkt)
        if buildings.any():
            # Zeleň těsně u domů je spíš předzahrádka – nechat vyhladit se zástavbou.
            green &= distance_transform_edt(~buildings) * cell > 5.0
    lines = _rasterize_layers(zabaged_zip, PROTECT_LAYERS, shape, gt, srs_wkt)
    protect = (
        distance_transform_edt(~lines) * cell <= params.protect_buffer_m if lines.any() else None
    )
    return urban, green, protect


def write_adaptive_contour_dem(
    dem_smooth: Path,
    dest: Path,
    *,
    interval_m: float,
    scale: int,
    scalefactor: float,
    zabaged_zip: Path | None = None,
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
    urban, green, protect = zabaged_masks(zabaged_zip, z.shape, gt, src.GetProjection(), params)
    out, w = adaptive_contour_array(
        z, valid, cell_m=cell_m, interval_m=interval_m, scale=scale, params=params,
        urban=urban, green=green, protect=protect,
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
        "share_urban": round(float(urban[valid].mean()), 4) if urban is not None else None,
        "share_green": round(float(green[valid].mean()), 4) if green is not None else None,
    }
    if log:
        log(
            "Vrstevnice adaptivně: vyhlazeno "
            f"{summary['share_smoothed'] * 100:.0f} % plochy "
            f"(plně {summary['share_full'] * 100:.0f} %), σ' {params.sigma_strong_m:g} m, "
            f"rovina {params.flat_lo_mm:g}–{params.flat_hi_mm:g} mm, "
            f"detail {params.detail_lo_m:g}–{params.detail_hi_m:g} m, "
            f"zástavba {_pct(summary['share_urban'])}, zeleň beze změny {_pct(summary['share_green'])}"
        )
    return summary
