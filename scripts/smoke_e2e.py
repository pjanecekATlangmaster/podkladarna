#!/usr/bin/env python3
"""End-to-end smoke test proti běžící instanci (typicky docker compose dev).

Použití:
  python scripts/smoke_e2e.py
  python scripts/smoke_e2e.py --base http://127.0.0.1:8672
  python scripts/smoke_e2e.py --data-dir D:/path/to/sance --wait-minutes 30
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx


def main() -> int:
    p = argparse.ArgumentParser(description="Podkladárna E2E smoke test")
    p.add_argument("--base", default="http://127.0.0.1:8672", help="Base URL služby")
    p.add_argument("--data-dir", type=Path, help="Složka s DMR5G.laz, DMP1G.laz, Zabaged*.zip")
    p.add_argument("--wait-minutes", type=int, default=0, help="Po uploadu čekat na done (0 = jen upload)")
    args = p.parse_args()
    base = args.base.rstrip("/")

    with httpx.Client(base_url=base, timeout=600.0) as client:
        print("1/4 health …")
        r = client.get("/api/health")
        r.raise_for_status()
        print("   ", r.json())

        print("2/4 presets …")
        r = client.get("/api/presets")
        r.raise_for_status()
        preset_id = next(iter(r.json()))
        print("   preset:", preset_id)

        print("3/4 upload job …")
        files = []
        data = {
            "name": "smoke-e2e",
            "preset_id": preset_id,
            "run_vectors": "false",
            "output_png": "true",
            "output_dxf": "false",
            "output_zabaged_clean": "false",
            "savetempfolders": "true",
        }

        if args.data_dir:
            dmr = list(args.data_dir.glob("**/DMR*.laz")) + list(args.data_dir.glob("**/DMR*.las"))
            dmp = list(args.data_dir.glob("**/DMP*.laz")) + list(args.data_dir.glob("**/DMP*.las"))
            zab = list(args.data_dir.glob("**/Zabaged*.zip")) + list(args.data_dir.glob("**/zabaged*.zip"))
            if not dmr or not dmp:
                print("Chyba: v --data-dir chybí DMR/DMP LAZ", file=sys.stderr)
                return 1
            for f in dmr[:3]:
                files.append(("dmr_files", (f.name, f.read_bytes(), "application/octet-stream")))
            for f in dmp[:3]:
                files.append(("dmp_files", (f.name, f.read_bytes(), "application/octet-stream")))
            if zab:
                z = zab[0]
                files.append(("zabaged_file", (z.name, z.read_bytes(), "application/zip")))
                data["run_vectors"] = "true"
        else:
            files = [
                ("dmr_files", ("test.laz", b"fake-dmr", "application/octet-stream")),
                ("dmp_files", ("test2.laz", b"fake-dmp", "application/octet-stream")),
            ]

        t0 = time.time()
        r = client.post("/api/jobs", data=data, files=files)
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
                return 0 if job["status"] == "done" else 2
            time.sleep(5)
        print("Timeout", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
