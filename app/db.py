from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.settings import DB_PATH, JOBS_DIR


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                preset_id TEXT NOT NULL,
                status TEXT NOT NULL,
                phase TEXT,
                options_json TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                line TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES jobs(id)
            )
            """
        )
        conn.commit()


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def create_job(name: str, preset_id: str, options: dict[str, Any]) -> dict[str, Any]:
    job_id = uuid.uuid4().hex[:12]
    now = _utcnow()
    job_dir = JOBS_DIR / job_id
    for sub in ("input/dmr", "input/dmp", "input/zabaged", "work", "output"):
        (job_dir / sub).mkdir(parents=True, exist_ok=True)

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, name, preset_id, status, phase, options_json, error, created_at, updated_at)
            VALUES (?, ?, ?, 'pending', NULL, ?, NULL, ?, ?)
            """,
            (job_id, name, preset_id, json.dumps(options), now, now),
        )
        conn.commit()
    return get_job(job_id)


def get_job(job_id: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise KeyError(job_id)
    return _row_to_job(row)


def list_jobs() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100").fetchall()
    return [_row_to_job(r) for r in rows]


def update_job(job_id: str, **fields: Any) -> None:
    fields["updated_at"] = _utcnow()
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [job_id]
    with connect() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", vals)
        conn.commit()


def append_log(job_id: str, line: str) -> None:
    log_file = JOBS_DIR / job_id / "log.txt"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")
    with connect() as conn:
        conn.execute(
            "INSERT INTO job_logs (job_id, line, created_at) VALUES (?, ?, ?)",
            (job_id, line.rstrip(), _utcnow()),
        )
        conn.commit()


def get_logs(job_id: str, after_id: int = 0) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, line, created_at FROM job_logs WHERE job_id = ? AND id > ? ORDER BY id",
            (job_id, after_id),
        ).fetchall()
    return [{"id": r["id"], "line": r["line"], "at": r["created_at"]} for r in rows]


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    job_dir = JOBS_DIR / row["id"]
    return {
        "id": row["id"],
        "name": row["name"],
        "preset_id": row["preset_id"],
        "status": row["status"],
        "phase": row["phase"],
        "options": json.loads(row["options_json"]),
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "has_output": (job_dir / "output" / "podkladarna_output.zip").exists(),
        "has_preview": (job_dir / "output" / "pullautus.png").exists(),
    }
