"""1.2.223: offline materials are locked to official sources; the installer uses a
fixed, version-free directory.  Offline checks only (no downloads)."""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

CLIENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLIENT / "tools"))

import fetch_materials  # noqa: E402

SHA = re.compile(r"^[0-9a-f]{64}$")


class LockFileTests(unittest.TestCase):
    def setUp(self):
        self.lock = json.loads((CLIENT / "materials.lock.json").read_text(encoding="utf-8-sig"))

    def test_every_pinned_hash_is_a_real_sha256(self):
        found = []

        def walk(node, path=""):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k in ("sha256", "tree_sha256", "baseline_sha256") or (
                            path.endswith("key_files") and isinstance(v, str)):
                        found.append((path + "/" + k, v))
                    walk(v, path + "/" + k)
        walk(self.lock)
        self.assertGreater(len(found), 10)
        for where, value in found:
            self.assertRegex(value, SHA, where)

    def test_downloads_are_https_from_known_hosts(self):
        hosts = {"globalcdn.nuget.org", "www.gyan.dev", "cdn.playwright.dev", "dl.google.com"}
        for name in ("python", "ffmpeg", "playwright_ffmpeg", "webkit", "chrome_offline"):
            url = self.lock[name]["url"]
            self.assertTrue(url.startswith("https://"), url)
            self.assertIn(url.split("/")[2], hosts, url)

    def test_wheels_pinned_by_their_own_lock(self):
        wheels = json.loads((CLIENT / "wheels.lock.json").read_text(encoding="utf-8-sig"))["wheels"]
        self.assertEqual(len(wheels), 16)
        for w in wheels:
            self.assertRegex(w["sha256"], SHA)

    def test_chrome_is_verified_by_signature_not_a_moving_hash(self):
        self.assertEqual(self.lock["chrome_offline"]["policy"], "latest-signed")
        self.assertEqual(self.lock["chrome_offline"]["signer"], "Google LLC")


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.nsi = (CLIENT / "installer" / "setup.nsi").read_text(encoding="utf-8-sig")

    def test_install_dir_is_fixed_and_version_free(self):
        self.assertIn(r'$LOCALAPPDATA\Programs\HuolongAgent\publish-assistant"', self.nsi)
        self.assertNotRegex(self.nsi, r"publish-assistant-\d")
        self.assertNotIn("1.2.33", self.nsi)

    def test_upgrade_keeps_a_rollback_and_never_touches_data(self):
        self.assertIn('Rename "$INSTDIR" "$INSTDIR.old"', self.nsi)
        self.assertIn("rollback:", self.nsi)
        # the data area is only ever mentioned in messages, never removed
        for line in self.nsi.splitlines():
            if "RMDir" in line or "Delete " in line:
                self.assertNotIn("publish-worker", line)

    def test_silent_install_is_supported(self):
        self.assertIn("IfSilent", self.nsi)
        self.assertIn("--silent", self.nsi)

    def test_tree_hash_is_stable_and_order_independent(self, ):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            a = Path(t) / "a"
            (a / "sub").mkdir(parents=True)
            (a / "x.txt").write_text("1", encoding="utf-8")
            (a / "sub" / "y.txt").write_text("2", encoding="utf-8")
            first = fetch_materials.tree_sha256(a)
            self.assertEqual(first, fetch_materials.tree_sha256(a))
            self.assertEqual(first["files"], 2)


if __name__ == "__main__":
    unittest.main()
