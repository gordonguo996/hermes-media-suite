"""Local control panel (the customer-facing "status lights" screen).

Why a local web panel and not a GUI window: the bundled CPython runtime does
NOT contain ``tkinter`` (the NuGet build excludes it), so a Tk window would
mean shipping a different runtime.  A loopback-only HTTP panel needs nothing
beyond the standard library, cannot be reached from the network, and gives the
customer what was promised:

  * no command line -- they double-click a shortcut, the panel opens in their
    normal browser;
  * 1.1.0: ONE CARD PER ENVIRONMENT (1\u53f7 / 2\u53f7 / \u5fae\u4fe1 / \u5c0f\u7ea2\u4e66), and inside each
    card one status light per platform it carries
    (source/env-design.md: \u9762\u677f\u6309\u73af\u5883\u5c55\u793a);
  * one click to start the login for a platform, one click to re-check it;
  * the real-Chrome health banner (found / version / policy) at the top.

Only loopback binding, no cookies and no credentials are ever rendered: the
page shows counts, domains, ports and reasons.
"""

from __future__ import annotations

import html
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import chrome, health
from .jsonio import write_json
from .platforms import DEFAULT_PANEL_PORT
from .topics import format_topics
from .version import APP_NAME_DISPLAY

STATUS_COLORS = {
    "ONLINE": "#128a3c",
    "OFFLINE": "#c62828",
    "UNVERIFIED": "#c62828",
    "DISABLED": "#5f6368",
    "UNKNOWN": "#5f6368",
}

STATUS_LABELS = {
    "ONLINE": "\u767b\u5f55\u6709\u6548",
    "OFFLINE": "\u6709\u95ee\u9898",
    "UNVERIFIED": "\u5f85\u68c0\u67e5",
    "DISABLED": "\u5df2\u505c\u7528",
    "UNKNOWN": "\u672a\u77e5",
}

#: D -- \u73af\u5883/\u8d26\u53f7\u72b6\u6001\u673a\u7684 6+1 \u4e2a\u6001\uff08state_machine.LABELS \u7684\u955c\u50cf\uff0c\u4f9b\u9875\u9762\u914d\u8272\uff09
ACCOUNT_STATE_COLORS = {
    "VALID": "#128a3c",
    "NOT_LOGGED_IN": "#c62828",
    "DROPPED": "#c62828",
    "RATE_LIMITED": "#b26a00",
    "BANNED": "#7f1d1d",
    "UNKNOWN": "#5f6368",
    "DISABLED": "#9ca3af",
}

#: Customer-facing wording only.  The state machine keeps its precise internal
#: labels; the panel deliberately collapses them to three words the customer
#: already understands: \u672a\u767b\u5f55 / \u5df2\u767b\u5f55 / \u5f02\u5e38.
ENV_STATUS_LABELS = {
    "ONLINE": "\u5df2\u767b\u5f55",
    "UNVERIFIED": "\u672a\u767b\u5f55",
    "OFFLINE": "\u672a\u767b\u5f55",
    "UNKNOWN": "\u5f02\u5e38",
    "DISABLED": "\u5f02\u5e38",
}

ACCOUNT_STATE_LABELS = {
    "VALID": "\u5df2\u767b\u5f55",
    "NOT_LOGGED_IN": "\u672a\u767b\u5f55",
    "DROPPED": "\u5f02\u5e38",
    "RATE_LIMITED": "\u5f02\u5e38",
    "BANNED": "\u5f02\u5e38",
    "UNKNOWN": "\u5f02\u5e38",
    "DISABLED": "\u5f02\u5e38",
}

ENV_STATUS_CLASSES = {
    "ONLINE": "st-ok",
    "OFFLINE": "st-bad",
    "UNVERIFIED": "st-bad",
    "DISABLED": "st-muted",
    "UNKNOWN": "st-muted",
}

ACCOUNT_STATE_CLASSES = {
    "VALID": "st-ok",
    "NOT_LOGGED_IN": "st-bad",
    "DROPPED": "st-bad",
    "RATE_LIMITED": "st-warn",
    "BANNED": "st-bad",
    "UNKNOWN": "st-muted",
    "DISABLED": "st-muted",
}

STATUS_CLASS_NAMES = {"st-ok", "st-bad", "st-warn", "st-muted"}


def _env_status_class(status) -> str:
    return ENV_STATUS_CLASSES.get(status, "st-muted")


def _account_status_class(account_state) -> str:
    return ACCOUNT_STATE_CLASSES.get(account_state, "st-muted")


def _color_class(color: str) -> str:
    return {
        "#128a3c": "st-ok",
        "#c62828": "st-bad",
        "#7f1d1d": "st-bad",
        "#b26a00": "st-warn",
        "#5f6368": "st-muted",
        "#9ca3af": "st-muted",
    }.get(color, "st-muted")


STATE_HINTS = {
    "NOT_LOGGED_IN": "\u8fd8\u6ca1\u767b\u5f55\uff0c\u70b9\u300c\u5f00\u59cb\u767b\u5f55\u300d",
    "DROPPED": "\u767b\u5f55\u5df2\u5931\u6548\uff0c\u70b9\u300c\u5f00\u59cb\u767b\u5f55\u300d\u91cd\u65b0\u767b\u5f55",
    "RATE_LIMITED": "\u64cd\u4f5c\u592a\u9891\u7e41\uff0c\u7a0d\u540e\u518d\u8bd5",
    "BANNED": "\u8d26\u53f7\u88ab\u9650\u5236\uff0c\u8bf7\u8054\u7cfb\u6211\u4eec",
    "UNKNOWN": "\u6682\u65f6\u5224\u65ad\u4e0d\u4e86\uff0c\u70b9\u300c\u6211\u767b\u5f55\u597d\u4e86\u300d\u518d\u68c0\u67e5\u4e00\u6b21",
}

#: Toast/result wording is produced by lower layers that also feed logs and
#: diagnostics.  Keep those internals intact, but translate the few sentences
#: that can reach the customer's panel into plain language at this boundary.
_PLAIN_TEXT_RULES = (
    (re.compile(r"127\.0\.0\.1"), "\u672c\u673a"),
    (re.compile(r"cookie", re.I), "\u8d26\u53f7\u4fe1\u606f"),
    (re.compile('\u767b\u5f55\u6001(\u76ee\u5f55)?'), "\u767b\u5f55\u4fe1\u606f"),
    (re.compile('\u8c03\u8bd5\u7aef\u53e3\\s*\\d*'), "\u6d4f\u89c8\u5668\u8fde\u63a5"),
    (re.compile('\u7aef\u53e3\\s*\\d+'), "\u6d4f\u89c8\u5668\u8fde\u63a5"),
    (re.compile('\u72b6\u6001\u706f'), "\u72b6\u6001"),
    (re.compile('\u9759\u6001\u81ea\u68c0'), "\u68c0\u67e5"),
    (re.compile('\u5931\u8d25\u7559\u8bc1(\u76ee\u5f55)?'), "\u51fa\u9519\u8bb0\u5f55"),
    (re.compile('\u7559\u8bc1\u76ee\u5f55'), "\u51fa\u9519\u8bb0\u5f55"),
    (re.compile(r"CDP_NOT_UP"), "\u6d4f\u89c8\u5668\u8fde\u63a5\u5931\u8d25"),
    (re.compile(r"CDP", re.I), "\u6d4f\u89c8\u5668\u8fde\u63a5"),
    (re.compile(r"922[4-7]"), ""),
    (re.compile('\u4f01\u4e1a\u7b56\u7565'), "Chrome \u8bbe\u7f6e"),
)


