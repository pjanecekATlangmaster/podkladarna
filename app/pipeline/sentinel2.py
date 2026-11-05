"""Družicový snímek Sentinel-2 jako podklad pro mapaře (šablona v .omap a soubor v ZIPu).

Petr 2026-11-05: „oko mapaře může vidět co my ne“ – nejnovější bezoblačný snímek výřezu
(pravé barvy a infračervené falešné barvy), s datem snímku v názvu souboru. Do kresby
nevstupuje (Sentinel-2 má 10 m/px, viz zamítnutý návrh pasek S1).

Zdroj: Sentinel-2 L2A jako Cloud Optimized GeoTIFF na AWS (Element84 earth-search STAC,
bez registrace, licence Copernicus – volné užití s uvedením zdroje). Čte se jen okno
výřezu přes ``/vsicurl/`` (GDAL), ne celé dlaždice 110 × 110 km.

Výběr snímku: nejnovější s jasným výřezem podle masky SCL (mraky, stíny, sníh do
``MAX_BAD_SHARE``) a navíc nejnovější jasný z vegetační sezóny, pokud ten první
v sezóně není (listnatý les vypadá v zimě jinak).
"""

from __future__ import annotations

import json
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from app.pipeline.crs_5514 import CRS_PROJ4
from app.pipeline.fetch_openzu import USER_AGENT

STAC_SEARCH_URL = "https://earth-search.aws.element84.com/v1/search"
STAC_COLLECTION = "sentinel-2-l2a"
# Jak daleko do minulosti hledat (pokryje i poslední vegetační sezónu).
SEARCH_DAYS = 400
# Předfiltr podle oblačnosti celé dlaždice (%); přesně rozhoduje maska SCL ve výřezu.
TILE_CLOUD_MAX = 60
SEARCH_LIMIT = 100
# Kolik kandidátů nejvýš ověřit maskou SCL (každý = jedno malé čtení COG).
MAX_SCL_CHECKS = 12
# SCL: 0 bez dat, 1 saturace, 3 stín mraku, 8 a 9 mraky, 10 cirrus, 11 sníh.
SCL_BAD = frozenset({0, 1, 3, 8, 9, 10, 11})
MAX_BAD_SHARE = 0.02
# Vegetační sezóna (měsíce) pro druhý snímek.
LEAF_MONTHS = frozenset({5, 6, 7, 8, 9})
# Výstupní krok: nativně 10 m, 5 m s kubickým převzorkováním je v Mapperu příjemnější
# na oko a ZIP to zvětší jen o stovky kB.
OUT_MPP = 5.0
JPEG_QUALITY = 90
# L2A od processing baseline 04.00: DN = odrazivost · 10 000 + 1 000 (offset -0,1 ve STAC).
# Rozsahy odrazivosti pro falešné barvy (NIR, červená, zelená) – běžné natažení CIR.
CIR_BANDS = (("nir", 0.0, 0.65), ("red", 0.0, 0.15), ("green", 0.0, 0.15))
HTTP_TIMEOUT_S = 60
# GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR ne: platí i pro místní výstup (ENVI pak gdalwarp
# skončí chybou bez hlášky); vedlejší soubory u COG nehledá už povolená přípona.
GDAL_HTTP_CONFIG = (
    ("GDAL_HTTP_TIMEOUT", str(HTTP_TIMEOUT_S)),
    ("GDAL_HTTP_MAX_RETRY", "3"),
    ("GDAL_HTTP_RETRY_DELAY", "3"),
    ("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif"),
)
FILE_PREFIX = "sentinel2_"
INFO_NAME = "sentinel2.txt"
ATTRIBUTION = "Obsahuje upravená data Copernicus Sentinel {year} (ESA / EU Copernicus)."


@dataclass(frozen=True)
class Scene:
    item_id: str
    day: date
    tile_cloud: float
    assets: dict[str, str]
    dn_offset: float  # DN odpovídající nulové odrazivosti (1000 od baseline 04.00, jinak 0)

    @property
    def leaf_on(self) -> bool:
        return self.day.month in LEAF_MONTHS


def _gdal_config_args() -> list[str]:
    args: list[str] = []
    for key, value in GDAL_HTTP_CONFIG:
        args += ["--config", key, value]
    return args


def _scene_from_feature(feat: dict) -> Scene | None:
    props = feat.get("properties") or {}
    assets = feat.get("assets") or {}
    hrefs: dict[str, str] = {}
    for key in ("visual", "scl", "nir", "red", "green"):
        href = (assets.get(key) or {}).get("href", "")
        if not href.startswith("https://"):
            return None
        hrefs[key] = href
    try:
        day = date.fromisoformat(str(props["datetime"])[:10])
    except (KeyError, ValueError):
        return None
    bands = (assets.get("nir") or {}).get("raster:bands") or [{}]
    offset = float(bands[0].get("offset") or 0.0)
    scale = float(bands[0].get("scale") or 0.0001)
    return Scene(
        item_id=str(feat.get("id", "")),
        day=day,
        tile_cloud=float(props.get("eo:cloud_cover") or 0.0),
        assets=hrefs,
        dn_offset=-offset / scale if scale else 0.0,
    )


