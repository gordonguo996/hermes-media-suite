"""1.2.20 publish cadence scheduler.

The customer can tune the four natural publish windows, a per-account daily
cap, and the random gap band.  All generated neighbouring publish points inside
the same selected window are separated by a random 5-8 minute gap (never a
fixed cadence), while separate selected windows remain intentionally quiet.
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MIN_LEAD_MINUTES = 3
MAX_HORIZON_DAYS = 14
DEFAULT_GAP_MIN = 5
DEFAULT_GAP_MAX = 8
DEFAULT_MAX_PER_DAY_PER_ACCOUNT = 0
DEFAULT_AVOID_MINUTE_MARKS = ()
DEFAULT_WINDOWS = (
    ("00:00", "23:59"),
)
DEFAULT_BACKOFF = {"initial_minutes": 30, "multiplier": 2.0, "max_minutes": 360}


def parse_time(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(
            str(value or "").strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _aware(value, label="datetime"):
    if not isinstance(value, datetime):
        raise ValueError(f"{label} must be a datetime")
    if value.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _hhmm_to_minutes(value):
    if isinstance(value, (tuple, list)):
        hour, minute = value
    else:
        hour, minute = str(value).strip().split(":")
    hour, minute = int(hour), int(minute)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"bad HH:MM: {value!r}")
    return hour * 60 + minute


def _minutes_to_hhmm(value):
    hour, minute = divmod(int(value), 60)
    return f"{hour:02d}:{minute:02d}"


def _clamp_int(value, default, low, high):
    if value is None:
        return default
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _coerce_window_entry(entry, index=0):
    """Return a normalized {'id','start','end','enabled'} or None.

    Accepts both the legacy ``["07:00", "09:30"]`` pair and the 1.2.20 panel
    form ``{"id": ..., "start": ..., "end": ..., "enabled": ...}``.
    """
    if isinstance(entry, dict):
        start = entry.get("start")
        end = entry.get("end")
        enabled = entry.get("enabled", True)
        window_id = entry.get("id") or f"w{index + 1}"
    elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
        start, end = entry[0], entry[1]
        enabled = True
        window_id = f"w{index + 1}"
    else:
        return None
    try:
        start_min = _hhmm_to_minutes(start)
        end_min = _hhmm_to_minutes(end)
    except (TypeError, ValueError):
        return None
    if start_min >= end_min:
        return None
    return {"id": str(window_id),
            "start": _minutes_to_hhmm(start_min),
            "end": _minutes_to_hhmm(end_min),
            "enabled": bool(enabled)}


def _normalize_windows(value):
    """Normalize configured windows into full entries.

    Legacy pair-only inputs are kept working.  Cross-midnight entries are
    deliberately rejected here (customers split them into two entries).
    """
    if not isinstance(value, (list, tuple)):
        value = DEFAULT_WINDOWS
    entries = []
    for index, entry in enumerate(value):
        normalized = _coerce_window_entry(entry, index)
        if normalized:
            entries.append(normalized)
    return entries or [_coerce_window_entry(entry, index)
                       for index, entry in enumerate(DEFAULT_WINDOWS)]


def _enabled_window_pairs(entries):
    return [[entry["start"], entry["end"]] for entry in entries
            if entry.get("enabled", True) is True]


def merge_window_entries(entries):
    """Return non-overlapping effective intervals for scheduling.

    The customer may intentionally keep overlapping entries in settings; that
    overlap is allowed as configuration, but a generated/manual point must not
    be duplicated into both entries.  Scheduling therefore works on the merged
    union so every point belongs to exactly one effective interval.
    """
    intervals = []
    for entry in entries:
        if not entry.get("enabled", True):
            continue
        intervals.append((_hhmm_to_minutes(entry["start"]),
                          _hhmm_to_minutes(entry["end"])))
    intervals.sort()
    merged = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [[_minutes_to_hhmm(start), _minutes_to_hhmm(end)]
            for start, end in merged]


def _normalize_gap_pair(raw):
    gap_min = _clamp_int(raw.get("gap_min", raw.get("min_gap_minutes")),
                         DEFAULT_GAP_MIN, 5, 8)
    gap_max = _clamp_int(raw.get("gap_max", raw.get("max_gap_minutes")),
                         DEFAULT_GAP_MAX, gap_min, 8)
    return gap_min, gap_max


def normalize_schedule_settings(settings=None):
    source = settings or {}
    top = source if isinstance(source, dict) else {}
    raw = top.get("schedule") if isinstance(top.get("schedule"), dict) else top
    raw = raw if isinstance(raw, dict) else {}
    gap_min, gap_max = _normalize_gap_pair(raw)
    marks = raw.get("avoid_minute_marks")
    if not isinstance(marks, (list, tuple)):
        marks = DEFAULT_AVOID_MINUTE_MARKS
    marks = tuple(sorted({int(m) for m in marks if 0 <= int(m) <= 59}))
    backoff = raw.get("backoff") if isinstance(raw.get("backoff"), dict) else {}
    window_entries = _normalize_windows(
        raw.get("window_entries", raw.get("windows")))
    return {
        "timezone": str(top.get("timezone") or "Asia/Shanghai"),
        "window_entries": window_entries,
        "windows": merge_window_entries(window_entries),
        "max_per_day_per_account": _clamp_int(
            raw.get("max_per_day_per_account"),
            DEFAULT_MAX_PER_DAY_PER_ACCOUNT, 0, 10),
        "gap_min": gap_min,
        "gap_max": gap_max,
        "min_gap_minutes": gap_min,
        "max_gap_minutes": gap_max,
        "cross_account_gap_minutes": [gap_min, gap_max],
        "min_lead_minutes": _clamp_int(
            raw.get("min_lead_minutes"), MIN_LEAD_MINUTES,
            MIN_LEAD_MINUTES, 10080),
        "max_horizon_days": _clamp_int(
            raw.get("max_horizon_days"), MAX_HORIZON_DAYS, 1, 30),
        "avoid_minute_marks": marks or DEFAULT_AVOID_MINUTE_MARKS,
        "backoff": {
            "initial_minutes": _clamp_int(
                backoff.get("initial_minutes"),
                DEFAULT_BACKOFF["initial_minutes"], 1, 360),
            "multiplier": float(backoff.get("multiplier") or
                                DEFAULT_BACKOFF["multiplier"]),
            "max_minutes": _clamp_int(
                backoff.get("max_minutes"),
                DEFAULT_BACKOFF["max_minutes"], 1, 1440),
        },
    }


def resolve_timezone(settings=None):
    name = normalize_schedule_settings(settings)["timezone"]
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        try:
            return datetime.now().astimezone().tzinfo
        except Exception:
            return timezone.utc


def window_contains(value, windows=None, settings=None):
    local = parse_time(value).astimezone(resolve_timezone(settings))
    windows = (windows if windows is not None
               else normalize_schedule_settings(settings)["windows"])
    minute = local.hour * 60 + local.minute
    return any(_hhmm_to_minutes(start) <= minute <= _hhmm_to_minutes(end)
               for start, end in windows)


def next_slot(now=None, *, settings=None, min_lead_minutes=None):
    """Earliest legal publish point at or after now+min_lead inside a window.

    Used for catch-up after the machine was asleep/off: the overdue job is not
    published late (out-of-window), it is moved to the next natural window so
    the cadence stays human.  Returns an aware datetime in the configured tz.
    """
    cfg = normalize_schedule_settings(settings)
    tz = resolve_timezone(settings)
    now = _aware(now or datetime.now(timezone.utc)).astimezone(tz)
    lead = int(min_lead_minutes or cfg["min_lead_minutes"])
    # 1.2.205: never hand back a slot that is already under the lead once the
    # caller spends a moment before publishing — ceil to the next minute and
    # keep one extra minute of headroom.
    earliest = now + timedelta(minutes=lead + 1)
    marks = set(cfg["avoid_minute_marks"])

    def _at(day, hhmm):
        minutes = _hhmm_to_minutes(hhmm)
        return day.replace(hour=minutes // 60, minute=minutes % 60,
                           second=0, microsecond=0)

    for day_offset in range(0, cfg["max_horizon_days"] + 1):
        day = (now + timedelta(days=day_offset)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        for start, end in cfg["windows"]:
            low, high = _at(day, start), _at(day, end)
            cand = max(low, earliest)
            if cand.second or cand.microsecond:
                cand = cand.replace(second=0, microsecond=0) + timedelta(minutes=1)
            if cand.minute in marks:
                cand += timedelta(minutes=1)
            if cand > high:
                continue
            if cand.minute in marks:      # window too short to dodge the mark
                continue
            return cand
    raise ValueError("no publish slot within horizon")


def validate_window(schedule_at, now, *,
                    min_lead_minutes=MIN_LEAD_MINUTES,
                    max_horizon_days=MAX_HORIZON_DAYS):
    schedule_at = _aware(schedule_at, "schedule_at")
    now = _aware(now, "now")
    delta = schedule_at - now
    if delta < timedelta(minutes=min_lead_minutes):
        return False, f"too_soon: {delta} < {min_lead_minutes} min"
    if delta > timedelta(days=max_horizon_days):
        return False, f"beyond_horizon: {delta.days}d > {max_horizon_days}d"
    return True, "ok"


def backoff_delay(failures, settings=None):
    cfg = normalize_schedule_settings(settings)["backoff"]
    failures = max(0, int(failures or 0))
    multiplier = max(1.0, float(cfg.get("multiplier") or 1.0))
    return int(min(int(cfg["initial_minutes"]) * (multiplier ** failures),
                   int(cfg["max_minutes"])))


def _window_bounds(day, window, tz):
    start_min = _hhmm_to_minutes(window[0])
    end_min = _hhmm_to_minutes(window[1])
    return (datetime(day.year, day.month, day.day, start_min // 60,
                     start_min % 60, tzinfo=tz),
            datetime(day.year, day.month, day.day, end_min // 60,
                     end_min % 60, tzinfo=tz))


def _same_window(local_min, other_min, windows, _tz=None):
    return any(_hhmm_to_minutes(start) <= local_min <= _hhmm_to_minutes(end)
               and _hhmm_to_minutes(start) <= other_min <= _hhmm_to_minutes(end)
               for start, end in windows)


def _window_index(local, windows):
    minute = local.hour * 60 + local.minute
    for index, (start, end) in enumerate(windows):
        if _hhmm_to_minutes(start) <= minute <= _hhmm_to_minutes(end):
            return index
    return None


def _allowed_minutes(start, end, avoid_minute_marks):
    start = start.replace(second=0, microsecond=0)
    end = end.replace(second=0, microsecond=0)
    if start > end:
        return []
    bad = set(avoid_minute_marks)
    out = []
    point = start
    while point <= end:
        if point.minute not in bad:
            out.append(point)
        point += timedelta(minutes=1)
    return out


def _slot_record(item, dt, *, manual=False, manual_override=False):
    record = {
        "account_id": item.get("account_id"),
        "scheduled_at": dt.isoformat(),
        "manual": bool(manual),
        "manual_override": bool(manual_override),
    }
    if item.get("id") is not None:
        record["item_id"] = item["id"]
    # Keep the material identity with the slot.  The cadence view and the
    # topic editor address one material, not just a time point, so dropping
    # these fields here would make an edit write to the wrong (or no) plan.
    for field in ("piece_id", "title", "file_name", "fingerprint", "sha256",
                  "copy_body", "topics", "plan", "plan_path", "plan_file",
                  "job_id", "queue_job_id", "env_id", "platform",
                  "platform_account_id"):
        if field in item:
            record[field] = item[field]
    return record


def _internal(item, dt, *, manual=False, manual_override=False, settings=None):
    record = _slot_record(item, dt, manual=manual,
                          manual_override=manual_override)
    local = dt.astimezone(resolve_timezone(settings))
    record["_dt"] = dt
    record["_local_date"] = local.date()
    record["_local_minute"] = local.strftime("%Y-%m-%d %H:%M")
    return record


def _gap_verdict(candidate, assigned, account_id, cfg, tz):
    local = candidate.astimezone(tz)
    if _window_index(local, cfg["windows"]) is None:
        return {"ok": False, "code": "OUTSIDE_WINDOW",
                "message": "指定时间不在勾选时段内，客户确认后才能覆盖"}
    if local.minute in cfg["avoid_minute_marks"]:
        return {"ok": False, "code": "PROHIBITED_MINUTE",
                "message": "发布时间不能落在整点或 5 分钟刻度上"}

    same_day_count = 0
    same_window_neighbours = []
    for slot in assigned or []:
        try:
            other = parse_time(slot.get("scheduled_at"))
        except (TypeError, ValueError):
            continue
        other_local = other.astimezone(tz)
        delta = abs((candidate - other).total_seconds())
        if delta < cfg["gap_min"] * 60:
            code = ("SAME_ACCOUNT_GAP"
                    if slot.get("account_id") == account_id
                    else "CROSS_ACCOUNT_GAP")
            return {"ok": False, "code": code,
                    "message": f"相邻发布点间隔不足 {cfg['gap_min']} 分钟"}
        if slot.get("account_id") == account_id:
            if (other_local.date(), other_local.hour, other_local.minute) == \
                    (local.date(), local.hour, local.minute):
                return {"ok": False, "code": "SAME_ACCOUNT_SAME_MINUTE",
                        "message": "同号不能在同一分钟排两条"}
            if other_local.date() == local.date():
                same_day_count += 1
        if other_local.date() == local.date() and _window_index(
                other_local, cfg["windows"]) == _window_index(local,
                                                              cfg["windows"]):
            same_window_neighbours.append(other)

    if cfg["max_per_day_per_account"] > 0 and \
            same_day_count >= cfg["max_per_day_per_account"]:
        return {"ok": False, "code": "PER_DAY_CAP",
                "message": f"单号单日最多 {cfg['max_per_day_per_account']} 条"}

    same_window_neighbours.append(candidate)
    same_window_neighbours.sort()
    index = same_window_neighbours.index(candidate)
    for neighbour in (same_window_neighbours[index - 1:index] +
                      same_window_neighbours[index + 1:index + 2]):
        delta = abs((candidate - neighbour).total_seconds())
        if not (cfg["gap_min"] * 60 <= delta <= cfg["gap_max"] * 60):
            code = "CROSS_ACCOUNT_GAP"
            for slot in assigned or []:
                try:
                    other = parse_time(slot.get("scheduled_at"))
                except (TypeError, ValueError):
                    continue
                if other == neighbour and slot.get("account_id") == account_id:
                    code = "SAME_ACCOUNT_GAP"
                    break
            return {"ok": False, "code": code,
                    "message": f"相邻发布点间隔必须落在 {cfg['gap_min']}–{cfg['gap_max']} 分钟"}
    return {"ok": True}


def validate_manual_time(schedule_at, now, *, settings=None, account_id=None,
                         assigned=None):
    cfg = normalize_schedule_settings(settings)
    try:
        parsed = _aware(parse_time(schedule_at), "schedule_at")
    except (TypeError, ValueError):
        return {"ok": False, "code": "INVALID_TIME",
                "message": "指定时间格式不正确"}
    now = _aware(now, "now")
    if parsed < now:
        return {"ok": False, "code": "PAST_TIME", "message": "不能指定过去时间"}
    ok, reason = validate_window(
        parsed, now, min_lead_minutes=cfg["min_lead_minutes"],
        max_horizon_days=cfg["max_horizon_days"])
    if not ok:
        return {"ok": False,
                "code": ("TOO_SOON" if reason.startswith("too_soon")
                         else "BEYOND_HORIZON"),
                "message": (f"至少提前 {cfg['min_lead_minutes']} 分钟"
                            if reason.startswith("too_soon")
                            else f"不能超过 {cfg['max_horizon_days']} 天")}
    return _gap_verdict(parsed, assigned, account_id, cfg,
                        resolve_timezone(settings))


def _public(slot):
    return {key: value for key, value in slot.items()
            if not key.startswith("_")}


def _slots_in_window(count, start, end, rng, *, gap_min=DEFAULT_GAP_MIN,
                     gap_max=DEFAULT_GAP_MAX,
                     avoid_minute_marks=DEFAULT_AVOID_MINUTE_MARKS):
    if count <= 0:
        return []
    start = start.replace(second=0, microsecond=0)
    end = end.replace(second=0, microsecond=0)
    if start > end:
        return []
    allowed = _allowed_minutes(start, end, avoid_minute_marks)
    if not allowed:
        return []
    for _ in range(2000):
        slots = [allowed[rng.randint(0, len(allowed) - 1)]]
        ok = True
        for _ in range(1, count):
            choices = []
            for gap in range(gap_min, gap_max + 1):
                candidate = slots[-1] + timedelta(minutes=gap)
                if start <= candidate <= end and \
                        candidate.minute not in set(avoid_minute_marks):
                    choices.append(candidate)
            if not choices:
                ok = False
                break
            slots.append(rng.choice(choices))
        if ok:
            return slots
    return []


def _pick_simple(count, now, *, window_start=None, window_end=None, seed=None,
                 settings=None):
    cfg = normalize_schedule_settings(settings)
    rng = random.Random(seed)
    tz = resolve_timezone(settings)
    if count <= 0:
        return []
    if window_start is not None or window_end is not None:
        floor = now + timedelta(minutes=cfg["min_lead_minutes"] + 1)
        if window_start is not None:
            window_start = _aware(window_start, "window_start").astimezone(tz)
        start = max(window_start, floor) if window_start is not None else floor
        if start.second or start.microsecond:
            start = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
        end = window_end if window_end is not None else start
        if start > end:
            return []
        end = window_end or (start + timedelta(days=1))
        return _slots_in_window(count, start, end, rng,
                                gap_min=cfg["gap_min"],
                                gap_max=cfg["gap_max"],
                                avoid_minute_marks=cfg["avoid_minute_marks"])
    start = now.astimezone(tz) + timedelta(minutes=cfg["min_lead_minutes"] + 1)
    start = start.replace(second=0, microsecond=0)
    if start <= now.astimezone(tz) + timedelta(minutes=cfg["min_lead_minutes"]):
        start += timedelta(minutes=1)
    end = now.astimezone(tz) + timedelta(days=cfg["max_horizon_days"])
    slots = []
    remaining = count
    day = start.date()
    while day <= end.date() and remaining > 0:
        for window in cfg["windows"]:
            st, en = _window_bounds(day, window, tz)
            st, en = max(st, start), min(en, end)
            if en <= st:
                continue
            run = _slots_in_window(remaining, st, en, rng,
                                   gap_min=cfg["gap_min"],
                                   gap_max=cfg["gap_max"],
                                   avoid_minute_marks=cfg["avoid_minute_marks"])
            if run:
                slots.extend(run)
                remaining -= len(run)
                if remaining <= 0:
                    break
        day += timedelta(days=1)
    return sorted(slots)


def _daily_remaining(group, assigned, account_id, day, cap):
    already = sum(1 for slot in assigned
                  if slot.get("account_id") == account_id
                  and slot.get("_local_date") == day)
    if cap == 0:
        return len(group)
    return min(len(group), max(0, cap - already))


def _pick_multi(items, now, *, settings=None, seed=None, existing=None):
    cfg = normalize_schedule_settings(settings)
    tz = resolve_timezone(settings)
    rng = random.Random(seed)
    assigned = []
    problems = []
    auto = []
    overflow = []

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append({"item": index, "code": "ITEM_INVALID",
                             "message": "任务必须是对象"})
            continue
        account_id = item.get("account_id") or item.get("env_id") or \
            item.get("account") or f"item-{index}"
        item = dict(item)
        item["account_id"] = account_id
        if item.get("schedule_at"):
            verdict = validate_manual_time(
                item["schedule_at"], now, settings=settings,
                account_id=account_id, assigned=assigned)
            if not verdict["ok"]:
                if verdict["code"] == "OUTSIDE_WINDOW" and \
                        item.get("allow_outside_window"):
                    assigned.append(_internal(
                        item, parse_time(item["schedule_at"]),
                        manual=True, manual_override=True, settings=settings))
                    continue
                problems.append({"item_id": item.get("id") or index,
                                 "account_id": account_id, **verdict})
                overflow.append({"item_id": item.get("id"),
                                 "account_id": account_id,
                                 "title": item.get("title"),
                                 "file_name": item.get("file_name"),
                                 "fingerprint": item.get("fingerprint") or
                                 item.get("sha256"),
                                 "reason": verdict.get("message")})
                continue
            assigned.append(_internal(item, parse_time(item["schedule_at"]),
                                      manual=True, settings=settings))
        else:
            auto.append(item)

    for slot in existing or []:
        try:
            assigned.append(_internal(
                {"id": slot.get("item_id"),
                 "account_id": slot.get("account_id") or slot.get("env_id") or ""},
                parse_time(slot.get("scheduled_at")),
                manual=bool(slot.get("manual")), settings=settings))
        except (TypeError, ValueError):
            problems.append({"item_id": slot.get("item_id"),
                             "code": "INVALID_TIME",
                             "message": "已有排期时间格式不正确"})

    groups = defaultdict(list)
    for item in auto:
        groups[item["account_id"]].append(item)

    # 1.2.205: same ceiling rule as next_slot — a slot equal to now+lead is
    # already stale by the time the job dispatches, so keep one extra minute.
    start = now.astimezone(tz) + timedelta(minutes=cfg["min_lead_minutes"] + 1)
    start = start.replace(second=0, microsecond=0)
    if start <= now.astimezone(tz) + timedelta(minutes=cfg["min_lead_minutes"]):
        start += timedelta(minutes=1)
    horizon = now.astimezone(tz) + timedelta(days=cfg["max_horizon_days"])
    day = start.date()
    last_day = horizon.date()

    while day <= last_day and any(groups.values()):
        windows_info = []
        for window_index, window in enumerate(cfg["windows"]):
            st, en = _window_bounds(day, window, tz)
            if day == start.date():
                st = max(st, start)
            en = min(en, horizon)
            if en <= st:
                continue
            allowed = _allowed_minutes(st, en, cfg["avoid_minute_marks"])
            if not allowed:
                continue
            windows_info.append({
                "index": window_index,
                "st": st,
                "en": en,
                "allowed": allowed,
                "last_dt": None,
                "account_pointer": 0,
            })

        account_order = [account for account in list(groups)
                         if groups.get(account) and
                         _daily_remaining(groups.get(account, []), assigned,
                                          account, day,
                                          cfg["max_per_day_per_account"]) > 0]
        rng.shuffle(account_order)
        active = set(account_order)
        failed_pairs = set()
        while active and windows_info:
            placed_any = False
            for window_info in windows_info:
                if not active:
                    break
                window_index = window_info["index"]
                active_list = [account for account in account_order
                               if account in active]
                if not active_list:
                    break
                start_account_pointer = window_info["account_pointer"]
                while True:
                    account_id = active_list[
                        window_info["account_pointer"] % len(active_list)]
                    window_info["account_pointer"] += 1
                    pending = groups.get(account_id) or []
                    if _daily_remaining(pending, assigned, account_id, day,
                                        cfg["max_per_day_per_account"]) <= 0:
                        active.discard(account_id)
                        active_list = [account for account in account_order
                                       if account in active]
                        if not active_list:
                            break
                        continue
                    if (window_index, account_id) in failed_pairs:
                        if window_info["account_pointer"] % len(active_list) == \
                                start_account_pointer % len(active_list):
                            break
                        continue
                    break
                if not active_list:
                    continue

                st = window_info["st"]
                en = window_info["en"]
                allowed = window_info["allowed"]
                last_dt = window_info["last_dt"]
                candidates = []
                if last_dt is None:
                    candidates = [allowed[rng.randint(0, len(allowed) - 1)]
                                  for _ in range(24)]
                else:
                    for gap in range(cfg["gap_min"], cfg["gap_max"] + 1):
                        candidate = last_dt + timedelta(minutes=gap)
                        if st <= candidate <= en and \
                                candidate.minute not in set(
                                    cfg["avoid_minute_marks"]):
                            candidates.append(candidate)
                    rng.shuffle(candidates)

                placed = False
                for candidate in candidates:
                    verdict = _gap_verdict(candidate, assigned, account_id,
                                           cfg, tz)
                    if not verdict["ok"]:
                        continue
                    assigned.append(_internal(pending[0], candidate,
                                              settings=settings))
                    groups[account_id] = pending[1:]
                    window_info["last_dt"] = candidate
                    placed_any = True
                    placed = True
                    failed_pairs.discard((window_index, account_id))
                    if not groups.get(account_id):
                        active.discard(account_id)
                    break
                if not placed:
                    failed_pairs.add((window_index, account_id))

            if not placed_any:
                stuck = []
                for account_id in list(active):
                    if all((window_info["index"], account_id) in failed_pairs
                           for window_info in windows_info):
                        stuck.append(account_id)
                if stuck:
                    for account_id in stuck:
                        active.discard(account_id)
                else:
                    break
        day += timedelta(days=1)

    for account_id, pending in groups.items():
        for item in pending:
            row = {
                "item_id": item.get("id"),
                "account_id": account_id,
                "title": item.get("title"),
                "file_name": item.get("file_name"),
                "fingerprint": item.get("fingerprint") or item.get("sha256"),
                "reason": "排不下，转人工",
            }
            for field in ("piece_id", "copy_body", "topics", "plan",
                          "plan_path", "plan_file", "job_id", "queue_job_id",
                          "env_id", "platform", "platform_account_id"):
                if field in item:
                    row[field] = item[field]
            overflow.append(row)

    assigned.sort(key=lambda slot: slot["_dt"])
    message = "ok" if not overflow and not problems else "排不下，转人工"
    return {"ok": not overflow and not problems,
            "slots": [_public(slot) for slot in assigned],
            "problems": problems,
            "overflow": overflow,
            "unplaced": overflow,
            "message": message}


def pick_slots(count_or_items, now, *, window_start=None, window_end=None,
               seed=None, min_gap_minutes=None, max_gap_minutes=None,
               avoid_minute_marks=DEFAULT_AVOID_MINUTE_MARKS,
               settings=None, existing=None, windows=None, tz_name=None):
    _aware(now, "now")
    if settings is None:
        settings = {"timezone": tz_name or "Asia/Shanghai",
                    "schedule": {"windows": windows}}
    if min_gap_minutes is not None or max_gap_minutes is not None:
        settings = dict(settings)
        schedule = settings.get("schedule") if isinstance(
            settings.get("schedule"), dict) else {}
        schedule = dict(schedule)
        if min_gap_minutes is not None:
            schedule["gap_min"] = min_gap_minutes
        if max_gap_minutes is not None:
            schedule["gap_max"] = max_gap_minutes
        settings["schedule"] = schedule
    if isinstance(count_or_items, int):
        return _pick_simple(count_or_items, now, window_start=window_start,
                            window_end=window_end, seed=seed, settings=settings)
    return _pick_multi(list(count_or_items), now, settings=settings,
                       seed=seed, existing=existing)
