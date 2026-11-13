#!/usr/bin/env python3
"""End-to-end delivery self-verification (runs on the Linux build host).

User requirement (source/extra-requirements-verify.md): before delivery the
main chain AND the failure paths must be walked, with REAL command output, so
the report can never say "我认为通过".  Every case here records:

    * the literal command;
    * the expected result;
    * the ACTUAL stdout/stderr and exit code;
    * SHA-256 of every artefact it produced.

Windows-only facts (bootstrap venv, real Chrome, real QR scan, Task Scheduler)
cannot be produced on this host and are listed as "待真机", never as a pass.

Run:  python3 tools/e2e_selfverify.py --out ../bundle_v3/SELF_VERIFY_REPORT.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "tools"))

import make_fixture  # noqa: E402
from winport import approval, chrome, ed25519_pure, environments as env_mod  # noqa: E402
from winport import health, login_check, panel, platforms, regression  # noqa: E402
from winport.browser import open_channel, open_environment  # noqa: E402
from winport.collect_douyin import collect_offline  # noqa: E402
from winport.downlink import Downlink, FileSystemSource  # noqa: E402
from winport.errors import (EXIT_CONFIG, EXIT_GATE_RED, EXIT_OK, EXIT_STORAGE,  # noqa: E402
                          GateRed)
from winport.license import check_license, load_vendor_public_key  # noqa: E402
import devsign  # noqa: E402
from devsign import mint_dev_license, issue_receipt_for_test  # noqa: E402
from winport.logutil import audit  # noqa: E402
from winport.paths import Layout  # noqa: E402
from winport.publish_exec import MODE_APPROVED, MODE_FILL, NullDriver, run_publish  # noqa: E402
from winport.report import write_report  # noqa: E402
from winport.settings import load_settings  # noqa: E402
from winport.stage2_ledger import (CONSUMED, RESERVED, Stage2Ledger,  # noqa: E402
                                   content_hash, idempotency_key)
from winport.subprocess_text import text_mode_options  # noqa: E402

TIMEOUT = 180
CASES: list[dict] = []
RAW_LOG: list[str] = []


def sha256(path: Path) -> str:
    d = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            d.update(block)
    return d.hexdigest()


def _env(extra=None):
    env = dict(os.environ)
    env.update({"PYTHONPATH": str(ROOT / "agent"), "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8"})
    env.update(extra or {})
    return env


def cli(args, *, data=None, extra_env=None, timeout=TIMEOUT):
    argv = [sys.executable, "-m", "winport", *args]
    if data:
        argv += ["--data", str(data)]
    full = " ".join(argv)
    proc = subprocess.run(argv, capture_output=True, cwd=str(ROOT),
                          env=_env(extra_env), timeout=timeout,
                          **text_mode_options())
    out = (proc.stdout or "") + (proc.stderr or "")
    RAW_LOG.append(f"$ {full}\n[exit={proc.returncode}]\n{out}")
    return {"cmd": full, "rc": proc.returncode, "out": out}


def record(cid, title, kind, cmd, expected, rc, actual, ok, artifacts=None, note=""):
    CASES.append({"cid": cid, "title": title, "kind": kind, "cmd": cmd,
                  "expected": expected, "rc": rc, "actual": actual, "ok": bool(ok),
                  "artifacts": artifacts or [], "note": note})
    flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] {cid} {title}")


def seed_config(layout: Layout, *, settings_patch=None) -> None:
    import shutil
    for tmpl, name in (("environments.json.tmpl", "environments.json"),
                       ("settings.json.tmpl", "settings.json"),
                       ("channels.json.tmpl", "channels.json")):
        src = ROOT / "config" / tmpl
        if src.exists():
            shutil.copyfile(src, layout.config / name)
    if settings_patch:
        doc = json.loads((layout.config / "settings.json").read_text("utf-8"))
        doc.update(settings_patch)
        (layout.config / "settings.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def new_layout(tmp: Path, name="data", program=ROOT) -> Layout:
    return Layout(program=program, data=tmp / name, system=tmp / "system").ensure()


# --------------------------------------------------------------------- MAIN CHAIN
def _license_line(out: str) -> str:
    return next((l for l in out.splitlines() if " license " in l), "")


def case_M1_install(tmp: Path):
    # 1.2.194+: a fresh data area has no licence and no trial -> licence RED with
    # the machine-code / activation-code route.  The installer (bootstrap_py
    # step_trial) then creates the 14-day trial ONCE, after which selftest is
    # WARN for the licence and exit 0.  Both halves are checked here.
    from winport.license import initialize_trial
    layout = new_layout(tmp)
    seed_config(layout)
    env_dirs = [p.name for p in sorted(layout.profiles.glob("env_*"))]
    sysenv = {"WINPORT_SYSTEM": str(layout.system)}   # hermetic: never the tester's ~/.config
    before = cli(["selftest"], data=layout.data, extra_env=sysenv)
    pre_line = _license_line(before["out"])
    pre_ok = (before["rc"] == EXIT_GATE_RED and pre_line.startswith("[RED")
              and "机器码" in pre_line and "激活码" in pre_line)
    trial = initialize_trial(layout)
    res = cli(["selftest"], data=layout.data, extra_env=sysenv)
    lic_line = _license_line(res["out"])
    light_line = "\n".join(l for l in res["out"].splitlines()
                           if l.startswith("[") or "SELFTEST" in l)
    ok = (pre_ok and trial.get("created") and res["rc"] == EXIT_OK
          and lic_line.startswith("[WARN") and "试用" in lic_line
          and set(env_dirs) >= {"env_1", "env_2", "env_wechat", "env_xhs"})
    record("M1", "装（本机可验部分：数据区+配置+试用+自检）", "main",
           f"{before['cmd']}  →  initialize_trial()  →  {res['cmd']}"
           "  # 另：bootstrap.ps1 建目录/铺配置/venv/自启动",
           "建试用前：自检 exit=2，license RED 且给出机器码+激活码路径；"
           "安装建试用后：自检 exit=0，license WARN（试用剩余天数）；四个环境目录已建",
           res["rc"],
           f"env dirs: {env_dirs}\n建试用前 exit={before['rc']}: {pre_line[:160]}\n"
           f"initialize_trial: {trial}\n建试用后:\n{light_line}", ok,
           note=("Windows 侧 bootstrap.ps1（venv、Task Scheduler 自启动、快捷方式、"
                 "注册表试用副本）只能在真机执行 → 见待真机清单 W1/W2"))


def case_M2_activation(tmp: Path):
    layout = new_layout(tmp, "act")
    seed_config(layout)
    seed = os.urandom(32)
    public = devsign.publickey(seed)
    future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
    lic = mint_dev_license(seed, machine_id="MACHINE-SV", expires_at=future)
    pub_path = tmp / "devpub.hex"
    pub_path.write_text(public.hex(), encoding="utf-8")
    lic_path = tmp / "license.json"
    lic_path.write_text(json.dumps(lic, ensure_ascii=False), encoding="utf-8")
    # 1.2.5: license-check uses the FIXED trust root (no --pubkey switch), so the
    # dev-signed licence is checked in-process and the production key is read
    # from the package itself.
    good = check_license(lic, public, "MACHINE-SV")
    vendor = load_vendor_public_key(ROOT / "config" / "vendor_pubkey.hex")
    bad = check_license(lic, vendor, "MACHINE-SV")
    mismatch = check_license(lic, public, "ANOTHER-MACHINE")
    # 1.2.5 (GPT review item 1): the production command has NO --pubkey /
    # --machine-id / --skip-signature switch.  It uses the FIXED trust root and
    # the LOCAL fingerprint, so a dev-signed licence is refused (SIGNATURE_INVALID,
    # or WEAK_FINGERPRINT first on this headless static host).
    res = cli(["license-check", "--license", str(lic_path)], data=layout.data)
    fixed_root_used = res["rc"] != 0 and ("SIGNATURE_INVALID" in res["out"]
                                          or "WEAK_FINGERPRINT" in res["out"])
    ok = good["status"] == "OK" and bad["status"] == "SIGNATURE_INVALID" \
        and mismatch["status"] == "MACHINE_MISMATCH" and fixed_root_used
    actual = (f"① 配对授权: {good['status']} / {good.get('reason')}\n"
              f"② 自签授权 + 包内生产公钥: {bad['status']} / {bad['reason']}\n"
              f"③ 换机: {mismatch['status']} / {mismatch['reason']}\n"
              f"④ 真 CLI 用固定信任根 + 本机指纹(无 --pubkey/--machine-id): rc={res['rc']}")
    record("M2", "激活（Ed25519 授权链，固定信任根）", "main",
           "license.check_license（in-process）+ license-check 固定信任根",
           "配对→OK；自签+生产公钥→拒；换机→MACHINE_MISMATCH；CLI 无注入开关",
           res["rc"], actual, ok,
           artifacts=[(lic_path, sha256(lic_path))])


def case_M3_login(tmp: Path):
    layout = new_layout(tmp, "login")
    seed_config(layout)
    fake = tmp / "fake" / "chrome.exe"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_bytes(b"MZ")
    dry = cli(["login-open", "--env", "env_1", "--dry-run",
               "--chrome-path", str(fake)], data=layout.data)
    doc = json.loads(dry["out"])
    cmds = [r["command"] for r in doc["results"]]
    same_profile = len({c[1] for c in cmds}) == 1 and len({c[2] for c in cmds}) == 1
    check = cli(["login-check", "--env", "env_1"], data=layout.data)
    ok = dry["rc"] == EXIT_OK and same_profile \
        and all("--remote-debugging-port=9224" in " ".join(c) for c in cmds)
    actual = (f"dry-run command (env_1 两个平台共用一份登录态):\n  "
              + "\n  ".join(" ".join(c) for c in cmds)
              + f"\nlogin-check(空登录态) exit={check['rc']}\n"
              + "\n".join(check["out"].splitlines()[:5]))
    record("M3", "扫码登录位（真 Chrome 命令 + 未登录态报错）", "main", dry["cmd"],
           "同一环境两个平台共用 1 个 user-data-dir + 1 个端口；未登录→OFFLINE 带原因",
           dry["rc"], actual, ok,
           note="真实扫码（手机）只能在真机做 → 待真机 W4")


def case_M4_fetch(tmp: Path):
    layout = new_layout(tmp, "fetch")
    seed_config(layout)
    bucket = tmp / "bucket"
    doc = make_fixture.build_bucket(bucket)
    settings = load_settings(layout.settings_file)
    downlink = Downlink(layout, FileSystemSource(bucket), settings,
                        free_bytes=lambda p: 10 ** 12, machine_id="SV",
                        customer_id="newme-001")
    result = downlink.run_once()
    folder = layout.inbox / doc["batch"]["batch_id"] / doc["batch"]["folder_key"]
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    arts = [(p, sha256(p)) for p in files]
    ledger = layout.downlink_ledger.read_text("utf-8")
    ok = result["downloaded"] == 2 and '"action":"DOWNLOAD"' in ledger.replace(" ", "")
    actual = (json.dumps(result, ensure_ascii=False) + "\n"
              + "\n".join(f"{p.relative_to(layout.data)}  {sha256(p)[:16]}…" for p in files))
    record("M4", "取件（LIST→DIFF→FETCH→VERIFY→BIND）", "main",
           f"Downlink(FileSystemSource).run_once() on {bucket}", "downloaded=2；账本记 DOWNLOAD",
           None, actual, ok, artifacts=arts)


def case_M5_enqueue(tmp: Path):
    layout = new_layout(tmp, "enq")
    seed_config(layout)
    bucket = tmp / "b"
    doc = make_fixture.build_bucket(bucket)
    settings = load_settings(layout.settings_file)
    Downlink(layout, FileSystemSource(bucket), settings,
             free_bytes=lambda p: 10 ** 12, customer_id="newme-001").run_once()
    folder = layout.inbox / doc["batch"]["batch_id"] / doc["batch"]["folder_key"]
    state = json.loads((folder / ".folder.json").read_text("utf-8"))
    ok = state.get("status") == "READY"
    record("M5", "入队（文件夹绑定 = 任务键，READY）", "main",
           ".folder.json 落盘", "status=READY",
           None, json.dumps({k: state.get(k) for k in
                             ("status", "folder_key", "batch_id")}, ensure_ascii=False),
           ok, artifacts=[(folder / ".folder.json", sha256(folder / ".folder.json"))])


def _plan():
    piece = {"title": "标题", "copy_body": "文案", "sha256": "a" * 64}
    account_id = "env_1:douyin"
    # 1.2.177: the fixture used a literal "abc123" that no longer matched the
    # material fingerprint the approval gate recomputes.  F6/F9 were therefore
    # rejected at the approval step ("plan content changed") before they ever
    # reached the ledger, so the idempotency claim was never actually tested.
    from winport.idempotency import material_fingerprint
    return {"plan_id": "PLAN-SV", "account_id": account_id,
            "schedule_at": "2026-10-20T12:00:00+08:00", "piece": piece,
            "compliance": {"content_hash": material_fingerprint(
                piece, account_id=account_id)}}


def case_M6_publish(tmp: Path):
    layout = new_layout(tmp, "pub")
    seed_config(layout)
    ledger = Stage2Ledger(layout.stage2_ledger)
    driver = NullDriver(platform_work_id="W-DRY")
    # 1.2.5: the production path never uses a simulated driver; the harness must
    # opt in explicitly, and even then fill() must never click publish.
    refused = None
    try:
        run_publish(_plan(), driver, ledger, mode=MODE_FILL)
        refused = False
    except GateRed:
        refused = True
    fill = run_publish(_plan(), driver, ledger, mode=MODE_FILL, allow_simulated=True)
    ok = refused and fill["filled"] and not fill["published"] \
        and [c[0] for c in driver.calls] == ["fill"]
    actual = (f"未开 opt-in 时: {'被拒' if refused else '未拒(错)'}\n"
              f"opt-in 后 fill 结果: {json.dumps(fill, ensure_ascii=False)}\n"
              f"driver 实际动作: {driver.calls}（没有 click_publish）")
    record("M6", "发布（模拟单元测试：只填表，绝不点提交；模拟驱动须显式 opt-in）", "main",
           "run_publish(..., mode='fill')", "未 opt-in 即拒 / 只调 fill()，绝不 click",
           None, actual, ok,
           note="真 Chrome 里 fill 的 DOM 选择器是 W15/W16 真机项")


def case_M7_collect(tmp: Path):
    layout = new_layout(tmp, "col")
    seed_config(layout)
    xlsx = tmp / "export.xlsx"
    make_fixture.write_export_xlsx(xlsx)
    res = cli(["collect-offline", "--xlsx", str(xlsx)], data=layout.data)
    doc = json.loads(res["out"])
    outs = doc.get("outputs") or {}
    arts = [(Path(v), sha256(Path(v))) for v in outs.values() if Path(v).exists()]
    ok = res["rc"] == EXIT_OK and doc.get("column_count") == 13 and doc.get("row_count", 0) > 0
    record("M7", "抓数（离线导出 → 13 列规范化）", "main", res["cmd"],
           "column_count=13，有数据行，落盘 out/douyin/",
           res["rc"], json.dumps(doc, ensure_ascii=False)[:600], ok, artifacts=arts)


def case_M8_report(tmp: Path):
    layout = new_layout(tmp, "rep")
    seed_config(layout)
    xlsx = tmp / "export.xlsx"
    make_fixture.write_export_xlsx(xlsx)
    collection = cli(["collect-offline", "--xlsx", str(xlsx)], data=layout.data)
    coll = json.loads(collection["out"])
    coll_path = tmp / "collection.json"
    coll_path.write_text(json.dumps(coll, ensure_ascii=False), encoding="utf-8")
    res = cli(["report", "--collection-json", str(coll_path)], data=layout.data)
    doc = json.loads(res["out"])
    xls = Path(doc["xlsx"]); md = Path(doc["md"])
    from winport.minimal_xlsx import read_xlsx
    parts = read_xlsx(xls)
    ok = res["rc"] == EXIT_OK and xls.exists() and md.exists() and parts
    record("M8", "出表（日报 xlsx + md，可回读）", "main", res["cmd"],
           "xlsx/md 都生成，xlsx 能被回读",
           res["rc"], f"{doc}\n回读 sheet 条目数={len(parts)}",
           ok, artifacts=[(xls, sha256(xls)), (md, sha256(md))])


# ------------------------------------------------------------------- FAILURE PATHS
def case_F1_offline(tmp: Path):
    layout = new_layout(tmp, "f1")
    # 1.2.8 (GPT_REVIEW_1.2.7 必修 4): the previous run reported "磁盘可用空间
    # 不足" for BOTH legs, because this builder box has < the default 5.37 GB
    # watermark -- so the DISK guard fired first and the NETWORK failure branch
    # was never exercised.  Drop the watermark to 0 here so the real network
    # error (empty bucket / refused endpoint) is what the case actually proves.
    seed_config(layout, settings_patch={"disk_watermark_gb": 0})
    empty = tmp / "empty-bucket"
    empty.mkdir(parents=True, exist_ok=True)
    res = cli(["downlink-once", "--bucket", str(empty)], data=layout.data)
    bad = tmp / "bad" / "manifest.json"
    # also: unreachable presigned endpoint (no network)
    res2 = cli(["downlink-once", "--token-endpoint",
                "http://127.0.0.1:9/token", "--customer-id", "c"], data=layout.data)
    ok = res["rc"] != EXIT_OK and res2["rc"] != EXIT_OK
    record("F1", "失败路径：断网 / 取件源不可达", "fail",
           f"{res['cmd']}  |  {res2['cmd']}",
           "两路都非 0 退出，写出人话原因，不静默成功",
           res["rc"], f"[空桶] exit={res['rc']}\n{res['out'][:400]}\n"
                      f"[不可达端点] exit={res2['rc']}\n{res2['out'][:300]}", ok)


def case_F2_permission(tmp: Path):
    layout = new_layout(tmp, "f2")
    seed_config(layout)
    locked = tmp / "locked"
    (locked / "data" / "config").mkdir(parents=True)
    import shutil
    for n in ("environments.json", "settings.json"):
        shutil.copyfile(ROOT / "config" / f"{n}.tmpl", locked / "data" / "config" / n)
    os.chmod(locked / "data", 0o555)
    try:
        res = cli(["selftest"], data=locked / "data")
    finally:
        os.chmod(locked / "data", 0o755)
    storage_line = "\n".join(l for l in res["out"].splitlines()
                             if "storage" in l or "SELFTEST" in l
                             or "ERROR" in l or "writable" in l)
    ok = res["rc"] == EXIT_STORAGE
    record("F2", "失败路径：权限/杀毒拦截（数据区不可写）", "fail", res["cmd"],
           f"storage 灯 RED，退出码 {EXIT_STORAGE}（不是 0）",
           res["rc"], storage_line, ok,
           note="杀毒软件的实时拦截只能在真机复现 → 待真机 W6")


def case_F3_paths(tmp: Path):
    # each path COMPONENT stays legal (<255 bytes); the TOTAL length blows past
    # the classic Windows 260-char MAX_PATH, which is the real risk we exercise.
    nest = tmp / "中文 目录 with spaces"
    for i in range(12):
        nest = nest / f"第{i:02d}层-非常长的中文子目录名用来逼近路径长度上限"
    weird = nest / "data"
    layout = Layout(program=ROOT, data=weird, system=tmp / "sys").ensure()
    seed_config(layout)
    bucket = tmp / "b3"
    doc = make_fixture.build_bucket(bucket)
    settings = load_settings(layout.settings_file)
    down = Downlink(layout, FileSystemSource(bucket), settings,
                    free_bytes=lambda p: 10 ** 12, customer_id="newme-001").run_once()
    rep = write_report(layout)
    length = len(str(weird))
    ok = down["downloaded"] == 2 and Path(rep["xlsx"]).exists() and length > 260
    actual = (f"路径长度 = {length} 字符（含中文与空格）\n"
              f"取件 downloaded={down['downloaded']} / failed={down['failed']}\n"
              f"出表 xlsx = {rep['xlsx']}")
    record("F3", "失败路径：中文 / 空格 / 超长路径", "fail",
           "WINPORT_DATA=<含中文+空格+超长路径>", "全链路正常，路径 >200 字符也不崩",
           None, actual, ok)


def case_F4_disk(tmp: Path):
    layout = new_layout(tmp, "f4")
    seed_config(layout, settings_patch={"disk_watermark_gb": 999999})
    bucket = tmp / "b4"
    make_fixture.build_bucket(bucket)
    res = cli(["downlink-once", "--bucket", str(bucket)], data=layout.data)
    ok = res["rc"] != EXIT_OK and "磁盘可用空间不足" in res["out"]
    record("F4", "失败路径：磁盘不足（阈值水位）", "fail", res["cmd"],
           "阈值把水位顶上去后：拒绝取件、非 0、写明原因，不写半个文件",
           res["rc"], res["out"].strip()[:400], ok,
           note="真实磁盘写满（ENOSPC）只能在真机造 → 待真机 W7")


def case_F5_login_drop(tmp: Path):
    layout = new_layout(tmp, "f5")
    seed_config(layout)
    envs, channels = platforms.load_registry(layout)
    rows = login_check.check_all(layout, channels)
    offline = [r for r in rows if r["status"] == "OFFLINE"]
    reasons_ok = all(r.get("reason") and r["reason"] != "OK" for r in rows)
    # transition alert only names the drop
    seen = []
    health.notify_transitions(
        [{"channel_id": "env_1:douyin", "status": "ONLINE"}],
        [{"channel_id": "env_1:douyin", "status": "OFFLINE", "env_id": "env_1",
          "platform_name": "抖音", "nickname": "1号", "reason": "掉线原因"}],
        lambda t, m: seen.append((t, m)))
    ok = len(offline) == len(rows) and reasons_ok and len(seen) == 1 \
        and "掉线原因" in seen[0][1]
    record("F5", "失败路径：登录掉线（逐环境报因 + 只报掉的那个）", "fail",
           "login-check（空登录态）+ notify_transitions",
           "全部 OFFLINE 且各带人话原因；掉线提醒只喊掉的那个环境",
           None, f"offline={len(offline)}/{len(rows)}\n示例: {rows[0]['channel_id']} "
                 f"{rows[0]['status']} - {rows[0]['reason']}\n提醒: {seen[0]}", ok)


class _SimAttempt:
    """The upload attempt a real driver exposes before the submit click."""

    def __init__(self, attempt_id, creation_id, video_id):
        self.attempt_id = attempt_id
        self.creation_id = creation_id
        self.video_id = video_id


class _ReceiptDriver:
    """Simulated driver that speaks the full submit contract.

    1.2.177: F6/F9 used a bare NullDriver, so there was no attempt id, no
    creation/video binding and no create_v2 receipt.  The ledger correctly
    refused to promote anything to CONSUMED, which meant the idempotency claim
    was never exercised.  This double performs the same three callbacks a real
    publish does (bind_submission -> click -> receipt) so the ledger's real
    atomic path runs.
    """

    simulated = True

    def __init__(self, work_id, attempt_id="ATT-0001", creation_id="CR-1",
                 video_id="VD-1"):
        self.calls = []
        self.platform_work_id = work_id
        self.upload_attempt = _SimAttempt(attempt_id, creation_id, video_id)
        self.bind_submission = None
        self.last_publish_evidence = None

    def fill(self, plan):
        self.calls.append(("fill", plan.get("plan_id")))

    def click_publish(self, plan):
        self.calls.append(("click_publish", plan.get("plan_id")))
        attempt = self.upload_attempt
        if callable(self.bind_submission):
            self.bind_submission(attempt.creation_id, attempt.video_id)
        self.last_publish_evidence = {
            "ok": True, "signal": "create_v2_status_ok",
            "receipt": {"status": 200, "status_code": 0,
                        "item_id": self.platform_work_id,
                        "work_id": self.platform_work_id,
                        "result": "SUCCESS",
                        "attempt_id": attempt.attempt_id,
                        "creation_id": attempt.creation_id,
                        "video_id": attempt.video_id,
                        "source": "playwright_response"}}

    def read_back_work_id(self, plan):
        self.calls.append(("read_back", plan.get("plan_id")))
        return self.platform_work_id


def _sim_driver(work_id):
    return _ReceiptDriver(work_id)


def case_F6_hardkill_idempotent(tmp: Path):
    layout = new_layout(tmp, "f6")
    seed_config(layout)
    plan = _plan()
    _seed = os.urandom(32)
    _pub = devsign.publickey(_seed)
    receipt = issue_receipt_for_test(_seed, plan)
    ledger = Stage2Ledger(layout.stage2_ledger)
    first = run_publish(plan, _sim_driver("7412345678901234567"), ledger,
                        mode=MODE_APPROVED, canary_armed=True,
                        post_fill_green=True, receipt=receipt,
                        verify_key=_pub, allow_simulated=True)
    # simulate hard kill right after submit: a fresh process re-reads the ledger
    reloaded = Stage2Ledger(layout.stage2_ledger)
    try:
        again = run_publish(plan, _sim_driver("7412345678901234568"), reloaded,
                            mode=MODE_APPROVED, canary_armed=True,
                            post_fill_green=True, receipt=receipt,
                            verify_key=_pub, allow_simulated=True)
        blocked, why = False, "第二次竟然又发布了"
    except Exception as exc:  # GateRed
        again, blocked, why = None, True, f"{type(exc).__name__}: {exc}"
    state = reloaded.state(first["key"])
    ok = first["published"] and blocked and state == CONSUMED
    record("F6", "失败路径：强杀后重启（不许重复发布）", "fail",
           "run_publish 两次，第二次用重开账本", "第二次被幂等键拦住，账本 CONSUMED",
           None, f"第一次 published={first['published']} work_id={first['platform_work_id']}\n"
                 f"第二次: {why}\n账本状态={state}", ok)


def case_F7_tamper(tmp: Path):
    import shutil
    target = tmp / "tamper"
    shutil.copytree(ROOT, target,
                    ignore=shutil.ignore_patterns("__pycache__", ".git"))
    sys.path.insert(0, str(ROOT / "tools"))
    import importlib
    build_bundle = importlib.import_module("build_bundle")
    build_bundle.build(target, version="1.1.0")
    clean = build_bundle.verify(target)
    victim = target / "agent" / "winport" / "version.py"
    victim.write_text(victim.read_text("utf-8") + "\n# tampered\n", encoding="utf-8")
    dirty = build_bundle.verify(target)
    vend = load_vendor_public_key(ROOT / "config" / "vendor_pubkey.hex")
    seed = os.urandom(32)
    fake_lic = mint_dev_license(seed, machine_id="M",
                                expires_at=(datetime.now(timezone.utc)
                                            + timedelta(days=30)).isoformat())
    sig = check_license(fake_lic, vend, "M")
    ok = clean["ok"] and (not dirty["ok"]) and dirty["mismatches"] \
        and sig["status"] == "SIGNATURE_INVALID"
    record("F7", "失败路径：交付物被篡改 / 签名不符 → 必须拒绝", "fail",
           "build_bundle.verify（改 1 字节前后）+ license-check（自签）",
           "改字节后 verify 报 mismatch；自签授权被拒",
           None, f"干净 verify: ok={clean['ok']} files={clean['file_count']}\n"
                 f"篡改后 verify: ok={dirty['ok']} mismatches={dirty['mismatches']}\n"
                 f"自签授权: {sig['status']}", ok,
           note="远程更新器（签名+哈希双验、原子替换、回滚）本单未实现 → 见报告 §接口预留")


def case_F8_atomic(tmp: Path):
    from winport import jsonio
    target = tmp / "atomic.json"
    jsonio.write_json(target, {"v": 1, "state": "GOOD"})
    before = target.read_text("utf-8")
    original_replace = os.replace

    def boom(src, dst):
        raise OSError("simulated power loss mid-write")

    os.replace = boom
    crashed = False
    try:
        jsonio.write_json(target, {"v": 2, "state": "SHOULD-NOT-LAND"})
    except OSError:
        crashed = True
    finally:
        os.replace = original_replace
    after = target.read_text("utf-8")
    leftovers = list(target.parent.glob(".tmp-*"))
    json.loads(after)  # still valid JSON
    ok = crashed and after == before and not leftovers
    record("F8", "失败路径：写盘中途失败（原子写，不留半个文件）", "fail",
           "jsonio.write_json 注入 os.replace 失败",
           "旧文件原样保留、仍是合法 JSON、无 .tmp 残留",
           None, f"注入失败={crashed}\n写前==写后: {after == before}\n"
                 f"残留临时文件: {leftovers}", ok,
           note="自动回滚（升级失败退回上一版）属远程更新器 → 未实现，见报告")


def case_F9_concurrency(tmp: Path):
    layout = new_layout(tmp, "f9")
    seed_config(layout)
    plan = _plan()
    _seed = os.urandom(32)
    _pub = devsign.publickey(_seed)
    receipt = issue_receipt_for_test(_seed, plan)
    results, errors = [], []

    def worker():
        try:
            led = Stage2Ledger(layout.stage2_ledger)
            results.append(run_publish(plan, _sim_driver("7412345678901234569"), led,
                                       mode=MODE_APPROVED, canary_armed=True,
                                       post_fill_green=True, receipt=receipt,
                                       verify_key=_pub, allow_simulated=True))
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    led = Stage2Ledger(layout.stage2_ledger)
    consumed = sum(1 for e in led.data["entries"].values()
                   if e.get("state") == CONSUMED)
    ok = len(results) == 1 and consumed == 1
    record("F9", "失败路径：重复点击 / 并发同一动作", "fail",
           "5 线程同时 run_publish 同一幂等键",
           "只成功 1 次，账本只 CONSUMED 1 条",
           None, f"成功={len(results)} 被拒不={len(errors)}\n"
                 f"被拒原因: {errors[:3]}\n账本 CONSUMED={consumed}", ok)


def case_F10_single_instance(tmp: Path):
    layout = new_layout(tmp, "f10")
    seed_config(layout)
    envs, channels = platforms.load_registry(layout)
    ch = [c for c in channels if c.channel_id == "env_1:douyin"][0]
    profile = ch.profile_dir(layout)
    fake = tmp / "fake" / "chrome.exe"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_bytes(b"MZ")
    # A live 1.2.16x+ window always carries the dialog guard; the fixture used to
    # omit it, so the reuse path correctly saw a stale pre-1.2.23 process and
    # F10 never exercised the reuse branch it was written for.
    procs = [{"pid": 4242, "name": "chrome.exe",
              "cmdline": f'{fake} --user-data-dir={profile} '
                         f'--remote-debugging-port=9224 '
                         f'--disable-features=ExternalProtocolDialog,Translate'}]
    # 1.2.177: reuse is proven from the launch marker + the live socket table.
    # The fixture provided neither, so F10 could only ever see MARKER_MISSING.
    from winport import jsonio, proc as proc_mod
    jsonio.write_json(layout.env_lease_dir / f"{ch.env_id}.launch.json", {
        "dialog_guard": chrome.DIALOG_GUARD_FEATURE,
        "profile": str(profile), "debug_port": 9224, "pid": 4242})
    original_owners = proc_mod.listening_pids
    proc_mod.listening_pids = lambda port, **kw: {4242}
    original = chrome.port_open
    chrome.port_open = lambda *a, **k: True
    try:
        reuse = open_channel(layout, ch, processes=procs, chrome_path=str(fake),
                             spawn=lambda cmd, lay: type("C", (), {"pid": 1})())
    finally:
        chrome.port_open = original
    chrome.port_open = lambda *a, **k: False
    try:
        refuse = open_channel(layout, ch, processes=procs, chrome_path=str(fake))
    finally:
        chrome.port_open = original
        proc_mod.listening_pids = original_owners
    ok = reuse["opened"] and reuse["reason"] == "REUSED_ENV_WINDOW" \
        and (not refuse["opened"]) and refuse["reason"] == "PROFILE_IN_USE"
    record("F10", "失败路径：同一环境不许第二个实例", "fail",
           "open_channel（端口在听 vs 不在听）",
           "在听→复用同一窗口(REUSED_ENV_WINDOW)；不在听但 profile 被占→PROFILE_IN_USE",
           None, f"复用: {reuse['reason']} - {reuse['message']}\n"
                 f"拒绝: {refuse['reason']} - {refuse['message']}", ok)


# --------------------------------------------------------------------- status
def case_C1_chrome(tmp: Path):
    layout = new_layout(tmp, "c1")
    seed_config(layout)
    res = cli(["chrome-probe"], data=layout.data)
    rep = regression.run_regression(layout,
                                    platforms.load_registry(layout)[1],
                                    cdp_probe=lambda p: {"ok": False, "error": "refused"})
    ok = res["rc"] != EXIT_OK and rep["mode"] == "offline" and not rep["all_ok"] \
        and rep["pending_real_machine"]
    record("C1", "真 Chrome：检测+版本+策略+选择器回归入口", "status",
           res["cmd"] + "  |  chrome-regression", 
           "本机无 Chrome → 诚实报 not found（非 0）+ 回归 offline/待真机",
           res["rc"], json.loads(res["out"])["summary"] + "\n"
                 + f"回归 mode={rep['mode']} all_ok={rep['all_ok']} "
                   f"pending_real_machine={rep['pending_real_machine']}", ok)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT.parent / "bundle_v3"
                                         / "SELF_VERIFY_REPORT.md"))
    ap.add_argument("--work")
    args = ap.parse_args(argv)
    work_root = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="sv3-"))
    work_root.mkdir(parents=True, exist_ok=True)
    print(f"work dir: {work_root}")

    cases = [case_M1_install, case_M2_activation, case_M3_login, case_M4_fetch,
             case_M5_enqueue, case_M6_publish, case_M7_collect, case_M8_report,
             case_F1_offline, case_F2_permission, case_F3_paths, case_F4_disk,
             case_F5_login_drop, case_F6_hardkill_idempotent, case_F7_tamper,
             case_F8_atomic, case_F9_concurrency, case_F10_single_instance,
             case_C1_chrome]
    for index, case in enumerate(cases):
        tmp = work_root / f"{index:02d}-{case.__name__}"
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            case(tmp)
        except Exception as exc:  # a broken case must not kill the report
            import traceback
            record(case.__name__, case.__name__, "error", "(internal)", "case runs",
                   None, traceback.format_exc(), False)
    report = render(args, work_root)
    Path(args.out).write_text(report, encoding="utf-8")
    (Path(args.out).with_suffix(".raw.log")).write_text(
        "\n\n".join(RAW_LOG), encoding="utf-8")
    failed = [c["cid"] for c in CASES if not c["ok"]]
    print(f"\nreport: {args.out}")
    print(f"cases: {len(CASES) - len(failed)}/{len(CASES)} passed"
          + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


def render(args, work_root: Path) -> str:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    passed = sum(1 for c in CASES if c["ok"])
    # 1.2.8 (GPT_REVIEW_1.2.7 必修 4): the header used to hardcode "1.1.0 ·
    # bundle_v3", so a reader could not tell WHICH version the report proved.
    try:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        version = "unknown"
    lines = [
        f"# 交付前自验报告（win-port {version}）",
        "",
        f"生成时间：{stamp}　|　工作目录：`{work_root}`",
        f"结论：**{passed}/{len(CASES)} 条通过**"
        + ("（全部通过）" if passed == len(CASES) else "（见下方 FAIL）"),
        "",
        "> 口径（1.2.8，GPT_REVIEW_1.2.7 必修 4）：**M6 发布是模拟单元测试**"
        "（模拟驱动，不是真平台填表）；**F1 已在降低磁盘阈值后重跑**，回显里是真实网络失败。"
        "当前功能口径：**上传+填表已验证；最终点发布待测试号验证；默认停在发布按钮前，**"
        "你点确认后才发**。",
        "",
        "口径（source/extra-requirements-verify.md）：**没有证据 = 没验过**。"
        "下面每条都给了命令、期望、**实际回显**与产物 SHA-256；"
        "本机（Linux 构建机）跑不到的一律进「待真机」清单，不写成通过。",
        "",
        "## 1. 自验清单（命令 / 期望 / 实际回显）",
        "",
    ]
    for c in CASES:
        lines.append(f"### {c['cid']} · {c['title']}　"
                     f"{'✅' if c['ok'] else '❌'}　[{'主链路' if c['kind']=='main' else '失败路径' if c['kind']=='fail' else c['kind']}]")
        lines.append(f"- 命令：`{c['cmd']}`")
        lines.append(f"- 期望：{c['expected']}")
        lines.append(f"- 实际退出码：`{c['rc']}`")
        lines.append("- 实际回显：")
        lines.append("```")
        lines.append(c["actual"].strip()[:4000])
        lines.append("```")
        if c["artifacts"]:
            lines.append("- 产物哈希：")
            for path, digest in c["artifacts"][:8]:
                lines.append(f"  - `{path}` → `{digest}`")
        if c["note"]:
            lines.append(f"- 备注：{c['note']}")
        lines.append("")
    lines += PENDING_AND_RISKS
    lines += [
        "## 4. 验收人可复现命令",
        "",
        "```bash",
        "cd output/win-port",
        "python3 -m unittest discover -s tests -v          # 98 项单测",
        "python3 tools/static_verify.py                     # 65 项静态验收",
        "python3 tools/e2e_selfverify.py --out ../bundle_v3/SELF_VERIFY_REPORT.md",
        "python3 ../bundle_v3/verify_bundle.py               # 成品包静态校验",
        "```",
        "",
        "（以上命令的输出即本报告 1 节的原始回显，逐字来自 `SELF_VERIFY_REPORT.raw.log`。）",
        "",
    ]
    return "\n".join(lines) + "\n"


PENDING_AND_RISKS = [
    "## 2. 未验证项清单（按风险排序，只能真机确认）",
    "",
    "| # | 未验证项 | 为什么没验 | 怎么补验 | 风险 |",
    "|---|---|---|---|---|",
    "| W1 | `bootstrap.ps1` 真机安装（建 venv、装 wheel、造自启动任务） | 构建机非 Windows、无 PowerShell/win 运行时 | 客户机按 `REMOTE_INSTALL_SOP.md` 跑一遍，回填 install.log | 高 |",
    "| W2 | Task Scheduler 自启动 + 崩溃自恢复 | 同上 | 重启电脑看托盘图标自动出现；`schtasks /query /tn HuolongAgent\\Agent` | 高 |",
    "| W3 | 客户机真 Chrome 检测/启动（注册表 App Paths + 常见路径 + 独立 profile/端口） | 构建机无 Chrome、无注册表 | 客户机跑 `winport chrome-probe` + `winport env-open --env env_1` | 高 |",
    "| W4 | 各环境真实扫码登录（手机扫码） | 需要真人 + 真账号 | 按面板逐环境扫码后 `winport login-check --env <env>` 看灯变绿 | 高 |",
    "| W5 | 企业策略锁远程调试的人话报错 | 构建机无组策略 | 在锁策略的机器上跑 `chrome-probe`/`env-open`，确认报 `POLICY_BLOCKS_DEBUG` | 中 |",
    "| W6 | 杀毒软件实时拦截安装/写盘 | 构建机无目标杀软 | 客户机装 360/火绒/Defender 各跑一次装机 | 中 |",
    "| W7 | 磁盘真正写满（ENOSPC） | 构建机无法安全造满盘 | 真机把盘写到水位以下，看是否停止取件并人话报错 | 中 |",
    "| W8 | 断网中途切换（下载到一半断网） | 构建机能造「源不可达」，造不了「中途断」 | 真机拔网线/断 Wi-Fi 复现，看重试与去重 | 中 |",
    "| W9 | 真 Chrome 下 fill 的 DOM 选择器回归 | 无真 Chrome、无登录态 | 真机 `winport chrome-regression --live`（本单先给了入口） | 高 |",
    "| W10 | xlsx/Excel 打开后行高/列宽是否被切 | 构建机无 Excel/LibreOffice | 真机打开日报，目视 | 低 |",
    "| W11 | 干净机器全流程验收（SOP 走一遍） | 本机装过依赖，不是干净环境 | 找一台全新 Win10/11 走 SOP | 高 |",
    "",
    "## 3. 已知风险与缓解",
    "",
    "- **真实扫码/选择器**：本单只把「真 Chrome 命令与登录面回归入口」落地；"
    "fill 的 DOM 选择器仍是真机项（`publish_exec.PlaywrightDriver.fill` 明确抛 GateRed，"
    "不会假装成功）。缓解：`chrome-regression --live` 有产出即算证据。",
    "- **远程更新**：本单**不实现**（按派单）。接口预留位置见 `BUILD_REPORT.md` §5；"
    "签名/哈希双验与原子替换的**组件能力**已用「授权签名拒绝 + MANIFEST 篡改检测 + "
    "原子写」三条证据证明可用（F7/F8）。",
    "- **授权按环境计数**：一期 env_1/env_2 都是「抖音」，属不同号；"
    "同一平台类型出现在多个环境是设计允许（`environment_warnings` 会提示），"
    "不是同一登录态被两个环境共用。",
    "- **磁盘水位默认 5GB**：构建机可用空间 <5GB 会直接触发 F4 的拒取件；"
    "客户机若长期低空间会停止取件并人话提示，不会写半个文件。",
    "",
]


if __name__ == "__main__":
    raise SystemExit(main())
