"""Nasazení bez přerušení jobu: .deploy-drain v aplikaci a update-nas-ab.sh s mocky."""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app import db

ROOT = Path(__file__).resolve().parents[1]


class _FakeThread:
    started: list[str] = []

    def __init__(self, target, args, daemon, **_kw):
        self.args = args

    def start(self):
        _FakeThread.started.append(self.args[0])


def _worker(monkeypatch):
    import app.worker as worker

    importlib.reload(worker)
    _FakeThread.started = []
    monkeypatch.setattr(worker.threading, "Thread", _FakeThread)
    return worker


def test_drain_queues_new_jobs_and_starts_them_after_deploy(monkeypatch, data_dir):
    worker = _worker(monkeypatch)
    job_id = db.create_job("a", "sprint_2m", {})["id"]
    drain = data_dir / ".deploy-drain"
    drain.touch()
    assert worker.is_draining()

    worker.enqueue(job_id)
    assert _FakeThread.started == []
    assert worker._running is None and worker._queue == [job_id]
    assert db.get_job(job_id)["status"] == "queued"  # přežije restart (requeue_after_restart)
    worker._start_next()
    assert _FakeThread.started == []

    drain.unlink()
    worker._start_next()  # totéž dělá hlídač _drain_watch
    assert _FakeThread.started == [job_id]
    assert db.get_job(job_id)["status"] == "running"


def test_forgotten_drain_file_expires(monkeypatch, data_dir):
    worker = _worker(monkeypatch)
    drain = data_dir / ".deploy-drain"
    drain.touch()
    old = time.time() - worker.drain_max_age_s() - 10
    os.utime(drain, (old, old))
    assert not worker.is_draining()


def test_health_reports_draining(client, data_dir):
    assert client.get("/api/health").json()["draining"] is False
    (data_dir / ".deploy-drain").touch()
    body = client.get("/api/health").json()
    assert body["draining"] is True and body["busy"] is False


def test_job_waiting_for_shared_slot_returns_to_queue_on_drain(monkeypatch, tmp_path, data_dir):
    import app.worker as worker
    from app.slot_lock import SharedSlot

    importlib.reload(worker)
    monkeypatch.setattr(worker.settings, "SHARED_SLOTS_DIR", tmp_path / "slots")
    monkeypatch.setattr(worker, "_start_next", lambda: None)
    popen_called = threading.Event()
    monkeypatch.setattr(worker, "_popen_job", lambda _jid: popen_called.set())
    job_id = db.create_job("ab", "sprint_2m", {})["id"]
    (data_dir / "jobs" / job_id).mkdir(parents=True, exist_ok=True)
    other = SharedSlot(tmp_path / "slots", owner="A")
    assert other.try_acquire()
    try:
        worker._running = job_id
        t = threading.Thread(target=worker._run, args=(job_id,), daemon=True)
        t.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and db.get_job(job_id)["phase"] != "waiting_server":
            time.sleep(0.05)
        (data_dir / ".deploy-drain").touch()
        t.join(10)
        assert not t.is_alive()
    finally:
        other.release()
    assert not popen_called.is_set()
    assert worker._running is None and worker._queue == [job_id]
    assert db.get_job(job_id)["status"] == "queued"


# --- update-nas-ab.sh s falešným docker / curl / synowebapi ---------------------------

FAKE_DOCKER = """#!/bin/sh
echo "docker $*" >> "$LOG"
case "$1 $2" in
  "compose version") exit 0 ;;
  "compose -f") case " $* " in *" up "*) [ -e data-test/.deploy-drain ] && echo "UP_WITH_DRAIN" >> "$LOG" ;; esac; exit 0 ;;
  "ps -q") echo abc123 ;;
  "inspect --format={{.Config.Image}}") echo ghcr.io/x/podkladarna:latest ;;
  "inspect --format={{.Image}}") echo sha256:old ;;
  "image inspect") echo "sha256:${NEW_IMAGE:-new}" ;;
esac
exit 0
"""

FAKE_CURL = """#!/bin/sh
case "$*" in
  *api/health*)
    n=$(cat "$STATE/health" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$STATE/health"
    [ -e data-test/.deploy-drain ] && echo "HEALTH_WITH_DRAIN" >> "$LOG"
    if [ "$n" -le "${BUSY_CALLS:-0}" ]; then echo '{"ok":true,"busy":true}'; else echo '{"ok":true,"busy":false}'; fi ;;
  *) printf 200 ;;
esac
"""


def _run_script(tmp_path: Path, *args: str, **env: str) -> tuple[subprocess.CompletedProcess, str]:
    if shutil.which("sh") is None:
        pytest.skip("bez POSIX sh")
    work = tmp_path / "nas"
    work.mkdir()
    for name in ("update-nas-ab.sh", "nas-lib.sh"):
        shutil.copy(ROOT / name, work / name)
    (work / ".env").write_text("GHCR_OWNER=pjanecekatlangmaster\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL), ("synowebapi", "#!/bin/sh\nexit 1\n")):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "log.txt"
    log.write_text("", encoding="utf-8")
    full_env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "LOG": str(log),
        "STATE": str(tmp_path),
        "NAS_POLL_S": "1",
        "NAS_DRAIN_GRACE_S": "0",
        **env,
    }
    proc = subprocess.run(
        ["sh", str(work / "update-nas-ab.sh"), *args],
        cwd=work, env=full_env, capture_output=True, text=True, timeout=60,
    )
    assert not (work / "data-test" / ".deploy-drain").exists()  # nikdy nezůstane viset
    return proc, log.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "args, env, rc, waited, deployed",
    [
        # job běží 2 kontroly → čeká s .deploy-drain, pak nasadí bez něj
        (("test", "--if-idle"), {"BUSY_CALLS": "2"}, 0, True, True),
        # job nedoběhne do limitu → nic nerestartuje
        (("test", "--wait-minutes=0"), {"BUSY_CALLS": "99"}, 1, True, False),
        # stejný image → nečeká a nic nezakládá
        (("test",), {"BUSY_CALLS": "99", "NEW_IMAGE": "old"}, 0, False, True),
        # --force → restart hned bez čekání
        (("test", "--force"), {"BUSY_CALLS": "99"}, 0, False, True),
    ],
)
def test_update_script_waits_for_running_job(tmp_path, args, env, rc, waited, deployed):
    proc, log = _run_script(tmp_path, *args, **env)
    assert proc.returncode == rc, proc.stdout + proc.stderr
    assert ("HEALTH_WITH_DRAIN" in log) is waited
    assert (" up -d " in log) is deployed
    # drain drží až do `up -d` (starý kontejner mezitím z fronty nic nespustí)
    assert ("UP_WITH_DRAIN" in log) is (waited and deployed)
