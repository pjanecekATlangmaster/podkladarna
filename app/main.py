from __future__ import annotations

import hmac
import logging
from html import escape as html_escape
import mimetypes
import re
import traceback
import urllib.parse
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import countries, db, dtm_coverage, i18n, settings as app_settings, source_status, worker
from app.cleanup import maybe_purge_old_jobs, purge_old_jobs, start_cleanup_scheduler
from app.client_ip import client_ip
from app.feedback import (
    MAX_COMMENT_LEN,
    MAX_CONTACT_LEN,
    MAX_JOB_ID_LEN,
    check_feedback_rate,
    send_feedback,
)
from app.guide_text import web_about_html
from app.job_template import job_form_template
from app.mail import MailError
from app.rate_limit import check_create_api_job, check_create_job
from app.pipeline import build_stats
from app.pipeline.fetch_openzu import (
    FetchError,
    MAX_BBOX_AREA_KM2,
    MAX_BBOX_KM,
    MAX_SHEETS,
    REF_PNG_ESTIMATE_MINUTES,
    bbox_area_km2,
    bbox_exceeds_limit,
    bbox_size_km,
    estimate_georef_minutes,
    estimate_map_minutes,
    estimate_note,
    estimate_minutes,
    forest_area_hint,
    forest_max_area_km2,
    parse_bbox,
    query_sm5_sheets,
    sprint_area_hint,
    sprint_max_area_km2,
)
from app.pipeline.job_options import (
    FORMLINE_MODES,
    KP_CLIFF_SENSITIVITY,
    KP_VEGE_HEIGHT_CHOICES,
    CONTOUR_SMOOTH_LEVELS,
    VEG_SMOOTH_LEVELS,
    default_map_types,
    load_presets,
    parse_map_types,
    resolve_cliff_sensitivity,
    resolve_formline_mode,
    resolve_vege_height,
    OSM_OBJECTS_ALL,
    OSM_OBJECTS_AUTO,
    OSM_OBJECTS_NONE,
    resolve_osm_objects_mode,
    resolve_use_dtm,
    OSM_OBJECTS_LEGACY_KEYS,
    resolve_contour_smooth,
    resolve_veg_smooth,
)
from app.pipeline.package_oom import (
    CONTOURS_BY_SCALE,
    DEFAULT_CONTOUR_BY_SCALE,
    MAP_SCALES,
    resolve_omap_job,
)
from app.settings import (
    CLEANUP_INTERVAL_HOURS,
    DEFAULT_OPTIONS,
    DOWNLOADS_DIR,
    ESTIMATE_CALIB_MIN_JOBS,
    ESTIMATE_WARN_MINUTES,
    JOB_TIMEOUT_MINUTES,
    JOBS_DIR,
    MAX_QUEUE_SIZE,
    APP_VERSION,
    PRIVATE_JOB_RETENTION_HOURS,
    SMTP_FROM,
    SMTP_HOST,
    default_footway_as_sidewalk,
)
from app.tiles import TileError, fetch_tile
from app.tool_env import log_ignored_gdal_plugins, tool_status
from app.whats_new import whats_new_payload

logger = logging.getLogger("podkladarna")


def _upload_files(form, key: str) -> list[UploadFile]:
    """Vrátí nahrané soubory pro dané pole (robustní vůči TestClient i prohlížeči)."""
    files: list[UploadFile] = []
    for field_name, value in form.multi_items():
        if field_name != key:
            continue
        if not hasattr(value, "read"):
            continue
        if not (getattr(value, "filename", None) or "").strip():
            continue
        # filename může být None u některých klientů – řeší se při ukládání
        files.append(value)
    return files


def _form_str(form, key: str, default: str = "") -> str:
    val = form.get(key)
    if val is None:
        return default
    return str(val)


def _form_list(form, key: str) -> list[str]:
    """Všechny hodnoty pole formuláře (opakované pole / getlist)."""
    getlist = getattr(form, "getlist", None)
    if getlist is None:
        val = form.get(key)
        return [] if val is None else [str(val)]
    return [str(v) for v in getlist(key)]


_FORMLINE_LABEL = {"off": "ne", "sparse": "střídmě", "more": "víc", "all": "všude"}
_OSM_OBJECTS_LABEL = {"auto": "základní ve sprintu", "1": "vše", "none": "žádné"}

app = FastAPI(title="Podkladarna", version=APP_VERSION)

STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
# Minimální image (conda) často nezná .svg → nginx nosniff logo nenačte.
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("image/png", ".png")


@app.on_event("startup")
def startup() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log_ignored_gdal_plugins()
    db.init_db()
    interrupted = worker.recover_after_restart()
    if interrupted:
        logger.info("Recovered %s interrupted job(s): %s", len(interrupted), interrupted)
    requeued = worker.requeue_after_restart()
    if requeued:
        logger.info("Requeued %s waiting job(s): %s", len(requeued), requeued)
    worker.start_drain_watch()
    from app.job_worker import retry_missed_private_mails

    resent = retry_missed_private_mails()
    if resent:
        logger.info("Resent private download mail for %s job(s): %s", len(resent), resent)
    removed = purge_old_jobs()
    if removed:
        logger.info("Startup cleanup: removed %s old job(s)", removed)
    start_cleanup_scheduler(CLEANUP_INTERVAL_HOURS)
    tools = tool_status()
    missing = [name for name, path in tools.items() if not path]
    logger.info("Nástroje: %s", tools)
    if missing:
        logger.warning(
            "Chybí %s – pipeline na tomto stroji nepoběží. "
            "Windows: OSGeo4W / QGIS PATH, nebo docker compose -f docker-compose.dev.yml up",
            ", ".join(missing),
        )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError):
    logger.warning("Validation error: %s", exc.errors())
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    if isinstance(detail, i18n.Msg):
        # Hláška v jazyce stránky + stabilní kód pro klienty API (text se může měnit).
        content = {"detail": detail.render(i18n.resolve_lang(request)[0]), "code": detail.key}
        return JSONResponse(status_code=exc.status_code, content=content)
    return JSONResponse(status_code=exc.status_code, content={"detail": detail})


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request: Request, exc: Exception):
    logger.exception("Unhandled error")
    return JSONResponse(status_code=500, content={"detail": str(exc)})


WEB_DIR = STATIC_DIR.parent


def _is_test_channel() -> bool:
    return app_settings.INSTANCE_CHANNEL == "test"


def apply_channel(html: str) -> str:
    """Testovací verze (B): banner s odkazem na stabilní verzi a ``noindex``.

    Stabilní verze (A) o testovací nic neví – žádný odkaz ani zmínka.
    """
    if not _is_test_channel():
        return html
    name = f" ({app_settings.INSTANCE_NAME})" if app_settings.INSTANCE_NAME else ""
    stable = html_escape(app_settings.STABLE_URL, quote=True)
    banner = (
        '<div class="channel-banner" role="note">'
        f"{{{{t:web.channel.test}}}}{html_escape(name)} – {{{{t:web.channel.may_have_bugs}}}} "
        f'<a href="{stable}">{{{{t:web.channel.go_stable}}}}</a>'
        "</div>"
    )
    # Na testu úvodní text „O co jde“ nepotřebujeme – formulář je hned nahoře.
    html = re.sub(r'\s*<section class="card about-card">.*?</section>', "", html, count=1, flags=re.S)
    html = html.replace("<body>", "<body>\n  " + banner, 1)
    return html.replace("</head>", '  <meta name="robots" content="noindex, nofollow" />\n</head>', 1)


