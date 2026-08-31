from __future__ import annotations

import importlib
import os
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
