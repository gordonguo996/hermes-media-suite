"""Unified authorisation state -- ONE verdict for panel / self-test / daemon.

1.2.8 (GPT_REVIEW_1.2.7 必修 1).  Before this module the three surfaces each
computed their own answer:

  * ``panel.license_banner`` only counted days to expiry -- it never verified the
    signature, never checked the machine code and never read the clock
    high-water mark, so a licence whose ``signature`` was 128 zeroes still showed
    "授权剩余 1172 天" (green);
  * ``selftest._license_light`` verified the signature but ignored the clock;
  * ``daemon.check_licence`` checked everything but DROPPED ``reasons`` and
    ``clock_rollback`` from the dict it returned, so the display could not
    explain a refusal.

This module is the single entry point.  It answers the same question everywhere,
against the FIXED in-package trust root (``config/vendor_pubkey.hex``), the real
machine fingerprint, the plan's seat and the clock high-water mark, and always
carries a Chinese ``reasons`` list.  A refusal is NAMED (``SIGNATURE_INVALID``,
``CLOCK_ROLLBACK``, ...), never a bare bool.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

#: statuses that only this layer introduces (the rest come from ``license``).
STATUS_NOT_ACTIVATED = "NOT_ACTIVATED"
STATUS_UNREADABLE = "LICENCE_UNREADABLE"
STATUS_NO_VENDOR_KEY = "NO_VENDOR_KEY"
STATUS_TOMBSTONE_FAILED = "TRIAL_TOMBSTONE_FAILED"

#: human-readable, Chinese label per status -- the ONE wording the panel,
#: self-test and daemon all reuse (no more three competing sentences).
STATUS_ZH = {
    "OK": "授权有效",
    "GRACE": "授权已过期，处于离线宽限期",
    "EXPIRED": "授权已过期",
    "MACHINE_MISMATCH": "授权与本机机器码不匹配",
    "SIGNATURE_INVALID": "授权签名验证失败（文件被改动，或不是本服务商签发）",
    "SCHEMA_INVALID": "授权文件字段不完整或类型错误",
    "WEAK_FINGERPRINT": "本机机器指纹太弱，拒绝绑定",
    "CRYPTO_UNAVAILABLE": "本机缺少可用的密码学后端",
    "SEAT_DENIED": "当前账号未授权或已停用",
    "TRIAL": "试用授权",
    STATUS_NOT_ACTIVATED: "尚未导入正式授权",
    STATUS_UNREADABLE: "授权文件损坏或无法解析",
    STATUS_NO_VENDOR_KEY: "供应商公钥缺失或损坏",
    STATUS_TOMBSTONE_FAILED: "防回放墓碑写入失败",
    "CLOCK_ROLLBACK": "检测到系统时间被回拨",
    "CLOCK_STATE_UNREADABLE": "时钟状态文件无法读取",
    "CLOCK_STATE_INVALID": "时钟状态文件损坏或缺失",
    "CLOCK_WRITE_FAILED": "时钟状态无法写入（磁盘满或无权限）",
}

#: The one remediation a customer can actually follow for a broken licence file.
REIMPORT_STEPS = ("请点面板顶部「查看机器码」，把机器码发给服务商换取 8 位激活码，"
                  "填入面板「激活码」框后点「用激活码激活」；也可用服务商签发的 "
                  "license.json 通过包内 import-license.bat 导入。",)
REIMPORT_TEXT = "".join(REIMPORT_STEPS)


def _clock_zh(status: str, reason: str) -> str:
    label = STATUS_ZH.get(status, "时钟状态异常")
    return f"{label}（{status}）" + (f"：{reason}" if reason else "")


def read_license_file(path) -> dict:
    """Read ``license.json`` WITHOUT ever raising.

    Returns ``{"kind", "obj", "error"}`` where ``kind`` is one of
    ``missing`` / ``unreadable`` / ``empty`` / ``corrupt`` / ``ok``.  Only
    ``missing`` is "not activated yet" (a WARN before step 6); every other
    failure is a damaged licence and must be RED with a re-import remedy
    (GPT_REVIEW_1.2.7 必修 2).
    """
    p = Path(path)
    if not p.exists():
        return {"kind": "missing", "obj": None, "error": None}
    try:
        text = p.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return {"kind": "unreadable", "obj": None, "error": f"无法读取：{exc}"}
    except UnicodeDecodeError as exc:
        return {"kind": "unreadable", "obj": None,
                "error": f"不是 UTF-8 文本：{exc}"}
    if not text.strip():
        return {"kind": "empty", "obj": None, "error": "文件内容为空"}
    try:
        obj = json.loads(text)
    except ValueError as exc:
        return {"kind": "corrupt", "obj": None, "error": f"JSON 解析失败：{exc}"}
    if not isinstance(obj, dict):
        return {"kind": "corrupt", "obj": None, "error": "顶层不是 JSON 对象"}
    if not obj:
        return {"kind": "empty", "obj": None, "error": "是空对象 {}"}
    return {"kind": "ok", "obj": obj, "error": None}


def _load_trust_root(layout, *, public_key, public_key_path):
    """The ONE production trust root -- ALWAYS ``paths.install_root()``.

    1.2.10 (GPT_REVIEW_1.2.8 必修 1): this function used to fall back to
    ``require_vendor_public_key(getattr(layout, "program", None))``.
    ``layout.program`` honours ``WINPORT_PROGRAM`` / ``--program``, so anyone
    could point the DISPLAY's crypto trust chain at a directory holding THEIR OWN
    ``config/vendor_pubkey.hex``: the panel then showed a green
    "授权有效，剩余 1172 天" for a licence signed by that other key, while the CLI
    (which already reads the fixed root) correctly refused it -- two answers to
    one question.

    The production root is now ONLY ``require_vendor_public_key()`` (== the
    fixed in-package ``config/vendor_pubkey.hex`` under ``install_root()``, the
    directory this code actually runs from).  ``layout`` is kept in the signature
    only for call-site compatibility and is deliberately NOT consulted.

    ``public_key`` / ``public_key_path`` remain the EXPLICIT internal test
    injection: plain keyword arguments the static harness passes directly.  There
    is no environment variable and no CLI flag that reaches them, so a caller
    cannot redirect the production trust root from outside the process.
    """
    from .license import load_vendor_public_key, require_vendor_public_key
    if public_key is not None:
        return public_key, "ok"                    # explicit test injection only
    try:
        if public_key_path is not None:
            return load_vendor_public_key(public_key_path), "ok"   # test only
        return require_vendor_public_key(), "ok"   # FIXED: install_root() only
    except Exception as exc:                       # ConfigError / OSError / ...
        return None, str(exc)


def unified_license_state(layout, *, now=None, account_id=None,
                          license_path=None, machine_id=None,
                          fingerprint_info=None, public_key=None,
                          public_key_path=None, clock_state_path=None,
                          ) -> dict:
    """The ONE authorisation verdict shared by panel / self-test / daemon.

    Never raises: a broken licence, a missing key or an unreadable clock state
    all come back as a NAMED refusal with a Chinese ``reasons`` list.  The
    fixed trust root is used unless a test injects ``public_key`` /
    ``fingerprint_info`` explicitly (mirrors ``cli._licence_gate``).
    """
    from .license import (clock_guard, publish_gate, CLOCK_BLOCKING_STATUSES,
                          STATUS_SEAT_DENIED)
    from .customer_paths import layout_dir

    lp = Path(license_path) if license_path else Path(layout.license_file)
    out = {
        "status": STATUS_NOT_ACTIVATED,
        "license_status": STATUS_NOT_ACTIVATED,
        "publish_allowed": False,
        "collect_allowed": True,
        "days_left": None,
        "expiry_level": None,
        "level": "bad",
        "text": "",
        "label": STATUS_ZH[STATUS_NOT_ACTIVATED],
        "notices": [],
        "reasons": [],
        "seat": None,
        "clock_rollback": False,
        "clock": None,
        "trust_root": "ok",
        "license_path": str(lp),
        "source": "licstate",
        "tombstone_error": None,
    }

    # ---- clock high-water (always evaluated: a rollback blocks publish) ------
    clock_path = (Path(clock_state_path) if clock_state_path
                  else layout_dir(layout, "run") /
                  "license-clock.json")
    try:
        clock = clock_guard(clock_path, now=now)
    except Exception as exc:                        # never let the clock crash us
        clock = {"rollback": True, "state_ok": False,
                 "status": "CLOCK_WRITE_FAILED", "reason": str(exc),
                 "now": None, "last_seen": None, "high_water": None}
    out["clock"] = clock
    clock_status = clock.get("status") or (
        "CLOCK_ROLLBACK" if clock.get("rollback") else "CLOCK_OK")
    clock_bad = clock_status in CLOCK_BLOCKING_STATUSES or not clock.get(
        "state_ok", True)
    out["clock_rollback"] = bool(clock.get("rollback"))

    # ---- fixed trust root (checked before trial is even considered) ---------
    key, key_note = _load_trust_root(layout, public_key=public_key,
                                     public_key_path=public_key_path)
    if key is None:
        out["status"] = STATUS_NO_VENDOR_KEY
        out["trust_root"] = key_note
        out["reasons"].append(f"供应商公钥缺失或损坏，拒绝发布：{key_note}")
        out["text"] = "供应商公钥缺失或损坏；新增发布暂停，请联系服务商重新安装。"
        return out
    out["trust_root"] = "ok"

    # ---- machine ------------------------------------------------------------
    if fingerprint_info is None or machine_id is None:
        from .fingerprint import fingerprint
        fp = fingerprint()
        if fingerprint_info is None:
            fingerprint_info = fp
        if machine_id is None:
            machine_id = fp.get("machine_id")

    # ---- read the licence file / install-time trial --------------------------
    read = read_license_file(lp)
    if read["kind"] == "missing":
        from .license import trial_state
        trial = trial_state(layout, now=now, account_id=account_id,
                            fingerprint_info=fingerprint_info, clock=clock)
        if trial is not None:
            out.update({k: v for k, v in trial.items()
                        if k not in ("reasons", "notices")})
            out["reasons"] = list(trial.get("reasons") or [])
            out["notices"] = list(trial.get("notices") or [])
            if trial.get("publish_allowed"):
                out["level"] = "ok"
                out["label"] = "试用中"
                out["text"] = ("试用授权进行中，剩余 %s 天"
                               % trial.get("days_left"))
            else:
                out["level"] = "bad"
                out["label"] = STATUS_ZH.get(
                    trial.get("status"), "试用授权被拒绝")
                first = (out["reasons"] or ["试用授权未通过校验"])[0]
                out["text"] = f"试用授权被拒绝（{out['label']}）：{first}"
            return out
        if clock_bad:
            out["status"] = clock_status
            out["level"] = "bad"
            out["label"] = STATUS_ZH.get(clock_status, "时钟校验未通过")
            out["reasons"].append(
                _clock_zh(clock_status, clock.get("reason", ""))
                + "：为防绕过试用期，新增发布已暂停；请校准系统时间后重试。")
            out["text"] = "授权被拒绝（%s）：%s" % (
                out["label"], out["reasons"][-1])
            return out
        from .license import trial_unavailable_reason
        code, why = trial_unavailable_reason(layout, now=now,
                                             fingerprint_info=fingerprint_info)
        out["trial_unavailable"] = code
        out["reasons"].append("未找到授权文件（license.json），且没有可继续的试用记录：拒绝发布。")
        out["notices"].append(why)
        out["text"] = "未激活且试用不可用，新增发布暂停。" + why
        out["level"] = "warn"
        return out
    if read["kind"] != "ok":
        out["status"] = STATUS_UNREADABLE
        out["license_status"] = STATUS_UNREADABLE
        out["reasons"].append(
            f"授权文件损坏（{read['error']}）：新增发布暂停，抓数与历史数据不受影响。"
            + REIMPORT_TEXT)
        out["notices"].append("授权文件损坏：" + REIMPORT_TEXT)
        out["text"] = "授权文件损坏，无法解析；新增发布暂停。" + REIMPORT_TEXT
        return out

    # ---- gate (signature + machine + expiry + seat + clock) -----------------
    try:
        gate = publish_gate(read["obj"], key, machine_id, now=now,
                            account_id=account_id,
                            fingerprint_info=fingerprint_info,
                            clock_rollback=clock_bad)
    except Exception as exc:                        # never let a bad field crash us
        out["status"] = "SCHEMA_INVALID"
        out["reasons"].append(f"授权文件字段异常，无法计算授权状态：{exc}")
        out["text"] = "授权文件字段异常；新增发布暂停。" + REIMPORT_TEXT
        return out

    # A verified formal licence permanently ends the install-time trial.  Do
    # this on observation too, so an installation upgraded from an older build
    # is covered before the customer ever deletes license.json.
    if gate.get("license_status") in ("OK", "GRACE", "EXPIRED"):
        try:
            from .license import mark_trial_license_seen
            mark_trial_license_seen(layout, now=now,
                                    fingerprint_info=fingerprint_info)
        except Exception as exc:
            out["tombstone_error"] = str(exc)
            tombstone_failed = True
        else:
            tombstone_failed = False
    else:
        tombstone_failed = False

    status = gate.get("status")
    out["license_status"] = gate.get("license_status") or status
    if clock_bad and status in ("OK", "GRACE", STATUS_SEAT_DENIED):
        status = clock_status
    out["status"] = status
    out["publish_allowed"] = bool(gate.get("publish_allowed"))
    out["days_left"] = gate.get("days_left")
    out["expiry_level"] = gate.get("level")
    out["seat"] = gate.get("seat")
    out["notices"] = list(gate.get("notices") or [])
    out["reasons"] = list(gate.get("reasons") or [])
    if tombstone_failed:
        # An existing formal licence is valid, but the anti-replay tombstone
        # could not be persisted.  Continuing would let a later delete of
        # license.json restart the trial, so this observation/publish path must
        # fail closed instead of merely recording the error.
        out["publish_allowed"] = False
        out["status"] = STATUS_TOMBSTONE_FAILED
        status = STATUS_TOMBSTONE_FAILED
        out["level"] = "bad"
        out["reasons"].append(
            "已读取正式授权，但防回放墓碑写入失败：%s；新增发布已暂停，"
            "请检查磁盘/注册表权限后重试或联系服务商。"
            % out["tombstone_error"])
        out["notices"].append("防回放墓碑写入失败，本次不发布。")

    # Make the refusal explicit and Chinese-first, even for a bare bool from the
    # gate: every blocked state must carry a named reason.
    if not out["publish_allowed"]:
        if clock_bad and not any(
                clock_status in r or "回拨" in r or "时钟" in r
                for r in out["reasons"]):
            out["reasons"].append(
                _clock_zh(clock_status, clock.get("reason", ""))
                + "：为防绕过授权到期，新增发布已暂停；请校准系统时间后重试。")
        if status in (STATUS_UNREADABLE, "SCHEMA_INVALID") and not out["reasons"]:
            out["reasons"].append(STATUS_ZH[status] + "。" + REIMPORT_TEXT)
        if not out["reasons"]:
            out["reasons"].append(
                f"授权状态 {status}：{STATUS_ZH.get(status, '未通过校验')}")

    # ---- presentational verdict --------------------------------------------
    if status in ("OK", "GRACE"):
        out["level"] = {"none": "ok", "30": "ok", "7": "warn", "1": "warn",
                        "grace": "warn"}.get(out["expiry_level"], "warn")
        days = out["days_left"]
        if status == "GRACE":
            out["label"] = STATUS_ZH["GRACE"]
            out["text"] = ("授权已过期，处于离线宽限期"
                           + (f"（剩余 {-days:.1f} 天）" if days is not None else "")
                           + "；到期后只停新增发布，抓数与历史数据不受影响。")
        else:
            out["label"] = STATUS_ZH["OK"]
            out["text"] = (f"授权有效，剩余 {days:.1f} 天" if days is not None
                           else "授权有效")
            if out["expiry_level"] in ("7", "1") and out["notices"]:
                out["text"] += "；" + out["notices"][0]
    else:
        out["level"] = "bad"
        out["label"] = STATUS_ZH.get(status, "授权未通过校验")
        first = out["reasons"][0] if out["reasons"] else out["label"]
        out["text"] = f"授权被拒绝（{out['label']}）：{first}"
        if status in (STATUS_UNREADABLE, "SCHEMA_INVALID"):
            out["text"] = "授权文件损坏或字段异常；新增发布暂停。" + REIMPORT_TEXT
    return out


def summary_line(state: dict) -> str:
    """One-line self-test / log form, always Chinese + named status."""
    return f"[{state.get('status')}] {state.get('text', '')}".strip()
