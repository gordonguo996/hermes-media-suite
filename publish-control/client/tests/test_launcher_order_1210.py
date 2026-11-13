"""1.2.210: an install must survive a locked Desktop shortcut.

Field failure (2026-11-10): 漫剧发布助手.vbs was open in wscript.exe, so
os.replace() raised WinError 5, the installer aborted at that step and the HKCU
Run value was never rewritten.  It still pointed at a deleted program
directory, so the machine could not start the agent again.

Rules under test:
  1. the live autostart value is written and read back BEFORE legacy values are
     deleted, and an unverifiable value is a failure (never an assumed success);
  2. a blocked desktop launcher falls back to "...-new.vbs" and never deletes
     the icon the user already has;
  3. the icon may only be skipped when a supervisor provides the start path.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

CLIENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLIENT))
sys.path.insert(0, str(CLIENT / "agent"))

_spec = importlib.util.spec_from_file_location(
    "bootstrap_py_1210", CLIENT / "scripts" / "bootstrap_py.py")
bp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bp)

REG_SZ = 1


class FakeHive:
    """winreg module double: module-level functions taking a key handle."""

    def __init__(self, values=None, fail_write=False, fail_read=False,
                 lie_on_read=False, fail_delete=None):
        self.values = dict(values or {})
        self.calls = []
        self.fail_write = fail_write
        self.fail_read = fail_read
        self.lie_on_read = lie_on_read
        self.fail_delete = set(fail_delete or ())

    def SetValueEx(self, key, name, reserved, reg_type, value):
        self.calls.append(("set", name))
        if self.fail_write:
            raise OSError(5, "access denied")
        self.values[name] = value

    def QueryValueEx(self, key, name):
        self.calls.append(("get", name))
        if self.fail_read:
            raise OSError(5, "access denied")
        if self.lie_on_read:
            return ("something-else", 1)
        return (self.values[name], 1)

    def DeleteValue(self, key, name):
        self.calls.append(("delete", name))
        if name in self.fail_delete:
            raise OSError(5, "access denied")
        if name not in self.values:
            raise FileNotFoundError(2, "missing")
        del self.values[name]


def test_live_value_written_and_verified_before_legacy_cleanup():
    reg = FakeHive(values={"ManjuPublishAssistant": "old", "OldTask": "x"})
    bp._update_run_value(reg, object(), "ManjuPublishAssistant", "new",
                         ["ManjuPublishAssistant", "OldTask"], REG_SZ,
                         lambda *_: None)
    kinds = [c[0] for c in reg.calls]
    assert kinds[:2] == ["set", "get"]          # write, then verify
    assert kinds.index("delete") > kinds.index("get")
    assert reg.values["ManjuPublishAssistant"] == "new"
    assert "OldTask" not in reg.values
    assert ("delete", "ManjuPublishAssistant") not in reg.calls


def test_failed_write_keeps_old_value_and_deletes_nothing():
    reg = FakeHive(values={"ManjuPublishAssistant": "old"}, fail_write=True)
    with pytest.raises(OSError):
        bp._update_run_value(reg, object(), "ManjuPublishAssistant", "new",
                             ["ManjuPublishAssistant", "OldTask"], REG_SZ,
                             lambda *_: None)
    assert reg.values["ManjuPublishAssistant"] == "old"
    assert [c for c in reg.calls if c[0] == "delete"] == []


def test_read_back_mismatch_raises_and_stops_before_delete():
    reg = FakeHive(values={"ManjuPublishAssistant": "old"}, lie_on_read=True)
    with pytest.raises(RuntimeError):
        bp._update_run_value(reg, object(), "ManjuPublishAssistant", "new",
                             ["ManjuPublishAssistant", "OldTask"], REG_SZ,
                             lambda *_: None)
    assert [c for c in reg.calls if c[0] == "delete"] == []


def test_unverifiable_value_is_not_treated_as_success():
    reg = FakeHive(values={"ManjuPublishAssistant": "old"}, fail_read=True)
    with pytest.raises(RuntimeError):
        bp._update_run_value(reg, object(), "ManjuPublishAssistant", "new",
                             ["ManjuPublishAssistant"], REG_SZ,
                             lambda *_: None)
    assert [c for c in reg.calls if c[0] == "delete"] == []


def test_legacy_cleanup_failure_is_reported_not_swallowed():
    seen = []
    reg = FakeHive(values={"ManjuPublishAssistant": "old", "OldTask": "x"},
                   fail_delete={"OldTask"})
    bp._update_run_value(reg, object(), "ManjuPublishAssistant", "new",
                         ["ManjuPublishAssistant", "OldTask"], REG_SZ,
                         seen.append)
    assert reg.values["ManjuPublishAssistant"] == "new"
    assert any("not removed" in line for line in seen)


def _patch_writer(monkeypatch, failing_names):
    def fake(path, payload, label):
        if Path(path).name in failing_names:
            raise RuntimeError("cannot write desktop launcher %s: WinError 5"
                               % path)
    monkeypatch.setattr(bp, "_write_verified_bytes", fake)


def test_locked_launcher_falls_back_to_exact_alternate_name(tmp_path, monkeypatch):
    target = tmp_path / "assistant.vbs"
    target.write_text("old working launcher", encoding="utf-8")
    _patch_writer(monkeypatch, {"assistant.vbs"})
    out = bp._write_desktop_launcher(target, b"new", lambda *_: None,
                                     allow_skip=True)
    assert out == tmp_path / "assistant-new.vbs"
    assert target.read_text(encoding="utf-8") == "old working launcher"


def test_skip_only_when_allowed(tmp_path, monkeypatch):
    target = tmp_path / "assistant.vbs"
    target.write_text("old", encoding="utf-8")
    _patch_writer(monkeypatch, {"assistant.vbs", "assistant-new.vbs"})
    assert bp._write_desktop_launcher(target, b"new", lambda *_: None,
                                      allow_skip=True) is None
    with pytest.raises(RuntimeError):
        bp._write_desktop_launcher(target, b"new", lambda *_: None,
                                   allow_skip=False)
    assert target.read_text(encoding="utf-8") == "old"


def _record_launcher(monkeypatch, events):
    def fake(target, payload, log, *, allow_skip):
        events.append(("launcher", Path(target)))
        return Path(target)
    monkeypatch.setattr(bp, "_write_desktop_launcher", fake)


def test_start_paths_with_supervisor_autostarts_first(tmp_path, monkeypatch):
    events = []
    supervisor = tmp_path / "run-agent-task.vbs"
    supervisor.write_text("x", encoding="utf-8")
    _record_launcher(monkeypatch, events)
    entry = bp._install_start_paths(
        supervisor=supervisor, target=tmp_path / "icon.vbs", payload=b"p",
        autostart=lambda path: events.append(("autostart", Path(path))),
        log=lambda *_: None)
    # Run must point at the supervisor BEFORE the cosmetic icon is attempted
    assert events == [("autostart", supervisor), ("launcher", tmp_path / "icon.vbs")]
    assert entry == tmp_path / "icon.vbs"


def test_start_paths_without_supervisor_writes_icon_then_autostarts(tmp_path, monkeypatch):
    events = []
    _record_launcher(monkeypatch, events)
    entry = bp._install_start_paths(
        supervisor=tmp_path / "missing.vbs", target=tmp_path / "icon.vbs",
        payload=b"p",
        autostart=lambda path: events.append(("autostart", Path(path))),
        log=lambda *_: None)
    # no supervisor: the icon must exist before Run is pointed at it
    assert events == [("launcher", tmp_path / "icon.vbs"),
                      ("autostart", tmp_path / "icon.vbs")]
    assert entry == tmp_path / "icon.vbs"


def test_without_supervisor_a_locked_icon_fails_the_install(tmp_path, monkeypatch):
    events = []
    _patch_writer(monkeypatch, {"icon.vbs", "icon-new.vbs"})
    with pytest.raises(RuntimeError):
        bp._install_start_paths(
            supervisor=tmp_path / "missing.vbs", target=tmp_path / "icon.vbs",
            payload=b"p",
            autostart=lambda path: events.append(("autostart", Path(path))),
            log=lambda *_: None)
    assert events == []          # never point Run at a launcher that failed