def search_scenes(
    bbox_wgs84: tuple[float, float, float, float],
    *,
    today: date | None = None,
    opener=urllib.request.urlopen,
) -> list[Scene]:
    """Kandidáti od nejnovějšího (jen podle oblačnosti celé dlaždice)."""
    end = (today or date.today()) + timedelta(days=1)
    start = end - timedelta(days=SEARCH_DAYS)
    body = {
        "collections": [STAC_COLLECTION],
        "bbox": [float(v) for v in bbox_wgs84],
        "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T00:00:00Z",
        "query": {"eo:cloud_cover": {"lt": TILE_CLOUD_MAX}},
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
        "limit": SEARCH_LIMIT,
    }
    req = urllib.request.Request(
        STAC_SEARCH_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with opener(req, timeout=HTTP_TIMEOUT_S) as resp:
        data = json.load(resp)
    scenes: list[Scene] = []
    for feat in data.get("features") or []:
        scene = _scene_from_feature(feat)
        if scene is not None:
            scenes.append(scene)
    scenes.sort(key=lambda s: (s.day, -s.tile_cloud), reverse=True)
    return scenes


def bad_share(scl_values: bytes) -> float:
    """Podíl pixelů SCL, které výřez kazí (mraky, stíny, sníh, bez dat)."""
    if not scl_values:
        return 1.0
    bad = sum(1 for v in scl_values if v in SCL_BAD)
    return bad / len(scl_values)


def _warp(
    srcs: list[str],
    bounds_5514: tuple[float, float, float, float],
    dest: Path,
    *,
    mpp: float,
    resampling: str,
    out_format: str,
    run_cmd,
    gdal_tool,
    log=None,
) -> None:
    xmin, ymin, xmax, ymax = bounds_5514
    cmd = [
        gdal_tool("gdalwarp"),
        "-q",
        "-overwrite",
        *_gdal_config_args(),
        "-t_srs",
        CRS_PROJ4,
        "-te",
        f"{xmin:.3f}",
        f"{ymin:.3f}",
        f"{xmax:.3f}",
        f"{ymax:.3f}",
        "-tr",
        f"{mpp}",
        f"{mpp}",
        "-r",
        resampling,
        "-of",
        out_format,
        *[f"/vsicurl/{s}" for s in srcs],
        str(dest),
    ]
    run_cmd(cmd, log=log)


def scl_bad_share(scene: Scene, bounds_5514, tmp: Path, *, run_cmd, gdal_tool, log=None) -> float:
    raw = tmp / f"scl_{scene.item_id}.bin"
    _warp(
        [scene.assets["scl"]],
        bounds_5514,
        raw,
        mpp=20.0,
        resampling="near",
        out_format="ENVI",
        run_cmd=run_cmd,
        gdal_tool=gdal_tool,
    )
    return bad_share(raw.read_bytes())


def pick_scenes(scenes: list[Scene], is_clear) -> list[Scene]:
    """Nejnovější jasný snímek + nejnovější jasný ve vegetační sezóně (když ten první není)."""
    picked: list[Scene] = []
    checked = 0
    seen_days: set[date] = set()
    for scene in scenes:
        if picked and picked[0].leaf_on:
            break
        if picked and not scene.leaf_on:
            continue
        if scene.day in seen_days or checked >= MAX_SCL_CHECKS:
            continue
        checked += 1
        if not is_clear(scene):
            continue
        seen_days.add(scene.day)
        picked.append(scene)
        if len(picked) == 2:
            break
    return picked


def _render(
    scene: Scene,
    bounds_5514,
    out_dir: Path,
    tmp: Path,
    *,
    run_cmd,
    gdal_tool,
    write_world,
    log=None,
) -> list[tuple[str, Path]]:
    """Pravé barvy (TCI) a falešné barvy CIR jako JPEG + .jgw. Vrací (klíč, cesta)."""
    stamp = scene.day.isoformat()
    xmin, ymin, xmax, ymax = bounds_5514
    width = max(1, round((xmax - xmin) / OUT_MPP))
    height = max(1, round((ymax - ymin) / OUT_MPP))
    bounds = (xmin, ymax - height * OUT_MPP, xmin + width * OUT_MPP, ymax)
    out: list[tuple[str, Path]] = []

    def _to_jpeg(src_tif: Path, dest: Path, scale_args: list[str]) -> None:
        run_cmd(
            [
                gdal_tool("gdal_translate"),
                "-q",
                "-of",
                "JPEG",
                "-ot",
                "Byte",
                *scale_args,
                "-co",
                f"QUALITY={JPEG_QUALITY}",
                str(src_tif),
                str(dest),
            ],
            log=log,
        )
        write_world(dest.with_suffix(".jgw"), *bounds, width, height)
        aux = dest.with_name(dest.name + ".aux.xml")
        if aux.is_file():
            aux.unlink()

    tci = tmp / f"tci_{stamp}.tif"
    _warp([scene.assets["visual"]], bounds, tci, mpp=OUT_MPP, resampling="cubic",
          out_format="GTiff", run_cmd=run_cmd, gdal_tool=gdal_tool, log=log)
    rgb = out_dir / f"{FILE_PREFIX}{stamp}.jpg"
    _to_jpeg(tci, rgb, ["-b", "1", "-b", "2", "-b", "3"])
    out.append((f"sentinel2_{stamp}", rgb))

    scale_args: list[str] = []
    band_tifs: list[Path] = []
    for i, (key, lo, hi) in enumerate(CIR_BANDS, start=1):
        band_tif = tmp / f"{key}_{stamp}.tif"
        _warp([scene.assets[key]], bounds, band_tif, mpp=OUT_MPP, resampling="cubic",
              out_format="GTiff", run_cmd=run_cmd, gdal_tool=gdal_tool, log=log)
        band_tifs.append(band_tif)
        scale_args += [
            f"-scale_{i}",
            f"{scene.dn_offset + lo * 10000:.0f}",
            f"{scene.dn_offset + hi * 10000:.0f}",
            "1",
            "255",
        ]
    vrt = tmp / f"cir_{stamp}.vrt"
    run_cmd([gdal_tool("gdalbuildvrt"), "-q", "-separate", str(vrt), *map(str, band_tifs)], log=log)
    cir = out_dir / f"{FILE_PREFIX}{stamp}_nir.jpg"
    _to_jpeg(vrt, cir, scale_args)
    out.append((f"sentinel2_{stamp}_nir", cir))
    return out


def info_text(scenes: list[Scene]) -> str:
    lines = [
        "Sentinel-2 L2A – družicový snímek jako podklad pro mapaře (do kresby nevstupuje)",
        "",
    ]
    for scene in scenes:
        season = "vegetační sezóna" if scene.leaf_on else "mimo vegetační sezónu"
        lines += [
            f"Datum snímku: {scene.day.isoformat()} ({season})",
            f"  snímek: {scene.item_id}, oblačnost celé dlaždice {scene.tile_cloud:.1f} %",
            f"  {FILE_PREFIX}{scene.day.isoformat()}.jpg – pravé barvy",
            f"  {FILE_PREFIX}{scene.day.isoformat()}_nir.jpg – falešné barvy (NIR, červená, zelená):"
            " živá vegetace červeně, holiny a paseky šedohnědě, voda tmavě",
        ]
    lines += [
        "",
        f"Rozlišení 10 m/px (převzorkováno na {OUT_MPP:g} m/px), S-JTSK Křovák (EPSG:5514), world file .jgw.",
        "Ve výřezu bez mraků podle masky SCL. Hodí se k posouzení novějších změn (paseky, polomy,",
        "nové stavby, cesty) po datu ortofota a LiDARu; drobnosti pod ~20 m nerozliší.",
        "Zdroj: Copernicus Sentinel-2 L2A přes Element84 earth-search (AWS Open Data).",
        ATTRIBUTION.format(year=", ".join(sorted({str(s.day.year) for s in scenes}))),
        "",
    ]
    return "\n".join(lines)


def build_sentinel2_reference(
    bbox_wgs84: tuple[float, float, float, float],
    bounds_5514: tuple[float, float, float, float],
    out_dir: Path,
    *,
    run_cmd,
    gdal_tool,
    write_world,
    log=None,
    today: date | None = None,
) -> dict[str, Path]:
    """Stáhne snímky do ``out_dir`` (JPEG + .jgw + ``sentinel2.txt``). Vrací {klíč: JPEG}."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"{FILE_PREFIX}*"):
        old.unlink()
    scenes = search_scenes(bbox_wgs84, today=today)
    if log:
        log(f"Sentinel-2: {len(scenes)} kandidátů za {SEARCH_DAYS} dní")
    if not scenes:
        return {}
    with tempfile.TemporaryDirectory(prefix="s2_", dir=out_dir.parent) as tmp_name:
        tmp = Path(tmp_name)

        def _clear(scene: Scene) -> bool:
            try:
                share = scl_bad_share(scene, bounds_5514, tmp, run_cmd=run_cmd, gdal_tool=gdal_tool)
            except Exception as exc:
                if log:
                    log(f"Sentinel-2 {scene.day}: maska SCL nečitelná ({type(exc).__name__})")
                return False
            if log:
                log(f"Sentinel-2 {scene.day}: mraky / sníh / bez dat ve výřezu {share * 100:.1f} %")
            return share <= MAX_BAD_SHARE

        picked = pick_scenes(scenes, _clear)
        if not picked:
            if log:
                log("Sentinel-2: žádný jasný snímek výřezu – přeskočeno")
            return {}
        built: dict[str, Path] = {}
        for scene in picked:
            for key, path in _render(
                scene, bounds_5514, out_dir, tmp,
                run_cmd=run_cmd, gdal_tool=gdal_tool, write_world=write_world, log=log,
            ):
                built[key] = path
    (out_dir / INFO_NAME).write_text(info_text(picked), encoding="utf-8")
    if log:
        log("Sentinel-2: " + ", ".join(p.name for p in built.values()))
    return built
