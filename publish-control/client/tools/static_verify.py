"""Static acceptance rehearsal -- one command, one verdict.

Runs the parts of the port that are provable on a Linux box (no browser, no
network, no Windows APIs) and prints PASS/FAIL per check.  Real-machine items
are NOT run here; they live in VERIFY.md.
"""

from __future__ import annotations

import os

import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import make_fixture  # noqa: E402
from winport import approval, folder_bind, logutil  # noqa: E402
from winport import manifest as manifest_mod  # noqa: E402
from winport import ed25519_pure  # noqa: E402
from winport.collect_douyin import normalize_export  # noqa: E402
from winport.downlink import Downlink, FileSystemSource  # noqa: E402
from winport.errors import CanaryNotArmed, ConfigError, GateRed  # noqa: E402
from winport.fingerprint import fingerprint  # noqa: E402
from winport.license import check_license  # noqa: E402
try:
    import devsign  # noqa: E402
    from devsign import mint_dev_license, issue_receipt_for_test  # noqa: E402
    DEV_SIGN_AVAILABLE = True
except ImportError:
    devsign = None
    mint_dev_license = None
    issue_receipt_for_test = None
    DEV_SIGN_AVAILABLE = False
from winport.minimal_xlsx import read_xlsx, write_xlsx  # noqa: E402
from winport.paths import Layout  # noqa: E402
from winport.publish_exec import (MODE_APPROVED, MODE_FILL, NullDriver,  # noqa: E402
                                  PUBLISH_BUTTON_SELECTORS,
                                  assert_selector_policy, run_publish)
from winport import humanize as humanize_mod  # noqa: E402
from winport import publish_guard as publish_guard  # noqa: E402
from winport.settings import load_settings  # noqa: E402
try:
    from receipt_fixture import ReceiptDriver  # noqa: E402
    RECEIPT_FIXTURE_AVAILABLE = True
except ImportError:
    ReceiptDriver = None
    RECEIPT_FIXTURE_AVAILABLE = False
from winport.stage2_ledger import Stage2Ledger  # noqa: E402
from winport import onboard  # noqa: E402
from winport.subprocess_text import text_mode_options  # noqa: E402

RESULTS = []
SKIPS = []


def read_script_text(path):
    """Decode one of OUR Windows scripts regardless of its on-disk encoding.

    1.2.4 encoding policy (source/brief-encoding-20261016.md): ``.ps1`` is
    UTF-8 WITH BOM and ``.bat``/``.vbs`` is ANSI/GBK.  The verifier must not
    assume one -- try UTF-8 (BOM-tolerant) first, then GBK.
    """
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    print(f"[{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -- {detail}" if detail else ""))
    return bool(condition)