@app.middleware("http")
async def _noindex_test_channel(request: Request, call_next):
    response = await call_next(request)
    if _is_test_channel():
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _note_download(job_id: str, zip_path: Path) -> None:
    """Statistika použití: stažení výstupního ZIPu (chyba stažení nezastaví)."""
    try:
        db.record_download(job_id, zip_path.stat().st_size)
    except Exception:
        logger.exception("Statistika stažení jobu %s selhala", job_id)


_CS_MONTHS = (
    "ledna", "února", "března", "dubna", "května", "června",
    "července", "srpna", "září", "října", "listopadu", "prosince",
)


def _cs_int(n: float) -> str:
    return f"{int(round(n)):,}".replace(",", "\u00a0")


def _cs_bytes(n: float) -> str:
    gb = n / 1e9
    if gb >= 1000:
        return f"{gb / 1000:.1f}".replace(".", ",") + "\u00a0TB"
    if gb >= 10:
        return f"{_cs_int(gb)}\u00a0GB"
    if gb >= 1:
        return f"{gb:.1f}".replace(".", ",") + "\u00a0GB"
    return f"{_cs_int(n / 1e6)}\u00a0MB"


def _cs_since(iso: str | None) -> str:
    try:
        d = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return ""
    return f"{d.day}. {_CS_MONTHS[d.month - 1]} {d.year}"


def _en_int(n: float) -> str:
    return f"{int(round(n)):,}"


def _en_bytes(n: float) -> str:
    gb = n / 1e9
    if gb >= 1000:
        return f"{gb / 1000:.1f}\u00a0TB"
    if gb >= 10:
        return f"{_en_int(gb)}\u00a0GB"
    if gb >= 1:
        return f"{gb:.1f}\u00a0GB"
    return f"{_en_int(n / 1e6)}\u00a0MB"


def _en_since(iso: str | None) -> str:
    try:
        d = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return ""
    return f"{d.day} {_EN_MONTHS[d.month - 1]} {d.year}"


_EN_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _usage_footer_en(totals: dict, val, n_maps: int, since_iso: str | None) -> str:
    n_jobs = int(round(val("jobs_done")))
    parts = [
        f"{_en_int(n_maps)} map{'s' if n_maps != 1 else ''} generated "
        f"({_en_int(n_jobs)} job{'s' if n_jobs != 1 else ''})"
    ]
    hours = val("build_seconds") / 3600
    if hours >= 1:
        parts.append(f"{_en_int(hours)} h of processing")
    if val("source_bytes") > 0:
        parts.append(f"{_en_bytes(val('source_bytes'))} of source data processed")
    if val("download_count") > 0:
        parts.append(f"{_en_int(val('download_count'))}× downloaded ({_en_bytes(val('download_bytes'))})")
    since = _en_since(since_iso)
    lead = f"Since {since}: " if since else "Total: "
    if app_settings.INSTANCE_CHANNEL == "test":
        lead = "Test version – " + lead[0].lower() + lead[1:]
    return f'<p class="footer-usage">{html_escape(lead + " · ".join(parts))}</p>'


def usage_footer_html(lang: str = "cs") -> str:
    """Řádek patičky se souhrnnou statistikou použití (jen čísla, bez osobních údajů)."""
    if not app_settings.SHOW_USAGE_STATS:
        return ""
    try:
        totals = db.usage_totals()
    except Exception:
        logger.exception("Statistika použití nejde načíst")
        return ""
    maps = totals.get("maps_done")
    if not maps or maps["value"] <= 0:
        return ""

    def val(key: str) -> float:
        return float((totals.get(key) or {}).get("value") or 0)

    n_maps = int(round(maps["value"]))
    if lang != "cs":
        return _usage_footer_en(totals, val, n_maps, maps.get("since"))
    word = "mapa" if n_maps == 1 else ("mapy" if 2 <= n_maps <= 4 else "map")
    parts = [f"vygenerováno {_cs_int(n_maps)} {word} ({_cs_int(val('jobs_done'))} zadání)"]
    hours = val("build_seconds") / 3600
    if hours >= 1:
        parts.append(f"{_cs_int(hours)} h zpracování")
    if val("source_bytes") > 0:
        parts.append(f"zpracováno {_cs_bytes(val('source_bytes'))} podkladů")
    if val("download_count") > 0:
        parts.append(
            f"{_cs_int(val('download_count'))}× staženo ({_cs_bytes(val('download_bytes'))})"
        )
    since = _cs_since(maps.get("since"))
    lead = f"Od {since}: " if since else "Celkem: "
    if app_settings.INSTANCE_CHANNEL == "test":
        lead = "Testovací verze – " + lead[0].lower() + lead[1:]
    # Stažení se měří až od 3.25.0 – od kdy, napoví title.
    detail = "; ".join(
        f"{label} od {_cs_since(totals[key]['since'])}"
        for key, label in (("build_seconds", "čas zpracování"), ("download_count", "stažení"))
        if key in totals and _cs_since(totals[key]["since"]) != since
    )
    title = f' title="{html_escape(detail, quote=True)}"' if detail else ""
    return f'<p class="footer-usage"{title}>{html_escape(lead + " · ".join(parts))}</p>'


def usage_stats_payload() -> dict:
    """Agregovaná statistika pro box na webu (veřejné i privátní joby, bez osobních údajů)."""
    if not app_settings.SHOW_USAGE_STATS:
        return {"enabled": False}
    totals = db.usage_totals()

    def total(key: str) -> dict | None:
        t = totals.get(key)
        return {"value": t["value"], "since": t["since"]} if t else None

    year = db.recent_usage(app_settings.ORIGIN_RETENTION_DAYS)
    return {
        "enabled": True,
        "channel": app_settings.INSTANCE_CHANNEL,
        "totals": {
            key: total(key)
            for key in (
                "jobs_done",
                "maps_done",
                "jobs_failed",
                "build_seconds",
                "area_km2",
                "source_bytes",
                "output_bytes",
                "download_count",
                "download_bytes",
            )
        },
        "last_30_days": db.recent_usage(30),
        "last_year": year,
        # Místo pro budoucí součet příspěvků (zatím se nezobrazuje).
        "donations": None,
    }


def _localized_html(request: Request, html: str, *, web_catalog: bool = False) -> HTMLResponse:
    """Stránka v jazyce požadavku (i18n.resolve_lang); HTML se necachuje (jazyk z cookie / domény)."""
    lang, set_cookie = i18n.resolve_lang(request)
    response = HTMLResponse(i18n.localize_page(html, request, lang, web_catalog=web_catalog))
    response.headers["Cache-Control"] = "private, no-store"
    if set_cookie:
        forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        secure = request.url.scheme == "https" or forwarded == "https"
        i18n.set_lang_cookie(response, lang, secure=secure)
    return response


