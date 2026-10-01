"""Gating vestigial KP-era steps on the bez-KP path."""

from __future__ import annotations

import re
from pathlib import Path

import app.pipeline.run_job as run_job


def test_write_osm_kp_zip_gated_behind_use_kp():
    """ogr2ogr → osm_kp.zip is only for pullauta vector pass, not bez-KP."""
    src = Path(run_job.__file__).read_text(encoding="utf-8")
    assert re.search(
        r"if use_kp:\s*\n\s*osm_kp_zip = write_osm_kp_zip\(",
        src,
    ), "write_osm_kp_zip must run only inside if use_kp"


def test_dsm_filled_not_generated_in_dem_prep():
    """CHM uses dsm_raw; fillnodata DSM was a leftover that nothing consumed."""
    from app.pipeline import dem_prep

    src = Path(dem_prep.__file__).read_text(encoding="utf-8")
    assert "_fill_dem_nodata(dsm_raw" not in src
    assert "_gdal_chm(dem_filled, dsm_raw" in src
