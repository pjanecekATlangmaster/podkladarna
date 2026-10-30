from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PODKLADARNA_DATA", str(tmp_path))

    import app.settings as settings

    importlib.reload(settings)
    import app.db as db

    importlib.reload(db)
    db.init_db()
    return tmp_path


@pytest.fixture()
def client(data_dir: Path):
    import app.db as db
    import app.worker as worker
    import app.main as main

    importlib.reload(worker)
    importlib.reload(main)
    db.init_db()

    from fastapi.testclient import TestClient

    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture()
def seen_generator_codes(monkeypatch: pytest.MonkeyPatch):
    """Opt-in špión: sbírá kódy předané do ``set_code`` / ``symbol_index_for_code``.

    Chování se nemění (volá se původní funkce). Obalí i moduly, které si
    ``symbol_index_for_code`` naimportovaly přes ``from … import``. Fixture vrací
    množinu viděných kódů (testy ji porovnávají s katalogem).
    """
    import sys

    import app.pipeline.oom_symbol_map as sm

    seen: set[str] = set()
    orig_set_code = sm.set_code
    orig_index = sm.symbol_index_for_code

    def spy_set_code(preset_id: str, scale: int, gen_code: str):
        seen.add(gen_code)
        return orig_set_code(preset_id, scale, gen_code)

    def spy_index(preset_id: str, scale: int, code: str):
        seen.add(code)
        return orig_index(preset_id, scale, code)

    monkeypatch.setattr(sm, "set_code", spy_set_code)
    for name, mod in list(sys.modules.items()):
        if name.startswith("app.") and getattr(mod, "symbol_index_for_code", None) is orig_index:
            monkeypatch.setattr(mod, "symbol_index_for_code", spy_index)
    yield seen


@pytest.fixture(autouse=True)
def _no_osm_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """OSM cache (DOWNLOADS_DIR) by mezi testy vracela cizí odpovědi."""
    monkeypatch.setenv("PODKLADARNA_OSM_CACHE_HOURS", "0")


@pytest.fixture(autouse=True)
def _corridor_seeds_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Testy koridoru silnice jsou na malých tvarech; semínka varianty E (12 m × 100 m) se zapínají jen v jejím testu."""
    from app.pipeline import dtm_sidewalks

    monkeypatch.setattr(dtm_sidewalks, "CORRIDOR_SEEDS_E", False)
