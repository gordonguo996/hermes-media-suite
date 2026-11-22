"""One-click self-test (installer + customer).  Lights:
Python / tray / browser (Google Chrome -- the only engine we drive) /
media tool / licence / storage.

Exit codes follow the shared contract (errors.py).  On a non-Windows static
host the platform-bound lights are reported as SKIPPED (static host) rather
than faked green -- we never claim a Windows fact was verified here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .errors import (EXIT_CONFIG, EXIT_GATE_RED, EXIT_OK, EXIT_STORAGE,
                     EXIT_MEANING)
from .paths import Layout

STATUS_GREEN = "GREEN"
STATUS_RED = "RED"
STATUS_WARN = "WARN"
STATUS_SKIP = "SKIPPED"


def _reconfigure_stdout() -> None:
    """Never override the codec the entry chose; only soften encode errors.

    1.2.5 round-6/round-7 (GPT review item 13 / N4): the Windows entry points
    (selftest.ps1 / bootstrap.ps1) export ``PYTHONIOENCODING`` as the console's
    OWN code page (e.g. ``cp936``) so Python opens stdout with that codec; if a
    caller explicitly asks for ``ascii``, that must be honoured too.  Rewriting
    the encoding here re-created the exact GBK/UTF-8 mojibake the code-page
    policy exists to avoid.  The only guarantee we add is that a character the
    chosen code page cannot represent never crashes the report
    (``errors="replace"``) -- the encoding itself is left untouched.
    """
    try:
        sys.stdout.reconfigure(errors="replace")
        # 1.2.5 round-14 (8-A-R2): also flush after every line.  The install
        # entries export PYTHONUNBUFFERED=1 and pass -u, but a caller that
        # invokes the module directly must not lose diagnostics in the
        # interpreter's own buffer when the process is killed mid-run.
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError, OSError):
        pass


def _python_light(static):
    version = ".".join(str(p) for p in sys.version_info[:3])
    try:
        import playwright  # noqa: F401
        return STATUS_GREEN, f"python {version}, playwright import ok"
    except ImportError:
        if static:
            return STATUS_SKIP, f"python {version}, playwright absent (static host)"
        return STATUS_RED, f"python {version} but playwright missing"


def _chrome_light(layout: Layout, static):
    """Browser light -- Google Chrome only (source/brief-chrome-only-20261016.md).

    1.2.2: the light answers "is the customer's real Google Chrome present?".
    There is no substitute: no Chrome -> RED (on a real Windows box) carrying the
    plain-language "本程序需要 Google Chrome" sentence plus the in-package
    official installer.  Edge/Chromium are never offered as a replacement, and we
    never install anything for the customer.  On the Linux build host we cannot
    probe a customer machine -> SKIPPED, never fake-green.
    """
    from . import chrome
    report = chrome.detect(layout, store=False)
    if not report.get("found"):
        if static:
            return STATUS_SKIP, ("浏览器检测跳过（构建机静态自检）："
                                 + report.get("reason", ""))
        return STATUS_RED, chrome.summarize(report)
    policy = report.get("policy") or {}
    if policy.get("blocked"):
        return STATUS_RED, chrome.summarize(report)
    if report.get("version_ok") is False:
        return STATUS_RED, chrome.summarize(report)
    if report.get("version_changed"):
        return STATUS_WARN, chrome.summarize(report)
    if report.get("version_ok") is None:
        return STATUS_WARN, chrome.summarize(report)
    return STATUS_GREEN, chrome.summarize(report)


def _media_light(layout: Layout, static):
    if layout.ffmpeg.exists():
        return STATUS_GREEN, f"ffmpeg present: {layout.ffmpeg.name}"
    return (STATUS_SKIP, "ffmpeg absent (static host)") if static else (
        STATUS_RED, f"ffmpeg missing: {layout.ffmpeg}")


def _tray_light(static):
    """Tray light = "will the customer actually see the tray icon?".

    This is the G2 evidence: the launcher now starts the resident agent with the
    venv's pythonw (bootstrap installs pystray/Pillow into that venv), and the
    self-test runs in the SAME interpreter -- so a green tray light here is a
    statement about the interpreter the launcher really uses.
    """
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
        return STATUS_GREEN, "pystray + Pillow import ok (tray icon can start)"
    except ImportError as exc:
        if static:
            return STATUS_SKIP, f"pystray/Pillow absent (static host): {exc}"
        return STATUS_RED, ("tray deps missing from THIS interpreter "
                            f"({exc}); the launcher must use the venv pythonw")


def _vendor_key_note(layout: Layout):
    """Validate the shipped production public key, so G1 cannot silently rot.

    1.2.10 (GPT_REVIEW_1.2.8 必修 1): read the key from ``install_root()`` -- the
    directory this code actually runs from -- NOT ``layout.program`` (which
    honours ``WINPORT_PROGRAM`` / ``--program``).  Otherwise the self-test's
    "G1" pre-check would validate an ATTACKER's key while the licence verdict
    below (which already uses the fixed root) rejected it -- the same display /
    production split the review found on the panel.  ``layout`` stays in the
    signature for call-site compatibility only.
    """
    from .ed25519_pure import public_key_valid
    from .paths import install_root
    key_path = install_root() / "config" / "vendor_pubkey.hex"
    if not key_path.exists():
        return False, f"vendor_pubkey.hex absent: {key_path}"
    try:
        key = bytes.fromhex(key_path.read_text(encoding="utf-8-sig").strip())
    except ValueError:
        return False, f"vendor_pubkey.hex is not hex: {key_path}"
    ok, reason = public_key_valid(key)
    return ok, f"vendor key {reason} ({key_path.name}, {len(key)} bytes)"


def _machine_code() -> str:
    """The machine id the vendor binds a licence / activation code to."""
    try:
        from .fingerprint import fingerprint
        return str(fingerprint().get("machine_id") or "")
    except Exception:
        return ""


def _activation_hint() -> str:
    code = _machine_code()
    machine = "面板顶部「查看机器码」" + (f"（本机机器码：{code}）" if code else "")
    return ("请打开" + machine + "，把机器码发给服务商换取 8 位激活码，"
            "填入面板「激活码」框后点「用激活码激活」；也可让服务商签发 "
            "license.json，再用包内 import-license.bat 导入。")


def _license_light(layout: Layout, static):
    """Licence light -- now the SAME verdict the panel and daemon show.

    1.2.8 (GPT_REVIEW_1.2.7 必修 2): reading license.json used to sit OUTSIDE
    the try, so a half-written ``{`` crashed the module into an English
    JSONDecodeError traceback -- and ``bootstrap.ps1`` calls this module during
    install.  The file is now read through ``licstate.read_license_file`` (never
    raises) and:
      * file MISSING + trial valid -> WARN with the remaining days (new-customer
                            package: the install-time trial is the expected
                            state before a formal licence arrives);
      * file MISSING + trial unusable -> RED with the machine code / activation
                            code route;
      * file damaged / empty / not an object -> RED, Chinese re-import steps, and
                            a NONZERO exit code (EXIT_GATE_RED via run_selftest).
    The signature / machine / clock verdict itself comes from the one shared
    ``licstate.unified_license_state``.
    """
    from .licstate import (REIMPORT_TEXT, STATUS_UNREADABLE,
                           read_license_file, unified_license_state)
    key_ok, key_note = _vendor_key_note(layout)
    read = read_license_file(layout.license_file)
    if read["kind"] == "missing":
        if not key_ok:
            return (STATUS_SKIP, f"licence check skipped (static host): {key_note}") \
                if static else (STATUS_RED, key_note)
        state = unified_license_state(layout)
        status = state.get("status")
        if status == "TRIAL" and state.get("publish_allowed"):
            return STATUS_WARN, (
                "未导入正式授权（新客户包正常状态）；试用授权有效，剩余 %s 天。%s"
                % (state.get("days_left"), _activation_hint()))
        # 1.2.227: say WHY there is no trial and what to do (same text the panel shows).
        notices = state.get("notices") or []
        detail = ("；".join(notices) or "；".join(state.get("reasons") or [])
                  or state.get("text") or "试用记录不存在或已失效").rstrip("。")
        return STATUS_RED, (
            "未找到正式授权（license.json），且试用未创建或已失效：%s。%s"
            % (detail, _activation_hint()))
    if read["kind"] != "ok":
        return STATUS_RED, (f"授权文件损坏，无法解析（{read['error']}），"
                            "新增发布暂停（抓数与历史数据不受影响）。"
                            + REIMPORT_TEXT)
    if not key_ok:
        return (STATUS_SKIP, f"licence check skipped (static host): {key_note}") \
            if static else (STATUS_RED, key_note)
    state = unified_license_state(layout)
    status = state.get("status")
    if status == "NO_VENDOR_KEY":
        detail = state.get("trust_root") or key_note
        return (STATUS_SKIP, f"licence check skipped (static host): {detail}") \
            if static else (STATUS_RED,
                            f"供应商公钥缺失或损坏，拒绝发布：{detail}")
    if status in (STATUS_UNREADABLE, "SCHEMA_INVALID"):
        return STATUS_RED, (state.get("text") or
                            ("授权文件损坏或字段异常。" + REIMPORT_TEXT))
    if status == "OK":
        level = state.get("expiry_level")
        days = state.get("days_left")
        if level in ("none", "30"):
            return STATUS_GREEN, (
                f"licence OK days_left={days}; {state.get('text')}；{key_note}")
        if level in ("7", "1"):
            return STATUS_WARN, (
                f"licence OK days_left={days}; 授权即将到期，剩余 {days} 天，"
                f"请尽快续期；{state.get('text')}；{key_note}")
        return STATUS_WARN, (
            f"licence OK days_left={days}; 授权到期级别 {level}，请尽快续期；"
            f"{state.get('text')}；{key_note}")
    if status == "GRACE":
        return STATUS_WARN, (
            f"licence GRACE days_left={state.get('days_left')}; "
            "授权已过期，处于离线宽限期；到期后只停新增发布，"
            f"抓数与历史数据不受影响；{key_note}")
    if status == "TRIAL" and state.get("publish_allowed"):
        return STATUS_WARN, (
            "未导入正式授权；试用授权有效，剩余 %s 天。%s"
            % (state.get("days_left"), _activation_hint()))
    reasons = ("；".join(state.get("reasons") or [])
               or "授权校验未通过").rstrip("。")
    return STATUS_RED, (f"licence {status}（{state.get('label')}）：{reasons}。"
                        + _activation_hint()
                        + (f"；{key_note}" if key_note else ""))


def _evidence_light(layout: Layout):
    """B: the failure-evidence directory exists, is writable and has a policy."""
    from .evidence import EvidenceStore
    try:
        store = EvidenceStore(layout)
        store.ensure()
        probe = store.root / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return STATUS_RED, f"evidence dir not writable: {exc}"
    return STATUS_GREEN, (f"{store.root}（保留 {store.retention_days} 天，"
                          f"现有 {len(store.list_entries())} 条）")


def _storage_light(layout: Layout):
    if layout.writable():
        return STATUS_GREEN, f"data area writable: {layout.data}"
    return STATUS_RED, f"data area not writable: {layout.data}"


def run_selftest(layout: Layout | None = None, *, static=None) -> dict:
    _reconfigure_stdout()
    layout = (layout or Layout()).ensure()
    if static is None:
        static = os.name != "nt"
    checks = {
        "python": _python_light(static),
        "tray": _tray_light(static),
        "chrome": _chrome_light(layout, static),
        "media_tool": _media_light(layout, static),
        "license": _license_light(layout, static),
        "evidence": _evidence_light(layout),
        "storage": _storage_light(layout),
    }
    reds = [k for k, (status, _) in checks.items() if status == STATUS_RED]
    if "storage" in reds:
        code = EXIT_STORAGE
    elif reds:
        code = EXIT_GATE_RED
    else:
        code = EXIT_OK
    return {"checks": checks, "red": reds, "exit_code": code, "static": static,
            "machine_code": _machine_code(), "layout": layout.describe()}


def print_selftest(result: dict) -> None:
    # 1.2.5 round-14 (8-A-R2): flush every line, so a killed / blocked
    # interpreter keeps the report it has already written.
    print("=" * 60, flush=True)
    print("publish-assistant self-test", flush=True)
    print("=" * 60, flush=True)
    for name, (status, detail) in result["checks"].items():
        print(f"[{status:7}] {name:15} {detail}", flush=True)
    print(f"机器码: {result.get('machine_code') or '（读取失败）'}", flush=True)
    print("-" * 60, flush=True)
    if result["exit_code"] == EXIT_OK:
        print("SELFTEST: ALL GREEN"
              + ("  (static host: platform-bound lights skipped)"
                 if result["static"] else ""), flush=True)
    else:
        print(f"SELFTEST: FAILED reds={result['red']}  "
              f"({EXIT_MEANING.get(result['exit_code'])})", flush=True)
    print("=" * 60, flush=True)


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="winport.selftest")
    parser.add_argument("--data")
    parser.add_argument("--program")
    parser.add_argument("--license-pending-ok", action="store_true",
                        help="installer use: a missing licence/trial is the ONLY red -> exit 0 "
                             "(the product is installed; the panel asks for activation)")
    parser.add_argument("--require-green", action="store_true",
                        help="treat WARN as failure too")
    args = parser.parse_args(argv)
    layout = Layout(program=Path(args.program) if args.program else None,
                    data=Path(args.data) if args.data else None)
    result = run_selftest(layout)
    print_selftest(result)
    if args.license_pending_ok and result["red"] == ["license"]:
        print("授权待办：除授权外自检全部通过。程序已安装，请在面板里激活（见上面的授权提示）。",
              flush=True)
        return EXIT_OK
    if args.require_green:
        warns = [k for k, (s, _) in result["checks"].items() if s == STATUS_WARN]
        if warns:
            return EXIT_CONFIG
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
