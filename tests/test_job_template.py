"""Zadání jobu pro formulář nové mapy (GET /api/jobs/{id}/template)."""

from __future__ import annotations

import pytest

BBOX = [14.4, 50.08, 14.42, 50.09]


@pytest.mark.parametrize(
    "preset, options, expect",
    [
        # Současný job: volby se vrátí tak, jak je formulář pošle.
        (
            "mtbo_10000",
            {
                "map_scale": 10000, "contour_interval": 5, "map_types": ["mtbo"],
                "contour_formlines": "sparse", "contour_smooth": "0", "veg_smooth": "2",
                "kp_vege_height": 2.5, "kp_cliff_symbol": "earth_bank",
                "kp_cliff_sensitivity": "high", "kp_osm_objects": True,
                "dtm_sprint": True, "dtm_forest": True, "output_georef": True,
                "output_references": False,
            },
            {
                "map_scale": "10000", "contour_interval": "5", "map_types": ["mtbo"],
                "contour_formlines": "sparse", "contour_smooth": "0", "veg_smooth": "2",
                "kp_vege_height": "2.5", "kp_cliff_symbol": "earth_bank",
                "kp_cliff_sensitivity": "high", "kp_osm_objects": True, "dtm": "1",
                "output_georef": True, "output_references": False,
            },
        ),
        # Starší job jen s preset_id: měřítko z presetu, chybějící volby výchozí.
        (
            "sprint_2m",
            {"kp_cliff_symbol": "rock_face", "kp_osm_benches": True, "veg_smooth": "round"},
            {
                "map_scale": "4000", "map_types": [], "kp_cliff_symbol": "auto",
                "kp_osm_objects": True, "veg_smooth": "1", "contour_smooth": "1",
                "kp_vege_height": "2", "dtm": "auto", "output_references": True,
            },
        ),
        # Les bez uložených typů map: výchozí podle měřítka; DTM vypnuté u obou.
        (
            "forest_10000",
            {"map_scale": 10000, "dtm_sprint": False, "dtm_forest": False},
            {"map_scale": "10000", "map_types": ["les", "mtbo"], "dtm": "0"},
        ),
    ],
)
def test_job_template_form(client, preset, options, expect):
    import app.db as db

    job = db.create_job("Nusle park", preset, {"bbox_wgs84": BBOX, **options})
    r = client.get(f"/api/jobs/{job['id']}/template")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["name"] == "Nusle park"
    assert data["bbox_wgs84"] == BBOX
    for key, value in expect.items():
        assert data["form"][key] == value, key


def test_job_template_hides_private_and_reuse_only_when_finished(client):
    import app.db as db

    job = db.create_job(
        "Tajná",
        "forest_10000",
        {"bbox_wgs84": BBOX, "private": True, "notify_email": "a@b.cz", "client_ip": "1.2.3.4"},
    )
    lidar = db.JOBS_DIR / job["id"] / "work" / "lidar"
    lidar.mkdir(parents=True, exist_ok=True)
    (lidar / "merged_crop.laz").write_bytes(b"x")
    r = client.get(f"/api/jobs/{job['id']}/template")
    body = r.text
    assert "a@b.cz" not in body and "1.2.3.4" not in body
    assert r.json()["source_private"] is True
    # Běžící job: LAZ ještě není hotový, nepřebírat.
    assert r.json()["reuse_job_id"] is None
    db.update_job(job["id"], status="done")
    assert client.get(f"/api/jobs/{job['id']}/template").json()["reuse_job_id"] == job["id"]
    # Ani detail jobu neposílá IP a e-mail.
    detail = client.get(f"/api/jobs/{job['id']}").json()
    assert "notify_email" not in detail["options"] and "client_ip" not in detail["options"]
    assert client.get("/api/jobs/neexistuje/template").status_code == 404


def test_job_template_without_bbox(client):
    import app.db as db

    job = db.create_job("Bez výřezu", "forest_10000", {})
    data = client.get(f"/api/jobs/{job['id']}/template").json()
    assert data["bbox_wgs84"] is None
    assert data["notes"]
