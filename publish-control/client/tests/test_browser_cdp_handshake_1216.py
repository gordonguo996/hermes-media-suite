"""1.2.216: a listening debug port must be proven by a real CDP handshake.

The failure this guards: Chrome froze with port 9224 still listening and
/json/version still answering.  Every reuse check passed, so the publish worker
handed the frozen browser to connect_over_cdp and died 30s later with
"发布浏览器没有启动成功".  The fix probes the browser-level WebSocket and
replaces a frozen instance instead of trusting the port.
"""
import sys
import types
from pathlib import Path

from agent.winport import chrome, daemon as daemon_mod, platforms


class _Env:
    def __init__(self, env_id, port, enabled=True, label=None, profile=None):
        self.env_id = env_id
        self.debug_port = port
        self.enabled = enabled
        self.label = label or env_id
        self._profile = profile

    def profile_dir(self, layout):
        return Path(self._profile or (Path(layout.root) / "profiles" / self.env_id))


class _Layout:
    def __init__(self, root):
        self.root = Path(root)
        self.logs = self.root / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.run = self.root / "run"
        self.run.mkdir(parents=True, exist_ok=True)
        self.profiles = self.root / "profiles"
        self.program = self.root
        self.env_lease_dir = self.run
        self.settings_file = self.run / "settings.json"


# ---------------------------------------------------------------- alive probe

def _fake_playwright(monkeypatch, *, hang=False):
    calls = {}

    class _Browser:
        version = "Chrome/156.0.8078.80"

        def close(self):
            calls["closed"] = True

    class _Chromium:
        def connect_over_cdp(self, url, timeout=None):
            calls["url"] = url
            calls["timeout"] = timeout
            if hang:
                raise TimeoutError("browser handshake never answered")
            return _Browser()

    class _PW:
        chromium = _Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: _PW()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return calls


def test_alive_probe_opens_a_real_handshake_and_disconnects(monkeypatch):
    calls = _fake_playwright(monkeypatch)
    assert chrome.cdp_browser_alive(9224)["ok"] is True
    assert calls["url"] == "http://127.0.0.1:9224"
    assert calls["closed"] is True


def test_alive_probe_stays_neutral_without_playwright(monkeypatch):
    """A missing playwright must never be read as "browser hung"."""
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    alive = chrome.cdp_browser_alive(9224)
    assert alive["ok"] is True
    assert alive["skipped"] is True


def test_alive_probe_reports_a_wedged_browser(monkeypatch):
    _fake_playwright(monkeypatch, hang=True)
    alive = chrome.cdp_browser_alive(9224, timeout=1)
    assert alive["ok"] is False
    assert "TimeoutError" in alive["error"]


# ------------------------------------------------------------------- daemon

def _stub_daemon(monkeypatch, envs, port_open, alive):
    monkeypatch.setattr(platforms, "load_registry", lambda layout: (envs, []))
    monkeypatch.setattr(chrome, "port_open", port_open)
    monkeypatch.setattr(chrome, "cdp_browser_alive", alive)


def test_supervisor_restarts_a_frozen_browser_whose_port_is_still_open(tmp_path, monkeypatch):
    env = _Env("env_1", 9224)
    launched = []
    _stub_daemon(monkeypatch, [env], lambda port, **kw: True,
                 lambda port, **kw: {"ok": False, "error": "TimeoutError"})
    d = daemon_mod.Daemon(_Layout(tmp_path))
    d._environment_was_signed_in = lambda e: True
    monkeypatch.setattr(daemon_mod, "browser", types.SimpleNamespace(
        open_environment=lambda layout, e, **kw: launched.append(e.env_id) or
        {"opened": True, "reused": False, "reason": "LAUNCHED"}), raising=False)
    import agent.winport.browser as browser_mod
    monkeypatch.setattr(browser_mod, "open_environment",
                        lambda layout, e, **kw: launched.append(e.env_id) or
                        {"opened": True, "reused": False, "reason": "LAUNCHED"})
    info = d._keep_browsers_alive()
    assert info["hung"] == ["env_1"]
    assert launched == ["env_1"]
    assert info["restarted"] == [{"env_id": "env_1", "ok": True, "reason": "LAUNCHED"}]


