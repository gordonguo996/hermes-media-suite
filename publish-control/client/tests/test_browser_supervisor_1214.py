"""1.2.214: browsers of signed-in environments are kept alive, safely."""
from pathlib import Path

from agent.winport import browser as browser_mod
from agent.winport import chrome, daemon as daemon_mod, health, platforms
from agent.winport.lock import FileLock


class _Env:
    def __init__(self, env_id, port, enabled=True, label=None):
        self.env_id = env_id
        self.debug_port = port
        self.enabled = enabled
        self.label = label or env_id


class _Layout:
    def __init__(self, root):
        self.root = Path(root)
        self.logs = self.root / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.run = self.root / "run"
        self.run.mkdir(parents=True, exist_ok=True)
        self.profiles = self.root / "profiles"
        self.program = self.root


def _daemon(tmp_path, signed_in=("env_1", "env_2")):
    d = daemon_mod.Daemon(_Layout(tmp_path))
    d._environment_was_signed_in = lambda env: env.env_id in signed_in
    return d


def _stub(monkeypatch, envs, port_open, open_impl, cdp_alive=None):
    monkeypatch.setattr(platforms, "load_registry", lambda layout: (envs, []))
    monkeypatch.setattr(chrome, "port_open", port_open)
    # 1.2.216: a listening port only counts as healthy when the browser also
    # answers a real CDP handshake, so every stub must state that explicitly.
    monkeypatch.setattr(chrome, "cdp_browser_alive",
                        cdp_alive or (lambda port, **kw: {"ok": True, "browser": "Chrome/156"}))
    monkeypatch.setattr(browser_mod, "open_environment", open_impl)


def test_restarts_browser_when_port_dead_and_env_was_signed_in(tmp_path, monkeypatch):
    env = _Env("env_2", 9225)
    calls = []
    _stub(monkeypatch, [env], lambda port, **kw: False,
          lambda layout, e, **kw: calls.append(e.env_id) or
          {"opened": True, "reused": False, "reason": "LAUNCHED"})
    info = _daemon(tmp_path)._keep_browsers_alive()
    assert calls == ["env_2"]
    assert info["restarted"] == [{"env_id": "env_2", "ok": True, "reason": "LAUNCHED"}]


def test_does_nothing_when_port_is_up(tmp_path, monkeypatch):
    env = _Env("env_1", 9224)
    calls = []
    _stub(monkeypatch, [env], lambda port, **kw: True,
          lambda *a, **k: calls.append(1) or {"opened": True})
    info = _daemon(tmp_path)._keep_browsers_alive()
    assert calls == []
    assert info["restarted"] == []
    assert info["checked"] == 1


def test_never_signed_in_environment_is_left_alone(tmp_path, monkeypatch):
    env = _Env("env_3", 9226)
    calls = []
    _stub(monkeypatch, [env], lambda port, **kw: False,
          lambda *a, **k: calls.append(1) or {"opened": True})
    info = _daemon(tmp_path)._keep_browsers_alive()
    assert calls == []
    assert info["skipped"] == ["env_3"]


def test_disabled_and_port_less_environments_are_ignored(tmp_path, monkeypatch):
    envs = [_Env("env_off", 9227, enabled=False), _Env("env_noport", None)]
    calls = []
    _stub(monkeypatch, envs, lambda port, **kw: False,
          lambda *a, **k: calls.append(1) or {"opened": True})
    info = _daemon(tmp_path, signed_in=("env_off", "env_noport"))._keep_browsers_alive()
    assert calls == []
    assert info["checked"] == 0


def test_success_is_followed_by_a_cooldown(tmp_path, monkeypatch):
    """A browser that dies right after starting must not be relaunched at once."""
    env = _Env("env_2", 9225)
    calls = []
    _stub(monkeypatch, [env], lambda port, **kw: False,
          lambda *a, **k: calls.append(1) or {"opened": True, "reused": False})
    d = _daemon(tmp_path)
    d._keep_browsers_alive()
    info = d._keep_browsers_alive()
    assert len(calls) == 1
    assert info["skipped"] == ["env_2"]


