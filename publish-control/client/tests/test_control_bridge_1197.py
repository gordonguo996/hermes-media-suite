"""1.2.197: historical tenant detection, runtime status, in-place panel refresh."""

from __future__ import annotations

import json
import logging
import re
import shutil
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from winport import agentpoll, control_status, main, panel  # noqa: E402
from winport.paths import Layout  # noqa: E402


def _layout(tmp_path: Path) -> Layout:
    return Layout(program=tmp_path / "program", data=tmp_path / "data",
                  system=tmp_path / "system").ensure()


def _control(customer_id="c_123") -> dict:
    return {
        "control_url": "https://control.example.test",
        "customer_id": customer_id,
        "customer_token": "customer-secret",
        "device_id": "d_456",
        "device_token": "device-secret",
        "binding_epoch": "0",
    }


def _clear_env(monkeypatch):
    for name in (
            "CONTROL_URL", "CUSTOMER_ID", "CUSTOMER_TOKEN", "DEVICE_ID",
            "DEVICE_TOKEN", "DEVICE_BINDING_EPOCH", "BINDING_EPOCH",
            "WINPORT_CUSTOMER_ID", "WINPORT_CUSTOMER_TOKEN",
            "WINPORT_DEVICE_ID", "AGENT_CUSTOMER_ID", "AGENT_TOKEN",
            "AGENT_DEVICE_ID", "WINPORT_CUSTOMER_ROOT", "AGENT_SINGLE_ROOT",
            "WINPORT_DATA"):
        monkeypatch.delenv(name, raising=False)


def _argv_data(config):
    argv = agentpoll.build_argv(config, "status", {})
    return Path(argv[argv.index("--data") + 1])


class _FakePoller:
    instances = []

    def __init__(self, config, logger=None, **_kwargs):
        self.config = config
        self.logger = logger
        self.starts = 0
        self.__class__.instances.append(self)

    def start(self):
        self.starts += 1
        return self

    def stop(self):
        return None


