"""Tagy z technické mapy (DTM ČÚZK) pro volbu ``osm_tags`` – prvky k párování nad hotovou mapou.

Volba ``osm_tags`` (výchozí vypnuto) páruje prvky zdrojů s objekty hotové mapy podle tvaru (``osm_tags``).
Tento modul dodá prvky DTM: vybrané typy objektů z balíků ZPS_KR (čáry) a ZPS_OPL (plochy, obrys jako čára)
z cache stažených balíků jobu (``dtm/fences.json`` → ``opl_files``, ZPS_KR leží vedle se stejným jménem).
Přípravu DTM ani kresbu nemění; čte se až při zápisu mapy a jen se zapnutou volbou.

Tag prvku DTM:

* sjednocené tagy ve slovníku OSM (``barrier=fence``, ``building=yes``, ``surface=asphalt`` …), aby stejný
  objekt z OSM i z DTM nesl stejné klíče (sloučení a konflikty řeší ``osm_tags.attach_osm_tags``),
* ``dtm:typ`` (typ objektu JVF, např. ``Plot``), ``dtm:id`` a atributy JVF jako ``dtm:<Atribut>`` (kódy beze změny;
  bez správních údajů o záznamu jako editor nebo ID změny).
"""

from __future__ import annotations

import json
from pathlib import Path

SOURCE = "dtm"
# Cache prvků u balíku (všechny vybrané typy, celé území balíku); změna výběru / tagů = nová přípona.
CACHE_SUFFIX = ".tags2.json"

# Kódy ``PrevazujiciPovrch`` (viz dtm_sidewalks.DRIVEWAY_TYPES) → OSM ``surface``.
SURFACE = {"1": "asphalt", "2": "concrete", "3": "paving_stones", "4": "compacted", "5": "sand", "6": "gravel", "7": "unpaved"}
# ``DruhPlotu``: 1 dřevěný, 2 drátěný, 3 kovový, 4 zděný, 5 živý, 98 jiný, 99 nezjištěno.
FENCE_TYPE = {"1": "wood", "2": "chain_link", "3": "metal"}
# ``TypZemedelskePlochy``: 1 orná půda, 2 vinice, 3 chmelnice, 4 ovocný sad, 5 TTP.
FARMLAND = {"1": {"landuse": "farmland"}, "2": {"landuse": "vineyard"}, "3": {"landuse": "farmland", "crop": "hop"},
            "4": {"landuse": "orchard"}, "5": {"landuse": "meadow"}}

# Typ objektu JVF → sjednocené tagy. Jen typy, které mapa kreslí (nebo jejich protějšek z OSM): obrysové čáry
# ploch, hranice dopravních ploch, definiční body ap. se nepárují (seděly by na cizí objekty).
KR_LINE_TYPES: dict[str, dict] = {
    "Plot": {"barrier": "fence"},
    "ZedLinie": {"barrier": "wall"},
}
OPL_AREA_TYPES: dict[str, dict] = {
    "BudovaPlocha": {"building": "yes"},
    "OstatniZastresenaStavbaPlocha": {"building": "roof"},
    "ZastreseniPlocha": {"building": "roof"},
    "SklenikPlocha": {"building": "greenhouse"},
    "ZedPlocha": {"barrier": "wall"},
    "ChodnikPlocha": {"area:highway": "footway"},
    "ProvozniPlochaPozemniKomunikacePlocha": {"area:highway": "road"},
    "ManipulacniPlochaPlocha": {"area:highway": "service"},
    "NajezdPlocha": {"area:highway": "service", "service": "driveway"},
    "ParkovisteOdstavnaPlochaPlocha": {"amenity": "parking"},
    "SchodistePlocha": {"area:highway": "steps"},
    "DopravniOstruvekPlocha": {"area:highway": "traffic_island"},
    "NastupistePlocha": {"public_transport": "platform"},
    "StavbaProZpevneniPovrchuPlocha": {},
    "PlochaMostniKonstrukcePlocha": {"man_made": "bridge"},
    "UdrzovanaPlochaZelenePlocha": {"landuse": "grass"},
    "ZahradaPlocha": {"leisure": "garden"},
    "DvurNadvoriPlocha": {},
    "LesPlocha": {"landuse": "forest"},
    "ZemedelskaPlochaPlocha": {},
    "HospodarskyNevyuzivanaPlochaPlocha": {"landuse": "brownfield"},
    "ZahradniBazenPlocha": {"leisure": "swimming_pool"},
    "VodniTokPlocha": {"natural": "water", "water": "river"},
    "SouhrnnaPlochaZeleznicnichDrahPlocha": {"landuse": "railway"},
    "TramvajovaDrahaPlocha": {"landuse": "railway"},
}
# Správní údaje záznamu (kdo a kdy vkládal) se do tagů nepíšou; ID a úroveň mají vlastní klíč.
SKIP_ATTRS = frozenset({
    "ID", "IDZmeny", "IDEditora", "VkladOsoba", "ZmenaOsoba", "ICS", "DatumVkladu", "UrovenUmisteniObjektuZPS",
    "TridaPresnostiVyska",
})
EMPTY_VALUES = frozenset({"", "n/a", "N/A"})
PLACEHOLDER_YEAR = "1900"