def _page_source(name: str, request: Request) -> str:
    """Celá stránka v jazyce (``web/<jazyk>/<name>``); bez překladu anglicky, pak česky ``web/<name>``."""
    lang = i18n.resolve_lang(request)[0]
    if lang != i18n.SOURCE_LANG:
        for code in (lang, i18n.BASE_LANG):
            path = WEB_DIR / code / name
            if path.is_file():
                return path.read_text(encoding="utf-8")
    return (WEB_DIR / name).read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    try:
        removed = maybe_purge_old_jobs()
        if removed:
            logger.info("Pageview cleanup: removed %s old job(s)", removed)
    except Exception:
        logger.exception("Pageview cleanup failed")
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    lang = i18n.resolve_lang(request)[0]
    html = html.replace("<!-- PODKLADARNA_ABOUT -->", web_about_html(lang))
    html = html.replace("{{APP_VERSION}}", APP_VERSION)
    html = html.replace("<!-- PODKLADARNA_USAGE -->", usage_footer_html(lang))
    # Cloudflare cachuje /static/* až 4 h – query podle verze vynutí nový JS/CSS po deployi.
    v = urllib.parse.quote(APP_VERSION, safe="")
    html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={v}"')
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={v}"')
    return _localized_html(request, apply_channel(html), web_catalog=True)


@app.get("/licence", response_class=HTMLResponse)
def licence_page(request: Request) -> HTMLResponse:
    html = _page_source("licence.html", request)
    html = html.replace("{{APP_VERSION}}", APP_VERSION)
    v = urllib.parse.quote(APP_VERSION, safe="")
    html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={v}"')
    return _localized_html(request, html)


@app.get("/podpora", response_class=HTMLResponse)
def support_page(request: Request) -> HTMLResponse:
    html = _page_source("podpora.html", request)
    html = html.replace("{{APP_VERSION}}", APP_VERSION)
    v = urllib.parse.quote(APP_VERSION, safe="")
    html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={v}"')
    return _localized_html(request, html)


@app.get("/api/usage_stats")
def api_usage_stats():
    try:
        return usage_stats_payload()
    except Exception:
        logger.exception("Statistika použití nejde načíst")
        return {"enabled": False}


@app.get("/api/source_status")
def api_source_status():
    """Dostupnost zdrojů dat (web podle ní varuje před spuštěním).

    Starší výsledek než ``SOURCE_CHECK_TTL_MIN`` spustí kontrolu na pozadí (``checking``)."""
    ttl = app_settings.SOURCE_CHECK_TTL_MIN
    checking = source_status.refresh_if_stale()
    return {"enabled": ttl > 0, "ttl_min": ttl, "checking": checking, **source_status.snapshot()}


@app.get("/api/presets")
def api_presets():
    presets = load_presets()
    return {
        k: {"id": k, "label": v.get("label", k), **v}
        for k, v in presets.items()
    }


@app.get("/api/map_options")
def api_map_options():
    """Měřítka a povolené ekvidistance pro GUI (bez „typu mapy“)."""
    return {
        "scales": list(MAP_SCALES),
        "contours_by_scale": {
            str(scale): list(vals) for scale, vals in CONTOURS_BY_SCALE.items()
        },
        "default_contour_by_scale": {
            str(scale): val for scale, val in DEFAULT_CONTOUR_BY_SCALE.items()
        },
    }


@app.get("/api/whats_new")
def api_whats_new(request: Request):
    """Stáří posledního buildu + větší změny (box nad formulářem), v jazyce stránky."""
    return whats_new_payload(lang=i18n.resolve_lang(request)[0])


def _exc_detail(exc: BaseException):
    """Hláška výjimky pro HTTPException: ``i18n.Msg`` beze změny (přeloží se), jinak text."""
    arg = exc.args[0] if exc.args else None
    return arg if isinstance(arg, i18n.Msg) else str(exc)


def _check_country(
    bbox: tuple[float, float, float, float], request: Request, country: str | None = None
) -> tuple[countries.Country, float]:
    """Výřez proti povolenému území domény (polygon země, aspoň polovina výřezu v ní) – vždy na serveru."""
    try:
        return countries.check_bbox(bbox, i18n.site_for_request(request), country or None)
    except countries.CountryError as exc:
        raise HTTPException(400, exc.args[0]) from exc


@app.get("/api/coverage")
def api_coverage(request: Request):
    """Povolené země domény s obrysem hranice (mapa v GUI: pohyb, maska mimo území)."""
    return countries.coverage_payload(i18n.site_for_request(request), i18n.resolve_lang(request)[0])


@app.get("/api/dtm_coverage")
def api_dtm_coverage(bbox: str, request: Request, country: str = ""):
    """Odhad kvality DTM obcí ve výřezu (velikost balíků z HEAD, bez stahování) – jen doplněk pro GUI."""
    try:
        box = parse_bbox(bbox)
    except FetchError as exc:
        raise HTTPException(400, _exc_detail(exc)) from exc
    job_country, _share = _check_country(box, request, country)
    if job_country.code != "CZ":
        return {"available": False, "reason": "country", "units": []}
    return dtm_coverage.coverage_cached(box)


@app.get("/api/sheets")
def api_sheets(bbox: str, request: Request, country: str = ""):
    return i18n.localize(_sheets_payload(bbox, request, country), i18n.resolve_lang(request)[0])


