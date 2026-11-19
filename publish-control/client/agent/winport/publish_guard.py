"""Auditable guards for the real Douyin publish driver.

This module keeps the pure, Linux-testable rules in one place so the GPT review
can verify them without a browser:

* selector policy: zero class-name locators, no substring text matching;
* upload/transcode evidence: a successful form cannot be inferred from
  ``set_input_files`` + "title area exists" + "button enabled";
* work-id binding: only one work id bound to this account/attempt may consume
  the ledger;
* fault holds: captcha / risk-control / login-lost / network errors and an
  unrecognisable post-publish verification persist across restarts; explicit
  operator resolution is required.
"""

from __future__ import annotations

import hashlib
import math
import json
import re
import shutil
import subprocess
import time
import unicodedata
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .hashing import sha256_bytes
from .subprocess_text import hidden_window_options, text_mode_options
from .jsonio import read_json, write_json
from .lock import FileLock

CAPTCHA = "captcha"
RISK_CONTROL = "risk_control"
LOGIN_LOST = "login_lost"
NETWORK_ERROR = "network_error"
SMS_VERIFICATION = "sms_verification"
VERIFICATION_UNKNOWN = "verification_unknown"
PUBLISH_UNKNOWN = "publish_unknown"
LEASE_TIMEOUT = "lease_timeout"
CHANNEL_UNBOUND = "channel_unbound"
FAULT_KINDS = (CAPTCHA, RISK_CONTROL, LOGIN_LOST, NETWORK_ERROR, SMS_VERIFICATION,
               VERIFICATION_UNKNOWN, PUBLISH_UNKNOWN, LEASE_TIMEOUT,
               CHANNEL_UNBOUND)

#: Who created a hold.  Records written before 1.2.222 have no source and are
#: deliberately treated as human: an unknown brake must never be auto-released.
HOLD_SOURCES = ("agent", "bot", "human")
AUTO_CLEAR_REASON_CODES = frozenset({LEASE_TIMEOUT, PUBLISH_UNKNOWN,
                                     CHANNEL_UNBOUND})

ASCII_MID_RE = re.compile(r"[0-9]{19}\Z")

SMS_MARKERS = ("为确保是本人操作", "短信二次验证", "二次验证", "短信验证码")
VERIFICATION_UNKNOWN_MARKERS = ("无法识别验证方式", "等待本人扫码超时", "验证方式超时",
                                "验证失败", "二维码已失效")
CAPTCHA_MARKERS = ("验证码", "captcha", "安全验证", "拖动滑块", "扫码验证", "captcha_box")
RISK_MARKERS = ("风控", "操作频繁", "请求过于频繁", "账号异常", "被限制",
                "risk control", "rate limit", "too many requests")
LOGIN_MARKERS = ("登录", "passport", "重新登录", "扫码登录", "登录已失效",
                 "登录过期", "会话过期", "login")
NETWORK_MARKERS = ("网络", "连接失败", "加载失败", "无法访问", "timeout",
                   "timed out", "network error", "ERR_", "无法连接")

FORBIDDEN_SELECTOR_MARKERS = (
    "[class",
    "class=",
    ":has-text(",
    ">> text=",
)

# Real Douyin 2026-10-17 evidence requires substring *attribute* matches for two
# stable platform labels. The selector policy remains zero-class/fuzzy-text;
# only these exact, evidence-backed selectors are exempt from the substring-attr
# ban, and they are re-validated by publish_exec.assert_selector_policy().
REAL_EVIDENCE_SUBTEXT_ATTR_SELECTORS = {
    "input[placeholder*='作品标题']",
    "input[placeholder*='验证码']",
    "input[aria-label*='验证码']",
    "[contenteditable='true'][data-placeholder*='作品描述']",
}
SUBTEXT_ATTR_RE = re.compile(r"\[\s*([A-Za-z0-9_-]+)\s*\*=")
CLASS_DOT_RE = re.compile(r"(?:^|[\s>+~])\.[A-Za-z_][A-Za-z0-9_-]*")


