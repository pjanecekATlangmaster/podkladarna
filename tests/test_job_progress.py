from app.pipeline.job_progress import (
    JobProgress,
    format_elapsed,
    indented_log,
    plan_pipeline_steps,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_job_progress_numbered_headers_with_elapsed():
    lines: list[str] = []
    clock = _Clock()
    progress = JobProgress(lines.append, ["alpha", "beta", "gamma"], clock=clock)
    progress.announce_plan()
    assert lines[0] == "Plán (3 kroků): alpha · beta · gamma"
    progress.begin("alpha")
    assert lines[-1] == "[1/3] Alpha · 0:00"
    progress.done()
    clock.t = 75
    progress.begin("beta")
    # Prázdný řádek odděluje kroky.
    assert lines[-2:] == ["", "[2/3] Beta · 1:15"]
    progress.done()
    clock.t = 3725
    progress.begin("gamma")
    assert lines[-1] == "[3/3] Gamma · 1:02:05"
    progress.finish_all()
    assert progress.elapsed() == "1:02:05"


def test_job_progress_skip_advances():
    lines: list[str] = []
    progress = JobProgress(lines.append, ["a", "b", "c"])
    progress.skip("a", reason="cache")
    assert lines[-1] == "[1/3] A – přeskočeno (cache)"
    progress.begin("b")
    assert lines[-1].startswith("[2/3] B · ")


def test_job_progress_silent_without_logger():
    progress = JobProgress(None, ["x"])
    progress.announce_plan()
    progress.begin("x")
    progress.done()


def test_format_elapsed():
    assert format_elapsed(0) == "0:00"
    assert format_elapsed(59.6) == "1:00"
    assert format_elapsed(3600) == "1:00:00"


def test_indented_log_indents_every_line():
    lines: list[str] = []
    log = indented_log(lines.append)
    log("jedna")
    log("")
    log("a\nb")
    assert lines == ["  jedna", "", "  a\n  b"]


def test_plan_pipeline_steps_full_bbox_zip():
    steps = plan_pipeline_steps(
        bbox=(1, 2, 3, 4),
        reused_from=None,
        want_zip=True,
        want_refs=True,
    )
    assert steps[0] == "stažení LiDAR"
    assert "příprava LiDAR" in steps
    assert steps[-1] == "sestavení mapy a ZIP"
    assert "referenční podklady" in steps
    assert "RÚIAN a AOPK" in steps
    assert "balení výstupu" not in steps


def test_plan_pipeline_steps_reuse_includes_copy_and_prepare():
    steps = plan_pipeline_steps(
        bbox=(1, 2, 3, 4),
        reused_from="abc",
        want_zip=True,
        want_refs=False,
    )
    assert steps[0] == "kopie LAZ z předchozího jobu"
    assert "příprava LiDAR" in steps
    assert "referenční podklady" not in steps
