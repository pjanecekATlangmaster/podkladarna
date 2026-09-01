from __future__ import annotations

import json
import math
import subprocess
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from app.pipeline.fetch_openzu import USER_AGENT, crop_bounds_5514
from app.pipeline.georef import PgwGeoref, read_pgw
from app.pipeline.prepare_lidar import find_tool, run_cmd
from app.tiles import fetch_tile

# Kartografický standard (GDAL výchozí): světlo ze severozápadu, 45° nad obzorem.
HILLSHADE_AZIMUTH = 315
HILLSHADE_ALTITUDE = 45
MAX_REF_PIXELS = 4096
WEB_MERCATOR_HALF = 20037508.342789244

ORTOFOTO_WMS = (
    "https://ags.cuzk.gov.cz/arcgis1/services/ORTOFOTO/MapServer/WMSServer"
)


def reference_metadata() -> dict:
    return {
        "hillshade_azimuth_deg": HILLSHADE_AZIMUTH,
        "hillshade_altitude_deg": HILLSHADE_ALTITUDE,
        "hillshade_source": "DMR 5G (ground)",
        "hillshade_tool": "gdaldem hillshade",
    }


def _gdal_tool(name: str) -> str:
    for candidate in (name, f"{name}.exe"):
        try:
            return find_tool(candidate)
        except RuntimeError:
            continue
    raise RuntimeError(f"GDAL nástroj '{name}' není k dispozici")


def _raster_size(png: Path) -> tuple[int, int]:
    from struct import unpack

    data = png.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Neplatný PNG: {png}")
    offset = 8
    while offset + 8 <= len(data):
        length = unpack(">I", data[offset : offset + 4])[0]
        chunk = data[offset + 4 : offset + 8]
        if chunk == b"IHDR":
            w, h = unpack(">II", data[offset + 8 : offset + 16])
            return int(w), int(h)
        offset += 12 + length
    raise ValueError(f"PNG bez IHDR: {png}")


def _target_size(width: int, height: int, max_px: int = MAX_REF_PIXELS) -> tuple[int, int]:
    longest = max(width, height, 1)
    if longest <= max_px:
        return width, height
    scale = max_px / longest
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def _template_extent(template_png: Path, template_pgw: Path) -> tuple[float, float, float, float, int, int]:
    georef = read_pgw(template_pgw)
    width, height = _raster_size(template_png)
    xmin = georef.origin_x
    ymax = georef.origin_y
    xmax = xmin + width * georef.pixel_x
    ymin = ymax + height * georef.pixel_y
    return xmin, ymin, xmax, ymax, width, height


def _write_pgw_for_extent(
    dest_pgw: Path,
    xmin: float,
    ymin: float,
    xmax: float,
    ymax: float,
    width: int,
    height: int,
) -> None:
    PgwGeoref(
        pixel_x=(xmax - xmin) / width,
        rot_row=0.0,
        rot_col=0.0,
        pixel_y=-(ymax - ymin) / height,
        origin_x=xmin,
        origin_y=ymax,
    ).write(dest_pgw)


def _dem_resolution_m(template_png: Path, template_pgw: Path) -> float:
    """Rozlišení DEM v metrech – sladěné s cílovým PNG (max MAX_REF_PIXELS)."""
    xmin, ymin, xmax, ymax, width, height = _template_extent(template_png, template_pgw)
    tw, th = _target_size(width, height)
    res = max((xmax - xmin) / tw, (ymax - ymin) / th)
    return max(0.25, min(res, 8.0))


def _template_bounds(
    template_png: Path, template_pgw: Path
) -> tuple[float, float, float, float]:
    xmin, ymin, xmax, ymax, _, _ = _template_extent(template_png, template_pgw)
    return xmin, ymin, xmax, ymax


