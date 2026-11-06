"""Presety map a čtení voleb jobu z formuláře / API."""

from __future__ import annotations

import yaml

from app.settings import CONFIG_DIR

# greenhigh (m) – body nad touto výškou jdou do „high“ vegetace.
# Volba ve formuláři / API: kp_vege_height (prefix je historický, ne runtime KP).
KP_VEGE_HEIGHT_CHOICES = (1.5, 2.0, 2.5, 3.0)
KP_VEGE_HEIGHT_DEFAULT = 2.0

# cliff1/cliff2 – min. výškový skok; nižší = citlivější (více srázů).
# Výchozí „low“: ještě vyšší práh proti falešným zemním srázům (kartografie).
# „normal“ = dřívější low (1,8 / 3,4). Citlivější „high“ = 1,4 / 2,8.
KP_CLIFF_SENSITIVITY: dict[str, tuple[float, float]] = {
    "low": (2.2, 4.0),
    "normal": (1.8, 3.4),
    "high": (1.4, 2.8),
    "very_high": (1.15, 2.0),
}
KP_CLIFF_SENSITIVITY_DEFAULT = "low"

# Pomocné vrstevnice 103 (půl ekvidistance): sparse/more jen kde ukážou tvar
# navíc, all = všude jako vodítko (jako OCAD z LAZ).
FORMLINE_MODES = ("off", "sparse", "more", "all")
FORMLINE_MODE_DEFAULT = "off"


# Tvar hranic porostů 401–410 v lese a MTBO (sprint beze změny, viz veg_smooth):
# 0 = kostičky 1:1 z rastru, 1 = vyhladit schodky bez posunu hranice (do ~1,5 m),
# 2 = navíc zjednodušit drobné výběžky a zátoky do 6 m, 3 = do 12 m.
# Plocha se nikdy nepřelije přes cesty, budovy a vodu z OSM.
VEG_SMOOTH_LEVELS = (0, 1, 2, 3)
VEG_SMOOTH_DEFAULT = 1
# Starý dvouhodnotový přepínač (off / round).
_VEG_SMOOTH_LEGACY = {"off": 0, "round": 1}


def resolve_veg_smooth(options: dict | None) -> int:
    raw = str((options or {}).get("veg_smooth", "")).strip().lower()
    if raw in _VEG_SMOOTH_LEGACY:
        return _VEG_SMOOTH_LEGACY[raw]
    try:
        level = int(raw)
    except ValueError:
        return VEG_SMOOTH_DEFAULT
    return level if level in VEG_SMOOTH_LEVELS else VEG_SMOOTH_DEFAULT


# Vyhlazení vrstevnic (contour_smooth, viz contours_adaptive): 0 = méně vyhlazení
# (původní chování do 3.6.x), 1 = adaptivní (víc vyhladit rovinu a zástavbu, členitý
# terén bit po bitu jako 0). Od 3.7.4 je výchozí 1 (Petr 2026-11-02), i pro API a
# starší joby bez volby.
CONTOUR_SMOOTH_LEVELS = (0, 1)
CONTOUR_SMOOTH_DEFAULT = 1


def resolve_contour_smooth(options: dict | None) -> int:
    """Stupeň vyhlazení vrstevnic; neplatná nebo chybějící hodnota = výchozí."""
    raw = str((options or {}).get("contour_smooth", "")).strip().lower()
    try:
        level = int(raw)
    except ValueError:
        return CONTOUR_SMOOTH_DEFAULT
    return level if level in CONTOUR_SMOOTH_LEVELS else CONTOUR_SMOOTH_DEFAULT