@dataclass
class UploadAttempt:
    attempt_id: str
    env_id: str
    account_id: str
    local_path: str
    size: int | None = None
    sha256: str | None = None
    duration_known: bool = False
    duration_ms: float | None = None
    encoding_known: bool = False
    encoding: str | None = None
    input_set: bool = False
    input_set_at: float | None = None
    request_observed: bool = False
    request_ids: list[str] = field(default_factory=list)
    resource_id: str | None = None
    resource_observed_at: float | None = None
    transcode_done: bool = False
    transcode_failed: bool = False
    transcode_done_at: float | None = None
    progress_visible: bool = False
    title_preexisting: bool = False
    button_enabled_before_done: bool = False
    size_matched: bool | None = None
    request_records: list[dict[str, Any]] = field(default_factory=list)
    response_records: list[dict[str, Any]] = field(default_factory=list)
    observed_duration_known: bool = False
    observed_duration_ms: float | None = None
    observed_encoding_known: bool = False
    observed_encoding: str | None = None
    resource_node_id: str | None = None
    submit_started_at: float | None = None
    final_work_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def media_metadata(path: str | Path, *, probe=None) -> dict:
    """Read local metadata before upload.

    Size/sha256 are mandatory. Duration/codec are read from ``probe`` when
    available; otherwise they are recorded as ``None`` and ``*_known=False`` so
    the driver cannot pretend it verified them.
    """
    path = Path(path)
    from .hashing import sha256_file
    size = path.stat().st_size
    out = {
        "path": str(path),
        "size": size,
        "sha256": sha256_file(path),
        "duration_ms": None,
        "duration_known": False,
        "encoding": None,
        "encoding_known": False,
    }
    if probe is None:
        return out
    try:
        probed = probe(str(path))
    except Exception:
        probed = None
    if isinstance(probed, dict):
        if probed.get("duration_ms") is not None:
            out["duration_ms"] = float(probed["duration_ms"])
            out["duration_known"] = math.isfinite(out["duration_ms"]) and out["duration_ms"] > 0
        if probed.get("encoding"):
            out["encoding"] = str(probed["encoding"])
            out["encoding_known"] = True
    return out


def ffprobe_media_metadata(path: str | Path) -> dict:
    """Probe real media locally.

    WAV is parsed by the standard library so Linux tests exercise a real local
    probe without external tools.  Video files use ``ffprobe`` (falling back to
    ``ffmpeg -i``); if neither binary is installed the result stays unknown and
    the publish guard will block instead of fabricating duration/codec.
    """
    path = Path(path)
    if path.suffix.lower() in (".mp4", ".mov", ".m4v"):
        from .media_probe import probe_mp4
        try:
            return probe_mp4(path)
        except (ValueError, OSError, IndexError, UnicodeError):
            return {"duration_ms": None, "encoding": None}
    suffix = path.suffix.lower()
    if suffix == ".wav":
        try:
            with wave.open(str(path), "rb") as wav:
                frames = wav.getnframes()
                rate = wav.getframerate() or 1
                channels = max(1, wav.getnchannels() or 1)
                sample_width = wav.getsampwidth() or 2
                encoding = {1: "pcm_u8", 2: "pcm_s16le",
                            3: "pcm_s24le", 4: "pcm_s32le"}.get(
                    sample_width, f"pcm_{sample_width * 8}")
                return {"duration_ms": frames * 1000.0 / rate,
                        "duration_known": True,
                        "encoding": encoding,
                        "encoding_known": True}
        except Exception:
            return {"duration_ms": None, "duration_known": False,
                    "encoding": None, "encoding_known": False}
    exe = shutil.which("ffprobe")
    cmd = None
    if exe:
        cmd = [exe, "-v", "error", "-show_entries", "format=duration",
               "-show_entries", "stream=codec_name", "-of", "json", str(path)]
    else:
        exe = shutil.which("ffmpeg")
        if exe:
            cmd = [exe, "-hide_banner", "-i", str(path)]
    if not cmd:
        return {"duration_ms": None, "duration_known": False,
                "encoding": None, "encoding_known": False}
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30, check=False,
                              **hidden_window_options(cmd),
                              **text_mode_options())
    except (OSError, subprocess.SubprocessError):
        return {"duration_ms": None, "duration_known": False,
                "encoding": None, "encoding_known": False}
    duration_ms = None
    encoding = None
    if "ffprobe" in str(cmd[0]):
        try:
            data = json.loads(proc.stdout or "{}")
            fmt = data.get("format") or {}
            if fmt.get("duration") not in (None, "N/A"):
                duration_ms = float(fmt["duration"]) * 1000.0
            streams = data.get("streams") or []
            codecs = [str(s.get("codec_name") or "") for s in streams
                      if s.get("codec_name")]
            if codecs:
                encoding = ",".join(dict.fromkeys(codecs))
        except (ValueError, TypeError):
            pass
    elif proc.stderr:
        duration_ms = None
        codec_match = re.search(r"Video:\s*([^,\s]+)", proc.stderr)
        if codec_match:
            encoding = codec_match.group(1)
    return {"duration_ms": duration_ms,
            "duration_known": duration_ms is not None,
            "encoding": encoding,
            "encoding_known": bool(encoding)}