def skip(name, detail=""):
    """Record a check that needs developer-only helpers absent from the package."""
    SKIPS.append((name, detail))
    print(f"[SKIP] {name}" + (f"  -- {detail}" if detail else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="winport-verify-"))
    layout = Layout(program=ROOT, data=tmp / "data", system=tmp / "system").ensure()
    # bootstrap.ps1 seeds the config templates into the data area; do the same
    # here so the environment registry is exercised exactly as installed.
    import shutil
    shutil.copyfile(ROOT / "config" / "environments.json.tmpl",
                    layout.environments_file)

    print("== fingerprint ==")
    fp = fingerprint({"machine_guid": "GUID-A", "volume_serial": "DEADBEEF",
                      "bios_uuid": "UUID-A"})
    check("fingerprint is deterministic", fp["machine_id"] ==
          fingerprint({"machine_guid": "GUID-A", "volume_serial": "DEADBEEF",
                       "bios_uuid": "UUID-A"})["machine_id"])
    check("fingerprint changes with a component",
          fp["machine_id"] != fingerprint({"machine_guid": "GUID-B",
                                           "volume_serial": "DEADBEEF",
                                           "bios_uuid": "UUID-A"})["machine_id"])

    print("== licence (Ed25519, machine-bound) ==")
    if DEV_SIGN_AVAILABLE:
        seed = os.urandom(32)
        public = devsign.publickey(seed)
        future = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
        lic = mint_dev_license(seed, machine_id="MACHINE-A", expires_at=future)
        check("valid licence -> OK",
              check_license(lic, public, "MACHINE-A")["status"] == "OK")
        check("copied licence -> MACHINE_MISMATCH",
              check_license(lic, public, "MACHINE-B")["status"] == "MACHINE_MISMATCH")
        tampered = dict(lic, customer_id="attacker")
        check("tampered licence -> SIGNATURE_INVALID",
              check_license(tampered, public, "MACHINE-A")["status"]
              == "SIGNATURE_INVALID")
    else:
        skip("licence (Ed25519, machine-bound)",
             "devsign unavailable in this package")

    print("== manifest validation ==")
    good = make_fixture.build_bucket(tmp / "bucket")
    check("generated manifest is valid",
          manifest_mod.validate_manifest(good) == [])
    bad = json.loads(json.dumps(good))
    bad["batch"]["folder_key"] = "B20261015-01_示例账号_种草 "  # trailing space
    check("folder_key with space rejected",
          any("folder_key" in e for e in manifest_mod.validate_manifest(bad)))
    bad2 = json.loads(json.dumps(good))
    bad2["pieces"][0]["sha256"] = "zz"
    check("bad sha256 rejected",
          any("sha256" in e for e in manifest_mod.validate_manifest(bad2)))

    print("== folder binding (exact whitelist) ==")
    wrong = layout.inbox / "B20261015-01" / "B20261015-01_示例账号_免费"
    wrong.mkdir(parents=True)
    check("unknown folder -> UNKNOWN_FOLDER",
          folder_bind.bind_folder(good, wrong, layout)["status"]
          == folder_bind.UNKNOWN_FOLDER)

    print("== downlink: fetch -> verify -> bind -> ready ==")
    settings = load_settings(layout.settings_file)
    downlink = Downlink(layout, FileSystemSource(tmp / "bucket"), settings,
                        free_bytes=lambda p: 10 ** 12,
                        machine_id="VERIFY", customer_id="newme-001")
    first = downlink.run_once()
    check("two pieces downloaded", first["downloaded"] == 2, json.dumps(
        {"downloaded": first["downloaded"], "folders": first["folders"][0]["status"]}))
    check("folder bound READY", first["folders"][0]["status"] == folder_bind.READY)
    make_fixture.build_bucket(tmp / "bucket", manifest_seq=20261015002)
    second = downlink.run_once()
    check("re-run fetches nothing new", second["downloaded"] == 0
          and second["skipped"] == 2)
    ledger_text = layout.downlink_ledger.read_text(encoding="utf-8")
    check("ledger carries no secrets",
          all(token not in ledger_text
              for token in ("Signature", "Cookie", "Bearer", "qrlogin")))

    print("== data collection (13 columns) ==")
    export = tmp / "export.xlsx"
    make_fixture.write_export_xlsx(export)
    rows = read_xlsx(export)
    norm = normalize_export(rows)
    check("13 columns accepted", norm["column_count"] == 13)
    try:
        normalize_export([["x"] * 12])
        drift_blocked = False
    except ConfigError:
        drift_blocked = True
    check("column drift blocked", drift_blocked)

    print("== report (xlsx that Excel/WPS can open) ==")
    from winport import report as report_mod
    rep = report_mod.write_report(layout, collection={
        "row_count": norm["row_count"], "column_count": norm["column_count"],
        "header": norm["header"]})
    import zipfile
    with zipfile.ZipFile(rep["xlsx"]) as zf:
        valid = zf.testzip() is None and "xl/workbook.xml" in zf.namelist()
    check("daily report xlsx written and valid", valid, rep["xlsx"])

    print("== publish gate ==")
    plan = {"plan_id": "PLAN-1", "account_id": "douyin-pilot-001",
            "schedule_at": "2026-10-20T12:00:00+08:00",
            "piece": {"title": "标题", "copy_body": "文案", "sha256": "a" * 64},
            "compliance": {"content_hash": "abc123"}}
    from winport.idempotency import material_fingerprint
    plan["compliance"]["content_hash"] = material_fingerprint(plan["piece"], account_id=plan["account_id"])
    led = Stage2Ledger(layout.stage2_ledger)
    # 1.2.5: a simulated driver is refused in EVERY mode (incl. fill) unless the
    # harness opts in, so a NullDriver can never masquerade as a filled form.
    try:
        run_publish(plan, NullDriver(platform_work_id="1111111111111111111"), led, mode=MODE_FILL)
        _fill_refused = False
    except GateRed:
        _fill_refused = True
    check("fill mode also refuses a simulated driver without opt-in", _fill_refused)
    fill = run_publish(plan, NullDriver(platform_work_id="1111111111111111111"), led, mode=MODE_FILL,
                       allow_simulated=True)
    check("fill mode never publishes (harness, opt-in)", fill["filled"] and not fill["published"])
    try:
        run_publish(plan, NullDriver(), led, mode=MODE_APPROVED,
                    canary_armed=False, allow_simulated=True)
        blocked = False
    except CanaryNotArmed:
        blocked = True
    check("approved mode needs canary", blocked)
    if DEV_SIGN_AVAILABLE and RECEIPT_FIXTURE_AVAILABLE:
        seed = os.urandom(32)
        pub = devsign.publickey(seed)
        receipt = issue_receipt_for_test(seed, plan)
        try:
            run_publish(plan, ReceiptDriver(), led, mode=MODE_APPROVED,
                        canary_armed=True, post_fill_green=True, receipt=None,
                        verify_key=pub, allow_simulated=True)
            blocked = False
        except GateRed:
            blocked = True
        check("approved mode needs a control-plane receipt", blocked)
        # 1.2.5: a simulated driver must be refused unless the caller opts in.
        try:
            run_publish(plan, ReceiptDriver(), led, mode=MODE_APPROVED,
                        canary_armed=True, post_fill_green=True,
                        receipt=receipt, verify_key=pub)
            refused = False
        except GateRed:
            refused = True
        check("simulated driver refused without opt-in (no auto-submit)",
              refused)
        ok_run = run_publish(
            plan, ReceiptDriver(platform_work_id="7697584059211402536"), led,
            mode=MODE_APPROVED, canary_armed=True, post_fill_green=True,
            receipt=receipt, verify_key=pub, allow_simulated=True)
        check("harness: approved publish reads back a platform work id",
              ok_run["published"]
              and ok_run["platform_work_id"] == "7697584059211402536")
        try:
            run_publish(
                plan, ReceiptDriver(platform_work_id="2222222222222222222"),
                led, mode=MODE_APPROVED, canary_armed=True,
                post_fill_green=True, receipt=receipt, verify_key=pub,
                allow_simulated=True)
            blocked = False
        except GateRed:
            blocked = True
        check("idempotency blocks a second publish", blocked)
    else:
        for name in (
                "approved mode needs a control-plane receipt",
                "simulated driver refused without opt-in (no auto-submit)",
                "harness: approved publish reads back a platform work id",
                "idempotency blocks a second publish"):
            skip(name, "devsign/receipt_fixture unavailable in this package")

    print("== redaction ==")
    red = logutil.redact("https://oss/x?Signature=SECRET Cookie: a=b Bearer zz.yy")
    check("presigned/cookie/bearer redacted",
          "SECRET" not in red and "Bearer zz" not in red)

    # ---- G1: the production vendor key is in the package and usable ---------
    print("== G1 vendor public key ==")
    key_path = ROOT / "config" / "vendor_pubkey.hex"
    check("config/vendor_pubkey.hex ships", key_path.exists())
    key_bytes = bytes.fromhex(key_path.read_text(encoding="utf-8").strip())
    check("vendor key is 32 bytes of hex", len(key_bytes) == 32)
    ok, reason = ed25519_pure.public_key_valid(key_bytes)
    check("vendor key is a usable Ed25519 point", ok, reason)
    if DEV_SIGN_AVAILABLE:
        forged_seed = os.urandom(32)
        forged = devsign.sign(b"x", forged_seed,
                              devsign.publickey(forged_seed))
        check("signature from another key is refused",
              not ed25519_pure.verify(forged, b"x", key_bytes))
    else:
        skip("signature from another key is refused",
             "devsign unavailable in this package")

    # ---- G2: the launcher prefers the interpreter the wheels went into ------
    print("== G2 launcher interpreter ==")
    for name in ("launcher.vbs", "run-agent.vbs"):
        text = read_script_text(ROOT / "scripts" / name)
        check(f"{name} prefers the venv pythonw",
              r".venv\Scripts\pythonw.exe" in text)
        # 1.2.177: the bundled runtime has no playwright/pystray; falling back to
        # it only produced a half-working agent.  The launcher must refuse
        # instead of silently degrading, so the absence is the invariant now.
        check(f"{name} never falls back to the bundled runtime",
              r"runtime\pythonw.exe" not in text and r"runtime\python.exe" not in text)
        check(f"{name} sets PLAYWRIGHT_BROWSERS_PATH",
              "PLAYWRIGHT_BROWSERS_PATH" in text)

    # ---- G3: one login slot per platform, each with its own light ----------
    print("== G3 multi-platform login slots ==")
    from winport import browser as browser_mod
    from winport import health, login_check, panel, platforms
    slots = platforms.load_channels(layout.channels_file,
                                    accounts_path=layout.accounts_file)
    names = {c.platform for c in slots}
    for want in ("douyin", "xingtu", "shipinhao", "xiaohongshu", "gongzhonghao"):
        check(f"platform slot present: {want}", want in names)
    profiles = [str(c.profile_dir(layout)) for c in slots]
    ports = [c.debug_port for c in slots if c.debug_port]
    check("every slot owns a distinct profile dir",
          len(profiles) == len(set(profiles)))
    check("every CDP slot owns a distinct debug port",
          len(ports) == len(set(ports)))
    check("WebKit slot does not pretend to have a CDP port",
          all((c.debug_port is None) or c.supports_cdp for c in slots))

    offline = login_check.evaluate_channel_evidence(
        slots[0], {"profile_present": True, "debug_port_listening": True,
                   "cookie_count": 0})
    check("login check refuses to say OK without cookie evidence",
          offline[0] == "OFFLINE" and "cookie" in offline[1], offline[1])
    online = login_check.evaluate_channel_evidence(
        slots[0], {"profile_present": True, "debug_port_listening": True,
                   "cookie_count": 2, "backend_reachable": True,
                   "backend_status": 200, "redirected_to_login": False})
    check("login check reports cookie + backend as evidence",
          online[0] == "ONLINE" and "cookie 2" in online[1], online[1])

    state = panel.build_state(layout, slots, tcp=lambda h, p: False)
    html = panel.render_html(state)
    check("panel renders a status card per platform",
          all(c.platform_name in html for c in slots))
    check("panel offers one-click login and re-check",
          "开始登录" in html and "我登录好了" in html)
    handlers = panel.PanelHandlers(layout, slots,
                                   opener=lambda ch: {"message": "x", "reason": "OPENED"},
                                   checker=lambda ch: {"status": "ONLINE", "reason": "y"},
                                   tcp=lambda h, p: False)
    check("panel /api/open routes to the slot opener",
          handlers.open(slots[0].channel_id)["result"]["reason"] == "OPENED")
    check("panel refuses an unknown channel",
          "error" in handlers.open("does-not-exist"))

    # ---- 1.2.20 publish cadence ------------------------------------------
    print("== publish cadence (1.2.20) ==")
    from winport import schedule as schedule_mod
    cadence_settings = schedule_mod.normalize_schedule_settings(
        load_settings(layout.settings_file))
    check("cadence default windows cover the whole day (1.2.205)",
          cadence_settings["windows"] == [["00:00", "23:59"]],
          str(cadence_settings["windows"]))
    check("cadence defaults leave the per-account daily cap unlimited",
          cadence_settings["max_per_day_per_account"] == 0)
    check("cadence backoff doubles from 30 minutes and caps at six hours",
          schedule_mod.backoff_delay(0, settings=load_settings(layout.settings_file)) == 30
          and schedule_mod.backoff_delay(4, settings=load_settings(layout.settings_file)) == 360)
    check("panel renders the publish-cadence controls",
          "发布节奏" in html and "saveCadence" in html
          and "每天每个号发几条" in html)

    # ---- 1.1.0 real Chrome -------------------------------------------------
    print("== real Chrome (source/extra-requirements-chrome.md) ==")
    from winport import chrome as chrome_mod
    fake_chrome = tmp / "Google" / "Chrome" / "Application" / "chrome.exe"
    fake_chrome.parent.mkdir(parents=True, exist_ok=True)
    fake_chrome.write_bytes(b"MZ")
    plan = browser_mod.plan_open(layout, slots[0], chrome_path=str(fake_chrome))
    check("per-slot command carries --user-data-dir + --remote-debugging-port",
          plan["error"] is None
          and any("--user-data-dir=" in str(a) for a in plan["command"])
          and any("--remote-debugging-port=" in str(a) for a in plan["command"])
          and slots[0].login_url in plan["command"],
          " ".join(str(a) for a in plan["command"]) or str(plan["error"]))
    missing = browser_mod.plan_open(layout, slots[0],
                                    chrome_path=str(tmp / "nope" / "chrome.exe"),
                                    edge_path=str(tmp / "nope" / "msedge.exe"))
    check("no Chrome -> the caller gets the \"需要 Google Chrome\" sentence "
          "(official installer + official page)",
          missing["error"] is not None
          and chrome_mod.CHROME_OFFLINE_DOWNLOAD_URL in missing["error"]
          and "需要 Google Chrome" in missing["error"],
          (missing["error"] or "")[:110])
    check("no in-package Chromium is consulted any more",
          not hasattr(browser_mod, "chromium_executable"))
    args = chrome_mod.build_launch_args("C:/chrome.exe", "C:/prof/env_1", 9224,
                                        "https://creator.douyin.com/")
    check("launch never uses the customer's default profile dir",
          "--user-data-dir=C:/prof/env_1" in args
          and not any("Default" in a for a in args))
    # The flag is one comma-separated --disable-features list, so an exact
    # list-element comparison is the wrong probe: parse it the same way the
    # runtime does (proc.has_disable_feature) before judging reuse safety.
    from winport import proc as proc_mod
    check("Chrome launch disables the native external-protocol dialog",
          proc_mod.has_disable_feature(args, "ExternalProtocolDialog"),
          " ".join(args))
    check("Chrome version floor is recorded", chrome_mod.MIN_CHROME_MAJOR >= 100)

    # ---- 1.1.0 environments ------------------------------------------------
    print("== environments (source/env-design.md) ==")
    from winport import environments as env_mod
    envs, channels = platforms.load_registry(layout)
    check("four phase-1 environments are registered",
          [e.env_id for e in envs] == ["env_1", "env_2", "env_wechat", "env_xhs"],
          str([e.env_id for e in envs]))
    check("env ports are 9224..9227",
          [e.debug_port for e in envs] == [9224, 9225, 9226, 9227])
    check("environments config validates clean",
          env_mod.validate_environments(envs) == [])
    check("every environment has a reserved (empty) proxy field",
          all(e.proxy is None for e in envs))
    shared = {e.env_id: {str(c.profile_dir(layout)) for c in channels
                         if c.env_id == e.env_id} for e in envs}
    check("each environment owns exactly one profile dir",
          all(len(v) == 1 for v in shared.values()), str(shared))
    state = panel.build_state(layout, channels, envs=envs, tcp=lambda h, p: False,
                              chrome_report={"found": False, "summary": "x"})
    check("panel renders one card per opened environment (抖音/星图 only)",
          len(state["environments"]) == 2)
    html_env = panel.render_html(state)
    check("panel shows each platform light inside its environment card",
          "data-env=\"env_1\"" in html_env and "data-row=\"env_1:douyin\"" in html_env)
    check("panel hides WeChat / Xiaohongshu instead of greying them out",
          'data-env="env_wechat"' not in html_env
          and 'data-env="env_xhs"' not in html_env
          and "微信（视频号/公众号）、小红书本版未开通" in html_env)

    seen = []
    dropped = health.notify_transitions(
        [{"channel_id": slots[0].channel_id, "status": "ONLINE"},
         {"channel_id": slots[1].channel_id, "status": "ONLINE"}],
        [{"channel_id": slots[0].channel_id, "status": "OFFLINE",
          "platform_name": slots[0].platform_name, "nickname": "n", "reason": "r"},
         {"channel_id": slots[1].channel_id, "status": "ONLINE"}],
        lambda t, m: seen.append(t))
    check("only the dropped platform is alerted",
          len(seen) == 1 and len(dropped) == 1)

    # ---- G4.1: the shortcut icon exists and is a real ICO -------------------
    print("== G4.1 shortcut icon ==")
    ico = ROOT / "assets" / "app.ico"
    check("assets/app.ico ships", ico.exists())
    if ico.exists():
        raw = ico.read_bytes()
        check("app.ico is a valid multi-size ICO",
              raw[:4] == b"\x00\x00\x01\x00" and raw[4] | (raw[5] << 8) >= 1,
              f"{raw[4] | (raw[5] << 8)} sizes, {len(raw)} bytes")
    check("make-shortcut.ps1 uses the icon and says so when it is missing",
          "assets\\app.ico" in read_script_text(
              ROOT / "scripts" / "make-shortcut.ps1"))

    # ---- G3 licensing: per-号 counting --------------------------------------
    print("== per-account authorisation ==")
    from winport.license import account_authorization, account_quota
    seats = {"accounts": [{"account_id": "douyin-pilot-001", "platform": "douyin"},
                          {"account_id": "xingtu-pilot-001", "platform": "xingtu",
                           "enabled": False}],
             "quota": {"accounts": 5}}
    check("a seat that is not in the licence is refused",
          account_authorization(seats, "shipinhao-pilot-001")["enabled"] is False)
    check("a disabled seat does not affect its neighbours",
          account_authorization(seats, "xingtu-pilot-001")["enabled"] is False
          and account_authorization(seats, "douyin-pilot-001")["enabled"] is True)
    check("seats are counted per platform",
          account_quota(seats)["per_platform"] == {"douyin": 1, "xingtu": 1})

    # ---- 1.2.0: A-E (mainstream-tool improvements) -------------------------
    print("== A dry-run / B evidence / C idempotency / D state / E expiry ==")
    from winport import dryrun as dryrun_mod
    from winport import evidence as evidence_mod
    from winport import idempotency as idem_mod
    from winport import state_machine as sm_mod
    piece = {"title": "t", "copy_body": "c", "sha256": "a" * 64,
             "topics": ["#x"], "covers": {}}
    plan = {"plan_id": "P1", "env_id": "env_1", "channel_id": "env_1:douyin",
            "account_id": "env_1:douyin", "platform": "douyin",
            "schedule_at": (datetime.now(timezone.utc)
                            + timedelta(hours=4)).isoformat(),
            "piece": piece,
            "compliance": {"content_hash": idem_mod.material_fingerprint(
                piece, account_id="env_1:douyin")}}
    preview = dryrun_mod.build_preview(plan, envs=envs, channels=channels,
                                       settings={})
    check("A dry-run passes a valid plan and mints a confirm token",
          preview["ok"] and preview["confirm_ref"].startswith("dryrun:"))
    bad_plan = json.loads(json.dumps(plan))
    bad_plan["piece"]["title"] = ""
    preview_bad = dryrun_mod.build_preview(bad_plan, envs=envs, channels=channels,
                                          settings={})
    check("A dry-run blocks a plan with no title",
          not preview_bad["ok"]
          and any(p["code"] == "TITLE_MISSING" for p in preview_bad["problems"]))

    ev_store = evidence_mod.EvidenceStore(layout, retention_days=14)
    rec = ev_store.capture("fill", env_id="env_1", error="x",
                           screenshot=b"png", html="<html>")
    check("B failure evidence keeps png+html+index with env in the name",
          rec["screenshot_available"] and rec["html_available"]
          and "env_1" in rec["screenshot"]
          and len(ev_store.list_entries()) == 1)
    check("B prune removes the evidence and the index row",
          ev_store.prune(days=0, now=time.time() + 5)["removed_count"] == 3
          and ev_store.list_entries() == [])

    at = "2026-10-16T14:37:00+08:00"
    key = idem_mod.publish_key("env_1", piece, at)
    check("C idempotency key = material + env + window (stable in a window)",
          key == idem_mod.publish_key("env_1", piece, "2026-10-16T14:47:00+08:00")
          and key != idem_mod.publish_key("env_2", piece, at)
          and key != idem_mod.publish_key("env_1", piece, "2026-10-16T16:05:00+08:00"))

    check("D state machine: never logged in vs dropped vs rate-limited",
          sm_mod.classify({"status": "OFFLINE",
                           "evidence": {"cookie_count": 0}})["state"] == "NOT_LOGGED_IN"
          and sm_mod.classify({"status": "OFFLINE",
                               "evidence": {"cookie_count": 0}},
                              previous={"state": "VALID"})["state"] == "DROPPED"
          and sm_mod.classify({"status": "UNVERIFIED",
                               "evidence": {"backend_status": 429}})["state"]
          == "RATE_LIMITED"
          and sm_mod.classify({"status": "OFFLINE", "reason": "账号封禁",
                               "evidence": {"backend_status": 403}})["state"]
          == "BANNED")
    alerts = sm_mod.env_alerts(
        [{"channel_id": "env_1:douyin", "account_state": "VALID"}],
        [{"channel_id": "env_1:douyin", "env_id": "env_1", "platform_name": "抖音",
          "account_state": "RATE_LIMITED", "account_state_label": "限流",
          "account_state_reason": "HTTP 429"}], env_labels={"env_1": "1号"})
    check("D alerts name only the affected environment",
          len(alerts) == 1 and alerts[0]["env_id"] == "env_1")

    from winport.license import expiry_notices, publish_gate
    if DEV_SIGN_AVAILABLE:
        seed2 = os.urandom(32)
        public2 = devsign.publickey(seed2)
        lic_soon = mint_dev_license(seed2, machine_id="M1", expires_at=(
            datetime.now(timezone.utc) + timedelta(days=3)).isoformat())
        check("E expiry notice fires the 7-day threshold",
              expiry_notices(lic_soon)["level"] == "7"
              and not expiry_notices(lic_soon)["expiry_blocks_publish"])
        lic_dead = mint_dev_license(seed2, machine_id="M1", grace_days=14,
                                    expires_at=(datetime.now(timezone.utc)
                                                - timedelta(days=40)).isoformat())
        gate = publish_gate(lic_dead, public2, "M1")
        check("E expired licence blocks publish but not collect/data",
              gate["publish_allowed"] is False
              and gate["collect_allowed"] is True)
    else:
        skip("E licence expiry notices",
             "devsign unavailable in this package")

    # ---- 1.2.1 self-service install: machine code + offline licence import ---
    print("== 1.2.1 self-service install chain ==")
    from winport.cli import known_commands
    commands = known_commands()
    check("`winport machine-code` subcommand exists", "machine-code" in commands)
    check("`winport license-import` subcommand exists", "license-import" in commands)

    self_tmp = Path(tempfile.mkdtemp(prefix="winport-selfserve-"))
    program = self_tmp / "program"
    (program / "config").mkdir(parents=True, exist_ok=True)
    lay3 = Layout(program=program, data=self_tmp / "data",
                  system=self_tmp / "system").ensure()

    info = onboard.machine_code_info(
        components={"machine_guid": "G", "volume_serial": "S"})
    mc_path = onboard.write_machine_code(lay3, info=info)
    check("机器码 written to <logs>/机器码.txt",
          mc_path.name == "机器码.txt" and mc_path.parent == lay3.logs)
    mc_text = mc_path.read_text(encoding="utf-8")
    check("机器码 file carries the code + the 3 next steps",
          info["machine_id"] in mc_text and "license.json" in mc_text)

    if DEV_SIGN_AVAILABLE:
        seed3 = os.urandom(32)
        public3 = devsign.publickey(seed3)
        (program / "config" / "vendor_pubkey.hex").write_text(
            public3.hex(), encoding="utf-8")
        fp3 = {"machine_id": "MACHINE-SELF", "weak": False,
               "components": {"machine_guid": "G", "volume_serial": "S"},
               "stable_component_count": 2, "platform": "windows"}
        future = (datetime.now(timezone.utc) + timedelta(days=60)).isoformat()
        good = mint_dev_license(seed3, machine_id=fp3["machine_id"],
                                expires_at=future)
        good_path = self_tmp / "license.json"
        good_path.write_text(json.dumps(good, ensure_ascii=False),
                             encoding="utf-8")
        res = onboard.import_license(lay3, good_path, fingerprint_info=fp3,
                                     allow_test_injection=True,
                                     public_key_override=public3)
        check("licence import installs a valid, correctly-bound licence",
              res["installed"] and res["status"] == "OK"
              and lay3.license_file.exists(), res.get("reason", ""))

        other = mint_dev_license(seed3, machine_id="SOMEONE-ELSE",
                                 expires_at=future)
        other_path = self_tmp / "other.json"
        other_path.write_text(json.dumps(other, ensure_ascii=False),
                              encoding="utf-8")
        lay4 = Layout(program=program, data=self_tmp / "data4",
                      system=self_tmp / "system4").ensure()
        res2 = onboard.import_license(lay4, other_path, fingerprint_info=fp3,
                                      allow_test_injection=True,
                                      public_key_override=public3)
        check("licence import refuses another machine's licence",
              (not res2["installed"]) and res2["status"] == "MACHINE_MISMATCH"
              and not lay4.license_file.exists())

        forged = mint_dev_license(os.urandom(32),
                                  machine_id=fp3["machine_id"],
                                  expires_at=future)
        forged_path = self_tmp / "forged.json"
        forged_path.write_text(json.dumps(forged, ensure_ascii=False),
                               encoding="utf-8")
        res3 = onboard.import_license(lay4, forged_path, fingerprint_info=fp3,
                                      allow_test_injection=True,
                                      public_key_override=public3)
        check("licence import refuses a bad-signature licence",
              (not res3["installed"])
              and res3["status"] == "SIGNATURE_INVALID")
    else:
        for name in (
                "licence import installs a valid, correctly-bound licence",
                "licence import refuses another machine's licence",
                "licence import refuses a bad-signature licence"):
            skip(name, "devsign unavailable in this package")

    # the Windows entry points the customer actually double-clicks
    scripts = ROOT / "scripts"
    for name, needle in (("show-machine-code.ps1", "winport"),
                         ("show-machine-code.ps1", "machine-code"),
                         ("import-license.ps1", "license-import"),
                         ("panel.ps1", "agent-ready.json"),
                         ("panel.ps1", "healthz")):
        text = read_script_text(scripts / name)
        check(f"{name} drives `{needle}`", needle in text)
    for name in ("show-machine-code.vbs", "import-license.vbs"):
        check(f"{name} wrapper ships", (scripts / name).exists())
    shim = scripts / "import-license-shortcut.cmd"
    check("import-license-shortcut.cmd ships for drag-and-drop",
          shim.exists() and '-Src "%~1"' in read_script_text(shim))
    boot = read_script_text(scripts / "bootstrap.ps1")
    check("bootstrap.ps1 shows + writes the machine code",
          "machine-code" in boot and "机器码.txt" in boot)
    # 1.2.44 intentionally accepts a .cmd fallback and validates every entry it
    # finds; the old check still demanded the pre-1.2.44 "exactly one .lnk"
    # contract and a magic log string, so it failed a correct script.
    check("bootstrap.ps1 verifies every desktop entry and its target",
          "漫剧发布助手*" in boot and "Get-ChildItem" in boot
          and "$desktopEntries.Count -lt 1" in boot
          and "'.lnk', '.cmd'" in boot
          and "快捷方式目标不存在" in boot)
    ms = scripts / "make-shortcut.ps1"
    if ms.exists():
        mst = read_script_text(ms)
        check("make-shortcut.ps1 creates only the single fixed shortcut",
              mst.count('File = "$Name') == 1
              and "foreach ($env in $envs)" not in mst)
        check("make-shortcut.ps1 uses powershell.exe, not wscript/.vbs targets",
              "Target = 'powershell.exe'" in mst
              and "$lnk.TargetPath = 'wscript.exe'" not in mst
              and "import-license-shortcut.cmd" in mst)
    for name in ("bootstrap.bat", "show-machine-code.bat", "import-license.bat"):
        check(f"{name} entry point ships", (ROOT / name).exists())
    check("a license drop folder README ships",
          (ROOT / "license" / "README.txt").exists())

    # ---- 1.2.2 browser: Google Chrome ONLY (no Edge/Chromium fallback) -----
    # source/brief-chrome-only-20261016.md: "浏览器只认 Google Chrome ... 绝不
    # 静默用 Edge / Chromium / 其他浏览器顶替执行".  Edge is demoted to a
    # diagnostics line ("你机器上的 Edge 不参与运行") and never runs.
    print("== browser: Google Chrome only (no Edge/Chromium fallback) ==")
    from winport import chrome as cb

    t1_env = {"LOCALAPPDATA": str(tmp / "t1" / "localappdata")}
    fake_edge_only = tmp / "t2" / "pfx86" / "Microsoft" / "Edge" / "Application"
    fake_edge_only.mkdir(parents=True, exist_ok=True)
    (fake_edge_only / "msedge.exe").write_bytes(b"MZ")
    t2_env = {"PROGRAMFILES(X86)": str(tmp / "t2" / "pfx86")}
    chrome_at = Path(t1_env["LOCALAPPDATA"]) / "Google" / "Chrome" / "Application"
    chrome_at.mkdir(parents=True, exist_ok=True)
    (chrome_at / "chrome.exe").write_bytes(b"MZ")

    r1 = cb.resolve_browser(env=t1_env)
    check("Chrome present -> used (tier 1, engine=chrome)",
          r1["found"] and r1["engine"] == "chrome" and r1["tier"] == 1,
          f"engine={r1.get('engine')} tier={r1.get('tier')} path={r1.get('path')}")

    r2 = cb.resolve_browser(env=t2_env, explicit=str(tmp / "nope" / "chrome.exe"))
    check("no Chrome (Edge present) -> STOP, Edge is NEVER used",
          (not r2["found"]) and r2["engine"] is None and r2["tier"] is None
          and r2["needs_chrome"],
          f"engine={r2.get('engine')} tier={r2.get('tier')} found={r2['found']}")

    check("Edge detected only as diagnostics ('不参与运行'), not as an engine",
          r2["edge"]["present"] and "不参与运行" in r2["edge"]["note"]
          and "不参与运行" in r2["reason"]
          and cb.ENGINE_EDGE not in cb.ENGINE_TIERS
          and [t["engine"] for t in r2["tiers"]] == ["chrome"],
          f"edge.present={r2['edge']['present']} tiers="
          f"{[t['engine'] for t in r2['tiers']]} ENGINE_TIERS={cb.ENGINE_TIERS}")

    r3 = cb.resolve_browser(env={}, explicit=str(tmp / "nope" / "chrome.exe"),
                            edge_explicit=str(tmp / "nope" / "msedge.exe"))
    check("no Chrome, no Edge -> same stop + official installer + official page",
          (not r3["found"]) and cb.CHROME_OFFLINE_DOWNLOAD_URL in r3["reason"]
          and cb.CHROME_BUNDLED_INSTALLER_DISPLAY in r3["reason"]
          and "需要 Google Chrome" in r3["reason"],
          r3["reason"][:150])
    check("the stop message never offers Edge/Chromium as a substitute",
          "Edge" not in r3["reason"].split("顺带说明")[0].replace("不用 Microsoft Edge", "")
          and "Chromium" not in r3["reason"].split("顺带说明")[0].replace("Chromium 或", ""))

    check("Chrome launch argv keeps isolation, no Edge-only flags",
          "--user-data-dir=C:/prof/env_1" in cb.build_launch_args(
              "C:/Google/chrome.exe", "C:/prof/env_1", 9224)
          and "--remote-debugging-port=9224" in cb.build_launch_args(
              "C:/Google/chrome.exe", "C:/prof/env_1", 9224)
          and not any("msEdgeFirstRunExperience" in a for a in cb.build_launch_args(
              "C:/Google/chrome.exe", "C:/prof/env_1", 9224)))
    raised = False
    try:
        cb.build_launch_args("C:/Edge/msedge.exe", "C:/prof/env_1", 9224)
    except cb.ConfigError:
        raised = True
    check("build_launch_args REFUSES msedge.exe (no silent substitution)", raised,
          "ConfigError raised for msedge.exe" if raised else "NO error -> substitution possible")
    check("engine identity helpers still name Edge, but only for detection",
          cb.engine_for_path("C:/Edge/msedge.exe") == "edge"
          and cb.engine_for_path("C:/Chrome/chrome.exe") == "chrome")
    check("CDP identity maps Edg/ -> edge and Chrome/ -> chrome (report only)",
          cb.browser_engine_of("Edg/130.0.0.0") == "edge"
          and cb.browser_engine_of("Chrome/130.0.0.0") == "chrome"
          and cb.browser_engine_of("HeadlessChrome/120") is None)
    check("the bundled official Chrome installer is declared in code",
          cb.CHROME_BUNDLED_INSTALLER_REL.endswith("ChromeStandaloneSetup64.exe")
          and cb.CHROME_BUNDLED_SOURCE_URL.startswith("https://dl.google.com/"))

    # ---- 1.2.21 publish-driver guard (GPT_REVIEW_1.2.20 items 2-7) --------
    print("== 1.2.21 publish-driver guard ==")
    try:
        assert_selector_policy()
        selector_policy_ok = True
    except RuntimeError as exc:
        selector_policy_ok = False
        selector_policy_reason = str(exc)
    check("publish selector policy has zero class / fuzzy-text candidates",
          selector_policy_ok, selector_policy_reason if not selector_policy_ok else "")
    check("publish button uses exact semantic selectors only",
          all("[class" not in s and ":has-text(" not in s
              for s in PUBLISH_BUTTON_SELECTORS),
          str(PUBLISH_BUTTON_SELECTORS))
    bad_selectors = publish_guard.validate_selectors([
        "[class*='publish'] button", "button:has-text('发布')"])
    check("class and fuzzy negative selectors are rejected",
          len(bad_selectors) == 2)
    with tempfile.TemporaryDirectory() as tmp:
        attempt = publish_guard.make_upload_attempt("env_1", "env_1:douyin",
                                                    Path(tmp) / "a.mp4")
        attempt.size = 1
        attempt.sha256 = "a" * 64
        attempt.input_set = True
        attempt.request_observed = False
        attempt.title_preexisting = True
        check("upload guard rejects preexisting title + missing request",
              "TITLE_PREEXISTING" in publish_guard.upload_failure_reasons(attempt)
              and "NO_UPLOAD_REQUEST" in publish_guard.upload_failure_reasons(attempt))
    with tempfile.TemporaryDirectory() as tmp:
        attempt = publish_guard.make_upload_attempt("env_1", "env_1:douyin",
                                                    Path(tmp) / "a.mp4")
        attempt.size = 1
        attempt.sha256 = "a" * 64
        attempt.input_set = True
        attempt.request_observed = True
        attempt.request_ids = ["u"]
        attempt.resource_id = "R"
        attempt.resource_observed_at = 1
        attempt.transcode_done = True
        attempt.transcode_done_at = 2
        attempt.button_enabled_before_done = True
        check("transcode guard rejects button-enabled-before-done",
              "BUTTON_ENABLED_BEFORE_DONE" in publish_guard.transcode_failure_reasons(attempt))
    ok_bind, bind_reasons = publish_guard.validate_work_id_binding(
        "1111111111111111111",
        publish_guard.UploadAttempt(attempt_id="A", env_id="env_1",
                                    account_id="env_1:douyin", local_path="x",
                                    resource_id="R", submit_started_at=1),
        [{"work_id": "1111111111111111111", "account_id": "env_1:douyin",
          "title": "标题", "resource_id": "R", "published_at": 2}],
        title="标题", account_id="env_1:douyin")
    check("work id binding accepts exactly one bound candidate", ok_bind,
          "; ".join(bind_reasons))
    ok_bind2, _ = publish_guard.validate_work_id_binding(
        "1111111111111111111",
        publish_guard.UploadAttempt(attempt_id="A", env_id="env_1",
                                    account_id="env_1:douyin", local_path="x",
                                    resource_id="R", submit_started_at=1),
        [{"work_id": "1111111111111111111", "account_id": "env_2:douyin",
          "title": "标题", "resource_id": "R", "published_at": 2}],
        title="标题", account_id="env_1:douyin")
    check("work id binding rejects another account link", not ok_bind2)
    h = humanize_mod.Humanizer(
        humanize_mod.PaceProfile.from_dict(
            {"pace": "medium", "step_pause_ms": [1, 2],
             "think_pause_ms": [1, 2], "type_char_ms": [1, 2],
             "type_block_min": 3, "type_block_max": 3,
             "type_block_pause_ms": [1, 2], "typo_probability": 0.0,
             "hover_ms": [1, 2], "landing_jitter_px": 2,
             "bezier_points": [3, 5], "scroll_segments": [2, 3],
             "page_load_ms": [1, 2], "pre_submit_ms": [1, 2],
             "poll_jitter_ms": [1, 2], "seed_shift": 0}, env_id="env_1"))
    h.step_pause()
    h.think_pause()
    h.poll_pause()
    check("humanize delay records carry sampled/realised and validate",
          humanize_mod.validate_delay_records(h.delay_log) == [])
    hold_path = layout.run / "publish_holds.json"
    hold_store = publish_guard.PublishHoldStore(hold_path)
    hold_store.set_hold("ch", publish_guard.CAPTCHA, {"screenshot": "s"})
    hold_store.clear_if_online("ch", "VALID")
    check("fault hold is persistent and not released by non-ONLINE",
          publish_guard.PublishHoldStore(hold_path).get("ch") is not None)
    hold_store.clear_if_online("ch", "ONLINE")
    check("ONLINE does not release a fault hold",
          publish_guard.PublishHoldStore(hold_path).get("ch") is not None)
    hold_store.clear_manual("ch", reason="fixture operator resolved")
    check("explicit manual resolution clears hold", hold_store.get("ch") is None)
    scan = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "scan_forbidden_patterns.py"),
         "--root", str(ROOT), "--json"],
        capture_output=True, **text_mode_options())
    scan_ok = scan.returncode == 0
    check("runtime literal forbidden-pattern scan (not variable-delay proof)", scan_ok,
          (scan.stdout or scan.stderr)[:500])

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("-" * 60)
    print(f"STATIC VERIFY: {passed}/{total} checks passed, {len(SKIPS)} skipped")
    print("-" * 60)
    return 0 if passed == total else 2


if __name__ == "__main__":
    raise SystemExit(main())
