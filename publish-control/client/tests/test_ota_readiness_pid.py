"""Regression tests for the Windows OTA post-restart readiness handshake.

The failure this guards against: a venv ``pythonw.exe`` redirector is spawned
by the updater supervisor, then it starts ``runtime\\pythonw.exe``.  The child
serves /healthz and writes ``agent-ready.json``.  1.2.191 compares its spawned
pid (the redirector) with ``marker["pid"]`` (the child), so every update fails.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from winport import process_identity, updater  # noqa: E402


TOKEN = "0123456789abcdef0123456789abcdef"
VERSION = "1.2.193"
HEALTH_URL = "http://127.0.0.1:9223/healthz"


def _write_marker(data: Path, marker: dict) -> None:
    run = data / "run"
    run.mkdir(parents=True)
    (run / "agent-ready.json").write_text(
        json.dumps(marker, ensure_ascii=False, indent=1), encoding="utf-8")


def _job(tmp_path: Path) -> dict:
    program = tmp_path / "program"
    program.mkdir()
    return {"data": str(tmp_path / "data"), "program": str(program),
            "health_url": HEALTH_URL}


def legacy_probe_health(job: dict, pid: int, version: str):
    """The exact 1.2.191 probe_health pid rule (updater.py:972-994)."""
    marker_path = Path(job["data"]) / "run" / "agent-ready.json"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        marker = {}
    health = updater._local_http_json(job["health_url"])
    if not health:
        return False, "healthz not reachable"
    if health.get("ok") is not True:
        return False, "healthz did not report ok"
    if health.get("version") != version:
        return False, "healthz version mismatch"
    if marker.get("ready") is not True:
        return False, "readiness marker is not ready"
    if marker.get("version") != version:
        return False, "readiness marker version mismatch"
    if int(marker.get("pid") or 0) != int(pid):
        return False, "readiness marker belongs to another process"
    instance = marker.get("instance")
    if not instance or health.get("instance") != instance:
        return False, "healthz instance does not match the readiness marker"
    verdict = updater.verify_program(Path(job["program"]))
    if not verdict.get("ok"):
        return False, "integrity check failed: " + str(verdict.get("reason") or "unknown")
    return True, "ok"


@pytest.fixture
def fake_health(monkeypatch):
    class Health:
        value = None

    def local_json(url, timeout=2.0):
        return Health.value

    # Fake the OS parent chain used by the ownership check: the runtime child
    # 10612 is the venv redirector 5648's direct child.
    parents = {10612: 5648, 5648: 0}
    monkeypatch.setattr(process_identity, "_parent_pid",
                        lambda pid: parents.get(int(pid), 0))
    monkeypatch.setattr(updater, "_local_http_json", local_json)
    monkeypatch.setattr(updater, "verify_program", lambda program: {"ok": True})
    return Health


def new_marker(launcher_pid: int, agent_pid: int, instance: str = TOKEN) -> dict:
    return {
        "pid": launcher_pid,
        "agent_pid": agent_pid,
        "pid_chain": [launcher_pid, agent_pid],
        "ready": True,
        "version": VERSION,
        "instance": instance,
        "panel_url": HEALTH_URL.rsplit("/", 1)[0] + "/",
    }


def _spawn_marker_chain(tmp_path: Path):
    agent_pid_file = tmp_path / "agent-pid.txt"
    output_file = tmp_path / "identity.json"
    launcher = textwrap.dedent(
        """
        import json, os, subprocess, sys
        child = subprocess.Popen([sys.executable, "-c", %r])
        print(json.dumps({"launcher_pid": os.getpid(), "agent_pid": child.pid}))
        raise SystemExit(child.wait())
        """
    ).strip()
    agent = textwrap.dedent(
        """
        import json, os
        from pathlib import Path
        from winport.process_identity import marker_identity
        Path(%r).write_text(json.dumps(marker_identity()), encoding="utf-8")
        Path(%r).write_text(str(os.getpid()), encoding="utf-8")
        """
    ).strip() % (str(output_file), str(agent_pid_file))
    launcher = launcher % agent
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "agent")
    env["WINPORT_INSTANCE_TOKEN"] = TOKEN
    env.pop("WINPORT_LAUNCHER_PID", None)
    proc = subprocess.run([sys.executable, "-c", launcher], env=env,
                          text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    reported = json.loads(proc.stdout.strip().splitlines()[-1])
    identity = json.loads(output_file.read_text(encoding="utf-8"))
    actual_agent_pid = int(agent_pid_file.read_text(encoding="utf-8"))
    return reported, identity, actual_agent_pid


def test_marker_pid_follows_the_extra_exec_layer(tmp_path):
    """supervisor -> venv redirector -> runtime agent: pid is the redirector."""
    reported, identity, actual_agent_pid = _spawn_marker_chain(tmp_path)
    assert reported["agent_pid"] == actual_agent_pid
    assert identity["pid"] == reported["launcher_pid"]
    assert identity["agent_pid"] == actual_agent_pid
    assert identity["pid_chain"] == [reported["launcher_pid"], actual_agent_pid]


def test_legacy_probe_accepts_marker_written_by_real_child_chain(tmp_path, fake_health):
    reported, identity, _ = _spawn_marker_chain(tmp_path)
    job = _job(tmp_path)
    _write_marker(Path(job["data"]), {**identity, "ready": True,
                                       "version": VERSION, "instance": TOKEN,
                                       "panel_url": "http://127.0.0.1:9223/"})
    fake_health.value = {"ok": True, "version": VERSION, "instance": TOKEN}
    ok, reason = legacy_probe_health(job, reported["launcher_pid"], VERSION)
    assert ok, reason


def test_normal_launch_keeps_historical_pid_semantics():
    identity = process_identity.marker_identity(
        environ={}, self_pid=200, parent_pid=100)
    assert identity == {"pid": 200, "agent_pid": 200, "pid_chain": [200]}


def test_explicit_launcher_pid_covers_another_exec_layer():
    identity = process_identity.marker_identity(
        environ={"WINPORT_INSTANCE_TOKEN": TOKEN,
                 "WINPORT_LAUNCHER_PID": "100"},
        self_pid=300, parent_pid=200)
    assert identity == {"pid": 100, "agent_pid": 300,
                        "pid_chain": [100, 300]}


def test_old_1_2_191_probe_accepts_new_marker(tmp_path, fake_health):
    job = _job(tmp_path)
    _write_marker(Path(job["data"]), new_marker(5648, 10612))
    fake_health.value = {"ok": True, "version": VERSION, "instance": TOKEN}

    ok, reason = legacy_probe_health(job, 5648, VERSION)
    assert ok, reason
    ok, reason = updater.probe_health(job, 5648, VERSION)
    assert ok, reason
    # The live runtime child is also a valid new-style target.
    ok, reason = updater.probe_health(job, 10612, VERSION)
    assert ok, reason


def test_old_probe_still_rejects_the_child_when_marker_is_old(tmp_path, fake_health):
    """Documents the exact 1.2.191 regression: child pid != spawned pid."""
    job = _job(tmp_path)
    marker = new_marker(5648, 10612)
    marker.pop("agent_pid")
    marker.pop("pid_chain")
    marker["pid"] = 10612
    _write_marker(Path(job["data"]), marker)
    fake_health.value = {"ok": True, "version": VERSION, "instance": TOKEN}

    ok, reason = legacy_probe_health(job, 5648, VERSION)
    assert not ok
    assert reason == "readiness marker belongs to another process"


def test_impostor_instance_is_still_rejected(tmp_path, fake_health):
    job = _job(tmp_path)
    _write_marker(Path(job["data"]),
                  new_marker(5648, 10612, instance="f" * 32))
    fake_health.value = {"ok": True, "version": VERSION, "instance": TOKEN}

    ok, reason = updater.probe_health(job, 5648, VERSION)
    assert not ok
    assert reason == "healthz instance does not match the readiness marker"
    ok, reason = legacy_probe_health(job, 5648, VERSION)
    assert not ok
    assert reason == "healthz instance does not match the readiness marker"


def test_bad_pid_chain_is_rejected(tmp_path, fake_health):
    job = _job(tmp_path)
    marker = new_marker(5648, 10612)
    marker["pid_chain"] = [9999, 10612]
    _write_marker(Path(job["data"]), marker)
    fake_health.value = {"ok": True, "version": VERSION, "instance": TOKEN}

    ok, reason = updater.probe_health(job, 5648, VERSION)
    assert not ok
    assert reason == "readiness marker has invalid pid evidence"


def test_tunnel_session_accepts_launcher_or_agent_pid(tmp_path, monkeypatch):
    job = _job(tmp_path)
    data = Path(job["data"])
    remote = data / "remote-channel"
    remote.mkdir(parents=True)
    (remote / "token").write_text("t" * 32, encoding="utf-8")
    (remote / "relay-port").write_text("23001", encoding="ascii")
    (remote / "session.json").write_text(
        json.dumps({"token": "t" * 32, "port": 19000,
                    "pid": 5648, "agent_pid": 10612,
                    "pid_chain": [5648, 10612]}), encoding="utf-8")
    logs = data / "logs"
    logs.mkdir()
    (logs / "remote-channel.log").write_text("SSH launched\n", encoding="utf-8")

    from winport import remote_channel
    parents = {10612: 5648, 5648: 0}
    monkeypatch.setattr(process_identity, "_parent_pid",
                        lambda pid: parents.get(int(pid), 0))
    monkeypatch.setattr(updater, "_remote_enabled", lambda job: True)
    monkeypatch.setattr(remote_channel, "relay_provisioning_status",
                        lambda layout: {"ok": True})
    assert updater._tunnel_snapshot_ok(job, 5648) == (True, "ok")
    assert updater._tunnel_snapshot_ok(job, 10612) == (True, "ok")
    assert updater._tunnel_snapshot_ok(job, 7777)[0] is False


def test_is_descendant_uses_the_real_parent_chain():
    parents = {300: 200, 200: 100, 100: 0}
    assert process_identity.is_descendant(300, 100, parent_of=parents.get)
    assert process_identity.is_descendant(200, 100, parent_of=parents.get)
    assert not process_identity.is_descendant(300, 400, parent_of=parents.get)


def test_rollback_legacy_marker_is_accepted_through_parent_chain(tmp_path, fake_health):
    """A rollback may leave the 1.2.191 marker shape (only child pid).

    New supervisors must still prove that the recorded child is a descendant
    of the pid they spawned; this is the parent/child rule that makes rollback
    work without weakening the check to a version-only check.
    """
    job = _job(tmp_path)
    _write_marker(Path(job["data"]), {
        "pid": 10612, "ready": True, "version": VERSION, "instance": TOKEN,
        "panel_url": "http://127.0.0.1:9223/"})
    fake_health.value = {"ok": True, "version": VERSION, "instance": TOKEN}

    ok, reason = updater.probe_health(job, 5648, VERSION)
    assert ok, reason
    # An unrelated supervisor pid is still rejected.
    ok, reason = updater.probe_health(job, 7777, VERSION)
    assert not ok
    assert reason == "readiness marker belongs to another process"


def test_new_marker_with_unrelated_agent_pid_is_rejected(tmp_path, fake_health):
    """The pid chain is not just decorative: 7777 is not 5648's child."""
    job = _job(tmp_path)
    marker = new_marker(5648, 7777)
    _write_marker(Path(job["data"]), marker)
    fake_health.value = {"ok": True, "version": VERSION, "instance": TOKEN}

    ok, reason = updater.probe_health(job, 5648, VERSION)
    assert not ok
    assert reason == "readiness marker belongs to another process"


