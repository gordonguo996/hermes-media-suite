"""Xingtu task (star order) selection, attachment checks and local dedup.

Selection happens inside the Douyin publish page: the caller supplies a task
name and no Xingtu backend list is needed.  Duplicate protection is local:
the publish flow checks the used-task ledger before submit and appends to it
only after a confirmed publish.  Post-publish backend list read-back was
removed because Xingtu itself rejects duplicate submissions.  This module
never contains a task name, order id, demander or customer name.  Task type is
display/filter data only; it is not a code branch.

The pure parsers at the top are used by the Linux test suite.  The live
functions at the bottom drive the already signed-in Chrome page through
Playwright and fail closed.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .errors import GateRed

try:  # POSIX cross-process lock
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:  # Windows cross-process lock
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None

CREATOR_URL = "https://www.xingtu.cn/sup/creator"
STAR_AD_URL = "https://www.xingtu.cn/sup/creator/star-ad-unite"
TASK_MENU_SELECTOR = 'a[href="/sup/user/task"]'
TASK_URL = "https://www.xingtu.cn/sup/creator/user/task"
DOUYIN_UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"
DOUYIN_PUBLISH_URL = "https://creator.douyin.com/creator-micro/content/post/video"

STATUS_FOUND = "FOUND"
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_UNKNOWN = "UNKNOWN"
RESOLVE_OK = "ok"
RESOLVE_NOT_FOUND = "not_found"
RESOLVE_AMBIGUOUS = "ambiguous"
RESOLVE_UNAVAILABLE = "unavailable"
TASK_REF_SCHEMA = 2

#: The one task-family contract shared by the bot and the client.  ``token`` is
#: the only value that may be written as a legacy ``task_name``; ``keywords``
#: are material-name inputs only.  Customer names are deliberately absent.
TASK_FAMILIES = {
    "free": {
        "token": "\u514d\u8d39-\u65b0\u7801",
        "keywords": ("\u514d\u8d39-\u65b0\u7801", "\u514d\u8d39"),
    },
    "incentive": {
        "token": "\u6fc0\u52b1-\u65b0\u7801",
        "keywords": ("\u6fc0\u52b1-\u65b0\u7801", "\u6fc0\u52b1"),
    },
    "app_seed": {
        "token": "APP\u79cd\u8349",
        "keywords": ("APP\u79cd\u8349", "\u79cd\u8349"),
    },
    "drama": {
        "token": "\u5267\u96c6\u526a\u8f91",
        "keywords": ("\u5267\u96c6\u526a\u8f91", "\u5267\u96c6", "\u526a\u8f91"),
    },
}

CLIENT_CAPABILITIES = (
    "xingtu_task_contract_v2",
    "xingtu_picker_preflight_v2",
    "error_envelope_v2",
)

_KNOWN_BAD_TASK_NAME_MARKERS = ("\u4f18\u77e9",)
_ERROR_CODE_ALIASES = {
    "XINGTU.DEDUP.LEDGER_ERROR": "XINGTU_DEDUP_LEDGER_ERROR",
    "XINGTU.DEDUP.LEDGER_LOCK_ERROR": "XINGTU_DEDUP_LEDGER_ERROR",
    "XINGTU.DEDUP.LEDGER_LOCK_TIMEOUT": "XINGTU_DEDUP_LEDGER_ERROR",
}


def normalize_error_code(value) -> str:
    """Return one stable, upper-case error code for all consumers."""
    code = "" if value is None else str(value).strip().upper()
    return _ERROR_CODE_ALIASES.get(code, code)

#: Local ledger of task names we have already published.  It lives under the
#: publish-worker data directory (``<data>/run/xingtu_used_tasks.json``).
USED_TASKS_FILENAME = "xingtu_used_tasks.json"
USED_TASKS_VERSION = 1
USED_TASKS_LOCK_TIMEOUT_SECONDS = 10.0
USED_TASKS_LOCK_POLL_SECONDS = 0.05

_ORDER_RE = re.compile(
    r"(?:order_id|orderId|orderid|order%5Fid|task_id|taskId|taskid)"
    r"[=:%3D]+([A-Za-z0-9_-]{6,64})",
    re.IGNORECASE,
)
_DETAIL_ID_RE = re.compile(r"/detail/([A-Za-z0-9_-]{6,64})")
_DETAIL_ID_LINE_RE = re.compile(
    r"(?:\u4efb\u52a1ID|\u8ba2\u5355\u53f7|\u8ba2\u5355ID|task_id|order_id)"
    r"\s*[:=\uff1a]?\s*([A-Za-z0-9_-]{6,64})",
    re.IGNORECASE,
)
_ACTION_WORDS = (
    "\u67e5\u770b\u8be6\u60c5", "\u53bb\u6295\u7a3f", "\u7acb\u5373\u6295\u7a3f",
    "\u6295\u7a3f", "\u53c2\u4e0e\u4efb\u52a1", "\u7acb\u5373\u53c2\u4e0e",
)
_UNAVAILABLE_WORDS = (
    "\u5df2\u7ed3\u675f", "\u5df2\u4e0b\u7ebf", "\u5df2\u62a2\u5149",
    "\u5df2\u6ee1", "\u65e0\u5269\u4f59", "\u4e0d\u53ef\u6295\u7a3f",
    # 页面实际显示「无法投稿」「状态异常」，与「不可投稿」不是同一个词。
    "\u65e0\u6cd5\u6295\u7a3f", "\u72b6\u6001\u5f02\u5e38",
    "\u5df2\u505c\u6295", "\u65e0\u6cd5\u53c2\u4e0e",
)
# 页面把任务状态直接拼在名字后面（如「...-星川任务-2无法投稿」）。比对前先
# 剥掉这些尾巴，否则同一个任务在计划里和卡片上永远对不上。
_STATUS_SUFFIX_WORDS = (
    "\u65e0\u6cd5\u6295\u7a3f", "\u72b6\u6001\u5f02\u5e38", "\u4e0d\u53ef\u6295\u7a3f",
    "\u5df2\u7ed3\u675f", "\u5df2\u4e0b\u7ebf", "\u5df2\u62a2\u5149",
    "\u5df2\u6ee1", "\u65e0\u5269\u4f59", "\u5df2\u505c\u6295", "\u65e0\u6cd5\u53c2\u4e0e",
)
_EMPTY_SUBMISSION_WORDS = (
    "\u6682\u65e0\u6295\u7a3f", "\u8fd8\u672a\u6295\u7a3f", "\u6682\u65e0\u4f5c\u54c1",
    "\u672a\u6295\u7a3f", "\u65e0\u6295\u7a3f\u8bb0\u5f55",
)
_EMPTY_LIST_WORDS = (
    "\u6682\u65e0\u4efb\u52a1", "\u6682\u65e0\u6570\u636e", "\u6682\u65e0\u5185\u5bb9",
    "\u6ca1\u6709\u4efb\u52a1", "\u6682\u65e0\u76f8\u5173\u4efb\u52a1", "\u4efb\u52a1\u4e3a\u7a7a",
)
_LOGIN_WORDS = (
    "\u626b\u7801\u767b\u5f55", "\u624b\u673a\u53f7\u767b\u5f55", "\u5bc6\u7801\u767b\u5f55",
    "\u767b\u5f55\u661f\u56fe", "\u7acb\u5373\u767b\u5f55", "\u8bf7\u5148\u767b\u5f55",
    "\u8d26\u53f7\u767b\u5f55",
)
_SUBMIT_ACTION_WORDS = ("\u8ffd\u52a0\u6295\u7a3f", "\u4e0a\u4f20\u89c6\u9891",
                        "\u53bb\u4e0a\u4f20")
_DETAIL_ACTION = "\u67e5\u770b\u8be6\u60c5"
_ROW_VIDEO_STATUSES = ("\u89c6\u9891\u5df2\u4e0a\u4f20",
                       "\u5f85\u4e0a\u4f20\u89c6\u9891")
_DETAIL_NAME_LABELS = ("\u521b\u610f\u63cf\u8ff0", "\u4efb\u52a1\u540d\u79f0",
                       "\u4efb\u52a1\u6807\u9898", "\u4efb\u52a1\u540d")

_LABELS = {
    "task_name": ("\u4efb\u52a1\u540d\u79f0", "\u4efb\u52a1\u540d", "\u4efb\u52a1\u6807\u9898",
                  "\u4efb\u52a1"),
    "demander": ("\u9700\u6c42\u65b9", "\u5ba2\u6237\u540d\u79f0", "\u54c1\u724c\u540d\u79f0",
                 "\u5e7f\u544a\u4e3b", "\u5408\u4f5c\u65b9"),
    "task_type": ("\u4efb\u52a1\u7c7b\u578b", "\u4efb\u52a1\u5f62\u5f0f",
                  "\u4efb\u52a1\u73a9\u6cd5", "\u5408\u4f5c\u7c7b\u578b"),
    "remaining_quota": ("\u5269\u4f59\u914d\u989d", "\u5269\u4f59\u540d\u989d",
                        "\u53ef\u6295\u7a3f\u6b21\u6570", "\u5269\u4f59\u53ef\u6295\u7a3f",
                        "\u5269\u4f59\u91cf", "\u5269\u4f59\u4f4d"),
    "order_id": ("\u8ba2\u5355\u53f7", "\u8ba2\u5355ID", "order_id", "orderId"),
    "task_id": ("\u4efb\u52a1ID", "task_id", "taskId"),
}
_LABEL_TO_FIELD = {label: field for field, labels in _LABELS.items()
                   for label in labels}
_GENERIC_TASK_LABEL = "\u4efb\u52a1"


class XingtuError(Exception):
    """Base class for a Xingtu operation that must stop the publish."""

    code = "XINGTU_ERROR"
    failure_scope = "task"
    retryable = False
    action = "manual_check"
    stage = "confirm"

    def __init__(self, message="", *, code=None, details=None,
                 failure_scope=None, retryable=None, action=None, stage=None):
        if code:
            self.code = str(code)
        if details is not None:
            self.details = dict(details or {})
        else:
            self.details = {}
        if failure_scope:
            self.failure_scope = str(failure_scope)
        if retryable is not None:
            self.retryable = bool(retryable)
        if action:
            self.action = str(action)
        if stage:
            self.stage = str(stage)
        super().__init__(str(message or self.code))


class XingtuTaskUnavailable(XingtuError):
    """The backend list could not be read or yielded no attributable tasks."""

    code = "XINGTU_TASK_UNAVAILABLE"
    failure_scope = "task"
    action = "choose_another_task"


class XingtuNotSignedIn(XingtuTaskUnavailable):
    """The backend showed a login page; the operator must sign in again."""

    code = "XINGTU_CLIENT_SIGNED_OUT"
    failure_scope = "client"
    action = "sign_in"


class XingtuTaskListEmpty(XingtuTaskUnavailable):
    """The backend loaded a real task list, but it contained no tasks."""

    code = "XINGTU_TASK_NOT_FOUND"


class XingtuTaskDetailUnreadable(XingtuTaskUnavailable):
    """Task rows exist, but their detail pages did not yield an order id."""

    code = "XINGTU_TASK_UNAVAILABLE"


class XingtuTaskNotAttached(XingtuError):
    """The upload page does not show the selected task; never click publish."""

    code = "XINGTU_TASK_NOT_ATTACHED"
    failure_scope = "task"
    action = "manual_check"


class XingtuTaskNotFound(XingtuError):
    """No live picker card matches the requested task identity."""

    code = "XINGTU_TASK_NOT_FOUND"
    failure_scope = "task"
    action = "choose_another_task"


class XingtuTaskAmbiguous(XingtuError):
    """More than one live picker card matches; never choose the first one."""

    code = "XINGTU_TASK_AMBIGUOUS"
    failure_scope = "task"
    action = "choose_exact_task"


class XingtuTaskChanged(XingtuError):
    """The picker card changed or disappeared between preflight and attach."""

    code = "XINGTU_TASK_CHANGED"
    failure_scope = "task"
    action = "retry_or_reconfirm"


class XingtuPlanSchemaInvalid(XingtuError):
    """The plan's task selector cannot be interpreted safely."""

    code = "XINGTU_PLAN_SCHEMA_INVALID"
    failure_scope = "batch"
    action = "regenerate_plan"


class XingtuAlreadySubmitted(XingtuError):
    """This order id + video already appears in the task submission list."""

    code = "XINGTU_ALREADY_SUBMITTED"
    failure_scope = "item"
    action = "change_material"


class XingtuDuplicateCheckUnavailable(XingtuError):
    """The duplicate check could not be completed; do not risk a second submit."""

    code = "XINGTU_DUPLICATE_CHECK_UNKNOWN"
    failure_scope = "client"
    action = "contact_admin"


class XingtuDuplicateLocal(GateRed):
    """The local ledger already contains this exact task and material."""

    code = "XINGTU_DUPLICATE_LOCAL"
    failure_scope = "item"
    retryable = False
    action = "change_material"
    stage = "confirm"

    def __init__(self, task_name, matched=None, *, details=None):
        matched = matched if isinstance(matched, dict) else {}
        name = _text(task_name) or _text(matched.get("task_name")) \
            or _text(matched.get("order_id"))
        self.matched = matched
        self.details = dict(details or matched or {})
        self.details.setdefault("task_name", name)
        self.details.setdefault(
            "task_name_full", _text(matched.get("task_name_full")) or name)
        self.details.setdefault("published_at",
                                _text(matched.get("published_at")))
        self.details.setdefault("media_sha256_prefix",
                                _text(matched.get("media_sha256_prefix")))
        self.details.setdefault("matched_by", _text(matched.get("matched_by")))
        super().__init__(
            "同素材 + 同任务已投过「" + name + "」；系统未重复发布。")


