"""1.2.220: a logon task the installer may not change must start the CURRENT
version, never an old one and never a "cannot find script file" popup.

Platform-neutral parts are tested here; the forwarder itself is executed by
wscript/cscript on Windows in tests/test_task_forwarder_win.py.
"""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_py.py"


def _load():
    spec = importlib.util.spec_from_file_location("bootstrap_py_fwd", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Actions Context="Author">
    <Exec>
      <Command>wscript.exe</Command>
      <Arguments>//nologo "C:\\Users\\example\\桌面 新建\\manju-publish-assistant-1.2.188-win64\\scripts\\run-agent-task.vbs"</Arguments>
    </Exec>
  </Actions>
</Task>
"""


class TaskXmlTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_target_from_utf16_xml_with_chinese_path(self):
        raw = XML.encode("utf-16")                      # what schtasks /XML emits
        text = self.mod._decode_schtasks_xml(raw)
        self.assertEqual(self.mod._task_target_from_xml(text),
                         "C:\\Users\\example\\桌面 新建\\manju-publish-assistant-1.2.188-win64"
                         "\\scripts\\run-agent-task.vbs")

    def test_utf16_without_bom_and_utf8(self):
        self.assertIn("<Task", self.mod._decode_schtasks_xml(XML.encode("utf-16-le")))
        self.assertIn("<Task", self.mod._decode_schtasks_xml(XML.encode("utf-8")))

    def test_escaped_quotes_and_unquoted_path(self):
        xml = ("<Command>wscript.exe</Command><Arguments>//nologo &quot;D:\\a b\\x.vbs"
               "&quot;</Arguments>")
        self.assertEqual(self.mod._task_target_from_xml(xml), "D:\\a b\\x.vbs")
        self.assertEqual(self.mod._task_target_from_xml(
            "<Command>wscript.exe</Command><Arguments>//nologo D:\\x\\y.vbs</Arguments>"),
            "D:\\x\\y.vbs")
        self.assertIsNone(self.mod._task_target_from_xml("<Command>notepad.exe</Command>"))

    def test_forwarder_is_ascii_and_forwards_via_program_path(self):
        body = self.mod.TASK_FORWARDER_VBS
        body.encode("ascii")
        self.assertIn("program-path.txt", body)
        self.assertIn("run-agent-task.vbs", body)
        self.assertIn("//B //nologo", body)               # no dialog at logon


class ForwarderWriteTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.logs = []
        inst = object.__new__(self.mod.Installer)
        inst.log = self.logs.append
        self.inst = inst
        self.launcher = self.root / "new" / "scripts" / "run-agent-task.vbs"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text("current", encoding="ascii")

    def test_purged_old_dir_is_recreated_with_only_the_forwarder(self):
        old = self.root / "Desktop" / "manju-publish-assistant-1.2.188-win64" / "scripts" / "run-agent-task.vbs"
        self.assertTrue(self.inst._install_task_forwarder(self.launcher, target=str(old)))
        self.assertEqual(old.read_bytes(), self.mod.TASK_FORWARDER_VBS.encode("ascii"))
        pkg = old.parents[1]
        self.assertEqual(sorted(p.name for p in pkg.rglob("*")), ["run-agent-task.vbs", "scripts"])
        # no bootstrap entry / MANIFEST -> later purges never treat it as an install
        self.assertFalse((pkg / "bootstrap.bat").exists() or (pkg / "MANIFEST.json").exists())

    def test_surviving_old_supervisor_is_replaced(self):
        old = self.root / "old" / "scripts" / "run-agent-task.vbs"
        old.parent.mkdir(parents=True)
        old.write_text("' old supervisor that would start 1.2.188", encoding="ascii")
        self.assertTrue(self.inst._install_task_forwarder(self.launcher, target=str(old)))
        self.assertTrue(old.read_text(encoding="ascii").startswith(self.mod.TASK_FORWARDER_MARKER))

    def test_same_path_and_idempotent(self):
        self.assertTrue(self.inst._install_task_forwarder(self.launcher, target=str(self.launcher)))
        self.assertEqual(self.launcher.read_text(encoding="ascii"), "current")
        old = self.root / "old2" / "scripts" / "run-agent-task.vbs"
        self.inst._install_task_forwarder(self.launcher, target=str(old))
        self.assertTrue(self.inst._install_task_forwarder(self.launcher, target=str(old)))
        self.assertTrue(any("already in place" in l for l in self.logs))

    def test_unknown_target_never_raises(self):
        self.inst._supervisor_task_target = lambda: None
        self.assertFalse(self.inst._install_task_forwarder(self.launcher))


if __name__ == "__main__":
    unittest.main()
