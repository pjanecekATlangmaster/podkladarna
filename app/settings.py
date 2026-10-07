from __future__ import annotations

import os
from pathlib import Path

from app.tool_env import apply_local_gis_env

APP_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = APP_ROOT / "configs"
APP_VERSION = "2.3.1"

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
MAX_QUEUE_SIZE = int(os.environ.get("MAX_QUEUE_SIZE", "10"))
JOB_RETENTION_HOURS = int(os.environ.get("JOB_RETENTION_HOURS", "48"))
JOB_RETENTION_DAYS = int(os.environ.get("JOB_RETENTION_DAYS", "0"))  # legacy; použijte JOB_RETENTION_HOURS
MAX_ACTIVE_JOBS_PER_IP = int(os.environ.get("MAX_ACTIVE_JOBS_PER_IP", "2"))
MAX_JOBS_PER_IP_HOUR = int(os.environ.get("MAX_JOBS_PER_IP_HOUR", "10"))
JOB_TIMEOUT_MINUTES = int(os.environ.get("JOB_TIMEOUT_MINUTES", "90"))
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
TEMP_RETENTION_DAYS = int(os.environ.get("TEMP_RETENTION_DAYS", "7"))
CLEANUP_INTERVAL_HOURS = int(os.environ.get("CLEANUP_INTERVAL_HOURS", "24"))
LIDAR_CACHE_MAX_AGE_DAYS = int(os.environ.get("LIDAR_CACHE_MAX_AGE_DAYS", "180"))
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
    # Georef PNG+PGW (±GeoTIFF) do výstupního ZIPu – GUI checkbox, default off.
    "output_georef": False,
    # Ortofoto / OSM / ZTM / katastr / hillshade / DMP – stahovat a dát do ZIPu.
    "output_references": True,
    "output_dxf": True,
    "output_zabaged_clean": False,
    "savetempfolders": False,  # budoucí expert režim / API iterace
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
    # OSM volitelné objekty – default vypnuto (rozšířená nastavení).
    "kp_osm_benches": False,
    "kp_osm_lamps": False,
    "kp_osm_playground_equipment": False,
    # Priorita OSM (urban pack) – dočasně default zapnuto kvůli testování.
    "kp_osm_priority": True,
    # Všechny highway=footway jako zpevněný chodník (sprint 501.6).
    # Les/MTBO: chodník = linie 506/834 (ne zpevněná plocha 501.1/529).
    # Statický default = off; při vytváření jobu doplní default_footway_as_sidewalk
    # (sprint / 1:4000 → on, jinak off), pokud formulář hodnotu nepošle.
    "kp_osm_footway_as_sidewalk": False,
    # Zdroj cest: mixed | zabaged | osm (viz path_source v osm_paths.py).
    "path_source": "mixed",
    # Sprint: nepřístupné dvory uvnitř budov (díry v 521) vyplnit olivou 520.
    "sprint_courtyard_olive": True,
    # Sprint: mezery v OSM landuse=residential jako zpevněná 501 – default vypnuto.
    "sprint_residual_paved": False,
    # Max. velikost zbytku do auto .omap: small | medium | large (ZIP má vždy pásma).
    "sprint_residual_size": "small",
    # Ostatní plocha v sídlech do auto .omap: none | small | medium | large.
    # Default small = jen ≤5 ha (současné chování). ZIP má vždy pásma SHP.
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
