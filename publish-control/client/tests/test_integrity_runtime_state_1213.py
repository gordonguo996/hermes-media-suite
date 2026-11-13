"""1.2.213: runtime config files under config/ are not "tamper"."""
import hashlib
import json
from pathlib import Path

from agent.winport.integrity import verify_program


def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _mkprogram(tmp_path, extra_files=None):
    prog = tmp_path / "prog"
    (prog / "agent" / "winport").mkdir(parents=True)
    (prog / "config").mkdir(parents=True)
    src = prog / "agent" / "winport" / "cli.py"
    src.write_text("print('x')\n", encoding="utf-8")
    cfg = prog / "config" / "environments.json"
    cfg.write_text("{}\n", encoding="utf-8")
    manifest = {
        "schema_version": "1",
        "version": "1.2.213",
        "files": {
            "agent/winport/cli.py": {"sha256": _sha(src)},
            "config/environments.json": {"sha256": _sha(cfg)},
        },
    }
    (prog / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    for rel, body in (extra_files or {}).items():
        p = prog / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return prog


def test_activation_and_license_json_are_not_tamper(tmp_path):
    prog = _mkprogram(tmp_path, {
        "config/activation.json": json.dumps({"endpoint": "https://x/v1/activate"}),
        "config/license.json": "{}",
    })
    v = verify_program(prog)
    assert v["ok"] is True, v
    assert v["extra"] == []
    assert v["checked"] == 2


def test_other_json_under_config_is_still_extra(tmp_path):
    """Only the two exact paths are exempt -- not every .json in config/."""
    prog = _mkprogram(tmp_path, {"config/settings.json": "{}",
                                 "config/activation.json.bak": "{}"})
    v = verify_program(prog)
    assert v["ok"] is False
    assert "config/settings.json" in v["extra"]
    assert "config/activation.json.bak" in v["extra"]


def test_script_smuggled_into_config_is_still_extra(tmp_path):
    prog = _mkprogram(tmp_path, {"config/patch.py": "print(1)\n"})
    v = verify_program(prog)
    assert v["ok"] is False
    assert "config/patch.py" in v["extra"]


def test_script_elsewhere_still_extra(tmp_path):
    prog = _mkprogram(tmp_path, {"agent/winport/evil.py": "print(1)\n"})
    v = verify_program(prog)
    assert v["ok"] is False
    assert "agent/winport/evil.py" in v["extra"]


def test_modified_file_still_fails(tmp_path):
    prog = _mkprogram(tmp_path)
    (prog / "agent" / "winport" / "cli.py").write_text("print('hacked')\n", encoding="utf-8")
    v = verify_program(prog)
    assert v["ok"] is False
    assert "agent/winport/cli.py" in v["modified"]


def _mkprogram_with_tracked_state(tmp_path):
    """activation.json / license.json are legitimately in MANIFEST too.

    Some packages ship a template copy, so the allowlist must not weaken the
    hash check for a tracked file.
    """
    prog = tmp_path / "prog"
    (prog / "agent" / "winport").mkdir(parents=True)
    (prog / "config").mkdir(parents=True)
    src = prog / "agent" / "winport" / "cli.py"
    src.write_text("print('x')\n", encoding="utf-8")
    act = prog / "config" / "activation.json"
    act.write_text("{}", encoding="utf-8")
    lic = prog / "config" / "license.json"
    lic.write_text("{}", encoding="utf-8")
    manifest = {
        "schema_version": "1",
        "version": "1.2.213",
        "files": {
            "agent/winport/cli.py": {"sha256": _sha(src)},
            "config/activation.json": {"sha256": _sha(act)},
            "config/license.json": {"sha256": _sha(lic)},
        },
    }
    (prog / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    return prog, act, lic


def test_tracked_activation_json_unchanged_passes(tmp_path):
    prog, _act, _lic = _mkprogram_with_tracked_state(tmp_path)
    v = verify_program(prog)
    assert v["ok"] is True, v
    assert v["checked"] == 3


def test_tracked_activation_json_modified_still_fails(tmp_path):
    """The allowlist only suppresses EXTRA -- hash check stays live."""
    prog, act, _lic = _mkprogram_with_tracked_state(tmp_path)
    act.write_text('{"endpoint": "https://evil/v1/activate"}', encoding="utf-8")
    v = verify_program(prog)
    assert v["ok"] is False
    assert "config/activation.json" in v["modified"]


def test_tracked_license_json_deleted_still_fails(tmp_path):
    prog, _act, lic = _mkprogram_with_tracked_state(tmp_path)
    lic.unlink()
    v = verify_program(prog)
    assert v["ok"] is False
    assert "config/license.json" in v["missing"]


def test_nested_config_activation_json_is_extra(tmp_path):
    """Exact path only: config/sub/activation.json is NOT exempt."""
    prog = _mkprogram(tmp_path, {"config/sub/activation.json": "{}"})
    v = verify_program(prog)
    assert v["ok"] is False
    assert "config/sub/activation.json" in v["extra"]
