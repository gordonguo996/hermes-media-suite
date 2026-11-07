"""Signed, in-place incremental updates with a detached restart supervisor.

Protocol summary
----------------
The control plane publishes a JSON manifest at a static COS URL.  The manifest
is signed with the SAME Ed25519 vendor key that verifies licences and approval
receipts (``config/vendor_pubkey.hex``).  It lists one changed-file payload zip;
every file is bound by relative path, size and SHA-256.

The running agent only downloads to ``<data>/run/update``.  Once the patch has
been fully verified and staged, it starts a detached supervisor (this module,
already loaded from the OLD version), then asks the normal agent lifecycle to
stop.  The supervisor waits for the single-instance lock and panel port to be
released, commits each file with ``os.replace``, refreshes MANIFEST.json and
VERSION, and starts the SAME ``-m winport --data <data>`` command on the SAME
data directory.  The new instance therefore reuses the persisted relay port,
token and settings, so the reverse tunnel comes back without a desktop click.

The supervisor then proves healthz + readiness marker + version + startup-chain
integrity + (when enabled) the local remote-channel control service.  Any
failure restores the per-file backup from
``run/update/backup-<from_version>/`` and starts the old version again.

Security boundary: this module never touches ``license.json``, remote tokens,
publish holds, the Feishu approval state or idempotency ledgers.  It cannot
re-authorise a machine, clear a hold or skip an integrity check.  Updates are
limited to the first-party program tree; third-party ``runtime/``, ``browsers/``,
``wheels/`` and ``bin/`` payloads are refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import signal
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import ed25519_pure
from .hashing import sha256_bytes, sha256_file
from .integrity import verify_program
from .jsonio import read_json, write_json, write_text_atomic
from .license import require_vendor_public_key
from .lock import FileLock, lock_is_held
from .paths import Layout, install_root
from .process_identity import descendant_pids, is_descendant, process_parents
from .settings import load_settings
from .subprocess_text import hidden_window_options

SCHEMA = 1
MANIFEST_LIMIT = 1024 * 1024
PATCH_LIMIT = 128 * 1024 * 1024
FILE_LIMIT = 64 * 1024 * 1024

ALLOWED_PREFIXES = ("agent/", "scripts/", "config/", "assets/", "tools/", "tests/")
ALLOWED_ROOT_FILES = {
    "run_panel.py", "requirements.txt", "bootstrap.bat", "selftest.bat",
    "show-machine-code.bat", "import-license.bat", "fix-blackscreen.bat",
}
PROTECTED_PREFIXES = (
    "runtime/", "browsers/", "wheels/", "bin/", "chrome-offline/", "license/",
)
PROTECTED_FILES = {
    "config/vendor_pubkey.hex", "config/receipt_pubkey.hex",
    "config/vendor_pubkey.hex.oldkey", "config/vendor_pubkey.hex.tmpl",
    "config/receipt_pubkey.hex.tmpl", "config/relay_key", "config/relay.json",
    "config/client_key", "config/activation.json",
    "VERSION", "MANIFEST.json", "SHA256SUMS.txt",
}
RESERVED_NAMES = {
    "con", "prn", "aux", "nul", *{f"com{i}" for i in range(1, 10)},
    *{f"lpt{i}" for i in range(1, 10)},
}

INFLIGHT_STATUSES = ("running", "pending", "needs_reconcile")

STATUS_TEXT = {
    "checking": "正在检查更新",
    "downloading": "正在准备更新",
    "ready": "正在准备更新",
    "applying": "正在更新，完成后自动恢复",
    "restarting": "正在更新，完成后自动恢复",
    "updated": "已更新到 {version}",
    "rolled_back": "更新未完成，已自动恢复到 {version}",
    "failed": "更新暂时失败，稍后自动重试",
    "rollback_failed": "更新未完成，请重启电脑后联系服务商",
}


class UpdateError(Exception):
    """A refused or failed update.  The message is safe for the local log."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> float:
    return time.time()


def _safe_reason(exc) -> str:
    text = f"{type(exc).__name__}: {exc}" if not isinstance(exc, str) else exc
    text = re.sub(r"(?i)(https?://[^\s?#]+)\?[^\s#]*", r"\1?<redacted>", text)
    text = re.sub(r"(?i)(token|signature|authorization|secret)=[^\s&;]+",
                  r"\1=<redacted>", text)
    return text[:500]


def update_dir(layout: Layout) -> Path:
    path = Path(layout.run) / "update"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _log_path(layout: Layout) -> Path:
    return update_dir(layout) / "update.log"


def log_event(layout: Layout, message: str) -> None:
    """Append one timestamped line to run/update/update.log."""
    path = _log_path(layout)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n"
    lock = FileLock(update_dir(layout) / "update-log.lock")
    try:
        if lock.acquire(timeout=2.0):
            with path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
    except OSError:
        pass
    finally:
        lock.release()


def write_status(layout: Layout, state: str, *, version: str = "",
                 detail: str = "", **extra) -> dict:
    text = STATUS_TEXT.get(state, "")
    if state == "idle":
        text = ""
    elif state in ("updated", "rolled_back") and version:
        text = text.format(version=version)
    payload = {
        "schema": SCHEMA,
        "state": state,
        "text": text,
        "version": version,
        "detail": detail,
        "ts": _now_iso(),
    }
    payload.update(extra)
    try:
        write_json(update_dir(layout) / "status.json", payload)
    except OSError as exc:
        log_event(layout, f"status write failed: {_safe_reason(exc)}")
    return payload


def read_status(layout: Layout) -> dict:
    try:
        value = read_json(update_dir(layout) / "status.json", default={})
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def update_banner(layout: Layout) -> dict:
    """One customer-safe line for the panel.  Never contains paths/ports."""
    status = read_status(layout)
    text = str(status.get("text") or "").strip()
    state = str(status.get("state") or "")
    if not text:
        return {"state": state, "text": "", "level": "muted"}
    level = "ok" if state == "updated" else (
        "warn" if state in ("checking", "downloading", "ready", "applying",
                            "restarting", "rolled_back") else "bad")
    return {"state": state, "text": text, "level": level}


# --------------------------------------------------------------------------- #
# Configuration / versions
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG = {
    "enabled": True,
    "manifest_url": "",
    "check_interval_seconds": 3600,
    "initial_delay_seconds": 180,
    "request_timeout_seconds": 30,
    "download_retries": 4,
    "failed_retry_seconds": 21600,
    "health_timeout_seconds": 60,
    "tunnel_timeout_seconds": 30,
    "allow_insecure_http": False,
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        out[key] = value
    return out


def load_update_config(layout: Layout) -> dict:
    """settings.json ``updates`` plus optional data/config/update.json."""
    from_settings = load_settings(layout.settings_file).get("updates") or {}
    from_file = read_json(Path(layout.config) / "update.json", default={}) or {}
    if not isinstance(from_settings, dict) or not isinstance(from_file, dict):
        raise UpdateError("update configuration must be an object")
    cfg = _merge(DEFAULT_CONFIG, from_settings)
    cfg = _merge(cfg, from_file)
    if type(cfg.get("enabled")) is not bool:
        raise UpdateError("updates.enabled must be boolean")
    if not isinstance(cfg.get("manifest_url"), str):
        raise UpdateError("updates.manifest_url must be a string")
    for name in ("check_interval_seconds", "initial_delay_seconds",
                 "request_timeout_seconds", "download_retries",
                 "failed_retry_seconds", "health_timeout_seconds",
                 "tunnel_timeout_seconds"):
        try:
            value = int(cfg.get(name))
        except (TypeError, ValueError) as exc:
            raise UpdateError(f"updates.{name} must be an integer") from exc
        if value < 0:
            raise UpdateError(f"updates.{name} must be >= 0")
        cfg[name] = value
    if type(cfg.get("allow_insecure_http")) is not bool:
        raise UpdateError("updates.allow_insecure_http must be boolean")
    return cfg


def read_installed_version(program: Path) -> str:
    try:
        value = (Path(program) / "VERSION").read_text(encoding="utf-8-sig").strip()
    except OSError:
        value = ""
    if not value:
        from .version import VERSION
        value = VERSION
    return value


def parse_version(value) -> tuple:
    if not isinstance(value, str):
        raise UpdateError("version must be a string")
    text = value.strip()
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", text):
        raise UpdateError(f"invalid version: {value!r}")
    parts = [int(part) for part in text.split(".")]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts)


