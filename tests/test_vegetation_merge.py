import numpy as np

from app.pipeline.vegetation_merge import merge_small_regions


def _base():
    a = np.zeros((40, 40), dtype=np.uint8)
    a[:, :20] = 3  # velká 408 vlevo
    a[:, 20:] = 4  # velká 410 vpravo
    return a


def test_small_patch_filled_with_surrounding_class():
    a = _base()
    a[10:12, 5:7] = 1  # 4 px žlutá uvnitř 408
    out = merge_small_regions(a, 1.0, min_area_m2=10.0)
    assert (out[10:12, 5:7] == 3).all()


def test_small_patch_in_white_becomes_white():
    a = np.zeros((40, 40), dtype=np.uint8)
    a[10:12, 5:7] = 2
    out = merge_small_regions(a, 1.0, min_area_m2=10.0)
    assert not out.any()


def test_small_white_hole_in_yellow_is_filled_yellow():
    a = np.ones((40, 40), dtype=np.uint8)
    a[10:12, 5:7] = 0
    out = merge_small_regions(a, 1.0, min_area_m2=10.0)
    assert (out == 1).all()


def test_large_patch_kept_and_input_untouched():
    a = _base()
    a[5:15, 5:15] = 1  # 100 px
    ref = a.copy()
    out = merge_small_regions(a, 1.0, min_area_m2=10.0)
    assert (out == ref).all()
    assert (a == ref).all()


def test_filled_patch_joins_neighbour_into_one_component():
    from scipy import ndimage

    a = np.zeros((40, 40), dtype=np.uint8)
    a[:, :20] = 1
    a[10:12, 20:22] = 3  # drobná zelená přilepená k žluté
    out = merge_small_regions(a, 1.0, min_area_m2=10.0)
    _, n = ndimage.label(out == 1)
    assert n == 1
    assert not (out == 3).any()


# --- 401 v zástavbě ----------------------------------------------------------


def _village(size=240):
    """1 m/px; v levé polovině ulice domů, vpravo pole. 401 všude, s děrami."""
    from app.pipeline.vegetation_merge import SETTLEMENT_MIN_ZONE_M2  # noqa: F401

    cls = np.ones((size, size), dtype=np.uint8)
    bld = np.zeros((size, size), dtype=bool)
    for y in range(20, size - 20, 25):
        for x in range(20, 100, 25):
            bld[y : y + 10, x : x + 10] = True
    return cls, bld


def test_settlement_zone_covers_village_not_field():
    from app.pipeline.vegetation_merge import settlement_zone

    _, bld = _village()
    zone = settlement_zone(bld, 1.0)
    assert zone[60, 60]
    assert not zone[120, 200]  # pole daleko od domů


def test_settlement_isolated_house_is_not_zone():
    from app.pipeline.vegetation_merge import settlement_zone

    bld = np.zeros((200, 200), dtype=bool)
    bld[95:105, 95:105] = True
    assert not settlement_zone(bld, 1.0).any()


def test_open_holes_filled_and_tiny_patches_dropped_in_village_only():
    from app.pipeline.vegetation_merge import simplify_open_in_settlements

    cls, bld = _village()
    cls[55:60, 55:60] = 4  # strom (25 m²) uvnitř zástavby → zaplnit
    cls[130:135, 200:205] = 4  # stejný strom v poli → zůstane
    iso = cls.copy()
    iso[:, :120] = 0
    iso[70:75, 70:75] = 1  # osamělý flíček 401 v zástavbě mimo velkou plochu
    out = simplify_open_in_settlements(cls, bld, 1.0)
    assert (out[55:60, 55:60] == 1).all()
    assert (out[130:135, 200:205] == 4).all()
    out2 = simplify_open_in_settlements(iso, bld, 1.0)
    assert not (out2[70:75, 70:75] == 1).any()


def test_rasterize_buildings_polygon_with_hole():
    from app.pipeline.vegetation_merge import rasterize_buildings

    gj = {
        "features": [
            {
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[10, 40], [30, 40], [30, 20], [10, 20], [10, 40]],
                        [[15, 35], [25, 35], [25, 25], [15, 25], [15, 35]],
                    ],
                }
            }
        ]
    }
    m = rasterize_buildings(gj, (0.0, 1.0, 0.0, 50.0, 0.0, -1.0), (50, 50))
    assert m[15, 12] and not m[20, 20] and not m[2, 2]
