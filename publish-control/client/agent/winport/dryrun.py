"""A · 发布前 dry-run：把「将要发布什么」做成可校验、可确认的清单。

source/design-refs-mainstream.md A: 蚁小二等已做「发布前 validate/dry-run」。
这一模块给客户 / 我们的运营看「这一步到底要发什么」，并且：

  1. 逐字段 schema 校验（环境 / 平台 / 标题 / 话题 / 封面 / 计划时间 / 素材指纹）；
  2. 校验不通过 → 明确列出每条原因，命令非 0 退出，**绝不允许硬发**；
  3. 产出一个稳定的 ``confirm_token``，飞书确认按钮直接引用这个 token（而不是
     引用一个会被时间戳改动的全文），运营点「确认发布」时带回同一个 token，
     发布侧用它比对「被确认的就是被校验通过的那份清单」。

清单默认落盘到 ``<Data>/run/publish-preview.json``（机器可读）与
``<Data>/run/publish-preview.md``（人看）。dry-run 不启动浏览器、不落登录态、
不点发布键。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .hashing import is_hex, sha256_bytes
from .idempotency import (DEFAULT_WINDOW_MINUTES, material_fingerprint,
                          time_window_bucket)
from .schedule import validate_window

SCHEMA = "publish-dryrun/1.0"
PREVIEW_JSON = "publish-preview.json"
PREVIEW_MD = "publish-preview.md"


def _parse_time(value):
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _canonical(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def resolve_targets(plan, envs, channels):
    """(env, channel) for a plan; raises ValueError with a human reason."""
    env_id = plan.get("env_id")
    channel_id = plan.get("channel_id")
    account_id = plan.get("account_id")
    platform = plan.get("platform")

    if not env_id and channel_id and ":" in str(channel_id):
        env_id = str(channel_id).split(":", 1)[0]
    if not env_id and account_id and ":" in str(account_id):
        env_id = str(account_id).split(":", 1)[0]

    channel = None
    if channel_id:
        channel = next((c for c in channels if c.channel_id == channel_id), None)
    if channel is None and account_id:
        channel = next((c for c in channels if c.account_id == account_id), None)
    if channel is None and env_id and platform:
        channel = next((c for c in channels
                        if c.env_id == env_id and c.platform == platform), None)
    if channel is None and platform:
        hits = [c for c in channels if c.platform == platform]
        if len(hits) == 1:
            channel = hits[0]
    if channel is None:
        raise ValueError(f"找不到发布目标：env_id={env_id!r} channel_id={channel_id!r} "
                         f"account_id={account_id!r} platform={platform!r}")

    if account_id and channel.account_id != account_id:
        raise ValueError("account_id 与通道不一致")
    if env_id and channel.env_id != env_id:
        raise ValueError("env_id 与通道不一致")
    if platform and channel.platform != platform:
        raise ValueError("platform 与通道不一致")
    env = next((e for e in (envs or []) if e.env_id == channel.env_id), None)
    if env is None:
        env_id = channel.env_id or env_id
    return env, channel


def build_preview(plan: dict, *, envs=None, channels=None, settings=None,
                  license_info=None, now=None,
                  window_minutes: int = DEFAULT_WINDOW_MINUTES, publishing=False) -> dict:
    """Validate + describe a publish plan.  Never raises for bad data: bad data
    comes back as ``problems`` with ``ok=False`` so the caller refuses to send.

    ``license_info`` is an optional dict from ``license.publish_gate`` (E); when
    it says publishing is paused, the preview is REFUSED (not just warned).
    """
    now = now or datetime.now(timezone.utc)
    channels = list(channels or [])
    envs = list(envs or [])
    settings = settings or {}
    problems: list[dict] = []
    warnings: list[dict] = []

    def bad(code, field, message):
        problems.append({"code": code, "field": field, "message": message})

    def warn(code, field, message):
        warnings.append({"code": code, "field": field, "message": message})

    piece = plan.get("piece") or {}
    schedule_at = plan.get("schedule_at")
    title = piece.get("title")
    topics = piece.get("topics")
    covers = piece.get("covers")
    media_sha256 = piece.get("sha256")

    try:
        env, channel = resolve_targets(plan, envs, channels)
    except ValueError as exc:
        env, channel = None, None
        bad("TARGET_UNRESOLVED", "env_id/channel_id", str(exc))

    env_id = env.env_id if env else (plan.get("env_id") or
                                     (str(plan.get("channel_id", "")).split(":", 1)[0]
                                      if ":" in str(plan.get("channel_id", "")) else ""))
    account_id = (channel.account_id if channel else plan.get("account_id")) or ""
    platform = (channel.platform if channel else plan.get("platform")) or ""

    if channel is not None:
        if not channel.enabled:
            bad("CHANNEL_DISABLED", "channel_id",
                f"通道 {channel.channel_id} 已停用（enabled=false），停用按号生效")
        if not channel.url_verified:
            warn("URL_UNVERIFIED", "platform",
                 f"平台 {channel.platform_name} 的后台地址是配置默认值、尚未在真机确认"
                 f"（{channel.url_source or '无来源'}），发布前请在真机核对")
    if env is not None and not env.enabled:
        bad("ENV_DISABLED", "env_id", f"环境 {env.env_id} 已停用")

    # ---- schema: title / topics / cover ----------------------------------
    if not isinstance(title, str) or not title.strip():
        bad("TITLE_MISSING", "piece.title", "标题缺失或为空")
    elif len(title) > 30:
        bad("TITLE_TOO_LONG", "piece.title", f"标题超过 30 字（{len(title)}）")

    if not isinstance(topics, list):
        bad("TOPICS_NOT_LIST", "piece.topics", "话题必须是数组")
    else:
        for index, topic in enumerate(topics):
            if not isinstance(topic, str) or not topic.strip():
                bad("TOPIC_INVALID", f"piece.topics[{index}]", "话题为空或不是字符串")
            elif not topic.lstrip().startswith("#"):
                warn("TOPIC_NO_HASH", f"piece.topics[{index}]",
                     f"话题 {topic!r} 没有以 # 开头，平台可能识别不到")
        if not topics:
            warn("TOPICS_EMPTY", "piece.topics", "话题为空（可以发，但通常建议至少一个）")

    if not isinstance(covers, dict):
        bad("COVER_NOT_OBJECT", "piece.covers", "封面字段必须是对象（vertical/horizontal）")
        has_cover = False
    else:
        has_cover = bool(covers.get("vertical") or covers.get("horizontal"))
        if not has_cover:
            warn("COVER_EMPTY", "piece.covers",
                 "封面未设置（vertical/horizontal 都为空）")

    # ---- schema: material fingerprint ------------------------------------
    if not is_hex(media_sha256 or "", 64):
        bad("MATERIAL_SHA256_INVALID", "piece.sha256",
            "素材 sha256 必须是 64 位小写十六进制（缺了它就无法去重/防重复发布）")
    if piece.get("md5") and not is_hex(piece.get("md5"), 32):
        bad("MATERIAL_MD5_INVALID", "piece.md5", "素材 md5 必须是 32 位小写十六进制")

    material_fp = (material_fingerprint(piece, account_id=account_id)
                   if is_hex(media_sha256 or "", 64) else None)

    # ---- schema: schedule time + window ----------------------------------
    window_key = None
    schedule_local = None
    if not schedule_at:
        bad("SCHEDULE_MISSING", "schedule_at", "计划发布时间缺失")
    else:
        try:
            parsed = _parse_time(schedule_at)
            min_lead = int((settings.get("schedule") or {}).get(
                "min_lead_minutes", 3))
            max_horizon = int((settings.get("schedule") or {}).get(
                "max_horizon_days", 14))
            ok, reason = validate_window(parsed, now, min_lead_minutes=min_lead,
                                         max_horizon_days=max_horizon)
            if publishing:
                age = (now - parsed).total_seconds()
                ok, reason = 0 <= age <= 600, "尚未到发布时间或已逾期超过 10 分钟"
            if not ok:
                bad("SCHEDULE_OUT_OF_WINDOW", "schedule_at",
                    f"计划时间不在合法窗口内（{reason}）")
            schedule_local = parsed.isoformat()
            window_key = time_window_bucket(parsed, window_minutes=window_minutes)
        except ValueError:
            bad("SCHEDULE_NOT_ISO8601", "schedule_at",
                "计划时间不是 ISO-8601（带时区）")

    # ---- schema: compliance content hash (approval binding) --------------
    compliance = plan.get("compliance") or {}
    declared = compliance.get("content_hash")
    computed = material_fp
    if declared and computed and declared != computed:
        bad("CONTENT_HASH_MISMATCH", "compliance.content_hash",
            "compliance.content_hash 与按素材重算的结果不一致（审批绑定已失效）")
    if not declared:
        warn("CONTENT_HASH_ABSENT", "compliance.content_hash",
             "没有 compliance.content_hash；如需审批发布（approved_publish）必须先补")

    if isinstance(covers, dict) and any(covers.values()):
        try:
            from .content_binding import validate_local_content
            validate_local_content(plan)
        except Exception as exc:
            bad("COVER_BINDING", "piece.cover_sha256", str(exc))

    # ---- E: licence gate -------------------------------------------------
    license_section = None
    if license_info is not None:
        license_section = {
            "status": license_info.get("status"),
            "publish_allowed": bool(license_info.get("publish_allowed", True)),
            "collect_allowed": bool(license_info.get("collect_allowed", True)),
            "days_left": license_info.get("days_left"),
            "level": license_info.get("level"),
            "notices": list(license_info.get("notices") or []),
        }
        if not license_section["publish_allowed"]:
            bad("LICENCE_BLOCKS_PUBLISH", "license",
                "授权状态不允许新增发布（抓数与历史数据不受影响）："
                + "；".join(license_info.get("reasons") or ["已到期"]))

    row = {
        "env_id": env_id,
        "env_label": (env.label if env else None),
        "channel_id": channel.channel_id if channel else plan.get("channel_id"),
        "platform": platform,
        "platform_name": (channel.platform_name if channel else None),
        "account_id": account_id,
        "title": title,
        "topics": topics,
        "cover": covers,
        "has_cover": has_cover,
        "schedule_at": schedule_at,
        "schedule_local": schedule_local,
        "time_window": window_key,
        "material_fingerprint": material_fp,
        "media_sha256": media_sha256,
        "media_md5": piece.get("md5"),
        "piece_no": piece.get("piece_no"),
        "file_name": piece.get("file_name"),
        "url_verified": bool(channel.url_verified) if channel else False,
    }

    stable = {"schema": SCHEMA, "rows": [row],
              "env_id": env_id, "channel_id": row["channel_id"],
              "material_fingerprint": material_fp, "time_window": window_key,
              "schedule_at": schedule_at, "plan_id": plan.get("plan_id")}
    confirm_token = sha256_bytes(_canonical(stable))

    return {
        "schema": SCHEMA,
        "generated_at": now.isoformat(),
        "ok": not problems,
        "plan_id": plan.get("plan_id"),
        "mode": plan.get("mode", "fill"),
        "env": row["env_id"],
        "env_label": row["env_label"],
        "platform": row["platform"],
        "platform_name": row["platform_name"],
        "title": row["title"],
        "topics": row["topics"],
        "cover": row["cover"],
        "has_cover": row["has_cover"],
        "schedule_at": row["schedule_at"],
        "time_window": row["time_window"],
        "material_fingerprint": row["material_fingerprint"],
        "idempotency_key": (f"{env_id}|{material_fp}|{window_key}"
                            if (env_id and material_fp and window_key) else None),
        "rows": [row],
        "problems": problems,
        "warnings": warnings,
        "license": license_section,
        "confirm_token": confirm_token,
        #: the string a Feishu confirm button carries / echoes back
        "confirm_ref": f"dryrun:{confirm_token[:16]}",
    }


def render_markdown(preview: dict) -> str:
    lines = ["# 发布前确认清单（dry-run）", "",
             f"- 生成时间：{preview['generated_at']}",
             f"- 结论：{'✅ 校验通过，可发飞书确认' if preview['ok'] else '❌ 校验未通过，禁止发布'}",
             f"- 确认 token（飞书按钮引用）：`{preview['confirm_ref']}`",
             f"- 幂等键：`{preview.get('idempotency_key')}`", ""]
    for row in preview["rows"]:
        lines += [
            f"## {row['env_label'] or row['env_id']} · {row['platform_name'] or row['platform']}",
            f"- 环境：`{row['env_id']}`",
            f"- 平台：{row['platform_name'] or row['platform']}（`{row['channel_id']}`）",
            f"- 账号：`{row['account_id']}`",
            f"- 标题：{row['title']}",
            f"- 话题：{'、'.join(row['topics'] or []) or '（无）'}",
            f"- 封面：{'已设置' if row['has_cover'] else '未设置（仅提醒）'}",
            f"- 计划时间：{row['schedule_at']}（时间窗 {row['time_window']}）",
            f"- 素材指纹 sha256：`{row['media_sha256']}`",
            f"- 素材指纹（去重键）：`{row['material_fingerprint']}`",
            "",
        ]
    if preview["problems"]:
        lines.append("## ❌ 阻断项（必须逐条修好才能发布）")
        for problem in preview["problems"]:
            lines.append(f"- `{problem['code']}` {problem['field']}：{problem['message']}")
        lines.append("")
    if preview["warnings"]:
        lines.append("## ⚠️ 提醒项（不阻断）")
        for item in preview["warnings"]:
            lines.append(f"- `{item['code']}` {item['field']}：{item['message']}")
        lines.append("")
    if preview.get("license"):
        lic = preview["license"]
        lines.append(f"## 授权：{lic['status']}（剩余 {lic['days_left']} 天）")
        for notice in lic.get("notices", []):
            lines.append(f"- {notice}")
        lines.append("")
    return "\n".join(lines) + "\n"


def write_preview(layout, preview: dict, *, out=None, out_md=None) -> dict:
    json_path = Path(out) if out else layout.run / PREVIEW_JSON
    md_path = Path(out_md) if out_md else layout.run / PREVIEW_MD
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(preview, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    md_path.write_text(render_markdown(preview), encoding="utf-8")
    return {"json": str(json_path), "md": str(md_path),
            "confirm_token": preview["confirm_token"],
            "confirm_ref": preview["confirm_ref"],
            "ok": preview["ok"]}


def read_preview(layout, *, path=None):
    """Read the stored dry-run preview (None when it has not been produced)."""
    p = Path(path) if path else layout.run / PREVIEW_JSON
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def check_confirmation(preview_stored: dict, plan: dict, *, envs=None,
                       channels=None, settings=None, confirm_token=None,
                       license_info=None, now=None,
                       window_minutes=DEFAULT_WINDOW_MINUTES, publishing=False) -> dict:
    """Re-derive the preview and verify the token the operator confirmed matches.

    A confirmation for a DIFFERENT preview (someone edited the title after the
    confirm button was clicked) must never publish: the token is compared, not
    the file name.
    """
    fresh = build_preview(plan, envs=envs, channels=channels, settings=settings,
                          license_info=license_info, now=now,
                          window_minutes=window_minutes, publishing=publishing)
    stored_token = (preview_stored or {}).get("confirm_token")
    ok = bool(confirm_token) and confirm_token == fresh["confirm_token"] == stored_token
    return {"ok": ok and fresh["ok"], "fresh": fresh,
            "expected_token": stored_token,
            "presented_token": confirm_token,
            "reason": ("确认 token 不一致：清单在确认后被改过，拒绝发布"
                       if not ok else ("确认 token 一致，校验通过" if fresh["ok"] else
                       "；".join(p["message"] for p in fresh["problems"])))}
