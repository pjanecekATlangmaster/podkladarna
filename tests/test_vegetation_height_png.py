import numpy as np

from app.pipeline import vegetation_height_png as vh
from app.pipeline.georef import read_pgw
from app.pipeline.oom_layers import collect_oom_templates


def test_vegetation_height_png_colors_georef_and_template(tmp_path, monkeypatch):
    """CHM → RGBA: pod 0,5 m a NoData průhledné, vyšší barevně; .pgw na střed pixelu; šablona v .omap skrytá."""
    chm = np.array([[0.0, 0.3, 1.0], [6.0, 20.0, -9999.0]], dtype=np.float32)
    gt = (-750000.0, 1.0, 0.0, -1046000.0, 0.0, -1.0)
    src = tmp_path / "chm.tif"
    src.write_bytes(b"x")
    monkeypatch.setattr(vh, "read_float32_geotiff", lambda path, log=None: (chm, gt, -9999.0))
    png = vh.write_vegetation_height_png(src, tmp_path / "references")
    from PIL import Image

    rgba = np.asarray(Image.open(png))
    assert rgba.shape == (2, 3, 4)
    assert list(rgba[..., 3].ravel()) == [0, 0, 255, 255, 255, 0]
    assert len({tuple(rgba[0, 2, :3]), tuple(rgba[1, 0, :3]), tuple(rgba[1, 1, :3])}) == 3
    pgw = read_pgw(tmp_path / "references" / vh.PGW_NAME)
    assert (pgw.origin_x, pgw.origin_y, pgw.pixel_x, pgw.pixel_y) == (-749999.5, -1046000.5, 1.0, -1.0)

    templates = collect_oom_templates(tmp_path, built_refs={vh.REF_KEY: png})
    veg = [t for t in templates if t.relpath == "references/vyska_vegetace.png"]
    assert len(veg) == 1 and not veg[0].visible

    # Bez vegetace se PNG nepíše; velký CHM se zmenší blokovým maximem pod MAX_PX.
    monkeypatch.setattr(vh, "read_float32_geotiff", lambda path, log=None: (np.zeros((4, 4), np.float32), gt, None))
    assert vh.write_vegetation_height_png(src, tmp_path / "prazdne") is None
    big = np.zeros((10, 10), np.float32)
    big[9, 9] = 12.0
    monkeypatch.setattr(vh, "MAX_PX", 4)
    monkeypatch.setattr(vh, "read_float32_geotiff", lambda path, log=None: (big, gt, None))
    small = np.asarray(Image.open(vh.write_vegetation_height_png(src, tmp_path / "zmenseno")))
    assert small.shape[:2] == (4, 4) and small[3, 3, 3] == 255