def _as_bool(raw, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on", "dtm"}:
        return True
    if text in {"0", "false", "no", "off", "osm"}:
        return False
    return default


# Další objekty OSM (lavičky, lampy, herní prvky, brány, pomníky, přístřešky, fitness…): jedna volba
# `kp_osm_objects` (Petr 2026-11-02, nahradila „OSM detaily“ `kp_osm_priority` a tři dílčí volby).
# Ploty, zdi a živé ploty z OSM se načítají vždy, na volbě nezávisí.
OSM_OBJECTS_LEGACY_KEYS = (
    "kp_osm_benches",
    "kp_osm_lamps",
    "kp_osm_playground_equipment",
    "kp_osm_furniture",
)


# Režim objektů OSM (3.19.0, Petr 2026-11-06): „auto“ = výchozí základní sada ve sprintu (lavičky, stoly,
# rozcestníky, pomníky, kříže, sochy, památné stromy; les a MTBO nic), „1“ = vše (navíc lampy, tabule, herní
# prvky, brány, přístřešky, fitness, ohniště), „none“ = žádné. Starší joby / API s `False` / „0“ = auto (byl to výchozí stav).
OSM_OBJECTS_AUTO = "auto"
OSM_OBJECTS_ALL = "1"
OSM_OBJECTS_NONE = "none"


def resolve_osm_objects_mode(options: dict | None) -> str:
    """`auto` | `1` | `none` podle `kp_osm_objects`; starší joby / API: kterákoli z dřívějších dílčích voleb = vše."""
    opts = options or {}
    raw = opts.get("kp_osm_objects")
    if raw is None:
        return OSM_OBJECTS_ALL if any(_as_bool(opts.get(k), False) for k in OSM_OBJECTS_LEGACY_KEYS) else OSM_OBJECTS_AUTO
    text = str(raw).strip().lower()
    if text in {"none", "off_all", "zadne"}:
        return OSM_OBJECTS_NONE
    if text in {"auto", "basic", ""}:
        return OSM_OBJECTS_AUTO
    return OSM_OBJECTS_ALL if _as_bool(raw, False) else OSM_OBJECTS_AUTO


def resolve_osm_objects(options: dict | None) -> bool:
    """Kreslit všechny další objekty OSM (lampy, tabule, herní prvky, brány…)? `kp_osm_objects` = „1“.

    Starý `kp_osm_priority` se ignoruje (byl ve výchozím stavu zapnutý a nesl hlavně ploty, které jsou teď vždy).
    """
    return resolve_osm_objects_mode(options) == OSM_OBJECTS_ALL


def resolve_osm_basic_objects(options: dict | None, preset_id: str) -> bool:
    """Základní sada objektů OSM (lavičky, rozcestníky, pomníky, památné stromy…): ve sprintu výchozí, jinde jen s „vše“."""
    mode = resolve_osm_objects_mode(options)
    if mode == OSM_OBJECTS_NONE:
        return False
    return mode == OSM_OBJECTS_ALL or str(preset_id).startswith("sprint")


def resolve_use_dtm(options: dict | None, preset_id: str) -> bool:
    """Kreslit preset z DTM? Sprint: `dtm_sprint` (výchozí ano); les a MTBO: `dtm_forest` (výchozí ano od 3.18.0,
    Petr 2026-11-06; 2.43.0–3.17.x ne; GUI má jednu volbu `dtm`, která nastaví obě)."""
    opts = options or {}
    if str(preset_id).startswith("sprint"):
        return _as_bool(opts.get("dtm_sprint"), True)
    return _as_bool(opts.get("dtm_forest"), True)


def load_presets() -> dict:
    return yaml.safe_load((CONFIG_DIR / "presets.yaml").read_text(encoding="utf-8"))


def resolve_vege_height(options: dict | None) -> float:
    raw = (options or {}).get("kp_vege_height", KP_VEGE_HEIGHT_DEFAULT)
    try:
        h = float(raw)
    except (TypeError, ValueError):
        return KP_VEGE_HEIGHT_DEFAULT
    for choice in KP_VEGE_HEIGHT_CHOICES:
        if abs(h - choice) < 1e-6:
            return choice
    return KP_VEGE_HEIGHT_DEFAULT


def resolve_cliff_sensitivity(options: dict | None) -> str:
    raw = str((options or {}).get("kp_cliff_sensitivity") or KP_CLIFF_SENSITIVITY_DEFAULT)
    key = raw.strip().lower()
    if key in KP_CLIFF_SENSITIVITY:
        return key
    return KP_CLIFF_SENSITIVITY_DEFAULT


def resolve_formline_mode(options: dict | None) -> str:
    raw = str((options or {}).get("contour_formlines") or "").strip().lower()
    return raw if raw in FORMLINE_MODES else FORMLINE_MODE_DEFAULT


# Typy map v lesních měřítkách (1:7500 / 1:10000 / 1:15000): job staví jen vybrané.
# Sprint (1:4000) je vždy jedna mapa a typ nevolí.
MAP_TYPES = ("les", "mtbo")


def default_map_types(map_scale) -> list[str]:
    """Výchozí typy map: 1:15000 jen MTBO, jinak jen les."""
    try:
        scale = int(float(map_scale))
    except (TypeError, ValueError):
        scale = 0
    return ["mtbo"] if scale == 15000 else ["les"]


def parse_map_types(raw_values: list[str]) -> list[str]:
    """Hodnoty formuláře (i „les,mtbo“) → platné typy v pořadí MAP_TYPES, bez duplikátů."""
    found: set[str] = set()
    for raw in raw_values or []:
        for part in str(raw).split(","):
            item = part.strip().lower()
            if item in MAP_TYPES:
                found.add(item)
    return [t for t in MAP_TYPES if t in found]


def resolve_map_types(options: dict | None) -> list[str] | None:
    """Typy map z voleb jobu; None = všechny disciplíny (starší joby bez klíče)."""
    raw = (options or {}).get("map_types")
    if not isinstance(raw, (list, tuple)):
        return None
    types = parse_map_types([str(x) for x in raw])
    return types or None
