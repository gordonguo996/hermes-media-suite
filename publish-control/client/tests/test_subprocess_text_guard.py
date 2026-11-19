"""Every product subprocess that reads text names its encoding.

The product runs with PYTHONUTF8=1 while Windows tools (icacls, schtasks,
tasklist...) print in the ANSI code page, GBK on a Chinese Windows.  A bare
``text=True`` then decodes GBK as UTF-8 and the pipe reader thread raises
UnicodeDecodeError (1.2.222's icacls hardening did this on every activation).
Use ``**text_mode_options()`` for native tools, or an explicit encoding for
our own UTF-8 children.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

CLIENT = Path(__file__).resolve().parents[1]


class TextEncodingGuard(unittest.TestCase):
    def test_no_bare_text_mode(self):
        files = sorted((CLIENT / "agent" / "winport").glob("*.py")) + \
            sorted((CLIENT / "scripts").glob("*.py")) + [CLIENT / "run_panel.py"]
        bad = []
        for path in files:
            if not path.exists():
                continue
            src = path.read_text(encoding="utf-8-sig")
            for node in ast.walk(ast.parse(src)):
                if not (isinstance(node, ast.Call)
                        and getattr(node.func, "attr", None) in ("run", "Popen", "check_output")):
                    continue
                kws = {k.arg: k.value for k in node.keywords}
                flag = kws.get("text", kws.get("universal_newlines"))
                if isinstance(flag, ast.Constant) and flag.value is True and "encoding" not in kws:
                    bad.append("%s:%d" % (path.relative_to(CLIENT), node.lineno))
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
