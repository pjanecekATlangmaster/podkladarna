"""Hlídač schválených oprav zdrojů z master (``app.source_fixes``)."""

from __future__ import annotations

import hashlib
import importlib
from pathlib import Path

import pytest

SHA = "b" * 40


@pytest.fixture()
def sf(data_dir: Path):
    import app.source_overrides as ov

    importlib.reload(ov)
    import app.source_fixes as sf

    importlib.reload(sf)
    return sf


def _fake_install(ov, calls: list):
    def install(sources, commit, *, force=False, fetch=None, base_mod=None):
        calls.append((list(sources), commit))
        data = f"# oprava {commit}\n".encode()
        digest = hashlib.sha256(data).hexdigest()
        ov.patches_dir().mkdir(parents=True, exist_ok=True)
        (ov.patches_dir() / f"{digest}.py").write_bytes(data)
        manifest = ov.read_manifest()
        manifest["patches"] = [e for e in manifest["patches"] if e.get("source") not in sources] + [
            {"source": s, "module": ov.PATCH_MODULE, "commit": commit, "version": "99.0.0", "sha256": digest,
             "installed_at": "2000-01-01T00:00:00+00:00"} for s in sources
        ]
        ov._write_manifest(manifest)
        return {"sources": sources, "commit": commit, "version": "99.0.0", "sha256": digest}

    return install


def _run(sf, text: str, *, idle=True):
    mails: list = []
    state = sf.sync(fetch=lambda: text, idle=lambda: idle, notify=lambda s, b: mails.append((s, b)))
    return state, mails


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("tuning: {}\npatches: []\n", True),
        ("", True),
        (f"tuning:\n  si_clss:\n    workers: 2\npatches:\n  - sources: [si_clss]\n    commit: {SHA}\n", True),
        ("patches:\n  - sources: [si_clss]\n    commit: abc1234\n", False),  # zkrácené SHA
        (f"patches:\n  - sources: [cz_openzu]\n    commit: {SHA}\n", False),  # Česko nikdy
        (f"patches:\n  - sources: [si_clss]\n    commit: {SHA}\n    url: x\n", False),
        ("tuning:\n  si_clss:\n    workers: 50\n", False),
        ("restart: true\n", False),
    ],
)
def test_manifest_validation(sf, text, ok):
    _, errors = sf.parse_manifest(text)
    assert (not errors) == ok, errors


def test_repo_manifest_is_valid(sf):
    text = (Path(__file__).resolve().parent.parent / sf.MANIFEST_PATH).read_text(encoding="utf-8")
    assert sf.parse_manifest(text)[1] == []


def test_tuning_applied_and_removed_manual_wins(sf):
    ov = sf.ov
    _, mails = _run(sf, "tuning:\n  si_clss:\n    workers: 2\n  de_he:\n    workers: 3\n")
    assert ov.workers("si_clss", 6) == 2 and mails
    ov.tuning_path().write_text("si_clss:\n  workers: 1\n", encoding="utf-8")
    assert ov.workers("si_clss", 6) == 1 and ov.workers("de_he", 6) == 3
    _, mails = _run(sf, "tuning: {}\n")
    assert ov.workers("de_he", 6) == 6 and not ov.tuning_auto_path().exists() and mails
    _, mails = _run(sf, "tuning: {}\n")
    assert not mails  # beze změny ticho


def test_patch_installed_only_when_idle_and_removed(sf, monkeypatch):
    ov = sf.ov
    calls: list = []
    monkeypatch.setattr(ov, "install", _fake_install(ov, calls))
    text = f"patches:\n  - sources: [si_clss]\n    commit: {SHA}\n"
    state, mails = _run(sf, text, idle=False)
    assert not calls and state["pending"] and not mails
    state, mails = _run(sf, text)
    assert calls == [(["si_clss"], SHA)] and ov.patch_entry("si_clss")["auto"] and state["applied"] == {"si_clss": SHA[:10]}
    _run(sf, text)
    assert len(calls) == 1  # už nainstalováno
    state, mails = _run(sf, "patches: []\n")
    assert ov.patch_entry("si_clss") is None and state["applied"] == {} and mails


def test_manual_patch_is_kept(sf):
    ov = sf.ov
    ov._write_manifest({"patches": [{"source": "de_he", "module": ov.PATCH_MODULE, "commit": "c" * 40}]})
    _run(sf, "patches: []\n")
    assert ov.patch_entry("de_he") is not None


def test_failed_job_rolls_back_and_commit_not_retried(sf, monkeypatch):
    from app import db

    ov = sf.ov
    calls: list = []
    monkeypatch.setattr(ov, "install", _fake_install(ov, calls))
    text = f"patches:\n  - sources: [si_clss]\n    commit: {SHA}\n"
    _run(sf, text)
    outage = db.create_job("a", "les", {"source_outage": "si_clss"})
    db.append_log(outage["id"], f"Oprava zdroje si_clss: kód z commitu {SHA[:10]} (verze 99.0.0) místo image")
    db.update_job(outage["id"], status="failed")
    _run(sf, text)
    assert ov.patch_entry("si_clss") is not None  # selhání na výpadku zdroje opravu nevrací
    job = db.create_job("b", "les", {})
    db.append_log(job["id"], f"Oprava zdroje si_clss: kód z commitu {SHA[:10]} (verze 99.0.0) místo image")
    db.update_job(job["id"], status="failed")
    state, mails = _run(sf, text)
    assert ov.patch_entry("si_clss") is None and state["rejected"] == {"si_clss": SHA}
    assert any("VRÁCENO" in b for _, b in mails) and len(calls) == 1
    _run(sf, "patches: []\n")
    assert sf.read_state()["rejected"] == {}


@pytest.mark.parametrize(
    ("msg", "in_image", "mailed"),
    [
        ("commit má verzi 4.0.0, image 4.34.0 je stejná nebo novější – oprava není potřeba", True, False),
        ("commit 0123 není v master (stav diverged)", False, True),
    ],
)
def test_install_refused(sf, monkeypatch, msg, in_image, mailed):
    def refuse(*a, **k):
        raise SystemExit(msg)

    monkeypatch.setattr(sf.ov, "install", refuse)
    state, mails = _run(sf, f"patches:\n  - sources: [si_clss]\n    commit: {SHA}\n")
    assert bool(state.get("in_image")) == in_image and bool(mails) == mailed
    assert ("si_clss" in state["rejected"]) == (not in_image)


def test_fetch_error_and_invalid_manifest_change_nothing(sf):
    ov = sf.ov
    _run(sf, "tuning:\n  si_clss:\n    workers: 2\n")

    def boom():
        raise OSError("síť")

    state = sf.sync(fetch=boom, idle=lambda: True, notify=lambda s, b: None)
    assert "nejde stáhnout" in state["last_error"] and ov.workers("si_clss", 6) == 2
    state, mails = _run(sf, "tuning:\n  si_clss:\n    workers: 99\n")
    assert "neplatný" in state["last_error"] and ov.workers("si_clss", 6) == 2 and len(mails) == 1
    _, mails = _run(sf, "tuning:\n  si_clss:\n    workers: 99\n")
    assert not mails  # tentýž neplatný soubor se hlásí jednou


def test_health_in_api(client):
    body = client.get("/api/health").json()
    assert body["source_fixes"]["auto"] is False and body["source_fixes"]["applied"] == {}
