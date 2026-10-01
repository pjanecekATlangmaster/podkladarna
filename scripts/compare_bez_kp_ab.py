#!/usr/bin/env python3
"""A/B srovnání výstupů KP vs ``use_kp=false`` (vegetace / srázy / náhled).

Analogie k ``scripts/compare_contours_oom.py``, ale negeneruje vrstvy znovu:
bere **dva hotové job output** (rozbalený ZIP, ``output/``, nebo job root
s ``output/`` + ``work/``) a reportuje přítomnost + základní statistiky
(počet prvků, plochy, délky). Není to subjektivní pass/fail.

Příklad (stejná AOI, jednou s KP, jednou bez)::

  python scripts/compare_bez_kp_ab.py ^
    --a "data\\jobs\\<kp_job>\\output" ^
    --b "data\\jobs\\<bez_kp_job>\\output" ^
    --label-a KP --label-b bez-KP

  python scripts/compare_bez_kp_ab.py --a path\\a.zip_extracted --b path\\b --json
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.pipeline.oom_import import _pyogrio_layer_rows

# Hledané cesty relativně ke kořeni jobu / ZIPu / output.
_VEGE_CANDIDATES = (
    "base/vegetation.shp",
    "vegetation/vegetation.shp",
    "work/vegetation/vegetation.shp",
    "output/base/vegetation.shp",
    "vegetation.shp",
)

_CLIFF_SMALL_CANDIDATES = (
    "base/cliffs_small.dxf",
    "temp/c2g.dxf",
    "temp/c1g.dxf",
    "work/temp/c2g.dxf",
    "work/temp/c1g.dxf",
    "cliffs/cliffs_small.dxf",
    "cliffs_small.dxf",
)

_CLIFF_LARGE_CANDIDATES = (
    "base/cliffs_large.dxf",
    "temp/c3g.dxf",
    "temp/c2.dxf",
    "work/temp/c3g.dxf",
    "work/temp/c2.dxf",
    "cliffs/cliffs_large.dxf",
    "cliffs_large.dxf",
)

_PREVIEW_CANDIDATES = (
    "preview/preview.png",
    "preview.png",
    "output/preview.png",
    "work/preview.png",
)

_PULLAUTUS_CANDIDATES = (
    "kp/pullautus.png",
    "pullautus.png",
    "output/pullautus.png",
    "output/kp/pullautus.png",
    "basemap/pullautus.png",
)


def _first_existing(root: Path, relatives: tuple[str, ...]) -> Path | None:
    for rel in relatives:
        path = root / rel
        if path.is_file():
            return path
    return None


def resolve_side_root(path: Path) -> Path:
    """Normalizuje vstup na kořen artefaktů (ZIP extract / output / job)."""
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"Neexistuje: {path}")
    if path.is_file():
        raise NotADirectoryError(f"Očekáván adresář (rozbalený ZIP/output), ne soubor: {path}")
    # Job root: preferuj output/, pokud už má vege/preview; jinak root.
    out = path / "output"
    if out.is_dir() and (
        _first_existing(out, _VEGE_CANDIDATES + _PREVIEW_CANDIDATES + _PULLAUTUS_CANDIDATES)
        or (out / "base").is_dir()
        or list(out.glob("*.zip"))
    ):
        return out
    return path


def load_metadata(root: Path) -> dict[str, Any]:
    for rel in ("metadata.json", "output/metadata.json"):
        meta = root / rel
        if meta.is_file():
            try:
                return json.loads(meta.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
    # Job root → output/metadata.json when root is job and we resolved to output.
    parent_meta = root.parent / "metadata.json"
    if parent_meta.is_file():
        try:
            return json.loads(parent_meta.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def find_vegetation_shp(root: Path) -> Path | None:
    return _first_existing(root, _VEGE_CANDIDATES)


def find_cliff_paths(root: Path) -> dict[str, Path | None]:
    return {
        "small": _first_existing(root, _CLIFF_SMALL_CANDIDATES),
        "large": _first_existing(root, _CLIFF_LARGE_CANDIDATES),
    }


def find_preview_assets(root: Path) -> dict[str, Path | None]:
    return {
        "preview": _first_existing(root, _PREVIEW_CANDIDATES),
        "pullautus": _first_existing(root, _PULLAUTUS_CANDIDATES),
    }


def parse_dxf_line_segments(path: Path) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """ASCII DXF LINE entity parser (KP/DEM cliffs) – bez OGR."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    # Pair group-code / value lines.
    raw_lines = [ln.strip() for ln in text.splitlines()]
    pairs: list[tuple[str, str]] = []
    i = 0
    while i + 1 < len(raw_lines):
        pairs.append((raw_lines[i], raw_lines[i + 1]))
        i += 2

    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    ent: dict[str, float] = {}
    in_line = False
    for code, val in pairs:
        if code == "0":
            if in_line and {"10", "20", "11", "21"} <= ent.keys():
                segments.append(
                    ((ent["10"], ent["20"]), (ent["11"], ent["21"]))
                )
            ent = {}
            in_line = val.upper() == "LINE"
            continue
        if not in_line:
            continue
        if code in {"10", "20", "11", "21", "30", "31"}:
            try:
                ent[code] = float(val)
            except ValueError:
                pass
    if in_line and {"10", "20", "11", "21"} <= ent.keys():
        segments.append(((ent["10"], ent["20"]), (ent["11"], ent["21"])))
    return segments


