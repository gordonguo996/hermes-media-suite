"""1.2.221: the bot uses named client operations when the client offers them,
keeps the legacy scripts for older clients, and tells the operator when a
1.2.221 client refuses something because remote assist is off.

The end-to-end class wires the bot's relay call straight into a real client
``ControlService`` so both halves of the protocol are exercised together.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "client"


def _load_bot():
    sys.path.insert(0, str(ROOT / "feishu_publish_bot"))
    spec = importlib.util.spec_from_file_location("bot_named_1221", ROOT / "feishu_publish_bot" / "bot.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch("sys.stderr", io.StringIO()):
        spec.loader.exec_module(module)
    return module


class _BotBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot()

    def setUp(self):
        self.calls = []
        self.legacy = []
        patches = [
            mock.patch.object(self.bot, "_runtime_ready", lambda: True),
            mock.patch.object(self.bot, "_client_exec_code", self._legacy),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.set_caps(["named-ops-1"])

    def set_caps(self, caps):
        with self.bot._RUNTIME_LOCK:
            self.bot._RUNTIME_STATE["capabilities"] = list(caps)

    def _legacy(self, code, timeout=60, cwd=None, env=None):
        self.legacy.append(code)
        return {"stdout": json.dumps({"ok": True, "legacy": True, "files": [],
                                      "removed": {}, "path": ""}), "exit_code": 0}

    def relay(self, answer):
        def fake(operation, payload=None, timeout=20):
            self.calls.append((operation, payload))
            return answer(operation, payload) if callable(answer) else answer
        p = mock.patch.object(self.bot, "_runtime_http", fake)
        p.start()
        self.addCleanup(p.stop)


class SwitchTests(_BotBase):
    def test_new_client_gets_named_ops_and_no_scripts(self):
        self.relay({"ok": True, "files": [], "gap_min": 5, "gap_max": 8})
        out = self.bot.scan_inbox()
        self.assertTrue(out["ok"])
        self.assertEqual(self.calls, [("named", {"name": "inbox-scan", "args": {}})])
        self.assertEqual(self.legacy, [])

    def test_old_client_keeps_the_legacy_script(self):
        self.set_caps([])
        self.relay({})
        out = self.bot.scan_inbox()
        self.assertTrue(out.get("legacy"))
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.legacy), 1)

    def test_plan_time_ledger_and_batch_arguments(self):
        self.relay({"ok": True})
        self.bot._update_plan_time(r"C:\data\run\plan.json", "2026-11-19T10:00:00+08:00")
        self.bot.clear_client_ledger_blocks()
        self.bot.make_batch_plans("chat", [{"path": r"C:\in\a.mp4"}, {"path": r"C:\in\b.mp4"}],
                                  [["#a"], []], "batch-0001", xingtu_task={"order_id": "O1"})
        names = [c[1]["name"] for c in self.calls]
        self.assertEqual(names, ["plan-set-time", "ledger-unblock", "plan-make"])
        batch = self.calls[2][1]["args"]
        self.assertEqual(batch["files"], [r"C:\in\a.mp4", r"C:\in\b.mp4"])
        self.assertEqual(batch["item_topics"], [["#a"], []])
        self.assertEqual(batch["batch_id"], "batch-0001")
        self.assertEqual(batch["xingtu_task"], {"order_id": "O1"})
        self.assertEqual(self.legacy, [])

    def test_remote_assist_refusal_becomes_a_customer_hint(self):
        def refuse(operation, payload):
            raise urllib.error.HTTPError(
                "http://x/named", 403, "Forbidden", {},
                io.BytesIO(json.dumps({"error": "x", "remote_assist_required": True}).encode()))
        self.relay(refuse)
        out = self.bot.scan_inbox()
        self.assertFalse(out["ok"])
        self.assertEqual(out["err"], self.bot.REMOTE_ASSIST_HINT)

    def test_exports_are_listed_then_read_inside_the_data_area(self):
        import base64

        def answer(operation, payload):
            if operation == "named":
                return {"ok": True, "files": [{"name": "b.json", "path": r"C:\d\out\douyin\b.json"},
                                              {"name": "a.json", "path": r"C:\d\out\douyin\a.json"}]}
            return {"base64": base64.b64encode(payload["path"].encode()).decode()}
        self.relay(answer)
        files = self.bot._fetch_exports("douyin", "作品列表导出_20261118", "unused")
        self.assertEqual([n for n, _ in files], ["a.json", "b.json"])
        self.assertEqual([c[0] for c in self.calls], ["named", "read", "read"])

    def test_capabilities_are_learned_from_info(self):
        self.set_caps([])
        self.relay(lambda op, p: {"version": "1.2.221", "program": r"C:\prog", "python": r"C:\prog\runtime\python.exe",
                                  "capabilities": ["named-ops-1"]})
        with mock.patch.object(self.bot, "_runtime_version_matches", lambda *a: True):
            self.assertTrue(self.bot.ensure_runtime())
        self.assertTrue(self.bot._client_has_named_ops())


class MtRescheduleTests(_BotBase):
    def test_named_result_unwrapped(self):
        with mock.patch.object(self.bot, "remote_op",
                               lambda *a, **k: {"ok": True, "result": {"ok": True, "confirm_token": "t"}}):
            res = self.bot._mt_reschedule_named("c1", {"desired_topics": ["#x"]}, "p", "v", "S", None)
        self.assertEqual(res, {"ok": True, "confirm_token": "t"})

    def test_old_client_falls_back(self):
        def old(*a, **k):
            raise self.bot.RemoteOpError("远程执行失败：ValueError: unsupported op: named", {})
        with mock.patch.object(self.bot, "remote_op", old):
            self.assertIsNone(self.bot._mt_reschedule_named("c1", {}, "p", "v", "S", None))

    def test_named_failure_is_reported_not_retried_as_script(self):
        def failed(*a, **k):
            raise self.bot.RemoteOpError("远程执行失败", {"ok": False, "error": "path is outside the data area"})
        with mock.patch.object(self.bot, "remote_op", failed):
            res = self.bot._mt_reschedule_named("c1", {}, "p", "v", "S", None)
        self.assertFalse(res["ok"])


class EndToEndWithRealClientTests(_BotBase):
    """Bot request -> real client ControlService.dispatch -> named_ops."""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(CLIENT / "agent"))
        from winport import remote_channel
        from winport.paths import Layout
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.layout = Layout(program=CLIENT, data=self.tmp / "data", system=self.tmp / "sys").ensure()
        (self.layout.data / "inbox").mkdir(parents=True, exist_ok=True)
        self.video = self.layout.data / "inbox" / "第一集.mp4"
        self.video.write_bytes(b"v")
        self.service = remote_channel.ControlService(self.layout, logger=None)
        home = mock.patch.dict(os.environ, {"USERPROFILE": str(self.tmp / "home")})
        home.start()
        self.addCleanup(home.stop)

        def to_client(operation, payload=None, timeout=20):
            self.calls.append((operation, payload))
            # same JSON round trip as the HTTP relay
            body = json.loads(json.dumps(payload or {}))
            return json.loads(json.dumps(self.service.dispatch(operation, body)))
        p = mock.patch.object(self.bot, "_runtime_http", to_client)
        p.start()
        self.addCleanup(p.stop)

    def test_scan_make_and_retime_round_trip(self):
        scan = self.bot.scan_inbox()
        self.assertEqual([f["name"] for f in scan["files"]], ["第一集.mp4"])
        info = self.bot._client_named_op("plan-make", self.bot._plan_make_args(
            [{"path": str(self.video)}], "#短剧"))
        self.assertTrue(info["ok"], info)
        self.assertEqual(info["title"], "第一集")
        out = self.bot._update_plan_time(info["plan"], "2026-11-20T10:00:00+08:00")
        self.assertTrue(out["ok"], out)
        self.assertEqual(json.loads(Path(info["plan"]).read_text(encoding="utf-8"))["schedule_at"],
                         "2026-11-20T10:00:00+08:00")
        self.assertEqual(self.legacy, [])

    def test_legacy_script_is_refused_by_a_new_client(self):
        with self.assertRaises(PermissionError):
            self.service.dispatch("exec", {"argv": [sys.executable, "-c", "print(1)"]})


if __name__ == "__main__":
    unittest.main()
