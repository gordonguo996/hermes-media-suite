"""The operator channel must come up with the poller, exactly once."""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from winport import main as winport_main
from winport import remote_agent


class _FakeThread:
    def __init__(self, *args, **kwargs):
        self.started = False

    def start(self):
        self.started = True

    def is_alive(self):
        return True


class _Cfg:
    control_url = "http://127.0.0.1:9"
    customer_id = "c1"
    device_id = "d1"
    token = "t1"


def test_start_remote_agent_is_idempotent(monkeypatch):
    made = []

    def fake_thread(*args, **kwargs):
        thread = _FakeThread()
        made.append(thread)
        return thread

    monkeypatch.setattr(remote_agent.threading, "Thread", fake_thread)
    monkeypatch.setattr(remote_agent, "_THREAD", None)
    monkeypatch.setattr(remote_agent, "_THREAD_KEY", None)

    first = remote_agent.start_remote_agent(_Cfg())
    second = remote_agent.start_remote_agent(_Cfg())

    assert first is second
    assert len(made) == 1
    assert made[0].started


def test_restart_keeps_the_operator_channel(monkeypatch):
    seen = {}

    def fake_start(layout, logger, start_remote=True):
        seen["start_remote"] = start_remote
        return object()

    monkeypatch.setattr(winport_main, "_start_agentpoll", fake_start)
    layout = types.SimpleNamespace()
    logger = types.SimpleNamespace(info=lambda *a, **k: None,
                                   warning=lambda *a, **k: None,
                                   error=lambda *a, **k: None)
    slot = {}
    winport_main._restart_agentpoll(layout, logger, slot)
    assert seen.get("start_remote") is True
    assert slot.get("poller") is not None
