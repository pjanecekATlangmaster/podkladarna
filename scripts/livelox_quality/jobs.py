"""Výřez jobu a jeho založení / čekání přes API běžící instance (lokální server v cloudu, ne ostrá A)."""
from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


def quad(blob: dict) -> list[tuple[float, float]]:
    """Rohy mapy (lon, lat) v pořadí obrázku: JZ, JV, SV, SZ (Livelox boundingQuadrilateral)."""
    return [(v["longitude"], v["latitude"]) for v in blob["map"]["boundingQuadrilateral"]["vertices"]]


def job_bbox(blob: dict, size_km: float) -> tuple[float, float, float, float]:
    q = quad(blob)
    lon = sum(p[0] for p in q) / 4
    lat = sum(p[1] for p in q) / 4
    dlat = size_km / 2 / 111.32
    dlon = size_km / 2 / (111.32 * math.cos(math.radians(lat)))
    return (round(lon - dlon, 6), round(lat - dlat, 6), round(lon + dlon, 6), round(lat + dlat, 6))


def _multipart(fields: list[tuple[str, str]]) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    body = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields)
    return body + f"--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def submit(base: str, ref: dict, bbox) -> str:
    fields = [("name", f"kontrola {ref['id']}"), ("bbox", ",".join(map(str, bbox))),
              ("map_scale", str(ref["map_scale"])), ("dtm", "0")]
    if ref["kind"] == "les":
        fields += [("map_types_form", "1"), ("map_types", "les")]
    body, ctype = _multipart(fields)
    req = urllib.request.Request(f"{base}/api/jobs?lang=en", data=body, method="POST", headers={"Content-Type": ctype})
    with _opener().open(req, timeout=120) as r:
        return json.load(r)["id"]


def status(base: str, job_id: str) -> dict:
    with _opener().open(f"{base}/api/jobs/{urllib.parse.quote(job_id)}", timeout=60) as r:
        return json.load(r)


def wait(base: str, job_id: str, timeout_s: int = 3600) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        st = status(base, job_id)
        if st.get("status") in ("done", "failed", "error", "cancelled"):
            return st
        time.sleep(15)
    return {"status": "timeout"}