def _sheets_payload(bbox: str, request: Request, country: str = "") -> dict:
    """bbox=west,south,east,north (WGS84) → listy SM5 + odhad času."""
    try:
        west, south, east, north = parse_bbox(bbox)
    except FetchError as exc:
        raise HTTPException(400, _exc_detail(exc)) from exc
    job_country, share = _check_country((west, south, east, north), request, country)
    country_info = {"country": job_country.code, "outside_share": countries.outside_share(share)}
    width_km, height_km = bbox_size_km(west, south, east, north)
    area_km2 = bbox_area_km2(west, south, east, north)
    if bbox_exceeds_limit(west, south, east, north):
        return {
            **country_info,
            "sheets": [],
            "count": 0,
            "max_sheets": MAX_SHEETS,
            "width_km": round(width_km, 2),
            "height_km": round(height_km, 2),
            "area_km2": round(area_km2, 1),
            "max_km": MAX_BBOX_KM,
            "max_area_km2": MAX_BBOX_AREA_KM2,
            "sprint_max_area_km2": sprint_max_area_km2(),
            "forest_max_area_km2": forest_max_area_km2(),
            "too_large": True,
            "too_large_reason": "size",
            "estimate_minutes": None,
            "estimate_minutes_with_refs": None,
            "label": i18n.Msg(
                "api.sheets.label_area",
                width=float(width_km),
                height=float(height_km),
                area=float(area_km2),
            ),
            "hint": i18n.Msg(
                "api.sheets.too_big",
                width=float(width_km),
                height=float(height_km),
                area=float(area_km2),
                max=float(MAX_BBOX_AREA_KM2),
                side=float(MAX_BBOX_KM),
                forest_max=float(forest_max_area_km2()),
                sprint_max=float(sprint_max_area_km2()),
            ),
        }
    try:
        sheets = query_sm5_sheets(west, south, east, north)
    except FetchError as exc:
        raise HTTPException(400, _exc_detail(exc)) from exc
    names = [s["mapnom"] for s in sheets]
    sheets_too_big = len(sheets) > MAX_SHEETS
    hint = (
        i18n.Msg("api.sheets.too_many", names=", ".join(names), count=len(sheets), max=MAX_SHEETS)
        if sheets_too_big
        else None
    )
    can_estimate = bool(sheets) and not sheets_too_big
    # Samokalibrace z vlastních dokončených jobů (3.34.0): zvlášť příprava a skládání map.
    try:
        cal = db.estimate_calibration()
    except Exception:  # noqa: BLE001 – odhad musí jít i bez kalibrace
        cal = {"prep": 1.0, "maps": 1.0, "jobs": 0}
    pf, mf = float(cal["prep"]), float(cal["maps"])
    calibrated = int(cal.get("jobs") or 0) >= ESTIMATE_CALIB_MIN_JOBS and (pf != 1.0 or mf != 1.0)
    extra = 1.0 if calibrated else 0.0
    kw = {"prep_factor": pf, "maps_factor": mf, "prep_extra": extra}
    est = estimate_minutes(names, area_km2=area_km2, **kw) if can_estimate else None
    est_refs = (
        estimate_minutes(names, area_km2=area_km2, include_references=True, **kw)
        if can_estimate
        else None
    )
    # Bez skládání .omap: tlačítko si přičte mapy podle měřítka a volby DTM (sprint 1 mapa, les + MTBO 2).
    est_base = (
        {
            "plain": estimate_minutes(
                names, area_km2=area_km2, include_maps=False, prep_factor=pf, prep_extra=extra
            ),
            "refs": estimate_minutes(
                names,
                area_km2=area_km2,
                include_maps=False,
                include_references=True,
                prep_factor=pf,
                prep_extra=extra,
            ),
        }
        if can_estimate
        else None
    )
    est_maps = (
        {
            str(s): {
                "dtm": estimate_map_minutes(area_km2, s, True, mf),
                "nodtm": estimate_map_minutes(area_km2, s, False, mf),
            }
            for s in MAP_SCALES
        }
        if can_estimate
        else None
    )
    # Georef GeoTIFF závisí na měřítku – tlačítko si přičte podle formuláře.
    est_georef = (
        {str(s): estimate_georef_minutes(area_km2, s, mf) for s in MAP_SCALES}
        if can_estimate
        else None
    )
    note = estimate_note(names) if can_estimate else None
    if can_estimate and calibrated:
        calib_note = i18n.Msg("api.sheets.calibrated", jobs=int(cal["jobs"]))
        note = i18n.Msg("api.sheets.note_join", note=note, extra=calib_note) if note else calib_note
    return {
        **country_info,
        "sheets": sheets,
        "count": len(sheets),
        "max_sheets": MAX_SHEETS,
        "width_km": round(width_km, 2),
        "height_km": round(height_km, 2),
        "area_km2": round(area_km2, 1),
        "max_km": MAX_BBOX_KM,
        "max_area_km2": MAX_BBOX_AREA_KM2,
        # Sprint (1:4000) má přísnější strop; GUI ho porovná s `area_km2` podle zvoleného měřítka.
        "sprint_max_area_km2": sprint_max_area_km2(),
        "sprint_hint": sprint_area_hint(area_km2, width_km, height_km)
        if area_km2 > sprint_max_area_km2() + 1e-6
        else None,
        # Les bez MTBO má strop FOREST_MAX_BBOX_AREA_KM2; GUI hlášku použije, když není zaškrtnuté MTBO.
        "forest_max_area_km2": forest_max_area_km2(),
        "forest_hint": forest_area_hint(area_km2, width_km, height_km)
        if area_km2 > forest_max_area_km2() + 1e-6
        else None,
        "ref_png_estimate_minutes": REF_PNG_ESTIMATE_MINUTES,
        "too_large": sheets_too_big,
        "too_large_reason": "sheets" if sheets_too_big else None,
        "estimate_minutes": est,
        "estimate_minutes_with_refs": est_refs,
        "estimate_georef_minutes": est_georef,
        "estimate_base_minutes": est_base,
        "job_timeout_minutes": JOB_TIMEOUT_MINUTES,
        "estimate_warn_minutes": ESTIMATE_WARN_MINUTES,
        "estimate_map_minutes": est_maps,
        "estimate_note": note,
        "estimate_calibration": cal,
        "label": (
            i18n.Msg("api.sheets.label", names=", ".join(names), count=len(sheets))
            if sheets
            else i18n.Msg("api.sheets.none")
        ),
        "hint": hint,
    }


# Volby jobu, které se ven neposílají (IP klienta, e-mail privátního jobu).
_JOB_OPTIONS_HIDDEN = ("client_ip", "notify_email")


def _public_job(job: dict) -> dict:
    """Kopie jobu pro odpověď API bez osobních údajů v `options`."""
    opts = job.get("options")
    if not isinstance(opts, dict) or not any(k in opts for k in _JOB_OPTIONS_HIDDEN):
        return job
    out = dict(job)
    out["options"] = {k: v for k, v in opts.items() if k not in _JOB_OPTIONS_HIDDEN}
    return out


@app.get("/api/jobs")
def api_list_jobs():
    jobs = [_public_job(j) for j in db.list_jobs(include_private=False)]
    for job in jobs:
        pos = worker.queue_position(job["id"])
        if pos is not None:
            job["queue_position"] = pos
    snap = worker.queue_snapshot()
    # Veřejný listing neobsahuje privátní joby – ale panel „Běžící a fronta“
    # potřebuje vědět, že něco běží (bez jména/logů/artefaktů).
    private_live = None
    current_id = snap.get("current")
    if current_id:
        try:
            current_job = db.get_job(current_id)
        except KeyError:
            current_job = None
        if current_job and db.job_is_private(current_job):
            private_live = {
                "status": current_job.get("status") or "running",
                "started_at": current_job.get("started_at")
                or current_job.get("created_at"),
            }
    return {"jobs": jobs, **snap, "private_live": private_live}


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: str):
    try:
        job = db.get_job(job_id)
    except KeyError:
        raise HTTPException(404, i18n.Msg("api.job_not_found"))
    if db.job_is_expired(job):
        db.delete_job(job_id)
        raise HTTPException(410, i18n.Msg("api.private_expired"))
    # Statistiky buildu (data, časy kroků) jen v detailu – seznam jobů zůstane lehký.
    stats = build_stats.read(JOBS_DIR / job_id)
    if stats:
        job["build_stats"] = stats
    out = _public_job(job)
    # Pozice ve frontě i u privátního jobu (není ve veřejném seznamu, „Moje mapy“ ji čtou odtud).
    pos = worker.queue_position(job_id)
    if pos is not None:
        out["queue_position"] = pos
    return out


@app.get("/api/jobs/{job_id}/template")
def api_job_template(job_id: str):
    """Zadání jobu pro formulář nové mapy (název, výřez, volby) – i u běžícího jobu."""
    try:
        job = db.get_job(job_id)
    except KeyError:
        raise HTTPException(404, i18n.Msg("api.job_not_found"))
    if db.job_is_expired(job):
        db.delete_job(job_id)
        raise HTTPException(410, i18n.Msg("api.private_expired"))
    return job_form_template(job, presets=load_presets())


@app.get("/api/jobs/{job_id}/log")
def api_job_log(job_id: str, after: int = 0):
    try:
        job = db.get_job(job_id)
    except KeyError:
        raise HTTPException(404, i18n.Msg("api.job_not_found"))
    if db.job_is_expired(job):
        db.delete_job(job_id)
        raise HTTPException(410, i18n.Msg("api.private_expired"))
    return {"lines": db.get_logs(job_id, after)}


@app.get("/api/jobs/{job_id}/download/oom")
def api_download_oom(job_id: str, token: str | None = None):
    """Zpětná kompatibilita – stejný balíček jako /download."""
    return api_download(job_id, token=token)


