#!/usr/bin/env python3
"""Build and gate OTA patches from git history (1.2.219).

Why this exists: until 1.2.218 the patch zip + manifest were produced outside
the repo.  1.2.218 declared ``min_version`` 1.2.188 but only carried the files
changed since 1.2.192; a 1.2.188-1.2.191 install that applied it could not
import ``winport.process_identity``, failed its health probe and rolled back on
every check.  The updater refreshes MANIFEST.json only for patched files, so
the on-box integrity check cannot see stale ones.  Completeness must therefore
be proven at build time.

Release points come from git: every commit whose
``publish-control/client/VERSION`` reads X is a candidate source for version X
(the newest such commit is used).  Versions with no commit in git (shipped
from elsewhere) cannot be checked and are reported as UNKNOWN.

Commands
  build  --min-version 1.2.192 [--target HEAD] --out DIR [--url-base URL]
         [--sign-seed-file seed.hex] [--notes TEXT]
         Patch = union of shippable files changed between EVERY known release
         point >= min_version and the target, so each of them is complete.
  check  --manifest M --zip Z [--target REF] [--simulate]
         Zip hash/size, every file == target source bytes, signature against
         the target's config/vendor_pubkey.hex, version == target VERSION, and
         per release point >= min_version: changed files subset of patch.
         --simulate overlays the zip on each release point's client tree and
         requires every winport module to import and ``-m winport --help``
         to exit 0.
  gate   [--target HEAD] [--simulate]
         CI form: build from ota_policy.json into a temp dir, then check.

Exit codes: 0 pass, 1 check failed, 2 usage / environment error.
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
import zipfile
from datetime import datetime, timezone
from pathlib import Path

CLIENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLIENT / "agent"))
sys.path.insert(0, str(CLIENT / "tools"))

from winport import updater  # noqa: E402

CLIENT_PREFIX = "publish-control/client/"
VERSION_PATH = CLIENT_PREFIX + "VERSION"
POLICY_PATH = CLIENT / "tools" / "ota_policy.json"
DEV_PREFIXES = ("tests/", "tools/", "installer/", "materials.lock.json")  # build-time only: never in the package
DOC_SUFFIXES = (".md", ".txt", ".pdf", ".png", ".jpg")   # not read by the program
ZIP_DATE = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o644


class GitError(RuntimeError):
    pass


def git(repo, *args) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if proc.returncode != 0:
        raise GitError("git %s: %s" % (" ".join(args),
                                       proc.stderr.decode("utf-8", "replace").strip()))
    return proc.stdout


def repo_root(start: Path) -> Path:
    return Path(git(start, "rev-parse", "--show-toplevel").decode().strip())


def read_at(repo, ref, path) -> bytes:
    return git(repo, "show", "%s:%s" % (ref, path))


def version_at(repo, ref) -> str:
    return read_at(repo, ref, VERSION_PATH).decode("utf-8-sig").strip()


def vt(version: str) -> tuple:
    return updater.parse_version(version)


def release_points(repo, target) -> dict:
    """version -> newest commit (ancestor of target) whose VERSION reads it."""
    commits = git(repo, "rev-list", target).decode().split()
    if not commits:
        return {}
    requests = "".join("%s:%s\n" % (c, VERSION_PATH) for c in commits).encode()
    proc = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch"],
                          input=requests, capture_output=True, check=True)
    out, pos, points = proc.stdout, 0, {}
    for commit in commits:
        nl = out.index(b"\n", pos)
        header = out[pos:nl].decode()
        pos = nl + 1
        if header.endswith("missing"):
            continue
        size = int(header.split()[2])
        body = out[pos:pos + size]
        pos += size + 1
        text = body.decode("utf-8-sig", "replace").strip()
        try:
            vt(text)
        except updater.UpdateError:
            continue
        points.setdefault(text, commit)          # rev-list is newest first
    return points


def shippable(rel: str, include_dev: bool) -> str | None:
    """Program-relative path the updater may write, or None."""
    try:
        rel = updater.normalize_rel_path(rel)
    except updater.UpdateError:
        return None
    lower = rel.lower()
    if lower in {p.lower() for p in updater.PROTECTED_FILES}:
        return None
    if lower.startswith(tuple(p.lower() for p in updater.PROTECTED_PREFIXES)):
        return None
    if not include_dev and lower.startswith(DEV_PREFIXES):
        return None
    return rel


def changed(repo, base, target, include_dev):
    """(shippable, deleted, code outside the updatable area, docs outside it)."""
    raw = git(repo, "diff", "--name-status", "-z", "--no-renames", base, target,
              "--", CLIENT_PREFIX).decode("utf-8").split("\0")
    ship, deleted, blocked, docs = set(), set(), set(), set()
    it = iter([x for x in raw if x != ""])
    for status in it:
        path = next(it)
        rel = path[len(CLIENT_PREFIX):]
        if rel in ("VERSION", "MANIFEST.json", "SHA256SUMS.txt"):
            continue
        ok = shippable(rel, include_dev)
        if status.startswith("D"):
            if ok:
                deleted.add(ok)
            continue
        if ok:
            ship.add(ok)
        elif not include_dev and rel.lower().startswith(DEV_PREFIXES):
            continue
        elif rel.lower().endswith(DOC_SUFFIXES):
            docs.add(rel)
        else:
            blocked.add(rel)
    return ship, deleted, blocked, docs


def in_range(points, min_version, target_version):
    lo, hi = vt(min_version), vt(target_version)
    return {v: c for v, c in points.items() if lo <= vt(v) < hi}


def unknown_versions(points, min_version, target_version):
    """Patch numbers in [min, target) that never appear in git."""
    lo, hi = vt(min_version), vt(target_version)
    if lo[:3] != hi[:3] and lo[:2] == hi[:2]:
        known = {vt(v)[2] for v in points if vt(v)[:2] == lo[:2]}
        return ["%d.%d.%d" % (lo[0], lo[1], n) for n in range(lo[2], hi[2])
                if n not in known]
    return []


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- build
def build(repo, target, min_version, out_dir: Path, *, url_base, notes,
          include_dev=False, sign_seed=None, released_at=None) -> dict:
    target_version = version_at(repo, target)
    points = release_points(repo, target)
    if min_version not in points:
        raise SystemExit("min_version %s has no commit in git history of %s; "
                         "cannot prove the patch is complete for it"
                         % (min_version, target))
    bases = in_range(points, min_version, target_version)
    files, problems = set(), []
    for version, commit in sorted(bases.items(), key=lambda kv: vt(kv[0])):
        ship, _deleted, blocked, _docs = changed(repo, commit, target, include_dev)
        files |= ship
        if blocked:
            problems.append("%s: changed outside the updatable area (needs full "
                            "installer): %s" % (version, ", ".join(sorted(blocked))))
    if problems:
        raise SystemExit("\n".join(problems))
    if not files:
        raise SystemExit("nothing to ship between %s and %s" % (min_version, target))

    out_dir.mkdir(parents=True, exist_ok=True)
    zip_name = "patch-%s.zip" % target_version
    zip_path = out_dir / zip_name
    entries = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(files):
            data = read_at(repo, target, CLIENT_PREFIX + rel)
            info = zipfile.ZipInfo(rel, date_time=ZIP_DATE)
            info.external_attr = (0o100000 | FILE_MODE) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
            entries.append({"path": rel, "sha256": sha256(data),
                            "size": len(data), "mode": FILE_MODE})
    zip_bytes = zip_path.read_bytes()
    manifest = {
        "version": target_version,
        "min_version": min_version,
        "schema_version": "1.0",
        "notes": notes or "",
        "released_at": released_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": entries,
        "patch_zip_url": (url_base.rstrip("/") + "/" + zip_name) if url_base else zip_name,
        "patch_zip_sha256": sha256(zip_bytes),
        "patch_zip_size": len(zip_bytes),
        "source_commit": git(repo, "rev-parse", target).decode().strip(),
    }
    if sign_seed is not None:
        import devsign
        public = devsign.publickey(sign_seed)
        manifest["signature"] = devsign.sign(updater.manifest_payload(manifest),
                                             sign_seed, public).hex()
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "manifest.payload.sha256").write_text(
        sha256(updater.manifest_payload(manifest)) + "\n", encoding="utf-8")
    return manifest


# --------------------------------------------------------------------------- check
def _simulate(repo, commit, zip_path: Path, python: str) -> str | None:
    """Overlay the patch on a release point's client tree; None if it starts."""
    with tempfile.TemporaryDirectory(prefix="ota-sim-") as tmp:
        tmp = Path(tmp)
        archive = subprocess.run(["git", "-C", str(repo), "archive", commit,
                                  "publish-control/client"], capture_output=True, check=True)
        subprocess.run(["tar", "-x", "-C", str(tmp)], input=archive.stdout, check=True)
        client = tmp / "publish-control" / "client"
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                dest = client / updater.normalize_rel_path(info.filename)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(info))
        env = dict(os.environ, PYTHONPATH=str(client / "agent"), PYTHONUTF8="1",
                   PYTHONDONTWRITEBYTECODE="1")
        probe = ("import importlib,pkgutil,winport\n"
                 "bad=[]\n"
                 "for m in pkgutil.iter_modules(winport.__path__):\n"
                 "    try: importlib.import_module('winport.'+m.name)\n"
                 "    except Exception as e: bad.append('%s: %s: %s'%(m.name,type(e).__name__,e))\n"
                 "print('\\n'.join(bad)); raise SystemExit(1 if bad else 0)\n")
        proc = subprocess.run([python, "-c", probe], cwd=str(client), env=env,
                              capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            return "module import failed: " + (proc.stdout + proc.stderr).strip()[-600:]
        proc = subprocess.run([python, "-m", "winport", "--help"], cwd=str(client),
                              env=env, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            return "`python -m winport --help` rc=%d: %s" % (
                proc.returncode, (proc.stdout + proc.stderr).strip()[-600:])
    return None


def check(repo, target, manifest_path: Path, zip_path: Path, *, simulate=False,
          include_dev=False, require_signature=True, python=sys.executable) -> dict:
    fails, warns, notes = [], [], []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    zip_bytes = zip_path.read_bytes()
    if sha256(zip_bytes) != str(manifest.get("patch_zip_sha256", "")).lower():
        fails.append("patch zip sha256 != manifest.patch_zip_sha256")
    if len(zip_bytes) != manifest.get("patch_zip_size"):
        fails.append("patch zip size != manifest.patch_zip_size")

    target_version = version_at(repo, target)
    if manifest.get("version") != target_version:
        fails.append("manifest version %r != target VERSION %r"
                     % (manifest.get("version"), target_version))

    if "signature" in manifest:
        try:
            pub = bytes.fromhex(read_at(repo, target, CLIENT_PREFIX
                                        + "config/vendor_pubkey.hex").decode().strip())
        except (GitError, ValueError) as exc:
            fails.append("vendor_pubkey.hex unreadable at target: %s" % exc)
        else:
            if not updater.verify_manifest_signature(manifest, pub):
                fails.append("signature does not verify with the target's vendor_pubkey.hex")
            else:
                notes.append("signature OK (target config/vendor_pubkey.hex)")
    elif require_signature:
        fails.append("manifest is unsigned")
    else:
        warns.append("manifest is unsigned (allowed by --unsigned-ok)")

    listed = {}
    for entry in manifest.get("files") or []:
        try:
            rel = updater.normalize_rel_path(entry.get("path"))
        except updater.UpdateError as exc:
            fails.append(str(exc))
            continue
        listed[rel] = entry
    with zipfile.ZipFile(zip_path) as zf:
        names = {i.filename for i in zf.infolist() if not i.is_dir()}
        if names != set(listed):
            fails.append("zip entries != manifest files (only in zip: %s; only in "
                         "manifest: %s)" % (sorted(names - set(listed)),
                                            sorted(set(listed) - names)))
        for rel, entry in sorted(listed.items()):
            if rel not in names:
                continue
            data = zf.read(rel)
            if sha256(data) != entry.get("sha256") or len(data) != entry.get("size"):
                fails.append("%s: bytes do not match manifest sha256/size" % rel)
            try:
                src = read_at(repo, target, CLIENT_PREFIX + rel)
            except GitError:
                fails.append("%s: not present in target source" % rel)
                continue
            if src != data:
                fails.append("%s: differs from target source" % rel)
    notes.append("%d files checked against %s" % (len(listed), target))

    points = release_points(repo, target)
    min_version = manifest.get("min_version", "")
    try:
        bases = in_range(points, min_version, target_version)
    except updater.UpdateError as exc:
        fails.append("bad min_version: %s" % exc)
        bases = {}
    if min_version and min_version not in points:
        warns.append("min_version %s itself has no commit in git" % min_version)
    gaps = unknown_versions(points, min_version, target_version) if bases else []
    if gaps:
        warns.append("versions in range with no source in git (cannot be checked; "
                     "keep their installs on the full installer): " + ", ".join(gaps))
    per_version = {}
    for version, commit in sorted(bases.items(), key=lambda kv: vt(kv[0])):
        ship, deleted, blocked, docs = changed(repo, commit, target, include_dev)
        if docs:
            warns.append("%s -> %s: docs outside the updatable area stay old "
                         "(full installer refreshes them): %s"
                         % (version, target_version, ", ".join(sorted(docs))))
        missing = sorted(ship - set(listed))
        status = "OK"
        if missing:
            status = "MISSING"
            fails.append("%s -> %s: patch lacks %s" % (version, target_version,
                                                       ", ".join(missing)))
        if blocked:
            status = "BLOCKED"
            fails.append("%s -> %s: changes outside updatable area: %s"
                         % (version, target_version, ", ".join(sorted(blocked))))
        if deleted:
            warns.append("%s -> %s: deleted upstream, will linger: %s"
                         % (version, target_version, ", ".join(sorted(deleted))))
        if simulate and status == "OK":
            err = _simulate(repo, commit, zip_path, python)
            if err:
                status = "START_FAIL"
                fails.append("%s + patch: %s" % (version, err))
        per_version[version] = {"commit": commit[:12], "status": status,
                                "missing": missing}
    return {"ok": not fails, "fails": fails, "warns": warns, "notes": notes,
            "per_version": per_version, "target_version": target_version}


def _print(report):
    for version, row in report["per_version"].items():
        print("  %-10s %-11s %s%s" % (version, row["status"], row["commit"],
                                      ("  missing: " + ", ".join(row["missing"]))
                                      if row["missing"] else ""))
    for n in report["notes"]:
        print("NOTE  " + n)
    for w in report["warns"]:
        print("WARN  " + w)
    for f in report["fails"]:
        print("FAIL  " + f)
    print("OTA CHECK: %s (target %s)" % ("PASS" if report["ok"] else "FAIL",
                                          report["target_version"]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=str(CLIENT))
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--target", default="HEAD")
    b.add_argument("--min-version", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--url-base", default="")
    b.add_argument("--notes", default="")
    b.add_argument("--include-dev", action="store_true")
    b.add_argument("--sign-seed-file", help="hex Ed25519 seed (32 bytes); omit to sign offline")
    c = sub.add_parser("check")
    c.add_argument("--target", default="HEAD")
    c.add_argument("--manifest", required=True)
    c.add_argument("--zip", required=True)
    c.add_argument("--simulate", action="store_true")
    c.add_argument("--include-dev", action="store_true")
    c.add_argument("--unsigned-ok", action="store_true")
    g = sub.add_parser("gate")
    g.add_argument("--target", default="HEAD")
    g.add_argument("--simulate", action="store_true")
    a = ap.parse_args(argv)
    try:
        repo = repo_root(Path(a.repo))
        if a.cmd == "build":
            seed = None
            if a.sign_seed_file:
                seed = bytes.fromhex(Path(a.sign_seed_file).read_text().strip())
                if len(seed) != 32:
                    print("seed must be 32 bytes hex", file=sys.stderr)
                    return 2
            m = build(repo, a.target, a.min_version, Path(a.out), url_base=a.url_base,
                      notes=a.notes, include_dev=a.include_dev, sign_seed=seed)
            print("built %s: %d files, zip sha256 %s%s" % (
                m["version"], len(m["files"]), m["patch_zip_sha256"],
                "" if "signature" in m else " (UNSIGNED: sign manifest.payload offline)"))
            return 0
        if a.cmd == "check":
            report = check(repo, a.target, Path(a.manifest), Path(a.zip),
                           simulate=a.simulate, include_dev=a.include_dev,
                           require_signature=not a.unsigned_ok)
        else:
            policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
            with tempfile.TemporaryDirectory(prefix="ota-gate-") as tmp:
                out = Path(tmp)
                build(repo, a.target, policy["min_version"], out, url_base="",
                      notes="ci gate", include_dev=False)
                report = check(repo, a.target, out / "manifest.json",
                               next(out.glob("patch-*.zip")), simulate=a.simulate,
                               require_signature=False)
                shutil.rmtree(out, ignore_errors=True)
        _print(report)
        return 0 if report["ok"] else 1
    except (GitError, updater.UpdateError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
