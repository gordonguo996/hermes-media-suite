"""1.2.199/1.2.200: panel polling, control errors and monotonic writes."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from winport import activation, control_status, panel  # noqa: E402
from winport.paths import Layout  # noqa: E402


def _layout(tmp_path: Path) -> Layout:
    return Layout(program=tmp_path / "program", data=tmp_path / "data",
                  system=tmp_path / "system").ensure()


def _control(**overrides) -> dict:
    control = {
        "control_url": "https://control.example.test",
        "customer_id": "c_123",
        "customer_token": "stale-secret",
        "device_id": "d_456",
        "device_token": "device-secret",
        "binding_epoch": "0",
    }
    control.update(overrides)
    return control


def _formal_state():
    return {"publish_allowed": True, "status": "OK", "source": "licence",
            "days_left": 300, "text": "授权有效", "tombstone_error": None}


def _response(control, *, rotated=None, control_error=None):
    body = {"license": {"machine_id": "MACHINE-1",
                        "expires_at": "2099-01-01"},
            "control": control}
    if rotated is not None:
        body["rotated"] = rotated
    if control_error is not None:
        body["control_error"] = control_error
    return body


def _write_control(layout, control):
    target = Path(layout.data) / "control.json"
    target.write_text(json.dumps(control), encoding="utf-8")
    target.chmod(0o600)
    return target


def _patch_refresh(tmp_path, monkeypatch, old_control, new_control,
                   *, rotated=None, control_error=None):
    layout = _layout(tmp_path)
    target = _write_control(layout, old_control)
    control_status.write_status(layout, "error", "接入凭据失效")
    monkeypatch.setattr(activation, "fingerprint",
                        lambda: {"machine_id": "MACHINE-1"})
    monkeypatch.setattr(activation, "unified_license_state",
                        lambda *_args, **_kwargs: dict(_formal_state()))
    monkeypatch.setattr(activation, "exchange",
                        lambda *_args, **_kwargs: _response(
                            dict(new_control), rotated=rotated,
                            control_error=control_error))
    restarts = []
    handlers = panel.PanelHandlers(
        layout, [],
        restart_poller=lambda: restarts.append("restart")
        or {"ok": True, "reason": "接入服务已重启"})
    return layout, target, handlers, restarts


def test_same_code_same_credentials_is_not_reported_as_repaired(
        tmp_path, monkeypatch):
    old_control = _control()
    layout, target, handlers, restarts = _patch_refresh(
        tmp_path, monkeypatch, old_control, _control(), rotated=False)
    before_bytes = target.read_bytes()
    before_mtime = target.stat().st_mtime_ns

    result = handlers.control_refresh("ABCD1234")

    assert result["ok"] is False
    assert result["reason"] == activation.UNCHANGED_CONTROL_REASON
    assert result["control_state"] == "error"
    assert result["control_ok"] is False
    assert result["control_written"] is False
    assert "poller_restarted" not in result
    assert restarts == []
    assert target.read_bytes() == before_bytes
    assert target.stat().st_mtime_ns == before_mtime

    status = control_status.read_status(layout)
    assert status["state"] == "error"
    assert status["reason"] == activation.UNCHANGED_CONTROL_REASON
    status_text = (Path(layout.data) / "control-status.json").read_text(
        encoding="utf-8")
    assert "stale-secret" not in status_text
    assert "device-secret" not in status_text

    banner = panel.control_banner(layout)
    assert banner["state"] == "error"
    assert banner["level"] == "bad"
    assert activation.UNCHANGED_CONTROL_REASON in banner["text"]


@pytest.mark.parametrize("field, value", [
    ("customer_token", "fresh-secret"),
    ("device_token", "fresh-device-secret"),
    ("binding_epoch", "1"),
])
def test_any_changed_credential_field_is_really_written(
        tmp_path, monkeypatch, field, value):
    old_control = _control()
    new_control = _control(**{field: value})
    layout, target, handlers, restarts = _patch_refresh(
        tmp_path, monkeypatch, old_control, new_control)

    result = handlers.control_refresh("ABCD1234")

    assert result["ok"] is True
    assert result["control_ok"] is True
    assert result["control_written"] is True
    assert result["control_state"] == "pending"
    assert restarts == ["restart"]
    assert json.loads(target.read_text(encoding="utf-8"))[field] == value
    assert control_status.read_status(layout)["state"] == "pending"


@pytest.mark.parametrize("error,expected", [
    ("control_unreachable", activation.CONTROL_UNREACHABLE_REASON),
    ("control_admin_rejected", activation.CONTROL_ADMIN_REJECTED_REASON),
])
def test_control_error_wins_over_unchanged_credentials(
        tmp_path, monkeypatch, error, expected):
    old_control = _control()
    layout, target, handlers, restarts = _patch_refresh(
        tmp_path, monkeypatch, old_control, _control(), control_error=error)
    before = target.read_bytes()

    result = handlers.control_refresh("ABCD1234")

    # The licence is already valid; the control-plane failure is surfaced in the
    # reason/status without revoking authorisation.
    assert result["ok"] is True
    assert "授权已生效，但接入未完成：" in result["reason"]
    assert expected in result["reason"]
    assert result["control_written"] is False
    assert restarts == []
    assert target.read_bytes() == before
    status = control_status.read_status(layout)
    assert status["state"] == "error"
    assert status["reason"] == expected
    assert error not in status["reason"]


def test_missing_rotated_marker_is_unknown_not_new_code(
        tmp_path, monkeypatch):
    old_control = _control()
    layout, target, handlers, restarts = _patch_refresh(
        tmp_path, monkeypatch, old_control, _control())
    before = target.read_bytes()

    result = handlers.control_refresh("ABCD1234")

    assert result["ok"] is False
    assert result["reason"] == activation.UNCONFIRMED_CONTROL_REASON
    assert result["reason"] != activation.UNCHANGED_CONTROL_REASON
    assert "新的 8 位激活码" not in result["reason"]
    assert restarts == []
    assert target.read_bytes() == before


def test_older_binding_epoch_is_discarded(tmp_path, monkeypatch):
    old_control = _control(binding_epoch="2")
    new_control = _control(customer_token="fresh-secret", binding_epoch="1")
    layout, target, handlers, restarts = _patch_refresh(
        tmp_path, monkeypatch, old_control, new_control)
    before = target.read_bytes()

    result = handlers.control_refresh("ABCD1234")

    assert result["ok"] is True
    assert result["reason"] == activation.STALE_CONTROL_REASON
    assert result["control_written"] is False
    assert restarts == []
    assert target.read_bytes() == before


def test_same_code_exchange_and_write_are_serialized(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    target = _write_control(layout, _control(binding_epoch="0"))
    monkeypatch.setattr(activation, "fingerprint",
                        lambda: {"machine_id": "MACHINE-1"})
    monkeypatch.setattr(activation, "unified_license_state",
                        lambda *_args, **_kwargs: dict(_formal_state()))
    started = threading.Event()
    state_lock = threading.Lock()
    calls = []
    active = 0
    max_active = 0

    def _exchange(*_args, **_kwargs):
        nonlocal active, max_active
        with state_lock:
            calls.append(1)
            index = len(calls)
            active += 1
            max_active = max(max_active, active)
        started.set()
        if index == 1:
            time.sleep(0.05)
            response = _response(_control(binding_epoch="2"))
        else:
            time.sleep(0.02)
            response = _response(_control(binding_epoch="1"))
        with state_lock:
            active -= 1
        return response

    monkeypatch.setattr(activation, "exchange", _exchange)
    results = []

    def _worker():
        results.append(activation.activate_with_code(
            layout, "ABCD1234", force=True))

    first = threading.Thread(target=_worker)
    second = threading.Thread(target=_worker)
    first.start()
    assert started.wait(2)
    second.start()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert max_active == 1
    assert json.loads(target.read_text(encoding="utf-8"))["binding_epoch"] == "2"
    assert any(activation.STALE_CONTROL_REASON in str(result.get("reason"))
               for result in results)


_NODE_HARNESS = r'''
const vm = require('vm');
const fs = require('fs');
const code = fs.readFileSync(0, 'utf8');
const statusResponses = __STATUS_RESPONSES__;
const activateResponse = __ACTIVATE_RESPONSE__;

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
line.appendChild(new TextNode('飞书接入：连接中…'));
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
let statusIndex = 0;
const timers = [];
const delays = [];
let nextTimerId = 1;
let now = 0;
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
  Date: {now: () => now},
  urls,
  __dot: dot,
  __clock: {
    now: () => now,
    advance: (ms) => { now += ms; },
  },
  __timers: timers,
  __delays: delays,
  setTimeout: (fn, ms) => {
    const id = nextTimerId++;
    delays.push(ms || 0);
    timers.push({id, fn, ms: ms || 0});
    return id;
  },
  clearTimeout: (id) => {
    const index = timers.findIndex(timer => timer.id === id);
    if (index >= 0) timers.splice(index, 1);
  },
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
      return answer(activateResponse || {ok: true, reason: '授权已生效，接入已补齐'});
    }
    if (String(url).includes('/api/control-refresh')) {
      return answer({ok: true, reason: '已重新获取接入配置'});
    }
    if (String(url).includes('/api/control-status')) {
      const data = statusResponses[Math.min(statusIndex,
                                            statusResponses.length - 1)];
      statusIndex += 1;
      return answer(data);
    }
    return answer({ok: true});
  },
};
vm.createContext(sandbox);
vm.runInContext(code, sandbox, {timeout: 5000});
Promise.resolve(sandbox.__testResult).then((value) => {
  process.stdout.write(JSON.stringify(value));
  process.exit(0);
}).catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
'''


def _run_polling_js(page, expression, status_responses, activate_response=None):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    assert scripts
    code = scripts[-1]
    code += r'''
void (globalThis.__testResult = (async () => {
  await (__EXPRESSION__);
  let guard = 0;
  while (globalThis.__timers.length > 0 && guard < 200) {
    const timer = globalThis.__timers.shift();
    globalThis.__clock.advance(timer.ms);
    await timer.fn();
    guard += 1;
  }
  return {
    line: document.getElementById('control-status-line').textContent,
    lineClass: document.getElementById('control-status-line').className,
    dotClass: globalThis.__dot.className,
    result: document.getElementById('license-result').textContent,
    urls: urls,
    statusCalls: urls.filter(url => url.includes('/api/control-status')).length,
    remainingTimers: globalThis.__timers.length,
    delays: globalThis.__delays.slice(),
    clock: globalThis.__clock.now(),
  };
})());
'''.replace("__EXPRESSION__", expression)
    harness = _NODE_HARNESS.replace(
        "__STATUS_RESPONSES__",
        json.dumps(status_responses, ensure_ascii=False)).replace(
            "__ACTIVATE_RESPONSE__",
            json.dumps(activate_response, ensure_ascii=False))
    completed = subprocess.run(
        ["node", "-e", harness], input=code, text=True,
        capture_output=True, timeout=60)  # 冷启动 node 在 CI 上偶尔超过 20s
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _rendered_page(tmp_path):
    layout = _layout(tmp_path)
    state = panel.build_state(layout, [], tcp=lambda _host, _port: False)
    return panel.render_html(state)


def _status(state, level, text):
    return {"state": state, "level": level, "text": text}


def test_pending_then_connected_updates_in_place(tmp_path):
    page = _rendered_page(tmp_path)
    responses = [
        _status("pending", "warn", "飞书接入：连接中…"),
        _status("connected", "ok", "飞书接入：已连接"),
    ]

    result = _run_polling_js(
        page, "activateWithCode(document.getElementById('act-code-btn'))",
        responses)

    assert result["line"] == "飞书接入：已连接"
    assert "st-ok" in result["lineClass"].split()
    assert "st-ok" in result["dotClass"].split()
    assert result["result"] == "授权已生效，接入已补齐"
    assert result["statusCalls"] == 2
    assert result["remainingTimers"] == 0
    assert result["delays"] == [2000]


def test_pending_then_error_stops_polling_and_shows_reason(tmp_path):
    page = _rendered_page(tmp_path)
    responses = [
        _status("pending", "warn", "飞书接入：连接中…"),
        _status("error", "bad", "飞书接入：未连接（接入凭据失效）"),
    ]

    result = _run_polling_js(
        page, "refreshControl(document.getElementById('control-refresh-btn'))",
        responses)

    assert result["line"] == "飞书接入：未连接（接入凭据失效）"
    assert "st-bad" in result["lineClass"].split()
    assert "st-bad" in result["dotClass"].split()
    assert result["result"] == "已重新获取接入配置"
    assert result["statusCalls"] == 2
    assert result["remainingTimers"] == 0
    assert result["delays"] == [2000]


def test_pending_timeout_stops_after_sixty_seconds(tmp_path):
    page = _rendered_page(tmp_path)
    responses = [_status("pending", "warn", "飞书接入：连接中…")]

    result = _run_polling_js(
        page, "activateWithCode(document.getElementById('act-code-btn'))",
        responses)

    assert result["line"] == "飞书接入：连接中…（可稍后刷新页面）"
    assert "st-warn" in result["lineClass"].split()
    assert result["result"] == "授权已生效，接入已补齐"
    assert result["statusCalls"] == 31
    assert result["remainingTimers"] == 0
    assert result["delays"] == [2000] * 30
    assert result["clock"] == 60000


def test_activate_failure_refreshes_status_line_once(tmp_path):
    page = _rendered_page(tmp_path)
    failure = "服务端未确认已换发新凭据，请稍后再点「重新获取接入配置」"
    responses = [_status("error", "bad", "飞书接入：未连接（接入凭据失效）")]

    result = _run_polling_js(
        page, "activateWithCode(document.getElementById('act-code-btn'))",
        responses, activate_response={"ok": False, "reason": failure})

    assert result["line"] == "飞书接入：未连接（接入凭据失效）"
    assert "st-bad" in result["lineClass"].split()
    assert result["result"] == failure
    assert result["statusCalls"] == 1
    assert result["remainingTimers"] == 0
    assert result["delays"] == []


def test_timeout_after_poller_stopped_says_restart_panel(tmp_path):
    page = _rendered_page(tmp_path)
    responses = [
        _status("disabled", "warn", "飞书接入：未连接（接入服务已停止）"),
    ]

    result = _run_polling_js(
        page, "activateWithCode(document.getElementById('act-code-btn'))",
        responses)

    assert result["line"] == "飞书接入：接入服务已停止，请重启面板"
    assert "st-warn" in result["lineClass"].split()
    assert result["result"] == "授权已生效，接入已补齐"
    assert result["statusCalls"] == 31
    assert result["remainingTimers"] == 0
    assert result["delays"] == [2000] * 30
    assert result["clock"] == 60000
