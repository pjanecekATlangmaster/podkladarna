from pathlib import Path

from app.proj_env import ensure_proj_data


def test_ensure_proj_data_sets_env_when_proj_db_exists(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PROJ_DATA", raising=False)
    monkeypatch.delenv("PROJ_LIB", raising=False)
    fake = tmp_path / "proj"
    fake.mkdir()
    (fake / "proj.db").write_bytes(b"x")
    monkeypatch.setenv("PROJ_DATA", str(fake))
    assert ensure_proj_data() == str(fake)
