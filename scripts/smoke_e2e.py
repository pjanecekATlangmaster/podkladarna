#!/usr/bin/env python3
"""End-to-end smoke test proti běžící instanci (typicky docker compose dev).

Použití:
  python scripts/smoke_e2e.py
  python scripts/smoke_e2e.py --wait-minutes 30
  python scripts/smoke_e2e.py --data-dir testdata --preset sprint_2m
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TESTDATA = REPO_ROOT / "testdata"


def find_inputs(data_dir: Path) -> tuple[list[Path], list[Path], list[Path]]:
    dmr = sorted(data_dir.glob("DMR*.laz")) + sorted(data_dir.glob("DMR*.las"))
    dmr += sorted(data_dir.glob("**/DMR*.laz")) + sorted(data_dir.glob("**/DMR*.las"))
    dmp = sorted(data_dir.glob("DMP*.laz")) + sorted(data_dir.glob("DMP*.las"))
    dmp += sorted(data_dir.glob("**/DMP*.laz")) + sorted(data_dir.glob("**/DMP*.las"))
    zab = sorted(data_dir.glob("Zabaged*.zip")) + sorted(data_dir.glob("zabaged*.zip"))
    zab += sorted(data_dir.glob("*.zip"))
    # dedupe paths
    dmr = list(dict.fromkeys(dmr))
    dmp = list(dict.fromkeys(dmp))
    zab = list(dict.fromkeys(zab))
    return dmr, dmp, zab


def main() -> int:
    p = argparse.ArgumentParser(description="Podkladárna E2E smoke test")
    p.add_argument("--base", default="http://127.0.0.1:8672", help="Base URL služby")
    p.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help=f"Složka s LAZ/ZIP (výchozí: {DEFAULT_TESTDATA} pokud existuje)",
    )
    p.add_argument("--fake", action="store_true", help="Fake LAZ místo testdata (jen test uploadu)")
    p.add_argument("--preset", default="sprint_2m", help="Preset ID")
    p.add_argument("--wait-minutes", type=int, default=0, help="Po uploadu čekat na done (0 = jen upload)")
    args = p.parse_args()
    base = args.base.rstrip("/")

    if args.data_dir is None and not args.fake and DEFAULT_TESTDATA.is_dir():
        args.data_dir = DEFAULT_TESTDATA

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

        print("3/4 upload job …")
        files = []
        data = {
            "name": "smoke-e2e",
            "preset_id": preset_id,
            "run_vectors": "false",
            "output_png": "true",
            "output_dxf": "true",
            "output_zabaged_clean": "true",
            "savetempfolders": "true",
        }

        if args.data_dir and not args.fake:
            print("   data-dir:", args.data_dir)
            dmr, dmp, zab = find_inputs(args.data_dir)
            if not dmr or not dmp:
                print(f"Chyba: v {args.data_dir} chybí DMR/DMP LAZ", file=sys.stderr)
                print(f"  nalezeno DMR={len(dmr)} DMP={len(dmp)} ZAB={len(zab)}", file=sys.stderr)
                return 1
            for f in dmr[:3]:
                print(f"   DMR: {f.name} ({f.stat().st_size / 1e6:.2f} MB)")
                files.append(("dmr_files", (f.name, f.read_bytes(), "application/octet-stream")))
            for f in dmp[:3]:
                print(f"   DMP: {f.name} ({f.stat().st_size / 1e6:.2f} MB)")
                files.append(("dmp_files", (f.name, f.read_bytes(), "application/octet-stream")))
            if zab:
                z = zab[0]
                print(f"   ZABAGED: {z.name} ({z.stat().st_size / 1e6:.2f} MB)")
                files.append(("zabaged_file", (z.name, z.read_bytes(), "application/zip")))
                data["run_vectors"] = "true"
        else:
            print("   (bez testdata – fake LAZ, pipeline v Dockeru spadne)")
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
                if job["status"] == "done" and job.get("has_output"):
                    print(f"   download: {base}/api/jobs/{job_id}/download")
                return 0 if job["status"] == "done" else 2
            time.sleep(5)
        print("Timeout", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
