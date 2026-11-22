"""One-click diagnostics bundle for the customer (1.2.227).

The customer clicks 导出诊断包 in the panel (or runs ``diag-export``) and sends
one zip to the vendor, so a support case does not need a remote session into
the customer's machine.

What goes in: version / platform / machine code, the tail of the product logs,
the audit trails (remote access, hold clears), and the configuration files with
secrets removed.  What never goes in: credentials (control.json, relay keys,
tokens), the licence file, browser profiles, cookies, media and evidence images.
Every text file is passed through ``scrub`` and capped in size.
"""

from __future__ import annotations

import json
import platform
import re
import sys
import time
import zipfile
from pathlib import Path

TAIL_BYTES = 400 * 1024
MAX_FILES = 40
KEEP_BUNDLES = 5

#: relative to the data area; only these are ever read.
LOG_GLOBS = ("logs/*.log", "logs/*.log.*", "logs/*.jsonl", "logs/*.jsonl.1", "logs/*.txt")
RUN_FILES = ("run/publish_hold_audit.jsonl", "run/auto_unblock.log", "run/publish_holds.json",
             "run/license-clock.json", "run/program-path.txt")
CONFIG_FILES = ("config/settings.json", "config/channels.json", "config/accounts.json",
                "config/environments.json", "config/update.json")

SECRET_KEY = re.compile(r"(token|secret|authorization|cookie|passw(or)?d|private|credential|signature|keys?$|^keys?[_-]|[_-]keys?[_-])",
                        re.I)
_TEXT_SECRET = re.compile(
    r"""(?ix)
    (?P<k>["']?[A-Za-z0-9_\-]*(?:token|secret|authorization|cookie|passw(?:or)?d|[_-]key|key[_-]|credential)[A-Za-z0-9_\-]*["']?
       \s*[:=]\s*)
    (?P<v>"[^"]*"|'[^']*'|[^\s,;}\]]+)""")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}")


def scrub(text: str) -> str:
    """Mask secret-looking values in free text (logs, JSON lines)."""
    text = _BEARER.sub(lambda m: m.group(1) + " ***", text)
    return _TEXT_SECRET.sub(lambda m: m.group("k") + '"***"', text)


def scrub_json(node):
    if isinstance(node, dict):
        return {k: ("***" if SECRET_KEY.search(str(k)) and node[k] not in (None, "", False, 0)
                    else scrub_json(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [scrub_json(v) for v in node]
    return node


def _tail(path: Path, limit=TAIL_BYTES) -> str:
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > limit:
            fh.seek(size - limit)
        raw = fh.read()
    text = raw.decode("utf-8", "replace")
    return ("[... 只保留末尾 %d KB ...]\n" % (limit // 1024) + text) if size > limit else text


def _read_config(path: Path) -> str:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        return json.dumps(scrub_json(json.loads(raw)), ensure_ascii=False, indent=2)
    except ValueError:
        return scrub(raw)


def _collect(layout):
    data = Path(layout.data)
    found = []
    for pattern in LOG_GLOBS:
        found += sorted(data.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for rel in RUN_FILES + CONFIG_FILES:
        p = data / rel
        if p.is_file():
            found.append(p)
    seen, out = set(), []
    for p in found:
        if p not in seen and p.is_file():
            seen.add(p)
            out.append(p)
    return out[:MAX_FILES]


def build_summary(layout, fingerprint_info=None) -> dict:
    from .version import VERSION
    try:
        from .fingerprint import fingerprint
        fp = fingerprint_info or fingerprint()
    except Exception:  # noqa: BLE001
        fp = {}
    data = Path(layout.data)
    return {
        "product_version": VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine_code": fp.get("machine_id"),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "program_dir": str(layout.program),
        "data_dir": str(data),
        "license_file_present": Path(layout.license_file).exists(),
        "free_disk_gb": _free_gb(data),
    }


def _free_gb(path: Path):
    try:
        import shutil
        return round(shutil.disk_usage(path if path.exists() else path.parent).free / 2**30, 1)
    except OSError:
        return None


def export(layout, out_dir=None, now=None, fingerprint_info=None) -> dict:
    """Write the bundle and return ``{ok, path, size, files, reason}``; never raises."""
    try:
        data = Path(layout.data)
        out_dir = Path(out_dir) if out_dir else data / "diagnostics"
        out_dir.mkdir(parents=True, exist_ok=True)
        summary = build_summary(layout, fingerprint_info)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        code = (summary.get("machine_code") or "unknown")[:8]
        target = out_dir / ("diag-%s-%s.zip" % (code, stamp))
        names = []
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
            for p in _collect(layout):
                rel = p.relative_to(data).as_posix()
                try:
                    body = _read_config(p) if rel.startswith("config/") else scrub(_tail(p))
                except OSError:
                    continue
                zf.writestr(rel, body)
                names.append(rel)
        for old in sorted(out_dir.glob("diag-*.zip"), key=lambda p: p.stat().st_mtime)[:-KEEP_BUNDLES]:
            try:
                old.unlink()
            except OSError:
                pass
        return {"ok": True, "path": str(target), "size": target.stat().st_size, "files": names,
                "reason": "诊断包已生成：%s（%d KB）。请把这个文件发给服务商；里面没有密码、令牌、授权文件和浏览器登录数据。"
                          % (target, max(1, target.stat().st_size // 1024))}
    except Exception as exc:  # noqa: BLE001 - the customer needs a sentence, not a traceback
        return {"ok": False, "reason": "诊断包生成失败：%s。请截图本页并联系服务商。" % exc}