def test_failure_backs_off_then_recovers(tmp_path, monkeypatch):
    env = _Env("env_2", 9225)
    calls = []
    _stub(monkeypatch, [env], lambda port, **kw: False,
          lambda *a, **k: calls.append(1) or
          {"opened": False, "reused": False, "reason": "BROWSER_MISSING"})
    d = _daemon(tmp_path)
    d._keep_browsers_alive()
    d._keep_browsers_alive()
    assert len(calls) == 1, "second round must respect the backoff"
    d._browser_cooldown["env_2"] = 0        # cooldown expired
    d._keep_browsers_alive()
    assert len(calls) == 2


def test_one_broken_environment_does_not_stop_the_others(tmp_path, monkeypatch):
    calls = []

    def _open(layout, env, **kw):
        calls.append(env.env_id)
        if env.env_id == "env_2":
            raise RuntimeError("chrome path missing")
        return {"opened": True, "reused": False}

    envs = [_Env("env_2", 9225), _Env("env_1", 9224)]
    _stub(monkeypatch, envs, lambda port, **kw: False, _open)
    info = _daemon(tmp_path)._keep_browsers_alive()
    assert calls == ["env_2", "env_1"]
    assert info["errors"] and "env_2" in info["errors"][0]
    assert info["restarted"] == [{"env_id": "env_1", "ok": True, "reason": ""}]


def test_only_one_launch_per_round(tmp_path, monkeypatch):
    """N dead environments must not stack N x 15s into one heartbeat."""
    calls = []
    _stub(monkeypatch, [_Env("env_1", 9224), _Env("env_2", 9225)],
          lambda port, **kw: False,
          lambda layout, env, **kw: calls.append(env.env_id) or
          {"opened": True, "reused": False})
    _daemon(tmp_path)._keep_browsers_alive()
    assert calls == ["env_1"]


def test_launch_lock_outputs_busy_instead_of_second_chrome(tmp_path, monkeypatch):
    """open_environment is the single launch gate: a held lock means 'busy'."""
    layout = _Layout(tmp_path)
    env = _Env("env_2", 9225)
    calls = []
    monkeypatch.setattr(browser_mod, "_open_environment_impl",
                        lambda *a, **k: calls.append(1) or {"opened": True})
    lock = FileLock(layout.run / f"env-{env.env_id}.launch.lock")
    assert lock.acquire()
    try:
        result = browser_mod.open_environment(layout, env)
    finally:
        lock.release()
    assert calls == []
    assert result["opened"] is False
    assert result["reason"] == "LAUNCH_IN_PROGRESS"


def test_launch_lock_is_released_after_a_normal_launch(tmp_path, monkeypatch):
    layout = _Layout(tmp_path)
    env = _Env("env_2", 9225)
    monkeypatch.setattr(browser_mod, "_open_environment_impl",
                        lambda *a, **k: {"opened": True, "reused": False})
    assert browser_mod.open_environment(layout, env)["opened"] is True
    probe = FileLock(layout.run / f"env-{env.env_id}.launch.lock")
    assert probe.acquire(), "lock must be free again after the launch"
    probe.release()


def test_supervisor_never_breaks_the_heartbeat_round(tmp_path, monkeypatch):
    def _boom(self):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(daemon_mod.Daemon, "_keep_browsers_alive", _boom)
    monkeypatch.setattr(daemon_mod.Daemon, "refresh_channels", lambda self: [])
    monkeypatch.setattr(daemon_mod.Daemon, "check_licence", lambda self: {})
    monkeypatch.setattr(daemon_mod.Daemon, "_write_heartbeat", lambda self, s: None)
    monkeypatch.setattr(daemon_mod.report_mod, "write_report",
                        lambda layout, collection=None: {"md": "", "xlsx": ""})
    summary = _daemon(tmp_path).run_round()
    assert summary["browsers"]["error"].startswith("RuntimeError")


