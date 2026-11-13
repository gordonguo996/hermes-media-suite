"""Every shipped Python file must compile with SyntaxWarning promoted to error.

Windows paths in docstrings (``run\\program-path.txt``) are "invalid escape
sequences": a SyntaxWarning on Python 3.12-3.14 (customer machines run 3.14)
and scheduled to become a SyntaxError.  bootstrap_py.py is the installer, so a
future Python turning that warning into an error would break every install.
Use raw docstrings (r\"\"\"...\"\"\") for text containing backslashes.
"""
from __future__ import annotations

import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {"__pycache__", ".venv", "venv", "node_modules"}


class NoInvalidEscapesTests(unittest.TestCase):
    def test_all_python_files_compile_without_syntax_warnings(self):
        bad = []
        files = [p for p in ROOT.rglob("*.py") if not SKIP_PARTS & set(p.parts)]
        self.assertGreater(len(files), 50)
        for path in files:
            src = path.read_text(encoding="utf-8-sig")
            with warnings.catch_warnings():
                warnings.simplefilter("error", SyntaxWarning)
                try:
                    compile(src, str(path), "exec")
                except SyntaxError as exc:
                    bad.append("%s:%s %s" % (path.relative_to(ROOT), exc.lineno, exc.msg))
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
