"""Tests for DEM cliff candidates."""

from __future__ import annotations

import math
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from shapely.geometry import Point

from app.pipeline.cliffs_dem import (
    ROCK_MIN_GRADE,
    TICK_HALF_LEN_M,
    detect_cliff_ticks,
    generate_job_cliffs_dem,
    resolve_drop_thresholds,
    write_cliff_ticks_dxf,
)


def _pixel_to_world(gt, col: float, row: float) -> tuple[float, float]:
    x = gt[0] + col * gt[1] + row * gt[2]
    y = gt[3] + col * gt[4] + row * gt[5]
    return x, y


def _detect_cliff_ticks_loop(
    arr,
    valid,
    gt,
    *,
    near: int,
    far: int,
    px: float,
    py: float,
    min_drop_m: float,
    major_drop_m: float,
    rock_min_drop: float,
    stride: int,
    max_ticks: int,
):
    """Původní smyčka (reference pro testy vektorové verze)."""
    h, w = arr.shape
    small: list[tuple[tuple[float, float], tuple[float, float]]] = []
    large: list[tuple[tuple[float, float], tuple[float, float]]] = []

    for r in range(far, h - far, stride):
        for c in range(far, w - far, stride):
            if not valid[r, c]:
                continue
            z0 = float(arr[r, c])
            # Gradient in world units (X east / Y north). gt[5] usually < 0.
            z_e = float(arr[r, c + near]) if valid[r, c + near] else z0
            z_w = float(arr[r, c - near]) if valid[r, c - near] else z0
            z_s = float(arr[r + near, c]) if valid[r + near, c] else z0
            z_n = float(arr[r - near, c]) if valid[r - near, c] else z0
            gx = (z_e - z_w) / (2.0 * near * px)  # dZ/dX
            # Row+ is south when gt[5] < 0 → dZ/dY_north = -(z_s - z_n)/(2*near*py)
            gy = -(z_s - z_n) / (2.0 * near * py)
            gmag = math.hypot(gx, gy)
            if gmag < 1e-6:
                continue
            ux, uy = gx / gmag, gy / gmag  # unit downhill in world XY
            # Pixel step along downhill (col east, row south when gt[5]<0).
            dcol = ux
            drow = -uy if gt[5] < 0 else uy
            plen = math.hypot(dcol, drow)
            if plen < 1e-9:
                continue
            dcol /= plen
            drow /= plen

            def _at(dist_px: float) -> float | None:
                cc = int(round(c + dcol * dist_px))
                rr = int(round(r + drow * dist_px))
                if rr < 0 or rr >= h or cc < 0 or cc >= w or not valid[rr, cc]:
                    return None
                return float(arr[rr, cc])

            near_lo = _at(-near)
            near_hi = _at(near)
            far_lo = _at(-far)
            far_hi = _at(far)
            if None in (near_lo, near_hi, far_lo, far_hi):
                continue
            step_m = min(px, py)
            span_m = (far - near) * step_m
            if span_m <= 0:
                continue
            grade = ((near_lo - far_lo) + (far_hi - near_hi)) / (2.0 * span_m)
            expected = grade * 2.0 * near * step_m
            drop = abs((near_hi - near_lo) - expected)
            if drop < min_drop_m:
                continue
            run_m = 2.0 * near * step_m
            grade = drop / run_m if run_m > 0 else 0.0
            # major_drop_m: vysoký schod, který je pořád dost strmý, ber jako skálu.
            steep_enough = grade >= ROCK_MIN_GRADE or (
                drop >= major_drop_m and grade >= ROCK_MIN_GRADE * 0.75
            )

            # Tick perpendicular to downhill (along contour / cliff face).
            nx, ny = -uy, ux
            nlen = math.hypot(nx, ny)
            if nlen < 1e-9:
                continue
            nx /= nlen
            ny /= nlen
            cx, cy = _pixel_to_world(gt, c + 0.5, r + 0.5)
            a = (cx - nx * TICK_HALF_LEN_M, cy - ny * TICK_HALF_LEN_M)
            b = (cx + nx * TICK_HALF_LEN_M, cy + ny * TICK_HALF_LEN_M)
            if steep_enough:
                # Přísnější práh jen pro skálu; slabé strmé → pryč (ne 104).
                if drop >= rock_min_drop:
                    large.append((a, b))
            else:
                small.append((a, b))
            if len(small) + len(large) >= max_ticks:
                return small, large

    return small, large



def test_resolve_drop_thresholds_normal():
    c1, c2 = resolve_drop_thresholds({"kp_cliff_sensitivity": "normal"})
    assert c1 == 1.8
    assert c2 == 3.4


