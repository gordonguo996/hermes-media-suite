"""Regression tests for stopping the whole agent tree before rollback.

The updater spawns a venv ``pythonw.exe`` redirector which starts
``runtime\\pythonw.exe``.  Killing only the Popen object leaves the runtime
child holding ``run/agent.lock`` and port 9223, so restoring the old files can
race a live new agent.  These tests use real local process trees (Linux and
Windows both work); no psutil is required.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from winport import process_identity, updater  # noqa: E402
from winport.lock import lock_is_held  # noqa: E402


TOKEN = "0123456789abcdef0123456789abcdef"
TARGET = "1.2.193"


HOLDER = r"""
import json, os, socket, sys, time
from pathlib import Path
from winport.lock import FileLock

data = Path(sys.argv[1])
info = Path(sys.argv[2])
lock_path = data / "run" / "agent.lock"
lock_path.parent.mkdir(parents=True, exist_ok=True)
lock = FileLock(lock_path)
assert lock.acquire(), "could not take agent lock"
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.bind(("127.0.0.1", 0))
server.listen(1)
info.write_text(json.dumps({"pid": os.getpid(),
                            "port": server.getsockname()[1]}),
                encoding="utf-8")
while True:
    connection, _ = server.accept()
    connection.close()
"""


LAUNCHER = r"""
import subprocess, sys, time
subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2], sys.argv[3]],
                 env=dict(__import__("os").environ))
while True:
    time.sleep(1)
"""


LISTENER = r"""
import json, os, socket, sys, time
from pathlib import Path

info = Path(sys.argv[1])
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.bind(("127.0.0.1", 0))
server.listen(1)
info.write_text(json.dumps({"pid": os.getpid(),
                            "port": server.getsockname()[1]}),
                encoding="utf-8")
while True:
    connection, _ = server.accept()
    connection.close()
