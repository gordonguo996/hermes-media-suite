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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", type=Path, required=True)
    ap.add_argument("--version", required=True)
    args = ap.parse_args()
    if os.name != "nt":
        fail("只能在 Windows 上运行")
    local = Path(os.environ["LOCALAPPDATA"])
    app = local / "Programs" / "HuolongAgent" / "publish-assistant"
    data = local / "HuolongAgent" / "publish-worker"

    install(args.setup)
    py = app / "runtime" / "python.exe"
    if not py.exists():
        fail("安装后没有 %s" % py)
    got = (app / "VERSION").read_text(encoding="utf-8-sig").strip()
    if got != args.version:
        fail("安装目录 VERSION=%s，期望 %s" % (got, args.version))
    if not (data / "run" / "program-path.txt").exists():
        fail("没有写 program-path.txt（开机任务转发器靠它找到当前版本）")
    recorded = (data / "run" / "program-path.txt").read_text(encoding="utf-8-sig").strip()
    if Path(recorded).resolve() != app.resolve():
        fail("program-path.txt 指向 %s，不是固定目录 %s" % (recorded, app))

    env = dict(os.environ, PYTHONUTF8="1")
    res = run([py, "-m", "agent.winport.cli", "selftest"], cwd=str(app), env=env,
              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    print(res.stdout[-3000:], res.stderr[-2000:])
    if res.returncode != 0:
        fail("selftest 返回 %s" % res.returncode)

    # 覆盖升级：放一个只存在于旧目录的标记文件，升级后它必须消失（不留旧版残渣），
    # 同时数据目录里的标记必须还在（升级不动数据）。
    stale = app / "stale-from-old-version.txt"
    stale.write_text("x", encoding="utf-8")
    keep = data / "keep-me.txt"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text("x", encoding="utf-8")
    install(args.setup)
    if stale.exists():
        fail("覆盖升级后旧版残留文件还在")
    if not keep.exists():
        fail("覆盖升级动了数据目录")
    if Path(str(app) + ".old").exists():
        fail("升级成功后 .old 回滚目录没清掉")
    if not py.exists():
        fail("升级后运行时丢失")

    un = app / "uninstall.exe"
    if not un.exists():
        fail("没有卸载程序")
    res = run([un, "/S"], timeout=600)
    # NSIS 卸载器会把自己复制到临时目录再运行，/S 下先返回；等目录消失。
    import time
    for _ in range(60):
        if not app.exists():
            break
        time.sleep(1)
    if app.exists():
        fail("卸载后程序目录还在")
    if not keep.exists():
        fail("卸载动了数据目录")
    print(json.dumps({"ok": True, "version": args.version, "app": str(app)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
