"""Licence: Ed25519-signed JSON bound to a machine fingerprint.

Field set and behaviour mirror the Mac plan section 6.1 / Windows plan 9.x:
  customer_id, machine_id, accounts[], plan, quota, features[],
  issued_at, expires_at, grace_days, signature

The cloud only validates licences / counts quota / aggregates read-only
reports; it never holds the customer cookie, login state, media or a browser
instance (hard constraint, background.md section 3).
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone

from . import ed25519_pure
from .errors import ConfigError

STATUS_OK = "OK"
STATUS_MACHINE_MISMATCH = "MACHINE_MISMATCH"
STATUS_EXPIRED = "EXPIRED"
STATUS_GRACE = "GRACE"
STATUS_BAD_SIGNATURE = "SIGNATURE_INVALID"
STATUS_BAD_SCHEMA = "SCHEMA_INVALID"
STATUS_CRYPTO_UNAVAILABLE = "CRYPTO_UNAVAILABLE"
STATUS_WEAK_FINGERPRINT = "WEAK_FINGERPRINT"
STATUS_CLOCK_ROLLBACK = "CLOCK_ROLLBACK"
STATUS_SEAT_DENIED = "SEAT_DENIED"

#: Explicit clock-state verdicts (1.2.8, GPT_REVIEW_1.2.7 必修 1).  The guard
#: used to answer only with a bool, so a caller could not tell "the clock is
#: fine" from "the clock state is unreadable" -- both looked like "no rollback"
#: to a display that only read ``rollback``.  We now name the state.
CLOCK_OK = "CLOCK_OK"
CLOCK_ROLLBACK = "CLOCK_ROLLBACK"
CLOCK_STATE_UNREADABLE = "CLOCK_STATE_UNREADABLE"
CLOCK_STATE_INVALID = "CLOCK_STATE_INVALID"
CLOCK_WRITE_FAILED = "CLOCK_WRITE_FAILED"
CLOCK_BLOCKING_STATUSES = (CLOCK_ROLLBACK, CLOCK_STATE_UNREADABLE,
                           CLOCK_STATE_INVALID, CLOCK_WRITE_FAILED)


def canonical_payload(license_obj: dict) -> bytes:
    """Deterministic bytes that the vendor signs (signature field excluded)."""
    body = {k: v for k, v in license_obj.items() if k != "signature"}
    return json.dumps(body, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def parse_time(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def validate_schema(license_obj: dict) -> list:
    required = ("customer_id", "machine_id", "plan", "issued_at", "expires_at",
                "signature")
    missing = [k for k in required if not license_obj.get(k)]
    errors = [f"missing field: {k}" for k in missing]
    # 1.2.5 (GPT review item 9): a per-seat object used to be accepted with any
    # value types -- ``enabled`` as the STRING "false" is truthy after bool(),
    # and a garbage ``expires_at`` produced ``days_left=None`` and was allowed.
    # We now reject the shape outright so publish_gate sees BAD_SCHEMA.
    if license_obj.get("accounts") is not None:
        accounts = license_obj["accounts"]
        if not isinstance(accounts, list):
            errors.append("accounts must be a list")
        else:
            errors.extend(_validate_accounts(accounts))
    if license_obj.get("features") is not None:
        feats = license_obj["features"]
        if not isinstance(feats, list) or any(not isinstance(f, str) for f in feats):
            errors.append("features must be a list of strings")
    if license_obj.get("quota") is not None and not isinstance(
            license_obj["quota"], dict):
        errors.append("quota must be an object")
    if "seats" in license_obj and license_obj["seats"] is not None:
        try:
            seats = int(license_obj["seats"])
        except (TypeError, ValueError):
            errors.append("seats must be an integer")
        else:
            if seats < 0:
                errors.append("seats must be >= 0")
    for field in ("issued_at", "expires_at"):
        if license_obj.get(field):
            try:
                parse_time(license_obj[field])
            except (ValueError, TypeError, AttributeError):
                errors.append(f"{field} is not ISO-8601")
    if "grace_days" in license_obj and license_obj["grace_days"] is not None:
        try:
            value = float(license_obj["grace_days"])
        except (TypeError, ValueError):
            errors.append("grace_days is not a number")
        else:
            if not math.isfinite(value):
                errors.append("grace_days must be a finite number")
    if "signature" in license_obj and license_obj["signature"] is not None \
            and not isinstance(license_obj["signature"], str):
        errors.append("signature must be a hex string")
    return errors


def _validate_accounts(accounts: list) -> list:
    """Type-strict checks for the accounts[] seats (GPT review 1.2.5 item 9)."""
    errors = []
    for i, entry in enumerate(accounts):
        if isinstance(entry, str):
            if not entry.strip():
                errors.append(f"accounts[{i}] is an empty string")
            continue
        if not isinstance(entry, dict):
            errors.append(f"accounts[{i}] must be a string or object")
            continue
        aid = entry.get("account_id")
        if not isinstance(aid, str) or not aid.strip():
            errors.append(f"accounts[{i}].account_id missing or not a string")
        if "enabled" in entry and not isinstance(entry["enabled"], bool):
            errors.append(f"accounts[{i}].enabled must be a boolean")
        expires = entry.get("expires_at")
        if expires is not None:
            if not isinstance(expires, str):
                errors.append(f"accounts[{i}].expires_at must be ISO-8601")
            else:
                try:
                    parse_time(expires)
                except (ValueError, TypeError, AttributeError):
                    errors.append(f"accounts[{i}].expires_at is not ISO-8601")
        for name in ("platform", "nickname"):
            if name in entry and entry[name] is not None \
                    and not isinstance(entry[name], str):
                errors.append(f"accounts[{i}].{name} must be a string")
    return errors


def grace_days_of(license_obj) -> float:
    """Grace period in days; a missing / null / bad value falls back to the
    default.  1.2.5 (GPT review item 9): ``validate_schema`` used to SKIP
    ``grace_days=None`` while ``check_license`` then ran ``float(None)``."""
    value = (license_obj or {}).get("grace_days")
    if value is None:
        return 14.0
    try:
        days = float(value)
    except (TypeError, ValueError):
        return 14.0
    return days if days >= 0 else 14.0


def check_license(license_obj, public_key: bytes, machine_id: str, *,
                  now=None, verify_signature=True, fingerprint_info=None) -> dict:
    """Return {'status', 'reason', 'days_left', 'features', 'quota'}."""
    now = now or datetime.now(timezone.utc)
    errors = validate_schema(license_obj)
    if errors:
        return {"status": STATUS_BAD_SCHEMA, "reason": "; ".join(errors)}

    # A weak fingerprint (fewer than 2 stable components) hashes almost the same
    # on every machine -> binding would be meaningless and clones would pass.
    if fingerprint_info is not None and fingerprint_info.get("weak"):
        return {"status": STATUS_WEAK_FINGERPRINT,
                "reason": "machine fingerprint has <2 stable components; "
                          "refusing to bind (clone risk)"}

    if verify_signature:
        try:
            signature = bytes.fromhex(license_obj["signature"])
        except ValueError:
            return {"status": STATUS_BAD_SCHEMA, "reason": "signature not hex"}
        if not ed25519_pure.verify(signature, canonical_payload(license_obj),
                                   public_key):
            return {"status": STATUS_BAD_SIGNATURE,
                    "reason": "signature does not verify with vendor key"}

    if license_obj["machine_id"] != machine_id:
        return {"status": STATUS_MACHINE_MISMATCH,
                "reason": "机器指纹不符，请联系换签"}

    expires = parse_time(license_obj["expires_at"])
    days_left = (expires - now).total_seconds() / 86400.0
    grace_days = grace_days_of(license_obj)
    if days_left < 0:
        if days_left >= -grace_days:
            return {"status": STATUS_GRACE, "days_left": days_left,
                    "reason": f"offline grace, {-days_left:.1f} days used",
                    "features": license_obj.get("features", []),
                    "quota": license_obj.get("quota", {})}
        return {"status": STATUS_EXPIRED, "days_left": days_left,
                "reason": "expired beyond grace"}

    return {"status": STATUS_OK, "days_left": days_left,
            "features": license_obj.get("features", []),
            "quota": license_obj.get("quota", {})}


def feature_allowed(license_obj: dict, feature: str) -> bool:
    return feature in (license_obj.get("features") or [])


def license_seats(license_obj: dict | None) -> int:
    """The self-service account ceiling encoded in a licence.

    1.2.18: a licence may carry ``seats`` (default 4).  A missing, null or
    invalid value falls back to the default; the customer-visible limit is
    always capped at 4 by the caller.
    """
    if not isinstance(license_obj, dict) or "seats" not in license_obj:
        return 4
    try:
        seats = int(license_obj["seats"])
    except (TypeError, ValueError):
        return 4
    return seats if seats >= 0 else 4

def _normalise_accounts(license_obj: dict) -> dict:
    """accounts[] may be plain ids or objects; normalise to {id: {...}}.

    Per-号 authorisation (source/extra-requirements-update.md section 4): one
    account = one seat.  A list that holds both a platform and an enabled flag
    lets multiple platforms / multiple accounts be counted and disabled
    separately, without touching anyone else's seat.
    """
    out = {}
    for entry in (license_obj.get("accounts") or []):
        if isinstance(entry, str):
            out[entry] = {"account_id": entry, "enabled": True}
        elif isinstance(entry, dict) and entry.get("account_id"):
            out[entry["account_id"]] = {
                "account_id": entry["account_id"],
                "platform": entry.get("platform"),
                "nickname": entry.get("nickname"),
                "enabled": bool(entry.get("enabled", True)),
                "expires_at": entry.get("expires_at"),
            }
    return out


def authorized_accounts(license_obj: dict) -> dict:
    return _normalise_accounts(license_obj)


def _wildcard_seat(seats: dict, account_id: str):
    """Activation-issued licences cover every local seat (``*`` / ``平台:*``).

    A customer's account ids are created on their own machine
    (``env_1:douyin``), so a licence cannot list them in advance.  The
    vendor signs a wildcard entry instead; matching stays cheapest-first:
    exact id, then ``<platform>:*``, then ``*``.
    """
    platform = account_id.rsplit(':', 1)[-1] if ':' in account_id else None
    for key in ((f"{platform}:*" if platform else None), "*"):
        if key and key in seats:
            entry = dict(seats[key])
            entry["wildcard"] = key
            return entry
    return None


def account_authorization(license_obj: dict, account_id: str, *,
                          now=None) -> dict:
    """Per-seat verdict, so one expired/disabled seat never blocks the rest."""
    now = now or datetime.now(timezone.utc)
    seats = _normalise_accounts(license_obj)
    seat = seats.get(account_id)
    if seat is None:
        seat = _wildcard_seat(seats, account_id)
    if seat is None:
        return {"account_id": account_id, "authorized": False, "enabled": False,
                "reason": f"该号未授权（{account_id} 不在授权名单里），请联系我们加号"}
    if not seat.get("enabled", True):
        return {"account_id": account_id, "authorized": True, "enabled": False,
                "reason": f"该号已停用（{account_id}），其它平台不受影响"}
    expires_at = seat.get("expires_at") or license_obj.get("expires_at")
    if expires_at:
        try:
            days_left = (parse_time(expires_at) - now).total_seconds() / 86400.0
        except ValueError:
            days_left = None
        if days_left is not None and days_left < 0:
            return {"account_id": account_id, "authorized": True, "enabled": False,
                    "days_left": days_left,
                    "reason": f"该号授权已过期（{account_id}），请联系我们续签"}
        seat["days_left"] = days_left
    return {"account_id": account_id, "authorized": True, "enabled": True,
            "platform": seat.get("platform"), "days_left": seat.get("days_left"),
            "reason": "已授权"}


def account_quota(license_obj: dict) -> dict:
    """Count seats by platform -- report only; never a silent upsell."""
    seats = _normalise_accounts(license_obj)
    per_platform = {}
    for seat in seats.values():
        key = seat.get("platform") or "(未标注平台)"
        per_platform[key] = per_platform.get(key, 0) + 1
    quota = dict(license_obj.get("quota") or {})
    quota.setdefault("accounts", 0)
    return {"quota": quota, "seats_total": len(seats),
            "seats_enabled": sum(1 for s in seats.values() if s.get("enabled", True)),
            "per_platform": per_platform,
            "seats": list(seats.values())}


#: fixed, non-overridable location of the trust root inside the program tree.
VENDOR_PUBKEY_RELATIVE = "config/vendor_pubkey.hex"

#: separate, fixed trust root for approval receipts.  Receipts are signed by
#: the control plane with a key that is NOT the licence vendor key, so a
#: missing / leaked vendor private key cannot mint publish approvals.
RECEIPT_PUBKEY_RELATIVE = "config/receipt_pubkey.hex"


def _load_public_key(path, label: str) -> bytes:
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"{label} missing: {p}")
    text = p.read_text(encoding="utf-8-sig").strip()
    if not text:
        raise ConfigError(f"{label} is empty: {p}")
    try:
        key = bytes.fromhex(text)
    except ValueError:
        raise ConfigError(f"{label} is not hex: {p}")
    if len(key) != 32:
        raise ConfigError(f"{label} must be 32 bytes, got {len(key)}: {p}")
    return key


def load_vendor_public_key(path) -> bytes:
    return _load_public_key(path, "vendor public key")


def require_vendor_public_key(program=None) -> bytes:
    """Load the trust root from its FIXED in-package location.

    When ``program`` is omitted the root is derived from ``paths.install_root()``
    -- the directory this code actually runs from -- so neither ``--program`` nor
    ``WINPORT_PROGRAM`` can point the crypto trust chain at another directory
    (GPT review 1.2.5 item 3).  Raises ConfigError when the key is missing /
    empty / malformed -- never returns a soft "allow".
    """
    from pathlib import Path
    if program is None:
        from .paths import install_root
        program = install_root()
    return load_vendor_public_key(Path(program) / VENDOR_PUBKEY_RELATIVE)


def require_receipt_public_key() -> bytes:
    """Load the approval-receipt trust root from its FIXED in-package location.

    This intentionally takes no path/program/key argument: the receipt trust
    root is always ``install_root()/config/receipt_pubkey.hex``.  Missing,
    empty, non-hex or non-32-byte files raise ``ConfigError`` (fail closed).
    """
    from pathlib import Path
    from .paths import install_root
    return _load_public_key(Path(install_root()) / RECEIPT_PUBKEY_RELATIVE,
                            "receipt public key")


# ------------------------------------------------------------------ clock (E)
#: How far the clock may drift backwards before we call it a rollback.  Windows
#: time sync (w32time) and DST-style corrections stay well under this.
CLOCK_ROLLBACK_TOLERANCE_SECONDS = 300


def clock_guard(state_path, *, now=None,
                tolerance_seconds=CLOCK_ROLLBACK_TOLERANCE_SECONDS) -> dict:
    """Monotonic-time guard against "wind the clock back to revive a lapsed licence".

    We record the highest UTC time we have ever trusted in a small state file and
    refuse the *publish* path when the current clock is meaningfully BEHIND that
    high-water mark.  This defeats a pure "change the system clock" attack.

    Threat boundary (documented, and deliberately honest -- see README/VERIFY):
    an attacker with full local control can also roll this state file back, and
    an OFFLINE client cannot prevent that.  The real authority is the server-side
    signed issued_at / short lease; this guard is defence-in-depth, not a promise
    of tamper-proofness.
    """
    from .jsonio import write_json
    from pathlib import Path
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    path = Path(state_path)
    state_ok = True
    reason = ""
    last = None
    status = CLOCK_OK
    # 1.2.5 (GPT review item 4): distinguish "no state yet" (a genuine FIRST run
    # on a fresh install) from "state present but NOT usable".  A present but
    # empty / null / unparseable / key-missing file is treated as TAMPERED:
    # fail closed AND do not rewrite it, so a retry cannot "repair" the trust
    # by overwriting the corrupt file with the current (attacker-set) time.
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:               # unreadable state file
            text = None
            state_ok = False
            status = CLOCK_STATE_UNREADABLE
            reason = f"时钟状态文件无法读取：{exc}"
        if text is not None:
            stripped = text.strip()
            if not stripped:
                state_ok = False
                status = CLOCK_STATE_INVALID
                reason = "时钟状态文件为空（可能被清空或篡改），拒绝发布"
            else:
                try:
                    state = json.loads(stripped)
                except ValueError as exc:
                    state = None
                    state_ok = False
                    status = CLOCK_STATE_INVALID
                    reason = f"时钟状态文件已损坏：{exc}"
                if state_ok and not isinstance(state, dict):
                    state_ok = False
                    status = CLOCK_STATE_INVALID
                    reason = "时钟状态文件不是 JSON 对象，拒绝发布"
                if state_ok:
                    raw = state.get("last_seen_utc")
                    if not isinstance(raw, str) or not raw.strip():
                        state_ok = False
                        status = CLOCK_STATE_INVALID
                        reason = "时钟高水位记录缺失或为空，无法判断是否回拨，拒绝发布"
                    else:
                        try:
                            last = parse_time(raw)
                        except (ValueError, TypeError, AttributeError):
                            state_ok = False
                            status = CLOCK_STATE_INVALID
                            reason = "时钟高水位记录不是有效时间，无法判断是否回拨，拒绝发布"
                            last = None
    rollback = bool(last is not None
                    and now < last - timedelta(seconds=tolerance_seconds))
    high = now if (last is None or now > last) else last
    if state_ok:
        try:
            write_json(path, {"last_seen_utc": high.isoformat(),
                              "updated_at": now.isoformat(),
                              "rollback_detected": rollback})
        except OSError as exc:               # cannot persist the new high-water
            state_ok = False
            status = CLOCK_WRITE_FAILED
            reason = reason or f"时钟状态无法写入（磁盘满/无权限）：{exc}"
    # 1.2.8 (GPT_REVIEW_1.2.7 必修 1): the verdict is a NAMED state, not just a
    # bool.  A display that only read ``rollback`` could not tell "clock fine"
    # from "clock state unreadable"; now both the panel and the self-test show
    # the reason in Chinese.
    if status == CLOCK_OK and rollback:
        status = CLOCK_ROLLBACK
    # Fail CLOSED: if we cannot trust the clock state we treat it as a rollback,
    # so "cannot check" never becomes "no rollback" (GPT review 1.2.5 item 4).
    return {"rollback": bool(rollback or not state_ok),
            "state_ok": state_ok,
            "status": status,
            "reason": reason,
            "now": now.isoformat(),
            "last_seen": last.isoformat() if last else None,
            "high_water": high.isoformat(),
            "tolerance_seconds": tolerance_seconds}


# ---------------------------------------------------------------- expiry (E)
#: 授权到期提醒 + 「到期只停新增发布，不影响已有数据与抓数」(design-refs C/E).
#: Publish is the only thing that stops: collect / report / downlink keep
#: working, because the customer's login state and their historical data are
#: theirs and must not be held hostage by a lapsed seat.
DEFAULT_NOTICE_THRESHOLDS = (30, 7, 1)

#: Statuses that forbid a NEW publish (collect / report / evidence still run).
#: 1.2.5: a detected clock rollback also forbids a NEW publish (see clock_guard).
PUBLISH_BLOCKING = (STATUS_EXPIRED, STATUS_MACHINE_MISMATCH, STATUS_BAD_SIGNATURE,
                    STATUS_BAD_SCHEMA, STATUS_WEAK_FINGERPRINT,
                    STATUS_CRYPTO_UNAVAILABLE, STATUS_CLOCK_ROLLBACK)

NOTICE_TEXT = {
    30: "授权还有 30 天到期，请安排续期（不影响现在的发布与抓数）。",
    7: "授权还有 7 天到期，请尽快续期；到期后只停新增发布，抓数与历史数据不受影响。",
    1: "授权 1 天内到期！请立刻续期；到期后新增发布会被暂停，抓数与已有数据不受影响。",
}


def expiry_notices(license_obj: dict, *, now=None,
                   thresholds=DEFAULT_NOTICE_THRESHOLDS) -> dict:
    """Return the expiry reminder for a licence (E).

    ``level`` is the tightest threshold already reached ("none"/"30"/"7"/"1"/
    "grace"/"expired"); ``notices`` is the ordered list of human sentences to
    show in the tray + panel.
    """
    now = now or datetime.now(timezone.utc)
    errors = validate_schema(license_obj)
    if errors:
        return {"level": "invalid", "days_left": None,
                "notices": ["授权文件不完整：" + "；".join(errors)],
                "expiry_blocks_publish": True, "collect_allowed": True,
                "renew_url": "", "reason": "SCHEMA_INVALID"}
    try:
        expires = parse_time(license_obj["expires_at"])
    except (ValueError, TypeError, AttributeError):
        return {"level": "invalid", "days_left": None,
                "notices": ["授权到期时间不是 ISO-8601，无法计算剩余天数"],
                "expiry_blocks_publish": True, "collect_allowed": True,
                "renew_url": "", "reason": "BAD_EXPIRES_AT"}
    days_left = (expires - now).total_seconds() / 86400.0
    grace_days = grace_days_of(license_obj)
    notices = []
    level = "none"
    if days_left >= 0:
        # ascending: pick the TIGHTEST threshold the licence has already reached
        # (days_left=3 with thresholds 30/7/1 must say "7", not "30").
        for threshold in sorted({int(t) for t in thresholds}):
            if days_left <= threshold:
                level = str(threshold)
                notices.append(NOTICE_TEXT.get(
                    threshold,
                    f"授权还有 {threshold} 天到期，请安排续期。"))
                break
    elif days_left >= -grace_days:
        level = "grace"
        notices.append(
            f"授权已过期 {-days_left:.1f} 天，处于离线宽限期内；尽快续期，"
            "宽限期结束后新增发布会暂停（抓数与历史数据不受影响）。")
    else:
        level = "expired"
        notices.append("授权已过期，新增发布已暂停；抓数与历史数据仍可用，续期后自动恢复。")
    return {
        "level": level,
        "days_left": round(days_left, 2),
        "expires_at": license_obj["expires_at"],
        "notices": notices,
        # 1.2.5 (GPT review item 1): this flag speaks ONLY about expiry -- it is
        # deliberately NOT called "publish_allowed" so a diagnostic can never show
        # a competing "allowed" next to the real, full publish gate.
        "expiry_blocks_publish": level in ("expired", "invalid"),
        "collect_allowed": True,
        "renew_url": license_obj.get("renew_url", ""),
        "reason": "ok" if level not in ("expired", "invalid") else level,
    }


def publish_gate(license_obj, public_key: bytes, machine_id: str, *,
                 now=None, account_id=None, fingerprint_info=None,
                 clock_rollback=False) -> dict:
    """Decide whether a NEW publish may start (E).

    Data / collect are never gated here: this function only answers the publish
    question, so a lapsed licence can never take away 抓数 or 历史数据.

    ``fingerprint_info`` (from ``fingerprint.fingerprint()``) turns on the weak-
    fingerprint clone check; ``clock_rollback`` (from ``clock_guard``) blocks a
    publish attempt made after the system clock was wound back.
    """
    result = check_license(license_obj, public_key, machine_id, now=now,
                           fingerprint_info=fingerprint_info)
    notice = expiry_notices(license_obj, now=now)
    seat = None
    if account_id:
        seat = account_authorization(license_obj, account_id, now=now)
    allowed = result["status"] not in PUBLISH_BLOCKING
    status = result["status"]
    reasons = []
    if not allowed:
        reasons.append(f"授权状态 {result['status']}：{result.get('reason', '')}")
    if seat is not None and not seat.get("enabled", True):
        allowed = False
        if status in (STATUS_OK, STATUS_GRACE):
            status = STATUS_SEAT_DENIED
        reasons.append(seat.get("reason", "该号未授权或已停用"))
    if clock_rollback:
        allowed = False
        reasons.append("检测到系统时间被回拨（CLOCK_ROLLBACK）：为防绕过授权到期，"
                       "新增发布已暂停；请校准系统时间后重试。")
    return {
        "publish_allowed": bool(allowed),
        "collect_allowed": True,
        "status": status,
        "license_status": result["status"],
        "days_left": notice.get("days_left"),
        "level": notice.get("level"),
        "notices": notice.get("notices", []),
        "seat": seat,
        "clock_rollback": bool(clock_rollback),
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# 14 天自动试用（1.2.40）：安装后未导入 license.json 时自动开通，绑定本机。
# 目的是让客户/测试方装完即可登录、排期、发布，不必先等人工签发；
# 正式授权导入后以 license.json 为准（status=OK），试用文件不再生效。
# ---------------------------------------------------------------------------
TRIAL_DAYS = 14
TRIAL_REG_KEY = r"Software\HuolongAgent\Agent"
_TRIAL_VERSION = 2
_TRIAL_RECORD_VALUE = "TrialRecord"
_TRIAL_SEEN_VALUE = "TrialSeen"


def _utc(value=None):
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _trial_path(layout):
    from pathlib import Path
    from .customer_paths import layout_dir
    root = layout_dir(layout, "system")
    return root / "trial.json"


def _trial_user():
    """Stable user identity for the portable (non-Windows test) fallback."""
    if os.name == "nt":
        domain = str(os.environ.get("USERDOMAIN") or "").strip()
        user = str(os.environ.get("USERNAME") or "").strip()
        return (domain + "\\" + user) if domain else (user or "windows-user")
    try:
        return "uid:%s" % os.getuid()
    except (AttributeError, OSError):
        return "user:unknown"


def _dpapi_protect(raw: bytes) -> bytes:
    """Encrypt with DPAPI user scope: another Windows user/machine cannot read it."""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    source = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    in_blob = DATA_BLOB(len(raw), source)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.c_wchar_p,
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    if not crypt32.CryptProtectData(
            ctypes.byref(in_blob), "manju-trial-record-v2", None, None, None,
            0, ctypes.byref(out_blob)):
        raise OSError(ctypes.get_last_error(),
                      "CryptProtectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))


def _dpapi_unprotect(raw: bytes) -> bytes:
    """Decrypt a DPAPI-user record; failure is corruption, never a reset."""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    source = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    in_blob = DATA_BLOB(len(raw), source)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.POINTER(ctypes.c_wchar_p),
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    if not crypt32.CryptUnprotectData(
            ctypes.byref(in_blob), None, None, None, None, 0,
            ctypes.byref(out_blob)):
        raise OSError(ctypes.get_last_error(),
                      "CryptUnprotectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))


def _portable_check(raw: bytes, machine_id: str, user: str) -> str:
    """Corruption check for Linux tests; Windows always uses DPAPI above."""
    material = (raw + b"|" + machine_id.encode("utf-8") + b"|" +
                user.encode("utf-8"))
    return hashlib.sha256(material).hexdigest()


def _trial_container(record):
    raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if os.name == "nt":
        protection = "dpapi-user"
        payload = _dpapi_protect(raw)
        check = ""
    else:
        protection = "portable-user"
        payload = raw
        check = _portable_check(payload, str(record.get("machine") or ""),
                                str(record.get("user") or ""))
    return {"version": _TRIAL_VERSION, "protection": protection,
            "payload": base64.b64encode(payload).decode("ascii"),
            "check": check}


def _open_trial_container(container, machine_id, user):
    if not isinstance(container, dict):
        return None
    if container.get("version") != _TRIAL_VERSION:
        return None
    protection = container.get("protection")
    try:
        payload = base64.b64decode(str(container.get("payload") or ""),
                                   validate=True)
    except (ValueError, TypeError):
        return None
    if protection == "dpapi-user":
        if os.name != "nt":
            return None
        try:
            raw = _dpapi_unprotect(payload)
        except OSError:
            return None
    elif protection == "portable-user":
        # The portable form is a public SHA256, not a trusted seal.  It exists
        # only for the Linux test/graceful-degradation path; a Windows reader
        # must never accept it, otherwise a user can mint a fresh trial start
        # without touching DPAPI.  Windows is fail-closed even when the record
        # claims the Windows platform.
        if os.name == "nt":
            logging.getLogger(__name__).warning(
                "trial record refused: portable-user protection on Windows")
            return None
        if not hmac.compare_digest(
                str(container.get("check") or ""),
                _portable_check(payload, machine_id, user)):
            return None
        raw = payload
    else:
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("machine") != machine_id or record.get("user") != user:
        return None
    if type(record.get("days")) is not int or record["days"] != TRIAL_DAYS:
        return None
    if not isinstance(record.get("license_seen"), bool):
        return None
    if not isinstance(record.get("issued"), str) or not record["issued"].strip():
        return None
    return record


def _trial_record(machine_id, issued, license_seen=False):
    record = {"schema": _TRIAL_VERSION, "machine": machine_id,
              "user": _trial_user(), "issued": issued,
              "days": TRIAL_DAYS, "license_seen": bool(license_seen),
              "platform": os.name}
    return record


def _parse_trial_record(record, machine_id, now):
    if not isinstance(record, dict):
        return None
    if record.get("schema") != _TRIAL_VERSION:
        return None
    # A record that explicitly claims another platform was copied or forged.
    # Older v2 records have no platform field; missing is not a mismatch, but
    # any present and different value is refused and logged.
    claimed_platform = record.get("platform")
    if claimed_platform is not None and claimed_platform != os.name:
        logging.getLogger(__name__).warning(
            "trial record refused: platform %r does not match %r",
            claimed_platform, os.name)
        return None
    if record.get("machine") != machine_id or record.get("user") != _trial_user():
        return None
    if type(record.get("days")) is not int or record["days"] != TRIAL_DAYS:
        return None
    if not isinstance(record.get("license_seen"), bool):
        return None
    if not isinstance(record.get("issued"), str) or not record["issued"].strip():
        return None
    try:
        issued = parse_time(record["issued"])
    except (ValueError, TypeError, AttributeError):
        return None
    if issued > now + timedelta(minutes=5):
        return None
    return dict(record)


def _read_trial_registry():
    if os.name != "nt":
        return None
    logger = logging.getLogger(__name__)
    try:
        import winreg
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, TRIAL_REG_KEY)
        except FileNotFoundError:
            return None
        with key:
            values = {}
            types = {}
            errors = {}
            for name in (_TRIAL_RECORD_VALUE, _TRIAL_SEEN_VALUE):
                try:
                    values[name], types[name] = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    errors[name] = "value_missing"
                except OSError as exc:
                    errors[name] = "query_error: %s" % exc
    except ImportError as exc:
        logger.warning("trial registry unavailable: %s", exc)
        return {"error": "winreg unavailable: %s" % exc}
    except OSError as exc:
        logger.warning("trial registry key query failed: %s", exc)
        return {"error": "key_query_error: %s" % exc}
    raw = values.get(_TRIAL_RECORD_VALUE)
    seen = values.get(_TRIAL_SEEN_VALUE)
    state = errors.get(_TRIAL_RECORD_VALUE, "valid")
    if not errors and (types.get(_TRIAL_RECORD_VALUE) != winreg.REG_SZ or
                       types.get(_TRIAL_SEEN_VALUE) != winreg.REG_SZ):
        state = "invalid_type"
    if state == "valid" and (not isinstance(raw, str) or not raw.strip()):
        state = "empty"
    container = None
    if state == "valid":
        try:
            container = json.loads(raw)
        except (TypeError, ValueError):
            state = "invalid_json"
        if not isinstance(container, dict):
            state = "invalid_container"
    corrupt = bool(errors or state != "valid" or seen not in ("0", "1"))
    if corrupt:
        seen_state = errors.get(
            _TRIAL_SEEN_VALUE,
            "empty_or_invalid" if seen not in ("0", "1") else "valid")
        logger.warning(
            "trial registry is untrusted: record=%s seen=%s value_errors=%s",
            state, seen_state, errors)
    return {"container": container, "seen": seen == "1", "present": True,
            "raw_present": raw is not None, "corrupt": corrupt,
            "state": state, "value_errors": errors}


def _write_trial_registry(record):
    if os.name != "nt":
        return
    try:
        import winreg

        with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, TRIAL_REG_KEY, 0,
                winreg.KEY_SET_VALUE) as key:
            payload = json.dumps(_trial_container(record), sort_keys=True,
                                 separators=(",", ":"))
            winreg.SetValueEx(key, _TRIAL_RECORD_VALUE, 0, winreg.REG_SZ, payload)
            winreg.SetValueEx(key, _TRIAL_SEEN_VALUE, 0, winreg.REG_SZ,
                              "1" if record.get("license_seen") is True else "0")
    except ImportError as exc:
        raise OSError("winreg unavailable on Windows: %s" % exc) from exc


def _write_trial_file(layout, record):
    path = _trial_path(layout)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(_trial_container(record), ensure_ascii=False),
        encoding="utf-8")
    os.replace(temporary, path)


def _write_trial_record(layout, record):
    """Write the record to both stores; tombstone writes registry first."""
    if record.get("license_seen") is True:
        registry_error = None
        try:
            _write_trial_registry(record)
        except OSError as exc:
            registry_error = exc
        try:
            _write_trial_file(layout, record)
        except OSError as exc:
            if registry_error is not None:
                raise OSError("registry tombstone failed (%s); file tombstone "
                              "failed (%s)" % (registry_error, exc)) from exc
            raise
        if registry_error is not None:
            raise registry_error
        return
    _write_trial_file(layout, record)
    try:
        _write_trial_registry(record)
    except OSError:
        try:
            _trial_path(layout).unlink()
        except OSError:
            pass
        raise


def _same_trial_record(left, right):
    fields = ("schema", "machine", "user", "issued", "days", "license_seen",
              "platform")
    return all(left.get(field) == right.get(field) for field in fields)


def _collect_trial_records(layout, machine_id, now):
    """Read BOTH stores, with the registry tombstone authoritative."""
    registry = _read_trial_registry()
    registry_error = bool(isinstance(registry, dict) and registry.get("error"))
    registry_present = bool(registry is not None and not registry_error)
    registry_seen = bool(registry and registry.get("seen"))
    registry_record = None
    # A key that exists but whose value cannot be parsed is "already used or
    # damaged", never "absent": otherwise a corrupted HKCU value plus a valid
    # file record re-opens the one-time trial.
    registry_corrupt = bool(registry_present and registry.get("corrupt"))
    if (registry_present and registry.get("raw_present")
            and registry.get("container") is None):
        registry_corrupt = True
    if registry_present and registry.get("container") is not None:
        opened = _open_trial_container(registry["container"], machine_id,
                                       _trial_user())
        registry_record = _parse_trial_record(opened, machine_id, now)
        registry_corrupt = registry_corrupt or registry_record is None

    path = _trial_path(layout)
    file_record = None
    file_corrupt = False
    try:
        path.stat()
        file_present = True
    except FileNotFoundError:
        file_present = False
    except OSError:
        file_present = True
        file_corrupt = True
    if file_present:
        try:
            container = json.loads(path.read_text(encoding="utf-8-sig"))
            opened = _open_trial_container(container, machine_id, _trial_user())
            file_record = _parse_trial_record(opened, machine_id, now)
            file_corrupt = file_record is None
        except (OSError, UnicodeError, ValueError):
            file_corrupt = True

    tombstone = bool(registry_seen or (file_record and file_record.get(
        "license_seen") is True) or (registry_record and registry_record.get(
            "license_seen") is True))
    conflict = False
    if file_record is not None and registry_record is not None:
        if not _same_trial_record(file_record, registry_record):
            conflict = True
    if registry_seen and file_record is not None and \
            file_record.get("license_seen") is not True:
        conflict = True
    if registry_present and not registry_seen and file_record is not None and \
            file_record.get("license_seen") is True:
        conflict = True

    record = registry_record or file_record
    if tombstone and file_record is not None and file_record.get(
            "license_seen") is True:
        record = file_record
    corrupt = bool(registry_error or registry_corrupt or file_corrupt)
    blocked = bool(tombstone or conflict or corrupt)
    if tombstone:
        source = "tombstone"
    elif conflict:
        source = "conflict"
    elif record is not None:
        source = ("both" if registry_record is not None and
                  file_record is not None else
                  "registry" if registry_record is not None else "file")
    elif corrupt or registry_present or file_present:
        source = "corrupt"
    else:
        source = "missing"
    return {"record": record, "source": source, "blocked": blocked,
            "tombstone": tombstone, "conflict": conflict,
            "corrupt": corrupt, "registry_error": registry_error}


def _read_trial_record(layout, machine_id, now):
    state = _collect_trial_records(layout, machine_id, now)
    return state["record"], state["source"]


def initialize_trial(layout, now=None, *, allow_create=True,
                     fingerprint_info=None):
    """Create the install-time trial once; never restart it after loss."""
    from pathlib import Path
    from .fingerprint import fingerprint

    now = _utc(now)
    fp = fingerprint_info or fingerprint()
    machine_id = str(fp.get("machine_id") or "")
    if not machine_id or fp.get("weak"):
        return {"created": False,
                "reason": "machine fingerprint is weak; trial not issued"}
    if Path(layout.license_file).exists():
        try:
            mark_trial_license_seen(layout, now=now, fingerprint_info=fp)
        except OSError as exc:
            return {"created": False,
                    "reason": "formal licence present, but trial tombstone "
                              "could not be written: %s" % exc}
        return {"created": False, "reason": "formal licence present; trial disabled"}
    state = _collect_trial_records(layout, machine_id, now)
    record, source = state["record"], state["source"]
    if state["blocked"]:
        return {"created": False,
                "reason": "trial tombstone/record is unavailable or corrupt; "
                          "refusing to restart it"}
    if record is not None:
        return {"created": False, "reason": "existing trial kept (%s)" % source,
                "issued": record.get("issued")}
    if source != "missing":
        return {"created": False, "reason": "unusable existing trial; refusing reset"}
    if not allow_create:
        return {"created": False,
                "reason": "trial record missing on existing data; refusing reset"}
    record = _trial_record(machine_id, now.isoformat(), False)
    try:
        _write_trial_record(layout, record)
    except OSError as exc:
        return {"created": False, "reason": "trial write failed: %s" % exc}
    return {"created": True, "reason": "trial started", "issued": record["issued"]}


def mark_trial_license_seen(layout, now=None, *, fingerprint_info=None):
    """Record that a formal licence was installed, so its deletion grants no trial."""
    from .fingerprint import fingerprint

    now = _utc(now)
    fp = fingerprint_info or fingerprint()
    machine_id = str(fp.get("machine_id") or "")
    if not machine_id or fp.get("weak"):
        raise OSError("cannot bind trial tombstone: machine fingerprint is weak")
    state = _collect_trial_records(layout, machine_id, now)
    record = state.get("record")
    issued = (record or {}).get("issued") or now.isoformat()
    tombstone = _trial_record(machine_id, issued, True)
    _write_trial_record(layout, tombstone)
    return True


def _trial_refusal(status, reason, clock_rollback=False):
    return {
        "status": status,
        "license_status": "TRIAL",
        "publish_allowed": False,
        "collect_allowed": True,
        "days_left": None,
        "level": "bad",
        "seat": None,
        "notices": [],
        "reasons": [reason],
        "clock_rollback": bool(clock_rollback),
        "license_path": "",
        "source": "trial",
    }


def trial_state(layout, now=None, *, account_id=None, fingerprint_info=None,
                clock=None):
    """Return a trial verdict only from the sealed install-time record.

    A missing, corrupt, deleted or machine-mismatched record is NOT recreated
    here.  Clock rollback, a weak fingerprint and a missing account are checked
    before any trial allowance is returned.
    """
    from pathlib import Path
    from .fingerprint import fingerprint

    now = _utc(now)
    fp = fingerprint_info or fingerprint()
    machine_id = str(fp.get("machine_id") or "")
    if not machine_id or fp.get("weak"):
        return _trial_refusal(
            STATUS_WEAK_FINGERPRINT,
            "本机机器指纹太弱，试用授权拒绝绑定；新增发布暂停。")
    if clock is None:
        from .customer_paths import layout_dir
        clock = clock_guard(layout_dir(layout, "run", "run")
                            / "license-clock.json", now=now)
    clock_status = clock.get("status") or (
        CLOCK_ROLLBACK if clock.get("rollback") else CLOCK_OK)
    if clock.get("rollback") or not clock.get("state_ok", True):
        return _trial_refusal(
            clock_status,
            "时钟校验未通过（%s）：为防绕过试用期，新增发布已暂停；"
            "请校准系统时间后重试。" % clock_status,
            clock_rollback=True)
    # Read the registry tombstone BEFORE trusting any file record.  A restored
    # old trial.json must never outvote a formal-licence tombstone.
    state = _collect_trial_records(layout, machine_id, now)
    if state["blocked"]:
        return None
    if Path(layout.license_file).exists():
        return None
    record = state["record"]
    if record is None or record.get("license_seen") is True:
        return None
    try:
        issued = parse_time(record["issued"])
    except (ValueError, TypeError, AttributeError):
        return None
    left = (issued + timedelta(days=TRIAL_DAYS) - now).total_seconds() / 86400.0
    if left <= 0:
        return None
    seat = None
    if account_id:
        seat = {"account_id": account_id, "authorized": True, "enabled": True,
                "reason": "试用期内"}
    return {
        "status": "TRIAL",
        "license_status": "TRIAL",
        "publish_allowed": True,
        "collect_allowed": True,
        "days_left": round(left, 1),
        "level": "ok",
        "seat": seat,
        "notices": ["试用授权：剩余 %d 天，导入正式授权后自动切换。" % int(left)],
        "reasons": [],
        "clock_rollback": False,
        "license_path": str(_trial_path(layout)),
        "source": "trial",
    }
