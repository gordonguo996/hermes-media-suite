"""1.2.195: activation response -> control.json bridge and late recheck."""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from winport import activation, main  # noqa: E402
from winport import agentpoll  # noqa: E402
from winport.paths import Layout  # noqa: E402


def _layout(tmp_path: Path) -> Layout:
    return Layout(program=tmp_path / "program", data=tmp_path / "data",
                  system=tmp_path / "system").ensure()


def _control() -> dict:
    return {
        "control_url": "https://control.example.test",
        "customer_id": "c_123",
        "customer_token": "customer-secret",
        "device_id": "d_456",
        "device_token": "device-secret",
        "binding_epoch": "0",
    }


def _response(control=None):
    body = {"license": {"machine_id": "MACHINE-1", "expires_at": "2099-01-01"}}
    if control is not None:
        body["control"] = control
    return body


def _patch_activation(monkeypatch, response):
    installed = []
    monkeypatch.setattr(activation, "fingerprint",
                        lambda: {"machine_id": "MACHINE-1"})
    monkeypatch.setattr(
        activation, "unified_license_state",
        lambda *_args, **_kwargs: {
            "publish_allowed": False,
            "status": "NOT_ACTIVATED",
            "source": "licstate",
            "text": "尚未导入正式授权",
        })
    monkeypatch.setattr(activation, "exchange",
                        lambda *_args, **_kwargs: response)

    def fake_install(layout, document, **_kwargs):
        installed.append(document)
        return {"ok": True, "reason": "授权已自动生效"}

    monkeypatch.setattr(activation, "install_document", fake_install)
    return installed


def test_activate_with_code_writes_valid_control_atomically(
        tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    control = _control()
    response = _response(control)
    installed = _patch_activation(monkeypatch, response)

    result = activation.activate_with_code(layout, "ABCD1234")

    assert result["ok"] is True
    assert "接入已补齐" in result["reason"]
    assert installed == [response["license"]]
    target = Path(layout.data) / "control.json"
    assert json.loads(target.read_text(encoding="utf-8")) == control
    assert os.name == "nt" or stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(Path(layout.data).glob(".control-*.json"))


def test_activate_online_response_writes_control(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    install_dir = tmp_path / "install"
    (install_dir / "config").mkdir(parents=True)
    (install_dir / "config" / "activation.json").write_text(
        json.dumps({"enrollment_token": "ABCD1234"}), encoding="utf-8")
    control = _control()
    installed = _patch_activation(monkeypatch, _response(control))
    monkeypatch.setattr(activation, "install_root", lambda: install_dir)

    result = activation.activate(layout)

    assert result["ok"] is True
    assert "接入已补齐" in result["reason"]
    assert len(installed) == 1
    target = Path(layout.data) / "control.json"
    assert json.loads(target.read_text(encoding="utf-8")) == control
    assert os.name == "nt" or stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize("control", [
    None,
    {key: value for key, value in _control().items()
     if key != "binding_epoch"},
    {**_control(), "control_url": "http://control.example.test"},
    {**_control(), "customer_token": ""},
    {**_control(), "device_token": ""},
])
def test_missing_or_invalid_control_does_not_write_and_still_authorizes(
        tmp_path, monkeypatch, control):
    layout = _layout(tmp_path)
    _patch_activation(monkeypatch, _response(control))

    result = activation.activate_with_code(layout, "ABCD1234")

    assert result["ok"] is True
    assert "接入已补齐" not in result["reason"]
    assert not (Path(layout.data) / "control.json").exists()


def test_exchange_returns_license_and_control(monkeypatch):
    control = _control()
    body = json.dumps({"license": {"machine_id": "MACHINE-1"},
                       "control": control}).encode("utf-8")

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return body

    class FakeOpener:
        def open(self, _request, timeout=None):
            return FakeResponse()

    monkeypatch.setattr(activation, "build_opener",
                        lambda _handler: FakeOpener())

    response = activation.exchange("ABCD1234", "MACHINE-1")

    assert response["license"] == {"machine_id": "MACHINE-1"}
    assert response["control"] == control


class _FakePoller:
    instances = []

    def __init__(self, config, logger=None, **_kwargs):
        self.config = config
        self.logger = logger
        self.starts = 0
        self.stops = 0
        self.__class__.instances.append(self)

    def start(self):
        self.starts += 1
        return self

    def stop(self):
        self.stops += 1


def _clear_agentpoll_env(monkeypatch):
    for name in (
            "CONTROL_URL", "CUSTOMER_ID", "CUSTOMER_TOKEN", "DEVICE_ID",
            "DEVICE_TOKEN", "DEVICE_BINDING_EPOCH", "BINDING_EPOCH",
            "WINPORT_CUSTOMER_ID", "WINPORT_CUSTOMER_TOKEN",
            "WINPORT_DEVICE_ID", "AGENT_CUSTOMER_ID", "AGENT_TOKEN",
            "AGENT_DEVICE_ID"):
        monkeypatch.delenv(name, raising=False)


def test_main_recheck_starts_poller_once_when_control_appears(
        tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    _clear_agentpoll_env(monkeypatch)
    _FakePoller.instances = []
    monkeypatch.setattr(agentpoll, "AgentPoller", _FakePoller)
    monkeypatch.setattr("winport.remote_agent.start_remote_agent",
                        lambda *_args, **_kwargs: None)
    logger = logging.getLogger("test-control-recheck")

    assert main._start_agentpoll(layout, logger) is None
    assert main._agentpoll_marker_state(layout) == "disabled"

    target = Path(layout.data) / "control.json"
    target.write_text(json.dumps(_control()), encoding="utf-8")
    stop_event = threading.Event()
    poller_slot = {"poller": None}

    outcome = main._control_recheck_once(layout, logger, poller_slot,
                                         stop_event=stop_event)

    assert outcome == "started"
    assert len(_FakePoller.instances) == 1
    assert _FakePoller.instances[0].starts == 1
    assert poller_slot["poller"] is _FakePoller.instances[0]


def test_main_recheck_loop_stops_after_the_first_start(
        tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    _clear_agentpoll_env(monkeypatch)
    _FakePoller.instances = []
    monkeypatch.setattr(agentpoll, "AgentPoller", _FakePoller)
    monkeypatch.setattr("winport.remote_agent.start_remote_agent",
                        lambda *_args, **_kwargs: None)
    logger = logging.getLogger("test-control-recheck-loop")

    assert main._start_agentpoll(layout, logger) is None
    stop_event = threading.Event()
    poller_slot = {"poller": None}
    worker = threading.Thread(
        target=main._control_recheck_loop,
        args=(layout, logger, stop_event, poller_slot, 0.01),
        daemon=True)
    worker.start()
    time.sleep(0.03)
    assert _FakePoller.instances == []

    (Path(layout.data) / "control.json").write_text(
        json.dumps(_control()), encoding="utf-8")
    worker.join(1.0)

    assert not worker.is_alive()
    assert len(_FakePoller.instances) == 1
    assert _FakePoller.instances[0].starts == 1
    assert poller_slot["poller"] is _FakePoller.instances[0]
