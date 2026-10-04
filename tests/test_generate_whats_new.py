from __future__ import annotations

from scripts.generate_whats_new import (
    collect_entries,
    drop_milestone_duplicates,
    filter_recent_vs_milestones,
    load_milestones,
    render_yaml,
    summarize_by_day,
    to_czech_title,
)


def test_to_czech_title_known_phrases():
    assert "měřítko" in to_czech_title(
        "Replace map-type preset with scale and contour selects (v1.8)."
    ).casefold()
    veg = to_czech_title(
        "Fix color_map population in ensure_isom_color (fixes black vegetation in MTBO)"
    )
    assert "vegetace" in veg.casefold() or veg.startswith("Oprava")
    assert to_czech_title("fix: something broke").startswith("Oprava:")
    # Anglické „symbol“ nesmí vypnout překlad (dřív false positive v _looks_czech).
    bridge = to_czech_title(
        "Draw sprint OSM bridges as the connecting path symbol, not 512.1."
    )
    assert "most" in bridge.casefold()
    assert not bridge.startswith("Draw")
    mapper = to_czech_title(
        "Web Mapper náhled: ořez fialovým AOI a zrušení deklinace."
    )
    assert "náhled" in mapper.casefold()
    assert "deklinace" in mapper.casefold()
    assert "APP_VERSION" not in mapper
    kp = to_czech_title("Drop Karttapullautin runtime from tip pipeline.")
    assert "karttapullautin" in kp.casefold()


def test_collect_entries_from_git():
    entries = collect_entries(since_days=60, max_entries=5)
    assert isinstance(entries, list)
    assert len(entries) <= 5
    if entries:
        assert "date" in entries[0]
        assert "title" in entries[0]
        assert entries[0]["title"]
        # Denní souhrn: jeden záznam na den
        dates = [e["date"] for e in entries]
        assert len(dates) == len(set(dates))


def test_summarize_by_day_joins_top_titles():
    raw = [
        {
            "date": "2026-10-03",
            "title": "Zemní srázy: min. ~50 m",
            "body": "",
            "score": 80,
        },
        {
            "date": "2026-10-03",
            "title": "Vyšší panel běžících jobů",
            "body": "",
            "score": 40,
        },
        {
            "date": "2026-10-02",
            "title": "Bez KP: louky",
            "body": "",
            "score": 70,
        },
    ]
    out = summarize_by_day(raw, max_days=3, max_per_day=2)
    assert out[0]["date"] == "2026-10-03"
    assert "srázy" in out[0]["title"]
    assert ";" in out[0]["title"]
    assert out[1]["date"] == "2026-10-02"


def test_drop_milestone_duplicates_before_summary():
    raw = [
        {
            "date": "2026-10-03",
            "title": "Webový náhled mapy přes OpenOrienteering Mapper",
            "body": "",
            "score": 90,
        },
        {
            "date": "2026-10-03",
            "title": "Zemní srázy: min. ~50 m",
            "body": "",
            "score": 70,
        },
    ]
    milestones = [
        {
            "date": "2026-10-03",
            "title": "Webový náhled mapy přes OpenOrienteering Mapper",
            "body": "",
        }
    ]
    kept = drop_milestone_duplicates(raw, milestones)
    assert len(kept) == 1
    assert "srázy" in str(kept[0]["title"])
    # Zpětná kompatibilita helperu na hotových souhrnech
    recent = [
        {
            "date": "2026-10-03",
            "title": "Webový náhled mapy přes OpenOrienteering Mapper; Vyšší panel",
            "body": "",
        }
    ]
    out = filter_recent_vs_milestones(recent, milestones)
    assert len(out) == 1
    assert "panel" in out[0]["title"].casefold()


def test_load_milestones_file():
    ms = load_milestones()
    assert ms
    assert any("Karttapullautin" in m["title"] for m in ms)
    assert any("ZABAGED" in m["title"] for m in ms)
    assert any("Mapper" in m["title"] for m in ms)


def test_render_yaml_includes_milestones():
    text = render_yaml(
        "2026-09-16T12:00:00Z",
        [{"date": "2026-09-16", "title": "Test change", "body": ""}],
        [{"date": "2026-09-01", "title": "Milník KP", "body": ""}],
    )
    assert "AUTO-GENERATED" in text
    assert "Test change" in text
    assert "milestones:" in text
    assert "Milník KP" in text
    assert "whats_new_milestones.yaml" in text