def validate_selector(selector: str) -> str | None:
    """Return a block reason for one selector, else None."""
    if not isinstance(selector, str) or not selector.strip():
        return "empty selector"
    if any(marker in selector for marker in FORBIDDEN_SELECTOR_MARKERS):
        return f"class-name / substring-text selector forbidden: {selector!r}"
    normalized = selector.replace('"', "'")
    allowed = {item.replace('"', "'") for item in REAL_EVIDENCE_SUBTEXT_ATTR_SELECTORS}
    if SUBTEXT_ATTR_RE.search(selector) and normalized not in allowed:
        return f"substring attribute match forbidden: {selector!r}"
    if re.search(r"\.[A-Za-z_][A-Za-z0-9_-]*", re.sub(r"'[^^']*'|\"[^\"]*\"", "", selector)):
        return f"dot-class selector forbidden outside own UI: {selector!r}"
    return None


def validate_selectors(selectors) -> list[str]:
    out = []
    for index, selector in enumerate(selectors or ()):
        reason = validate_selector(selector)
        if reason:
            out.append(f"[{index}] {reason}")
    return out


def make_upload_attempt(env_id: str, account_id: str, local_path: str | Path,
                        *, now=None) -> UploadAttempt:
    now = time.time() if now is None else now
    path = Path(local_path)
    attempt_id = sha256_bytes(
        f"{env_id}|{account_id}|{path.name}|{now}|{time.monotonic()}".encode()
    )[:24]
    return UploadAttempt(attempt_id=attempt_id, env_id=env_id,
                         account_id=account_id, local_path=str(path))


def upload_failure_reasons(attempt: UploadAttempt, *, require_duration=False) -> list[str]:
    """All reasons this upload attempt is not acceptable as "upload started/completed"."""
    reasons: list[str] = []
    if not attempt or not attempt.attempt_id:
        return ["ATTEMPT_ID_MISSING"]
    if not attempt.input_set:
        reasons.append("INPUT_NOT_SET")
    if attempt.size is None or attempt.sha256 is None:
        reasons.append("LOCAL_METADATA_MISSING")
    if attempt.title_preexisting:
        reasons.append("TITLE_PREEXISTING")
    if not attempt.request_observed or not attempt.request_ids:
        reasons.append("NO_UPLOAD_REQUEST")
    if not attempt.request_records or not any(
            r.get("method") and r.get("bytes") for r in attempt.request_records):
        reasons.append("NO_UPLOAD_BYTES")
    upload_request_ids = {str(value) for value in (attempt.request_ids or [])
                          if value}
    if not any(
            str(r.get("request_id") or "") in upload_request_ids and
            r.get("status") and 200 <= int(r["status"]) < 300
            for r in (attempt.response_records or [])):
        reasons.append("NO_UPLOAD_RESPONSE_OK")
    if not attempt.resource_id or attempt.resource_observed_at is None:
        reasons.append("NO_UPLOAD_RESOURCE")
    if attempt.size_matched is False:
        reasons.append("UPLOAD_SIZE_MISMATCH")
    if attempt.size_matched is None:
        reasons.append("UPLOAD_SIZE_UNVERIFIED")
    if attempt.transcode_failed:
        reasons.append("TRANSCODE_FAILED")
    if attempt.button_enabled_before_done:
        reasons.append("BUTTON_ENABLED_BEFORE_DONE")
    if require_duration and not attempt.duration_known:
        reasons.append("DURATION_UNKNOWN")
    return reasons


