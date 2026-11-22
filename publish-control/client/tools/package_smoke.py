"""干净 Windows 上对安装程序做静默验收：安装 -> 自检 -> 覆盖升级 -> 卸载。

    python tools/package_smoke.py --setup <安装程序.exe> --version 1.2.223

只在 Windows 上运行（CI 的 windows-latest 是每次全新的机器）。任何一步不满足就以非零退出。
这是"客户双击安装"这件事的最小自动化替身；真实客户机的验收仍要人工做。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def fail(msg):
    print("FAIL:", msg, file=sys.stderr)
    sys.exit(1)


def run(cmd, **kw):
    print("$", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], **kw)


def install(setup):
    res = run([setup, "/S"], timeout=1800)
    if res.returncode != 0:
        fail("静默安装返回 %s" % res.returncode)



LAUNCHER_GLOBS = ("漫剧发布助手*.vbs", "漫剧发布助手*.lnk")


def launcher_dirs():
    """桌面 + 开始菜单：安装时应当出现启动入口，卸载后必须消失。"""
    user = Path(os.environ.get("USERPROFILE", ""))
    appdata = Path(os.environ.get("APPDATA", ""))
    return [user / "Desktop",
            appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs"]


def find_launchers():
    found = []
    for d in launcher_dirs():
        if not d.is_dir():
            continue
        for pat in LAUNCHER_GLOBS:
            found.extend(sorted(d.glob(pat)))
    return found


def diagnose_leftovers(app, data, rest):
    """说不清为什么删不掉就等于没修。卸载失败时把证据打出来：

    * 卸载器自己的 quiesce 日志（silent 卸载唯一能留痕的地方）
    * 还活着的候选进程（python/wscript/chrome/agent 之类）
    * 第一个残留文件到底是"被占用"(winerror 32) 还是"只读/权限"(13/5)
    * 再手动跑一次 quiesce，看它能不能清干净
    """
    qlog = data / "logs" / "uninstall-quiesce.log"
    print("---- uninstall-quiesce.log ----", file=sys.stderr)
    if qlog.exists():
        print(qlog.read_text(encoding="utf-8", errors="replace")[-4000:], file=sys.stderr)
    else:
        print("(没有日志：卸载器根本没跑 quiesce 脚本)", file=sys.stderr)

    ps = ("Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
          "Where-Object { $_.Name -match 'python|wscript|cscript|chrome|msedge|agent|publish' } | "
          "ForEach-Object { '{0}|{1}|{2}|{3}' -f $_.ProcessId, $_.Name, $_.ExecutablePath, $_.CommandLine }")
    res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    print("---- 候选进程 ----", file=sys.stderr)
    print(res.stdout.strip() or "(无)", file=sys.stderr)

    first = app / rest[0]
    print("---- 第一个残留项 %s ----" % first, file=sys.stderr)
    try:
        st = first.stat()
        print("attributes=%#x (readonly=%s dir=%s)"
              % (st.st_file_attributes, bool(st.st_file_attributes & 1),
                 first.is_dir()), file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - 诊断路径，异常本身也是证据
        print("stat 失败: %r" % (exc,), file=sys.stderr)
    if first.is_file():
        try:
            first.unlink()
            print("python 能删掉它：说明不是文件级占用，是目录树/重试逻辑问题", file=sys.stderr)
        except OSError as exc:
            print("python 删不掉: winerror=%r %s" % (getattr(exc, "winerror", None), exc), file=sys.stderr)

    script = app / "scripts" / "uninstall-quiesce.ps1"
    if script.exists():
        print("---- 手动再跑一次 quiesce ----", file=sys.stderr)
        res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                              "-ExecutionPolicy", "Bypass", "-File", str(script),
                              "-Program", str(app), "-DataDir", str(data), "-Rounds", "3"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(res.stdout[-4000:], res.stderr[-2000:], file=sys.stderr)
        print("quiesce rc=%s" % res.returncode, file=sys.stderr)


def purge_authorisation(data):
    """把机器退化成「试用记录没了、也没有正式授权」的客户机状态。"""
    removed = []
    for rel in ("system/trial.json", "system/license.json"):
        f = data / rel
        if f.exists():
            f.unlink()
            removed.append(rel)
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\HuolongAgent\Agent", 0,
                             winreg.KEY_ALL_ACCESS)
    except OSError:
        key = None
    if key is not None:
        for name in ("TrialRecord", "TrialSeen"):
            try:
                winreg.DeleteValue(key, name)
                removed.append("HKCU:Agent\\" + name)
            except OSError:
                pass
        winreg.CloseKey(key)
    print("清除授权痕迹：%s" % removed)
    if len(removed) < 2:
        fail("试用记录没清干净（%s），后面的 pending 判定不可信" % removed)


def pending_install(setup, version, app, data):
    """1.2.227 第三处改动的真机验收：授权不可用时必须「保留安装 + 退出码 6」。

    真实场景：试用记录被删/过期、又没有正式授权，客户重装一次。旧安装器在这里
    要么弹框卡死、要么整体回滚，客户机上表现为「卸了装不回来」。
    """
    install(setup)                                  # 第一次：正常装，rc 必须 0
    purge_authorisation(data)
    res = run([setup, "/S"], timeout=1800)
    print("授权缺失时静默安装返回码：%s（期望 6）" % res.returncode)
    if res.returncode != 6:
        fail("授权缺失时安装返回 %s，期望 6" % res.returncode)
    if not (app / "runtime" / "python.exe").exists():
        fail("退出码 6 之后程序目录被回滚了（应当保留安装）")
    got = (app / "VERSION").read_text(encoding="utf-8-sig").strip()
    if got != version:
        fail("保留的安装 VERSION=%s，期望 %s" % (got, version))
    if not find_launchers():
        fail("退出码 6 之后没有启动入口，安装没保留完整")
    venv_py = data / ".venv" / "Scripts" / "python.exe"
    if not venv_py.exists():
        fail("退出码 6 之后数据目录的 venv 丢了")
    res = run([venv_py, "-m", "agent.winport.cli", "selftest", "--license-pending-ok"],
              cwd=str(app), env=dict(os.environ, PYTHONUTF8="1"),
              capture_output=True, text=True, encoding="utf-8", errors="replace",
              timeout=600)
    print(res.stdout[-1500:], res.stderr[-800:])
    if res.returncode != 0:
        fail("授权 pending 时 selftest 必须放行，实际返回 %s" % res.returncode)
    print(json.dumps({"ok": True, "version": version, "mode": "activation-pending",
                      "install_retained": True}, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", type=Path, required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--expect-pending", action="store_true",
                    help="只验「授权不可用 → 保留安装 + 退出码 6」这条分支")
    args = ap.parse_args()
    if os.name != "nt":
        fail("只能在 Windows 上运行")
    local = Path(os.environ["LOCALAPPDATA"])
    app = local / "Programs" / "HuolongAgent" / "publish-assistant"
    data = local / "HuolongAgent" / "publish-worker"

    if args.expect_pending:
        pending_install(args.setup, args.version, app, data)
        return

    install(args.setup)
    py = app / "runtime" / "python.exe"
    if not py.exists():
        fail("安装后没有 %s" % py)
    # 依赖装在数据目录的 venv 里（bootstrap_py.py step_wheels），不在 runtime 那棵树上。
    # 用 runtime\python.exe 跑 selftest 会一律红在 playwright/pystray——那是拿错了解释器。
    venv_py = data / ".venv" / "Scripts" / "python.exe"
    if not venv_py.exists():
        fail("安装后没有 %s（bootstrap 没建 venv？看 install.log）" % venv_py)
    got = (app / "VERSION").read_text(encoding="utf-8-sig").strip()
    if got != args.version:
        fail("安装目录 VERSION=%s，期望 %s" % (got, args.version))
    if not (data / "run" / "program-path.txt").exists():
        fail("没有写 program-path.txt（开机任务转发器靠它找到当前版本）")
    recorded = (data / "run" / "program-path.txt").read_text(encoding="utf-8-sig").strip()
    if Path(recorded).resolve() != app.resolve():
        fail("program-path.txt 指向 %s，不是固定目录 %s" % (recorded, app))

    env = dict(os.environ, PYTHONUTF8="1")
    res = run([venv_py, "-m", "agent.winport.cli", "selftest"], cwd=str(app), env=env,
              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    print(res.stdout[-3000:], res.stderr[-2000:])
    if res.returncode != 0:
        log = data / "logs" / "install.log"
        if log.exists():
            print("---- install.log tail ----", file=sys.stderr)
            print(log.read_text(encoding="utf-8", errors="replace")[-4000:], file=sys.stderr)
        fail("selftest 返回 %s（解释器 %s）" % (res.returncode, venv_py))

    # 覆盖升级：放一个只存在于旧目录的标记文件，升级后它必须消失（不留旧版残渣），
    # 同时数据目录里的标记必须还在（升级不动数据）。
    stale = app / "stale-from-old-version.txt"
    stale.write_text("x", encoding="utf-8")
    keep = data / "keep-me.txt"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text("x", encoding="utf-8")
    install(args.setup)
    launchers = find_launchers()
    if not launchers:
        fail("安装后桌面/开始菜单没有启动入口")
    print("安装后启动入口：%s" % [x.name for x in launchers])
    if stale.exists():
        fail("覆盖升级后旧版残留文件还在")
    if not keep.exists():
        fail("覆盖升级动了数据目录")
    if Path(str(app) + ".old").exists():
        fail("升级成功后 .old 回滚目录没清掉")
    if not py.exists():
        fail("升级后运行时丢失")
    if not venv_py.exists():
        fail("升级后 venv 丢失")

    un = app / "uninstall.exe"
    if not un.exists():
        fail("没有卸载程序")
    res = run([un, "/S"], timeout=600)
    # NSIS 卸载器会把自己复制到临时目录再运行，/S 下先返回；等目录消失。
    import time
    for _ in range(120):
        if not app.exists():
            break
        time.sleep(1)
    if app.exists():
        rest = sorted(x.relative_to(app).as_posix() for x in app.rglob("*"))
        print("卸载后仍残留 %d 项，前 15 项：%s" % (len(rest), rest[:15]), file=sys.stderr)
        diagnose_leftovers(app, data, rest)
        fail("卸载后程序目录还在")
    for _ in range(30):
        if not find_launchers():
            break
        time.sleep(1)
    left = find_launchers()
    if left:
        fail("卸载后启动入口还在：%s" % [str(x) for x in left])
    if not keep.exists():
        fail("卸载动了数据目录")
    qlog = data / "logs" / "uninstall-quiesce.log"
    print("---- uninstall-quiesce.log（卸载器实际做了什么的唯一证据）----")
    print(qlog.read_text(encoding="utf-8", errors="replace")[-4000:] if qlog.exists()
          else "(没有日志：卸载器没跑 quiesce 脚本，删除残留是巧合)")
    print(json.dumps({"ok": True, "version": args.version, "app": str(app)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
