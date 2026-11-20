"""1.2.196: close the control bridge (data root, re-provision, visibility)."""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "agent"))

from winport import activation, agentpoll, control_status, main, panel  # noqa: E402
from winport.paths import Layout  # noqa: E402


def _layout(tmp_path: Path) -> Layout:
    return Layout(program=tmp_path / "program", data=tmp_path / "data",
                  system=tmp_path / "system").ensure()


def _control(customer_id="c_123", binding_epoch="0") -> dict:
    return {
        "control_url": "https://control.example.test",
        "customer_id": customer_id,
        "customer_token": "customer-secret",
        "device_id": "d_456",
        "device_token": "device-secret",
        "binding_epoch": binding_epoch,
    }


def _response(control=None, control_error=None):
    body = {"license": {"machine_id": "MACHINE-1", "expires_at": "2099-01-01"}}
    if control is not None:
        body["control"] = control
    if control_error is not None:
        body["control_error"] = control_error
    return body


def _clear_env(monkeypatch):
    for name in (
            "CONTROL_URL", "CUSTOMER_ID", "CUSTOMER_TOKEN", "DEVICE_ID",
            "DEVICE_TOKEN", "DEVICE_BINDING_EPOCH", "BINDING_EPOCH",
            "WINPORT_CUSTOMER_ID", "WINPORT_CUSTOMER_TOKEN",
            "WINPORT_DEVICE_ID", "AGENT_CUSTOMER_ID", "AGENT_TOKEN",
            "AGENT_DEVICE_ID", "WINPORT_CUSTOMER_ROOT", "AGENT_SINGLE_ROOT"):
        monkeypatch.delenv(name, raising=False)


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


def _start(layout, monkeypatch):
    _clear_env(monkeypatch)
    _FakePoller.instances = []
    monkeypatch.setattr(agentpoll, "AgentPoller", _FakePoller)
    monkeypatch.setattr("winport.remote_agent.start_remote_agent",
                        lambda *_args, **_kwargs: None)
    logger = logging.getLogger("test-control-1196")
    poller = main._start_agentpoll(layout, logger)
    assert poller is not None
    return poller


def _argv_data(config):
    argv = agentpoll.build_argv(config, "status", {})
    return Path(argv[argv.index("--data") + 1])