def test_audit_failure_does_not_undo_a_good_launch(tmp_path, monkeypatch):
    """A failing audit write must not skip the cooldown or the break."""
    env = _Env("env_2", 9225)
    calls = []
    _stub(monkeypatch, [env], lambda port, **kw: False,
          lambda layout, e, **kw: calls.append(e.env_id) or
          {"opened": True, "reused": False, "reason": "LAUNCHED"})

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(daemon_mod, "audit", _boom)
    d = _daemon(tmp_path)
    before = daemon_mod.time.monotonic()
    info = d._keep_browsers_alive()
    assert info["restarted"][0]["ok"] is True
    assert info["errors"][0].startswith("audit env_2")
    # success cooldown (120s), not the 300s failure backoff
    assert d._browser_cooldown["env_2"] - before <= 130


def test_round_budget_stops_further_launch_attempts(tmp_path, monkeypatch):
    """A dead profile can burn the launch timeout; the round must cap it."""
    calls = []
    _stub(monkeypatch, [_Env("env_1", 9224), _Env("env_2", 9225)],
          lambda port, **kw: False,
          lambda layout, env, **kw: calls.append(env.env_id) or
          {"opened": False, "reused": False, "reason": "BROWSER_MISSING"})
    d = _daemon(tmp_path)
    d.browser_budget_seconds = 0
    info = d._keep_browsers_alive()
    assert calls == []
    assert info["budget_exhausted"] is True


def test_remaining_budget_is_passed_as_launch_timeout(tmp_path, monkeypatch):
    seen = {}
    _stub(monkeypatch, [_Env("env_2", 9225)], lambda port, **kw: False,
          lambda layout, env, **kw: seen.update(kw) or
          {"opened": True, "reused": False})
    _daemon(tmp_path)._keep_browsers_alive()
    assert 0 < seen.get("timeout", 0) <= 20.0


def test_signed_in_evidence_is_read_from_login_check_state(tmp_path, monkeypatch):
    """Eligibility comes from a real ONLINE login check, not from a cookie file."""
    layout = _Layout(tmp_path)

    class _Channel:
        def __init__(self, channel_id, env_id):
            self.channel_id = channel_id
            self.env_id = env_id

    channels = [_Channel("env_2:douyin", "env_2")]
    monkeypatch.setattr(platforms, "load_registry", lambda lay: ([], channels))
    d = daemon_mod.Daemon(layout)

    monkeypatch.setattr(health, "load_state",
                        lambda lay: {"channels": {"env_2:douyin": {"evidence": {
                            "login_check": {"status": "OFFLINE"}}}}})
    assert d._environment_was_signed_in(_Env("env_2", 9225)) is False

    monkeypatch.setattr(health, "load_state",
                        lambda lay: {"channels": {"env_2:douyin": {"evidence": {
                            "login_check": {"status": "ONLINE"}}}}})
    assert d._environment_was_signed_in(_Env("env_2", 9225)) is True
    assert d._environment_was_signed_in(_Env("env_9", 9229)) is False


def test_failed_launches_consume_the_round_budget(tmp_path, monkeypatch):
    """A 20s failure eats the budget: the next env gets the remainder only."""
    clock = {"t": 1000.0}

    class _Clock:
        @staticmethod
        def monotonic():
            return clock["t"]

        strftime = staticmethod(daemon_mod.time.strftime)

    monkeypatch.setattr(daemon_mod, "time", _Clock)
    seen = []

    def _open(layout, env, **kw):
        seen.append((env.env_id, kw.get("timeout")))
        clock["t"] += 20.0                      # a real launch timeout
        return {"opened": False, "reused": False, "reason": "BROWSER_MISSING"}

    envs = [_Env("env_1", 9224), _Env("env_2", 9225), _Env("env_3", 9226)]
    _stub(monkeypatch, envs, lambda port, **kw: False, _open)
    d = _daemon(tmp_path, signed_in=("env_1", "env_2", "env_3"))
    info = d._keep_browsers_alive()
    assert [item[0] for item in seen] == ["env_1", "env_2"]
    assert abs(seen[0][1] - 20.0) < 0.2
    assert abs(seen[1][1] - 5.0) < 0.2, "second launch must get the remaining budget"
    assert info["budget_exhausted"] is True
