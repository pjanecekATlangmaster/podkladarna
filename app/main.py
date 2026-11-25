from __future__ import annotations

import hmac
import json
import logging
from html import escape as html_escape
import mimetypes
import re
import threading
import traceback
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import area_probe, countries, db, donations, failed_jobs, i18n, log_i18n, paid_flow, prefetch, samples_refresh, settings as app_settings, source_events, source_fixes, source_gate, source_status, user_sources, worker
from app.cleanup import maybe_purge_old_jobs, prune_caches, purge_old_jobs, start_cleanup_scheduler
from app.client_ip import client_ip
from app.feedback import (
    MAX_COMMENT_LEN,
    MAX_CONTACT_LEN,
    MAX_JOB_ID_LEN,
    MAX_SOURCE_AREA_LEN,
    MAX_SOURCE_LICENSE_LEN,
    check_feedback_rate,
    is_valid_source_url,
    send_feedback,
    send_source_suggestion,
)
from app.guide_text import strip_github_links, web_about_html
from app.job_template import job_form_template
from app.mail import MailError
from app.rate_limit import check_banned_email, check_create_api_job, check_create_job, check_private_email
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
    FORMLINE_MODE_AUTO,
    FORMLINE_MODES,
    KP_CLIFF_SENSITIVITY,
    KP_VEGE_HEIGHT_CHOICES,
    CONTOUR_SMOOTH_LEVELS,
    VEG_SMOOTH_LEVELS,
    CLEAN_OPEN_MODES,
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
from app.tiles import TileError, allow_fetch, fetch_tile, is_cached, validate_tile
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
    # Úklid cache na pozadí (průchod cache LiDARu trvá, start webu nesmí čekat) a rezerva místa na disku.
    threading.Thread(target=prune_caches, name="cache-cleanup", daemon=True).start()
    start_cleanup_scheduler(CLEANUP_INTERVAL_HOURS)
    samples_refresh.start_scheduler()
    source_fixes.start_scheduler()
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
        lang = i18n.resolve_lang(request)[0]
        shown = detail
        if intl_site(request) and f"{detail.key}_intl" in i18n.catalog(lang):
            # Mezinárodní doména: znění bez odkazů, které tam nepatří (GitHub; Petr 2026-11-19); kód zůstává.
            shown = i18n.Msg(f"{detail.key}_intl", **detail.params)
        content = {"detail": shown.render(lang), "code": detail.key}
        return JSONResponse(status_code=exc.status_code, content=content)
    return JSONResponse(status_code=exc.status_code, content={"detail": detail})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error")
    lang = i18n.resolve_lang(request)[0]
    if lang != i18n.SOURCE_LANG:
        # Mimo češtinu obecná hláška (text výjimky bývá česky a technický; je v logu serveru).
        msg = i18n.Msg("api.internal_error")
        return JSONResponse(status_code=500, content={"detail": msg.render(lang), "code": msg.key})
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
    # Mezinárodní doména: název služby a texty ``_intl`` i v češtině (i18n.set_request_site); česká doména beze změny.
    token = i18n.set_request_site(i18n.site_for_request(request))
    try:
        response = await call_next(request)
    finally:
        i18n.reset_request_site(token)
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
    if intl_site(request):
        # Mezinárodní doména: vlastní překlad stránky (``web/intl/<jazyk>/``, česky jako Omapic), jinak jako dřív.
        path = WEB_DIR / "intl" / lang / name
        if path.is_file():
            return path.read_text(encoding="utf-8")
    if lang != i18n.SOURCE_LANG:
        for code in (lang, i18n.BASE_LANG):
            path = WEB_DIR / code / name
            if path.is_file():
                return path.read_text(encoding="utf-8")
    return (WEB_DIR / name).read_text(encoding="utf-8")


GITHUB_REPO_URL = "https://github.com/pjanecekATlangmaster/podkladarna"


def source_links_html(lang: str, *, intl: bool = False) -> str:
    """Odkazy na zdrojový kód a Issues v patičce jen v české verzi na české doméně (mezinárodní verze GitHub
    neukazuje nikde, ani česky; Petr 2026-11-19)."""
    if intl or lang != i18n.SOURCE_LANG:
        return ""
    return (
        f'<a href="{GITHUB_REPO_URL}" target="_blank" rel="noopener">{{{{t:web.footer.source_code}}}}</a>\n'
        f'      · <a href="{GITHUB_REPO_URL}/issues" target="_blank" rel="noopener">{{{{t:web.footer.issues}}}}</a>\n'
        "      · "
    )


SAMPLE_KINDS = ("sprint", "les", "mtbo")
# Mezinárodní stránka (Petr 2026-11-17): ukázky z národních podkladů ciziny + česká mapa s DTM jako „reference: Česko“.
SAMPLE_KINDS_EN = SAMPLE_KINDS + ("reference",)
_SAMPLE_NAME_RE = re.compile(r"^(sprint|les|mtbo)\.zip$|^(sprint|les|mtbo|reference)-en\.zip$")
SAMPLES_MANIFEST = "samples.json"


def _sample_file(kind: str, lang: str) -> Path:
    """Ukázkový ZIP v ``SAMPLES_DIR``: česky ``<druh>.zip``, jinak anglický ``<druh>-en.zip``."""
    suffix = "" if lang == i18n.SOURCE_LANG else "-en"
    return Path(app_settings.SAMPLES_DIR) / f"{kind}{suffix}.zip"


def _sample_labels() -> dict[str, str]:
    """Volitelné popisky ukázek ``samples.json`` vedle ZIPů: ``{"sprint-en.zip": "sprint – Amsterdam (BGT, AHN)"}``.

    Popisek má pravdivě říct území a data, ze kterých ukázka vznikla; bez souboru platí popisky z katalogu."""
    try:
        data = json.loads((Path(app_settings.SAMPLES_DIR) / SAMPLES_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str) and v.strip()}


