#!/usr/bin/env python3
"""End-to-end smoke test proti běžící instanci (typicky docker compose dev).

Použití:
  python scripts/smoke_e2e.py
  python scripts/smoke_e2e.py --wait-minutes 30
  python scripts/smoke_e2e.py --bbox 14.40,50.08,14.42,50.09 --preset sprint_2m
"""
from __future__ import annotations

import argparse
import sys
import time

import httpx

DEFAULT_BBOX = "14.40,50.08,14.42,50.09"


def main() -> int:
    p = argparse.ArgumentParser(description="Podkladárna E2E smoke test")
    p.add_argument("--base", default="http://127.0.0.1:8672", help="Base URL služby")
    p.add_argument("--bbox", default=DEFAULT_BBOX, help="Výřez WGS84 west,south,east,north")
    p.add_argument("--preset", default="sprint_2m", help="Preset ID")
    p.add_argument("--wait-minutes", type=int, default=0, help="Po založení čekat na done (0 = jen enqueue)")
    args = p.parse_args()
    base = args.base.rstrip("/")

    with httpx.Client(base_url=base, timeout=3600.0) as client:
        print("1/4 health …")
        r = client.get("/api/health")
        r.raise_for_status()
        print("   ", r.json())

        print("2/4 presets …")
        r = client.get("/api/presets")
        r.raise_for_status()
        presets = r.json()
        preset_id = args.preset if args.preset in presets else next(iter(presets))
        print("   preset:", preset_id)

        print("3/4 založit job (výřez na mapě) …")
        data = {
            "name": "smoke-e2e",
            "preset_id": preset_id,
            "bbox": args.bbox,
        }
        print("   bbox:", args.bbox)

        t0 = time.time()
        r = client.post("/api/jobs", data=data)
        print(f"   HTTP {r.status_code} ({time.time() - t0:.1f}s)")
        if r.status_code != 200:
            print(r.text[:2000], file=sys.stderr)
            return 1
        job = r.json()
        job_id = job["id"]
        print("   job_id:", job_id, "status:", job["status"])

        r = client.get(f"/api/jobs/{job_id}/log")
        for line in r.json().get("lines", []):
            print("   log:", line["line"])

        if args.wait_minutes <= 0:
            print("4/4 hotovo (bez cekani na pipeline)")
            return 0

        print(f"4/4 cekam max {args.wait_minutes} min …")
        deadline = time.time() + args.wait_minutes * 60
        last_after = 0
        while time.time() < deadline:
            job = client.get(f"/api/jobs/{job_id}").json()
            r = client.get(f"/api/jobs/{job_id}/log", params={"after": last_after})
            for line in r.json().get("lines", []):
                print("   log:", line["line"])
                last_after = line["id"]
            if job["status"] in ("done", "failed"):
                print("   vysledek:", job["status"], job.get("error") or "")
                if job["status"] == "done" and job.get("has_output"):
                    print(f"   download: {base}/api/jobs/{job_id}/download")
                return 0 if job["status"] == "done" else 2
            time.sleep(5)
        print("Timeout", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