def _hillshade_laz_candidates(job_dir: Path) -> list[Path]:
    lidar = job_dir / "work" / "lidar"
    if not lidar.is_dir():
        return []
    seen: set[Path] = set()
    ordered: list[Path] = []
    for pattern in ("dmr_ground_*.laz", "ground_merged.laz"):
        for path in sorted(lidar.glob(pattern)):
            if path.is_file() and path.stat().st_size > 1000 and path not in seen:
                seen.add(path)
                ordered.append(path)
    for name in ("merged_crop.laz", "merged_crop_retry.laz", "merged.laz"):
        path = lidar / name
        if path.is_file() and path.stat().st_size > 1000 and path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def _align_to_template(
    src: Path,
    template_png: Path,
    template_pgw: Path,
    dest_png: Path,
    dest_pgw: Path,
    *,
    log: callable | None = None,
) -> None:
    xmin, ymin, xmax, ymax, width, height = _template_extent(template_png, template_pgw)
    tw, th = _target_size(width, height)
    gdalwarp = _gdal_tool("gdalwarp")
    dest_png.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(
        [
            gdalwarp,
            "-t_srs",
            "EPSG:5514",
            "-te",
            str(xmin),
            str(ymin),
            str(xmax),
            str(ymax),
            "-ts",
            str(tw),
            str(th),
            "-r",
            "bilinear",
            "-of",
            "PNG",
            "-overwrite",
            str(src),
            str(dest_png),
        ],
        log=log,
    )
    _write_pgw_for_extent(dest_pgw, xmin, ymin, xmax, ymax, tw, th)


def _laz_needs_ground_filter(laz: Path) -> bool:
    """Sloučený LAZ (DMR+DMP) potřebuje odfiltrovat vegetaci; čistý ground ne."""
    return laz.name.lower() in (
        "merged.laz",
        "merged_crop.laz",
        "merged_crop_retry.laz",
    )


def _pdal_dem_from_laz(
    laz: Path,
    bounds: tuple[float, float, float, float],
    dest_tif: Path,
    *,
    resolution_m: float,
    log: callable | None = None,
) -> Path:
    xmin, ymin, xmax, ymax = bounds
    pdal = find_tool("pdal")
    steps: list[object] = [
        str(laz),
        {
            "type": "filters.crop",
            "bounds": f"([{xmin},{xmax}],[{ymin},{ymax}])",
        },
    ]
    if _laz_needs_ground_filter(laz):
        steps.append(
            {
                "type": "filters.range",
                "limits": "Classification[2:2]",
            }
        )
    steps.append(
        {
            "type": "writers.gdal",
            "filename": str(dest_tif),
            "resolution": resolution_m,
            "output_type": "float32",
            "gdaldriver": "GTiff",
            "nodata": -9999,
        }
    )
    pipeline = {"pipeline": steps}
    dest_tif.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        json.dump(pipeline, tmp)
        pipe_path = tmp.name
    try:
        run_cmd([pdal, "pipeline", pipe_path], log=log)
    finally:
        Path(pipe_path).unlink(missing_ok=True)
    return dest_tif


def build_hillshade_from_dmr(
    dmr_laz: Path,
    template_png: Path,
    template_pgw: Path,
    dest_png: Path,
    dest_pgw: Path,
    *,
    log: callable | None = None,
) -> bool:
    if not dmr_laz.is_file():
        return False
    work = dest_png.parent
    dem_tif = work / "_dem.tif"
    shade_tif = work / "_hillshade.tif"
    crop = _template_bounds(template_png, template_pgw)
    resolution_m = _dem_resolution_m(template_png, template_pgw)
    if log:
        log(
            f"Hillshade: zdroj {dmr_laz.name}, DEM {resolution_m:.2f} m/px "
            f"(výřez dle pullautus.pgw)"
        )
    _pdal_dem_from_laz(
        dmr_laz, crop, dem_tif, resolution_m=resolution_m, log=log
    )
    if not dem_tif.is_file() or dem_tif.stat().st_size < 500:
        raise RuntimeError(f"PDAL nevytvořil DEM ({dem_tif.name})")
    gdaldem = _gdal_tool("gdaldem")
    run_cmd(
        [
            gdaldem,
            "hillshade",
            str(dem_tif),
            str(shade_tif),
            "-az",
            str(HILLSHADE_AZIMUTH),
            "-alt",
            str(HILLSHADE_ALTITUDE),
            "-compute_edges",
            "-of",
            "GTiff",
        ],
        log=log,
    )
    _align_to_template(shade_tif, template_png, template_pgw, dest_png, dest_pgw, log=log)
    dem_tif.unlink(missing_ok=True)
    shade_tif.unlink(missing_ok=True)
    if log:
        log(
            f"Hillshade DMR 5G: azimut {HILLSHADE_AZIMUTH}°, "
            f"výška slunce {HILLSHADE_ALTITUDE}° → {dest_png.name}"
        )
    return True


