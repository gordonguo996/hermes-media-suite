"""Windows-only: the logon-task forwarder against real schtasks and wscript.

Uses its own task name (never the product's) and a private %LOCALAPPDATA%, so
it is safe on a developer's or customer's machine.  Skipped elsewhere.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_py.py"
MARKER_VBS = (
    'Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
    'Set f = fso.CreateTextFile(fso.GetParentFolderName(WScript.ScriptFullName) '
    '& "\\ran.txt", True)\r\n'
    'f.Write "current"\r\n'
    "f.Close\r\n"
)


def _load():
    spec = importlib.util.spec_from_file_location("bootstrap_py_fwd_win", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(os.name == "nt", "Windows only")
class ForwarderOnWindowsTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        tmp = tempfile.TemporaryDirectory(prefix="fwd 测试 ")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.logs = []
        inst = object.__new__(self.mod.Installer)
        inst.log = self.logs.append
        self.inst = inst

    def test_reads_target_of_a_real_task(self):
        name = "ManjuFwdTest-" + uuid.uuid4().hex[:8]
        old = self.root / "旧版 1.2.188" / "scripts" / "run-agent-task.vbs"
        tr = 'wscript.exe //nologo "%s"' % old
        made = subprocess.run(["schtasks", "/Create", "/TN", name, "/TR", tr, "/SC", "ONCE",
                               "/ST", "23:59", "/F"], capture_output=True, timeout=60)
        if made.returncode != 0:
            self.skipTest("cannot create a test task here: %r" % made.stderr[:200])
        self.addCleanup(subprocess.run, ["schtasks", "/Delete", "/TN", name, "/F"],
                        capture_output=True, timeout=60)
        self.mod.SUPERVISOR_TASK_NAME = name
        self.assertEqual(os.path.normcase(self.inst._supervisor_task_target()),
                         os.path.normcase(str(old)))

    def test_forwarder_starts_the_current_program(self):
        program = self.root / "当前 版本" / "manju-publish-assistant-1.2.220-win64"
        current = program / "scripts" / "run-agent-task.vbs"
        current.parent.mkdir(parents=True)
        current.write_bytes(MARKER_VBS.encode("ascii"))
        local = self.root / "LocalAppData"
        run = local / "HuolongAgent" / "publish-worker" / "run"
        run.mkdir(parents=True)
        (run / "program-path.txt").write_bytes(str(program).encode("utf-8"))
        old = self.root / "Desktop" / "manju-publish-assistant-1.2.188-win64" / "scripts" / "run-agent-task.vbs"
        self.assertTrue(self.inst._install_task_forwarder(current, target=str(old)))
        env = dict(os.environ, LOCALAPPDATA=str(local))
        proc = subprocess.run(["cscript.exe", "//B", "//nologo", str(old)], env=env,
                              capture_output=True, timeout=60)
        ran = program / "scripts" / "ran.txt"
        for _ in range(50):
            if ran.exists():
                break
            time.sleep(0.1)  # wait-ok: poll
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(ran.read_text(encoding="ascii"), "current")

    def test_forwarder_exits_quietly_without_program_path(self):
        local = self.root / "EmptyLocal"
        local.mkdir()
        old = self.root / "o" / "scripts" / "run-agent-task.vbs"
        self.inst._install_task_forwarder(self.root / "x.vbs", target=str(old))
        proc = subprocess.run(["cscript.exe", "//B", "//nologo", str(old)],
                              env=dict(os.environ, LOCALAPPDATA=str(local)),
                              capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 3)


if __name__ == "__main__":
    unittest.main()