class XingtuDuplicatePossibleLocal(XingtuDuplicateLocal):
    """A legacy family-only ledger entry may match; require manual review."""

    code = "XINGTU_DUPLICATE_POSSIBLE_LOCAL"
    action = "contact_admin"


class XingtuDedupLedgerError(GateRed):
    """The local used-task ledger is unreadable or unwritable; fail closed."""

    code = "XINGTU_DEDUP_LEDGER_ERROR"
    failure_scope = "client"
    retryable = False
    action = "contact_admin"
    stage = "confirm"

    def __init__(self, message, code=None):
        if code:
            self.code = normalize_error_code(code)
        self.details = {"reason": str(message or "")}
        super().__init__("本地防重账本不可用，已停止发布；请联系管理员。")


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _normal(value) -> str:
    return re.sub(r"\s+", "", _text(value)).replace(":", "").replace("\uff1a", "")


def normalize_task_name(value) -> str:
    """Normalize a task name for strict equality, never substring matching."""
    text = _text(value)
    for char in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        text = text.replace(char, "")
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[\u2010-\u2015\u2212\uFE58\uFE63\uFF0D_]", "-", text)
    return re.sub(r"^\u4efb\u52a1[:\uff1a]?", "", text)


def strip_task_status(name) -> str:
    """Drop the status words the picker pastes onto a task name."""
    text = normalize_task_name(name)
    changed = True
    while changed:
        changed = False
        for word in _STATUS_SUFFIX_WORDS:
            if text.endswith(word) and len(text) > len(word):
                text = text[: -len(word)]
                changed = True
    return text


def same_task_variant(left, right) -> bool:
    """True when two names differ only by a trailing ``-<digits>`` variant.

    The picker re-issues a task with a numeric suffix (``-2``) while an older
    plan still carries the unsuffixed name.  Only a suffix difference counts:
    any other edit stays a mismatch so we never attach the wrong order.
    """
    a, b = strip_task_status(left), strip_task_status(right)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if not long.startswith(short + "-"):
        return False
    return long[len(short) + 1:].isdigit()


def canonical_task_name(family) -> str:
    """Return the canonical legacy token for a stable task family."""
    spec = TASK_FAMILIES.get(str(family or ""))
    return spec["token"] if spec else ""


def _is_known_bad_task_alias(text, *, full_name=False) -> bool:
    if not any(marker in text for marker in _KNOWN_BAD_TASK_NAME_MARKERS):
        return False
    if not full_name:
        return True
    # Exact picker titles can legitimately carry the customer segment.  Only
    # trust it when the canonical token is followed by the numeric task suffix;
    # short aliases such as "APP\u79cd\u8349-\u4f18\u77e9\u4e92\u52a8" stay rejected.
    for spec in TASK_FAMILIES.values():
        token = normalize_task_name(spec.get("token")).upper()
        if token and re.search(r"(?:^|-)%s-\d+(?:-|$)" % re.escape(token), text):
            return False
    return True


def resolve_task_family(value, *, full_name=False) -> str:
    """Resolve one material name or task label to a stable family.

    Canonical task tokens win over generic material keywords, so ``APP种草``
    is not mistaken for a generic ``种草`` rule in another family.  An
    unknown label or a known bad alias returns ``""`` and callers must fail
    closed.  ``full_name`` is reserved for exact picker titles; even then the
    customer-name segment is accepted only with the canonical numeric suffix.
    """
    text = normalize_task_name(value).upper()
    if not text:
        return ""
    if _is_known_bad_task_alias(text, full_name=full_name):
        return ""
    token_hits = [family for family, spec in TASK_FAMILIES.items()
                  if normalize_task_name(spec.get("token")).upper() in text]
    if token_hits:
        return token_hits[0] if len(set(token_hits)) == 1 else ""
    keyword_hits = []
    for family, spec in TASK_FAMILIES.items():
        for keyword in spec.get("keywords") or ():
            if normalize_task_name(keyword).upper() in text:
                keyword_hits.append(family)
                break
    if len(set(keyword_hits)) == 1:
        return keyword_hits[0]
    if len(set(keyword_hits)) > 1:
        return ""
    if re.search(r"FREE\d", text):
        return "free"
    return ""


def task_ref_for(family, *, source="material", display_name="", exact=None,
                 full_name="", order_id="") -> dict:
    """Build the additive schema-2 task reference written next to legacy fields."""
    family = str(family or "")
    if family not in TASK_FAMILIES:
        raise XingtuPlanSchemaInvalid("unknown Xingtu task family: %r" % family)
    token = canonical_task_name(family)
    order_id = normalize_order_id(order_id)
    full_name = normalize_task_name(full_name)
    exact = normalize_task_name(exact) if exact else None
    if order_id:
        match = {"kind": "order_id", "value": order_id}
    elif full_name:
        match = {"kind": "full_name", "value": full_name}
    else:
        match = {"kind": "family_token", "value": token}
    if exact is None:
        exact = order_id or full_name or None
    return {
        "schema": TASK_REF_SCHEMA,
        "family": family,
        "match": match,
        "exact": exact,
        "source": str(source or "material"),
        "display_name": normalize_task_name(display_name) or token,
    }


def _invalid_schema(message):
    raise XingtuPlanSchemaInvalid(message, details={"reason": str(message)})


def parse_task_ref(ref) -> dict:
    """Validate and normalize a schema-2 task reference; never guess."""
    if not isinstance(ref, dict):
        _invalid_schema("xingtu_task_ref must be an object")
    try:
        schema = int(ref.get("schema"))
    except (TypeError, ValueError):
        _invalid_schema("xingtu_task_ref.schema must be 2")
    if schema != TASK_REF_SCHEMA:
        _invalid_schema("unsupported xingtu_task_ref.schema")
    family = str(ref.get("family") or "").strip()
    if family not in TASK_FAMILIES:
        _invalid_schema("xingtu_task_ref.family is not in the whitelist")
    match = ref.get("match")
    if not isinstance(match, dict):
        _invalid_schema("xingtu_task_ref.match must be an object")
    kind = str(match.get("kind") or "").strip()
    value = match.get("value")
    if kind not in ("order_id", "full_name", "family_token"):
        _invalid_schema("xingtu_task_ref.match.kind is unsupported")
    if kind == "order_id":
        value = normalize_order_id(value)
    else:
        value = normalize_task_name(value)
    if not value:
        _invalid_schema("xingtu_task_ref.match.value is empty")
    token = canonical_task_name(family)
    if kind == "family_token" and value != token:
        _invalid_schema("family_token does not match the selected family")
    if kind == "full_name":
        if token not in value:
            _invalid_schema("full_name must contain the canonical task token")
        if resolve_task_family(value, full_name=True) != family:
            _invalid_schema("full_name does not belong to the selected family")
    display = normalize_task_name(ref.get("display_name")) or token
    exact = ref.get("exact")
    exact = (normalize_order_id(exact) if kind == "order_id"
             else normalize_task_name(exact) if exact else "")
    return {
        "schema": TASK_REF_SCHEMA,
        "family": family,
        "match": {"kind": kind, "value": value},
        "exact": exact or None,
        "source": _text(ref.get("source")) or "material",
        "display_name": display,
    }


def task_selector(task) -> dict:
    """Return the normalized selector carried by a task dictionary."""
    task = task if isinstance(task, dict) else {}
    ref = task.get("task_ref") if isinstance(task.get("task_ref"), dict) else None
    if ref:
        parsed = parse_task_ref(ref)
        return {
            "kind": parsed["match"]["kind"],
            "value": parsed["match"]["value"],
            "family": parsed["family"],
            "full_name": (parsed["match"]["value"]
                          if parsed["match"]["kind"] == "full_name" else ""),
            "order_id": (parsed["match"]["value"]
                         if parsed["match"]["kind"] == "order_id" else ""),
            "display_name": parsed["display_name"],
        }
    order_id = normalize_order_id(task.get("order_id"))
    full_name = normalize_task_name(task.get("task_name_full"))
    family = str(task.get("family") or "")
    name = normalize_task_name(task.get("task_name"))
    if not family and name:
        family = resolve_task_family(name)
    token = canonical_task_name(family)
    if order_id:
        return {"kind": "order_id", "value": order_id, "family": family,
                "full_name": full_name, "order_id": order_id,
                "display_name": name or full_name or token}
    if full_name:
        return {"kind": "full_name", "value": full_name, "family": family,
                "full_name": full_name, "order_id": "",
                "display_name": name or full_name}
    if name and token and token in name:
        return {"kind": "family_token", "value": token, "family": family,
                "full_name": name if name != token else "", "order_id": "",
                "display_name": name}
    return {"kind": "", "value": "", "family": family, "full_name": "",
            "order_id": "", "display_name": name}


def render_error_message(code, details=None) -> str:
    """Render a stable code as customer-facing Chinese, never as a raw code."""
    code = normalize_error_code(code)
    details = details if isinstance(details, dict) else {}
    task = _text(details.get("task_name_full")) or _text(details.get("task_name"))
    when = _text(details.get("published_at"))
    prefix = _text(details.get("media_sha256_prefix"))
    candidates = [str(item.get("task_name") or item.get("name") or "")
                  for item in (details.get("candidates") or [])
                  if isinstance(item, dict)]
    candidates = [item for item in candidates if item][:8]
    quoted = "「%s」" % task if task else "该星图任务"
    if code == "XINGTU_TASK_NOT_FOUND":
        suffix = ("。可选任务：" + "、".join(candidates[:4])) if candidates else ""
        return "发布页找不到" + quoted + suffix + "。请确认任务仍在后重新选择。"
    if code == "XINGTU_TASK_AMBIGUOUS":
        suffix = ("。候选：" + "、".join(candidates)) if candidates else ""
        return "同一类星图任务有多张，系统不会替你选第一张" + suffix + "。请指定完整任务名或 order_id。"
    if code == "XINGTU_TASK_CHANGED":
        return quoted + "在发布过程中发生变化或消失，已停止，不会点击发布。请重新确认后再试。"
    if code == "XINGTU_TASK_UNAVAILABLE":
        return quoted + "当前不可用（已结束、下线或抢光），请换任务或等任务恢复。"
    if code == "XINGTU_TASK_NOT_ATTACHED":
        return quoted + "没有成功挂到发布页，已停止，不会发成无任务。请人工确认页面。"
    if code in ("XINGTU_DUPLICATE_LOCAL", "XINGTU_DUPLICATE_POSSIBLE_LOCAL"):
        bits = ["同素材 + 同任务已投过" + quoted]
        if when:
            bits.append("发布时间 %s" % when)
        if prefix:
            bits.append("素材指纹 %s" % prefix)
        if code == "XINGTU_DUPLICATE_POSSIBLE_LOCAL":
            bits.append("旧账本只能确认同任务族，需管理员人工核对")
        else:
            bits.append("系统没有重复发布")
        return "；".join(bits) + "。"
    if code == "XINGTU_DEDUP_LEDGER_ERROR":
        return "本地防重账本不可用，已停止发布。请联系管理员修复账本。"
    if code == "XINGTU_PLAN_SCHEMA_INVALID":
        return "星图任务计划格式无效，已阻止发布。请重新生成计划。"
    if code == "XINGTU_CLIENT_CAPABILITY_MISSING":
        return "当前发布助手版本不支持所需能力，请升级客户端。"
    if code == "XINGTU_ALREADY_SUBMITTED":
        return "同素材 + 同任务在星图后台已经提交过，系统没有重复发布。请换素材或人工核对。"
    if code == "XINGTU_DUPLICATE_CHECK_UNKNOWN":
        return "本地防重检查无法完成，已停止发布。请联系管理员。"
    if code == "XINGTU_CLIENT_SIGNED_OUT":
        return "星图登录状态已失效，请重新登录后再发布。"
    if code == "CLIENT_UNREACHABLE":
        return "客户机暂时连不上，请检查客户机开机、联网且发布助手正在运行。"
    return "发布没有完成，请稍后重试；如反复失败请联系管理员。"


def error_envelope(exc, *, client_version="", capabilities=None,
                   stage=None) -> dict:
    """Return the stable client error envelope used by the bot and tests."""
    code = normalize_error_code(
        getattr(exc, "code", "XINGTU_ERROR") or "XINGTU_ERROR")
    details = dict(getattr(exc, "details", {}) or {})
    message = render_error_message(code, details)
    scope = str(getattr(exc, "failure_scope", "task") or "task")
    return {
        "ok": False,
        "published": False,
        "stage": str(stage or getattr(exc, "stage", "confirm") or "confirm"),
        "code": code,
        "message": message,
        "error": message,
        "xingtu_error": message,
        "retryable": bool(getattr(exc, "retryable", False)),
        "failure_scope": scope,
        "action": str(getattr(exc, "action", "manual_check") or "manual_check"),
        "details": details,
        "problems": [{"code": code, "message": message}],
        "client_version": str(client_version or ""),
        "capabilities": list(capabilities or CLIENT_CAPABILITIES),
    }


def normalize_order_id(value) -> str:
    """Normalize an order id for ledger comparison."""
    text = unicodedata.normalize("NFKC", _text(value)).replace("\u200b", "")
    return re.sub(r"\s+", "", text)


def used_tasks_path(layout) -> Path:
    """Return the ledger path under the publish-worker ``run`` directory."""
    run_dir = getattr(layout, "run", None) if layout is not None else None
    if run_dir is None:
        raise XingtuDedupLedgerError("publish data run directory unavailable")
    return Path(run_dir) / USED_TASKS_FILENAME


def _empty_ledger() -> dict:
    return {"version": USED_TASKS_VERSION, "tasks": []}


def _ledger_label(path) -> str:
    """Return a log-safe ledger name; never expose the absolute path."""
    name = os.path.basename(str(path).rstrip("/\\"))
    return name or "<ledger>"


def _ledger_error_text(path, exc) -> str:
    """Strip the full ledger path from an exception message."""
    full = str(path)
    text = str(exc)
    if full:
        text = text.replace(full, _ledger_label(path))
    return text