def fetch_orthophoto_wms(
    bounds_5514: tuple[float, float, float, float],
    template_png: Path,
    template_pgw: Path,
    dest_png: Path,
    dest_pgw: Path,
    *,
    log: callable | None = None,
) -> bool:
    xmin, ymin, xmax, ymax = bounds_5514
    _, _, _, _, width, height = _template_extent(template_png, template_pgw)
    tw, th = _target_size(width, height)
    params = {
        "service": "WMS",
        "request": "GetMap",
        "version": "1.3.0",
        "layers": "0",
        "styles": "default",
        "crs": "EPSG:5514",
        "bbox": f"{xmin},{ymin},{xmax},{ymax}",
        "width": str(tw),
        "height": str(th),
        "format": "image/jpeg",
        "transparent": "false",
    }
    url = ORTOFOTO_WMS + "?" + urllib.parse.urlencode(params)
    if log:
        log(f"Stahuji ortofoto ČÚZK ({tw}×{th} px)…")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    if len(data) < 500:
        return False
    tmp = dest_png.parent / "_ortho.jpg"
    tmp.write_bytes(data)
    gdaltranslate = _gdal_tool("gdal_translate")
    dest_png.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(
        [
            gdaltranslate,
            str(tmp),
            str(dest_png),
            "-of",
            "PNG",
            "-a_srs",
            "EPSG:5514",
            "-a_ullr",
            str(xmin),
            str(ymax),
            str(xmax),
            str(ymin),
        ],
        log=log,
    )
    tmp.unlink(missing_ok=True)
    _write_pgw_for_extent(dest_pgw, xmin, ymin, xmax, ymax, tw, th)
    if log:
        log(f"Ortofoto: {dest_png.name}")
    return True


def _lon_lat_to_tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    n = 1 << zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def _mercator_tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 1 << z
    tile_size = 2 * WEB_MERCATOR_HALF / n
    xmin = -WEB_MERCATOR_HALF + x * tile_size
    xmax = xmin + tile_size
    ymax = WEB_MERCATOR_HALF - y * tile_size
    ymin = ymax - tile_size
    return xmin, ymin, xmax, ymax


def _pick_osm_zoom(west: float, south: float, east: float, north: float) -> int:
    width_m = max(abs(east - west), abs(north - south)) * 111_000
    for z in range(18, 11, -1):
        res = 156543.03 * math.cos(math.radians((south + north) / 2)) / (1 << z)
        px = width_m / max(res, 1)
        if px <= MAX_REF_PIXELS * 1.2:
            return z
    return 12


def _write_osm_vrt(
    tiles: list[tuple[Path, int, int]],
    z: int,
    x0: int,
    y0: int,
    vrt_path: Path,
) -> None:
    width_px = (max(t[1] for t in tiles) - x0 + 1) * 256
    height_px = (max(t[2] for t in tiles) - y0 + 1) * 256
    ulx, _, _, uly = _mercator_tile_bounds(z, x0, y0)
    pixel = (2 * WEB_MERCATOR_HALF / (1 << z)) / 256
    lines = [
        f'<VRTDataset rasterXSize="{width_px}" rasterYSize="{height_px}">',
        f'  <GeoTransform>{ulx}, {pixel}, 0, {uly}, 0, {-pixel}</GeoTransform>',
        '  <SRS>EPSG:3857</SRS>',
        '  <VRTRasterBand dataType="Byte" band="1">',
    ]
    for path, x, y in tiles:
        dx = (x - x0) * 256
        dy = (y - y0) * 256
        lines.extend(
            [
                "    <SimpleSource>",
                f'      <SourceFilename relativeToVRT="0">{path.as_posix()}</SourceFilename>',
                '      <SrcRect xOff="0" yOff="0" xSize="256" ySize="256"/>',
                f'      <DstRect xOff="{dx}" yOff="{dy}" xSize="256" ySize="256"/>',
                "    </SimpleSource>",
            ]
        )
    lines.extend(["  </VRTRasterBand>", "</VRTDataset>"])
    vrt_path.write_text("\n".join(lines), encoding="utf-8")


