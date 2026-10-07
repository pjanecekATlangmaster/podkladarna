"""Pipeline nesmí volat pozůstatky Karttapullautinu (runtime KP je pryč)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


def _app_sources() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(APP.rglob("*.py")))


def test_app_has_no_kp_runtime():
    src = _app_sources()
    for needle in (
        "write_osm_kp_zip",
        "write_pullauta_ini",
        "PULLAUTA_BIN",
        "resolve_pullauta",
        "pullautus",
        "use_kp",
    ):
        assert needle not in src, needle


def test_no_kp_modules_or_configs():
    for rel in (
        "app/pipeline/ini_builder.py",
        "app/pipeline/karttapullautin_dxf.py",
        "app/pipeline/open_land_subtract.py",
        "configs/pullauta.base.ini",
    ):
        assert not (ROOT / rel).exists(), rel
