"""1.2.222: publish holds are released by source/reason policy, not blindly.

Four end-to-end bot paths are covered here: an agent transient hold may be
cleared, a human hold must be refused, the 24h limit stops auto-clearing, and
an auto-cleared hold still cannot publish when the fresh dry-run fails.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "client"
sys.path.insert(0, str(CLIENT / "agent"))

from winport.publish_guard import (  # noqa: E402
    PUBLISH_UNKNOWN,
    PublishHoldStore,
    RISK_CONTROL,
)


def _load_bot():
    sys.path.insert(0, str(ROOT / "feishu_publish_bot"))
    spec = importlib.util.spec_from_file_location(
        "bot_hold_policy_1222", ROOT / "feishu_publish_bot" / "bot.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch("sys.stderr", io.StringIO()):
        spec.loader.exec_module(module)
    return module


class HoldRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "publish_holds.json"
        self.store = PublishHoldStore(self.path)

    def test_legacy_record_without_fields_is_treated_as_human(self):
        self.path.write_text(json.dumps({"holds": {
            "ch-legacy": {"kind": PUBLISH_UNKNOWN, "reason": "old"}}}),
            encoding="utf-8")
        hold = PublishHoldStore(self.path).get("ch-legacy")
        self.assertEqual(hold["source"], "human")
        self.assertEqual(hold["reason_code"], "")

    def test_agent_hold_carries_reason_code(self):
        self.store.set_hold("ch-agent", PUBLISH_UNKNOWN, {"reason": "x"})
        hold = self.store.get("ch-agent")
        self.assertEqual(hold["source"], "agent")
        self.assertEqual(hold["reason_code"], "publish_unknown")

    def test_bot_clear_is_enforced_on_the_client_too(self):
        self.store.set_hold("ch-risk", RISK_CONTROL, {"reason": "风控"},
                            source="agent", reason_code="risk_control")
        refused = self.store.clear_manual(
            "ch-risk", reason="bot retry", actor="bot")
        self.assertFalse(refused["cleared"])
        self.assertTrue(refused["refused"])
        self.assertEqual(refused["code"], "reason_not_allowed")
        self.assertIsNotNone(self.store.get("ch-risk"))
        human = self.store.clear_manual(
            "ch-risk", reason="operator confirmed", actor="human")
        self.assertTrue(human["cleared"])
        self.assertIsNone(self.store.get("ch-risk"))


class BotHoldPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "auto_unblock.log"
        self.calls = []
        self.hold = None
        self.dry_run = {"ok": True, "confirm_token": "tok"}
        patches = [
            mock.patch.object(self.bot, "AUTO_UNBLOCK_LOG", str(self.log)),
            mock.patch.object(self.bot, "run_cli", self._run_cli),
            mock.patch.object(self.bot, "_parse_json_blob",
                              lambda text: json.loads(text)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _run_cli(self, args, timeout=115, customer_id=None):
        self.calls.append(list(args))
        if args[0] == "publish-hold-status":
            return {"stdout": json.dumps({"channel": args[1],
                                          "hold": self.hold}), "exit_code": 0}
        if args[0] == "publish-hold-clear":
            return {"stdout": json.dumps({
                "cleared": True, "source": "agent",
                "reason_code": self.hold.get("reason_code"),
                "code": "cleared"}), "exit_code": 0}
        if args[0] == "dry-run":
            return {"stdout": json.dumps(self.dry_run), "exit_code": 0}
        if args[0] == "publish":
            return {"stdout": json.dumps({"published": True}), "exit_code": 0}
        return {"stdout": "", "exit_code": 1}

    def _calls(self, name):
        return [args for args in self.calls if args and args[0] == name]

    def _audit(self):
        return [json.loads(line) for line in
                self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_agent_transient_hold_is_cleared_then_published(self):
        self.hold = {"kind": "lease_timeout", "source": "agent",
                     "reason_code": "lease_timeout"}
        result, recovery = self.bot.run_publish_with_recovery(
            ["publish", "--plan", "plan.json"], "ch-1")
        self.assertTrue(json.loads(result["stdout"])["published"])
        self.assertEqual(recovery["result"], "cleared")
        self.assertEqual(len(self._calls("publish-hold-clear")), 1)
        self.assertEqual(len(self._calls("dry-run")), 1)
        audit = self._audit()
        self.assertEqual(audit[-1]["source"], "agent")
        self.assertEqual(audit[-1]["reason_code"], "lease_timeout")
        self.assertEqual(audit[-1]["result"], "cleared")
        self.assertEqual(audit[-1]["count_24h"], 0)

    def test_human_hold_is_refused_with_manual_notice(self):
        self.hold = {"kind": "risk_control", "source": "human",
                     "reason_code": ""}
        result, recovery = self.bot.run_publish_with_recovery(
            ["publish", "--plan", "plan.json"], "ch-1")
        self.assertEqual(self._calls("publish-hold-clear"), [])
        self.assertEqual(self._calls("publish"), [])
        self.assertFalse(recovery["allowed"])
        self.assertEqual(recovery["result"], "refused_source")
        self.assertIn("发布被冻结，需人工确认后解除", result["stderr"])
        self.assertEqual(self._audit()[-1]["result"], "refused_source")

    def test_limit_stops_auto_clear_after_three_successes(self):
        now = time.time()
        rows = [{"at_epoch": now - 60 * (index + 1), "channel": "ch-1",
                 "result": "cleared"} for index in range(3)]
        self.log.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                            encoding="utf-8")
        self.hold = {"kind": "publish_unknown", "source": "agent",
                     "reason_code": "publish_unknown"}
        result, recovery = self.bot.run_publish_with_recovery(
            ["publish", "--plan", "plan.json"], "ch-1")
        self.assertEqual(self._calls("publish-hold-clear"), [])
        self.assertEqual(self._calls("publish"), [])
        self.assertEqual(recovery["result"], "limit")
        self.assertIn("24 小时内已自动解冻 3 次", result["stderr"])
        self.assertEqual(self._audit()[-1]["result"], "limit")
        self.assertEqual(self._audit()[-1]["count_24h"], 3)

    def test_failed_dry_run_after_clear_blocks_publish(self):
        self.hold = {"kind": "publish_unknown", "source": "agent",
                     "reason_code": "publish_unknown"}
        self.dry_run = {"ok": False,
                        "problems": [{"message": "标题超长"}]}
        result, recovery = self.bot.run_publish_with_recovery(
            ["publish", "--plan", "plan.json"], "ch-1")
        self.assertEqual(len(self._calls("publish-hold-clear")), 1)
        self.assertEqual(self._calls("publish"), [])
        self.assertEqual(recovery["result"], "dry_run_failed")
        self.assertIn("发布前校验未通过", result["stderr"])
        self.assertEqual(self._audit()[-1]["result"], "dry_run_failed")


if __name__ == "__main__":
    unittest.main()