def build_osm_reference(
    bbox_wgs84: tuple[float, float, float, float],
    template_png: Path,
    template_pgw: Path,
    dest_png: Path,
    dest_pgw: Path,
    *,
    log: callable | None = None,
) -> bool:
    west, south, east, north = bbox_wgs84
    z = _pick_osm_zoom(west, south, east, north)
    x0, y1 = _lon_lat_to_tile(west, north, z)
    x1, y0 = _lon_lat_to_tile(east, south, z)
    tiles: list[tuple[Path, int, int]] = []
    work = dest_png.parent / "_osm_tiles"
    work.mkdir(parents=True, exist_ok=True)
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            try:
                src = fetch_tile(z, x, y)
            except Exception:
                continue
            dest = work / f"{z}_{x}_{y}.png"
            if not dest.exists():
                dest.write_bytes(src.read_bytes())
            tiles.append((dest, x, y))
    if not tiles:
        return False
    if log:
        log(f"OSM dlaždice: zoom {z}, {len(tiles)} ks")
    vrt = work / "mosaic.vrt"
    _write_osm_vrt(tiles, z, x0, y0, vrt)
    raw_tif = work / "mosaic_3857.tif"
    gdaltranslate = _gdal_tool("gdal_translate")
    run_cmd([gdaltranslate, str(vrt), str(raw_tif), "-of", "GTiff"], log=log)
    _align_to_template(raw_tif, template_png, template_pgw, dest_png, dest_pgw, log=log)
    if log:
        log(f"OSM podklad: {dest_png.name} (© OpenStreetMap)")
    return True


def _find_dmr_ground_laz(job_dir: Path) -> Path | None:
    candidates = _hillshade_laz_candidates(job_dir)
    return candidates[0] if candidates else None


def build_reference_layers(
    job_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    template_png: Path,
    template_pgw: Path,
    out_dir: Path,
    *,
    log: callable | None = None,
) -> dict[str, Path]:
    """Vytvoří referenční PNG+PGW pro OOM (hillshade, ortofoto, OSM)."""
    if not template_png.is_file() or not template_pgw.is_file():
        return {}
    out_dir.mkdir(parents=True, exist_ok=True)
    west, south, east, north = bbox_wgs84
    bounds = crop_bounds_5514(west, south, east, north)
    built: dict[str, Path] = {}

    hill_png = out_dir / "hillshade_dmr5g.png"
    hill_pgw = out_dir / "hillshade_dmr5g.pgw"
    laz_candidates = _hillshade_laz_candidates(job_dir)
    if log and not laz_candidates:
        log("Hillshade: nenalezen LAZ v work/lidar")
    hill_ok = False
    for laz in laz_candidates:
        try:
            if build_hillshade_from_dmr(
                laz, template_png, template_pgw, hill_png, hill_pgw, log=log
            ):
                built["hillshade"] = hill_png
                hill_ok = True
                break
        except subprocess.CalledProcessError as exc:
            if log:
                log(f"Hillshade ({laz.name}): přeskočeno ({exc})")
                err = (exc.stderr or exc.stdout or "").strip()
                if err:
                    for line in err.splitlines()[-8:]:
                        log(line)
        except Exception as exc:
            if log:
                log(f"Hillshade ({laz.name}): přeskočeno ({exc})")
    if log and laz_candidates and not hill_ok:
        log("Hillshade: žádný LAZ zdroj neuspěl")

    ortho_png = out_dir / "orthophoto.png"
    ortho_pgw = out_dir / "orthophoto.pgw"
    try:
        if fetch_orthophoto_wms(
            bounds, template_png, template_pgw, ortho_png, ortho_pgw, log=log
        ):
            built["orthophoto"] = ortho_png
    except Exception as exc:
        if log:
            log(f"Ortofoto: přeskočeno ({exc})")

    osm_png = out_dir / "osm.png"
    osm_pgw = (out_dir / "osm.png").with_suffix(".pgw")
    try:
        if build_osm_reference(
            bbox_wgs84, template_png, template_pgw, osm_png, osm_pgw, log=log
        ):
            built["osm"] = osm_png
    except Exception as exc:
        if log:
            log(f"OSM podklad: přeskočeno ({exc})")

    return built