def test_standard_layout_poller_data_is_worker_root(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    (Path(layout.data) / "control.json").write_text(
        json.dumps(_control()), encoding="utf-8")

    poller = _start(layout, monkeypatch)

    assert poller.config.single_root is True
    assert _argv_data(poller.config) == Path(layout.data)
    assert (Path(layout.data) / "inbox").is_dir()


def test_historical_tenant_layout_keeps_scoped_data_root(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    tenant = Path(layout.data) / "customers" / "c_123"
    (tenant / "run").mkdir(parents=True)
    (Path(layout.data) / "control.json").write_text(
        json.dumps(_control()), encoding="utf-8")

    poller = _start(layout, monkeypatch)

    assert (Path(layout.data) / "inbox").is_dir()
    assert poller.config.single_root is False
    assert _argv_data(poller.config) == tenant


def test_invalid_data_control_falls_back_to_program_control(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    (Path(layout.data) / "control.json").write_text("{}", encoding="utf-8")
    program = Path(layout.program)
    program.mkdir(parents=True, exist_ok=True)
    (program / "control.json").write_text(
        json.dumps(_control()), encoding="utf-8")

    poller = _start(layout, monkeypatch)

    assert _argv_data(poller.config) == Path(layout.data)
    assert poller.config.customer_id == "c_123"
    assert poller.config.token == "device-secret"


def test_numeric_binding_epoch_zero_is_not_dropped(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    (Path(layout.data) / "control.json").write_text(
        json.dumps(_control(binding_epoch=0)), encoding="utf-8")

    poller = _start(layout, monkeypatch)

    assert poller.config.binding_epoch == 0


def _formal_state():
    return {"publish_allowed": True, "status": "OK", "source": "licence",
            "days_left": 300, "text": "授权有效", "tombstone_error": None}


def test_existing_formal_licence_reprovisions_missing_control(
        tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    exchanges = []
    installed = []
    monkeypatch.setattr(activation, "fingerprint",
                        lambda: {"machine_id": "MACHINE-1"})
    monkeypatch.setattr(activation, "unified_license_state",
                        lambda *_args, **_kwargs: dict(_formal_state()))
    monkeypatch.setattr(activation, "exchange",
                        lambda *_args, **_kwargs: exchanges.append(1)
                        or _response(_control()))
    monkeypatch.setattr(activation, "install_document",
                        lambda *_args, **_kwargs: installed.append(1)
                        or {"ok": True, "reason": "授权已自动生效"})

    result = activation.activate_with_code(layout, "ABCD1234")

    assert result["ok"] is True
    assert "授权已生效，接入已补齐" in result["reason"]
    assert len(exchanges) == 1
    assert installed == []
    target = Path(layout.data) / "control.json"
    assert json.loads(target.read_text(encoding="utf-8"))["customer_id"] == "c_123"
    status = control_status.read_status(layout)
    assert status["state"] == "pending"
    assert panel.control_banner(layout)["state"] != "connected"
    assert os.name == "nt" or stat.S_IMODE((Path(layout.data) / "control-status.json").stat().st_mode) == 0o600


def test_existing_licence_control_error_keeps_authorisation_and_shows_reason(
        tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    exchanges = []
    installed = []
    monkeypatch.setattr(activation, "fingerprint",
                        lambda: {"machine_id": "MACHINE-1"})
    monkeypatch.setattr(activation, "unified_license_state",
                        lambda *_args, **_kwargs: dict(_formal_state()))
    monkeypatch.setattr(activation, "exchange",
                        lambda *_args, **_kwargs: exchanges.append(1)
                        or _response(control_error="control plane unavailable"))
    monkeypatch.setattr(activation, "install_document",
                        lambda *_args, **_kwargs: installed.append(1)
                        or {"ok": True, "reason": "授权已自动生效"})

    result = activation.activate_with_code(layout, "ABCD1234")

    assert result["ok"] is True
    assert "授权已生效，但接入未完成：control plane unavailable" in result["reason"]
    assert len(exchanges) == 1
    assert installed == []
    assert not (Path(layout.data) / "control.json").exists()
    status = control_status.read_status(layout)
    assert status["state"] == "error"
    assert status["reason"] == "control plane unavailable"
    text = (Path(layout.data) / "control-status.json").read_text(encoding="utf-8")
    assert "customer-secret" not in text
    assert "device-secret" not in text


def test_existing_licence_exchange_timeout_keeps_authorisation(
        tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    monkeypatch.setattr(activation, "fingerprint",
                        lambda: {"machine_id": "MACHINE-1"})
    monkeypatch.setattr(activation, "unified_license_state",
                        lambda *_args, **_kwargs: dict(_formal_state()))

    def _timeout(*_args, **_kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(activation, "exchange", _timeout)

    result = activation.activate_with_code(layout, "ABCD1234")

    assert result["ok"] is True
    assert "授权已生效，但接入未完成" in result["reason"]
    assert "超时" in result["reason"]
    assert control_status.read_status(layout)["state"] == "error"


def test_panel_shows_control_status_and_refresh_button(tmp_path):
    layout = _layout(tmp_path)
    control_status.write_status(layout, "error", "control plane unavailable")
    state = panel.build_state(layout, [], tcp=lambda host, port: False)
    page = panel.render_html(state)

    assert "飞书接入：未连接（control plane unavailable）" in page
    assert "重新获取接入配置" in page

    (Path(layout.data) / "control.json").write_text(
        json.dumps(_control()), encoding="utf-8")
    control_status.write_status(layout, "connected")
    state = panel.build_state(layout, [], tcp=lambda host, port: False)
    page = panel.render_html(state)
    assert "飞书接入：已连接" in page


def test_status_written_in_the_same_clock_tick_still_counts_as_fresh(tmp_path):
    """Windows 上 time.time() 只有 ~15.6ms 精度，文件 mtime 却是真实时间戳。

    先写 control.json、紧接着写"已连接"状态时，状态里的 updated 可能比 mtime 小几毫秒；
    严格比较会把刚写好的状态当成过期，面板卡在"连接中…"。CI 上就是这样偶发红的
    （main push 44748206712）。这里把 updated 压在 mtime 之下但仍在容差内，必须判成已连接。
    """
    layout = _layout(tmp_path)
    control = Path(layout.data) / "control.json"
    control.write_text(json.dumps(_control()), encoding="utf-8")
    mtime = control.stat().st_mtime
    control_status.write_status(layout, "connected", updated=mtime - 0.02)
    state = panel.build_state(layout, [], tcp=lambda host, port: False)
    page = panel.render_html(state)
    assert "飞书接入：已连接" in page

    # 明显过期的状态（早于 control.json 超过容差）仍然不能冒充"已连接"
    control_status.write_status(layout, "connected", updated=mtime - 5.0)
    state = panel.build_state(layout, [], tcp=lambda host, port: False)
    assert "飞书接入：已连接" not in panel.render_html(state)


def test_refresh_without_code_prompts_for_activation_code(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    monkeypatch.setattr(activation, "install_root", lambda: tmp_path / "install")
    monkeypatch.setattr(activation, "unified_license_state",
                        lambda *_args, **_kwargs: {"publish_allowed": False,
                                                   "source": "licstate"})

    result = activation.refresh_control(layout)

    assert result["ok"] is False
    assert "8 位激活码" in result["reason"]


def test_auth_failure_classification():
    assert main._is_auth_failure("poll_error", "HTTP 401") is True
    assert main._is_auth_failure("auth_error", "token rejected") is True
    assert main._is_auth_failure("poll_error", "connection timed out") is False


def test_recheck_loop_stops_on_explicit_auth_failure(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_control_recheck_once",
                        lambda *_args, **_kwargs: calls.append(1) or "auth_failed")
    stop_event = threading.Event()
    worker = threading.Thread(
        target=main._control_recheck_loop,
        args=(_layout(tmp_path), logging.getLogger("t-auth"), stop_event, {}, 0.01),
        daemon=True)
    worker.start()
    worker.join(0.5)

    assert not worker.is_alive()
    assert len(calls) == 1


def test_recheck_loop_keeps_retrying_transient_failures(tmp_path, monkeypatch):
    outcomes = ["retry", "retry", "started"]

    def _once(*_args, **_kwargs):
        return outcomes.pop(0) if outcomes else "retry"

    monkeypatch.setattr(main, "_control_recheck_once", _once)
    stop_event = threading.Event()
    worker = threading.Thread(
        target=main._control_recheck_loop,
        args=(_layout(tmp_path), logging.getLogger("t-retry"), stop_event, {}, 0.01),
        daemon=True)
    worker.start()
    worker.join(0.5)

    assert not worker.is_alive()
    assert outcomes == []


def test_recheck_loop_gives_up_after_24_hour_budget(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_control_recheck_once",
                        lambda *_args, **_kwargs: calls.append(1) or "retry")
    stop_event = threading.Event()
    worker = threading.Thread(
        target=main._control_recheck_loop,
        args=(_layout(tmp_path), logging.getLogger("t-budget"), stop_event, {},
              0.01, 0.0),
        daemon=True)
    worker.start()
    worker.join(0.5)

    assert not worker.is_alive()
    assert len(calls) >= 1