def segment_length_m(
    a: tuple[float, float], b: tuple[float, float]
) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def cliff_file_stats(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {
            "present": False,
            "path": None,
            "segment_count": 0,
            "total_length_m": 0.0,
            "size_bytes": 0,
        }
    segs = parse_dxf_line_segments(path)
    total = sum(segment_length_m(a, b) for a, b in segs)
    return {
        "present": True,
        "path": str(path),
        "segment_count": len(segs),
        "total_length_m": round(total, 2),
        "size_bytes": path.stat().st_size,
    }


def cliffs_stats(root: Path) -> dict[str, Any]:
    paths = find_cliff_paths(root)
    small = cliff_file_stats(paths["small"])
    large = cliff_file_stats(paths["large"])
    return {
        "small": small,
        "large": large,
        "segment_count": int(small["segment_count"]) + int(large["segment_count"]),
        "total_length_m": round(
            float(small["total_length_m"]) + float(large["total_length_m"]), 2
        ),
        "any_present": bool(small["present"] or large["present"]),
    }


def _polygon_area_m2_from_wkb(wkb: bytes) -> float:
    """Plocha v m² (EPSG:5514 / rovinné souřadnice) přes shapely; 0 při chybě."""
    if not wkb:
        return 0.0
    try:
        from shapely import wkb as shapely_wkb
    except ImportError:
        return 0.0
    try:
        geom = shapely_wkb.loads(bytes(wkb))
    except Exception:
        return 0.0
    if geom is None or geom.is_empty:
        return 0.0
    return float(abs(geom.area))


def vegetation_stats(shp: Path | None) -> dict[str, Any]:
    """Feature count + area by ``code`` / ``cls`` z vegetation.shp."""
    empty = {
        "present": False,
        "path": None,
        "feature_count": 0,
        "area_m2_total": 0.0,
        "area_by_code": {},
        "area_by_cls": {},
        "count_by_code": {},
        "count_by_cls": {},
    }
    if shp is None or not shp.is_file():
        return empty
    try:
        import pyogrio
    except ImportError:
        return {
            **empty,
            "present": True,
            "path": str(shp),
            "error": "pyogrio unavailable",
            "size_bytes": shp.stat().st_size,
        }

    area_by_code: dict[str, float] = {}
    area_by_cls: dict[str, float] = {}
    count_by_code: dict[str, int] = {}
    count_by_cls: dict[str, int] = {}
    n = 0
    total = 0.0
    try:
        layers = pyogrio.list_layers(shp)
    except Exception as exc:
        return {
            **empty,
            "present": True,
            "path": str(shp),
            "error": str(exc),
            "size_bytes": shp.stat().st_size,
        }

    for layer_name, _t in layers:
        for props, wkb in _pyogrio_layer_rows(shp, layer=layer_name):
            n += 1
            area = _polygon_area_m2_from_wkb(bytes(wkb) if wkb is not None else b"")
            total += area
            code = props.get("code")
            cls = props.get("cls")
            code_key = str(code) if code is not None and str(code) not in {"nan", "None"} else "?"
            if cls is None or str(cls) in {"nan", "None"}:
                cls_key = "?"
            else:
                try:
                    cls_key = str(int(cls))
                except (TypeError, ValueError):
                    cls_key = str(cls)
            area_by_code[code_key] = area_by_code.get(code_key, 0.0) + area
            area_by_cls[cls_key] = area_by_cls.get(cls_key, 0.0) + area
            count_by_code[code_key] = count_by_code.get(code_key, 0) + 1
            count_by_cls[cls_key] = count_by_cls.get(cls_key, 0) + 1

    def _round_map(d: dict[str, float]) -> dict[str, float]:
        return {k: round(v, 2) for k, v in sorted(d.items())}

    return {
        "present": True,
        "path": str(shp),
        "feature_count": n,
        "area_m2_total": round(total, 2),
        "area_by_code": _round_map(area_by_code),
        "area_by_cls": _round_map(area_by_cls),
        "count_by_code": dict(sorted(count_by_code.items())),
        "count_by_cls": dict(sorted(count_by_cls.items())),
        "size_bytes": shp.stat().st_size,
    }


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Neplatný PNG: {path}")
    offset = 8
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk = data[offset + 4 : offset + 8]
        if chunk == b"IHDR":
            w, h = struct.unpack(">II", data[offset + 8 : offset + 16])
            return int(w), int(h)
        offset += 12 + length
    raise ValueError(f"PNG bez IHDR: {path}")


def preview_stats(root: Path) -> dict[str, Any]:
    assets = find_preview_assets(root)
    out: dict[str, Any] = {
        "preview_present": assets["preview"] is not None,
        "pullautus_present": assets["pullautus"] is not None,
        "preview": None,
        "pullautus": None,
        "preferred": None,
    }
    for key in ("preview", "pullautus"):
        path = assets[key]
        if path is None:
            continue
        try:
            w, h = png_size(path)
        except ValueError as exc:
            out[key] = {"path": str(path), "error": str(exc), "size_bytes": path.stat().st_size}
            continue
        info = {
            "path": str(path),
            "width": w,
            "height": h,
            "size_bytes": path.stat().st_size,
        }
        out[key] = info
    # Preferovaný náhled = preview.png, jinak pullautus (stejná priorita jako resolve_preview_png).
    out["preferred"] = out["preview"] or out["pullautus"]
    return out


def presence_matrix(root: Path) -> dict[str, bool]:
    cliffs = find_cliff_paths(root)
    prev = find_preview_assets(root)
    return {
        "vegetation.shp": find_vegetation_shp(root) is not None,
        "cliffs_small.dxf": cliffs["small"] is not None,
        "cliffs_large.dxf": cliffs["large"] is not None,
        "preview.png": prev["preview"] is not None,
        "pullautus.png": prev["pullautus"] is not None,
    }


def relative_diff(a: float, b: float) -> float | None:
    """(b - a) / a; None pokud a==0 (nedefinováno)."""
    if a == 0:
        return None if b == 0 else float("inf") if b > 0 else float("-inf")
    return (b - a) / a


def _fmt_pct(ratio: float | None) -> str:
    if ratio is None:
        return "n/a"
    if math.isinf(ratio):
        return "+∞" if ratio > 0 else "-∞"
    return f"{ratio * 100:+.1f}%"


def summarize_side(root: Path, label: str) -> dict[str, Any]:
    root = resolve_side_root(root)
    meta = load_metadata(root)
    return {
        "label": label,
        "root": str(root),
        "use_kp": meta.get("use_kp"),
        "name": meta.get("name"),
        "app_version": meta.get("app_version"),
        "presence": presence_matrix(root),
        "vegetation": vegetation_stats(find_vegetation_shp(root)),
        "cliffs": cliffs_stats(root),
        "preview": preview_stats(root),
    }


def compare_ab(
    a_root: Path,
    b_root: Path,
    *,
    label_a: str = "A",
    label_b: str = "B",
) -> dict[str, Any]:
    a = summarize_side(a_root, label_a)
    b = summarize_side(b_root, label_b)
    va, vb = a["vegetation"], b["vegetation"]
    ca, cb = a["cliffs"], b["cliffs"]
    diffs = {
        "vegetation_feature_count": {
            "a": va["feature_count"],
            "b": vb["feature_count"],
            "delta": vb["feature_count"] - va["feature_count"],
            "rel_b_vs_a": relative_diff(float(va["feature_count"]), float(vb["feature_count"])),
        },
        "vegetation_area_m2": {
            "a": va["area_m2_total"],
            "b": vb["area_m2_total"],
            "delta": round(vb["area_m2_total"] - va["area_m2_total"], 2),
            "rel_b_vs_a": relative_diff(float(va["area_m2_total"]), float(vb["area_m2_total"])),
        },
        "cliffs_segment_count": {
            "a": ca["segment_count"],
            "b": cb["segment_count"],
            "delta": cb["segment_count"] - ca["segment_count"],
            "rel_b_vs_a": relative_diff(float(ca["segment_count"]), float(cb["segment_count"])),
        },
        "cliffs_length_m": {
            "a": ca["total_length_m"],
            "b": cb["total_length_m"],
            "delta": round(cb["total_length_m"] - ca["total_length_m"], 2),
            "rel_b_vs_a": relative_diff(float(ca["total_length_m"]), float(cb["total_length_m"])),
        },
    }
    # Presence mismatches (useful smoke signal).
    keys = sorted(set(a["presence"]) | set(b["presence"]))
    presence_delta = {
        k: {"a": a["presence"].get(k, False), "b": b["presence"].get(k, False)}
        for k in keys
        if a["presence"].get(k, False) != b["presence"].get(k, False)
    }
    return {
        "a": a,
        "b": b,
        "diffs": diffs,
        "presence_mismatch": presence_delta,
    }


def format_report(report: dict[str, Any]) -> str:
    a, b = report["a"], report["b"]
    lines: list[str] = []
    lines.append(f"=== A/B: {a['label']} vs {b['label']} ===")
    lines.append(f"{a['label']}: {a['root']}  (use_kp={a.get('use_kp')!r}, name={a.get('name')!r})")
    lines.append(f"{b['label']}: {b['root']}  (use_kp={b.get('use_kp')!r}, name={b.get('name')!r})")
    lines.append("")
    lines.append("## Presence")
    keys = sorted(set(a["presence"]) | set(b["presence"]))
    lines.append(f"{'artifact':<22} {a['label']:<8} {b['label']:<8}")
    for k in keys:
        av = "yes" if a["presence"].get(k) else "no"
        bv = "yes" if b["presence"].get(k) else "no"
        mark = "  ←" if av != bv else ""
        lines.append(f"{k:<22} {av:<8} {bv:<8}{mark}")

    lines.append("")
    lines.append("## Vegetation")
    va, vb = a["vegetation"], b["vegetation"]
    d = report["diffs"]["vegetation_feature_count"]
    da = report["diffs"]["vegetation_area_m2"]
    lines.append(
        f"{a['label']}: present={va['present']}  features={va['feature_count']}  "
        f"area_m2={va['area_m2_total']}  by_code={va.get('area_by_code')}"
    )
    lines.append(
        f"{b['label']}: present={vb['present']}  features={vb['feature_count']}  "
        f"area_m2={vb['area_m2_total']}  by_code={vb.get('area_by_code')}"
    )
    lines.append(
        f"Δ features: {d['delta']:+d} ({_fmt_pct(d['rel_b_vs_a'])} B vs A); "
        f"Δ area: {da['delta']:+.2f} m² ({_fmt_pct(da['rel_b_vs_a'])})"
    )

    lines.append("")
    lines.append("## Cliffs")
    ca, cb = a["cliffs"], b["cliffs"]
    ds = report["diffs"]["cliffs_segment_count"]
    dl = report["diffs"]["cliffs_length_m"]
    lines.append(
        f"{a['label']}: segs={ca['segment_count']}  length_m={ca['total_length_m']}  "
        f"small={ca['small']['segment_count']}/{ca['small']['total_length_m']} m  "
        f"large={ca['large']['segment_count']}/{ca['large']['total_length_m']} m"
    )
    lines.append(
        f"{b['label']}: segs={cb['segment_count']}  length_m={cb['total_length_m']}  "
        f"small={cb['small']['segment_count']}/{cb['small']['total_length_m']} m  "
        f"large={cb['large']['segment_count']}/{cb['large']['total_length_m']} m"
    )
    lines.append(
        f"Δ segs: {ds['delta']:+d} ({_fmt_pct(ds['rel_b_vs_a'])}); "
        f"Δ length: {dl['delta']:+.2f} m ({_fmt_pct(dl['rel_b_vs_a'])})"
    )

    lines.append("")
    lines.append("## Preview")
    for side in (a, b):
        p = side["preview"]
        pref = p.get("preferred")
        if pref:
            lines.append(
                f"{side['label']}: preferred={Path(pref['path']).name}  "
                f"{pref['width']}×{pref['height']}  {pref['size_bytes']} B  "
                f"(preview={p['preview_present']}, pullautus={p['pullautus_present']})"
            )
        else:
            lines.append(
                f"{side['label']}: no preview/pullautus  "
                f"(preview={p['preview_present']}, pullautus={p['pullautus_present']})"
            )
    pa = a["preview"].get("preferred")
    pb = b["preview"].get("preferred")
    if pa and pb:
        same_dim = pa["width"] == pb["width"] and pa["height"] == pb["height"]
        size_rel = relative_diff(float(pa["size_bytes"]), float(pb["size_bytes"]))
        lines.append(
            f"dims_match={same_dim}; size B vs A: {_fmt_pct(size_rel)}"
        )

    if report.get("presence_mismatch"):
        lines.append("")
        lines.append("## Presence mismatches")
        for k, v in report["presence_mismatch"].items():
            lines.append(f"- {k}: {a['label']}={v['a']}  {b['label']}={v['b']}")

    lines.append("")
    lines.append(
        "Pozn.: report je deskriptivní (počty/plochy/délky), ne blind QA pass. "
        "Orto epoch gate zůstává manuální checklist."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--a",
        type=Path,
        required=True,
        help="Job output / rozbalený ZIP / job root (strana A, typicky KP)",
    )
    ap.add_argument(
        "--b",
        type=Path,
        required=True,
        help="Job output / rozbalený ZIP / job root (strana B, typicky use_kp=false)",
    )
    ap.add_argument("--label-a", default="KP", help="Popisek strany A (default KP)")
    ap.add_argument("--label-b", default="bez-KP", help="Popisek strany B (default bez-KP)")
    ap.add_argument(
        "--json",
        action="store_true",
        help="Vypsat strojový JSON report na stdout",
    )
    ap.add_argument(
        "-o",
        "--out",
        type=Path,
        help="Volitelně uložit textový report sem",
    )
    args = ap.parse_args(argv)

    try:
        report = compare_ab(
            args.a, args.b, label_a=args.label_a, label_b=args.label_b
        )
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"Chyba: {exc}", file=sys.stderr)
        return 2

    text = format_report(report)
    if args.json:
        def _sanitize(obj: Any) -> Any:
            if isinstance(obj, float) and (math.isinf(obj) or math.isnan(obj)):
                return None
            if isinstance(obj, dict):
                return {k: _sanitize(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_sanitize(v) for v in obj]
            return obj

        print(json.dumps(_sanitize(report), ensure_ascii=False, indent=2))
    else:
        print(text)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"Uloženo: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
