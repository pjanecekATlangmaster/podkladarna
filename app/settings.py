from __future__ import annotations

import os
from pathlib import Path

from app.tool_env import apply_local_gis_env

APP_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = APP_ROOT / "configs"
APP_VERSION = "4.4.0"

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
# Ukázkové ZIPy na webu (sprint.zip, les.zip, mtbo.zip; anglické *-en.zip) – nahrávají se ručně, ne do image.
SAMPLES_DIR = DATA_ROOT / "samples"
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
# Tolik minut po doběhnutí jobu (done) se smažou mezivýsledky: work/ kromě souhrnů a náhledu a v output/ vše kromě
# ZIPů a náhledu (rozbalené kopie). Log zůstává. Celý job pak smaže JOB_RETENTION_HOURS. 0 = nemazat (Petr 2026-11-20).
JOB_TRIM_AFTER_MINUTES = int(os.environ.get("JOB_TRIM_AFTER_MINUTES", "180") or 0)
MAX_ACTIVE_JOBS_PER_IP = int(os.environ.get("MAX_ACTIVE_JOBS_PER_IP", "2"))
# Petr 2026-11-20: build trvá 5–20 min, 10 za hodinu nikoho nebrzdilo; brzdí denní strop (IP, síť /24 kvůli CGNAT volněji).
MAX_JOBS_PER_IP_HOUR = int(os.environ.get("MAX_JOBS_PER_IP_HOUR", "4"))
MAX_JOBS_PER_IP_DAY = int(os.environ.get("MAX_JOBS_PER_IP_DAY", "15"))  # 0 = vypnuto
MAX_JOBS_PER_NET_DAY = int(os.environ.get("MAX_JOBS_PER_NET_DAY", "40"))  # síť /24 (IPv6 /48); 0 = vypnuto
# Soukromé joby na jeden e-mail za klouzavých 24 h (Petr 2026-11-20, mírný limit proti zneužití); 0 = vypnuto.
# E-mail se počítá normalizovaný (bez +značky, u Gmailu bez teček) a jen jako HMAC značka v job_origin.
MAX_PRIVATE_JOBS_PER_EMAIL_DAY = int(os.environ.get("MAX_PRIVATE_JOBS_PER_EMAIL_DAY", "8"))
# Zablokované e-maily (čárkami; jen v .env na serveru, nikdy v repu). Porovnání po stejné normalizaci jako limit.
BANNED_EMAILS = os.environ.get("BANNED_EMAILS", "")
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
# Zadání jobu mimo Česko: potvrzený výpadek zdroje výšek země (dva neúspěšné dotazy za sebou) job odmítne
# (``app.source_gate``); výsledek kontroly platí tolik minut. SOURCE_GATE=0 vypne.
SOURCE_GATE = os.environ.get("SOURCE_GATE", "1").strip().lower() not in {"0", "false", "no", "off", ""}
SOURCE_GATE_TTL_MIN = float(os.environ.get("SOURCE_GATE_TTL_MIN", "5") or 0)
# Okamžitý e-mail jen o zdrojích, bez kterých job selže, v těchto zemích (čárkami; * = všechny, prázdné = žádné).
# Ostatní problémy zdrojů jdou do souhrnu (Petr 2026-11-20: maily o zahraničních výpadcích nic nezmění).
SOURCE_ALERT_COUNTRIES = os.environ.get("SOURCE_ALERT_COUNTRIES", "CZ")
# Souhrnný e-mail o problémech zdrojů (deník DATA_ROOT/source_events.jsonl) jednou za tolik hodin, jen když
# je co hlásit; 0 = bez souhrnu. Test B ve výchozím stavu neposílá (souhrn hlásí ostrá A).
SOURCE_DIGEST_HOURS = float(
    os.environ.get("SOURCE_DIGEST_HOURS", "0" if INSTANCE_CHANNEL == "test" else "24") or 0
)
# Jak dlouho deník zdrojů drží události (dny).
SOURCE_EVENTS_KEEP_DAYS = int(os.environ.get("SOURCE_EVENTS_KEEP_DAYS", "30") or 0)
# Token pro čtení deníku přes GET /api/source_events (hlavička Authorization: Bearer …);
# prázdný = endpoint neexistuje. Nastavuje se jen v .env serveru, nikdy v repozitáři.
SOURCE_EVENTS_TOKEN = os.environ.get("SOURCE_EVENTS_TOKEN", "").strip()
LIDAR_CACHE_MAX_AGE_DAYS = int(os.environ.get("LIDAR_CACHE_MAX_AGE_DAYS", "180"))
# Listy LiDARu (DMR 5G, DMP OK) se podle stáří znovu nestahují: jednou za tolik dní se u ČÚZK ověří
# (HEAD: Last-Modified + velikost), jestli nevyšla novější verze; jinak se list bere z cache.
LIDAR_VERSION_CHECK_DAYS = int(os.environ.get("LIDAR_VERSION_CHECK_DAYS", "30"))
# Strop velikosti cache LiDARu (listy SM5, jejich ořezy, AOI ořezy) v GB; nad ním se mažou nejdéle
# nepoužité listy / výřezy (Petr 2026-11-02: 200–300 GB). 0 = jen podíl disku (LIDAR_CACHE_MAX_DISK_SHARE).
LIDAR_CACHE_MAX_GB = float(os.environ.get("LIDAR_CACHE_MAX_GB", "250"))
# Strop cache LiDARu nejvýš tento podíl velikosti disku s cache (ostrá A 2026-11-15: 250 GB bylo víc než disk).
# Platí menší z obou; 0 = jen ``LIDAR_CACHE_MAX_GB``.
LIDAR_CACHE_MAX_DISK_SHARE = float(os.environ.get("LIDAR_CACHE_MAX_DISK_SHARE", "0.5") or 0)
# Volné místo před jobem: základ + GB na km² výřezu (listy DMP OK ~350 MB, ořezy, rastry, ortofoto, ZIP).
# Když chybí, smaže se nejdéle nepoužitá cache, jinak job skončí hláškou o plném disku. 0 a 0 = bez kontroly.
DISK_JOB_BASE_GB = float(os.environ.get("DISK_JOB_BASE_GB", "3") or 0)
DISK_JOB_GB_PER_KM2 = float(os.environ.get("DISK_JOB_GB_PER_KM2", "0.5") or 0)
# Při nedostatku místa na disku s joby se smažou mezivýsledky nejstarších hotových jobů a pak celé nejstarší
# hotové joby; job dokončený před méně než tolika hodinami se celý nemaže (uživatel si ZIP teprve stahuje).
DISK_JOB_DELETE_MIN_AGE_HOURS = float(os.environ.get("DISK_JOB_DELETE_MIN_AGE_HOURS", "6") or 0)
# Rozbalené kopie podkladů vedle ZIPu v output/ (shade/, references/, uzitecne/; ~ velikost ZIPu): jen pro
# otevření .omap přímo z output/ při vývoji. 0 = jen ZIP (ostrá A, šetří disk s joby).
OUTPUT_UNPACKED_COPIES = os.environ.get("OUTPUT_UNPACKED_COPIES", "1").strip().lower() not in ("0", "false", "no", "off")
# Rezerva ``DATA_ROOT/.disk_reserve`` (MB): při plném disku se smaže, aby šel zapsat stav jobu „selhalo“.
DISK_RESERVE_MB = float(os.environ.get("DISK_RESERVE_MB", "64") or 0)
# Souběžná stahování listů LiDARu z ČÚZK (OpenZU). Jedno spojení dává ~2 MB/s, list DMP OK má ~340 MB
# (Most 12 listů 2026-11-02: ~35 min jen čekání na stažení); 3 spojení = mírná zátěž ČÚZK.
LIDAR_DOWNLOAD_WORKERS = max(1, int(os.environ.get("LIDAR_DOWNLOAD_WORKERS", "3")))
# Předstažení velkých dat mimo frontu výpočtů (app/prefetch.py, Petr 2026-11-16): job v Česku s aspoň
# PREFETCH_MIN_SHEETS listy LiDARu mimo cache stahuje při obsazeném serveru samostatně s nižší prioritou
# (nejvýš PREFETCH_CONCURRENCY naráz, PREFETCH_DOWNLOAD_WORKERS spojení) a do fronty výpočtů jde až potom.
PREFETCH_ENABLED = os.environ.get("PREFETCH_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
PREFETCH_MIN_SHEETS = max(1, int(os.environ.get("PREFETCH_MIN_SHEETS", "2")))
PREFETCH_CONCURRENCY = max(1, int(os.environ.get("PREFETCH_CONCURRENCY", "1")))
PREFETCH_DOWNLOAD_WORKERS = max(1, int(os.environ.get("PREFETCH_DOWNLOAD_WORKERS", "2")))
PREFETCH_NICE = int(os.environ.get("PREFETCH_NICE", "10"))
PREFETCH_TIMEOUT_MINUTES = max(1, int(os.environ.get("PREFETCH_TIMEOUT_MINUTES", "60")))
ZABAGED_CACHE_MAX_AGE_DAYS = int(os.environ.get("ZABAGED_CACHE_MAX_AGE_DAYS", "30"))
# Slovensko: celostátní budovy a silnice INSPIRE ZBGIS (cache/sk_zbgis, ~6 GB) se smažou po této době bez použití
SK_ZBGIS_CACHE_MAX_AGE_DAYS = int(os.environ.get("SK_ZBGIS_CACHE_MAX_AGE_DAYS", "90"))
# Finsko: ZIPy Maastotietokanta po polovinách listů (cache/fi_mtk, ~10 MB / 144 km²) se smažou po této době bez použití
FI_MTK_CACHE_MAX_AGE_DAYS = int(os.environ.get("FI_MTK_CACHE_MAX_AGE_DAYS", "90"))
# Ortofoto / OSM / ZTM / katastr / hillshade PNG pro OOM.
REF_CACHE_MAX_AGE_DAYS = int(os.environ.get("REF_CACHE_MAX_AGE_DAYS", "30"))
RUIAN_CACHE_MAX_AGE_DAYS = int(os.environ.get("RUIAN_CACHE_MAX_AGE_DAYS", "30"))
AOPK_CACHE_MAX_AGE_DAYS = int(os.environ.get("AOPK_CACHE_MAX_AGE_DAYS", "30"))
# Odpovědi Overpass (cache/osm): job je bere jen do PODKLADARNA_OSM_CACHE_HOURS (24 h), starší soubory
# se mažou po tolika dnech (revize 2026-11-16, dřív se nemazaly nikdy). Nejméně platnost cache + 1 den.
OSM_CACHE_MAX_AGE_DAYS = int(os.environ.get("OSM_CACHE_MAX_AGE_DAYS", "2"))
# Dlaždice podkladové mapy webu (cache/tiles): starší než tolik dní se mažou (stáhnou se znovu čerstvé),
# nad stropem GB nejstarší. 0 = bez limitu.
TILE_CACHE_MAX_AGE_DAYS = int(os.environ.get("TILE_CACHE_MAX_AGE_DAYS", "30"))
TILE_CACHE_MAX_GB = float(os.environ.get("TILE_CACHE_MAX_GB", "2") or 0)
# /tiles: kolik dlaždic mimo cache smí server stáhnout za minutu pro jednu IP a celkem (dlaždice z cache
# bez limitu). Server nemá sloužit jako volný proxy dlaždic OSM. 0 = bez limitu.
TILE_FETCH_PER_IP_MIN = int(os.environ.get("TILE_FETCH_PER_IP_MIN", "300"))
TILE_FETCH_PER_MIN = int(os.environ.get("TILE_FETCH_PER_MIN", "1200"))
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
# Vlastní výšková data zadavatele (Omapic mimo Česko, app/user_sources.py): vypnuto, dokud se nezapne `USER_SOURCES=1`.
USER_SOURCES_ENABLED = os.environ.get("USER_SOURCES", "0").strip().lower() in ("1", "true", "yes", "on")
# Strop obou nahraných GeoTIFFů dohromady v MB (Cloudflare zdarma propustí tělo požadavku do 100 MB).
USER_SOURCE_MAX_MB = int(os.environ.get("USER_SOURCE_MAX_MB", "95"))
# Ukázkové ZIPy (``SAMPLES_DIR``) se přegenerují na serveru samy (app.samples_refresh, Petr 2026-11-19):
# po změně minor verze nebo po SAMPLES_REFRESH_MAX_AGE_DAYS, jen když server nepočítá. V nočním okně
# SAMPLES_REFRESH_HOURS (místní čas, „od-do“) hned, přes den až po SAMPLES_REFRESH_IDLE_MIN minutách bez
# nového jobu (0 = jen v noci). SAMPLES_REFRESH=0 vypne.
SAMPLES_REFRESH = os.environ.get("SAMPLES_REFRESH", "1").strip().lower() not in {"0", "false", "off", "no"}
SAMPLES_REFRESH_HOURS = os.environ.get("SAMPLES_REFRESH_HOURS", "1-5").strip()
SAMPLES_REFRESH_IDLE_MIN = int(os.environ.get("SAMPLES_REFRESH_IDLE_MIN", "60"))
SAMPLES_REFRESH_MAX_AGE_DAYS = int(os.environ.get("SAMPLES_REFRESH_MAX_AGE_DAYS", "30"))
SAMPLES_REFRESH_TZ = os.environ.get("SAMPLES_REFRESH_TZ", "Europe/Prague").strip()
# Joby ukázek zakládá server sám sobě přes POST /api/jobs (stejné volby jako z webu).
SAMPLES_REFRESH_URL = os.environ.get("SAMPLES_REFRESH_URL", "http://127.0.0.1:8672").rstrip("/")
# Interní statistika původu jobů (tabulka job_origin, nikde nevystavená): retence ve dnech.
ORIGIN_RETENTION_DAYS = int(os.environ.get("ORIGIN_RETENTION_DAYS", "365"))
# Prodej map na doméně v režimu paid (app.orders, app.payments; návrh platby-navrh.md).
# Prázdné = vypnuto (výchozí): placená doména se chová jako dosud. „fake“ = testovací poskytovatel
# bez skutečné platby (jen vývoj a test B); skuteční poskytovatelé přibudou po rozhodnutí Petra.
PAYMENTS_PROVIDER = os.environ.get("PAYMENTS_PROVIDER", "").strip().lower()
# Pojistka: „fake“ (platba zdarma jedním klikem) se zapne jen s PAYMENTS_FAKE_ALLOWED=1 – nikdy na ostré.
PAYMENTS_FAKE_ALLOWED = os.environ.get("PAYMENTS_FAKE_ALLOWED", "").strip() == "1"
# Cena jedné objednávky v nejmenších jednotkách měny (1000 = 10,00 €), včetně DPH.
PAID_PRICE_CENTS = int(os.environ.get("PAID_PRICE_CENTS", "1000"))
PAID_CURRENCY = os.environ.get("PAID_CURRENCY", "EUR").strip().upper() or "EUR"
# Platnost odkazu „potvrdit e-mail a spustit výpočet“ (hodiny).
ORDER_CONFIRM_HOURS = int(os.environ.get("ORDER_CONFIRM_HOURS", "24"))
# Nezaplacená objednávka: náhled, ukázka a odkaz na nákup platí N hodin od potvrzení, pak se smaže.
UNPAID_ORDER_RETENTION_HOURS = int(os.environ.get("UNPAID_ORDER_RETENTION_HOURS", "168"))
# Zaplacená objednávka: odkaz ke stažení plné mapy platí N dní od zaplacení.
PAID_DOWNLOAD_DAYS = int(os.environ.get("PAID_DOWNLOAD_DAYS", "30"))
# Nejvýš tolik rozpracovaných nezaplacených objednávek (čeká na potvrzení / počítá se / náhled) na e-mail.
MAX_UNPAID_ORDERS_PER_EMAIL = int(os.environ.get("MAX_UNPAID_ORDERS_PER_EMAIL", "1"))
# Potvrzovací e-maily objednávek (chrání SMTP a cizí schránky): nejvýš N za hodinu z jedné IP
# a celkem za hodinu na instanci (výjimky RATE_LIMIT_EXEMPT_IPS platí i tady).
ORDER_MAILS_PER_IP_HOUR = int(os.environ.get("ORDER_MAILS_PER_IP_HOUR", "3"))
ORDER_MAILS_PER_HOUR = int(os.environ.get("ORDER_MAILS_PER_HOUR", "60"))
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
    # Pomocné vrstevnice 103: auto (podle země, countries.yaml ``contour_formlines``) | off | sparse | more | all.
    "contour_formlines": "auto",
    # Vyhlazení vrstevnic: auto = podle země (výchozí; Česko adaptivní, sever méně, countries.yaml ``contour_smooth``),
    # 1 = adaptivní (rovina a zástavba víc), 0 = méně vyhlazení (původní).
    "contour_smooth": "auto",
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
