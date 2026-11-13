from __future__ import annotations

import os
from pathlib import Path

from app.tool_env import apply_local_gis_env

APP_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = APP_ROOT / "configs"
APP_VERSION = "3.34.1"

# Product defaults for NAS Docker when compose injects empty SMTP_HOST= /
# PUBLIC_BASE_URL= and host .env never got the SMTP lines. Not secrets.
_NAS_DEFAULT_SMTP_HOST = "datais-cz.mail.protection.outlook.com"
_NAS_DEFAULT_PUBLIC_BASE_URL = "https://podkladarna.kibos.link"


def _running_in_docker() -> bool:
    return Path("/.dockerenv").is_file()


def _load_dotenv(path: Path) -> None:
    """Načte KEY=VALUE z .env do os.environ (neprepisuje neprázdné). Stdlib only.

    Prázdný existující env (např. compose ``SMTP_HOST=${SMTP_HOST:-}``) se
    doplní z .env — jinak by ``/data/.env`` na NAS nikdy nefungoval.
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        existing = os.environ.get(key)
        if existing is not None and existing.strip():
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {'"', "'"}:
            val = val[1:-1]
        os.environ[key] = val


# Tip / lokál: SMTP + PUBLIC_BASE_URL žijí v .env vedle checkoutu (viz DEV.md).
# Bez toho privátní job skončí done, ale e-mail se neodešle.
_load_dotenv(APP_ROOT / ".env")

apply_local_gis_env()

if os.environ.get("PODKLADARNA_DATA"):
    DATA_ROOT = Path(os.environ["PODKLADARNA_DATA"])
elif os.name == "nt":
    DATA_ROOT = APP_ROOT / "data"
else:
    DATA_ROOT = Path("/data")
# Docker/NAS: host .env u compose se do image nedostane; volitelně SMTP v /data/.env.
_load_dotenv(DATA_ROOT / ".env")
JOBS_DIR = DATA_ROOT / "jobs"
CACHE_DIR = DATA_ROOT / "cache"
DOWNLOADS_DIR = CACHE_DIR
DB_PATH = DATA_ROOT / "podkladarna.db"

MAX_CONCURRENT_LIDAR = int(os.environ.get("MAX_CONCURRENT_LIDAR", "1"))
# Les a MTBO jednoho jobu se sestavují souběžně ve dvou procesech (ne sprint; viz discipline_maps.py).
PARALLEL_DISCIPLINE_MAPS = os.environ.get("PARALLEL_DISCIPLINE_MAPS", "1").strip().lower() not in ("0", "false", "no", "off", "")
# Provoz A/B (stabilní + test) na jednom HW: sdílený adresář se zámkem těžkého jobu.
# Bez PODKLADARNA_SHARED_SLOTS se nic nesdílí (jedna instance).
_shared_slots = os.environ.get("PODKLADARNA_SHARED_SLOTS", "").strip()
SHARED_SLOTS_DIR = Path(_shared_slots) if _shared_slots else None
INSTANCE_NAME = os.environ.get("PODKLADARNA_INSTANCE", "").strip()
# stable = veřejná stabilní verze (A); test = testovací verze (B): banner, noindex.
INSTANCE_CHANNEL = os.environ.get("PODKLADARNA_CHANNEL", "stable").strip().lower() or "stable"
# Souhrnná statistika použití (patička, box v hlavičce): auto / 1 = zobrazit (i na testu B,
# s vlastními čísly a označením testu, ať jde ověřit), 0 = nikdy.
_show_usage = os.environ.get("SHOW_USAGE_STATS", "auto").strip().lower()
SHOW_USAGE_STATS = _show_usage not in ("0", "false", "no", "off")
STABLE_URL = os.environ.get("PODKLADARNA_STABLE_URL", "https://podkladarna.kibos.link").rstrip("/")
MAX_QUEUE_SIZE = int(os.environ.get("MAX_QUEUE_SIZE", "10"))
JOB_RETENTION_HOURS = int(os.environ.get("JOB_RETENTION_HOURS", "48"))
JOB_RETENTION_DAYS = int(os.environ.get("JOB_RETENTION_DAYS", "0"))  # legacy; použijte JOB_RETENTION_HOURS
MAX_ACTIVE_JOBS_PER_IP = int(os.environ.get("MAX_ACTIVE_JOBS_PER_IP", "2"))
MAX_JOBS_PER_IP_HOUR = int(os.environ.get("MAX_JOBS_PER_IP_HOUR", "10"))
JOB_TIMEOUT_MINUTES = int(os.environ.get("JOB_TIMEOUT_MINUTES", "90"))
# Strop plochy výřezu pro sprint (1:4000), km². Petr 2026-11-05: max 2 × 2 km (dřív 9 km² od 2026-11-02) –
# sprint na větším území nikdo nepostaví a kresba z DTM je pomalá (~2 min/km² skládání .omap).
SPRINT_MAX_BBOX_AREA_KM2 = float(os.environ.get("SPRINT_MAX_BBOX_AREA_KM2", "4"))
# Strop pro les bez MTBO (1:7500 – 1:15000), km². Petr 2026-11-05: omezit, aby se negenerovaly zbytečně velké
# mapy; 15 km² pokryje A4 v 1:15 000 (~13 km²). Job s MTBO má dál obecný MAX_BBOX_AREA_KM2 (25 km²).
FOREST_MAX_BBOX_AREA_KM2 = float(os.environ.get("FOREST_MAX_BBOX_AREA_KM2", "15"))
# Jak dlouho nejvýš job čeká na sdílený slot (druhá instance má vlastní limit jobu).
SHARED_SLOT_WAIT_MINUTES = int(os.environ.get("SHARED_SLOT_WAIT_MINUTES", str(JOB_TIMEOUT_MINUTES + 15)))
# Od kdy formulář varuje, že odhad doby běhu jobu se blíží limitu (5/6 limitu, pro 90 min → 75 min).
ESTIMATE_WARN_MINUTES = JOB_TIMEOUT_MINUTES * 5 // 6
# Samokalibrace odhadu doby jobu (3.34.0): koeficient zvlášť pro přípravu a skládání map = kvantil ESTIMATE_CALIB_QUANTILE poměru
# skutečné doby k vzorci z posledních ESTIMATE_CALIB_JOBS dokončených jobů za ESTIMATE_CALIB_DAYS dní
# (aspoň ESTIMATE_CALIB_MIN_JOBS poměrů), × rezerva, v mezích MIN–MAX. ESTIMATE_CALIBRATION=0 vypne.
ESTIMATE_CALIBRATION = os.environ.get("ESTIMATE_CALIBRATION", "1").strip().lower() not in (
    "0", "false", "no", "off"
)
ESTIMATE_CALIB_JOBS = 40
ESTIMATE_CALIB_DAYS = 90
ESTIMATE_CALIB_MIN_JOBS = 5
# p90: raději nadhodnotit (doba skládání závisí na hustotě zástavby, kterou vzorec nezná; na datech ostré p80 u lesa 12 km² podstřelil).
ESTIMATE_CALIB_QUANTILE = 0.9
ESTIMATE_CALIB_MIN = 0.25
ESTIMATE_CALIB_MAX = 1.5
ESTIMATE_CALIB_RESERVE = 1.15
if os.environ.get("JOB_TIMEOUT_SECONDS"):
    JOB_TIMEOUT_SECONDS = max(1, int(os.environ["JOB_TIMEOUT_SECONDS"]))
else:
    JOB_TIMEOUT_SECONDS = max(60, JOB_TIMEOUT_MINUTES * 60)
TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", "1").lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def _parse_ip_list(raw: str) -> frozenset[str]:
    return frozenset(p.strip() for p in (raw or "").split(",") if p.strip())


RATE_LIMIT_EXEMPT_IPS = _parse_ip_list(os.environ.get("RATE_LIMIT_EXEMPT_IPS", ""))
# API klient (Stavitel): sdílený klíč v hlavičce X-Api-Key → soukromý job bez e-mailu,
# `download_token` v odpovědi. Prázdné = API klíč vypnutý (hlavička se odmítne 401).
API_CLIENT_KEY = os.environ.get("API_CLIENT_KEY", "").strip()
# Vlastní limit pro joby s klíčem (místo limitu podle IP).
API_KEY_MAX_ACTIVE_JOBS = int(os.environ.get("API_KEY_MAX_ACTIVE_JOBS", "3"))
API_KEY_MAX_JOBS_HOUR = int(os.environ.get("API_KEY_MAX_JOBS_HOUR", "20"))
CLEANUP_INTERVAL_HOURS = int(os.environ.get("CLEANUP_INTERVAL_HOURS", "24"))
# Kontrola dostupnosti zdrojů dat (ČÚZK, DTM, OSM …) při otevření stránky / startu jobu;
# výsledek platí tolik minut (víc návštěvníků = jedna kontrola). 0 = vypnuto.
SOURCE_CHECK_TTL_MIN = float(os.environ.get("SOURCE_CHECK_TTL_MIN", "3") or 0)
# Zdroj mimo provoz nepřetržitě aspoň tolik minut → jeden e-mail na FEEDBACK_TO (a jeden po obnovení).
# 0 = bez e-mailů; test B ve výchozím stavu neposílá (výpadek hlásí ostrá A, ne obě).
SOURCE_ALERT_AFTER_MIN = float(
    os.environ.get("SOURCE_ALERT_AFTER_MIN", "0" if INSTANCE_CHANNEL == "test" else "30") or 0
)
LIDAR_CACHE_MAX_AGE_DAYS = int(os.environ.get("LIDAR_CACHE_MAX_AGE_DAYS", "180"))
# Listy LiDARu (DMR 5G, DMP OK) se podle stáří znovu nestahují: jednou za tolik dní se u ČÚZK ověří
# (HEAD: Last-Modified + velikost), jestli nevyšla novější verze; jinak se list bere z cache.
LIDAR_VERSION_CHECK_DAYS = int(os.environ.get("LIDAR_VERSION_CHECK_DAYS", "30"))
# Strop velikosti cache LiDARu (listy SM5, jejich ořezy, AOI ořezy) v GB; nad ním se mažou nejdéle
# nepoužité listy / výřezy (Petr 2026-11-02: 200–300 GB). 0 = bez limitu.
LIDAR_CACHE_MAX_GB = float(os.environ.get("LIDAR_CACHE_MAX_GB", "250"))
# Souběžná stahování listů LiDARu z ČÚZK (OpenZU). Jedno spojení dává ~2 MB/s, list DMP OK má ~340 MB
# (Most 12 listů 2026-11-02: ~35 min jen čekání na stažení); 3 spojení = mírná zátěž ČÚZK.
LIDAR_DOWNLOAD_WORKERS = max(1, int(os.environ.get("LIDAR_DOWNLOAD_WORKERS", "3")))
ZABAGED_CACHE_MAX_AGE_DAYS = int(os.environ.get("ZABAGED_CACHE_MAX_AGE_DAYS", "30"))
# Ortofoto / OSM / ZTM / katastr / hillshade PNG pro OOM.
REF_CACHE_MAX_AGE_DAYS = int(os.environ.get("REF_CACHE_MAX_AGE_DAYS", "30"))
RUIAN_CACHE_MAX_AGE_DAYS = int(os.environ.get("RUIAN_CACHE_MAX_AGE_DAYS", "30"))
AOPK_CACHE_MAX_AGE_DAYS = int(os.environ.get("AOPK_CACHE_MAX_AGE_DAYS", "30"))
# DEM/DSM/CHM + shade AOI cache (§10).
SURFACES_CACHE_MAX_AGE_DAYS = int(
    os.environ.get("SURFACES_CACHE_MAX_AGE_DAYS", str(LIDAR_CACHE_MAX_AGE_DAYS))
)
# Escape hatch: přeskočit všechny AOI/underlay cache (env nebo options.force_refresh).
FORCE_REFRESH_DEFAULT = os.environ.get("PODKLADARNA_FORCE_REFRESH", "0").lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# SMTP – privátní joby (odkaz ke stažení e-mailem). Prázdný USER/PASSWORD = IP relay.
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
if not SMTP_HOST and _running_in_docker():
    SMTP_HOST = _NAS_DEFAULT_SMTP_HOST
SMTP_PORT = int(os.environ.get("SMTP_PORT", "25"))
SMTP_ENCRYPTION = os.environ.get("SMTP_ENCRYPTION", "starttls").strip().lower()
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "podkladarna@datais.cz").strip()
if not SMTP_FROM and _running_in_docker():
    SMTP_FROM = "podkladarna@datais.cz"
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "OB podklady").strip()
if not SMTP_FROM_NAME and _running_in_docker():
    SMTP_FROM_NAME = "OB podklady"
# Absolutní URL instance (bez koncového /) pro odkazy v e-mailu, např. https://podkladarna.example
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
if not PUBLIC_BASE_URL and _running_in_docker():
    PUBLIC_BASE_URL = _NAS_DEFAULT_PUBLIC_BASE_URL
# Privátní joby: platnost odkazu / mazání artefaktů (hodiny od založení).
PRIVATE_JOB_RETENTION_HOURS = int(
    os.environ.get("PRIVATE_JOB_RETENTION_HOURS", "48")
)
# Interní statistika původu jobů (tabulka job_origin, nikde nevystavená): retence ve dnech.
ORIGIN_RETENTION_DAYS = int(os.environ.get("ORIGIN_RETENTION_DAYS", "365"))
# Zpětná vazba z webu → e-mail vlastníkovi (fallback = kontakt z README/ZIP).
FEEDBACK_TO = (
    os.environ.get("FEEDBACK_TO")
    or os.environ.get("OWNER_EMAIL")
    or "janecek@datais.cz"
).strip()
MAX_FEEDBACK_PER_IP_HOUR = int(os.environ.get("MAX_FEEDBACK_PER_IP_HOUR", "5"))

DEFAULT_OPTIONS = {
    "run_vectors": True,
    "output_png": True,
    # ZIP pro OOM (vektory + .omap). False = jen PNG náhled na webu (legacy API;
    # pipeline stejně vždy balí .omap/ZIP).
    "output_zip": True,
    # Georef GeoTIFF (300 DPI, dlaždice + náhledy) do výstupního ZIPu – GUI checkbox, default off.
    "output_georef": False,
    # Podklady (ortofoto, OSM, ZTM, katastr, hillshade, DMP, DTM): True = stáhnout obrázky do ZIPu,
    # False = online šablony GDAL WMS (výchozí formuláře i API, Petr 2026-11-08).
    "output_references": False,
    # Sentinel-2 jde vždy (main.py: bez výslovné volby True).
    "output_sentinel": True,
    "output_dxf": True,
    "output_zabaged_clean": False,
    # §10: default reuse AOI cache; True / PODKLADARNA_FORCE_REFRESH = přegenerovat.
    # GUI ve <details> Pokročilé (2026-10); běžný uživatel nepotřebuje.
    "force_refresh": False,
    # auto=skála 201.2/206 vs zem 104; symbol_206=vše jako 206 plocha;
    # earth_bank=104; off=přeskočit. (rock_face zrušeno – linie 201 se nepoužívají)
    "kp_cliff_symbol": "auto",
    # greenhigh (m) – výška vegetace pro hustotu LiDAR odrazů.
    "kp_vege_height": 2.0,
    # Citlivost detekce srázů: low | normal | high | very_high (výchozí low = méně srázů).
    "kp_cliff_sensitivity": "low",
    # Objekty OSM: auto = ve sprintu základní sada (lavičky, stoly, rozcestníky, pomníky, kříže, sochy, památné
    # stromy, od 3.23.0 i herní prvky, přístřešky, fitness, ohniště, zábrany), True = vše (navíc lampy a tabule),
    # "none" = žádné (3.19.0).
    # Ploty a zdi z OSM vždy (bez volby). Dřívější kp_osm_benches / _lamps / _playground_equipment
    # čte resolve_osm_objects_mode u starších jobů a API; kp_osm_priority se ignoruje.
    "kp_osm_objects": "auto",
    # Všechny highway=footway jako zpevněný chodník (sprint 501.10).
    # Les/MTBO: chodník = linie 506/834 (ne zpevněná plocha 501.1/529).
    # Statický default = off; při vytváření jobu doplní default_footway_as_sidewalk
    # (sprint / 1:4000 → on, jinak off), pokud formulář hodnotu nepošle.
    "kp_osm_footway_as_sidewalk": False,
    # Zdroj cest: mixed | zabaged | osm (viz path_source v osm_paths.py).
    "path_source": "mixed",
    # Pomocné vrstevnice 103: off | sparse | more (jen kde ukážou tvar navíc).
    "contour_formlines": "off",
    # Vyhlazení vrstevnic: 1 = adaptivní (výchozí, rovina a zástavba víc), 0 = méně vyhlazení (původní).
    "contour_smooth": "1",
    # Tvar hranic porostů: 0 (kostičky 1:1) až 3 (zjednodušit drobnosti do 12 m), výchozí 1.
    "veg_smooth": "1",
    # Zdroj kresby (jedna volba „DTM“ v GUI): od 3.18.0 (Petr 2026-11-06) výchozí DTM všude, kde je
    # k dispozici (sprint, les i MTBO); volba „Podle presetu“ = jen sprint z DTM (2.43.0–3.17.x výchozí).
    "dtm_sprint": True,
    "dtm_forest": True,
    # Sprint: mezery v OSM landuse=residential jako zpevněná 501 – default vypnuto.
    "sprint_residual_paved": False,
    # Max. velikost zbytku do auto .omap: small | medium | large (ZIP má vždy pásma).
    "sprint_residual_size": "small",
    # Ostatní plocha v sídlech do auto .omap: none | small | medium | large.
    # Default small = jen ≤5 ha (současné chování). ZIP má vždy pásma SHP.
    # Volba z GUI i formuláře odebrána (2026-10-31); pipeline parametr zůstává.
    "ostatni_plocha": "small",
    # Ostatní plocha jako 403 (rough open) místo 501 – default vypnuto.
    # GUI skryté (2026-10); API/pipeline flag zůstává.
    "ostatni_plocha_as_403": False,
    # Filtr velikosti/tvaru vegetace (401/406/408/410): default | strict.
    # Sprint vždy default (resolve_veg_size_profile). Přísnější jen opt-in A/B.
    "veg_size_profile": "default",
}


def default_footway_as_sidewalk(
    map_scale: int | float | None = None,
    preset_id: str | None = None,
) -> bool:
    """Sprint (1:4000 / preset ``sprint*``) → footway jako chodník; jinak off."""
    try:
        if map_scale is not None and int(map_scale) == 4000:
            return True
    except (TypeError, ValueError):
        pass
    pid = (preset_id or "").strip().lower()
    return pid.startswith("sprint")
