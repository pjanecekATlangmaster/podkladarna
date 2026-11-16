"""Obrázky podkladů v ZIPu podle licence zdroje (zip_images, registr image_in_zip)."""
from __future__ import annotations

import fnmatch
import zipfile
from pathlib import Path

import pytest

from app.pipeline import zip_images
from app.pipeline.online_templates import online_labels
from app.pipeline.package_oom import build_oom_zip, oom_readme
from app.pipeline.reference_layers import HILLSHADE_VARIANTS, _ref_png_key


def _all_reference_keys() -> set[str]:
    keys = set(online_labels())
    keys |= {"orthophoto", "orthophoto_r0c1", "katastr_vector", "sentinel2_2026-10-01", "vyska_vegetace"}
    keys |= {_ref_png_key(n) for n in ("osm.png", "mapa_ztm.png", "katastr.png", "dtm_plochy.png",
                                       "dtm_linie.png", "dmpok_nahled.png")}
    keys |= {k for k, *_r in HILLSHADE_VARIANTS}
    keys |= zip_images.OWN_RENDER_KEYS
    return keys


def test_every_reference_key_has_one_source_and_status():
    sources = zip_images._sources()
    for key in sorted(_all_reference_keys()):
        assert zip_images.image_in_zip(key) in {"allowed", "forbidden", "unclear"}
        if key in zip_images.OWN_RENDER_KEYS:
            continue
        hits = [s["id"] for s in sources if any(fnmatch.fnmatchcase(key, p) for p in s["reference_keys"])]
        assert len(hits) == 1, f"{key}: {hits}"
    for s in sources:
        assert s.get("image_in_zip") in {"allowed", "forbidden", "unclear"}, s["id"]
        if s["image_in_zip"] == "forbidden":
            for lang in ("cs", "en"):
                assert {"name", "why", "online"} <= set(s["zip_notice"][lang]), s["id"]


@pytest.mark.parametrize(
    "country, expected",
    [("CZ", ["osm_tiles"]), ("AT", ["osm_tiles"]), ("IT", ["osm_tiles", "it_wms"])],
)
def test_withheld_sources_per_country(country, expected):
    assert zip_images.withheld_sources(country) == expected


def test_withhold_images_drops_forbidden_rasters_keeps_online(tmp_path: Path):
    refs = tmp_path / "references"
    refs.mkdir()
    built = {}
    for key, name in (("osm", "osm.png"), ("online_osm", "osm.vrt"), ("ztm", "mapa_ztm.png"),
                      ("online_it_ortho", "ortofoto_trento.jpg")):
        (refs / name).write_bytes(b"x")
        built[key] = refs / name
    (refs / "osm.pgw").write_text("1", encoding="utf-8")
    out = zip_images.withhold_images(built)
    assert set(out) == {"online_osm", "ztm"}
    assert not (refs / "osm.png").exists() and not (refs / "osm.pgw").exists()
    assert (refs / "osm.vrt").exists()


@pytest.mark.parametrize("lang, notice", [("cs", "CHYBEJICI_PODKLADY.txt"), ("en", "MISSING_BASE_MAPS.txt")])
def test_zip_leaves_out_osm_image_with_notice(tmp_path: Path, lang, notice):
    kp = tmp_path / "kp"
    kp.mkdir()
    refs = tmp_path / "references"
    refs.mkdir()
    for name in ("osm.png", "mapa_ztm.png", "osm.vrt"):
        (refs / name).write_bytes(b"x")
    meta = {"label": "t", "scale": 10000, "lang": lang, "country": "CZ", "withheld_images": ["osm_tiles"],
            "reference_layers": ["mapa_ztm.png", "osm.vrt"]}
    dest = tmp_path / "out.zip"
    build_oom_zip(kp, dest, zabaged_clean=None, metadata=meta, reference_dir=refs)
    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
        text = zf.read(f"references/{notice}").decode("utf-8")
        readme = zf.read("README_OOM.txt").decode("utf-8")
    assert "references/osm.png" not in names
    assert {"references/mapa_ztm.png", "references/osm.vrt"} <= names
    assert "openstreetmap.org" in text and "osm.vrt" in text
    assert notice in readme


def test_readme_without_withheld_is_unchanged():
    meta = {"label": "t", "scale": 10000, "country": "CZ", "reference_layers": ["osm.vrt"]}
    assert "CHYBEJICI_PODKLADY" not in oom_readme(meta)
