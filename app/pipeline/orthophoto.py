"""Ortofoto ČÚZK (WMS ORTOFOTO) jako pole RGB v S-JTSK a z něj odvozená maska zeleně (ExG)."""

from __future__ import annotations

import json
import math
from pathlib import Path

CACHE_SUBDIR = "orthophoto"
ORTHO_NAME = "ortofoto.png"
ORTHO_META_NAME = "ortofoto.json"
# Ortofoto referenčního podkladu (``work/references/orthophoto*.jpg`` + ``.jgw``, 0,125–0,25 m/px).
REFERENCE_SUBDIR = "references"
# Podklad je ořezaný na mřížku mapy (výřez + ~1 m), parcely se stahují s přesahem 80 m: pokrytí se měří na výřezu
# mapy; když ho dlaždice nepokryjí, stahuje se zvlášť (jinak by uvnitř mapy chyběla zeleň).
REFERENCE_MIN_COVER = 0.97
# Nad tento počet pixelů (při 0,5 m/px cca 12 × 12 km) se ortofoto nestahuje (paměť, počet dlaždic).
MAX_PIXELS = 150_000_000


def fetch_orthophoto(work_dir: Path, bbox_5514, mpp: float = 0.5, log=None, need_bounds=None):
    """``(rgb uint8 HxWx3, geotransform GDAL)`` pro obálku S-JTSK, nebo ``None`` (síť, příliš velký výřez).

    Ortofoto referenčního podkladu (``work/references``) se převezme, pokrývá-li ``need_bounds`` (výřez mapy;
    jinak celou obálku). Dlaždice nad ``WMS_MAX_GETMAP_PX`` skládá existující helper z ``reference_layers``. Cache:
    ``work_dir/orthophoto/`` (platí jen pro stejnou obálku a rozlišení)."""
    xmin, ymin, xmax, ymax = (float(v) for v in bbox_5514)
    width = max(1, math.ceil((xmax - xmin) / mpp))
    height = max(1, math.ceil((ymax - ymin) / mpp))
    gt = (xmin, (xmax - xmin) / width, 0.0, ymax, 0.0, -(ymax - ymin) / height)
    out_dir = Path(work_dir) / CACHE_SUBDIR
    png, meta_path = out_dir / ORTHO_NAME, out_dir / ORTHO_META_NAME
    want = {"bounds": [xmin, ymin, xmax, ymax], "width": width, "height": height}
    try:
        import numpy as np
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = None
        if png.is_file() and meta_path.is_file() and json.loads(meta_path.read_text(encoding="utf-8")) == want:
            with Image.open(png) as im:
                return np.asarray(im.convert("RGB")), gt
        if width * height > MAX_PIXELS:
            if log:
                log(f"⚠ Ortofoto: výřez je příliš velký ({width}×{height} px), vynecháno")
            return None
        from app.pipeline.reference_layers import ORTOFOTO_WMS, _download_wms_raster, _write_pgw_for_extent

        out_dir.mkdir(parents=True, exist_ok=True)
        rgb = _from_reference_tiles(
            Path(work_dir) / REFERENCE_SUBDIR, (xmin, ymin, xmax, ymax), width, height, need_bounds=need_bounds
        )
        if rgb is not None:  # stejná WMS vrstva už stažená jako podklad – nestahovat podruhé
            Image.fromarray(rgb).save(png)
            _write_pgw_for_extent(png.with_suffix(".pgw"), xmin, ymin, xmax, ymax, width, height)
            meta_path.write_text(json.dumps(want), encoding="utf-8")
            if log:
                log("Ortofoto: z referenčního podkladu (bez dalšího stahování)")
            return rgb, gt
        ok = _download_wms_raster(
            ORTOFOTO_WMS,
            "0",
            (xmin, ymin, xmax, ymax),
            png,
            png.with_suffix(".pgw"),
            width=width,
            height=height,
            image_format="image/png",
            log=log,
        )
        if not ok:
            return None
        with Image.open(png) as im:
            rgb = np.asarray(im.convert("RGB"))
        meta_path.write_text(json.dumps(want), encoding="utf-8")
        return rgb, gt
    except Exception as exc:  # síť i zpracování: volající kreslí dál bez ortofota
        if log:
            log(f"⚠ Ortofoto: přeskočeno ({exc})")
        return None