def version_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def _http_url_allowed(url: str, *, allow_insecure_http: bool = False) -> bool:
    try:
        parts = urllib.parse.urlsplit(str(url))
    except ValueError:
        return False
    if parts.scheme == "https" and parts.hostname:
        return True
    if (allow_insecure_http and parts.scheme == "http"
            and parts.hostname in ("127.0.0.1", "::1", "localhost")):
        return True
    return False


# --------------------------------------------------------------------------- #
# Manifest signature and shape
# --------------------------------------------------------------------------- #


def manifest_payload(manifest: dict) -> bytes:
    body = {k: v for k, v in manifest.items() if k != "signature"}
    return json.dumps(body, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def verify_manifest_signature(manifest: dict, public_key: bytes) -> bool:
    signature = manifest.get("signature")
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-fA-F]{128}", signature):
        return False
    ok, _reason = ed25519_pure.public_key_valid(bytes(public_key))
    if not ok:
        return False
    try:
        raw = bytes.fromhex(signature)
    except ValueError:
        return False
    return ed25519_pure.verify(raw, manifest_payload(manifest), bytes(public_key))


def normalize_rel_path(value) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise UpdateError("file path is empty or invalid")
    if any(ord(char) < 32 for char in value):
        raise UpdateError("file path contains a control character")
    if "\\" in value:
        raise UpdateError(f"file path must use forward slashes: {value!r}")
    pure = PurePosixPath(value)
    if pure.is_absolute() or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise UpdateError(f"absolute path refused: {value!r}")
    parts = pure.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise UpdateError(f"path traversal refused: {value!r}")
    for part in parts:
        if part.rstrip(" .") != part:
            raise UpdateError(f"Windows-unsafe path segment refused: {value!r}")
        if ":" in part:
            raise UpdateError(f"Windows alternate-data-stream path refused: {value!r}")
        if part.split(".")[0].lower() in RESERVED_NAMES:
            raise UpdateError(f"reserved Windows name refused: {value!r}")
    rel = pure.as_posix()
    lower = rel.lower()
    if lower in PROTECTED_FILES or lower.startswith(PROTECTED_PREFIXES):
        raise UpdateError(f"protected path refused: {rel}")
    if lower not in {name.lower() for name in ALLOWED_ROOT_FILES} \
            and not lower.startswith(ALLOWED_PREFIXES):
        raise UpdateError(f"path outside the updatable program area: {rel}")
    return rel


def validate_manifest(manifest: dict, *, current_version: str,
                      public_key: bytes | None = None,
                      allow_insecure_http: bool = False) -> dict:
    """Shape + signature + downgrade gate.  Never partially trusts a manifest."""
    if not isinstance(manifest, dict):
        return {"ok": False, "reason": "manifest must be a JSON object"}
    required = ("version", "files", "patch_zip_url", "patch_zip_sha256",
                "min_version", "signature")
    missing = [name for name in required if not manifest.get(name)]
    if missing:
        return {"ok": False, "reason": "manifest missing: " + ", ".join(missing)}
    if public_key is None:
        try:
            public_key = require_vendor_public_key()
        except Exception as exc:
            return {"ok": False, "reason": f"vendor public key unavailable: {_safe_reason(exc)}"}
    if not isinstance(public_key, (bytes, bytearray)) or len(public_key) != 32:
        return {"ok": False, "reason": "vendor public key must be 32 bytes"}
    if not verify_manifest_signature(manifest, bytes(public_key)):
        return {"ok": False, "reason": "signature does not verify with vendor key"}

    try:
        target = parse_version(manifest["version"])
        current = parse_version(current_version)
        minimum = parse_version(manifest["min_version"])
    except UpdateError as exc:
        return {"ok": False, "reason": str(exc)}
    if target == current:
        return {"ok": False, "no_update": True,
                "reason": f"already current: {current_version}"}
    if target < current:
        return {"ok": False, "reason": f"downgrade refused: {manifest['version']} <= {current_version}"}
    if current < minimum:
        return {"ok": False,
                "reason": f"version too old to cross: {current_version} < {manifest['min_version']}"}

    zip_hash = manifest.get("patch_zip_sha256")
    if not isinstance(zip_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", zip_hash.lower()):
        return {"ok": False, "reason": "patch_zip_sha256 is not a sha256 hex digest"}
    url = manifest.get("patch_zip_url")
    if not _http_url_allowed(url, allow_insecure_http=allow_insecure_http):
        return {"ok": False, "reason": "patch_zip_url must use HTTPS (loopback HTTP only in local tests)"}

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        return {"ok": False, "reason": "files must be a non-empty list"}
    seen = set()
    normalized = []
    for item in files:
        if not isinstance(item, dict):
            return {"ok": False, "reason": "each files[] entry must be an object"}
        try:
            rel = normalize_rel_path(item.get("path"))
        except UpdateError as exc:
            return {"ok": False, "reason": str(exc)}
        key = rel.lower()
        if key in seen:
            return {"ok": False, "reason": f"duplicate file path: {rel}"}
        seen.add(key)
        digest = item.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest.lower()):
            return {"ok": False, "reason": f"bad sha256 for {rel}"}
        if "mode" not in item:
            return {"ok": False, "reason": f"mode missing for {rel}"}
        try:
            size = int(item.get("size"))
            mode = int(item.get("mode"))
        except (TypeError, ValueError):
            return {"ok": False, "reason": f"bad size/mode for {rel}"}
        if size < 0 or size > FILE_LIMIT:
            return {"ok": False, "reason": f"file size out of range: {rel}"}
        if mode < 0 or mode > 0o777:
            return {"ok": False, "reason": f"file mode out of range: {rel}"}
        normalized.append({"path": rel, "sha256": digest.lower(),
                           "size": size, "mode": mode})
    return {"ok": True, "version": manifest["version"],
            "min_version": manifest["min_version"],
            "files": normalized, "public_key": bytes(public_key),
            "manifest": manifest}


# --------------------------------------------------------------------------- #
# Download / staging
# --------------------------------------------------------------------------- #


def _zip_member_path(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if "\\" in name:
        raise UpdateError(f"zip entry uses backslashes: {name!r}")
    return normalize_rel_path(name.rstrip("/")) if not info.is_dir() else name.rstrip("/")


def stage_patch(zip_path: Path, files: list, stage_dir: Path) -> list:
    """Validate the zip against the signed file list and extract to staging."""
    stage_dir = Path(stage_dir)
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)
    expected = {item["path"].lower(): item for item in files}
    staged = []
    total = 0
    try:
        with zipfile.ZipFile(zip_path) as archive:
            infos = archive.infolist()
            seen = set()
            for info in infos:
                if info.is_dir():
                    continue
                rel = _zip_member_path(info)
                key = rel.lower()
                if key not in expected:
                    raise UpdateError(f"patch contains an unlisted file: {rel}")
                if key in seen:
                    raise UpdateError(f"patch contains a duplicate file: {rel}")
                seen.add(key)
                if info.flag_bits & 0x1:
                    raise UpdateError(f"encrypted zip entry refused: {rel}")
                mode = (info.external_attr >> 16) & 0xFFFF
                if mode and stat.S_ISLNK(mode):
                    raise UpdateError(f"symlink zip entry refused: {rel}")
                item = expected[key]
                if info.file_size != item["size"]:
                    raise UpdateError(f"zip size mismatch for {rel}")
                total += info.file_size
                if total > PATCH_LIMIT:
                    raise UpdateError("patch expands beyond the allowed size")
                target = stage_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                written = 0
                with archive.open(info) as source, target.open("wb") as output:
                    while True:
                        block = source.read(1 << 20)
                        if not block:
                            break
                        written += len(block)
                        if written > item["size"]:
                            raise UpdateError(f"zip entry exceeds declared size: {rel}")
                        digest.update(block)
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                if written != item["size"] or digest.hexdigest() != item["sha256"]:
                    raise UpdateError(f"file sha256 mismatch: {rel}")
                try:
                    os.chmod(target, item["mode"])
                except OSError:
                    pass
                staged.append({**item, "source": str(target)})
            missing = set(expected) - seen
            if missing:
                raise UpdateError("patch is missing: " + ", ".join(sorted(missing)))
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError(f"patch zip rejected: {_safe_reason(exc)}") from exc
    if not staged:
        raise UpdateError("patch zip contains no files")
    return staged


