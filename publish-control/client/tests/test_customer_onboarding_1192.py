"""1.2.192 new-customer package: licence handover truth + fail-closed checks."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from winport import activation, licstate, onboard, panel, selftest  # noqa: E402
from winport.fingerprint import fingerprint  # noqa: E402
from winport.license import initialize_trial  # noqa: E402
from winport.paths import Layout  # noqa: E402


def _layout(tmp_path: Path) -> Layout:
    return Layout(program=ROOT, data=tmp_path / "data",
                  system=tmp_path / "system").ensure()


def _bootstrap_module():
    spec = importlib.util.spec_from_file_location(
        "bootstrap_py_1192", ROOT / "scripts" / "bootstrap_py.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_customer_readme_uses_activation_code_primary_flow():
    raw = (ROOT / "CUSTOMER_README.txt").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw.count(b"\r\n") == raw.count(b"\n")
    text = raw.decode("utf-8-sig")
    for expected in (
            "14 天试用",
            "查看机器码",
            "8 位激活码",
            "用激活码激活",
            "授权维护 → 导入授权",
            "import-license.bat",
            "%LOCALAPPDATA%\\HuolongAgent\\publish-worker\\logs\\机器码.txt",
            "license.json"):
        assert expected in text, expected
    for forbidden in (
            "装完自动完成授权",
            "重试自动授权",
            "我们在后台处理",
            "不需要你导入文件",
            "不用发机器码",
            "export-machine-code.bat"):
        assert forbidden not in text, forbidden


def test_panel_auto_activation_button_follows_activation_json(
        tmp_path, monkeypatch):
    import winport.paths as paths

    monkeypatch.setattr(paths, "install_root", lambda: tmp_path)
    assert panel._auto_activation_available() is False
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "activation.json").write_text("{}", encoding="utf-8")
    assert panel._auto_activation_available() is True


def test_panel_hides_dead_retry_button_but_keeps_real_licence_entries(tmp_path):
    layout = _layout(tmp_path)
    state = panel.build_state(layout, [], tcp=lambda host, port: False)
    assert state["auto_activation_available"] is False
    page = panel.render_html(state)
    assert "重试自动授权" not in page
    assert "查看机器码" in page
    assert "用激活码激活" in page
    assert "导入授权" in page

    state["auto_activation_available"] = True
    page = panel.render_html(state)
    assert "重试自动授权" in page


def test_selftest_without_licence_or_trial_is_red_and_points_to_machine_code(
        tmp_path):
    layout = _layout(tmp_path)
    result = selftest.run_selftest(layout, static=True)
    level, detail = result["checks"]["license"]
    assert level == selftest.STATUS_RED
    assert "试用未创建或已失效" in detail
    assert "查看机器码" in detail
    assert "用激活码激活" in detail
    assert result["machine_code"] == fingerprint()["machine_id"]


def test_selftest_with_valid_trial_warns_with_days_left(tmp_path):
    layout = _layout(tmp_path)
    created = initialize_trial(layout)
    assert created["created"] is True
    result = selftest.run_selftest(layout, static=True)
    level, detail = result["checks"]["license"]
    assert level == selftest.STATUS_WARN
    assert "试用授权有效" in detail
    assert "剩余" in detail
    assert result["exit_code"] == selftest.EXIT_OK


def test_selftest_expiring_formal_licence_warns_with_renewal_hint(
        tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    monkeypatch.setattr(licstate, "read_license_file",
                        lambda _path: {"kind": "ok", "obj": {}})
    monkeypatch.setattr(
        licstate, "unified_license_state",
        lambda *_args, **_kwargs: {
            "status": "OK", "publish_allowed": True, "days_left": 7.0,
            "expiry_level": "7", "text": "授权有效，剩余 7.0 天",
            "label": "授权有效", "reasons": [], "notices": [],
        })
    level, detail = selftest._license_light(layout, static=True)
    assert level == selftest.STATUS_WARN
    assert "licence OK" in detail
    assert "剩余 7.0 天" in detail
    assert "续期" in detail


def test_selftest_last_day_formal_licence_warns(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    monkeypatch.setattr(licstate, "read_license_file",
                        lambda _path: {"kind": "ok", "obj": {}})
    monkeypatch.setattr(
        licstate, "unified_license_state",
        lambda *_args, **_kwargs: {
            "status": "OK", "publish_allowed": True, "days_left": 1.0,
            "expiry_level": "1", "text": "授权有效，剩余 1.0 天",
            "label": "授权有效", "reasons": [], "notices": [],
        })
    level, detail = selftest._license_light(layout, static=True)
    assert level == selftest.STATUS_WARN
    assert "剩余 1.0 天" in detail
    assert "续期" in detail


def test_selftest_formal_licence_in_grace_warns(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    monkeypatch.setattr(licstate, "read_license_file",
                        lambda _path: {"kind": "ok", "obj": {}})
    monkeypatch.setattr(
        licstate, "unified_license_state",
        lambda *_args, **_kwargs: {
            "status": "GRACE", "publish_allowed": True, "days_left": -2.0,
            "expiry_level": "grace", "text": "授权已过期",
            "label": "授权已过期", "reasons": [], "notices": [],
        })
    level, detail = selftest._license_light(layout, static=True)
    assert level == selftest.STATUS_WARN
    assert "已过期" in detail
    assert "离线宽限期" in detail
    assert "只停新增发布" in detail


def test_selftest_invalid_formal_licence_is_red_with_activation_route(
        tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    monkeypatch.setattr(licstate, "read_license_file",
                        lambda _path: {"kind": "ok", "obj": {}})
    monkeypatch.setattr(
        licstate, "unified_license_state",
        lambda *_args, **_kwargs: {
            "status": "EXPIRED", "publish_allowed": False, "days_left": -1.0,
            "expiry_level": "grace", "text": "授权已过期",
            "label": "授权已过期", "reasons": ["授权已过期"], "notices": [],
        })
    level, detail = selftest._license_light(layout, static=True)
    assert level == selftest.STATUS_RED
    assert "查看机器码" in detail
    assert "用激活码激活" in detail


def test_bootstrap_trial_failure_is_explicit_in_console_and_install_log(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("WINPORT_SYSTEM", str(tmp_path / "system"))
    program = tmp_path / "program"
    program.mkdir()
    (program / "VERSION").write_text("1.2.192", encoding="utf-8")

    import winport.license as license_mod
    monkeypatch.setattr(
        license_mod, "initialize_trial",
        lambda *_args, **_kwargs: {"created": False,
                                   "reason": "trial write failed: disk full"})
    monkeypatch.setattr(
        licstate, "unified_license_state",
        lambda *_args, **_kwargs: {
            "status": "NOT_ACTIVATED", "publish_allowed": False,
            "reasons": ["未找到授权文件，且没有可继续的试用记录"],
        })

    module = _bootstrap_module()
    installer = module.Installer(program)
    installer.step_trial()
    text = "\n".join(installer.lines)
    assert "程序已安装，但这台电脑还不能发布" in text
    assert "人工恢复办法" in text
    assert "查看机器码" in text
    assert "用激活码激活" in text
    assert "import-license.bat" in text
    assert installer.problems
    log = installer.data / "logs" / "install.log"
    log_text = log.read_text(encoding="utf-8")
    assert "程序已安装，但这台电脑还不能发布" in log_text
    assert "人工恢复办法" in log_text


def test_bootstrap_trial_success_passes_handover(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("WINPORT_SYSTEM", str(tmp_path / "system"))
    program = tmp_path / "program"
    program.mkdir()
    (program / "VERSION").write_text("1.2.192", encoding="utf-8")

    module = _bootstrap_module()
    installer = module.Installer(program)
    installer.step_trial()
    assert installer.license_handover_ok is True
    assert installer.problems == []
    assert installer.step_license_handover() is True
    assert "trial active" in "\n".join(installer.lines)


def test_activation_and_onboard_text_point_to_the_real_route(tmp_path):
    layout = _layout(tmp_path)
    result = activation.activate(layout)
    assert result["ok"] is False
    assert "查看机器码" in result["reason"]
    assert "用激活码激活" in result["reason"]
    assert "无需自行导入授权" not in result["reason"]

    text = onboard.machine_code_text(onboard.machine_code_info())
    assert "8 位激活码" in text
    assert "查看机器码" in text
    assert "import-license.bat" in text
    assert "不用发机器码" not in text
