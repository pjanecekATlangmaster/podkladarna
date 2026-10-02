"""Bez-KP pipeline must not call legacy KP-only helpers."""
from __future__ import annotations

from pathlib import Path

RUN_JOB = Path(__file__).resolve().parents[1] / "app" / "pipeline" / "run_job.py"


def test_run_job_has_no_kp_runtime():
    src = RUN_JOB.read_text(encoding="utf-8")
    assert "write_osm_kp_zip" not in src
    assert "write_pullauta_ini" not in src
    assert "PULLAUTA_BIN" not in src
    assert "if use_kp" not in src
