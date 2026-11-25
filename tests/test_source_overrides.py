"""Rychlá oprava zdrojů výšek na serveru bez image (``app.source_overrides``)."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

PATCH_SRC = '''
from dataclasses import dataclass
from pathlib import Path

@dataclass
class SourceRasters:
    dtm: Path
    dsm: Path | None = None
    vertical_offset: object = None

class ElevationUnavailable(RuntimeError):
    pass

def _patched(bounds, out_dir, log=None):
    raise ElevationUnavailable("z opravy")

PROVIDERS = {"si_clss": _patched}
'''


@pytest.fixture()
def ov(data_dir: Path):
    import importlib

    import app.source_overrides as ov

    importlib.reload(ov)
    return ov


def _install_patch(ov, src: str = PATCH_SRC, *, version: str = "99.0.0", source: str = "si_clss") -> dict:
    data = src.encode()
    digest = hashlib.sha256(data).hexdigest()
    ov.patches_dir().mkdir(parents=True, exist_ok=True)
    (ov.patches_dir() / f"{digest}.py").write_bytes(data)
    entry = {"source": source, "module": ov.PATCH_MODULE, "commit": "a" * 40, "version": version,
             "sha256": digest, "installed_at": "2026-11-25T08:00:00+00:00"}
    ov._write_manifest({"patches": [entry]})
    return entry


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("si_clss:\n  workers: 1\n  note: x\n", True),
        ("si_clss:\n  disabled: true\n", True),
        ("si_clss:\n  workers: 20\n", False),
        ("si_clss:\n  url: https://x\n", False),
        ("cz_openzu:\n  workers: 1\n", False),
        ("[1, 2]\n", False),
        ("si_clss: [\n", False),
    ],
)
def test_tuning_validation(ov, text, ok):
    ov.base_dir().mkdir(parents=True, exist_ok=True)
    ov.tuning_path().write_text(text, encoding="utf-8")
    entries, errors = ov.load_tuning()
    assert (not errors) is ok
    assert bool(entries) is ok  # neplatný soubor se nepoužije vůbec


def test_workers_default_and_override(ov):
    assert ov.workers("si_clss", 2) == 2
    ov.base_dir().mkdir(parents=True, exist_ok=True)
    ov.tuning_path().write_text("si_clss:\n  workers: 1\n", encoding="utf-8")
    assert ov.workers("si_clss", 2) == 1
    assert ov.workers("pt_dgt", 2) == 2


def test_off_source_rejected_and_not_started(ov, monkeypatch):
    from app import countries, source_gate
    from app.pipeline import elevation_raster as er

    ov.base_dir().mkdir(parents=True, exist_ok=True)
    ov.tuning_path().write_text("si_clss:\n  disabled: true\n  note: 429\n", encoding="utf-8")
    with pytest.raises(er.ElevationUnavailable, match="vypnutý"):
        er.resolve_provider("si_clss")
    si = [c.code for c in countries.load_countries().values() if c.elevation == "si_clss"]
    assert si
    assert source_gate.check(si[0])["key"] == "si_clss"
    assert source_gate.check(si[0], has_fallback=True) is None
    assert source_gate.check("CZ") is None


def test_patch_used_only_for_its_source(ov):
    from app.pipeline import elevation_raster as er

    _install_patch(ov)
    lines: list[str] = []
    provider = er.resolve_provider("si_clss", log=lines.append)
    assert provider is not er.PROVIDERS["si_clss"]
    # výjimka z opravy je táž třída jako v image (volající ji chytá)
    with pytest.raises(er.ElevationUnavailable, match="z opravy"):
        provider((0, 0, 1, 1), Path("."), None)
    assert any(line.startswith("Oprava zdroje si_clss: kód z commitu aaaaaaaaaa") for line in lines)
    assert er.resolve_provider("pt_dgt") is er.PROVIDERS["pt_dgt"]


@pytest.mark.parametrize("case", ["old_version", "bad_hash", "broken", "other_fields"])
def test_bad_patch_falls_back_to_image(ov, case):
    from app.pipeline import elevation_raster as er

    src = PATCH_SRC
    if case == "broken":
        src += "\nimport neexistujici_modul_xyz\n"
    if case == "other_fields":
        src = src.replace("    vertical_offset: object = None", "    vertical_offset: object = None\n    navic: int = 0")
    entry = _install_patch(ov, src, version="1.0.0" if case == "old_version" else "99.0.0")
    if case == "bad_hash":
        (ov.patches_dir() / f"{entry['sha256']}.py").write_text(src + "\n# zmena\n", encoding="utf-8")
    lines: list[str] = []
    assert er.resolve_provider("si_clss", log=lines.append) is er.PROVIDERS["si_clss"]
    assert any("nepoužita" in line or "selhala" in line for line in lines)


def _fake_github(files: dict[str, bytes], *, status: str = "ahead"):
    def fetch(path: str):
        if path.startswith("commits/"):
            return {"sha": "b" * 40}
        if path.startswith("compare/"):
            return {"status": status}
        name = path.split("contents/", 1)[1].split("?", 1)[0]
        data = files[name]
        import app.source_overrides as ov

        return {"encoding": "base64", "size": len(data), "content": base64.b64encode(data).decode(),
                "sha": ov.git_blob_sha(data)}

    return fetch


def test_install_and_remove(ov):
    files = {"app/settings.py": b'APP_VERSION = "99.1.0"\n', ov.PATCH_MODULE: PATCH_SRC.encode()}
    res = ov.install(["si_clss"], "bbbbbbb", fetch=_fake_github(files))
    assert res["version"] == "99.1.0"
    entry = ov.patch_entry("si_clss")
    assert entry["commit"] == "b" * 40 and ov.patch_state(entry) == (True, "aktivní")
    assert ov.remove(["si_clss"]) == ["si_clss"]
    assert ov.patch_entry("si_clss") is None
    assert json.loads((ov.patches_dir() / "manifest.prev.json").read_text())["patches"]


@pytest.mark.parametrize(
    ("case", "match"),
    [
        ("not_in_master", "není v master"),
        ("old_version", "není potřeba"),
        ("czech", "Česko"),
        ("unknown", "neznámý zdroj"),
        ("tampered", "otisk"),
        ("busy", "Běží"),
    ],
)
def test_install_refused(ov, monkeypatch, case, match):
    files = {"app/settings.py": b'APP_VERSION = "99.1.0"\n', ov.PATCH_MODULE: PATCH_SRC.encode()}
    if case == "old_version":
        files["app/settings.py"] = b'APP_VERSION = "1.0.0"\n'
    fetch = _fake_github(files, status="diverged" if case == "not_in_master" else "ahead")
    if case == "tampered":
        inner = fetch

        def fetch(path):  # noqa: F811
            meta = inner(path)
            if "elevation_raster" in path:
                meta["sha"] = "0" * 40
            return meta
    if case == "busy":
        monkeypatch.setattr(ov, "running_jobs", lambda: 1)
    source = {"czech": "cz_openzu", "unknown": "xx_nic"}.get(case, "si_clss")
    with pytest.raises(SystemExit, match=match):
        ov.install([source], "bbbbbbb", fetch=fetch)
    assert ov.patch_entry("si_clss") is None


def test_log_lines_translated():
    from app.log_i18n import translate_line

    for line in (
        "Oprava zdroje si_clss: kód z commitu abcdef1234 (verze 4.31.1, nainstalováno 2026-11-25) místo image 4.31.0",
        "Ladění zdrojů: si_clss – souběžnost 1 (tuning.yaml)",
    ):
        assert translate_line(line, "en") != line