# --------------------------------------------------------------------------- #
# Idle detection / backup / atomic commit
# --------------------------------------------------------------------------- #


def _queue_busy(layout: Layout) -> str:
    queue_lock = Path(layout.run) / "publish-queue.lock"
    if queue_lock.exists() and lock_is_held(queue_lock):
        return "publish queue lock is held"
    for path in sorted((Path(layout.run) / "publish-queue").glob("*.json")):
        try:
            job = read_json(path, default={})
        except (OSError, ValueError):
            return f"unreadable publish job: {path.name}"
        if isinstance(job, dict) and job.get("status") in INFLIGHT_STATUSES:
            return f"publish job {job.get('job_id') or path.stem} is {job.get('status')}"
    lock_dir = Path(layout.run) / "publish-locks"
    if lock_dir.exists():
        for path in lock_dir.glob("*.lock"):
            if lock_is_held(path):
                return "a publish environment lock is held"
    return ""


def idle_verdict(layout: Layout) -> dict:
    reason = _queue_busy(layout)
    return {"idle": not reason, "reason": reason}


def _copy_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".copy-", suffix=".tmp")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "wb") as output, Path(source).open("rb") as source_fh:
            while True:
                block = source_fh.read(1 << 20)
                if not block:
                    break
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        try:
            shutil.copystat(source, tmp_path)
        except OSError:
            pass
        os.replace(tmp_path, target)
    finally:
        tmp_path.unlink(missing_ok=True)


def _write_atomic_bytes(target: Path, data: bytes, mode: int = 0o644) -> None:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".new-", suffix=".tmp")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp_path, mode)
        except OSError:
            pass
        os.replace(tmp_path, target)
    finally:
        tmp_path.unlink(missing_ok=True)


def _cleanup_atomic_temps(program: Path) -> None:
    """Remove temporary files left behind by a killed atomic write/rollback."""
    program = Path(program)
    for pattern in (".new-*.tmp", ".copy-*.tmp", ".tmp-*.json", ".tmp-*.txt"):
        candidates = list(program.glob(pattern))
        for prefix in ALLOWED_PREFIXES:
            base = program / prefix.rstrip("/")
            if base.is_dir():
                candidates.extend(base.rglob(pattern))
        for path in candidates:
            try:
                path.unlink()
            except OSError:
                pass


def create_backup(program: Path, backup_dir: Path, files: list,
                  *, from_version: str,
                  meta_files=("VERSION", "MANIFEST.json", "SHA256SUMS.txt")) -> dict:
    """Snapshot every destination (and VERSION/MANIFEST) before any replace."""
    program = Path(program)
    backup_dir = Path(backup_dir)
    marker = backup_dir / "backup-complete.json"
    if marker.exists():
        existing = read_json(marker, default={"ok": False})
        if not isinstance(existing, dict):
            existing = {}
        expected_paths = {item.get("path") for item in files
                          if isinstance(item, dict)}
        existing_paths = {entry.get("path") for entry in
                          (existing.get("files") or [])
                          if isinstance(entry, dict)}
        valid = (existing.get("ok") is True
                 and existing.get("from_version") == from_version
                 and existing_paths == expected_paths)
        if valid:
            entries = []
            for key in ("files", "meta"):
                value = existing.get(key)
                if isinstance(value, list):
                    entries.extend(value)
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("had_previous"):
                    continue
                backup = Path(entry.get("backup") or "")
                if not backup.is_file() or sha256_file(backup) != entry.get("sha256"):
                    valid = False
                    break
        if valid:
            return existing
        shutil.rmtree(backup_dir)
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for item in files:
        rel = item["path"]
        destination = program / rel
        exists = destination.is_file()
        digest = ""
        backup = backup_dir / rel
        if exists:
            digest = sha256_file(destination)
            _copy_atomic(destination, backup)
            if sha256_file(backup) != digest:
                raise UpdateError(f"backup verification failed: {rel}")
        entries.append({"path": rel, "had_previous": exists,
                        "backup": str(backup), "sha256": digest,
                        "size": destination.stat().st_size if exists else 0})
    meta = []
    for rel in meta_files:
        source = program / rel
        exists = source.is_file()
        backup = backup_dir / rel
        digest = ""
        if exists:
            digest = sha256_file(source)
            _copy_atomic(source, backup)
        meta.append({"path": rel, "had_previous": exists, "backup": str(backup),
                     "sha256": digest})
    record = {"ok": True, "from_version": from_version,
              "files": entries, "meta": meta, "ts": _now_iso()}
    write_json(marker, record)
    return record


def _refresh_program_manifest(program: Path, staged: list, target_version: str) -> None:
    manifest_path = Path(program) / "MANIFEST.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise UpdateError(f"local MANIFEST.json unreadable: {_safe_reason(exc)}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise UpdateError("local MANIFEST.json has no file map")
    files = manifest["files"]
    for item in staged:
        files[item["path"]] = {"sha256": item["sha256"], "bytes": item["size"]}
    version_bytes = (target_version + "\n").encode("utf-8")
    files["VERSION"] = {"sha256": sha256_bytes(version_bytes), "bytes": len(version_bytes)}
    manifest["version"] = target_version
    write_json(manifest_path, manifest)
    _refresh_sha256sums(program, staged, target_version)


def _refresh_sha256sums(program: Path, staged: list, target_version: str) -> None:
    """Keep the shipped, human-readable index consistent with MANIFEST.json."""
    path = Path(program) / "SHA256SUMS.txt"
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    updates = {item["path"]: item["sha256"] for item in staged}
    updates["VERSION"] = sha256_bytes((target_version + "\n").encode("utf-8"))
    updates["MANIFEST.json"] = sha256_file(Path(program) / "MANIFEST.json")
    order = []
    values = {}
    for line in lines:
        if len(line) >= 66 and line[64:66] == "  ":
            rel = line[66:]
            if rel not in values:
                order.append(rel)
            values[rel] = line[:64].lower()
    for rel, digest in updates.items():
        if rel not in values:
            order.append(rel)
        values[rel] = digest
    text = "".join(f"{values[rel]}  {rel}\n" for rel in order)
    write_text_atomic(path, text, encoding="utf-8", newline="\n")


def _write_version(program: Path, version: str) -> None:
    write_text_atomic(Path(program) / "VERSION", version + "\n", encoding="utf-8")


def apply_update(job: dict, *, fail_after: int | None = None) -> dict:
    """Commit staged files in order; caller MUST rollback on any exception."""
    program = Path(job["program"])
    backup_dir = Path(job["backup_dir"])
    backup = create_backup(program, backup_dir, job["files"],
                           from_version=job["from_version"])
    by_path = {entry["path"]: entry for entry in backup["files"]}
    job["status"] = "applying"
    job["backup"] = backup
    write_json(job["journal_path"], job)
    applied = 0
    for item in job["files"]:
        rel = normalize_rel_path(item["path"])
        source = Path(item["source"])
        try:
            source.relative_to(Path(job["stage_dir"]))
        except ValueError as exc:
            raise UpdateError(f"staged source escaped the staging directory: {rel}") from exc
        if not source.is_file() or sha256_file(source) != item["sha256"]:
            raise UpdateError(f"staged file changed before commit: {rel}")
        destination = program / rel
        data = source.read_bytes()
        if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise UpdateError(f"staged file hash mismatch at commit: {rel}")
        _write_atomic_bytes(destination, data, item.get("mode", 0o644))
        if sha256_file(destination) != item["sha256"]:
            raise UpdateError(f"post-replace verification failed: {rel}")
        applied += 1
        by_path[rel]["applied"] = True
        job["applied"] = applied
        write_json(job["journal_path"], job)
        if fail_after is not None and applied >= fail_after:
            raise UpdateError("injected interruption after file replacement")
    _refresh_program_manifest(program, job["files"], job["target_version"])
    _write_version(program, job["target_version"])
    job["status"] = "applied"
    job["applied"] = applied
    write_json(job["journal_path"], job)
    return job