def transcode_failure_reasons(attempt: UploadAttempt) -> list[str]:
    reasons = upload_failure_reasons(attempt, require_duration=True)
    if attempt.progress_visible:
        reasons.append("PROGRESS_VISIBLE")
    if not attempt.transcode_done:
        reasons.append("TRANSCODE_NOT_DONE")
    if attempt.transcode_failed:
        reasons.append("TRANSCODE_FAILED")
    if not attempt.transcode_done_at:
        reasons.append("TRANSCODE_DONE_TIME_MISSING")
    if not attempt.resource_node_id:
        reasons.append("RESOURCE_NOT_BOUND")
    return sorted(set(reasons))


def validate_work_id_binding(work_id: str | None, attempt: UploadAttempt,
                             candidates: list[dict], *, title: str = "",
                             account_id: str = "") -> tuple[bool, list[str]]:
    """Only a single work id bound to THIS account/attempt may be consumed."""
    reasons: list[str] = []
    if not work_id:
        reasons.append("EMPTY_WORK_ID")
    if not attempt or not attempt.attempt_id:
        return False, ["ATTEMPT_MISSING"]
    if len(candidates) != 1:
        reasons.append(f"CANDIDATE_COUNT_{len(candidates)}")
    if candidates:
        candidate = candidates[0]
        if account_id and candidate.get("account_id") != account_id:
            reasons.append("ACCOUNT_MISMATCH")
        if not (candidate.get("resource_id") == attempt.resource_id
                and (not title or candidate.get("title") == title)):
            reasons.append("ATTEMPT_TITLE_MISMATCH")
        published_at = candidate.get("published_at")
        submit_started_at = getattr(attempt, "submit_started_at", None)
        if published_at is None or submit_started_at is None:
            reasons.append("PUBLISH_TIME_NOT_BOUND")
        else:
            try:
                if not math.isfinite(float(published_at)) or not math.isfinite(float(submit_started_at)):
                    reasons.append("PUBLISH_TIME_NOT_BOUND")
                elif float(published_at) < float(submit_started_at):
                    reasons.append("OLD_WORK_ID")
            except (TypeError, ValueError):
                reasons.append("PUBLISH_TIME_NOT_BOUND")
        if candidate.get("work_id") != work_id:
            reasons.append("WORK_ID_MISMATCH")
    return (not reasons, reasons)


def validate_real_work_id_binding(work_id: str | None, attempt: UploadAttempt,
                                 *, candidate_count: int = 1) -> tuple[bool, list[str]]:
    """Real-account 2026-10-17 binding: only the edit-link ``mid=`` value counts.

    The upload resource DOM no longer exists on the manage page, so this path
    deliberately does NOT fabricate a resource/title binding. It still requires
    one observed edit-link candidate and a non-empty mid extracted from its URL.
    """
    reasons: list[str] = []
    if not work_id:
        reasons.append("EMPTY_WORK_ID")
    elif not is_ascii_mid(work_id):
        reasons.append("MID_FORMAT_INVALID")
    if not attempt or not attempt.attempt_id:
        reasons.append("ATTEMPT_MISSING")
    if candidate_count != 1:
        reasons.append(f"EDIT_LINK_CANDIDATE_COUNT_{candidate_count}")
    return (not reasons, reasons)


def is_ascii_mid(value) -> bool:
    """Platform mids are exactly 19 ASCII decimal digits."""
    return bool(ASCII_MID_RE.fullmatch(str(value or "").strip()))


