"""Smoke test Mapper CLI v Docker image: .omap z pipeline → georef PNG + OCD12.

Spouští se při ``docker build`` (viz Dockerfile). Mapa má stejnou S-JTSK
georeferenci jako joby, takže odhalí i chybějící PROJ data / fonty. Při chybě
vypíše celý stderr Mapperu a skončí nenulově.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from app.pipeline.oom_preview import (
    MapperConvertError,
    MapperExportError,
    _png_size,
    run_mapper_convert,
    run_mapper_export,
)
from app.pipeline.package_oom import prepare_oom_map


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="mapper_smoke_") as tmp:
        root = Path(tmp)
        work = root / "work"
        work.mkdir()
        omap = prepare_oom_map(
            work,
            root / "smoke-les.omap",
            map_name="smoke",
            scale=10000,
            preset_id="forest_10000",
            bbox_wgs84=(14.40, 50.08, 14.41, 50.085),
            built_refs=None,
        )
        if omap is None or not omap.is_file():
            print("mapper_smoke: prepare_oom_map nevytvořil .omap", file=sys.stderr)
            return 1
        png = root / "smoke-les.png"
        try:
            run_mapper_export(omap, png, dpi=150, log=print, full_stderr=True)
            ocd = run_mapper_convert(omap, log=print)
        except (MapperExportError, MapperConvertError) as exc:
            print(f"mapper_smoke: FAIL {exc}", file=sys.stderr)
            return 1
        w, h = _png_size(png)
        if w < 50 or h < 50:
            print(f"mapper_smoke: PNG podezřele malý {w}×{h}", file=sys.stderr)
            return 1
        if ocd is None or not ocd.is_file():
            print("mapper_smoke: convert nevytvořil .ocd", file=sys.stderr)
            return 1
        print(f"mapper_smoke: OK export {w}×{h} @150 DPI, {ocd.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