def rollback_update(job: dict) -> dict:
    """Restore every backup in reverse order; safe to call more than once."""
    program = Path(job["program"])
    backup = job.get("backup") or read_json(Path(job["backup_dir"]) / "backup-complete.json",
                                            default={})
    # A crash can happen while the backup itself is being written, before the
    # complete marker exists.  Never interpret that as "these files did not
    # exist before": doing so would delete the working program during the
    # rollback attempt.  Refuse the restore and leave the mixed tree intact for
    # the next startup/recovery pass instead.
    if not isinstance(backup, dict) or backup.get("ok") is not True:
        raise UpdateError("backup is incomplete; rollback refused")
    expected_paths = {item.get("path") for item in (job.get("files") or [])
                      if isinstance(item, dict)}
    backup_paths = {entry.get("path") for entry in (backup.get("files") or [])
                    if isinstance(entry, dict)}
    if not expected_paths or not expected_paths.issubset(backup_paths):
        raise UpdateError("backup does not cover every changed file; rollback refused")
    if not isinstance(backup.get("meta"), list):
        raise UpdateError("backup metadata is incomplete; rollback refused")
    job["status"] = "restoring"
    write_json(job["journal_path"], job)
    file_entries = {entry["path"]: entry for entry in (backup.get("files") or [])}
    for item in reversed(job.get("files") or []):
        rel = item["path"]
        entry = file_entries.get(rel)
        destination = program / rel
        if entry and entry.get("had_previous"):
            source = Path(entry["backup"])
            if not source.is_file():
                raise UpdateError(f"backup file missing for rollback: {rel}")
            _copy_atomic(source, destination)
            if entry.get("sha256") and sha256_file(destination) != entry["sha256"]:
                raise UpdateError(f"rollback verification failed: {rel}")
        else:
            destination.unlink(missing_ok=True)
    for entry in reversed(backup.get("meta") or []):
        destination = program / entry["path"]
        if entry.get("had_previous"):
            source = Path(entry["backup"])
            if not source.is_file():
                raise UpdateError(f"backup metadata missing: {entry['path']}")
            _copy_atomic(source, destination)
            if entry.get("sha256") and sha256_file(destination) != entry["sha256"]:
                raise UpdateError(f"rollback metadata verification failed: {entry['path']}")
        else:
            destination.unlink(missing_ok=True)
    if not (program / "VERSION").is_file():
        _write_version(program, job["from_version"])
    job["status"] = "rolled_back"
    job["rolled_back_at"] = _now_iso()
    write_json(job["journal_path"], job)
    return job


def recover_interrupted(layout: Layout, program: Path | None = None) -> dict:
    """Roll back a crash left *before* VERSION was committed.

    If VERSION already equals the target, every replacement happened, so the
    new instance is allowed to start; the supervisor/next check owns the rest.
    """
    program = Path(program or install_root())
    job_path = update_dir(layout) / "transaction.json"
    _cleanup_atomic_temps(program)
    job = read_json(job_path, default=None)
    if not isinstance(job, dict) or job.get("status") not in ("applying", "restoring"):
        return {"recovered": False}
    if read_installed_version(program) == job.get("target_version"):
        return {"recovered": False, "reason": "target version already committed"}
    log_event(layout, "interrupted update detected before commit; rolling back")
    rollback_update(job)
    return {"recovered": True, "version": job.get("from_version")}


# --------------------------------------------------------------------------- #
# Detached supervisor
# --------------------------------------------------------------------------- #


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        import psutil  # type: ignore
        if not psutil.pid_exists(pid):
            return False
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except Exception:
            return True
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return True
    try:
        status_path = Path(f"/proc/{pid}/stat")
        if status_path.exists():
            fields = status_path.read_text(encoding="ascii", errors="replace").split()
            if len(fields) > 2 and fields[2] == "Z":
                return False
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _agent_lock_path(job: dict) -> Path:
    return Path(job["data"]) / "run" / "agent.lock"


def _wait_old_agent_stopped(job: dict, *, timeout: float = 90.0,
                            sleep=time.sleep) -> bool:
    deadline = time.monotonic() + timeout
    old_pid = job.get("old_pid")
    lock_path = _agent_lock_path(job)
    while time.monotonic() < deadline:
        alive = _pid_alive(old_pid)
        locked = lock_path.exists() and lock_is_held(lock_path)
        if not alive and not locked:
            return True
        sleep(0.25)
    return False


def _local_http_json(url: str, timeout: float = 2.0):
    parts = urllib.parse.urlsplit(str(url))
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "::1", "localhost"):
        return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            if getattr(response, "status", 200) != 200:
                return None
            data = response.read(64 * 1024)
        value = json.loads(data.decode("utf-8"))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _read_marker(job: dict) -> dict:
    try:
        value = read_json(Path(job["data"]) / "run" / "agent-ready.json", default={})
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _pid_evidence(record: dict) -> tuple[int, int]:
    """Return (supervisor-visible pid, panel-serving pid) or (0, 0).

    New records carry both pids plus ``pid_chain``.  The chain must start at
    the supervisor-visible pid and end at the process that owns the panel;
    malformed or contradictory evidence is rejected instead of being partially
    trusted.
    """
    try:
        owner_pid = int(record.get("pid") or 0)
        agent_pid = int(record.get("agent_pid") or owner_pid)
    except (TypeError, ValueError):
        return 0, 0
    if owner_pid <= 0 or agent_pid <= 0:
        return 0, 0
    chain = record.get("pid_chain")
    if chain is not None:
        if not isinstance(chain, (list, tuple)) or not chain:
            return 0, 0
        try:
            chain = [int(item) for item in chain]
        except (TypeError, ValueError):
            return 0, 0
        if any(item <= 0 for item in chain):
            return 0, 0
        if chain[0] != owner_pid or chain[-1] != agent_pid:
            return 0, 0
        if len(set(chain)) != len(chain):
            return 0, 0
    return owner_pid, agent_pid


def _pid_belongs(pid: int, owner_pid: int, agent_pid: int,
                 *, legacy: bool) -> bool:
    """Match a supervisor pid against the record's ownership evidence.

    The supervisor compares the pid it spawned with the record.  For new
    records the recorded owner (legacy ``pid``) and live panel process
    (``agent_pid``) must also form a real parent/child chain.  An old 1.2.191
    marker has only ``pid`` and records the runtime child, which is a
    descendant of the venv redirector the supervisor spawned; accept that exact
    parent/child relationship for rollback, never an unrelated process.
    """
    pid = int(pid)
    owner_pid = int(owner_pid)
    agent_pid = int(agent_pid)
    if pid not in (owner_pid, agent_pid):
        return bool(legacy and is_descendant(agent_pid, pid))
    if agent_pid == owner_pid:
        return True
    # New semantics: the panel process must really be the recorded launcher's
    # descendant.  This rejects a marker that merely repeats the supervisor pid
    # while claiming an unrelated live process as the panel owner.
    return is_descendant(agent_pid, owner_pid)


def _remote_enabled(job: dict) -> bool:
    try:
        settings = load_settings(Path(job["data"]) / "config" / "settings.json")
        remote = settings.get("remote_channel") or {}
        return bool(remote.get("enabled", True)) if isinstance(remote, dict) else True
    except Exception:
        return True