def dtm_tags(type_name: str, attrs: dict) -> dict:
    """Tagy jednoho záznamu: sjednocené klíče, pak ``dtm:typ``, ``dtm:id`` a ``dtm:<Atribut>``."""
    base = dict(KR_LINE_TYPES.get(type_name) or OPL_AREA_TYPES.get(type_name) or {})
    if type_name == "Plot":
        druh = str(attrs.get("DruhPlotu") or "").strip()
        if druh == "4":
            base = {"barrier": "wall"}
        elif druh == "5":
            base = {"barrier": "hedge"}
        elif druh in FENCE_TYPE:
            base["fence_type"] = FENCE_TYPE[druh]
    elif type_name == "ZemedelskaPlochaPlocha":
        base.update(FARMLAND.get(str(attrs.get("TypZemedelskePlochy") or "").strip(), {}))
    surf = SURFACE.get(str(attrs.get("PrevazujiciPovrch") or "").strip())
    if surf:
        base["surface"] = surf
    try:
        level = int(float(attrs.get("UrovenUmisteniObjektuZPS") or 0))
    except ValueError:
        level = 0
    if level:
        base["layer"] = str(level)
    out = {**base, "dtm:typ": type_name}
    oid = str(attrs.get("ID") or "").strip()
    if oid:
        out["dtm:id"] = oid
    for k in sorted(attrs):
        v = str(attrs[k] or "").strip()
        if k in SKIP_ATTRS or v in EMPTY_VALUES:
            continue
        if k.startswith("Datum"):
            v = v[:10]
            if v.startswith(PLACEHOLDER_YEAR):  # 1900-01-01 = datum nezjištěno (stará migrace)
                continue
        out[f"dtm:{k}"] = v
    return out


def _package_records(package: Path, types: dict, geom: str) -> list[dict]:
    """Prvky vybraných typů z jednoho balíku (cache u balíku); ``lines`` = čáry / obrysy ploch v S-JTSK."""
    from app.pipeline.jvf_reader import iter_records

    cache = package.with_name(package.name + CACHE_SUFFIX)
    try:
        if cache.is_file() and cache.stat().st_mtime >= package.stat().st_mtime:
            return json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    out: list[dict] = []
    for rec in iter_records(package, set(types)):
        if rec.geom_type != geom or not rec.coords or rec.level < 0:
            continue
        if geom == "LineString":
            lines = [[[float(c[0]), float(c[1])] for c in rec.coords]]
        else:
            lines = [[[float(c[0]), float(c[1])] for c in ring] for ring in rec.coords if len(ring) >= 4]
        lines = [ln for ln in lines if len(ln) >= 2]
        if not lines:
            continue
        tags = dtm_tags(rec.type_name, rec.attrs)
        out.append({"id": f"dtm/{tags.get('dtm:id') or f'{package.name}#{len(out)}'}", "src": SOURCE, "tags": tags, "lines": lines})
    try:
        from app.fsutil import atomic_write_text

        atomic_write_text(cache, json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception:  # cache je jen zrychlení
        pass
    return out


def dtm_packages(work_dir: Path) -> tuple[list[Path], list[Path]]:
    """(ZPS_KR, ZPS_OPL) balíky jobu podle ``dtm/fences.json``; prázdné bez DTM nebo s DTM vypnutým pro mapu."""
    from app.pipeline.dtm_import import fences_path

    src = fences_path(Path(work_dir))
    if not src.is_file():
        return [], []
    try:
        opl = [Path(p) for p in (json.loads(src.read_text(encoding="utf-8")).get("opl_files") or [])]
    except (OSError, ValueError, AttributeError):
        return [], []
    opl = [p for p in opl if p.is_file()]
    kr = [p.with_name(p.name.replace("_ZPS_OPL.", "_ZPS_KR.")) for p in opl]
    return [p for p in kr if p.is_file() and p not in opl], opl


def dtm_records(work_dir: Path, bounds: tuple[float, float, float, float] | None = None) -> list[dict]:
    """Prvky DTM zasahující do ``bounds`` (S-JTSK xmin, ymin, xmax, ymax); prázdné bez DTM."""
    kr, opl = dtm_packages(work_dir)
    out: list[dict] = []
    seen: set[str] = set()
    for pkgs, types, geom in ((kr, KR_LINE_TYPES, "LineString"), (opl, OPL_AREA_TYPES, "Polygon")):
        for pkg in pkgs:
            for rec in _package_records(pkg, types, geom):
                if bounds is not None:
                    xs = [p[0] for ln in rec["lines"] for p in ln]
                    ys = [p[1] for ln in rec["lines"] for p in ln]
                    if max(xs) < bounds[0] or min(xs) > bounds[2] or max(ys) < bounds[1] or min(ys) > bounds[3]:
                        continue
                if rec["id"] in seen:  # objekt na hranici obcí je v obou balících
                    continue
                seen.add(rec["id"])
                out.append(rec)
    return out