def _output_zip_path(job_id: str) -> Path | None:
    out = JOBS_DIR / job_id / "output"
    for name in ("podkladarna_output.zip", "podkladarna_oom.zip"):
        path = out / name
        if path.is_file():
            return path
    return None


def _load_job_for_artifact(job_id: str, token: str | None) -> dict:
    """Načte job; u privátních vyžaduje platný token a kontroluje expiraci."""
    try:
        job = db.get_job(job_id, reveal_artifacts=True)
    except KeyError:
        raise HTTPException(404, i18n.Msg("api.job_not_found")) from None
    if db.job_is_private(job):
        raw_token = None
        with db.connect() as conn:
            row = conn.execute(
                "SELECT download_token FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row:
                raw_token = row["download_token"]
        job["download_token"] = raw_token
        if db.job_is_expired(job):
            db.delete_job(job_id)
            raise HTTPException(410, i18n.Msg("api.private_link_expired"))
        if not db.token_matches(job, token):
            raise HTTPException(404, i18n.Msg("api.job_not_found"))
    return job


def _require_finished(job: dict) -> None:
    """ZIP ke stažení až po doběhnutí jobu (dřív šel stáhnout rozpracovaný)."""
    if job.get("status") not in db.JOB_FINISHED_STATUSES:
        raise HTTPException(409, i18n.Msg("api.zip_packing"))


@app.get("/api/jobs/{job_id}/download")
def api_download(job_id: str, token: str | None = None):
    job = _load_job_for_artifact(job_id, token)
    _require_finished(job)
    zip_path = _output_zip_path(job_id)
    if not zip_path:
        raise HTTPException(404, i18n.Msg("api.output_not_ready"))
    filename = db.zip_download_filename(job_id, job["name"])
    _note_download(job_id, zip_path)
    return FileResponse(zip_path, filename=filename)


@app.get("/api/jobs/{job_id}/download/georef-previews")
def api_download_georef_previews(job_id: str, token: str | None = None):
    """Malý ZIP jen s georeferencovanými náhledy (GeoTIFF; PNG+PGW jen bez GDAL)."""
    from app.pipeline.oom_preview import (
        GEOREF_PREVIEWS_ZIP_NAME,
        build_georef_previews_zip,
    )

    job = _load_job_for_artifact(job_id, token)
    _require_finished(job)
    out = JOBS_DIR / job_id / "output"
    zip_path = out / GEOREF_PREVIEWS_ZIP_NAME
    if not zip_path.is_file():
        # Lazy: sestav z preview/, pokud job doběhl před zavedením malého ZIPu.
        built = build_georef_previews_zip(out)
        if built is None or not built.is_file():
            raise HTTPException(404, i18n.Msg("api.georef_previews_missing"))
        zip_path = built
    stem = db.safe_zip_stem(job["name"])
    filename = f"podkladarna-{stem}-georef-nahledy.zip"
    return FileResponse(zip_path, filename=filename)


@app.get("/api/jobs/{job_id}/preview.png")
def api_preview(job_id: str, token: str | None = None):
    from app.pipeline.preview import resolve_preview_png

    _load_job_for_artifact(job_id, token)
    job_dir = JOBS_DIR / job_id
    png = resolve_preview_png(job_dir / "output", job_dir / "work")
    if png is None:
        raise HTTPException(404, i18n.Msg("api.preview_missing"))
    return FileResponse(png)


@app.get("/d/{token}")
def download_by_token(token: str):
    """Privátní stažení ZIP podle neprůhledného tokenu (odkaz z e-mailu)."""
    try:
        job = db.get_job_by_download_token(token)
    except KeyError:
        raise HTTPException(404, i18n.Msg("api.link_invalid")) from None
    if db.job_is_expired(job):
        db.delete_job(job["id"])
        raise HTTPException(410, i18n.Msg("api.private_link_expired"))
    _require_finished(job)
    zip_path = _output_zip_path(job["id"])
    if not zip_path:
        raise HTTPException(404, i18n.Msg("api.output_not_ready"))
    filename = db.zip_download_filename(job["id"], job["name"])
    _note_download(job["id"], zip_path)
    return FileResponse(zip_path, filename=filename)


@app.get("/api/health")
def api_health():
    import shutil

    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(JOBS_DIR)
    tools = tool_status()
    mail_ok = bool((SMTP_HOST or "").strip() and (SMTP_FROM or "").strip())
    return {
        "ok": True,
        "version": APP_VERSION,
        "data_dir": str(JOBS_DIR.parent),
        "downloads_dir": str(DOWNLOADS_DIR),
        "disk_free_gb": round(usage.free / 1e9, 2),
        "busy": worker.is_busy(),
        # Čeká nasazení nové verze (update-nas-ab.sh): nové joby jen do fronty.
        "draining": worker.is_draining(),
        "channel": app_settings.INSTANCE_CHANNEL,
        "instance": app_settings.INSTANCE_NAME or None,
        "tools": tools,
        "pipeline_ready": all(tools.values()),
        # Bez hesel: jen zda feedback / privátní mail vůbec zkusí SMTP.
        "mail_configured": mail_ok,
    }


@app.post("/api/feedback")
async def api_feedback(request: Request):
    """Textová zpětná vazba → e-mail Petrovi.

    Nezávislé na frontě generování (žádný worker.is_busy / check_create_job).
    """
    content_type = (request.headers.get("content-type") or "").lower()
    honeypot = ""
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(400, i18n.Msg("api.invalid_json")) from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, i18n.Msg("api.invalid_json"))
        contact = str(payload.get("contact") or "").strip()
        comment = str(payload.get("comment") or "").strip()
        job_id = str(payload.get("job_id") or "").strip()
        honeypot = str(
            payload.get("website") or payload.get("company") or ""
        ).strip()
    else:
        try:
            form = await request.form()
        except Exception as exc:
            raise HTTPException(400, i18n.Msg("api.form_unreadable", error=str(exc))) from exc
        contact = _form_str(form, "contact").strip()
        comment = _form_str(form, "comment").strip()
        job_id = _form_str(form, "job_id").strip()
        honeypot = (
            _form_str(form, "website").strip()
            or _form_str(form, "company").strip()
        )

    # Honeypot: boti vyplní skryté pole — tiše OK, nic neodesílat.
    if honeypot:
        logger.info("Feedback honeypot hit from %s", client_ip(request))
        return {"ok": True}

    if not contact:
        raise HTTPException(400, i18n.Msg("api.feedback.contact_missing"))
    if not comment:
        raise HTTPException(400, i18n.Msg("api.feedback.comment_missing"))
    if not job_id:
        raise HTTPException(400, i18n.Msg("api.feedback.job_missing"))
    if len(contact) > MAX_CONTACT_LEN:
        raise HTTPException(400, i18n.Msg("api.feedback.contact_long", max=MAX_CONTACT_LEN))
    if len(comment) > MAX_COMMENT_LEN:
        raise HTTPException(400, i18n.Msg("api.feedback.comment_long", max=MAX_COMMENT_LEN))
    if len(job_id) > MAX_JOB_ID_LEN:
        raise HTTPException(400, i18n.Msg("api.feedback.job_invalid"))
    if not all(c.isalnum() or c in "-_" for c in job_id):
        raise HTTPException(400, i18n.Msg("api.feedback.job_invalid"))

    # Job může běžet, čekat i být hotový — odeslat jde vždy (Petr 2026-10-05).
    job_name: str | None = None
    job_status: str | None = None
    try:
        job = db.get_job(job_id)
        job_name = str(job.get("name") or "").strip() or None
        job_status = str(job.get("status") or "").strip() or None
    except KeyError:
        raise HTTPException(404, i18n.Msg("api.job_not_found_dot")) from None

    ip = client_ip(request)
    limit_msg = check_feedback_rate(ip)
    if limit_msg:
        raise HTTPException(429, limit_msg)

    try:
        send_feedback(
            contact=contact,
            comment=comment,
            job_id=job_id,
            client_ip=ip,
            job_name=job_name,
            job_status=job_status,
        )
    except MailError as exc:
        # 502, ne 503 — 503 si FE/klienti spojují s plnou frontou generování.
        logger.warning("Feedback mail failed: %s", exc)
        # Detail obsahuje MailError (např. chybějící SMTP_HOST) – FE ukáže detail.
        raise HTTPException(
            502,
            i18n.Msg("api.feedback.mail_failed", error=str(exc)),
        ) from exc

    return {"ok": True}


