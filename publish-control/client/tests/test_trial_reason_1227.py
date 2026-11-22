"""1.2.227: when no trial is running the panel says why, in words the customer can act on."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from winport import license as lic  # noqa: E402
from winport.paths import Layout  # noqa: E402

FP = {"machine_id": "m" * 32, "weak": False}
NOW = datetime(2026, 11, 21, 8, 0, tzinfo=timezone.utc)


def _layout(tmp_path):
    return Layout(program=ROOT, data=tmp_path / "data", system=tmp_path / "sys").ensure()


def test_fresh_machine_asks_for_activation(tmp_path):
    code, text = lic.trial_unavailable_reason(_layout(tmp_path), NOW, fingerprint_info=FP)
    assert code == "none"
    assert "激活码" in text and "损坏" not in text


def test_licensed_before_is_not_called_corrupt(tmp_path):
    layout = _layout(tmp_path)
    lic.mark_trial_license_seen(layout, now=NOW, fingerprint_info=FP)
    code, text = lic.trial_unavailable_reason(layout, NOW, fingerprint_info=FP)
    assert code == "had_licence"
    assert "导入授权" in text and "损坏" not in text


def test_expired_trial_says_expired(tmp_path):
    layout = _layout(tmp_path)
    assert lic.initialize_trial(layout, now=NOW - timedelta(days=30), fingerprint_info=FP)["created"]
    code, text = lic.trial_unavailable_reason(layout, NOW, fingerprint_info=FP)
    assert code == "expired" and "到期" in text


def test_garbage_record_is_reported_without_restarting_trial(tmp_path):
    layout = _layout(tmp_path)
    assert lic.initialize_trial(layout, now=NOW, fingerprint_info=FP)["created"]
    lic._trial_path(layout).write_text("{not json", encoding="utf-8")
    code, text = lic.trial_unavailable_reason(layout, NOW, fingerprint_info=FP)
    assert code in ("corrupt", "conflict")
    assert "不会自动重新开始试用" in text
    assert lic.initialize_trial(layout, now=NOW, fingerprint_info=FP)["created"] is False
