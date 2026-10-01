from app.pipeline.prepare_lidar import log_step


def test_log_step_explains_before_tool():
    lines: list[str] = []
    log_step(lines.append, "Připravuji DEM z DMR5G (terén pro vrstevnice a srázy)")
    log_step(lines.append, "Klasifikuji vegetaci z hustoty LiDAR odrazů (náhrada KP)")
    assert lines == [
        "Připravuji DEM z DMR5G (terén pro vrstevnice a srázy)…",
        "Klasifikuji vegetaci z hustoty LiDAR odrazů (náhrada KP)…",
    ]


def test_log_step_keeps_existing_ending():
    lines: list[str] = []
    log_step(lines.append, "Hotovo…")
    log_step(lines.append, "Konec.")
    assert lines == ["Hotovo…", "Konec."]


def test_log_step_silent_without_logger():
    log_step(None, "nic")
