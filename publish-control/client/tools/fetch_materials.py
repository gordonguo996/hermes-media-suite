"""按 materials.lock.json 下载并校验离线材料，整理成 build_setup.py --materials 要的目录。

不再依赖任何人的电脑：每一项都从官方地址下载，哈希对不上就失败（fail closed）。

    python tools/fetch_materials.py --out <空目录> [--cache <下载缓存>] [--skip-runtime] [--no-webkit]

全部在任何系统上都能取（运行时来自 NuGet 的 python 包）。
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
import time
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


def _download_once(url, dest, tmp):
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-materials/1"})
    with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)


def download(url, dest, sha256=None, attempts=5):
    dest = Path(dest)
    if dest.exists() and sha256 and sha256_file(dest) == sha256:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    # 官方源偶尔 503/超时（CI 实测 gyan.dev 会），一律退避重试，不要一次失败就让整条链路红。
    for attempt in range(attempts):
        try:
            _download_once(url, dest, tmp)
            break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            code = getattr(exc, "code", None)
            fatal = code is not None and 400 <= code < 500 and code not in (408, 429)
            if fatal or attempt == attempts - 1:
                raise MaterialError("下载失败 %s（第 %d 次，%s）" % (url, attempt + 1, exc)) from exc
            delay = 2 ** attempt
            print("WARN: %s 第 %d 次失败（%s），%ds 后重试" % (url, attempt + 1, exc, delay),
                  file=sys.stderr)
            time.sleep(delay)
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


def _parse_signature_json(out):
    """从 PowerShell 的 JSON 输出里取出签名结论；拿不到就给 None。"""
    for line in reversed((out or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        data.setdefault("status", "")
        data.setdefault("subject", "")
        data.setdefault("detail", "")
        if not data["status"]:
            data["status"] = "ERROR"
            data["detail"] = data["detail"] or "Get-AuthenticodeSignature 返回空对象"
        return data
    return None


def _authenticode(path):
    """Windows 上取签名结论；其它系统返回 None。

    返回 {"status","subject","detail"}，status 直接取 SignatureStatus。调用本身失败时
    给 ERROR 并把原因带回来——不能让上层只看到一个空的 "INVALID:"。
    """
    if os.name != "nt":
        return None
    script = (
        "$ErrorActionPreference='Stop';"
        "Import-Module Microsoft.PowerShell.Security -ErrorAction SilentlyContinue;"
        "try{"
        "$s=Get-AuthenticodeSignature -LiteralPath $env:HMS_TARGET;"
        "$subj=if($s.SignerCertificate){$s.SignerCertificate.Subject}else{''};"
        "@{status=[string]$s.Status;subject=$subj;detail=''} | ConvertTo-Json -Compress"
        "}catch{"
        "@{status='ERROR';subject='';detail=$_.Exception.Message} | ConvertTo-Json -Compress"
        "}"
    )
    env = dict(os.environ)
    env["HMS_TARGET"] = str(path)
    shells = ["powershell"]
    if shutil.which("pwsh"):
        shells.append("pwsh")  # 5.1 偶尔加载不到 Security 模块（CI 实测），7 一般可以
    res = None
    data = None
    for shell in shells:
        res = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", env=env)
        out = (res.stdout or "").strip()
        data = _parse_signature_json(out)
        if data is None or data["status"].lower() != "error":
            break
    out = (res.stdout or "").strip()
    if data is not None:
        return data
    return {"status": "ERROR", "subject": "",
            "detail": ((res.stderr or out or "powershell 无输出").strip())[:400]}


# 这些是签名系统给出的确定结论：文件确实没有可接受的签名，不许用哈希兜底。
_CHROME_DEFINITE_FAIL = {"notsigned", "hashmismatch", "nottrusted"}


def chrome_verdict(sig, digest, cfg):
    """把验签结论翻译成 report 里的 verified 字段；不该放行时抛 MaterialError。

    放行条件（按优先级）：
      1. 签名有效且签名者是 lock 里要求的 signer（Google LLC）
      2. 签名系统给不出结论（ERROR/UnknownError 等）且哈希命中 lock 里的已知值
    其余一律失败——包括签名有效但签名者不对、以及明确的 NotSigned/HashMismatch。
    """
    want = cfg.get("signer", "Google LLC")
    pinned = [cfg["baseline_sha256"]] + list(cfg.get("accepted_sha256") or [])
    if sig is None:
        if digest in pinned:
            return "sha256-pinned(non-windows)"
        raise MaterialError("非 Windows 无法验证 Chrome 签名，且哈希不在锁定值中：%s" % digest)
    status = (sig.get("status") or "").strip()
    subject = sig.get("subject") or ""
    detail = (sig.get("detail") or "").strip()
    if status.lower() == "valid":
        if want in subject:
            return "authenticode:" + subject
        raise MaterialError("Chrome 安装包签名者不是 %s：%s" % (want, subject))
    if status.lower() in _CHROME_DEFINITE_FAIL:
        raise MaterialError("Chrome 安装包验签失败（%s %s），哈希=%s" % (status, detail, digest))
    if digest in pinned:
        print("WARN: Chrome 验签不可用（%s %s），按锁定的哈希放行：%s"
              % (status, detail, digest), file=sys.stderr)
        return "sha256-pinned(%s)" % status
    raise MaterialError("Chrome 验签不可用（%s %s），且哈希不在锁定值中：%s"
                        % (status, detail, digest))


def fetch_chrome(lock, cache, out, unchecked=False):
    cfg = lock["chrome_offline"]
    raw = download(cfg["url"], cache / "ChromeStandaloneSetup64.exe")
    digest = sha256_file(raw)
    if unchecked:
        verified = "UNCHECKED-debug"
    else:
        verified = chrome_verdict(_authenticode(raw), digest, cfg)
    dest = out / cfg["extract_to"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(raw, dest)
    (dest.parent / "ChromeStandaloneSetup64.exe.sha256").write_text(
        "%s  ChromeStandaloneSetup64.exe\n" % digest, encoding="utf-8")
    return {"sha256": digest, "size": raw.stat().st_size, "verified": verified,
            "is_baseline": digest == cfg["baseline_sha256"]}


def fetch_runtime(lock, cache, out):
    """runtime/ = NuGet python.3.11.9 的 tools/；两个 VC DLL 改名 .__rt，删 colorama 测试文件。
    任何系统都能取，不需要运行安装程序。"""
    cfg = lock["python"]
    archive = download(cfg["url"], cache / "python.nupkg", cfg["sha256"])
    dest = out / "runtime"
    dest.mkdir(parents=True)
    prefix = cfg["member_prefix"]
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.startswith(prefix):
                continue
            rel = info.filename[len(prefix):]
            if any(rel.startswith(p) for p in cfg["drop_prefixes"]):
                continue
            target = (dest / rel).resolve()
            if dest.resolve() not in target.parents:
                raise MaterialError("压缩包含越界路径：" + info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
    for name in cfg["rename_to_rt"]:
        (dest / name).rename(dest / (name + ".__rt"))
    wrong = {}
    for rel, want in cfg["key_files"].items():
        p = dest / rel
        got = sha256_file(p) if p.exists() else None
        if got != want:
            wrong[rel] = got
    tree = tree_sha256(dest)
    return {"key_file_mismatch": wrong, "tree": tree, "baseline": cfg["baseline_tree"],
            "status": "match" if tree == cfg["baseline_tree"] and not wrong else "differs"}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True, help="材料输出目录（必须不存在或为空）")
    ap.add_argument("--cache", type=Path, default=None)
    ap.add_argument("--skip-runtime", action="store_true")
    ap.add_argument("--no-webkit", action="store_true", help="不取 WebKit（对方确认没人用后）")
    ap.add_argument("--chrome-unchecked", action="store_true", help="仅调试：非 Windows 不验签")
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
    (out / "materials.report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if bad:
        print("与基线不一致的项：", bad, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
