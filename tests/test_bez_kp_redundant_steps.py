"""Pipeline must not call legacy Karttapullautin helpers."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_JOB = ROOT / "app" / "pipeline" / "run_job.py"
TOOL_ENV = ROOT / "app" / "tool_env.py"
INI = ROOT / "app" / "pipeline" / "ini_builder.py"


def test_run_job_has_no_kp_runtime():
    src = RUN_JOB.read_text(encoding="utf-8")
    assert "write_osm_kp_zip" not in src
    assert "write_pullauta_ini" not in src
    assert "PULLAUTA_BIN" not in src
    assert "if use_kp" not in src
    assert "prefer_kp_pullautus" not in src


def test_no_pullauta_resolver_or_ini_writer():
    assert "resolve_pullauta" not in TOOL_ENV.read_text(encoding="utf-8")
    assert "write_pullauta_ini" not in INI.read_text(encoding="utf-8")
    assert not (ROOT / "configs" / "pullauta.base.ini").exists()
