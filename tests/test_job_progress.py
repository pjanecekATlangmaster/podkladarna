import pytest

from app.pipeline import job_progress as jp
from app.pipeline.job_progress import (
    BGT_STEP,
    DTM_STEP,
    MAP_PHASES,
    PREVIEW_STEP,
    ZIP_STEP,
    JobProgress,
    MapPhaseTracker,
    format_elapsed,
    indented_log,
    omitted_steps,
    plan_pipeline_steps,
)


class _FakeProgress:
    def __init__(self) -> None:
        self.begins: list[str] = []

    def begin(self, label: str) -> None:
        self.begins.append(label)


def test_map_phase_tracker_two_tags_waits_for_slowest():
    p = _FakeProgress()
    t = MapPhaseTracker(p, ["les", "mtbo"])
    t.start()
    assert p.begins == [MAP_PHASES[0]]
    t.report("les", MAP_PHASES[1])
    t.report("les", MAP_PHASES[2])
    assert p.begins == [MAP_PHASES[0]]
    t.report("mtbo", MAP_PHASES[2])  # mezifáze 1 se přeskočí
    assert p.begins == [MAP_PHASES[0], MAP_PHASES[2]]
    # opakované / zpětné hlášení, neznámý tag i label nic nemění
    for tag, label in [("mtbo", MAP_PHASES[2]), ("mtbo", MAP_PHASES[1]), ("les", MAP_PHASES[1]),
                       ("sprint", MAP_PHASES[4]), ("les", "neznámá fáze")]:  # fmt: skip
        t.report(tag, label)
    assert p.begins == [MAP_PHASES[0], MAP_PHASES[2]]


def test_map_phase_tracker_single_tag_begins_every_phase():
    p = _FakeProgress()
    t = MapPhaseTracker(p, ["sprint"])
    t.start()
    for label in MAP_PHASES[1:]:
        t.report("sprint", label)
    assert p.begins == list(MAP_PHASES)


def test_map_phase_hook_swallows_errors_and_returns_previous():
    got: list[str] = []
    jp.map_phase(MAP_PHASES[0])  # bez hooku nic
    hook = got.append
    prev = jp.set_map_phase_hook(hook)
    try:
        jp.map_phase(MAP_PHASES[1])
        assert got == [MAP_PHASES[1]]

        def boom(_label: str) -> None:
            raise RuntimeError("x")

        assert jp.set_map_phase_hook(boom) is hook
        jp.map_phase(MAP_PHASES[2])  # výjimka se spolkne
    finally:
        jp.set_map_phase_hook(prev)


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


def test_job_progress_unavailable_and_reused():
    lines: list[str] = []
    progress = JobProgress(lines.append, ["a", "b", "c"])
    progress.reuse("a")
    assert lines[-1] == "[1/3] A – převzato z předchozího jobu"
    progress.begin("b")
    progress.done(unavailable="kresba z OSM a ZABAGED")
    assert lines[-1] == "[2/3] B – nedostupné (kresba z OSM a ZABAGED)"
    progress.begin("c")
    assert lines[-1].startswith("[3/3] C · ")
    timings = progress.timings()
    assert timings[0]["reason"] == "z předchozího jobu"
    assert timings[1]["unavailable"] is True


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
    assert steps[steps.index("RÚIAN a AOPK"):] == [
        "RÚIAN a AOPK", DTM_STEP, *MAP_PHASES, PREVIEW_STEP, ZIP_STEP
    ]
    assert steps[-1] == "balení ZIP"
    assert "referenční podklady" in steps
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


@pytest.mark.parametrize(
    ("tech", "expected_step", "absent"),
    [("nl_bgt", BGT_STEP, DTM_STEP), ("ch_av", "data AV", DTM_STEP), ("at_fmzk", "data FMZK", DTM_STEP), ("de_sb", "data Straßenbefahrung", DTM_STEP),
     ("fr_pdv", "data Plan de voirie", DTM_STEP), ("ee_etak", "data ETAK", DTM_STEP), ("sk_zbgis", "data ZBGIS", DTM_STEP),
     ("be_grb", "data GRB", DTM_STEP), ("de_alkis", "data ALKIS", DTM_STEP), ("dk_geodanmark", "data GeoDanmark", DTM_STEP), ("us_city", "data Planimetrics", DTM_STEP),
     ("none", None, DTM_STEP)],
)
def test_plan_steps_abroad_without_czech_sources(tech, expected_step, absent):
    """Mimo Česko plán bez ZABAGED, RÚIAN a LiDARu; místo „DTM kraje“ obdoba země (BGT) nebo nic."""
    kw = {"cz_sources": False, "raster_elevation": True, "technical_map": tech}
    steps = plan_pipeline_steps(bbox=(1, 2, 3, 4), reused_from=None, want_zip=True, want_refs=True, **kw)
    for step in ("stažení ZABAGED", "příprava ZABAGED", "RÚIAN a AOPK", "stažení LiDAR", "příprava LiDAR", absent):
        assert step not in steps
    if expected_step:
        assert steps[steps.index(expected_step) + 1] == MAP_PHASES[0]
    lines: list[str] = []
    progress = JobProgress(lines.append, steps, clock=lambda: 0.0, omitted=omitted_steps(**kw))
    progress.skip("stažení ZABAGED", reason="mimo Česko")  # vynechaný krok neposune číslování ani nic nevypíše
    progress.begin("mřížka mapy")
    assert lines == [f"[1/{len(steps)}] Mřížka mapy · 0:00"]


@pytest.mark.parametrize(
    ("state", "raises", "expected_end"),
    [(None, False, "hotovo za 0:00"), ("nedostupné", False, "nedostupné"), (None, True, "selhalo")],
)
def test_background_task_lines(state, raises, expected_end):
    lines: list[str] = []
    try:
        with jp.background_task(lines.append, "Ortofoto · test") as task:
            task.state = state
            if raises:
                raise RuntimeError("x")
    except RuntimeError:
        pass
    assert lines == ["Na pozadí: Ortofoto - test · běží", f"Na pozadí: Ortofoto - test · {expected_end}"]