def samples_html(lang: str) -> str:
    """Odkazy na ukázkové ZIPy, které na serveru jsou (bez souborů nic)."""
    labels = _sample_labels()
    links = []
    for kind in SAMPLE_KINDS if lang == i18n.SOURCE_LANG else SAMPLE_KINDS_EN:
        path = _sample_file(kind, lang)
        if not path.is_file():
            continue
        size = i18n.fmt_num(path.stat().st_size / 1_000_000, lang)
        custom = labels.get(path.name)
        # Vlastní popisek escapovaně a bez složených závorek (šablona je pak prochází jako {{t:…}}).
        label = (
            html_escape(custom.strip()).replace("{", "").replace("}", "")
            if custom
            else f"{{{{t:web.page.sample_{kind}}}}}"
        )
        links.append(f'<a href="/samples/{path.name}" download>{label}</a> ({size} MB)')
    if not links:
        return ""
    return '<p class="about-samples">{{t:web.page.samples}} ' + " · ".join(links) + "</p>"


@app.get("/samples/{name}")
def download_sample(name: str):
    """Ukázkový ZIP (jen pevná jména ze seznamu, žádné cesty)."""
    m = _SAMPLE_NAME_RE.match(name)
    path = Path(app_settings.SAMPLES_DIR) / name
    if not m or not path.is_file():
        raise HTTPException(404, i18n.Msg("api.output_not_ready"))
    kind, prefix = (m.group(2), "omapic-sample") if m.group(2) else (m.group(1), "podkladarna-ukazka")
    return FileResponse(path, filename=f"{prefix}-{kind}.zip", media_type="application/zip")


# Patička mezinárodní placené domény (Petr 2026-11-16): bez daru (dar jen na bezplatné doméně), bez „Z jedné
# rodiny“ (české projekty) a bez řádku „Data: ČÚZK …“ – zdroje všech zemí jsou na /licence.
_FREE_ONLY_FOOTER = (
    re.compile(r'\n\s*· <a href="/podpora">\{\{t:web\.footer\.support\}\}</a>'),
    re.compile(r'\n\s*<p class="footer-family">.*?</p>', re.S),
    re.compile(r'\{\{t:web\.footer\.data\}\}.*?CC BY 4\.0</a>\)\s*·\s*', re.S),
    re.compile(r'<p class="support-note">\{\{h:web\.page\.prisel_podklad_vhod_podkladarna_je_zdarma\}\}</p>'),
)


def paid_site_footer(html: str, *, keep_donation: bool = False) -> str:
    """Odstraní z indexu dar, „Z jedné rodiny“ a české zdroje dat (jen pro placenou doménu).

    ``keep_donation``: doména s dobrovolným darem přes PayPal (``app.donations``, prodej neběží) si nechá
    odkaz na podporu v patičce a větu pod hotovou mapou."""
    for i, pattern in enumerate(_FREE_ONLY_FOOTER):
        if keep_donation and i in (0, 3):
            continue
        html = pattern.sub("", html, count=1)
    return html


_INTL_PAGE_KEYS = (
    "web.page.podkladarna_sama_sklada_podklady_pro_orientacni",
    "web.page.pro_ocad_v_zipu_georeferencovany_v",
)


def intl_site(request: Request) -> bool:
    """Doména nabízí i jiné země než Česko (``SITES``); bez ``SITES`` česká verze."""
    return i18n.is_intl_site(i18n.site_for_request(request))


def user_sources_form_html(request: Request, lang: str) -> str:
    """Nahrání vlastních výšek zadavatele (``app.user_sources``) nad volbou Soukromě: jen Omapic
    (doména i pro jiné země), se zapnutým ``USER_SOURCES`` a mimo placenou objednávku. Česká doména beze změny;
    web pole ukazuje jen u výřezu mimo Česko."""
    if not user_sources.enabled() or not intl_site(request) or paid_flow.active_for(request):
        return ""
    mb = int(user_sources.MAX_TOTAL_MB)
    limits = html_escape(i18n.t("web.page.user_sources.limits", lang, max=mb))
    return f"""<details class="advanced-options user-sources" id="user-sources" data-max-mb="{mb}" hidden>
          <summary>{{{{h:web.page.user_sources.title}}}}</summary>
          <p class="hint">{{{{h:web.page.user_sources.intro}}}}</p>
          <label>{{{{h:web.page.user_sources.dtm}}}}
            <input type="file" name="user_dtm" id="user_dtm" accept=".tif,.tiff,image/tiff" />
          </label>
          <label>{{{{h:web.page.user_sources.dsm}}}}
            <input type="file" name="user_dsm" id="user_dsm" accept=".tif,.tiff,image/tiff" />
          </label>
          <p class="hint">{limits} <a href="/own-data" target="_blank" rel="noopener">{{{{h:web.page.user_sources.guide}}}}</a></p>
          <p class="hint user-sources-cmd-wrap" id="user-sources-cmd-wrap" hidden>{{{{h:web.page.user_sources.cmd}}}}
            <code class="user-sources-cmd" id="user-sources-cmd"></code></p>
          <label class="checkbox-row">
            <input type="checkbox" name="user_sources_consent" id="user_sources_consent" value="1" />
            {{{{h:web.page.user_sources.consent}}}}
          </label>
          <p class="hint">{{{{h:web.page.user_sources.privacy}}}}</p>
          <div class="hint user-sources-status" id="user-sources-status" aria-live="polite"></div>
        </details>
"""


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
    intl = intl_site(request)
    html = html.replace("<!-- PODKLADARNA_ABOUT -->", web_about_html(lang, intl=intl))
    if intl:
        # Doména i pro jiné země: texty o souřadnicích bez S-JTSK (česká doména beze změny).
        for key in _INTL_PAGE_KEYS:
            html = html.replace(f"{{{{h:{key}}}}}", f"{{{{h:{key}_intl}}}}")
    html = html.replace("<!-- PODKLADARNA_SOURCE_LINKS -->", source_links_html(lang, intl=intl))
    html = html.replace("<!-- PODKLADARNA_SAMPLES -->", samples_html(lang))
    html = html.replace("{{APP_VERSION}}", APP_VERSION)
    html = html.replace("<!-- PODKLADARNA_USAGE -->", usage_footer_html(lang))
    html = html.replace("<!-- PODKLADARNA_USER_SOURCES -->", user_sources_form_html(request, lang))
    html = html.replace("<!-- PODKLADARNA_PAID -->", paid_flow.form_note_html(request, lang))
    if paid_flow.paid_site(request) is not None:
        # Placená služba: věta „… je zdarma, příspěvek pomůže“ ani dar v patičce neplatí.
        keep_donation = donations.enabled_for_request(request)
        html = paid_site_footer(html, keep_donation=keep_donation)
        if keep_donation:
            html = html.replace(
                "{{h:web.page.prisel_podklad_vhod_podkladarna_je_zdarma}}", "{{h:web.page.donate_note_intl}}"
            )
    # Cloudflare cachuje /static/* až 4 h – query podle verze vynutí nový JS/CSS po deployi.
    v = urllib.parse.quote(APP_VERSION, safe="")
    html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={v}"')
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={v}"')
    return _localized_html(request, apply_channel(html), web_catalog=True)