API_KEY_HEADER = "x-api-key"


def _api_client_authenticated(request: Request) -> bool:
    """Hlavička X-Api-Key: chybí → False; platná → True; neplatná / klíč nenastaven → 401.

    Klíč se porovnává v konstantním čase a nikdy se neloguje ani nevrací.
    """
    got = request.headers.get(API_KEY_HEADER)
    if got is None:
        return False
    expected = app_settings.API_CLIENT_KEY
    if not expected or not hmac.compare_digest(
        got.strip().encode("utf-8"), expected.encode("utf-8")
    ):
        logger.warning("POST /api/jobs – neplatný API klíč z %s", client_ip(request))
        raise HTTPException(401, i18n.Msg("api.key_invalid"))
    return True


@app.post("/api/jobs")
async def api_create_job(request: Request):
    logger.info("POST /api/jobs – cteni multipart form")
    try:
        form = await request.form()
    except Exception as exc:
        logger.exception("Multipart form parse failed")
        raise HTTPException(400, i18n.Msg("api.form_unreadable", error=str(exc))) from exc

    api_client = _api_client_authenticated(request)

    name = _form_str(form, "name").strip()
    if not name:
        raise HTTPException(400, i18n.Msg("api.job.name_missing"))

    presets = load_presets()
    map_scale_raw = _form_str(form, "map_scale").strip()
    contour_raw = _form_str(form, "contour_interval").strip()
    preset_id = _form_str(form, "preset_id").strip()
    try:
        if map_scale_raw:
            resolved = resolve_omap_job(
                map_scale_raw,
                contour_raw or None,
                presets=presets,
            )
        elif preset_id:
            # Zpětná kompatibilita starých klientů / iterace z jobu s preset_id.
            resolved = resolve_omap_job(
                None,
                contour_raw or None,
                presets=presets,
                preset_id_fallback=preset_id,
            )
        else:
            raise HTTPException(400, i18n.Msg("api.job.scale_missing"))
    except ValueError as exc:
        raise HTTPException(400, _exc_detail(exc)) from exc
    preset_id = resolved["preset_id"]
    if preset_id not in presets:
        raise HTTPException(400, i18n.Msg("api.job.preset_unknown", preset=preset_id))

    # Typy map (les / MTBO) jen u lesních měřítek; bez pole výchozí podle měřítka (API klienti).
    # GUI posílá skryté `map_types_form`, protože nezaškrtnutý checkbox se neodešle vůbec.
    map_types: list[str] | None = None
    if int(resolved["map_scale"]) != 4000:
        map_types_raw = _form_list(form, "map_types")
        if "map_types" not in form and "map_types_form" not in form:
            map_types = default_map_types(resolved["map_scale"])
        else:
            map_types = parse_map_types(map_types_raw)
            if not map_types:
                raise HTTPException(
                    400,
                    i18n.Msg("api.job.map_type_missing"),
                )

    if (worker.is_busy() or worker.is_draining()) and not worker.can_accept_job():
        raise HTTPException(
            503,
            i18n.Msg("api.job.queue_full_wait", max=MAX_QUEUE_SIZE),
        )

    bbox_raw = _form_str(form, "bbox").strip()
    if not bbox_raw:
        raise HTTPException(400, i18n.Msg("api.job.bbox_missing"))
    try:
        bbox = parse_bbox(bbox_raw)
    except FetchError as exc:
        raise HTTPException(400, _exc_detail(exc)) from exc
    job_country, country_share = _check_country(bbox, request, _form_str(form, "country").strip())
    width_km, height_km = bbox_size_km(*bbox)
    if bbox_exceeds_limit(*bbox):
        area = bbox_area_km2(*bbox)
        raise HTTPException(
            400,
            i18n.Msg(
                "api.job.bbox_too_big",
                width=float(width_km),
                height=float(height_km),
                area=float(area),
                max=float(MAX_BBOX_AREA_KM2),
            ),
        )
    # Sprint (1:4000) jen pro menší území – kontrola na serveru, GUI jde obejít.
    is_sprint = int(resolved["map_scale"]) <= 5000 or str(preset_id).startswith("sprint")
    if is_sprint:
        area = bbox_area_km2(*bbox)
        if area > sprint_max_area_km2() + 1e-6:
            raise HTTPException(400, sprint_area_hint(area, width_km, height_km))
    elif "mtbo" not in (map_types or []) and not str(preset_id).startswith("mtbo"):
        # Les bez MTBO: menší strop (MTBO dál do MAX_BBOX_AREA_KM2).
        area = bbox_area_km2(*bbox)
        if area > forest_max_area_km2() + 1e-6:
            raise HTTPException(400, forest_area_hint(area, width_km, height_km))
    # Listy SM5: preferuj výsledek z /api/sheets (UI už ověřilo) – ušetří další ArcGIS call.
    sheets: list[dict] = []
    sheets_raw = _form_str(form, "sm5_sheets").strip()
    if sheets_raw:
        seen_noms: set[str] = set()
        for part in sheets_raw.replace(";", ",").split(","):
            mapnom = part.strip().upper()
            if not mapnom or mapnom in seen_noms:
                continue
            if not all(c.isalnum() or c in "-_" for c in mapnom):
                continue
            seen_noms.add(mapnom)
            sheets.append({"mapnom": mapnom, "name": mapnom})
    if not sheets:
        try:
            sheets = query_sm5_sheets(*bbox)
        except FetchError as exc:
            raise HTTPException(400, _exc_detail(exc)) from exc
    if not sheets:
        raise HTTPException(400, i18n.Msg("api.job.no_sheet"))
    if len(sheets) > MAX_SHEETS:
        names = ", ".join(s["mapnom"] for s in sheets)
        raise HTTPException(
            400,
            i18n.Msg("api.job.too_many_sheets", count=len(sheets), max=MAX_SHEETS, names=names),
        )

    dmr_uploads = _upload_files(form, "dmr_files")
    dmp_uploads = _upload_files(form, "dmp_files")
    if dmr_uploads or dmp_uploads or _upload_files(form, "zabaged_file"):
        raise HTTPException(
            400,
            i18n.Msg("api.job.upload_unsupported"),
        )

    remote_ip = client_ip(request)
    limit_msg = check_create_api_job() if api_client else check_create_job(remote_ip)
    if limit_msg:
        raise HTTPException(429, limit_msg)

    options = {
        **DEFAULT_OPTIONS,
        "source_mode": "map",
        "bbox_wgs84": list(bbox),
        "sm5_sheets": [s["mapnom"] for s in sheets],
        "client_ip": remote_ip,
        "map_scale": resolved["map_scale"],
        "scalefactor": resolved["scalefactor"],
        "contour_interval": resolved["contour_interval"],
        "indexcontours": resolved["indexcontours"],
    }
    cliff_raw = _form_str(form, "kp_cliff_symbol").strip().lower()
    if cliff_raw == "rock_face":
        # Legacy: force linear 201 už nedává smysl (skály = plochy 201.2/206).
        cliff_raw = "auto"
    if cliff_raw in {"auto", "earth_bank", "symbol_206", "off"}:
        options["kp_cliff_symbol"] = cliff_raw
    # Pevný default (bez volby ve formuláři): kupky a ďolíky z DEM jen u lesního OB, ne ve sprintu a MTBO.
    options["include_knolls"] = not (preset_id.startswith("sprint") or preset_id.startswith("mtbo"))
    vege_raw = _form_str(form, "kp_vege_height").strip()
    if vege_raw:
        try:
            vh = float(vege_raw.replace(",", "."))
        except ValueError:
            vh = None
        if vh is not None and any(abs(vh - c) < 1e-6 for c in KP_VEGE_HEIGHT_CHOICES):
            options["kp_vege_height"] = resolve_vege_height({"kp_vege_height": vh})
    sens_raw = _form_str(form, "kp_cliff_sensitivity").strip().lower()
    if sens_raw in KP_CLIFF_SENSITIVITY:
        options["kp_cliff_sensitivity"] = resolve_cliff_sensitivity(
            {"kp_cliff_sensitivity": sens_raw}
        )
    def _opt_bool(key: str) -> bool:
        return _form_str(form, key).strip().lower() in {"1", "true", "yes", "on"}

    # Volba „Objekty OSM“ (3.19.0): auto = ve sprintu základní sada (lavičky, rozcestníky, pomníky, stromy), 1 = vše,
    # none = žádné. Starší klienti API mohou poslat dílčí volby (lavičky / lampy / herní prvky / kp_osm_furniture) –
    # kterákoli zapne vše. kp_osm_priority („OSM detaily“) se ignoruje: ploty a zdi se načítají vždy.
    _osm_mode = resolve_osm_objects_mode(
        {
            key: _form_str(form, key).strip() or None
            for key in ("kp_osm_objects", *OSM_OBJECTS_LEGACY_KEYS)
        }
    )
    options["kp_osm_objects"] = {OSM_OBJECTS_ALL: True, OSM_OBJECTS_NONE: OSM_OBJECTS_NONE}.get(_osm_mode, OSM_OBJECTS_AUTO)
    # Pevný default (bez volby ve formuláři): footway jako zpevněný chodník jen ve sprintu.
    options["kp_osm_footway_as_sidewalk"] = default_footway_as_sidewalk(
        options.get("map_scale"),
        preset_id,
    )
    if map_types:
        options["map_types"] = map_types
    formlines_raw = _form_str(form, "contour_formlines").strip().lower()
    if formlines_raw in FORMLINE_MODES:
        options["contour_formlines"] = formlines_raw
    contour_smooth_raw = _form_str(form, "contour_smooth").strip().lower()
    if contour_smooth_raw.isdigit() and int(contour_smooth_raw) in CONTOUR_SMOOTH_LEVELS:
        options["contour_smooth"] = contour_smooth_raw
    veg_smooth_raw = _form_str(form, "veg_smooth").strip().lower()
    if veg_smooth_raw.isdigit() and int(veg_smooth_raw) in VEG_SMOOTH_LEVELS:
        options["veg_smooth"] = veg_smooth_raw
    # Jedna volba `dtm` v GUI: 1 (DTM všude, výchozí od 3.18.0) | auto (podle presetu: jen sprint z DTM)
    # | 0 (jen OSM + ZABAGED). Starší klienti API mohou dál posílat `dtm_sprint` / `dtm_forest` zvlášť.
    dtm_all = _form_str(form, "dtm").strip().lower()
    if dtm_all in {"1", "0", "true", "false", "on", "off"}:
        options["dtm_sprint"] = options["dtm_forest"] = dtm_all in {"1", "true", "on"}
    elif dtm_all == "auto":
        options["dtm_sprint"], options["dtm_forest"] = True, False
    for dtm_key in ("dtm_sprint", "dtm_forest"):
        dtm_raw = _form_str(form, dtm_key).strip().lower()
        if dtm_raw in {"1", "0", "true", "false", "on", "off"}:
            options[dtm_key] = dtm_raw in {"1", "true", "on"}
    options["sprint_residual_paved"] = _opt_bool("sprint_residual_paved")
    residual_size_raw = _form_str(form, "sprint_residual_size").strip().lower()
    if residual_size_raw in {"small", "medium", "large"}:
        options["sprint_residual_size"] = residual_size_raw
    # Ostatní plocha v sídlech: volba z GUI i formuláře odebrána (Petr 2026-10-31),
    # pipeline bere výchozí "small" (resolve_ostatni_plocha); parametr zůstává pro návrat.
    options["ostatni_plocha_as_403"] = _opt_bool("ostatni_plocha_as_403")
    mode_raw = _form_str(form, "output_mode").strip().lower()
    if mode_raw in {"png", "png_only"}:
        options["output_zip"] = False
    elif mode_raw in {"png_zip", "zip", "both", ""}:
        options["output_zip"] = True
    elif _form_str(form, "output_zip").strip():
        options["output_zip"] = _opt_bool("output_zip")
    # Georef GeoTIFF do ZIPu – default off (formulářové combo Formát už není).
    options["output_georef"] = _opt_bool("output_georef")
    # Podklady: 1 = stáhnout šablony předem, jinak (výchozí, i API bez volby) online šablony GDAL WMS (Petr 2026-11-08).
    options["output_references"] = _opt_bool("output_references")
    # Sentinel-2 jde vždy (online šablonu neumí – vybírá snímek bez mraků podle data);
    # starší klienti API mohou poslat output_sentinel=0.
    if _form_str(form, "output_sentinel").strip():
        options["output_sentinel"] = _opt_bool("output_sentinel")
    else:
        options["output_sentinel"] = True
    options["force_refresh"] = _opt_bool("force_refresh")
    # Jazyk a doména zadání: e-maily jobu (jiný proces) píšou v tomto jazyce a s odkazy na tuto doménu
    # (jen ze seznamu SITES, nikdy přímo z hlavičky Host).
    options["lang"] = i18n.resolve_lang(request)[0]
    _site = i18n.site_for_request(request)
    if _site is not None:
        options["site"] = _site.host
    # Země výřezu (jeden CRS a sada zdrojů); přesah za hranici se kreslí jen z OSM.
    options["country"] = job_country.code
    if countries.outside_share(country_share) > 0:
        options["outside_share"] = countries.outside_share(country_share)
    # Privátní režim: mimo veřejný seznam + e-mail s tokenizovaným odkazem.
    # API klient (platný X-Api-Key) má job vždy privátní, e-mail nepotřebuje –
    # `download_token` dostane v odpovědi.
    if api_client:
        options["private"] = True
        options["api_client"] = True
        notify_email = _form_str(form, "notify_email").strip()
        if notify_email and "@" in notify_email and "." in notify_email.split("@")[-1]:
            options["notify_email"] = notify_email
    elif _opt_bool("private"):
        notify_email = _form_str(form, "notify_email").strip()
        if not notify_email or "@" not in notify_email or "." not in notify_email.split("@")[-1]:
            raise HTTPException(
                400,
                i18n.Msg("api.job.private_email"),
            )
        options["private"] = True
        options["notify_email"] = notify_email
    reuse_id = _form_str(form, "reuse_job_id").strip()
    if reuse_id:
        try:
            prev = db.get_job(reuse_id)
        except KeyError:
            prev = None
        prev_bbox = (prev or {}).get("options", {}).get("bbox_wgs84")
        if prev and prev.get("has_reusable_lidar") and db.bbox_close(prev_bbox or [], bbox):
            options["reused_from"] = reuse_id
    dup = db.find_duplicate_active_job(options)
    if dup:
        logger.info(
            "Duplicate job skipped – returning existing %s (status=%s)",
            dup["id"],
            dup.get("status"),
        )
        msg = (
            "Nový job se nezaložil – stejný výřez a volby už "
            f"{'běží' if dup.get('status') == 'running' else 'čekají'} "
            f"(job {dup['id'][:8]}…, „{dup.get('name') or 'bez názvu'}“)."
        )
        db.append_log(dup["id"], msg)
        out = _public_job(dict(dup))
        # Stejný job založený týmž API klíčem – klient potřebuje token ke stažení.
        if api_client and (dup.get("options") or {}).get("api_client"):
            token = db.get_download_token(dup["id"])
            if token:
                out["download_token"] = token
        out["duplicate_skipped"] = True
        out["duplicate_message"] = msg
        return out
    job = db.create_job(name, preset_id, options)
    job_id = job["id"]

    def log(msg: str) -> None:
        db.append_log(job_id, msg)

    # Kopie LAZ z předchozího jobu běží až ve workeru (run_job_pipeline),
    # ať odpověď „job založen“ nepřijde až po dlouhém copy.
    if options.get("reused_from"):
        log("Data LiDAR se převezmou z předchozí mapy.")

    def _yn(key: str, default: bool = False) -> str:
        return "ano" if options.get(key, default) else "ne"

    log(
        f"Přijato: preset {preset_id} · 1:{resolved['map_scale']} · "
        f"ekvidistance {resolved['contour_interval']} m · "
        f"listy SM5 {', '.join(options['sm5_sheets'])}"
        + (
            " · mapy les + MTBO"
            if map_types and len(map_types) > 1
            else f" · mapa {'MTBO' if map_types[0] == 'mtbo' else 'les'}"
            if map_types
            else ""
        )
    )
    log(
        f"  Terén: vegetace {options.get('kp_vege_height', 2.0)} m · "
        f"tvar vegetace {resolve_veg_smooth(options)} · "
        f"vrstevnice {'adaptivní' if resolve_contour_smooth(options) else 'méně vyhlazení'} · "
        f"kresba sprint {'DTM' if resolve_use_dtm(options, 'sprint') else 'OSM+ZABAGED'}/"
        f"les+MTBO {'DTM' if resolve_use_dtm(options, 'forest') else 'OSM+ZABAGED'} · "
        f"srázy {options.get('kp_cliff_sensitivity', 'low')}/"
        f"{options.get('kp_cliff_symbol', 'auto')} · "
        f"pomocné vrstevnice {_FORMLINE_LABEL[resolve_formline_mode(options)]}"
    )
    log(
        f"  OSM: objekty {_OSM_OBJECTS_LABEL[resolve_osm_objects_mode(options)]} · ploty a zdi vždy · "
        f"footway jako chodník {_yn('kp_osm_footway_as_sidewalk')}"
    )
    log(
        f"  Sídla: residential 501 {_yn('sprint_residual_paved')}"
        f"/{options.get('sprint_residual_size', 'small')} · "
        f"ostatní plocha {options.get('ostatni_plocha', 'small')}"
        f"{'/403' if options.get('ostatni_plocha_as_403') else ''}"
    )
    log(
        f"  Výstup: {'ZIP' if options.get('output_zip', True) else 'jen PNG'} · "
        f"podklady {'stažené' if options.get('output_references') else 'online'} "
        f"(Sentinel-2 {_yn('output_sentinel')}) · "
        f"georef GeoTIFF {_yn('output_georef')} · "
        f"vynucené obnovení {_yn('force_refresh')} · "
        f"privátní {_yn('private')}"
        + (" · API klient" if api_client else "")
    )
    outside = countries.outside_note(job_country, country_share)
    if outside:
        log(f"⚠ {outside}")
    if options.get("private") and not options.get("notify_email"):
        hours = PRIVATE_JOB_RETENTION_HOURS if PRIVATE_JOB_RETENTION_HOURS > 0 else 48
        log(f"Privátní job API klienta – odkaz s tokenem platí {hours} h.")
    elif options.get("private"):
        hours = PRIVATE_JOB_RETENTION_HOURS if PRIVATE_JOB_RETENTION_HOURS > 0 else 48
        log(
            f"Privátní režim – po dokončení přijde e-mail na {options.get('notify_email')} "
            f"(odkaz platí {hours} h)."
        )

    try:
        worker.enqueue(job_id)
        pos = worker.queue_position(job_id)
        if pos and pos > 0:
            log(f"Job ve fronte – pozice {pos}.")

        logger.info("Job %s enqueued (queue_pos=%s)", job_id, pos)
        out = db.get_job(job_id)
        if api_client:
            token = db.get_download_token(job_id)
            if token:
                out["download_token"] = token
        return out
    except Exception as exc:
        tb = traceback.format_exc()
        log(f"CHYBA pri vytvareni jobu: {exc}")
        log(tb)
        db.update_job(job_id, status="failed", phase="upload", error=str(exc))
        logger.exception("Job %s upload failed", job_id)
        raise HTTPException(500, i18n.Msg("api.job.create_failed", error=str(exc))) from exc


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    png = STATIC_DIR / "logo.png"
    if png.is_file():
        return FileResponse(png, media_type="image/png")
    svg = STATIC_DIR / "logo.svg"
    if svg.is_file():
        return FileResponse(svg, media_type="image/svg+xml")
    raise HTTPException(404, i18n.Msg("api.logo_missing"))


@app.get("/tiles/{z}/{x}/{y}.png", include_in_schema=False)
def map_tile(z: int, x: int, y: int):
    try:
        path = fetch_tile(z, x, y)
    except TileError as exc:
        raise HTTPException(400, str(exc)) from exc
    return FileResponse(
        path,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=86400",
            "CDN-Cache-Control": "public, max-age=604800",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/api/jobs/{job_id}/start")
def api_start_job(job_id: str):
    try:
        job = db.get_job(job_id)
    except KeyError:
        raise HTTPException(404, i18n.Msg("api.job_not_found"))
    if job["status"] == "running":
        return _public_job(job)
    if (worker.is_busy() or worker.is_draining()) and not worker.can_accept_job():
        raise HTTPException(503, i18n.Msg("api.job.queue_full", max=MAX_QUEUE_SIZE))
    worker.enqueue(job_id)
    return _public_job(db.get_job(job_id))


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
