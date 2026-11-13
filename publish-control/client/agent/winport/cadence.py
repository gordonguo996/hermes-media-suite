"""Cadence settings, task scheduling state, backoff and confirmation flow.

The customer-facing cadence state lives under ``run/cadence.json``; settings
continue to live in ``config/settings.json``.  This module deliberately keeps
the pure scheduling algorithm in ``schedule.py`` and only adds persistence,
logging, panel-friendly views and the Feishu confirmation lifecycle.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .jsonio import read_json, write_json
from .schedule import (backoff_delay, normalize_schedule_settings,
                       parse_time, resolve_timezone, validate_manual_time,
                       window_contains)
from .settings import load_settings
from .topics import normalize_topics

CANCEL_STATUS = "cancelled"
VOID_STATUS = "void"
POSTPONED_STATUS = "postponed"
PENDING_STATUS = "pending_confirm"
CONFIRMED_STATUS = "confirmed"
SCHEDULED_STATUS = "scheduled"

STATUS_LABELS = {
    SCHEDULED_STATUS: "已排期",
    PENDING_STATUS: "待确认",
    CONFIRMED_STATUS: "已确认",
    POSTPONED_STATUS: "已顺延一次",
    CANCEL_STATUS: "已取消",
    VOID_STATUS: "已作废",
}


def _utcnow(now=None):
    return now or datetime.now(timezone.utc)


def _iso(value):
    return parse_time(value).isoformat()


def cadence_path(layout) -> Path:
    return layout.run / "cadence.json"


def default_state():
    return {
        "schema": "cadence/1.0",
        "settings_log": [],
        "tasks": [],
        "unplaced": [],
        "backoff": {},
        "published": {},
        "manual_ledger": [],
        "manual_mode": False,
        "batch_approval": {},
    }


def load_cadence(layout) -> dict:
    raw = read_json(cadence_path(layout), default=None)
    state = default_state()
    if isinstance(raw, dict):
        for key in ("settings_log", "tasks", "unplaced", "backoff",
                    "published", "manual_ledger", "batch_approval"):
            if isinstance(raw.get(key), (list, dict)):
                state[key] = raw[key]
        if raw.get("schema"):
            state["schema"] = raw["schema"]
        state["manual_mode"] = bool(raw.get("manual_mode", False))
    return state


def save_cadence(layout, state) -> dict:
    write_json(cadence_path(layout), state)
    return state


def load_schedule_settings(layout):
    return load_settings(layout.settings_file)


def save_settings(layout, settings) -> None:
    write_json(layout.settings_file, settings)


def schedule_config(layout, settings=None):
    settings = settings if settings is not None else load_schedule_settings(layout)
    return normalize_schedule_settings(settings)


def append_settings_log(layout, state, *, actor, before, after) -> None:
    state.setdefault("settings_log", []).append({
        "at": _iso(_utcnow()),
        "actor": actor or "customer",
        "before": before,
        "after": after,
    })
    save_cadence(layout, state)


def update_cadence_settings(layout, patch, *, actor="customer") -> dict:
    """Merge a schedule patch, hard-clamp it, write settings and log the diff."""
    from .schedule import _coerce_window_entry
    for field in ("window_entries", "windows"):
        if field in (patch or {}):
            entries = patch[field]
            if not isinstance(entries, list) or not entries or any(
                    _coerce_window_entry(e, i) is None for i, e in enumerate(entries)):
                raise ValueError("发布时间窗无效：使用 HH:MM，开始须早于结束；跨午夜请拆分")
    settings = load_schedule_settings(layout)
    schedule = dict(settings.get("schedule") or {})
    before = normalize_schedule_settings(settings)
    schedule.update(patch or {})
    settings["schedule"] = schedule
    save_settings(layout, settings)
    after = schedule_config(layout, settings)
    state = load_cadence(layout)
    append_settings_log(layout, state, actor=actor,
                        before={"schedule": before}, after={"schedule": after})
    return {"settings": after, "log": state["settings_log"][-1]}


def set_manual_mode(layout, enabled, *, actor="customer") -> dict:
    """Toggle manual publishing mode; manual rows are hidden until enabled."""
    state = load_cadence(layout)
    before = bool(state.get("manual_mode", False))
    state["manual_mode"] = bool(enabled)
    state.setdefault("settings_log", []).append({
        "at": _iso(_utcnow()),
        "actor": actor or "customer",
        "before": {"manual_mode": before},
        "after": {"manual_mode": state["manual_mode"]},
    })
    save_cadence(layout, state)
    return {"ok": True, "manual_mode": state["manual_mode"]}


def manual_publish_evidence(layout, item_id, account_id):
    """Resolve an opaque reference to server-collected evidence, never POST JSON.

    An unobserved human publication cannot be retrospectively upgraded from a
    list. It remains unverified until a monitored attempt has a platform receipt.
    """
    import json
    from .stage2_ledger import Stage2Ledger, CONSUMED
    from .publish_guard import is_ascii_mid
    state=load_cadence(layout)
    row=next((r for r in state.get('unplaced',[]) if str(r.get('item_id') or r.get('id'))==str(item_id)),None)
    if not state.get('manual_mode') or not row or str(row.get('account_id'))!=str(account_id):
        return {'ok':False,'code':'EVIDENCE_REQUIRED','message':'未找到对应账号的手工素材'}
    fingerprint=str(row.get('fingerprint') or '')
    matches=[]
    for key,entry in Stage2Ledger(layout.stage2_ledger).snapshot()['entries'].items():
        proof=entry.get('publish_evidence') or {}; receipt=proof.get('receipt') or {}
        if (entry.get('state')==CONSUMED and entry.get('account_id')==account_id and
            fingerprint and entry.get('content_hash')==fingerprint and proof.get('ok') is True and
            receipt.get('source')=='playwright_response' and receipt.get('ok') is True and
            receipt.get('account_id')==account_id and receipt.get('fingerprint')==fingerprint and
            receipt.get('attempt_id') and receipt.get('attempt_id')==entry.get('attempt_id') and
            receipt.get('item_id')==receipt.get('work_id') and receipt.get('work_id')==entry.get('platform_work_id') and is_ascii_mid(receipt.get('work_id')) and
            type(receipt.get('status')) is int and receipt['status']==200 and
            type(receipt.get('status_code')) is int and receipt['status_code']==0 and
            receipt.get('creation_id') and receipt['creation_id']==entry.get('creation_id') and
            receipt.get('video_id') and receipt['video_id']==entry.get('video_id') and
            receipt.get('response_sha256')):
            ref=hashlib.sha256(json.dumps([key,entry],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            matches.append((ref,proof))
    if len(matches)!=1:
        return {'ok':False,'code':'EVIDENCE_REQUIRED','message':'未取得该素材的受监控平台回执；保持未核验，不能由列表或手填信息补记成功'}
    ref,proof=matches[0]
    return {'ok':True,'evidence_ref':ref,'evidence':proof}


def record_manual_published(layout, item_id, *, account_id=None, now=None,
                            settings=None, actor="customer", evidence=None,
                            evidence_ref=None) -> dict:
    from .lock import FileLock
    lock=FileLock(layout.run/'manual-evidence.lock')
    if not lock.acquire():
        return {'ok':False,'code':'EVIDENCE_BUSY','message':'核验正在进行，请稍后重试'}
    try:
        return _record_manual_published(layout,item_id,account_id=account_id,now=now,
            settings=settings,actor=actor,evidence=evidence,evidence_ref=evidence_ref)
    finally: lock.release()


def _record_manual_published(layout, item_id, *, account_id=None, now=None,
                             settings=None, actor="customer", evidence=None,
                             evidence_ref=None) -> dict:
    state=load_cadence(layout)
    if not state.get('manual_mode'):
        return {'ok':False,'code':'MANUAL_MODE_DISABLED','message':'手工发布模式未开启'}
    if evidence is not None or not isinstance(evidence_ref,str):
        return {'ok':False,'code':'EVIDENCE_REQUIRED','message':'只接受服务端采集的证据引用，不接受自填 evidence'}
    resolved=manual_publish_evidence(layout,item_id,account_id)
    if not resolved['ok']: return resolved
    if evidence_ref!=resolved['evidence_ref']:
        return {'ok':False,'code':'EVIDENCE_INVALID','message':'证据引用不匹配账号、素材或提交尝试'}
    proof=resolved['evidence']; mid=proof['receipt']['work_id']
    row=next(r for r in state['unplaced'] if str(r.get('item_id') or r.get('id'))==str(item_id))
    if row.get('published') or any(r.get('platform_work_id')==mid or r.get('evidence_ref')==evidence_ref
                                 for r in state.get('manual_ledger',[])):
        return {'ok':False,'code':'DUPLICATE_MANUAL','message':'素材或平台作品已记账，不能重复或跨素材重放'}
    now=now or _utcnow()
    settings=settings if settings is not None else load_schedule_settings(layout)
    local_key=now.astimezone(resolve_timezone(settings)).date().isoformat()
    row.update(published=True,published_at=_iso(now),published_account_id=account_id,
               published_fingerprint=row['fingerprint'])
    state.setdefault('manual_ledger',[]).append(dict(item_id=str(item_id),account_id=account_id,
        platform_work_id=mid,verified=True,evidence=proof,evidence_ref=evidence_ref,
        at=_iso(now),local_date=local_key,actor=actor or 'customer'))
    counts=state.setdefault('published',{}).setdefault(str(account_id),{})
    counts[local_key]=int(counts.get(local_key,0))+1
    save_cadence(layout,state)
    return {'ok':True,'item_id':str(item_id),'account_id':account_id,'count':counts[local_key],'row':dict(row)}


def _public_task(task):
    out = dict(task)
    out["status_label"] = STATUS_LABELS.get(task.get("status"),
                                            task.get("status") or "已排期")
    if task.get("needs_reapproval") and task.get("status") == SCHEDULED_STATUS:
        out["status_label"] = "\u9700\u91cd\u65b0\u5ba1\u6279"
    return out


def _find_task(state, task_id):
    for task in state.get("tasks") or []:
        if task.get("task_id") == task_id:
            return task
    return None


def _find_unplaced(state, item_id):
    for row in state.get("unplaced") or []:
        if str(row.get("item_id") or row.get("id")) == str(item_id):
            return row
    return None


def _material_targets(targets):
    if isinstance(targets, str):
        targets = [targets]
    if not isinstance(targets, (list, tuple)):
        raise ValueError("targets must be a list")
    out = []
    for raw in targets:
        if isinstance(raw, dict):
            kind = str(raw.get("kind") or "")
            item_id = str(raw.get("id") or "")
        else:
            text = str(raw or "")
            kind, item_id = "", text
            for prefix in ("task", "unplaced", "job"):
                marker = prefix + ":"
                if text.startswith(marker):
                    kind, item_id = prefix, text[len(marker):]
                    break
        if item_id:
            out.append((kind, item_id))
    return out


def _queue_job_path(layout, job_id):
    text = str(job_id or "")
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-_"
    if not text or any(char not in allowed for char in text.lower()):
        return None
    path = (Path(layout.run) / "publish-queue" / (text + ".json")).resolve()
    try:
        path.relative_to((Path(layout.run) / "publish-queue").resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def _load_queue_job(layout, job_id):
    path = _queue_job_path(layout, job_id)
    if path is None:
        return None, None
    payload = read_json(path, default=None)
    if not isinstance(payload, dict) or not isinstance(payload.get("plan"), dict):
        return None, path
    return payload, path


def _resolve_material(layout, state, kind, item_id):
    if kind == "task":
        return "task", _find_task(state, item_id), None
    if kind == "unplaced":
        return "unplaced", _find_unplaced(state, item_id), None
    if kind == "job":
        job, path = _load_queue_job(layout, item_id)
        return "job", job, path
    task = _find_task(state, item_id)
    if task is not None:
        return "task", task, None
    row = _find_unplaced(state, item_id)
    if row is not None:
        return "unplaced", row, None
    job, path = _load_queue_job(layout, item_id)
    if job is not None:
        return "job", job, path
    return "", None, None


def _plan_topics(plan):
    if not isinstance(plan, dict):
        return []
    piece = plan.get("piece")
    if not isinstance(piece, dict):
        return []
    return normalize_topics(piece.get("topics"))


def _record_topics(record):
    if "topics" in record:
        return normalize_topics(record.get("topics"))
    return _plan_topics(record.get("plan"))


def _apply_plan_topics(plan, topics):
    piece = plan.get("piece")
    if not isinstance(piece, dict):
        return False
    old = normalize_topics(piece.get("topics"))
    piece["topics"] = list(topics)
    compliance = plan.get("compliance")
    if isinstance(compliance, dict) and (piece.get("sha256") or
                                         piece.get("media_sha256")):
        from .idempotency import material_fingerprint
        compliance["content_hash"] = material_fingerprint(
            piece, account_id=plan.get("account_id", ""))
    return old != list(topics)


def _safe_plan_path(layout, raw):
    if not raw:
        return None
    path = Path(str(raw))
    try:
        if path.is_absolute():
            resolved = path.resolve()
        else:
            resolved = (Path(layout.data) / path).resolve()
            resolved.relative_to(Path(layout.data).resolve())
    except (OSError, ValueError):
        return None
    if resolved.suffix.lower() != ".json" or not resolved.is_file():
        return None
    return resolved


def _rebuild_job_preview(layout, plan, settings):
    from .dryrun import build_preview
    from .platforms import load_registry
    envs, channels = load_registry(layout)
    return build_preview(plan, envs=envs, channels=channels,
                         settings=settings)


def _write_plan_file(layout, path, topics, *, now, settings):
    payload = read_json(path, default=None)
    if not isinstance(payload, dict):
        return {"changed": False, "updated": False, "error": "plan unreadable"}
    nested = isinstance(payload.get("plan"), dict) and (
        "job_id" in payload or "preview" in payload or "receipt" in payload)
    plan = payload.get("plan") if nested else payload
    if not isinstance(plan, dict):
        return {"changed": False, "updated": False, "error": "plan unreadable"}
    changed = _apply_plan_topics(plan, topics)
    now_iso = _iso(now)
    if nested:
        status = str(payload.get("status") or "")
        if status in ("running", "needs_reconcile", "published"):
            return {"changed": changed, "updated": False,
                    "locked": status, "error": "job is not editable"}
        payload["approval"] = None
        payload["approval_invalidated_at"] = now_iso
        payload["topics_reapproval_required"] = True
        if status in ("queued", "pending", "blocked"):
            payload["status"] = "queued"
        try:
            payload["preview"] = _rebuild_job_preview(layout, plan, settings)
        except Exception as exc:
            preview = payload.get("preview")
            if not isinstance(preview, dict):
                preview = {}
            preview["ok"] = False
            preview["confirm_token"] = None
            preview["idempotency_key"] = None
            preview["problems"] = list(preview.get("problems") or []) + [{
                "code": "TOPICS_CHANGED_REAPPROVE",
                "field": "piece.topics",
                "message": "topics changed; rebuild the preview and approval"}]
            payload["preview"] = preview
            payload["preview_rebuild_error"] = f"{type(exc).__name__}: {exc}"
    payload["topics_updated_at"] = now_iso
    write_json(path, payload)
    return {"changed": changed, "updated": True}


def _write_record_plan(layout, record, topics, *, now, settings):
    changed = False
    updated = 0
    errors = []
    plan = record.get("plan")
    if isinstance(plan, dict) and isinstance(plan.get("piece"), dict):
        changed = _apply_plan_topics(plan, topics) or changed
        updated += 1
    paths = []
    for key in ("plan_path", "plan_file"):
        path = _safe_plan_path(layout, record.get(key))
        if path is not None:
            paths.append(path)
    job_id = record.get("job_id") or record.get("queue_job_id")
    if job_id:
        queue_path = _safe_plan_path(
            layout, Path("run") / "publish-queue" / (str(job_id) + ".json"))
        if queue_path is not None:
            paths.append(queue_path)
    seen = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result = _write_plan_file(layout, path, topics, now=now,
                                  settings=settings)
        changed = bool(result.get("changed")) or changed
        if result.get("updated"):
            updated += 1
        if result.get("error"):
            errors.append({"path": key, "error": result["error"]})
    return {"changed": changed, "updated": updated, "errors": errors}


def _reset_task_approval(task):
    changed = False
    for key in ("approval_id", "approval_status", "confirm_sent_at",
                "batch_confirm", "decided_at", "batch_decision"):
        if key in task:
            task.pop(key, None)
            changed = True
    if task.get("status") in (PENDING_STATUS, CONFIRMED_STATUS):
        task["status"] = SCHEDULED_STATUS
        changed = True
    if changed:
        task["needs_reapproval"] = True
    return changed


def _invalidate_batch_approval(state, task_ids):
    batch = state.get("batch_approval") or {}
    batch_ids = {str(value) for value in (batch.get("task_ids") or [])}
    if not (batch_ids & set(task_ids)):
        return False
    for task_id in batch_ids:
        task = _find_task(state, task_id)
        if task is not None:
            _reset_task_approval(task)
    state["batch_approval"] = {}
    return True


def update_topics(layout, targets, value, *, actor="customer", now=None):
    """Set one material's topics, or the same topics on selected materials.

    The cadence task/unplaced row is the durable customer-facing state.  When a
    material carries a plan (inline, a plan path, or a queue job), the same
    ``piece.topics`` is written there.  Any changed content resets the old
    confirmation and marks the material for re-approval; approval tokens are
    never silently reused.
    """
    topics = normalize_topics(value)
    target_rows = _material_targets(targets)
    if not target_rows:
        return {"ok": False, "code": "NO_TARGETS",
                "message": "\u8bf7\u5148\u9009\u62e9\u8981\u4fee\u6539\u7684\u7d20\u6750"}
    now = now or _utcnow()
    settings = load_schedule_settings(layout)
    state = load_cadence(layout)
    updated = 0
    changed_count = 0
    plans_updated = 0
    plan_errors = []
    not_found = []
    locked = []
    task_ids = set()
    changed_ids = set()
    before = {}
    after = {}
    for kind, item_id in target_rows:
        kind, record, path = _resolve_material(layout, state, kind, item_id)
        if record is None:
            not_found.append(item_id)
            continue
        if kind == "job":
            old = _plan_topics(record.get("plan"))
            plan_result = _write_plan_file(layout, path, topics, now=now,
                                           settings=settings)
            if plan_result.get("locked"):
                locked.append({"target": "job:" + item_id,
                               "status": plan_result["locked"]})
                continue
            if plan_result.get("error") and not plan_result.get("updated"):
                plan_errors.append({"target": "job:" + item_id,
                                    "error": plan_result["error"]})
                continue
            plans_updated += 1 if plan_result.get("updated") else 0
            did_change = old != list(topics) or plan_result.get("changed")
            before["job:" + item_id] = old
            after["job:" + item_id] = list(topics)
            updated += 1
            if did_change:
                changed_count += 1
                changed_ids.add("job:" + item_id)
            continue
        old = _record_topics(record)
        plans = []
        inline = record.get("plan")
        if isinstance(inline, dict):
            plans.append(_plan_topics(inline))
        record["topics"] = list(topics)
        record["topics_updated_at"] = _iso(now)
        record["topics_updated_by"] = actor or "customer"
        plan_result = _write_record_plan(layout, record, topics, now=now,
                                         settings=settings)
        plans_updated += plan_result.get("updated", 0)
        plan_errors.extend(plan_result.get("errors") or [])
        did_change = old != list(topics) or plan_result.get("changed") or any(
            candidate != list(topics) for candidate in plans)
        before[item_id] = old
        after[item_id] = list(topics)
        updated += 1
        if did_change:
            changed_count += 1
            changed_ids.add(item_id)
            if kind == "task":
                task_ids.add(str(record.get("task_id") or item_id))
                _reset_task_approval(record)
            else:
                record["needs_reapproval"] = True
    batch_invalidated = _invalidate_batch_approval(state, task_ids)
    reapproval = bool(changed_count or batch_invalidated)
    if updated:
        state.setdefault("settings_log", []).append({
            "at": _iso(now), "actor": actor or "customer",
            "before": {"topics": before},
            "after": {"topics": after},
            "reason": "topics_changed" if reapproval else "topics_saved",
            "reapproval_required": reapproval,
        })
        save_cadence(layout, state)
    if not updated:
        return {"ok": False,
                "code": "TARGET_LOCKED" if locked else "TARGET_NOT_FOUND",
                "message": ("\u7d20\u6750\u6b63\u5728\u53d1\u5e03\u6216\u5df2\u53d1\u5e03\uff0c"
                            "\u4e0d\u80fd\u6539\u8bdd\u9898" if locked else
                            "\u627e\u4e0d\u5230\u8981\u4fee\u6539\u7684\u7d20\u6750"),
                "not_found": not_found, "locked": locked, "plan_errors": plan_errors}
    if not topics:
        message = ("\u5df2\u6e05\u7a7a\u8bdd\u9898\uff1b\u53d1\u5e03\u65f6"
                   "\u4e0d\u586b\u8bdd\u9898")
    elif reapproval:
        message = ("\u5df2\u4fdd\u5b58\u8bdd\u9898\uff1b\u5185\u5bb9\u5df2"
                   "\u53d8\uff0c\u9700\u91cd\u65b0\u5ba1\u6279")
    else:
        message = "\u5df2\u4fdd\u5b58\u8bdd\u9898"
    return {"ok": True, "topics": topics, "updated": updated,
            "changed": changed_count, "not_found": not_found,
            "batch": len(target_rows) > 1,
            "reapproval_required": reapproval,
            "batch_approval_invalidated": batch_invalidated,
            "plans_updated": plans_updated, "plan_errors": plan_errors,
            "locked": locked,
            "message": message}


def _task_from_slot(slot, now):
    task = {
        "task_id": str(slot.get("item_id") or slot.get("account_id")),
        "account_id": slot.get("account_id"),
        "schedule_at": slot.get("scheduled_at"),
        "title": slot.get("title"),
        "file_name": slot.get("file_name"),
        "fingerprint": slot.get("fingerprint") or slot.get("sha256"),
        "piece_id": slot.get("piece_id"),
        "copy_body": slot.get("copy_body"),
        "manual": bool(slot.get("manual")),
        "manual_override": bool(slot.get("manual_override")),
        "status": SCHEDULED_STATUS,
        "postpone_count": 0,
        "created_at": _iso(now),
        "updated_at": _iso(now),
    }
    for field in ("piece_id", "copy_body", "topics", "plan", "plan_path",
                  "plan_file", "job_id", "queue_job_id", "env_id", "platform",
                  "platform_account_id"):
        if field in slot:
            task[field] = slot[field]
    return task


def _unplaced_rows(unplaced, now):
    rows = []
    for entry in unplaced or []:
        row = {
            "item_id": str(entry.get("item_id") or entry.get("id") or ""),
            "account_id": entry.get("account_id"),
            "title": entry.get("title"),
            "file_name": entry.get("file_name"),
            "fingerprint": entry.get("fingerprint") or entry.get("sha256"),
            "reason": entry.get("reason") or "排不下，转人工",
            "created_at": _iso(now),
        }
        for field in ("piece_id", "copy_body", "topics", "plan", "plan_path",
                      "plan_file", "job_id", "queue_job_id", "env_id",
                      "platform", "platform_account_id"):
            if field in entry:
                row[field] = entry[field]
        rows.append(row)
    return rows


def schedule_tasks(layout, items, *, seed=None, actor="customer", settings=None,
                   now=None):
    """Replace future random tasks with a freshly validated schedule."""
    settings = settings if settings is not None else load_schedule_settings(layout)
    state = load_cadence(layout)
    result = _schedule_items(items, settings, seed=seed, now=now)
    now = _utcnow(now)
    tasks = [_task_from_slot(slot, now) for slot in result.get("slots") or []]
    state["tasks"] = tasks
    state["unplaced"] = _unplaced_rows(result.get("unplaced") or [], now)
    save_cadence(layout, state)
    return result


def _schedule_items(items, settings, *, seed=None, now=None):
    from .schedule import pick_slots
    now = _utcnow(now)
    return pick_slots(items, now, settings=settings, seed=seed)


def set_manual_time(layout, task_id, schedule_at, *, actor="customer",
                    override=False, settings=None):
    settings = settings if settings is not None else load_schedule_settings(layout)
    state = load_cadence(layout)
    task = _find_task(state, task_id)
    if task is None:
        return {"ok": False, "code": "TASK_NOT_FOUND", "message": "找不到任务"}
    assigned = [{"account_id": other.get("account_id"),
                 "scheduled_at": other.get("schedule_at")}
                for other in state.get("tasks") or []
                if other.get("task_id") != task_id and other.get("schedule_at")]
    verdict = validate_manual_time(
        schedule_at, _utcnow(), settings=settings,
        account_id=task.get("account_id"), assigned=assigned)
    outside = verdict.get("code") == "OUTSIDE_WINDOW"
    if not verdict.get("ok") and not (outside and override):
        verdict["task_id"] = task_id
        return verdict
    before = {"task_id": task_id, "schedule_at": task.get("schedule_at")}
    task["schedule_at"] = _iso(schedule_at)
    task["manual"] = True
    task["manual_override"] = bool(outside and override)
    task["status"] = SCHEDULED_STATUS
    task["postpone_count"] = 0
    task["updated_at"] = _iso(_utcnow())
    state.setdefault("settings_log", []).append({
        "at": task["updated_at"], "actor": actor or "customer",
        "before": before,
        "after": {"schedule_at": task["schedule_at"],
                  "manual_override": task["manual_override"]},
    })
    save_cadence(layout, state)
    return {"ok": True, "task": _public_task(task)}


def cancel_task(layout, task_id, *, actor="customer"):
    state = load_cadence(layout)
    task = _find_task(state, task_id)
    if task is None:
        return {"ok": False, "code": "TASK_NOT_FOUND", "message": "找不到任务"}
    task["status"] = CANCEL_STATUS
    task["updated_at"] = _iso(_utcnow())
    save_cadence(layout, state)
    return {"ok": True, "task": _public_task(task)}


def published_today(layout, account_id, *, now=None, settings=None):
    """Count confirmed publishes for one account on the local date.

    A page-confirmed publish without a work ID stays SUBMIT_INTENT in the
    ledger (so it cannot replay), but it still consumed a daily publish slot.
    """
    now = now or _utcnow()
    settings = settings if settings is not None else load_schedule_settings(layout)
    raw = read_json(layout.stage2_ledger, default={})
    entries = raw.get("entries", {}) if isinstance(raw, dict) else {}
    local_now = now.astimezone(resolve_timezone(settings))
    count = 0
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        confirmed_without_receipt = (
            entry.get("state") == "SUBMIT_INTENT"
            and entry.get("publish_confirmed") is True)
        if entry.get("state") != "CONSUMED" and not confirmed_without_receipt:
            continue
        meta_account = entry.get("account_id")
        if meta_account is None:
            meta_account = str(key).split("|", 1)[0]
        if meta_account != account_id:
            continue
        schedule_at = entry.get("schedule_at")
        if not schedule_at:
            continue
        try:
            when = parse_time(schedule_at).astimezone(resolve_timezone(settings))
        except (TypeError, ValueError):
            continue
        if when.date() == local_now.date():
            count += 1
    return count


def _state_published_today(state, account_id, now, settings):
    key = now.astimezone(resolve_timezone(settings)).date().isoformat()
    published = state.get("published") or {}
    account_data = published.get(account_id) or {}
    return int(account_data.get(key, 0))


def record_published(layout, account_id, *, now=None, settings=None):
    settings = settings if settings is not None else load_schedule_settings(layout)
    now = now or _utcnow()
    state = load_cadence(layout)
    key = now.astimezone(resolve_timezone(settings)).date().isoformat()
    published = state.setdefault("published", {})
    account_data = published.setdefault(str(account_id), {})
    account_data[key] = int(account_data.get(key, 0)) + 1
    save_cadence(layout, state)
    return account_data[key]


def record_backoff(layout, account_id, *, reason="RATE_LIMITED", now=None,
                   settings=None, notify=None):
    settings = settings if settings is not None else load_schedule_settings(layout)
    now = now or _utcnow()
    state = load_cadence(layout)
    entry = state.setdefault("backoff", {}).setdefault(
        str(account_id), {"failures": 0, "next_allowed_at": None,
                          "reason": reason})
    previous_failures = int(entry.get("failures", 0))
    entry["failures"] = previous_failures + 1
    delay = backoff_delay(previous_failures, settings)
    next_allowed = now + timedelta(minutes=delay)
    entry["next_allowed_at"] = _iso(next_allowed)
    entry["reason"] = reason
    entry["updated_at"] = _iso(now)
    pushed = 0
    for task in state.get("tasks") or []:
        if str(task.get("account_id") or "") != str(account_id):
            continue
        if task.get("status") not in (SCHEDULED_STATUS, PENDING_STATUS,
                                      POSTPONED_STATUS):
            continue
        try:
            at = parse_time(task.get("schedule_at"))
        except (TypeError, ValueError):
            continue
        shifted_at = at + timedelta(minutes=delay)
        if shifted_at < next_allowed:
            shifted_at = next_allowed
        task["schedule_at"] = shifted_at.isoformat()
        task["backoff_applied_at"] = _iso(now)
        task["backoff_delay_minutes"] = delay
        task["updated_at"] = _iso(now)
        pushed += 1
    entry["pushed_tasks"] = pushed
    if reason in ("LOGIN_INVALID", "CAPTCHA"):
        entry["paused"] = True
        entry["pause_reason"] = reason
    if entry["failures"] >= 3:
        entry["paused"] = True
        entry["pause_reason"] = reason
    if notify:
        if reason == "LOGIN_INVALID":
            notify(f"{account_id} 登录失效，已停止该号排期",
                   "请到面板检查登录状态，确认后手动恢复。")
        elif entry["failures"] >= 3:
            notify(f"{account_id} 连续失败，已暂停该号排期",
                   f"已连续失败 {entry['failures']} 次，请到面板检查。")
        elif reason == "RATE_LIMITED":
            notify(f"{account_id} 遇到限流，后续排期已推后",
                   f"本次退避 {delay} 分钟，已推后 {pushed} 条排期。")
    save_cadence(layout, state)
    return dict(entry)


def reset_backoff(layout, account_id):
    state = load_cadence(layout)
    state.setdefault("backoff", {}).pop(str(account_id), None)
    save_cadence(layout, state)


def next_available_after(layout, after, account_id, *, settings=None, now=None):
    settings = settings if settings is not None else load_schedule_settings(layout)
    cfg = normalize_schedule_settings(settings)
    tz = resolve_timezone(settings)
    after = parse_time(after).astimezone(tz)
    now = parse_time(now).astimezone(tz) if now is not None else _utcnow()
    horizon = after + timedelta(days=cfg["max_horizon_days"])
    point = max(after + timedelta(minutes=1),
                now + timedelta(minutes=cfg["min_lead_minutes"] + 1))
    point = point.replace(second=0, microsecond=0)
    if point <= now + timedelta(minutes=cfg["min_lead_minutes"]):
        point += timedelta(minutes=1)
    while point <= horizon:
        verdict = validate_manual_time(
            point, now, settings=settings, account_id=account_id,
            assigned=[])
        if verdict.get("ok"):
            return point
        point += timedelta(minutes=1)
    return None


def _task_fingerprint(task):
    return task.get("fingerprint") or task.get("sha256") or ("0" * 64)


def _batch_key(tasks):
    payload = []
    for task in tasks:
        task_id = str(task.get("task_id") or task.get("id") or "")
        if not task_id:
            continue
        payload.append({
            "task_id": task_id,
            "topics": normalize_topics(task.get("topics")),
            "fingerprint": _task_fingerprint(task),
            "title": task.get("title") or "",
            "schedule_at": task.get("schedule_at") or "",
        })
    payload.sort(key=lambda row: row["task_id"])
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                       sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")
                            ).hexdigest()[:24]
    return f"cadence:batch:{digest}"


def _task_card_key(task):
    task_id = str(task.get("task_id") or "")
    payload = {
        "task_id": task_id,
        "topics": normalize_topics(task.get("topics")),
        "fingerprint": _task_fingerprint(task),
        "title": task.get("title") or "",
        "schedule_at": task.get("schedule_at") or "",
    }
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                       sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")
                            ).hexdigest()[:24]
    return f"cadence:{task_id}:{digest}"


def send_batch_confirmation_card(layout, tasks, settings, *, now=None):
    """Send ONE Feishu summary card for a group of today's publish tasks.

    Publishing remains automatic; the card is only a one-click consent step.
    Single-item cards remain available through ``send_confirmation_card``.
    """
    now = now or _utcnow()
    tasks = [task for task in (tasks or []) if task]
    if not tasks:
        return {"ok": False, "status": "empty", "reason": "没有可批量确认的任务"}
    cfg = settings.get("feishu_approval") or {}
    if not cfg.get("enabled", True):
        return {"ok": False, "status": "disabled", "reason": "审批服务已关闭"}
    key = _batch_key(tasks)
    first_schedule = min(
        (str(task.get("schedule_at") or "") for task in tasks if task.get("schedule_at")),
        default="")
    rows = []
    for task in tasks:
        fingerprint = _task_fingerprint(task)
        rows.append({
            "task_id": str(task.get("task_id") or ""),
            "account_id": task.get("account_id"),
            "account": task.get("account_id"),
            "media_sha256": fingerprint,
            "title": task.get("title") or "漫剧发布任务",
            "topics": normalize_topics(task.get("topics")),
            "schedule_at": task.get("schedule_at"),
        })
    plan = {
        "plan_id": key,
        "account_id": rows[0].get("account_id") or "batch",
        "channel_id": rows[0].get("account_id") or "batch",
        "env_id": "batch",
        "platform": "douyin",
        "schedule_at": first_schedule,
        "piece": {
            "title": f"今日要发的 {len(tasks)} 条",
            "topics": [],
            "sha256": rows[0]["media_sha256"],
            "covers": {"vertical": None, "horizontal": None},
        },
    }
    preview = {
        "ok": True,
        "idempotency_key": key,
        "confirm_token": key,
        "env": "batch",
        "env_label": "今日批量发布",
        "platform": "douyin",
        "platform_name": "抖音",
        "title": f"今日要发的 {len(tasks)} 条",
        "schedule_at": first_schedule,
        "topics": [],
        "rows": rows,
    }
    try:
        from . import feishu_approval
        record = feishu_approval.submit_pending(layout, plan, preview, settings,
                                                now=now)
    except Exception as exc:
        return {"ok": False, "status": "submit_failed", "reason": str(exc)}
    state = load_cadence(layout)
    state["batch_approval"] = {
        "approval_id": record.get("approval_id"),
        "status": record.get("status"),
        "key": key,
        "task_ids": [row["task_id"] for row in rows if row.get("task_id")],
        "rows": rows,
        "created_at": _iso(now),
        "updated_at": _iso(now),
    }
    save_cadence(layout, state)
    return {"ok": record.get("status") in ("pending", "confirmed"),
            "status": record.get("status"),
            "reason": record.get("reason"),
            "task_count": len(tasks)}


def refresh_batch_approval(layout, settings, *, now=None):
    state = load_cadence(layout)
    batch = state.get("batch_approval") or {}
    approval_id = batch.get("approval_id")
    if not approval_id:
        return {"ok": False, "status": "missing", "reason": "没有批量确认单"}
    try:
        from . import feishu_approval
        record = feishu_approval.load_record(layout, approval_id)
        if not record:
            record = {"approval_id": approval_id, "status": batch.get("status")}
        record = feishu_approval.refresh(layout, record, settings, now=now)
    except Exception as exc:
        return {"ok": False, "status": "submit_failed", "reason": str(exc)}
    status = record.get("status")
    if status in ("confirmed", "rejected", "timeout"):
        return confirm_batch(layout, decision=status, now=now, settings=settings,
                             batch_status=status)
    return {"ok": False, "status": status or "pending",
            "reason": record.get("reason")}


def confirm_batch(layout, *, decision="confirmed", now=None, settings=None,
                  batch_status=None):
    """Apply a Feishu batch answer to every task in the summary card."""
    now = now or _utcnow()
    state = load_cadence(layout)
    batch = state.get("batch_approval") or {}
    task_ids = batch.get("task_ids") or []
    if not task_ids:
        return {"ok": False, "code": "BATCH_NOT_FOUND", "message": "找不到批量确认单"}
    if decision == "confirmed":
        target_status = CONFIRMED_STATUS
    else:
        target_status = VOID_STATUS
    changed = 0
    for task_id in task_ids:
        task = _find_task(state, task_id)
        if task is None:
            continue
        if task.get("status") in (CANCEL_STATUS, VOID_STATUS, CONFIRMED_STATUS):
            continue
        task["status"] = target_status
        task["decided_at"] = _iso(now)
        task["batch_decision"] = decision
        task.pop("needs_reapproval", None)
        task["updated_at"] = _iso(now)
        changed += 1
    batch["status"] = batch_status or decision
    batch["decided_at"] = _iso(now)
    batch["updated_at"] = _iso(now)
    state["batch_approval"] = batch
    save_cadence(layout, state)
    return {"ok": True, "decision": decision, "changed": changed,
            "status": batch["status"]}


def send_confirmation_card(layout, task, settings, *, now=None):
    """Best-effort Feishu card submission for a cadence task."""
    now = now or _utcnow()
    cfg = settings.get("feishu_approval") or {}
    if not cfg.get("enabled", True):
        return {"ok": False, "status": "disabled", "reason": "审批服务已关闭"}
    try:
        from . import feishu_approval
        task_id = str(task.get("task_id") or "")
        fingerprint = _task_fingerprint(task)
        topics = normalize_topics(task.get("topics"))
        plan = {
            "plan_id": task_id,
            "account_id": task.get("account_id"),
            "channel_id": task.get("account_id"),
            "env_id": task.get("env_id") or task.get("account_id"),
            "platform": "douyin",
            "schedule_at": task.get("schedule_at"),
            "piece": {"title": task.get("title") or "漫剧发布任务",
                      "topics": topics, "sha256": fingerprint,
                      "covers": {"vertical": None, "horizontal": None}},
        }
        card_key = _task_card_key(task)
        preview = {
            "ok": True,
            "idempotency_key": card_key,
            "confirm_token": card_key,
            "env": task.get("env_id") or task.get("account_id"),
            "env_label": task.get("account_id"),
            "platform": "douyin",
            "platform_name": "抖音",
            "title": task.get("title") or "漫剧发布任务",
            "topics": topics,
            "rows": [{"account_id": task.get("account_id"),
                      "media_sha256": fingerprint,
                      "account": task.get("account_id"),
                      "topics": topics}],
        }
        record = feishu_approval.submit_pending(
            layout, plan, preview, settings, now=now)
        return {"ok": record.get("status") in ("pending", "confirmed"),
                "status": record.get("status"), "reason": record.get("reason")}
    except Exception as exc:
        return {"ok": False, "status": "submit_failed", "reason": str(exc)}


def advance_confirmation(layout, task_id, *, decision, now=None, settings=None,
                         notify=None):
    """Confirm, postpone once, or void an unconfirmed manual task."""
    settings = settings if settings is not None else load_schedule_settings(layout)
    now = now or _utcnow()
    state = load_cadence(layout)
    task = _find_task(state, task_id)
    if task is None:
        return {"ok": False, "code": "TASK_NOT_FOUND", "message": "找不到任务"}
    if decision == "confirmed":
        task["status"] = CONFIRMED_STATUS
        task["decided_at"] = _iso(now)
        task.pop("needs_reapproval", None)
    else:
        if int(task.get("postpone_count") or 0) >= 1:
            task["status"] = VOID_STATUS
            task["voided_at"] = _iso(now)
            if notify:
                notify(f"{task.get('account_id')} 发布任务已作废",
                       "到点仍未确认，任务已作废")
        else:
            next_at = next_available_after(layout, task.get("schedule_at"),
                                           task.get("account_id"),
                                           settings=settings, now=now)
            if next_at is None:
                task["status"] = VOID_STATUS
                task["voided_at"] = _iso(now)
                task["reason"] = "没有可用顺延点，任务作废"
            else:
                task["schedule_at"] = next_at.isoformat()
                task["status"] = POSTPONED_STATUS
                task["postpone_count"] = int(task.get("postpone_count") or 0) + 1
                if notify:
                    notify(f"{task.get('account_id')} 发布任务已顺延",
                           f"到点未确认，顺延到 {next_at.isoformat()}")
    task["updated_at"] = _iso(now)
    save_cadence(layout, state)
    return {"ok": True, "task": _public_task(task)}


def cadence_view(layout, *, settings=None, now=None):
    settings = settings if settings is not None else load_schedule_settings(layout)
    now = now or _utcnow()
    cfg = schedule_config(layout, settings)
    state = load_cadence(layout)
    tasks = [_public_task(task) for task in state.get("tasks") or []
             if task.get("status") not in (CANCEL_STATUS, VOID_STATUS)]
    tasks.sort(key=lambda item: item.get("schedule_at") or "")
    next_at = tasks[0].get("schedule_at") if tasks else None
    account_ids = sorted(set(
        [task.get("account_id") for task in tasks if task.get("account_id")] +
        [str(key) for key in (state.get("published") or {})] +
        [str(entry.get("account_id") or "") for entry in
         (state.get("manual_ledger") or []) if entry.get("account_id")]))
    today_counts = {}
    for account_id in account_ids:
        ledger_count = published_today(layout, account_id, now=now,
                                       settings=settings)
        state_count = _state_published_today(state, account_id, now, settings)
        today_counts[account_id] = max(ledger_count, state_count)
    return {
        "settings": cfg,
        "today": now.astimezone(resolve_timezone(settings)).date().isoformat(),
        "today_published": today_counts,
        "manual_mode": bool(state.get("manual_mode", False)),
        "manual_ledger": list(state.get("manual_ledger") or []),
        "max_per_day_per_account": cfg["max_per_day_per_account"],
        "min_gap_minutes": cfg["min_gap_minutes"],
        "cross_account_gap_minutes": cfg["cross_account_gap_minutes"],
        "next_publish_at": next_at,
        "tasks": tasks,
        "unplaced": list(state.get("unplaced") or []),
        "backoff": dict(state.get("backoff") or {}),
        "settings_log": list(state.get("settings_log") or [])[-20:],
    }


def run_confirmation_sweep(layout, *, now=None, settings=None, notify=None):
    """Send 10-minute cards and progress unconfirmed tasks.

    The default path sends one batch summary card for all tasks whose
    confirmation window opens in this sweep; single-task mode still uses the
    original per-task card.  The batch answer is then applied to every listed
    task, so the customer clicks once instead of once per item.
    """
    settings = settings if settings is not None else load_schedule_settings(layout)
    now = now or _utcnow()
    state = load_cadence(layout)
    tasks = state.get("tasks") or []
    due_for_card = []
    for task in tasks:
        if task.get("status") not in (SCHEDULED_STATUS, PENDING_STATUS):
            continue
        if task.get("confirm_sent_at"):
            continue
        try:
            at = parse_time(task.get("schedule_at"))
        except (TypeError, ValueError):
            continue
        if at - timedelta(minutes=10) <= now:
            due_for_card.append(task)

    changed = False
    if len(due_for_card) > 1:
        result = send_batch_confirmation_card(layout, due_for_card, settings,
                                               now=now)
        for task in due_for_card:
            task["confirm_sent_at"] = _iso(now)
            task["approval_status"] = result.get("status")
            task["batch_confirm"] = True
            task["status"] = PENDING_STATUS
        save_cadence(layout, state)
    elif len(due_for_card) == 1:
        task = due_for_card[0]
        result = send_confirmation_card(layout, task, settings, now=now)
        task["confirm_sent_at"] = _iso(now)
        task["approval_status"] = result.get("status")
        task["status"] = PENDING_STATUS
        save_cadence(layout, state)

    refresh_batch_approval(layout, settings, now=now)

    state = load_cadence(layout)
    tasks = state.get("tasks") or []
    for task in tasks:
        if task.get("status") != PENDING_STATUS:
            continue
        try:
            at = parse_time(task.get("schedule_at"))
        except (TypeError, ValueError):
            continue
        if now >= at:
            advance_confirmation(layout, task.get("task_id"),
                                 decision="unconfirmed", now=now,
                                 settings=settings, notify=notify)
            state = load_cadence(layout)
            tasks = state.get("tasks") or []

    return cadence_view(layout, settings=settings, now=now)