def country_sources_html(request: Request, lang: str) -> str:
    """Zdroje a citace zemí mimo Česko, které doména nabízí (``countries.yaml``), pro stránku licence
    v jiném jazyce než češtině (``<!-- PODKLADARNA_COUNTRY_SOURCES -->``; česká stránka značku nemá)."""
    from app.pipeline import country_profile as _country_profile

    items = []
    for country in countries.allowed_countries(i18n.site_for_request(request)):
        if country.code == countries.DEFAULT_COUNTRY or not (country.citation or country.attribution):
            continue
        lines = "".join(
            f"<li>{html_escape(line)}</li>" for line in _country_profile.attribution_lines({"country": country.code})
        )
        items.append(
            f"<h3>{html_escape(country.name(lang))}</h3>\n"
            f"<p><strong>{html_escape(country.citation)}</strong></p>\n"
            + (f"<ul>{lines}</ul>\n" if lines else "")
        )
    return "".join(items)


@app.get("/licence", response_class=HTMLResponse)
def licence_page(request: Request) -> HTMLResponse:
    html = _page_source("licence.html", request)
    html = html.replace("{{APP_VERSION}}", APP_VERSION)
    site = i18n.site_for_request(request)
    base = (site.base_url if site else app_settings.PUBLIC_BASE_URL or "").rstrip("/")
    html = html.replace("{{SITE_URL}}", html_escape(base or "/"))
    html = html.replace("{{SITE_HOST}}", html_escape(urllib.parse.urlsplit(base).netloc or i18n._request_host(request)))
    if site is not None and tuple(site.countries) != ("CZ",):
        html = strip_github_links(html)  # mezinárodní verze GitHub neukazuje nikde (Petr 2026-11-19)
    if "<!-- PODKLADARNA_COUNTRY_SOURCES -->" in html:
        sources = country_sources_html(request, i18n.resolve_lang(request)[0])
        html = html.replace("<!-- PODKLADARNA_COUNTRY_SOURCES -->", sources)
    v = urllib.parse.quote(APP_VERSION, safe="")
    html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={v}"')
    return _localized_html(request, html)


@app.get("/own-data", response_class=HTMLResponse)
def own_data_page(request: Request) -> HTMLResponse:
    """Návod k vlastním výškovým datům (``app.user_sources``): jen tam, kde je nahrání na formuláři."""
    if not user_sources_form_html(request, i18n.resolve_lang(request)[0]):
        raise HTTPException(404)
    html = _page_source("own-data.html", request)
    html = html.replace("{{APP_VERSION}}", APP_VERSION).replace("{{MAX_MB}}", str(int(user_sources.MAX_TOTAL_MB)))
    v = urllib.parse.quote(APP_VERSION, safe="")
    html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={v}"')
    return _localized_html(request, html)


@app.get("/podpora", response_class=HTMLResponse)
def support_page(request: Request) -> HTMLResponse:
    if donations.enabled_for_request(request):
        # Mezinárodní doména s darem přes PayPal (Petr 2026-11-19); česká stránka s QR platbou beze změny.
        lang = i18n.resolve_lang(request)[0]
        html = (WEB_DIR / "donate.html").read_text(encoding="utf-8")
        html = html.replace("{{LANG}}", lang)
        # Česky jen česká QR Platba, jinak Stripe, SEPA (EPC QR) a PayPal podle .env (Petr 2026-11-19).
        czech = lang == i18n.SOURCE_LANG
        html = html.replace("<!-- DONATE_CZ_QR -->", donations.czech_qr_block_html(lang))
        html = html.replace("<!-- DONATE_STRIPE -->", "" if czech else donations.stripe_block_html(lang))
        html = html.replace("<!-- DONATE_SEPA -->", "" if czech else donations.sepa_block_html(lang))
        html = html.replace("<!-- DONATE_PAYPAL -->", "" if czech else donations.paypal_block_html(lang))
    elif paid_flow.paid_site(request) is not None:
        return RedirectResponse("/", status_code=302)  # dar jen na bezplatné doméně (Petr 2026-11-16)
    else:
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
def api_source_status(request: Request, country: str | None = None):
    """Dostupnost zdrojů dat (web podle ní varuje před spuštěním); ``country`` = země výřezu (místní servery).

    Starší výsledek než ``SOURCE_CHECK_TTL_MIN`` spustí kontrolu na pozadí (``checking``). Názvy a dopady
    zdrojů v jazyce stránky (čeština beze změny)."""
    ttl = app_settings.SOURCE_CHECK_TTL_MIN
    code = str(country or "").strip().upper()
    if code not in countries.offered_codes():
        code = countries.DEFAULT_COUNTRY
    checking = source_status.refresh_if_stale()
    snap = _source_snapshot(code, request)
    return {"enabled": ttl > 0, "ttl_min": ttl, "checking": checking, "country": code, **snap}


def _source_snapshot(code: str, request: Request) -> dict:
    """Stav zdrojů země v jazyce stránky; ``blocks_job`` = výpadek zdroje job odmítne (``app.source_gate``)."""
    snap = source_status.localized(source_status.snapshot(code), i18n.resolve_lang(request)[0])
    if source_gate.enabled():
        critical = {p.key for p in source_gate.critical_probes(code)}
        snap["services"] = [{**svc, "blocks_job": svc.get("key") in critical} for svc in snap.get("services") or []]
    return snap


