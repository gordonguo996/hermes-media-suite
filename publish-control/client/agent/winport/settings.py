"""settings.json / accounts.json loading with sane defaults.

Hardcoded ~/publish-worker paths (Mac plan section 10.2) are replaced by the
Layout object; settings only carry customer-visible preferences.
"""

from __future__ import annotations

import copy

from .errors import ConfigError
from .jsonio import read_json

DEFAULT_SETTINGS = {
    "remote_channel": {"enabled": True},
    "vendor": None,                       # filled from version.py when absent
    "customer_id": "",
    "timezone": "Asia/Shanghai",
    "schedule": {
        "min_lead_minutes": 3,            # 1.2.205: 想什么时候发就什么时候发，只留 3 分钟下限
        "max_horizon_days": 14,           # Mac parity, do not change
        "avoid_minute_marks": [],
        "gap_min": 5,
        "gap_max": 8,
        # 1.2.20: customer-adjustable publish cadence controls.
        # 1.2.205: 取消发布时段限制，全天可发。
        "windows": [["00:00", "23:59"]],
        "window_entries": [
            {"id": "allday", "start": "00:00", "end": "23:59", "enabled": True}
        ],
        "max_per_day_per_account": 0,
        "cross_account_gap_minutes": [5, 8],
        "backoff": {
            "initial_minutes": 30,
            "multiplier": 2.0,
            "max_minutes": 360
        },
    },
    "downlink": {
        "poll_interval_seconds": 300,     # launchd StartInterval=300 parity
        "max_concurrency": 2,
        "retry_backoff_seconds": [5, 15, 45, 135],
        "manifest_url_ttl_seconds": 600,  # <= 10 min
        "media_url_ttl_seconds": 3600,    # <= 60 min
        "bandwidth_limit_kbps": 0,        # 0 = unlimited
    },
    "retention_days": 14,
    "disk_watermark_gb": 5,
    "sms_wait_seconds": 180,
    "verification_detect_seconds": 30,
    "qr_wait_seconds": 300,
    "alerts": {"tray": True, "upload_summary": True},
    # 1.2.13: publishing waits for a Feishu confirmation.  The customer machine
    # carries only a low-privilege install token; the vendor Feishu app secret
    # lives on the approval service, never in this file or this package.
    "feishu_approval": {
        "enabled": True,
        "endpoint": "",
        "client_token": "",
        "timeout_minutes": 30,
        "poll_interval_seconds": 15,
    },
    # In-place incremental updates.  An empty manifest_url means "disabled"
    # even when enabled is true, so an unconfigured install never polls.
    "updates": {
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
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


ALL_DAY_WINDOWS = [["00:00", "23:59"]]
ALL_DAY_ENTRY = {"id": "allday", "start": "00:00", "end": "23:59", "enabled": True}


def _migrate_schedule_anytime(merged):
    """1.2.205: publishing is allowed at any hour.

    The product decision is "publish whenever the customer wants"; the only
    remaining rule is a 3-minute floor enforced by validate_window().  Older
    settings.json files pin four legal bands, a per-5-minute avoid list and a
    large lead, and hand-edited files may hold nulls, strings or disabled
    entries.  Normalise the schedule on every load so behaviour never depends
    on the exact legacy shape, and so no truthiness trap can leave us with an
    empty window list (which would silently stop all scheduling).
    """
    schedule = merged.get("schedule")
    if not isinstance(schedule, dict):
        return merged
    windows = schedule.get("windows")
    entries = schedule.get("window_entries")
    raw_lead = schedule.get("min_lead_minutes")
    entry = entries[0] if isinstance(entries, list) and len(entries) == 1 else None
    already = (
        windows == ALL_DAY_WINDOWS
        and type(raw_lead) is int
        and raw_lead == 3
        and schedule.get("avoid_minute_marks") == []
        and isinstance(entry, dict)
        and entry.get("enabled") is True
        and entry.get("id") == "allday"
        and entry.get("start") == "00:00"
        and entry.get("end") == "23:59")
    if not already:
        schedule["min_lead_minutes"] = 3
        schedule["avoid_minute_marks"] = []
        schedule["windows"] = [list(pair) for pair in ALL_DAY_WINDOWS]
        schedule["window_entries"] = [dict(ALL_DAY_ENTRY)]
    return merged


def load_settings(path):
    raw = read_json(path, default=None)
    if raw is None:
        return copy.deepcopy(DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        raise ConfigError(f"settings.json must be an object: {path}")
    return _migrate_schedule_anytime(_deep_merge(DEFAULT_SETTINGS, raw))