def _tunnel_snapshot_ok(job: dict, pid: int) -> tuple[bool, str]:
    if not _remote_enabled(job):
        return True, "remote channel disabled"
    data = Path(job["data"])
    try:
        from .remote_channel import relay_provisioning_status
        provisioned = relay_provisioning_status(Layout(data=data))
    except Exception as exc:
        # A healthy process with remote support enabled must still be able to
        # load its remote-channel path.  Treat an import/status failure as a
        # failed self-check, not as "no tunnel needed".
        return False, "remote support state unavailable: " + _safe_reason(exc)
    if not isinstance(provisioned, dict):
        return False, "remote support state is invalid"
    if not provisioned.get("ok"):
        return True, "remote support not enabled"
    try:
        session = read_json(data / "remote-channel" / "session.json", default={}) or {}
        token = (data / "remote-channel" / "token").read_text(encoding="utf-8").strip()
        relay = (data / "remote-channel" / "relay-port").read_text(encoding="ascii").strip()
    except (OSError, ValueError, UnicodeError) as exc:
        return False, f"remote state unreadable: {_safe_reason(exc)}"
    if not token or not relay.isdigit():
        return False, "remote token/relay port missing"
    session_pid, session_agent = _pid_evidence(session)
    if not session_pid:
        return False, "remote control service pid evidence is invalid"
    if not _pid_belongs(pid, session_pid, session_agent,
                        legacy=not session.get("agent_pid")):
        return False, "remote control service is not owned by the new instance"
    try:
        log_path = data / "logs" / "remote-channel.log"
        offset = max(0, int(job.get("remote_log_offset") or 0))
        size = log_path.stat().st_size
        if offset > size:
            offset = 0
        with log_path.open("rb") as stream:
            stream.seek(offset)
            tail = stream.read(40000).decode("utf-8", errors="replace")
    except OSError:
        tail = ""
    if "SSH launched" not in tail:
        return False, "SSH tunnel was not launched"
    return True, "ok"


def _remote_log_offset(job: dict) -> int:
    try:
        return (Path(job["data"]) / "logs" / "remote-channel.log").stat().st_size
    except OSError:
        return 0


def probe_health(job: dict, pid: int, version: str) -> tuple[bool, str]:
    """healthz + readiness marker + version + startup-chain integrity."""
    marker = _read_marker(job)
    health = _local_http_json(job["health_url"])
    if not health:
        return False, "healthz not reachable"
    if health.get("ok") is not True:
        return False, "healthz did not report ok"
    if health.get("version") != version:
        return False, f"healthz version is {health.get('version')!r}, expected {version}"
    if marker.get("ready") is not True:
        return False, "readiness marker is not ready"
    if marker.get("version") != version:
        return False, "readiness marker version mismatch"
    marker_pid, agent_pid = _pid_evidence(marker)
    if not marker_pid:
        return False, "readiness marker has invalid pid evidence"
    if not _pid_belongs(pid, marker_pid, agent_pid,
                        legacy=not marker.get("agent_pid")):
        return False, "readiness marker belongs to another process"
    instance = marker.get("instance")
    if not instance or health.get("instance") != instance:
        return False, "healthz instance does not match the readiness marker"
    verdict = verify_program(Path(job["program"]))
    if not verdict.get("ok"):
        return False, "integrity check failed: " + str(verdict.get("reason") or "unknown")
    return True, "ok"


def start_agent(job: dict) -> subprocess.Popen:
    command = [str(part) for part in job["agent_command"]]
    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in (job.get("agent_env") or {}).items()})
    token = secrets.token_hex(16)
    env["WINPORT_INSTANCE_TOKEN"] = token
    # Remember this launch's token in the journal.  If the new agent fails its
    # post-restart checks, this is the proof that a marker in the data directory
    # really belongs to the child tree we are about to stop, not a stale or
    # unrelated process.
    job["new_instance_token"] = token
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    log_path = update_dir(Layout(data=Path(job["data"]))) / "supervisor.out"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stream = log_path.open("ab")
    try:
        kwargs = hidden_window_options(command, popen=True)
        if os.name == "nt":
            kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | 0x00000008
        proc = subprocess.Popen(command, cwd=str(job["program"]), env=env,
                                stdout=stream, stderr=stream, close_fds=True,
                                **kwargs)
    finally:
        stream.close()
    return proc


def _marker_instance_pids(job: dict) -> list[int]:
    """Pids from a marker that provably belongs to the newly spawned agent."""
    token = str(job.get("new_instance_token") or "")
    if not token:
        return []
    marker = _read_marker(job)
    if marker.get("instance") != token:
        return []
    if marker.get("version") != job.get("target_version"):
        return []
    owner_pid, agent_pid = _pid_evidence(marker)
    return [pid for pid in (owner_pid, agent_pid) if pid]


def _agent_lock_owner_pid(job: dict) -> int:
    """Return the process currently holding the agent lock, or 0.

    ``FileLock`` stamps the owner pid into the lock file.  This is the last
    resort when the redirector already exited and no readiness marker was
    written: only the real runtime child can hold this installation's
    ``run/agent.lock``.
    """
    path = _agent_lock_path(job)
    try:
        if not lock_is_held(path):
            return 0
        value = read_json(path, default={})
        pid = int(value.get("pid") or 0) if isinstance(value, dict) else 0
    except (OSError, ValueError, TypeError):
        return 0
    return pid if pid > 0 else 0


def _panel_address(job: dict) -> tuple[str, int]:
    try:
        parts = urllib.parse.urlsplit(str(job.get("health_url") or ""))
        host = parts.hostname or ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            return "", 0
        port = int(parts.port or (443 if parts.scheme == "https" else 80))
        return host, port
    except (TypeError, ValueError):
        return "", 0


def _panel_port_open(job: dict, *, timeout: float = 0.3) -> bool:
    host, port = _panel_address(job)
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_agent_runtime_released(job: dict, *, timeout: float = 10.0,
                                 sleep=time.sleep, port_open=None,
                                 lock_held=None) -> tuple[bool, str]:
    """Wait until the failed agent released its lock and panel port."""
    port_open = port_open or (lambda: _panel_port_open(job))
    lock_held = lock_held or (lambda: lock_is_held(_agent_lock_path(job)))
    deadline = time.monotonic() + max(0.1, timeout)
    last = "agent runtime still held"
    while True:
        try:
            held = bool(lock_held())
        except Exception:
            held = True
        try:
            listening = bool(port_open())
        except Exception:
            listening = True
        if not held and not listening:
            return True, "ok"
        last = (f"agent runtime not released (lock_held={held}, "
                f"panel_port_open={listening})")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, last
        sleep(min(0.2, max(0.01, remaining)))