@app.post("/api/source_status/recheck")
def api_source_status_recheck(request: Request, country: str | None = None):
    """„Zkusit zdroje znovu“: ověří zdroje země, které poslední kontrola vidí mimo provoz.

    Každý zdroj nejvýš jednou za ``source_gate.RECHECK_MIN_S`` (víc návštěvníků = jeden dotaz na cizí server)."""
    code = str(country or "").strip().upper()
    if code not in countries.offered_codes():
        code = countries.DEFAULT_COUNTRY
    out = source_gate.recheck(code) if app_settings.SOURCE_CHECK_TTL_MIN > 0 else {"rechecked": [], "throttled": False}
    snap = _source_snapshot(code, request)
    return {
        "enabled": app_settings.SOURCE_CHECK_TTL_MIN > 0,
        "ttl_min": app_settings.SOURCE_CHECK_TTL_MIN,
        "checking": False,
        "country": code,
        **out,
        **snap,
    }


def _recent_source_outage(job_country: countries.Country, bbox) -> dict | None:
    """Tentýž výřez v poslední hodině selhal na výpadku zdroje → {key, label, minutes} pro upozornění v GUI."""
    try:
        since = datetime.now(timezone.utc) - timedelta(hours=1)
        job = db.recent_source_outage(job_country.code, list(bbox), since)
    except Exception:  # noqa: BLE001 – upozornění je jen doplněk
        logger.warning("Hledání jobu selhaného na výpadku zdroje selhalo", exc_info=True)
        return None
    if not job:
        return None
    key = str((job.get("options") or {}).get("source_outage") or "")
    probe = next((p for p in source_status.probes_for(job_country.code) if p.key == key), None)
    try:
        minutes = max(1, int((datetime.now(timezone.utc) - datetime.fromisoformat(job["created_at"])).total_seconds() // 60))
    except (KeyError, TypeError, ValueError):
        minutes = None
    return {
        "key": key,
        "label": source_gate._label(probe) if probe else key,
        "minutes": minutes,
    }


@app.get("/api/source_events")
def api_source_events(request: Request, days: float = 7):
    """Deník problémů zdrojů dat pro provozovatele (souhrn a události, bez osobních údajů).

    Jen s hlavičkou ``Authorization: Bearer <SOURCE_EVENTS_TOKEN>``; bez nastaveného tokenu 404."""
    _require_ops_token(request, "/api/source_events")
    return source_events.report(days)


def _require_ops_token(request: Request, path: str) -> None:
    """Provozní výpisy jen s ``Authorization: Bearer <SOURCE_EVENTS_TOKEN>``; bez nastaveného tokenu 404."""
    expected = app_settings.SOURCE_EVENTS_TOKEN
    if not expected:
        raise HTTPException(404)
    got = request.headers.get("authorization") or ""
    scheme, _, token = got.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        token.strip().encode("utf-8"), expected.encode("utf-8")
    ):
        logger.warning("GET %s – neplatný token z %s", path, client_ip(request))
        raise HTTPException(401)


@app.get("/api/ops/failed_jobs")
def api_ops_failed_jobs(request: Request, hours: float = 24):
    """Nepovedené joby za ``hours`` hodin pro denní kontrolu (i privátní, bez osobních údajů; ``app.failed_jobs``)."""
    _require_ops_token(request, "/api/ops/failed_jobs")
    return failed_jobs.report(hours)


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


def _area_probe(
    job_country: countries.Country, bbox, deadline_s: float = area_probe.DEADLINE_S, *, include_dtm: bool = True
) -> dict:
    """Sonda zdrojů výřezu (výšky, technická mapa, DTM obcí) s limitem; chyba sondy job nebrzdí."""
    try:
        return area_probe.probe(job_country, tuple(bbox), deadline_s=deadline_s, include_dtm=include_dtm)
    except Exception:  # noqa: BLE001 – sonda je jen pojistka
        logger.warning("Sonda zdrojů výřezu selhala", exc_info=True)
        return {"verdict": "unknown", "warnings": [], "block": None}


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
    if bbox_exceeds_limit(*box):  # bez limitu by šly stránkované dotazy RÚIAN třeba pro celou ČR
        return {"available": False, "reason": "too_large", "units": []}
    return area_probe.dtm_coverage(box, timeout=area_probe.DEADLINE_S)


def _country_licenses(country) -> dict | None:
    """Shrnutí zdrojů a licencí výřezu mimo Česko pro GUI (Petr 2026-11-19, volba C: jen informace, bez souhlasu).

    Stejné texty jako ZIP (ABOUT_OMAPIC.txt) a stránka /licence (``countries.yaml`` citation / attribution);
    Česko klíč nedostane, odpověď pro český výřez zůstává beze změny."""
    from app.pipeline import country_profile as _country_profile

    if country.code == countries.DEFAULT_COUNTRY or not (country.citation or country.attribution):
        return None
    return {
        "citation": country.citation or "",
        "lines": list(_country_profile.attribution_lines({"country": country.code})),
        "url": "/licence",
    }


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
    licenses = _country_licenses(job_country)
    if licenses:
        country_info["licenses"] = licenses
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
    probe = _area_probe(job_country, (west, south, east, north), include_dtm=False)
    if probe.get("block") is not None:
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
            "too_large_reason": "elevation",
            "elevation_share": (probe.get("elevation") or {}).get("share"),
            "estimate_minutes": None,
            "estimate_minutes_with_refs": None,
            "label": i18n.Msg("api.sheets.label_area", width=float(width_km), height=float(height_km), area=float(area_km2)),
            "hint": probe["block"],
        }
    lidar_sheets = job_country.elevation == "cz_openzu"  # listy SM5 jen u LiDARu ČÚZK
    try:
        sheets = query_sm5_sheets(west, south, east, north) if lidar_sheets else []
    except FetchError as exc:
        raise HTTPException(400, _exc_detail(exc)) from exc
    names = [s["mapnom"] for s in sheets]
    sheets_too_big = len(sheets) > MAX_SHEETS
    hint = (
        i18n.Msg("api.sheets.too_many", names=", ".join(names), count=len(sheets), max=MAX_SHEETS)
        if sheets_too_big
        else None
    )
    can_estimate = (bool(sheets) or not lidar_sheets) and not sheets_too_big
    # Samokalibrace z vlastních dokončených jobů (3.34.0): zvlášť příprava a skládání map, jen joby téže země.
    try:
        cal = db.estimate_calibration(country=job_country.code)
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
        # Velké stahování (listy LiDARu mimo cache): GUI hned varuje, že mapa bude hotová později (app/prefetch.py).
        "big_download": prefetch.big_download_info(names, pf) if can_estimate and lidar_sheets else None,
        "label": (
            i18n.Msg("api.sheets.label", names=", ".join(names), count=len(sheets))
            if sheets
            else i18n.Msg("api.sheets.none")
            if lidar_sheets
            else i18n.Msg("api.sheets.raster", country=job_country.msg_name())
        ),
        # Mimo Česko výšky z rastru země bez listů SM5: GUI pak nežádá aspoň jeden list.
        "needs_sheets": lidar_sheets,
        "hint": hint,
        # Sonda zdrojů výřezu: varování, kde výšky nebo technická mapa výřez nepokrývají (kreslí se z OSM).
        "probe": {
            "verdict": probe.get("verdict"),
            "elevation": probe.get("elevation"),
            "technical_map": probe.get("technical_map"),
            "warnings": probe.get("warnings") or [],
            "data_notes": probe.get("data_notes"),
        },
        # Tentýž výřez v poslední hodině selhal na výpadku zdroje: GUI nabídne ověření zdrojů místo opakování.
        "recent_source_outage": _recent_source_outage(job_country, (west, south, east, north)),
    }