def test_resolve_drop_thresholds_default_is_low():
    c1, c2 = resolve_drop_thresholds({})
    assert c1 == 2.2
    assert c2 == 4.0


def test_resolve_drop_thresholds_high():
    c1, c2 = resolve_drop_thresholds({"kp_cliff_sensitivity": "high"})
    assert c1 == 1.4
    assert c2 == 2.8
    c1v, c2v = resolve_drop_thresholds({"kp_cliff_sensitivity": "very_high"})
    assert c1v < c1
    assert c2v <= c2


def test_flat_dem_yields_no_ticks():
    elev = np.full((40, 40), 100.0, dtype=np.float32)
    gt = (0.0, 1.0, 0.0, 40.0, 0.0, -1.0)
    small, large = detect_cliff_ticks(elev, gt, min_drop_m=1.4, major_drop_m=2.8)
    assert small == []
    assert large == []


def test_step_dem_yields_ticks():
    """Schod ~4 m napříč středem → skalní kandidáti (strmý skok)."""
    elev = np.zeros((50, 50), dtype=np.float32)
    elev[:, 25:] = -4.0
    gt = (0.0, 1.0, 0.0, 50.0, 0.0, -1.0)
    earth, rock = detect_cliff_ticks(
        elev, gt, min_drop_m=1.0, major_drop_m=2.0, stride=1
    )
    assert len(earth) + len(rock) > 0
    assert len(rock) > len(earth)


@pytest.mark.parametrize(
    "base_grade, step_m, ratio, expect_ticks",
    [
        (0.0, 4.0, None, True),  # schod mezi rovinami
        (0.0, 4.0, 0.5, True),  # sprint: skutečný skok zůstává
        (1.2, 2.5, None, True),  # strmý svah s malým zlomem – bez testu skoku sráz
        (1.2, 2.5, 0.5, False),  # sprint: na souvislém strmém svahu ne (ukážou vrstevnice)
    ],
)
def test_sprint_step_side_ratio(base_grade, step_m, ratio, expect_ticks):
    """Sprint (``step_side_ratio``): sráz jen tam, kde je terén po obou stranách výrazně méně strmý než schod."""
    x = np.arange(50, dtype=np.float32)
    row = -base_grade * x - np.where(x >= 25, step_m, 0.0)
    elev = np.tile(row, (50, 1)).astype(np.float32)
    gt = (0.0, 1.0, 0.0, 50.0, 0.0, -1.0)
    earth, rock = detect_cliff_ticks(elev, gt, min_drop_m=1.8, major_drop_m=3.4, stride=1, step_side_ratio=ratio)
    assert bool(earth or rock) is expect_ticks


@pytest.mark.parametrize("ticks, expected", [(None, None), ([], 0.0), ([((0.0, 0.0), (0.0, 2.9))], "area")])
def test_dem_step_zone(tmp_path, ticks, expected):
    """Okolí ticků z DEM, ve kterém ZABAGED terénní stupeň ve sprintu skok potvrzuje; bez souborů detekce None."""
    from app.pipeline.cliffs_dem import dem_step_zone

    if ticks is not None:
        (tmp_path / "temp").mkdir()
        write_cliff_ticks_dxf(ticks, tmp_path / "temp" / "c_rock.dxf")
        (tmp_path / "temp" / "c2g.dxf").write_text("0\nEOF\n", encoding="utf-8")
    zone = dem_step_zone(tmp_path)
    if expected is None:
        assert zone is None
    elif expected == "area":
        assert zone.area > 20 and zone.contains(Point(2.0, 1.0)) and not zone.contains(Point(5.0, 1.0))
    else:
        assert zone.area == expected


def test_gentle_bank_is_mostly_earth():
    """Nízký schod (~1,6 m) není skála."""
    elev = np.zeros((50, 50), dtype=np.float32)
    elev[:, 25:] = -1.6
    gt = (0.0, 1.0, 0.0, 50.0, 0.0, -1.0)
    earth, rock = detect_cliff_ticks(
        elev, gt, min_drop_m=1.2, major_drop_m=4.0, stride=1
    )
    assert len(earth) > 0
    assert len(rock) < len(earth)