def _plain_text(value) -> str:
    text = "" if value is None else str(value)
    for pattern, replacement in _PLAIN_TEXT_RULES:
        text = pattern.sub(replacement, text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return text


def _clean_result(result):
    """Return a copy of an action result with customer-facing words cleaned."""
    if not isinstance(result, dict):
        return result
    cleaned = dict(result)
    for key in ("message", "reason"):
        if isinstance(cleaned.get(key), str):
            cleaned[key] = _plain_text(cleaned[key])
    return cleaned


def chrome_banner(layout, *, report=None) -> dict:
    report = report or chrome.detect(layout, store=False)
    policy = report.get("policy") or {}
    level = "ok"
    if not report.get("found"):
        level = "bad"
    elif report.get("version_ok") is False:
        level = "bad"
    elif policy.get("blocked") or report.get("version_changed") \
            or report.get("engine_changed"):
        level = "warn"
    return {"level": level, "text": chrome.summarize(report),
            "download_url": chrome.CHROME_DOWNLOAD_URL,
            "offline_download_url": chrome.CHROME_OFFLINE_DOWNLOAD_URL,
            "found": bool(report.get("found")),
            "engine": report.get("engine"),
            "engine_label": report.get("engine_label"),
            "tier": report.get("tier"),
            "version": report.get("version"),
            "version_ok": report.get("version_ok"),
            "policy_blocked": bool(policy.get("blocked")),
            "version_changed": bool(report.get("version_changed")),
            "engine_changed": bool(report.get("engine_changed"))}


def publish_status(layout):
    from .publish_guard import PublishHoldStore
    from .jsonio import read_json
    from .settings import load_settings
    try:
        enabled = bool((load_settings(layout.settings_file).get("publish_queue") or {}).get("enabled"))
        jobs = []
        labels = {"queued": "\u5df2\u6392\u961f", "pending": "\u7b49\u5f85\u98de\u4e66\u786e\u8ba4", "running": "\u6267\u884c\u4e2d",
                  "published": "\u5df2\u53d1\u5e03", "blocked": "\u5df2\u963b\u65ad", "needs_reconcile": "\u7ed3\u679c\u5f85\u6838\u5bf9"}
        for path in sorted((layout.run / "publish-queue").glob("*.json")):
            job = read_json(path)
            piece = (job.get("plan") or {}).get("piece") or {}
            jobs.append({"job_id": job.get("job_id") or path.stem,
                         "title": (piece.get("title") or path.stem),
                         "status": labels.get(job.get("status"), "\u672a\u77e5"),
                         "status_code": job.get("status"),
                         "reason": job.get("reason") or "",
                         "topics": piece.get("topics") or [],
                         "account_id": (job.get("plan") or {}).get("account_id") or "",
                         "topics_reapproval_required": bool(job.get("topics_reapproval_required")),
                         "schedule_at": (job.get("plan") or {}).get("schedule_at") or ""})
        holds = PublishHoldStore(layout.run / "publish_holds.json").all_holds()
        return {"enabled": enabled, "jobs": jobs,
                "holds": {channel: hold.get("reason", "\u53d1\u5e03\u51bb\u7ed3") for channel, hold in holds.items()}}
    except Exception:
        return {"enabled": False, "error": "\u53d1\u5e03\u8bb0\u5f55\u65e0\u6cd5\u8bfb\u53d6\uff0c\u8bf7\u8054\u7cfb\u670d\u52a1\u5546\u6838\u67e5", "jobs": [], "holds": {}}


def build_state(layout, channels, *, envs=None, tcp=None, processes=None,
                chrome_report=None) -> dict:
    from . import state_machine
    from . import envadmin
    from . import cadence as cadence_mod
    from .remote_channel import remote_support_view
    base_rows = health.probe_all(layout, channels, tcp=tcp or health.tcp_listening,
                                 processes=processes)
    rows = state_machine.annotate(base_rows,
                                  previous=state_machine.load_previous(layout))
    visible_envs = envadmin.visible_envs(envs) if envs else None
    environments = health.environments_view(visible_envs if envs is not None else envs,
                                            rows)
    account_count = len(visible_envs or [])
    account_limit = envadmin.license_account_limit(layout)
    remote_support = remote_support_view(layout)
    try:
        from .version import VERSION as _VERSION_VALUE
    except Exception:
        _VERSION_VALUE = ""
    account_licenses = {}
    for row in rows:
        account_id = row.get("account_id")
        if not account_id:
            continue
        if account_id not in account_licenses:
            account_licenses[account_id] = license_banner(
                layout, account_id=account_id)
        row["license"] = account_licenses[account_id]
    for environment in environments:
        environment["license"] = _worst_license(
            [row.get("license") for row in (environment.get("rows") or [])
             if row.get("license")])
    base_license = license_banner(layout)
    denied = [item for item in account_licenses.values()
              if item.get("publish_allowed") is False]
    header_license = denied[0] if denied else base_license
    if len(denied) > 1:
        header_license = dict(header_license)
        header_license["text"] = ("%d 个账号授权被拒绝；%s"
                                  % (len(denied), header_license.get("text", "")))
    return {"title": APP_NAME_DISPLAY,
            "version": _VERSION_VALUE,
            "environments": environments,
            "channels": rows,
            "update": __import__("winport.updater", fromlist=["update_banner"]).update_banner(layout),
            "counts": _counts([r for r in rows
                               if visible_envs is None
                               or r.get("env_id") in {e.env_id for e in visible_envs}]),
            "chrome": chrome_banner(layout, report=chrome_report),
            "license": header_license,
            "control": control_banner(layout),
            "evidence": evidence_banner(layout),
            "cadence": cadence_mod.cadence_view(layout),
            "remote_channel": __import__("winport.settings", fromlist=["load_settings"]).load_settings(layout.settings_file).get("remote_channel", {"enabled": True}),
            "remote_support": remote_support,
            "support_code": (_support_code(layout)
                             if remote_support.get("enabled")
                             and remote_support.get("provisioned") else ""),
            "auto_activation_available": _auto_activation_available(),
            "publish_status": publish_status(layout),
            "account_count": account_count,
            "account_limit": account_limit,
            "account_full": account_count >= account_limit,
            "account_default_name": "" if account_count >= account_limit
                                   else f"{account_count + 1}\u53f7",
            "available_platforms": [{"id": "douyin", "name": "\u6296\u97f3"},
                                    {"id": "xingtu", "name": "\u661f\u56fe"}],
            "hidden_platforms_note": "\u5fae\u4fe1\uff08\u89c6\u9891\u53f7/\u516c\u4f17\u53f7\uff09\u3001\u5c0f\u7ea2\u4e66\u672c\u7248\u672a\u5f00\u901a"}


def license_banner(layout, *, license_path=None, public_key=None,
                   public_key_path=None, machine_id=None,
                   fingerprint_info=None, account_id=None, now=None) -> dict:
    """E: the panel's ONE authorisation verdict (never blocks collect).

    1.2.8 (GPT_REVIEW_1.2.7 \u5fc5\u4fee 1): the panel used to count days to expiry
    only -- a licence whose ``signature`` was 128 zeroes still showed
    "\u6388\u6743\u5269\u4f59 1172 \u5929" in green, because neither the signature, the machine code
    nor the clock high-water mark was ever consulted here.  This banner now
    reuses ``licstate.unified_license_state`` (fixed trust root + machine/seat +
    clock) exactly like the daemon and the self-test, so a refusal is visible --
    and named, in Chinese -- on the status screen too.

    The keyword-only overrides exist for the static test harness (``cli``
    ``_licence_gate`` has the same shape); production passes only ``layout``.
    """
    from .licstate import unified_license_state
    try:
        state = unified_license_state(
            layout, now=now, account_id=account_id, license_path=license_path,
            machine_id=machine_id, fingerprint_info=fingerprint_info,
            public_key=public_key, public_key_path=public_key_path)
    except Exception as exc:                        # last-ditch: never crash the panel
        return {"level": "bad", "days_left": None,
                "publish_allowed": False,
                "text": "\u6388\u6743\u72b6\u6001\u65e0\u6cd5\u8ba1\u7b97\uff1b\u65b0\u589e\u53d1\u5e03\u6682\u505c\uff0c\u6293\u6570\u4e0e\u5386\u53f2\u6570\u636e\u4e0d\u53d7\u5f71\u54cd\u3002"
                        "\u8bf7\u91cd\u65b0\u5bfc\u5165\u670d\u52a1\u5546\u7b7e\u53d1\u7684 license.json\u3002",
                "notices": [f"\u6388\u6743\u72b6\u6001\u5f02\u5e38\uff1a{exc}"],
                "status": "ERROR", "reasons": [f"\u6388\u6743\u72b6\u6001\u5f02\u5e38\uff1a{exc}"],
                "clock_rollback": False}
    return {"level": state.get("level", "warn"),
            "days_left": state.get("days_left"),
            "publish_allowed": bool(state.get("publish_allowed")),
            "text": state.get("text", ""),
            "notices": state.get("notices", []),
            "status": state.get("status"),
            "license_status": state.get("license_status"),
            "account_id": state.get("account_id", account_id),
            "seat": state.get("seat"),
            "reasons": state.get("reasons", []),
            "clock_rollback": state.get("clock_rollback", False)}


def evidence_banner(layout) -> dict:
    """B: where failures are kept + how long they are kept."""
    from .evidence import EvidenceStore
    try:
        store = EvidenceStore(layout)
        return {"root": str(store.root), "retention_days": store.retention_days,
                "entries": len(store.list_entries())}
    except Exception:
        return {"root": str(getattr(layout, "evidence", "")),
                "retention_days": None, "entries": None}


def _counts(rows):
    out = {}
    for row in rows:
        out[row["status"]] = out.get(row["status"], 0) + 1
    return out


def _platform_state(row) -> tuple:
    """Return (customer status word, color, short hint) for one platform."""
    account_state = row.get("account_state")
    if account_state in ACCOUNT_STATE_LABELS:
        label = ACCOUNT_STATE_LABELS[account_state]
        color = ACCOUNT_STATE_COLORS.get(account_state, "#5f6368")
        hint = STATE_HINTS.get(account_state, "")
        if account_state == "VALID":
            hint = ""
        return label, color, hint

    status = row.get("status")
    label = ENV_STATUS_LABELS.get(status, "\u5f02\u5e38")
    color = STATUS_COLORS.get(status, "#5f6368")
    if status == "ONLINE":
        return label, color, ""
    if status in ("OFFLINE", "UNVERIFIED"):
        return label, color, "\u8fd8\u6ca1\u767b\u5f55\uff0c\u70b9\u300c\u5f00\u59cb\u767b\u5f55\u300d"
    return label, color, "\u6682\u65f6\u5224\u65ad\u4e0d\u4e86\uff0c\u70b9\u300c\u6211\u767b\u5f55\u597d\u4e86\u300d\u518d\u68c0\u67e5\u4e00\u6b21"


def _chrome_view(banner) -> dict:
    """One plain-language Chrome line; never exposes a path or policy wording."""
    if not banner.get("found"):
        return {"level": "bad", "text": "\u6ca1\u627e\u5230 Google Chrome",
                "link": banner.get("download_url") or chrome.CHROME_DOWNLOAD_URL,
                "link_text": "\u600e\u4e48\u88c5"}
    if banner.get("version_ok") is False:
        return {"level": "bad", "text": "Google Chrome \u9700\u8981\u66f4\u65b0",
                "link": chrome.CHROME_DOWNLOAD_URL, "link_text": "\u600e\u4e48\u66f4\u65b0"}
    if banner.get("policy_blocked"):
        return {"level": "warn", "text": "Chrome \u7684\u8bbe\u7f6e\u9700\u8981\u8c03\u6574",
                "link": None, "link_text": ""}
    if banner.get("version_changed") or banner.get("engine_changed"):
        return {"level": "warn", "text": "Chrome \u5df2\u66f4\u65b0\uff0c\u5efa\u8bae\u91cd\u65b0\u68c0\u67e5\u4e00\u6b21",
                "link": None, "link_text": ""}
    return {"level": "ok", "text": "\u5df2\u627e\u5230 Google Chrome",
            "link": None, "link_text": ""}


def _auth_view(licence) -> dict:
    """One short authorisation line; no technical or file-format wording."""
    status = (licence or {}).get("status")
    if (licence or {}).get("publish_allowed") is False:
        if status == "SEAT_DENIED":
            return {"level": "bad", "text": "\u5f53\u524d\u8d26\u53f7\u672a\u6388\u6743\u6216\u5df2\u505c\u7528"}
        return {"level": "bad", "text": "\u6388\u6743\u88ab\u62d2\u7edd\uff08%s\uff09" % (
            status or "\u672a\u901a\u8fc7\u6821\u9a8c")}
    if status == "TRIAL":
        return {"level": "ok", "text": "\u8bd5\u7528\u6388\u6743\u4e2d"}
    if status == "OK":
        return {"level": "ok", "text": "\u6388\u6743\u6709\u6548"}
    if status == "GRACE":
        return {"level": "warn", "text": "\u6388\u6743\u5df2\u8fc7\u671f\uff08\u5bbd\u9650\u671f\uff09"}
    if status == "EXPIRED":
        return {"level": "bad", "text": "\u6388\u6743\u5df2\u8fc7\u671f"}
    if status == "NOT_ACTIVATED":
        return {"level": "warn", "text": "\u6388\u6743\u672a\u6fc0\u6d3b"}
    if (licence or {}).get("level") == "ok" and status is None:
        return {"level": "ok", "text": "\u6388\u6743\u6709\u6548"}
    return {"level": "warn", "text": "\u6388\u6743\u9700\u8981\u5904\u7406"}


def control_banner(layout) -> dict:
    """The customer-visible Feishu/control bridge status.

    The on-disk control.json proves that provisioning produced a binding, but
    it does not prove that the control plane accepts it right now.  Green is
    reserved for a ``connected`` status written by a real poll *after* this
    control.json was saved.  A newer runtime ``error``/``disabled`` status wins;
    otherwise a valid-but-unverified file shows ``pending`` ("连接中…"), never
    green.  A deleted/corrupt control.json still never shows green merely
    because an old status line said "connected".
    """
    from .activation import is_valid_control
    from .control_status import read_status
    valid_control = False
    valid_mtime = None
    candidates = [Path(layout.data) / "control.json"]
    program = getattr(layout, "program", None)
    if program:
        candidates.append(Path(program) / "control.json")
    for candidate in candidates:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if is_valid_control(data):
            valid_control = True
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                mtime = None
            valid_mtime = mtime
            break
    status = read_status(layout) or {}
    state = status.get("state") or "missing"
    reason = status.get("reason") or ""
    updated = status.get("updated")
    try:
        updated = float(updated) if updated is not None else None
    except (TypeError, ValueError):
        updated = None
    fresh = (updated is not None and valid_mtime is not None
             and updated >= valid_mtime)
    if valid_control:
        # A saved binding is not a verified connection.  Only a status written
        # after this exact control.json by a real successful poll may be green.
        if state == "connected" and fresh:
            return {"state": "connected", "level": "ok", "reason": "",
                    "text": "飞书接入：已连接"}
        if state in ("error", "disabled", "missing") and fresh:
            if state == "error":
                level = "bad"
                reason = reason or "接入配置失败"
            else:
                level = "warn"
                reason = reason or ("接入配置缺失" if state == "missing"
                                    else "接入服务未启动")
            return {"state": state, "level": level, "reason": reason,
                    "text": "飞书接入：未连接（%s）" % reason}
        return {"state": "pending", "level": "warn", "reason": "",
                "text": "飞书接入：连接中…"}
    if state == "connected":
        state, reason = "missing", "接入配置文件缺失，请点「重新获取接入配置」"
    if state == "pending":
        level = "warn"
        reason = reason or "等待接入服务首次连接"
    elif state == "missing":
        reason = reason or "尚未获取接入配置，请用激活码激活或点「重新获取接入配置」"
        level = "warn"
    elif state == "error":
        level = "bad"
        reason = reason or "接入配置失败"
    else:
        level = "warn"
        reason = reason or "接入服务未启动"
    return {"state": state, "level": level, "reason": reason,
            "text": "飞书接入：未连接（%s）" % reason}


def _worst_license(items):
    """Pick a refused account verdict before an allowed one."""
    items = [item for item in (items or []) if item]
    for item in items:
        if item.get("publish_allowed") is False:
            return item
    return items[0] if items else {}


def _platform_row(row) -> str:
    status_text, color, hint = _platform_state(row)
    account_state = row.get("account_state")
    status_class = (_account_status_class(account_state) if account_state
                    else _env_status_class(row.get("status")))
    hint_html = f'<p class="phint">{html.escape(hint)}</p>' if hint else ""
    licence_view = _auth_view(row.get("license") or {})
    licence_html = ('<p class="phint">\u8d26\u53f7\u6388\u6743\uff1a%s</p>'
                    % html.escape(str(licence_view.get("text") or "")))
    channel_id = html.escape(str(row.get("channel_id", "")))
    platform_name = html.escape(str(row.get("platform_name") or row.get("platform") or ""))
    return f"""
      <div class="prow" data-row="{channel_id}">
        <span class="dot {status_class}"></span>
        <span class="pname">{platform_name}</span>
        <span class="pstatus {status_class}">{status_text}</span>
        <span class="pact">
          <button onclick="act('open','{channel_id}',this)">\u5f00\u59cb\u767b\u5f55</button>
          <button onclick="act('check','{channel_id}',this)">\u6211\u767b\u5f55\u597d\u4e86</button>
        </span>
        {hint_html}{licence_html}
      </div>"""


def _env_card(card) -> str:
    status_text = ENV_STATUS_LABELS.get(card.get("status"), "\u5f02\u5e38")
    color = STATUS_COLORS.get(card.get("status"), "#5f6368")
    status_class = _env_status_class(card.get("status"))
    rows = card.get("rows") or []
    if rows:
        platform_html = "".join(_platform_row(row) for row in rows)
    else:
        platform_html = '<p class="phint">\u8fd8\u6ca1\u6709\u5e73\u53f0</p>'
    env_id = html.escape(str(card.get("env_id", "")))
    label = html.escape(str(card.get("label") or card.get("env_id") or ""))
    disabled = bool(card.get("enabled") is False)
    disabled_text = "\uff08\u5df2\u505c\u7528\uff09" if disabled else ""
    licence_view = _auth_view(card.get("license") or {})
    licence_text = html.escape(str(licence_view.get("text") or ""))
    licence_class = html.escape(str(licence_view.get("level") or "warn"))
    return f"""
  <section class="card" id="{env_id}" data-env="{env_id}">
    <div class="head">
      <span class="dot {status_class}"></span>
      <h2>{label}{disabled_text}</h2>
      <span class="status {status_class}">{status_text}</span>
    </div>
    <p class="licence {licence_class}">\u8d26\u53f7\u6388\u6743\uff1a{licence_text}</p>
    <div class="plats">{platform_html}</div>
    <div class="env-actions">
      <button class="danger" onclick="disableEnv('{env_id}',this)">\u505c\u7528\u8d26\u53f7</button>
      <button class="danger" onclick="deleteEnv('{env_id}',this)">\u5220\u9664\u8d26\u53f7</button>
    </div>
  </section>"""


def _cadence_section(cadence) -> str:
    """1.2.20: customer-facing publish-cadence settings and schedule."""
    cadence = cadence or {}
    cfg = cadence.get("settings") or {}
    windows = cfg.get("window_entries") or [
        {"id": f"w{i}", "start": start, "end": end, "enabled": True}
        for i, (start, end) in enumerate(cfg.get("windows") or [])]
    gap_min = int(cfg.get("gap_min") or cfg.get("min_gap_minutes") or 5)
    gap_max = int(cfg.get("gap_max") or cfg.get("max_gap_minutes") or 8)
    cross_gap = cfg.get("cross_account_gap_minutes")
    if isinstance(cross_gap, (list, tuple)):
        cross_gap_text = f"{cross_gap[0]}\u2013{cross_gap[-1]} \u968f\u673a"
    else:
        cross_gap_text = "5\u20138 \u968f\u673a"
    max_per_day = int(cfg.get("max_per_day_per_account") or 0)
    manual_mode = bool(cadence.get("manual_mode", False))
    today_published = cadence.get("today_published") or {}
    tasks = cadence.get("tasks") or []
    unplaced = cadence.get("unplaced") or []
    account_ids = sorted(set(
        list(today_published.keys()) +
        [task.get("account_id") for task in tasks
         if task.get("account_id")] +
        [row.get("account_id") for row in unplaced
         if row.get("account_id")]))
    count_lines = []
    cap_text = "\u4e0d\u9650" if max_per_day == 0 else str(max_per_day)
    for account_id in account_ids:
        count_lines.append(
            f'<span class="cad-count"><b>{html.escape(str(account_id))}</b> '
            f'\u4eca\u5929\u5df2\u53d1 {int(today_published.get(account_id, 0))} / '
            f'{cap_text}</span>')
    count_html = "".join(count_lines) or '<span class="cad-count">\u6682\u65e0\u6392\u671f</span>'
    next_at = html.escape(str(cadence.get("next_publish_at") or "\u6682\u65e0"))
    task_rows = []
    for task in tasks:
        task_id = str(task.get("task_id") or "")
        account_id = html.escape(str(task.get("account_id") or ""))
        schedule_at = html.escape(str(task.get("schedule_at") or ""))
        status = html.escape(str(task.get("status_label") or task.get("status") or ""))
        task_rows.append(
            f'<div class="cad-task" data-task="{html.escape(task_id)}">'
            f'<span>{account_id}</span>'
            f'<input id="time-{html.escape(task_id)}" type="text" '
            f'value="{schedule_at}" title="\u8ba1\u5212\u53d1\u5e03\u65f6\u95f4\uff0c\u683c\u5f0f YYYY-MM-DD HH:MM">'
            f'<span>{status}</span>'
            f'<button onclick="saveCadenceTime('
            f'{json.dumps(task_id)},this)">\u4fdd\u5b58</button>'
            f'<button class="danger" onclick="cancelCadenceTask('
            f'{json.dumps(task_id)},this)">\u53d6\u6d88</button></div>')
    task_html = "".join(task_rows) or '<p class="phint">\u672a\u6765\u6392\u671f\u4e3a\u7a7a\uff0c\u4fdd\u5b58\u53c2\u6570\u540e\u4f1a\u91cd\u65b0\u751f\u6210</p>'
    unplaced_rows = []
    for row in unplaced:
        item_id = str(row.get("item_id") or "")
        title = html.escape(str(row.get("title") or row.get("file_name") or item_id))
        account_id = html.escape(str(row.get("account_id") or ""))
        reason = html.escape(str(row.get("reason") or "\u6392\u4e0d\u4e0b\uff0c\u8f6c\u4eba\u5de5"))
        action = html.escape(f"markManualPublished({json.dumps(item_id)},{json.dumps(str(row.get('account_id') or ''))},this)", quote=True)
        unplaced_rows.append(
            f'<div class="cad-task cad-manual-row" data-unplaced="{html.escape(item_id)}">'
            f'<span>{account_id}</span><span>{title}</span>'
            f'<span>{reason}</span>'
            f'<button onclick="{action}">\u6838\u9a8c\u5e76\u8bb0\u5df2\u53d1</button></div>')
    unplaced_html = "".join(unplaced_rows) or \
        '<p class="phint">\u6682\u65e0\u9700\u8981\u4eba\u5de5\u5904\u7406\u7684\u7d20\u6750</p>'
    manual_style = "" if manual_mode else ' style="display:none"'
    manual_checked = " checked" if manual_mode else ""
    return f"""
<section class="cadence">
  <div class="cad-head">
    <h2>\u53d1\u5e03\u8282\u594f</h2>
    <span class="cad-next">\u4e0b\u4e00\u6b21\u53d1\u5e03\uff1a{next_at}</span>
  </div>
  <div class="cad-counts">{count_html}</div>
  <div class="cad-form">
    <label class="cad-slider">\u6bcf\u5929\u6bcf\u4e2a\u53f7\u53d1\u51e0\u6761
      <input id="cadence-max" type="range" min="0" max="30" value="{max_per_day}"
             oninput="document.getElementById('cadence-max-val').textContent=
               this.value == '0' ? '\u4e0d\u9650' : this.value">
      <output id="cadence-max-val">{cap_text}</output>
    </label>
    <label class="cad-gap"><span>\u540c\u53f7/\u8de8\u53f7\u968f\u673a\u95f4\u9694</span>
      <input id="cadence-min-gap" type="number" min="5" max="8" step="1"
             value="{gap_min}"> \u2013
      <input id="cadence-max-gap" type="number" min="5" max="8" step="1"
             value="{gap_max}"> \u5206\u949f</label>
    <label class="cad-gap"><span>\u8de8\u53f7\u9519\u5cf0</span>
      <b>{cross_gap_text}</b></label>
    <label class="cad-manual-toggle">
      <input id="cadence-manual-mode" type="checkbox"{manual_checked}
             onchange="setManualMode(this.checked)"> \u624b\u5de5\u6a21\u5f0f\uff08\u9ed8\u8ba4\u5173\u95ed\uff09
    </label>
    <button onclick="saveCadence(this)">\u4fdd\u5b58\u5e76\u81ea\u52a8\u91cd\u6392\u672a\u6765\u6392\u671f</button>
  </div>
  <div class="cad-manual"{manual_style}>
    <h3>\u5f85\u624b\u5de5\u53d1</h3>
    {unplaced_html}
  </div>
  <div class="cad-tasks">{task_html}</div>
</section>"""


def _topics_section(cadence, publish=None) -> str:
    """Per-material topic editor (single save + batch fill)."""
    cadence = cadence or {}
    publish = publish or {}
    materials = []
    linked_jobs = set()
    for task in cadence.get("tasks") or []:
        task_id = str(task.get("task_id") or "")
        if not task_id:
            continue
        if task.get("job_id"):
            linked_jobs.add(str(task.get("job_id")))
        materials.append({
            "target": "task:" + task_id,
            "title": task.get("title") or task.get("file_name") or task_id,
            "account": task.get("account_id") or "",
            "schedule": task.get("schedule_at") or "",
            "status": task.get("status_label") or task.get("status") or "",
            "topics": task.get("topics") or [],
            "needs_reapproval": bool(task.get("needs_reapproval")),
            "editable": True,
        })
    for row in cadence.get("unplaced") or []:
        if row.get("published"):
            continue
        item_id = str(row.get("item_id") or row.get("id") or "")
        if not item_id:
            continue
        materials.append({
            "target": "unplaced:" + item_id,
            "title": row.get("title") or row.get("file_name") or item_id,
            "account": row.get("account_id") or "",
            "schedule": "\u5f85\u4eba\u5de5",
            "status": row.get("reason") or "",
            "topics": row.get("topics") or [],
            "needs_reapproval": bool(row.get("needs_reapproval")),
            "editable": True,
        })
    for job in publish.get("jobs") or []:
        job_id = str(job.get("job_id") or "")
        status = str(job.get("status_code") or "")
        if not job_id or job_id in linked_jobs:
            continue
        if status not in ("queued", "pending", "blocked"):
            continue
        materials.append({
            "target": "job:" + job_id,
            "title": job.get("title") or job_id,
            "account": job.get("account_id") or "",
            "schedule": job.get("schedule_at") or "",
            "status": job.get("status") or "",
            "topics": job.get("topics") or [],
            "needs_reapproval": bool(job.get("topics_reapproval_required")),
            "editable": status != "running",
        })
    if not materials:
        return ""
    rows = []
    for item in materials:
        target = html.escape(item["target"], quote=True)
        try:
            value = format_topics(item["topics"])
        except (TypeError, ValueError):
            value = str(item["topics"] or "")
        value = html.escape(value, quote=True)
        title = html.escape(str(item["title"]))
        meta = " \u00b7 ".join(html.escape(str(value)) for value in
                                (item["account"], item["schedule"], item["status"])
                                if value)
        warning = ('<span class="topic-warn">\u9700\u91cd\u65b0\u5ba1\u6279'
                   '</span>' if item["needs_reapproval"] else "")
        disabled = "" if item.get("editable", True) else " disabled"
        rows.append(
            f'<div class="topic-row" data-target="{target}">'
            f'<input type="checkbox" class="topic-pick" value="{target}" '
            f'title="\u52fe\u9009\u540e\u53ef\u6279\u91cf\u586b\u5165" '
            f'aria-label="\u9009\u62e9\u7d20\u6750">'
            f'<span class="topic-meta"><b>{title}</b><small>{meta}{warning}</small></span>'
            f'<input class="topic-input" type="text" maxlength="2000" '
            f'value="{value}" '
            f'placeholder="#\u706b\u9f99\u6f2b\u5267 #\u65b0\u5267\uff1b\u7559\u7a7a\u8868\u793a\u4e0d\u586b"{disabled}>'
            f'<button onclick="saveMaterialTopics(this)"{disabled}>\u4fdd\u5b58</button>'
            f'</div>')
    return f"""
<section class="cadence topics">
  <div class="cad-head">
    <h2>\u7d20\u6750\u8bdd\u9898</h2>
    <span class="cad-next">\u4e00\u6761\u7d20\u6750\u4e00\u4e2a\u8bdd\u9898\uff0c\u4e92\u4e0d\u5f71\u54cd</span>
  </div>
  <p class="phint">\u53d1\u7d20\u6750\u65f6\u5e26\u4e00\u53e5\u300c\u8bdd\u9898 xxx\u300d\u4f1a\u81ea\u52a8\u5e26\u5165\uff1b\u8fd9\u91cc\u53ef\u5355\u6761\u6539\uff0c\u4e5f\u53ef\u52fe\u9009\u591a\u6761\u6279\u91cf\u586b\u5165\u3002\u8bdd\u9898\u53d8\u4e86\u9700\u91cd\u65b0\u5ba1\u6279\u3002</p>
  <div class="topic-batch">
    <input id="topic-batch-input" type="text" maxlength="2000"
           placeholder="#\u706b\u9f99\u6f2b\u5267 #\u65b0\u5267\uff08\u7a7a\u683c\u6216\u9017\u53f7\u5206\u9694\uff09">
    <button onclick="applyMaterialTopicsBatch(this)">\u6279\u91cf\u586b\u5165</button>
  </div>
  <div class="topic-list">{''.join(rows)}</div>
</section>"""


def _support_code(layout):
    """Return the per-machine remote-support bearer so the user can read it out.

    The token is generated and stored on this machine only; showing it in the
    local panel (loopback + consent checkbox) is the only way support can be
    granted access without the customer running any command.
    """
    try:
        return (layout.data / "remote-channel" / "token").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _relay_port(layout):
    """The per-machine relay port shown to the customer (persisted or derived)."""
    try:
        from .remote_channel import display_relay_port
        return display_relay_port(layout)
    except Exception:
        return None


TOPICS_JS = r"""
async function postMaterialTopics(targets, topics) {
  try {
    const r = await fetch('/api/material-topics', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({targets: targets, topics: topics})
    });
    const data = await r.json();
    const result = data.result || {};
    toast(result.message || data.error || (result.ok ? '\u5df2\u4fdd\u5b58\u8bdd\u9898' : '\u4fdd\u5b58\u5931\u8d25'));
    if (result.ok) setTimeout(() => location.reload(), 500);
    return result;
  } catch (e) {
    toast('\u64cd\u4f5c\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5');
    return {ok: false};
  }
}
function saveMaterialTopics(btn) {
  const row = btn.closest('.topic-row');
  if (!row) return;
  const input = row.querySelector('.topic-input');
  btn.disabled = true;
  postMaterialTopics([row.dataset.target], input ? input.value : '')
    .finally(() => { btn.disabled = false; });
}
function applyMaterialTopicsBatch(btn) {
  const targets = Array.from(document.querySelectorAll('.topic-pick:checked'))
    .map(item => item.value);
  const input = document.getElementById('topic-batch-input');
  if (!targets.length) { toast('\u8bf7\u5148\u52fe\u9009\u8981\u586b\u5165\u7684\u7d20\u6750'); return; }
  if (!input) return;
  btn.disabled = true;
  postMaterialTopics(targets, input.value)
    .finally(() => { btn.disabled = false; });
}
"""


def render_html(state: dict) -> str:
    update_view = state.get("update") or {}
    update_html = ""
    if update_view.get("text"):
        update_class = {"ok": "st-ok", "warn": "st-warn",
                        "bad": "st-bad"}.get(update_view.get("level"), "st-muted")
        update_html = ('<section class="cadence" style="padding:12px 16px">'
                       '<div class="cad-head"><h2>程序更新</h2>'
                       f'<span class="cad-next"><span class="dot {update_class}"></span> '
                       f'{html.escape(str(update_view["text"]))}</span></div></section>')
    cards = update_html + "".join(_env_card(card) for card in state.get("environments", []))
    cadence_html = _cadence_section(state.get("cadence"))
    cadence_html += _topics_section(state.get("cadence"),
                                    state.get("publish_status"))
    cadence_html += "<script>" + TOPICS_JS + "</script>"
    try:
        from .version import VERSION as _VERSION_VALUE
    except Exception:
        _VERSION_VALUE = ''
    _ver = (state.get('version') or _VERSION_VALUE or '')
    _bits = ['\u7a0b\u5e8f\u7248\u672c <code>%s</code>' % html.escape(str(_ver))]
    cadence_html += '<section style="padding:10px 16px"><p class="st-muted">' + ' \uff5c '.join(_bits) + '</p></section>'
    remote_support = state.get('remote_support') or {}
    remote_checked = ' checked' if (state.get('remote_channel') or {}).get('enabled', True) else ''
    cadence_html += ('<section><h2>\u8fdc\u7a0b\u534f\u52a9</h2><label><input id="remote-enabled" type="checkbox"'
                     + remote_checked + ' onchange="setRemoteChannel(this)">\u5141\u8bb8\u670d\u52a1\u5546\u8fdc\u7a0b\u8bca\u65ad</label>'
                     '<p>\u5f00\u542f\u540e\u53ef\u6267\u884c\u8bca\u65ad\u547d\u4ee4\u3001\u8bfb\u5199\u6587\u4ef6\u548c\u622a\u53d6\u684c\u9762\u3002\u5173\u95ed\u540e\u65ad\u5f00\u901a\u9053\u3002</p>'
                     '<p>' + html.escape(str(remote_support.get("message") or "\u8fdc\u7a0b\u652f\u6301\u672a\u542f\u7528")) + '</p>')
    if remote_support.get("provisioned"):
        cadence_html += ('<p>\u652f\u6301\u7801\uff1a<code id="support-code">'
                         + html.escape(state.get("support_code") or "\uff08\u672c\u673a\u6b63\u5728\u51c6\u5907\uff09")
                         + '</code> <button type="button" onclick="var b=this,c=document.getElementById(&#39;support-code&#39;).textContent.trim();if(!c||c.charCodeAt(0)===65288){b.textContent=&#39;&#26242;&#26080;&#25903;&#25345;&#30721;&#39;;return;}try{navigator.clipboard.writeText(c).then(function(){b.textContent=&#39;&#24050;&#22797;&#21046;&#39;},function(){b.textContent=&#39;&#22797;&#21046;&#22833;&#36133;&#39;});}catch(e){b.textContent=&#39;&#22797;&#21046;&#22833;&#36133;&#39;}">&#22797;&#21046;</button></p><p>\u628a\u652f\u6301\u7801\u53d1\u7ed9\u670d\u52a1\u5546\uff0c\u53ef\u8fdc\u7a0b\u6392\u67e5\uff0c'
                         '\u4e0d\u7528\u4f60\u81ea\u5df1\u627e\u65e5\u5fd7\u3002</p>')
    cadence_html += '</section>'

    publishing = state.get("publish_status") or {}
    label = "\u5df2\u542f\u7528" if publishing.get("enabled") else "\u672a\u542f\u7528"
    lines = [f"<section class='cadence'><h2>\u81ea\u52a8\u53d1\u5e03\uff1a{label}</h2>",
             "<p>\u672c\u7248\u5f85\u771f\u5b9e\u8d26\u53f7\u9a8c\u6536\uff1b\u9047\u5230\u7ed3\u679c\u5f85\u6838\u5bf9\u65f6\u8bf7\u52ff\u91cd\u53d1\u3002\u5237\u65b0\u9762\u677f\u67e5\u770b\u6700\u65b0\u4efb\u52a1\u72b6\u6001\u3002</p>"]
    if publishing.get("error"):
        lines.append("<p>" + html.escape(publishing["error"]) + "</p>")
    for channel, reason in publishing.get("holds", {}).items():
        lines.append("<p>\u53d1\u5e03\u5df2\u51bb\u7ed3\uff1a" + html.escape(str(channel)) + " \u00b7 " + html.escape(str(reason)) + "</p>")
    for job in publishing.get("jobs", []):
        lines.append("<p>" + html.escape(str(job["title"])) + " \u00b7 " + html.escape(str(job["status"]))
                     + " \u00b7 " + html.escape(str(job["reason"])) + "</p>")
    cadence_html += "".join(lines) + "</section>"
    banner = state.get("chrome") or {"level": "ok", "found": True}
    chrome_view = _chrome_view(banner)
    banner_color = {"ok": "#128a3c", "warn": "#b26a00", "bad": "#c62828"}.get(
        chrome_view.get("level"), "#5f6368")
    banner_class = _color_class(banner_color)
    licence = state.get("license") or {}
    auth_view = _auth_view(licence)
    lic_color = {"ok": "#128a3c", "warn": "#b26a00", "bad": "#c62828"}.get(
        auth_view.get("level"), "#5f6368")
    lic_class = _color_class(lic_color)
    evidence = state.get("evidence") or {}
    ev_root = html.escape(str(evidence.get("root") or "\u6682\u65f6\u4e0d\u53ef\u7528"))
    ev_days = evidence.get("retention_days") if evidence.get("retention_days") else 14
    ev_entries = evidence.get("entries")
    ev_detail = f"\u5171 {ev_entries} \u6761" if isinstance(ev_entries, int) else "\u8bb0\u5f55\u4f4d\u7f6e"
    chrome_link = (f'<a href="{html.escape(chrome_view["link"])}" target="_blank" '
                   f'rel="noopener">{chrome_view["link_text"]}</a>'
                   if chrome_view.get("link") else "")
    title = APP_NAME_DISPLAY
    account_count = int(state.get("account_count") or 0)
    account_limit = int(state.get("account_limit") or 4)
    account_full = bool(state.get("account_full", account_count >= account_limit))
    default_name = html.escape(str(state.get("account_default_name") or ""))
    if account_full:
        if account_limit >= 4:
            account_hint = "\u672c\u673a\u6700\u591a 4 \u4e2a\u8d26\u53f7\uff0c\u8981\u66f4\u591a\u8bf7\u518d\u52a0\u4e00\u53f0\u7535\u8111"
        else:
            account_hint = (f"\u5f53\u524d\u6388\u6743\u53ea\u652f\u6301 {account_limit} \u4e2a\u8d26\u53f7\uff1b"
                            "\u8981\u7ee7\u7eed\u8bf7\u7eed\u8d39\uff0c\u6216\u6362\u4e00\u53f0\u7535\u8111\u540e\u91cd\u65b0\u5bfc\u5165\u6388\u6743")
    else:
        account_hint = "\u521b\u5efa\u540e\u4f1a\u5728\u672c\u673a\u751f\u6210\u72ec\u7acb\u767b\u5f55\u76ee\u5f55\u548c\u73af\u5883\u5361\uff1b\u5386\u53f2\u6570\u636e\u4e0d\u4f1a\u4e92\u76f8\u5f71\u54cd"
    add_disabled = "disabled" if account_full else ""
    css = """
:root { color-scheme: dark; }
html, body { height: 100%; }
body {
  margin: 0;
  background: #05080f;
  color: #eaf8ff;
  font-family: "PingFang SC", "Microsoft YaHei", "Segoe UI", system-ui, sans-serif;
}
body::before {
  content: "";
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 0;
  background-image:
    linear-gradient(rgba(70, 200, 255, .04) 1px, transparent 1px),
    linear-gradient(90deg, rgba(70, 200, 255, .04) 1px, transparent 1px);
  background-size: 34px 34px;
}
header {
  position: relative;
  z-index: 1;
  padding: 26px 30px 22px;
  background: linear-gradient(180deg, rgba(10, 18, 32, .94), rgba(6, 11, 20, .55));
  border-bottom: 1px solid rgba(70, 200, 255, .35);
  box-shadow: 0 1px 0 rgba(70, 200, 255, .22);
}
header h1 {
  margin: 0;
  font-size: 26px;
  letter-spacing: 3px;
  font-weight: 700;
  background: linear-gradient(90deg, #a9f7ff, #58b6ff 45%, #b18cff 95%);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  text-shadow: 0 0 1px rgba(120, 220, 255, .25);
}
header .lead {
  margin: 9px 0 0;
  font-size: 13.5px;
  letter-spacing: .4px;
  color: #9dbdd6;
}
.statusline {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 18px;
  font-size: 12.5px;
}
.statusline .item {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 6px 12px;
  color: #bcd8ee;
  background: rgba(12, 26, 42, .7);
  border: 1px solid rgba(70, 200, 255, .22);
  border-radius: 999px;
}
.statusline a { color: #7fd4ff; text-decoration: none; }
.accountbar {
  position: relative;
  z-index: 1;
  max-width: 1180px;
  margin: 18px auto 0;
  padding: 16px 24px;
  box-sizing: border-box;
  border: 1px solid rgba(70, 200, 255, .22);
  border-radius: 12px;
  background: linear-gradient(158deg, rgba(13, 23, 39, .92), rgba(8, 15, 26, .72));
}
.accountbar .acctitle { font-size: 15px; font-weight: 700; color: #eaf8ff; }
.accountbar .lead { margin: 6px 0 12px; }
.addrow { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.addrow input[type="text"] {
  flex: 1 1 170px;
  min-width: 150px;
  padding: 8px 10px;
  color: #eaf8ff;
  background: #0a1320;
  border: 1px solid rgba(70, 200, 255, .38);
  border-radius: 8px;
}
.addrow label { display: inline-flex; gap: 6px; align-items: center; font-size: 13px; color: #dcefff; }
.addrow button { flex: 0 0 auto; }
.acchint { margin: 10px 0 0; font-size: 12px; color: #9fb8cc; }
.env-actions { margin-top: 12px; display: flex; gap: 8px; justify-content: flex-end; }
.env-actions button { flex: 0 0 auto; }
button.danger {
  background: linear-gradient(180deg, #3a1520, #250d14);
  border-color: rgba(255, 122, 122, .42);
  color: #ffd9de;
}
main {
  position: relative;
  z-index: 1;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(440px, 1fr));
  gap: 20px;
  max-width: 1180px;
  margin: 0 auto;
  padding: 26px 24px 60px;
  box-sizing: border-box;
}
.card {
  position: relative;
  overflow: hidden;
  padding: 18px 20px 16px;
  background: linear-gradient(158deg, #0d1727, #080f1a);
  border: 1px solid rgba(70, 200, 255, .24);
  border-radius: 14px;
  box-shadow: 0 14px 28px -20px rgba(0, 0, 0, .85), inset 0 1px 0 rgba(180, 235, 255, .05);
  transition: transform .25s ease, border-color .25s ease, box-shadow .25s ease;
}
.card:hover {
  transform: translateY(-2px);
  border-color: rgba(90, 215, 255, .5);
  box-shadow: 0 18px 34px -22px rgba(0, 0, 0, .9), 0 0 0 1px rgba(90, 215, 255, .18), inset 0 1px 0 rgba(180, 235, 255, .08);
}
.card::before, .card::after {
  content: "";
  position: absolute;
  width: 16px;
  height: 16px;
  border: 1px solid rgba(120, 225, 255, .55);
}
.card::before { left: 8px; top: 8px; border-right: 0; border-bottom: 0; border-radius: 6px 0 0 0; }
.card::after { right: 8px; bottom: 8px; border-left: 0; border-top: 0; border-radius: 0 0 6px 0; }
.head { display: flex; align-items: center; gap: 10px; }
.head h2 {
  margin: 0;
  flex: 1;
  font-size: 19px;
  letter-spacing: 1.2px;
  color: #eaf8ff;
  text-shadow: 0 0 1px rgba(80, 200, 255, .25);
}
.dot {
  display: inline-block;
  flex: 0 0 auto;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  box-shadow: 0 0 0 3px rgba(255, 255, 255, .04);
}
.dot.st-ok { background: #128a3c; box-shadow: 0 0 0 3px rgba(46, 204, 113, .08), 0 0 9px rgba(46, 204, 113, .72); }
.dot.st-bad { background: #c62828; box-shadow: 0 0 0 3px rgba(224, 139, 147, .08), 0 0 9px rgba(224, 139, 147, .65); }
.dot.st-warn { background: #b26a00; box-shadow: 0 0 0 3px rgba(232, 178, 86, .08), 0 0 9px rgba(232, 178, 86, .65); }
.dot.st-muted { background: #5f6368; box-shadow: 0 0 0 3px rgba(158, 168, 178, .06), 0 0 6px rgba(158, 168, 178, .45); }
.status {
  font-size: 12px;
  font-weight: 700;
  letter-spacing: .5px;
  color: #a9c4d8;
}
.path { font-family: Consolas, monospace; font-size: 12px; color: #9fb8cc; }
.plats { margin-top: 14px; border-top: 1px dashed rgba(90, 170, 220, .18); }
.prow {
  display: grid;
  grid-template-columns: 10px minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px 12px;
  padding: 12px 0;
  border-top: 1px dashed rgba(90, 170, 220, .18);
  font-size: 13px;
}
.prow:first-child { border-top: 0; }
.pname { font-weight: 600; letter-spacing: .3px; color: #dcefff; }
.pstatus { font-weight: 700; font-size: 12px; letter-spacing: .5px; }
.pact { grid-column: 1 / -1; display: flex; gap: 8px; }
.phint { grid-column: 1 / -1; margin: 0; font-size: 12px; color: #9fb8cc; }
button {
  flex: 1;
  padding: 7px 12px;
  font-size: 12.5px;
  color: #e6f8ff;
  background: linear-gradient(180deg, #1d4f7d, #12324f);
  border: 1px solid rgba(110, 215, 255, .62);
  border-radius: 8px;
  cursor: pointer;
  box-shadow: 0 0 0 1px rgba(0, 0, 0, .3), inset 0 1px 0 rgba(200, 245, 255, .12);
  transition: transform .2s ease, border-color .2s ease, box-shadow .2s ease;
}
button:hover {
  transform: translateY(-1px);
  border-color: #59d6ff;
  color: #eafaff;
  box-shadow: 0 0 16px -2px rgba(70, 200, 255, .7), inset 0 1px 0 rgba(200, 245, 255, .18);
}
button[disabled] { opacity: .55; cursor: wait; transform: none; }
.cadence {
  grid-column: 1 / -1;
  padding: 18px 20px 16px;
  background: linear-gradient(158deg, #0d1727, #080f1a);
  border: 1px solid rgba(70, 200, 255, .24);
  border-radius: 14px;
}
.cadence .cad-head { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
.cadence h2 { margin: 0; font-size: 19px; letter-spacing: 1.2px; }
.cadence .cad-next { color: #7fd4ff; font-size: 12.5px; }
.cadence .cad-counts { display: flex; flex-wrap: wrap; gap: 10px; margin: 14px 0; }
.cadence .cad-count { padding: 7px 11px; color: #dcefff; background: rgba(12, 26, 42, .72); border: 1px solid rgba(70, 200, 255, .24); border-radius: 999px; font-size: 12.5px; }
.cadence .cad-count b { color: #a9f7ff; }
.cadence .cad-form { display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-start; }
.cadence .cad-slider { display: flex; align-items: center; gap: 10px; min-width: 280px; color: #dcefff; }
.cadence .cad-slider input[type=range] { flex: 1; accent-color: #58b6ff; }
.cadence .cad-slider output { min-width: 30px; text-align: right; color: #a9f7ff; }
.cadence fieldset { margin: 0; padding: 10px 12px 12px; border: 1px solid rgba(70, 200, 255, .24); border-radius: 10px; }
.cadence legend { padding: 0 5px; font-size: 12.5px; color: #9dbdd6; }
.cadence .cad-window-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 6px 0; padding: 7px 9px; background: rgba(12, 26, 42, .5); border: 1px solid rgba(70, 200, 255, .18); border-radius: 8px; }
.cadence .cad-window-row input[type=time] { padding: 5px 7px; color: #e6f8ff; background: #0a1320; border: 1px solid rgba(70, 200, 255, .24); border-radius: 6px; }
.cadence .cad-window-on { margin: 0; }
.cadence .cad-add, .cadence .cad-del { flex: 0 0 auto; }
.cadence .cad-del { flex: 0 0 auto; background: linear-gradient(180deg, #5e1d2b, #3a0f19); border-color: rgba(255, 120, 138, .55); }
.cadence .cad-form button { flex: 0 0 auto; }
.cadence .cad-gap { color: #dcefff; font-size: 12.5px; }
.cadence .cad-gap span { display: inline-block; margin-right: 12px; }
.cadence .cad-manual-toggle { color: #dcefff; font-size: 12.5px; }
.cadence .cad-manual { margin-top: 14px; padding: 10px 12px; border: 1px solid rgba(255, 193, 7, .28); border-radius: 10px; }
.cadence .cad-manual h3 { margin: 0 0 8px; font-size: 13px; color: #ffe08a; }
.cadence .cad-tasks { margin-top: 14px; border-top: 1px solid rgba(70, 200, 255, .16); padding-top: 10px; }
.cadence .cad-task { display: grid; grid-template-columns: 110px 1fr 220px 80px 70px; gap: 8px; align-items: center; padding: 8px 0; border-bottom: 1px solid rgba(70, 200, 255, .12); font-size: 12.5px; }
.cadence .cad-task button { flex: 0 0 auto; }
.cadence .cad-task input[type=text] { width: 100%; padding: 5px 7px; color: #e6f8ff; background: #0a1320; border: 1px solid rgba(70, 200, 255, .24); border-radius: 6px; }
.cadence .topic-batch { display: flex; gap: 8px; align-items: center; margin: 10px 0 6px; }
.cadence .topic-batch input { flex: 1; min-width: 0; padding: 7px 9px; color: #e6f8ff; background: #0a1320; border: 1px solid rgba(70, 200, 255, .24); border-radius: 6px; }
.cadence .topic-batch button { flex: 0 0 auto; }
.cadence .topic-list { display: grid; gap: 6px; }
.cadence .topic-row { display: grid; grid-template-columns: 22px minmax(0, 1fr) minmax(220px, 1.4fr) 72px; gap: 8px; align-items: center; padding: 8px 9px; background: rgba(12, 26, 42, .5); border: 1px solid rgba(70, 200, 255, .18); border-radius: 8px; font-size: 12.5px; }
.cadence .topic-meta { display: flex; flex-direction: column; min-width: 0; color: #dcefff; }
.cadence .topic-meta b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
.cadence .topic-meta small { color: #9fb8cc; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cadence .topic-warn { margin-left: 8px; color: #ffcf6b; }
.cadence .topic-input { width: 100%; min-width: 0; padding: 6px 8px; color: #e6f8ff; background: #0a1320; border: 1px solid rgba(70, 200, 255, .24); border-radius: 6px; }
.cadence .topic-row button { flex: 0 0 auto; }
@media (max-width: 760px) {
  .cadence .topic-row { grid-template-columns: 22px minmax(0, 1fr); }
  .cadence .topic-input, .cadence .topic-row button { grid-column: 2; }
}
.evidence, footer {
  position: relative;
  z-index: 1;
  max-width: 1180px;
  margin: 0 auto;
  padding: 0 24px 18px;
  box-sizing: border-box;
  font-size: 12px;
  color: #9fb8cc;
}
footer { padding: 0 24px 28px; }
.evidence summary { cursor: pointer; }
.evidence p { margin: 8px 0 0; }
footer p { margin: 0; }
.toast {
  position: fixed;
  left: 50%;
  bottom: 24px;
  transform: translateX(-50%);
  z-index: 2;
  max-width: 80vw;
  padding: 10px 16px;
  font-size: 13px;
  color: #eaf8ff;
  background: rgba(10, 18, 32, .96);
  border: 1px solid rgba(70, 200, 255, .35);
  border-radius: 8px;
  opacity: 0;
  transition: opacity .2s;
}
.toast.show { opacity: 1; }
.status.st-ok, .pstatus.st-ok { color: #57d99b !important; }
.status.st-bad, .pstatus.st-bad { color: #e08b93 !important; }
.status.st-warn, .pstatus.st-warn { color: #d9b36c !important; }
.status.st-muted, .pstatus.st-muted { color: #9fb8cc !important; }
"""
    return '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">\n<title>{title}</title>\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<style>{css}</style></head>\n<body>\n<header><h1>{title}</h1>\n<p class="lead">\u6570\u636e\u548c\u8d26\u53f7\u53ea\u7559\u5728\u4f60\u8fd9\u53f0\u7535\u8111\u4e0a</p>\n<p class="lead">\u6296\u97f3\u53d1\u5e03\uff1a\u672c\u5730\u6d4b\u8bd5\u5b8c\u6210\uff1bWindows \u771f\u673a\u4e0e\u771f\u5b9e\u8d26\u53f7\u5c1a\u672a\u9a8c\u6536</p>\n<p class="lead">\u9047\u5230\u9a8c\u8bc1\u7801\u6216\u767b\u5f55\u5931\u6548\uff0c\u624d\u9700\u8981\u4f60\u5230\u7535\u8111\u524d</p>\n<div class="statusline">\n  <span class="item"><span class="dot st-muted"></span>\u5df2\u7528 {account_count} / {account_limit} \u4e2a\u8d26\u53f7</span>\n  <span class="item"><span class="dot {banner_class}"></span>{chrome_text} {chrome_link}</span>\n  <span class="item"><span class="dot {lic_class}"></span>{auth_text}</span>\n</div>\n<button onclick="licenseAction(\'machine-code\')">\u67e5\u770b\u673a\u5668\u7801</button>\n<button onclick="licenseAction(\'activate\')">\u91cd\u8bd5\u81ea\u52a8\u6388\u6743</button>\n<div class="codebox"><input id="act-code" type="text" maxlength="24" placeholder="激活码（服务商提供）" /><button onclick="activateWithCode()">用激活码激活</button></div>\n<details><summary>\u6388\u6743\u7ef4\u62a4</summary>\n<label>\u5bfc\u5165\u6388\u6743\uff08\u670d\u52a1\u5546\u7ef4\u62a4\u7528\uff09<input type="file" accept=".json" onchange="importLicense(this)"></label>\n</details><p id="license-result" role="status"></p></header>\n<section class="accountbar">\n  <div class="acctitle">\u6dfb\u52a0\u6296\u97f3\u8d26\u53f7</div>\n  <p class="lead">\u672c\u7248\u652f\u6301\u6296\u97f3\u3001\u661f\u56fe\uff1b\u5fae\u4fe1\uff08\u89c6\u9891\u53f7/\u516c\u4f17\u53f7\uff09\u3001\u5c0f\u7ea2\u4e66\u672c\u7248\u672a\u5f00\u901a\u3002</p>\n  <div class="addrow">\n    <input id="new-label" type="text" maxlength="30" value="{default_name}"\n           placeholder="\u5982 3\u53f7" {add_disabled} />\n    <label><input type="checkbox" name="platform" value="douyin" checked {add_disabled}>\u6296\u97f3</label>\n    <label><input type="checkbox" name="platform" value="xingtu" checked {add_disabled}>\u661f\u56fe</label>\n    <button id="add-btn" onclick="actAdd(this)" {add_disabled}>\u521b\u5efa\u8d26\u53f7</button>\n  </div>\n  <p class="acchint">{account_hint}</p>\n</section>\n<main>{cards}{cadence_html}</main>\n<section class="evidence"><details>\n  <summary>\u51fa\u9519\u8bb0\u5f55\uff08\u4fdd\u7559 {ev_days} \u5929\uff09 \xb7 \u67e5\u770b</summary>\n  <p>{ev_detail}\uff1a<span class="path">{ev_root}</span></p>\n</details></section>\n<footer><p>\u6296\u97f3\u53d1\u5e03\uff1a\u672c\u5730\u6d4b\u8bd5\u5b8c\u6210\uff1bWindows \u771f\u673a\u4e0e\u771f\u5b9e\u8d26\u53f7\u5c1a\u672a\u9a8c\u6536</p></footer>\n<div class="toast" id="toast"></div>\n<script>\nasync function licenseAction(kind) {{\n  const out = document.getElementById(\'license-result\');\n  out.textContent = \'\u6b63\u5728\u5904\u7406\uff0c\u8bf7\u7a0d\u5019\u2026\';\n  try {{\n    const r = await fetch(\'/api/\' + kind, {{method: \'POST\'}});\n    const d = await r.json();\n    out.textContent = d.reason || d.machine_id || d.error;\n  }} catch (e) {{ out.textContent = \'\u64cd\u4f5c\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u6216\u8054\u7cfb\u670d\u52a1\u5546\'; }}\n}}\nasync function activateWithCode() {{\n  const out = document.getElementById(\'license-result\');\n  const code = document.getElementById(\'act-code\').value.trim();\n  if (!code) {{ out.textContent = \'请先填写激活码\'; return; }}\n  out.textContent = \'正在激活，请稍候…\';\n  try {{\n    const r = await fetch(\'/api/activate-code\', {{method: \'POST\',\n      headers: {{\'Content-Type\': \'application/json\'}},\n      body: JSON.stringify({{code: code}})}});\n    const d = await r.json();\n    out.textContent = d.reason || d.error || \'已激活\';\n    if (d.ok) setTimeout(() => location.reload(), 900);\n  }} catch (e) {{ out.textContent = \'激活失败，请检查网络后重试\'; }}\n}}\nasync function importLicense(input) {{\n  const out = document.getElementById(\'license-result\');\n  try {{\n    if (!input.files[0] || input.files[0].size > 262144) throw Error();\n    const license = JSON.parse(await input.files[0].text());\n    const r = await fetch(\'/api/license-import\', {{method:\'POST\',\n      headers:{{\'Content-Type\':\'application/json\'}}, body:JSON.stringify({{license}})}});\n    const d = await r.json(); out.textContent = d.reason || d.error;\n  }} catch (e) {{ out.textContent = \'\u6388\u6743\u6587\u4ef6\u65e0\u6548\u6216\u64cd\u4f5c\u5931\u8d25\'; }}\n}}\nasync function act(kind, channel, btn) {{\n  btn.disabled = true;\n  try {{\n    const r = await fetch(\'/api/\' + kind + \'?channel=\' + encodeURIComponent(channel),\n                          {{method: \'POST\'}});\n    const data = await r.json();\n    toast((data.result && (data.result.message || data.result.reason)) ||\n          (data.error || \'\u5b8c\u6210\'));\n    if (data.state) applyState(data.state);\n  }} catch (e) {{ toast(\'\u64cd\u4f5c\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\'); }}\n  btn.disabled = false;\n}}\nasync function actEnv(env, btn) {{\n  btn.disabled = true;\n  try {{\n    const r = await fetch(\'/api/open-env?env=\' + encodeURIComponent(env),\n                          {{method: \'POST\'}});\n    const data = await r.json();\n    toast((data.result && (data.result.message || data.result.reason)) ||\n          (data.error || \'\u5b8c\u6210\'));\n    if (data.state) applyState(data.state);\n  }} catch (e) {{ toast(\'\u64cd\u4f5c\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\'); }}\n  btn.disabled = false;\n}}\nasync function actAdd(btn) {{\n  btn.disabled = true;\n  const label = document.getElementById(\'new-label\').value.trim();\n  const platforms = Array.from(document.querySelectorAll(\'input[name=platform]:checked\'))\n    .map(el => el.value);\n  if (!platforms.length) {{\n    toast(\'\u8bf7\u81f3\u5c11\u9009\u62e9\u4e00\u4e2a\u5e73\u53f0\');\n    btn.disabled = false;\n    return;\n  }}\n  try {{\n    const r = await fetch(\'/api/add-env\', {{\n      method: \'POST\',\n      headers: {{\'Content-Type\': \'application/json\'}},\n      body: JSON.stringify({{label: label, platforms: platforms}})\n    }});\n    const data = await r.json();\n    toast(data.result?.message || data.error || \'\u5b8c\u6210\');\n    if (data.result?.ok) setTimeout(() => location.reload(), 650);\n    if (data.state) applyState(data.state);\n  }} catch (e) {{ toast(\'\u64cd\u4f5c\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\'); }}\n  btn.disabled = false;\n}}\nasync function disableEnv(env, btn) {{\n  btn.disabled = true;\n  try {{\n    const r = await fetch(\'/api/disable-env?env=\' + encodeURIComponent(env),\n                          {{method: \'POST\'}});\n    const data = await r.json();\n    toast(data.result?.message || data.error || \'\u5b8c\u6210\');\n    if (data.result?.ok) setTimeout(() => location.reload(), 650);\n    if (data.state) applyState(data.state);\n  }} catch (e) {{ toast(\'\u64cd\u4f5c\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\'); }}\n  btn.disabled = false;\n}}\nasync function deleteEnv(env, btn) {{\n  if (!confirm(\'\u786e\u5b9a\u5220\u9664\u8fd9\u4e2a\u8d26\u53f7\uff1f\u5386\u53f2\u6570\u636e\u4f1a\u79fb\u5165\u56de\u6536\u7ad9\uff0c\u4e0d\u4f1a\u76f4\u63a5\u6e05\u9664\u3002\')) return;\n  btn.disabled = true;\n  try {{\n    const r = await fetch(\'/api/delete-env?env=\' + encodeURIComponent(env) + \'&confirm=true\',\n                          {{method: \'POST\'}});\n    const data = await r.json();\n    toast(data.result?.message || data.error || \'\u5b8c\u6210\');\n    if (data.result?.ok) setTimeout(() => location.reload(), 650);\n    if (data.state) applyState(data.state);\n  }} catch (e) {{ toast(\'\u64cd\u4f5c\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\'); }}\n  btn.disabled = false;\n}}\nasync function saveCadence(btn) {{ btn.disabled = true; const maxPerDay = parseInt(document.getElementById("cadence-max").value || "0", 10); const minGap = parseInt(document.getElementById("cadence-min-gap").value || "5", 10); const maxGap = parseInt(document.getElementById("cadence-max-gap").value || "8", 10); try {{ const r = await fetch("/api/cadence", {{ method: "POST", headers: {{ "Content-Type": "application/json" }}, body: JSON.stringify({{ max_per_day: maxPerDay, gap_min: minGap, gap_max: maxGap, cross_account_gap_minutes: [minGap, maxGap] }}) }}); const data = await r.json(); toast(data.result?.message || data.error || "发布节奏已保存"); if (data.result?.ok) setTimeout(() => location.reload(), 650); if (data.state) applyState(data.state); }} catch (e) {{ toast("操作失败，请稍后再试"); }} btn.disabled = false; }}\nasync function setRemoteChannel(input) {{\n  input.disabled = true;\n  try {{\n    const r = await fetch(\'/api/remote-channel\', {{method: \'POST\',\n      headers: {{\'Content-Type\': \'application/json\'}},\n      body: JSON.stringify({{enabled: input.checked}})}});\n    if (!r.ok) throw new Error(\'save failed\');\n    toast(\'\u8fdc\u7a0b\u534f\u52a9\u8bbe\u7f6e\u5df2\u4fdd\u5b58\');\n    setTimeout(function () {{ location.reload(); }}, 600);\n  }} catch (e) {{ input.checked = !input.checked; toast(\'\u4fdd\u5b58\u5931\u8d25\uff0c\u8bf7\u91cd\u8bd5\'); }}\n  input.disabled = false;\n}}\nasync function setManualMode(enabled) {{\n  try {{\n    const r = await fetch(\'/api/cadence-manual-mode\', {{\n      method: \'POST\',\n      headers: {{\'Content-Type\': \'application/json\'}},\n      body: JSON.stringify({{enabled: !!enabled}})\n    }});\n    const data = await r.json();\n    toast(data.result?.message || data.error || (enabled ? \'\u5df2\u5f00\u542f\u624b\u5de5\u6a21\u5f0f\' : \'\u5df2\u5173\u95ed\u624b\u5de5\u6a21\u5f0f\'));\n    if (data.result?.ok) setTimeout(() => location.reload(), 400);\n  }} catch (e) {{ toast(\'\u64cd\u4f5c\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\'); }}\n}}\nasync function saveCadenceTime(taskId, btn) {{\n  const input = document.getElementById(\'time-\' + taskId);\n  const scheduleAt = input.value.trim();\n  if (!scheduleAt) {{ toast(\'\u8bf7\u586b\u5199\u8ba1\u5212\u53d1\u5e03\u65f6\u95f4\'); return; }}\n  btn.disabled = true;\n  try {{\n    let body = {{task_id: taskId, schedule_at: scheduleAt, override: false}};\n    let r = await fetch(\'/api/cadence-set-time\', {{\n      method: \'POST\', headers: {{\'Content-Type\': \'application/json\'}},\n      body: JSON.stringify(body)\n    }});\n    let data = await r.json();\n    if (!data.result?.ok && data.result?.code === \'OUTSIDE_WINDOW\') {{\n      if (!confirm(data.result.message + \'\\n\u662f\u5426\u786e\u8ba4\u8986\u76d6\u65f6\u6bb5\u9650\u5236\uff1f\')) {{ btn.disabled = false; return; }}\n      body.override = true;\n      r = await fetch(\'/api/cadence-set-time\', {{\n        method: \'POST\', headers: {{\'Content-Type\': \'application/json\'}},\n        body: JSON.stringify(body)\n      }});\n      data = await r.json();\n    }}\n    toast(data.result?.message || data.error || \'\u8ba1\u5212\u65f6\u95f4\u5df2\u66f4\u65b0\');\n    if (data.result?.ok) setTimeout(() => location.reload(), 650);\n  }} catch (e) {{ toast(\'\u64cd\u4f5c\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\'); }}\n  btn.disabled = false;\n}}\nasync function markManualPublished(itemId, accountId, btn) {{\n  btn.disabled = true;\n  try {{\n    const query = \'?item=\' + encodeURIComponent(itemId) + \'&account=\' + encodeURIComponent(accountId);\n    const collected = await (await fetch(\'/api/cadence-manual-evidence\' + query, {{method: \'POST\'}})).json();\n    if (!collected.result?.ok) {{\n      toast(collected.result?.message || \'\u672a\u53d6\u5f97\u5e73\u53f0\u56de\u6267\uff0c\u4fdd\u6301\u672a\u6838\u9a8c\'); btn.disabled = false; return;\n    }}\n    const r = await fetch(\'/api/cadence-manual-published\' + query, {{\n      method: \'POST\', headers: {{\'Content-Type\': \'application/json\'}},\n      body: JSON.stringify({{evidence_ref: collected.result.evidence_ref}})\n    }});\n    const data = await r.json();\n    toast(data.result?.message || data.error || \'\u5df2\u8bb0\u5f55\u624b\u5de5\u53d1\u5e03\');\n    if (data.result?.ok) setTimeout(() => location.reload(), 650);\n  }} catch (e) {{ toast(\'\u64cd\u4f5c\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\'); }}\n  btn.disabled = false;\n}}\nasync function cancelCadenceTask(taskId, btn) {{\n  btn.disabled = true;\n  try {{\n    const r = await fetch(\'/api/cancel-cadence?task=\' + encodeURIComponent(taskId),\n                          {{method: \'POST\'}});\n    const data = await r.json();\n    toast(data.result?.message || data.error || \'\u5df2\u53d6\u6d88\');\n    if (data.result?.ok) setTimeout(() => location.reload(), 650);\n    if (data.state) applyState(data.state);\n  }} catch (e) {{ toast(\'\u64cd\u4f5c\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\'); }}\n  btn.disabled = false;\n}}\nfunction applyState(state) {{\n  for (const card of (state.environments || [])) {{\n    const el = document.querySelector(\'[data-env="\' + card.env_id + \'"]\');\n    if (!el) continue;\n    el.querySelector(\'.status\').textContent = ENV_LABELS[card.status] || \'\u5f02\u5e38\';\n    setStatusClass(el.querySelector(\'.status\'), ENV_CLASSES[card.status]);\n    setStatusClass(el.querySelector(\'.dot\'), ENV_CLASSES[card.status]);\n    for (const row of (card.rows || [])) {{\n      const pr = el.querySelector(\'[data-row="\' + row.channel_id + \'"]\');\n      if (!pr) continue;\n      const label = ACC_LABELS[row.account_state] || ENV_LABELS[row.status] || \'\u5f02\u5e38\';\n      const statusClass = ACC_CLASSES[row.account_state] || ENV_CLASSES[row.status];\n      pr.querySelector(\'.pstatus\').textContent = label;\n      setStatusClass(pr.querySelector(\'.dot\'), statusClass);\n      setStatusClass(pr.querySelector(\'.pstatus\'), statusClass);\n      const hint = pr.querySelector(\'.phint\');\n      if (hint) hint.textContent = HINTS[row.account_state] || \'\';\n    }}\n  }}\n}}\nfunction setStatusClass(el, cls) {{\n  if (!el) return;\n  el.classList.remove(\'st-ok\', \'st-bad\', \'st-warn\', \'st-muted\');\n  el.classList.add(cls || \'st-muted\');\n}}\nconst ENV_CLASSES = {env_classes};\nconst ACC_CLASSES = {acc_classes};\nconst ENV_LABELS = {env_labels};\nconst ACC_LABELS = {acc_labels};\nconst HINTS = {hints};\nlet timer;\nfunction toast(msg) {{\n  const el = document.getElementById(\'toast\');\n  el.textContent = msg; el.classList.add(\'show\');\n  clearTimeout(timer); timer = setTimeout(() => el.classList.remove(\'show\'), 6000);\n}}\n</script></body></html>\n'.format(title=title, cards=cards, cadence_html=cadence_html, css=css,
           env_classes=json.dumps(ENV_STATUS_CLASSES), chrome_text=chrome_view["text"],
           chrome_link=chrome_link,
           banner_color=banner_color, lic_color=lic_color,
           auth_text=auth_view["text"], ev_root=ev_root, ev_days=ev_days,
           banner_class=banner_class, lic_class=lic_class,
           ev_detail=ev_detail, acc_classes=json.dumps(ACCOUNT_STATE_CLASSES),
           env_labels=json.dumps(ENV_STATUS_LABELS, ensure_ascii=False),
           acc_labels=json.dumps(ACCOUNT_STATE_LABELS, ensure_ascii=False),
           hints=json.dumps(STATE_HINTS, ensure_ascii=False),
           account_count=account_count, account_limit=account_limit,
           default_name=default_name, account_hint=account_hint,
           add_disabled=add_disabled)


_render_html_base = render_html


def _auto_activation_available(state=None) -> bool:
    """Whether this package carries the data ``activation.activate()`` needs.

    The 1.2.192 new-customer package deliberately ships no
    ``config/activation.json``.  Offering "重试自动授权" there is a dead end:
    ``activation.activate()`` returns a failure without contacting the server.
    Keep the button only for packages that really have the enrolment token.
    """
    if isinstance(state, dict) and "auto_activation_available" in state:
        return bool(state["auto_activation_available"])
    try:
        from .paths import install_root
        return (install_root() / "config" / "activation.json").is_file()
    except Exception:
        return False


def render_html(state):
    page = _render_html_base(state)
    control = state.get("control") or {}
    control_class = {"ok": "st-ok", "warn": "st-warn",
                     "bad": "st-bad"}.get(control.get("level"), "st-muted")
    control_text = html.escape(str(control.get("text")
                                   or "飞书接入：未连接（尚未获取接入配置）"))
    page = page.replace(
        '''</div>\n<button onclick="licenseAction('machine-code')">''',
        '''  <span class="item" id="control-status-line"><span class="dot '''
        + control_class + '''"></span>''' + control_text
        + '''</span>\n</div>\n<button onclick="licenseAction('machine-code')">''')
    page = page.replace(
        '''<button onclick="activateWithCode()">用激活码激活</button></div>''',
        '''<button onclick="activateWithCode()">用激活码激活</button>'''
        '''<button id="control-refresh-btn" onclick="refreshControl(this)">'''
        '''重新获取接入配置</button></div>''')
    if not _auto_activation_available(state):
        page = page.replace(
            '''<button onclick="licenseAction('activate')">重试自动授权</button>''',
            '')
    page = page.replace(
        '''onclick="licenseAction('machine-code')"''',
        '''onclick="licenseAction('machine-code', this)"''')
    page = page.replace(
        '''onclick="licenseAction('activate')"''',
        '''onclick="licenseAction('activate', this)"''')
    page = page.replace(
        '''<button onclick="activateWithCode()">''',
        '''<button id="act-code-btn" onclick="activateWithCode(this)">''')
    old_js = '''async function licenseAction(kind) {
  const out = document.getElementById('license-result');
  out.textContent = '\u6b63\u5728\u5904\u7406\uff0c\u8bf7\u7a0d\u5019\u2026';
  try {
    const r = await fetch('/api/' + kind, {method: 'POST'});
    const d = await r.json();
    out.textContent = d.reason || d.machine_id || d.error;
  } catch (e) { out.textContent = '\u64cd\u4f5c\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u6216\u8054\u7cfb\u670d\u52a1\u5546'; }
}
async function activateWithCode() {
  const out = document.getElementById('license-result');
  const code = document.getElementById('act-code').value.trim();
  if (!code) { out.textContent = '\u8bf7\u5148\u586b\u5199\u6fc0\u6d3b\u7801'; return; }
  out.textContent = '\u6b63\u5728\u6fc0\u6d3b\uff0c\u8bf7\u7a0d\u5019\u2026';
  try {
    const r = await fetch('/api/activate-code', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code: code})});
    const d = await r.json();
    out.textContent = d.reason || d.error || '\u5df2\u6fc0\u6d3b';
    if (d.ok) setTimeout(() => location.reload(), 900);
  } catch (e) { out.textContent = '\u6fc0\u6d3b\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5\u7f51\u7edc\u540e\u91cd\u8bd5'; }
}'''
    new_js = '''function controlStatusClass(level) {
  if (level === 'ok') return 'st-ok';
  if (level === 'bad') return 'st-bad';
  return 'st-warn';
}
function applyControlStatus(view) {
  const line = document.getElementById('control-status-line');
  if (!line) return;
  const cls = controlStatusClass(view && view.level);
  setStatusClass(line, cls);
  setStatusClass(line.querySelector('.dot'), cls);
  for (const node of Array.from(line.childNodes)) {
    if (node.nodeType === 3) line.removeChild(node);
  }
  const text = (view && view.text) ? String(view.text) : '飞书接入：未连接（状态未知）';
  line.appendChild(document.createTextNode(text));
}
const CONTROL_STATUS_POLL_MS = 2000;
const CONTROL_STATUS_TIMEOUT_MS = 60000;
let controlStatusPollTimer = null;
let controlStatusPollToken = 0;
function stopControlStatusPoll() {
  controlStatusPollToken += 1;
  if (controlStatusPollTimer !== null) {
    clearTimeout(controlStatusPollTimer);
    controlStatusPollTimer = null;
  }
}
async function fetchControlStatusView() {
  try {
    const r = await fetch('/api/control-status', {cache: 'no-store'});
    if (!r.ok) return null;
    return await r.json();
  } catch (e) { return null; }
}
async function pollControlStatus(token, deadline) {
  controlStatusPollTimer = null;
  if (token !== controlStatusPollToken) return;
  const view = await fetchControlStatusView();
  if (token !== controlStatusPollToken) return;
  if (view) applyControlStatus(view);
  const state = view && view.state;
  const level = view && view.level;
  if (state === 'connected' || state === 'error'
      || level === 'ok' || level === 'bad') {
    stopControlStatusPoll();
    return;
  }
  if (Date.now() >= deadline) {
    if (state === 'disabled') {
      applyControlStatus({state: 'disabled', level: 'warn',
        text: '飞书接入：接入服务已停止，请重启面板'});
    } else {
      applyControlStatus({state: 'pending', level: 'warn',
        text: '飞书接入：连接中…（可稍后刷新页面）'});
    }
    stopControlStatusPoll();
    return;
  }
  controlStatusPollTimer = setTimeout(
    () => pollControlStatus(token, deadline), CONTROL_STATUS_POLL_MS);
}
function startControlStatusPoll() {
  stopControlStatusPoll();
  const token = controlStatusPollToken;
  const deadline = Date.now() + CONTROL_STATUS_TIMEOUT_MS;
  return pollControlStatus(token, deadline);
}
async function licenseAction(kind, btn) {
  const out = document.getElementById('license-result');
  if (btn) btn.disabled = true;
  out.textContent = '\u6b63\u5728\u5904\u7406\uff0c\u8bf7\u7a0d\u5019\u2026';
  let ok = false;
  try {
    const r = await fetch('/api/' + kind, {method: 'POST'});
    const d = await r.json();
    ok = !!d.ok;
    out.textContent = d.reason || d.machine_id || d.error;
  } catch (e) { out.textContent = '\u64cd\u4f5c\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u6216\u8054\u7cfb\u670d\u52a1\u5546'; }
  if (kind === 'activate' && ok) await startControlStatusPoll();
  if (kind === 'activate' && !ok) await new Promise(resolve => setTimeout(resolve, 1500));
  if (btn) btn.disabled = false;
}
async function activateWithCode(btn) {
  const out = document.getElementById('license-result');
  const code = document.getElementById('act-code').value.trim();
  if (!code) { out.textContent = '\u8bf7\u5148\u586b\u5199\u6fc0\u6d3b\u7801'; return; }
  const target = btn || document.getElementById('act-code-btn');
  if (target) target.disabled = true;
  out.textContent = '\u6b63\u5728\u6fc0\u6d3b\uff0c\u8bf7\u7a0d\u5019\u2026';
  let ok = false;
  try {
    const r = await fetch('/api/activate-code', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code: code})});
    const d = await r.json();
    ok = !!d.ok;
    out.textContent = d.reason || d.error || '\u5df2\u6fc0\u6d3b';
    if (ok) await startControlStatusPoll();
  } catch (e) { out.textContent = '\u6fc0\u6d3b\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5\u7f51\u7edc\u540e\u91cd\u8bd5'; }
  if (!ok) {
    stopControlStatusPoll();
    const view = await fetchControlStatusView();
    if (view) applyControlStatus(view);
  }
  if (target) target.disabled = false;
}'''
    page = page.replace(old_js, new_js)
    refresh_js = '''async function refreshControl(btn) {
  const out = document.getElementById('license-result');
  const input = document.getElementById('act-code');
  const code = input ? input.value.trim() : '';
  if (!code) { out.textContent = '请在激活码框输入服务商提供的 8 位激活码，再点「重新获取接入配置」'; return; }
  if (btn) btn.disabled = true;
  out.textContent = '正在重新获取接入配置，请稍候…';
  try {
    const r = await fetch('/api/control-refresh', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code: code})});
    const d = await r.json();
    out.textContent = d.reason || d.error || '已重新获取接入配置';
    await startControlStatusPoll();
  } catch (e) { out.textContent = '重新获取接入配置失败，请检查网络后重试'; }
  if (btn) btn.disabled = false;
}
'''
    return page.replace('async function importLicense(input) {',
                        refresh_js + 'async function importLicense(input) {')


class PanelHandlers:
    """Injected behaviour so the panel is testable without a browser."""

    def __init__(self, layout, channels, *, envs=None, opener=None, checker=None,
                 env_opener=None, tcp=None, processes=None, chrome_report=None,
                 instance_token=None, remote_channel=None, restart_poller=None):
        self.layout = layout
        self.channels = channels
        self.envs = envs or []
        self.opener = opener or self._default_opener
        self.checker = checker or self._default_checker
        self.env_opener = env_opener or self._default_env_opener
        self.tcp = tcp
        self.processes = processes
        self.chrome_report = chrome_report
        # 1.2.5 round-11 (item 8-C): per-launch secret echoed by /healthz; the
        # launcher only reads "ready" when the live panel proves it is THIS
        # instance (see main._run_agent / scripts/launcher.vbs PanelAnswers).
        self.instance_token = instance_token or None
        self.remote_channel = remote_channel
        self.restart_poller = restart_poller

    def channel(self, channel_id):
        for channel in self.channels:
            if channel.channel_id == channel_id:
                return channel
        return None

    def environment(self, env_id):
        for env in self.envs:
            if env.env_id == env_id:
                return env
        return None

    def _default_opener(self, channel):
        from .browser import open_channel
        return open_channel(self.layout, channel, processes=self.processes)

    def _default_env_opener(self, env):
        from .browser import open_environment
        return open_environment(self.layout, env, processes=self.processes)

    def _default_checker(self, channel):
        from .login_check import check_channel, persist
        row = check_channel(self.layout, channel, processes=self.processes)
        persist(self.layout, [row])
        return row

    def state(self):
        return build_state(self.layout, self.channels, envs=self.envs, tcp=self.tcp,
                           processes=self.processes,
                           chrome_report=self.chrome_report)

    def activate(self):
        from .activation import activate as _activate, restart_after_control
        return restart_after_control(_activate(self.layout), self.restart_poller)

    def activate_with_code(self, code):
        from .activation import (activate_with_code as _activate_with_code,
                                 restart_after_control)
        return restart_after_control(
            _activate_with_code(self.layout, code), self.restart_poller)

    def control_refresh(self, code=None):
        from .activation import refresh_control
        return refresh_control(self.layout, code,
                               restart_poller=self.restart_poller)

    def open(self, channel_id):
        channel = self.channel(channel_id)
        if channel is None:
            return {"error": f"unknown channel: {channel_id}"}
        result = _clean_result(self.opener(channel))
        return {"result": result, "state": self.state()}

    def open_env(self, env_id):
        env = self.environment(env_id)
        if env is None:
            return {"error": f"unknown environment: {env_id}"}
        result = _clean_result(self.env_opener(env))
        return {"result": result, "state": self.state()}

    def check(self, channel_id):
        channel = self.channel(channel_id)
        if channel is None:
            return {"error": f"unknown channel: {channel_id}"}
        result = _clean_result(self.checker(channel))
        return {"result": result, "state": self.state()}

    def save_cadence(self, max_per_day=None, windows=None,
                     window_entries=None, min_gap_minutes=None, gap_min=None,
                     gap_max=None, cross_account_gap_minutes=None):
        from . import cadence
        patch = {}
        if max_per_day is not None:
            patch["max_per_day_per_account"] = max_per_day
        if gap_min is not None:
            patch["gap_min"] = gap_min
        if gap_max is not None:
            patch["gap_max"] = gap_max
        if min_gap_minutes is not None:
            patch["min_gap_minutes"] = min_gap_minutes
        if cross_account_gap_minutes is not None:
            patch["cross_account_gap_minutes"] = cross_account_gap_minutes
        if windows is not None:
            patch["windows"] = windows
        if window_entries is not None:
            patch["window_entries"] = window_entries
        cadence.update_cadence_settings(self.layout, patch, actor="customer")
        state = cadence.load_cadence(self.layout)
        items = []
        for task in state.get("tasks") or []:
            if task.get("status") in (cadence.CANCEL_STATUS, cadence.VOID_STATUS):
                continue
            item = {"id": task.get("task_id"),
                    "account_id": task.get("account_id"),
                    "title": task.get("title"),
                    "file_name": task.get("file_name"),
                    "fingerprint": task.get("fingerprint"),
                    "piece_id": task.get("piece_id"),
                    "copy_body": task.get("copy_body")}
            for field in ("topics", "plan", "plan_path", "plan_file", "job_id",
                          "queue_job_id", "env_id", "platform",
                          "platform_account_id"):
                if field in task:
                    item[field] = task[field]
            if task.get("manual"):
                item["schedule_at"] = task.get("schedule_at")
            items.append(item)
        for row in state.get("unplaced") or []:
            if row.get("published"):
                continue
            item = {"id": row.get("item_id") or row.get("id"),
                    "account_id": row.get("account_id"),
                    "title": row.get("title"),
                    "file_name": row.get("file_name"),
                    "fingerprint": row.get("fingerprint") or
                    row.get("sha256"),
                    "piece_id": row.get("piece_id"),
                    "copy_body": row.get("copy_body")}
            for field in ("topics", "plan", "plan_path", "plan_file", "job_id",
                          "queue_job_id", "env_id", "platform",
                          "platform_account_id"):
                if field in row:
                    item[field] = row[field]
            items.append(item)
        result = cadence.schedule_tasks(self.layout, items, actor="customer")
        message = result.get("message") or "\u53d1\u5e03\u8282\u594f\u5df2\u4fdd\u5b58"
        return {"result": {"ok": bool(result.get("ok")), "message": message,
                           "result": result}, "state": self.state()}

    def update_material_topics(self, targets, topics):
        from . import cadence
        result = cadence.update_topics(self.layout, targets, topics,
                                       actor="customer")
        return {"result": result, "state": self.state()}

    def cancel_cadence_task(self, task_id):
        from . import cadence
        result = cadence.cancel_task(self.layout, task_id, actor="customer")
        return {"result": result, "state": self.state()}

    def set_cadence_manual_mode(self, enabled):
        from . import cadence
        result = cadence.set_manual_mode(self.layout, enabled, actor="customer")
        return {"result": result, "state": self.state()}

    def set_cadence_time(self, task_id, schedule_at, override=False):
        from . import cadence
        result = cadence.set_manual_time(self.layout, task_id, schedule_at,
                                         actor="customer", override=override)
        return {"result": result, "state": self.state()}

    def manual_publish_evidence(self, item_id, account_id):
        from . import cadence
        result = cadence.manual_publish_evidence(self.layout, item_id, account_id)
        # Return only an opaque server reference; clients cannot author proof.
        result.pop("evidence", None)
        return {"result": result}

    def mark_manual_published(self, item_id, account_id, evidence=None, evidence_ref=None):
        from . import cadence
        result = cadence.record_manual_published(
            self.layout, item_id, account_id=account_id, actor="customer",
            evidence=evidence, evidence_ref=evidence_ref)
        return {"result": result, "state": self.state()}

    def _reload_registry(self):
        from . import platforms
        self.envs, self.channels = platforms.load_registry(self.layout)

    def add_env(self, label=None, platforms=None):
        from . import envadmin
        result = envadmin.add_environment(self.layout, label=label,
                                          platforms=platforms)
        if result.get("ok"):
            self._reload_registry()
        return {"result": result, "state": self.state()}

    def disable_env(self, env_id):
        from . import envadmin
        result = envadmin.disable_environment(self.layout, env_id)
        if result.get("ok"):
            self._reload_registry()
        return {"result": result, "state": self.state()}

    def delete_env(self, env_id, *, confirm=False):
        from . import envadmin
        result = envadmin.delete_environment(self.layout, env_id, confirm=confirm)
        if result.get("ok"):
            self._reload_registry()
        return {"result": result, "state": self.state()}


def make_handler(handlers: PanelHandlers):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ManjuPanel/1.1"

        def log_message(self, fmt, *args):  # keep the panel quiet on stdout
            pass

        def _send(self, code, body, content_type):
            payload = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False, indent=2)
                       + "\n", "application/json; charset=utf-8")

        def do_GET(self):
            self._safe_route("GET")

        def do_POST(self):
            self._safe_route("POST")

        def _safe_route(self, method):
            try:
                self._route(method)
            except (ValueError, TypeError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:
                logging.getLogger("winport.panel").exception(
                    "panel operation failed: %s", type(exc).__name__)
                self._json(500, {"error": "\u64cd\u4f5c\u5931\u8d25\uff0c\u8bf7\u67e5\u770b\u672c\u673a\u65e5\u5fd7"})

        def _loopback_ok(self) -> bool:
            client = self.client_address[0] if self.client_address else ""
            host = self.headers.get("Host", "")
            try:
                hostname = urlparse("//" + host).hostname or ""
            except ValueError:
                hostname = ""
            return client in ("127.0.0.1", "::1") and hostname in (
                "127.0.0.1", "localhost", "::1")

        def _read_json_body(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0 or length > 1024 * 1024:
                return None
            try:
                obj = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return None
            return obj if isinstance(obj, dict) else None

        def _route(self, method):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            channel_id = (query.get("channel") or [""])[0]
            env_id = (query.get("env") or [""])[0]
            task_id = (query.get("task") or [""])[0]
            if not self._loopback_ok():
                self._json(403, {"error": "\u53ea\u5141\u8bb8\u672c\u673a\u9762\u677f\u64cd\u4f5c"})
                return
            readonly = {"/", "/index.html", "/healthz", "/api/state", "/api/support-code"}
            if parsed.path not in readonly and method != "POST":
                self._json(405, {"error": "\u64cd\u4f5c\u63a5\u53e3\u53ea\u5141\u8bb8 POST"})
                return
            if method == "POST":
                origin = self.headers.get("Origin")
                expected = "http://" + self.headers.get("Host", "")
                if (origin and origin != expected) or self.headers.get("Sec-Fetch-Site") == "cross-site":
                    self._json(403, {"error": "\u62d2\u7edd\u8de8\u7ad9\u64cd\u4f5c\u672c\u673a\u9762\u677f"})
                    return
            if parsed.path in ("/", "/index.html"):
                self._send(200, render_html(handlers.state()),
                           "text/html; charset=utf-8")
            elif parsed.path == "/healthz":
                # 1.2.5 round-11 (GPT review item 8-C): the readiness marker
                # carries a per-launch ``instance`` token; /healthz echoes it
                # so the launcher can prove THIS live panel belongs to the
                # agent it just started -- not a recycled pid or another
                # process answering 200.
                from .version import VERSION
                self._json(200, {"ok": True, "app": "winport", "version": VERSION,
                                 "instance": handlers.instance_token})
            elif parsed.path == "/api/state":
                self._json(200, handlers.state())
            elif parsed.path == "/api/support-code":
                from .remote_channel import remote_support_view
                view = remote_support_view(handlers.layout)
                code = (_support_code(handlers.layout)
                        if view.get("enabled") and view.get("provisioned") else "")
                self._json(200, {"support_code": code})
            elif parsed.path == "/api/machine-code":
                from .fingerprint import fingerprint
                self._json(200, {"machine_id": fingerprint()["machine_id"]})
            elif parsed.path == "/api/activate":
                self._json(200, handlers.activate())
            elif parsed.path == "/api/activate-code":
                body = self._read_json_body() or {}
                self._json(200, handlers.activate_with_code(body.get("code")))
            elif parsed.path == "/api/control-refresh":
                body = self._read_json_body() or {}
                self._json(200, handlers.control_refresh(body.get("code")))
            elif parsed.path == "/api/control-status":
                self._json(200, control_banner(handlers.layout))
            elif parsed.path == "/api/license-import":
                from .activation import install_document
                body = self._read_json_body() or {}
                overwrite = ({"overwrite": True}
                             if body.get("confirm_overwrite") is True else {})
                self._json(200, install_document(
                    handlers.layout, body.get("license"), **overwrite))
            elif parsed.path == "/api/open":
                self._json(200, handlers.open(channel_id))
            elif parsed.path == "/api/open-env":
                self._json(200, handlers.open_env(env_id))
            elif parsed.path == "/api/check":
                self._json(200, handlers.check(channel_id))
            elif parsed.path == "/api/remote-channel":
                body = self._read_json_body() or {}
                if type(body.get("enabled")) is not bool:
                    raise ValueError("enabled must be boolean")
                from .settings import load_settings
                settings = load_settings(handlers.layout.settings_file)
                remote_settings = settings.get("remote_channel")
                if not isinstance(remote_settings, dict):
                    remote_settings = {}
                remote_settings["enabled"] = body["enabled"]
                settings["remote_channel"] = remote_settings
                write_json(handlers.layout.settings_file, settings)
                if not body["enabled"]:
                    # The bearer grants full command execution; once support is
                    # switched off it must not survive in memory or on disk.
                    remote = handlers.remote_channel
                    if remote is not None:
                        remote.stop_now()
                    else:
                        for name in ("token", "session.json"):
                            (handlers.layout.data / "remote-channel" / name).unlink(
                                missing_ok=True)
                self._json(200, {"ok": True, "enabled": body["enabled"]})
            elif parsed.path == "/api/cadence":
                body = self._read_json_body() or {}
                self._json(200, handlers.save_cadence(
                    max_per_day=body.get("max_per_day"),
                    windows=body.get("windows"),
                    window_entries=body.get("window_entries"),
                    min_gap_minutes=body.get("min_gap_minutes"),
                    gap_min=body.get("gap_min"),
                    gap_max=body.get("gap_max"),
                    cross_account_gap_minutes=body.get(
                        "cross_account_gap_minutes")))
            elif parsed.path == "/api/material-topics":
                body = self._read_json_body() or {}
                self._json(200, handlers.update_material_topics(
                    body.get("targets"), body.get("topics")))
            elif parsed.path == "/api/cancel-cadence":
                self._json(200, handlers.cancel_cadence_task(task_id))
            elif parsed.path == "/api/cadence-manual-mode":
                body = self._read_json_body() or {}
                self._json(200, handlers.set_cadence_manual_mode(
                    bool(body.get("enabled"))))
            elif parsed.path == "/api/cadence-set-time":
                body = self._read_json_body() or {}
                self._json(200, handlers.set_cadence_time(
                    body.get("task_id"), body.get("schedule_at"),
                    override=bool(body.get("override"))))
            elif parsed.path == "/api/cadence-manual-evidence":
                self._json(200, handlers.manual_publish_evidence(
                    (query.get("item") or [""])[0], (query.get("account") or [""])[0]))
            elif parsed.path == "/api/cadence-manual-published":
                body = self._read_json_body() or {}
                item_id = (query.get("item") or [""])[0]
                account_id = (query.get("account") or [""])[0]
                self._json(200, handlers.mark_manual_published(
                    item_id, account_id, evidence=body.get("evidence"), evidence_ref=body.get("evidence_ref")))
            elif parsed.path == "/api/add-env":
                body = self._read_json_body() or {}
                label = body.get("label") or (query.get("label") or [""])[0]
                platforms = body.get("platforms") or (query.get("platforms") or [""])[0]
                self._json(200, handlers.add_env(label=label, platforms=platforms))
            elif parsed.path == "/api/disable-env":
                self._json(200, handlers.disable_env(env_id))
            elif parsed.path == "/api/delete-env":
                confirm = (query.get("confirm") or [""])[0]
                self._json(200, handlers.delete_env(
                    env_id, confirm=str(confirm).lower() in ("true", "1", "yes")))
            else:
                self._json(404, {"error": "not found", "path": parsed.path})

    return Handler


def make_server(layout, channels, *, host="127.0.0.1", port=None, envs=None,
                opener=None, checker=None, env_opener=None, tcp=None,
                processes=None, chrome_report=None, instance_token=None,
                remote_channel=None, restart_poller=None):
    port = int(port or DEFAULT_PANEL_PORT)
    handlers = PanelHandlers(layout, channels, envs=envs, opener=opener,
                             checker=checker, env_opener=env_opener, tcp=tcp,
                             processes=processes, chrome_report=chrome_report,
                             instance_token=instance_token,
                             remote_channel=remote_channel,
                             restart_poller=restart_poller)
    server = ThreadingHTTPServer((host, port), make_handler(handlers))
    server.daemon_threads = True
    return server, handlers


def serve_in_thread(layout, channels, *, host="127.0.0.1", port=None, **kw):
    """Start the panel on a daemon thread (used by the resident agent)."""
    server, _handlers = make_server(layout, channels, host=host, port=port, **kw)
    try:
        from .power import keep_display_awake
        keep_display_awake()
    except Exception:
        pass
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="winport-panel")
    thread.start()
    return {"server": server, "thread": thread,
            "url": panel_url(server.server_address)}


def panel_url(address) -> str:
    host, port = address[0], address[1]
    return f"http://{host}:{port}/"


def open_panel(url):
    """Open the panel in the customer's default browser (no console)."""
    import webbrowser
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def write_state_file(layout, state) -> None:
    write_json(layout.run / "panel-state.json", state)
