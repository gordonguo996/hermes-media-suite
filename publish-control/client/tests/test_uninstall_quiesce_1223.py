"""1.2.223: uninstall must (a) remove the real autostart entries and (b) stop the
data-dir venv python before deleting the program directory.

CI run 44741303657 left `runtime/` + 21 DLL files behind: the agent runs from
publish-worker\\.venv, loads python311.dll from the program dir runtime\\DLLs via
pyvenv.cfg, and matched neither of the old filters (ExecutablePath under the
program dir, or CommandLine containing it).  Static checks only -- the real
verification is the Windows package job.
"""
from __future__ import annotations

import unittest
from pathlib import Path

CLIENT = Path(__file__).resolve().parents[1]


class UninstallSectionTests(unittest.TestCase):
    def setUp(self):
        self.nsi = (CLIENT / "installer" / "setup.nsi").read_text(encoding="utf-8-sig")
        start = self.nsi.index('Section "Uninstall"')
        self.uninstall = self.nsi[start:self.nsi.index("SectionEnd", start)]

    def test_removes_the_real_autostart_names(self):
        # install-autostart.ps1 registers task 'HuolongAgent\Agent' and Run value
        # 'HuolongAgentAgent'; the old section only knew the legacy Manju* names.
        self.assertIn(r'/TN "HuolongAgent\Agent"', self.uninstall)
        self.assertIn('"HuolongAgentAgent"', self.uninstall)
        self.assertIn("install-autostart.ps1", self.uninstall)
        self.assertIn("-Remove", self.uninstall)

    def test_stops_data_dir_processes_before_deleting(self):
        self.assertIn("uninstall-quiesce.ps1", self.uninstall)
        self.assertIn("-DataDir", self.uninstall)
        quiesce = self.uninstall.index("uninstall-quiesce.ps1")
        rm = self.uninstall.index('RMDir /r "$INSTDIR"')
        self.assertLess(quiesce, rm, "必须先把进程停掉再删目录")

    def test_rechecks_processes_between_delete_retries(self):
        retry = self.uninstall.index("uninstall_retry:")
        tail = self.uninstall[retry:]
        self.assertIn("uninstall-quiesce.ps1", tail)


class QuiesceScriptTests(unittest.TestCase):
    def setUp(self):
        self.ps1 = (CLIENT / "scripts" / "uninstall-quiesce.ps1").read_text(encoding="utf-8-sig")

    def test_matches_both_program_and_data_directory(self):
        # exe under either directory, or a command line naming either one
        self.assertIn("$exe.StartsWith($prog", self.ps1)
        self.assertIn("$exe.StartsWith($dat", self.ps1)
        self.assertIn("$cmd.IndexOf($prog", self.ps1)
        self.assertIn("$cmd.IndexOf($dat", self.ps1)

    def test_kills_supervisor_before_its_children(self):
        self.assertIn("wscript|cscript|cmd|powershell|pwsh", self.ps1)
        parents = self.ps1.index("$parents")
        rest = self.ps1.index("$rest")
        self.assertLess(parents, rest)

    def test_never_touches_unrelated_processes(self):
        # only python/wscript-style children of our own dirs: no blanket taskkill
        self.assertNotIn("taskkill", self.ps1.lower())
        self.assertNotIn("/IM", self.ps1)
        self.assertIn("$selfPid", self.ps1)

    def test_reports_survivors_instead_of_pretending(self):
        self.assertIn("exit 3", self.ps1)
        self.assertIn("still running", self.ps1)


if __name__ == "__main__":
    unittest.main()
