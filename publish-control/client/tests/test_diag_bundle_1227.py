"""1.2.227: the diagnostics bundle carries logs and redacted config, never secrets."""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from winport import diag_bundle  # noqa: E402
from winport.paths import Layout  # noqa: E402

FP = {"machine_id": "abcdef0123456789" * 2, "weak": False}
SECRETS = ["SECRET-RELAY-123", "SECRET-CLIENT-456", "SECRET-TOKEN-789", "SECRET-LICENSE-000",
           "SECRET-COOKIE-111", "SECRET-BEARER-222"]


def _setup(tmp_path):
    layout = Layout(program=ROOT, data=tmp_path / "data", system=tmp_path / "sys").ensure()
    layout.logs.mkdir(parents=True, exist_ok=True)
    (layout.logs / "agent.log").write_text(
        "start ok\nAuthorization: Bearer SECRET-BEARER-222abcdef\nsession_cookie=SECRET-COOKIE-111\n",
        encoding="utf-8")
    layout.config.mkdir(parents=True, exist_ok=True)
    layout.settings_file.write_text(json.dumps(
        {"relay_key": "SECRET-RELAY-123", "nested": {"client_key": "SECRET-CLIENT-456",
                                                       "api_token": "SECRET-TOKEN-789"},
         "interval": 30}), encoding="utf-8")
    (layout.config / "control.json").write_text('{"key": "SECRET-RELAY-123"}', encoding="utf-8")
    layout.system.mkdir(parents=True, exist_ok=True)
    layout.license_file.write_text('{"sig": "SECRET-LICENSE-000"}', encoding="utf-8")
    prof = layout.data / "profiles" / "douyin"
    prof.mkdir(parents=True, exist_ok=True)
    (prof / "Cookies").write_text("SECRET-COOKIE-111", encoding="utf-8")
    return layout


def _read_all(path):
    with zipfile.ZipFile(path) as zf:
        return {n: zf.read(n).decode("utf-8", "replace") for n in zf.namelist()}


def test_bundle_has_logs_and_no_secrets(tmp_path):
    layout = _setup(tmp_path)
    res = diag_bundle.export(layout, fingerprint_info=FP)
    assert res["ok"], res
    files = _read_all(res["path"])
    assert "summary.json" in files and "logs/agent.log" in files
    blob = "\n".join(files.values())
    for s in SECRETS:
        assert s not in blob, s
    assert json.loads(files["config/settings.json"])["interval"] == 30


def test_credential_files_never_collected(tmp_path):
    layout = _setup(tmp_path)
    files = _read_all(diag_bundle.export(layout, fingerprint_info=FP)["path"])
    names = " ".join(files)
    assert "control.json" not in names and "license.json" not in names
    assert "Cookies" not in names and "profiles" not in names
    assert json.loads(files["summary.json"])["license_file_present"] is True


def test_old_bundles_are_pruned(tmp_path):
    layout = _setup(tmp_path)
    out = layout.data / "diagnostics"
    for i in range(8):
        diag_bundle.export(layout, out_dir=out, now=1_700_000_000 + i * 10, fingerprint_info=FP)
    assert len(list(out.glob("diag-*.zip"))) == diag_bundle.KEEP_BUNDLES


def test_failure_returns_sentence_not_traceback(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    layout = Layout(program=ROOT, data=tmp_path / "d", system=tmp_path / "s")
    res = diag_bundle.export(layout, out_dir=blocker / "sub", fingerprint_info=FP)
    assert res["ok"] is False and "诊断包生成失败" in res["reason"]


def test_export_is_customer_only_and_has_panel_button():
    from winport import remote_policy
    assert "diag-export" not in remote_policy.ALLOWED_CLI
    src = (ROOT / "agent" / "winport" / "panel.py").read_text(encoding="utf-8")
    assert "/api/diag-export" in src and "diag-export" in src


def test_panel_serves_button_and_endpoint_end_to_end(tmp_path):
    import socket
    import threading
    import urllib.error
    import urllib.request
    from winport import panel
    layout = _setup(tmp_path)
    # Windows can hand the probed port to someone else (WinError 10048); retry on a new port,
    # and never go through the machine's system proxy for a loopback call.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last = None
    for _ in range(5):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        try:
            server, _h = panel.make_server(layout, [], port=port)
        except OSError as exc:
            last = exc
            continue
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = "http://127.0.0.1:%d" % port
            html = opener.open(base + "/", timeout=20).read().decode("utf-8")
            req = urllib.request.Request(base + "/api/diag-export", method="POST", headers={"Origin": base})
            res = json.loads(opener.open(req, timeout=20).read())
        except (urllib.error.URLError, OSError) as exc:
            last = exc
            continue
        finally:
            server.shutdown()
            server.server_close()
        assert "diag-export" in html
        assert res["ok"]
        blob = "\n".join(_read_all(res["path"]).values())
        assert not any(sec in blob for sec in SECRETS)
        return
    raise AssertionError("panel never answered: %r" % (last,))
