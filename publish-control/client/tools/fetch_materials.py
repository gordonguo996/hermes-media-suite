"""按 materials.lock.json 下载并校验离线材料，整理成 build_setup.py --materials 要的目录。

不再依赖任何人的电脑：每一项都从官方地址下载，哈希对不上就失败（fail closed）。

    python tools/fetch_materials.py --out <空目录> [--cache <下载缓存>] [--skip-runtime] [--no-webkit]

运行时（Python）需要在 Windows 上用官方安装包安装到临时目录，其余项在任何系统上都能取。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

CLIENT = Path(__file__).resolve().parents[1]
LOCK = CLIENT / "materials.lock.json"


class MaterialError(RuntimeError):
    pass


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_sha256(root):
    """与基线核对用：相对路径 + 内容哈希，按路径排序（不含目录项）。"""
    root = Path(root)
    h, files, size = hashlib.sha256(), 0, 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            p = Path(dirpath) / name
            h.update(p.relative_to(root).as_posix().encode())
            h.update(sha256_file(p).encode())
            files += 1
            size += p.stat().st_size
    return {"files": files, "size": size, "tree_sha256": h.hexdigest()}


def download(url, dest, sha256=None):
    dest = Path(dest)
    if dest.exists() and sha256 and sha256_file(dest) == sha256:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-materials/1"})
    with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)
    got = sha256_file(tmp)
    if sha256 and got != sha256:
        tmp.unlink()
        raise MaterialError("哈希不符：%s\n  期望 %s\n  实际 %s" % (url, sha256, got))
    os.replace(tmp, dest)
    return dest


def _safe_extract(zf, target):
    target = Path(target).resolve()
    for info in zf.infolist():
        out = (target / info.filename).resolve()
        if target != out and target not in out.parents:
            raise MaterialError("压缩包含越界路径：" + info.filename)
    zf.extractall(target)


def fetch_ffmpeg(lock, cache, out):
    cfg = lock["ffmpeg"]
    archive = download(cfg["url"], cache / "ffmpeg.zip", cfg["sha256"])
    with zipfile.ZipFile(archive) as zf:
        for member, spec in cfg["members"].items():
            data = zf.read(member)
            if hashlib.sha256(data).hexdigest() != spec["sha256"] or len(data) != spec["size"]:
                raise MaterialError("ffmpeg 成员哈希不符：" + member)
            dest = out / spec["to"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
    return {"bin": "ok"}


def fetch_zip_tree(cfg, cache, out, name):
    archive = download(cfg["url"], cache / (name + ".zip"), cfg["sha256"])
    dest = out / cfg["extract_to"]
    if dest.exists():
        raise MaterialError("目标已存在：%s" % dest)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zf:
        _safe_extract(zf, dest)
    for name in cfg.get("rename_to_rt", []):
        src = dest / name
        if src.exists():
            src.rename(dest / (name + ".__rt"))
    got = tree_sha256(dest)
    want = cfg["baseline_tree"]
    status = "match" if got == want else "differs"
    return {"status": status, "got": got, "baseline": want}


def fetch_wheels(lock, out):
    cfg = lock["wheels"]
    entries = json.loads((CLIENT / cfg["lock_file"]).read_text(encoding="utf-8-sig"))["wheels"]
    dest = out / cfg["extract_to"]
    dest.mkdir(parents=True, exist_ok=True)
    specs = []
    for e in entries:
        dist = e["name"].split("-")[0].replace("_", "-")
        specs.append("%s==%s" % (dist, e["version"]))
    cmd = [sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
           "--platform", cfg["platform"], "--python-version", cfg["python_version"],
           "--implementation", "cp", "-d", str(dest)] + specs
    # 个别包是纯 Python 通用 wheel；pip 对 --platform 会放行 any 标签。
    subprocess.run(cmd, check=True)
    got = {p.name: sha256_file(p) for p in dest.glob("*.whl")}
    problems = []
    for e in entries:
        if got.get(e["name"]) != e["sha256"]:
            problems.append(e["name"])
    extra = sorted(set(got) - {e["name"] for e in entries})
    if problems or extra:
        raise MaterialError("wheels 与 wheels.lock.json 不一致：缺失/不符=%s 多余=%s" % (problems, extra))
    return {"count": len(got)}


def _authenticode_signer(path):
    """Windows 上取签名者；其它系统返回 None。"""
    if os.name != "nt":
        return None
    script = ("$s=Get-AuthenticodeSignature -LiteralPath '%s';"
              "if($s.Status -eq 'Valid'){$s.SignerCertificate.Subject}else{'INVALID:'+$s.Status}" % path)
    res = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    return res.stdout.strip()


def fetch_chrome(lock, cache, out, unchecked=False):
    cfg = lock["chrome_offline"]
    raw = download(cfg["url"], cache / "ChromeStandaloneSetup64.exe")
    digest = sha256_file(raw)
    signer = _authenticode_signer(str(raw))
    if signer is None:
        if unchecked:
            verified = "UNCHECKED-non-windows"
        elif digest != cfg["baseline_sha256"]:
            raise MaterialError("非 Windows 无法验证 Chrome 签名，且哈希不是基线版本；请在 Windows 上取（或仅调试时加 --chrome-unchecked）。")
        else:
            verified = "sha256-baseline"
    else:
        if "Google LLC" not in signer:
            raise MaterialError("Chrome 安装包签名不是 Google LLC：" + signer)
        verified = "authenticode:" + signer
    dest = out / cfg["extract_to"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(raw, dest)
    (dest.parent / "ChromeStandaloneSetup64.exe.sha256").write_text(
        "%s  ChromeStandaloneSetup64.exe\n" % digest, encoding="utf-8")
    return {"sha256": digest, "size": raw.stat().st_size, "verified": verified,
            "is_baseline": digest == cfg["baseline_sha256"]}


def fetch_runtime(lock, cache, out):
    if os.name != "nt":
        raise MaterialError("运行时需要在 Windows 上用官方安装包安装；非 Windows 请加 --skip-runtime。")
    cfg = lock["python"]
    exe = download(cfg["url"], cache / "python-installer.exe", cfg["sha256"])
    work = Path(tempfile.mkdtemp(prefix="hms-py-"))
    try:
        target = work / "inst"
        subprocess.run([str(exe)] + cfg["install_args"] + ["TargetDir=" + str(target)],
                       check=True, timeout=900)
        for name in cfg["rename_to_rt"]:
            src = target / name
            if src.exists():
                src.rename(target / (name + ".__rt"))
        dest = out / "runtime"
        shutil.copytree(target, dest)
    finally:
        # 安装器是 per-user 安装，不留注册信息：临时目录删掉即可。
        shutil.rmtree(work, ignore_errors=True)
    wrong = {}
    for rel, want in cfg["key_files"].items():
        p = dest / rel
        got = sha256_file(p) if p.exists() else None
        if got != want:
            wrong[rel] = got
    tree = tree_sha256(dest)
    return {"key_file_mismatch": wrong, "tree": tree, "baseline": cfg["baseline_tree"],
            "tree_matches_baseline": tree == cfg["baseline_tree"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True, help="材料输出目录（必须不存在或为空）")
    ap.add_argument("--cache", type=Path, default=None)
    ap.add_argument("--skip-runtime", action="store_true")
    ap.add_argument("--no-webkit", action="store_true", help="不取 WebKit（对方确认没人用后）")
    ap.add_argument("--chrome-unchecked", action="store_true", help="仅调试：非 Windows 不验签")
    ap.add_argument("--strict-tree", action="store_true",
                    help="runtime/浏览器目录与基线不逐文件一致就失败")
    args = ap.parse_args()
    lock = json.loads(LOCK.read_text(encoding="utf-8-sig"))
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        ap.error("--out 必须是空目录")
    out.mkdir(parents=True, exist_ok=True)
    cache = (args.cache or Path(tempfile.gettempdir()) / "hms-materials-cache").resolve()
    report = {}
    try:
        report["ffmpeg"] = fetch_ffmpeg(lock, cache, out)
        report["playwright_ffmpeg"] = fetch_zip_tree(lock["playwright_ffmpeg"], cache, out, "pw-ffmpeg")
        if lock["webkit"]["include_in_package"] and not args.no_webkit:
            report["webkit"] = fetch_zip_tree(lock["webkit"], cache, out, "webkit")
        report["wheels"] = fetch_wheels(lock, out)
        report["chrome_offline"] = fetch_chrome(lock, cache, out, args.chrome_unchecked)
        if not args.skip_runtime:
            report["runtime"] = fetch_runtime(lock, cache, out)
    except (MaterialError, subprocess.CalledProcessError) as exc:
        print("BLOCKED:", exc, file=sys.stderr)
        sys.exit(1)
    bad = [k for k, v in report.items() if isinstance(v, dict) and (
        v.get("status") == "differs" or v.get("key_file_mismatch"))]
    if args.strict_tree and report.get("runtime", {}).get("tree_matches_baseline") is False:
        bad.append("runtime-tree")
    (out / "materials.report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if bad:
        print("与基线不一致的项：", bad, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
