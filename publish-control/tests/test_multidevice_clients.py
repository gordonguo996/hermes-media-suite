from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot_agent_modules():
    """These classes stub ``agent`` / ``agent.winport`` in sys.modules.  Left in
    place they shadow the real client package for every later test file (a
    real ``winport.cli`` then resolves ``from .logutil`` against the stub)."""
    return {k: v for k, v in sys.modules.items() if k == "agent" or k.startswith("agent.")}


def _restore_agent_modules(saved):
    for k in [k for k in sys.modules if k == "agent" or k.startswith("agent.")]:
        if k not in saved:
            del sys.modules[k]
    sys.modules.update(saved)


class BotDeviceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "feishu_publish_bot"))
        cls.bot = _load("md_bot", ROOT / "feishu_publish_bot/bot.py")

    def test_enqueue_keeps_customer_from_chat_and_adds_device(self):
        with mock.patch.object(
                self.bot, "control_request",
                return_value=(200, {"command": {"command_id": "c1"}})) as request:
            self.bot.enqueue_for_chat(
                "oc_group", "publish", {"op": "create", "device_id": "d-a"},
                command_id="c1")
        body = request.call_args.args[2]
        self.assertNotIn("customer_id", body)
        self.assertEqual(body["chat_id"], "oc_group")
        self.assertEqual(body["device_id"], "d-a")

    def test_device_select_is_scoped_to_customer(self):
        with mock.patch.object(
                self.bot, "control_request",
                return_value=(200, {"device": {"device_id": "d-b", "online": True}})) as request:
            device = self.bot.select_customer_device("c-one")
        self.assertEqual(device["device_id"], "d-b")
        path = request.call_args.args[1]
        self.assertTrue(path.startswith("/api/customers/c-one/devices/select?"))

    def test_material_snapshot_marks_device_and_rejects_mismatch(self):
        customer = {"customer_id": "c-one", "device_binding_enabled": 1}
        device = {"device_id": "d-a", "label": "A机", "online": True}
        snapshot = {
            "ok": True, "schema": "publish-materials/1",
            "customer_id": "c-one", "device_id": "d-a", "materials": [{
                "name": "a.mp4", "path": "C:/a.mp4", "size": 3,
                "sha256": "a" * 64, "material_id": "m1"}]}
        with mock.patch.object(self.bot, "lookup_customer", return_value=customer), \
             mock.patch.object(self.bot, "select_customer_device", return_value=device), \
             mock.patch.object(self.bot, "_cp_remote", return_value=snapshot):
            got_device, rows, err = self.bot._mt_material_snapshot("oc")
        self.assertIsNone(err)
        self.assertEqual(got_device["device_id"], "d-a")
        self.assertEqual(rows[0]["device_id"], "d-a")
        self.assertEqual(rows[0]["sha256"], "a" * 64)

        snapshot["device_id"] = "d-b"
        with mock.patch.object(self.bot, "lookup_customer", return_value=customer), \
             mock.patch.object(self.bot, "select_customer_device", return_value=device), \
             mock.patch.object(self.bot, "_cp_remote", return_value=snapshot):
            _, rows, err = self.bot._mt_material_snapshot("oc")
        self.assertIsNone(rows)
        self.assertIn("不一致", err)

    def test_new_list_falls_back_from_an_offline_session_device(self):
        customer = {"customer_id": "c-one", "device_binding_enabled": 1,
                    "active_device_id": "d-a"}
        device_b = {"device_id": "d-b", "label": "B机", "online": True}
        snapshot = {
            "ok": True, "schema": "publish-materials/1",
            "customer_id": "c-one", "device_id": "d-b", "materials": []}

        def select(_cid, preferred=None, require_online=True):
            if preferred:
                raise self.bot.ControlPlaneError("requested device is offline")
            return device_b

        with mock.patch.object(self.bot, "lookup_customer", return_value=customer), \
             mock.patch.object(self.bot, "select_customer_device",
                               side_effect=select) as select_mock, \
             mock.patch.object(self.bot, "_cp_remote", return_value=snapshot):
            device, rows, err = self.bot._mt_material_snapshot(
                "oc", preferred_device="d-a")
        self.assertIsNone(err)
        self.assertEqual(device["device_id"], "d-b")
        self.assertEqual(select_mock.call_count, 2)
        self.assertIsNone(select_mock.call_args.kwargs["preferred"])

    def test_legacy_snapshot_is_untargeted_and_labeled(self):
        customer = {"customer_id": "c-one", "device_binding_enabled": 0}
        snapshot = {
            "ok": True, "schema": "publish-materials/1",
            "customer_id": "c-one", "device_id": None, "materials": [{
                "name": "a.mp4", "path": "C:/a.mp4", "size": 3,
                "sha256": "a" * 64, "material_id": "m1"}]}
        with mock.patch.object(self.bot, "lookup_customer", return_value=customer), \
             mock.patch.object(self.bot, "list_customer_devices", return_value=[]), \
             mock.patch.object(self.bot, "_cp_remote", return_value=snapshot) as remote:
            device, rows, err = self.bot._mt_material_snapshot("oc")
        self.assertIsNone(err)
        self.assertTrue(device["legacy"])
        self.assertIsNone(device["device_id"])
        self.assertIsNone(rows[0]["device_id"])
        self.assertNotIn("device_id", remote.call_args.args[2])

        snapshot["device_id"] = "d-someone"
        with mock.patch.object(self.bot, "lookup_customer", return_value=customer), \
             mock.patch.object(self.bot, "list_customer_devices", return_value=[]), \
             mock.patch.object(self.bot, "_cp_remote", return_value=snapshot):
            _, rows, err = self.bot._mt_material_snapshot("oc")
        self.assertIsNone(rows)
        self.assertIn("未登记设备", err)

    def test_staged_single_device_must_report_its_identity(self):
        customer = {"customer_id": "c-one", "device_binding_enabled": 0,
                    "active_device_id": "d-a"}
        snapshot = {
            "ok": True, "schema": "publish-materials/1",
            "customer_id": "c-one", "device_id": "d-a", "materials": [{
                "name": "a.mp4", "path": "C:/a.mp4", "size": 3,
                "sha256": "a" * 64, "material_id": "m1"}]}
        devices = [{"device_id": "d-a", "status": "active", "label": "A机"}]
        with mock.patch.object(self.bot, "lookup_customer", return_value=customer), \
             mock.patch.object(self.bot, "list_customer_devices", return_value=devices), \
             mock.patch.object(self.bot, "_cp_remote", return_value=snapshot):
            device, rows, err = self.bot._mt_material_snapshot("oc")
        self.assertIsNone(err)
        self.assertTrue(device["legacy"])
        self.assertEqual(device["device_id"], "d-a")
        self.assertEqual(rows[0]["device_id"], "d-a")

        snapshot["device_id"] = None
        with mock.patch.object(self.bot, "lookup_customer", return_value=customer), \
             mock.patch.object(self.bot, "list_customer_devices", return_value=devices), \
             mock.patch.object(self.bot, "_cp_remote", return_value=snapshot):
            _, rows, err = self.bot._mt_material_snapshot("oc")
        self.assertIsNone(rows)
        self.assertIn("未带设备身份", err)

    def test_two_registered_devices_require_binding(self):
        customer = {"customer_id": "c-one", "device_binding_enabled": 0,
                    "active_device_id": "d-a"}
        snapshot = {
            "ok": True, "schema": "publish-materials/1",
            "customer_id": "c-one", "device_id": "d-a", "materials": []}
        devices = [{"device_id": "d-a", "status": "active"},
                   {"device_id": "d-b", "status": "active"}]
        with mock.patch.object(self.bot, "lookup_customer", return_value=customer), \
             mock.patch.object(self.bot, "list_customer_devices", return_value=devices), \
             mock.patch.object(self.bot, "_cp_remote", return_value=snapshot):
            _, rows, err = self.bot._mt_material_snapshot("oc")
        self.assertIsNone(rows)
        self.assertIn("启用设备绑定", err)

    def test_prepared_plan_sha_must_match_snapshot(self):
        d = {
            "chat_id": "oc", "customer_id": "c-one", "device_id": "d-a",
            "status": "preparing", "topics_answered": True, "topics": [],
            "topics_by_name": {}, "prepare_command_id": "p1",
            "batch_id": "b1", "confirmed_title": "title", "max_length": 30,
            "revision": 1, "draft_started_at": time.time(),
            "selected": [{"name": "a.mp4", "sha256": "a" * 64,
                          "device_id": "d-a", "material_id": "m1",
                          "snapshot_id": "s1"}],
            "snapshot": {"snapshot_id": "s1", "device_id": "d-a",
                         "materials": [{"material_id": "m1"}]},
        }
        command = {
            "customer_id": "c-one", "command_id": "p1",
            "result": {"batch_id": "b1", "device_id": "d-a", "items": [{
                "name": "a.mp4", "title": "title",
                "plan_data": {"piece": {"sha256": "b" * 64}}}]},
        }
        replies = []
        with mock.patch.object(self.bot, "_dialog_load", return_value=d), \
             mock.patch.object(self.bot, "lookup_customer",
                               return_value={"customer_id": "c-one"}), \
             mock.patch.object(self.bot, "_dialog_reply",
                               side_effect=lambda _d, text: replies.append(text) or True), \
             mock.patch.object(self.bot, "store_mt_prepared") as store:
            self.bot._mt_accept_prepared("oc", command)
        self.assertFalse(store.called)
        self.assertTrue(replies and "发生变化" in replies[-1])

    def test_item_sha_prefers_full_plan_hash(self):
        full = "f" * 64
        item = {"sha256": "f" * 12,
                "plan_data": {"piece": {"sha256": full}}}
        self.assertEqual(self.bot._item_sha(item), full)
        self.assertEqual(
            self.bot._item_sha({"sha256": "0123456789ab"}), "0123456789ab")

    def _prepare_dialog(self, name, sha, device_id):
        return {
            "chat_id": "oc", "customer_id": "c-one", "device_id": device_id,
            "topics_answered": True, "topics": [], "item_topics": {},
            "confirmed_title": "title", "max_length": 30, "revision": 2,
            "selected": [{
                "name": name, "path": "C:/" + name, "sha256": sha,
                "material_id": "m1", "device_id": device_id,
                "snapshot_id": "s1", "display_index": 1}],
            "snapshot": {
                "snapshot_id": "s1", "customer_id": "c-one",
                "device_id": device_id, "expires_at": time.time() + 60,
                "materials": [{"material_id": "m1", "device_id": device_id}]},
        }

    def test_same_filename_on_two_devices_is_not_a_material_duplicate(self):
        d = self._prepare_dialog("same.mp4", "a" * 64, "d-a")
        other = {
            "chat_id": "oc", "customer_id": "c-one", "device_id": "d-b",
            "items": [{"name": "same.mp4", "status": "pending",
                       "device_id": "d-b", "sha256": "b" * 64}],
        }
        with mock.patch.object(self.bot, "_mt_chat_batches", return_value=[other]), \
             mock.patch.object(self.bot, "_dialog_save"), \
             mock.patch.object(self.bot, "_dialog_reply", return_value=True), \
             mock.patch.object(self.bot, "enqueue_for_chat",
                               return_value={"command": {"command_id": "x"}}) as enqueue:
            self.bot._mt_prepare_draft(d)
        self.assertTrue(enqueue.called)
        self.assertEqual(enqueue.call_args.args[2]["device_id"], "d-a")

    def test_same_sha_on_another_device_blocks_prepare(self):
        d = self._prepare_dialog("renamed.mp4", "a" * 64, "d-a")
        other = {
            "chat_id": "oc", "customer_id": "c-one", "device_id": "d-b",
            "items": [{"name": "original.mp4", "status": "pending",
                       "device_id": "d-b",
                       "plan_data": {"piece": {"sha256": "a" * 64}}}],
        }
        replies = []
        with mock.patch.object(self.bot, "_mt_chat_batches", return_value=[other]), \
             mock.patch.object(self.bot, "_dialog_save"), \
             mock.patch.object(self.bot, "_dialog_reply",
                               side_effect=lambda _d, text: replies.append(text) or True), \
             mock.patch.object(self.bot, "enqueue_for_chat") as enqueue:
            self.bot._mt_prepare_draft(d)
        self.assertFalse(enqueue.called)
        self.assertTrue(replies and "跨机" in replies[-1])

    def test_same_device_same_name_blocks_even_with_different_sha(self):
        d = self._prepare_dialog("same.mp4", "a" * 64, "d-a")
        other = {
            "chat_id": "oc", "customer_id": "c-one", "device_id": "d-a",
            "items": [{"name": "same.mp4", "status": "scheduled",
                       "device_id": "d-a", "sha256": "b" * 64}],
        }
        replies = []
        with mock.patch.object(self.bot, "_mt_chat_batches", return_value=[other]), \
             mock.patch.object(self.bot, "_dialog_save"), \
             mock.patch.object(self.bot, "_dialog_reply",
                               side_effect=lambda _d, text: replies.append(text) or True), \
             mock.patch.object(self.bot, "enqueue_for_chat") as enqueue:
            self.bot._mt_prepare_draft(d)
        self.assertFalse(enqueue.called)
        self.assertTrue(replies and "已有待发" in replies[-1])

    def test_legacy_prepare_does_not_send_a_device_identity(self):
        d = self._prepare_dialog("a.mp4", "a" * 64, None)
        d["snapshot"]["legacy"] = True
        d["snapshot"]["materials"][0]["device_id"] = None
        d["selected"][0]["device_id"] = None
        with mock.patch.object(self.bot, "_mt_chat_batches", return_value=[]), \
             mock.patch.object(self.bot, "_dialog_save"), \
             mock.patch.object(self.bot, "_dialog_reply", return_value=True), \
             mock.patch.object(self.bot, "enqueue_for_chat",
                               return_value={"command": {"command_id": "x"}}) as enqueue:
            self.bot._mt_prepare_draft(d)
        payload = enqueue.call_args.args[2]
        self.assertNotIn("device_id", payload)
        self.assertNotIn("target_device_id", payload)
        self.assertIsNone(enqueue.call_args.kwargs["device_id"])

    def test_prepare_payload_is_bound_to_selected_device_and_material(self):
        sha = "c" * 64
        d = {
            "chat_id": "oc", "customer_id": "c-one", "device_id": "d-a",
            "topics_answered": True, "topics": [], "item_topics": {},
            "confirmed_title": "title", "max_length": 30, "revision": 2,
            "selected": [{
                "name": "a.mp4", "path": "C:/a.mp4", "sha256": sha,
                "material_id": "m1", "device_id": "d-a",
                "snapshot_id": "s1", "display_index": 1}],
            "snapshot": {
                "snapshot_id": "s1", "customer_id": "c-one",
                "device_id": "d-a", "expires_at": time.time() + 60,
                "materials": [{"material_id": "m1", "device_id": "d-a"}]},
        }
        with mock.patch.object(self.bot, "_mt_chat_batches", return_value=[]), \
             mock.patch.object(self.bot, "_dialog_save"), \
             mock.patch.object(self.bot, "_dialog_reply", return_value=True), \
             mock.patch.object(self.bot, "enqueue_for_chat",
                               return_value={"command": {"command_id": "x"}}) as enqueue:
            self.bot._mt_prepare_draft(d)
        payload = enqueue.call_args.args[2]
        self.assertEqual(payload["device_id"], "d-a")
        self.assertEqual(payload["target_device_id"], "d-a")
        self.assertEqual(payload["material_sha256s"], [sha])
        self.assertEqual(enqueue.call_args.kwargs["device_id"], "d-a")


    def _dispatch(self, chat_id, state, item):
        command_id = self.bot._mt_command_id(state, item)
        receipt = {"receipt_id": "r", "plan_id": "pl", "content_hash": "c"}

        def issue(*_args, **kwargs):
            value = dict(receipt)
            if kwargs.get("device_id"):
                value["device_id"] = kwargs["device_id"]
            if kwargs.get("binding_epoch") is not None:
                value["device_binding_epoch"] = kwargs["binding_epoch"]
            return value

        with mock.patch.object(self.bot, "load_mt_batch", return_value=state), \
             mock.patch.object(self.bot, "save_mt_batch"), \
             mock.patch.object(self.bot, "_mt_validate_dispatch_binding"), \
             mock.patch.object(self.bot, "_mt_validate_receipt"), \
             mock.patch.object(self.bot.receipt_issue, "issue_receipt",
                               side_effect=issue) as issue_mock, \
             mock.patch.object(self.bot, "enqueue_for_chat",
                               return_value={"command": {"command_id": command_id}}) as enqueue:
            self.bot._dispatch_mt_item(chat_id, state, item)
        enqueue.call_args.issue_mock = issue_mock
        return enqueue.call_args

    def _dispatch_item(self):
        return {
            "index": 1, "status": "scheduled", "plan_id": "pl",
            "schedule_at": "2026-10-29T07:10:00+08:00",
            "content_hash": "c", "confirm_token": "t",
            "plan_path": "plan.json", "preview_path": "preview.json",
            "plan_data": {"piece": {}},
        }

    def test_bound_dispatch_carries_device_identity(self):
        item = self._dispatch_item()
        item.update(device_id="d-a", binding_epoch=4)
        call = self._dispatch("oc", {"batch_id": "b", "device_id": "d-a",
                                     "binding_epoch": 4, "items": []}, item)
        payload = call.args[2]
        self.assertEqual(payload["device_id"], "d-a")
        self.assertEqual(payload["target_device_id"], "d-a")
        self.assertEqual(payload["receipt"]["device_id"], "d-a")
        self.assertEqual(payload["receipt"]["device_binding_epoch"], 4)
        self.assertEqual(call.kwargs["device_id"], "d-a")

    def test_legacy_dispatch_omits_device_identity(self):
        item = self._dispatch_item()
        call = self._dispatch("oc", {"batch_id": "b", "device_id": None,
                                     "legacy": True, "items": []}, item)
        payload = call.args[2]
        self.assertNotIn("device_id", payload)
        self.assertNotIn("target_device_id", payload)
        self.assertNotIn("device_id", payload["receipt"])
        self.assertIsNone(call.kwargs["device_id"])


    def test_ledger_evidence_requires_matching_device_receipt(self):
        command = {
            "customer_id": "c-one", "device_id": "d-a",
            "payload": {
                "plan_data": {"compliance": {"content_hash": "a" * 64}},
                "receipt": {"receipt_id": "RCP-1"}}}
        entry = {
            "customer_id": "c-one", "content_hash": "a" * 64,
            "receipt_id": "RCP-1", "state": "CONSUMED",
            "publish_evidence": {
                "ok": True, "signal": "create_v2",
                "receipt": {"device_id": "d-b"}}}
        ledger = {"entries": {"k": entry}}
        self.assertIsNone(self.bot._mt_match_evidence(
            command, ledger, "c-one"))
        entry["publish_evidence"]["receipt"]["device_id"] = "d-a"
        match = self.bot._mt_match_evidence(command, ledger, "c-one")
        self.assertEqual(match["receipt_id"], "RCP-1")


    def test_legacy_confirm_batch_blocked_when_binding_enabled(self):
        state = {"chat_id": "oc", "status": "pending", "items": [],
                 "batch_id": "b1"}
        with mock.patch.object(self.bot, "load_batch", return_value=state), \
             mock.patch.object(
                 self.bot, "lookup_customer",
                 return_value={"customer_id": "c-one",
                               "device_binding_enabled": 1}), \
             mock.patch.object(self.bot, "reply") as reply, \
             mock.patch.object(self.bot.receipt_issue,
                               "issue_receipt") as issue:
            self.bot.do_confirm_batch("oc", "b1")
        self.assertIn("多设备绑定", reply.call_args.args[1])
        issue.assert_not_called()

    def test_legacy_confirm_fails_closed_when_binding_lookup_fails(self):
        state = {"chat_id": "oc", "status": "pending", "items": [],
                 "batch_id": "b1"}
        with mock.patch.object(self.bot, "load_batch", return_value=state), \
             mock.patch.object(self.bot, "lookup_customer",
                               side_effect=RuntimeError("control down")), \
             mock.patch.object(self.bot, "reply") as reply, \
             mock.patch.object(self.bot.receipt_issue,
                               "issue_receipt") as issue:
            self.bot.do_confirm_batch("oc", "b1")
        self.assertTrue(reply.called)
        issue.assert_not_called()

    def test_mt_republish_carries_device_identity(self):
        candidate = {
            "customer_id": "c-one", "plan": "plan.json",
            "preview": "preview.json", "token": "t", "title": "title",
            "name": "a.mp4",
            "plan_data": {"plan_id": "p1", "device_id": "d-a",
                          "binding_epoch": 3},
        }
        with mock.patch.object(
                self.bot, "lookup_customer",
                return_value={"customer_id": "c-one",
                              "device_binding_enabled": 1}), \
             mock.patch.object(self.bot.receipt_issue, "issue_receipt",
                               return_value={"receipt_id": "r"}) as issue, \
             mock.patch.object(self.bot, "enqueue_for_chat",
                               return_value={}) as enqueue, \
             mock.patch.object(self.bot, "clear_republish_candidate"), \
             mock.patch.object(self.bot, "reply"):
            self.bot._do_republish_mt("oc", candidate)
        self.assertEqual(issue.call_args.kwargs["device_id"], "d-a")
        self.assertEqual(issue.call_args.kwargs["binding_epoch"], 3)
        payload = enqueue.call_args.args[2]
        self.assertEqual(payload["device_id"], "d-a")
        self.assertEqual(payload["target_device_id"], "d-a")
        self.assertEqual(enqueue.call_args.kwargs["device_id"], "d-a")

    def test_mt_republish_requires_device_for_bound_customer(self):
        candidate = {
            "customer_id": "c-one", "plan": "plan.json",
            "plan_data": {"plan_id": "p1"},
        }
        with mock.patch.object(
                self.bot, "lookup_customer",
                return_value={"customer_id": "c-one",
                              "device_binding_enabled": 1}), \
             mock.patch.object(self.bot.receipt_issue,
                               "issue_receipt") as issue, \
             mock.patch.object(self.bot, "enqueue_for_chat") as enqueue, \
             mock.patch.object(self.bot, "clear_republish_candidate") as clear, \
             mock.patch.object(self.bot, "reply") as reply:
            self.bot._do_republish_mt("oc", candidate)
        self.assertIn("缺少目标机器", reply.call_args.args[1])
        issue.assert_not_called()
        enqueue.assert_not_called()
        clear.assert_called_once_with("oc")

    def test_legacy_local_republish_blocked_when_binding_enabled(self):
        candidate = {"customer_id": "c-one", "mode": "local",
                     "plan": "plan.json"}
        with mock.patch.object(self.bot, "get_republish_candidate",
                               return_value=(candidate, False)), \
             mock.patch.object(
                 self.bot, "lookup_customer",
                 return_value={"customer_id": "c-one",
                               "device_binding_enabled": 1}), \
             mock.patch.object(self.bot, "reply") as reply, \
             mock.patch.object(self.bot, "clear_republish_candidate") as clear, \
             mock.patch.object(self.bot.receipt_issue,
                               "issue_receipt") as issue:
            self.bot.do_republish("oc")
        self.assertIn("多设备绑定", reply.call_args.args[1])
        issue.assert_not_called()
        clear.assert_called_once_with("oc")

class ReceiptDeviceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "feishu_publish_bot"))
        cls.bot = _load("md_bot_receipt", ROOT / "feishu_publish_bot/bot.py")
        cls.receipts = cls.bot.receipt_issue

    def test_device_identity_is_inside_the_signed_body(self):
        seed = bytes.fromhex("11" * 32)
        plan = {"plan_id": "pl-1",
                "compliance": {"content_hash": "c" * 64}}
        with mock.patch.object(self.receipts, "_load_seed", return_value=seed):
            receipt = self.receipts.issue_receipt(
                plan, "approver", device_id="d-a", binding_epoch=7)
        self.assertEqual(receipt["device_id"], "d-a")
        self.assertEqual(receipt["device_binding_epoch"], 7)
        expected = self.receipts._sign(
            self.receipts._canonical(receipt), seed).hex()
        self.assertEqual(receipt["signature"], expected)

    def test_legacy_receipt_has_no_device_fields(self):
        seed = bytes.fromhex("22" * 32)
        plan = {"plan_id": "pl-2",
                "compliance": {"content_hash": "d" * 64}}
        with mock.patch.object(self.receipts, "_load_seed", return_value=seed):
            receipt = self.receipts.issue_receipt(plan, "approver")
        self.assertNotIn("device_id", receipt)
        self.assertNotIn("device_binding_epoch", receipt)

    def test_unsafe_device_id_is_rejected(self):
        plan = {"plan_id": "pl-3",
                "compliance": {"content_hash": "e" * 64}}
        with self.assertRaises(ValueError):
            self.receipts.issue_receipt(
                plan, "approver", device_id="bad device id")


class AgentProtocolTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        _restore_agent_modules(cls._saved_agent_modules)

    @classmethod
    def setUpClass(cls):
        cls._saved_agent_modules = _snapshot_agent_modules()
        for name in ("agent", "agent.winport"):
            module = types.ModuleType(name)
            module.__path__ = []
            sys.modules[name] = module

        customer_paths = types.ModuleType("agent.winport.customer_paths")
        customer_paths.DEFAULT_CUSTOMER_ID = "c_default"
        customer_paths.normalize_customer_id = lambda value: str(value).strip().lower()
        customer_paths.default_data_root = lambda: Path(tempfile.gettempdir())

        class CustomerLayout:
            def __init__(self, root, customer_id, scoped=True, system_root=None):
                self.root = Path(root)
                self.customer_id = customer_id
                self.run = self.root / "run"
                self.inbox = self.root / "inbox"
                self.system = Path(system_root) if system_root else self.root / "system"

            def ensure(self):
                self.root.mkdir(parents=True, exist_ok=True)
                return self

            def validate(self):
                return True

        customer_paths.CustomerLayout = CustomerLayout
        customer_paths.customer_root = lambda value: Path(tempfile.gettempdir()) / value
        secure = types.ModuleType("agent.winport.secure_io")
        secure.assert_no_reparse_chain = lambda *args, **kwargs: None
        secure.is_within = lambda *args, **kwargs: True
        subprocess_text = types.ModuleType("agent.winport.subprocess_text")
        subprocess_text.hidden_window_options = lambda argv: {}
        for module in (customer_paths, secure, subprocess_text):
            sys.modules[module.__name__] = module

        cls.agentpoll = _load(
            "agent.winport.agentpoll", ROOT / "client_patch/agentpoll.py")
        cls.remote_agent = _load(
            "agent.winport.remote_agent", ROOT / "client_patch/remote_agent.py")

    def test_config_reads_device_identity_and_sends_it(self):
        tmp = tempfile.mkdtemp(prefix="agent-md-")
        env = {
            "CUSTOMER_ID": "c-one", "DEVICE_TOKEN": "device-secret",
            "DEVICE_ID": "d-a", "DEVICE_BINDING_EPOCH": "3",
            "CONTROL_URL": "http://127.0.0.1:8899",
        }
        config = self.agentpoll.AgentConfig.from_env(env=env, data_root=tmp)
        self.assertEqual(config.token, "device-secret")
        self.assertEqual(config.device_id, "d-a")
        self.assertEqual(config.binding_epoch, 3)
        self.assertEqual(self.agentpoll._identity(config),
                         {"device_id": "d-a", "binding_epoch": 3})
        self.assertEqual(self.remote_agent._identity(config),
                         {"device_id": "d-a", "binding_epoch": 3})

        captured = {}

        def fake_http(config_arg, method, path, payload=None, timeout=None):
            captured.update(payload or {})
            return 200, {"command": None}

        with mock.patch.object(self.agentpoll, "_http_json", side_effect=fake_http):
            self.agentpoll.poll_once(config)
        self.assertEqual(captured["device_id"], "d-a")
        self.assertEqual(captured["binding_epoch"], 3)


class RemoteOpsDeviceTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        _restore_agent_modules(cls._saved_agent_modules)

    @classmethod
    def setUpClass(cls):
        cls._saved_agent_modules = _snapshot_agent_modules()
        # Install fresh agent.winport stubs (never mutate a real package that an
        # earlier-collected test imported -- setting __path__ = [] on it broke
        # every later relative import), then add the remaining winport modules.
        saved = getattr(AgentProtocolTests, "_saved_agent_modules", None)
        AgentProtocolTests.setUpClass()
        AgentProtocolTests._saved_agent_modules = saved
        idem = types.ModuleType("agent.winport.idempotency")
        idem.material_fingerprint = lambda piece, account_id=None: "hash"
        jsonio = types.ModuleType("agent.winport.jsonio")
        jsonio.write_json = lambda path, value: Path(path).write_text(
            json.dumps(value), encoding="utf-8")
        secure = sys.modules["agent.winport.secure_io"]
        secure.sha256_file = lambda path: ("0" * 64, Path(path).stat().st_size)
        secure.secure_stat = lambda path, root=None: Path(path).stat()
        secure.secure_open = lambda path, root=None: open(path, "rb")
        secure.copy_hashed_file = lambda *args, **kwargs: ("0" * 64, 0)
        for module in (idem, jsonio):
            sys.modules[module.__name__] = module
        cls.remote_ops = _load(
            "agent.winport.remote_ops", ROOT / "client_patch/remote_ops.py")

    def test_confirm_rejects_plan_from_another_device(self):
        root = Path(tempfile.mkdtemp(prefix="remote-ops-md-"))
        plan = root / "plan.json"
        plan.write_text(json.dumps({"device_id": "d-other"}), encoding="utf-8")
        preview = root / "preview.json"
        preview.write_text("{}", encoding="utf-8")

        class Layout:
            def ensure(self):
                return self

        class Config:
            customer_id = "c-one"
            device_id = "d-a"
            binding_epoch = 1
            token = "t"
            cli_python = "python"
            cli_root = None
            program_dir = None
            command_timeout = 10
            layout = Layout()

        Config.layout.root = root
        result = self.remote_ops.confirm(Config(), {
            "receipt": {"receipt_id": "r"}, "plan": str(plan),
            "preview": str(preview), "confirm_token": "x"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "plan_device_mismatch")


if __name__ == "__main__":
    unittest.main()