# Volby jobu, které se ven neposílají (IP klienta, e-mail privátního jobu, název API klienta).
_JOB_OPTIONS_HIDDEN = ("client_ip", "notify_email", "api_client_name")


def _public_job(job: dict) -> dict:
    """Kopie jobu pro odpověď API bez osobních údajů v `options`."""
    opts = job.get("options")
    if not isinstance(opts, dict) or not any(k in opts for k in _JOB_OPTIONS_HIDDEN):
        return job
    out = dict(job)
    out["options"] = {k: v for k, v in opts.items() if k not in _JOB_OPTIONS_HIDDEN}
    return out


def _job_on_paid_site(job: dict) -> bool:
    """Job zadaný na mezinárodní (placené) doméně – Omapic.

    Rozhoduje uložená doména jobu (``options.site``) podle ``SITES``; doména, která v ``SITES`` už
    není, se pozná podle jazyka zadání. Joby bez domény (starší, bez ``SITES``) patří Podkladárně.
    """
    opts = job.get("options") if isinstance(job.get("options"), dict) else {}
    host = str(opts.get("site") or "").strip().lower()
    if not host:
        return False
    site = i18n.sites().get(host)
    if site is not None:
        return site.mode == "paid"
    return str(opts.get("lang") or "cs") != "cs"


@app.get("/api/jobs")
def api_list_jobs(request: Request):
    # Podkladárna a Omapic sdílí server, ale seznam ukazuje jen joby své domény (Petr 2026-11-16);
    # job druhé domény je vidět jen anonymně jako běžící privátní (vytížení serveru).
    site = i18n.site_for_request(request)
    paid_request = site is not None and site.mode == "paid"
    all_jobs = db.list_jobs(include_private=True)
    foreign = {j["id"] for j in all_jobs if _job_on_paid_site(j) != paid_request}
    jobs = [_public_job(j) for j in all_jobs if not db.job_is_private(j) and j["id"] not in foreign]
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
        if current_job and _job_on_paid_site(current_job) != paid_request:
            foreign.add(current_id)
        if current_job and (db.job_is_private(current_job) or current_id in foreign):
            private_live = {
                "status": current_job.get("status") or "running",
                "started_at": current_job.get("started_at")
                or current_job.get("created_at"),
            }
    # ID privátních jobů a jobů druhé domény ven nepouštět: ID je klíč k detailu (název, výřez),
    # privátní job ho drží jen v „Moje mapy“ zadavatele. Počty fronty (queue_size) zůstávají.
    by_id = {j["id"]: j for j in all_jobs}

    def _hidden(job_id: str) -> bool:
        if job_id in foreign:
            return True
        job = by_id.get(job_id)
        if job is None:
            try:
                job = db.get_job(job_id)
            except KeyError:
                return False
            by_id[job_id] = job
        return db.job_is_private(job) or _job_on_paid_site(job) != paid_request

    if snap.get("current") and _hidden(snap["current"]):
        snap["current"] = None
    for key in ("queued", "prefetching", "prefetch_queued"):
        if isinstance(snap.get(key), list):
            snap[key] = [i for i in snap[key] if not _hidden(i)]
    return {"jobs": jobs, **snap, "private_live": private_live}


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: str, request: Request):
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
    if job.get("status") in ("done", "done_with_errors"):
        # Stáří a druh výškových dat země v jazyce stránky (web je ukáže u hotové mapy); Česko nic.
        from app import data_notes

        notes = data_notes.job_lines(job, i18n.resolve_lang(request)[0])
        if notes:
            out = {**out, "data_notes": notes}
    # Pozice ve frontě i u privátního jobu (není ve veřejném seznamu, „Moje mapy“ ji čtou odtud).
    pos = worker.queue_position(job_id)
    if pos is not None:
        out["queue_position"] = pos
    return out


@app.get("/api/jobs/{job_id}/template")
def api_job_template(job_id: str, request: Request):
    """Zadání jobu pro formulář nové mapy (název, výřez, volby) – i u běžícího jobu; poznámky v jazyce stránky."""
    try:
        job = db.get_job(job_id)
    except KeyError:
        raise HTTPException(404, i18n.Msg("api.job_not_found"))
    if db.job_is_expired(job):
        db.delete_job(job_id)
        raise HTTPException(410, i18n.Msg("api.private_expired"))
    return i18n.localize(job_form_template(job, presets=load_presets()), i18n.resolve_lang(request)[0])


@app.get("/api/jobs/{job_id}/log")
def api_job_log(job_id: str, request: Request, after: int = 0):
    try:
        job = db.get_job(job_id)
    except KeyError:
        raise HTTPException(404, i18n.Msg("api.job_not_found"))
    if db.job_is_expired(job):
        db.delete_job(job_id)
        raise HTTPException(410, i18n.Msg("api.private_expired"))
    # Log se ukládá česky (web z něj čte průběh); v jiném jazyce stránky přeložený řádek + původní v ``src``.
    return {"lines": log_i18n.localize_log_lines(db.get_logs(job_id, after), i18n.resolve_lang(request)[0])}


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


