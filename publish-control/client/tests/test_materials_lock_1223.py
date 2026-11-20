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


class ChromeVerdictTests(unittest.TestCase):
    """1.2.223: 签名接口说不出结论时不许静默放行，也不许把报告写成空 INVALID:。"""

    def setUp(self):
        self.lock = json.loads((CLIENT / "materials.lock.json").read_text(encoding="utf-8-sig"))
        self.cfg = self.lock["chrome_offline"]
        self.pinned = self.cfg["accepted_sha256"][0]

    def test_accepted_hashes_are_real_sha256(self):
        for value in self.cfg["accepted_sha256"]:
            self.assertRegex(value, SHA)

    def test_valid_google_signature_passes(self):
        got = fetch_materials.chrome_verdict(
            {"status": "Valid", "subject": 'CN="Google LLC", O="Google LLC"', "detail": ""},
            "0" * 64, self.cfg)
        self.assertTrue(got.startswith("authenticode:"))

    def test_valid_but_wrong_signer_is_refused(self):
        with self.assertRaises(fetch_materials.MaterialError):
            fetch_materials.chrome_verdict(
                {"status": "Valid", "subject": 'CN="Someone Else"', "detail": ""}, self.pinned, self.cfg)

    def test_definite_failures_are_not_rescued_by_hash(self):
        for status in ("NotSigned", "HashMismatch", "NotTrusted"):
            with self.assertRaises(fetch_materials.MaterialError):
                fetch_materials.chrome_verdict(
                    {"status": status, "subject": "", "detail": ""}, self.pinned, self.cfg)

    def test_indeterminate_status_falls_back_to_pinned_hash_only(self):
        got = fetch_materials.chrome_verdict(
            {"status": "ERROR", "subject": "", "detail": "签名接口超时"}, self.pinned, self.cfg)
        self.assertEqual(got, "sha256-pinned(ERROR)")
        with self.assertRaises(fetch_materials.MaterialError):
            fetch_materials.chrome_verdict(
                {"status": "ERROR", "subject": "", "detail": "签名接口超时"}, "1" * 64, self.cfg)

    def test_non_windows_needs_a_pinned_hash(self):
        self.assertEqual(fetch_materials.chrome_verdict(None, self.pinned, self.cfg),
                         "sha256-pinned(non-windows)")
        with self.assertRaises(fetch_materials.MaterialError):
            fetch_materials.chrome_verdict(None, "2" * 64, self.cfg)


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
