"""1.2.221 named operations (were bot-side ``python -c`` scripts).

Each one is checked for the shape the bot parses, for staying inside the data
area / material folders, and -- where the bot used to compute something
itself -- for producing exactly the old value, so plans made before and after
the switch stay interchangeable.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

CLIENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLIENT / "agent"))

from winport import named_ops  # noqa: E402
from winport.named_ops import NamedOpError  # noqa: E402
from winport.paths import Layout  # noqa: E402
from winport.stage2_ledger import CONSUMED, RESERVED, Stage2Ledger  # noqa: E402


def bot_content_hash(acct, title, copy, sha, topics):
    """The formula PLAN_MAKER / _MT_RESCHED used in the bot until 1.2.220."""
    fp = {"schema": "publish-content/3", "account_id": acct, "title": title,
          "copy_body": copy, "sha256": sha, "topics": topics, "cover_sha256": {},
          "covers_requested": [], "platform_account_id": "", "poi": None}
    return hashlib.sha256(json.dumps(fp, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.layout = Layout(program=CLIENT, data=self.tmp / "data",
                             system=self.tmp / "sys").ensure()
        self.inbox = self.layout.data / "inbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        # keep the real desktop out of material roots
        self._home = os.environ.get("USERPROFILE")
        os.environ["USERPROFILE"] = str(self.tmp / "home")
        self.addCleanup(self._restore_home)

    def _restore_home(self):
        if self._home is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = self._home

    def video(self, name, data=b"video-bytes", folder=None):
        path = (folder or self.inbox) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


class InboxScanTests(_Base):
    def test_lists_videos_oldest_first_with_gaps(self):
        a = self.video("第一集.mp4", b"a")
        b = self.video("第二集.mov", b"bb")
        (self.inbox / "notes.txt").write_text("x", encoding="utf-8")
        os.utime(a, (1000, 1000))
        os.utime(b, (2000, 2000))
        desk = self.video("桌面素材.mp4", b"ccc",
                          folder=self.tmp / "home" / "Desktop" / named_ops.MATERIAL_FOLDER)
        out = named_ops.inbox_scan(self.layout)
        self.assertEqual([f["name"] for f in out["files"]][:2], ["第一集.mp4", "第二集.mov"])
        self.assertIn(str(desk), [f["path"] for f in out["files"]])
        self.assertEqual((out["gap_min"], out["gap_max"]), (5, 8))


class PlanMakeTests(_Base):
    def test_single_plan_matches_the_old_bot_hash(self):
        v = self.video("霸总归来第一集.mp4")
        out = named_ops.plan_make(self.layout, {"files": [str(v)], "topics": "#短剧 漫剧",
                                                "env_id": "env_2"})
        self.assertTrue(out["ok"], out)
        plan = json.loads(Path(out["plan"]).read_text(encoding="utf-8"))
        self.assertEqual(plan["piece"]["title"], "霸总归来第一集")
        self.assertEqual(plan["piece"]["topics"], ["#短剧", "#漫剧"])
        sha = hashlib.sha256(b"video-bytes").hexdigest()
        self.assertEqual(plan["compliance"]["content_hash"],
                         bot_content_hash("env_2:douyin", "霸总归来第一集", "", sha, ["#短剧", "#漫剧"]))
        self.assertTrue(_inside(out["plan"], self.layout.data))

    def test_hash_like_names_get_the_default_title(self):
        v = self.video("a" * 32 + ".mp4")
        out = named_ops.plan_make(self.layout, {"files": [str(v)], "default_title": "默认标题"})
        self.assertEqual(out["title"], "默认标题")

    def test_batch_writes_one_plan_per_item(self):
        a, b = self.video("一.mp4", b"1"), self.video("二.mp4", b"2")
        out = named_ops.plan_make(self.layout, {"files": [str(a), str(b)], "batch_id": "b-1",
                                                "item_topics": [["#a"], None], "topics": "#t"})
        self.assertTrue(out["batch"])
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["items"][0]["topics"], ["#a"])
        self.assertEqual(out["items"][1]["topics"], ["#t"])
        self.assertTrue(all(Path(i["plan"]).is_file() for i in out["items"]))

    def test_files_outside_material_folders_are_refused(self):
        stray = self.tmp / "Documents" / "x.mp4"
        stray.parent.mkdir()
        stray.write_bytes(b"x")
        out = named_ops.plan_make(self.layout, {"files": [str(stray)]})
        self.assertFalse(out["ok"])
        self.assertEqual(out["missing"], [str(stray)])

    def test_bad_env_id(self):
        v = self.video("x.mp4")
        with self.assertRaises(NamedOpError):
            named_ops.plan_make(self.layout, {"files": [str(v)], "env_id": "../x"})


class PlanEditTests(_Base):
    def plan(self, **extra):
        p = self.layout.data / "run" / "batches" / "b" / "item_01" / "plan.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        doc = {"plan_id": "bot-b-001", "account_id": "env_1:douyin", "schedule_at": "2026-11-19T09:00:00+08:00",
               "piece": {"title": "标题", "copy_body": "", "topics": ["#a"], "sha256": "ab" * 32,
                         "covers": {"vertical": None, "horizontal": None}},
               "compliance": {"content_hash": "old"}}
        doc.update(extra)
        p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return p

    def test_set_time(self):
        p = self.plan()
        out = named_ops.plan_set_time(self.layout, {"plan": str(p), "schedule_at": "2026-11-20T10:00:00+08:00"})
        self.assertEqual(out["schedule_at"], "2026-11-20T10:00:00+08:00")
        self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["schedule_at"], out["schedule_at"])
        for bad in ({"plan": str(p), "schedule_at": "tomorrow"},
                    {"plan": str(self.tmp / "x.json"), "schedule_at": "2026-11-20T10:00:00"},
                    {"plan": str(self.layout.data / "a.txt"), "schedule_at": "2026-11-20T10:00:00"}):
            with self.assertRaises((NamedOpError, ValueError)):
                named_ops.plan_set_time(self.layout, bad)

    def test_reschedule_recomputes_hash_and_runs_dry_run(self):
        p = self.plan()
        preview = p.with_name("preview.json")
        calls = []

        def runner(argv, **kw):
            calls.append((argv, kw))
            preview.write_text(json.dumps({"confirm_token": "tok", "confirm_ref": "ref",
                                           "plan_id": "bot-b-001", "schedule_at": "S"}),
                               encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout='{"confirmation": {"fresh": {"warnings": ["w"]}}}',
                                   stderr="")

        out = named_ops.plan_reschedule(self.layout, {"plan": str(p), "preview": str(preview),
                                                      "schedule_at": "2026-11-21T08:00:00+08:00",
                                                      "topics": ["#新"]}, runner=runner)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["confirm_token"], "tok")
        self.assertEqual(out["warnings"], ["w"])
        self.assertEqual(out["content_hash"],
                         bot_content_hash("env_1:douyin", "标题", "", "ab" * 32, ["#新"]))
        argv, kw = calls[0]
        self.assertEqual(argv[1:4], ["-m", "agent.winport.cli", "dry-run"])
        self.assertEqual(kw["cwd"], str(CLIENT))
        self.assertTrue(_inside(argv[argv.index("--data") + 1], self.layout.data))

    def test_reschedule_without_preview_reports(self):
        p = self.plan()
        runner = lambda argv, **kw: SimpleNamespace(returncode=3, stdout="", stderr="boom")
        out = named_ops.plan_reschedule(self.layout, {"plan": str(p), "preview": str(p.with_name("preview.json"))},
                                        runner=runner)
        self.assertFalse(out["ok"])
        self.assertEqual(out["dryrun_exit"], 3)


class LedgerAndExportTests(_Base):
    def test_unblock_removes_only_reserved(self):
        ledger = Stage2Ledger(str(self.layout.stage2_ledger))
        with ledger.locked():
            ledger.data.setdefault("entries", {}).update(
                {"k1": {"state": RESERVED}, "k2": {"state": CONSUMED}})
            ledger.save()
        out = named_ops.ledger_unblock(self.layout)
        self.assertEqual(out["removed"], {"k1": RESERVED})
        self.assertTrue(out["backup"])
        left = json.loads(Path(self.layout.stage2_ledger).read_text(encoding="utf-8"))["entries"]
        self.assertEqual(list(left), ["k2"])

    def test_export_listing(self):
        d = self.layout.data / "out" / "douyin"
        d.mkdir(parents=True, exist_ok=True)
        (d / "作品列表导出_20261118_env_1.json").write_text("{}", encoding="utf-8")
        (d / "作品列表导出_20261117_env_1.json").write_text("{}", encoding="utf-8")
        out = named_ops.export_files(self.layout, {"kind": "douyin", "prefix": "作品列表导出_20261118"})
        self.assertEqual([f["name"] for f in out["files"]], ["作品列表导出_20261118_env_1.json"])
        for bad in ({"kind": "x", "prefix": "a"}, {"kind": "douyin", "prefix": "*"},
                    {"kind": "douyin", "prefix": "../a"}):
            with self.assertRaises(NamedOpError):
                named_ops.export_files(self.layout, bad)


class CliTests(_Base):
    def test_cli_prints_one_ascii_json_line(self):
        v = self.video("中文素材.mp4")
        proc = subprocess.run([sys.executable, "-m", "agent.winport.cli", "plan-make",
                               "--data", str(self.layout.data),
                               "--args-json", json.dumps({"files": [str(v)]})],
                              cwd=str(CLIENT), capture_output=True, timeout=120,
                              env=dict(os.environ, USERPROFILE=str(self.tmp / "home")))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = proc.stdout.decode("ascii").strip().splitlines()[-1]
        self.assertEqual(json.loads(line)["title"], "中文素材")

    def test_reschedule_keeps_the_bot_marker(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            named_ops.run("plan-reschedule", self.layout, json.dumps({"plan": "/nope.json"}))
        self.assertTrue(buf.getvalue().startswith(named_ops.RESCHED_TAG))


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    unittest.main()