def download_prefix(job: dict) -> str:
    """Předpona stahovaného souboru podle jazyka zadání (mezinárodní verze = omapic)."""
    from app.pipeline.zip_texts_en import cs_intl_site_url, zip_lang

    opts = job.get("options")
    return "podkladarna" if zip_lang(opts) == "cs" and not cs_intl_site_url(opts) else "omapic"


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
        if paid_flow.job_locked(job):  # objednávka: výstupy až po zaplacení (odkaz z e-mailu)
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
    filename = db.zip_download_filename(job_id, job["name"], download_prefix(job))
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
        from app.pipeline.zip_texts_en import cs_intl_site_url, zip_lang

        built = build_georef_previews_zip(
            out, lang=zip_lang(job.get("options")), cs_intl_url=cs_intl_site_url(job.get("options"))
        )
        if built is None or not built.is_file():
            raise HTTPException(404, i18n.Msg("api.georef_previews_missing"))
        zip_path = built
    stem = db.safe_zip_stem(job["name"])
    filename = (
        f"podkladarna-{stem}-georef-nahledy.zip"
        if download_prefix(job) == "podkladarna"
        else f"omapic-{stem}-georef-previews.zip"
    )
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
    if paid_flow.job_locked(job):
        raise HTTPException(404, i18n.Msg("api.link_invalid"))
    _require_finished(job)
    zip_path = _output_zip_path(job["id"])
    if not zip_path:
        raise HTTPException(404, i18n.Msg("api.output_not_ready"))
    filename = db.zip_download_filename(job["id"], job["name"], download_prefix(job))
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
        # Hlídač schválených oprav zdrojů (configs/source_fixes.yaml v master): co je na serveru nasazené.
        "source_fixes": source_fixes.health(),
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
            lang=i18n.resolve_lang(request)[0],
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


