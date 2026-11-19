"""1.2.222: ``dry-run --no-write`` must expose confirm_token without writing.

The bot clears an eligible agent hold, then validates with
``dry-run --plan ... --no-write`` and only publishes when that output carries
``ok=true`` and a ``confirm_token``.  If the CLI omitted the token, every
auto-clear would stop before publishing.  This test pins the real command's
output shape, not a mocked bot-side payload.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

CLIENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLIENT / "agent"))

from winport import cli, dryrun  # noqa: E402


class DryRunNoWriteTests(unittest.TestCase):
    def test_no_write_emits_confirm_token_and_does_not_write(self):
        layout = SimpleNamespace(settings_file=Path("/tmp/settings.json"),
                                 run=Path("/tmp/run"))
        preview = {
            "ok": True,
            "confirm_token": "tok-1222",
            "confirm_ref": "dryrun:tok-1222",
            "problems": [],
            "warnings": [],
            "idempotency_key": "idem-1222",
            "confirmation": None,
        }
        args = SimpleNamespace(plan="/tmp/plan.json", out=None, out_md=None,
                               no_write=True, confirm_token=None, license=None)
        writes = []

        def fake_write(*_a, **_k):
            writes.append(True)
            raise AssertionError("--no-write must not call write_preview")

        with mock.patch.object(cli, "_layout", return_value=layout), \
                mock.patch.object(cli, "read_json", return_value={"plan_id": "p"}), \
                mock.patch.object(cli.xingtu_tasks, "used_tasks_path",
                                  return_value=None), \
                mock.patch.object(cli.xingtu_tasks, "validate_plan_contract",
                                  return_value={"ok": True}), \
                mock.patch.object(cli, "_envs", return_value={}), \
                mock.patch.object(cli, "_channels", return_value={}), \
                mock.patch.object(cli, "_licence_gate", return_value={}), \
                mock.patch.object(dryrun, "build_preview", return_value=preview), \
                mock.patch.object(dryrun, "write_preview", side_effect=fake_write), \
                mock.patch("winport.settings.load_settings", return_value={}):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.cmd_dry_run(args)

        self.assertEqual(code, cli.EXIT_OK)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["confirm_token"], "tok-1222")
        self.assertEqual(payload["confirm_ref"], "dryrun:tok-1222")
        self.assertFalse(payload["written"])
        self.assertEqual(writes, [])


if __name__ == "__main__":
    unittest.main()