def normalize_title(value) -> str:
    """Full/half-width normalisation followed by whitespace removal.

    ``NFKC`` folds full-width Latin/digits/punctuation; ``split`` removes all
    ordinary Unicode whitespace.  Comparison is exact after that transformation.
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(text.split())


def coerce_timestamp(value) -> float | None:
    """Return a finite epoch timestamp, or ``None`` when proof is unavailable."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.now().astimezone().tzinfo)
        value = value.timestamp()
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def validate_publish_evidence(work_id: str | None, *, s0, s1,
                              submitted_title, observed_title,
                              published_at, click_started_at,
                              read_at) -> dict:
    """Validate list evidence used only as a secondary veto since 1.2.27.

    This helper never authorizes success without a platform response receipt.
    All three auxiliary checks must hold at once:

    1. ``S1 - S0`` contains exactly the claimed work id (and nothing else);
    2. the observed work title equals the submitted title after NFKC and
       whitespace removal;
    3. the platform publication timestamp is between the completed publish
       click and the read-back instant.

    Any missing/invalid field produces ``ok=False``; callers must then write
    ledger UNKNOWN and a persistent hold, never CONSUMED.
    """
    reasons: list[str] = []
    if not isinstance(s0, (list, tuple, set)) or not isinstance(s1, (list, tuple, set)):
        reasons.append("SCAN_SET_MISSING_OR_INVALID")
    try:
        before = set(str(item).strip() for item in (s0 or []))
        after = set(str(item).strip() for item in (s1 or []))
    except TypeError:
        before, after = set(), set()
        reasons.append("SCAN_SET_INVALID")
    for mid in sorted(before | after):
        if not is_ascii_mid(mid):
            reasons.append("MID_FORMAT_INVALID")
            break
    claim = str(work_id or "").strip()
    if not is_ascii_mid(claim):
        reasons.append("MID_FORMAT_INVALID")
    delta = after - before
    if len(delta) != 1:
        reasons.append("DELTA_NOT_ONE")
    elif claim and delta != {claim}:
        reasons.append("DELTA_WORK_ID_MISMATCH")

    expected_title = normalize_title(submitted_title)
    actual_title = normalize_title(observed_title)
    if not expected_title or actual_title != expected_title:
        reasons.append("TITLE_MISMATCH")

    clicked = coerce_timestamp(click_started_at)
    published = coerce_timestamp(published_at)
    read = coerce_timestamp(read_at)
    if clicked is None:
        reasons.append("CLICK_TIME_MISSING")
    if read is None:
        reasons.append("READ_TIME_MISSING")
    if published is None:
        reasons.append("PUBLISH_TIME_MISSING")
    elif clicked is not None and published < clicked:
        reasons.append("PUBLISH_TIME_BEFORE_CLICK")
    if published is not None and read is not None and published > read:
        reasons.append("PUBLISH_TIME_AFTER_READ")

    unique_reasons = sorted(set(reasons))
    return {
        "ok": not unique_reasons,
        "reasons": unique_reasons,
        "work_id": claim,
        "baseline_mids": sorted(before),
        "current_mids": sorted(after),
        "new_mids": sorted(delta),
        "submitted_title_normalized": expected_title,
        "observed_title_normalized": actual_title,
        "click_started_at": clicked,
        "published_at": published,
        "read_at": read,
    }


def classify_fault(evidence: dict | None) -> str | None:
    """Classify one browser/page failure into an auditable fault kind."""
    blob = " ".join(str((evidence or {}).get(key) or "")
                    for key in ("reason", "error", "url", "page_text", "html",
                               "console", "body_text"))
    blob_lower = blob.lower()
    for marker in VERIFICATION_UNKNOWN_MARKERS:
        if marker.lower() in blob_lower:
            return VERIFICATION_UNKNOWN
    for marker in SMS_MARKERS:
        if marker.lower() in blob_lower:
            return SMS_VERIFICATION
    for marker in CAPTCHA_MARKERS:
        if marker.lower() in blob_lower:
            return CAPTCHA
    for marker in RISK_MARKERS:
        if marker.lower() in blob_lower:
            return RISK_CONTROL
    for marker in LOGIN_MARKERS:
        if marker.lower() in blob_lower:
            return LOGIN_LOST
    for marker in NETWORK_MARKERS:
        if marker.lower() in blob_lower:
            return NETWORK_ERROR
    if "unknown" in blob_lower:
        return PUBLISH_UNKNOWN
    return None