def _terminate_pids(pids, *, timeout: float = 10.0, sleep=time.sleep) -> bool:
    targets = []
    for value in pids:
        try:
            pid = int(value)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            targets.append(pid)
    targets = list(dict.fromkeys(targets))
    if not targets:
        return True
    if os.name == "nt":
        for pid in reversed(targets):
            if not _pid_alive(pid):
                continue
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=15, **hidden_window_options(["taskkill"]))
            except (OSError, subprocess.SubprocessError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
    else:
        for pid in reversed(targets):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
    deadline = time.monotonic() + max(0.1, timeout)
    while any(_pid_alive(pid) for pid in targets):
        if time.monotonic() >= deadline:
            break
        sleep(0.1)
    remaining = [pid for pid in targets if _pid_alive(pid)]
    if remaining:
        for pid in remaining:
            try:
                if os.name == "nt":
                    os.kill(pid, signal.SIGTERM)
                else:
                    os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        deadline = time.monotonic() + max(0.1, timeout)
        while any(_pid_alive(pid) for pid in targets):
            if time.monotonic() >= deadline:
                break
            sleep(0.1)
    return not any(_pid_alive(pid) for pid in targets)


def _stop_agent_process_tree(job: dict, proc=None, *, timeout: float = 10.0,
                             release_timeout: float = 10.0, sleep=time.sleep,
                             parent_map=None, port_open=None,
                             lock_held=None) -> tuple[bool, str]:
    """Stop the spawned redirector and every runtime child, then verify release."""
    roots = []
    live_pid = 0
    if proc is not None:
        try:
            if proc.poll() is None:
                live_pid = int(getattr(proc, "pid", 0) or 0)
        except Exception:
            live_pid = 0
    if live_pid > 0:
        roots.append(live_pid)
    marker_pids = _marker_instance_pids(job)
    roots.extend(marker_pids)
    # Without an owned marker, the journal pid is only safe while the Popen
    # object is still the live process we spawned.  A reused pid after exit
    # must never be killed.
    if not marker_pids and live_pid > 0:
        try:
            journal_pid = int(job.get("new_pid") or 0)
        except (TypeError, ValueError):
            journal_pid = 0
        if journal_pid == live_pid:
            roots.append(journal_pid)
    if not marker_pids and live_pid <= 0:
        lock_pid = _agent_lock_owner_pid(job)
        if lock_pid > 0:
            roots.append(lock_pid)
    roots = list(dict.fromkeys(roots))
    if not roots:
        return _wait_agent_runtime_released(
            job, timeout=release_timeout, sleep=sleep, port_open=port_open,
            lock_held=lock_held)
    parents = parent_map if parent_map is not None else process_parents()
    if callable(parents):
        parents = parents()
    targets = descendant_pids(roots, parents=parents)
    alive = [pid for pid in targets if _pid_alive(pid)]
    if alive and not _terminate_pids(alive, timeout=timeout, sleep=sleep):
        return False, "agent process tree did not exit"
    # The redirector can create the runtime child between our snapshot and the
    # kill.  Take one more snapshot from the same roots and stop anything new.
    if parent_map is None:
        refreshed = [pid for pid in descendant_pids(roots)
                     if _pid_alive(pid)]
        if refreshed and not _terminate_pids(refreshed, timeout=timeout,
                                             sleep=sleep):
            return False, "agent process tree did not exit"
    return _wait_agent_runtime_released(
        job, timeout=release_timeout, sleep=sleep, port_open=port_open,
        lock_held=lock_held)


def _mark_rollback_failed(job: dict, job_path, layout: Layout,
                          reason: str) -> dict:
    job["status"] = "rollback_failed"
    job["rollback_reason"] = reason
    write_json(job_path, job)
    write_status(layout, "rollback_failed", version=job.get("from_version", ""),
                 detail=reason)
    log_event(layout, "ROLLBACK FAILED: " + reason)
    return {"ok": False, "rolled_back": False, "reason": reason}


def _wait_for_health(job: dict, proc, version: str, *, timeout: float,
                     sleep=time.sleep, health_probe=probe_health) -> tuple[bool, str]:
    deadline = time.monotonic() + max(1.0, timeout)
    last = "not ready"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False, f"agent exited with code {proc.returncode}"
        ok, reason = health_probe(job, proc.pid, version)
        if ok:
            return True, "ok"
        last = reason
        sleep(0.5)
    return False, last


def _wait_for_tunnel(job: dict, proc, *, timeout: float,
                     sleep=time.sleep, tunnel_probe=None) -> tuple[bool, str]:
    tunnel_probe = tunnel_probe or _tunnel_snapshot_ok
    deadline = time.monotonic() + max(1.0, timeout)
    last = "tunnel not ready"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False, "new agent exited before the tunnel came up"
        ok, reason = tunnel_probe(job, proc.pid)
        if ok:
            return True, "ok"
        last = reason
        sleep(0.5)
    return False, last


def supervise_job(job_path, *, spawn_agent=start_agent, health_probe=probe_health,
                  tunnel_probe=None, sleep=time.sleep, health_timeout=None,
                  tunnel_timeout=None) -> dict:
    """Run one prepared transaction.  Loaded from the OLD version on purpose."""
    job_path = Path(job_path)
    job = read_json(job_path, default=None)
    if not isinstance(job, dict):
        return {"ok": False, "reason": "transaction journal unreadable"}
    job["journal_path"] = str(job_path)
    job["supervisor_pid"] = os.getpid()
    layout = Layout(data=Path(job["data"]), program=Path(job["program"])).ensure()
    health_timeout = float(health_timeout if health_timeout is not None
                           else (job.get("health_timeout_seconds") or 60))
    tunnel_timeout = float(tunnel_timeout if tunnel_timeout is not None
                           else (job.get("tunnel_timeout_seconds") or 30))
    ready_path = Path(job["ready_path"])
    new_proc = None
    try:
        # Tell the running agent we are loaded and can take over BEFORE it is
        # asked to stop.  This is what breaks the "supervisor waits for agent,
        # agent waits for supervisor" deadlock.
        write_json(ready_path, {"pid": os.getpid(), "ts": _now_iso()})
        # Shutdown is cooperative: an in-flight report/collection round can
        # legitimately take longer than a minute.  Keep waiting long enough to
        # avoid the worst failure mode -- the old agent exits after we give up,
        # leaving nobody to start the replacement.
        if not _wait_old_agent_stopped(job, timeout=300, sleep=sleep):
            job["status"] = "failed"
            job["reason"] = "old agent did not release its lock"
            write_json(job_path, job)
            write_status(layout, "failed", detail=job["reason"])
            log_event(layout, "update supervisor: old agent did not stop; nothing replaced")
            return {"ok": False, "reason": job["reason"]}
        log_event(layout, f"supervisor {os.getpid()} ready; committing {job['target_version']}")
        _cleanup_atomic_temps(Path(job["program"]))
        apply_update(job)
        job["status"] = "restarting"
        job["remote_log_offset"] = _remote_log_offset(job)
        write_json(job_path, job)
        write_status(layout, "restarting", version=job["target_version"])
        log_event(layout, "files committed; starting new agent")
        new_proc = spawn_agent(job)
        job["new_pid"] = new_proc.pid
        write_json(job_path, job)
        ok, reason = _wait_for_health(job, new_proc, job["target_version"],
                                      timeout=health_timeout, sleep=sleep,
                                      health_probe=health_probe)
        if ok:
            ok, reason = _wait_for_tunnel(job, new_proc, timeout=tunnel_timeout,
                                          sleep=sleep, tunnel_probe=tunnel_probe)
        if ok:
            job["status"] = "updated"
            job["updated_at"] = _now_iso()
            write_json(job_path, job)
            write_status(layout, "updated", version=job["target_version"])
            _clear_failed_version(layout)
            log_event(layout, f"update complete: {job['from_version']} -> {job['target_version']}")
            return {"ok": True, "version": job["target_version"]}
        raise UpdateError(f"post-restart self-check failed: {reason}")
    except Exception as exc:
        reason = _safe_reason(exc)
        log_event(layout, "update failed: " + reason)
        stopped, stop_reason = _stop_agent_process_tree(job, new_proc)
        if not stopped:
            return _mark_rollback_failed(
                job, job_path, layout,
                "failed agent did not release its runtime before rollback: "
                + stop_reason)
        log_event(layout, "failed agent tree stopped; rollback can restore files")
        try:
            job["reason"] = reason
            try:
                rollback_update(job)
            except Exception as rollback_error:
                # If the failure happened before the first replacement (most
                # commonly a backup could not be completed), there is nothing
                # to restore.  Restart the old version rather than leaving the
                # machine with no resident agent.
                if (int(job.get("applied") or 0) == 0
                        and read_installed_version(Path(job["program"])) == job["from_version"]):
                    log_event(layout, "no files replaced; restarting old agent after aborted update")
                    job["status"] = "failed"
                    write_json(job_path, job)
                    old_proc = spawn_agent(job)
                    job["rollback_pid"] = old_proc.pid
                    write_json(job_path, job)
                    ok, health_reason = _wait_for_health(
                        job, old_proc, job["from_version"], timeout=health_timeout,
                        sleep=sleep, health_probe=health_probe)
                    if ok:
                        ok, health_reason = _wait_for_tunnel(
                            job, old_proc, timeout=tunnel_timeout, sleep=sleep,
                            tunnel_probe=tunnel_probe)
                    if ok:
                        write_status(layout, "failed", version=job["from_version"],
                                     detail=reason)
                        return {"ok": False, "rolled_back": False,
                                "restarted": True, "reason": reason}
                raise rollback_error
            write_status(layout, "rolled_back", version=job["from_version"],
                         detail=reason)
            _record_failed_version(layout, job["target_version"], reason)
            log_event(layout, f"rollback complete; restarting {job['from_version']}")
            job["remote_log_offset"] = _remote_log_offset(job)
            old_proc = spawn_agent(job)
            job["rollback_pid"] = old_proc.pid
            write_json(job_path, job)
            ok, health_reason = _wait_for_health(job, old_proc, job["from_version"],
                                                 timeout=health_timeout, sleep=sleep,
                                                 health_probe=health_probe)
            if ok:
                ok, health_reason = _wait_for_tunnel(job, old_proc, timeout=tunnel_timeout,
                                                     sleep=sleep, tunnel_probe=tunnel_probe)
            if ok:
                log_event(layout, f"rollback healthy: {job['from_version']}")
                return {"ok": False, "rolled_back": True, "reason": reason}
            raise UpdateError(f"old version did not become healthy: {health_reason}")
        except Exception as rollback_exc:
            rollback_reason = _safe_reason(rollback_exc)
            return _mark_rollback_failed(job, job_path, layout, rollback_reason)


def spawn_supervisor(layout: Layout, job: dict, *, python_exe=None) -> subprocess.Popen:
    command = [str(python_exe or sys.executable), "-B", "-m", "winport.updater",
               "supervise", "--job", str(job["journal_path"])]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(job["program"]) / "agent")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = update_dir(layout) / "supervisor.out"
    stream = out.open("ab")
    try:
        kwargs = hidden_window_options(command, popen=True)
        if os.name == "nt":
            kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | 0x00000008
        proc = subprocess.Popen(command, cwd=str(job["program"]), env=env,
                                stdout=stream, stderr=stream, close_fds=True,
                                **kwargs)
    finally:
        stream.close()
    return proc


