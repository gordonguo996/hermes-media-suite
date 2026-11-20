"""1.2.223: the WebKit engine is retired -- nothing may default into it, and an
old configuration that still names it gets a plain message, not a dead launch."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

CLIENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLIENT / "agent"))

from winport import browser, channels  # noqa: E402


class WebkitRetiredTests(unittest.TestCase):
    def test_entry_without_engine_defaults_to_chrome(self):
        acc = channels.account_from_entry({"account_id": "a1"})
        self.assertEqual(acc.engine, "chrome")

    def test_old_webkit_channel_reports_instead_of_launching(self):
        ch = SimpleNamespace(channel_id="douyin-002", env_id=None, engine="webkit",
                             login_url="https://example.invalid", debug_port=None)
        layout = SimpleNamespace(data=Path("."))
        plan = browser.plan_open(layout, ch, profile_dir=Path("p"))
        self.assertEqual(plan["command"], [])
        self.assertIn("WebKit", plan["error"])

    def test_templates_and_launcher_carry_no_webkit_slot(self):
        for name in ("channels.json.tmpl", "accounts.json.tmpl"):
            text = (CLIENT / "config" / name).read_text(encoding="utf-8-sig")
            self.assertNotIn('"engine": "webkit"', text, name)
            json.loads(text)
        self.assertNotIn("webkit-2070", (CLIENT / "bootstrap.bat").read_text(encoding="utf-8"))

    def test_webkit_is_not_in_the_package(self):
        lock = json.loads((CLIENT / "materials.lock.json").read_text(encoding="utf-8-sig"))
        self.assertFalse(lock["webkit"]["include_in_package"])


if __name__ == "__main__":
    unittest.main()
