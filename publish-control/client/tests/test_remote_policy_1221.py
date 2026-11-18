"""1.2.221: remote operators get product functions by default, everything else
only while the customer has remote assist switched on.

Covers the policy itself and both enforcement points (local relay
``remote_channel`` and control-plane ``remote_agent``), including a real
subprocess for the allowed CLI path.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

CLIENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLIENT / "agent"))

from winport import remote_agent, remote_channel, remote_policy  # noqa: E402
from winport.paths import Layout  # noqa: E402
from winport.remote_policy import RemoteAssistRequired  # noqa: E402

PY = sys.executable


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.layout = Layout(program=CLIENT, data=self.tmp / "data",
                             system=self.tmp / "sys").ensure()
        self.outside = self.tmp / "elsewhere"
        self.outside.mkdir()

    def audit(self):
        path = self.layout.logs / remote_policy.AUDIT_NAME
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]

    def assist(self, on=True, hours=4):
        return remote_policy.set_assist(self.layout, on, hours=hours, actor="test")


class AssistStateTests(_Base):
    def test_off_by_default(self):
        self.assertFalse(remote_policy.assist_state(self.layout)["active"])

    def test_on_then_expires(self):
        now = time.time()
        remote_policy.set_assist(self.layout, True, hours=1, now=now)
        self.assertTrue(remote_policy.assist_state(self.layout, now=now + 10)["active"])
        self.assertFalse(remote_policy.assist_state(self.layout, now=now + 3601)["active"])

    def test_hours_bounds_and_types(self):
        for bad in (0, 25, "x", -1):
            with self.assertRaises(ValueError):
                remote_policy.set_assist(self.layout, True, hours=bad)
        with self.assertRaises(ValueError):
            remote_policy.set_assist(self.layout, "yes")

    def test_hand_edited_far_future_is_clamped(self):
        settings = json.loads(self.layout.settings_file.read_text(encoding="utf-8")) \
            if self.layout.settings_file.exists() else {}
        settings["remote_assist"] = {"enabled": True, "until": time.time() + 10 * 365 * 86400}
        self.layout.settings_file.write_text(json.dumps(settings), encoding="utf-8")
        state = remote_policy.assist_state(self.layout)
        self.assertTrue(state["active"])
        self.assertLessEqual(state["remaining_s"], remote_policy.MAX_ASSIST_HOURS * 3600)

    def test_turning_on_and_off_is_audited(self):
        self.assist(True)
        self.assist(False)
        ops = [e["op"] for e in self.audit()]
        self.assertEqual(ops, ["assist-on", "assist-off"])


class ExecPolicyTests(_Base):
    def test_product_cli_is_allowed_without_assist(self):
        for module in ("agent.winport.cli", "winport"):
            argv = [PY, "-m", module, "inbox-scan", "--args-json", "{}"]
            self.assertEqual(remote_policy.cli_subcommand(argv, self.layout), "inbox-scan")
            cwd = remote_policy.authorize_exec(self.layout, argv, channel="relay")
            self.assertEqual(cwd, str(self.layout.program))

    def test_everything_else_needs_assist(self):
        cases = [
            [PY, "-c", "print(1)"],
            [PY, "-m", "agent.winport.cli", "bind", "--code", "x"],          # not allow-listed
            [PY, "-m", "agent.winport.cli", "publish-key"],
            [PY, "-m", "os"],
            [str(self.outside / "python.exe"), "-m", "agent.winport.cli", "state"],  # foreign python
            [PY, "-m", "agent.winport.cli", "state", "--program", str(self.outside)],
            [PY, "-m", "agent.winport.cli", "state", "--data", str(self.outside)],
            [PY, "-m", "agent.winport.cli", "state", "--data=" + str(self.outside)],
            ["notepad.exe"],
        ]
        for argv in cases:
            with self.subTest(argv=argv[1:4]):
                with self.assertRaises(RemoteAssistRequired):
                    remote_policy.authorize_exec(self.layout, argv, channel="relay")
        with self.assertRaises(RemoteAssistRequired):
            remote_policy.authorize_exec(self.layout, None, channel="relay", shell=True)
        denied = [e for e in self.audit() if e["op"] == "exec" and not e["allowed"]]
        self.assertEqual(len(denied), len(cases) + 1)

    def test_data_flag_inside_data_area_is_fine(self):
        argv = [PY, "-m", "agent.winport.cli", "state", "--data", str(self.layout.data / "customers" / "c1")]
        self.assertEqual(remote_policy.cli_subcommand(argv, self.layout), "state")

    def test_assist_unlocks_arbitrary_exec(self):
        self.assist(True)
        self.assertIsNone(remote_policy.authorize_exec(self.layout, [PY, "-c", "1"], channel="relay"))
        self.assertTrue(self.audit()[-1]["detail"].get("assist"))


class PathPolicyTests(_Base):
    def test_reads_inside_data_and_program(self):
        f = self.layout.data / "run" / "plan.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}", encoding="utf-8")
        remote_policy.authorize_path(self.layout, f, op="read", channel="relay")
        remote_policy.authorize_path(self.layout, CLIENT / "VERSION", op="read", channel="relay")
        remote_policy.authorize_path(self.layout, self.layout.data, op="ls", channel="control")

    def test_outside_reads_writes_and_screenshots_need_assist(self):
        for path, op in ((self.outside / "a.txt", "read"), (self.outside, "ls"),
                         (self.layout.data / "x.json", "write"), (None, "shot")):
            with self.subTest(op=op):
                with self.assertRaises(RemoteAssistRequired):
                    remote_policy.authorize_path(self.layout, path, op=op, channel="relay")
        self.assist(True)
        remote_policy.authorize_path(self.layout, self.outside / "a.txt", op="read", channel="relay")
        remote_policy.authorize_path(self.layout, None, op="shot", channel="relay")


class RelayChannelTests(_Base):
    def setUp(self):
        super().setUp()
        self.service = remote_channel.ControlService(self.layout, logger=None)
        self.service.roots = (self.tmp, self.layout.program)

    def test_info_advertises_named_ops_and_assist_state(self):
        info = self.service.dispatch("info", {})
        self.assertIn("named-ops-1", info["capabilities"])
        self.assertFalse(info["remote_assist"]["active"])

    def test_python_c_is_refused_and_nothing_runs(self):
        marker = self.outside / "ran"
        body = {"argv": [PY, "-c", "open(%r,'w').write('x')" % str(marker)], "timeout": 20}
        with self.assertRaises(PermissionError):
            self.service.dispatch("exec", body)
        self.assertFalse(marker.exists())

    def test_allowed_cli_really_runs_from_the_program_dir(self):
        body = {"argv": [PY, "-m", "agent.winport.cli", "plan-set-time",
                         "--data", str(self.layout.data),
                         "--args-json", json.dumps({"plan": str(self.outside / "p.json"),
                                                    "schedule_at": "2026-11-19T10:00:00+08:00"})],
                "timeout": 60}
        result = self.service.dispatch("exec", body)
        line = json.loads(result["stdout"].strip().splitlines()[-1])
        # it ran (product code answered) and the product code refused the path
        self.assertFalse(line["ok"])
        self.assertIn("outside the data area", line["error"])

    def test_read_inside_data_but_not_home(self):
        inside = self.layout.data / "run" / "x.json"
        inside.parent.mkdir(parents=True, exist_ok=True)
        inside.write_text('{"a":1}', encoding="utf-8")
        got = self.service.dispatch("read", {"path": str(inside)})
        self.assertEqual(base64.b64decode(got["base64"]), b'{"a":1}')
        other = self.outside / "secret.txt"
        other.write_text("s", encoding="utf-8")
        with self.assertRaises(PermissionError):
            self.service.dispatch("read", {"path": str(other)})

    def test_write_and_shot_need_assist(self):
        target = self.layout.data / "w.json"
        with self.assertRaises(PermissionError):
            self.service.dispatch("write", {"path": str(target), "base64": base64.b64encode(b"{}").decode()})
        self.assertFalse(target.exists())
        with self.assertRaises(PermissionError):
            self.service.dispatch("shot", {})
        self.assist(True)
        self.service.dispatch("write", {"path": str(target), "base64": base64.b64encode(b"{}").decode()})
        self.assertEqual(target.read_bytes(), b"{}")


class ControlPlaneAgentTests(_Base):
    def config(self):
        return SimpleNamespace(layout=self.layout)

    def test_shell_string_and_foreign_argv_refused(self):
        for body in ({"cmd": "echo hi"}, {"cmd": [PY, "-c", "print(1)"]}):
            with self.assertRaises(PermissionError):
                remote_agent.execute("exec", body, config=self.config())

    def test_allowed_cli_runs(self):
        body = {"cmd": [PY, "-m", "agent.winport.cli", "inbox-scan",
                        "--data", str(self.layout.data)], "timeout": 60}
        result = remote_agent.execute("exec", body, config=self.config())
        self.assertTrue(json.loads(result["stdout"].strip().splitlines()[-1])["ok"])

    def test_paths(self):
        inside = self.layout.data / "plans" / "p.json"
        inside.parent.mkdir(parents=True, exist_ok=True)
        inside.write_text("{}", encoding="utf-8")
        self.assertTrue(remote_agent.execute("read", {"path": str(inside)}, config=self.config())["ok"])
        self.assertTrue(remote_agent.execute("ls", {"path": str(inside.parent)}, config=self.config())["ok"])
        for op, body in (("read", {"path": str(self.outside / "x")}),
                         ("ls", {"path": str(self.outside)}),
                         ("write", {"path": str(inside), "base64": ""})):
            with self.assertRaises(PermissionError):
                remote_agent.execute(op, body, config=self.config())

    def test_no_config_fails_closed(self):
        with self.assertRaises(PermissionError):
            remote_agent.execute("exec", {"cmd": "echo hi"}, config=None)


if __name__ == "__main__":
    unittest.main()