def _record_failed_version(layout: Layout, version: str, reason: str) -> None:
    try:
        write_json(update_dir(layout) / "failed.json",
                   {"version": version, "ts": _now_ts(), "reason": reason})
    except OSError:
        pass


def _clear_failed_version(layout: Layout) -> None:
    try:
        (update_dir(layout) / "failed.json").unlink(missing_ok=True)
    except OSError:
        pass


def _failed_recently(layout: Layout, version: str, cooldown: int) -> bool:
    try:
        value = read_json(update_dir(layout) / "failed.json", default={}) or {}
        return (value.get("version") == version
                and _now_ts() - float(value.get("ts") or 0) < cooldown)
    except (OSError, ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# Manifest fetch + top-level update attempt
# --------------------------------------------------------------------------- #


def fetch_manifest(url: str, *, timeout: int = 30, retries: int = 2,
                   allow_insecure_http: bool = False, opener=None,
                   sleep=time.sleep) -> dict:
    if not _http_url_allowed(url, allow_insecure_http=allow_insecure_http):
        raise UpdateError("manifest URL must use HTTPS (loopback HTTP only in local tests)")
    opener = opener or urllib.request.urlopen
    last = None
    for attempt in range(max(1, retries + 1)):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "ManjuPublishAssistant-updater/1",
                              "Cache-Control": "no-cache"})
            with opener(request, timeout=timeout) as response:
                if getattr(response, "status", 200) != 200:
                    raise UpdateError(f"manifest returned HTTP {getattr(response, 'status', 0)}")
                final_url = getattr(response, "geturl", lambda: url)()
                if not _http_url_allowed(final_url, allow_insecure_http=allow_insecure_http):
                    raise UpdateError("manifest redirect left the HTTPS trust boundary")
                data = response.read(MANIFEST_LIMIT + 1)
            if len(data) > MANIFEST_LIMIT:
                raise UpdateError("manifest is too large")
            value = json.loads(data.decode("utf-8-sig"))
            if not isinstance(value, dict):
                raise UpdateError("manifest must be a JSON object")
            return value
        except Exception as exc:
            last = exc
            if attempt < max(1, retries):
                sleep(min(4, 2 ** attempt))
    raise UpdateError(f"manifest fetch failed: {_safe_reason(last)}")


