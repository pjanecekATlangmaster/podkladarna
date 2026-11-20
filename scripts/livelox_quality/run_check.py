"""Týdenní kontrola kvality proti Liveloxu: stáhne reference, vygeneruje mapy na lokální instanci, změří, napíše report.

python run_check.py --base http://localhost:8672 --data <PODKLADARNA_DATA> --cache <cache> --out <dir>
                    [--only id1,id2] [--jobs jobs.json] [--history history.jsonl]

Běží v cloudu z masteru proti vlastnímu serveru (ne proti ostré A ani testu B). Reference se stahují do --cache
mimo repozitář; do --out jde jen results.json a report.md s čísly.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import jobs  # noqa: E402
import measure  # noqa: E402
import report  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8672")
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--cache", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--only", default="")
    ap.add_argument("--jobs", type=Path, help="už založené joby {oblast: id} (přeskočí založení)")
    ap.add_argument("--history", type=Path)
    a = ap.parse_args()
    refs = yaml.safe_load((HERE / "references.yaml").read_text(encoding="utf-8"))["references"]
    if a.only:
        refs = [r for r in refs if r["id"] in a.only.split(",")]
    a.cache.mkdir(parents=True, exist_ok=True)
    a.out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.setdefault("NODE_PATH", subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip())
    subprocess.run(["node", str(HERE / "fetch_ref.js"), str(a.cache), *[str(r["class_id"]) for r in refs]], env=env, check=False)

    given = json.loads(a.jobs.read_text()) if a.jobs else {}
    started: dict[str, tuple[str, tuple]] = {}
    results: dict[str, dict] = {}
    for r in refs:
        blob_p = a.cache / str(r["class_id"]) / "blob.json"
        if not blob_p.exists():
            results[r["id"]] = {"ref": r, "error": "reference z Liveloxu nedostupná"}
            continue
        bbox = jobs.job_bbox(json.loads(blob_p.read_text()), r["size_km"])
        try:
            started[r["id"]] = (given.get(r["id"]) or jobs.submit(a.base, r, bbox), bbox)
        except Exception as e:  # noqa: BLE001
            results[r["id"]] = {"ref": r, "error": f"založení jobu: {e}"}
    try:
        with jobs._opener().open(f"{a.base}/api/health", timeout=30) as resp:
            version = json.load(resp).get("version")
    except Exception:  # noqa: BLE001
        version = None
    for r in refs:
        if r["id"] not in started:
            continue
        job_id, bbox = started[r["id"]]
        st = jobs.wait(a.base, job_id)
        e = {"ref": r, "job": job_id, "bbox": bbox, "version": version}
        if st.get("status") != "done":
            e["error"] = f"job {job_id}: {st.get('status')}"
        else:
            scale = json.loads((a.cache / str(r["class_id"]) / "blob.json").read_text())["map"].get("mapScale") or r["map_scale"]
            try:
                e["m"] = measure.measure(REPO, a.data / "jobs" / job_id, a.cache / str(r["class_id"]), r["kind"], int(scale), bbox)
            except Exception as ex:  # noqa: BLE001
                e["error"] = f"měření: {ex}"
        results[r["id"]] = e
        print(r["id"], "hotovo", e.get("error", ""), flush=True)
    order = [r["id"] for r in refs]
    results = {k: results[k] for k in order if k in results}
    (a.out / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    report.main(a.out / "results.json", a.out / "report.md", a.history)
    return 0


if __name__ == "__main__":
    sys.exit(main())
