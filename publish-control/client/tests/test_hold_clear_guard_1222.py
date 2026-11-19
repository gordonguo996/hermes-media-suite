"""1.2.222 follow-up: the client's graded hold policy cannot be sidestepped.

* A remote ``publish-hold-clear`` must say ``--actor bot``; without it the
  CLI defaults to ``human`` and would release any brake (risk control
  included), so that form needs the customer's remote assist.
* The 3-per-24h bot limit is enforced on the client from its own audit log,
  not only in the bot.
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

CLIENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLIENT / "agent"))

from winport import remote_channel, remote_policy  # noqa: E402
from winport.paths import Layout  # noqa: E402
from winport.publish_guard import (AUTO_CLEAR_MAX_24H, LEASE_TIMEOUT,  # noqa: E402
                                   RISK_CONTROL, PublishHoldStore)

PY = sys.executable
CH = "env_1:douyin"


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.layout = Layout(program=CLIENT, data=Path(tmp.name) / "data",
                             system=Path(tmp.name) / "sys").ensure()
        self.store = PublishHoldStore(self.layout.run / "publish_holds.json")


class RemoteActorTests(_Base):
    def argv(self, *extra):
        return [PY, "-m", "agent.winport.cli", "publish-hold-clear",
                "--channel", CH, "--reason", "r", *extra]

    def test_only_explicit_bot_actor_is_business(self):
        self.assertEqual(remote_policy.cli_subcommand(self.argv("--actor", "bot"), self.layout),
                         "publish-hold-clear")
        self.assertEqual(remote_policy.cli_subcommand(self.argv("--actor=bot"), self.layout),
                         "publish-hold-clear")
        for extra in ((), ("--actor", "human"), ("--actor", "bot", "--actor", "human"),
                      ("--actor",)):
            with self.subTest(extra=extra):
                self.assertIsNone(remote_policy.cli_subcommand(self.argv(*extra), self.layout))

    def test_relay_refuses_a_default_actor_clear_of_a_risk_hold(self):
        self.store.set_hold(CH, RISK_CONTROL, {"why": "platform"})
        service = remote_channel.ControlService(self.layout, logger=None)
        with self.assertRaises(PermissionError):
            service.dispatch("exec", {"argv": self.argv(), "timeout": 60})
        self.assertIsNotNone(self.store.get(CH))

    def test_relay_bot_clear_of_a_risk_hold_is_refused_by_the_store(self):
        import json
        self.store.set_hold(CH, RISK_CONTROL, {"why": "platform"})
        service = remote_channel.ControlService(self.layout, logger=None)
        argv = self.argv("--actor", "bot", "--data", str(self.layout.data))
        out = service.dispatch("exec", {"argv": argv, "timeout": 60})
        result = json.loads(out["stdout"].strip().splitlines()[-1])
        self.assertFalse(result["cleared"])
        self.assertEqual(result["code"], "reason_not_allowed")
        self.assertIsNotNone(self.store.get(CH))


class DailyLimitTests(_Base):
    def test_fourth_bot_clear_in_24h_is_refused_on_the_client(self):
        for _ in range(AUTO_CLEAR_MAX_24H):
            self.store.set_hold(CH, LEASE_TIMEOUT, {})
            self.assertTrue(self.store.clear_manual(CH, reason="auto", actor="bot")["cleared"])
        self.store.set_hold(CH, LEASE_TIMEOUT, {})
        res = self.store.clear_manual(CH, reason="auto", actor="bot")
        self.assertEqual((res["cleared"], res["code"], res["count_24h"]),
                         (False, "limit_24h", AUTO_CLEAR_MAX_24H))
        # the customer (human) can still release it
        self.assertTrue(self.store.clear_manual(CH, reason="checked", actor="human")["cleared"])

    def test_old_clears_and_other_channels_do_not_count(self):
        audit = self.layout.run / "publish_hold_audit.jsonl"
        import json
        with open(audit, "a", encoding="utf-8") as fh:
            for at in (time.time() - 90000,) * 5:
                fh.write(json.dumps({"channel_id": CH, "actor": "bot", "result": "cleared", "at": at}) + "\n")
            for _ in range(5):
                fh.write(json.dumps({"channel_id": "env_2:douyin", "actor": "bot",
                                     "result": "cleared", "at": time.time()}) + "\n")
            fh.write("not json\n")
        self.store.set_hold(CH, LEASE_TIMEOUT, {})
        self.assertTrue(self.store.clear_manual(CH, reason="auto", actor="bot")["cleared"])


if __name__ == "__main__":
    unittest.main()