class _UsedTasksLock:
    """Cross-process exclusive lock held for the whole read-modify-write."""

    def __init__(self, path, timeout=None):
        self.path = Path(path)
        self.lock_path = Path(str(path) + ".lock")
        self.timeout = (USED_TASKS_LOCK_TIMEOUT_SECONDS if timeout is None
                        else max(0.0, float(timeout)))
        self.handle = None
        self._mode = ""
        self._owned_path = False

    def _try_lock(self):
        if fcntl is not None:
            fcntl.flock(self.handle.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._mode = "fcntl"
            return
        if msvcrt is not None:
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            self._mode = "msvcrt"
            return
        fd = os.open(str(self.lock_path),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        self._owned_path = True
        self._mode = "exclusive"

    def _unlock(self):
        try:
            if self._mode == "fcntl" and fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            elif self._mode == "msvcrt" and msvcrt is not None:
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            if self._owned_path:
                try:
                    self.lock_path.unlink()
                except OSError:
                    pass
                self._owned_path = False

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            if fcntl is not None or msvcrt is not None:
                self.handle = self.lock_path.open("a+b")
                if msvcrt is not None:
                    self.handle.seek(0, os.SEEK_END)
                    if self.handle.tell() == 0:
                        self.handle.write(b"\0")
                        self.handle.flush()
        except Exception as exc:
            self._close_handle()
            raise XingtuDedupLedgerError(
                "cannot open lock %s: %s" % (
                    _ledger_label(self.lock_path),
                    _ledger_error_text(self.lock_path, exc)),
                code="XINGTU_DEDUP_LEDGER_ERROR") from exc
        while True:
            try:
                self._try_lock()
                return self
            except OSError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._close_handle()
                    raise XingtuDedupLedgerError(
                        "ledger lock timeout on %s after %.1fs: %s" % (
                            _ledger_label(self.lock_path), self.timeout,
                            _ledger_error_text(self.lock_path, exc)),
                        code="XINGTU_DEDUP_LEDGER_ERROR") from exc
                time.sleep(min(USED_TASKS_LOCK_POLL_SECONDS, remaining))

    def _close_handle(self):
        if self.handle is not None:
            try:
                self.handle.close()
            except OSError:
                pass
            self.handle = None

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._mode:
                self._unlock()
        finally:
            self._mode = ""
            self._close_handle()
        return False


def _write_ledger_atomic(path, payload) -> None:
    """Write JSON through a temp file in the same directory, then os.replace."""
    path = Path(path)
    tmp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2,
                      sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    except (OSError, TypeError, ValueError) as exc:
        raise XingtuDedupLedgerError(
            "cannot write %s: %s" % (
                _ledger_label(path), _ledger_error_text(path, exc))) from exc
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _load_used_tasks(path) -> dict:
    """Read and validate the ledger; caller must hold the ledger lock."""
    path = Path(path)
    try:
        if not path.exists():
            payload = _empty_ledger()
            _write_ledger_atomic(path, payload)
            return payload
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except XingtuDedupLedgerError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise XingtuDedupLedgerError(
            "cannot read %s: %s" % (
                _ledger_label(path), _ledger_error_text(path, exc))) from exc
    if not isinstance(payload, dict):
        raise XingtuDedupLedgerError(
            "%s is not a JSON object" % _ledger_label(path))
    if payload.get("version") != USED_TASKS_VERSION:
        raise XingtuDedupLedgerError(
            "%s has unsupported version %r" % (
                _ledger_label(path), payload.get("version")))
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        raise XingtuDedupLedgerError(
            "%s tasks is not a list" % _ledger_label(path))
    for entry in tasks:
        if not isinstance(entry, dict):
            raise XingtuDedupLedgerError(
                "%s contains a non-object task entry" % _ledger_label(path))
        if not isinstance(entry.get("task_name", ""), str) or \
                not isinstance(entry.get("order_id", ""), str):
            raise XingtuDedupLedgerError(
                "%s contains an invalid task entry" % _ledger_label(path))
        if "media_sha256" in entry and \
                not isinstance(entry.get("media_sha256"), str):
            raise XingtuDedupLedgerError(
                "%s contains an invalid media fingerprint" % _ledger_label(path))
    return payload


def load_used_tasks(path) -> dict:
    """Read and validate the ledger; create the empty ledger when absent."""
    path = Path(path)
    with _UsedTasksLock(path):
        return _load_used_tasks(path)


def find_used_task(path, task, media_sha256="") -> tuple[dict, str] | tuple[None, str]:
    """Return ``(entry, matched_by)`` when this task was already published.

    A definite duplicate is the same full task name or order id carrying the
    same media fingerprint.  Legacy entries that only carry the family token
    can prove less, so they return a ``*_possible`` marker and the caller must
    still fail closed for manual review.
    """
    payload = load_used_tasks(path)
    task = task if isinstance(task, dict) else {}
    target_selector = task_selector(task)
    target_name = normalize_task_name(task.get("task_name"))
    target_full = normalize_task_name(
        task.get("task_name_full") or target_selector.get("full_name"))
    target_order = normalize_order_id(
        task.get("order_id") or target_selector.get("order_id"))
    target_family = str(task.get("family") or
                        target_selector.get("family") or "")
    target_media = normalize_order_id(media_sha256)
    target_exact = (("order", target_order) if target_order else
                    ("full", target_full) if target_full else None)

    def enrich(entry, matched_by, *, possible=False):
        detail = dict(entry)
        entry_name = normalize_task_name(entry.get("task_name"))
        entry_full = normalize_task_name(entry.get("task_name_full"))
        entry_family = str(entry.get("task_family") or
                           resolve_task_family(entry.get("task_name")) or "")
        entry_media = normalize_order_id(entry.get("media_sha256"))
        detail.update({
            "task_name": entry_name,
            "task_name_full": entry_full or entry_name,
            "task_family": entry_family,
            "media_sha256_prefix": entry_media[:12],
            "published_at": _text(entry.get("published_at")),
            "matched_by": matched_by,
            "duplicate_kind": "possible" if possible else "definite",
        })
        return detail

    possible = None
    for entry in payload["tasks"]:
        entry_name = normalize_task_name(entry.get("task_name"))
        entry_order = normalize_order_id(entry.get("order_id"))
        entry_full = normalize_task_name(entry.get("task_name_full"))
        entry_family = str(entry.get("task_family") or
                           resolve_task_family(entry_name) or "")
        entry_media = normalize_order_id(entry.get("media_sha256"))
        same_order = bool(target_order and entry_order and target_order == entry_order)
        same_full = bool(target_full and entry_full and target_full == entry_full)
        same_legacy_name = bool(target_name and entry_name and
                                target_name == entry_name)

        if same_order:
            if target_media and entry_media and target_media == entry_media:
                return enrich(entry, "order_id+media"), "order_id+media"
            if not entry_media and possible is None:
                # Exact order id without a media fingerprint still cannot be
                # called a definite duplicate; require manual review.
                possible = (enrich(entry, "order_id", possible=True),
                            "order_id_possible")
        if same_full:
            if not target_media or not entry_media:
                if possible is None:
                    possible = (enrich(entry, "task_name_full", possible=True),
                                "task_name_full_possible")
                continue
            if target_media == entry_media:
                return enrich(entry, "task_name_full+media"), "task_name_full+media"
            continue

        # A family token cannot identify the exact card when the family has
        # several live tasks.  Same family + same media is therefore only a
        # possible duplicate unless both sides carry the same exact identity.
        # This also covers a new family-only plan matching an older entry that
        # already has a full name/order id.
        legacy_entry = not entry_full and not entry_order
        if not (target_media and entry_media and target_media == entry_media):
            continue
        entry_exact = (("order", entry_order) if entry_order else
                       ("full", entry_full) if entry_full else None)
        if same_legacy_name and legacy_entry:
            matched_by = "legacy_family+media"
        elif target_family and entry_family and target_family == entry_family:
            if target_full and entry_full and target_full != entry_full:
                continue
            if target_exact and entry_exact and target_exact != entry_exact:
                # Two different full names or two different order ids prove
                # different tasks.  A full name vs an order id cannot be
                # compared without a mapping, so fail closed for review.
                if target_exact[0] != entry_exact[0] and possible is None:
                    matched_by = "family+media"
                    possible = (enrich(entry, matched_by, possible=True),
                                matched_by + "_possible")
                continue
            matched_by = "family+media"
        else:
            continue
        if possible is None:
            possible = (enrich(entry, matched_by, possible=True),
                        matched_by + "_possible")
    if possible:
        return possible
    return None, ""


def append_used_task(path, task, *, attempt_id="", published_at=None,
                     media_sha256="", client_version="", matched_by="",
                     identity_source="") -> dict:
    """Append one successful publish to the v1 ledger with additive fields."""
    path = Path(path)
    with _UsedTasksLock(path):
        payload = _load_used_tasks(path)
        task = task if isinstance(task, dict) else {}
        selector = task_selector(task)
        family = str(task.get("family") or selector.get("family") or "")
        full_name = normalize_task_name(
            task.get("task_name_full") or selector.get("full_name"))
        order_id = _text(task.get("order_id") or selector.get("order_id"))
        task_name = (_text(task.get("task_name"))
                     or _text(selector.get("display_name"))
                     or canonical_task_name(family))
        source = str(identity_source or "")
        if not source:
            source = ("order_id" if normalize_order_id(order_id)
                      else "full_name" if full_name else "legacy_family")
        entry = {
            "task_name": task_name,
            "order_id": order_id,
            "media_sha256": _text(media_sha256).lower(),
            "published_at": _text(published_at) or datetime.now(
                timezone.utc).replace(microsecond=0).isoformat().replace(
                    "+00:00", "Z"),
            "attempt_id": _text(attempt_id),
        }
        entry.update({
            "task_name_full": full_name or _text(task.get("task_name")),
            "task_family": family,
            "identity_source": source,
            "matched_by": _text(matched_by) or (
                "order_id+media" if entry["order_id"] else
                "task_name_full+media" if full_name else "legacy_family+media"),
            "client_version": _text(client_version),
        })
        payload["tasks"].append(entry)
        _write_ledger_atomic(path, payload)
        return entry


# Shared browser-side definition used by both the backend read-back click and
# the publish-page picker/read-back.  The name is the visible card text after
# removing recognised action controls and recognised metadata fields.  Only
# CSS-visual hiding is treated as hidden: aria-hidden text still participates,
# so it cannot be used to append a visible suffix.  CSS-generated content and
# shadow roots cannot be compared from the text tree, so such cards fail
# closed.
XINGTU_VISIBLE_CARD_NAME_JS = r"""
  const __xingtuNormalize = value => {
    let text = String(value || '').replace(/\u200b/g, '');
    try { text = text.normalize('NFKC'); } catch (err) {}
    text = text.replace(/\s+/g, '')
        .replace(/[\u2010-\u2015\u2212\uFE58\uFE63\uFF0D_]/g, '-')
        .replace(/^任务[:\uff1a]?/, '');
    return text;
  };
  const __xingtuCardClassMatches = (el, pattern) => {
    if (!el || !el.classList || !pattern) return false;
    let re;
    try { re = new RegExp(pattern); } catch (err) { return false; }
    return Array.from(el.classList).some(name => re.test(name));
  };
  const __xingtuTaskCard = (el, pattern) => {
    if (!__xingtuCardClassMatches(el, pattern)) return false;
    const text = String(el.innerText || el.textContent || '')
        .replace(/\u200b/g, '').trim();
    return /^任务[:\uff1a]\s*\S/.test(text);
  };
  const __xingtuTaskCards = (root, selector, pattern) => {
    if (!root || !selector) return [];
    return Array.from(root.querySelectorAll(selector))
        .filter(el => __xingtuTaskCard(el, pattern));
  };
  const __xingtuClosestTaskCard = (el, pattern) => {
    for (let node = el; node && node !== document.body;
         node = node.parentElement) {
      if (__xingtuTaskCard(node, pattern)) return node;
    }
    return null;
  };
  const __xingtuVisible = el => {
    if (!el) return false;
    try {
      const style = window.getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden' ||
          Number(style.opacity || 1) === 0) return false;
    } catch (err) {
      return false;
    }
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  };
  const __xingtuMetaFields = ['客户名称', '需求方名称', '客户方', '需求方',
      '任务类型', '剩余额度', '任务状态', '任务时间', '发布时间', '创建时间',
      '更新时间', '客户', '状态', '时间'];
  const __xingtuMetaField = '(?:' + __xingtuMetaFields.join('|') + ')';
  const __xingtuEscapeReg = value => String(value || '')
      .replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const __xingtuMetaValuePattern = label => {
    if (label === '时间' || /时间$/.test(label)) {
      return '(?:\\d{4}[-/.年]\\d{1,2}[-/.月]\\d{1,2}日?'
          + '(?:\\s+\\d{1,2}:\\d{2}(?::\\d{2})?)?'
          + '|\\d{1,2}:\\d{2}(?::\\d{2})?)';
    }
    if (label.indexOf('额度') >= 0) return '\\d+(?:\\.\\d+)?';
    return '\\S+';
  };
  const __xingtuControlWords = ['更换', '删除', '查看详情', '取消', '确定'];
  const __xingtuIsControl = el => {
    if (!el) return false;
    const text = __xingtuNormalize(el.innerText || el.textContent || '');
    return !!text && __xingtuControlWords.indexOf(text) >= 0;
  };
  const __xingtuHasHiddenAncestor = (el, card) => {
    for (let node = el; node && node !== card.parentElement;
         node = node.parentElement) {
      if (node === card) break;
      try {
        const style = window.getComputedStyle(node);
        if (style.display === 'none' || style.visibility === 'hidden' ||
            Number(style.opacity || 1) === 0) return true;
      } catch (err) {
        return true;
      }
      const tag = String(node.tagName || '').toLowerCase();
      if (tag === 'script' || tag === 'style' || tag === 'template' ||
          tag === 'noscript') return true;
    }
    return false;
  };
  const __xingtuControlAncestor = (node, card) => {
    for (let el = node.parentElement; el && el !== card.parentElement;
         el = el.parentElement) {
      if (el === card) break;
      if (__xingtuIsControl(el)) return el;
    }
    return null;
  };
  const __xingtuVisibleNodes = card => {
    if (!card) return [];
    const out = [];
    const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      const parent = node.parentElement;
      if (!parent || !__xingtuVisible(parent)) continue;
      if (__xingtuHasHiddenAncestor(parent, card)) continue;
      if (__xingtuControlAncestor(node, card)) continue;
      out.push({node: node, el: parent, text: node.nodeValue || ''});
    }
    return out;
  };
  const __xingtuVisibleChunks = card =>
      __xingtuVisibleNodes(card).map(item => item.text);
  const __xingtuVisibleText = card =>
      __xingtuVisibleChunks(card).join('');
  const __xingtuMetaLabelOnly = value => {
    const text = String(value || '').trim();
    for (const label of __xingtuMetaFields) {
      if (new RegExp('^' + __xingtuEscapeReg(label)
              + '\\s*[:：]?\\s*$').test(text)) return label;
    }
    return '';
  };
  const __xingtuMetaLabelInText = value => {
    const text = String(value || '');
    return new RegExp('(?:' + __xingtuMetaFields.map(__xingtuEscapeReg).join('|')
        + ')\\s*[:：]').test(text);
  };
  const __xingtuInlineMetadata = value => {
    // Same text node: only remove label + a value with a recognised shape.
    // Anything else is unassignable and must leave the card name non-empty.
    const text = String(value || '');
    for (const label of __xingtuMetaFields) {
      const field = __xingtuEscapeReg(label);
      const shape = __xingtuMetaValuePattern(label);
      let match = text.match(new RegExp(
          '^\\s*' + field + '\\s*[:：]\\s*(' + shape + ')\\s*$'));
      if (match) return {kept: '', label: label};
      match = text.match(new RegExp(
          '^(.*?\\S)\\s*' + field + '\\s*[:：]\\s*(' + shape + ')\\s*$'));
      if (match) return {kept: match[1], label: label};
    }
    return null;
  };
  const __xingtuMetaValueOwned = (label, value) => {
    const text = String(value || '').trim();
    if (!text) return false;
    return new RegExp('^' + __xingtuMetaValuePattern(label) + '$').test(text);
  };
  const __xingtuSameContainer = (left, right) => !!left && !!right &&
      (left === right || left.parentElement === right.parentElement ||
       left === right.parentElement || right === left.parentElement);
  const __xingtuCardNameChunks = card => {
    const nodes = __xingtuVisibleNodes(card);
    const skipped = new Set();
    let unresolved = false;
    for (let index = 0; index < nodes.length; index++) {
      if (skipped.has(index)) continue;
      const item = nodes[index];
      const inline = __xingtuInlineMetadata(item.text);
      if (inline) {
        item.text = inline.kept;
        continue;
      }
      const label = __xingtuMetaLabelOnly(item.text);
      if (!label) {
        if (__xingtuMetaLabelInText(item.text)) unresolved = true;
        continue;
      }
      let valueIndex = index + 1;
      while (valueIndex < nodes.length &&
             !String(nodes[valueIndex].text || '').trim()) valueIndex++;
      if (valueIndex >= nodes.length) {
        unresolved = true;
        continue;
      }
      const value = nodes[valueIndex];
      if (__xingtuMetaLabelOnly(value.text) ||
          !__xingtuMetaValueOwned(label, value.text) ||
          !__xingtuSameContainer(item.el, value.el)) {
        unresolved = true;
        continue;
      }
      skipped.add(index);
      skipped.add(valueIndex);
      index = valueIndex;
    }
    if (unresolved) return [];
    return nodes.filter((_item, index) => !skipped.has(index))
        .map(item => item.text);
  };
  const __xingtuInsideControl = (el, card) => {
    for (let node = el; node && node !== card.parentElement;
         node = node.parentElement) {
      if (node !== card && __xingtuIsControl(node)) return true;
      if (node === card) break;
    }
    return false;
  };
  const __xingtuPseudoHasContent = value => {
    const text = String(value || '').trim();
    return !(text === '' || text === 'none' || text === 'normal' ||
             text === '""' || text === "''" || text === 'initial' ||
             text === 'inherit');
  };
  const __xingtuNameUnsafe = card => {
    if (!card) return true;
    const nodes = [card].concat(Array.from(card.querySelectorAll('*')));
    for (const el of nodes) {
      if (el !== card && __xingtuInsideControl(el, card)) continue;
      if (el !== card && __xingtuHasHiddenAncestor(el, card)) continue;
      if (!__xingtuVisible(el)) continue;
      if (el.shadowRoot) return true;
      try {
        if (__xingtuPseudoHasContent(
                window.getComputedStyle(el, '::before').content) ||
            __xingtuPseudoHasContent(
                window.getComputedStyle(el, '::after').content)) {
          return true;
        }
      } catch (err) {
        return true;
      }
    }
    return false;
  };
  const __xingtuCardName = card => {
    if (!card || __xingtuNameUnsafe(card)) return '';
    return __xingtuNormalize(__xingtuCardNameChunks(card).join(''));
  };
  const __xingtuOrderAttrs = [
    'data-order-id', 'data-orderid', 'data-order', 'data-order_id',
    'data-task-id', 'data-taskid', 'data-task_id'
  ];
  const __xingtuOrderValuesShallow = el => {
    const out = [];
    if (!el || !el.getAttribute) return out;
    for (const name of __xingtuOrderAttrs) {
      const value = String(el.getAttribute(name) || '').trim();
      if (value) out.push(value);
    }
    if (String(el.tagName || '').toLowerCase() === 'a') {
      try {
        const url = new URL(el.href, window.location.href);
        for (const key of ['order_id', 'orderId', 'task_id', 'taskId']) {
          const value = String(url.searchParams.get(key) || '').trim();
          if (value) out.push(value);
        }
      } catch (err) {}
    }
    return out;
  };
  const __xingtuUnique = values => Array.from(new Set(values));
  const __xingtuOrderValuesDeep = root => {
    if (!root) return [];
    const out = [];
    const nodes = [root].concat(Array.from(root.querySelectorAll('*')));
    for (const el of nodes) out.push(...__xingtuOrderValuesShallow(el));
    return __xingtuUnique(out);
  };
  const __xingtuCardOrderIds = card => {
    return __xingtuUnique(__xingtuOrderValuesDeep(card));
  };
"""


def _label_field(line: str) -> str | None:
    norm = _normal(line)
    if not norm:
        return None
    for label, field in sorted(_LABEL_TO_FIELD.items(),
                               key=lambda item: len(item[0]), reverse=True):
        label_norm = _normal(label)
        # The bare "\u4efb\u52a1" label must match exactly: list tables also render
        # "\u4efb\u52a1\u65f6\u95f4" / "\u4efb\u52a1\u72b6\u6001", which are not the task name.
        if norm == label_norm or (label_norm != _GENERIC_TASK_LABEL
                                  and norm.startswith(label_norm)):
            return field
    return None


def _label_value_inline(line: str) -> tuple[str, str] | None:
    text = _text(line)
    for sep in (":", "\uff1a"):
        if sep in text:
            left, right = text.split(sep, 1)
            field = _label_field(left)
            if field and _text(right):
                return field, _text(right)
    return None


def _is_action(line: str) -> bool:
    norm = _normal(line)
    return any(norm == _normal(word) or norm.endswith(_normal(word))
               for word in _ACTION_WORDS)


def parse_value_above(lines) -> dict:
    """Parse indicator cards whose value sits ABOVE its label.

    ``lines`` is one card's visible text, top to bottom.  For every recognised
    label the value is the nearest non-empty line above it that is not itself a
    label or an action button.  A label followed by its value on the same line
    (``name: value``) is also accepted because some backends render it that way.
    """
    rows = [_text(line) for line in (lines or []) if _text(line)]
    out: dict[str, str] = {}
    used: set[int] = set()
    for index, line in enumerate(rows):
        inline = _label_value_inline(line)
        if inline:
            field, value = inline
            out.setdefault(field, value)
            continue
        field = _label_field(line)
        if not field:
            continue
        for back in range(index - 1, -1, -1):
            if back in used:
                continue
            candidate = rows[back]
            if _label_field(candidate) or _is_action(candidate):
                continue
            out.setdefault(field, candidate)
            used.add(back)
            break
    return out


def extract_order_id(value) -> str:
    """Return an order id from a URL, href, attribute text or plain token."""
    text = _text(value)
    if not text:
        return ""
    try:
        parsed = urlparse(text)
        query = parse_qs(parsed.query or "")
        for key in ("order_id", "orderId", "orderid",
                    "task_id", "taskId", "taskid"):
            hits = query.get(key) or []
            if hits and _text(hits[0]):
                return _text(hits[0])
    except Exception:
        pass
    match = _ORDER_RE.search(text)
    if match:
        return _text(match.group(1))
    match = _DETAIL_ID_RE.search(text)
    if match:
        return _text(match.group(1))
    if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", text):
        return text
    return ""


def _detail_name(lines) -> str:
    """Task name from a detail page: creative description first, else title."""
    rows = [_text(line) for line in (lines or []) if _text(line)]
    for index, line in enumerate(rows):
        for label in _DETAIL_NAME_LABELS:
            label_norm = _normal(label)
            for sep in (":", "\uff1a"):
                if sep in line:
                    left, right = line.split(sep, 1)
                    if _normal(left) == label_norm and _text(right):
                        return _text(right)
            if _normal(line) == label_norm and index + 1 < len(rows):
                candidate = rows[index + 1]
                if not _label_field(candidate) and not _is_action(candidate):
                    return candidate
    for line in rows[:3]:
        if line.endswith(" - \u5de8\u91cf\u661f\u56fe"):
            candidate = _text(line[:-len(" - \u5de8\u91cf\u661f\u56fe")])
            if _normal(candidate) not in ("\u4efb\u52a1\u8be6\u60c5",
                                          "\u4efb\u52a1\u8be6\u60c5\u9875"):
                return candidate
    return ""


def _detail_remaining(lines, text) -> str:
    """Remaining submission quota from the detail page text."""
    rows = [_text(line) for line in (lines or []) if _text(line)]
    patterns = (
        r"\u5269\u4f59\u6295\u7a3f\s*[:\uff1a]?\s*(\d+)\s*\u6761?",
        r"\u5269\u4f59\u53ef\u6295\u7a3f\s*[:\uff1a]?\s*(\d+)",
        r"\u5269\u4f59(?:\u6295\u7a3f)?(?:\u6b21\u6570|\u989d\u5ea6|\u914d\u989d)?"
        r"\s*[:\uff1a]?\s*(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "")
        if match:
            return _text(match.group(1))
    for index, line in enumerate(rows):
        if _normal(line) in ("\u5269\u4f59\u6295\u7a3f", "\u5269\u4f59\u914d\u989d",
                             "\u5269\u4f59\u540d\u989d") and index + 1 < len(rows):
            match = re.fullmatch(r"\d+", _text(rows[index + 1]))
            if match:
                return _text(rows[index + 1])
    return ""


def _detail_can_submit(lines) -> bool:
    """True when the detail page exposes a submit button label."""
    rows = [_text(line) for line in (lines or []) if _text(line)]
    for line in rows:
        norm = _normal(line)
        if norm in _SUBMIT_ACTION_WORDS:
            return True
        if len(norm) <= 12 and any(norm.startswith(word)
                                   for word in _SUBMIT_ACTION_WORDS):
            return True
    return False


def parse_detail_task(url, text) -> dict:
    """Parse one Xingtu task detail page from its URL and visible innerText.

    The list page often carries no order id.  Opening "view details" gives a
    URL whose ``task_id`` (also called ``order_id``) is the id the Douyin
    upload page needs, while the page text carries the real task name and the
    remaining submission quota.  This function is deliberately browser-free so
    the real detail fixture can be checked offline.
    """
    url = _text(url)
    raw = _text(text)
    rows = [_text(line) for line in re.split(r"\r?\n", raw) if _text(line)]
    order_id = extract_order_id(url)
    task_id = order_id
    if not order_id:
        body_match = _DETAIL_ID_LINE_RE.search(raw)
        if body_match:
            order_id = _text(body_match.group(1))
            task_id = order_id
    task_name = _detail_name(rows)
    remaining = _detail_remaining(rows, raw)
    buttons = [line for line in rows if _detail_can_submit([line])]
    return {
        "task_name": task_name,
        "demander": "",
        "task_type": "",
        "remaining_quota": remaining,
        "order_id": order_id,
        "task_id": task_id,
        "upload_url": "",
        "lines": rows,
        "index": None,
        "buttons": buttons,
        "can_submit": _detail_can_submit(rows),
    }


def _first_line(lines) -> str:
    for line in lines or []:
        text = _text(line)
        if not text or _label_field(text) or _is_action(text):
            continue
        return text
    return ""


def row_video_status(lines) -> str:
    """Return the row's video status without depending on row-local buttons."""
    rows = [_text(line) for line in (lines or []) if _text(line)]
    for wanted in _ROW_VIDEO_STATUSES:
        wanted_norm = _normal(wanted)
        if any(_normal(line) == wanted_norm for line in rows):
            return wanted
    for line in rows:
        line_norm = _normal(line)
        for wanted in _ROW_VIDEO_STATUSES:
            if _normal(wanted) in line_norm:
                return wanted
    return ""


def _task_info_name(card: dict) -> str:
    """Task name from the list's ``\u4efb\u52a1\u4fe1\u606f`` block, in row order."""
    card = card if isinstance(card, dict) else {}
    for key in ("task_name", "name", "title"):
        value = _text(card.get(key))
        if value:
            return value
    info = card.get("task_info")
    if isinstance(info, dict):
        for key in ("task_name", "name", "title", "text"):
            value = _text(info.get(key))
            if value:
                return value
    elif isinstance(info, (list, tuple)):
        for value in info:
            value = _text(value)
            if value:
                return value
    cells = card.get("cells") or []
    if cells:
        value = _first_line(re.split(r"\r?\n", _text(cells[0])))
        if value:
            return value
    return _first_line(card.get("lines") or [])


def parse_card(card: dict) -> dict:
    """Turn one raw card (``lines``/``hrefs``/``attrs``) into a task dict."""
    card = card if isinstance(card, dict) else {}
    lines = list(card.get("lines") or [])
    values = parse_value_above(lines)
    order_id = _text(values.get("order_id"))
    upload_url = ""
    for href in card.get("hrefs") or []:
        found = extract_order_id(href)
        if found:
            order_id = order_id or found
            if "creator.douyin.com" in _text(href):
                upload_url = _text(href)
    for attr in card.get("attrs") or []:
        found = extract_order_id(attr)
        if found:
            order_id = order_id or found
    for line in lines:
        found = extract_order_id(line)
        if found:
            order_id = order_id or found
    task = {
        "task_name": (_task_info_name(card) or _text(values.get("task_name"))
                      or _first_line(lines)),
        "demander": _text(values.get("demander")),
        "task_type": _text(values.get("task_type")),
        "remaining_quota": _text(values.get("remaining_quota")),
        "status": (_text(card.get("status")) or row_video_status(lines)),
        "order_id": order_id,
        "task_id": _text(values.get("task_id")),
        "upload_url": upload_url,
        "lines": lines,
        "index": card.get("index"),
        # Raw lines as listed; _JS_CLICK_CARD_TEXT re-identifies the card by them.
        "card_lines": [str(x) for x in (card.get("lines") or [])],
        "buttons": list(card.get("buttons") or []),
    }
    return task


def task_available(task: dict) -> bool:
    if task.get("can_submit") is False:
        return False
    text = " ".join(_text(task.get(key)) for key in
                    ("task_name", "remaining_quota", "task_type", "lines"))
    if any(word in text for word in _UNAVAILABLE_WORDS):
        return False
    quota = _text(task.get("remaining_quota"))
    if quota and re.fullmatch(r"\d+", quota) and int(quota) <= 0:
        return False
    return bool(_text(task.get("task_name")))


def dedupe_tasks(tasks) -> list:
    out, seen = [], set()
    for task in tasks or []:
        if not task_available(task):
            continue
        key = (_text(task.get("order_id")), _normal(task.get("task_name")))
        if key in seen:
            continue
        seen.add(key)
        out.append(task)
    return out


def parse_task_cards(cards, *, task_names=None) -> list:
    """Parse list rows and align task names by row order.

    The list's first column is headed ``\u4efb\u52a1\u4fe1\u606f``.  Its visible task
    title is the authoritative list-side name; the customer column is not.
    ``task_names`` lets a caller pass the column's names separately while
    keeping row order.  Detail-page names are checked later by
    :func:`_detail_from_row`.
    """
    raw = list(cards or [])
    names = list(task_names or [])
    tasks = []
    for index, card in enumerate(raw):
        card = card if isinstance(card, dict) else {}
        name = _text(names[index]) if index < len(names) else ""
        name = name or _task_info_name(card)
        source = dict(card)
        if name:
            source["task_name"] = name
        task = parse_card(source)
        if name:
            task["task_name"] = name
        tasks.append(task)
    return dedupe_tasks(tasks)


def match_xingtu_task_name(value) -> str:
    """Map a material file name/path to its canonical legacy task token."""
    return canonical_task_name(resolve_task_family(value))


def _plan_material_names(plan: dict) -> list:
    plan = plan if isinstance(plan, dict) else {}
    piece = plan.get("piece") if isinstance(plan.get("piece"), dict) else {}
    task = plan.get("xingtu_task") if isinstance(plan.get("xingtu_task"), dict) else {}
    names = []
    for source in (piece, plan, task):
        for key in ("file_name", "media_path", "file_path", "path",
                    "material_name"):
            value = _text(source.get(key))
            if value:
                names.append(value)
    return names


def derive_xingtu_task_name(plan: dict) -> str:
    """Never guess a task from the material file name.

    Material names belong to one campaign family in the Huolong workflow.
    Customers also take tasks from other campaign owners, so a name-derived
    family token would silently attach the wrong task (or block a valid one).
    The task must be chosen explicitly by the customer; without one the
    publish stays blocked.
    """
    return ""


def skip_fill_task(plan: dict, mode=None) -> bool:
    """An explicitly empty task name is allowed only for a fill-only run."""
    plan = plan if isinstance(plan, dict) else {}
    task = plan.get("xingtu_task")
    return (str(mode or "") == "fill" and isinstance(task, dict)
            and not normalize_task_name(task.get("task_name"))
            and not plan.get("xingtu_task_ref")
            and not normalize_order_id(task.get("order_id")))


def match_picker_rows(rows, task):
    """Return one structured, fail-closed resolve result.

    ``status=ok`` is the only result a caller may click.  Order ids are exact
    and never fall back to a title.  Full names are exact.  Family tokens use
    an exact token/segment pass first, then containment as a compatibility
    fallback.  Multiple matches are always ``ambiguous``.
    """
    task = task if isinstance(task, dict) else {}
    selector = task_selector(task)
    visible = [row for row in (rows or []) if isinstance(row, dict)
               and row.get("visible")]

    def titles(row):
        values = []
        for value in (row.get("titles") or []):
            normalized = normalize_task_name(value)
            if normalized and normalized not in values:
                values.append(normalized)
        name = normalize_task_name(row.get("name"))
        if name and name not in values:
            values.insert(0, name)
        return values

    def candidate(row, *, usable=True):
        values = titles(row)
        order_ids = [normalize_order_id(value)
                     for value in (row.get("order_ids") or []) if value]
        if not order_ids and row.get("order_id"):
            order_ids = [normalize_order_id(row.get("order_id"))]
        return {
            "task_name": values[0] if values else "",
            "name": values[0] if values else "",
            "titles": values,
            "order_ids": order_ids[:4],
            "order_id_prefix": (order_ids[0][:10] if len(order_ids) == 1 else ""),
            "usable": bool(usable),
        }

    def is_usable(row):
        text = normalize_task_name(" ".join(
            titles(row) + [_text(row.get("text"))]))
        return not any(word in text for word in _UNAVAILABLE_WORDS)

    kind = selector.get("kind")
    value = selector.get("value")
    if not kind or not value:
        return {"status": RESOLVE_NOT_FOUND, "rows": [], "matches": [],
                "code": "XINGTU_TASK_NOT_FOUND",
                "candidates": [candidate(row, usable=is_usable(row))
                               for row in visible[:8]],
                "reason": "selector_missing", "resolved_identity": None}

    def unique_order_ids(row):
        order_ids = list(row.get("order_ids") or [])
        if not order_ids and row.get("order_id"):
            order_ids = [row.get("order_id")]
        return [normalize_order_id(item) for item in order_ids if item]

    if kind == "order_id":
        target = normalize_order_id(value)
        matches = [row for row in visible if target in unique_order_ids(row)]
        reason = "order_id"
    elif kind == "full_name":
        target = normalize_task_name(value)
        matches = [row for row in visible if target in titles(row)]
        reason = "full_name"
        if not matches:
            # 计划里的名字可能比卡片少一个「-2」这样的重发编号；只在差异
            # 仅限末尾纯数字编号、且唯一命中时才认，绝不扩大到模糊匹配。
            variants = []
            for row in visible:
                if any(same_task_variant(item, target) for item in titles(row)):
                    variants.append(row)
            if len(variants) == 1:
                matches = variants
                reason = "full_name_variant"
            elif len(variants) > 1:
                return {"status": RESOLVE_AMBIGUOUS, "rows": [],
                        "matches": variants,
                        "code": "XINGTU_TASK_AMBIGUOUS",
                        "candidates": [candidate(row, usable=is_usable(row))
                                       for row in variants[:8]],
                        "reason": "variant_multiple",
                        "resolved_identity": None}
    else:
        token = normalize_task_name(value)
        full = normalize_task_name(selector.get("full_name"))
        exact_full = ([row for row in visible
                       if full in titles(row)] if full else [])
        if exact_full:
            matches = exact_full
            reason = "full_name_exact"
        else:
            token_re = re.compile(r"(?:^|-)%s(?:-|$)" % re.escape(token))
            exact = [row for row in visible
                     if any(normalize_task_name(item) == token or
                            token_re.search(normalize_task_name(item))
                            for item in titles(row))]
            matches = exact or [row for row in visible
                                if any(token in normalize_task_name(item)
                                       for item in titles(row))]
            reason = "family_token_exact" if exact else "family_token_contained"

    if not matches:
        return {"status": RESOLVE_NOT_FOUND, "rows": [], "matches": [],
                "code": "XINGTU_TASK_NOT_FOUND",
                "candidates": [candidate(row, usable=is_usable(row))
                               for row in visible[:8]],
                "reason": "no_visible_match", "resolved_identity": None}

    usable = [row for row in matches if is_usable(row)]
    if not usable:
        return {"status": RESOLVE_UNAVAILABLE, "rows": [], "matches": matches,
                "code": "XINGTU_TASK_UNAVAILABLE",
                "candidates": [candidate(row, usable=False) for row in matches[:8]],
                "reason": "only_unavailable", "resolved_identity": None}

    # 发布页有时把同一条任务列两遍（同名、都没订单号）。这种情况不是
    # 真歧义：合并成一条取第一个，否则客户会被永远卡在"同名多条"。
    if len(usable) > 1:
        _distinct, _seen = [], set()
        for _row in usable:
            _key = (tuple(titles(_row)), tuple(unique_order_ids(_row)))
            if _key in _seen:
                continue
            _seen.add(_key)
            _distinct.append(_row)
        usable = _distinct

    multi_order = [row for row in usable if len(unique_order_ids(row)) > 1]
    if multi_order or len(usable) > 1:
        return {"status": RESOLVE_AMBIGUOUS, "rows": [], "matches": usable,
                "code": "XINGTU_TASK_AMBIGUOUS",
                "candidates": [candidate(row, usable=True) for row in usable[:8]],
                "reason": "multiple_order_ids" if multi_order else "multiple_matches",
                "resolved_identity": None}

    row = usable[0]
    order_ids = unique_order_ids(row)
    full_name = (titles(row)[0] if titles(row) else "")
    identity = ({"kind": "order_id", "value": order_ids[0],
                 "display_name": full_name or order_ids[0]}
                if order_ids else
                {"kind": "full_name", "value": full_name,
                 "display_name": full_name})
    return {"status": RESOLVE_OK, "rows": [row], "matches": [row],
            "code": "OK", "candidates": [candidate(row, usable=True)],
            "reason": reason, "resolved_identity": identity}


def raise_match_error(outcome, task=None):
    """Raise the typed error represented by a non-ok resolver outcome."""
    outcome = outcome if isinstance(outcome, dict) else {}
    if outcome.get("status") == RESOLVE_OK:
        return
    details = {
        "task_name": _text((task or {}).get("task_name")),
        "task_name_full": _text((task or {}).get("task_name_full")),
        "candidates": list(outcome.get("candidates") or [])[:8],
        "reason": _text(outcome.get("reason")),
    }
    code = str(outcome.get("code") or "XINGTU_TASK_NOT_FOUND")
    cls = {
        "XINGTU_TASK_NOT_FOUND": XingtuTaskNotFound,
        "XINGTU_TASK_AMBIGUOUS": XingtuTaskAmbiguous,
        "XINGTU_TASK_UNAVAILABLE": XingtuTaskUnavailable,
        "XINGTU_TASK_CHANGED": XingtuTaskChanged,
    }.get(code, XingtuTaskNotFound)
    raise cls(render_error_message(code, details), code=code, details=details)


def click_picker_element(item) -> bool:
    """Click the DOM element itself, never a possibly covered coordinate."""
    try:
        if not item.is_visible() or not item.is_enabled():
            return False
        if item.get_attribute("aria-disabled") == "true":
            return False
        # A trusted mouse click is required for a label's default activation
        # to run.  ``dispatch_event("click")`` is untrusted: React handlers
        # fire but the wrapped semi-radio input never becomes checked, which
        # surfaced as "star task card did not become selected" while the card
        # was visibly present.  Keep dispatch_event only as a fallback for
        # elements the mouse cannot reach.
        try:
            item.click(timeout=2000)
            return True
        except Exception:
            pass
        try:
            item.dispatch_event("click", timeout=2000)
        except TypeError:
            item.dispatch_event("click")
        return True
    except Exception:
        try:
            return bool(item.evaluate("""el => {
                if (!el.isConnected || el.disabled ||
                    el.getAttribute('aria-disabled') === 'true' ||
                    !el.getClientRects().length) return false;
                el.click(); return true;
            }"""))
        except Exception:
            return False


def task_from_plan(plan: dict, *, mode=None) -> dict:
    """Return the selected task, or an empty dict for a normal publish.

    A task name alone is a valid Xingtu selection: the task is chosen inside
    the Douyin publish page, where that name is searched in the task picker.
    A plan that claims to be a Xingtu publish with neither an order id nor a
    task name is still invalid and raises instead of degrading into a normal
    Douyin publish. Fill-only calls may skip an explicitly empty task name.
    """
    plan = plan if isinstance(plan, dict) else {}
    if skip_fill_task(plan, mode):
        return {}
    explicit = (str(plan.get("publish_mode") or "") == "xingtu"
                or "xingtu_task" in plan or "xingtu_task_ref" in plan
                or "xingtu" in plan)
    raw_task = plan.get("xingtu_task") or plan.get("xingtu")
    if not isinstance(raw_task, dict):
        raw_task = {}

    warnings = []
    ref = plan.get("xingtu_task_ref")
    if ref is not None:
        parsed = parse_task_ref(ref)
        family = parsed["family"]
        kind = parsed["match"]["kind"]
        value = parsed["match"]["value"]
        token = canonical_task_name(family)
        legacy_name = normalize_task_name(raw_task.get("task_name"))
        if legacy_name:
            legacy_family = resolve_task_family(legacy_name)
            if legacy_family and legacy_family != family:
                raise XingtuPlanSchemaInvalid(
                    "legacy task_name conflicts with xingtu_task_ref",
                    details={"legacy_task_name": legacy_name,
                             "task_ref_family": family})
            if legacy_name not in (token, value) and token not in legacy_name:
                warnings.append("legacy_task_name_not_canonical")
        display = parsed.get("display_name") or token
        order_id = value if kind == "order_id" else ""
        full_name = value if kind == "full_name" else ""
        task_name = token if kind == "family_token" else (full_name or display or token)
        cleaned = {
            "order_id": order_id,
            "task_name": task_name,
            "task_name_full": full_name,
            "family": family,
            "task_ref": parsed,
            "selector_kind": kind,
            "demander": _text(raw_task.get("demander")),
            "task_type": _text(raw_task.get("task_type")),
            "remaining_quota": _text(raw_task.get("remaining_quota")),
            "task_id": _text(raw_task.get("task_id")),
            "upload_url": _text(raw_task.get("upload_url")),
            "mode": "xingtu",
            "warnings": warnings,
        }
        return cleaned

    order_id = normalize_order_id(
        raw_task.get("order_id") or plan.get("order_id"))
    task_name = normalize_task_name(
        raw_task.get("task_name") or plan.get("xingtu_task_name"))
    full_name = normalize_task_name(
        raw_task.get("task_name_full") or plan.get("xingtu_task_full_name"))
    derived_name = ""
    if not task_name and not full_name and not order_id:
        derived_name = derive_xingtu_task_name(plan)
        if derived_name:
            task_name = derived_name
    family = (resolve_task_family(full_name, full_name=True)
              if full_name else resolve_task_family(task_name))
    token = canonical_task_name(family)
    # Any Xingtu task the customer actually holds is valid.  Task family
    # (免费/激励/种草/剧集) is legacy metadata for the Huolong workflow only;
    # it must never gate a publish, because customers also take tasks from
    # other campaign owners.  Identity comes from order_id first, then the
    # exact task name shown in the picker.
    if (task_name or full_name) and not family:
        warnings.append("unknown_task_family")
    if order_id or task_name or full_name:
        selector_kind = ("order_id" if order_id else
                         "full_name" if full_name else
                         "family_token" if family else "task_name")
        return {
            "order_id": order_id,
            "task_name": task_name or full_name or token,
            "task_name_full": full_name,
            "family": family,
            "selector_kind": selector_kind,
            "demander": _text(raw_task.get("demander")),
            "task_type": _text(raw_task.get("task_type")),
            "remaining_quota": _text(raw_task.get("remaining_quota")),
            "task_id": _text(raw_task.get("task_id")),
            "upload_url": _text(raw_task.get("upload_url")),
            "mode": "xingtu",
            "warnings": warnings,
            **({"name_source": "material"} if derived_name else {}),
        }
    if explicit:
        raise XingtuPlanSchemaInvalid(
            "xingtu publish plan has neither order_id nor task_name; "
            "refusing normal publish")
    return {}


def validate_plan_contract(plan, *, mode=None, ledger_path=None) -> dict:
    """Static, browser-free plan validation used by dry-run and tests."""
    problems = []
    task = {}
    try:
        task = task_from_plan(plan, mode=mode)
        ref = (plan or {}).get("xingtu_task_ref") if isinstance(plan, dict) else None
        legacy = ((plan or {}).get("xingtu_task") or {}) if isinstance(plan, dict) else {}
        if isinstance(ref, dict) and isinstance(legacy, dict):
            parsed = parse_task_ref(ref)
            legacy_name = normalize_task_name(legacy.get("task_name"))
            legacy_family = resolve_task_family(legacy_name)
            if legacy_family and legacy_family != parsed["family"]:
                problems.append({
                    "code": "XINGTU_PLAN_SCHEMA_INVALID",
                    "message": render_error_message("XINGTU_PLAN_SCHEMA_INVALID"),
                    "failure_scope": "batch",
                    "action": "regenerate_plan",
                })
    except XingtuError as exc:
        problems.append({
            "code": getattr(exc, "code", "XINGTU_PLAN_SCHEMA_INVALID"),
            "message": render_error_message(
                getattr(exc, "code", "XINGTU_PLAN_SCHEMA_INVALID"),
                getattr(exc, "details", {})),
            "failure_scope": getattr(exc, "failure_scope", "batch"),
            "action": getattr(exc, "action", "regenerate_plan"),
        })
    if task and ledger_path is not None and str(mode or "") != "fill":
        try:
            load_used_tasks(ledger_path)
        except XingtuDedupLedgerError as exc:
            problems.append({
                "code": exc.code,
                "message": render_error_message(exc.code),
                "failure_scope": "client",
                "action": "contact_admin",
            })
    return {"ok": not problems, "problems": problems, "task": task}


def xingtu_upload_url(task: dict) -> str:
    """The Douyin entry page for the selected task.

    With an order id the upload page keeps that id in its query string.  Name
    only selections use the ordinary publish page: the task is picked there,
    so no Xingtu backend list or order id is required.
    """
    order_id = _text((task or {}).get("order_id"))
    if not order_id:
        return DOUYIN_PUBLISH_URL
    upload_url = _text((task or {}).get("upload_url"))
    if upload_url:
        parsed = urlparse(upload_url)
        host = (parsed.hostname or "").lower()
        if host == "creator.douyin.com":
            ids = parse_qs(parsed.query or "").get("order_id") or []
            if ids and _text(ids[0]) == order_id:
                return upload_url
        raise XingtuTaskUnavailable(
            "xingtu entry is not a confirmed Douyin upload link")
    return DOUYIN_UPLOAD_URL + "?order_id=" + quote(order_id, safe="")


def page_has_task(page_text: str, task: dict) -> bool:
    """True only when the selected task name is visible on the upload page."""
    name = _normal((task or {}).get("task_name"))
    body = _normal(page_text)
    if len(name) < 2 or not body:
        return False
    return name in body


def submission_status(text: str, task: dict, *, title: str = "",
                      work_id: str = "") -> str:
    """FOUND / NOT_FOUND / UNKNOWN from a task detail page's visible text."""
    body = _normal(text)
    if not body:
        return STATUS_UNKNOWN
    if work_id and _normal(work_id) in body:
        return STATUS_FOUND
    title_norm = _normal(title)
    # A short title is not enough evidence on a task page: the task name or a
    # generic word can contain it.  Require a real title plus a submission-page
    # marker before claiming the star order was closed.
    if len(title_norm) >= 4 and title_norm in body and any(
            marker in body for marker in (
                "\u6211\u7684\u6295\u7a3f", "\u6295\u7a3f\u8bb0\u5f55",
                "\u6295\u7a3f\u4f5c\u54c1", "\u4f5c\u54c1\u6807\u9898",
                "\u6295\u7a3f\u6210\u529f", "\u5df2\u6295\u7a3f",
                "\u5ba1\u6838\u4e2d", "\u5df2\u5b8c\u6210")):
        return STATUS_FOUND
    order_id = _normal((task or {}).get("order_id"))
    if order_id and order_id in body and any(
            marker in body for marker in ("\u6295\u7a3f\u6210\u529f", "\u5df2\u6295\u7a3f",
                                          "\u5ba1\u6838\u4e2d", "\u5df2\u5b8c\u6210")):
        return STATUS_FOUND
    if any(word in body for word in _EMPTY_SUBMISSION_WORDS):
        return STATUS_NOT_FOUND
    # A readable submission list that does not contain this video means this
    # video has not been submitted to this task yet.  That is different from an
    # unreadable page (UNKNOWN) and lets a second video use the same order.
    if any(marker in body for marker in (
            "\u6211\u7684\u6295\u7a3f", "\u6295\u7a3f\u8bb0\u5f55",
            "\u6295\u7a3f\u4f5c\u54c1", "\u4f5c\u54c1\u6807\u9898")):
        return STATUS_NOT_FOUND
    return STATUS_UNKNOWN


def finalize_readback(result: dict, readback: dict, *, mode: str = "publish") -> dict:
    """Apply the read-back verdict: only FOUND may report publish success.

    ``fill`` stops before the publish button, so no submission record exists.
    A NOT_FOUND/UNKNOWN read-back is therefore not a fill failure; only the
    publish path may turn an unconfirmed record into a failure.
    """
    result = result if isinstance(result, dict) else {}
    readback = readback if isinstance(readback, dict) else {}
    status = _text(readback.get("status")).upper() or STATUS_UNKNOWN
    result["xingtu_readback"] = readback
    result["xingtu_submission_confirmed"] = status == STATUS_FOUND
    if status == STATUS_FOUND:
        result["publish_outcome"] = "CONFIRMED"
        return result
    if str(mode or "").strip().lower() == "fill":
        result["publish_outcome"] = "FILLED"
        return result
    # The platform may have accepted the video, but the star-order loop is not
    # closed.  Keep the platform fact and refuse to call the publish successful.
    result["platform_published"] = bool(
        result.get("platform_published") or result.get("published"))
    result["published"] = False
    result["publish_outcome"] = "UNKNOWN"
    problems = list(result.get("problems") or [])
    problems.append({"code": "XINGTU_READBACK_UNKNOWN",
                     "message": "xingtu submission record could not be read back"})
    result["problems"] = problems
    return result


# Task cards are found again on every call instead of being tagged with a
# data-winport-* attribute: writing our own attributes into the platform's DOM
# leaves a product-named trace the platform can see.  Identity across calls is
# checked by the card's text lines (see _JS_CLICK_CARD_TEXT).
# selector-ok: scoped-unique
_JS_TASK_CARD_ROOTS = r"""
  const __xingtuLines=(el)=>(el && el.innerText||'').split(/\n/).map(s=>s.trim()).filter(Boolean);
  const __xingtuTaskCardRoots=()=>{
      const out=[], seen=new Set();
      const push=(root)=>{
        if(!root || seen.has(root)) return;
        seen.add(root);
        out.push(root);
      };
      const labels=[...document.querySelectorAll('.indicator-label')];
      for(const label of labels){
        let root=label;
        for(let i=0;i<8 && root.parentElement;i++){
          root=root.parentElement;
          if(root.querySelectorAll('.indicator-label').length>=2) break;
        }
        push(root);
      }
      if(!out.length){
        // 1.2.177: the old fallback also matched every div under any class
        // containing "list", which pulled in navigation and page chrome.  Only
        // accept item rows, and only from a single visible item-list container.
        const visible = el => !!(el.offsetWidth || el.offsetHeight ||
                                 el.getClientRects().length);
        const containers = [...document.querySelectorAll('[class*=item-list]')]
          .filter(visible);
        const candidates = [...document.querySelectorAll(
          '[class*=task-item],[class*=taskItem]')];
        if(containers.length === 1){
          candidates.push(...containers[0].querySelectorAll(':scope > div'));
        }
        for(const node of new Set(candidates)){
          const text=(node.innerText||'');
          if(text.includes('\u4efb\u52a1') || text.includes('\u9700\u6c42\u65b9')) push(node);
        }
      }
      return out;
  };
"""


def _js_task_cards() -> str:
    return r"""() => {
""" + _JS_TASK_CARD_ROOTS + r"""
      return __xingtuTaskCardRoots().map((root, idx)=>{
        const lines=__xingtuLines(root);
        const hrefs=[...root.querySelectorAll('a[href]')].map(a=>a.getAttribute('href')||'');
        const attrs=[];
        root.querySelectorAll('*').forEach(el=>{
          for(const a of (el.attributes||[])){
            if(/order|task|data-/i.test(a.name)) attrs.push(a.name+'='+a.value);
          }
        });
        const buttons=[...root.querySelectorAll('button,[role=button],a')]
          .map(el=>(el.innerText||'').trim()).filter(Boolean);
        return {index:idx, lines:lines, hrefs:hrefs, attrs:attrs, buttons:buttons};
      });
    }"""


_JS_CLICK_CARD_TEXT = r"""(arg) => {
""" + _JS_TASK_CARD_ROOTS + r"""
  const cards=__xingtuTaskCardRoots();
  // Identity check: the card must still carry the lines it was listed with.
  // A re-render between listing and clicking shifts indexes; never click a
  // different card than the one the caller chose.
  let card=null;
  if(Array.isArray(arg.lines) && arg.lines.length){
    const want=arg.lines.join('\n');
    const same=cards.filter(c=>__xingtuLines(c).join('\n')===want);
    if(same.length===1) card=same[0];
    else if(same.length>1 && same.indexOf(cards[arg.index])>=0) card=cards[arg.index];
  } else {
    card=cards[arg.index]||null;
  }
  if(!card) return false;
  const nodes=[...card.querySelectorAll('button,[role=button],a')];
  let el=nodes.find(n=>(n.innerText||'').trim()===arg.text);
  if(!el) el=nodes.find(n=>(n.innerText||'').trim().includes(arg.text));
  if(!el) return false;
  el.scrollIntoView({block:'center'});
  el.click();
  return true;
}"""

_JS_CLICK_TEXT = r"""(text) => {
  const norm=s=>(s||'').replace(/\s+/g,'');
  const nodes=[...document.querySelectorAll('button,[role=button],a')];
  let el=nodes.find(n=>norm(n.innerText)===norm(text));
  if(!el) el=nodes.find(n=>norm(n.innerText).includes(norm(text)));
  if(!el) return false;
  el.scrollIntoView({block:'center'});
  el.click();
  return true;
}"""

_JS_CLICK_DETAIL_BY_TASK = r"""(name) => {
""" + _JS_TASK_CARD_ROOTS + r"""
""" + XINGTU_VISIBLE_CARD_NAME_JS + r"""
  const wanted=__xingtuNormalize(name);
  const cards=__xingtuTaskCardRoots();
  const matches=wanted ? cards.filter(c=>__xingtuCardName(c)===wanted) : [];
  if(matches.length!==1) return false;
  const card=matches[0];
  const nodes=[...card.querySelectorAll('button,[role=button],a')];
  let el=nodes.find(n=>__xingtuNormalize(n.innerText)==='\u67e5\u770b\u8be6\u60c5');
  if(!el) return false;
  el.scrollIntoView({block:'center'});
  el.click();
  return true;
}"""


_JS_CLICK_DETAIL_BY_ORDER = r"""(order) => {
""" + _JS_TASK_CARD_ROOTS + r"""
""" + XINGTU_VISIBLE_CARD_NAME_JS + r"""
  const value=String(order||'').trim();
  if(!value) return false;
  const cards=__xingtuTaskCardRoots();
  const matches=cards.filter(c=>{
    const ids=__xingtuCardOrderIds(c);
    return ids.length===1 && ids[0]===value;
  });
  if(matches.length!==1) return false;
  const card=matches[0];
  const nodes=[...card.querySelectorAll('button,[role=button],a')];
  let el=nodes.find(n=>(n.innerText||'').trim()==='\u67e5\u770b\u8be6\u60c5');
  if(!el) el=nodes.find(n=>(n.innerText||'').includes('\u67e5\u770b\u8be6\u60c5'));
  if(!el) return false;
  el.scrollIntoView({block:'center'});
  el.click();
  return true;
}"""


def _js_list_rows() -> str:
    """Tag real task-table rows, excluding Element UI's fixed-column clones."""
    return r"""() => {
      let rows=[...document.querySelectorAll('.el-table__body-wrapper tr.el-table__row')];
      if(!rows.length) rows=[...document.querySelectorAll('tr.el-table__row')];
      const statuses=['\u89c6\u9891\u5df2\u4e0a\u4f20','\u5f85\u4e0a\u4f20\u89c6\u9891'];
      const clean=s=>(s||'').replace(/\s+/g,' ').trim();
      const out=[];
      for(const root of rows){
        if(!root || !root.getClientRects().length) continue;
        const idx=out.length;
        const lines=(root.innerText||'').split(/\n/).map(s=>s.trim()).filter(Boolean);
        const hrefs=[...root.querySelectorAll('a[href]')].map(a=>a.getAttribute('href')||'');
        const attrs=[];
        root.querySelectorAll('*').forEach(el=>{
          for(const a of (el.attributes||[])){
            if(/order|task|data-/i.test(a.name)) attrs.push(a.name+'='+a.value);
          }
        });
        const buttons=[...root.querySelectorAll('button,[role=button],a')]
          .map(el=>(el.innerText||'').trim()).filter(Boolean);
        const titleNode=root.querySelector('.task-title .el-popover__reference') ||
                        root.querySelector('.task-title .xt-popover__content') ||
                        root.querySelector('.task-title');
        let task_name=clean(titleNode ? (titleNode.innerText || titleNode.textContent) : '');
        if(!task_name && lines.length) task_name=lines[0];
        const statusNode=root.querySelector('.author-task-status .xt-status__extra');
        let status=clean(statusNode ? (statusNode.innerText || statusNode.textContent) : '');
        if(!status){
          for(const line of lines){
            const hit=statuses.find(s=>(line||'').replace(/\s+/g,'').includes(s));
            if(hit){ status=hit; break; }
          }
        }
        out.push({index:idx, task_name:task_name, status:status,
                  lines:lines, hrefs:hrefs, attrs:attrs, buttons:buttons});
      }
      return out;
    }"""

_JS_PICKER_LEAF = r"""(root, texts) => {
  const scope = (root && root.ownerDocument) || document;
  const clean = value => String(value || '').replace(/\u200b/g, '').replace(/\s+/g, '');
  const targets = (texts || []).map(clean);
  const visible = el => {
    try {
      const style = getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden' ||
          Number(style.opacity || 1) === 0) return false;
    } catch (err) { return false; }
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  };
  const wanted = el => {
    const text = clean(el.innerText);
    if (!text || targets.indexOf(text) < 0) return false;
    for (const child of el.children) {
      if (clean(child.innerText) === text) return false;
    }
    return true;
  };
  const hits = Array.from(scope.querySelectorAll('span,div,p,a,button'))
      .filter(el => visible(el) && wanted(el));
  if (!hits.length) return null;
  hits.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
  return hits[0];
}"""


_JS_PICKER_OPEN_MODAL = r"""() => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  return Array.from(document.querySelectorAll('div.semi-portal')).some(
      el => visible(el) && (el.innerText || '').includes('星图任务'));
}"""


# selector-ok: scoped-unique
_JS_PICKER_CARDS = r"""() => {
  const clean = s => String(s || '').replace(/\u200b/g, '').replace(/\s+/g, ' ').trim();
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const textOf = el => String((el && (el.innerText || el.textContent)) || '');
  const portals = Array.from(document.querySelectorAll('div.semi-portal'))
      .filter(el => visible(el) && textOf(el).includes('星图任务'));
  const roots = portals.filter(el => !portals.some(other => other !== el && other.contains(el)));
  if (roots.length !== 1) return [];
  const scope = roots[0];
  const cards = Array.from(scope.querySelectorAll('div[class*="card-container-"]'))
      .filter(el => visible(el) && !/creator-layout/.test(String(el.className || '')));
  return cards.map((el, index) => {
    const lines = String(el.innerText || el.textContent || '').split(/\n/).map(s => s.trim()).filter(Boolean);
    const nameEl = el.querySelector('[class*="task-name"]');
    let name = clean(nameEl ? (nameEl.innerText || nameEl.textContent || '') : '');
    if (!name) {
      for (const line of lines) {
        const m = line.match(/^任务[:：]\s*(.+)$/);
        if (m) { name = clean(m[1]); break; }
      }
    }
    return {index: index, name: name, lines: lines};
  });
}"""


_PICKER_OPEN_TEXTS = ("请选择星图任务", "选择星图任务")


def _picker_control(page, timeout_ms=20000):
    """Return the real (trusted-clickable) placeholder element, or None."""
    deadline = time.time() + max(1.0, float(timeout_ms) / 1000.0)
    while time.time() < deadline:
        try:
            handle = page.locator("body").evaluate_handle(
                _JS_PICKER_LEAF, list(_PICKER_OPEN_TEXTS))
            element = handle.as_element() if handle is not None else None
            if element is not None:
                return element
        except Exception:
            pass
        try:
            page.wait_for_timeout(500)
        except Exception:
            # wait-ok: poll
            time.sleep(0.5)
    return None


def _picker_modal_open(page):
    try:
        return bool(page.evaluate(_JS_PICKER_OPEN_MODAL))
    except Exception:
        return False


def _open_picker(page, timeout_ms=20000):
    """Open the star-task picker with a trusted click on the real leaf label.

    A coordinate click on the surrounding row does nothing: the only element
    that opens the picker is the exact text leaf (``span.desc-*``).  This is
    the same click the publish flow uses, so listing tasks cannot drift away
    from the path that actually attaches a task.
    """
    element = _picker_control(page, timeout_ms)
    if element is None:
        return False
    for attempt in range(3):
        try:
            element.click(timeout=2500)
        except Exception:
            try:
                element.dispatch_event("click")
            except Exception:
                pass
        for _ in range(6):
            if _picker_modal_open(page):
                return True
            try:
                page.wait_for_timeout(500)
            except Exception:
                # wait-ok: poll
                time.sleep(0.5)
        element = _picker_control(page, 3000) or element
    return False


def _context_page(context):
    pages = getattr(context, "pages", None) or []
    for page in pages:
        if "xingtu.cn" in _text(getattr(page, "url", "")):
            return page
    return context.new_page()


def _goto_list(page, *, wait_ms: int = 2500):
    page.goto(CREATOR_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(wait_ms)
    page.goto(STAR_AD_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(wait_ms)
    menu = page.locator(TASK_MENU_SELECTOR)
    try:
        if menu.count() and menu.first.is_visible():
            menu.first.click()
            page.wait_for_timeout(wait_ms)
            return page
    except Exception:
        pass
    page.goto(TASK_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(wait_ms)
    return page


def _click_with_page_fallback(context, page, script, arg, *, timeout_ms=10000):
    """Click in a card and return the new tab (Chrome) or same page (WebKit)."""
    clicked = False
    try:
        with context.expect_page(timeout=timeout_ms) as opened:
            clicked = bool(page.evaluate(script, arg))
        target = opened.value
        try:
            target.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass
        target.wait_for_timeout(1200)
        return target, True
    except Exception:
        if not clicked:
            return None, False
        page.wait_for_timeout(1200)
        return page, True


def _entry_from_task(context, page, task, *, timeout_ms=10000):
    """Resolve order_id/upload_url by opening this task's detail/entry."""
    order_id = _text(task.get("order_id"))
    upload_url = _text(task.get("upload_url"))
    buttons = list(task.get("buttons") or [])
    index = task.get("index")
    if index is None:
        return task
    wanted = None
    for action in _ACTION_WORDS:
        if any(_normal(btn) == _normal(action) for btn in buttons):
            wanted = action
            break
    if wanted is None:
        return task
    target, clicked = _click_with_page_fallback(
        context, page, _JS_CLICK_CARD_TEXT,
        {"index": index, "text": wanted,
         "lines": list(task.get("card_lines") or [])}, timeout_ms=timeout_ms)
    if not clicked or target is None:
        return task
    same_page_navigation = target is page
    close_target = target is not page
    try:
        url = _text(getattr(target, "url", ""))
        found = extract_order_id(url)
        if found:
            order_id = order_id or found
            if "creator.douyin.com" in url:
                upload_url = upload_url or url
        if not order_id and wanted != "\u53bb\u6295\u7a3f":
            entry_target, entry_clicked = _click_with_page_fallback(
                context, target, _JS_CLICK_TEXT,
                "\u53bb\u6295\u7a3f", timeout_ms=timeout_ms)
            if entry_clicked and entry_target is not None:
                entry_url = _text(getattr(entry_target, "url", ""))
                found = extract_order_id(entry_url)
                if found:
                    order_id = found
                    if "creator.douyin.com" in entry_url:
                        upload_url = entry_url
                if entry_target is not target:
                    try:
                        entry_target.close()
                    except Exception:
                        pass
                try:
                    target.bring_to_front()
                except Exception:
                    pass
    finally:
        if close_target:
            try:
                target.close()
            except Exception:
                pass
        try:
            page.bring_to_front()
        except Exception:
            pass
        if same_page_navigation:
            # WebKit navigates the list tab itself.  Restore the list and
            # re-tag its cards so the next task can still be resolved; Chrome
            # keeps the list tab untouched and does not need this.
            try:
                _goto_list(page)
                page.evaluate(_js_task_cards())
            except Exception:
                pass
    task["order_id"] = order_id
    task["upload_url"] = upload_url
    return task


def _page_text(page) -> str:
    try:
        return _text(page.inner_text("body"))
    except Exception:
        return ""


def _looks_like_login(page, text: str) -> bool:
    url = _text(getattr(page, "url", "")).lower()
    if any(token in url for token in ("login", "passport", "sso")):
        return True
    return any(word in (text or "") for word in _LOGIN_WORDS)


def _looks_like_empty_list(text: str) -> bool:
    return any(word in (text or "") for word in _EMPTY_LIST_WORDS)


def _detail_buttons(page) -> list:
    """Return whole-page buttons whose visible text is ``\u67e5\u770b\u8be6\u60c5``."""
    buttons = []
    try:
        nodes = page.query_selector_all("button")
    except Exception:
        return buttons
    for node in nodes or []:
        try:
            if _text(node.inner_text()) != _DETAIL_ACTION:
                continue
        except Exception:
            continue
        try:
            if not node.is_visible():
                continue
        except Exception:
            pass
        buttons.append(node)
    return buttons


def _new_context_pages(context, before_ids) -> list:
    pages = list(getattr(context, "pages", None) or [])
    return [page for page in pages if id(page) not in before_ids]


def _detail_from_row(context, page, task, *, timeout_ms: int = 12000) -> dict:
    """Open a row's detail page by its whole-page button position.

    On the customer machine Element UI's fixed columns make the detail button
    invisible from ``row.querySelectorAll`` even though the row itself is
    tagged.  The proven path is the Nth whole-page ``\u67e5\u770b\u8be6\u60c5`` button.
    Chrome opens it in a new tab; ``context.pages`` is re-read after a longer
    wait because the popup event can arrive after the click returns.
    """
    index = task.get("index")
    if index is None:
        return task
    try:
        row_no = int(index) + 1
    except (TypeError, ValueError):
        row_no = 1
    current_url = _text(getattr(page, "url", ""))
    buttons = _detail_buttons(page)
    if len(buttons) <= int(index):
        task["detail_error"] = (
            "row %d (index %s): expected whole-page '%s' button at index %s, "
            "found %d; current URL: %s"
            % (row_no, index, _DETAIL_ACTION, index, len(buttons), current_url))
        return task

    before_ids = {id(item) for item in (getattr(context, "pages", None) or [])}
    target = None
    same_page = False
    try:
        button = buttons[int(index)]
        try:
            button.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            try:
                button.click(timeout=timeout_ms)
            except TypeError:
                button.click()
        except Exception as exc:
            task["detail_error"] = (
                "row %d (index %s): whole-page '%s' click failed (%s: %s); "
                "current URL: %s"
                % (row_no, index, _DETAIL_ACTION, type(exc).__name__, exc,
                   current_url))
            return task

        attempts = max(2, int(timeout_ms) // 4000)
        for attempt in range(attempts):
            page.wait_for_timeout(4000 if attempt == 0 else 3000)
            fresh = _new_context_pages(context, before_ids)
            if fresh:
                with_id = [item for item in fresh
                           if extract_order_id(getattr(item, "url", ""))]
                target = (with_id or fresh)[-1]
                if extract_order_id(getattr(target, "url", "")):
                    break
                continue
            page_url = _text(getattr(page, "url", ""))
            if page_url != current_url and extract_order_id(page_url):
                target = page
                same_page = True
                break

        if target is None:
            task["detail_error"] = (
                "row %d (index %s): no new detail tab after %.1f seconds and "
                "%d context.pages read(s); current URL: %s"
                % (row_no, index, (4000 + 3000 * max(0, attempts - 1)) / 1000.0,
                   attempts, _text(getattr(page, "url", "")) or current_url))
            return task

        same_page = target is page
        try:
            target.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass
        target.wait_for_timeout(1200)
        url = _text(getattr(target, "url", ""))
        text = _page_text(target)
        detail = parse_detail_task(url, text)
        if not detail.get("order_id"):
            task["detail_error"] = (
                "row %d (index %s): detail page URL has no task_id/order_id: %s"
                % (row_no, index, url or "about:blank"))
            return task
        task.pop("detail_error", None)
        task["order_id"] = detail["order_id"]
        task["task_id"] = detail.get("task_id") or detail["order_id"]
        detail_name = _text(detail.get("task_name"))
        row_name = _text(task.get("task_name"))
        if detail_name:
            task["detail_name"] = detail_name
            if row_name:
                task["list_task_name"] = row_name
            row_norm = _normal(row_name)
            detail_norm = _normal(detail_name)
            if not row_name:
                task["task_name"] = detail_name
                task["name_check"] = "detail_filled_missing_row"
            elif row_norm == detail_norm:
                task["name_check"] = "match"
            elif detail_norm and row_norm.startswith(detail_norm):
                # The detail page truncates long creative descriptions.  Keep
                # the complete list-side name and record that the detail name
                # was only a prefix.
                task["name_check"] = "row_name_kept_detail_truncated"
            else:
                task["task_name"] = detail_name
                task["name_check"] = "detail_overrides_row"
        if detail.get("remaining_quota"):
            task["remaining_quota"] = detail["remaining_quota"]
        task["can_submit"] = bool(detail.get("can_submit"))
        return task
    finally:
        if target is not None and target is not page:
            try:
                target.close()
            except Exception:
                pass
        try:
            page.bring_to_front()
        except Exception:
            pass
        if same_page:
            # WebKit replaced the list tab with the detail page.  Restore and
            # re-tag the table before resolving the next row.
            try:
                _goto_list(page)
                page.evaluate(_js_list_rows())
            except Exception:
                pass



def _parse_picker_card(row):
    """Turn one live picker card into the menu task shape."""
    row = row if isinstance(row, dict) else {}
    lines = [_text(line) for line in (row.get("lines") or []) if _text(line)]
    task_name = _text(row.get("name"))
    demander = ""
    task_type = ""
    for line in lines:
        if not task_name and re.match(r"^\u4efb\u52a1[:\uff1a]", line):
            task_name = re.sub(r"^\u4efb\u52a1[:\uff1a]\s*", "", line).strip()
        elif not demander and re.match(r"^(?:\u5ba2\u6237|\u9700\u6c42\u65b9)[:\uff1a]", line):
            demander = re.sub(r"^(?:\u5ba2\u6237|\u9700\u6c42\u65b9)[:\uff1a]\s*", "", line).strip()
    if task_name and len(lines) > 1:
        for line in lines[1:4]:
            if line and not re.match(r"^(?:\u5ba2\u6237|\u9700\u6c42\u65b9|\u5956\u91d1|\u843d\u5730\u9875)[:\uff1a]", line):
                task_type = line
                break
    return {
        "task_name": task_name,
        "order_id": "",
        "demander": demander,
        "task_type": task_type,
        "remaining_quota": "",
        "source": "publish_picker",
    }


def _picker_tasks_once(context, opener, *, max_tasks=20, timeout_ms=30000):
    """One attempt: open the publish-page picker and read its cards."""
    page = context.new_page()
    try:
        page.goto(DOUYIN_PUBLISH_URL, wait_until="domcontentloaded",
                  timeout=max(20000, int(timeout_ms)))
        # The form paints ~10s after first load, so wait for the real label
        # before clicking; the publish-path opener clicks immediately and is
        # only safe once the page is already warm.
        opened = _open_picker(page, timeout_ms)
        if not opened:
            # One reload: the form sometimes paints after a slow first load.
            try:
                page.reload(wait_until="domcontentloaded",
                            timeout=max(20000, int(timeout_ms)))
                opened = _open_picker(page, timeout_ms)
            except Exception:
                opened = False
        if not opened:
            text = _page_text(page)
            if _looks_like_login(page, text):
                raise XingtuNotSignedIn(
                    "douyin creator page is not signed in; sign in, then retry")
            raise XingtuTaskUnavailable(
                "star task picker control was not found on the publish page"
                " (url=%s, chars=%d)"
                % (_text(getattr(page, "url", ""))[:80], len(text or "")))
        rows = []
        for _ in range(16):
            try:
                if opener is not None:
                    rows = opener._xingtu_card_rows(page) or []
                else:
                    rows = page.evaluate(_JS_PICKER_CARDS) or []
            except Exception:
                rows = []
            if rows:
                break
            page.wait_for_timeout(500)
        tasks = []
        seen = set()
        for row in rows[:max(1, int(max_tasks))]:
            task = _parse_picker_card(row)
            name = normalize_task_name(task.get("task_name"))
            if not name or name in seen:
                continue
            seen.add(name)
            tasks.append(task)
        if not tasks:
            raise XingtuTaskListEmpty(
                "the publish-page star task picker has no writable task")
        return tasks
    finally:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        try:
            page.close()
        except Exception:
            pass


def list_picker_tasks(context, *, max_tasks=20, timeout_ms=30000):
    """List the tasks the publish page itself offers (no Xingtu login needed).

    The Douyin publish page shows the account's real, currently writable star
    tasks.  Reading that picker is the same source the publish flow uses, so a
    task listed here is a task that can actually be attached.  Nothing is
    selected and the picker is closed afterwards.

    A cold or throttled creator page occasionally paints an empty shell; one
    fresh-page retry keeps a slow paint from being reported as "no task".
    """
    opener = None
    try:
        from . import publish_exec as _pe  # proven publish-path helpers
        opener = _pe
    except Exception:
        opener = None
    last = None
    for _ in range(2):
        try:
            return _picker_tasks_once(context, opener, max_tasks=max_tasks,
                                      timeout_ms=timeout_ms)
        except XingtuNotSignedIn:
            raise
        except (XingtuTaskUnavailable, XingtuTaskListEmpty) as exc:
            last = exc
    raise last


def _final_task_list(tasks, unresolved: int) -> list:
    # A single malformed row must not hide every other writable task.  At the
    # same time a list with no attributable order id is an error, never a
    # reason to fall back to a normal Douyin publish.
    writable = dedupe_tasks([task for task in (tasks or []) if task_available(task)])
    writable = [task for task in writable if task.get("order_id")]
    if writable:
        return writable
    if unresolved:
        details = [_text(task.get("detail_error")) for task in (tasks or [])
                   if _text(task.get("detail_error"))]
        detail_text = ("; " + "; ".join(details)) if details else ""
        raise XingtuTaskDetailUnreadable(
            "xingtu found %d task row(s) but could not read an order id from "
            "their detail pages%s; open a detail page manually, confirm its "
            "URL contains task_id/order_id, then retry"
            % (unresolved, detail_text))
    raise XingtuTaskUnavailable(
        "xingtu task list loaded but has no writable task (all rows are ended, "
        "full or not accepting submissions); nothing to publish")


def list_available_tasks(context, *, max_tasks: int = 20,
                         resolve_entries: bool = True) -> list:
    """Read the live task list, falling back to the publish-page picker.

    The Xingtu backend needs its own login.  The Douyin publish page shows the
    same writable tasks for the signed-in account, so it is the reliable source
    for "what can I publish right now".  Both paths fail closed.
    """
    try:
        return _list_backend_tasks(context, max_tasks=max_tasks,
                                   resolve_entries=resolve_entries)
    except (XingtuNotSignedIn, XingtuTaskListEmpty, XingtuTaskUnavailable):
        return list_picker_tasks(context, max_tasks=max_tasks)


def _list_backend_tasks(context, *, max_tasks: int = 20,
                        resolve_entries: bool = True) -> list:
    """Read the live Xingtu backend task list.  Raises instead of guessing."""
    page = _context_page(context)
    close_page = page not in (getattr(context, "pages", None) or [])
    try:
        _goto_list(page)
        limit = max(1, int(max_tasks))
        rows = page.evaluate(_js_list_rows())
        if rows:
            tasks = parse_task_cards(rows[:limit])
            unresolved = 0
            resolved = []
            for task in tasks:
                if (task_available(task) and resolve_entries
                        and not task.get("order_id")):
                    task = _detail_from_row(context, page, task)
                if task_available(task) and not task.get("order_id"):
                    unresolved += 1
                resolved.append(task)
            return _final_task_list(resolved, unresolved)

        text = _page_text(page)
        if _looks_like_login(page, text):
            raise XingtuNotSignedIn(
                "xingtu is not signed in (a login page is showing); sign in to "
                "Xingtu in this environment, then retry xingtu-tasks")
        if _looks_like_empty_list(text):
            raise XingtuTaskListEmpty(
                "xingtu task list is empty for this account; there are no "
                "tasks to publish")

        cards = page.evaluate(_js_task_cards())
        if not cards:
            raise XingtuTaskUnavailable(
                "xingtu task table was not found after loading; wait for the "
                "page to finish loading, check the list layout, then retry")
        tasks = parse_task_cards(cards[:limit])
        unresolved = 0
        resolved = []
        for task in tasks:
            if task_available(task) and resolve_entries and not task.get("order_id"):
                task = _entry_from_task(context, page, task)
            if task_available(task) and not task.get("order_id"):
                unresolved += 1
            resolved.append(task)
        return _final_task_list(resolved, unresolved)
    finally:
        if close_page:
            try:
                page.close()
            except Exception:
                pass


def _task_card_lookup(task):
    """Return the click script and argument for this task card.

    An order id uniquely identifies the card, so it wins whenever present.
    The task name is only used when no order id was supplied.
    """
    order_id = _text((task or {}).get("order_id"))
    if order_id:
        return _JS_CLICK_DETAIL_BY_ORDER, order_id
    name = _text((task or {}).get("task_name"))
    if name:
        return _JS_CLICK_DETAIL_BY_TASK, name
    return None, None


def _find_task_card(page, task) -> bool:
    script, arg = _task_card_lookup(task)
    if script is None:
        return False
    try:
        return bool(page.evaluate(script, arg))
    except Exception:
        return False


def find_submission(context, task: dict, *, title: str = "", work_id: str = "",
                    attempts: int = 6, delay: float = 4.0) -> dict:
    """Read this task's submission record.  Returns a status dict, never a guess."""
    attempts = max(1, int(attempts))
    last_reason = ""
    for attempt in range(attempts):
        page = _context_page(context)
        close_page = page not in (getattr(context, "pages", None) or [])
        detail = None
        try:
            _goto_list(page)
            # _find_task_card clicks a card tagged by this exact script.  The
            # read-back path runs in a fresh tab, so it must tag the cards
            # here; otherwise every read-back would be UNKNOWN.
            page.evaluate(_js_task_cards())
            script, arg = _task_card_lookup(task)
            if not _find_task_card(page, task):
                last_reason = "task card not found"
            else:
                # Chrome opens task details in a new tab; WebKit navigates the
                # same tab.  The click MUST happen inside expect_page so the new
                # tab is not missed, and the same-page fallback keeps WebKit
                # working.
                detail, clicked = _click_with_page_fallback(
                    context, page, script, arg, timeout_ms=12000)
                if not clicked or detail is None:
                    last_reason = "task detail did not open"
                    detail = None
                if detail is None:
                    continue
                try:
                    detail.wait_for_load_state("domcontentloaded", timeout=30000)
                except Exception:
                    pass
                detail.wait_for_timeout(2500)
                text = ""
                try:
                    text = detail.inner_text("body")
                except Exception:
                    text = ""
                status = submission_status(text, task, title=title, work_id=work_id)
                if status == STATUS_FOUND:
                    return {"status": STATUS_FOUND, "text": text[:2000],
                            "attempt": attempt + 1}
                if status == STATUS_NOT_FOUND:
                    return {"status": STATUS_NOT_FOUND, "text": text[:2000],
                            "attempt": attempt + 1}
                last_reason = "submission record not visible"
        except Exception as exc:
            last_reason = "%s: %s" % (type(exc).__name__, exc)
        finally:
            if detail is not None and detail is not page:
                try:
                    detail.close()
                except Exception:
                    pass
            if close_page:
                try:
                    page.close()
                except Exception:
                    pass
        if attempt + 1 < attempts:
            time.sleep(max(0.0, float(delay)))
    return {"status": STATUS_UNKNOWN, "reason": last_reason or "readback unavailable"}


def tasks_json(tasks) -> str:
    return json.dumps({"ok": True, "tasks": tasks}, ensure_ascii=True)
