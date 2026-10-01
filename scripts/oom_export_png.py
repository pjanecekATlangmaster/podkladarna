"""Export jednoho .omap do PNG.

Stock OpenOrienteering Mapper 0.9.x nemá CLI export (otevře GUI). Tento skript
použije vestavěný náhled. Skutečný Mapper se zavolá jen když je nastavené
``PODKLADARNA_MAPPER_EXPORT`` (viz DEV.md).

Příklad:

    python scripts/oom_export_png.py data\\jobs\\<id>\\output\\Mapa-mtbo.omap -o preview.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.pipeline.oom_preview import find_mapper_exe, render_omap_to_png


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
    args = parser.parse_args(argv)
    omap = args.omap.resolve()
    if not omap.is_file():
        print(f"Chybí {omap}", file=sys.stderr)
        return 1
    dest = (args.output or omap.with_suffix(".png")).resolve()
    mapper = find_mapper_exe()
    if mapper:
        print(f"Mapper nalezen: {mapper} (CLI jen při PODKLADARNA_MAPPER_EXPORT)")
    else:
        print("Mapper.exe není na PATH – použije se vestavěný náhled")
    summary = render_omap_to_png(omap, dest, log=print, max_side=args.max_side)
    print(f"Hotovo: {dest} ({summary})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
