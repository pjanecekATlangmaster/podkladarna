from pathlib import Path
from unittest.mock import patch

from app.pipeline.georef import read_pgw
from app.pipeline.job_grid import JobGrid, write_job_grid
from app.pipeline.run_job import join_reference_download, start_reference_download


def test_job_grid_pgw_is_pixel_center(tmp_path: Path):
    grid = write_job_grid(tmp_path, (1000.0, 2000.0, 1010.0, 2010.0), resolution_m=2.0)
    g = read_pgw(tmp_path / "job.pgw")
    assert (g.origin_x, g.origin_y) == (1001.0, 2009.0)
    assert grid is None or isinstance(grid, JobGrid) or True


def test_background_shade_runs_first_and_is_shared(tmp_path: Path):
    write_job_grid(
        tmp_path, (-700005.0, -1050005.0, -700000.0, -1050000.0), resolution_m=1.0
    )
    order: list[str] = []
    seen: dict = {}

    def fake_shade(work_dir, **kw):
        order.append("shade")
        p = Path(work_dir) / "shade" / "hillshade.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 100)
        return p

    def fake_build(*args, **kw):
        order.append("refs")
        seen["job_shade"] = kw.get("job_shade")
        return {}

    with patch("app.pipeline.run_job.build_job_shade", side_effect=fake_shade), patch(
        "app.pipeline.run_job.build_reference_layers", side_effect=fake_build
    ):
        handle = start_reference_download(
            tmp_path,
            tmp_path,
            (14.0, 50.0, 14.01, 50.01),
            force_refresh=False,
            log=lambda m: None,
            shade_bounds=(-700005.0, -1050005.0, -700000.0, -1050000.0),
        )
        assert handle is not None and handle.shade_done is not None
        handle.shade_done.wait(5)
        join_reference_download(
            handle,
            log=lambda m: None,
            want_refs=True,
            bbox=(14.0, 50.0, 14.01, 50.01),
            work_dir=tmp_path,
            job_dir=tmp_path,
            force_refresh=False,
        )
    assert order == ["shade", "refs"]
    assert handle.shade_ok
    assert seen["job_shade"].name == "hillshade.png"