@app.post("/api/source_suggestion")
async def api_source_suggestion(request: Request):
    """Návrh nového zdroje dat (WMS apod.) → e-mail Petrovi k ručnímu posouzení.

    Navrženou adresu server nikdy nevolá (SSRF); kontroluje jen její tvar.
    Sdílí hodinový limit se zpětnou vazbou.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, i18n.Msg("api.invalid_json")) from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, i18n.Msg("api.invalid_json"))
    url = str(payload.get("url") or "").strip()
    area = str(payload.get("area") or "").strip()
    license_text = str(payload.get("license") or "").strip()
    contact = str(payload.get("contact") or "").strip()
    comment = str(payload.get("comment") or "").strip()
    honeypot = str(payload.get("website") or "").strip()

    if honeypot:
        logger.info("Source suggestion honeypot hit from %s", client_ip(request))
        return {"ok": True}

    if not is_valid_source_url(url):
        raise HTTPException(400, i18n.Msg("api.source_suggestion.url_invalid"))
    if not area:
        raise HTTPException(400, i18n.Msg("api.source_suggestion.area_missing"))
    if not contact:
        raise HTTPException(400, i18n.Msg("api.feedback.contact_missing"))
    if len(contact) > MAX_CONTACT_LEN:
        raise HTTPException(400, i18n.Msg("api.feedback.contact_long", max=MAX_CONTACT_LEN))
    if len(area) > MAX_SOURCE_AREA_LEN or len(license_text) > MAX_SOURCE_LICENSE_LEN:
        raise HTTPException(400, i18n.Msg("api.source_suggestion.too_long"))
    if len(comment) > MAX_COMMENT_LEN:
        raise HTTPException(400, i18n.Msg("api.feedback.comment_long", max=MAX_COMMENT_LEN))

    ip = client_ip(request)
    limit_msg = check_feedback_rate(ip)
    if limit_msg:
        raise HTTPException(429, limit_msg)

    try:
        send_source_suggestion(
            url=url,
            area=area,
            license_text=license_text,
            contact=contact,
            comment=comment,
            client_ip=ip,
            lang=i18n.resolve_lang(request)[0],
        )
    except MailError as exc:
        logger.warning("Source suggestion mail failed: %s", exc)
        raise HTTPException(
            502,
            i18n.Msg("api.feedback.mail_failed", error=str(exc)),
        ) from exc

    return {"ok": True}


API_KEY_HEADER = "x-api-key"


def _api_client_name(request: Request) -> str | None:
    """Hlavička X-Api-Key: chybí → None; platná → název klienta; neplatná / žádný klíč nenastaven → 401.

    Klíče jsou ``API_CLIENT_KEYS`` (název:klíč) a starý ``API_CLIENT_KEY`` jako klient ``default``. Porovnává se
    v konstantním čase se všemi klíči; hodnota klíče se nikdy neloguje ani nevrací, jen název klienta.
    """
    got = request.headers.get(API_KEY_HEADER)
    if got is None:
        return None
    keys = app_settings.parse_api_client_keys(app_settings.API_CLIENT_KEYS, app_settings.API_CLIENT_KEY)
    sent = got.strip().encode("utf-8")
    found = None
    for name, expected in keys.items():
        if hmac.compare_digest(sent, expected.encode("utf-8")) and found is None:
            found = name
    if found is None:
        logger.warning("POST /api/jobs – neplatný API klíč z %s", client_ip(request))
        raise HTTPException(401, i18n.Msg("api.key_invalid"))
    return found


def can_reuse_lidar(prev: dict | None, bbox: list[float], country: str) -> bool:
    """Převzít sloučený LAZ předchozího jobu: stejný výřez a stejná země (souřadnice LAZ). U hranice může
    tentýž výřez být na české doméně job CZ a na mezinárodní job AT – český LAZ v S-JTSK by se tam nehodil."""
    if not prev or not prev.get("has_reusable_lidar"):
        return False
    opts = prev.get("options") or {}
    prev_country = str(opts.get("country") or countries.DEFAULT_COUNTRY).upper()
    return prev_country == country and db.bbox_close(opts.get("bbox_wgs84") or [], bbox)


def _stage_user_sources(uploads: dict[str, UploadFile], bbox) -> tuple[Path, dict]:
    """Uloží a zkontroluje vlastní výšková data zadavatele (``app.user_sources``) do dočasné složky.

    Vrací (složka, informace podle druhu). Při chybě složku smaže a vyhodí ``UserSourceError``.
    """
    staging = user_sources.new_staging_dir()
    budget = user_sources.MAX_TOTAL_MB * 1_000_000
    try:
        for kind in user_sources.KINDS:
            upload = uploads[kind]
            try:
                budget -= user_sources.save_upload(upload.file, staging / f"{kind}.upload", max_bytes=budget)
            except user_sources.UserSourceError as exc:
                exc.params.setdefault("kind", kind.upper())
                raise
            finally:
                try:
                    upload.file.close()
                except Exception:  # noqa: BLE001
                    pass
        return staging, user_sources.prepare(staging, bbox)
    except BaseException:
        user_sources.discard(staging)
        raise


def _user_source_error(exc: "user_sources.UserSourceError") -> HTTPException:
    params = {"kind": "", "max": 0, "min": 0, "share": 0, **exc.params}
    return HTTPException(400, i18n.Msg(exc.key, **params))


@app.post("/api/jobs")
async def api_create_job(request: Request):
    logger.info("POST /api/jobs – cteni multipart form")
    # Tělo nad strop vlastních dat (+ rezerva na pole formuláře) se ani nečte (Cloudflare stejně pustí do 100 MB).
    try:
        content_length = int(request.headers.get("content-length") or 0)
    except ValueError:
        content_length = 0
    if content_length > (user_sources.MAX_TOTAL_MB + 5) * 1_000_000:
        raise HTTPException(413, i18n.Msg("api.user_sources.too_big", kind="", max=user_sources.MAX_TOTAL_MB))
    try:
        form = await request.form()
    except Exception as exc:
        logger.exception("Multipart form parse failed")
        raise HTTPException(400, i18n.Msg("api.form_unreadable", error=str(exc))) from exc

    # Job ukázky, který server zakládá sám sobě (app.samples_refresh): soukromý, bez e-mailu a limitů.
    sample_job = samples_refresh.request_is_internal(request)
    api_client_name = None if sample_job else _api_client_name(request)
    api_client = api_client_name is not None

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
    # Vlastní výšková data zadavatele (``app.user_sources``): jen Omapic mimo Česko, za vypínačem USER_SOURCES.
    user_uploads = {kind: _upload_files(form, f"user_{kind}") for kind in user_sources.KINDS}
    has_user_sources = any(user_uploads.values())
    if has_user_sources:
        if (
            not user_sources.enabled()
            or job_country.code == countries.DEFAULT_COUNTRY
            or not intl_site(request)
        ):
            raise HTTPException(400, i18n.Msg("api.job.upload_unsupported"))
        if not api_client and paid_flow.active_for(request):
            raise HTTPException(400, i18n.Msg("api.user_sources.paid_unsupported"))
        if any(len(user_uploads[kind]) != 1 for kind in user_sources.KINDS):
            raise HTTPException(400, i18n.Msg("api.user_sources.both_required"))
        if _form_str(form, "user_sources_consent").strip().lower() not in {"1", "true", "yes", "on"}:
            raise HTTPException(400, i18n.Msg("api.user_sources.consent_required"))
    # DTM obcí jen u výřezu v limitu (stránkované dotazy RÚIAN by šly pro libovolně velké území).
    # Sonda čeká až JOB_DEADLINE_S – mimo event loop, ať async handler nezdrží ostatní požadavky.
    probe = await run_in_threadpool(
        _area_probe, job_country, bbox, area_probe.JOB_DEADLINE_S, include_dtm=not bbox_exceeds_limit(*bbox)
    )
    if probe.get("block") is not None and not has_user_sources:  # s vlastními daty pokrytí zdroje země nerozhoduje
        raise HTTPException(400, probe["block"])
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
    lidar_sheets = job_country.elevation == "cz_openzu"  # mimo Česko výšky bez listů SM5
    if not lidar_sheets:
        sheets = []
    if not sheets and lidar_sheets:
        try:
            sheets = query_sm5_sheets(*bbox)
        except FetchError as exc:
            raise HTTPException(400, _exc_detail(exc)) from exc
    if not sheets and lidar_sheets:
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
    limit_msg = None if sample_job else check_create_api_job() if api_client else check_create_job(remote_ip)
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
    if formlines_raw in FORMLINE_MODES or formlines_raw == FORMLINE_MODE_AUTO:
        options["contour_formlines"] = formlines_raw
    contour_smooth_raw = _form_str(form, "contour_smooth").strip().lower()
    if contour_smooth_raw.isdigit() and int(contour_smooth_raw) in CONTOUR_SMOOTH_LEVELS:
        options["contour_smooth"] = contour_smooth_raw
    elif contour_smooth_raw == "auto":
        options["contour_smooth"] = "auto"
    veg_smooth_raw = _form_str(form, "veg_smooth").strip().lower()
    if veg_smooth_raw.isdigit() and int(veg_smooth_raw) in VEG_SMOOTH_LEVELS:
        options["veg_smooth"] = veg_smooth_raw
    clean_open_raw = _form_str(form, "clean_open").strip().lower()
    if clean_open_raw in CLEAN_OPEN_MODES:
        options["clean_open"] = clean_open_raw
    if _form_str(form, "osm_tags").strip().lower() in {"1", "true", "on"}:
        options["osm_tags"] = True
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
    if probe.get("verdict") in ("ok", "warn") and not has_user_sources:
        options["area_probe"] = area_probe.job_summary(probe)
    if countries.outside_share(country_share) > 0:
        options["outside_share"] = countries.outside_share(country_share)
    # Privátní režim: mimo veřejný seznam + e-mail s tokenizovaným odkazem.
    # API klient (platný X-Api-Key) má job vždy privátní, e-mail nepotřebuje –
    # `download_token` dostane v odpovědi.
    if api_client:
        options["private"] = True
        options["api_client"] = True
        options["api_client_name"] = api_client_name
        notify_email = _form_str(form, "notify_email").strip()
        if notify_email and "@" in notify_email and "." in notify_email.split("@")[-1]:
            options["notify_email"] = notify_email
    elif sample_job:
        options["private"] = True
        options["sample"] = True
    elif _opt_bool("private"):
        notify_email = _form_str(form, "notify_email").strip()
        if not notify_email or "@" not in notify_email or "." not in notify_email.split("@")[-1]:
            raise HTTPException(
                400,
                i18n.Msg("api.job.private_email"),
            )
        options["private"] = True
        options["notify_email"] = notify_email
        banned_msg = check_banned_email(notify_email)
        if banned_msg:
            raise HTTPException(403, banned_msg)
        # Mírný limit soukromých jobů na e-mail (objednávky na placené doméně mají vlastní tok).
        if not paid_flow.active_for(request):
            email_msg = check_private_email(notify_email, remote_ip)
            if email_msg:
                raise HTTPException(429, email_msg)
    if has_user_sources and not options.get("private"):
        # Vlastní data: výsledek jen zadavateli (soukromý job s odkazem e-mailem), nikdy ve veřejném seznamu.
        raise HTTPException(400, i18n.Msg("api.user_sources.private_required"))
    reuse_id = _form_str(form, "reuse_job_id").strip()
    if reuse_id and not has_user_sources:
        try:
            prev = db.get_job(reuse_id)
        except KeyError:
            prev = None
        if can_reuse_lidar(prev, bbox, job_country.code):
            options["reused_from"] = reuse_id
    # Potvrzený výpadek zdroje výšek země (mimo Česko) job odmítne dřív, než vznikne – nezapočítá se do limitů.
    outage = await run_in_threadpool(
        source_gate.check,
        job_country.code,
        has_fallback=has_user_sources or bool(options.get("reused_from")),
    )
    if outage is not None:
        raise HTTPException(503, outage["message"])
    # Placená doména se zapnutým prodejem: job vznikne až po potvrzení e-mailu (app.paid_flow).
    if not api_client and not sample_job and paid_flow.active_for(request):
        return paid_flow.start_order(
            request,
            name=name,
            preset_id=preset_id,
            options=options,
            email=_form_str(form, "notify_email"),
        )
    user_staging: Path | None = None
    user_info: dict = {}
    if has_user_sources:
        try:
            user_staging, user_info = await run_in_threadpool(
                _stage_user_sources, {k: v[0] for k, v in user_uploads.items()}, tuple(bbox)
            )
        except user_sources.UserSourceError as exc:
            raise _user_source_error(exc) from exc
        options["user_sources"] = True
    # Job s vlastními daty se s jiným nikdy neslučuje (stejný výřez neznamená stejná data).
    # Ukázka nesmí převzít cizí (i soukromý) job se stejným výřezem.
    dup = None if (has_user_sources or sample_job) else db.find_duplicate_active_job(options)
    if dup:
        logger.info(
            "Duplicate job skipped – returning existing %s (status=%s)",
            dup["id"],
            dup.get("status"),
        )
        msg = i18n.Msg(
            "api.job.duplicate",
            state=i18n.Msg("api.job.duplicate_running" if dup.get("status") == "running" else "api.job.duplicate_waiting"),
            id=dup["id"][:8],
            name=dup.get("name") or i18n.Msg("api.job.duplicate_unnamed"),
        )
        db.append_log(dup["id"], str(msg))  # log jobu česky
        out = _public_job(dict(dup))
        # Stejný job založený týmž API klíčem – klient potřebuje token ke stažení.
        dup_opts = dup.get("options") or {}
        if api_client and dup_opts.get("api_client") and (dup_opts.get("api_client_name") or "default") == api_client_name:
            token = db.get_download_token(dup["id"])
            if token:
                out["download_token"] = token
        out["duplicate_skipped"] = True
        out["duplicate_message"] = msg.render(i18n.resolve_lang(request)[0])
        return out
    try:
        job = db.create_job(name, preset_id, options)
        job_id = job["id"]
        if user_staging is not None:
            user_sources.attach(user_staging, JOBS_DIR / job_id)
    except BaseException:
        user_sources.discard(user_staging)
        raise

    def log(msg: str) -> None:
        db.append_log(job_id, msg)

    if user_info:
        # Jen technické údaje – žádné názvy souborů ani metadata z TIFF tagů.
        log(
            "Výšky: vlastní data zadavatele – terén "
            + user_info["dtm"].log_text()
            + "; povrch "
            + user_info["dsm"].log_text()
            + ". Použijí se jen pro tuto mapu a po jejím doběhnutí se smažou."
        )

    # Kopie LAZ z předchozího jobu běží až ve workeru (run_job_pipeline),
    # ať odpověď „job založen“ nepřijde až po dlouhém copy.
    if options.get("reused_from"):
        log("Data LiDAR se převezmou z předchozí mapy.")

    def _yn(key: str, default: bool = False) -> str:
        return "ano" if options.get(key, default) else "ne"

    from app.pipeline import country_profile as _profile

    _country = _profile.job_country(options)
    _tech = _profile.technical_map_source(options)
    from app.pipeline.technical_maps import foreign as _foreign_tech

    _spec = _foreign_tech(_tech)
    _tech_on, _tech_off = ("DTM", "OSM+ZABAGED") if _tech == "cz_dtm" else ((_spec.short, "OSM") if _spec else ("OSM", "OSM"))
    log(
        f"Přijato: preset {preset_id} · 1:{resolved['map_scale']} · "
        f"ekvidistance {resolved['contour_interval']} m · "
        + (
            f"listy SM5 {', '.join(options['sm5_sheets'])}"
            if options.get("sm5_sheets")
            else f"země {_country.name('cs') if _country else options.get('country') or '?'}"
        )
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
        f"kresba sprint {_tech_on if resolve_use_dtm(options, 'sprint') else _tech_off}/"
        f"les+MTBO {_tech_on if resolve_use_dtm(options, 'forest') else _tech_off} · "
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
        + (f" · API klient {api_client_name}" if api_client else "")
        + (" · ukázka (generuje server)" if sample_job else "")
    )
    outside = countries.outside_note(job_country, country_share)
    if outside:
        log(f"⚠ {outside}")
    if sample_job:
        log("Ukázka – generuje server (přegenerování ukázkových map), ZIP nahradí ukázku na webu.")
    elif options.get("private") and not options.get("notify_email"):
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
def map_tile(z: int, x: int, y: int, request: Request):
    try:
        validate_tile(z, x, y)
        if not is_cached(z, x, y) and not allow_fetch(client_ip(request)):
            # Dlaždici mimo cache server pro tuto IP teď nestáhne (limit za minutu, revize 2026-11-16).
            return Response(status_code=429, headers={"Retry-After": "60"})
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


app.include_router(paid_flow.router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
