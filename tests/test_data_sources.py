"""Registr zdrojů dat (configs/data_sources.yaml) a z něj generovaný docs/zdroje-dat.md."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

from app import source_status

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("data_sources_doc", ROOT / "scripts" / "data_sources_doc.py")
doc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(doc)

REQUIRED = {"id", "countries", "kinds", "name", "provider", "url", "role", "status", "license", "access",
            "checked", "issues"}


def _sources() -> list[dict]:
    return yaml.safe_load(doc.REGISTRY.read_text(encoding="utf-8"))["sources"]


def test_registry_fields_and_enums():
    ids = set()
    for s in _sources():
        assert REQUIRED <= set(s), s.get("id")
        assert s["id"] not in ids, s["id"]
        ids.add(s["id"])
        assert all(isinstance(c, str) for c in s["countries"]), s["id"]  # YAML: NO bez uvozovek = false
        assert set(s["kinds"]) <= set(doc.KINDS), s["id"]
        assert s["status"] in doc.STATUS, s["id"]
        assert s["access"] in doc.ACCESS, s["id"]
        assert s["license"]["class"] in doc.LICENSE_CLASS, s["id"]
        assert s["license"]["url"].startswith("https://"), s["id"]
        assert s["url"].startswith("https://"), s["id"]


def test_registry_covers_countries_providers_and_probes():
    sources = _sources()
    countries = yaml.safe_load(doc.COUNTRIES.read_text(encoding="utf-8"))
    covered = {c for s in sources for c in s["countries"]}
    assert set(countries) <= covered
    keys = {s.get("provider_key") for s in sources}
    for code, c in countries.items():
        for field in ("elevation", "technical_map"):
            value = c.get(field)
            if value and value != "none" and not str(value).startswith("local:"):
                assert value in keys, f"{code}.{field} = {value} chybí v registru"
    probes = {p for s in sources for p in doc.probes_of(s)}
    missing = [p.key for p in source_status.PROBES if p.key not in probes]
    assert not missing, f"sondy bez záznamu v registru: {missing}"


def test_doc_is_generated_from_registry():
    assert doc.DOC.read_text(encoding="utf-8") == doc.render(), (
        "docs/zdroje-dat.md neodpovídá registru – spusť python scripts/data_sources_doc.py"
    )