def test_normal_launch_marker_passes_new_probe(tmp_path, fake_health):
    """Desktop/VBS launches keep pid == agent_pid == os.getpid()."""
    job = _job(tmp_path)
    marker = new_marker(200, 200)
    marker["pid_chain"] = [200]
    _write_marker(Path(job["data"]), marker)
    fake_health.value = {"ok": True, "version": VERSION, "instance": TOKEN}

    ok, reason = updater.probe_health(job, 200, VERSION)
    assert ok, reason


def test_marker_without_pid_evidence_is_rejected(tmp_path, fake_health):
    job = _job(tmp_path)
    marker = new_marker(5648, 10612)
    marker["pid"] = None
    _write_marker(Path(job["data"]), marker)
    fake_health.value = {"ok": True, "version": VERSION, "instance": TOKEN}

    ok, reason = updater.probe_health(job, 5648, VERSION)
    assert not ok
    assert reason == "readiness marker has invalid pid evidence"


def test_tunnel_session_with_bad_chain_is_rejected(tmp_path, monkeypatch):
    job = _job(tmp_path)
    data = Path(job["data"])
    remote = data / "remote-channel"
    remote.mkdir(parents=True)
    (remote / "token").write_text("t" * 32, encoding="utf-8")
    (remote / "relay-port").write_text("23001", encoding="ascii")
    (remote / "session.json").write_text(
        json.dumps({"token": "t" * 32, "port": 19000, "pid": 5648,
                    "agent_pid": 10612, "pid_chain": [9999, 10612]}),
        encoding="utf-8")
    logs = data / "logs"
    logs.mkdir()
    (logs / "remote-channel.log").write_text("SSH launched\n", encoding="utf-8")

    from winport import remote_channel
    monkeypatch.setattr(updater, "_remote_enabled", lambda job: True)
    monkeypatch.setattr(remote_channel, "relay_provisioning_status",
                        lambda layout: {"ok": True})
    ok, reason = updater._tunnel_snapshot_ok(job, 5648)
    assert not ok
    assert reason == "remote control service pid evidence is invalid"