def _real_startup(tmp_path, monkeypatch):
    """Run main.run(), including Layout(...).ensure(), then start agentpoll."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("WINPORT_SYSTEM", str(tmp_path / "system"))
    _FakePoller.instances = []
    monkeypatch.setattr(agentpoll, "AgentPoller", _FakePoller)
    monkeypatch.setattr("winport.remote_agent.start_remote_agent",
                        lambda *_args, **_kwargs: None)
    seen = {}

    def _fake_run_agent(layout, logger, _args):
        seen["layout"] = layout
        seen["poller"] = main._start_agentpoll(layout, logger)
        return 0

    monkeypatch.setattr(main, "_run_agent", _fake_run_agent)
    data = tmp_path / "data"
    assert main.run(["--data", str(data)]) == 0
    return data, seen


def test_real_startup_standard_layout_uses_worker_root(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "control.json").write_text(json.dumps(_control()), encoding="utf-8")

    data, seen = _real_startup(tmp_path, monkeypatch)

    layout = seen["layout"]
    poller = seen["poller"]
    assert layout.data == data
    assert (data / "inbox").is_dir()
    assert poller is not None
    assert poller.config.single_root is True
    assert _argv_data(poller.config) == data


def test_real_startup_historical_layout_survives_ensure(tmp_path, monkeypatch):
    data = tmp_path / "data"
    tenant = data / "customers" / "c_123"
    (tenant / "run").mkdir(parents=True)
    (data / "control.json").write_text(json.dumps(_control()), encoding="utf-8")

    data, seen = _real_startup(tmp_path, monkeypatch)

    layout = seen["layout"]
    poller = seen["poller"]
    assert (data / "inbox").is_dir()          # ensure() really ran first
    assert layout.data == data
    assert poller is not None
    assert poller.config.single_root is False
    assert _argv_data(poller.config) == tenant


def test_start_agentpoll_does_not_claim_connected_before_first_poll(
        tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    (Path(layout.data) / "control.json").write_text(
        json.dumps(_control()), encoding="utf-8")
    _clear_env(monkeypatch)
    _FakePoller.instances = []
    monkeypatch.setattr(agentpoll, "AgentPoller", _FakePoller)
    monkeypatch.setattr("winport.remote_agent.start_remote_agent",
                        lambda *_args, **_kwargs: None)

    poller = main._start_agentpoll(layout, logging.getLogger("test-1197-start"))

    assert poller is not None
    assert control_status.read_status(layout) is None


@pytest.mark.parametrize("state,level,reason", [
    ("error", "bad", "接入凭据失效"),
    ("disabled", "warn", "接入服务未启动"),
])
def test_newer_runtime_failure_overrides_valid_control(
        tmp_path, state, level, reason):
    layout = _layout(tmp_path)
    target = Path(layout.data) / "control.json"
    target.write_text(json.dumps(_control()), encoding="utf-8")
    control_status.write_status(
        layout, state, reason, updated=target.stat().st_mtime + 1)

    banner = panel.control_banner(layout)

    assert banner["state"] == state
    assert banner["level"] == level
    assert reason in banner["text"]
    assert banner["state"] != "connected"


def test_older_status_keeps_new_control_pending_not_green(tmp_path):
    layout = _layout(tmp_path)
    target = Path(layout.data) / "control.json"
    target.write_text(json.dumps(_control()), encoding="utf-8")
    control_status.write_status(
        layout, "error", "old failure", updated=target.stat().st_mtime - 5)

    banner = panel.control_banner(layout)

    assert banner["state"] == "pending"
    assert banner["level"] == "warn"
    assert banner["state"] != "connected"


def _config(layout):
    return agentpoll.AgentConfig(
        customer_id="c_123",
        token="customer-secret",
        control_url="https://control.example.test",
        data_root=str(layout.data),
        single_root=True,
        device_id="d_456",
        binding_epoch=0,
    )


def _poller(layout):
    return agentpoll.AgentPoller(_config(layout),
                                 logger=logging.getLogger("test-1197-poller"))


@pytest.mark.parametrize("error,reason", [
    (agentpoll.AuthError("HTTP 401"), "接入凭据失效"),
    (agentpoll.AuthError("HTTP 403"), "接入凭据失效"),
    (agentpoll.ControlPlaneError("connection timed out"), "控制面连不上"),
])
def test_runtime_poll_failure_is_visible_over_valid_control(
        tmp_path, monkeypatch, error, reason):
    layout = _layout(tmp_path)
    (Path(layout.data) / "control.json").write_text(
        json.dumps(_control()), encoding="utf-8")
    poller = _poller(layout)

    def _fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(agentpoll, "poll_once", _fail)
    # 1.2.200: one transient failure is only a log entry; the second
    # consecutive failure is what becomes customer-visible.
    for _ in range(2):
        with pytest.raises(type(error)):
            poller.run_once()

    status = control_status.read_status(layout)
    assert status["state"] == "error"
    assert status["reason"] == reason
    banner = panel.control_banner(layout)
    assert banner["state"] == "error"
    assert banner["state"] != "connected"
    assert reason in banner["text"]
    text = (Path(layout.data) / "control-status.json").read_text(encoding="utf-8")
    assert "customer-secret" not in text
    assert "device-secret" not in text
    assert os.name == "nt" or stat.S_IMODE(
        (Path(layout.data) / "control-status.json").stat().st_mode) == 0o600


def test_first_successful_poll_writes_connected(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    (Path(layout.data) / "control.json").write_text(
        json.dumps(_control()), encoding="utf-8")
    control_status.write_status(layout, "missing", "尚未连接")
    poller = _poller(layout)
    monkeypatch.setattr(agentpoll, "poll_once", lambda *_args, **_kwargs: None)

    assert poller.run_once() is None

    status = control_status.read_status(layout)
    assert status["state"] == "connected"
    assert panel.control_banner(layout)["state"] == "connected"


def test_control_status_write_failure_does_not_stop_polling(
        tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    (Path(layout.data) / "control.json").write_text(
        json.dumps(_control()), encoding="utf-8")
    poller = _poller(layout)
    monkeypatch.setattr(agentpoll, "poll_once", lambda *_args, **_kwargs: None)

    def _fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(control_status, "write_status", _fail_write)

    assert poller.run_once() is None
    assert poller.status()["last_poll_at"] is not None


_NODE_HARNESS = r'''
const vm = require('vm');
const fs = require('fs');
const code = fs.readFileSync(0, 'utf8');

function ClassList(initial) {
  this.values = new Set(initial || []);
}
ClassList.prototype.add = function(...names) {
  for (const name of names) this.values.add(name);
};
ClassList.prototype.remove = function(...names) {
  for (const name of names) this.values.delete(name);
};
ClassList.prototype.contains = function(name) {
  return this.values.has(name);
};
ClassList.prototype.toString = function() {
  return Array.from(this.values).join(' ');
};

function TextNode(text) {
  this.nodeType = 3;
  this.textContent = String(text);
}
function El(id) {
  this.nodeType = 1;
  this.id = id || '';
  this.childNodes = [];
  this.classList = new ClassList();
  this.value = '';
  this.disabled = false;
}
Object.defineProperty(El.prototype, 'className', {
  get() { return this.classList.toString(); },
  set(value) {
    this.classList = new ClassList(String(value || '').split(/\s+/).filter(Boolean));
  }
});
El.prototype.appendChild = function(node) {
  this.childNodes.push(node);
  return node;
};
El.prototype.insertBefore = function(node, ref) {
  const index = this.childNodes.indexOf(ref);
  if (index < 0) this.childNodes.push(node);
  else this.childNodes.splice(index, 0, node);
  return node;
};
El.prototype.removeChild = function(node) {
  const index = this.childNodes.indexOf(node);
  if (index >= 0) this.childNodes.splice(index, 1);
  return node;
};
El.prototype.querySelector = function(selector) {
  if (selector === '.dot') {
    return this.childNodes.find(
      node => node.nodeType === 1 && node.classList.contains('dot')) || null;
  }
  return null;
};
Object.defineProperty(El.prototype, 'textContent', {
  get() {
    return this.childNodes
      .filter(node => node.nodeType === 3)
      .map(node => node.textContent)
      .join('');
  },
  set(value) {
    this.childNodes = this.childNodes.filter(node => node.nodeType !== 3);
    this.appendChild(new TextNode(value));
  }
});

const line = new El('control-status-line');
line.className = 'item';
const dot = new El('dot');
dot.className = 'dot st-warn';
line.appendChild(dot);
line.appendChild(new TextNode('飞书接入：未连接（旧状态）'));
const result = new El('license-result');
result.textContent = '原始结果';
const input = new El('act-code');
input.value = 'ABCD1234';
const btn = new El('act-code-btn');
const elements = {
  'control-status-line': line,
  'license-result': result,
  'act-code': input,
  'act-code-btn': btn,
  'control-refresh-btn': btn,
};
const urls = [];
function answer(data) {
  return {ok: true, json: () => Promise.resolve(data)};
}
const sandbox = {
  console,
  Array,
  Object,
  String,
  Promise,
  JSON,
  Error,
  urls,
  __dot: dot,
  setTimeout: (fn) => { fn(); return 0; },
  clearTimeout: () => {},
  document: {
    getElementById: (id) => elements[id] || null,
    createElement: (tag) => new El(tag),
    createTextNode: (text) => new TextNode(text),
    querySelector: () => null,
  },
  location: {
    reload: () => { throw new Error('location.reload called'); },
  },
  fetch: (url) => {
    urls.push(String(url));
    if (String(url).includes('/api/activate-code')) {
      return answer({ok: true, reason: '授权已生效，接入已补齐'});
    }
    if (String(url).includes('/api/control-refresh')) {
      return answer({ok: true, reason: '已重新获取接入配置'});
    }
    if (String(url).includes('/api/control-status')) {
      return answer({level: 'ok', text: '飞书接入：已连接'});
    }
    return answer({ok: true});
  },
};
vm.createContext(sandbox);
vm.runInContext(code, sandbox, {timeout: 5000});
Promise.resolve(sandbox.__testResult).then((value) => {
  process.stdout.write(JSON.stringify(value));
}).catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
'''


def _run_panel_js(page, expression):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    assert scripts
    code = scripts[-1]
    code += ("\nvoid (globalThis.__testResult = (async () => { await "
             + expression + ";\nreturn {line: document.getElementById("
             "'control-status-line').textContent, lineClass: document.getElementById("
             "'control-status-line').className, dotClass: globalThis.__dot.className, "
             "result: document.getElementById('license-result').textContent, "
             "urls: urls}; })());")
    completed = subprocess.run(
        ["node", "-e", _NODE_HARNESS], input=code, text=True, encoding="utf-8",
        capture_output=True, timeout=60)  # 冷启动 node 在 CI 上偶尔超过 20s
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _rendered_page(tmp_path):
    layout = _layout(tmp_path)
    state = panel.build_state(layout, [], tcp=lambda _host, _port: False)
    return panel.render_html(state)


def test_activation_updates_status_line_without_clobbering_result(
        tmp_path):
    page = _rendered_page(tmp_path)

    result = _run_panel_js(
        page, "activateWithCode(document.getElementById('act-code-btn'))")

    assert result["result"] == "授权已生效，接入已补齐"
    assert result["line"] == "飞书接入：已连接"
    assert "st-ok" in result["lineClass"].split()
    assert "st-ok" in result["dotClass"].split()
    assert any(url.endswith("/api/control-status") for url in result["urls"])


def test_control_refresh_updates_status_line_without_reload(tmp_path):
    page = _rendered_page(tmp_path)

    result = _run_panel_js(
        page, "refreshControl(document.getElementById('control-refresh-btn'))")

    assert result["result"] == "已重新获取接入配置"
    assert result["line"] == "飞书接入：已连接"
    assert "st-ok" in result["lineClass"].split()
    assert "st-ok" in result["dotClass"].split()
    assert any(url.endswith("/api/control-status") for url in result["urls"])