def normalize_hold(hold: dict | None) -> dict | None:
    """Return a hold with source/reason_code, defaulting legacy rows to human."""
    if not isinstance(hold, dict):
        return hold
    value = dict(hold)
    source = value.get("source")
    if source not in HOLD_SOURCES:
        source = "human"
    value["source"] = source
    reason_code = value.get("reason_code")
    if not isinstance(reason_code, str) or not reason_code.strip():
        kind = value.get("kind")
        reason_code = kind if source == "agent" and isinstance(kind, str) else ""
    value["reason_code"] = str(reason_code or "")
    return value


class PublishHoldStore:
    """Persistent per-channel holds for captcha/risk/login/network faults."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = FileLock(self.path.with_name(self.path.name + ".lock"))
        self.reload()

    def reload(self):
        self.data = read_json(self.path, default=None)
        if not self.path.exists():
            self.data = {"holds": {}}
        elif not isinstance(self.data, dict) or not isinstance(self.data.get("holds"), dict):
            raise ValueError("publish hold file damaged; manual recovery required")

    def _save_locked(self):
        write_json(self.path, self.data)

    def set_hold(self, channel_id: str, kind: str, evidence: dict,
                 *, reason: str = "", source: str = "agent",
                 reason_code: str | None = None, now=None) -> dict:
        if kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault kind {kind!r}")
        if source not in HOLD_SOURCES:
            raise ValueError(f"unknown hold source {source!r}")
        now = now or time.strftime("%Y-%m-%dT%H:%M:%S%z")
        code = str(reason_code or kind)
        with self.lock:
            self.reload()
            self.data.setdefault("holds", {})[channel_id] = {
                "kind": kind,
                "source": source,
                "reason_code": code,
                "reason": reason or kind,
                "held_since": now,
                "evidence": evidence or {},
            }
            self._save_locked()
        return dict(self.data["holds"][channel_id])

    def _emergency_path(self, channel_id):
        import hashlib
        key = hashlib.sha256(str(channel_id).encode("utf-8")).hexdigest()
        return self.path.with_name(self.path.name + ".emergency") / (key + ".json")

    def persist_failure(self, channel_id, kind, evidence, *, reason,
                        source: str = "agent", reason_code: str | None = None):
        """Never throw from freezing; try independent durable fallback on error."""
        try:
            self.set_hold(channel_id, kind, evidence, reason=reason,
                          source=source, reason_code=reason_code)
            return {"persisted": True, "emergency": False}
        except Exception as exc:
            import json, os, sys
            marker = self._emergency_path(channel_id)
            record = {"channel_id": channel_id, "kind": kind, "reason": reason,
                      "source": "human", "reason_code": "",
                      "primary_error": repr(exc), "held_since": time.time()}
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                with marker.open("w", encoding="utf-8") as stream:
                    json.dump(record, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                return {"persisted": True, "emergency": True}
            except Exception as fallback:
                message = "HOLD_PERSIST_FAILED: " + repr(exc) + "; " + repr(fallback)
                print(message, file=sys.stderr, flush=True)
                return {"persisted": False, "error": message}

    def get(self, channel_id: str) -> dict | None:
        marker = self._emergency_path(channel_id)
        if marker.exists():
            return normalize_hold({
                "kind": PUBLISH_UNKNOWN,
                "source": "human", "reason_code": "",
                "reason": "冻结主文件写入失败；须人工处理 emergency hold",
                "emergency_path": str(marker)})
        self.reload()
        return normalize_hold(self.data.get("holds", {}).get(channel_id))

    def clear_if_online(self, channel_id: str, account_state: str) -> bool:
        """Health checks never remove an operator hold."""
        return False

    def _audit(self, channel_id, *, actor, hold, result, count=0,
               detail="", reason=""):
        record = {
            "at": time.time(),
            "channel_id": str(channel_id),
            "actor": str(actor),
            "source": str((hold or {}).get("source") or "human"),
            "reason_code": str((hold or {}).get("reason_code") or ""),
            "kind": str((hold or {}).get("kind") or ""),
            "count_24h": int(count or 0),
            "result": str(result),
            "detail": str(detail or ""),
            "reason": str(reason or ""),
        }
        try:
            from .jsonio import append_jsonl
            append_jsonl(self.path.with_name("publish_hold_audit.jsonl"), record)
        except Exception as exc:
            record["audit_error"] = repr(exc)
        return record

    def clear_manual(self, channel_id: str, *, reason: str,
                     actor: str = "human", reason_code: str | None = None) -> dict:
        if not reason or not reason.strip():
            raise ValueError("manual recovery reason required")
        if actor not in ("human", "bot"):
            raise ValueError(f"unknown clear actor {actor!r}")
        with self.lock:
            self.reload()
            raw = self.data.get("holds", {}).get(channel_id)
            marker = self._emergency_path(channel_id)
            hold = normalize_hold(raw)
            if marker.exists() and hold is None:
                hold = normalize_hold({"kind": PUBLISH_UNKNOWN,
                                       "source": "human", "reason_code": "",
                                       "emergency_path": str(marker)})
            if not hold:
                audit = self._audit(channel_id, actor=actor, hold=None,
                                    result="not_found", reason=reason)
                return {"cleared": False, "refused": False, "code": "not_found",
                        "channel": str(channel_id), "audit": audit}
            if actor == "bot":
                if hold.get("source") != "agent":
                    audit = self._audit(channel_id, actor=actor, hold=hold,
                                        result="refused",
                                        detail="source_not_agent", reason=reason)
                    return {"cleared": False, "refused": True,
                            "code": "source_not_agent",
                            "source": hold.get("source"),
                            "reason_code": hold.get("reason_code"),
                            "channel": str(channel_id), "audit": audit}
                if hold.get("reason_code") not in AUTO_CLEAR_REASON_CODES:
                    audit = self._audit(channel_id, actor=actor, hold=hold,
                                        result="refused",
                                        detail="reason_not_allowed", reason=reason)
                    return {"cleared": False, "refused": True,
                            "code": "reason_not_allowed",
                            "source": hold.get("source"),
                            "reason_code": hold.get("reason_code"),
                            "channel": str(channel_id), "audit": audit}
                if reason_code and reason_code != hold.get("reason_code"):
                    audit = self._audit(channel_id, actor=actor, hold=hold,
                                        result="refused",
                                        detail="reason_code_mismatch", reason=reason)
                    return {"cleared": False, "refused": True,
                            "code": "reason_code_mismatch",
                            "source": hold.get("source"),
                            "reason_code": hold.get("reason_code"),
                            "channel": str(channel_id), "audit": audit}
            self.data.get("holds", {}).pop(channel_id, None)
            self.data.setdefault("resolved", []).append({
                "channel_id": channel_id, "hold": hold, "reason": reason,
                "actor": actor, "source": hold.get("source"),
                "reason_code": hold.get("reason_code"),
                "result": "cleared", "at": time.time()})
            self._save_locked()
            if marker.exists():
                marker.unlink()
            audit = self._audit(channel_id, actor=actor, hold=hold,
                                result="cleared", reason=reason)
            return {"cleared": True, "refused": False, "code": "cleared",
                    "source": hold.get("source"),
                    "reason_code": hold.get("reason_code"),
                    "channel": str(channel_id), "audit": audit}

    def all_holds(self) -> dict:
        self.reload()
        holds = dict(self.data.get("holds", {}))
        import json
        folder = self.path.with_name(self.path.name + ".emergency")
        if folder.exists():
            for marker in folder.glob("*.json"):
                try:
                    entry = json.loads(marker.read_text(encoding="utf-8"))
                    entry.setdefault("source", "human")
                    entry.setdefault("reason_code", "")
                    holds[entry["channel_id"]] = normalize_hold(entry)
                except Exception:
                    holds[marker.stem] = normalize_hold({
                        "kind": PUBLISH_UNKNOWN, "source": "human",
                        "reason_code": "", "reason": "damaged emergency hold"})
        return {channel: normalize_hold(hold) for channel, hold in holds.items()}