"""


def _wait_until(predicate, *, timeout: float = 20.0, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition was not reached before timeout")


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "agent")
    return env


def _start_holder(tmp_path: Path):
    script = tmp_path / "holder.py"
    script.write_text(HOLDER, encoding="utf-8")
    data = tmp_path / "data"
    info_path = tmp_path / "holder.json"
    proc = subprocess.Popen([sys.executable, str(script), str(data),
                             str(info_path)], env=_env())
    info = _wait_until(lambda: (json.loads(info_path.read_text(encoding="utf-8"))
                                if info_path.exists() else None))
    _wait_until(lambda: updater._pid_alive(info["pid"]))
    _wait_until(lambda: lock_is_held(data / "run" / "agent.lock"))
    return proc, data, info


def _start_tree(tmp_path: Path):
    holder = tmp_path / "holder.py"
    holder.write_text(HOLDER, encoding="utf-8")
    launcher = tmp_path / "launcher.py"
    launcher.write_text(LAUNCHER, encoding="utf-8")
    data = tmp_path / "data"
    info_path = tmp_path / "holder.json"
    proc = subprocess.Popen([sys.executable, str(launcher), str(holder),
                             str(data), str(info_path)], env=_env())
    info = _wait_until(lambda: (json.loads(info_path.read_text(encoding="utf-8"))
                                if info_path.exists() else None))
    _wait_until(lambda: updater._pid_alive(info["pid"]))
    _wait_until(lambda: lock_is_held(data / "run" / "agent.lock"))
    return proc, data, info


def _start_listener(tmp_path: Path):
    script = tmp_path / "listener.py"
    script.write_text(LISTENER, encoding="utf-8")
    info_path = tmp_path / "listener.json"
    proc = subprocess.Popen([sys.executable, str(script), str(info_path)],
                            env=_env())
    info = _wait_until(lambda: (json.loads(info_path.read_text(encoding="utf-8"))
                                if info_path.exists() else None))
    _wait_until(lambda: updater._pid_alive(info["pid"]))
    return proc, info


def _kill(proc):
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _kill_pid(pid):
    try:
        if updater._pid_alive(pid):
            os.kill(int(pid), 15)
    except (OSError, TypeError, ValueError):
        pass


def _job(tmp_path: Path, port: int, **extra) -> dict:
    program = tmp_path / "program"
    program.mkdir(parents=True, exist_ok=True)
    job = {
        "data": str(tmp_path / "data"),
        "program": str(program),
        "health_url": f"http://127.0.0.1:{port}/healthz",
        "target_version": TARGET,
    }
    job.update(extra)
    return job


def _write_marker(data: Path, marker: dict) -> None:
    run = data / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "agent-ready.json").write_text(
        json.dumps(marker), encoding="utf-8")


def test_stop_agent_process_tree_kills_redirector_and_runtime(tmp_path):
    parent, data, info = _start_tree(tmp_path)
    try:
        job = _job(tmp_path, info["port"], new_pid=parent.pid)
        assert updater._panel_port_open(job)
        assert lock_is_held(data / "run" / "agent.lock")

        ok, reason = updater._stop_agent_process_tree(
            job, parent, timeout=5, release_timeout=5)

        assert ok, reason
        assert parent.poll() is not None
        assert not updater._pid_alive(info["pid"])
        assert not lock_is_held(data / "run" / "agent.lock")
        assert not updater._panel_port_open(job)
    finally:
        _kill(parent)
        _kill_pid(info["pid"])


def test_stop_agent_process_tree_uses_owned_marker_when_redirector_exited(tmp_path):
    child, data, info = _start_holder(tmp_path)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=10)
    dead_pid = dead.pid
    try:
        _write_marker(data, {
            "pid": dead_pid,
            "agent_pid": info["pid"],
            "pid_chain": [dead_pid, info["pid"]],
            "ready": True,
            "version": TARGET,
            "instance": TOKEN,
        })
        job = _job(tmp_path, info["port"], new_pid=dead_pid,
                   new_instance_token=TOKEN)

        ok, reason = updater._stop_agent_process_tree(
            job, None, timeout=5, release_timeout=5)

        assert ok, reason
        assert not updater._pid_alive(info["pid"])
        assert not lock_is_held(data / "run" / "agent.lock")
        assert not updater._panel_port_open(job)
    finally:
        _kill(child)
        _kill_pid(info["pid"])


def test_stop_agent_process_tree_falls_back_to_agent_lock_owner(tmp_path):
    child, data, info = _start_holder(tmp_path)
    try:
        job = _job(tmp_path, info["port"])
        _wait_until(lambda: updater._agent_lock_owner_pid(job) == info["pid"])

        ok, reason = updater._stop_agent_process_tree(
            job, None, timeout=5, release_timeout=5)

        assert ok, reason
        assert not updater._pid_alive(info["pid"])
        assert not lock_is_held(data / "run" / "agent.lock")
        assert not updater._panel_port_open(job)
    finally:
        _kill(child)
        _kill_pid(info["pid"])


def test_stop_agent_process_tree_ignores_unowned_marker(tmp_path):
    child, info = _start_listener(tmp_path)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=10)
    dead_pid = dead.pid
    try:
        _write_marker(tmp_path / "data", {
            "pid": dead_pid,
            "agent_pid": info["pid"],
            "pid_chain": [dead_pid, info["pid"]],
            "ready": True,
            "version": TARGET,
            "instance": "f" * 32,
        })
        job = _job(tmp_path, info["port"], new_pid=dead_pid,
                   new_instance_token=TOKEN)

        ok, reason = updater._stop_agent_process_tree(
            job, None, timeout=1, release_timeout=0.3)

        assert not ok
        assert "not released" in reason
        assert updater._pid_alive(info["pid"])
    finally:
        _kill(child)
        _kill_pid(info["pid"])


class _FakeProc:
    def __init__(self, pid: int):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return None


def _journal(tmp_path: Path):
    data = tmp_path / "data"
    program = tmp_path / "program"
    program.mkdir(parents=True, exist_ok=True)
    ready = data / "run" / "update" / "supervisor.ready"
    ready.parent.mkdir(parents=True, exist_ok=True)
    job_path = ready.parent / "transaction.json"
    job = {
        "schema": 1,
        "status": "prepared",
        "program": str(program),
        "data": str(data),
        "from_version": "1.2.191",
        "target_version": TARGET,
        "old_pid": 999999,
        "health_url": "http://127.0.0.1:9/healthz",
        "files": [],
        "stage_dir": str(tmp_path / "stage"),
        "backup_dir": str(tmp_path / "backup"),
        "agent_command": [sys.executable, "-c", "pass"],
        "agent_env": {},
        "ready_path": str(ready),
        "health_timeout_seconds": 0.1,
        "tunnel_timeout_seconds": 0.1,
        "journal_path": str(job_path),
    }
    updater.write_json(job_path, job)
    return job_path


def test_supervise_rollback_cleans_tree_before_restoring_files(tmp_path, monkeypatch):
    job_path = _journal(tmp_path)
    calls = []
    monkeypatch.setattr(updater, "_wait_old_agent_stopped",
                        lambda job, timeout, sleep: True)
    monkeypatch.setattr(updater, "apply_update", lambda job: job)
    monkeypatch.setattr(updater, "_wait_for_health",
                        lambda *args, **kwargs: (False, "health failed"))

    def cleanup(job, proc, **kwargs):
        calls.append("cleanup")
        return True, "ok"

    def rollback(job):
        calls.append("rollback")
        return job

    monkeypatch.setattr(updater, "_stop_agent_process_tree", cleanup)
    monkeypatch.setattr(updater, "rollback_update", rollback)

    result = updater.supervise_job(
        job_path, spawn_agent=lambda job: _FakeProc(4321),
        health_timeout=0.1, tunnel_timeout=0.1, sleep=lambda _: None)

    assert calls[:2] == ["cleanup", "rollback"]
    assert result["rolled_back"] is False
    assert "old version did not become healthy" in result["reason"]


def test_supervise_rollback_refuses_restore_when_tree_will_not_stop(tmp_path, monkeypatch):
    job_path = _journal(tmp_path)
    calls = []
    monkeypatch.setattr(updater, "_wait_old_agent_stopped",
                        lambda job, timeout, sleep: True)
    monkeypatch.setattr(updater, "apply_update", lambda job: job)
    monkeypatch.setattr(updater, "_wait_for_health",
                        lambda *args, **kwargs: (False, "health failed"))

    def cleanup(job, proc, **kwargs):
        calls.append("cleanup")
        return False, "agent runtime not released (lock_held=True, panel_port_open=True)"

    def rollback(job):
        calls.append("rollback")
        return job

    monkeypatch.setattr(updater, "_stop_agent_process_tree", cleanup)
    monkeypatch.setattr(updater, "rollback_update", rollback)

    result = updater.supervise_job(
        job_path, spawn_agent=lambda job: _FakeProc(4321),
        health_timeout=0.1, tunnel_timeout=0.1, sleep=lambda _: None)

    assert calls == ["cleanup"]
    assert result["rolled_back"] is False
    assert "did not release its runtime" in result["reason"]
    assert "panel_port_open=True" in result["reason"]


def test_descendant_pids_walks_redirector_child_chain():
    parents = {100: 1, 200: 100, 300: 200, 999: 1}
    assert process_identity.descendant_pids([100], parents=parents) == [100, 200, 300]
    assert process_identity.descendant_pids([300], parents=parents) == [300]
