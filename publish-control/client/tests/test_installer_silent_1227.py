"""1.2.227: a silent install must never block on a message box (found on a real Windows machine:
a failed /S install sat on a modal dialog forever, which would also hang CI and any scripted rollout)."""
from __future__ import annotations

from pathlib import Path

NSI = Path(__file__).resolve().parents[1] / "installer" / "setup.nsi"


def test_every_message_box_has_a_silent_default():
    lines = [l for l in NSI.read_text(encoding="utf-8-sig").splitlines() if "MessageBox" in l and not l.strip().startswith(";")]
    assert lines, "installer has no message boxes?"
    missing = [l.strip() for l in lines if "/SD" not in l]
    assert not missing, missing


def test_failed_install_exits_non_zero():
    text = NSI.read_text(encoding="utf-8-sig")
    assert "SetErrorLevel 5" in text and "rollback:" in text


def test_missing_licence_alone_keeps_the_install():
    nsi = NSI.read_text(encoding="utf-8-sig")
    assert "IntCmp $0 6 pending failed failed" in nsi and "SetErrorLevel $R9" in nsi
    boot = (Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_py.py").read_text(encoding="utf-8")
    assert "EXIT_ACTIVATION_PENDING = 6" in boot and "--license-pending-ok" in boot


def test_selftest_license_pending_flag(tmp_path, capsys):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
    from winport import selftest
    from winport.errors import EXIT_OK

    real = selftest.run_selftest
    selftest.run_selftest = lambda layout: {
        "checks": {"license": ("RED", "x"), "python": ("GREEN", "ok")}, "red": ["license"],
        "exit_code": 2, "static": False, "machine_code": "m", "layout": ""}
    try:
        assert selftest.main(["--data", str(tmp_path / "d"), "--program", str(tmp_path / "p"),
                              "--license-pending-ok"]) == EXIT_OK
        assert selftest.main(["--data", str(tmp_path / "d"), "--program", str(tmp_path / "p")]) == 2
        selftest.run_selftest = lambda layout: {
            "checks": {"license": ("RED", "x"), "chrome": ("RED", "y")}, "red": ["license", "chrome"],
            "exit_code": 2, "static": False, "machine_code": "m", "layout": ""}
        assert selftest.main(["--data", str(tmp_path / "d"), "--program", str(tmp_path / "p"),
                              "--license-pending-ok"]) == 2
    finally:
        selftest.run_selftest = real