def _from_reference_tiles(ref_dir: Path, bounds, width: int, height: int, need_bounds=None):
    """Mozaika ``orthophoto*.jpg`` (+ ``.jgw``) převzorkovaná do mřížky ``bounds`` × ``width`` × ``height``.

    ``None``, když dlaždice chybí nebo nepokryjí aspoň ``REFERENCE_MIN_COVER`` z ``need_bounds`` (jinak z celé
    obálky) – pak se stahuje z WMS. Mimo dlaždice zůstane černá (není zeleň)."""
    import numpy as np
    from PIL import Image

    from app.pipeline.georef import read_pgw

    tiles = sorted(p for p in Path(ref_dir).glob("orthophoto*.jpg") if p.with_suffix(".jgw").is_file()) if Path(ref_dir).is_dir() else []
    if not tiles:
        return None
    xmin, _ymin, xmax, ymax = bounds
    mpp = (xmax - xmin) / width
    canvas = Image.new("RGB", (width, height))
    need = np.zeros((height, width), dtype=bool)
    if need_bounds is None:
        need[:, :] = True
    else:
        nx0, ny0, nx1, ny1 = need_bounds
        need[
            max(0, int((ymax - ny1) / mpp)) : max(0, math.ceil((ymax - ny0) / mpp)),
            max(0, int((nx0 - xmin) / mpp)) : max(0, math.ceil((nx1 - xmin) / mpp)),
        ] = True
    covered = np.zeros((height, width), dtype=bool)
    for tile in tiles:
        geo = read_pgw(tile.with_suffix(".jgw"))
        with Image.open(tile) as im:
            tw, th = im.size
            out_w = max(1, round(tw * abs(geo.pixel_x) / mpp))
            out_h = max(1, round(th * abs(geo.pixel_y) / mpp))
            im.draft("RGB", (out_w, out_h))  # JPEG se rovnou dekóduje zmenšený
            part = im.convert("RGB").resize((out_w, out_h), Image.Resampling.BOX)
        tx0 = geo.origin_x - geo.pixel_x / 2
        ty1 = geo.origin_y - geo.pixel_y / 2
        col, row = round((tx0 - xmin) / mpp), round((ymax - ty1) / mpp)
        canvas.paste(part, (col, row))
        covered[max(0, row) : max(0, row + out_h), max(0, col) : max(0, col + out_w)] = True
    n_need = int(need.sum())
    if n_need == 0 or int((covered & need).sum()) < REFERENCE_MIN_COVER * n_need:
        return None
    return np.asarray(canvas)


def exg(rgb):
    """Excess green index (2G − R − B) / (R + G + B); černá (součet 0) dává 0."""
    import numpy as np

    a = np.asarray(rgb, dtype=np.float32)
    total = a.sum(axis=-1)
    total[total == 0] = 1.0
    return (2 * a[..., 1] - a[..., 0] - a[..., 2]) / total


def saturation(rgb):
    """Sytost (max − min) / max; černá dává 0. Asfalt a dlažba jsou šedé (medián ~0,04), tráva ~0,16."""
    import numpy as np

    a = np.asarray(rgb, dtype=np.float32)
    mx = a.max(axis=-1)
    out = (mx - a.min(axis=-1)) / np.where(mx == 0, 1.0, mx)
    return out


def green_mask(rgb, exg_min: float = 0.02, sat_min: float = 0.12):
    """bool maska zeleně: ExG > ``exg_min`` a sytost > ``sat_min`` (bez morfologie).

    Prahy změřilo vlákno ortofota proti DS26 Barrandov (1 m od hran, mimo stín): 4,5 % dlažby a 71 % trávníku
    jako zeleň; samotné ExG > 0,08 zachytilo jen 33 % (suchého) trávníku."""
    return (exg(rgb) > exg_min) & (saturation(rgb) > sat_min)
