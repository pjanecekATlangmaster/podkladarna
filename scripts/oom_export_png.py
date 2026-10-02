"""Export jednoho .omap do PNG (výchozí = Pillow web cesta).

Webový náhled je vždy Pillow. Mapper CLI je jen pro georef ZIP (@ 600 DPI)
přes ``PODKLADARNA_MAPPER`` + ``PODKLADARNA_MAPPER_EXPORT`` (viz DEV.md).

Příklad:

    python scripts/oom_export_png.py data\\jobs\\<id>\\output\\Mapa-mtbo.omap -o preview.png
    python scripts/oom_export_png.py mapa.omap -o geo.png --georef
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.pipeline.oom_preview import find_mapper_exe, mapper_export_configured, render_omap_to_png


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vykreslí .omap do PNG.")
    parser.add_argument("omap", type=Path, help="Vstupní .omap")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Výstupní PNG (výchozí: vedle mapy, přípona .png)",
    )
    parser.add_argument("--max-side", type=int, default=1280)
    parser.add_argument(
        "--georef",
        action="store_true",
        help="Mapper CLI @ 600 DPI + PGW (vyžaduje PODKLADARNA_MAPPER_EXPORT)",
    )
    args = parser.parse_args(argv)
    omap = args.omap.resolve()
    if not omap.is_file():
        print(f"Chybí {omap}", file=sys.stderr)
        return 1
    dest = (args.output or omap.with_suffix(".png")).resolve()
    mapper = find_mapper_exe()
    if args.georef:
        if not mapper_export_configured():
            print(
                "Georef vyžaduje PODKLADARNA_MAPPER + PODKLADARNA_MAPPER_EXPORT "
                "(CLI build, ne stock 0.9.6).",
                file=sys.stderr,
            )
            return 2
        print(f"Mapper: {mapper}")
        summary = render_omap_to_png(
            omap,
            dest,
            log=print,
            write_pgw=True,
            engine="mapper",
        )
    else:
        if mapper:
            print(f"Mapper nalezen: {mapper} (web cesta = Pillow; georef = --georef)")
        summary = render_omap_to_png(
            omap, dest, log=print, max_side=args.max_side, engine="pillow"
        )
    print(f"Hotovo: {dest} ({summary})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
