from app.pipeline.job_progress import JobProgress, plan_pipeline_steps


def test_job_progress_shows_current_and_remaining():
    lines: list[str] = []
    progress = JobProgress(lines.append, ["alpha", "beta", "gamma"])
    progress.begin("alpha")
    assert lines[0] == "=== Krok 1/3: alpha ==="
    assert lines[1] == "  Následuje: beta · gamma"
    progress.done()
    assert lines[2] == ""
    progress.begin("beta")
    assert "=== Krok 2/3: beta ===" in lines
    assert "  Následuje: gamma" in lines
    progress.done()
    progress.begin("gamma")
    assert "  Následuje: (poslední krok)" in lines
    progress.done()
    assert lines[-1] == ""


def test_job_progress_skip_advances_and_blank_line():
    lines: list[str] = []
    progress = JobProgress(lines.append, ["a", "b", "c"])
    progress.skip("a", reason="cache")
    assert any("přeskočeno (cache)" in ln for ln in lines)
    assert lines[-1] == ""
    progress.begin("b")
    assert "=== Krok 2/3: b ===" in lines
    assert "  Následuje: c" in lines


def test_job_progress_silent_without_logger():
    progress = JobProgress(None, ["x"])
    progress.begin("x")
    progress.done()


def test_plan_pipeline_steps_full_bbox_zip():
    steps = plan_pipeline_steps(
        bbox=(1, 2, 3, 4),
        reused_from=None,
        want_zip=True,
        want_refs=True,
    )
    assert steps[0] == "stažení LiDAR"
    assert "prepare LiDAR" in steps
    assert steps[-1] == "OOM / ZIP"
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
    assert "prepare LiDAR" in steps
    assert "referenční podklady" not in steps