def _default_agent_env(program: Path, data: Path) -> dict:
    return {
        "PYTHONPATH": str(Path(program) / "agent"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PLAYWRIGHT_BROWSERS_PATH": str(Path(program) / "browsers"),
        "WINPORT_PROGRAM": str(program),
        "WINPORT_DATA": str(data),
    }


def run_update(layout: Layout, *, manifest_url: str | None = None,
               current_version: str | None = None, public_key: bytes | None = None,
               shutdown=None, health_url: str | None = None,
               agent_command: list | None = None, agent_env: dict | None = None,
               config: dict | None = None, logger=None, old_pid: int | None = None,
               sleep=time.sleep) -> dict:
    """One update attempt owned by the running agent.

    Returns before the replacement happens.  ``shutdown`` must stop the normal
    agent loop; the detached supervisor waits for that exit and commits.
    """
    try:
        config = config or load_update_config(layout)
    except Exception as exc:
        log_event(layout, "update config error: " + _safe_reason(exc))
        return {"status": "failed", "reason": _safe_reason(exc)}
    if not config.get("enabled") or not (manifest_url or config.get("manifest_url")):
        return {"status": "disabled"}
    if not health_url:
        log_event(layout, "update skipped: no local health URL")
        return {"status": "disabled", "reason": "no health_url"}
    url = manifest_url or config["manifest_url"]
    program = Path(layout.program)
    current = current_version or read_installed_version(program)
    lock = FileLock(update_dir(layout) / "update.lock")
    if not lock.acquire():
        return {"status": "busy"}
    try:
        idle = idle_verdict(layout)
        if not idle["idle"]:
            log_event(layout, "update deferred: " + idle["reason"])
            return {"status": "deferred", "reason": idle["reason"]}
        write_status(layout, "checking", version=current)
        log_event(layout, f"checking manifest from {current}")
        manifest = fetch_manifest(url, timeout=config["request_timeout_seconds"],
                                  allow_insecure_http=config["allow_insecure_http"],
                                  sleep=sleep)
        verdict = validate_manifest(
            manifest, current_version=current, public_key=public_key,
            allow_insecure_http=config["allow_insecure_http"])
        if not verdict.get("ok"):
            reason = str(verdict.get("reason") or "manifest rejected")
            if verdict.get("no_update"):
                log_event(layout, "manifest is already current; no update needed")
                write_status(layout, "idle", version=current)
                return {"status": "idle", "reason": reason}
            log_event(layout, "manifest rejected: " + reason)
            write_status(layout, "failed", version=current, detail=reason)
            return {"status": "failed", "reason": reason}
        target = verdict["version"]
        if _failed_recently(layout, target, config["failed_retry_seconds"]):
            log_event(layout, f"update {target} is in failure cooldown")
            return {"status": "cooldown", "version": target}
        write_status(layout, "downloading", version=target)
        log_event(layout, f"manifest accepted: {current} -> {target}")
        patch_dir = update_dir(layout) / "downloads"
        zip_path = patch_dir / f"patch-{target}.zip"
        # The zip size is not a manifest field; the signed sha256 is the
        # authority.  Resume the partial file, then verify the whole hash.
        zip_path = _download_zip(verdict["manifest"]["patch_zip_url"], zip_path,
                                 sha256=verdict["manifest"]["patch_zip_sha256"].lower(),
                                 timeout=config["request_timeout_seconds"],
                                 retries=config["download_retries"],
                                 allow_insecure_http=config["allow_insecure_http"],
                                 sleep=sleep)
        stage = update_dir(layout) / f"stage-{target}"
        staged = stage_patch(zip_path, verdict["files"], stage)
        idle = idle_verdict(layout)
        if not idle["idle"]:
            log_event(layout, "update commit deferred: " + idle["reason"])
            write_status(layout, "idle", version=current)
            return {"status": "deferred", "reason": idle["reason"]}
        backup_dir = update_dir(layout) / f"backup-{current}"
        ready_path = update_dir(layout) / "supervisor.ready"
        ready_path.unlink(missing_ok=True)
        job = {
            "schema": SCHEMA,
            "status": "prepared",
            "program": str(program),
            "data": str(layout.data),
            "from_version": current,
            "target_version": target,
            "old_pid": int(old_pid or os.getpid()),
            "health_url": health_url,
            "files": staged,
            "stage_dir": str(stage),
            "backup_dir": str(backup_dir),
            "agent_command": list(agent_command or [
                sys.executable, "-B", "-m", "winport", "--data", str(layout.data)]),
            "agent_env": dict(agent_env or _default_agent_env(program, layout.data)),
            "ready_path": str(ready_path),
            "health_timeout_seconds": config["health_timeout_seconds"],
            "tunnel_timeout_seconds": config["tunnel_timeout_seconds"],
            "created_at": _now_iso(),
            "journal_path": str(update_dir(layout) / "transaction.json"),
        }
        write_json(job["journal_path"], job)
        write_status(layout, "ready", version=target)
        supervisor = spawn_supervisor(layout, job)
        log_event(layout, f"supervisor spawned pid={supervisor.pid}")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if read_json(ready_path, default=None):
                break
            if supervisor.poll() is not None:
                raise UpdateError(f"supervisor exited before ready ({supervisor.returncode})")
            sleep(0.25)
        else:
            raise UpdateError("supervisor did not become ready")
        if supervisor.poll() is not None:
            raise UpdateError(f"supervisor exited before handoff ({supervisor.returncode})")
        write_status(layout, "restarting", version=target)
        log_event(layout, "requesting normal agent shutdown for restart")
        if callable(shutdown):
            shutdown()
        return {"status": "restarting", "version": target,
                "supervisor_pid": supervisor.pid}
    except Exception as exc:
        reason = _safe_reason(exc)
        log_event(layout, "update attempt failed: " + reason)
        try:
            current = current_version or read_installed_version(program)
        except Exception:
            current = ""
        write_status(layout, "failed", version=current, detail=reason)
        return {"status": "failed", "reason": reason}
    finally:
        lock.release()


def _download_zip(url: str, destination: Path, *, sha256: str, timeout: int,
                  retries: int, allow_insecure_http: bool, sleep=time.sleep,
                  opener=None) -> Path:
    """Download without a prior size; resume then verify the signed hash."""
    if not _http_url_allowed(url, allow_insecure_http=allow_insecure_http):
        raise UpdateError("patch URL is not allowed")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".part")
    if destination.exists() and sha256_file(destination) == sha256:
        return destination
    destination.unlink(missing_ok=True)
    opener = opener or urllib.request.urlopen
    attempts = max(1, int(retries) + 1)
    last = None
    for attempt in range(1, attempts + 1):
        offset = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": "ManjuPublishAssistant-updater/1"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            request = urllib.request.Request(url, headers=headers)
            with opener(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                if status not in (200, 206):
                    raise UpdateError(f"patch download returned HTTP {status}")
                final_url = getattr(response, "geturl", lambda: url)()
                if not _http_url_allowed(final_url, allow_insecure_http=allow_insecure_http):
                    raise UpdateError("patch redirect left the HTTPS trust boundary")
                if offset and status == 200:
                    offset = 0
                with part.open("ab" if offset else "wb") as output:
                    total = offset
                    while True:
                        block = response.read(1 << 20)
                        if not block:
                            break
                        total += len(block)
                        if total > PATCH_LIMIT:
                            raise UpdateError("patch exceeds the allowed size")
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
            if not part.is_file() or sha256_file(part) != sha256:
                part.unlink(missing_ok=True)
                raise UpdateError("patch sha256 mismatch")
            os.replace(part, destination)
            return destination
        except Exception as exc:
            last = exc
            if attempt < attempts:
                sleep(min(8, 2 ** (attempt - 1)))
    raise UpdateError(f"patch download failed: {_safe_reason(last)}")


class UpdateLoop(threading.Thread):
    """Periodic idle-time checker owned by the resident agent."""

    def __init__(self, layout: Layout, *, health_url: str, shutdown,
                 logger=None):
        super().__init__(daemon=True, name="winport-update")
        self.layout = layout
        self.health_url = health_url
        self.shutdown = shutdown
        self.logger = logger
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        try:
            config = load_update_config(self.layout)
        except Exception as exc:
            log_event(self.layout, "update loop disabled by configuration error: "
                      + _safe_reason(exc))
            return
        if not config.get("enabled") or not config.get("manifest_url"):
            log_event(self.layout, "update loop idle: no manifest URL configured")
            return
        if self.stop_event.wait(config["initial_delay_seconds"]):
            return
        while not self.stop_event.is_set():
            try:
                result = run_update(self.layout, health_url=self.health_url,
                                    shutdown=self.shutdown, logger=self.logger)
                if result.get("status") == "restarting":
                    return
            except Exception:                 # never kill the heartbeat
                log_event(self.layout, "update loop error: "
                          + _safe_reason(traceback.format_exc(limit=2)))
            self.stop_event.wait(max(60, config["check_interval_seconds"]))


# --------------------------------------------------------------------------- #
# CLI (also used by the local end-to-end drill)
# --------------------------------------------------------------------------- #


def _cli(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="winport.updater")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check")
    check.add_argument("--program", required=True)
    check.add_argument("--data", required=True)
    check.add_argument("--manifest-url", required=True)
    check.add_argument("--allow-insecure-http", action="store_true")

    perform = sub.add_parser("perform")
    perform.add_argument("--program", required=True)
    perform.add_argument("--data", required=True)
    perform.add_argument("--manifest-url", required=True)
    perform.add_argument("--agent-command-json", default="")
    perform.add_argument("--health-url", required=True)
    perform.add_argument("--old-pid", type=int, default=0)
    perform.add_argument("--stop-file", default="")
    perform.add_argument("--allow-insecure-http", action="store_true")
    perform.add_argument("--wait", type=float, default=0)

    supervise = sub.add_parser("supervise")
    supervise.add_argument("--job", required=True)

    args = parser.parse_args(argv)
    if args.command == "supervise":
        result = supervise_job(args.job)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") or result.get("rolled_back") else 1
    layout = Layout(program=Path(args.program), data=Path(args.data)).ensure()
    if args.command == "check":
        current = read_installed_version(Path(args.program))
        manifest = fetch_manifest(args.manifest_url,
                                  allow_insecure_http=args.allow_insecure_http)
        verdict = validate_manifest(manifest, current_version=current,
                                    allow_insecure_http=args.allow_insecure_http)
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
        return 0 if verdict.get("ok") else 2
    if args.command == "perform":
        command = json.loads(args.agent_command_json) if args.agent_command_json else None
        if command is not None and (not isinstance(command, list) or not command):
            raise SystemExit("agent-command-json must be a non-empty array")
        stop_file = Path(args.stop_file) if args.stop_file else None
        def _shutdown():
            if stop_file is not None:
                stop_file.write_text("stop\n", encoding="utf-8")
        config = load_update_config(layout)
        config["allow_insecure_http"] = bool(args.allow_insecure_http
                                             or config["allow_insecure_http"])
        result = run_update(layout, manifest_url=args.manifest_url, shutdown=_shutdown,
                            health_url=args.health_url, agent_command=command,
                            config=config, old_pid=args.old_pid or None)
        print(json.dumps(result, ensure_ascii=False))
        if args.wait:
            deadline = time.monotonic() + args.wait
            while time.monotonic() < deadline:
                state = read_status(layout).get("state")
                if state in ("updated", "rolled_back", "failed", "rollback_failed"):
                    print(json.dumps(read_status(layout), ensure_ascii=False))
                    return 0 if state in ("updated", "rolled_back") else 1
                threading.Event().wait(0.5)
            return 3
        return 0 if result.get("status") in ("restarting", "cooldown") else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