def test_steep_but_short_step_not_demoted_to_earth():
    """Strmý schod pod rock drop (cliff1+bonus) → ani skála, ani 104."""
    elev = np.zeros((50, 50), dtype=np.float32)
    elev[:, 25:] = -2.2  # nad cliff1=1.8, pod 1.8+0.5=2.3
    gt = (0.0, 1.0, 0.0, 50.0, 0.0, -1.0)
    earth, rock = detect_cliff_ticks(
        elev, gt, min_drop_m=1.8, major_drop_m=3.4, stride=1
    )
    assert rock == []
    # Zemní kandidáti mohou existovat jen mimo strmý pás; strmé slabé se zahodí.
    # Hlavní: žádné skalní ticky z ~2,2 m schodu.
    assert len(rock) == 0


def test_tall_steep_step_is_rock():
    """Vysoký strmý schod (≥ cliff1+bonus) → skála."""
    elev = np.zeros((50, 50), dtype=np.float32)
    elev[:, 25:] = -4.0
    gt = (0.0, 1.0, 0.0, 50.0, 0.0, -1.0)
    earth, rock = detect_cliff_ticks(
        elev, gt, min_drop_m=1.8, major_drop_m=3.4, stride=1
    )
    assert len(rock) > 0
    assert len(rock) > len(earth)


def test_uniform_slope_few_or_no_ticks():
    """Rovnoměrný svah bez schodu – trend odečet by měl potlačit falešné srázy."""
    elev = np.zeros((50, 50), dtype=np.float32)
    for c in range(50):
        elev[:, c] = -0.3 * c  # grade 0.3
    gt = (0.0, 1.0, 0.0, 50.0, 0.0, -1.0)
    small, large = detect_cliff_ticks(
        elev, gt, min_drop_m=1.4, major_drop_m=2.8, stride=1
    )
    assert len(small) + len(large) < 20


def test_write_cliff_ticks_dxf(tmp_path: Path):
    ticks = [((100.0, 200.0), (103.0, 200.0))]
    dest = tmp_path / "c2g.dxf"
    out = write_cliff_ticks_dxf(ticks, dest)
    assert out == dest
    text = dest.read_text(encoding="utf-8")
    assert "LINE" in text
    assert "100.000" in text


def test_generate_job_cliffs_dem_off(tmp_path: Path):
    assert generate_job_cliffs_dem(tmp_path, options={"kp_cliff_symbol": "off"}) == {}


def test_generate_job_cliffs_dem_missing_dem(tmp_path: Path):
    assert generate_job_cliffs_dem(tmp_path, options={}) == {}


def test_generate_job_cliffs_dem_calls_generator(tmp_path: Path):
    dem = tmp_path / "dem"
    dem.mkdir()
    (dem / "dem_filled.tif").write_bytes(b"dem" * 200)
    marker = tmp_path / "temp" / "c2g.dxf"

    def fake_gen(dem_tif, temp_dir, *, min_drop_m=1.4, major_drop_m=2.8, step_side_ratio=None, log=None):
        temp_dir.mkdir(parents=True, exist_ok=True)
        path = temp_dir / "c2g.dxf"
        path.write_text("0\nEOF\n", encoding="utf-8")
        return {"c2g.dxf": path}

    with patch(
        "app.pipeline.cliffs_dem.generate_cliffs_from_dem",
        side_effect=fake_gen,
    ):
        out = generate_job_cliffs_dem(tmp_path, options={"kp_cliff_symbol": "earth_bank"})

    assert "c2g.dxf" in out
    assert marker.is_file()


def test_vectorized_cliff_ticks_match_reference_loop():
    """numpy detekce = původní smyčka (stejné ticky, pořadí, limit max_ticks)."""
    import numpy as np

    from app.pipeline import cliffs_dem as cd

    rng = np.random.default_rng(5)
    yy, xx = np.mgrid[0:160, 0:200]
    arr = (300 + 0.2 * xx + 4 * np.sin(yy / 9.0) + 6.0 * (xx > 90) + 4.0 * (yy > 70)).astype(np.float32)
    arr += rng.normal(0, 0.3, arr.shape).astype(np.float32)
    arr[40:44, 10:30] = -9999.0
    valid = arr != -9999.0
    gt = (-700000.0, 1.0, 0.0, -1050000.0, 0.0, -1.0)
    for c1, c2, cap in ((1.8, 3.4, 80_000), (1.15, 2.0, 37)):
        kw = dict(near=2, far=8, px=1.0, py=1.0, min_drop_m=c1, major_drop_m=c2,
                  rock_min_drop=c1 + cd.ROCK_MIN_DROP_BONUS_M, stride=2, max_ticks=cap)
        got = cd._detect_cliff_ticks_np(arr, valid, gt, rows_per_block=7, **kw)
        ref = _detect_cliff_ticks_loop(arr, valid, gt, **kw)
        assert got == ref
        assert sum(map(len, got)) > 0
