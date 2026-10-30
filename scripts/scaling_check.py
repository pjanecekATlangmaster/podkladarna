#!/usr/bin/env python3
"""Škálovací test skládání mapy: náročnost nesmí růst rychleji než lineárně s plochou.

Na běžící instanci (typicky ``dev.ps1 up``, http://localhost:8672) založí joby na vnořených čtvercích kolem
jednoho středu (výchozí 0,5 / 1 / 2 km²), změří čas skládání map (řádky ``OOM: … hotovo za m:ss`` v logu jobu)
a z log-log fitu spočítá exponent růstu. Kvadratický žrout (sprint 3.1.0: ořez každé čáry o zónu celé mapy)
se ukáže už mezi 0,5 a 2 km². Test končí chybou, když exponent překročí ``--max-exponent`` nebo když by další
území nestihlo celkový limit ``--budget-minutes``.

Použití:
  python scripts/scaling_check.py                                 # les + MTBO 1:10 000 s DTM, Barrandov
  python scripts/scaling_check.py --preset sprint_2m --map-scale 4000
  python scripts/scaling_check.py --areas 0.5,1,2,4 --budget-minutes 30
"""
from __future__ import annotations

import argparse
import math
import re
import sys
import time

import httpx

# Barrandov (Retest 6 / DS26), zástavba i les s DTM.
DEFAULT_CENTER = "14.3691,50.0345"
_MAP_DONE = re.compile(r"OOM: (.+?) hotovo za (?:(\d+):)?(\d+):(\d{2})")
_DONE_STATES = ("done", "done_with_errors", "failed")


def square_bbox(lon: float, lat: float, area_km2: float) -> str:
    """Čtverec ``area_km2`` kolem středu jako ``west,south,east,north`` (WGS84)."""
    half_km = math.sqrt(area_km2) / 2
    dlat = half_km / 111.32
    dlon = half_km / (111.32 * math.cos(math.radians(lat)))
    return f"{lon - dlon:.6f},{lat - dlat:.6f},{lon + dlon:.6f},{lat + dlat:.6f}"


def fit_exponent(points: list[tuple[float, float]]) -> float:
    """Sklon přímky log(čas) ~ log(plocha) metodou nejmenších čtverců."""
    xs = [math.log(a) for a, _ in points]
    ys = [math.log(max(t, 1.0)) for _, t in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx > 0 else 0.0


def run_job(client: httpx.Client, data: dict, deadline: float) -> tuple[float, float, dict[str, float]] | None:
    """Založí job a čeká na konec: ``(celkový čas, čas skládání map, čas po mapách)``; None při vypršení limitu."""
    r = client.post("/api/jobs", data=data)
    if r.status_code != 200:
        print(r.text[:2000], file=sys.stderr)
        raise SystemExit(1)
    job_id = r.json()["id"]
    print(f"   job {job_id}")
    t0 = time.time()
    last_after = 0
    maps: dict[str, float] = {}
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        for line in client.get(f"/api/jobs/{job_id}/log", params={"after": last_after}).json().get("lines", []):
            last_after = line["id"]
            m = _MAP_DONE.search(line["line"])
            if m:
                h, mi, s = int(m.group(2) or 0), int(m.group(3)), int(m.group(4))
                maps[m.group(1)] = h * 3600 + mi * 60 + s
                print(f"   {m.group(1)}: {maps[m.group(1)]:.0f} s")
        if job["status"] in _DONE_STATES:
            if job["status"] == "failed" or not maps:
                print(f"   job skončil {job['status']} bez složené mapy: {job.get('error') or ''}", file=sys.stderr)
                raise SystemExit(1)
            return time.time() - t0, sum(maps.values()), maps
        time.sleep(5)
    print(f"   celkový limit vypršel, job {job_id} běží dál (zruš ho ručně)", file=sys.stderr)
    return None


def main() -> int:
    p = argparse.ArgumentParser(description="Škálovací test skládání mapy na vnořených územích")
    p.add_argument("--base", default="http://127.0.0.1:8672", help="Base URL služby")
    p.add_argument("--center", default=DEFAULT_CENTER, help="Střed lon,lat (WGS84)")
    p.add_argument("--areas", default="0.5,1,2", help="Plochy v km², vzestupně (nejvýš 4)")
    p.add_argument("--preset", default="forest_10000", help="Preset ID")
    p.add_argument("--map-scale", default="10000", help="Měřítko (10000 = les + MTBO, 4000 = sprint)")
    p.add_argument("--dtm", default="1", help="Volba DTM jobu (auto / 1 / 0)")
    p.add_argument("--budget-minutes", type=float, default=20.0, help="Celkový limit testu")
    p.add_argument("--max-exponent", type=float, default=1.3, help="Nejvyšší přípustný exponent růstu")
    args = p.parse_args()
    lon, lat = (float(v) for v in args.center.split(","))
    areas = sorted(float(a) for a in args.areas.split(","))
    if len(areas) < 2 or len(areas) > 4:
        print("Zadej 2 až 4 plochy.", file=sys.stderr)
        return 1
    start = time.time()
    deadline = start + args.budget_minutes * 60
    points: list[tuple[float, float]] = []
    with httpx.Client(base_url=args.base.rstrip("/"), timeout=120.0) as client:
        client.get("/api/health").raise_for_status()
        for i, area in enumerate(areas):
            if points:
                # Další území se pustí, jen když ho stihneme i při mezním exponentu (stahování ~ plocha).
                a0, _ = points[-1]
                guess = last_total * (area / a0) ** args.max_exponent
                if time.time() + guess > deadline:
                    print(f"{area} km² by se do limitu nevešlo (odhad {guess / 60:.1f} min), končím.")
                    break
            bbox = square_bbox(lon, lat, area)
            print(f"{i + 1}/{len(areas)} {area} km², bbox {bbox}")
            data = {
                "name": f"scaling-{area}km2",
                "preset_id": args.preset,
                "map_scale": args.map_scale,
                "dtm": args.dtm,
                "bbox": bbox,
            }
            res = run_job(client, data, deadline)
            if res is None:
                print("FAIL: celkový limit vypršel uprostřed jobu.")
                return 2
            last_total, map_s, _maps = res
            print(f"   job {last_total / 60:.1f} min, skládání map {map_s:.0f} s")
            points.append((area, map_s))
    if len(points) < 2:
        print("FAIL: do limitu se vešlo méně než dvě území, exponent nejde spočítat.")
        return 2
    exp = fit_exponent(points)
    # Poslední dvojice je nejcitlivější: u malých území fit tlumí pevná režie mapy.
    tail = fit_exponent(points[-2:])
    print("\nplocha km²  skládání s  s/km²")
    for a, t in points:
        print(f"{a:10.2f}  {t:11.0f}  {t / a:6.0f}")
    print(f"exponent růstu {exp:.2f}, mezi posledními dvěma {tail:.2f} (limit {args.max_exponent}), celkem {(time.time() - start) / 60:.1f} min")
    if max(exp, tail) > args.max_exponent:
        print("FAIL: skládání roste rychleji než lineárně, hledej ořez / dotaz proti zóně celé mapy.")
        return 3
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
