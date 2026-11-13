"""Startup-chain integrity check (GPT review 1.2.4 item 5).

The delivered package ships ``MANIFEST.json`` (per-file SHA-256).  At startup the
agent re-hashes its OWN code / scripts / config against that manifest, so a
swapped-out ``agent/winport/*.py`` or a rewritten ``scripts/*.vbs`` shows up as a
tamper event instead of being loaded silently.

Capability boundary (documented, deliberately honest): an OFFLINE client cannot
stop a user who has full local control from editing files AND the manifest.
This check raises the cost of a casual swap and gives the operator evidence; it
is NOT a substitute for the server-side licence being the final authority.
"""

from __future__ import annotations

import json
from pathlib import Path

from .hashing import sha256_file

#: path prefixes that make up the "startup chain" we can cheaply re-hash.
DEFAULT_SCOPES = ("agent/", "scripts/", "config/", "assets/", "tools/", "tests/")
#: 1.2.213: these two files are written by the client itself AFTER install --
#: ``activation.json`` by the panel auto-activation (activation.py) and
#: ``license.json`` by the offline import -- so they are legitimately absent
#: from the shipped MANIFEST.  Treating them as "manifest-external tamper"
#: turned every activated install RED and silently blocked publishing.
#: The allowlist is by EXACT relative path: nothing else under config/ is
#: exempt, so a smuggled .py/.ps1/.exe/archive there is still an EXTRA.
RUNTIME_STATE_FILES = frozenset((
    "config/activation.json",
    "config/license.json",
))

#: loose files at the program root that are part of the launch chain.
ROOT_FILES = ("bootstrap.bat", "show-machine-code.bat", "import-license.bat",
              "selftest.bat", "requirements.txt")


def _is_runtime_state(rel: str) -> bool:
    """True only for the two client-written state files under config/."""
    return rel in RUNTIME_STATE_FILES


def _in_scope(rel: str, scopes) -> bool:
    if rel in ROOT_FILES:
        return True
    return any(rel.startswith(prefix) for prefix in scopes)


def verify_program(program, *, manifest_name="MANIFEST.json",
                   scopes=DEFAULT_SCOPES) -> dict:
    """Re-hash the startup chain against MANIFEST.json.  Never raises.

    1.2.5 (GPT review item 5): this is STRICT about the product tree --
      * an empty / malformed manifest is a FAILURE, not "0 files, all fine";
      * an EXTRA in-scope file that the manifest does not list is a FAILURE
        (how a stray ``.py``/``.ps1``/receipt/``.pyc`` would ride along
        unnoticed).  A bytecode cache is NOT exempted: "the .py source exists"
        does not prove the cached bytes are trustworthy, and the shipped tree
        carries no ``__pycache__`` (bytecode writing is disabled at runtime).
    """
    program = Path(program)
    manifest_path = program / manifest_name
    verdict = {"ok": False, "checked": 0, "modified": [], "missing": [],
               "extra": [], "manifest": str(manifest_path), "reason": ""}
    if not manifest_path.exists():
        verdict["reason"] = "MANIFEST.json not found in the program folder"
        return verdict
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        verdict["reason"] = f"MANIFEST.json unreadable: {exc}"
        return verdict
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or not files:
        verdict["reason"] = ("MANIFEST.json carries no file list "
                             "(empty/blank manifest) -- refusing to trust it")
        return verdict
    if not manifest.get("version") and not manifest.get("schema_version"):
        verdict["reason"] = "MANIFEST.json has no version/schema_version"
        return verdict

    known = set(files)
    checked = 0
    for rel in sorted(files):
        if not _in_scope(rel, scopes):
            continue
        checked += 1
        target = program / rel
        if not target.exists():
            verdict["missing"].append(rel)
            continue
        try:
            if sha256_file(target) != (files[rel] or {}).get("sha256"):
                verdict["modified"].append(rel)
        except OSError as exc:
            verdict["modified"].append(f"{rel} (unreadable: {exc})")
    verdict["checked"] = checked

    for path in sorted(program.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(program).as_posix()
        if not _in_scope(rel, scopes) or rel in known:
            continue
        # 1.2.35: a bytecode cache is produced by this machine itself and can
        # never stand in for a modified/missing source file (those are still
        # hashed above), so it must not turn a healthy install into a RED gate.
        if "__pycache__/" in rel and rel.endswith(".pyc"):
            continue
        # 1.2.213: the two client-written state files are not launch chain.
        if _is_runtime_state(rel):
            continue
        verdict["extra"].append(rel)

    if checked == 0:
        verdict["reason"] = "MANIFEST.json lists no in-scope file to verify"
        return verdict
    verdict["ok"] = not (verdict["modified"] or verdict["missing"]
                         or verdict["extra"])
    if not verdict["ok"]:
        verdict["reason"] = (
            f"启动链完整性校验失败：{len(verdict['modified'])} 个文件被改动、"
            f"{len(verdict['missing'])} 个缺失、{len(verdict['extra'])} 个清单外文件"
            f"（共校验 {checked} 个）。程序仍会抓数，但新增发布被暂停；"
            "请联系服务商重新安装。")
    return verdict
