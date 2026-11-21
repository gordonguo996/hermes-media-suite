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