def test_supervisor_leaves_a_healthy_browser_alone(tmp_path, monkeypatch):
    env = _Env("env_2", 9225)
    launched = []
    _stub_daemon(monkeypatch, [env], lambda port, **kw: True,
                 lambda port, **kw: {"ok": True, "browser": "Chrome/156"})
    import agent.winport.browser as browser_mod
    monkeypatch.setattr(browser_mod, "open_environment",
                        lambda *a, **k: launched.append(1) or {"opened": True})
    info = daemon_mod.Daemon(_Layout(tmp_path))._keep_browsers_alive()
    assert launched == []
    assert info.get("hung") is None
    assert info["restarted"] == []


# --------------------------------------------------------- launch_environment

def test_launch_environment_replaces_a_frozen_instance(tmp_path, monkeypatch):
    layout = _Layout(tmp_path)
    env = _Env("env_1", 9224)
    killed, spawned = [], []
    ports = {"open": True}

    monkeypatch.setattr(chrome, "resolve_browser",
                        lambda **kw: {"found": True, "path": "/c/Program Files/Google/Chrome/Application/chrome.exe",
                                      "tier": 1, "engine": "chrome"})
    monkeypatch.setattr(chrome, "file_version", lambda path: "156.0.8078.80")
    monkeypatch.setattr(chrome, "policy_remote_debugging", lambda **kw: {"blocked": False})
    monkeypatch.setattr(chrome, "read_json", lambda path, default=None: {"ok": True})
    monkeypatch.setattr(chrome, "reusable_instance_evidence",
                        lambda *a, **kw: {"ok": True, "pid": 52764, "port": 9224, "argv": []})
    monkeypatch.setattr(chrome, "find_open_page", lambda *a, **kw: {"url": "https://x"})
    monkeypatch.setattr(chrome, "port_open", lambda port, **kw: ports["open"])
    monkeypatch.setattr(chrome, "clear_stale_profile_locks", lambda profile: [])

    def _kill(profile, **kw):
        killed.append(kw.get("expected_port"))
        ports["open"] = False
        return 6

    from agent.winport import proc
    monkeypatch.setattr(proc, "kill_profile_browsers", _kill)
    monkeypatch.setattr(chrome, "spawn", lambda args, **kw: types.SimpleNamespace(pid=999))
    monkeypatch.setattr(chrome, "wait_for_cdp", lambda port, **kw: {"ok": True, "browser": "Chrome/156"})
    monkeypatch.setattr(chrome, "resolve_browser_pid", lambda *a, **kw: (999, True))

    probes = [{"ok": False, "error": "TimeoutError"}, {"ok": True, "browser": "Chrome/156"}]
    result = chrome.launch_environment(env, layout, url="https://creator.douyin.com/",
                                       alive_probe=lambda port, **kw: probes.pop(0))
    assert killed == [9224]
    assert result["hung_browser_restarted"] is True
    assert result["reason"] != "TAB_REUSED"
    assert result["opened"] is True
    assert result["pid"] == 999


def test_launch_environment_reuses_a_browser_that_answers(monkeypatch):
    class _L(_Layout):
        pass

    import tempfile
    layout = _L(tempfile.mkdtemp())
    env = _Env("env_1", 9224)
    monkeypatch.setattr(chrome, "resolve_browser",
                        lambda **kw: {"found": True, "path": "/c/Program Files/Google/Chrome/Application/chrome.exe",
                                      "tier": 1, "engine": "chrome"})
    monkeypatch.setattr(chrome, "file_version", lambda path: "156.0.8078.80")
    monkeypatch.setattr(chrome, "policy_remote_debugging", lambda **kw: {"blocked": False})
    monkeypatch.setattr(chrome, "read_json", lambda path, default=None: {"ok": True})
    monkeypatch.setattr(chrome, "reusable_instance_evidence",
                        lambda *a, **kw: {"ok": True, "pid": 52764, "port": 9224, "argv": []})
    monkeypatch.setattr(chrome, "find_open_page", lambda *a, **kw: {"url": "https://creator.douyin.com/x"})
    monkeypatch.setattr(chrome, "port_open", lambda port, **kw: True)
    monkeypatch.setattr(chrome, "cdp_version", lambda port, **kw: {"ok": True, "browser": "Chrome/156"})
    result = chrome.launch_environment(env, layout, url="https://creator.douyin.com/",
                                       alive_probe=lambda port, **kw: {"ok": True, "browser": "Chrome/156"})
    assert result["reason"] == "TAB_REUSED"
    assert result["reused"] is True
    assert "hung_browser_restarted" not in result
