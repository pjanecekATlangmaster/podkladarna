from __future__ import annotations

from app.pipeline.oom_georef import (
    grid_convergence_deg,
    magnetic_declination_deg,
    oom_north_angles,
)


def test_magnetic_declination_barrandov():
    # Barrandov (Praha 5) – WMM-2025, srovnatelné s mapy.ceskyorientak.cz
    dec = magnetic_declination_deg(50.037, 14.395)
    assert 5.0 < dec < 5.3


def test_grid_convergence_prague():
    conv = grid_convergence_deg(-727118.0, -1048388.0)
    assert -12.0 < conv < -3.0


def test_oom_north_angles_sjtsk_grivation():
    """Grivace S-JTSK ≈ deklinace − konvergence (OOM, ČSOS kalkulátor)."""
    ref_x, ref_y = -744200.0, -1043000.0
    decl, griv = oom_north_angles(ref_x, ref_y)
    conv = grid_convergence_deg(ref_x, ref_y)
    assert 4.8 < decl < 5.4
    assert 12.5 < griv < 13.5
    assert abs((decl - griv) - conv) < 0.02
