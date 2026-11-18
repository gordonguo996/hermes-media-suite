#!/usr/bin/env python3
"""\u77ed\u89c6\u9891\u53d1\u5e03\u52a9\u624b - \u98de\u4e66\u957f\u8fde\u63a5\u673a\u5668\u4eba
\u6536\u79c1\u804a/\u7fa4@\u6d88\u606f -> \u6307\u4ee4\u89e3\u6790 -> \u56de\u8bdd

\u591a\u5ba2\u6237\u6a21\u5f0f\uff1a\u8bbe\u7f6e ``CONTROL_URL`` \u540e\uff0c\u6d88\u606f\u5148\u6309\u98de\u4e66 chat_id \u8def\u7531\u5230\u63a7\u5236\u9762\u7684
\u5ba2\u6237\uff0c\u518d\u628a\u767d\u540d\u5355\u6307\u4ee4\u6295\u9012\u5230\u8be5\u5ba2\u6237\u7684\u961f\u5217\uff1b\u5ba2\u6237\u673a\u4e0a\u7684 ``agentpoll`` \u8f6e\u8be2\u6267\u884c\u3002
\u672a\u8bbe\u7f6e ``CONTROL_URL`` \u65f6\u5b8c\u5168\u4fdd\u6301\u539f\u6765\u7684\u5355\u5ba2\u6237\u96a7\u9053\u884c\u4e3a\u3002
"""
from __future__ import annotations

import os
import uuid, queue
import io, ntpath, tempfile, threading, json, re, sys, time, hashlib, urllib.parse

import receipt_issue
_HERE = os.path.dirname(os.path.abspath(__file__))
HERMES_CONFIG = os.environ.get("HERMES_CONFIG") or os.path.join(
    os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"), "config.yaml")
BUILD_TWO_ACCOUNT = os.environ.get("BUILD_TWO_ACCOUNT_SCRIPT") or os.path.join(
    os.path.dirname(os.path.dirname(_HERE)), "video-team", "tools", "build_two_account_table.py")

try:  # \u5141\u8bb8\u5728\u6ca1\u6709\u98de\u4e66 SDK \u7684\u73af\u5883\u91cc\u505a\u8def\u7531/\u89e3\u6790\u5355\u6d4b
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import *
except Exception:  # pragma: no cover - exercised only in minimal installs
    lark = None

BASE = os.path.dirname(os.path.abspath(__file__))
_ENV_FILE = os.path.join(BASE, ".env")
if os.path.exists(_ENV_FILE):
    for line in open(_ENV_FILE, encoding="utf-8"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
CLI_PY = os.environ.get("CLI_PY", r"C:\\Users\\<USER>\\Desktop\\manju-publish-assistant-1\\manju-publish-assistant-1.2.34-win64\\runtime\\python.exe")
# 客户机（Windows）上的用户目录，例如 C:\Users\<客户机用户名>。部署时在 .env 里配置；
# 代码里不写任何具体用户名。没配时路径里会出现 CLIENT_HOME_NOT_SET，报错一眼可见，
# 不会悄悄落到别人的目录。各路径仍可用下面原有的单项环境变量单独覆盖。
CLIENT_HOME = (os.environ.get("CLIENT_HOME") or "").rstrip("\\/")
if not CLIENT_HOME:
    print("[bot] CLIENT_HOME 未配置：请在 .env 加 CLIENT_HOME=C:\\Users\\<客户机用户名>",
          file=sys.stderr)
    CLIENT_HOME = r"C:\Users\CLIENT_HOME_NOT_SET"
# 注意：这里是客户机的 LOCALAPPDATA，不读本机（云端 Linux）的环境变量。
LOCALAPPDATA = os.environ.get("CLIENT_LOCALAPPDATA") or (CLIENT_HOME + r"\AppData\Local")
WORKER_ROOT = (os.environ.get("WORKER_DATA_ROOT")
               or LOCALAPPDATA + r"\HuolongAgent\publish-worker")
PY_EXE = (os.environ.get("PY_EXE")
          or LOCALAPPDATA
          + r"\HuolongAgent\publish-worker\.venv\Scripts\python.exe")

client = (
    lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
    if lark is not None and APP_ID and APP_SECRET else None
)

HELP = """\u8bf7\u56de\u590d\u5e8f\u53f7\uff1a

1. \u53d1\u5e03\uff08\u626b inbox \u5168\u90e8\u7d20\u6750\uff1b\u591a\u6761\u4e00\u6b21\u95ee\u8bdd\u9898\u3001\u6279\u91cf\u786e\u8ba4\uff0c\u6309 5-8 \u5206\u949f\u9010\u6761\u53d1\u5e03\uff09
2. \u6539\u671f\uff08\u5982\uff1a\u6539\u671f 1 2026-10-21 10:30\uff09
3. \u6682\u505c\uff08\u5982\uff1a\u6682\u505c 1\uff09
4. \u53d6\u6570
5. \u51fa\u8868
6. \u72b6\u6001

\u76f4\u63a5\u56de\u6570\u5b57\u5c31\u884c\uff0c\u6bd4\u5982\u56de 5 \u5c31\u662f\u51fa\u8868\u3002"""

TUNNEL = os.environ.get("TUNNEL_URL", "http://127.0.0.1:12224/exec")
TUNNEL_TOKEN = os.environ.get("TUNNEL_TOKEN", "")

#: Exact customer-facing text for winport's single-flight guard.
CLIENT_BUSY_MESSAGE = ("\u5ba2\u6237\u673a\u6b63\u5728\u53d1\u5e03\u4e2d\uff0c"
                       "\u8bf7\u7b49\u5f53\u524d\u8fd9\u6761\u53d1\u5b8c\u518d\u64cd\u4f5c\u3002")

def title_from_file(path):
    """A usable Douyin title from a material file name.

    Material names are not titles: some are content hashes, some are long
    descriptive names.  Hash-like names fall back to the default title so we
    never publish a 30-character hex string.
    """
    base = os.path.splitext(os.path.basename(str(path or "")))[0].strip()
    if (not base
            or re.fullmatch(r"[0-9a-fA-F]{16,64}", base)      # 内容哈希
            or not re.search(r"[\u4e00-\u9fff]", base)       # 纯英文/数字
            or len(base) > 30):                               # 截断会切出半句
        return DEFAULT_PUBLISH_TITLE
    return clamp_publish_title(base) or DEFAULT_PUBLISH_TITLE


def clamp_publish_title(text, limit=30):
    """Keep a Douyin title inside the platform limit (30 characters).

    Titles come from three places: an operator-supplied title, an LLM draft,
    and the material file name.  None of them reliably counts characters, and
    the client rejects the whole batch with TITLE_TOO_LONG, so clamp here --
    one choke point for every plan this bot builds.
    """
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) > limit:
        value = value[:limit].rstrip("\uff0c,\u3002.\u3001;\uff1b:\uff1a!\uff01?\uff1f~\uff5e-\u2014_ ")
    return value

#: \u591a\u5ba2\u6237\u63a7\u5236\u9762\u3002\u4e3a\u7a7a\u65f6\u8d70\u65e7\u5355\u5ba2\u6237\u96a7\u9053\uff0c\u4fdd\u8bc1\u5347\u7ea7\u517c\u5bb9\u3002
CONTROL_URL = os.environ.get("CONTROL_URL", "").rstrip("/")
CONTROL_TOKEN = os.environ.get("CONTROL_TOKEN", "") or os.environ.get("CONTROL_ADMIN_TOKEN", "")
CONTROL_INBOUND_TOKEN = os.environ.get("CONTROL_INBOUND_TOKEN", "")
#: chat_id -> operator-requested maximum title length ("改成10个字").
TITLE_LIMIT = {}

#: Matches a length instruction such as "标题改成10个字" / "不超过 12 字".
_TITLE_LIMIT_RE = re.compile(
    r"(?:标题[^。\n]{0,8}?)?"
    r"(?:改成|改到|不超过|最多|限制在?)"
    r"\s*(\d{1,2})\s*个?\s*字")


def requested_title_limit(text):
    """Return the character limit the operator just asked for, if any."""
    match = _TITLE_LIMIT_RE.search(str(text or ""))
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    return value if 1 <= value <= 30 else None


#: 「发布」没带标题时用的默认标题。留空会让客户机回落到文件名（32 位 hash），
#: 触发 TITLE_TOO_LONG 整批失败。抖音标题上限 30 字。
DEFAULT_PUBLISH_TITLE = (os.environ.get("PUBLISH_DEFAULT_TITLE")
                         or "\u706b\u9f99\u6f2b\u5267\u514d\u8d39\u770b").strip()[:30]
MULTITENANT = bool(CONTROL_URL)
DEFAULT_CUSTOMER_ID = "default"

#: Multi-customer result relay + locally stored confirmation batches.  The
#: control plane owns routing; these files only remember what this bot already
#: told a chat, so a restart does not resend the same result.
MT_RESULT_STATE = os.environ.get("BOT_MT_RESULT_STATE") or os.path.join(BASE, "mt_result_state.json")
MT_BATCH_DIR = os.environ.get("BOT_MT_BATCH_DIR") or os.path.join(BASE, "mt_batches")
MT_RESULT_INTERVAL = 10
MT_RESULT_LOCK = threading.RLock()
MT_BATCH_LOCK = threading.RLock()
REPUBLISH_FILE = os.environ.get("BOT_REPUBLISH_FILE") or os.path.join(BASE, "republish_pending.json")
REPUBLISH_TTL_SECONDS = 8 * 60

TOPIC_STATE_FILE = os.environ.get("BOT_TOPIC_STATE") or os.path.join(BASE, "topic_state.json")
PENDING_FILE = os.environ.get("BOT_PENDING_FILE") or "/tmp/bot_pending.json"
PENDING_TTL_SECONDS = 30 * 60
BATCH_DIR = os.environ.get("BOT_BATCH_DIR") or os.path.join(BASE, "batches")
BATCH_TTL_SECONDS = PENDING_TTL_SECONDS
try:
    MAX_BATCH_ITEMS = int(os.environ.get("BOT_MAX_BATCH_ITEMS", "50") or "50")
except ValueError:
    MAX_BATCH_ITEMS = 50
PUBLISH_ENV = os.environ.get("BOT_PUBLISH_ENV", "env_1")
BATCH_LOCK = threading.RLock()
AUTO_UNBLOCK_LOG = (os.environ.get("BOT_AUTO_UNBLOCK_LOG")
                    or os.path.join(BASE, "run", "auto_unblock.log"))
AUTO_UNBLOCK_LOCK = threading.RLock()
TOPIC_ASK = (
    "\u8fd9\u6279\u7d20\u6750\u7528\u4ec0\u4e48\u8bdd\u9898\uff1f\u76f4\u63a5\u56de\u6807\u7b7e\uff0c\u6bd4\u5982 #\u8bdd\u9898\uff08\u591a\u4e2a\u7528\u7a7a\u683c\u9694\u5f00\uff09\uff1b"
    "\u56de\u300c\u9ed8\u8ba4\u300d\u7528\u4e0a\u6b21\u7684\uff1b\u56de\u300c\u4e0d\u586b\u300d\u5c31\u4e0d\u5e26\u8bdd\u9898"
)
TOPIC_ASK_BATCH = (
    "\u53d1\u73b0 {count} \u6761\u7d20\u6750\uff0c\u5c06\u6309 {gap_min}-{gap_max} \u5206\u949f\u95f4\u9694\u4f9d\u6b21\u53d1\u5e03\uff1a\n"
    "{names}\n"
    "\u8fd9\u6279\u7d20\u6750\u7edf\u4e00\u7528\u4ec0\u4e48\u8bdd\u9898\uff1f\u76f4\u63a5\u56de\u6807\u7b7e\uff0c\u6bd4\u5982 #\u8bdd\u9898\uff08\u591a\u4e2a\u7528\u7a7a\u683c\u9694\u5f00\uff09\uff1b"
    "\u56de\u300c\u9ed8\u8ba4\u300d\u7528\u4e0a\u6b21\u7684\uff1b\u56de\u300c\u4e0d\u586b\u300d\u5c31\u4e0d\u5e26\u8bdd\u9898\u3002\n"
    "\u5982\u9700\u5355\u6761\u4e0d\u540c\uff1a\u5728\u7edf\u4e00\u8bdd\u9898\u540e\u52a0\u5e8f\u53f7\uff0c\u6bd4\u5982 2:#\u5355\u72ec\u8bdd\u9898\uff08\u5e8f\u53f7\u5bf9\u5e94\u4e0a\u9762\uff09\u3002\n"
    "\u53ea\u5199\u5355\u6761\u65f6\uff0c\u5176\u4f59\u4e0d\u5e26\u8bdd\u9898\uff1b\u8981\u7edf\u4e00\u8bf7\u5148\u5199\u7edf\u4e00\u8bdd\u9898\u3002"
)
REPUBLISH_ASK = (
    "\u5982\u679c\u786e\u8ba4\u8981\u518d\u53d1\u4e00\u6b21\uff0c\u8bf7\u5728 8 \u5206\u949f\u5185\u56de\u590d\u300c\u786e\u8ba4\u91cd\u53d1\u300d\uff1b"
    "\u4e0d\u60f3\u91cd\u53d1\u8bf7\u56de\u590d\u300c\u53d6\u6d88\u91cd\u53d1\u300d\u3002"
)
_TOPIC_STATE_LOCK = threading.RLock()


def normalize_topics(text):
    """\u628a\u5ba2\u6237\u8f93\u5165\u89c4\u6574\u6210 ``["#\u8bdd\u9898"]``\uff0c\u81ea\u52a8\u8865 #\u3001\u53bb\u91cd\u3001\u53bb\u7a7a\u3002"""
    raw = str(text or "").replace("\uff03", "#")
    for sep in ("\uff0c", "\u3001", "\uff1b", ";", ",", "|", "\n", "\r", "\t"):
        raw = raw.replace(sep, " ")
    topics = []
    for token in raw.split():
        token = token.strip().strip("#").strip()
        if not token:
            continue
        topic = "#" + token
        if topic not in topics:
            topics.append(topic)
    return topics


def _compact_text(text):
    return re.sub(r"\s+", "", str(text or ""))


def _is_default_reply(text):
    return _compact_text(text) in ("\u9ed8\u8ba4", "\u7528\u9ed8\u8ba4", "\u4e0a\u6b21\u7684", "\u7528\u4e0a\u6b21\u7684", "\u7528\u4e0a\u6b21")


def _is_skip_reply(text):
    return _compact_text(text) in ("\u4e0d\u586b", "\u4e0d\u5e26", "\u4e0d\u52a0", "\u65e0")


def _is_cancel_reply(text):
    return _compact_text(text) in (
        "\u53d6\u6d88", "\u7b97\u4e86", "\u4e0d\u53d1\u4e86",
        "\u53d6\u6d88\u53d1\u5e03", "\u53d6\u6d88\u5269\u4f59",
        "\u53d6\u6d88\u5269\u4f59\u53d1\u5e03", "\u6682\u505c\u53d1\u5e03")


def _is_cancel_publish_reply(text):
    return _compact_text(text) in (
        "\u53d6\u6d88", "\u7b97\u4e86", "\u4e0d\u53d1\u4e86",
        "\u53d6\u6d88\u53d1\u5e03", "\u53d6\u6d88\u5269\u4f59",
        "\u53d6\u6d88\u5269\u4f59\u53d1\u5e03", "\u6682\u505c\u53d1\u5e03")


def resolve_topic_reply(text, default_topics=None):
    """\u8fd4\u56de ``(topics, remember)``\u3002

    ``topics`` \u4e3a ``None`` \u8868\u793a\u8fd9\u6761\u56de\u590d\u6ca1\u6709\u7ed9\u51fa\u53ef\u7528\u8bdd\u9898\uff0c\u9700\u8981\u7ee7\u7eed\u8ffd\u95ee\uff1b
    ``[]`` \u8868\u793a\u5ba2\u6237\u660e\u786e\u9009\u62e9\u300c\u4e0d\u586b\u300d\u3002``remember`` \u8868\u793a\u662f\u5426\u8bb0\u4e3a\u9ed8\u8ba4\u8bdd\u9898\u3002
    """
    defaults = list(default_topics or [])
    if _is_default_reply(text):
        return (defaults, False) if defaults else (None, False)
    if _is_skip_reply(text):
        return [], False
    topics = normalize_topics(text)
    return (topics, True) if topics else (None, False)


def parse_batch_topic_reply(text, count, default_topics=None):
    """Parse one batch topic answer.

    Returns ``(global_topics, overrides, remember)`` or ``None`` when the
    answer is not usable.  ``overrides`` is ``{1-based index: [topics]}``.
    A reply without a global part is valid when it has at least one override;
    items not overridden then get no topic.
    """
    defaults = list(default_topics or [])
    raw = str(text or "").replace("\uff03", "#")
    for sep in ("\uff0c", "\u3001", "\uff1b", ";", ",", "|", "\n", "\r", "\t"):
        raw = raw.replace(sep, " ")
    tokens = raw.split()
    if not tokens:
        return None

    overrides = {}
    global_tokens = []
    for token in tokens:
        match = re.match(r"^(\d{1,3})[:\uff1a](.*)$", token)
        if match:
            index = int(match.group(1))
            value = match.group(2).strip()
            if index < 1 or index > int(count):
                return None
            if not value:
                return None
            if _is_skip_reply(value):
                overrides[index] = []
            elif _is_default_reply(value):
                if not defaults:
                    return None
                overrides[index] = list(defaults)
            else:
                topics = normalize_topics(value)
                if not topics:
                    return None
                overrides[index] = topics
        else:
            global_tokens.append(token)

    remember = False
    if global_tokens:
        joined = " ".join(global_tokens)
        if _is_default_reply(joined):
            if not defaults:
                return None
            global_topics = list(defaults)
        elif _is_skip_reply(joined):
            global_topics = []
        else:
            global_topics = normalize_topics(joined)
            if not global_topics:
                return None
            remember = True
    else:
        global_topics = None
    if global_topics is None and not overrides:
        return None
    return global_topics, overrides, remember


def _read_topic_state(path=None):
    target = path or TOPIC_STATE_FILE
    try:
        with open(target, encoding="utf-8") as fh:
            state = json.load(fh)
        if isinstance(state, dict) and isinstance(state.get("chats"), dict):
            return state
    except (OSError, ValueError):
        pass
    return {"chats": {}}


def _write_topic_state(state, path=None):
    target = path or TOPIC_STATE_FILE
    parent = os.path.dirname(os.path.abspath(target))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, target)


def _chat_record(state, chat_id):
    chats = state.setdefault("chats", {})
    record = chats.get(str(chat_id))
    if not isinstance(record, dict):
        record = {}
        chats[str(chat_id)] = record
    return record


def get_default_topics(chat_id, path=None):
    with _TOPIC_STATE_LOCK:
        record = _chat_record(_read_topic_state(path), chat_id)
        topics = record.get("default_topics")
        return list(topics) if isinstance(topics, list) else []


def remember_default_topics(chat_id, topics, path=None):
    topics = list(topics or [])
    if not topics:
        return
    with _TOPIC_STATE_LOCK:
        state = _read_topic_state(path)
        record = _chat_record(state, chat_id)
        record["default_topics"] = topics
        record["updated_at"] = time.time()
        _write_topic_state(state, path)


def set_pending_publish(chat_id, text, title="", path=None, now=None,
                        files=None, gap_min=None, gap_max=None, mode=None,
                        xingtu_task=None, stage=None, tasks=None):
    now = time.time() if now is None else now
    with _TOPIC_STATE_LOCK:
        state = _read_topic_state(path)
        record = _chat_record(state, chat_id)
        pending = {
            "kind": "publish",
            "text": str(text or ""),
            "title": str(title or ""),
            "created_at": now,
            "expires_at": now + PENDING_TTL_SECONDS,
        }
        if files is not None:
            pending["files"] = list(files)
        if gap_min is not None:
            pending["gap_min"] = int(gap_min)
        if gap_max is not None:
            pending["gap_max"] = int(gap_max)
        pending["mode"] = str(mode or "")
        pending["stage"] = str(stage or mode or "mode")
        if xingtu_task is not None:
            pending["xingtu_task"] = xingtu_task
        if tasks is not None:
            pending["tasks"] = tasks
        record["pending"] = pending
        _write_topic_state(state, path)


def get_pending_publish(chat_id, path=None, now=None, *, extend=False):
    """Read a pending publish without consuming it (card callbacks need this)."""
    now = time.time() if now is None else now
    with _TOPIC_STATE_LOCK:
        state = _read_topic_state(path)
        record = _chat_record(state, chat_id)
        pending = record.get("pending") if isinstance(record, dict) else None
        if not isinstance(pending, dict):
            return None, False
        try:
            expired = float(pending.get("expires_at") or 0) <= now
        except (TypeError, ValueError):
            expired = True
        if expired:
            return None, True
        if extend:
            pending["expires_at"] = now + PENDING_TTL_SECONDS
            record["pending"] = pending
            _write_topic_state(state, path)
        return pending, False


def take_pending_publish(chat_id, path=None, now=None):
    """\u53d6\u51fa\u5e76\u6e05\u9664\u5f85\u529e\uff0c\u8fd4\u56de ``(pending, expired)``\u3002"""
    now = time.time() if now is None else now
    with _TOPIC_STATE_LOCK:
        state = _read_topic_state(path)
        chats = state.get("chats") or {}
        record = chats.get(str(chat_id))
        pending = record.get("pending") if isinstance(record, dict) else None
        if not isinstance(pending, dict):
            return None, False
        del record["pending"]
        _write_topic_state(state, path)
        try:
            expired = float(pending.get("expires_at") or 0) <= now
        except (TypeError, ValueError):
            expired = True
        return (None, True) if expired else (pending, False)


def set_republish_candidate(chat_id, plan_path, preview_path=None, token=None,
                            title="", name="", path=None, batch_id=None,
                            item_index=None, customer_id=None, mode="local",
                            plan_data=None, command_id=None, now=None):
    now = time.time() if now is None else now
    with _TOPIC_STATE_LOCK:
        state = _read_topic_state(path or REPUBLISH_FILE)
        record = _chat_record(state, chat_id)
        record["republish"] = {
            "plan": str(plan_path or ""),
            "preview": str(preview_path or ""),
            "token": str(token or ""),
            "title": str(title or ""),
            "name": str(name or ""),
            "batch_id": batch_id,
            "item_index": item_index,
            "customer_id": customer_id,
            "mode": str(mode or "local"),
            "plan_data": plan_data,
            "command_id": command_id,
            "created_at": now,
            "expires_at": now + REPUBLISH_TTL_SECONDS,
        }
        _write_topic_state(state, path or REPUBLISH_FILE)


def get_republish_candidate(chat_id, path=None, now=None):
    now = time.time() if now is None else now
    with _TOPIC_STATE_LOCK:
        state = _read_topic_state(path or REPUBLISH_FILE)
        record = (state.get("chats") or {}).get(str(chat_id)) or {}
        pending = record.get("republish")
        if not isinstance(pending, dict):
            return None, False
        try:
            expired = float(pending.get("expires_at") or 0) <= now
        except (TypeError, ValueError):
            expired = True
        return pending, expired


def clear_republish_candidate(chat_id, path=None):
    with _TOPIC_STATE_LOCK:
        target = path or REPUBLISH_FILE
        state = _read_topic_state(target)
        record = (state.get("chats") or {}).get(str(chat_id))
        if isinstance(record, dict) and "republish" in record:
            del record["republish"]
            _write_topic_state(state, target)


def request_topics(chat_id, text, title="", path=None, files=None):
    previous = load_latest_batch(chat_id)
    if previous and previous.get("status") in ("confirmed", "running"):
        reply(chat_id, "\u4e0a\u4e00\u6279\u8fd8\u5728\u53d1\u5e03\u4e2d\uff0c\u7b49\u5b83\u7ed3\u675f\u518d\u53d1\u65b0\u7684\u3002")
        return
    if files is None:
        threading.Thread(target=_request_topics_scan,
                         args=(chat_id, text, title, path),
                         daemon=True).start()
        return
    _request_topics_with_files(chat_id, text, title, path, files, 5, 8)


def _request_topics_scan(chat_id, text, title, path):
    scanned = scan_inbox()
    if not scanned.get("ok"):
        reply(chat_id, _error_message(
            "\u8bfb\u53d6\u5ba2\u6237\u673a\u7d20\u6750\u5931\u8d25\uff1a",
            scanned.get("err"), "\u5ba2\u6237\u673a\u4e0d\u53ef\u8fbe"))
        return
    _request_topics_with_files(
        chat_id, text, title, path, scanned.get("files") or [],
        scanned.get("gap_min", 5), scanned.get("gap_max", 8))


def _request_topics_with_files(chat_id, text, title, path, files,
                               gap_min, gap_max):
    previous = load_latest_batch(chat_id)
    if previous and previous.get("status") in ("confirmed", "running"):
        reply(chat_id, "\u4e0a\u4e00\u6279\u8fd8\u5728\u53d1\u5e03\u4e2d\uff0c\u7b49\u5b83\u7ed3\u675f\u518d\u53d1\u65b0\u7684\u3002")
        return
    files = _file_entries(files)
    if not files:
        reply(chat_id, "inbox \u91cc\u6ca1\u6709\u89c6\u9891\uff08\u652f\u6301 .mp4/.mov\uff09\u3002")
        return
    if len(files) > MAX_BATCH_ITEMS:
        reply(chat_id, "\u8fd9\u6b21\u53d1\u73b0 %d \u6761\u7d20\u6750\uff0c\u8d85\u8fc7\u5355\u6279\u4e0a\u9650 %d \u6761\uff0c\u8bf7\u5148\u79fb\u8d70\u4e00\u90e8\u5206\u518d\u53d1\u300c\u53d1\u5e03\u300d\u3002"
              % (len(files), MAX_BATCH_ITEMS))
        return
    if previous and previous.get("status") == "pending":
        previous["status"] = "cancelled"
        previous["cancel_reason"] = "superseded by a new publish request"
        save_batch(previous)
    if not title and str(text or "").strip().startswith("\u53d1\u5e03"):
        title = _publish_title(text)
    set_pending_publish(chat_id, text, title, path, files=files,
                        gap_min=gap_min, gap_max=gap_max,
                        mode="", stage="mode")
    if not send_star_choice_card(chat_id):
        reply(chat_id, "\u8fd9\u6761\u8d70\u4e0d\u8d70\u661f\u56fe\u4efb\u52a1\uff1f"
              "\u56de\u300c\u666e\u901a\u53d1\u5e03\u300d\u6216\u300c\u661f\u56fe\u4efb\u52a1\u300d\u3002")


STAR_MODE_ASK = (
    "\u8fd9\u6761\u8d70\u4e0d\u8d70\u661f\u56fe\u4efb\u52a1\uff1f\u8bf7\u9009\u4e00\u4e2a\uff1a"
    "\u666e\u901a\u53d1\u5e03 / \u661f\u56fe\u4efb\u52a1"
)

#: Material keyword -> star task name substring shown in the Douyin picker.
#: Dates deliberately do not participate: the backend task names change.
XINGTU_TASK_LABELS = (
    ("\u514d\u8d39-\u65b0\u7801", ("\u514d\u8d39",)),
    ("\u6fc0\u52b1-\u65b0\u7801", ("\u6fc0\u52b1",)),
    ("APP\u79cd\u8349", ("APP\u79cd\u8349", "\u79cd\u8349")),
    ("\u5267\u96c6\u526a\u8f91", ("\u5267\u96c6", "\u526a\u8f91")),
)
XINGTU_NAME_ASK = (
    "\u7d20\u6750\u540d\u6ca1\u8bc6\u522b\u51fa\u661f\u56fe\u4efb\u52a1\uff0c\u8bf7\u56de\u4e00\u6761\u4efb\u52a1\u540d"
    "\uff08\u5e26 \u514d\u8d39 / \u6fc0\u52b1 / \u79cd\u8349 / \u5267\u96c6 \u5173\u952e\u8bcd\u5373\u53ef\uff09\u3002"
)


def match_xingtu_task_name(value):
    """Map a material file name/path or typed keyword to a task name."""
    text = _compact_text(value).upper()
    if not text:
        return ""
    for label, keywords in XINGTU_TASK_LABELS:
        for keyword in keywords:
            if _compact_text(keyword).upper() in text:
                return label
    return ""


def _xingtu_task_from_pending(pending):
    """Return the single task matched by all materials, or an empty string."""
    labels = []
    for entry in _file_entries((pending or {}).get("files") or []):
        label = match_xingtu_task_name(entry.get("name") or entry.get("path"))
        if label and label not in labels:
            labels.append(label)
    return labels[0] if len(labels) == 1 else ""


def _save_pending(chat_id, pending, path=None):
    set_pending_publish(
        chat_id, pending.get("text") or "", pending.get("title") or "", path,
        files=pending.get("files"), gap_min=pending.get("gap_min"),
        gap_max=pending.get("gap_max"), mode=pending.get("mode"),
        xingtu_task=pending.get("xingtu_task"), stage=pending.get("stage"),
        tasks=pending.get("tasks"))


def _topic_prompt(chat_id, pending):
    files = _file_entries(pending.get("files") or [])
    if len(files) <= 1:
        reply(chat_id, TOPIC_ASK)
        return
    names = "\n".join("%d. %s" % (index, item["name"])
                      for index, item in enumerate(files, 1))
    reply(chat_id, TOPIC_ASK_BATCH.format(
        count=len(files), gap_min=int(pending.get("gap_min") or 5),
        gap_max=int(pending.get("gap_max") or 8), names=names))


def _send_card(chat_id, card):
    from lark_oapi.api.im.v1 import CreateMessageRequestBody, CreateMessageRequest
    body = CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("interactive") \
        .content(json.dumps(card, ensure_ascii=False)).build()
    try:
        client.im.v1.message.create(CreateMessageRequest.builder()
                                    .receive_id_type("chat_id").request_body(body).build())
        return True
    except Exception as exc:
        print("card send failed", exc)
        return False


def send_star_choice_card(chat_id):
    card = {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": "blue",
                   "title": {"tag": "plain_text",
                             "content": "\u662f\u5426\u8d70\u661f\u56fe\u4efb\u52a1"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": "\u8fd9\u6761\u8d70\u4e0d\u8d70\u661f\u56fe\u4efb\u52a1\uff1f"
                        "\u8bf7\u4eba\u5de5\u9009\u4e00\u4e2a\uff0c\u4e0d\u9884\u9009\u3002"}},
            {"tag": "action", "actions": [
                {"tag": "button", "type": "default",
                 "text": {"tag": "plain_text", "content": "\u666e\u901a\u53d1\u5e03"},
                 "value": {"act": "publish_mode", "mode": "normal"}},
                {"tag": "button", "type": "primary",
                 "text": {"tag": "plain_text", "content": "\u661f\u56fe\u4efb\u52a1"},
                 "value": {"act": "publish_mode", "mode": "xingtu"}},
            ]},
        ],
    }
    return _send_card(chat_id, card)


def send_xingtu_match_card(chat_id, task_name):
    card = {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": "blue",
                   "title": {"tag": "plain_text",
                             "content": "\u786e\u8ba4\u661f\u56fe\u4efb\u52a1"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": "\u672c\u6b21\u6309\u7d20\u6750\u5339\u914d\uff1a%s"
                        % task_name}},
            {"tag": "action", "actions": [
                {"tag": "button", "type": "primary",
                 "text": {"tag": "plain_text", "content": "\u5bf9\uff0c\u5c31\u5b83"},
                 "value": {"act": "xingtu_task_confirm",
                           "task_name": task_name}},
                {"tag": "button", "type": "default",
                 "text": {"tag": "plain_text", "content": "\u624b\u52a8\u6539\u4efb\u52a1\u540d"},
                 "value": {"act": "xingtu_task_manual"}},
            ]},
            {"tag": "note", "elements": [{"tag": "plain_text",
             "content": "\u4efb\u52a1\u5728\u6296\u97f3\u53d1\u5e03\u9875\u5185\u9009\u62e9\uff0c"
                        "\u4e0d\u8bfb\u661f\u56fe\u540e\u53f0\u5217\u8868\u3002"}]},
        ],
    }
    return _send_card(chat_id, card)


def choose_publish_mode(chat_id, mode, path=None, pending=None):
    if pending is None:
        pending, expired = get_pending_publish(chat_id, path, extend=True)
    else:
        expired = False
    if not pending:
        reply(chat_id, "\u6ca1\u6709\u5f85\u53d1\u5e03\u7684\u7d20\u6750\uff0c\u8bf7\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u3002")
        return
    if mode == "normal":
        pending.update(mode="normal", stage="topics", xingtu_task=None)
        _save_pending(chat_id, pending, path)
        _topic_prompt(chat_id, pending)
        return
    if mode != "xingtu":
        send_star_choice_card(chat_id)
        return
    pending.update(mode="xingtu", stage="task_confirm", xingtu_task=None, tasks=None)
    _save_pending(chat_id, pending, path)
    task_name = _xingtu_task_from_pending(pending)
    if not task_name:
        pending.update(stage="task_name")
        _save_pending(chat_id, pending, path)
        reply(chat_id, XINGTU_NAME_ASK)
        return
    if not send_xingtu_match_card(chat_id, task_name):
        reply(chat_id, "\u786e\u8ba4\u5361\u53d1\u9001\u5931\u8d25\uff0c"
              "\u8bf7\u56de\u4e00\u6761\u661f\u56fe\u4efb\u52a1\u540d\u3002")


def prompt_xingtu_task_name(chat_id, path=None, pending=None):
    if pending is None:
        pending, _expired = get_pending_publish(chat_id, path, extend=True)
    if not pending:
        reply(chat_id, "\u6ca1\u6709\u5f85\u53d1\u5e03\u7684\u7d20\u6750\uff0c\u8bf7\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u3002")
        return
    pending.update(mode="xingtu", stage="task_name", xingtu_task=None, tasks=None)
    _save_pending(chat_id, pending, path)
    reply(chat_id, XINGTU_NAME_ASK)


def confirm_xingtu_task(chat_id, task_name, path=None, pending=None):
    if pending is None:
        pending, _expired = get_pending_publish(chat_id, path, extend=True)
    if not pending:
        reply(chat_id, "\u6ca1\u6709\u5f85\u53d1\u5e03\u7684\u7d20\u6750\uff0c\u8bf7\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u3002")
        return
    text = str(task_name or "").strip()
    task_name = match_xingtu_task_name(text) or text
    if not task_name:
        reply(chat_id, XINGTU_NAME_ASK)
        return
    previous = pending.get("xingtu_task")
    previous = previous if isinstance(previous, dict) else {}
    task = {"task_name": task_name,
            "order_id": str(previous.get("order_id") or "")}
    pending.update(mode="xingtu", stage="topics", xingtu_task=task, tasks=[])
    _save_pending(chat_id, pending, path)
    reply(chat_id, "\u5df2\u9009\u661f\u56fe\u4efb\u52a1\uff1a%s" % task_name)
    _topic_prompt(chat_id, pending)


def handle_pending_reply(chat_id, text, pending, path=None):
    if _is_cancel_reply(text):
        reply(chat_id, "\u5df2\u53d6\u6d88\u8fd9\u6b21\u53d1\u5e03\u3002")
        return
    files = _file_entries(pending.get("files") or [])
    compact = _compact_text(text)
    stage = str(pending.get("stage") or pending.get("mode") or "mode")
    if stage == "mode":
        if compact in ("\u666e\u901a\u53d1\u5e03", "\u666e\u901a", "1", "normal"):
            choose_publish_mode(chat_id, "normal", path, pending=pending)
        elif compact in ("\u661f\u56fe\u4efb\u52a1", "\u661f\u56fe", "2", "xingtu"):
            choose_publish_mode(chat_id, "xingtu", path, pending=pending)
        else:
            reply(chat_id, STAR_MODE_ASK)
            send_star_choice_card(chat_id)
        return
    if stage == "task_confirm":
        if compact in ("\u5bf9\uff0c\u5c31\u5b83", "\u5bf9\u5c31\u5b83", "\u5bf9", "\u5c31\u5b83"):
            task_name = _xingtu_task_from_pending(pending)
            if task_name:
                confirm_xingtu_task(chat_id, task_name, path, pending=pending)
            else:
                prompt_xingtu_task_name(chat_id, path, pending=pending)
            return
        if compact in ("\u624b\u52a8\u6539\u4efb\u52a1\u540d", "\u624b\u52a8", "\u6539\u4efb\u52a1\u540d"):
            prompt_xingtu_task_name(chat_id, path, pending=pending)
            return
        task_name = match_xingtu_task_name(text)
        if task_name:
            confirm_xingtu_task(chat_id, task_name, path, pending=pending)
        else:
            reply(chat_id, "\u8bf7\u56de\u300c\u5bf9\uff0c\u5c31\u5b83\u300d"
                  "\u6216\u300c\u624b\u52a8\u6539\u4efb\u52a1\u540d\u300d\u3002")
        return
    if stage == "task_name":
        task_name = match_xingtu_task_name(text) or text.strip()
        confirm_xingtu_task(chat_id, task_name, path, pending=pending)
        return
    if compact == "1":
        request_topics(chat_id, pending.get("text") or text,
                       pending.get("title") or "", path, files=files or None)
        return
    if compact.startswith("\u53d1\u5e03"):
        request_topics(chat_id, text, text[2:].strip(), path,
                       files=files or None)
        return
    if len(files) > 1:
        parsed = parse_batch_topic_reply(
            text, len(files), get_default_topics(chat_id, path))
        if parsed is None:
            if _is_default_reply(text):
                msg = "\u8fd8\u6ca1\u6709\u9ed8\u8ba4\u8bdd\u9898\uff0c\u76f4\u63a5\u56de\u6807\u7b7e\u5427\uff0c\u6bd4\u5982 #\u8bdd\u9898\uff1b\u56de\u300c\u4e0d\u586b\u300d\u5c31\u4e0d\u5e26\u8bdd\u9898\u3002"
            else:
                msg = ("\u6ca1\u8bc6\u522b\u5230\u8bdd\u9898\u3002\u56de\u7edf\u4e00\u6807\u7b7e\uff0c\u6bd4\u5982 #\u8bdd\u9898\uff1b"
                       "\u5355\u6761\u4e0d\u540c\u8bf7\u7528 2:#\u8bdd\u9898\u3002")
            _save_pending(chat_id, pending, path)
            reply(chat_id, msg)
            return
        global_topics, overrides, remember = parsed
        if remember and global_topics:
            remember_default_topics(chat_id, global_topics, path)
        threading.Thread(
            target=do_publish_batch,
            args=(chat_id, pending.get("text") or "\u53d1\u5e03",
                  list(global_topics or []), files, overrides,
                  pending.get("title") or "",
                  int(pending.get("gap_min") or 5),
                  int(pending.get("gap_max") or 8),
                  pending.get("xingtu_task")),
            daemon=True).start()
        return
    topics, remember = resolve_topic_reply(text, get_default_topics(chat_id, path))
    if topics is None:
        if _is_default_reply(text):
            msg = "\u8fd8\u6ca1\u6709\u9ed8\u8ba4\u8bdd\u9898\uff0c\u76f4\u63a5\u56de\u6807\u7b7e\u5427\uff0c\u6bd4\u5982 #\u8bdd\u9898\uff1b\u56de\u300c\u4e0d\u586b\u300d\u5c31\u4e0d\u5e26\u8bdd\u9898\u3002"
        else:
            msg = "\u6ca1\u8bc6\u522b\u5230\u8bdd\u9898\uff0c\u76f4\u63a5\u56de\u6807\u7b7e\u5427\uff0c\u6bd4\u5982 #\u8bdd\u9898\uff08\u591a\u4e2a\u7528\u7a7a\u683c\u9694\u5f00\uff09\uff1b\u56de\u300c\u4e0d\u586b\u300d\u5c31\u4e0d\u5e26\u8bdd\u9898\u3002"
        _save_pending(chat_id, pending, path)
        reply(chat_id, msg)
        return
    if remember:
        remember_default_topics(chat_id, topics, path)
    threading.Thread(target=do_publish,
                     args=(chat_id, pending.get("text") or "\u53d1\u5e03", topics, files,
                           pending.get("xingtu_task")),
                     daemon=True).start()


class ControlPlaneError(RuntimeError):
    """\u63a7\u5236\u9762\u4e0d\u53ef\u8fbe\u6216\u8fd4\u56de\u4e86\u65e0\u6cd5\u5904\u7406\u7684\u54cd\u5e94\u3002"""


class UnknownChatError(ControlPlaneError):
    """\u98de\u4e66 chat_id \u8fd8\u6ca1\u6709\u7ed1\u5b9a\u5230\u4efb\u4f55\u5ba2\u6237\u3002"""

    def __init__(self, chat_id, detail=None):
        self.chat_id = chat_id
        self.detail = detail or {}
        super().__init__("unknown chat_id: %s" % chat_id)


class RemoteOpError(RuntimeError):
    """A remote op failed, carrying the child process output for diagnosis."""

    def __init__(self, message, result=None):
        self.result = result if isinstance(result, dict) else {}
        super().__init__(str(message))


class MTBindingError(RuntimeError):
    """The plan, preview, token and receipt no longer describe one publish."""


def control_request(method, path, body=None, timeout=15, control_url=None):
    """\u8c03\u7528\u63a7\u5236\u9762\uff0c\u8fd4\u56de ``(status, json)``\u3002

    HTTP 4xx/5xx \u4e0d\u629b\u5f02\u5e38\uff0c\u8c03\u7528\u65b9\u53ef\u4ee5\u6309\u72b6\u6001\u7801\u7ed9\u51fa\u660e\u786e\u7684\u5ba2\u6237\u63d0\u793a\uff1b\u7f51\u7edc\u9519\u8bef
    \u629b :class:`ControlPlaneError`\u3002
    """
    import urllib.error
    import urllib.request

    base = (control_url if control_url is not None else CONTROL_URL).rstrip("/")
    if not base:
        raise ControlPlaneError("CONTROL_URL is not configured")
    data = None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if CONTROL_TOKEN:
        headers["Authorization"] = "Bearer " + CONTROL_TOKEN
    # 飞书入站投递用独立 secret；管理读接口仍走 admin token。
    if CONTROL_INBOUND_TOKEN and path.startswith("/api/inbound"):
        headers["X-Inbound-Token"] = CONTROL_INBOUND_TOKEN
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw or "{}")
            except ValueError:
                return resp.status, {"raw": raw}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except ValueError:
            return exc.code, {"raw": raw}
    except Exception as exc:  # noqa: BLE001 - surfaced as a clear reply
        raise ControlPlaneError(str(exc)) from exc


def lookup_customer(chat_id, control_url=None, timeout=15):
    """\u6309\u98de\u4e66 chat_id \u67e5\u5ba2\u6237\uff1b\u672a\u7ed1\u5b9a\u8fd4\u56de ``None``\u3002"""
    from urllib.parse import quote

    status, obj = control_request(
        "GET",
        "/api/customers/by-chat/" + quote(str(chat_id), safe=""),
        timeout=timeout,
        control_url=control_url,
    )
    if status == 404:
        return None
    if status >= 400:
        raise ControlPlaneError(obj.get("error") or ("lookup failed (%s)" % status))
    return obj.get("customer") or obj


def list_customer_devices(customer_id, control_url=None, timeout=15):
    from urllib.parse import quote
    status, obj = control_request(
        "GET", "/api/customers/%s/devices" % quote(str(customer_id), safe=""),
        timeout=timeout, control_url=control_url)
    if status >= 400:
        raise ControlPlaneError(obj.get("error") or "device list failed")
    return obj.get("devices") or []


def select_customer_device(customer_id, preferred=None, require_online=True,
                           control_url=None, timeout=15):
    """Choose a device for the session-bound customer; never choose customer."""
    from urllib.parse import quote, urlencode
    query = {"require_online": "1" if require_online else "0"}
    if preferred:
        query["device_id"] = str(preferred)
    status, obj = control_request(
        "GET", "/api/customers/%s/devices/select?%s" % (
            quote(str(customer_id), safe=""), urlencode(query)),
        timeout=timeout, control_url=control_url)
    if status >= 400:
        raise ControlPlaneError(obj.get("error") or "no available device")
    device = obj.get("device") or {}
    if not device.get("device_id"):
        raise ControlPlaneError("no available device")
    return device


def add_customer_device(customer_id, label=None, machine_code=None,
                        enforce_binding=True, control_url=None, timeout=15):
    status, obj = control_request(
        "POST", "/api/customers/%s/devices" % customer_id,
        {"label": label, "machine_code": machine_code,
         "enforce_binding": bool(enforce_binding)},
        timeout=timeout, control_url=control_url)
    if status >= 400:
        raise ControlPlaneError(obj.get("error") or "add device failed")
    return obj


def switch_customer_device(customer_id, old_device_id, new_device_id,
                           control_url=None, timeout=15):
    status, obj = control_request(
        "POST", "/api/customers/%s/devices/switch" % customer_id,
        {"old_device_id": old_device_id, "new_device_id": new_device_id},
        timeout=timeout, control_url=control_url)
    if status >= 400:
        raise ControlPlaneError(obj.get("error") or "switch device failed")
    return obj


def isolate_customer_device(customer_id, device_id, retire=False,
                            control_url=None, timeout=15):
    action = "retire" if retire else "isolate"
    status, obj = control_request(
        "POST", "/api/customers/%s/devices/%s/%s" % (
            customer_id, device_id, action), {},
        timeout=timeout, control_url=control_url)
    if status >= 400:
        raise ControlPlaneError(obj.get("error") or "isolate device failed")
    return obj


def enqueue_for_chat(chat_id, kind, payload=None, command_id=None,
                     control_url=None, timeout=15, device_id=None):
    """\u628a\u4e00\u6761\u6307\u4ee4\u6295\u9012\u5230 chat_id \u5bf9\u5e94\u5ba2\u6237\u7684\u961f\u5217\u3002

    \u672a\u7ed1\u5b9a\u5ba2\u6237\u65f6\u629b :class:`UnknownChatError`\uff08\u5e26\u63a7\u5236\u9762\u7684\u660e\u786e\u9519\u8bef\u4f53\uff09\u3002
    """
    payload = payload if isinstance(payload, dict) else {}
    if device_id is None:
        device_id = payload.get("device_id") or payload.get("target_device_id")
    body = {"chat_id": str(chat_id), "kind": kind, "payload": payload}
    if device_id:
        body["device_id"] = str(device_id)
    if command_id:
        body["command_id"] = command_id
    status, obj = control_request(
        "POST", "/api/inbound", body, timeout=timeout, control_url=control_url
    )
    if status == 404:
        raise UnknownChatError(chat_id, obj)
    if status >= 400:
        raise ControlPlaneError(obj.get("error") or ("enqueue failed (%s)" % status))
    return obj


def enqueue_command(customer_id, kind, payload=None, command_id=None,
                    control_url=None, timeout=15, device_id=None):
    """\u6309 customer_id \u5b9a\u5411\u6295\u9012\uff08\u7ba1\u7406\u811a\u672c/\u6d4b\u8bd5\u7528\uff09\u3002"""
    payload = payload if isinstance(payload, dict) else {}
    if device_id is None:
        device_id = payload.get("device_id") or payload.get("target_device_id")
    body = {"customer_id": customer_id, "kind": kind, "payload": payload}
    if device_id:
        body["device_id"] = str(device_id)
    if command_id:
        body["command_id"] = command_id
    status, obj = control_request(
        "POST", "/api/command", body, timeout=timeout, control_url=control_url
    )
    if status >= 400:
        raise ControlPlaneError(obj.get("error") or ("enqueue failed (%s)" % status))
    return obj


def customer_data_root(customer_id=None):
    """\u5ba2\u6237\u673a\u6570\u636e\u6839\uff1adefault \u4fdd\u6301\u65e7\u8def\u5f84\uff0c\u5176\u4ed6\u5ba2\u6237\u843d\u5230 customers/<id>/\u3002"""
    base = WORKER_ROOT
    cid = str(customer_id or DEFAULT_CUSTOMER_ID)
    if cid == DEFAULT_CUSTOMER_ID:
        return base
    return os.path.join(base, "customers", cid)


def _exec_ok(result):
    return (result.get("exit_code") in (0, None, "0")
            and "\u5ba2\u6237\u673a\u4e0d\u53ef\u8fbe" not in (result.get("stderr") or ""))


def _last_line(text, limit=300):
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    return (lines[-1] if lines else "")[:limit]


def is_idempotency_consumed(text):
    """True when the customer runtime refused an already-published material."""
    return bool(re.search(r"idempotency\s*:\s*already\s+CONSUMED", str(text or ""), re.I))


def publish_unblock_reason(text):
    """Return the stale-publish marker to recover, or None.

    CONSUMED is deliberately excluded: it is a known success and must keep the
    customer-confirmed republish path.  RESERVED is also excluded because the
    publish may still be in flight.
    """
    haystack = str(text or "")
    if is_idempotency_consumed(haystack):
        return None
    low = haystack.lower()
    match = re.search(r"idempotency\s*:\s*already\s+(unknown|submit_intent)\b", low)
    if match:
        return match.group(1).upper()
    if "already unknown" in low:
        return "UNKNOWN"
    if "already submit_intent" in low or "already submit intent" in low:
        return "SUBMIT_INTENT"
    if ("frozen" in low or "freeze" in low or "publish_unknown" in low
            or re.search(r"\bhold", low) or "\u51bb\u7ed3" in haystack):
        return "HOLD"
    return None


def _result_text(result):
    return "\n".join(value for value in
                     ((result or {}).get("stderr"), (result or {}).get("stdout"))
                     if value)


def _published_result(result):
    try:
        data = _parse_json_blob((result or {}).get("stdout"))
    except Exception:
        return False
    return isinstance(data, dict) and bool(data.get("published"))


def _plan_channel(plan):
    if not isinstance(plan, dict):
        return ""
    return str(plan.get("channel_id") or plan.get("account_id") or "").strip()


def _publish_mode_line(result, plan=None):
    """Customer-readable publish mode, including the star order id."""
    result = result if isinstance(result, dict) else {}
    plan = plan if isinstance(plan, dict) else {}
    task = result.get("xingtu_task") or plan.get("xingtu_task") or {}
    if not isinstance(task, dict):
        task = {}
    mode = str(result.get("publish_mode") or plan.get("publish_mode") or "")
    order_id = str(result.get("xingtu_order_id") or task.get("order_id") or
                   plan.get("order_id") or "")
    task_name = str(result.get("xingtu_task_name") or task.get("task_name") or "")
    if mode != "xingtu" and not order_id:
        return "\u53d1\u5e03\u65b9\u5f0f\uff1a\u666e\u901a\u53d1\u5e03"
    line = "\u53d1\u5e03\u65b9\u5f0f\uff1a\u661f\u56fe\u4efb\u52a1"
    if task_name:
        line += " \u00b7 " + task_name
    if order_id:
        line += "\uff08order_id: %s\uff09" % order_id
    readback = result.get("xingtu_readback") or {}
    if isinstance(readback, dict) and readback.get("status"):
        line += "\uff1b\u6295\u7a3f\u72b6\u6001\uff1a%s" % readback.get("status")
    return line


def _ledger_auto_clear_source(data_root=None):
    """Remote Python source that deletes only UNKNOWN / SUBMIT_INTENT rows.

    Without ``data_root`` the client resolves its own data directory, exactly
    like the publish CLI.  A caller-supplied root is only used for a bound
    customer directory.
    """
    source = r'''
import json, os, sys
__ROOT_SETUP__
try:
    from agent.winport.paths import Layout
    path = str(Layout().stage2_ledger)
except Exception as exc:
    print(json.dumps({"ok": False, "path": "", "removed": {},
                      "error": "client layout unavailable: %s" % exc},
                     ensure_ascii=True, sort_keys=True))
    raise SystemExit(0)
if not os.path.exists(path):
    print(json.dumps({"ok": True, "path": path, "removed": {},
                      "backup": None}, ensure_ascii=True, sort_keys=True))
    raise SystemExit(0)
try:
    from agent.winport.stage2_ledger import Stage2Ledger, UNKNOWN, SUBMIT_INTENT, RESERVED
except Exception as exc:
    print(json.dumps({"ok": False, "path": path, "removed": {},
                      "error": "ledger module unavailable: %s" % exc},
                     ensure_ascii=True, sort_keys=True))
    raise SystemExit(0)
ledger = Stage2Ledger(path)
with ledger.locked():
    entries = ledger.data.get("entries") or {}
    removed = {}
    for key, entry in list(entries.items()):
        state = (entry or {}).get("state")
        if state == RESERVED:
            removed[key] = state
    backup = None
    if removed:
        backup = ledger.backup("bot auto unblock")
        for key in removed:
            entries.pop(key, None)
        ledger.save()
print(json.dumps({"ok": True, "path": path, "removed": removed,
                  "backup": str(backup) if backup else None},
                 ensure_ascii=True, sort_keys=True))
'''
    setup = ""
    if data_root:
        setup = ("os.environ[\"WINPORT_DATA\"] = %s\n"
                 % json.dumps(str(data_root)))
    return source.replace("__ROOT_SETUP__", setup)


def clear_client_ledger_blocks(customer_id=None):
    """Remove stale UNKNOWN / SUBMIT_INTENT rows from the client ledger."""
    custom = str(customer_id or DEFAULT_CUSTOMER_ID) != DEFAULT_CUSTOMER_ID
    root = customer_data_root(customer_id) if custom else None
    env = None
    if custom:
        env = {"WINPORT_DATA": root,
               "WINPORT_SYSTEM": os.path.join(root, "system")}
    if _use_named_ops():
        info = _client_named_op("ledger-unblock", timeout=60)
        if not info.get("ok"):
            return {"ok": False, "removed": {}, "path": "",
                    "error": info.get("err") or "ledger clear failed"}
        return info
    try:
        result = _client_exec_code(_ledger_auto_clear_source(root),
                                   timeout=60, cwd=CLI_ROOT, env=env)
    except Exception as exc:
        return {"ok": False, "removed": {}, "path": "",
                "error": _last_line(exc) or type(exc).__name__}
    text = (result.get("stdout") or "").strip()
    if not _exec_ok(result) or not text:
        return {"ok": False, "removed": {}, "path": "",
                "error": _result_error(result, "ledger clear failed")}
    try:
        info = json.loads(text.splitlines()[-1])
    except Exception:
        return {"ok": False, "removed": {}, "path": "",
                "error": "ledger clear returned invalid JSON"}
    if not isinstance(info, dict):
        return {"ok": False, "removed": {}, "path": "",
                "error": "ledger clear returned invalid shape"}
    return info


def _append_auto_unblock_log(channel, trigger, removed, hold, ledger):
    record = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "at_epoch": time.time(),
        "channel": str(channel or ""),
        "trigger": str(trigger or ""),
        "removed": dict(removed or {}),
        "removed_count": len(removed or {}),
        "hold_ok": bool((hold or {}).get("ok")),
        "hold_cleared": bool((hold or {}).get("cleared")),
        "ledger_ok": bool((ledger or {}).get("ok")),
        "ledger_error": str((ledger or {}).get("error") or ""),
        "hold_error": str((hold or {}).get("error") or ""),
    }
    try:
        parent = os.path.dirname(os.path.abspath(AUTO_UNBLOCK_LOG))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        line = json.dumps(record, ensure_ascii=True, sort_keys=True)
        with AUTO_UNBLOCK_LOCK:
            with open(AUTO_UNBLOCK_LOG, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:
        sys.stderr.write("[auto-unblock] audit log failed: %s\n" % exc)
    return record


def auto_unblock_publish(channel, trigger, customer_id=None):
    """Best-effort hold + ledger recovery before one retry."""
    channel = str(channel or "").strip()
    hold = {"ok": False, "cleared": False, "error": ""}
    if channel:
        try:
            result = run_cli(
                ["publish-hold-clear", "--channel", channel,
                 "--reason", "bot auto unblock: clear stale publish state"],
                timeout=120, customer_id=customer_id)
            text = (result.get("stdout") or "").strip()
            hold["ok"] = _exec_ok(result)
            if not hold["ok"]:
                hold["error"] = _result_error(result, "hold clear failed")
            else:
                try:
                    info = json.loads(text.splitlines()[-1]) if text else {}
                    hold["cleared"] = bool((info or {}).get("cleared"))
                except Exception:
                    hold["cleared"] = False
        except Exception as exc:
            hold["error"] = _last_line(exc) or type(exc).__name__
    else:
        hold["error"] = "missing channel"
    ledger = clear_client_ledger_blocks(customer_id)
    _append_auto_unblock_log(channel, trigger,
                             ledger.get("removed") or {}, hold, ledger)
    return {"channel": channel, "hold": hold, "ledger": ledger,
            "removed": ledger.get("removed") or {}}


def run_publish_with_recovery(args, channel, customer_id=None, timeout=900):
    """Run exactly one publish call.

    A failed publish may have reached Douyin before the response was lost.
    Automatically clearing the ledger and retrying can therefore publish the
    same material twice.  Recovery is now a deliberate, customer-confirmed
    action; this helper never retries.
    """
    result = run_cli(args, timeout=timeout, customer_id=customer_id)
    if _published_result(result):
        mark_publish_receipt()
    trigger = publish_unblock_reason(_result_text(result))
    if trigger:
        print("[publish] stale block (%s); no automatic retry" % trigger,
              flush=True)
    return result, None


def publish_failure_detail(result, recovery=None, fallback="publish failed"):
    """Plain-Chinese failure text, including a failed post-recovery retry."""
    if recovery and publish_unblock_reason(_result_text(result)):
        return ("\u4e0a\u6b21\u53d1\u5e03\u7684\u6b8b\u7559\u8bb0\u5f55\u81ea\u52a8\u6e05\u7406\u540e\uff0c"
                "\u8fd9\u6b21\u4ecd\u88ab\u62e6\u4f4f\u3002\u8bf7\u91cd\u542f\u53d1\u5e03\u52a9\u624b\u540e\u518d\u8bd5\uff1b"
                "\u5982\u679c\u8fd8\u4e0d\u884c\uff0c\u8bf7\u8054\u7cfb\u7ba1\u7406\u5458\u6838\u5bf9\u4e0a\u6b21\u662f\u5426\u5df2\u7ecf\u53d1\u51fa\u3002")
    return _result_error(result, fallback)


def _has_chinese(text):
    return bool(re.search(r"[\u4e00-\u9fff]", str(text or "")))


def _looks_like_machine_error(text):
    """True for stack/exception/code fragments that must not reach a customer."""
    return bool(re.search(
        r"(?i)(traceback|exception\b|\w+error\b|error\s*[:(]|\.py:\d+|"
        r"errno\b|stack trace|refusing to|already consumed)", str(text or "")))


def is_client_busy(text):
    """True when the customer machine reports the single-flight publish guard."""
    haystack = str(text or "")
    return bool(re.search(r"operation\s+in\s+progress", haystack, re.I)
                or re.search(r"\b429\b", haystack))


def _client_failure_result(exc):
    """Shape a tunnel exception like /exec JSON, preserving HTTP 429 as busy."""
    import urllib.error

    status = getattr(exc, "code", None)
    detail = ""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
    combined = "\n".join(value for value in (detail, str(exc)) if value)
    if status == 429 or is_client_busy(combined):
        marker = "operation in progress"
        if status:
            marker += " (HTTP %s)" % status
        return {"stdout": "", "stderr": marker, "exit_code": -1,
                "transport_error": True, "status": status or 429}
    return {"stdout": "", "stderr": "[\u5ba2\u6237\u673a\u4e0d\u53ef\u8fbe] %s" % exc,
            "exit_code": -1, "transport_error": True}


def _error_message(prefix, text, fallback):
    """Add a context prefix unless the exact busy message must stand alone."""
    message = friendly_error(text, fallback)
    if message == CLIENT_BUSY_MESSAGE:
        return message
    return prefix + message


def friendly_error(text, fallback="\u53d1\u5e03\u6ca1\u6709\u5b8c\u6210\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002"):
    """Turn client ERROR(n)/English codes/stacks into one actionable sentence.

    Raw text is still printed to the bot log by callers for diagnosis; the
    customer only sees a Chinese instruction.  Unknown failures intentionally
    do not echo an English exception back to the chat.
    """
    haystack = str(text or "")
    raw = _last_line(haystack)
    low = raw.lower()
    if not raw:
        return fallback
    if any(k in haystack for k in ("Connection refused", "connection refused", "ConnectTimeout",
                                "Failed to establish", "NewConnectionError", "tunnel")):
        return ("\u5ba2\u6237\u673a\u6ca1\u5f00\uff08\u6216\u8fdc\u7a0b\u901a\u9053\u65ad\u4e86\uff09\uff0c\u5f00\u673a\u540e\u8bf4\u4e00\u58f0\u6211\u5c31\u80fd\u7ee7\u7eed\u3002")
    if CLIENT_BUSY_MESSAGE in haystack or is_client_busy(haystack):
        return CLIENT_BUSY_MESSAGE
    if is_idempotency_consumed(haystack):
        return ("\u8fd9\u6761\u7d20\u6750\u521a\u521a\u5df2\u7ecf\u53d1\u5e03\u6210\u529f\u8fc7\uff0c\u4e3a\u9632\u6b62\u91cd\u590d\u53d1\u5e03\uff0c"
                "\u7cfb\u7edf\u9ed8\u8ba4\u62e6\u4f4f\u4e86\u540c\u4e00\u6761\u91cd\u53d1\u3002")
    if "already submit_intent" in low or "already unknown" in low:
        return ("\u4e0a\u4e00\u6b21\u53d1\u5e03\u8fd8\u6ca1\u6709\u786e\u8ba4\u5b8c\u6210\uff0c\u4e0d\u80fd\u76f4\u63a5\u91cd\u8bd5\u3002"
                "\u8bf7\u5148\u5728\u6296\u97f3\u540e\u53f0\u6838\u5bf9\u662f\u5426\u5df2\u7ecf\u53d1\u51fa\uff0c\u518d\u8054\u7cfb\u7ba1\u7406\u5458\u5904\u7406\u3002")
    if "already reserved" in low:
        return "\u8fd9\u6761\u7d20\u6750\u6b63\u5728\u53d1\u5e03\u6216\u521a\u521a\u4e2d\u65ad\uff0c\u8bf7\u7b49\u5f85\u5b8c\u6210\u540e\u518d\u770b\u7ed3\u679c\u3002"
    if ("frozen" in low or "freeze" in low or "publish_unknown" in low
            or "hold" in low or "\u51bb\u7ed3" in raw):
        return ("\u8d26\u53f7\u5df2\u4e3a\u5b89\u5168\u539f\u56e0\u6682\u505c\u53d1\u5e03\uff0c\u9700\u8981\u7ba1\u7406\u5458\u6838\u5bf9\u540e\u624b\u52a8\u89e3\u9664\u3002"
                "\u8bf7\u5148\u4e0d\u8981\u53cd\u590d\u91cd\u8bd5\u3002")
    if "approval_expired" in low or "approval expired" in low:
        return "\u786e\u8ba4\u5df2\u8fc7\u671f\uff0c\u8bf7\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u751f\u6210\u65b0\u7684\u786e\u8ba4\u3002"
    if "canary" in low:
        return "\u5b89\u5168\u786e\u8ba4\u6ca1\u6709\u5b8c\u6210\uff0c\u8bf7\u91cd\u65b0\u70b9\u51fb\u786e\u8ba4\u5361\u540e\u518d\u8bd5\u3002"
    if "license" in low or "licence" in low or "not_activated" in low or "\u6388\u6743" in raw:
        return ("\u6388\u6743\u72b6\u6001\u4e0d\u5141\u8bb8\u65b0\u53d1\u5e03\uff0c\u8bf7\u5148\u6253\u5f00\u5ba2\u6237\u673a\u5bfc\u5165\u6388\u6743\u6587\u4ef6\uff0c"
                "\u6216\u8054\u7cfb\u7ba1\u7406\u5458\u68c0\u67e5\u6388\u6743\u662f\u5426\u5230\u671f\u3002")
    if "approval_signature" in low or "approval rejected" in low or "receipt" in low and "mismatch" in low:
        return "\u53d1\u5e03\u51ed\u636e\u6821\u9a8c\u672a\u901a\u8fc7\uff0c\u8bf7\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u751f\u6210\u65b0\u786e\u8ba4\u3002"
    if "confirm token" in low or "confirmation" in low or "token \u4e0d\u4e00\u81f4" in raw:
        return "\u786e\u8ba4\u4fe1\u606f\u5df2\u7ecf\u53d8\u5316\uff0c\u8bf7\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u751f\u6210\u65b0\u786e\u8ba4\u3002"
    if "playwright" in low or "chrome" in low or "cdp" in low or "browser" in low:
        return "\u53d1\u5e03\u6d4f\u89c8\u5668\u6ca1\u6709\u542f\u52a8\u6210\u529f\uff0c\u8bf7\u8ba9\u7ba1\u7406\u5458\u68c0\u67e5\u5ba2\u6237\u673a\u7684 Chrome \u548c\u53d1\u5e03\u7ec4\u4ef6\u3002"
    if "lock" in low or "already running" in low or "\u5df2\u6709\u53d1\u5e03" in raw:
        return "\u5ba2\u6237\u673a\u4e0a\u8fd8\u6709\u53d1\u5e03\u4efb\u52a1\u5728\u8dd1\uff0c\u8bf7\u7b49\u5b83\u7ed3\u675f\u540e\u518d\u8bd5\u3002"
    if "timed out" in low or "timeout" in low or "\u8d85\u65f6" in raw:
        return "\u5ba2\u6237\u673a\u54cd\u5e94\u8d85\u65f6\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\uff1b\u5982\u679c\u4e00\u76f4\u8d85\u65f6\uff0c\u8bf7\u68c0\u67e5\u5ba2\u6237\u673a\u662f\u5426\u5f00\u673a\u4e0e\u8054\u7f51\u3002"
    if "not writable" in low or "storage" in low or "disk" in low or "no space" in low:
        return "\u5ba2\u6237\u673a\u78c1\u76d8\u4e0d\u53ef\u5199\u6216\u7a7a\u95f4\u4e0d\u8db3\uff0c\u8bf7\u6e05\u7406\u78c1\u76d8\u540e\u91cd\u8bd5\u3002"
    if "urlopen" in low or "connection" in low or "unreachable" in low or "\u5ba2\u6237\u673a\u4e0d\u53ef\u8fbe" in raw:
        return "\u5ba2\u6237\u673a\u6682\u65f6\u8fde\u4e0d\u4e0a\uff0c\u8bf7\u786e\u8ba4\u5ba2\u6237\u673a\u5f00\u673a\u3001\u8054\u7f51\u4e14\u53d1\u5e03\u52a9\u624b\u6b63\u5728\u8fd0\u884c\u3002"
    clean = re.sub(r"^ERROR\(\d+\)\s*:\s*", "", raw).strip()
    if _has_chinese(clean) and not _looks_like_machine_error(clean):
        return clean[:300]
    return fallback


def _parse_json_blob(text):
    """\u4ece CLI \u8f93\u51fa\u91cc\u53d6\u51fa JSON \u5bf9\u8c61\uff1a\u6574\u6bb5\u89e3\u6790\uff0c\u5931\u8d25\u5219\u622a\u53d6\u9996\u5c3e\u5927\u62ec\u53f7\u3002"""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty")
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("no json object")


def _result_error(result, fallback="\u5ba2\u6237\u673a\u6267\u884c\u5931\u8d25"):
    if isinstance(result, dict) and result.get("runtime_error"):
        return RUNTIME_NOT_CONFIRMED
    raw = (_last_line(result.get("stderr"))
           or _last_line(result.get("stdout"))
           or fallback)
    return friendly_error(raw, fallback)


def _read_client_bytes(path, timeout=60):
    """Read a file from the customer machine (base64 over the exec tunnel)."""
    import base64
    code = (
        "import base64,sys\n"
        "sys.stdout.write(base64.b64encode(open(%s,'rb').read()).decode())\n"
    ) % json.dumps(str(path))
    result = exec_argv([CLI_PY, "-c", code], timeout=timeout)
    out = (result.get("stdout") or "").strip()
    if not _exec_ok(result) or not out:
        raise RuntimeError(_result_error(result, "\u8bfb\u53d6\u5ba2\u6237\u673a\u6587\u4ef6\u5931\u8d25"))
    try:
        return base64.b64decode(out)
    except Exception as exc:
        raise RuntimeError("\u8bfb\u53d6\u5ba2\u6237\u673a\u6587\u4ef6\u5931\u8d25\uff1a\u8fd4\u56de\u5185\u5bb9\u4e0d\u662f base64") from exc


def _write_client_bytes(path, data, timeout=30):
    """Write bytes atomically on the customer machine."""
    import base64
    payload = base64.b64encode(bytes(data)).decode()
    code = (
        "import base64,os\n"
        "p=%s\n"
        "d=os.path.dirname(p)\n"
        "if d and not os.path.isdir(d):\n"
        "    os.makedirs(d, exist_ok=True)\n"
        "tmp=p+'.tmp'\n"
        "fh=open(tmp,'wb')\n"
        "fh.write(base64.b64decode(%s))\n"
        "fh.close()\n"
        "os.replace(tmp,p)\n"
        "print(p)\n"
    ) % (json.dumps(str(path)), json.dumps(payload))
    result = exec_argv([CLI_PY, "-c", code], timeout=timeout)
    if not _exec_ok(result):
        raise RuntimeError(_result_error(result, "\u5199\u5165\u5ba2\u6237\u673a\u6587\u4ef6\u5931\u8d25"))
    return str(path)


def client_receipt_path(plan_path=None):
    """The receipt lives next to the plan, i.e. <worker>\\run\\receipt.json."""
    if plan_path:
        return ntpath.join(ntpath.dirname(str(plan_path)), "receipt.json")
    base = str(WORKER_ROOT).rstrip("\\/")
    sep = "\\" if ("\\" in base or re.match(r"^[A-Za-z]:", base)) else "/"
    return base + sep + "run" + sep + "receipt.json"


def _client_exec_code(code, timeout=60, cwd=None, env=None):
    """Run a short Python program on the customer machine and return /exec JSON."""
    import urllib.request
    if not _runtime_ready():
        return _runtime_blocked_result()
    payload = {"argv": [CLI_PY, "-c", code], "timeout": timeout}
    if cwd:
        payload["cwd"] = cwd
    if env:
        payload["env"] = env
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        TUNNEL, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + TUNNEL_TOKEN})
    try:
        return json.loads(urllib.request.urlopen(
            req, timeout=timeout + 30).read().decode())
    except Exception as exc:
        return _client_failure_result(exc)


#: 1.2.221: the client runs the bot's everyday work as named product
#: operations; arbitrary exec there needs the customer's remote assist.
NAMED_OPS_CAP = "named-ops-1"
REMOTE_ASSIST_HINT = ("\u5ba2\u6237\u673a\u672a\u5f00\u542f\u8fdc\u7a0b\u534f\u52a9\uff1a"
                      "\u8bf7\u5ba2\u6237\u5728\u53d1\u5e03\u52a9\u624b\u9762\u677f\u91cc\u70b9"
                      "\u300c\u5f00\u542f\u8fdc\u7a0b\u534f\u52a9 4 \u5c0f\u65f6\u300d\u540e\u518d\u8bd5\u3002")


def _client_has_named_ops():
    """True once /info (refreshed by ensure_runtime) advertised named ops."""
    with _RUNTIME_LOCK:
        return NAMED_OPS_CAP in (_RUNTIME_STATE.get("capabilities") or [])


def _use_named_ops():
    return _runtime_ready() and _client_has_named_ops()


def _http_error_detail(exc):
    """Text of a relay error; a 403 for remote assist gets a customer hint."""
    body = getattr(exc, "read", None)
    if callable(body):
        try:
            data = json.loads(body().decode("utf-8", "replace") or "{}")
            if data.get("remote_assist_required"):
                return REMOTE_ASSIST_HINT
            if data.get("error"):
                return str(data["error"])
        except Exception:
            pass
    return _last_line(exc) or type(exc).__name__


def _client_named_op(name, args=None, timeout=60):
    """Run one named operation on the client; always returns a dict."""
    try:
        res = _runtime_http("named", {"name": name, "args": args or {}},
                            timeout=timeout + 30)
    except Exception as exc:
        msg = _http_error_detail(exc)
        return {"ok": False, "err": msg, "error": msg, "transport_error": True}
    if not isinstance(res, dict):
        return {"ok": False, "err": "\u8fd4\u56de\u683c\u5f0f\u9519\u8bef"}
    if not res.get("ok") and not res.get("err") and res.get("error"):
        res["err"] = res["error"]
    return res


def _plan_make_args(files, topics_text="", title="", xingtu_task=None,
                    batch_id=None, item_topics=None):
    args = {"files": [item["path"] for item in _file_entries(files)],
            "topics": topics_text, "env_id": str(PUBLISH_ENV or "env_1"),
            "default_title": DEFAULT_PUBLISH_TITLE}
    if title:
        args["title"] = title
    if xingtu_task:
        args["xingtu_task"] = xingtu_task
    if batch_id:
        args["batch_id"] = str(batch_id)
    if item_topics is not None:
        args["item_topics"] = [list(v or []) for v in item_topics]
    return args


INBOX_SCAN = r"""
import os,glob,json
base=os.environ.get("BT_WORKER_BASE") or ((os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local"))+r"\HuolongAgent\publish-worker")
roots=[]
def _add(p):
    if p and p not in roots: roots.append(p)
for d in glob.glob(os.path.join(base,"customers","*","inbox")):
    _add(d)
_add(os.path.join(base,"inbox"))
home=os.environ.get("USERPROFILE") or os.path.expanduser("~")
cands=[os.path.join(home,"Desktop","\u53d1\u5e03\u7d20\u6750")]
for env in ("OneDrive","OneDriveConsumer","OneDriveCommercial"):
    v=os.environ.get(env)
    if v: cands.append(os.path.join(v,"Desktop","\u53d1\u5e03\u7d20\u6750"))
try:
    import winreg
    k=winreg.OpenKey(winreg.HKEY_CURRENT_USER,r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
    v=os.path.expandvars(winreg.QueryValueEx(k,"Desktop")[0])
    cands.append(os.path.join(v,"\u53d1\u5e03\u7d20\u6750"))
except Exception:
    pass
for c in cands:
    try:
        if os.path.isdir(c): _add(c)
    except OSError:
        pass
files=[]
seen=set()
for inbox in roots:
    for f in glob.glob(os.path.join(inbox,"*")):
        try:
            if not os.path.isfile(f): continue
            if not f.lower().endswith((".mp4",".mov")): continue
            name=os.path.basename(f)
            if name.startswith(".") or name.startswith("~$"): continue
            size=os.path.getsize(f)
            if size<=0: continue
            key=(name.lower(),size)
            if key in seen: continue
            seen.add(key)
            files.append({"path":f,"name":name,"mtime":os.path.getmtime(f),
                          "size":size})
        except OSError:
            continue
files.sort(key=lambda x:(x["mtime"],x["name"].lower()))
s={}
paths=[os.path.join(base,"config","settings.json"),os.path.join(base,"settings.json")]
for d in glob.glob(os.path.join(base,"customers","*","config","settings.json")):
    paths.append(d)
for sp in paths:
    try:
        s=json.load(open(sp,encoding="utf-8-sig"))
        if isinstance(s,dict): break
        s={}
    except Exception:
        s={}
g=s.get("schedule") if isinstance(s,dict) else {}
if not isinstance(g,dict): g={}
def _int(v,d):
    try: return int(v)
    except Exception: return d
gap_min=_int(g.get("gap_min"),5)
gap_max=_int(g.get("gap_max"),8)
if gap_min<1 or gap_min>120: gap_min=5
if gap_max<1 or gap_max>120: gap_max=8
if gap_min>gap_max: gap_min,gap_max=gap_max,gap_min
print(json.dumps({"ok":True,"files":files,"gap_min":gap_min,
                  "gap_max":gap_max},ensure_ascii=True))
"""


def scan_inbox():
    """List every publishable video in the customer's inbox, oldest first."""
    if _use_named_ops():
        info = _client_named_op("inbox-scan", timeout=30)
        if not info.get("ok"):
            return {"ok": False, "err": info.get("err") or "\u8bfb\u53d6 inbox \u5931\u8d25"}
        return info
    result = _client_exec_code(INBOX_SCAN, timeout=30, cwd=CLI_ROOT)
    out = (result.get("stdout") or "").strip()
    if not _exec_ok(result) or not out:
        return {"ok": False, "err": _result_error(result, "\u8bfb\u53d6 inbox \u5931\u8d25")}
    try:
        info = json.loads(out.splitlines()[-1])
    except Exception:
        return {"ok": False, "err": "inbox \u8fd4\u56de\u4e0d\u662f\u6709\u6548 JSON"}
    if not isinstance(info, dict):
        return {"ok": False, "err": "inbox \u8fd4\u56de\u683c\u5f0f\u9519\u8bef"}
    return info


def _file_entries(files):
    """Normalise scan/state entries to ``[{'path','name'}]``."""
    out = []
    for item in files or []:
        if isinstance(item, dict):
            path = str(item.get("path") or "")
            name = str(item.get("name") or "")
        else:
            path = str(item or "")
            name = ""
        if not path:
            continue
        if not name:
            name = os.path.basename(path.replace("\\", "/"))
        out.append({"path": path, "name": name})
    return out


def _env_json_line(name, value):
    """Assign JSON text to an env var through a Python string literal."""
    text = json.dumps(value, ensure_ascii=True)
    return 'import os;os.environ["%s"]=%s\n' % (
        name, json.dumps(text, ensure_ascii=True))


def _publish_plan_prelude(topics, files, title="", xingtu_task=None):
    pre = 'import os;os.environ["BT_TOPICS"]=%s\n' % json.dumps(
        " ".join(topics or []), ensure_ascii=True
    )
    paths = [item["path"] for item in _file_entries(files)]
    if paths:
        pre += _env_json_line("BT_FILES", paths)
    if title:
        pre += 'import os;os.environ["BT_TITLE"]=%s\n' % json.dumps(
            title, ensure_ascii=True
        )
    if xingtu_task:
        pre += _env_json_line("BT_XINGTU_TASK", xingtu_task)
    pre += _env_json_line("BT_ENV_ID", PUBLISH_ENV)
    return pre


def new_batch_id():
    return time.strftime("%Y%m%d%H%M%S") + "-" + os.urandom(4).hex()


def _batch_path(batch_id):
    value = str(batch_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", value):
        return None
    return os.path.join(BATCH_DIR, value + ".json")


def save_batch(state):
    path = _batch_path((state or {}).get("batch_id"))
    if not path:
        raise ValueError("bad batch id")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def load_batch(batch_id):
    path = _batch_path(batch_id)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    if str(state.get("batch_id")) != str(batch_id):
        return None
    return state


def _batch_chat_key(chat_id):
    return hashlib.sha1(str(chat_id).encode("utf-8")).hexdigest()[:16]


def _latest_batch_path(chat_id):
    return os.path.join(BATCH_DIR, "latest_" + _batch_chat_key(chat_id) + ".json")


def save_latest_batch(chat_id, batch_id):
    os.makedirs(BATCH_DIR, exist_ok=True)
    path = _latest_batch_path(chat_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"batch_id": str(batch_id), "updated_at": time.time()},
                  fh, ensure_ascii=False)
    os.replace(tmp, path)


def load_latest_batch(chat_id):
    try:
        with open(_latest_batch_path(chat_id), encoding="utf-8") as fh:
            pointer = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(pointer, dict):
        return None
    return load_batch(pointer.get("batch_id"))


def _update_batch_item(batch_id, item_index, **fields):
    if not batch_id:
        return None
    state = load_batch(batch_id)
    if not state:
        return None
    for item in state.get("items") or []:
        if int(item.get("index") or 0) == int(item_index or 0):
            item.update(fields)
            item["finished_at"] = time.time()
            break
    try:
        save_batch(state)
    except (OSError, ValueError):
        return None
    return state


def _batch_counts(state):
    counts = {"published": 0, "failed": 0, "skipped": 0,
              "needs_reconcile": 0, "planned": 0, "running": 0}
    for item in (state or {}).get("items") or []:
        status = str(item.get("status") or "planned")
        counts[status] = counts.get(status, 0) + 1
    return counts


CANCEL_ITEM_ERROR = "cancelled by customer"


def _batch_cancel_requested(state):
    """Read the durable cancel flag, refreshing a loop's local copy."""
    if not isinstance(state, dict):
        return False
    batch_id = state.get("batch_id")
    disk = load_batch(batch_id) if batch_id else None
    if isinstance(disk, dict) and disk.get("cancel_requested"):
        state["cancel_requested"] = True
        if disk.get("cancel_requested_at") is not None:
            state["cancel_requested_at"] = disk.get("cancel_requested_at")
        return True
    return bool(state.get("cancel_requested"))


def _save_batch_preserving_cancel(state):
    """Save loop state without losing a cancel flag written by another thread."""
    with BATCH_LOCK:
        disk = load_batch((state or {}).get("batch_id"))
        if isinstance(disk, dict) and disk.get("cancel_requested"):
            state["cancel_requested"] = True
            if disk.get("cancel_requested_at") is not None:
                state.setdefault("cancel_requested_at",
                                 disk.get("cancel_requested_at"))
        save_batch(state)


def _cancel_skipped_count(state):
    return sum(1 for item in (state or {}).get("items") or []
               if item.get("status") == "skipped"
               and item.get("error") == CANCEL_ITEM_ERROR)


def _skip_unstarted_for_cancel(state):
    """Skip every item that has not started; never touch an in-flight one."""
    skipped = 0
    for item in state.get("items") or []:
        status = item.get("status") or "planned"
        if status in ("published", "failed", "skipped",
                      "needs_reconcile", "running"):
            continue
        item["status"] = "skipped"
        item["error"] = CANCEL_ITEM_ERROR
        item["finished_at"] = time.time()
        skipped += 1
    state["cancel_requested"] = True
    _save_batch_preserving_cancel(state)
    return skipped


def _finish_batch_cancelled(chat_id, state):
    counts = _batch_counts(state)
    if counts.get("running", 0):
        return False
    state["status"] = "cancelled"
    state["finished_at"] = time.time()
    state["cancelled_at"] = state.get("cancelled_at") or time.time()
    _save_batch_preserving_cancel(state)
    reply(chat_id, "\u5df2\u53d1 %d \u6761\uff0c\u53d6\u6d88 %d \u6761\u3002"
          % (counts.get("published", 0), _cancel_skipped_count(state)))
    return True


def _wait_batch_gap(state, target):
    """Wait for the inter-item gap while polling cancellation every 2s."""
    while True:
        if _batch_cancel_requested(state):
            return True
        remaining = float(target) - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(remaining, 2.0))


def cancel_batch(chat_id, batch_id=None):
    """Customer cancel entry used by both text commands and the card button."""
    state = load_batch(batch_id) if batch_id else load_latest_batch(chat_id)
    if not state:
        reply(chat_id, "\u6ca1\u6709\u53ef\u53d6\u6d88\u7684\u53d1\u5e03\u3002")
        return
    if str(state.get("chat_id")) != str(chat_id):
        reply(chat_id, "\u8fd9\u4e2a\u6279\u6b21\u4e0d\u5c5e\u4e8e\u5f53\u524d\u4f1a\u8bdd\uff0c\u62d2\u7edd\u53d6\u6d88\u3002")
        return
    with BATCH_LOCK:
        state = load_batch(state.get("batch_id"))
        if not state:
            reply(chat_id, "\u6ca1\u6709\u53ef\u53d6\u6d88\u7684\u53d1\u5e03\u3002")
            return
        status = str(state.get("status") or "")
        if status == "pending":
            state["status"] = "cancelled"
            state["cancel_reason"] = "cancelled by customer"
            state["cancelled_at"] = time.time()
            save_batch(state)
            reply(chat_id, "\u5df2\u53d6\u6d88\u8fd9\u6279\u53d1\u5e03\uff0c\u4e00\u6761\u90fd\u6ca1\u53d1\u3002")
            return
        if status in ("confirmed", "running"):
            state["cancel_requested"] = True
            state["cancel_requested_at"] = time.time()
            save_batch(state)
            reply(chat_id, "\u6536\u5230\uff0c\u6b63\u5728\u53d1\u7684\u90a3\u6761\u53d1\u5b8c\u540e\u505c\u6b62\u5269\u4f59\uff0c\u672a\u5f00\u59cb\u7684\u5168\u90e8\u53d6\u6d88\u3002")
            return
    reply(chat_id, "\u8fd9\u6279\u5df2\u7ecf\u7ed3\u675f\uff0c\u6ca1\u6709\u53ef\u53d6\u6d88\u7684\u5185\u5bb9\u3002")


def _batch_gap_seconds(state):
    import random
    low = max(1, int((state or {}).get("gap_min") or 5))
    high = max(low, int((state or {}).get("gap_max") or 8))
    return random.randint(low * 60, high * 60)


def _sleep_until(target):
    while True:
        remaining = float(target) - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 60.0))


def _next_schedule_at(delay_seconds=5):
    import datetime as _dt
    tz = _dt.timezone(_dt.timedelta(hours=8))
    return (_dt.datetime.now(tz) + _dt.timedelta(
        seconds=int(delay_seconds))).replace(microsecond=0).isoformat()


def _wait_until_schedule(schedule_at):
    """Small pre-submit wait; the single-item path has its own verified wait."""
    import datetime as _dt
    try:
        due = _dt.datetime.fromisoformat(str(schedule_at).replace("Z", "+00:00"))
        if due.tzinfo is None:
            due = due.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
        remaining = (due - _dt.datetime.now(due.tzinfo)).total_seconds()
    except Exception:
        return
    if 0 < remaining <= 60:
        _sleep_until(time.monotonic() + remaining)


def _update_plan_time(plan_path, schedule_at):
    if _use_named_ops():
        info = _client_named_op("plan-set-time", {"plan": str(plan_path),
                                                  "schedule_at": str(schedule_at)}, timeout=30)
        if not info.get("ok") and not info.get("err"):
            info["err"] = "\u66f4\u65b0\u8ba1\u5212\u65f6\u95f4\u5931\u8d25"
        return info
    code = (
        "import json,os\n"
        "p=%s\n"
        "s=%s\n"
        "d=json.load(open(p,encoding='utf-8'))\n"
        "d['schedule_at']=s\n"
        "tmp=p+'.tmp'\n"
        "open(tmp,'w',encoding='utf-8').write(json.dumps(d,ensure_ascii=False,indent=1))\n"
        "os.replace(tmp,p)\n"
        "print(json.dumps({'ok':True,'plan_id':d.get('plan_id'),"
        "'schedule_at':d.get('schedule_at'),"
        "'content_hash':(d.get('compliance') or {}).get('content_hash')},"
        "ensure_ascii=True))\n"
    ) % (json.dumps(str(plan_path)), json.dumps(str(schedule_at)))
    result = _client_exec_code(code, timeout=30, cwd=CLI_ROOT)
    out = (result.get("stdout") or "").strip()
    if not _exec_ok(result) or not out:
        return {"ok": False, "err": _result_error(result, "\u66f4\u65b0\u8ba1\u5212\u65f6\u95f4\u5931\u8d25")}
    try:
        info = json.loads(out.splitlines()[-1])
    except Exception:
        return {"ok": False, "err": "\u66f4\u65b0\u8ba1\u5212\u65f6\u95f4\u8fd4\u56de\u4e0d\u662f\u6709\u6548 JSON"}
    return info if isinstance(info, dict) else {"ok": False, "err": "\u8fd4\u56de\u683c\u5f0f\u9519\u8bef"}


def reconcile_interrupted_batches():
    """Fail closed after a bot restart: never retry an in-flight publish."""
    try:
        names = sorted(os.listdir(BATCH_DIR))
    except OSError:
        return []
    handled = []
    for name in names:
        if not name.endswith(".json") or name.startswith("latest_"):
            continue
        state = load_batch(name[:-5])
        if not state or state.get("status") not in ("confirmed", "running"):
            continue
        if state.get("cancel_requested"):
            reconciled = 0
            for item in state.get("items") or []:
                status = item.get("status") or "planned"
                if status == "running":
                    item["status"] = "needs_reconcile"
                    item["error"] = "bot restarted during publish; verify manually"
                    reconciled += 1
                elif status not in ("published", "failed", "skipped",
                                    "needs_reconcile"):
                    item["status"] = "skipped"
                    item["error"] = CANCEL_ITEM_ERROR
                    item["finished_at"] = time.time()
            state["status"] = "cancelled"
            state["reason"] = "cancelled by customer before restart"
            state["finished_at"] = time.time()
            _save_batch_preserving_cancel(state)
            counts = _batch_counts(state)
            text = ("\u5df2\u53d1 %d \u6761\uff0c\u53d6\u6d88 %d \u6761\u3002"
                    % (counts.get("published", 0), _cancel_skipped_count(state)))
            if reconciled:
                text += ("\u91cd\u542f\u65f6\u6709 %d \u6761\u6b63\u5728\u53d1\u5e03\uff0c"
                         "\u9700\u4eba\u5de5\u6838\u5bf9\u3002" % reconciled)
            try:
                reply(state.get("chat_id"), text)
            except Exception as exc:
                sys.stderr.write("[batch] cancel reconcile reply failed: %s\n" % exc)
            handled.append(state.get("batch_id"))
            continue
        for item in state.get("items") or []:
            if item.get("status") == "running":
                item["status"] = "needs_reconcile"
                item["error"] = "bot restarted during publish; verify manually"
        state["status"] = "needs_reconcile"
        state["reason"] = "bot restarted before the batch finished"
        save_batch(state)
        counts = _batch_counts(state)
        try:
            reply(state.get("chat_id"), "\u4e0a\u6b21\u6279\u91cf\u53d1\u5e03\u4e2d\u65ad\uff1a\u5df2\u53d1 %d \u6761\uff0c\u5931\u8d25 %d \u6761\uff0c"
                  "\u672a\u5b8c\u6210 %d \u6761\u3002\u4e3a\u907f\u514d\u91cd\u590d\u53d1\u5e03\uff0c\u6ca1\u6709\u81ea\u52a8\u91cd\u8bd5\uff1b\u8bf7\u6838\u5bf9\u540e\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u3002"
                  % (counts.get("published", 0), counts.get("failed", 0),
                     counts.get("planned", 0) + counts.get("running", 0)
                     + counts.get("needs_reconcile", 0)))
        except Exception as exc:
            sys.stderr.write("[batch] reconcile reply failed: %s\n" % exc)
        handled.append(state.get("batch_id"))
    return handled


_PUBLISH_PY = None
_PUBLISH_PY_LOCK = threading.Lock()


def _client_publish_python():
    """Resolve the worker venv path on the client using its LOCALAPPDATA."""
    code = (
        "import os,sys\n"
        "candidates=[%s]\n"
        "root=%s\n"
        "candidates += [os.path.join(root,'.venv','Scripts','python.exe'),"
        " os.path.join(root,'runtime','python.exe')]\n"
        "la=os.environ.get('LOCALAPPDATA') or ''\n"
        "if la:\n"
        "    candidates.append(os.path.join(la, 'HuolongAgent',"
        " 'publish-worker','.venv','Scripts','python.exe'))\n"
        "for c in candidates:\n"
        "    if c and os.path.exists(c):\n"
        "        sys.stdout.write(c)\n"
        "        break\n"
    ) % (json.dumps(str(PY_EXE)), json.dumps(str(CLI_ROOT)))
    result = exec_argv([CLI_PY, "-c", code], timeout=20)
    if not _exec_ok(result):
        return None
    lines = [ln.strip() for ln in (result.get("stdout") or "").splitlines()
             if ln.strip()]
    return lines[-1] if lines else None


def publish_python():
    """Use the worker venv (has playwright) when present, else runtime python."""
    global _PUBLISH_PY
    with _PUBLISH_PY_LOCK:
        if _PUBLISH_PY:
            return _PUBLISH_PY
        try:
            found = PY_EXE if os.path.exists(PY_EXE) else _client_publish_python()
        except Exception:
            found = None
        _PUBLISH_PY = found or CLI_PY
        return _PUBLISH_PY


def _reset_publish_python_cache():
    global _PUBLISH_PY
    with _PUBLISH_PY_LOCK:
        _PUBLISH_PY = None


def _approver_id(chat_id):
    value = re.sub(r"[^A-Za-z0-9._:\-]", "", "feishu-" + str(chat_id or "user"))
    return (value or "feishu-user")[:64]


def _load_plan_for_receipt(pending):
    path = str(pending.get("plan") or "")
    if not path:
        raise ValueError("\u5f85\u53d1\u5e03\u8ba1\u5212\u8def\u5f84\u7f3a\u5931")
    if os.path.exists(path):
        with open(path, "rb") as fh:
            raw = fh.read()
    else:
        raw = _read_client_bytes(path)
    plan = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(plan, dict) or not plan:
        raise ValueError("\u8ba1\u5212\u6587\u4ef6\u4e3a\u7a7a\u6216\u683c\u5f0f\u9519\u8bef")
    return plan

def run_on_client(py_code, timeout=45):
    """\u5728\u5ba2\u6237\u673a(Win)\u4e0a\u6267\u884c\u4e00\u6bb5 python\uff0c\u8fd4\u56de stdout"""
    import urllib.request
    if not _runtime_ready():
        return "[" + RUNTIME_NOT_CONFIRMED + "]"
    body = json.dumps({"argv": [CLI_PY, "-c", py_code], "timeout": timeout}).encode()
    req = urllib.request.Request(TUNNEL, data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + TUNNEL_TOKEN})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=timeout + 30).read().decode())
        return (d.get("stdout") or "") + (("\n[stderr] " + d["stderr"][:300]) if d.get("stderr") else "")
    except Exception as e:
        return f"[\u5ba2\u6237\u673a\u4e0d\u53ef\u8fbe] {e}"

def status_text():
    if _use_named_ops():
        info = _client_named_op("agent-health", timeout=15)
        if not info.get("ok"):
            return ("\u5ba2\u6237\u673a\u72b6\u6001\u67e5\u8be2\u6ca1\u6709\u5b8c\u6210\uff1a"
                    + friendly_error(info.get("err"), "\u8bf7\u786e\u8ba4\u5ba2\u6237\u673a\u5f00\u673a\u3001\u8054\u7f51\u4e14\u53d1\u5e03\u52a9\u624b\u6b63\u5728\u8fd0\u884c\u3002"))
        health = info.get("health")
        text = health if isinstance(health, str) else json.dumps(health, ensure_ascii=False)
        return "\u5ba2\u6237\u673a\u72b6\u6001\uff1a\n" + (text.strip()[:600] or "\u65e0\u8fd4\u56de")
    out = run_on_client('import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:8795/health",timeout=5).read().decode())')
    if "[stderr]" in out or "\u5ba2\u6237\u673a\u4e0d\u53ef\u8fbe" in out:
        return ("\u5ba2\u6237\u673a\u72b6\u6001\u67e5\u8be2\u6ca1\u6709\u5b8c\u6210\uff1a"
                + friendly_error(out, "\u8bf7\u786e\u8ba4\u5ba2\u6237\u673a\u5f00\u673a\u3001\u8054\u7f51\u4e14\u53d1\u5e03\u52a9\u624b\u6b63\u5728\u8fd0\u884c\u3002"))
    return "\u5ba2\u6237\u673a\u72b6\u6001\uff1a\n" + (out.strip()[:600] or "\u65e0\u8fd4\u56de")

CLI_ROOT = os.environ.get("CLI_ROOT", r"C:\\Users\\<USER>\\Desktop\\manju-publish-assistant-1\\manju-publish-assistant-1.2.34-win64")
RUNTIME_NOT_CONFIRMED = "\u65e0\u6cd5\u786e\u8ba4\u53d1\u5e03\u52a9\u624b\u5f53\u524d\u8fd0\u884c\u65f6\u7248\u672c\uff0c\u5df2\u505c\u6b62\uff0c\u907f\u514d\u8bef\u7528\u65e7\u7248\u672c\u3002\u8bf7\u91cd\u542f\u53d1\u5e03\u52a9\u624b\u540e\u518d\u8bd5\u3002"

#: Runtime convergence.  The resident agent is the source of truth: /info
#: reports its program directory and interpreter.  Every request is then sent
#: to that runtime, never to a stale version directory left on disk.
_RUNTIME_LOCK = threading.RLock()
_RUNTIME_STATE = {"key": "", "checked_at": 0.0}
_RUNTIME_ENSURING = False


def _runtime_http(operation, payload=None, timeout=20):
    import urllib.request
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        _tunnel_op_url(operation), data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + TUNNEL_TOKEN},
        method="POST" if body is not None else "GET")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())


def _runtime_blocked_result():
    """Fail closed instead of falling back to a stale version directory."""
    return {"stdout": "", "stderr": RUNTIME_NOT_CONFIRMED,
            "exit_code": -1, "transport_error": True, "runtime_error": True}


def _runtime_ready():
    try:
        return bool(ensure_runtime())
    except Exception:
        return False


def _runtime_version_matches(program, version):
    if not program or not version:
        return False
    import base64
    import urllib.request
    path = ntpath.join(str(program), "VERSION")
    body = json.dumps({"path": path}).encode()
    req = urllib.request.Request(
        _tunnel_op_url("read"), data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + TUNNEL_TOKEN})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
        text = base64.b64decode(data.get("base64") or "", validate=True).decode(
            "utf-8-sig").strip()
    except Exception:
        return False
    return text == str(version).strip()


def _runtime_from_agent_pid(pid):
    """Old agent fallback: ask Windows for the resident process image path."""
    code = r"""
import ctypes,json,os
pid=int(%d)
k=ctypes.WinDLL('kernel32',use_last_error=True)
h=k.OpenProcess(0x1000,False,pid)
if not h:
    print(json.dumps({})); raise SystemExit
buf=ctypes.create_unicode_buffer(32768)
size=ctypes.c_uint(32768)
ok=k.QueryFullProcessImageNameW(h,0,buf,ctypes.byref(size))
k.CloseHandle(h)
exe=buf.value if ok else ''
p=os.path.dirname(exe)
program=''
for _ in range(7):
    if (os.path.isfile(os.path.join(p,'VERSION'))
            and os.path.isdir(os.path.join(p,'agent','winport'))):
        program=p; break
    q=os.path.dirname(p)
    if q==p: break
    p=q
print(json.dumps({'program':program,'exe':exe}))
""" % int(pid)
    try:
        result = _runtime_http("exec", {"argv": [CLI_PY, "-c", code],
                                        "timeout": 30}, timeout=45)
        text = (result.get("stdout") or "").strip()
        return json.loads(text.splitlines()[-1]) if text else {}
    except Exception:
        return {}


def _set_runtime(program, version, source):
    global CLI_ROOT, CLI_PY, _PUBLISH_PY
    if not program:
        return False
    root = str(program).rstrip("\\/")
    py = ntpath.join(root, "runtime", "python.exe")
    with _RUNTIME_LOCK:
        CLI_ROOT = root
        CLI_PY = py
        _PUBLISH_PY = None
        _RUNTIME_STATE.update({"key": str(version) + "|" + root,
                               "checked_at": time.time(), "source": source,
                               "python": py})
    return True


def ensure_runtime(force=False):
    """Converge CLI_ROOT/CLI_PY onto the resident agent's runtime."""
    global _RUNTIME_ENSURING
    with _RUNTIME_LOCK:
        if _RUNTIME_ENSURING:
            return bool(_RUNTIME_STATE.get("key"))
        # Re-read /info for every request.  A cached directory could point at
        # the previous version after an in-place upgrade and would then be
        # used by /exec even though it is no longer the resident runtime.
        _RUNTIME_ENSURING = True
    try:
        info = _runtime_http("info", timeout=10)
        version = str(info.get("version") or "")
        program = str(info.get("program") or "")
        python = str(info.get("python") or "")
        source = "info"
        if not program and info.get("pid"):
            found = _runtime_from_agent_pid(info.get("pid"))
            program = str(found.get("program") or "")
            source = "pid-image"
        if not program:
            return False
        if version and not _runtime_version_matches(program, version):
            return False
        if not _set_runtime(program, version, source):
            return False
        if python:
            with _RUNTIME_LOCK:
                globals()["CLI_PY"] = python
                _RUNTIME_STATE["python"] = python
        with _RUNTIME_LOCK:
            _RUNTIME_STATE["capabilities"] = [
                str(c) for c in (info.get("capabilities") or []) if isinstance(c, str)]
        return True
    except Exception:
        return False
    finally:
        with _RUNTIME_LOCK:
            _RUNTIME_ENSURING = False

def run_cli(args, timeout=115, customer_id=None):
    """\u5728\u5ba2\u6237\u673a\u8dd1 winport CLI \u5b50\u547d\u4ee4\u3002

    ``customer_id`` \u5b58\u5728\u65f6\u628a WINPORT_DATA/WINPORT_SYSTEM \u6307\u5411\u8be5\u5ba2\u6237\u76ee\u5f55\uff0c
    \u8fd9\u6837\u540c\u4e00\u4e2a CLI \u80fd\u670d\u52a1\u591a\u53f0\u5ba2\u6237\u673a/\u591a\u4e2a\u5ba2\u6237\u800c\u4e0d\u4e32\u6570\u636e\u3002
    """
    import urllib.request
    if not _runtime_ready():
        return _runtime_blocked_result()
    # dry-run / publish / xingtu-tasks need playwright, which the bundled
    # runtime python may lack.
    interpreter = CLI_PY
    if args and args[0] in ("dry-run", "publish", "xingtu-tasks"):
        try:
            interpreter = publish_python()
        except Exception:
            interpreter = CLI_PY
    payload = {"argv": [interpreter, "-m", "agent.winport.cli"] + args,
               "timeout": timeout, "cwd": CLI_ROOT}
    cid = str(customer_id or DEFAULT_CUSTOMER_ID)
    if cid != DEFAULT_CUSTOMER_ID:
        root = customer_data_root(cid)
        payload["env"] = {"WINPORT_DATA": root,
                          "WINPORT_SYSTEM": os.path.join(root, "system")}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(TUNNEL, data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + TUNNEL_TOKEN})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=timeout + 60).read().decode())
        return d
    except Exception as e:
        return {"stdout": "", "stderr": f"[\u5ba2\u6237\u673a\u4e0d\u53ef\u8fbe] {e}", "exit_code": -1}

def newest_report(customer_id=None):
    base = customer_data_root(customer_id)
    out = run_on_client(
        'import os,glob\\n'
        'r=r"%s"\\n'
        'fs=[]\\n'
        'for p in glob.glob(os.path.join(r,"out","**","*.xlsx"),recursive=True)+glob.glob(os.path.join(r,"**","*.xlsx"),recursive=True):\\n'
        '    fs.append((os.path.getmtime(p),p))\\n'
        'fs.sort(reverse=True)\\n'
        'print(fs[0][1] if fs else "")' % base.replace("\\","\\\\"))
    return out.strip().splitlines()[-1] if out.strip() else ""


def send_local_file(chat_id, path):
    from lark_oapi.api.im.v1 import CreateFileRequestBody, CreateFileRequest, CreateMessageRequestBody, CreateMessageRequest
    name = os.path.basename(path)
    fb = CreateFileRequestBody.builder().file_type("xlsx").file_name(name).file(open(path, "rb")).build()
    fr = client.im.v1.file.create(CreateFileRequest.builder().request_body(fb).build())
    if not fr.success():
        reply(chat_id, "\u8868\u683c\u4e0a\u4f20\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\uff1b\u5982\u4ecd\u5931\u8d25\u8bf7\u8054\u7cfb\u7ba1\u7406\u5458\u3002")
        return False
    body = CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("file") \
        .content(json.dumps({"file_key": fr.data.file_key})).build()
    client.im.v1.message.create(CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build())
    return True


def send_file(chat_id, path):
    """\u628a\u5ba2\u6237\u673a\u4e0a\u7684\u6587\u4ef6\u8bfb\u56de\u6765\u53d1\u7ed9\u7528\u6237"""
    import base64, urllib.request
    raw = None
    if os.path.exists(path):
        raw = open(path, "rb").read()
    b64 = "" if raw is not None else (
        run_on_client('import base64;print(base64.b64encode(open(r"%s","rb").read()).decode())' % path, timeout=120).strip()
        .splitlines()[-1] if run_on_client('import base64;print(base64.b64encode(open(r"%s","rb").read()).decode())' % path, timeout=120).strip() else "")
    if b64:
        reply(chat_id, "\u6ca1\u627e\u5230\u751f\u6210\u7684\u8868\u683c\u6587\u4ef6\u3002")
        return
    if raw is None:
        raw = base64.b64decode(b64)
    name = os.path.basename(path)
    tmp = os.path.join("/tmp", name)
    open(tmp, "wb").write(raw)
    from lark_oapi.api.im.v1 import CreateFileRequestBody, CreateFileRequest, CreateMessageRequestBody, CreateMessageRequest
    fb = CreateFileRequestBody.builder().file_type("xlsx").file_name(name).file(open(tmp, "rb")).build()
    fr = client.im.v1.file.create(CreateFileRequest.builder().request_body(fb).build())
    if not fr.success():
        reply(chat_id, "\u8868\u683c\u4e0a\u4f20\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\uff1b\u5982\u4ecd\u5931\u8d25\u8bf7\u8054\u7cfb\u7ba1\u7406\u5458\u3002")
        return
    body = CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("file") \
        .content(json.dumps({"file_key": fr.data.file_key})).build()
    r = client.im.v1.message.create(CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build())
    if not r.success():
        print("[send_file-fail]", r.code, r.msg, flush=True)
    return r

PUBLISH_RECEIPT = {"ts": 0.0}


def mark_publish_receipt():
    """Only a real publish call may set this; chat text never can."""
    PUBLISH_RECEIPT["ts"] = time.time()


def _publish_receipt_fresh(window=600):
    return (time.time() - PUBLISH_RECEIPT.get("ts", 0.0)) <= window


_SUCCESS_CLAIM = re.compile(
    "(已发布|发完了|发布成功|已成功发布|"
    "共成功发布|发布完成|已发出去|任务完成)")


def scrub_fake_success(text):
    """Block success wording that has no real publish receipt behind it."""
    if not text or not _SUCCESS_CLAIM.search(text):
        return text
    return "\u53d1\u5e03\u72b6\u6001\u8bf7\u4ee5\u672c\u6b21\u6307\u4ee4\u7684\u7ed3\u679c\u901a\u77e5\u6216\u8fdb\u5ea6\u67e5\u8be2\u4e3a\u51c6\u3002"


CHAT_HIST = {}


def _hist_add(chat_id, who, text):
    try:
        h = CHAT_HIST.setdefault(chat_id, [])
        h.append([who, (text or "")[:300]])
        del h[:-12]
    except Exception:
        pass


_TITLE_DRAFT_RE = re.compile(
    r"(\u6807\u9898[^\n\uff08(]{0,10}[\uff08(]\s*)(\d+)"
    r"(\s*\u4e2a?\s*\u5b57\s*[\uff09)]\s*[\uff1a:]\s*)([^\n]+)")


def fix_title_draft(text, limit=30):
    """Rewrite an LLM title draft so the title and its stated length agree.

    The model drafts titles in free text and routinely miscounts Chinese
    characters, e.g. it says 30 but ships 33.  The client rejects any title
    over 30, so clamp the title here and print the real length.
    """
    def _sub(match):
        title = clamp_publish_title(match.group(4).strip(), limit=limit)
        return match.group(1) + str(len(title)) + match.group(3) + title
    return _TITLE_DRAFT_RE.sub(_sub, str(text or ""))


def reply(chat_id, text, *, message_id=None):
    text = fix_title_draft(text, TITLE_LIMIT.get(chat_id, 30))
    _hist_add(chat_id, "客服", text)
    if client is None:
        print(f"[reply-skipped] {chat_id}: {text}", flush=True)
        return None
    builder = CreateMessageRequestBody.builder() \
        .receive_id(chat_id).msg_type("text") \
        .content(json.dumps({"text": text}, ensure_ascii=False))
    if message_id:
        builder = builder.uuid(message_id)
    body = builder.build()
    req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
    last = None
    for attempt in range(3):
        try:
            r = client.im.v1.message.create(req)
        except Exception as exc:  # network reset on the SG -> Feishu link
            last = exc
            print(f"[reply-retry] {attempt + 1} {type(exc).__name__}: {exc}", flush=True)
            time.sleep(0.8 * (attempt + 1))
            continue
        if not r.success():
            raise RuntimeError("Feishu rejected message: %s" % r.code)
        else:
            print("[reply-sent] %s %s" % (chat_id, (text or "")[:40].replace("\n", " ")), flush=True)
        return r
    print(f"[reply-error] {type(last).__name__}: {last}", flush=True)
    raise last

LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-flash")
LLM_KEY = ""
try:
    import re as _re
    _cfg = open(HERMES_CONFIG, encoding="utf-8").read()
    _m = _re.search(r"api_key:\s*(sk-[A-Za-z0-9]+)", _cfg)
    if _m:
        LLM_KEY = _m.group(1)
except Exception:
    pass

ACTIONS = {"collect": "4", "report": "5", "status": "6",
           "publish": "1", "reschedule": "2", "pause": "3"}

SYS = "\u4f60\u662f\u300c\u77ed\u89c6\u9891\u53d1\u5e03\u52a9\u624b\u300d\uff0c\u670d\u52a1\u5bf9\u8c61\u662f\u77ed\u89c6\u9891\u8fd0\u8425\u4eba\u5458\u3002\u4f60\u8d1f\u8d23\u628a\u7528\u6237\u7684\u4e00\u53e5\u8bdd\u7ffb\u8bd1\u6210\u4e00\u6761\u660e\u786e\u6307\u4ee4\u3002\n\n\u4f60\u80fd\u7406\u89e3\u7684\u52a8\u4f5c\uff08action\uff09\uff1a\n- publish_start\uff1a\u5f00\u59cb\u53d1\u5e03\u6d41\u7a0b\uff08\u8fd8\u6ca1\u9009\u5177\u4f53\u89c6\u9891\u65f6\uff09\n- select\uff1a\u9009\u4e2d\u5f85\u53d1\u5e03\u5217\u8868\u91cc\u7684\u7b2c N \u6761\uff0c\u53ef\u591a\u9009\uff1bindexes \u586b\u5e8f\u53f7\u6570\u7ec4\uff1b\u7528\u6237\u8bf4\"\u5168\u90e8\"\u5c31\u586b\u5168\u90e8\u5e8f\u53f7\n- set_mode\uff1a\u5207\u6362\u53d1\u5e03\u65b9\u5f0f\uff0cmode \u586b\u300c\u666e\u901a\u300d\u6216\u300c\u661f\u56fe\u300d\n- set_task\uff1a\u8bbe\u7f6e\u661f\u56fe\u4efb\u52a1\uff0ctask \u586b\u5173\u952e\u8bcd\uff08\u5982 \u514d\u8d39-\u65b0\u7801\uff09\n- set_title\uff1a\u8bbe\u7f6e\u4f5c\u54c1\u6807\u9898\uff0ctitle \u586b\u6807\u9898\u6587\u672c\n- confirm\uff1a\u7528\u6237\u786e\u8ba4\u5f00\u59cb\u53d1\u5e03\uff08\"\u786e\u8ba4\"\"\u53d1\u5427\"\"\u53ef\u4ee5\"\"\u5f00\u59cb\"\uff09\n- cancel\uff1a\u53d6\u6d88\n- collect\uff1a\u53d6\u6570\uff1breport\uff1a\u51fa\u8868\uff1bstatus\uff1a\u67e5\u72b6\u6001\n- chat\uff1a\u95f2\u804a\u6216\u4fe1\u606f\u4e0d\u8db3\uff0csay \u91cc\u56de\u4e00\u53e5\u7b80\u77ed\u4e2d\u6587\n\n\u8f93\u51fa\u53ea\u80fd\u662f JSON\uff0c\u4e0d\u8981\u591a\u4f59\u6587\u5b57\uff1a\n{\"action\":\"...\",\"indexes\":[],\"mode\":\"\",\"task\":\"\",\"title\":\"\",\"tags\":[],\"say\":\"\"}\n\n\u5224\u65ad\u89c4\u5219\uff1a\n1. \u7528\u6237\u56de\u7eaf\u6570\u5b57\u3001\"\u7b2c6\u6761\"\u3001\"6 7\"\u3001\"6\u548c7\"\u3001\"\u5168\u90e8\" \u90fd\u662f select\uff0cindexes \u5fc5\u987b\u586b\u6570\u5b57\u6570\u7ec4\n2. \u7528\u6237\u8bf4\"\u666e\u901a\"\"\u661f\u56fe\"\u662f set_mode\n3. \u7528\u6237\u63d0\u5230\u6807\u7b7e\uff08#\u5f00\u5934\uff09\u5c31\u653e\u8fdb tags\n4. \u7528\u6237\u8bf4\"\u786e\u8ba4\"\"\u53d1\u5427\"\"\u5f00\u59cb\"\u662f confirm\n5. \u770b\u4e0d\u61c2\u5c31 chat\uff0csay \u91cc\u95ee\u4e00\u53e5\u6700\u5173\u952e\u7684\uff0c\u4e0d\u8981\u91cd\u590d\u83dc\u5355\n6. \u4e0d\u8981\u7f16\u9020\u89c6\u9891\u540d\u3001\u6570\u91cf\u3001\u6570\u636e"


SYS = SYS + "\n\n- set_item_task\uff1a\u7ed9\u5df2\u9009\u89c6\u9891\u5206\u522b\u6307\u5b9a\u661f\u56fe\u4efb\u52a1\uff0citems \u586b [{\"index\":1,\"task\":\"\u514d\u8d39\u7801\"},{\"index\":2,\"task\":\"\u6fc0\u52b1\u7801\"}]\uff1b\u7528\u6237\u8bf4\u300c1\u7528\u514d\u8d39\u7801\uff0c2\u7528\u6fc0\u52b1\u7801\u300d\u5c31\u662f\u8fd9\u6761\n7. \u7528\u6237\u8bf4\u300cN\u7528X\u7801\u300d\u2192 set_item_task\uff0citems \u586b\u5bf9\u5e94\u5e8f\u53f7\u548c\u7801\u540d\n8. \u8f93\u51fa\u7684 JSON \u5fc5\u987b\u5305\u542b items \u5b57\u6bb5\uff08\u6ca1\u6709\u5c31\u7a7a\u6570\u7ec4\uff09"

SYS = SYS + "\n\n\u4f60\u6700\u7ec8\u8981\u4ea7\u51fa\u7684\u662f\u300c\u811a\u300d\u80fd\u76f4\u63a5\u6267\u884c\u7684\u4efb\u52a1\u5355\u3002\u811a\u672c\u63a5\u53e3\u5982\u4e0b\uff08\u4f60\u8f93\u51fa\u7684 jobs \u4f1a\u539f\u6837\u53d8\u6210\u5b83\u7684\u53c2\u6570\uff09\uff1a\npublish_once.py <\u89c6\u9891\u6587\u4ef6\u540d> --title <\u6807\u9898> --tags \"#\u6807\u7b7e1|#\u6807\u7b7e2\" --task <\u661f\u56fe\u4efb\u52a1\u5173\u952e\u8bcd> --publish\n- \u4e0d\u7ed9 --task \u5c31\u662f\u666e\u901a\u53d1\u5e03\uff08\u4e0d\u6302\u661f\u56fe\u4efb\u52a1\uff09\n- --tags \u591a\u4e2a\u7528\u7ad6\u7ebf | \u5206\u9694\uff1b\u6ca1\u6709\u6807\u7b7e\u5c31\u4e0d\u4f20\n- --task \u662f\u5173\u952e\u8bcd\u5305\u542b\u5339\u914d\uff0c\u4f8b\u5982 \u514d\u8d39-\u65b0\u7801 / \u6fc0\u52b1\n- \u6bcf\u6761\u89c6\u9891\u4e00\u4efd\u53c2\u6570\uff0c\u53ef\u4ee5\u5404\u4e0d\u76f8\u540c\uff08\u6807\u9898\u3001\u6807\u7b7e\u3001\u4efb\u52a1\u90fd\u80fd\u9010\u6761\u4e0d\u540c\uff09\n\u56e0\u6b64\u5f53\u7528\u6237\u628a\u8981\u53d1\u7684\u5185\u5bb9\u8bf4\u6e05\u695a\u65f6\uff0c\u8f93\u51fa\uff1a\n{\"action\":\"run_jobs\",\"jobs\":[{\"file\":\"\u514d\u8d39-49.mp4\",\"title\":\"\u706b\u9f99\u6f2b\u5267\u514d\u8d39\u770b\",\"tags\":[\"#\u706b\u9f99\u6f2b\u5267\",\"#\u6f2b\u5267\"],\"task\":\"\u514d\u8d39-\u65b0\u7801\"}],\"say\":\"\"}\njobs \u91cc\u7684 file \u5fc5\u987b\u662f\u6e05\u5355\u91cc\u51fa\u73b0\u8fc7\u7684\u6587\u4ef6\u540d\uff0c\u4e0d\u8bb8\u7f16\u9020\u3002\u4fe1\u606f\u4e0d\u5168\u5c31\u5148\u95ee\uff0c\u4e0d\u8981\u731c\u3002"

SYS = SYS + "\n\n\u53e6\u5916\u4e24\u4e2a\u52a8\u4f5c\u5fc5\u987b\u4f1a\u7528\uff1a\n- list_files\uff1a\u7528\u6237\u8981\"\u5217\u51fa\u89c6\u9891/\u6709\u54ea\u4e9b\u7d20\u6750/\u6e05\u5355\"\u65f6\u7528\uff0c\u4e0d\u8981\u95ee\u53d1\u5e03\u53c2\u6570\n- count_files\uff1a\u7528\u6237\u95ee\"\u6709\u51e0\u6761/\u591a\u5c11\u6761\u89c6\u9891\"\u65f6\u7528\n\u8fd9\u4e24\u4e2a\u52a8\u4f5c\u53ea\u9700\u8981 {\"action\":\"list_files\"} \u6216 {\"action\":\"count_files\"}\uff0c\u4e0d\u8981\u81ea\u5df1\u7f16\u6570\u5b57\u6216\u6587\u4ef6\u540d\uff0c\u771f\u5b9e\u6e05\u5355\u7531\u7a0b\u5e8f\u4ece\u6587\u4ef6\u5939\u8bfb\u51fa\u6765\u3002"

SYS = SYS + "\n\n\u518d\u52a0\u4e00\u4e2a\u52a8\u4f5c\uff1a\n- schedule\uff1a\u7528\u6237\u8981\u5b9a\u65f6\u53d1\uff08\"\u660e\u59299\u70b9\u53d1\"\"\u665a\u4e0a8\u70b9\"\uff09\u3002\u8f93\u51fa {\"action\":\"schedule\",\"at\":\"2026-10-24T09:00:00+08:00\",\"jobs\":[...]}\uff0cjobs \u683c\u5f0f\u548c run_jobs \u4e00\u6837\u3002\u65f6\u95f4\u5fc5\u987b\u662f ISO \u683c\u5f0f\uff0c\u6309 +08:00\u3002"

try:
    _kb_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kb_install.md")
    _KB_INSTALL = io.open(_kb_path, encoding="utf-8").read()
except Exception:
    _KB_INSTALL = ""
if _KB_INSTALL:
    SYS = SYS + ("\n\n=== \u5ba2\u6237\u5b89\u88c5\u95ee\u9898\u77e5\u8bc6\u5e93 ===\n" + _KB_INSTALL +
                 "\n\u5ba2\u6237\u95ee\u5b89\u88c5/\u6388\u6743/\u62a5\u9519/\u5f39\u7a97/\u767b\u5f55 \u2192 action \u7528 chat\uff0c"
                 "\u6309\u4e0a\u9762\u77e5\u8bc6\u5e93\u76f4\u63a5\u56de\u7b54\uff0c\u4e0d\u8981\u62c9\u5230\u53d1\u89c6\u9891\u6d41\u7a0b\uff1b"
                 "\u77e5\u8bc6\u5e93\u6ca1\u6709\u7684\u5c31\u56de\u300c\u8fd9\u4e2a\u6211\u5148\u8bb0\u4e0b\uff0c\u628a\u62a5\u9519\u622a\u56fe\u53d1\u6211\uff0c\u6211\u8f6c\u6280\u672f\u770b\u300d\u3002")
SYS = SYS + "\n\n\u8fd8\u6709\u4e00\u4e2a\u52a8\u4f5c\uff1a\n- delete\uff1a\u7528\u6237\u8981\u5220\u4f5c\u54c1\u3002\u8f93\u51fa {\"action\":\"delete\",\"title\":\"\u6807\u9898\u91cc\u7684\u5173\u952e\u8bcd\"}\u3002\u771f\u6b63\u5220\u9664\u524d\u7a0b\u5e8f\u4f1a\u518d\u95ee\u4e00\u6b21\u786e\u8ba4\uff0c\u4f60\u4e0d\u8981\u81ea\u5df1\u5220\u3002"

SYS = SYS + "\n\n\u8fd8\u6709\u4e00\u4e2a\u52a8\u4f5c\uff1a\n- analyze\uff1a\u7528\u6237\u95ee\"\u54ea\u6761\u6570\u636e\u597d/\u8981\u4e0d\u8981\u8ffd/\u600e\u4e48\u8c03\u6574\"\u65f6\u7528\uff0c\u8fd4\u56de {\"action\":\"analyze\"}\u3002\u7a0b\u5e8f\u4f1a\u5148\u53bb\u540e\u53f0\u53d6\u6570\uff0c\u518d\u7531\u4f60\u57fa\u4e8e\u771f\u5b9e\u6570\u636e\u7ed9\u7ed3\u8bba\u3002"

SYS = SYS + ("\n\n== \u7406\u89e3\u89c4\u5219\uff08\u6700\u91cd\u8981\uff09 ==\n"
    "1. \u7528\u6237\u8bf4\u8bdd\u5f88\u53e3\u8bed\u3001\u7ecf\u5e38\u7701\u7565\u3002\u5fc5\u987b\u7ed3\u5408\u300c\u6700\u8fd1\u5bf9\u8bdd\u300d\u7406\u89e3\u4ed6\u5728\u8bf4\u4ec0\u4e48\uff0c"
    "\u4e0d\u8981\u53ea\u770b\u8fd9\u4e00\u53e5\u3002\u201c\u90a3\u6d88\u8017\u5462\u201d\u201c\u8868\u683c\u5462\u201d\u201c\u4e24\u4e2a\u53f7\u5462\u201d\u90fd\u662f\u5ef6\u7eed\u4e0a\u4e00\u4e2a\u8bdd\u9898\u3002\n"
    "2. \u7528\u6237\u8981\u8868\u683c/Excel/\u6240\u6709\u6570\u636e \u2192 action \u7528 report\uff1b\u8981\u5206\u6790/\u5efa\u8bae \u2192 analyze\u3002\n"
    "3. \u4e0d\u786e\u5b9a\u5c31\u7528 chat\uff0csay \u91cc\u95ee\u4e00\u53e5\u5177\u4f53\u7684\uff08\u6bd4\u5982\u201c\u4f60\u662f\u8981\u4e24\u4e2a\u53f7\u7684\u5b8c\u6574\u8868\uff0c\u8fd8\u662f\u5148\u770b\u6f2b\u534a\u62cd\uff1f\u201d\uff09\uff0c"
    "\u4e0d\u8981\u56de\u83dc\u5355\uff0c\u4e0d\u8981\u91cd\u590d\u4e0a\u4e00\u53e5\u3002\n"
    "4. \u4e0d\u8981\u628a\u201c\u4e0d\u77e5\u9053\u201d\u5f53\u6210\u5ba2\u6237\u5728\u95ee\u5b89\u88c5\u95ee\u9898\u3002\u5ba2\u6237\u5728\u8bf4\u6570\u636e/\u53d1\u5e03/\u5220\u9664\u5c31\u6309\u5bf9\u5e94 action\u3002")

SYS = SYS + ("\n\n- \u7528\u6237\u62b1\u6028/\u50ac\u4fc3\uff08\u201c\u600e\u4e48\u6ca1\u53cd\u5e94\u201d\u201c\u5361\u4e86\u201d\u201c\u5feb\u70b9\u201d\u201c\u4e0d\u884c\u201d\uff09\u2192 \u7528 chat\uff0c"
    "\u4e00\u53e5\u8bdd\u8bf4\u6e05\u73b0\u5728\u5728\u5e72\u4ec0\u4e48/\u5361\u5728\u54ea\uff0c\u4e0d\u8981\u53bb\u51fa\u8868\u3002\n"
    "- \u7528\u6237\u95ee\u4eca\u5929\u51e0\u53f7/\u73b0\u5728\u51e0\u70b9 \u2192 chat\uff0c\u76f4\u63a5\u7528\u4e0a\u9762\u7684\u300c\u73b0\u5728\u65f6\u95f4\u300d\u56de\u7b54\uff0c\u4e0d\u8981\u8bf4\u8bfb\u4e0d\u5230\u65e5\u671f\u3002")

SYS = SYS + ("\n\n- \u4e0d\u5141\u8bb8\u7a7a\u627f\u8bfa\uff1a\u53ea\u6709 action \u4e0d\u662f chat \u65f6\uff0c\u624d\u80fd\u8bf4\u201c\u7a0d\u7b49/\u9a6c\u4e0a/\u8fd9\u5c31\u53bb/\u6211\u53bb\u53d6\u201d\u3002"
    "\u5982\u679c\u8fd9\u53e5\u8bdd\u9700\u8981\u53bb\u53d6\u6570\u3001\u51fa\u8868\u3001\u53d1\u5e03\uff0c\u5c31\u5fc5\u987b\u7ed9\u51fa\u5bf9\u5e94 action\uff0c"
    "\u4e0d\u80fd\u53ea\u5728 say \u91cc\u8bf4\u4e00\u58f0\u5c31\u4e0d\u52a8\u3002")

SYS = SYS + "\n\n\u8fd8\u6709\u4e00\u4e2a\u52a8\u4f5c\uff1a\n- edit\uff1a\u7528\u6237\u8981\u6539\u5df2\u53d1\u4f5c\u54c1\u7684\u6807\u9898/\u63cf\u8ff0\u3002\u8f93\u51fa {\"action\":\"edit\",\"match\":\"\u65e7\u6807\u9898\u91cc\u7684\u5173\u952e\u8bcd\",\"title\":\"\u65b0\u6807\u9898\"}\u3002\u7a0b\u5e8f\u4f1a\u518d\u95ee\u4e00\u6b21\u786e\u8ba4\u3002\u6ce8\u610f\u6296\u97f3\u6bcf\u4e2a\u4f5c\u54c1\u6700\u591a\u6539 5 \u6b21\u3002"

SYS = SYS + ("\n\n\u6807\u9898\u89c4\u5219\uff1a\u6296\u97f3\u4f5c\u54c1\u6807\u9898\u6700\u591a 30 \u4e2a\u5b57\u7b26"
    "\uff08\u4e2d\u6587\u3001\u6807\u70b9\u3001\u5b57\u6bcd\u90fd\u7b97 1 \u4e2a\uff09\u3002"
    "\u62df\u6807\u9898\u65f6\u5148\u6570\u5b57\u7b26\uff0c\u62a5\u7684\u5b57\u6570\u5fc5\u987b\u7b49\u4e8e\u5b9e\u9645\u957f\u5ea6\uff1b"
    "\u8d85\u8fc7 30 \u5c31\u5148\u5220\u8bcd\u518d\u53d1\u3002")


def llm_intent(text, ctx=""):
    import urllib.request
    if not LLM_KEY:
        return {"action": "chat", "say": HELP}
    user = (ctx + "\n\n\u7528\u6237\u8bf4\uff1a" + text) if ctx else text
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": SYS},
                     {"role": "user", "content": user}],
        "temperature": 0.1, "max_tokens": 3000,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request("https://api.deepseek.com/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + LLM_KEY})
    for _attempt in (1,):
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=30).read())
            content = (r["choices"][0]["message"].get("content") or "").strip()
            if not content:
                print("[llm] empty content attempt=%d usage=%s" % (_attempt, r.get("usage")), flush=True)
                continue
            try:
                out = json.loads(content)
            except Exception:
                _i, _j = content.find("{"), content.rfind("}")
                out = json.loads(content[_i:_j + 1]) if _i >= 0 and _j > _i else None
            if not isinstance(out, dict):
                continue
            out["say"] = scrub_fake_success(out.get("say") or "")
            return out
        except Exception as exc:
            print("[llm] error attempt=%d %s" % (_attempt, str(exc)[:200]), flush=True)
            continue
    return {"action": "chat", "say": "\u6ca1\u592a\u542c\u61c2\uff0c\u80fd\u4e0d\u80fd\u518d\u8bf4\u4e00\u904d\uff1f"}


def exec_shell(command, timeout=90, shell="powershell"):
    import urllib.request
    body = json.dumps({"shell": shell, "command": command, "timeout": timeout}).encode()
    req = urllib.request.Request(TUNNEL, data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + TUNNEL_TOKEN})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=timeout + 30).read().decode())
        return (d.get("stdout") or "") + (("\n[stderr] " + d["stderr"][:200]) if d.get("stderr") else "")
    except Exception as e:
        return "[client-unreachable] %s" % e


def ensure_browser(env="env_2", port=9225):
    ps = r"""
$u='http://127.0.0.1:__PORT__/json/version'
try { Invoke-WebRequest -UseBasicParsing $u -TimeoutSec 2 | Out-Null; Write-Output 'ALREADY'; exit 0 } catch {}
$prof=Join-Path $env:LOCALAPPDATA 'HuolongAgent\publish-worker\profiles\__ENV__'
$chrome=Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe'
$c='"' + $chrome + '" --remote-debugging-port=__PORT__ --user-data-dir="' + $prof + '" --no-first-run --no-default-browser-check about:blank'
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=$c} | Out-Null
for($i=0;$i -lt 25;$i++){ Start-Sleep -Seconds 1; try { Invoke-WebRequest -UseBasicParsing $u -TimeoutSec 2 | Out-Null; Write-Output 'CDP_UP'; break } catch {} }
""".replace("__PORT__", str(port)).replace("__ENV__", env)
    return exec_shell(ps, timeout=90)


_ORDINAL_LABELS = ("一号", "二号", "三号", "四号", "五号",
                   "六号", "七号", "八号", "九号", "十号")


def _ordinal_label(index):
    return _ORDINAL_LABELS[index] if 0 <= index < len(_ORDINAL_LABELS) else "第%d号" % (index + 1)


def _fetch_client_files(run, pattern, timeout=100):
    """在客户机上按通配符取回文件，返回 [(文件名, 字节)]，按文件名排序。

    先列文件名，再逐个取回：单个导出可能有几 MB，一次全塞进一条输出容易被截断。
    """
    import base64
    def _marked(out, tag):
        if isinstance(out, dict):
            out = out.get("stdout") or ""
        for ln in reversed((out or "").splitlines()):
            ln = ln.strip()
            if ln.startswith(tag):
                return ln[len(tag):]
        return ""
    listing = ('import base64,glob,json,os;print("FILES:"+base64.b64encode(json.dumps('
               '[f for f in sorted(glob.glob(r"%s"))]).encode()).decode())') % pattern
    raw = _marked(run(listing, timeout=timeout), "FILES:")
    try:
        names = json.loads(base64.b64decode(raw).decode("utf-8")) if raw else []
    except Exception:
        names = []
    files = []
    for remote in names:
        code = ('import base64;print("DATA:"+base64.b64encode(open(r"%s","rb").read()).decode())'
                % remote)
        data = _marked(run(code, timeout=timeout), "DATA:")
        if not data:
            continue
        try:
            files.append((remote.replace("\\", "/").rsplit("/", 1)[-1], base64.b64decode(data)))
        except Exception:
            continue
    return files


def _fetch_exports(kind, prefix, legacy_pattern, timeout=100):
    """Today's export files as [(name, bytes)].

    1.2.221+: list with the ``export-files`` named op and read each file with
    the relay's data-area read; older clients: the legacy listing script."""
    if not _use_named_ops():
        return _fetch_client_files(run_on_client, legacy_pattern, timeout=timeout)
    import base64
    info = _client_named_op("export-files", {"kind": kind, "prefix": prefix},
                            timeout=timeout)
    files = []
    for row in (info.get("files") or []) if info.get("ok") else []:
        try:
            got = _runtime_http("read", {"path": row["path"]}, timeout=timeout)
            files.append((row["name"], base64.b64decode(got.get("base64") or "")))
        except Exception:
            continue
    return sorted(files)


def collect_and_report(chat_id, customer_id=None):
    try:
        ensure_browser("env_2", 9225)
        d = run_cli(["collect-all"], timeout=115, customer_id=customer_id)
        _okp = d.get("exit_code") in (0, None)
        if not _okp:
            try:
                _j0 = json.loads((d.get("stdout") or "").strip())
                _okp = bool((_j0.get("collected") or {}).get("douyin"))
            except Exception:
                _okp = False
        if not _okp:
            reply(chat_id, "\u53d6\u6570\u6ca1\u6709\u5b8c\u6210\uff1a%s"
                  % friendly_error(d.get("stderr") or d.get("stdout"),
                                   "\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002"))
            return
        reply(chat_id, "\u53d6\u6570\u5b8c\u6210\uff0c\u6b63\u5728\u751f\u6210 20 \u5217\u4f5c\u54c1\u8868\u2026")
        import base64, subprocess, glob, time as _t
        base = os.path.join(customer_data_root(customer_id), "out")
        day = _t.strftime("%Y%m%d")
        # \u6309\u5f53\u5929\u5b9e\u9645\u5bfc\u51fa\u7684\u6587\u4ef6\u679a\u4e3e\u8d26\u53f7\uff0c\u4e0d\u5199\u6b7b\u5ba2\u6237\u7684\u6296\u97f3\u6635\u79f0\uff1a\u6362\u5ba2\u6237\u3001\u52a0\u53f7\u90fd\u4e0d\u7528\u6539\u4ee3\u7801\u3002
        paths, labels, missing = [], [], []
        dy_files = _fetch_exports("douyin", "\u4f5c\u54c1\u5217\u8868\u5bfc\u51fa_%s" % day,
                                  base + "\\douyin\\\u4f5c\u54c1\u5217\u8868\u5bfc\u51fa_%s*.json" % day)
        for idx, (_name, blob) in enumerate(dy_files):
            local = "/tmp/dy%d.json" % (idx + 1)
            open(local, "wb").write(blob)
            paths.append(local)
            labels.append(_ordinal_label(idx))
        # 1.2.188 \u8d77\u661f\u56fe\u6309\u8d26\u53f7\u5404\u4e00\u4efd\uff08\u661f\u56fe\u6570\u636e_<\u65e5\u671f>_<env>.json\uff09\uff0c\u5168\u90e8\u5e26\u4e0a\uff1b
        # \u53ea\u53d6\u4e00\u4efd\u4f1a\u8ba9\u5176\u4ed6\u53f7\u7684\u661f\u56fe\u6709\u6548\u64ad\u653e\u6052\u4e3a 0\u3002
        star_paths = []
        for idx, (_name, blob) in enumerate(_fetch_exports(
                "xingtu", "\u661f\u56fe\u6570\u636e_%s" % day,
                base + "\\xingtu\\\u661f\u56fe\u6570\u636e_%s*.json" % day)):
            local = "/tmp/xt%d.json" % (idx + 1)
            open(local, "wb").write(blob)
            star_paths.append(local)
        star_ok = bool(star_paths)
        if not paths:
            reply(chat_id, "\u4e24\u4e2a\u53f7\u90fd\u6ca1\u5bfc\u51fa\u6570\u636e\uff08\u6ca1\u767b\u5f55\uff09\uff0c\u5148\u5728\u9762\u677f\u767b\u4e00\u4e0b\u3002")
            return
        out = "/tmp/\u4e24\u4e2a\u53f7\u4f5c\u54c1_%s.xlsx" % day
        cmd = ["python3", BUILD_TWO_ACCOUNT,
               "--douyin"] + paths + ["--labels", ",".join(labels), "--out", out]
        if star_ok:
            cmd += ["--star"] + star_paths
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        send_file(chat_id, out)
        if missing:
            reply(chat_id, "\u6ce8\u610f\uff1a%s \u6ca1\u767b\u5f55\u6ca1\u53d6\u5230\uff0c\u8fd9\u5f20\u8868\u53ea\u6709 %s\u3002" % ("\u3001".join(missing), "\u3001".join(labels)))
        elif not star_ok:
            reply(chat_id, "\u661f\u56fe\u6570\u636e\u6ca1\u53d6\u5230\uff0c\u8868\u91cc\u661f\u56fe\u6709\u6548\u64ad\u653e\u548c\u5206\u6210\u4e3a 0\u3002")
    except Exception as e:
        import traceback
        traceback.print_exc()
        reply(chat_id, "\u51fa\u8868\u6ca1\u6709\u5b8c\u6210\uff1a%s" % str(e)[:200])



def exec_argv(argv, timeout=90, cwd=None):
    import urllib.request
    if not _runtime_ready():
        return _runtime_blocked_result()
    payload = {"argv": argv, "timeout": timeout}
    if cwd:
        payload["cwd"] = cwd
    body = json.dumps(payload).encode()
    req = urllib.request.Request(TUNNEL, data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + TUNNEL_TOKEN})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout + 40).read().decode())
    except Exception as e:
        return {"stdout": "", "stderr": "[\u5ba2\u6237\u673a\u4e0d\u53ef\u8fbe] %s" % e, "exit_code": -1}

def ops(*args, timeout=90):
    d = exec_argv([CLI_PY, CLI_ROOT + r"\\bot_ops.py"] + list(args), timeout=timeout, cwd=CLI_ROOT)
    txt = (d.get("stdout") or "").strip()
    try:
        return json.loads(txt.splitlines()[-1]) if txt else {}
    except Exception:
        return {"raw": txt[:400], "err": (d.get("stderr") or "")[:300]}

def plan_text(t):
    return "%s\uff5c%s\uff5c%s" % (t.get("at") or "-", t.get("status") or "-", (t.get("title") or "")[:16])

def show_tasks(chat_id):
    r = ops("list")
    rows = r.get("tasks") or []
    if not rows and (r.get("err") or (r.get("raw") and not r.get("ok"))):
        reply(chat_id, "\u6392\u671f\u67e5\u8be2\u6ca1\u6709\u5b8c\u6210\uff1a%s"
              % friendly_error(r.get("err") or r.get("raw"),
                               "\u8bf7\u7a0d\u540e\u91cd\u8bd5\uff0c\u6216\u8054\u7cfb\u7ba1\u7406\u5458\u3002"))
        return
    if not rows:
        reply(chat_id, "\u5f53\u524d\u6ca1\u6709\u6392\u671f\u4efb\u52a1\u3002\n\u7b49\u6211\u628a\u300c\u53d1\u5e03\u300d\u63a5\u5b8c\uff0c\u6392\u8fdb\u53bb\u5c31\u80fd\u5728\u8fd9\u91cc\u6539\u671f/\u6682\u505c\u3002")
        return
    lines = ["\u5f53\u524d\u6392\u671f\uff1a"]
    for t in rows[:12]:
        lines.append("%d. %s  [%s]" % (t["n"], plan_text(t), t.get("task_id","")[:8]))
    lines.append("\n\u6539\u671f\uff1a\u6539\u671f 1 2026-10-21 10:00")
    lines.append("\u6682\u505c\uff1a\u6682\u505c 1")
    reply(chat_id, "\n".join(lines))



PLAN_MAKER = r"""
import os,glob,hashlib,json,datetime,random
base=(os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local"))+r"\HuolongAgent\publish-worker"
base=os.environ.get("BT_WORKER_BASE") or base
inbox=os.path.join(base,"inbox")
raw_files=json.loads(os.environ.get("BT_FILES") or "[]")
if raw_files:
    wanted=[]
    for item in raw_files:
        p=item.get("path") if isinstance(item,dict) else item
        if p: wanted.append(str(p))
    vids=[]; missing=[]
    for p in wanted:
        if os.path.isfile(p) and p.lower().endswith((".mp4",".mov")):
            vids.append(p)
        else:
            missing.append(p)
    if missing:
        print(json.dumps({"ok":False,"err":"\u7d20\u6750\u4e0d\u5b58\u5728\u6216\u4e0d\u662f\u89c6\u9891","missing":missing},ensure_ascii=True)); raise SystemExit
else:
    vids=[f for f in glob.glob(inbox+r"\*") if os.path.isfile(f) and f.lower().endswith((".mp4",".mov"))]
    vids.sort(key=os.path.getmtime)
if not vids:
    print(json.dumps({"ok":False,"err":"inbox \u91cc\u6ca1\u6709\u89c6\u9891"},ensure_ascii=True)); raise SystemExit
vids.sort(key=lambda p:(os.path.getmtime(p),os.path.basename(p).lower()))
batch=len(vids)>1
batch_id=os.environ.get("BT_BATCH_ID") or (datetime.datetime.now().strftime("%Y%m%d%H%M%S")+"-"+os.urandom(4).hex())
batch_id="".join(c if (c.isalnum() or c in "-_") else "_" for c in str(batch_id))[:64]
raw_topics=os.environ.get("BT_TOPICS") or ""
topics=[]
for _t in raw_topics.replace("\uff03","#").replace("\uff0c"," ").replace(","," ").split():
    _t=_t.strip().strip("#").strip()
    if _t:
        _tag="#" + _t
        if _tag not in topics: topics.append(_tag)
raw_item_topics=json.loads(os.environ.get("BT_ITEM_TOPICS") or "[]")
if not isinstance(raw_item_topics,list): raw_item_topics=[]
if len(raw_item_topics)<len(vids): raw_item_topics=raw_item_topics+[None]*(len(vids)-len(raw_item_topics))
raw_xingtu=os.environ.get("BT_XINGTU_TASK") or ""
xingtu_task={}
try:
    parsed_xingtu=json.loads(raw_xingtu) if raw_xingtu else {}
    if isinstance(parsed_xingtu,dict) and (str(parsed_xingtu.get("order_id") or "").strip() or str(parsed_xingtu.get("task_name") or "").strip()):
        xingtu_task={k:str(parsed_xingtu.get(k) or "") for k in
                     ("order_id","task_name","demander","task_type","remaining_quota","task_id","upload_url")}
except Exception:
    xingtu_task={}
publish_mode="xingtu" if (xingtu_task.get("order_id") or xingtu_task.get("task_name")) else "normal"
now=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
env_id=os.environ.get("BT_ENV_ID") or "env_1"
if env_id[:1]=="\"":
    try: env_id=json.loads(env_id)
    except Exception: pass
acct=(env_id or "env_1")+":douyin"
account_name=""
try:
    env_path=os.path.join(base,"config","environments.json")
    envs=json.load(open(env_path,encoding="utf-8-sig"))
    rows=envs.get("environments") if isinstance(envs,dict) else envs
    for row in rows or []:
        if str(row.get("env_id") or "")==env_id:
            account_name=str(row.get("label") or "")
            break
except Exception:
    account_name=""
items=[]
for i,f in enumerate(vids):
    use_topics=raw_item_topics[i]
    use_topics=list(topics if use_topics is None else (use_topics or []))
    title=title_from_file(f)
    copy=""
    if not batch:
        title=clamp_publish_title(os.environ.get("BT_TITLE") or title)
        copy=os.environ.get("BT_COPY") or ""
    h=hashlib.sha256(open(f,"rb").read()).hexdigest()
    fp={"schema":"publish-content/3","account_id":acct,"title":title,"copy_body":copy,"sha256":h,"topics":use_topics,"cover_sha256":{},"covers_requested":[],"platform_account_id":"","poi":None}
    ch=hashlib.sha256(json.dumps(fp,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    lead=-10 if not batch else -10
    sched=(now+datetime.timedelta(seconds=lead)).replace(microsecond=0).isoformat()
    plan_id="bot-"+batch_id+("-"+"%03d"%(i+1) if batch else "")
    plan={"plan_id":plan_id,"env_id":env_id,"channel_id":acct,"account_id":acct,"platform":"douyin","schedule_at":sched,
    "account_name":account_name,"publish_mode":publish_mode,
    "piece":{"title":title,"copy_body":copy,"topics":use_topics,"file_path":f,"file_name":os.path.basename(f),
    "sha256":h,"covers":{"vertical":None,"horizontal":None}},"compliance":{"content_hash":ch}}
    if xingtu_task:
        if xingtu_task.get("order_id"):
            plan["order_id"]=xingtu_task["order_id"]
        plan["xingtu_task"]=dict(xingtu_task)
    if batch:
        d=os.path.join(base,"run","batches",batch_id,"item_%02d"%(i+1))
        os.makedirs(d,exist_ok=True)
        p=os.path.join(d,"plan.json")
        preview=os.path.join(d,"preview.json")
        preview_md=os.path.join(d,"preview.md")
    else:
        p=os.path.join(base,"run","plan_bot.json")
        preview=""; preview_md=""
    open(p,"w",encoding="utf-8").write(json.dumps(plan,ensure_ascii=False,indent=1))
    items.append({"index":i+1,"plan":p,"plan_id":plan_id,"title":title,"file":f,"name":os.path.basename(f),"at":sched,"sha256":h[:12],"content_hash":ch,"topics":use_topics,"preview":preview,"preview_md":preview_md,"account":account_name,"publish_mode":publish_mode,"xingtu_task":dict(xingtu_task) if xingtu_task else None})
out={"ok":True,"batch_id":batch_id,"batch":batch,"count":len(items),"items":items,"topics":topics}
if not batch:
    it=items[0]
    out.update({"plan":it["plan"],"title":it["title"],"file":it["name"],"at":it["at"],"sha256":it["sha256"],"topics":it["topics"]})
print(json.dumps(out,ensure_ascii=False))
"""

def _publish_title(text):
    """Title = the item's copy sentence, with trailing topic tags removed."""
    raw = str(text or "").strip()
    if raw.startswith("\u53d1\u5e03"):
        raw = raw[2:].strip()
    raw = re.sub(r"#[^\s#]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def do_publish(chat_id, text, topics=None, files=None, xingtu_task=None):
    if topics is None:
        request_topics(chat_id, text, files=files)
        return
    title = _publish_title(text)
    env = {"BT_TOPICS": " ".join(topics or [])}
    if title:
        env["BT_TITLE"] = title
    # \u5ba2\u6237\u673a /exec \u4e0d\u8bfb\u53d6 payload["env"]\uff0c\u73af\u5883\u53d8\u91cf\u5fc5\u987b\u4f5c\u4e3a\u524d\u7f6e\u884c\u6ce8\u5165\u4ee3\u7801\uff1b
    # \u4e2d\u6587\u5b57\u9762\u91cf\u7edf\u4e00\u8f6c\u4e49\u6210 \uXXXX\uff0c\u907f\u514d Windows ANSI \u4f20\u53c2\u4e71\u7801\u3002
    if _use_named_ops():
        info = _client_named_op("plan-make", _plan_make_args(
            files, " ".join(topics or []), title, xingtu_task), timeout=90)
        if info.get("transport_error"):
            reply(chat_id, _error_message(
                "\u751f\u6210\u53d1\u5e03\u8ba1\u5212\u5931\u8d25\uff1a", info.get("err"),
                "\u5ba2\u6237\u673a\u6682\u65f6\u4e0d\u53ef\u8fbe\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002")); return
    else:
        pre = _publish_plan_prelude(topics, files, title, xingtu_task=xingtu_task)
        code = pre + PLAN_MAKER
        d = _client_exec_code(code, timeout=90, cwd=CLI_ROOT, env=env)
        if d.get("transport_error"):
            sys.stderr.write("[plan] fail %s\n" % _last_line(d.get("stderr")))
            reply(chat_id, _error_message(
                "\u751f\u6210\u53d1\u5e03\u8ba1\u5212\u5931\u8d25\uff1a", d.get("stderr"),
                "\u5ba2\u6237\u673a\u6682\u65f6\u4e0d\u53ef\u8fbe\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002")); return
        out = (d.get("stdout") or "").strip()
        try:
            info = json.loads(out.splitlines()[-1])
        except Exception:
            reply(chat_id, _error_message(
                "\u751f\u6210\u53d1\u5e03\u8ba1\u5212\u5931\u8d25\uff1a", _result_error(d, "\u8fd4\u56de\u4e0d\u662f\u6709\u6548 JSON"), "")); return
    if not info.get("ok"):
        reply(chat_id, _error_message(
            "\u751f\u6210\u53d1\u5e03\u8ba1\u5212\u5931\u8d25\uff1a", info.get("err"),
            "\u751f\u6210\u53d1\u5e03\u8ba1\u5212\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5\u7d20\u6750\u3002")); return
    if info.get("batch") and int(info.get("count") or 0) > 1:
        reply(chat_id, "\u8fd9\u6279\u6709\u591a\u6761\u7d20\u6750\uff0c\u8bf7\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u8d70\u6279\u91cf\u786e\u8ba4\u3002"); return
    plan = info["plan"]
    reply(chat_id, "\u8ba1\u5212\u5df2\u751f\u6210\uff1a%s\uff08%s\uff09\n\u5148\u505a\u6821\u9a8c\u2026" % (info["title"], info["file"]))
    dr = run_cli(["dry-run", "--plan", plan], timeout=900)
    try:
        pv = _parse_json_blob(dr.get("stdout"))
    except Exception:
        reply(chat_id, _error_message(
            "\u6821\u9a8c\u7ed3\u679c\u89e3\u6790\u5931\u8d25\uff1a\n", _result_error(dr, "\u8fd4\u56de\u4e0d\u662f\u6709\u6548 JSON"), "")); return
    if not pv.get("ok"):
        probs = pv.get("problems") or []
        detail = friendly_error("\uff1b".join(
            str(p.get("message") or p.get("reason") or "")
            for p in probs[:4] if (p.get("message") or p.get("reason"))), "")
        err = friendly_error(dr.get("stderr"), "")
        reply(chat_id, _error_message(
            "\u6821\u9a8c\u6ca1\u8fc7\uff1a", detail or err,
            "\u8bf7\u68c0\u67e5\u6807\u9898\u3001\u8bdd\u9898\u548c\u53d1\u5e03\u65f6\u95f4\u3002"))
        return
    with open(PENDING_FILE, "w", encoding="utf-8") as fh:
        json.dump({"plan": plan, "preview": pv.get("json"),
                   "token": pv.get("confirm_token")}, fh, ensure_ascii=False)
    publish_info = {
        "file": info.get("file") or "",
        "account": info.get("account") or "",
        "title": info.get("title") or "",
        "topics": list(info.get("topics") or []),
        "mode": info.get("publish_mode") or ("xingtu" if xingtu_task else "normal"),
        "xingtu_task": info.get("xingtu_task") or xingtu_task,
    }
    if not send_confirm_card(chat_id, info.get("title") or "\u7d20\u6750",
                             info.get("at") or "", publish_info=publish_info):
        reply(chat_id, "\u786e\u8ba4\u5361\u53d1\u9001\u5931\u8d25\uff0c\u56de\u6211\u300c\u786e\u8ba4\u53d1\u5e03\u300d\u4e5f\u80fd\u53d1\u3002")




def make_batch_plans(chat_id, files, item_topics, batch_id,
                     gap_min=5, gap_max=8, xingtu_task=None):
    """Create one immutable plan per item on the customer machine."""
    entries = _file_entries(files)
    paths = [item["path"] for item in entries]
    if not paths:
        return {"ok": False, "err": "\u6ca1\u6709\u7d20\u6750"}
    if _use_named_ops():
        info = _client_named_op("plan-make", _plan_make_args(
            files, "", "", xingtu_task, batch_id=batch_id,
            item_topics=item_topics), timeout=180)
        if not info.get("ok") and not info.get("err"):
            info["err"] = "\u751f\u6210\u6279\u91cf\u8ba1\u5212\u5931\u8d25"
        return info
    pre = 'import os;os.environ["BT_BATCH_ID"]=%s\n' % json.dumps(
        str(batch_id), ensure_ascii=True)
    pre += _env_json_line("BT_FILES", paths)
    pre += _env_json_line(
        "BT_ITEM_TOPICS",
        [list(value or []) for value in item_topics],
    )
    pre += 'import os;os.environ["BT_TOPICS"]=""\n'
    pre += _env_json_line("BT_ENV_ID", PUBLISH_ENV)
    if xingtu_task:
        pre += _env_json_line("BT_XINGTU_TASK", xingtu_task)
    result = _client_exec_code(pre + PLAN_MAKER, timeout=180, cwd=CLI_ROOT)
    out = (result.get("stdout") or "").strip()
    if not _exec_ok(result) or not out:
        return {"ok": False, "err": _result_error(result, "\u751f\u6210\u6279\u91cf\u8ba1\u5212\u5931\u8d25")}
    try:
        info = json.loads(out.splitlines()[-1])
    except Exception:
        return {"ok": False, "err": "\u6279\u91cf\u8ba1\u5212\u8fd4\u56de\u4e0d\u662f\u6709\u6548 JSON"}
    if not isinstance(info, dict):
        return {"ok": False, "err": "\u6279\u91cf\u8ba1\u5212\u8fd4\u56de\u683c\u5f0f\u9519\u8bef"}
    return info


def _dry_run_error(preview):
    problems = (preview or {}).get("problems") or []
    if problems:
        return "\uff1b".join(str(p.get("message") or p.get("reason") or "\u6821\u9a8c\u672a\u901a\u8fc7")
                            for p in problems[:4])
    return friendly_error((preview or {}).get("err") or (preview or {}).get("reason"),
                          "\u6821\u9a8c\u672a\u901a\u8fc7\uff0c\u8bf7\u68c0\u67e5\u6807\u9898\u3001\u8bdd\u9898\u548c\u53d1\u5e03\u65f6\u95f4\u3002")


def _dry_run_plan(plan_path, preview_path=None, preview_md_path=None):
    args = ["dry-run", "--plan", str(plan_path)]
    if preview_path:
        args += ["--out", str(preview_path)]
    if preview_md_path:
        args += ["--out-md", str(preview_md_path)]
    result = run_cli(args, timeout=900)
    try:
        preview = _parse_json_blob(result.get("stdout"))
    except Exception:
        return {"ok": False,
                "err": _result_error(result, "\u6821\u9a8c\u7ed3\u679c\u4e0d\u662f\u6709\u6548 JSON")}
    if not preview.get("ok"):
        return {"ok": False, "problems": preview.get("problems") or [],
                "err": _dry_run_error(preview)}
    if not preview.get("confirm_token"):
        return {"ok": False, "err": "dry-run \u6ca1\u6709\u8fd4\u56de confirm_token"}
    return preview


def do_publish_batch(chat_id, text, topics, files, overrides=None,
                     title="", gap_min=5, gap_max=8, xingtu_task=None):
    """Build all plans, validate them, then send exactly one batch card."""
    entries = _file_entries(files)
    if not entries:
        reply(chat_id, "inbox \u91cc\u6ca1\u6709\u53ef\u53d1\u5e03\u7684\u89c6\u9891\u3002")
        return
    if len(entries) > MAX_BATCH_ITEMS:
        reply(chat_id, "\u5355\u6279\u6700\u591a %d \u6761\uff0c\u8bf7\u5148\u79fb\u8d70\u4e00\u90e8\u5206\u3002" % MAX_BATCH_ITEMS)
        return
    overrides = {int(key): list(value or [])
                 for key, value in (overrides or {}).items()}
    global_topics = list(topics or [])
    item_topics = [overrides.get(index, list(global_topics))
                   for index in range(1, len(entries) + 1)]
    batch_id = new_batch_id()
    reply(chat_id, "\u6536\u5230 %d \u6761\u7d20\u6750\uff0c\u6b63\u5728\u9010\u6761\u751f\u6210\u8ba1\u5212\u5e76\u6821\u9a8c\u2026" % len(entries))
    info = make_batch_plans(chat_id, entries, item_topics, batch_id,
                            gap_min=gap_min, gap_max=gap_max,
                            xingtu_task=xingtu_task)
    if not info.get("ok"):
        reply(chat_id, _error_message(
            "\u751f\u6210\u53d1\u5e03\u8ba1\u5212\u5931\u8d25\uff1a", info.get("err"),
            "\u8bf7\u68c0\u67e5\u7d20\u6750\u662f\u5426\u5b58\u5728\u3002"))
        return
    raw_items = info.get("items") or []
    if len(raw_items) != len(entries):
        reply(chat_id, "\u8ba1\u5212\u6570\u91cf\u4e0d\u4e00\u81f4\uff08\u7d20\u6750 %d\uff0c\u8ba1\u5212 %d\uff09\uff0c\u5df2\u505c\u6b62\u3002"
              % (len(entries), len(raw_items)))
        return
    items = []
    errors = []
    for position, raw in enumerate(raw_items, 1):
        item = {
            "index": int(raw.get("index") or position),
            "name": raw.get("name") or entries[position - 1]["name"],
            "file": raw.get("file") or entries[position - 1]["path"],
            "title": raw.get("title") or "",
            "topics": list(raw.get("topics") or []),
            "plan_path": raw.get("plan") or "",
            "plan_id": raw.get("plan_id") or "",
            "content_hash": raw.get("content_hash") or "",
            "preview_path": raw.get("preview") or "",
            "preview_md_path": raw.get("preview_md") or "",
            "account": raw.get("account") or "",
            "publish_mode": raw.get("publish_mode") or
                            ("xingtu" if xingtu_task else "normal"),
            "xingtu_task": raw.get("xingtu_task") or
                           (dict(xingtu_task) if xingtu_task else None),
            "token": None,
            "idempotency_key": None,
            "status": "planned",
            "work_id": None,
            "xingtu_order_id": (raw.get("xingtu_task") or xingtu_task or {}).get("order_id"),
            "xingtu_task_name": (raw.get("xingtu_task") or xingtu_task or {}).get("task_name"),
            "error": None,
        }
        preview = _dry_run_plan(item["plan_path"], item["preview_path"],
                                item["preview_md_path"])
        if not preview.get("ok"):
            if preview.get("err") == CLIENT_BUSY_MESSAGE:
                reply(chat_id, CLIENT_BUSY_MESSAGE)
                return
            errors.append("\u7b2c %d \u6761 %s\uff1a%s"
                          % (item["index"], item["name"],
                             _dry_run_error(preview)))
            continue
        item["token"] = preview.get("confirm_token")
        item["idempotency_key"] = preview.get("idempotency_key")
        item["preview_path"] = preview.get("json") or item["preview_path"]
        item["preview_md_path"] = preview.get("md") or item["preview_md_path"]
        items.append(item)
    if errors:
        tail = "" if len(errors) <= 8 else "\n\u2026\u53e6\u6709 %d \u6761" % (len(errors) - 8)
        reply(chat_id, "\u8fd9\u6279\u7d20\u6750\u6821\u9a8c\u6ca1\u8fc7\uff0c\u672a\u53d1\u786e\u8ba4\u5361\uff1a\n"
              + "\n".join(errors[:8]) + tail)
        return
    now = time.time()
    state = {
        "schema": "bot-batch/1",
        "batch_id": batch_id,
        "chat_id": str(chat_id),
        "text": str(text or ""),
        "title": str(title or ""),
        "topics": global_topics,
        "publish_mode": "xingtu" if xingtu_task else "normal",
        "xingtu_task": dict(xingtu_task) if xingtu_task else None,
        "gap_min": int(gap_min),
        "gap_max": int(gap_max),
        "created_at": now,
        "expires_at": now + BATCH_TTL_SECONDS,
        "status": "pending",
        "items": items,
    }
    save_batch(state)
    save_latest_batch(chat_id, batch_id)
    if not send_confirm_card(chat_id, title, "", batch=state):
        reply(chat_id, "\u786e\u8ba4\u5361\u53d1\u9001\u5931\u8d25\uff0c\u56de\u6211\u300c\u786e\u8ba4\u53d1\u5e03\u300d\u4e5f\u80fd\u53d1\u3002")


def do_confirm_batch(chat_id, batch_id=None, resume=False):
    """One card click authorizes the batch; each item still has its own gates."""
    state = load_batch(batch_id) if batch_id else load_latest_batch(chat_id)
    if not state:
        reply(chat_id, "\u6ca1\u6709\u5f85\u786e\u8ba4\u7684\u6279\u91cf\u53d1\u5e03\u3002\u5148\u53d1\u300c\u53d1\u5e03\u300d\u751f\u6210\u3002")
        return
    if str(state.get("chat_id")) != str(chat_id):
        reply(chat_id, "\u8fd9\u4e2a\u6279\u6b21\u4e0d\u5c5e\u4e8e\u5f53\u524d\u4f1a\u8bdd\uff0c\u62d2\u7edd\u786e\u8ba4\u3002")
        return
    if _legacy_publish_blocked(chat_id):
        reply(chat_id, "\u8be5\u5ba2\u6237\u5df2\u542f\u7528\u591a\u8bbe\u5907\u7ed1\u5b9a\uff0c\u65e7\u7684\u786e\u8ba4\u6d41\u7a0b\u65e0\u6cd5\u786e\u5b9a\u76ee\u6807\u673a\u5668\uff1b\u672c\u6279\u672a\u786e\u8ba4\uff0c\u8bf7\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u3002")
        return
    status = state.get("status")
    if status == "pending":
        try:
            expired = float(state.get("expires_at") or 0) <= time.time()
        except (TypeError, ValueError):
            expired = True
        if expired:
            state["status"] = "expired"
            _save_batch_preserving_cancel(state)
            reply(chat_id, "\u8fd9\u6279\u786e\u8ba4\u5df2\u8d85\u65f6\uff0c\u8bf7\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u3002")
            return
        with BATCH_LOCK:
            state = load_batch(state.get("batch_id"))
            if not state or state.get("status") != "pending":
                reply(chat_id, "\u8fd9\u6279\u5df2\u7ecf\u786e\u8ba4\u6216\u5728\u53d1\u5e03\u4e2d\uff0c\u4e0d\u7528\u91cd\u590d\u70b9\u3002")
                return
            state["status"] = "confirmed"
            state["confirmed_at"] = time.time()
            _save_batch_preserving_cancel(state)
    elif status in ("confirmed", "running"):
        if not resume:
            reply(chat_id, "\u8fd9\u6279\u5df2\u786e\u8ba4\uff0c\u6b63\u5728\u6309\u95f4\u9694\u53d1\u5e03\uff0c\u4e0d\u7528\u91cd\u590d\u70b9\u3002")
            return
        if status == "running":
            for item in state.get("items") or []:
                if item.get("status") == "running":
                    item["status"] = "needs_reconcile"
                    item["error"] = "bot restarted during publish; verify manually"
            state["status"] = "confirmed"
            _save_batch_preserving_cancel(state)
    else:
        labels = {"done": "\u5df2\u5b8c\u6210", "failed": "\u5df2\u7ed3\u675f\uff08\u6709\u5931\u8d25\uff09",
                  "cancelled": "\u5df2\u53d6\u6d88", "expired": "\u5df2\u8d85\u65f6",
                  "needs_reconcile": "\u9700\u4eba\u5de5\u6838\u5bf9"}
        reply(chat_id, "\u8fd9\u6279\u72b6\u6001\u662f\uff1a%s\uff0c\u4e0d\u80fd\u91cd\u590d\u53d1\u5e03\u3002"
              % labels.get(status, status or "\u672a\u77e5"))
        return

    items = state.get("items") or []
    total = len(items)
    if not items:
        state["status"] = "failed"
        state["reason"] = "batch has no items"
        _save_batch_preserving_cancel(state)
        reply(chat_id, "\u8fd9\u6279\u6ca1\u6709\u7d20\u6750\uff0c\u5df2\u505c\u6b62\u3002\u8bf7\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u3002")
        return
    if _batch_cancel_requested(state):
        _skip_unstarted_for_cancel(state)
        if _finish_batch_cancelled(chat_id, state):
            return
    state["status"] = "running"
    state["started_at"] = time.time()
    _save_batch_preserving_cancel(state)
    reply(chat_id, "\u786e\u8ba4\u5df2\u6536\u5230\uff1a\u5171 %d \u6761\uff0c\u6309 %d-%d \u5206\u949f\u95f4\u9694\u4f9d\u6b21\u53d1\u5e03\u3002"
          % (total, int(state.get("gap_min") or 5),
             int(state.get("gap_max") or 8)))
    last_started = None
    try:
        for item in items:
            if item.get("status") in ("published", "failed", "skipped",
                                      "needs_reconcile"):
                continue
            if _batch_cancel_requested(state):
                _skip_unstarted_for_cancel(state)
                break
            index = int(item.get("index") or 0)
            name = item.get("name") or item.get("title") or ("\u7b2c %d \u6761" % index)
            if last_started is not None:
                gap = _batch_gap_seconds(state)
                item["gap_before_seconds"] = gap
                target = last_started + gap
                if target > time.monotonic():
                    minutes = max(1, int(round(
                        (target - time.monotonic()) / 60.0)))
                    reply(chat_id, "\u7b2c %d/%d \u6761\u5c06\u5728\u7ea6 %d \u5206\u949f\u540e\u5f00\u59cb\u3002"
                          % (index, total, minutes))
                    if _wait_batch_gap(state, target):
                        _skip_unstarted_for_cancel(state)
                        break

            updated = _update_plan_time(item.get("plan_path"),
                                        _next_schedule_at(-5))
            if not updated.get("ok"):
                item["status"] = "failed"
                item["error"] = updated.get("err") or "\u66f4\u65b0\u8ba1\u5212\u65f6\u95f4\u5931\u8d25"
                item["finished_at"] = time.time()
                _save_batch_preserving_cancel(state)
                reply(chat_id, "\u7b2c %d/%d \u6761\u5931\u8d25\uff1a%s"
                      % (index, total, item["error"]))
                continue
            try:
                plan = _load_plan_for_receipt({"plan": item.get("plan_path")})
            except Exception as exc:
                item["status"] = "failed"
                item["error"] = (_last_line(exc) or str(exc)[:300])
                item["finished_at"] = time.time()
                _save_batch_preserving_cancel(state)
                reply(chat_id, "\u7b2c %d/%d \u6761\u5931\u8d25\uff1a\u8bfb\u53d6\u8ba1\u5212\u5931\u8d25\uff0c\u62d2\u7edd\u7b7e\u53d1\u3002"
                      % (index, total))
                continue
            plan_hash = (plan.get("compliance") or {}).get("content_hash")
            if (str(plan.get("plan_id")) != str(item.get("plan_id"))
                    or plan_hash != item.get("content_hash")):
                item["status"] = "failed"
                item["error"] = "plan binding changed"
                item["finished_at"] = time.time()
                _save_batch_preserving_cancel(state)
                reply(chat_id, "\u7b2c %d/%d \u6761\u5931\u8d25\uff1a\u8ba1\u5212\u5185\u5bb9\u4e0e\u786e\u8ba4\u65f6\u4e0d\u4e00\u81f4\u3002"
                      % (index, total))
                continue
            preview = _dry_run_plan(item.get("plan_path"),
                                    item.get("preview_path"),
                                    item.get("preview_md_path"))
            if not preview.get("ok"):
                item["status"] = "failed"
                item["error"] = _dry_run_error(preview)
                item["finished_at"] = time.time()
                _save_batch_preserving_cancel(state)
                reply(chat_id, "\u7b2c %d/%d \u6761\u5931\u8d25\uff1a%s"
                      % (index, total, item["error"]))
                continue
            item["token"] = preview.get("confirm_token")
            item["idempotency_key"] = preview.get("idempotency_key")
            item["preview_path"] = (preview.get("json")
                                    or item.get("preview_path"))
            item["preview_md_path"] = (preview.get("md")
                                       or item.get("preview_md_path"))
            _save_batch_preserving_cancel(state)
            try:
                device_id, binding_epoch = _plan_device_identity(
                    plan, item, state)
                receipt = receipt_issue.issue_receipt(
                    plan, _approver_id(chat_id),
                    operation_mode="approved_publish",
                    device_id=device_id,
                    binding_epoch=binding_epoch)
                receipt_path = client_receipt_path(item.get("plan_path"))
                _write_client_bytes(
                    receipt_path,
                    json.dumps(receipt, ensure_ascii=False,
                               sort_keys=True).encode("utf-8"))
            except Exception as exc:
                item["status"] = "failed"
                item["error"] = ("\u7b7e\u53d1\u53d1\u5e03\u51ed\u636e\u5931\u8d25\uff1a"
                                 + friendly_error(str(exc), "\u8bf7\u8054\u7cfb\u7ba1\u7406\u5458\u3002"))
                item["finished_at"] = time.time()
                _save_batch_preserving_cancel(state)
                reply(chat_id, "\u7b2c %d/%d \u6761\u5931\u8d25\uff1a\u7b7e\u53d1\u53d1\u5e03\u51ed\u636e\u5931\u8d25\u3002"
                      % (index, total))
                continue

            if _batch_cancel_requested(state):
                _skip_unstarted_for_cancel(state)
                break
            _wait_until_schedule(updated.get("schedule_at"))
            if _batch_cancel_requested(state):
                _skip_unstarted_for_cancel(state)
                break
            item["status"] = "running"
            item["started_at"] = time.time()
            _save_batch_preserving_cancel(state)
            last_started = time.monotonic()
            reply(chat_id, "\u7b2c %d/%d \u6761\u5f00\u59cb\uff1a%s" % (index, total, name))
            args = ["publish", "--mode", "approved_publish",
                    "--plan", item.get("plan_path")]
            if item.get("preview_path"):
                args += ["--preview", item["preview_path"]]
            if item.get("token"):
                args += ["--confirm-token", item["token"]]
            args += ["--receipt", receipt_path, "--allow-submit",
                     "--canary-armed"]
            result, _recovery = run_publish_with_recovery(
                args, _plan_channel(plan), timeout=900)
            raw_error = "\n".join(value for value in
                                  (result.get("stderr"), result.get("stdout"))
                                  if value)
            if (result.get("exit_code") == -1
                    or "\u5ba2\u6237\u673a\u4e0d\u53ef\u8fbe" in (result.get("stderr") or "")):
                item["status"] = "failed"
                item["error"] = "\u5ba2\u6237\u673a\u4e0d\u53ef\u8fbe"
                item["finished_at"] = time.time()
                for rest in items:
                    if rest.get("status") not in ("published", "failed",
                                                  "skipped", "needs_reconcile"):
                        rest["status"] = "skipped"
                        rest["error"] = "\u5ba2\u6237\u673a\u4e0d\u53ef\u8fbe\uff0c\u672a\u6267\u884c"
                _save_batch_preserving_cancel(state)
                reply(chat_id, "\u7b2c %d/%d \u6761\u5931\u8d25\uff1a\u5ba2\u6237\u673a\u4e0d\u53ef\u8fbe\u3002\u5269\u4f59\u672a\u6267\u884c\u7684\u7d20\u6750\u5df2\u505c\u6b62\u3002"
                      % (index, total))
                break
            try:
                published = _parse_json_blob(result.get("stdout"))
            except Exception:
                published = None
            if isinstance(published, dict) and published.get("published"):
                item["status"] = "published"
                item["work_id"] = published.get("platform_work_id")
                item["publish_outcome"] = published.get("publish_outcome") or "CONFIRMED"
                item["publish_mode"] = published.get("publish_mode") or item.get("publish_mode")
                item["xingtu_order_id"] = (published.get("xingtu_order_id") or
                                           item.get("xingtu_order_id"))
                item["xingtu_task_name"] = (published.get("xingtu_task_name") or
                                            item.get("xingtu_task_name"))
                item["xingtu_readback"] = published.get("xingtu_readback")
                item["finished_at"] = time.time()
                item["error"] = None
                _save_batch_preserving_cancel(state)
                reply(chat_id, "\u7b2c %d/%d \u6761\u5df2\u53d1\u5e03\uff1a%s\uff0c\u4f5c\u54c1\u53f7\uff1a%s\n%s"
                      % (index, total, name, item.get("work_id") or "-",
                         _publish_mode_line(published, plan)))
            else:
                if is_idempotency_consumed(raw_error):
                    set_republish_candidate(
                        chat_id, item.get("plan_path"), item.get("preview_path"),
                        item.get("token"), title=item.get("title") or "",
                        name=name, batch_id=state.get("batch_id"), item_index=index)
                    item["status"] = "failed"
                    item["error"] = friendly_error(raw_error)
                    item["finished_at"] = time.time()
                    _save_batch_preserving_cancel(state)
                    reply(chat_id, "\u7b2c %d/%d \u6761\uff1a%s\n%s"
                          % (index, total, friendly_error(raw_error), REPUBLISH_ASK))
                    continue
                detail = publish_failure_detail(
                    result, _recovery,
                    "\u53d1\u5e03\u6ca1\u6709\u5b8c\u6210\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002")
                item["status"] = "failed"
                item["error"] = detail
                item["publish_outcome"] = (published or {}).get("publish_outcome") if isinstance(published, dict) else None
                item["publish_mode"] = (published or {}).get("publish_mode") if isinstance(published, dict) else item.get("publish_mode")
                item["xingtu_order_id"] = (published or {}).get("xingtu_order_id") if isinstance(published, dict) else item.get("xingtu_order_id")
                item["xingtu_task_name"] = (published or {}).get("xingtu_task_name") if isinstance(published, dict) else item.get("xingtu_task_name")
                item["xingtu_readback"] = (published or {}).get("xingtu_readback") if isinstance(published, dict) else None
                item["finished_at"] = time.time()
                _save_batch_preserving_cancel(state)
                if isinstance(published, dict) and published.get("publish_outcome") == "UNKNOWN":
                    reply(chat_id, "\u7b2c %d/%d \u6761\u5e73\u53f0\u5df2\u63d0\u4ea4\uff0c\u4f46\u661f\u56fe\u6295\u7a3f\u72b6\u6001"
                          "\u672a\u8bfb\u56de\uff08UNKNOWN\uff09\uff0c\u4e0d\u80fd\u62a5\u6210\u529f\u3002\n%s"
                          % (index, total, _publish_mode_line(published, plan)))
                else:
                    reply(chat_id, "\u7b2c %d/%d \u6761\u6ca1\u53d1\u51fa\u53bb\uff1a%s\n%s\n%s"
                          % (index, total, name, detail,
                             _publish_mode_line(published or {}, plan)))
    except Exception as exc:
        state["status"] = "needs_reconcile"
        state["reason"] = "%s: %s" % (type(exc).__name__, exc)
        _save_batch_preserving_cancel(state)
        reply(chat_id, "\u6279\u91cf\u53d1\u5e03\u4e2d\u65ad\uff1a%s"
              % friendly_error(str(exc), "\u8bf7\u7a0d\u540e\u91cd\u8bd5\uff0c\u6216\u8054\u7cfb\u7ba1\u7406\u5458\u3002"))
        return

    counts = _batch_counts(state)
    if _cancel_skipped_count(state) and not counts.get("running", 0):
        _finish_batch_cancelled(chat_id, state)
        return
    clean = (counts.get("failed", 0) == 0 and counts.get("skipped", 0) == 0
             and counts.get("needs_reconcile", 0) == 0
             and counts.get("planned", 0) == 0
             and counts.get("running", 0) == 0)
    state["status"] = "done" if clean else "failed"
    state["finished_at"] = time.time()
    _save_batch_preserving_cancel(state)
    star_ok = sum(1 for item in items if item.get("status") == "published"
                  and item.get("publish_mode") == "xingtu")
    normal_ok = sum(1 for item in items if item.get("status") == "published"
                    and item.get("publish_mode") != "xingtu")
    reply(chat_id, "\u672c\u6279\u7ed3\u675f\uff1a\u6210\u529f %d\uff0c\u5931\u8d25 %d\uff0c\u672a\u6267\u884c %d\u3002\n"
          "\u5176\u4e2d\u661f\u56fe\u4efb\u52a1\u6210\u529f %d\uff0c\u666e\u901a\u53d1\u5e03\u6210\u529f %d\u3002"
          % (counts.get("published", 0), counts.get("failed", 0),
             counts.get("skipped", 0), star_ok, normal_ok))


def _tunnel_op_url(operation):
    """Derive a remote-channel operation URL from TUNNEL_URL (/exec)."""
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(TUNNEL)
    path = parts.path.rstrip("/")
    if path.endswith("/exec"):
        path = path[:-5] + "/" + operation
    else:
        path = (path + "/" if path else "/") + operation
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def client_decision_token_path(customer_id=None):
    """Path of the local decision token on the customer machine."""
    return ntpath.join(customer_data_root(customer_id),
                       "approval", "decision_token")


def client_decision_token(customer_id=None, timeout=30):
    """Read the customer-machine decision token through remote-channel /read."""
    import base64
    import urllib.request
    path = client_decision_token_path(customer_id)
    body = json.dumps({"path": path}).encode()
    req = urllib.request.Request(
        _tunnel_op_url("read"), data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + TUNNEL_TOKEN})
    try:
        data = json.loads(urllib.request.urlopen(
            req, timeout=timeout).read().decode())
        token = base64.b64decode(
            data.get("base64") or "", validate=True).decode("utf-8-sig").strip()
    except Exception as exc:
        raise RuntimeError(
            "\u5ba2\u6237\u673a\u5ba1\u6279\u670d\u52a1\u672a\u5c31\u7eea\uff08decision_token \u7f3a\u5931\uff09\uff1a%s"
            % (_last_line(exc) or type(exc).__name__)) from exc
    if not token:
        raise RuntimeError("\u5ba2\u6237\u673a\u5ba1\u6279\u670d\u52a1\u672a\u5c31\u7eea\uff08decision_token \u7f3a\u5931\uff09")
    return token


def remote_approve(approval_id):
    """\u6587\u5b57\u786e\u8ba4\uff1a\u8bfb\u53d6\u672c\u673a decision token \u540e\u8d70\u5ba1\u6279\u670d\u52a1\u7684\u51b3\u7b56\u63a5\u53e3\uff08\u7b49\u540c\u70b9\u5361\uff09\u3002"""
    if not re.fullmatch(r"fa_[0-9a-f]{24}", str(approval_id or "")):
        return "\u5ba1\u6279\u5355\u53f7\u4e0d\u5408\u6cd5\uff1a%s" % approval_id
    try:
        token = client_decision_token()
    except Exception as exc:
        return _last_line(exc) or "\u5ba2\u6237\u673a\u5ba1\u6279\u670d\u52a1\u672a\u5c31\u7eea\uff08decision_token \u7f3a\u5931\uff09"
    try:
        issued_at, signature = receipt_issue.sign_control_plane_decision(
            approval_id)
    except Exception as exc:
        return "\u63a7\u5236\u9762\u7b7e\u540d\u5931\u8d25\uff1a%s" % (_last_line(exc) or type(exc).__name__)
    import base64
    encoded = base64.b64encode(token.encode("utf-8")).decode("ascii")
    code = (
        "import base64,json,urllib.request;"
        "t=base64.b64decode(%r).decode('utf-8');"
        "req=urllib.request.Request("
        "'http://127.0.0.1:8795/api/approvals/%s/decision',"
        "data=json.dumps({'decision':'confirm','operator':'feishu-owner'}).encode(),"
        "headers={'Content-Type':'application/json',"
        "'X-Approval-Token':t,"
        "'X-Control-Plane-Signature':%r,"
        "'X-Control-Plane-Issued-At':%r});"
        "print(urllib.request.urlopen(req,timeout=10).read().decode())"
    ) % (encoded, approval_id, signature, issued_at)
    return run_on_client(code, timeout=20)


def _card_mode_text(task, mode=None):
    task = task if isinstance(task, dict) else {}
    if str(mode or "") == "xingtu" or task.get("order_id"):
        name = str(task.get("task_name") or "\u4efb\u52a1\u672a\u77e5")
        order_id = str(task.get("order_id") or "")
        suffix = ("\uff08order_id: %s\uff09" % order_id) if order_id else ""
        return "\u661f\u56fe\u4efb\u52a1 \u00b7 " + name + suffix
    return "\u666e\u901a\u53d1\u5e03"


def build_confirm_card(title, when, batch=None, publish_info=None):
    value = {"act": "approve"}
    actions = []
    if batch:
        items = batch.get("items") or []
        lines = ["**%d \u6761\u7d20\u6750**" % len(items)]
        for item in items[:10]:
            topics = " ".join(item.get("topics") or []) or "\u65e0\u8bdd\u9898"
            lines.append(
                "%d. \u7d20\u6750\uff1a%s\n"
                "   \u8d26\u53f7\uff1a%s\n"
                "   \u6807\u9898\uff1a%s\n"
                "   \u4f5c\u54c1\u63cf\u8ff0\uff1a%s\n"
                "   \u53d1\u5e03\u65b9\u5f0f\uff1a%s"
                % (int(item.get("index") or 0),
                   item.get("name") or item.get("title") or "-",
                   item.get("account") or "-",
                   item.get("title") or "-",
                   topics,
                   _card_mode_text(item.get("xingtu_task"),
                                   item.get("publish_mode"))))
        if len(items) > 10:
            lines.append("\u2026\u53e6\u6709 %d \u6761" % (len(items) - 10))
        lines += ["", "\u53d1\u5e03\u95f4\u9694\uff1a%d-%d \u5206\u949f"
                  % (int(batch.get("gap_min") or 5),
                     int(batch.get("gap_max") or 8))]
        card_title = "\u5f85\u786e\u8ba4\u6279\u91cf\u53d1\u5e03"
        content = "\n".join(lines)
        value = {"act": "approve", "batch_id": batch.get("batch_id")}
        note = ("\u70b9\u4e00\u6b21\u6309\u94ae\u5373\u6388\u6743\u672c\u6279 %d \u6761\uff1b\u4e4b\u540e\u81ea\u52a8\u9010\u6761\u53d1\u5e03\uff1b30 \u5206\u949f\u5185\u6709\u6548"
                % len(items))
    else:
        card_title = "\u5f85\u786e\u8ba4\u53d1\u5e03"
        info = publish_info if isinstance(publish_info, dict) else {}
        content = "\n".join([
            "**\u7d20\u6750\uff1a%s**" % (info.get("file") or title or "-"),
            "\u8d26\u53f7\uff1a%s" % (info.get("account") or "-"),
            "\u6807\u9898\uff1a%s" % (info.get("title") or title or "-"),
            "\u4f5c\u54c1\u63cf\u8ff0\uff1a%s" % (
                " ".join(info.get("topics") or []) or "\u65e0\u8bdd\u9898"),
            "\u53d1\u5e03\u65b9\u5f0f\uff1a%s" % _card_mode_text(
                info.get("xingtu_task"), info.get("mode")),
            "\u8ba1\u5212\u53d1\u5e03\uff1a%s" % (when or "-"),
        ])
        note = "\u70b9\u6309\u94ae\u5373\u6388\u6743\u53d1\u5e03\uff1b30 \u5206\u949f\u5185\u6709\u6548"
    actions.append({
        "tag": "button", "type": "primary",
        "text": {"tag": "plain_text", "content": "\u786e\u8ba4\u5e76\u53d1\u5e03"},
        "value": value})
    if batch:
        actions.append({
            "tag": "button", "type": "default",
            "text": {"tag": "plain_text",
                     "content": "\u53d6\u6d88\u5269\u4f59\u53d1\u5e03"},
            "value": {"op": "cancel_batch", "batch_id": batch.get("batch_id")}})
    card = {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": "blue",
                   "title": {"tag": "plain_text", "content": card_title}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": content}},
            {"tag": "action", "actions": actions},
            {"tag": "note", "elements": [{"tag": "plain_text",
             "content": note}]},
        ],
    }
    return card


def send_confirm_card(chat_id, title, when, id_type="chat_id", batch=None,
                      publish_info=None):
    card = build_confirm_card(title, when, batch=batch,
                              publish_info=publish_info)
    from lark_oapi.api.im.v1 import CreateMessageRequestBody, CreateMessageRequest
    body = CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("interactive") \
        .content(json.dumps(card, ensure_ascii=False)).build()
    try:
        client.im.v1.message.create(CreateMessageRequest.builder()
                                    .receive_id_type(id_type).request_body(body).build())
        return True
    except Exception as e:
        print("card send failed", e)
        return False


def on_card_action(data):
    try:
        ev = data.event
        value = ev.action.value or {}
        chat_id = (ev.context.open_chat_id if ev.context else "") or ""
    except Exception:
        return None
    act = value.get("act") or ""
    if value.get("op") == "cancel_batch":
        batch_id = value.get("batch_id")
        if batch_id and MULTITENANT:
            cancel_mt_batch(chat_id, batch_id)
            return None
        if batch_id:
            threading.Thread(target=cancel_batch,
                             args=(chat_id, batch_id), daemon=True).start()
        return None
    if act in ("mt_approve", "mt_cancel"):
        if act == "mt_cancel":
            cancel_mt_batch(chat_id, value.get("batch_id"))
        elif value.get("chat_id") == chat_id:
            reply(chat_id, "\u6536\u5230\uff0c\u6b63\u5728\u6838\u5bf9\u786e\u8ba4\u5361\u3002")
            _submit_mt_work(chat_id, "", card=value)
        return None
    if act == "publish_mode":
        mode = str(value.get("mode") or "")
        if mode in ("normal", "xingtu"):
            threading.Thread(target=choose_publish_mode,
                             args=(chat_id, mode), daemon=True).start()
        return None
    if act == "xingtu_task_confirm":
        task_name = str(value.get("task_name") or "")
        if task_name:
            threading.Thread(target=confirm_xingtu_task,
                             args=(chat_id, task_name), daemon=True).start()
        return None
    if act == "xingtu_task_manual":
        threading.Thread(target=prompt_xingtu_task_name,
                         args=(chat_id,), daemon=True).start()
        return None
    if act != "approve":
        return None
    batch_id = value.get("batch_id")
    if batch_id:
        threading.Thread(target=do_confirm_batch,
                         args=(chat_id, batch_id), daemon=True).start()
    else:
        threading.Thread(target=do_confirm, args=(chat_id,), daemon=True).start()
    return None

def do_confirm(chat_id):
    try:
        with open(PENDING_FILE, encoding="utf-8") as fh:
            st = json.load(fh)
    except Exception:
        reply(chat_id, "\u6ca1\u6709\u5f85\u786e\u8ba4\u7684\u53d1\u5e03\u3002\u5148\u53d1\u300c\u53d1\u5e03\u300d\u751f\u6210\u3002"); return
    if _legacy_publish_blocked(chat_id):
        reply(chat_id, "\u8be5\u5ba2\u6237\u5df2\u542f\u7528\u591a\u8bbe\u5907\u7ed1\u5b9a\uff0c\u65e7\u7684\u786e\u8ba4\u6d41\u7a0b\u65e0\u6cd5\u786e\u5b9a\u76ee\u6807\u673a\u5668\uff1b\u672c\u6b21\u672a\u786e\u8ba4\uff0c\u8bf7\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u3002")
        return
    plan_path = str(st.get("plan") or "")
    try:
        plan = _load_plan_for_receipt(st)
    except Exception as exc:
        reply(chat_id, "\u8bfb\u53d6\u53d1\u5e03\u8ba1\u5212\u5931\u8d25\uff0c\u62d2\u7edd\u7b7e\u53d1\uff1a%s"
              % friendly_error(_last_line(exc) or str(exc)[:300],
                               "\u8bf7\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u751f\u6210\u65b0\u786e\u8ba4\u3002"))
        return
    if not st.get("token"):
        reply(chat_id, "\u5f85\u786e\u8ba4\u4efb\u52a1\u7f3a\u5c11\u6821\u9a8c\u51ed\u636e\uff0c\u62d2\u7edd\u7b7e\u53d1")
        return
    reply(chat_id, "\u786e\u8ba4\u5df2\u6536\u5230\uff0c\u5f00\u59cb\u53d1\u5e03\u2026")
    try:
        device_id, binding_epoch = _plan_device_identity(plan, st)
        receipt = receipt_issue.issue_receipt(
            plan, _approver_id(chat_id), operation_mode="approved_publish",
            device_id=device_id, binding_epoch=binding_epoch)
        receipt_path = client_receipt_path(plan_path)
        _write_client_bytes(
            receipt_path,
            json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    except Exception as exc:
        reply(chat_id, "\u7b7e\u53d1\u53d1\u5e03\u51ed\u636e\u5931\u8d25\uff1a%s"
              % friendly_error(_last_line(exc) or str(exc), "\u8bf7\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u751f\u6210\u65b0\u786e\u8ba4\u3002"))
        return

    # \u7b49\u8ba1\u5212\u65f6\u95f4\u5230\u70b9\u518d\u53d1\uff1aCLI \u53ea\u63a5\u53d7\u300c\u5df2\u5230\u70b9\u4e14\u4e0d\u8d85 10 \u5206\u949f\u300d\u7684\u8ba1\u5212\u65f6\u95f4\u3002
    try:
        import datetime as _dt
        _sched = (plan.get("schedule_at") or "").strip()
        if _sched:
            _t = _dt.datetime.fromisoformat(_sched.replace("Z", "+00:00"))
            if _t.tzinfo is None:
                _t = _t.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
            _wait = (_t - _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))).total_seconds()
            if 0 < _wait <= 600:
                time.sleep(min(_wait + 3, 600))
    except Exception as exc:
        sys.stderr.write("[confirm] wait skipped: %s\n" % exc)

    args = ["publish", "--mode", "approved_publish", "--plan", plan_path]
    if st.get("preview"):
        args += ["--preview", str(st["preview"])]
    if st.get("token"):
        args += ["--confirm-token", str(st["token"])]
    args += ["--receipt", receipt_path, "--allow-submit", "--canary-armed"]
    r, _recovery = run_publish_with_recovery(
        args, _plan_channel(plan), timeout=900)
    out = (r.get("stdout") or "").strip()
    raw_error = "\n".join(value for value in (r.get("stderr"), out) if value)
    try:
        pr = _parse_json_blob(out)
    except Exception:
        if is_idempotency_consumed(raw_error):
            set_republish_candidate(
                chat_id, plan_path, st.get("preview"), st.get("token"),
                title=(plan.get("piece") or {}).get("title") or "",
                name=os.path.basename(str(plan_path)))
            reply(chat_id, friendly_error(raw_error) + "\n" + REPUBLISH_ASK)
            return
        reply(chat_id, "\u53d1\u5e03\u5931\u8d25\uff1a%s"
              % publish_failure_detail(
                  r, _recovery,
                  "\u53d1\u5e03\u6ca1\u6709\u5b8c\u6210\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002"))
        return
    if pr.get("published"):
        reply(chat_id, "\u5df2\u53d1\u5e03\uff0c\u4f5c\u54c1\u53f7\uff1a%s\n%s"
              % (pr.get("platform_work_id"), _publish_mode_line(pr, plan)))
    else:
        if is_idempotency_consumed(raw_error + "\n" + json.dumps(pr.get("problems") or pr, ensure_ascii=False)):
            set_republish_candidate(
                chat_id, plan_path, st.get("preview"), st.get("token"),
                title=(plan.get("piece") or {}).get("title") or "",
                name=os.path.basename(str(plan_path)))
            reply(chat_id, friendly_error(raw_error) + "\n" + REPUBLISH_ASK)
            return
        detail = publish_failure_detail(
            r, _recovery,
            "\u53d1\u5e03\u6ca1\u6709\u5b8c\u6210\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002")
        if pr.get("publish_outcome") == "UNKNOWN":
            reply(chat_id, "\u5e73\u53f0\u5df2\u63d0\u4ea4\uff0c\u4f46\u661f\u56fe\u6295\u7a3f\u72b6\u6001\u672a\u8bfb\u56de"
                  "\uff08UNKNOWN\uff09\uff0c\u4e0d\u80fd\u62a5\u6210\u529f\u3002\n%s"
                  % _publish_mode_line(pr, plan))
        else:
            reply(chat_id, "\u6ca1\u53d1\u51fa\u53bb\uff1a%s\n%s"
                  % (detail, _publish_mode_line(pr, plan)))


def _do_republish_mt(chat_id, candidate):
    """Queue one explicit republish for the customer bound to this chat."""
    plan = candidate.get("plan_data")
    customer_id = str(candidate.get("customer_id") or "")
    if not isinstance(plan, dict) or not plan.get("plan_id") or not customer_id:
        clear_republish_candidate(chat_id)
        reply(chat_id, "\u91cd\u53d1\u4fe1\u606f\u4e0d\u5b8c\u6574\uff0c\u8bf7\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u751f\u6210\u65b0\u7684\u786e\u8ba4\u3002")
        return
    try:
        customer = lookup_customer(chat_id)
    except ControlPlaneError:
        reply(chat_id, "\u63a7\u5236\u9762\u6682\u65f6\u4e0d\u53ef\u8fbe\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\u3002")
        return
    if not customer or str(customer.get("customer_id") or "") != customer_id:
        clear_republish_candidate(chat_id)
        reply(chat_id, "\u8fd9\u4e2a\u91cd\u53d1\u4efb\u52a1\u4e0d\u5c5e\u4e8e\u5f53\u524d\u4f1a\u8bdd\uff0c\u5df2\u62d2\u7edd\u3002")
        return
    device_id, binding_epoch = _plan_device_identity(plan, candidate)
    if customer.get("device_binding_enabled") and not device_id:
        clear_republish_candidate(chat_id)
        reply(chat_id, "\u8be5\u5ba2\u6237\u5df2\u542f\u7528\u591a\u8bbe\u5907\u7ed1\u5b9a\uff0c\u4f46\u91cd\u53d1\u4fe1\u606f\u7f3a\u5c11\u76ee\u6807\u673a\u5668\uff1b\u672c\u6b21\u672a\u91cd\u53d1\uff0c\u8bf7\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u3002")
        return
    try:
        receipt = receipt_issue.issue_receipt(
            plan, _approver_id(chat_id), operation_mode="approved_publish",
            republish=True, device_id=device_id,
            binding_epoch=binding_epoch)
    except Exception as exc:
        reply(chat_id, "\u7b7e\u53d1\u91cd\u53d1\u51ed\u636e\u5931\u8d25\uff1a%s"
              % friendly_error(str(exc), "\u8bf7\u8054\u7cfb\u7ba1\u7406\u5458\u68c0\u67e5\u53d1\u5e03\u51ed\u636e\u914d\u7f6e\u3002"))
        return
    payload = {
        "op": "confirm",
        "plan": candidate.get("plan"),
        "preview": candidate.get("preview"),
        "confirm_token": candidate.get("token"),
        "receipt": receipt,
        "allow_submit": True,
        "canary_armed": True,
        "timeout": 1800,
        "republish_confirmed": True,
        "plan_data": plan,
        "title": candidate.get("title") or "",
        "name": candidate.get("name") or "",
    }
    if device_id:
        payload["device_id"] = device_id
        payload["target_device_id"] = device_id
    try:
        enqueue_for_chat(chat_id, "publish", payload,
                         device_id=device_id or None)
    except Exception as exc:
        reply(chat_id, "\u91cd\u53d1\u6307\u4ee4\u6ca1\u6709\u6295\u9012\u6210\u529f\uff1a%s"
              % friendly_error(str(exc), "\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002"))
        return
    clear_republish_candidate(chat_id)
    reply(chat_id, "\u5df2\u6309\u4f60\u7684\u786e\u8ba4\u5b89\u6392\u91cd\u53d1\uff0c\u7ed3\u679c\u4f1a\u56de\u5230\u672c\u4f1a\u8bdd\u3002")


def do_republish(chat_id):
    """Customer-confirmed one-shot republish.

    The receipt carries a signed ``republish`` claim, so the client bypasses
    only the already-consumed window.  License, account, freeze, canary,
    receipt-trust-root and confirm-token checks still run unchanged.
    """
    candidate, expired = get_republish_candidate(chat_id)
    if expired:
        clear_republish_candidate(chat_id)
        reply(chat_id, "\u91cd\u53d1\u786e\u8ba4\u5df2\u8fc7\u671f\uff0c\u8bf7\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u751f\u6210\u65b0\u7684\u786e\u8ba4\u3002")
        return
    if not candidate:
        reply(chat_id, "\u6ca1\u6709\u5f85\u786e\u8ba4\u7684\u91cd\u53d1\u3002\u5148\u53d1\u300c\u53d1\u5e03\u300d\u751f\u6210\u3002")
        return
    if str(candidate.get("mode") or "local") == "mt":
        _do_republish_mt(chat_id, candidate)
        return
    if _legacy_publish_blocked(chat_id):
        clear_republish_candidate(chat_id)
        reply(chat_id, "\u8be5\u5ba2\u6237\u5df2\u542f\u7528\u591a\u8bbe\u5907\u7ed1\u5b9a\uff0c\u65e7\u7684\u91cd\u53d1\u6d41\u7a0b\u65e0\u6cd5\u786e\u5b9a\u76ee\u6807\u673a\u5668\uff1b\u672c\u6b21\u672a\u91cd\u53d1\uff0c\u8bf7\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u3002")
        return
    try:
        plan = _load_plan_for_receipt(candidate)
    except Exception as exc:
        clear_republish_candidate(chat_id)
        reply(chat_id, "\u8bfb\u53d6\u53d1\u5e03\u8ba1\u5212\u5931\u8d25\uff0c\u4e0d\u80fd\u91cd\u53d1\uff1a%s"
              % friendly_error(str(exc), "\u8bf7\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u751f\u6210\u65b0\u786e\u8ba4\u3002"))
        return
    try:
        device_id, binding_epoch = _plan_device_identity(plan, candidate)
        receipt = receipt_issue.issue_receipt(
            plan, _approver_id(chat_id), operation_mode="approved_publish",
            republish=True, device_id=device_id,
            binding_epoch=binding_epoch)
        receipt_path = ntpath.join(ntpath.dirname(str(candidate.get("plan") or "")),
                                   "receipt_republish.json")
        _write_client_bytes(
            receipt_path,
            json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    except Exception as exc:
        reply(chat_id, "\u7b7e\u53d1\u91cd\u53d1\u51ed\u636e\u5931\u8d25\uff1a%s"
              % friendly_error(str(exc), "\u8bf7\u8054\u7cfb\u7ba1\u7406\u5458\u68c0\u67e5\u53d1\u5e03\u51ed\u636e\u914d\u7f6e\u3002"))
        return
    reply(chat_id, "\u6536\u5230\u786e\u8ba4\uff0c\u6b63\u5728\u91cd\u53d1\u8fd9\u6761\u7d20\u6750\u2026")
    args = ["publish", "--mode", "approved_publish",
            "--plan", str(candidate.get("plan") or "")]
    if candidate.get("preview"):
        args += ["--preview", str(candidate.get("preview"))]
    if candidate.get("token"):
        args += ["--confirm-token", str(candidate.get("token"))]
    args += ["--receipt", receipt_path, "--allow-submit", "--canary-armed",
             "--republish-confirmed"]
    result, _recovery = run_publish_with_recovery(
        args, _plan_channel(plan), timeout=900)
    raw_error = "\n".join(value for value in (result.get("stderr"), result.get("stdout")) if value)
    try:
        published = _parse_json_blob(result.get("stdout"))
    except Exception:
        published = None
    if isinstance(published, dict) and published.get("published"):
        clear_republish_candidate(chat_id)
        if candidate.get("batch_id"):
            _update_batch_item(candidate.get("batch_id"), candidate.get("item_index"),
                               status="published", error=None,
                               work_id=published.get("platform_work_id"),
                               publish_mode=published.get("publish_mode"),
                               xingtu_order_id=published.get("xingtu_order_id"),
                               xingtu_task_name=published.get("xingtu_task_name"))
        reply(chat_id, "\u5df2\u6309\u4f60\u7684\u786e\u8ba4\u91cd\u53d1\uff0c\u4f5c\u54c1\u53f7\uff1a%s\n%s"
              % (published.get("platform_work_id") or "-",
                 _publish_mode_line(published, plan)))
        return
    detail = publish_failure_detail(
        result, _recovery,
        "\u91cd\u53d1\u6ca1\u6709\u5b8c\u6210\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002")
    if is_idempotency_consumed(raw_error):
        detail = friendly_error(raw_error)
    if isinstance(published, dict) and published.get("publish_outcome") == "UNKNOWN":
        detail = ("\u5e73\u53f0\u5df2\u63d0\u4ea4\uff0c\u4f46\u661f\u56fe\u6295\u7a3f\u72b6\u6001\u672a\u8bfb\u56de"
                  "\uff08UNKNOWN\uff09\uff0c\u4e0d\u80fd\u62a5\u6210\u529f\u3002")
    reply(chat_id, "\u91cd\u53d1\u6ca1\u6210\u529f\uff1a%s\n%s"
          % (detail, _publish_mode_line(published or {}, plan)))


def parse_command(text):
    """\u628a\u4e00\u6761\u7528\u6237\u6d88\u606f\u89e3\u6790\u6210 (kind, payload)\u3002

    \u53ea\u8fd4\u56de\u63a7\u5236\u9762\u767d\u540d\u5355\u91cc\u7684 kind\uff08publish/schedule/collect/report/status\uff09\uff1b
    \u83dc\u5355\u3001\u95f2\u804a\u8fd4\u56de (None, {})\uff0c\u7531\u8c03\u7528\u65b9\u51b3\u5b9a\u56de HELP \u6216 LLM \u56de\u590d\u3002
    """
    raw = (text or "").strip()
    compact = re.sub(r"\s+", "", raw)
    if compact in ("0", "\u83dc\u5355", "\u5e2e\u52a9", "help", "?"):
        return None, {}
    if compact in ("6", "\u72b6\u6001", "status"):
        return "status", {}
    if compact in ("5", "\u51fa\u8868", "\u62a5\u8868", "\u6570\u636e\u8868"):
        return "report", {}
    if compact == "4" or compact.startswith("\u53d6\u6570"):
        return "collect", {}
    if MULTITENANT and (compact in MT_CANCEL_WORDS or re.search(r"\u6539\u671f|\u6682\u505c|\u6392\u671f", raw)):
        return None, {}
    if compact in ("2", "3", "\u6392\u671f", "\u6539\u671f", "\u6682\u505c"):
        return "schedule", {"op": "list"}
    m = re.match(r"^\u6539\u671f\s*(\S+)\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})", raw)
    if m:
        return "schedule", {
            "op": "time",
            "task": m.group(1),
            "at": m.group(2) + " " + m.group(3),
            "text": raw,
        }
    m = re.match(r"^\u6682\u505c\s*(\S+)", raw)
    if m:
        return "schedule", {"op": "cancel", "task": m.group(1), "text": raw}
    if compact in ("\u786e\u8ba4\u53d1\u5e03", "\u786e\u8ba4"):
        return "publish", {"op": "confirm", "text": raw}
    if compact == "1" or compact.startswith("\u53d1\u5e03"):
        title = compact[2:].strip() if compact.startswith("\u53d1\u5e03") else ""
        topics = normalize_topics(" ".join(re.findall(r"#[^\s#]+", raw)))
        title = re.sub(r"#[^\s#]+", " ", title).strip()
        if not title:
            title = DEFAULT_PUBLISH_TITLE
        return "publish", {"op": "create", "title": title,
                           "topics": topics, "text": raw}
    return None, {}


def _mt_chat_key(chat_id):
    return hashlib.sha1(str(chat_id).encode("utf-8")).hexdigest()[:16]


def _mt_batch_path(batch_id):
    value = str(batch_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", value):
        return None
    return os.path.join(MT_BATCH_DIR, value + ".json")


def save_mt_batch(state):
    path = _mt_batch_path((state or {}).get("batch_id"))
    if not path:
        raise ValueError("bad mt batch id")
    with MT_BATCH_LOCK:
        os.makedirs(MT_BATCH_DIR, exist_ok=True)

        def _atomic_json(target, value):
            fd, tmp = tempfile.mkstemp(prefix=os.path.basename(target) + ".",
                                       suffix=".tmp", dir=os.path.dirname(target))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(value, fh, ensure_ascii=False, indent=1)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, target)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

        old = load_mt_batch(state.get("batch_id"))
        if (old and old.get("cancel_requested")) or _mt_is_cancelled(state):
            _mt_cancel_state(state)
        _atomic_json(path, state)
        latest = os.path.join(
            MT_BATCH_DIR, "latest_" + _mt_chat_key(state.get("chat_id")) + ".json")
        _atomic_json(latest, {"batch_id": state.get("batch_id"),
                              "updated_at": time.time()})
    return path


def load_mt_batch(batch_id):
    path = _mt_batch_path(batch_id)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def load_latest_mt_batch(chat_id):
    path = os.path.join(MT_BATCH_DIR, "latest_" + _mt_chat_key(chat_id) + ".json")
    try:
        with open(path, encoding="utf-8") as fh:
            pointer = json.load(fh)
    except (OSError, ValueError):
        return None
    return load_mt_batch((pointer or {}).get("batch_id"))


def _mt_refresh_plan_data(chat_id, item):
    """改完计划后必须重读计划文件：content_hash 变了，凭据必须跟着变。"""
    customer = lookup_customer(chat_id) or {}
    cid = str(customer.get("customer_id") or "")
    if not cid:
        raise RuntimeError("会话未绑定客户")
    raw = remote_op(cid, "read", {"path": str(item.get("plan_path") or "")},
                    timeout=90, device_id=item.get("device_id"))
    import base64 as _b64
    plan = json.loads(_b64.b64decode(raw.get("base64") or "").decode("utf-8-sig"))
    item["plan_data"] = plan
    item["content_hash"] = (plan.get("compliance") or {}).get("content_hash")
    item["schedule_at"] = plan.get("schedule_at") or item.get("schedule_at")
    title = str((plan.get("piece") or {}).get("title") or "")
    if title:
        item["title"] = title
    return item


def _mt_rebind_prepared(chat_id, customer_id, items):
    """准备阶段就把计划改成"按当前登录账号自动绑定"。

    客户机 prepare 会把 platform_account_id 填成 env_x:平台，publish 时却拿
    页面上的真实 uid 比对，永远不一致。这里重建计划 + 重算指纹 + 重新校验，
    发布前就不会再卡账号。
    """
    for item in items:
        try:
            _mt_reschedule_item(chat_id, item, item.get("schedule_at") or "")
        except Exception as exc:
            index = int(item.get("index") or 0)
            print("[mt-rebind] item=%s %s" % (index, exc), flush=True)
            raise MTBindingError(
                "第 %d 条重建计划/预览失败：%s"
                % (index, _remote_result_detail(
                    getattr(exc, "result", {}), str(exc) or "未知错误"))) from exc
    return items


def _item_sha(item):
    """取一条计划里视频的 sha256，用于去重与批次幂等。"""
    if not isinstance(item, dict):
        return ""
    piece = ((item.get("plan_data") or {}).get("piece") or {})
    full = str(piece.get("sha256") or "")
    if full:
        return full
    # Legacy plan summaries may only carry a 12-character display hash.
    return str(item.get("sha256") or "")


def _plan_device_identity(plan, *fallbacks):
    """Return ``(device_id, binding_epoch)`` already carried by a plan.

    Device identity must come from the prepared plan, not from whoever happens
    to click a button later.  Legacy plans return ``(None, None)``.
    """
    sources = [plan]
    sources.extend(value for value in fallbacks if isinstance(value, dict))
    device_id = ""
    epoch = None
    for source in sources:
        if not device_id:
            device_id = str(source.get("device_id") or "").strip()
        if epoch is None and source.get("binding_epoch") is not None:
            value = source.get("binding_epoch")
            if type(value) is int and value >= 0:
                epoch = value
            else:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
                if value >= 0:
                    epoch = value
    return device_id or None, epoch


def _legacy_publish_blocked(chat_id):
    """Bound customers must use the device-aware MT path, never legacy CLI.

    If binding state cannot be checked, fail closed: the legacy path has no
    device identity and may otherwise publish from the default machine.
    """
    try:
        customer = lookup_customer(chat_id) or {}
    except Exception:
        return True
    return bool(customer.get("device_binding_enabled"))


def _dedupe_items(items):
    """同一条视频被复制成多个文件名时只保留第一条，并按顺序重编号。"""
    seen, out = set(), []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        sha = _item_sha(item)
        if sha and sha in seen:
            continue
        if sha:
            seen.add(sha)
        out.append(item)
    for n, item in enumerate(out, 1):
        item["index"] = n
    return out


def _open_batch_shas(chat_id):
    """还没下发的批次里已有的视频指纹（用于拦住连点「发布」）。"""
    shas = set()
    try:
        names = os.listdir(MT_BATCH_DIR)
    except OSError:
        return shas
    for name in names:
        if not name.endswith(".json") or name.startswith("latest_"):
            continue
        try:
            with open(os.path.join(MT_BATCH_DIR, name), encoding="utf-8") as fh:
                state = json.load(fh)
        except Exception:
            continue
        if str(state.get("chat_id") or "") != str(chat_id):
            continue
        if str(state.get("status")) not in ("pending", "scheduled", "active",
                                            "needs_reconcile"):
            continue
        for item in state.get("items") or []:
            if str(item.get("status")) not in (
                    "pending", "scheduled", "dispatching", "dispatched",
                    "needs_reconcile"):
                continue
            sha = _item_sha(item)
            if sha:
                shas.add(sha)
    return shas


def _mt_publish_evidence(chat_id, command):
    """Read only the command customer's ledger; never accept another receipt."""
    payload = command.get("payload") or {}
    cid = str(command.get("customer_id") or "")
    customer = lookup_customer(chat_id) or {}
    if not cid or customer.get("customer_id") != cid:
        return None
    plan_path = str(payload.get("plan") or "")
    if not plan_path:
        return None
    run_dir = plan_path
    for _ in range(4):
        run_dir = ntpath.dirname(run_dir)
    try:
        ledger = _mt_read_client_json(
            cid, ntpath.join(run_dir, "stage2-ledger.json"),
            device_id=command.get("device_id"))
    except Exception as exc:
        print("[mt-evidence] %s" % type(exc).__name__, flush=True)
        return None
    return _mt_match_evidence(command, ledger, cid)


def _mt_mark_failed(chat_id, command, detail=""):
    result = command.get("normalized_result") or _mt_normalize_result(command)
    _mt_apply_result(chat_id, command, result)


def _mt_reply_progress(chat_id):
    """Answer 好了没/进度/几条 这类问题 from real batch state, never from thin air."""
    try:
        state = load_latest_mt_batch(chat_id)
    except Exception:
        state = None
    if not state:
        reply(chat_id, "现在没有正在跑的发布。要发就说「发布」。")
        return
    items = [i for i in (state.get("items") or []) if isinstance(i, dict)]
    customer = lookup_customer(chat_id) or {}
    if customer.get("customer_id") != state.get("customer_id"):
        reply(chat_id, "\u4f1a\u8bdd\u7ed1\u5b9a\u5df2\u53d8\u5316\uff0c\u4e0d\u80fd\u8bfb\u53d6\u65e7\u5ba2\u6237\u8fdb\u5ea6\u3002")
        return
    done = [i for i in items if i.get("status") == "published"]
    failed = [i for i in items
              if i.get("status") in ("failed", "paused", "expired")]
    reconcile = [i for i in items if i.get("status") == "needs_reconcile"]
    pending = [i for i in items if i.get("status") in ("pending", "scheduled", "planned")]
    cancelled = [i for i in items if i.get("status") in ("skipped", "cancelled")]
    inflight = [i for i in items if i.get("status") in ("dispatching", "dispatched", "running")]
    lines = ["\u8fd9\u6279\u5171 %d \u6761\uff1a\u5df2\u53d1 %d\uff0c\u5931\u8d25 %d\uff0c\u5f85\u53d1 %d\uff0c\u53d6\u6d88 %d\uff0c\u5728\u9014 %d\u3002"
             % (len(items), len(done), len(failed), len(pending), len(cancelled), len(inflight))]
    if reconcile:
        lines.append("\u5f85\u6838\u5b9e %d \u6761\uff1b\u672c\u6279\u505c\u6b62\uff0c\u4e0d\u4f1a\u81ea\u52a8\u91cd\u8bd5\u3002" % len(reconcile))
    nxt = None
    for item in pending:
        when = str(item.get("schedule_at") or "")
        if when and (nxt is None or when < nxt[1]):
            nxt = (int(item.get("index") or 0), when)
    if nxt and state.get("status") in ("scheduled", "active") and not state.get("cancel_requested"):
        lines.append("下一条：第 %d 条，%s 发。" % (nxt[0], nxt[1][11:16]))
    if failed:
        last = failed[-1]
        reason = str(last.get("error") or "").strip()
        lines.append("最近失败：第 %d 条%s" % (
            int(last.get("index") or 0), ("（%s）" % reason[:60]) if reason else ""))
    if not pending and not failed and not reconcile:
        lines.append("这一批已经全部处理完。")
    elif state.get("status") == "pending":
        lines.append("还等你回复「确认」才会开始发。")
    reply(chat_id, "\n".join(lines))


def _mt_mark_published(chat_id, command):
    result = command.get("normalized_result") or _mt_normalize_result(command)
    _mt_apply_result(chat_id, command, result)


def reconcile_interrupted_mt_batches():
    """On restart, mark in-flight MT items for manual reconciliation; never re-send."""
    changed = []
    try:
        names = sorted(os.listdir(MT_BATCH_DIR))
    except OSError:
        return changed
    with MT_BATCH_LOCK:
        for name in names:
            if not name.endswith(".json") or name.startswith("latest_"):
                continue
            path = os.path.join(MT_BATCH_DIR, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    state = json.load(fh)
            except Exception:
                continue
            if not isinstance(state, dict):
                continue
            touched = _mt_is_cancelled(state)
            if touched:
                _mt_cancel_state(state)
            for item in state.get("items") or []:
                if str(item.get("status")) in ("dispatching", "dispatched"):
                    item["status"] = "needs_reconcile"
                    item["error"] = "bot restarted during publish; verify manually"
                    item["reconciled_at"] = time.time()
                    touched = True
            if touched:
                state["status"] = "needs_reconcile"
                state["reason"] = "bot restarted with an in-flight MT publish"
                save_mt_batch(state)
                changed.append(state.get("batch_id"))
                try:
                    reply(state.get("chat_id"),
                          "机器人重启时发现本批有在途发布，已停止后续条目；"
                          "请先在抖音后台核对，确认没发出去后再重新发「发布」。")
                except Exception:
                    pass
    return changed


def store_mt_prepared(chat_id, customer_id, result, device_id=None):
    batch_id = str(result.get("batch_id") or "")
    items = _dedupe_items(result.get("items") or [])
    if not batch_id or not items:
        return None
    device_id = str(device_id or result.get("device_id") or "")
    binding_epoch = result.get("binding_epoch")
    for item in items:
        if device_id:
            item.setdefault("device_id", device_id)
        if binding_epoch is not None and item.get("binding_epoch") is None:
            item["binding_epoch"] = binding_epoch
        plan_device = str((item.get("plan_data") or {}).get("device_id") or "")
        if device_id and plan_device and plan_device != device_id:
            raise MTBindingError("prepared plan belongs to another device")
    for item in items:
        item.setdefault("status", "pending")
    _mt_rebind_prepared(chat_id, customer_id, items)
    state = {
        "schema": "bot-mt-batch/1",
        "batch_id": batch_id,
        "chat_id": str(chat_id),
        "customer_id": str(customer_id),
        "device_id": device_id or None,
        "created_at": time.time(),
        "expires_at": time.time() + PENDING_TTL_SECONDS,
        "status": "pending",
        "gap_min": result.get("gap_min"),
        "gap_max": result.get("gap_max"),
        "items": items,
    }
    save_mt_batch(state)
    return state


def send_mt_confirm_card(chat_id, batch):
    if not batch.get("topics_answered") or batch.get("cancel_requested") or not batch.get("confirmation_digest"):
        return False
    items = batch.get("items") or []
    device_line = ""
    if batch.get("device_id") or batch.get("legacy"):
        device_line = "设备：%s\n" % (
            batch.get("device_label") or batch.get("device_id") or "旧机（未登记设备）")
    lines = ["%s**%d \u6761\u7d20\u6750 \u00b7 \u7248\u672c %s \u00b7 \u6e05\u5355 %s**" % (
        device_line, len(items), batch.get("draft_revision"),
        str(batch.get("snapshot_id"))[:8])]
    for item in items:
        topics = " ".join(item.get("topics") or []) or "\u65e0\u8bdd\u9898"
        lines.append("%d. %s\n\u6807\u9898\uff1a%s\n\u6392\u671f\uff1a%s \u00b7 %s" % (
            int(item.get("index") or 0), item.get("name") or "-",
            item.get("title") or "", item.get("schedule_at") or "\u5f85\u5b9a", topics))
    if batch.get("gap_min") and batch.get("gap_max"):
        lines += ["", "\u53d1\u5e03\u95f4\u9694\uff1a%d-%d \u5206\u949f"
                  % (int(batch["gap_min"]), int(batch["gap_max"]))]
    card = {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": "blue",
                   "title": {"tag": "plain_text", "content": "\u5f85\u786e\u8ba4\u6279\u91cf\u53d1\u5e03"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {"tag": "action", "actions": [{
                "tag": "button", "type": "primary",
                "text": {"tag": "plain_text", "content": "\u786e\u8ba4\u5e76\u53d1\u5e03"},
                "value": {"act": "mt_approve", "batch_id": batch.get("batch_id"),
                          "customer_id": batch.get("customer_id"), "chat_id": chat_id,
                          "revision": batch.get("draft_revision"),
                          "digest": batch.get("confirmation_digest")}},
                {"tag": "button", "type": "default",
                 "text": {"tag": "plain_text", "content": "\u53d6\u6d88\u5269\u4f59\u53d1\u5e03"},
                 "value": {"act": "mt_cancel", "batch_id": batch.get("batch_id")}}]},
            {"tag": "note", "elements": [{"tag": "plain_text",
             "content": "\u70b9\u4e00\u6b21\u6309\u94ae\u5373\u6388\u6743\u672c\u6279\uff1b"
                        "\u6bcf\u6761\u4ecd\u5355\u72ec\u7b7e\u53d1\u51ed\u636e\uff1b30 \u5206\u949f\u5185\u6709\u6548"}]},
        ],
    }
    from lark_oapi.api.im.v1 import CreateMessageRequestBody, CreateMessageRequest
    body = CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("interactive") \
        .content(json.dumps(card, ensure_ascii=False)).build()
    try:
        response = client.im.v1.message.create(CreateMessageRequest.builder()
                                    .receive_id_type("chat_id").request_body(body).build())
        return response.success() is True
    except Exception as exc:
        print("mt card send failed", type(exc).__name__, flush=True)
        return False


#: 排期下发提前量：客户机只接受"计划时间之后"的执行，故给 -5 秒（到点后 5 秒发）
SCHEDULE_DISPATCH_LEAD = -1


def _parse_schedule_ts(value):
    try:
        import datetime as _dt
        return _dt.datetime.fromisoformat(
            str(value or "").replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _mt_wait_until_due(item, max_wait=180.0):
    """Wait until the plan's own schedule time before dispatching.

    The client accepts a publish only inside [schedule_at, schedule_at+10min].
    Rescheduling to now+90s and dispatching immediately is therefore always
    "too early" and every attempt is rejected with SCHEDULE_OUT_OF_WINDOW.
    """
    due = _parse_schedule_ts(item.get("schedule_at")) or 0
    if not due:
        return
    wait = due + 1.0 - time.time()
    if wait > 0:
        time.sleep(min(wait, max_wait))


def _fmt_slot(value):
    ts = _parse_schedule_ts(value)
    if ts is None:
        return str(value or "")
    import datetime as _dt
    return _dt.datetime.fromtimestamp(
        ts, _dt.timezone(_dt.timedelta(hours=8))).strftime("%H:%M")


CONTROL_DB = os.environ.get("CONTROL_DB") or os.path.join(
    os.path.dirname(_HERE), "control.db")


def remote_op(customer_id, op, args=None, timeout=120, device_id=None):
    """在指定客户机上同步执行一次远程 op（exec/ls/read/write）。"""
    import sqlite3
    status, obj = control_request(
        "POST", "/api/remote/exec",
        {"customer_id": str(customer_id), "op": op, "args": args or {},
         **({"device_id": str(device_id)} if device_id else {})}, timeout=20)
    if status >= 400 or not obj.get("ok"):
        detail = _remote_result_detail(obj, fallback="远程投递失败 %s" % status)
        raise RemoteOpError(detail, obj)
    op_id = obj.get("id")
    if not op_id:
        raise RemoteOpError("远程投递失败：控制面没有返回操作 id", obj)
    deadline = time.time() + timeout
    while time.time() < deadline:
        db = None
        try:
            db = sqlite3.connect("file:%s?mode=ro" % CONTROL_DB, uri=True)
            row = db.execute(
                "select status,result from remote_ops where id=?", (op_id,)).fetchone()
        except Exception as exc:
            raise RemoteOpError(
                "无法读取控制面数据库 %s：%s" % (CONTROL_DB, exc),
                {"op_id": op_id, "error": str(exc)}) from exc
        finally:
            if db is not None:
                db.close()
        if row and row[0] in ("done", "failed"):
            try:
                res = json.loads(row[1] or "{}")
            except Exception:
                res = {}
            if row[0] != "done" or not res.get("ok"):
                raise RemoteOpError(
                    _remote_result_detail(res, fallback="远程执行失败"), res)
            return res
        time.sleep(1)
    raise RemoteOpError("客户机超时未响应（remote_op=%s）" % op_id,
                        {"op_id": op_id, "timeout": timeout})


def _remote_result_detail(result, fallback="远程执行失败"):
    """Preserve the real child-process error instead of only ``error``."""
    result = result if isinstance(result, dict) else {}
    parts = []
    for key in ("error", "message", "stderr", "stdout"):
        value = result.get(key)
        if value:
            parts.append(str(value).strip())
    nested = result.get("result")
    if isinstance(nested, dict):
        for key in ("error", "message", "stderr", "stdout"):
            value = nested.get(key)
            if value:
                parts.append(str(value).strip())
    exit_code = result.get("exit_code")
    if exit_code is None and isinstance(nested, dict):
        exit_code = nested.get("exit_code")
    unique = []
    for value in parts:
        if value and value not in unique:
            unique.append(value)
    if unique:
        detail = "；".join(unique)
        if exit_code is not None:
            detail += "（exit=%s）" % exit_code
        return detail[:4000]
    if exit_code is not None:
        return "%s（exit=%s，stdout/stderr 均为空）" % (fallback, exit_code)
    return fallback


def _cn_schedule_at(seconds_offset=0):
    import datetime as _dt
    tz = _dt.timezone(_dt.timedelta(hours=8))
    return (_dt.datetime.now(tz) + _dt.timedelta(seconds=seconds_offset)).replace(
        microsecond=0).isoformat()


def _mt_content_hash(plan):
    """Recompute the exact publish-content/3 fingerprint used by the client."""
    plan = plan if isinstance(plan, dict) else {}
    piece = plan.get("piece") or {}
    payload = {
        "schema": "publish-content/3",
        "account_id": plan.get("account_id") or "",
        "title": piece.get("title") or "",
        "copy_body": piece.get("copy_body") or "",
        "sha256": piece.get("sha256") or "",
        "topics": piece.get("topics") or [],
        "cover_sha256": piece.get("cover_sha256") or {},
        "covers_requested": sorted(
            key for key, value in (piece.get("covers") or {}).items() if value),
        "platform_account_id": piece.get("platform_account_id") or "",
        "poi": piece.get("poi"),
    }
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _mt_read_client_json(customer_id, path, timeout=90, device_id=None):
    import base64 as _b64
    if not path:
        raise MTBindingError("客户机文件路径缺失")
    raw = remote_op(str(customer_id), "read", {"path": str(path)},
                    timeout=timeout, device_id=device_id)
    value = raw.get("base64")
    if not value and isinstance(raw.get("result"), dict):
        value = raw["result"].get("base64")
    if not value:
        raise RemoteOpError("读取客户机文件失败：%s" % path, raw)
    try:
        return json.loads(_b64.b64decode(value).decode("utf-8-sig"))
    except Exception as exc:
        raise MTBindingError("客户机文件不是有效 JSON：%s" % path) from exc


def _mt_problem_messages(problems):
    messages = []
    for row in problems or []:
        if isinstance(row, dict):
            text = str(row.get("message") or row.get("code") or "").strip()
        else:
            text = str(row or "").strip()
        if text and text not in messages:
            messages.append(text)
    return messages


def _mt_clear_credentials(item):
    for key in ("confirm_token", "confirm_ref", "preview_plan_id",
                "preview_fingerprint", "preview_schedule_at",
                "preview_generated_at", "preview_ok", "preview_problems",
                "preview_warnings", "preview_exit_code", "receipt_id",
                "receipt_plan_id", "receipt_content_hash", "receipt_mode"):
        item.pop(key, None)


def _mt_validate_receipt(plan, receipt):
    plan_id = str((plan or {}).get("plan_id") or "")
    content_hash = str(((plan or {}).get("compliance") or {}).get("content_hash") or "")
    issues = []
    if str((receipt or {}).get("plan_id") or "") != plan_id:
        issues.append("receipt.plan_id 与 plan.plan_id 不一致")
    if str((receipt or {}).get("content_hash") or "") != content_hash:
        issues.append("receipt.content_hash 与 plan 不一致")
    if str((receipt or {}).get("operation_mode") or "") != "approved_publish":
        issues.append("receipt.operation_mode 不是 approved_publish")
    if not receipt.get("receipt_id") or not receipt.get("signature"):
        issues.append("receipt 缺少 receipt_id/signature")
    if issues:
        raise MTBindingError("签发的发布凭据无效：" + "；".join(issues))


_MT_RESCHED = r"""
import json,hashlib,subprocess,os,sys,glob,re,traceback
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

def emit(payload, code=0):
    print("MT_RESCHED_RESULT=" + json.dumps(payload, ensure_ascii=False))
    sys.exit(code)

def parse_obj(text):
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end+1])
        except Exception:
            return {}
    return {}

try:
    plan_path = %(plan)s
    preview_path = %(preview)s
    sched = %(sched)s
    if not preview_path:
        raise RuntimeError("preview path missing")
    plan = json.load(open(plan_path, encoding="utf-8-sig"))
    piece = plan.get("piece") or {}
    piece["platform_account_id"] = ""
    desired_topics = %(topics)s
    if desired_topics is not None:
        piece["topics"] = desired_topics
    plan["piece"] = piece
    payload = {"schema": "publish-content/3",
               "account_id": plan.get("account_id") or "",
               "title": piece.get("title") or "",
               "copy_body": piece.get("copy_body") or "",
               "sha256": piece.get("sha256") or "",
               "topics": piece.get("topics") or [],
               "cover_sha256": piece.get("cover_sha256") or {},
               "covers_requested": sorted(
                   k for k, v in (piece.get("covers") or {}).items() if v),
               "platform_account_id": "",
               "poi": piece.get("poi")}
    plan.setdefault("compliance", {})["content_hash"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()
    if sched:
        plan["schedule_at"] = sched
    tmp = plan_path + ".mt-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, plan_path)
    try:
        os.remove(preview_path)
    except FileNotFoundError:
        pass

    pw = os.path.join(os.environ["LOCALAPPDATA"], "HuolongAgent", "publish-worker")
    cands = []
    for pat in ("Programs/HuolongAgent/publish-assistant-*",
                "Desktop/manju-publish-assistant-*/*",
                "Desktop/publish-assistant-*",
                "Desktop/manju-publish-assistant-*"):
        for env_name in ("LOCALAPPDATA", "USERPROFILE"):
            root = os.environ.get(env_name) or ""
            if root:
                cands += [x for x in glob.glob(os.path.join(root, *pat.split("/")))
                          if os.path.isdir(os.path.join(x, "agent"))]
    cands = sorted(set(cands), key=os.path.getmtime)
    prog = (cands[-1] if cands else
            os.path.join(os.environ.get("USERPROFILE") or "", "Desktop"))
    env = dict(os.environ)
    env["PYTHONPATH"] = prog
    py = os.path.join(pw, ".venv", "Scripts", "python.exe")
    data_root = plan_path
    for _ in range(5):
        data_root = os.path.dirname(data_root)
    args = [py, "-m", "agent.winport.cli", "dry-run",
            "--data", data_root, "--plan", plan_path, "--out", preview_path]
    rp = subprocess.run(args, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", cwd=prog,
                        env=env, timeout=300)
    parsed = parse_obj(rp.stdout)
    confirmation = (parsed or {}).get("confirmation") or {}
    fresh = confirmation.get("fresh") or {}
    problems = fresh.get("problems") or []
    warnings = fresh.get("warnings") or []
    preview = None
    preview_error = ""
    try:
        preview = json.load(open(preview_path, encoding="utf-8-sig"))
    except Exception as exc:
        preview_error = str(exc)
    plan_after = json.load(open(plan_path, encoding="utf-8-sig"))
    token = ((preview or {}).get("confirm_token")
             or fresh.get("confirm_token") or "")
    confirm_ref = ((preview or {}).get("confirm_ref")
                   or fresh.get("confirm_ref") or "")
    preview_plan_id = (preview or {}).get("plan_id") or ""
    preview_hash = (preview or {}).get("material_fingerprint") or ""
    preview_sched = (preview or {}).get("schedule_at") or ""
    if not preview:
        emit({"ok": False, "error": "dry-run 未生成 preview：" + preview_error,
              "dryrun_exit": rp.returncode,
              "stderr": (rp.stderr or "")[-2000:],
              "stdout": (rp.stdout or "")[-2000:]}, 2)
    if not token or not confirm_ref:
        emit({"ok": False, "error": "dry-run preview 缺少 confirm_token/confirm_ref",
              "dryrun_exit": rp.returncode,
              "stderr": (rp.stderr or "")[-2000:],
              "stdout": (rp.stdout or "")[-2000:]}, 2)
    emit({"ok": True,
          "plan_id": plan_after.get("plan_id") or "",
          "schedule_at": plan_after.get("schedule_at") or "",
          "content_hash": (plan_after.get("compliance") or {}).get("content_hash") or "",
          "preview_plan_id": preview_plan_id,
          "preview_fingerprint": preview_hash,
          "preview_schedule_at": preview_sched,
          "preview_generated_at": (preview or {}).get("generated_at") or "",
          "confirm_token": token,
          "confirm_ref": confirm_ref,
          "dryrun_exit": rp.returncode,
          "problems": problems,
          "warnings": warnings,
          "reason": (confirmation.get("reason")
                     or ((confirmation.get("fresh") or {}).get("reason"))
                     or ""),
          "stderr": (rp.stderr or "")[-2000:],
          "stdout_tail": (rp.stdout or "")[-1000:]}, 0)
except Exception as exc:
    emit({"ok": False, "error": str(exc),
          "trace": traceback.format_exc()[-2000:]}, 2)
"""


def _mt_reschedule_named(cid, item, plan_path, preview_path, schedule_at, device_id):
    """1.2.221+ clients: the reschedule is product code (``named`` op).

    Returns the operation's result dict, or None when the client predates
    named operations (then the caller falls back to the legacy script)."""
    args = {"plan": plan_path, "preview": preview_path,
            "schedule_at": str(schedule_at), "topics": item.get("desired_topics")}
    try:
        res = remote_op(cid, "named", {"name": "plan-reschedule", "args": args},
                        timeout=460, device_id=device_id)
    except RemoteOpError as exc:
        detail = str(exc)
        if "unsupported op" in detail or "unknown named" in detail:
            return None
        res = exc.result if isinstance(exc.result, dict) else {}
        if not res:
            raise MTBindingError("客户机重排失败：%s" % detail) from exc
    if isinstance(res.get("result"), dict) and "ok" in res["result"]:
        res = res["result"]
    return res


def _mt_reschedule_legacy(cid, item, plan_path, preview_path, schedule_at, device_id):
    """Clients before 1.2.221: ship _MT_RESCHED (needs remote assist on 1.2.221+)."""
    code = _MT_RESCHED % {
        "plan": json.dumps(plan_path, ensure_ascii=True),
        "sched": json.dumps(str(schedule_at), ensure_ascii=True),
        "preview": json.dumps(preview_path, ensure_ascii=True),
        "topics": repr(item.get("desired_topics")),
    }
    import base64 as _b64
    import uuid
    # A fixed ProgramData path lets two reschedules overwrite each other's
    # script between the write and exec calls.  Keep every invocation private.
    script_tag = uuid.uuid4().hex
    py_path = "C:\\ProgramData\\mt_resched_%s.py" % script_tag
    bat_path = "C:\\ProgramData\\mt_resched_%s.bat" % script_tag
    bat = ("@echo off\r\nsetlocal\r\nset \"PW=%LOCALAPPDATA%\\HuolongAgent\\publish-worker\"\r\n"
           "\"%PW%\\.venv\\Scripts\\python.exe\" \"" + py_path + "\"\r\n")
    remote_op(cid, "write", {"path": py_path,
                             "base64": _b64.b64encode(code.encode("utf-8")).decode()},
              timeout=60, device_id=device_id)
    remote_op(cid, "write", {"path": bat_path,
                             "base64": _b64.b64encode(bat.encode("utf-8")).decode()},
              timeout=60, device_id=device_id)
    try:
        res = remote_op(cid, "exec",
                        {"cmd": ["cmd", "/c", bat_path], "timeout": 420},
                        timeout=460, device_id=device_id)
    except RemoteOpError as exc:
        res = exc.result
    sentinel = None
    for line in reversed((res.get("stdout") or "").splitlines()):
        if line.strip().startswith("MT_RESCHED_RESULT="):
            try:
                sentinel = json.loads(line.strip().split("=", 1)[1])
            except ValueError:
                sentinel = None
            break
    if not isinstance(sentinel, dict):
        raise MTBindingError(
            "重排脚本没有返回可解析结果：%s"
            % _remote_result_detail(res, "客户机重排失败"))
    return sentinel



def _mt_reschedule_item(chat_id, item, schedule_at, require_window=False,
                        device_id=None):
    """Rewrite one plan and issue a preview/token for that exact final plan.

    The old preview is deleted on the client before dry-run.  A plan without
    matching preview/token is never left usable by the dispatcher.
    """
    customer = lookup_customer(chat_id) or {}
    cid = str(customer.get("customer_id") or "")
    if not cid:
        raise MTBindingError("会话未绑定客户")
    plan_path = str(item.get("plan_path") or "")
    preview_path = str(item.get("preview_path") or "")
    if not plan_path:
        raise MTBindingError("计划路径缺失")
    if not preview_path:
        raise MTBindingError("预览文件路径缺失")
    if not str(schedule_at or "").strip():
        raise MTBindingError("计划时间缺失")
    device_id = device_id or item.get("device_id")

    _mt_clear_credentials(item)
    sentinel = _mt_reschedule_named(cid, item, plan_path, preview_path,
                                    schedule_at, device_id)
    if sentinel is None:            # client older than 1.2.221
        sentinel = _mt_reschedule_legacy(cid, item, plan_path, preview_path,
                                         schedule_at, device_id)
    if not sentinel.get("ok"):
        raise MTBindingError(
            "重建计划/预览失败：%s"
            % _remote_result_detail(sentinel, "dry-run 未生成有效 preview"))

    plan = _mt_read_client_json(cid, plan_path, device_id=device_id)
    preview = _mt_read_client_json(cid, preview_path, device_id=device_id)
    plan_id = str(plan.get("plan_id") or "")
    content_hash = str((plan.get("compliance") or {}).get("content_hash") or "")
    canonical_hash = _mt_content_hash(plan)
    token = str(item.get("confirm_token") or preview.get("confirm_token") or "")
    token = str(preview.get("confirm_token") or token)
    confirm_ref = str(preview.get("confirm_ref") or "")
    problems = preview.get("problems") or sentinel.get("problems") or []
    warnings = preview.get("warnings") or sentinel.get("warnings") or []
    fatal_problems = [
        problem for problem in problems
        if str((problem or {}).get("code") or "") != "SCHEDULE_OUT_OF_WINDOW"
    ]
    if not plan_id:
        raise MTBindingError("计划缺少 plan_id")
    if item.get("plan_id") and str(item.get("plan_id")) != plan_id:
        raise MTBindingError("重排后 plan_id 发生变化：%s -> %s"
                             % (item.get("plan_id"), plan_id))
    if str(plan.get("schedule_at") or "") != str(schedule_at):
        raise MTBindingError("计划文件时间与重排时间不一致：%s != %s"
                             % (plan.get("schedule_at"), schedule_at))
    if not content_hash or canonical_hash != content_hash:
        raise MTBindingError("计划 content_hash 无法复算：plan=%s calc=%s"
                             % (content_hash, canonical_hash))
    if str(sentinel.get("content_hash") or "") != content_hash:
        raise MTBindingError("重排脚本回报的 content_hash 与计划文件不一致")
    if str(preview.get("plan_id") or "") != plan_id:
        raise MTBindingError("preview.plan_id 与 plan.plan_id 不一致")
    if str(preview.get("material_fingerprint") or "") != content_hash:
        raise MTBindingError("preview.material_fingerprint 与 plan.content_hash 不一致")
    if str(preview.get("schedule_at") or "") != str(plan.get("schedule_at") or ""):
        raise MTBindingError("preview.schedule_at 与 plan.schedule_at 不一致")
    if not token:
        raise MTBindingError("dry-run 没有返回 confirm_token")
    if str(preview.get("confirm_token") or "") != token:
        raise MTBindingError("preview.confirm_token 与待下发 token 不一致")
    if confirm_ref != "dryrun:" + token[:16]:
        raise MTBindingError("preview.confirm_ref 与 confirm_token 不匹配")
    if require_window:
        messages = _mt_problem_messages(problems)
        if (sentinel.get("dryrun_exit") not in (0, None, "0")
                or messages or preview.get("ok") is False):
            detail = "；".join(messages) or sentinel.get("reason") or sentinel.get("stderr")
            raise MTBindingError(
                "dry-run 未通过发布窗口校验（exit=%s）：%s"
                % (sentinel.get("dryrun_exit"), detail or "无诊断输出"))
    elif fatal_problems:
        raise MTBindingError("dry-run 发现阻断问题：%s"
                             % "；".join(_mt_problem_messages(fatal_problems)))

    item["plan_data"] = plan
    item["plan_id"] = plan_id
    item["content_hash"] = content_hash
    item["schedule_at"] = str(plan.get("schedule_at") or schedule_at)
    item["confirm_token"] = token
    item["confirm_ref"] = confirm_ref
    item["preview_plan_id"] = str(preview.get("plan_id") or "")
    item["preview_fingerprint"] = str(preview.get("material_fingerprint") or "")
    item["preview_schedule_at"] = str(preview.get("schedule_at") or "")
    item["preview_generated_at"] = str(preview.get("generated_at") or "")
    item["preview_ok"] = not problems and sentinel.get("dryrun_exit") in (0, None, "0")
    item["preview_problems"] = problems
    item["preview_warnings"] = warnings
    item["preview_exit_code"] = sentinel.get("dryrun_exit")
    title = str((plan.get("piece") or {}).get("title") or "")
    if title:
        item["title"] = title
    return item


def _mt_validate_dispatch_binding(chat_id, item):
    """Fail closed unless plan, preview, token and item metadata are identical."""
    customer = lookup_customer(chat_id) or {}
    cid = str(customer.get("customer_id") or "")
    if not cid:
        raise MTBindingError("会话未绑定客户")
    device_id = item.get("device_id")
    plan = _mt_read_client_json(cid, item.get("plan_path"), device_id=device_id)
    preview = _mt_read_client_json(cid, item.get("preview_path"), device_id=device_id)
    plan_id = str(plan.get("plan_id") or "")
    content_hash = str((plan.get("compliance") or {}).get("content_hash") or "")
    schedule_at = str(plan.get("schedule_at") or "")
    token = str(item.get("confirm_token") or "")
    confirm_ref = str(preview.get("confirm_ref") or "")
    problems = preview.get("problems") or []
    issues = []
    if not plan_id:
        issues.append("plan.plan_id 缺失")
    if str(item.get("plan_id") or "") != plan_id:
        issues.append("条目的 plan_id 与计划文件不一致")
    if str(item.get("schedule_at") or "") != schedule_at:
        issues.append("条目的 schedule_at 与计划文件不一致")
    if not content_hash or _mt_content_hash(plan) != content_hash:
        issues.append("计划 content_hash 无法复算")
    if str(item.get("content_hash") or "") != content_hash:
        issues.append("条目的 content_hash 与计划文件不一致")
    item_device = str(item.get("device_id") or "")
    plan_device = str(plan.get("device_id") or "")
    if plan_device and item_device and plan_device != item_device:
        issues.append("计划设备与条目设备不一致")
    plan_epoch = plan.get("binding_epoch")
    item_epoch = item.get("binding_epoch")
    if (plan_epoch is not None and item_epoch is not None and
            int(plan_epoch) != int(item_epoch)):
        issues.append("计划 epoch 与条目 epoch 不一致")
    if not token:
        issues.append("confirm_token 缺失")
    if str(preview.get("plan_id") or "") != plan_id:
        issues.append("preview.plan_id 与 plan.plan_id 不一致")
    if str(preview.get("material_fingerprint") or "") != content_hash:
        issues.append("preview.material_fingerprint 与 plan.content_hash 不一致")
    if str(preview.get("schedule_at") or "") != schedule_at:
        issues.append("preview.schedule_at 与 plan.schedule_at 不一致")
    if str(preview.get("confirm_token") or "") != token:
        issues.append("preview.confirm_token 与待下发 token 不一致")
    if confirm_ref != "dryrun:" + token[:16]:
        issues.append("preview.confirm_ref 与 confirm_token 不匹配")
    if preview.get("ok") is False or problems:
        issues.append("preview 仍有阻断问题：" + "；".join(
            _mt_problem_messages(problems) or ["confirmation.ok=false"]))
    if issues:
        raise MTBindingError("下发前凭据校验失败：" + "；".join(issues))
    item["plan_data"] = plan
    item["content_hash"] = content_hash
    item["schedule_at"] = schedule_at
    item["preview_plan_id"] = plan_id
    item["preview_fingerprint"] = content_hash
    item["preview_schedule_at"] = schedule_at
    return item


def _mt_command_id(state, item):
    material = "|".join([
        str((state or {}).get("batch_id") or ""),
        str(item.get("device_id") or (state or {}).get("device_id") or ""),
        str(item.get("index") or ""),
        str(item.get("plan_id") or ""),
        str(item.get("schedule_at") or ""),
        str(item.get("content_hash") or ""),
        str(item.get("confirm_token") or ""),
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _mt_display_error(detail):
    raw = str(detail or "").strip()
    # The publish CLI streams PUBLISH_STAGE machine logs to stderr.  Dumping
    # them into the chat is unreadable; keep only the human verdict line.
    _lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    _verdict = [ln for ln in _lines
                if ln.startswith("ERROR(") or ln.startswith("BLOCKED")]
    if _verdict:
        raw = _verdict[-1]
    elif any(ln.startswith("PUBLISH_STAGE") for ln in _lines):
        _rest = [ln for ln in _lines if not ln.startswith("PUBLISH_STAGE")]
        raw = "\n".join(_rest)[-300:] or _lines[-1][:300]
    if not raw:
        return "客户机返回失败，但没有 stdout/stderr；已停止本批，不会自动重试。"
    if "confirm_token_missing" in raw.lower():
        return "确认凭据缺失（confirm_token_missing），已停止本批，不会自动重试。"
    message = friendly_error(raw, raw[:300])
    if message == raw and _looks_like_machine_error(raw) and not _has_chinese(raw):
        return "客户机返回：%s" % raw[:300]
    return message


def _mt_pause_remaining_locked(state, reason, exclude=None):
    paused = 0
    for other in state.get("items") or []:
        if other is exclude:
            continue
        if str(other.get("status")) in ("pending", "scheduled"):
            other["status"] = "paused"
            other["error"] = str(reason or "前一条失败，已停止剩余发布")[:500]
            other["paused_at"] = time.time()
            paused += 1
    return paused


def _mt_record_failure_locked(state, item, detail):
    detail = str(detail or "发布失败，未提供诊断")[:1000]
    item["status"] = "failed"
    item["error"] = detail
    item["failed_at"] = time.time()
    _mt_pause_remaining_locked(state, "前一条失败，已停止本批剩余发布")
    state["status"] = "failed"
    state["failed_at"] = time.time()
    state["last_error"] = detail
    save_mt_batch(state)


def _dispatch_mt_item(chat_id, state, item):
    """Atomically claim one item, then enqueue its matching receipt."""
    with MT_BATCH_LOCK:
        current = load_mt_batch(state.get("batch_id")) or state
        if _mt_is_cancelled(current) or state.get("cancel_requested") or chat_id in MT_CANCEL_SIGNALS:
            raise MTBindingError("batch cancelled; no dispatch")
        current_item = next((i for i in current.get("items", [])
                             if i.get("index") == item.get("index")), None)
        if current_item and (current_item.get("command_id") or
                             current_item.get("status") not in ("scheduled", "pending")):
            raise MTBindingError("\u6301\u4e45\u5316\u6761\u76ee\u72b6\u6001\u4e0d\u662f\u5f85\u4e0b\u53d1\uff1a\u5df2\u5360\u7528\u6216\u5df2\u7ed3\u675f")
        if str(item.get("status")) not in ("scheduled", "pending"):
            raise MTBindingError("条目状态不是待下发：%s" % item.get("status"))
        if item.get("command_id") and str(item.get("status")) == "dispatching":
            raise MTBindingError("该条目已有在途指令，需人工核对，禁止重发")
        _mt_validate_dispatch_binding(chat_id, item)
        if state.get("confirmation_digest") and _mt_contract_digest(state) != state["confirmation_digest"]:
            raise MTBindingError("confirmed content changed; new confirmation required")
        plan = item.get("plan_data") or {}
        device_id = str(item.get("device_id") or state.get("device_id") or "")
        legacy = bool(state.get("legacy") or item.get("legacy"))
        if not device_id and not legacy:
            raise MTBindingError("条目缺少设备归属，禁止下发")
        epoch = item.get("binding_epoch")
        if epoch is None:
            epoch = state.get("binding_epoch")
        receipt = receipt_issue.issue_receipt(
            plan, _approver_id(chat_id), operation_mode="approved_publish",
            device_id=device_id or None,
            binding_epoch=(int(epoch) if epoch is not None else None))
        _mt_validate_receipt(plan, receipt)
        # Claim and durable stop share a lock; after this claim the one item is in flight.
        with MT_DIALOG_LOCK:
            if chat_id in MT_CANCEL_SIGNALS or _mt_is_cancelled(state):
                raise MTBindingError("cancel received before enqueue")
            command_id = _mt_command_id(state, item)
            item["command_id"] = command_id
            item["status"] = "dispatching"
            item["dispatch_attempt"] = int(item.get("dispatch_attempt") or 0) + 1
            item["dispatch_started_at"] = time.time()
            item["receipt_id"] = receipt.get("receipt_id")
            item["receipt_plan_id"] = receipt.get("plan_id")
            item["receipt_content_hash"] = receipt.get("content_hash")
            item["receipt_mode"] = receipt.get("operation_mode")
            state["status"] = "active"
            save_mt_batch(state)

        payload = {
            "op": "confirm",
            "plan": item.get("plan_path"),
            "preview": item.get("preview_path"),
            "confirm_token": item.get("confirm_token"),
            "receipt": receipt,
            "allow_submit": True,
            "canary_armed": True,
            "timeout": 1800,
            "plan_data": plan,
            "title": item.get("title") or "",
            "name": item.get("name") or "",
            "material_sha256": (plan.get("piece") or {}).get("sha256") or "",
        }
        if device_id:
            payload["device_id"] = device_id
            payload["target_device_id"] = device_id
        try:
            response = enqueue_for_chat(
                chat_id, "publish", payload, command_id=command_id,
                device_id=device_id or None)
        except Exception as exc:
            detail = _remote_result_detail(
                getattr(exc, "result", {}), str(exc) or "指令投递失败")
            _mt_record_failure_locked(state, item, detail)
            raise MTBindingError("第 %d 条指令投递失败：%s"
                                 % (int(item.get("index") or 0), detail)) from exc
        command = response.get("command") or {}
        returned_id = str(command.get("command_id") or command.get("id") or "")
        if returned_id and returned_id != command_id:
            detail = "控制面返回的 command_id 与本地固定 id 不一致"
            _mt_record_failure_locked(state, item, detail)
            raise MTBindingError("第 %d 条%s：%s != %s"
                                 % (int(item.get("index") or 0), detail,
                                    command_id, returned_id))
        item["status"] = "dispatched"
        item["dispatched_at"] = time.time()
        state["status"] = "active"
        save_mt_batch(state)
        return item


def _dispatch_due_batches():
    """Dispatch at most one item per batch, with one attempt and no retry."""
    fired = 0
    try:
        names = sorted(os.listdir(MT_BATCH_DIR))
    except OSError:
        return 0
    with MT_BATCH_LOCK:
        for name in names:
            if not name.endswith(".json") or name.startswith("latest_"):
                continue
            path = os.path.join(MT_BATCH_DIR, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    state = json.load(fh)
            except Exception:
                continue
            if not isinstance(state, dict):
                continue
            if _mt_is_cancelled(state) or state.get("chat_id") in MT_CANCEL_SIGNALS:
                _mt_cancel_state(state)
                save_mt_batch(state)
                continue
            if str(state.get("status")) not in ("scheduled", "active"):
                continue
            in_flight = any(
                str(i.get("status")) in ("dispatching", "dispatched",
                                         "needs_reconcile")
                for i in (state.get("items") or []))
            if in_flight:
                # Never fan out a second publish while one is still in flight.
                continue
            scheduled = [i for i in (state.get("items") or [])
                         if isinstance(i, dict) and i.get("status") == "scheduled"]
            if not scheduled:
                continue
            chat_id = str(state.get("chat_id") or "")
            try:
                bound = (lookup_customer(chat_id) or {}).get("customer_id")
            except Exception as exc:
                print("[sched] customer lookup failed: %s" % exc, flush=True)
                continue
            if str(bound or "") != str(state.get("customer_id") or ""):
                state["status"] = "cancelled"
                state["cancel_reason"] = "chat rebound before dispatch"
                save_mt_batch(state)
                continue
            scheduled.sort(key=lambda row: (
                _parse_schedule_ts(row.get("schedule_at")) or float("inf"),
                int(row.get("index") or 0)))
            item = scheduled[0]
            due = _parse_schedule_ts(item.get("schedule_at"))
            now_ts = time.time()
            if due is None:
                detail = "计划时间无效：%s" % item.get("schedule_at")
                _mt_record_failure_locked(state, item, detail)
                reply(chat_id, "第 %d 条%s；本批已停止，不会自动重试。"
                      % (int(item.get("index") or 0), detail))
                continue
            if now_ts - due > 600:
                item["status"] = "expired"
                item["error"] = "错过排期窗口（超过计划时间 10 分钟）"
                _mt_pause_remaining_locked(
                    state, "前一条错过排期窗口，已停止本批剩余发布", exclude=item)
                state["status"] = "expired"
                save_mt_batch(state)
                reply(chat_id, "排期已过，第 %d 条没有发出；本批已停止，请重新发「发布」。"
                      % int(item.get("index") or 0))
                continue
            if due - now_ts > SCHEDULE_DISPATCH_LEAD:
                continue
            try:
                if state.get("confirmation_digest") and _mt_contract_digest(state) != state["confirmation_digest"]:
                    raise MTBindingError("confirmed content changed before dispatch")
                _mt_reschedule_item(
                    chat_id, item, item.get("schedule_at"), require_window=True)
                if state.get("confirmation_digest") and _mt_contract_digest(state) != state["confirmation_digest"]:
                    raise MTBindingError("confirmed content changed while refreshing preview")
                _dispatch_mt_item(chat_id, state, item)
                fired += 1
                reply(chat_id, "到点了，正在发布第 %d 条：%s"
                      % (int(item.get("index") or 0),
                         item.get("title") or item.get("name") or ""))
            except Exception as exc:
                detail = _remote_result_detail(
                    getattr(exc, "result", {}), str(exc) or "排期发布失败")
                _mt_record_failure_locked(state, item, detail)
                reply(chat_id, "排期发布失败（第 %d 条）：%s\n本批已停止，不会自动重试。"
                      % (int(item.get("index") or 0), _mt_display_error(detail)))
            # One dispatch attempt per batch per cycle; never fan out three
            # commands in the same 30-second window.
            break
    return fired


def start_scheduled_dispatcher():
    def _loop():
        while True:
            try:
                count = _dispatch_due_batches()
                if count:
                    print("[sched] dispatched %d item(s)" % count, flush=True)
            except Exception as exc:
                print("scheduled dispatcher error", exc, flush=True)
            time.sleep(30)
    threading.Thread(target=_loop, daemon=True,
                     name="mt-scheduled-dispatch").start()


def confirm_mt_batch(chat_id, batch_id, confirmation_digest=None):
    with MT_BATCH_LOCK:
        state = load_mt_batch(batch_id)
        if not state or str(state.get("chat_id")) != str(chat_id):
            reply(chat_id, "没有待确认的批量发布。")
            return
        try:
            customer = lookup_customer(chat_id)
        except ControlPlaneError:
            reply(chat_id, "控制面暂时不可达，暂时不能确认发布，请稍后再试。")
            return
        if (not customer
                or str(customer.get("customer_id") or "")
                != str(state.get("customer_id") or "")):
            state["status"] = "cancelled"
            state["cancel_reason"] = "chat rebound before confirmation"
            save_mt_batch(state)
            reply(chat_id, "这个会话已经不再绑定该客户，这批发布已取消。请重新发「发布」。")
            return
        d = _dialog_load(chat_id, state.get("customer_id"))
        if (_mt_is_cancelled(state) or chat_id in MT_CANCEL_SIGNALS or
                not state.get("topics_answered") or
                not confirmation_digest or confirmation_digest != state.get("confirmation_digest") or
                confirmation_digest != _mt_contract_digest(state) or
                d.get("status") != "awaiting_confirmation" or
                d.get("batch_id") != batch_id or d.get("revision") != state.get("draft_revision")):
            reply(chat_id, "\u786e\u8ba4\u5361\u5df2\u5931\u6548\u6216\u672a\u5b8c\u6210\u8bdd\u9898/\u6807\u9898\u786e\u8ba4\uff0c\u8bf7\u91cd\u65b0\u751f\u6210\u8349\u7a3f\u3002")
            return
        if state.get("status") != "pending":
            reply(chat_id, "这批已经提交，不用重复点。")
            return
        if float(state.get("expires_at") or 0) <= time.time():
            state["status"] = "expired"
            save_mt_batch(state)
            reply(chat_id, "这批确认已超时，请重新发「发布」。")
            return
        scheduled = 0
        for item in state.get("items") or []:
            if str(item.get("status")) == "pending":
                item["status"] = "scheduled"
                scheduled += 1
        state["status"] = "scheduled"
        state["confirmed_at"] = time.time()
        save_mt_batch(state)

        d['status'] = 'scheduled'
        _dialog_save(d)
        reply(chat_id, "\u5df2\u786e\u8ba4 %d \u6761\uff0c\u6309\u786e\u8ba4\u5361\u6392\u671f\u7b49\u5f85\u8c03\u5ea6\u3002" % scheduled)


def _mt_result_state():
    try:
        with open(MT_RESULT_STATE, encoding="utf-8") as fh:
            state = json.load(fh)
        if isinstance(state, dict) and isinstance(state.get("seen"), dict):
            return state
        raise ValueError("invalid result state; manual recovery required")
    except FileNotFoundError:
        pass
    return {"seen": {}, "since": time.time()}


def _save_mt_result_state(state):
    parent = os.path.dirname(os.path.abspath(MT_RESULT_STATE))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    tmp = MT_RESULT_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, MT_RESULT_STATE)
    fd = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mt_terminal(command):
    return str(command.get("status") or "") in ("done", "failed", "cancelled", "error")


def _mt_result_error_text(command):
    """Extract every available diagnostic from a terminal publish command."""
    command = command if isinstance(command, dict) else {}
    result = command.get("result")
    result = result if isinstance(result, dict) else {}
    nested = result.get("result")
    nested = nested if isinstance(nested, dict) else {}
    parts = []
    for source in (result, nested, command):
        for key in ("error", "message", "stderr", "stdout"):
            value = source.get(key) if isinstance(source, dict) else None
            if value:
                text = str(value).strip()
                if text and text not in parts:
                    parts.append(text)
    parsed = None
    for value in (result.get("stdout"), nested.get("stdout")):
        if not value:
            continue
        try:
            parsed = _parse_json_blob(value)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            break
    if isinstance(parsed, dict):
        confirmation = parsed.get("confirmation") or {}
        fresh = confirmation.get("fresh") or {}
        for problem in (fresh.get("problems") or confirmation.get("problems") or []):
            text = str((problem or {}).get("message")
                       or (problem or {}).get("code") or "").strip()
            if text and text not in parts:
                parts.append(text)
        reason = str(confirmation.get("reason")
                     or fresh.get("reason") or parsed.get("error") or "").strip()
        if reason and reason not in parts:
            parts.append(reason)
    exit_code = result.get("exit_code")
    if exit_code is None:
        exit_code = nested.get("exit_code")
    if not parts:
        if exit_code is not None:
            return ("客户机返回 exit=%s，但 stdout/stderr 均为空，未提供诊断；"
                    "已停止本批，不会自动重试。" % exit_code)
        return "客户机没有返回错误文本；已停止本批，不会自动重试。"
    detail = "\n".join(parts)
    if exit_code is not None:
        detail += "\n（exit=%s）" % exit_code
    return detail[:4000]


def _mt_identity(command):
    payload = command.get("payload") or {}
    plan = payload.get("plan_data") or {}
    return (str(command.get("customer_id") or ""),
            str((plan.get("compliance") or {}).get("content_hash") or ""),
            str((payload.get("receipt") or {}).get("receipt_id") or ""))


def _mt_match_evidence(command, ledger, ledger_customer):
    cid, content_hash, receipt_id = _mt_identity(command)
    if not all((cid, content_hash, receipt_id)) or ledger_customer != cid:
        return None
    for entry in (ledger.get("entries") or {}).values():
        if not isinstance(entry, dict):
            continue
        if (entry.get("customer_id", ledger_customer) != cid
                or entry.get("content_hash") != content_hash
                or entry.get("receipt_id") != receipt_id):
            continue
        evidence = entry.get("publish_evidence") or {}
        proof = evidence.get("receipt") if isinstance(evidence, dict) else {}
        proof = proof if isinstance(proof, dict) else {}
        command_device = str(command.get("device_id") or "")
        proof_device = str(proof.get("device_id") or "")
        if command_device and proof_device and command_device != proof_device:
            continue
        toast = (entry.get("publish_confirmed") is True
                 and entry.get("publish_confirmed_signal") == "success_toast")
        create = (entry.get("state") == "CONSUMED"
                  and evidence.get("ok") is True
                  and evidence.get("signal") == "create_v2")
        if toast or create:
            return {"customer_id": cid, "content_hash": content_hash,
                    "receipt_id": receipt_id,
                    "source": "success_toast" if toast else "create_v2",
                    "platform_work_id": (entry.get("publish_confirmed_work_id")
                                         or entry.get("platform_work_id")
                                         or evidence.get("work_id") or "")}
    return None


def _mt_normalize_result(command, evidence=None):
    """Pure result contract. published means submitted, never review-approved."""
    result = command.get("result") or {}
    result = result if isinstance(result, dict) else {}
    payload = command.get("payload") or {}
    nested = result.get("result") or {}
    sources = [("result", result)]
    if isinstance(nested, dict):
        sources.append(("result.result", nested))
    for name, source in list(sources):
        try:
            parsed = _parse_json_blob(source.get("stdout") or "")
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            sources.append((name + ".stdout", parsed))
    reasons, true_at, false_at, exits = [], [], [], []
    for name, source in sources:
        if "published" in source:
            value = source["published"]
            if value is True:
                true_at.append(name + ".published=true")
            elif value is False:
                false_at.append(name + ".published=false")
            else:
                reasons.append(name + ".published is not boolean")
        if source.get("exit_code") is not None:
            value = source["exit_code"]
            if type(value) is not int:
                reasons.append(name + ".exit_code is not integer")
            else:
                exits.append(value)
    cid, content_hash, receipt_id = _mt_identity(command)
    strong = bool(evidence and all((cid, content_hash, receipt_id))
                  and (evidence.get("customer_id"), evidence.get("content_hash"),
                       evidence.get("receipt_id")) == (cid, content_hash, receipt_id)
                  and evidence.get("source") in ("success_toast", "create_v2"))
    if not (cid and (command.get("command_id") or command.get("id"))
            and command.get("kind") == "publish"
            and payload.get("op") == "confirm"):
        reasons.append("command identity or confirm operation missing")
    if result.get("stage") not in (None, "confirm"):
        reasons.append("result.stage conflicts with confirm operation")
    for name, source in sources:
        for flag in ("allow_submit", "canary_armed"):
            if flag in source and source[flag] is not True and (true_at or strong or 0 in exits):
                reasons.append(name + "." + flag + " conflicts with submission")
        for key, expected in (("customer_id", cid),
                              ("command_id", command.get("command_id") or command.get("id"))):
            if source.get(key) and source[key] != expected:
                reasons.append(name + "." + key + " mismatch")
        if source.get("publish_outcome") == "UNKNOWN":
            reasons.append(name + ".publish_outcome=UNKNOWN")
    if false_at and (true_at or strong or 0 in exits):
        reasons.extend(false_at + true_at)
        if strong:
            reasons.append("ledger." + evidence["source"])
        if 0 in exits:
            reasons.append("exit_code=0 conflicts with explicit false")
    if any(code != 0 for code in exits) and (true_at or strong or 0 in exits):
        reasons.append("nonzero exit conflicts with success evidence")
    if command.get("status") != "done" and (true_at or strong or 0 in exits):
        reasons.append("command terminal status conflicts with success evidence")
    empty = all(not str(src.get("stdout") or "").strip() for _, src in sources)
    compatibility = (not true_at and not false_at and 0 in exits and empty
                     and payload.get("allow_submit") is True
                     and payload.get("canary_armed") is True
                     and (payload.get("receipt") or {}).get("operation_mode") == "approved_publish"
                     and all((content_hash, receipt_id))
                     and result.get("protocol_version") in (None, 1))
    if reasons:
        status = "needs_reconcile"
    elif false_at or any(code != 0 for code in exits):
        status = "failed"
    elif true_at or strong or compatibility:
        status = "published"
    else:
        status = "needs_reconcile"
        reasons.append("insufficient submission evidence")
    review_values = {src.get("review_status") for _, src in sources
                     if src.get("review_status") in ("unknown", "reviewing", "approved", "rejected")}
    review = next(iter(review_values)) if len(review_values) == 1 else "unknown"
    work_id = next((str(src["platform_work_id"]) for _, src in sources
                    if src.get("platform_work_id") and str(src["platform_work_id"]).strip() != "-"), "")
    if strong:
        ledger_work = str(evidence.get("platform_work_id") or "")
        if work_id and ledger_work and work_id != ledger_work:
            reasons.append("result.platform_work_id conflicts with ledger")
            status = "needs_reconcile"
        if not work_id:
            work_id = ledger_work
    return {"contract_version": 1, "status": status, "published": status == "published",
            "platform_work_id": work_id, "review_status": review,
            "reasons": reasons, "evidence_source": (
                evidence["source"] if strong else "explicit" if true_at or false_at
                else "legacy_empty_exit_zero" if compatibility else "none")}


def _mt_render_result(result):
    if result["status"] == "published":
        text = ("\u5df2\u63d0\u4ea4\uff0c\u4f5c\u54c1\u53f7\uff1a" + result["platform_work_id"]
                if result.get("platform_work_id") else "\u5df2\u63d0\u4ea4\uff0c\u5e73\u53f0\u4f5c\u54c1\u53f7\u6682\u672a\u8fd4\u56de")
        review = {"unknown": "\u672a\u77e5", "reviewing": "\u5ba1\u6838\u4e2d",
                  "approved": "\u5ba1\u6838\u901a\u8fc7", "rejected": "\u5ba1\u6838\u672a\u901a\u8fc7"}
        return text + "\n\u5e73\u53f0\u5ba1\u6838\u72b6\u6001\uff1a" + review[result["review_status"]]
    if result["status"] == "needs_reconcile":
        return "\u53d1\u5e03\u7ed3\u679c\u5f85\u6838\u5b9e\uff1a%s\n\u672c\u6279\u5df2\u505c\u6b62\uff0c\u4e0d\u4f1a\u81ea\u52a8\u91cd\u8bd5\u3002" % "\uff1b".join(result["reasons"])
    text = "\u672c\u6b21\u53d1\u5e03\u672a\u786e\u8ba4\u6210\u529f\uff1b\u672c\u6279\u5df2\u505c\u6b62\uff0c\u4e0d\u4f1a\u81ea\u52a8\u91cd\u8bd5\u3002"
    if result.get("error"):
        text += "\n" + result["error"]
    if result.get("republish_confirmation_required"):
        text += "\n" + REPUBLISH_ASK
    return text


def _mt_apply_result(chat_id, command, result):
    command_id = str(command.get("command_id") or command.get("id") or "")
    cid = str(command.get("customer_id") or "")
    if not command_id or not cid:
        return
    with MT_BATCH_LOCK:
        try:
            names = os.listdir(MT_BATCH_DIR)
        except FileNotFoundError:
            return
        for name in names:
            if not name.endswith(".json") or name.startswith("latest_"):
                continue
            with open(os.path.join(MT_BATCH_DIR, name), encoding="utf-8") as fh:
                batch = json.load(fh)
            if batch.get("chat_id") != chat_id or batch.get("customer_id") != cid:
                continue
            for item in batch.get("items") or []:
                if item.get("command_id") != command_id:
                    continue
                if item.get("normalized_result") == result and item.get("status") == result["status"]:
                    return
                item["normalized_result"] = result
                item["status"] = result["status"]
                item["work_id"] = result["platform_work_id"]
                item["review_status"] = result["review_status"]
                if result["status"] != "published":
                    item["error"] = "; ".join(result["reasons"]) or "submission not confirmed"
                    _mt_pause_remaining_locked(batch, "\u524d\u4e00\u6761\u7ed3\u679c\u672a\u786e\u8ba4\uff0c\u505c\u6b62\u5269\u4f59\u53d1\u5e03", exclude=item)
                    batch["status"] = result["status"]
                else:
                    item.pop("error", None)
                    item.setdefault("published_at", time.time())
                    statuses = {i.get("status") for i in batch.get("items") or []}
                    batch["status"] = ("needs_reconcile" if "needs_reconcile" in statuses
                                       else "failed" if statuses & {"failed", "paused"}
                                       else "active" if statuses & {"scheduled", "dispatching", "dispatched"}
                                       else "completed")
                save_mt_batch(batch)
                return


def _mt_store_result(state, chat_id, command):
    command_id = str(command.get("command_id") or command.get("id") or "")
    if not command_id:
        raise ValueError("result has no command identity")
    results = state.setdefault("results", {})
    if command_id not in results:
        result = _mt_normalize_result(command, _mt_publish_evidence(chat_id, command))
        # Store no signed receipt or raw platform URL in the notification file.
        detail = _mt_result_error_text(command)
        if result["status"] == "failed":
            result["error"] = _mt_display_error(detail)
            payload = command.get("payload") or {}
            if (is_idempotency_consumed(detail) and payload.get("plan")
                    and isinstance(payload.get("plan_data"), dict)):
                set_republish_candidate(
                    chat_id, payload.get("plan"), payload.get("preview"),
                    payload.get("confirm_token"), title=payload.get("title") or "",
                    name=payload.get("name") or "", customer_id=command["customer_id"],
                    mode="mt", plan_data=payload["plan_data"], command_id=command_id)
                result["republish_confirmation_required"] = True
        results[command_id] = {"command_id": command_id,
                               "customer_id": command["customer_id"],
                               "chat_id": chat_id, "normalized_result": result,
                               "recorded_at": time.time()}
        _save_mt_result_state(state)
    _mt_prepare_notification(state, results[command_id])


def _mt_prepare_notification(state, record):
    # Idempotent replay heals a crash between result persistence and outbox creation.
    _mt_apply_result(record["chat_id"], record, record["normalized_result"])
    key = hashlib.sha256(json.dumps([record["customer_id"], record["command_id"],
                                    record["chat_id"], "publish_result"]).encode()).hexdigest()[:32]
    outbox = state.setdefault("notifications", {})
    if key not in outbox:
        outbox[key] = {"command_id": record["command_id"],
                       "customer_id": record["customer_id"], "chat_id": record["chat_id"],
                       "text": _mt_render_result(record["normalized_result"]),
                       "status": "pending", "attempts": 0}
        _save_mt_result_state(state)


def _mt_flush_notifications(state):
    for record in list(state.get("results", {}).values()):
        _mt_prepare_notification(state, record)
    sent = 0
    for key, notice in state.get("notifications", {}).items():
        if notice["status"] == "sent":
            continue
        try:
            customer = lookup_customer(notice["chat_id"]) or {}
            if customer.get("customer_id") != notice["customer_id"]:
                notice["status"] = "binding_changed"
                _save_mt_result_state(state)
                continue
            # Feishu's deduplication window is finite. After an ambiguous
            # attempt ages out, require manual reconciliation, not blind resend.
            if (notice.get("first_attempt_at")
                    and time.time() - notice["first_attempt_at"] >= 3600):
                notice["status"] = "needs_reconcile"
                _save_mt_result_state(state)
                continue
            notice["status"] = "sending"
            notice["attempts"] += 1
            notice.setdefault("first_attempt_at", time.time())
            _save_mt_result_state(state)
            response = reply(notice["chat_id"], notice["text"], message_id=key)
            if response is None or response.success() is not True:
                raise RuntimeError("Feishu acknowledgement missing")
            notice["status"] = "sent"
            notice["sent_at"] = time.time()
            state["seen"][notice["command_id"]] = time.time()
            sent += 1
        except Exception as exc:
            notice["status"] = "uncertain"
            notice["last_error"] = type(exc).__name__
        _save_mt_result_state(state)
    return sent


def _mt_tracked_commands(customer_id, chat_id):
    ids = set()
    try:
        names = os.listdir(MT_BATCH_DIR)
    except FileNotFoundError:
        return ids
    for name in names:
        if not name.endswith(".json") or name.startswith("latest_"):
            continue
        with open(os.path.join(MT_BATCH_DIR, name), encoding="utf-8") as fh:
            batch = json.load(fh)
        if batch.get("customer_id") == customer_id and batch.get("chat_id") == chat_id:
            ids.update(str(i["command_id"]) for i in batch.get("items") or []
                       if i.get("command_id"))
    return ids


def _mt_reply_result(chat_id, command):
    result = command.get("result")
    if not isinstance(result, dict):
        result = {}
    kind = str(command.get("kind") or "")
    if (kind == "publish" and result.get("stage") == "prepared"
            and (command.get("payload") or {}).get("op") != "confirm"):
        if result.get("ok") and result.get("items"):
            return _mt_accept_prepared(chat_id, command)
        detail = result.get("message") or result.get("stderr") or result.get("error")
        if result.get("error") == "inbox_empty":
            detail = "inbox \u91cc\u6ca1\u6709\u89c6\u9891\uff08\u652f\u6301 .mp4/.mov\uff09\u3002"
        elif result.get("problems"):
            lines = []
            for row in result.get("problems") or []:
                for problem in row.get("problems") or []:
                    lines.append(str(problem.get("message") or problem.get("code") or ""))
            detail = "\uff1b".join([value for value in lines if value][:4])
        reply(chat_id, "\u751f\u6210\u53d1\u5e03\u8ba1\u5212\u5931\u8d25\uff1a%s"
              % friendly_error(detail, "\u8bf7\u68c0\u67e5\u5ba2\u6237\u673a\u7684\u7d20\u6750\u548c\u6388\u6743\u72b6\u6001\u3002"))
        return
    if kind == "publish" and (result.get("stage") == "confirm"
                              or (command.get("payload") or {}).get("op") == "confirm"):
        with MT_RESULT_LOCK:
            state = _mt_result_state()
            _mt_store_result(state, chat_id, command)
            return _mt_flush_notifications(state)
    if result.get("ok"):
        reply(chat_id, "\u5ba2\u6237\u673a\u5df2\u5b8c\u6210\u6307\u4ee4\u3002")
    else:
        detail = _mt_result_error_text(command)
        reply(chat_id, "\u6307\u4ee4\u6ca1\u6709\u5b8c\u6210\uff1a%s"
              % _mt_display_error(detail))


def poll_mt_results():
    """Recover durable notifications independently of command discovery."""
    with MT_RESULT_LOCK:
        state = _mt_result_state()
        relayed = _mt_flush_notifications(state)
        status, listing = control_request("GET", "/api/customers", timeout=15)
        if status >= 400:
            return relayed
        for customer in listing.get("customers") or []:
            cid = str(customer.get("customer_id") or "")
            chat = str(customer.get("feishu_chat_id") or customer.get("chat_id") or "")
            if not cid or not chat:
                continue
            code, body = control_request(
                "GET", "/api/command/list?customer_id=" + urllib.parse.quote(cid), timeout=15)
            if code >= 400:
                continue
            commands = {str(c.get("command_id") or c.get("id") or ""): c
                        for c in body.get("commands") or []}
            # The current server ignores pagination parameters. Read tracked
            # commands by ID so the 50-row listing cannot starve a publication.
            for command_id in _mt_tracked_commands(cid, chat):
                if command_id in commands or command_id in state.get("results", {}):
                    continue
                code, body = control_request(
                    "GET", "/api/command/" + urllib.parse.quote(command_id, safe=""), timeout=15)
                if code < 400 and isinstance(body.get("command"), dict):
                    commands[command_id] = body["command"]
            for command_id, command in commands.items():
                if (not command_id or command.get("customer_id") != cid
                        or not _mt_terminal(command)):
                    continue
                if command_id in state.get("results", {}):
                    continue
                if command_id in state["seen"]:
                    continue
                if float(command.get("created") or 0) < float(state.get("since") or 0):
                    continue
                result = command.get("result") or {}
                if command.get("kind") == "publish" and (
                        result.get("stage") == "confirm"
                        or (command.get("payload") or {}).get("op") == "confirm"):
                    _mt_store_result(state, chat, command)
                else:
                    # Preparation remains the existing workflow; a rejected
                    # SDK reply now raises instead of being marked delivered.
                    _mt_reply_result(chat, command)
                    state["seen"][command_id] = time.time()
                    _save_mt_result_state(state)
                    relayed += 1
        return relayed + _mt_flush_notifications(state)


def start_mt_result_relay():
    def _loop():
        while True:
            try:
                poll_mt_results()
            except Exception as exc:
                print("[mt-relay] %s" % exc, flush=True)
            time.sleep(MT_RESULT_INTERVAL)
    threading.Thread(target=_loop, daemon=True, name="mt-result-relay").start()


def _mt_send_now(chat_id):
    with MT_BATCH_LOCK:
        return _mt_send_now_locked(chat_id)


def _mt_send_now_locked(chat_id):
    """把最新一批里还没发的条目改成 1 分钟后发（客户机只认计划时间之后）。"""
    import datetime as _dt
    batch = load_latest_mt_batch(chat_id)
    if not batch:
        reply(chat_id, "没有可发的任务。先发「发布」生成计划。")
        return
    items = [i for i in (batch.get("items") or [])
             if isinstance(i, dict) and i.get("status") in ("scheduled", "pending", "failed")]
    if not items:
        reply(chat_id, "这一批没有待发的条目（可能已发完）。要重发请发「发布」。")
        return
    item = items[0]
    sched = (_dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))
             + _dt.timedelta(seconds=90)).replace(microsecond=0).isoformat()
    try:
        _mt_reschedule_item(chat_id, item, sched)
        customer = lookup_customer(chat_id) or {}
        raw = remote_op(str(customer.get("customer_id") or ""), "read",
                        {"path": str(item.get("plan_path") or "")}, timeout=60,
                        device_id=item.get("device_id") or batch.get("device_id"))
        import base64 as _b64
        item["plan_data"] = json.loads(_b64.b64decode(raw.get("base64") or "").decode("utf-8-sig"))
        item["content_hash"] = (item["plan_data"].get("compliance") or {}).get("content_hash")
        item["schedule_at"] = item["plan_data"].get("schedule_at") or sched
    except Exception as exc:
        reply(chat_id, "立即发准备失败：%s" % friendly_error(str(exc), "请稍后重试。"))
        return
    item["status"] = "scheduled"
    batch["status"] = "scheduled"
    batch["confirmed_at"] = time.time()
    save_mt_batch(batch)
    reply(chat_id, "好，改成 %s 发（约 1 分钟后），到点自动下发。"
          % _fmt_slot(item.get("schedule_at")))



# ---------------------------------------------------------------- 改排期
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_index(value):
    value = str(value or "").strip()
    if value.isdigit():
        return int(value)
    if value in _CN_NUM:
        return _CN_NUM[value]
    if value.startswith("十") and len(value) == 2:
        return 10 + _CN_NUM.get(value[1], 0)
    return None


def _parse_cn_time(text, now=None):
    """Parse a Chinese wall-clock expression into an aware datetime (CST)."""
    import datetime as _dt
    if now is None:
        now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))
    s = str(text or "")
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", s)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = now.year + (1 if month < now.month else 0)
        try:
            base = now.replace(year=year, month=month, day=day,
                               hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            return None
    else:
        base = now.replace(second=0, microsecond=0)
        if "后天" in s:
            base += _dt.timedelta(days=2)
        elif "明天" in s:
            base += _dt.timedelta(days=1)
    m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})", s)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(\d{1,2})\s*[点时]\s*(半|\d{1,2})?", s)
        if not m:
            return None
        hour = int(m.group(1))
        tail = m.group(2)
        minute = 30 if tail == "半" else (int(tail) if tail and tail.isdigit() else 0)
    if any(word in s for word in ("下午", "晚上", "傍晚")) and hour < 12:
        hour += 12
    if "中午" in s and hour < 11:
        hour += 12
    if "凌晨" in s and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    target = base.replace(hour=hour, minute=minute)
    if target <= now + _dt.timedelta(minutes=2):
        target += _dt.timedelta(days=1)
    return target


def _mt_change_schedule(chat_id, text):
    with MT_BATCH_LOCK:
        return _mt_change_schedule_locked(chat_id, text)


def _mt_change_schedule_locked(chat_id, text):
    import datetime as dt
    cid = str((lookup_customer(chat_id) or {}).get('customer_id') or '')
    d = _dialog_load(chat_id, cid)
    batch = load_mt_batch(d.get('batch_id'))
    if (not batch or batch.get('customer_id') != cid or batch.get('chat_id') != chat_id
            or batch.get('cancel_requested') or batch.get('status') not in ('pending', 'scheduled')
            or not batch.get('topics_answered')):
        return reply(chat_id, '\u6ca1\u6709\u53ef\u5b89\u5168\u6539\u671f\u7684\u5f53\u524d\u8349\u7a3f\uff1b\u5df2\u53d6\u6d88\u3001\u5728\u9014\u6216\u5f85\u6838\u5b9e\u6279\u6b21\u4e0d\u80fd\u7531\u6539\u671f\u6062\u590d\u3002')
    target = _parse_cn_time(text)
    if not target:
        return reply(chat_id, '\u8bf7\u8bf4\u660e\u5177\u4f53\u65f6\u95f4\uff0c\u4f8b\u5982\u300c\u6539\u5230\u660e\u592911\u70b9\u300d\u3002')
    if re.search(r'\u7b2c.*\u6761', text):
        return reply(chat_id, '\u5f53\u524d\u4ec5\u652f\u6301\u6574\u6279\u6539\u6392\u671f\uff1b\u672a\u4fee\u6539\u4efb\u4f55\u6761\u76ee\u3002')
    # Persist unapproved version before remote writes. Partial failure cannot dispatch.
    batch['status'] = 'editing'
    batch['confirmation_digest'] = None
    batch['draft_revision'] += 1
    for item in batch['items']:
        item['status'] = 'pending'
        _mt_clear_credentials(item)
    save_mt_batch(batch)
    d.update(revision=batch['draft_revision'], status='editing')
    _dialog_save(d)
    try:
        for order, item in enumerate(batch['items']):
            if chat_id in MT_CANCEL_SIGNALS:
                raise MTBindingError('cancel during reschedule')
            sched = (target + dt.timedelta(minutes=5 * order)).replace(microsecond=0).isoformat()
            _mt_reschedule_item(chat_id, item, sched)
        batch['status'] = 'pending'
        batch['expires_at'] = time.time() + PENDING_TTL_SECONDS
        batch['confirmation_digest'] = _mt_contract_digest(batch)
        save_mt_batch(batch)
        d['status'] = 'awaiting_confirmation'
        _dialog_save(d)
        send_mt_confirm_card(chat_id, batch)
        return reply(chat_id, '\u65b0\u6392\u671f\u5df2\u751f\u6210\uff0c\u8bf7\u91cd\u65b0\u786e\u8ba4\uff1b\u65e7\u786e\u8ba4\u5361\u5df2\u5931\u6548\u3002')
    except Exception:
        batch['status'] = 'needs_reconcile'
        batch['reason'] = 'reschedule incomplete; no automatic dispatch'
        save_mt_batch(batch)
        return reply(chat_id, '\u6539\u671f\u672a\u5b8c\u6210\uff0c\u5df2\u505c\u6b62\u672c\u6279\u8c03\u5ea6\uff0c\u9700\u8981\u6838\u5bf9\u540e\u91cd\u65b0\u51c6\u5907\u3002')


MAT_DIR = os.environ.get("MAT_DIR") or "C:\\Users\\<USER>\\Desktop\\发布素材"
CONTROL_DB = os.environ.get("CONTROL_DB") or os.path.join(
    os.path.dirname(_HERE), "control.db")


def _cp_remote(customer_id, op, args, timeout=25, device_id=None):
    """Run one remote op on the customer machine through the control plane."""
    import sqlite3, time, urllib.request
    if not CONTROL_URL:
        return {"ok": False, "error": "no control plane"}
    url = CONTROL_URL.rstrip("/") + "/api/remote/exec"
    request_body = {"customer_id": customer_id, "op": op, "args": args}
    if device_id:
        request_body["device_id"] = str(device_id)
    body = json.dumps(request_body).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if CONTROL_TOKEN:
        req.add_header("Authorization", "Bearer " + CONTROL_TOKEN)
    end = time.monotonic() + min(30, timeout)
    try:
        with urllib.request.urlopen(req, timeout=min(10, timeout)) as resp:
            op_id = json.loads(resp.read().decode()).get("id")
    except Exception as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    while time.monotonic() < end:
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % CONTROL_DB, uri=True, timeout=min(1, max(0.01, end - time.monotonic())))
            row = conn.execute("select status,result from remote_ops where id=?", (op_id,)).fetchone()
            conn.close()
        except Exception:
            row = None
        if row and row[0] in ("done", "failed"):
            try:
                data = json.loads(row[1] or "{}")
            except Exception:
                data = {"ok": False, "error": str(row[1])[:200]}
            data.setdefault("ok", row[0] == "done")
            return data
        time.sleep(1)
    return {"ok": False, "error": "timeout", "op_id": op_id}


def _mt_active_batches(chat_id):
    import glob
    cid = str((lookup_customer(chat_id) or {}).get("customer_id") or "")
    items = []
    for path in glob.glob(os.path.join(MT_BATCH_DIR, "*.json")):
        if os.path.basename(path).startswith("latest_"):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                state = json.load(fh)
        except Exception:
            continue
        if str(state.get("chat_id") or "") != str(chat_id):
            continue
        if str(state.get("customer_id") or "") != cid:
            continue
        if str(state.get("status") or "") not in ("pending", "scheduled", "running", "active", "cancelling"):
            continue
        items.append(state)
    items.sort(key=lambda st: float(st.get("created_at") or 0))
    return items


def _mt_reply_plan_list(chat_id):
    batches = _mt_active_batches(chat_id)
    if not batches:
        reply(chat_id, "现在没有待发布的计划。发「发布」我先扫素材。")
        return
    lines = []
    total = 0
    for state in batches:
        status = str(state.get("status") or "")
        head = "待确认" if status == "pending" else ("已确认待发" if status == "scheduled" else "发布中")
        lines.append("【%s】批次 %s · 设备 %s" % (
            head, str(state.get("batch_id") or "")[-6:],
            state.get("device_label") or state.get("device_id") or "未标注"))
        for item in state.get("items") or []:
            total += 1
            when = ("待你点确认" if status == "pending"
                    else _fmt_slot(item.get("schedule_at")))
            lines.append("  %d. %s  %s" % (int(item.get("index") or 0),
                                          item.get("name") or "", when))
    lines.insert(0, "共 %d 条待发布：" % total)
    reply(chat_id, "\n".join(lines))


def _device_display(device):
    device = device or {}
    label = (device.get("label") or device.get("machine_code")
             or device.get("device_id") or "未命名设备")
    if device.get("online"):
        state = "在线"
    elif device.get("legacy"):
        state = "兼容模式"
    else:
        state = "离线"
    return "%s（%s，%s）" % (label, device.get("device_id") or "-", state)


def _mt_material_snapshot(chat_id, preferred_device=None):
    """Return (device, rows, error) for this session's customer only.

    The customer is always resolved from chat_id.  Once device binding is
    enabled, the device is selected from that customer's own active devices;
    the caller can prefer one but cannot cross into another customer.
    Customers that have not been migrated still use the untargeted legacy
    channel, because their old agent cannot prove a device identity.
    """
    customer = lookup_customer(chat_id) or {}
    cid = str(customer.get("customer_id") or "")
    if not cid:
        return None, None, "这个飞书会话还没绑定客户。"
    if not customer.get("device_binding_enabled"):
        return _mt_legacy_material_snapshot(
            cid, has_default=bool(customer.get("active_device_id")))
    try:
        device = select_customer_device(cid, preferred=preferred_device)
    except Exception as exc:
        if not preferred_device:
            return None, None, "当前客户没有在线可用设备：%s" % str(exc)[:120]
        # A new list may move to another online machine.  A prepared batch
        # never does this: dispatch always keeps its stored device_id.
        try:
            device = select_customer_device(cid, preferred=None)
        except Exception as fallback_exc:
            return None, None, "当前客户没有在线可用设备：%s" % str(
                fallback_exc)[:120]
    did = str(device.get("device_id") or "")
    data = _cp_remote(
        cid, "materials",
        {"schema": "publish-materials/1", "customer_id": cid,
         "device_id": did}, timeout=30, device_id=did)
    if not data.get("ok"):
        return device, None, "素材清单暂不可用：%s" % str(
            data.get("error") or data.get("message") or data)[:160]
    if data.get("schema") != "publish-materials/1":
        return device, None, "素材清单暂不可用：客户端不支持该协议。"
    if data.get("customer_id") != cid or data.get("device_id") != did:
        return device, None, "素材清单的来源设备或客户不一致，已拒绝。"
    try:
        device['accounts'] = [a for a in (data.get('accounts') or [])
                              if isinstance(a, dict)]
    except Exception:
        device['accounts'] = []
    return device, _mt_clean_material_rows(data, did), None


def _mt_legacy_material_snapshot(cid, has_default=False):
    """Untargeted snapshot for a customer not yet device-bound.

    Compatibility is intentionally narrow.  With zero registered devices the
    old untargeted path is accepted and labelled "旧机".  With exactly one
    registered device the agent must prove which machine answered by returning
    that device_id.  Two or more registered devices require binding to be
    enabled first; an untargeted response cannot be attributed safely.

    If the control plane has not run the additive migration yet, legacy
    customers keep the old untargeted behaviour.  Device features remain
    fail-closed on the server in that state.
    """
    device = {"device_id": None, "label": "旧机（未登记设备）",
              "online": None, "legacy": True}
    data = _cp_remote(
        cid, "materials",
        {"schema": "publish-materials/1", "customer_id": cid},
        timeout=30, device_id=None)
    if not data.get("ok"):
        return device, None, "素材清单暂不可用：%s" % str(
            data.get("error") or data.get("message") or data)[:160]
    if data.get("schema") != "publish-materials/1":
        return device, None, "素材清单暂不可用：客户端不支持该协议。"
    if data.get("customer_id") != cid:
        return device, None, "素材清单的客户不一致，已拒绝。"
    devices = []
    schema_missing = False
    if has_default:
        try:
            devices = list_customer_devices(cid)
        except Exception as exc:
            if "migration required" in str(exc).lower():
                schema_missing = True
            else:
                return device, None, "无法核对设备清单：%s" % str(exc)[:120]
    if schema_missing:
        migrated = dict(device, label="旧机（未迁移多设备模型）")
        return migrated, _mt_clean_material_rows(data, None), None
    active = [d for d in devices if str(d.get("status") or "") == "active"]
    reported = str(data.get("device_id") or "")
    if len(active) > 1:
        return device, None, "该客户已登记多台设备，请先启用设备绑定后再查素材。"
    if not active:
        if reported:
            return device, None, "素材清单来自未登记设备，已拒绝；请先完成设备登记。"
        return device, _mt_clean_material_rows(data, None), None
    only = dict(active[0])
    only_id = str(only.get("device_id") or "")
    if not reported:
        return device, None, "素材清单未带设备身份，但该客户已登记设备；请先升级客户机 agent。"
    if reported != only_id:
        return device, None, "素材清单的设备不属于该客户，已拒绝。"
    only.update(legacy=True)
    return only, _mt_clean_material_rows(data, only_id), None

def _mt_clean_material_rows(data, device_id):
    rows = []
    for row in data.get("materials") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        if (not name.lower().endswith((".mp4", ".mov"))
                or ntpath.basename(name) != name
                or row.get("is_symlink")):
            continue
        rows.append({
            "name": name,
            "path": row.get("path"),
            "size": row.get("size"),
            "mtime": row.get("mtime"),
            "sha256": str(row.get("sha256") or "").lower() or None,
            "material_id": row.get("material_id"),
            "device_id": device_id,
        })
    return rows

def _mt_material_names(chat_id):
    device, rows, err = _mt_material_snapshot(chat_id)
    if err:
        return None, err
    return [row["name"] for row in rows or []], None


def _mt_reply_material_list(chat_id, count_only=False):
    device, rows, err = _mt_material_snapshot(chat_id)
    if err:
        reply(chat_id, err)
        return
    names = [row["name"] for row in rows or []]
    if not names:
        reply(chat_id, "%s 的素材文件夹里现在没有可发的视频。" % _device_display(device))
        return
    if count_only:
        reply(chat_id, "%s 共有 %d 条视频。" % (_device_display(device), len(names)))
        return
    lines = ["设备：%s" % _device_display(device),
             "素材文件夹里共 %d 条视频：" % len(names)]
    for i, name in enumerate(names, 1):
        lines.append("%d. %s" % (i, name))
    reply(chat_id, "\n".join(lines))



# Versioned local dialogue state. No tunnel, ONCE state or customer ledger reads.
MT_DIALOG_LOCK = threading.RLock()
MT_SESSION_TTL = 1800
MT_TITLE = {}  # compatibility only; never authority for MT publishing
MT_CANCEL_WORDS = {"\u505c", "\u6682\u505c", "\u6682\u505c\u53d1\u5e03", "\u505c\u6b62", "\u53d6\u6d88", "\u53d6\u6d88\u53d1\u5e03", "\u53d6\u6d88\u5269\u4f59",
                   "\u53d6\u6d88\u5269\u4f59\u53d1\u5e03", "\u4e0d\u53d1\u4e86", "\u7b97\u4e86"}


def _dialog_path(chat_id, customer_id):
    key = hashlib.sha256(json.dumps([str(chat_id), str(customer_id)]).encode()).hexdigest()
    return os.path.join(MT_BATCH_DIR, 'dialog', key + '.json')


def _dialog_write(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(value, fh, ensure_ascii=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        fd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _dialog_load(chat_id, customer_id):
    with MT_DIALOG_LOCK:
        try:
            with open(_dialog_path(chat_id, customer_id), encoding='utf-8') as fh:
                d = json.load(fh)
        except FileNotFoundError:
            d = {}
        if d and (d.get('chat_id') != str(chat_id) or d.get('customer_id') != str(customer_id)):
            raise ValueError('dialog binding mismatch')
        if float(d.get('expires_at', 0)) <= time.time():
            d = {'schema': 'bot-dialog/1', 'chat_id': str(chat_id),
                 'customer_id': str(customer_id), 'revision': 0, 'history': [],
                 'draft_title': DEFAULT_PUBLISH_TITLE, 'confirmed_title': None,
                 'max_length': 30, 'status': 'draft'}
        return d


def _dialog_save(d):
    with MT_DIALOG_LOCK:
        try:
            with open(_dialog_path(d['chat_id'], d['customer_id']), encoding='utf-8') as fh:
                old = json.load(fh)
        except FileNotFoundError:
            old = {}
        if old.get('expires_at', 0) > time.time() and old.get('revision', 0) > d['revision']:
            raise ValueError('stale dialogue revision')
        d['expires_at'] = time.time() + MT_SESSION_TTL
        _dialog_write(_dialog_path(d['chat_id'], d['customer_id']), d)


def _dialog_reply(d, text):
    # Merge history into the current revision; a late reply cannot revive a cancelled draft.
    with MT_DIALOG_LOCK:
        current = _dialog_load(d['chat_id'], d['customer_id'])
        current.setdefault('history', []).append(['assistant', str(text)[:400]])
        current['history'] = current['history'][-12:]
        _dialog_save(current)
    return reply(d['chat_id'], text)


def _mt_cancel_state(state):
    state['cancel_requested'] = True
    state.setdefault('cancelled_at', time.time())
    for item in state.get('items') or []:
        if item.get('status') in ('pending', 'scheduled', 'planned', 'paused') and not item.get('command_id'):
            item['status'] = 'skipped'
            item['error'] = 'cancelled by customer'
            _mt_clear_credentials(item)
    statuses = {i.get('status') for i in state.get('items') or []}
    state['status'] = ('needs_reconcile' if 'needs_reconcile' in statuses else
                       'cancelling' if statuses & {'dispatching', 'dispatched', 'running'} else 'cancelled')


def _mt_cancel_intent_path(chat_id, cid):
    return _dialog_path(chat_id, cid).replace('.json', '.cancel.json')


def _mt_cancel_intent(chat_id, cid):
    try:
        with open(_mt_cancel_intent_path(chat_id, cid), encoding='utf-8') as fh:
            value = json.load(fh)
    except FileNotFoundError:
        return {}
    if value.get('chat_id') != str(chat_id) or value.get('customer_id') != str(cid):
        raise ValueError('cancel intent binding mismatch')
    return value


def _mt_is_cancelled(state):
    if state.get('cancel_requested'):
        return True
    intent = _mt_cancel_intent(state.get('chat_id'), state.get('customer_id'))
    if state.get('batch_id') in intent.get('batches', {}):
        return True
    return (state.get('status') not in ('completed', 'expired', 'cancelled') and
            float(intent.get('before', 0)) > 0 and
            float(state.get('created_at', 0)) <= float(intent['before']))


def cancel_mt_batch(chat_id, batch_id=None):
    customer = lookup_customer(chat_id) or {}
    cid = str(customer.get('customer_id') or '')
    if not cid:
        return reply(chat_id, '\u4f1a\u8bdd\u672a\u7ed1\u5b9a\u5ba2\u6237\uff0c\u65e0\u6cd5\u53d6\u6d88\u3002')
    # Card cancellation is batch-scoped and must not stop a newer batch.
    if batch_id:
        target = load_mt_batch(batch_id)
        if (not target or target.get('chat_id') != chat_id or target.get('customer_id') != cid
                or target.get('status') in ('completed', 'cancelled', 'expired')):
            return reply(chat_id, '\u8fd9\u5f20\u53d6\u6d88\u5361\u5df2\u5931\u6548\u6216\u6279\u6b21\u5df2\u7ed3\u675f\uff0c\u672a\u4fee\u6539\u5f53\u524d\u6279\u6b21\u3002')
    # Independent durable intent BEFORE waiting for a batch lock held by remote I/O.
    with MT_DIALOG_LOCK:
        intent = _mt_cancel_intent(chat_id, cid)
        intent.update(chat_id=str(chat_id), customer_id=cid)
        if batch_id:
            intent.setdefault('batches', {})[batch_id] = time.time()
        else:
            intent['before'] = time.time()
            MT_CANCEL_SIGNALS.add(chat_id)
            MT_CANCEL_CUTOFF[chat_id] = time.monotonic()
        _dialog_write(_mt_cancel_intent_path(chat_id, cid), intent)
        d = _dialog_load(chat_id, cid)
        if not batch_id or d.get('batch_id') == batch_id:
            d.update(status='cancelled', revision=d['revision'] + 1, confirmed_title=None)
            _dialog_save(d)
    with MT_BATCH_LOCK:
        candidates = [load_mt_batch(batch_id)] if batch_id else _mt_chat_batches(chat_id, cid)
        count = inflight = published = 0
        for state in candidates:
            if not state or state.get('chat_id') != chat_id or state.get('customer_id') != cid:
                continue
            if state.get('status') in ('completed', 'cancelled', 'expired'):
                continue
            _mt_cancel_state(state)
            save_mt_batch(state)
            count += sum(i.get('status') == 'skipped' for i in state.get('items', []))
            inflight += sum(i.get('status') in ('dispatching', 'dispatched', 'running', 'needs_reconcile') for i in state.get('items', []))
            published += sum(i.get('status') == 'published' for i in state.get('items', []))
    return reply(chat_id, '\u5df2\u8bb0\u5f55\u53d6\u6d88\uff1a\u5df2\u53d1 %d \u6761\uff0c\u53d6\u6d88 %d \u6761\uff1b\u5728\u9014\u6216\u5f85\u6838\u5b9e %d \u6761\uff0c\u53ea\u7b49\u5f85\u7ed3\u679c\uff0c\u4e0d\u4e2d\u65ad\u53d1\u5e03\u8fdb\u7a0b\u3002' % (published, count, inflight))


def _mt_chat_batches(chat_id, cid):
    try:
        names = os.listdir(MT_BATCH_DIR)
    except FileNotFoundError:
        return []
    out = []
    for name in names:
        if name.endswith('.json') and not name.startswith('latest_'):
            state = load_mt_batch(name[:-5])
            if state and state.get('chat_id') == str(chat_id) and state.get('customer_id') == str(cid):
                out.append(state)
    return out


def _dialog_mutate(d):
    # Invalidate all old preparation responses/cards before changing draft fields.
    with MT_BATCH_LOCK, MT_DIALOG_LOCK:
        current = _dialog_load(d['chat_id'], d['customer_id'])
        if current['revision'] != d['revision'] or current['status'] != d['status']:
            raise ValueError('draft changed during request; retry explicitly')
        old = load_mt_batch(d.get('batch_id'))
        if old and old.get('status') == 'pending':
            _mt_cancel_state(old)
            save_mt_batch(old)
        d['revision'] += 1
        d['confirmed_title'] = None
        d.pop('batch_id', None)
        d.pop('prepare_command_id', None)
        d['status'] = 'draft'
        _dialog_save(d)


def _mt_take_title(chat_id, text):
    match = re.fullmatch(r'(?:\u6807\u9898\s*[:\uff1a]\s*|\u628a\u6807\u9898\u6539\u4e3a\s*)(.+)', str(text or '').strip())
    return match.group(1).strip() if match else None


def _build_mt_ctx(chat_id, customer_id=None):
    cid = customer_id or (lookup_customer(chat_id) or {}).get('customer_id')
    d = _dialog_load(chat_id, cid)
    batches = _mt_chat_batches(chat_id, cid)
    latest = max(batches, key=lambda b: b.get('created_at', 0), default={})
    return json.dumps({'customer_id': cid, 'chat_id': chat_id, 'now': time.time(),
        'history': d.get('history', [])[-12:], 'draft_title': d['draft_title'],
        'confirmed_title': d['confirmed_title'], 'max_length': d['max_length'],
        'snapshot': d.get('snapshot'), 'status': d['status'],
        'last_error': latest.get('last_error') or latest.get('reason'),
        'batch_status': latest.get('status')}, ensure_ascii=False)


def _mt_model(text, ctx, *, title_limit=None):
    # A single request, no retries or actions inferred from unconstrained prose.
    if not LLM_KEY:
        return {'error': 'model_unavailable'}
    import urllib.request
    system = ('\u53ea\u8f93\u51faJSON\u3002action\u4ec5\u5141\u8bb8chat/list_files/count_files/progress\u3002'
              '\u4e0d\u80fd\u58f0\u660e\u5df2\u53d1\u5e03\u3001\u5df2\u505c\u6b62\u3001\u5df2\u66f4\u6539\uff1b\u4e0d\u6267\u884c\u5176\u4ed6\u52a8\u4f5c\u3002')
    if title_limit:
        system = ('\u53ea\u8f93\u51faJSON\u5b57\u6bb5candidate\u3002\u4ec5\u7f29\u5199\u63d0\u4f9b\u7684\u6807\u9898\uff0c\u4e0d\u589e\u52a0\u60c5\u8282\u3001\u4eba\u7269\u6216\u4e8b\u5b9e\u3002'
                  '\u6807\u9898\u6700\u591a%d\u4e2aUnicode\u5b57\u7b26\uff0c\u4fdd\u6301\u5b8c\u6574\u8bed\u53e5\u3002' % title_limit)
    body = json.dumps({'model': LLM_MODEL, 'messages': [
        {'role': 'system', 'content': system}, {'role': 'user', 'content': ctx + '\n' + text}],
        'response_format': {'type': 'json_object'}, 'max_tokens': 800, 'temperature': 0.1}).encode()
    req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions', data=body,
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + LLM_KEY})
    deadline = time.monotonic() + 30
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            chunks = []
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError()
                sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                if sock is not None:
                    sock.settimeout(left)
                chunk = response.read1(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if sum(map(len, chunks)) > 262144:
                    raise ValueError('oversized model response')
            raw = json.loads(b''.join(chunks))
        value = json.loads(raw['choices'][0]['message']['content'])
        if time.monotonic() > deadline or not isinstance(value, dict):
            raise ValueError('invalid model response')
        return value
    except Exception as exc:
        return {'error': type(exc).__name__}


def _mt_title_reply(d):
    title = d['draft_title']
    limit = d['max_length']
    if len(title) > limit:
        # One bounded rewrite. Invalid or still-long candidates never become publish titles.
        value = _mt_model(title, '\u4ec5\u7f29\u5199\u6b64\u6807\u9898', title_limit=limit)
        candidate = value.get('candidate')
        if isinstance(candidate, str) and candidate.strip() and len(candidate.strip()) <= limit:
            d['draft_title'] = candidate.strip()
            _dialog_save(d)
            return _dialog_reply(d, '\u5f85\u786e\u8ba4\u6807\u9898\uff1a%s\uff08%d \u5b57\uff0c\u4e0a\u9650 %d\uff09\u3002\u56de\u590d\u300c\u786e\u8ba4\u6807\u9898\u300d\u3002' % (d['draft_title'], len(d['draft_title']), limit))
        return _dialog_reply(d, '\u6807\u9898\u6709 %d \u5b57\uff0c\u8d85\u8fc7 %d \u5b57\uff1b\u672a\u622a\u65ad\u3001\u672a\u786e\u8ba4\u3002\u8bf7\u7528\u300c\u6807\u9898\uff1a\u2026\u300d\u63d0\u4f9b\u8f83\u77ed\u5b8c\u6574\u6807\u9898\u3002' % (len(title), limit))
    return _dialog_reply(d, '\u6807\u9898\u8349\u7a3f\uff1a%s\uff08%d \u5b57\uff0c\u4e0a\u9650 %d\uff09\uff0c\u56de\u590d\u300c\u786e\u8ba4\u6807\u9898\u300d\u3002' % (title, len(title), limit))


def _mt_snapshot(d, count_only=False):
    device, rows, err = _mt_material_snapshot(
        d['chat_id'], preferred_device=d.get('device_id'))
    if err:
        return _dialog_reply(d, err)
    names = [row['name'] for row in rows or []]
    if count_only:
        return _dialog_reply(d, '%s 共有 %d 条素材。' % (
            _device_display(device), len(names or [])))
    _dialog_mutate(d)
    MT_CANCEL_SIGNALS.discard(d['chat_id'])
    for key in ('selected', 'topics_answered', 'topics_by_name', 'item_topics', 'topics'):
        d.pop(key, None)
    names = list(names or [])
    if len(set(names)) != len(names):
        return _dialog_reply(d, '素材名称重复，无法安全生成选条清单。')
    d['draft_started_at'] = time.time()
    d['device_id'] = device.get('device_id')
    d['device'] = device
    d['accounts'] = list(device.get('accounts') or [])
    d['snapshot'] = {'snapshot_id': uuid.uuid4().hex,
                     'customer_id': d['customer_id'],
                     'device_id': device.get('device_id'),
                     'legacy': bool(device.get('legacy')),
                     'device_label': _device_display(device),
                     'expires_at': time.time() + MT_SESSION_TTL,
                     'materials': [dict(
                         row, material_id=(
                             row.get('material_id')
                             or hashlib.sha256((MAT_DIR + '/' + row['name']).encode()).hexdigest()))
                         for row in rows]}
    _dialog_save(d)
    return _dialog_reply(d, '设备：%s\n清单 %s（30 分钟内有效）：\n%s\n回复序号（如 1,2）或「全部」。' % (
        _device_display(device), d['snapshot']['snapshot_id'][:8],
        '\n'.join('%d. %s' % (i, n) for i, n in enumerate(names, 1))))


def _mt_pick(d, text):
    snap = d.get('snapshot') or {}
    rows = snap.get('materials') or []
    if snap.get('customer_id') != d['customer_id'] or snap.get('expires_at', 0) <= time.time():
        return _dialog_reply(d, '\u6e05\u5355\u5df2\u5931\u6548\uff0c\u8bf7\u91cd\u65b0\u53d1\u300c\u6e05\u5355\u300d\u3002')
    if snap.get('device_id') != d.get('device_id'):
        return _dialog_reply(d, '清单设备与当前会话不一致，请重新发「清单」。')
    if text in ('\u5168\u90e8', '\u5168\u53d1', '\u90fd\u53d1'):
        indexes = list(range(1, len(rows) + 1))
    else:
        cleaned = re.sub(r'^(?:\u53ea\u53d1|\u9009|\u53d1)\s*', '', text)
        cleaned = cleaned.replace('\u7b2c', '').replace('\u6761', '')
        parts = re.split(r'[,\uff0c\u3001\s]+', cleaned.strip())
        if not all(x.isdigit() for x in parts):
            return _dialog_reply(d, '\u8bf7\u7528\u9017\u53f7\u5206\u9694\u5e8f\u53f7\uff0c\u4f8b\u5982 8,9\uff1b89\u53ea\u8868\u793a\u7b2c89\u6761\u3002')
        indexes = [int(x) for x in parts]
    if not indexes or len(set(indexes)) != len(indexes) or any(i < 1 or i > len(rows) for i in indexes):
        return _dialog_reply(d, '\u5e8f\u53f7\u8d8a\u754c\u6216\u91cd\u590d\uff1b89\u53ea\u8868\u793a\u7b2c89\u6761\uff0c\u8bf7\u6309\u5f53\u524d\u6e05\u5355\u9009\u62e9\u3002')
    _dialog_mutate(d)
    selected = [dict(rows[i - 1], display_index=i,
                     snapshot_id=snap['snapshot_id']) for i in indexes]
    if {row.get('device_id') for row in selected} != {snap.get('device_id')}:
        return _dialog_reply(d, '所选素材跨越了设备，已拒绝；请按当前设备清单重选。')
    d['selected'] = selected
    d['status'] = 'awaiting_topics'
    d['topics_answered'] = False
    _dialog_save(d)
    return _dialog_reply(d, '\u5df2\u9009 %s\uff1b\u672c\u6279\u7528\u4ec0\u4e48\u8bdd\u9898\uff1f\u56de\u590d\u300c\u9ed8\u8ba4\u300d\u300c\u4e0d\u586b\u300d\u6216 #\u8bdd\u9898\uff0c\u6309\u6e05\u5355\u5e8f\u53f7\u8986\u76d6\u5982 2:#\u5355\u72ec\u8bdd\u9898\u3002' % ','.join(map(str, indexes)))


_CN_DIGITS = {'\u4e00': 1, '\u4e8c': 2, '\u4e24': 2, '\u4e09': 3, '\u56db': 4, '\u4e94': 5,
              '\u516d': 6, '\u4e03': 7, '\u516b': 8, '\u4e5d': 9, '\u5341': 10}


def _pick_account(text, accounts):
    """\u5ba2\u6237\u56de\u7684\u8bdd \u2192 \u9009\u4e2d\u7684\u8d26\u53f7\uff1b\u62ff\u4e0d\u51c6\u5c31\u8fd4\u56de None\uff08\u5b81\u53ef\u518d\u95ee\u4e00\u6b21\uff0c\u4e5f\u4e0d\u53d1\u9519\u53f7\uff09\u3002

    \u987a\u5e8f\uff1a\u6635\u79f0\u5b8c\u5168\u76f8\u540c \u2192 \u5e8f\u53f7\uff081 / \u7b2c2\u4e2a / \u4e8c\u53f7 / \u7b2c\u4e09\u4e2a\uff09\u2192 \u6635\u79f0\u5305\u542b\u5728\u56de\u590d\u91cc\u3002
    \u5305\u542b\u5339\u914d\u53ea\u5728\u300c\u6700\u957f\u7684\u90a3\u4e2a\u6635\u79f0\u552f\u4e00\u300d\u65f6\u6210\u7acb\uff1a\u4e24\u4e2a\u53f7\u53eb\u300c\u5c0f\u6f2b\u300d\u300c\u5c0f\u6f2b\u5267\u573a\u300d\uff0c
    \u5ba2\u6237\u56de\u300c\u5c0f\u6f2b\u5267\u573a\u300d\u5fc5\u987b\u9009\u540e\u8005\uff0c\u4e0d\u80fd\u56e0\u4e3a\u300c\u5c0f\u6f2b\u300d\u6392\u5728\u524d\u9762\u5c31\u9009\u9519\u3002
    """
    t = re.sub(r'\s+', '', str(text or ''))
    if not t:
        return None
    named = [(a, str(a.get('nickname') or '').strip()) for a in accounts]
    named = [(a, n) for a, n in named if n and n != '\u6296\u97f3']
    exact = [a for a, n in named if re.sub(r'\s+', '', n) == t]
    if len(exact) == 1:
        return exact[0]
    m = re.fullmatch(r'\u7b2c?(\d{1,2}|[\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341])'
                     r'(?:\u4e2a|\u53f7)?(?:\u53f7|\u8d26\u53f7)?', t)
    if m:
        raw = m.group(1)
        i = int(raw) if raw.isdigit() else _CN_DIGITS.get(raw, 0)
        return accounts[i - 1] if 1 <= i <= len(accounts) else None
    hits = [(a, n) for a, n in named if len(n) >= 2 and re.sub(r'\s+', '', n) in t]
    if not hits:
        return None
    longest = max(len(n) for _, n in hits)
    best = [a for a, n in hits if len(n) == longest]
    return best[0] if len(best) == 1 else None


def _selectable_accounts(d):
    """\u80fd\u53d1\u7684\u53f7\uff1a\u542f\u7528\u4e14\u771f\u7684\u767b\u5f55\u8fc7\u3002

    \u5ba2\u6237\u7aef materials \u65e9\u5c31\u6807\u4e86 logged_in\uff08\u7a7a\u73af\u5883\u4e0d\u7b97\u53f7\uff09\uff0c\u4ee5\u524d\u8fd9\u91cc\u6ca1\u770b\uff0c
    \u53ea\u767b\u4e86\u4e00\u4e2a\u53f7\u7684\u7535\u8111\u4e5f\u4f1a\u88ab\u53cd\u590d\u8ffd\u95ee\u300c\u53d1\u54ea\u4e2a\u300d\u3002\u65e7\u5ba2\u6237\u7aef\u6ca1\u6709\u8be5\u5b57\u6bb5\uff0c\u6309\u5df2\u767b\u5f55\u5904\u7406\u3002
    """
    return [a for a in (d.get('accounts') or [])
            if a.get('enabled', True) and a.get('logged_in', True)]


def _mt_prepare_draft(d):
    accounts = _selectable_accounts(d)
    if len(accounts) == 1 and not d.get('account_env') and len(d.get('accounts') or []) > 1:
        # \u58f0\u660e\u4e86\u591a\u4e2a\u73af\u5883\u4f46\u53ea\u6709\u4e00\u4e2a\u771f\u767b\u5f55\u8fc7\uff1a\u76f4\u63a5\u7528\u5b83\uff0c\u4e0d\u6253\u6270\u5ba2\u6237\u3002
        d['account_env'] = accounts[0].get('env_id')
        d['account_label'] = accounts[0].get('nickname')
    if len(accounts) > 1 and not d.get('account_env'):
        d['pending_accounts'] = accounts
        d['pre_account_status'] = d.get('status')
        d['status'] = 'awaiting_account'
        _dialog_save(d)
        lines = ['这台电脑登录了 %d 个抖音号，发哪个？' % len(accounts)]
        for i, a in enumerate(accounts, 1):
            lines.append('%d) %s' % (i, a.get('nickname') or a.get('account_id')))
        lines.append('回数字即可。')
        return _dialog_reply(d, '\n'.join(lines))
    if not d.get('topics_answered') or not d.get('selected'):
        return _dialog_reply(d, '\u8bf7\u5148\u9009\u7d20\u6750\u5e76\u56de\u7b54\u672c\u6279\u8bdd\u9898\u3002')
    if not d.get('confirmed_title') or len(d['confirmed_title']) > d['max_length']:
        d['status'] = 'awaiting_title'
        _dialog_save(d)
        return _mt_title_reply(d)
    snap = d.get('snapshot') or {}
    device_id = str(snap.get('device_id') or d.get('device_id') or '')
    legacy = bool(snap.get('legacy'))
    selected_names = {r['name'] for r in d['selected']}
    selected_hashes = {str(r.get('sha256') or '').lower()
                       for r in d['selected'] if r.get('sha256')}
    active_statuses = ('pending', 'scheduled', 'dispatching', 'dispatched', 'needs_reconcile')
    for batch in _mt_chat_batches(d['chat_id'], d['customer_id']):
        batch_device = str(batch.get('device_id') or '')
        for item in batch.get('items', []):
            if item.get('status') not in active_statuses:
                continue
            item_device = str(item.get('device_id') or batch_device or '')
            if item_device == device_id:
                # Same machine: filename is the same workspace identity.
                if item.get('name') in selected_names:
                    return _dialog_reply(d, '\u6240\u9009\u7d20\u6750\u5df2\u6709\u5f85\u53d1\u3001\u5728\u9014\u6216\u5f85\u6838\u5b9e\u8bb0\u5f55\uff0c\u672a\u91cd\u590d\u51c6\u5907\uff1b\u8bf7\u5148\u6838\u5bf9\u8fdb\u5ea6\u3002')
                continue
            # Different machine: only a matching content hash proves it is
            # the same material.  Equal filenames alone are not evidence.
            item_hash = _item_sha(item).lower()
            if item_hash and item_hash in selected_hashes:
                return _dialog_reply(d, '\u8be5\u7d20\u6750\u5df2\u5728\u540c\u5ba2\u6237\u53e6\u4e00\u53f0\u673a\u5668\u4e0a\u53d1\u9001\u6216\u5f85\u53d1\uff0c\u4e0d\u5141\u8bb8\u8de8\u673a\u91cd\u590d\u53d1\u5e03\u3002')
    if snap.get('expires_at', 0) <= time.time():
        return _dialog_reply(d, '\u6e05\u5355\u5df2\u5931\u6548\uff0c\u8bf7\u91cd\u65b0\u53d1\u300c\u6e05\u5355\u300d\u3002')
    if any(r.get('snapshot_id') != snap['snapshot_id'] or
           r.get('material_id') not in {m['material_id'] for m in snap['materials']}
           for r in d['selected']):
        return _dialog_reply(d, '\u6240\u9009\u7d20\u6750\u4e0d\u5c5e\u4e8e\u5f53\u524d\u6e05\u5355\uff0c\u8bf7\u91cd\u65b0\u9009\u6761\u3002')
    if not legacy and (not device_id or any(
            r.get('device_id') != device_id for r in d['selected'])):
        return _dialog_reply(d, '所选素材的设备归属不一致，已阻止准备。')
    # Remote prepare sorts by mtime, so overrides must be checked by name on return.
    batch_id = 'draft-' + uuid.uuid4().hex
    command_id = 'prepare-' + uuid.uuid4().hex
    d.update(batch_id=batch_id, prepare_command_id=command_id, status='preparing')
    d['device_id'] = device_id
    _dialog_save(d)  # persist before the side effect, never silently retry uncertain enqueue
    payload = {'op': 'create', 'batch_id': batch_id, 'title': d['confirmed_title'],
               'files': [r.get('path') or (MAT_DIR + '\\' + r['name'])
                         for r in d['selected']],
               'topics': d['topics'], 'item_topics': d.get('item_topics', {}),
               'draft_revision': d['revision'], 'snapshot_id': snap['snapshot_id'],
               'materials': [{k: r.get(k) for k in
                              ('material_id', 'sha256', 'name', 'path')}
                             for r in d['selected']],
               'material_sha256s': [r.get('sha256') for r in d['selected']
                                    if r.get('sha256')]}
    if d.get('account_env'):
        payload['env_id'] = d['account_env']
    if len(accounts) == 1:
        payload.setdefault('env_id', accounts[0].get('env_id'))
    if device_id:
        payload['device_id'] = device_id
        payload['target_device_id'] = device_id
    try:
        response = enqueue_for_chat(d['chat_id'], 'publish', payload,
                                    command_id=command_id,
                                    device_id=device_id or None)
        returned = (response.get('command') or {}).get('command_id') or (response.get('command') or {}).get('id')
        if returned and returned != command_id:
            raise ValueError('prepare identity mismatch')
    except Exception as exc:
        detail = str(exc)
        if ('MATERIAL_DEVICE_CONFLICT' in detail
                or 'material already belongs' in detail):
            return _dialog_reply(d, '该素材已在同客户另一台机器上发布或占用，不能跨机重复发布；本批未准备。')
        if 'NO_AVAILABLE_DEVICE' in detail or 'no online device' in detail:
            return _dialog_reply(d, '当前客户没有在线可用设备，本批未准备。')
        return _dialog_reply(d, '\u51c6\u5907\u6307\u4ee4\u7ed3\u679c\u672a\u786e\u8ba4\uff1b\u8349\u7a3f\u5df2\u4fdd\u7559\uff0c\u8bf7\u67e5\u72b6\u6001\uff0c\u4e0d\u8981\u91cd\u590d\u786e\u8ba4\u3002')
    return _dialog_reply(d, '\u51c6\u5907\u6307\u4ee4\u5df2\u6295\u9012\uff1b\u7ed3\u679c\u6838\u5bf9\u540e\u518d\u53d1\u9001\u6700\u7ec8\u786e\u8ba4\u5361\u3002')


def _mt_contract_digest(batch):
    value = {k: batch.get(k) for k in ('customer_id', 'device_id', 'chat_id', 'batch_id', 'draft_revision', 'snapshot_id', 'max_length')}
    value['items'] = [{k: i.get(k) for k in ('name', 'title', 'topics', 'content_hash', 'schedule_at', 'plan_id', 'device_id', 'binding_epoch')} for i in batch.get('items', [])]
    value['plans'] = [{k: (i.get('plan_data') or {}).get(k) for k in
                       ('plan_id', 'account_id', 'channel_id', 'env_id', 'platform', 'piece', 'schedule_at')}
                      for i in batch.get('items', [])]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _mt_accept_prepared(chat_id, command):
    cid = str(command.get('customer_id') or '')
    with MT_BATCH_LOCK:
        d = _dialog_load(chat_id, cid)
        result = command.get('result') or {}
        if (result.get('device_id') and d.get('device_id') and
                str(result.get('device_id')) != str(d.get('device_id'))):
            return _dialog_reply(d, '准备结果来自另一台设备，已阻止确认。')
        if ((lookup_customer(chat_id) or {}).get('customer_id') != cid or
            d.get('status') != 'preparing' or not d.get('topics_answered') or
            d.get('prepare_command_id') != str(command.get('command_id') or command.get('id') or '') or
            d.get('batch_id') != result.get('batch_id')):
            return reply(chat_id, '\u51c6\u5907\u7ed3\u679c\u5df2\u8fc7\u671f\u3001\u53d6\u6d88\u6216\u4e0d\u5c5e\u4e8e\u5f53\u524d\u8349\u7a3f\uff0c\u672a\u751f\u6210\u786e\u8ba4\u5361\u3002')
        expected = {r['name']: d.get('topics_by_name', {}).get(r['name'], d['topics']) for r in d['selected']}
        items = result.get('items') or []
        if len(items) != len(expected) or {i.get('name') for i in items} != set(expected):
            return _dialog_reply(d, '\u51c6\u5907\u7ed3\u679c\u4e0e\u9009\u4e2d\u7d20\u6750\u4e0d\u4e00\u81f4\uff0c\u5df2\u963b\u6b62\u786e\u8ba4\uff0c\u8bf7\u91cd\u65b0\u67e5\u6e05\u5355\u3002')
        selected_hashes = {r['name']: str(r.get('sha256') or '').lower()
                           for r in d['selected']}
        for item in items:
            item['desired_topics'] = expected[item['name']]
            if item.get('title') != d['confirmed_title']:
                return _dialog_reply(d, '\u51c6\u5907\u7ed3\u679c\u7684\u6807\u9898\u6216\u9010\u6761\u8bdd\u9898\u4e0d\u5339\u914d\uff0c\u5df2\u963b\u6b62\u786e\u8ba4\uff1b\u8bf7\u91cd\u65b0\u67e5\u6e05\u5355\u3002')
            expected_sha = selected_hashes.get(item['name']) or ''
            actual_sha = str(((item.get('plan_data') or {}).get('piece') or {}).get(
                'sha256') or '').lower()
            if expected_sha and actual_sha and expected_sha != actual_sha:
                return _dialog_reply(d, '素材在准备期间发生变化，无法确认是同一内容；本批未确认，请重新查清单。')
        batch = store_mt_prepared(
            chat_id, cid, result, device_id=d.get('device_id'))
        batch['device_label'] = d.get('snapshot', {}).get('device_label') or d.get('device_id')
        batch['legacy'] = bool(d.get('snapshot', {}).get('legacy'))
        if len(batch['items']) != len(expected):
            _mt_cancel_state(batch)
            save_mt_batch(batch)
            return _dialog_reply(d, '\u6240\u9009\u7d20\u6750\u5305\u542b\u91cd\u590d\u5185\u5bb9\uff0c\u5df2\u963b\u6b62\u786e\u8ba4\uff1b\u8bf7\u91cd\u65b0\u9009\u62e9\u3002')
        for item in batch['items']:
            actual = (item.get('plan_data') or {}).get('piece') or {}
            if actual.get('topics') != expected[item['name']] or actual.get('title') != d['confirmed_title']:
                _mt_cancel_state(batch)
                save_mt_batch(batch)
                return _dialog_reply(d, '\u91cd\u5efa\u9884\u89c8\u4e0e\u8349\u7a3f\u4e0d\u4e00\u81f4\uff0c\u5df2\u53d6\u6d88\u8be5\u6279\u3002')
            item['topics'] = actual['topics']
        batch['created_at'] = d.get('draft_started_at', batch['created_at'])
        batch.update(draft_revision=d['revision'], snapshot_id=d['snapshot']['snapshot_id'], topics_answered=True, max_length=d['max_length'])
        batch['confirmation_digest'] = _mt_contract_digest(batch)
        save_mt_batch(batch)
        if _mt_is_cancelled(batch):
            _mt_cancel_state(batch)
            save_mt_batch(batch)
            return reply(chat_id, '\u51c6\u5907\u671f\u95f4\u6536\u5230\u53d6\u6d88\uff0c\u672a\u751f\u6210\u786e\u8ba4\u5361\u3002')
        d['status'] = 'awaiting_confirmation'
        _dialog_save(d)
        if not send_mt_confirm_card(chat_id, batch):
            _dialog_reply(d, '\u786e\u8ba4\u5361\u672a\u9001\u8fbe\uff1b\u8bf7\u53d1\u300c\u786e\u8ba4\u5361\u300d\u91cd\u53d1\u5361\u7247\uff0c\u4e0d\u4f1a\u91cd\u5efa\u53d1\u5e03\u547d\u4ee4\u3002')


def handle_multitenant(chat_id, text):
    request_id = uuid.uuid4().hex[:12]
    started = time.monotonic()
    try:
        customer = lookup_customer(chat_id) or {}
        cid = str(customer.get('customer_id') or '')
        if not cid:
            return reply(chat_id, '\u8fd9\u4e2a\u4f1a\u8bdd\u8fd8\u6ca1\u7ed1\u5b9a\u5ba2\u6237\u3002')
        d = _dialog_load(chat_id, cid)
        text = str(text or '').strip()
        compact = re.sub(r'\s+', '', text)
        d.setdefault('history', []).append(['user', text[:400]])
        d['history'] = d['history'][-12:]
        _dialog_save(d)
        if compact in MT_CANCEL_WORDS:
            return cancel_mt_batch(chat_id)
        if (compact in ('重选账号', '换账号') and d.get('account_env')
                and d.get('status') != 'awaiting_account' and len(_selectable_accounts(d)) > 1):
            # 选完号之后也能改：清掉选择，重新问一次。
            d.pop('account_env', None)
            d.pop('account_label', None)
            _dialog_save(d)
            return _mt_prepare_draft(d)
        if d.get('status') == 'awaiting_account' and d.get('pending_accounts'):
            accs = d['pending_accounts']
            if compact in ('\u91cd\u9009\u8d26\u53f7', '\u6362\u8d26\u53f7'):
                d.pop('account_env', None)
                d['status'] = d.get('pre_account_status') or 'awaiting_topics'
                d.pop('pending_accounts', None)
                _dialog_save(d)
                return _mt_prepare_draft(d)
            pick = _pick_account(compact, accs)
            if pick:
                d['account_env'] = pick.get('env_id')
                d['account_label'] = pick.get('nickname')
                d.pop('pending_accounts', None)
                d['status'] = d.get('pre_account_status') or 'awaiting_topics'
                _dialog_save(d)
                return _mt_prepare_draft(d)
            names = ' / '.join('%d)%s' % (i + 1, a.get('nickname') or a.get('account_id'))
                               for i, a in enumerate(accs))
            return _dialog_reply(d, '\u8bf7\u56de\u6570\u5b57\u9009\u8d26\u53f7\uff1a' + names)
        if compact in ('\u53ef\u4ee5', '\u597d', '\u884c', '\u597d\u7684', '\u55ef'):
            return _dialog_reply(d, '\u8fd9\u53e5\u8bdd\u4e0d\u4f5c\u4e3a\u53d1\u5e03\u6388\u6743\uff1b\u8bf7\u5b8c\u6210\u8bdd\u9898\u548c\u6807\u9898\u786e\u8ba4\uff0c\u518d\u786e\u8ba4\u6700\u7ec8\u53d1\u5e03\u5361\u3002')
        if compact == '\u786e\u8ba4\u91cd\u53d1':
            return do_republish(chat_id)
        if compact in ('\u53d6\u6d88\u91cd\u53d1', '\u4e0d\u91cd\u53d1'):
            clear_republish_candidate(chat_id)
            return _dialog_reply(d, '\u5df2\u53d6\u6d88\u672c\u6b21\u91cd\u53d1\u5019\u9009\u3002')
        if compact in ('\u786e\u8ba4', '\u786e\u8ba4\u53d1\u5e03'):
            batches = [b for b in _mt_chat_batches(chat_id, cid) if b.get('status') == 'pending']
            if len(batches) != 1:
                return _dialog_reply(d, '\u6ca1\u6709\u552f\u4e00\u5f85\u786e\u8ba4\u8349\u7a3f\uff0c\u8bf7\u4f7f\u7528\u5f53\u524d\u786e\u8ba4\u5361\u3002')
            return confirm_mt_batch(chat_id, batches[0]['batch_id'], batches[0].get('confirmation_digest'))
        if compact == '\u786e\u8ba4\u5361':
            batch = load_mt_batch(d.get('batch_id'))
            if batch and batch.get('status') == 'pending':
                return send_mt_confirm_card(chat_id, batch)
            return _dialog_reply(d, '\u6ca1\u6709\u5f85\u786e\u8ba4\u5361\u3002')
        if compact == '\u786e\u8ba4\u6807\u9898':
            if d['status'] in ('preparing', 'awaiting_confirmation', 'cancelled'):
                return _dialog_reply(d, '\u5f53\u524d\u8349\u7a3f\u4e0d\u80fd\u518d\u6b21\u786e\u8ba4\u6807\u9898\uff0c\u8bf7\u5148\u4fee\u6539\u6807\u9898\u6216\u91cd\u65b0\u9009\u7d20\u6750\u3002')
            if not d['draft_title'] or len(d['draft_title']) > d['max_length']:
                return _mt_title_reply(d)
            d['confirmed_title'] = d['draft_title']
            _dialog_save(d)
            if d.get('topics_answered'):
                return _mt_prepare_draft(d)
            return _dialog_reply(d, '\u8349\u7a3f\u6807\u9898\u5df2\u786e\u8ba4\uff1b\u8bf7\u53d1\u300c\u53d1\u5e03\u300d\u9009\u62e9\u7d20\u6750\u548c\u8bdd\u9898\u3002')
        topic_edit = re.fullmatch(r'(?:\u8bdd\u9898\s*[:\uff1a]\s*|\u628a\u8bdd\u9898\u6539\u4e3a\s*)(.+)', text)
        if topic_edit:
            if not d.get('selected'):
                return _dialog_reply(d, '\u8bf7\u5148\u67e5\u6e05\u5355\u5e76\u9009\u62e9\u7d20\u6750\u3002')
            _dialog_mutate(d)
            d['status'] = 'awaiting_topics'
            d['topics_answered'] = False
            _dialog_save(d)
            text = topic_edit.group(1).strip()
            compact = re.sub(r'\s+', '', text)
        title = _mt_take_title(chat_id, text)
        limit = requested_title_limit(text)
        if title is not None or limit:
            _dialog_mutate(d)
            if title is not None:
                d['draft_title'] = title
            if limit:
                d['max_length'] = min(30, max(1, int(limit)))
            _dialog_save(d)
            return _mt_title_reply(d)
        if '\u6807\u9898' in text:
            return _dialog_reply(d, '\u5f53\u524d\u6807\u9898\u8349\u7a3f\uff1a%s\uff08%d/%d\u5b57\uff0c%s\uff09\u3002\u4fee\u6539\u8bf7\u7528\u300c\u6807\u9898\uff1a\u2026\u300d\u6216\u300c\u628a\u6807\u9898\u6539\u4e3a\u2026\u300d\u3002' % (
                d['draft_title'], len(d['draft_title']), d['max_length'], '\u5df2\u786e\u8ba4' if d['confirmed_title'] else '\u5f85\u786e\u8ba4'))
        if re.search(r'(\u6539\u6392\u671f|\u6392\u671f\u6539|\u6539\u671f|\u6539\u65f6\u95f4|\u6539\u5230|\u6362\u4e2a\u65f6\u95f4|\u5f80\u540e\u63a8|\u63d0\u524d\u5230)', text):
            return _mt_change_schedule(chat_id, text)
        if compact in ('\u53d1\u5e03', '\u53d1\u89c6\u9891', '\u53d1\u5e03\u89c6\u9891', '\u6211\u8981\u53d1\u89c6\u9891', '\u4e0a\u65b0', '\u51fa\u7247', '\u6e05\u5355', '\u5217\u6e05\u5355', '\u7d20\u6750', '\u89c6\u9891'):
            return _mt_snapshot(d)
        if compact in ('\u603b\u5171\u6709\u51e0\u6761', '\u51e0\u6761', '\u591a\u5c11\u6761', '\u7d20\u6750\u6709\u51e0\u6761', '\u89c6\u9891\u6709\u51e0\u6761'):
            return _mt_snapshot(d, count_only=True)
        if compact in ('\u6392\u671f', '\u53d1\u5e03\u8ba1\u5212', '\u6392\u671f\u8868', '\u770b\u8ba1\u5212', '\u4efb\u52a1\u5217\u8868', '\u4ec0\u4e48\u65f6\u5019\u53d1'):
            return _mt_reply_plan_list(chat_id)
        if compact in ('\u8fdb\u5ea6', '\u72b6\u6001', '\u597d\u4e86\u6ca1', '\u53d1\u4e86\u51e0\u6761', '\u53d1\u5b8c\u6ca1', '\u8fd8\u6709\u51e0\u6761'):
            return _mt_reply_progress(chat_id)
        if compact == '\u4ec0\u4e48\u5b89\u5168\u539f\u56e0':
            batches = _mt_chat_batches(chat_id, cid)
            last = max(batches, key=lambda b: b.get('created_at', 0), default={})
            reason = last.get('last_error') or last.get('reason')
            return _dialog_reply(d, ('\u5df2\u8bb0\u5f55\u539f\u56e0\uff1a' + _mt_display_error(reason)) if reason else '\u5f53\u524d\u8bb0\u5f55\u6ca1\u6709\u5177\u4f53\u5b89\u5168\u539f\u56e0\uff0c\u65e0\u6cd5\u636e\u6b64\u5224\u65ad\uff1b\u8bf7\u63d0\u4f9b\u5bf9\u5e94\u62a5\u9519\u539f\u6587\u3002')
        if re.fullmatch(r'(?:\u53ea\u53d1|\u9009|\u53d1)?[\u7b2c\u6761\d,\uff0c\u3001\s]+', text) or compact in ('\u5168\u90e8', '\u5168\u53d1', '\u90fd\u53d1'):
            return _mt_pick(d, text)
        if d['status'] == 'awaiting_topics':
            if not ('#' in text or compact in ('\u9ed8\u8ba4', '\u4e0d\u586b') or re.search(r'\d+[:\uff1a]', text)):
                return _dialog_reply(d, '\u8bf7\u660e\u786e\u56de\u590d #\u8bdd\u9898\u3001\u300c\u9ed8\u8ba4\u300d\u300c\u4e0d\u586b\u300d\u6216\u5e8f\u53f7\u8986\u76d6\uff1b\u672a\u91c7\u7528\u5176\u4ed6\u5bf9\u8bdd\u4f5c\u4e3a\u8bdd\u9898\u3002')
            parsed = parse_batch_topic_reply(text, len(d['snapshot']['materials']), d.get('default_topics', []))
            if not parsed:
                return _dialog_reply(d, '\u8bdd\u9898\u672a\u8bc6\u522b\uff1b\u8bf7\u56de\u590d #\u8bdd\u9898\u3001\u300c\u9ed8\u8ba4\u300d\u6216\u300c\u4e0d\u586b\u300d\u3002')
            topics, overrides, remember = parsed
            selected = d['selected']
            if any(i not in {r['display_index'] for r in selected} for i in overrides):
                return _dialog_reply(d, '\u8986\u76d6\u5e8f\u53f7\u4e0d\u5728\u672c\u6b21\u6240\u9009\u7d20\u6750\u5185\u3002')
            d['topics'] = topics or []
            d['topics_by_name'] = {r['name']: overrides.get(r['display_index'], d['topics']) for r in selected}
            d['item_topics'] = {str(i): d['topics_by_name'][r['name']] for i, r in enumerate(selected, 1)}
            if remember:
                d['default_topics'] = d['topics']
            d['topics_answered'] = True
            _dialog_save(d)
            return _mt_prepare_draft(d)
        if compact in ('\u7acb\u5373\u53d1', '\u9a6c\u4e0a\u53d1', '\u73b0\u5728\u5c31\u53d1', '\u7acb\u523b\u53d1'):
            return _dialog_reply(d, '\u5f53\u524d\u6a21\u5f0f\u6309\u5df2\u786e\u8ba4\u6392\u671f\u6267\u884c\uff0c\u4e0d\u652f\u6301\u7ed5\u8fc7\u6700\u7ec8\u786e\u8ba4\u6216\u65f6\u95f4\u7a97\u53e3\u7acb\u5373\u53d1\u5e03\u3002')
        a = _mt_model(text, _build_mt_ctx(chat_id, cid))
        if a.get('error'):
            return _dialog_reply(d, '\u7406\u89e3\u8bf7\u6c42\u5931\u8d25\u6216\u8d85\u65f6\uff0c\u672a\u6267\u884c\u52a8\u4f5c\uff1b\u8bf7\u91cd\u8ff0\u5177\u4f53\u64cd\u4f5c\u3002\u7f16\u53f7\uff1a' + request_id)
        if a.get('action') == 'list_files':
            return _mt_snapshot(d)
        if a.get('action') == 'count_files':
            return _mt_snapshot(d, count_only=True)
        if a.get('action') == 'progress':
            return _mt_reply_progress(chat_id)
        return _dialog_reply(d, '\u5f53\u524d\u6a21\u5f0f\u652f\u6301\u67e5\u6e05\u5355\u3001\u9009\u6761\u3001\u8bbe\u7f6e\u6807\u9898\u548c\u8bdd\u9898\u3001\u6539\u6392\u671f\u3001\u67e5\u8fdb\u5ea6\u53ca\u53d6\u6d88\uff1b\u8bf7\u8bf4\u660e\u5177\u4f53\u64cd\u4f5c\u3002')
    except Exception as exc:
        print(json.dumps({'request_id': request_id, 'route': 'mt', 'error': type(exc).__name__,
                          'elapsed': round(time.monotonic() - started, 3)}), flush=True)
        return reply(chat_id, '\u672c\u6b21\u5904\u7406\u5931\u8d25\uff0c\u672a\u786e\u8ba4\u5b8c\u6210\uff1b\u8bf7\u67e5\u8fdb\u5ea6\u540e\u91cd\u8bd5\u3002\u7f16\u53f7\uff1a' + request_id)
    finally:
        print(json.dumps({'request_id': request_id, 'route': 'mt',
                          'elapsed': round(time.monotonic() - started, 3)}), flush=True)



# ---------------------------------------------------------------- publish_once flow
# 2026-10-23: bypass the legacy publish gate chain; call the verified one-shot
# script on the customer machine (upload -> title -> tags -> AI declaration ->
# star task -> recommended-frame cover -> publish).
ONCE_SCRIPT = os.environ.get("ONCE_SCRIPT") or CLIENT_HOME + r"\publish_once.py"
ONCE_PY = os.environ.get("ONCE_PY") or WORKER_ROOT + r"\.venv\Scripts\python.exe"
PUB_INBOX = os.environ.get("PUB_INBOX") or WORKER_ROOT + r"\inbox"
ONCE_LEDGER = os.environ.get("ONCE_LEDGER", "/tmp/publish_once_done.json")
ONCE_TAGS = os.environ.get("ONCE_TAGS", "#\u706b\u9f99\u6f2b\u5267|#\u6f2b\u5267|#\u817e\u8baf\u6f2b\u5267")
ONCE_TASK = os.environ.get("ONCE_TASK", "\u514d\u8d39-\u65b0\u7801")
ONCE_DEFAULT_TITLE = os.environ.get("ONCE_TITLE", "\u706b\u9f99\u6f2b\u5267\u514d\u8d39\u770b")
ONCE_PENDING = {}
ONCE_TAGS_OVERRIDE = {}
ONCE_TITLE_OVERRIDE = {}
ONCE_LIST = {}
ONCE_TASK_OVERRIDE = {}
ONCE_MODE = {}
ONCE_DELETE = {}
ONCE_EDIT = {}
PUB_RUNNING = set()
EDIT_SCRIPT = os.environ.get("EDIT_SCRIPT") or CLIENT_HOME + r"\edit_work.py"
DELETE_SCRIPT = os.environ.get("DELETE_SCRIPT") or CLIENT_HOME + r"\delete_work.py"
ONCE_AWAIT_MODE = {}
ONCE_STATE_FILE = os.environ.get("ONCE_STATE_FILE", "/tmp/publish_bot_state.json")


def _once_state_load():
    try:
        with open(ONCE_STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _once_state_set(chat_id, **kw):
    d = _once_state_load()
    cur = d.get(chat_id) or {}
    cur.update(kw)
    d[chat_id] = cur
    try:
        with open(ONCE_STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)
    except Exception:
        pass


def _once_state_get(chat_id):
    return (_once_state_load().get(chat_id) or {})


def _extract_tags(text):
    return re.findall(r"#[^\s#,\uff0c\u3002;\uff1b]+", text or "")


def _once_call(argv, timeout=420):
    import urllib.request
    _a0 = str(argv[0]).lower() if argv else ""
    if _a0 in ("cmd", "cmd.exe", "powershell", "powershell.exe"):
        _parts = list(argv[1:])
        if _parts and str(_parts[0]).lower() in ("/c", "-c", "/k"):
            _cmd = " ".join(str(x) for x in _parts[1:])
        else:
            _cmd = " ".join(str(x) for x in _parts)
        body = json.dumps({"shell": "cmd" if _a0.startswith("cmd") else "powershell",
                           "command": _cmd, "timeout": timeout}).encode()
    else:
        body = json.dumps({"argv": argv, "timeout": timeout}).encode()
    req = urllib.request.Request(
        TUNNEL, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + TUNNEL_TOKEN})
    return json.loads(urllib.request.urlopen(req, timeout=timeout + 60).read().decode())


def _once_ledger():
    try:
        with open(ONCE_LEDGER, encoding="utf-8") as fh:
            return set(json.load(fh))
    except Exception:
        return set()


def _once_mark(name):
    s = _once_ledger()
    s.add(name)
    try:
        with open(ONCE_LEDGER, "w", encoding="utf-8") as fh:
            json.dump(sorted(s), fh, ensure_ascii=False)
    except Exception:
        pass


def _once_queue():
    code = ("import os,json;print(json.dumps(sorted(f for f in os.listdir(r'%s')"
            " if f.lower().endswith('.mp4'))))" % PUB_INBOX)
    d = _once_call([ONCE_PY, "-c", code], timeout=60)
    try:
        names = json.loads((d.get("stdout") or "[]").strip().splitlines()[-1])
    except Exception:
        names = []
    return names, _once_ledger()


def _once_title(text):
    t = (text or "").strip()
    for kw in ("\u4f5c\u54c1\u63cf\u8ff0\u662f", "\u4f5c\u54c1\u63cf\u8ff0", "\u63cf\u8ff0\u662f"):
        if kw in t:
            t = t.split(kw, 1)[1]
            break
    if t.startswith("\u53d1\u5e03"):
        t = t[2:]
    t = re.sub(r"#[^\s#,\uff0c\u3002;\uff1b]+", " ", t)
    t = re.sub(r"^[\s,\uff0c:\uff1a\u662f]+|[\s,\uff0c:\uff1a]+$", "", t)
    return t or ONCE_DEFAULT_TITLE


def _show_list(chat_id):
    try:
        names, done = _once_queue()
    except Exception as exc:
        # multi-tenant chats have no ssh tunnel: fall back to the control plane
        if MULTITENANT:
            try:
                if lookup_customer(chat_id):
                    return handle_multitenant(chat_id, "\u53d1\u5e03")
            except Exception:
                pass
        reply(chat_id, "\u53d6\u5f85\u53d1\u961f\u5217\u5931\u8d25\uff1a%s" % str(exc)[:150])
        return
    if not names:
        reply(chat_id, "\u5f85\u53d1\u5e03\u7d20\u6750\u91cc\u6ca1\u6709\u89c6\u9891\u3002"
                       "\u628a\u89c6\u9891\u653e\u8fdb\u684c\u9762\u300c\u5f85\u53d1\u5e03\u7d20\u6750\u300d\u540e\u518d\u53d1\u300c\u53d1\u89c6\u9891\u300d\u3002")
        return
    ONCE_LIST[chat_id] = names
    mode = ONCE_MODE.get(chat_id) or "\u661f\u56fe"
    lines = ["\u53d1\u5e03\u65b9\u5f0f\uff1a%s" % mode,
             "\u5f85\u53d1\u5e03\u5171 %d \u6761\uff1a" % len(names)]
    for i, n in enumerate(names[:12], 1):
        mark = "\uff08\u5df2\u53d1\uff09" if n in done else "\uff08\u65b0\uff09"
        lines.append("%d. %s %s" % (i, n, mark))
    lines.append("")
    lines.append("\u53d1\u54ea\u4e9b\uff1f\u56de\u6570\u5b57\uff08\u5982 5 / 1 3 5 / \u5168\u90e8\uff09\u3002"
                 "\u60f3\u6362\u65b9\u5f0f\u53d1\u300c\u666e\u901a\u300d\u6216\u300c\u661f\u56fe\u300d\u3002")
    reply(chat_id, "\n".join(lines))


def start_once_publish(chat_id, text):
    tags = _extract_tags(text)
    if tags:
        ONCE_TAGS_OVERRIDE[chat_id] = tags
    _st = _once_state_get(chat_id)
    if not ONCE_MODE.get(chat_id) and _st.get("mode"):
        ONCE_MODE[chat_id] = _st["mode"]
    if not ONCE_MODE.get(chat_id):
        ONCE_AWAIT_MODE[chat_id] = True
        _once_state_set(chat_id, await_mode=True)
        reply(chat_id, "\u600e\u4e48\u53d1\uff1f\n1. \u666e\u901a\u53d1\u5e03\uff08\u4e0d\u6302\u661f\u56fe\u4efb\u52a1\uff09\n"
                       "2. \u661f\u56fe\u53d1\u5e03\uff08\u6302\u661f\u56fe\u4efb\u52a1\uff09\n\n\u56de\u300c\u666e\u901a\u300d\u6216\u300c\u661f\u56fe\u300d\u3002")
        return
    _show_list(chat_id)


def set_once_mode(chat_id, text):
    m = "\u661f\u56fe" if "\u661f\u56fe" in (text or "") else "\u666e\u901a"
    ONCE_MODE[chat_id] = m
    ONCE_AWAIT_MODE.pop(chat_id, None)
    _once_state_set(chat_id, mode=m, await_mode=False)
    reply(chat_id, "\u53d1\u5e03\u65b9\u5f0f\u5df2\u8bbe\u4e3a\uff1a%s" % m)
    _show_list(chat_id)


def _parse_pick(cmd, n):
    cmd = (cmd or "").strip()
    for _pre in ("\u53ea\u8981", "\u6211\u8981", "\u5c31\u53d1", "\u53ea\u53d1", "\u53d1"):
        if cmd.startswith(_pre):
            cmd = cmd[len(_pre):]
            break
    if cmd in ("\u5168\u90e8", "\u90fd\u53d1", "\u5168\u53d1", "\u5168\u90e8\u53d1"):
        return list(range(1, n + 1))
    if not re.fullmatch(r"[\d\u7b2c\u6761\u4ef6\u3001,\uff0c\-\s\u548c\u8ddf\u4e0e/\uff0f]+", cmd or ""):
        return None
    nums = []
    for part in re.split(r"[\u3001,\uff0c\s\u548c\u8ddf\u4e0e/\uff0f]+", cmd):
        part = part.strip("\u7b2c\u6761\u4ef6")
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if a.isdigit() and b.isdigit():
                nums += list(range(int(a), int(b) + 1))
        elif part.isdigit():
            nums.append(int(part))
    nums = sorted(set(x for x in nums if 1 <= x <= n))
    return nums or None


def _norm_task(word):
    w = (word or "").strip()
    if "\u514d\u8d39" in w:
        return "\u514d\u8d39-\u65b0\u7801"
    if "\u6fc0\u52b1" in w:
        return "\u6fc0\u52b1"
    return w or (ONCE_TASK_OVERRIDE.get("__global__") or ONCE_TASK)


def _render_pending(chat_id):
    p = ONCE_PENDING.get(chat_id) or {}
    items = p.get("items") or []
    if not items:
        reply(chat_id, "\u6ca1\u6709\u5f85\u53d1\u5e03\u7684\u89c6\u9891\u3002")
        return
    star = (ONCE_MODE.get(chat_id) or "\u661f\u56fe") == "\u661f\u56fe"
    lines = ["\u51c6\u5907\u53d1\u5e03 %d \u6761\uff1a" % len(items)]
    for k, it in enumerate(items, 1):
        flag = "\uff08\u4e4b\u524d\u53d1\u8fc7\uff09" if it.get("republish") else ""
        t = ("  \u661f\u56fe\u4efb\u52a1\uff1a%s" % it.get("task")) if star else ""
        lines.append("%d. %s%s%s" % (k, it["video"], flag, t))
    lines.append("\u6807\u9898\uff1a%s" % (items[0].get("title") or ONCE_DEFAULT_TITLE))
    lines.append("\u6807\u7b7e\uff1a%s" % " ".join(items[0].get("tags") or ONCE_TAGS.split("|")))
    lines.append("")
    any_rep = any(it.get("republish") for it in items)
    lines.append("\u56de\u590d\u300c\u786e\u8ba4\u91cd\u53d1\u300d\u5f00\u59cb\uff0c\u53d6\u6d88\u56de\u300c\u53d6\u6d88\u300d\u3002"
                 if any_rep else
                 "\u56de\u590d\u300c\u786e\u8ba4\u300d\u5f00\u59cb\u53d1\u5e03\uff08\u6bcf\u6761\u7ea6 1 \u5206\u949f\uff09\u3002")
    reply(chat_id, "\n".join(lines))


def set_item_tasks(chat_id, items):
    p = ONCE_PENDING.get(chat_id) or {}
    cur = p.get("items") or []
    if not cur:
        reply(chat_id, "\u5148\u9009\u8981\u53d1\u54ea\u51e0\u6761\u3002")
        return
    for it in items:
        try:
            idx = int(it.get("index"))
        except Exception:
            continue
        if 1 <= idx <= len(cur):
            cur[idx - 1]["task"] = _norm_task(it.get("task") or "")
    _render_pending(chat_id)


def select_once_item(chat_id, idxs, text, item_tasks=None):
    q = ONCE_LIST.get(chat_id) or []
    if not q:
        reply(chat_id, "\u5148\u53d1\u300c\u53d1\u89c6\u9891\u300d\u770b\u5217\u8868\u3002")
        return
    tags = _extract_tags(text)
    if tags:
        ONCE_TAGS_OVERRIDE[chat_id] = tags
    use_tags = ONCE_TAGS_OVERRIDE.get(chat_id) or ONCE_TAGS.split("|")
    title = ONCE_TITLE_OVERRIDE.get(chat_id) or ONCE_DEFAULT_TITLE
    done = _once_ledger()
    items = []
    for i in idxs:
        v = q[i - 1]
        items.append({"video": v, "title": title, "tags": use_tags, "republish": v in done,
                      "task": ONCE_TASK_OVERRIDE.get(chat_id) or ONCE_TASK})
    if item_tasks:
        for it in item_tasks:
            try:
                _i = int(it.get("index"))
            except Exception:
                continue
            if 1 <= _i <= len(items):
                items[_i - 1]["task"] = _norm_task(it.get("task") or "")
    ONCE_PENDING[chat_id] = {"items": items}
    _render_pending(chat_id)


def set_once_task(chat_id, text):
    arg = re.sub(r"^\u4efb\u52a1[:\uff1a\s]*", "", (text or "").strip())
    if arg:
        ONCE_TASK_OVERRIDE[chat_id] = arg
    cur = ONCE_TASK_OVERRIDE.get(chat_id) or ONCE_TASK
    reply(chat_id, "\u661f\u56fe\u4efb\u52a1\uff1a%s\n\n\u6539\uff1a\u53d1\u300c\u4efb\u52a1 \u5173\u952e\u8bcd\u300d"
                   "\uff08\u5982 \u4efb\u52a1 \u4ed8\u8d39-\u65b0\u7801\uff09\u3002" % cur)


def set_once_title(chat_id, text):
    t = re.sub(r"^\u6807\u9898[:\uff1a\s]*", "", (text or "").strip())
    if t:
        ONCE_TITLE_OVERRIDE[chat_id] = t
    reply(chat_id, "\u6807\u9898\u5df2\u8bbe\u4e3a\uff1a%s" % (t or ONCE_TITLE_OVERRIDE.get(chat_id) or ONCE_DEFAULT_TITLE))


def verify_published(title, since_ts, timeout=420):
    """Re-read both douyin works lists and confirm the work really exists."""
    try:
        run_cli(["collect-all", "--skip-xingtu"], timeout=timeout)
        base = WORKER_ROOT + r"\out\douyin"
        code = ("import glob,json,os,io;"
                "fs=glob.glob(os.path.join(r'%s','*.json'));"
                "fs.sort(key=os.path.getmtime,reverse=True);"
                "out=[];"
                "seen=set();"
                "\nfor f in fs[:4]:\n"
                "    try: d=json.load(io.open(f,encoding='utf-8-sig'))\n"
                "    except Exception: continue\n"
                "    for x in (d.get('records') or []):\n"
                "        k=(x.get('作品名称'),x.get('发布时间'))\n"
                "        if k in seen: continue\n"
                "        seen.add(k); out.append([x.get('作品名称'),x.get('发布时间')])\n"
                "print(json.dumps(out,ensure_ascii=False))" % base.replace("\\","\\\\"))
        raw = run_on_client(code, timeout=120)
        line = raw.strip().splitlines()[-1] if raw.strip() else "[]"
        recs = json.loads(line)
        key = re.sub(r"\s+", "", str(title or ""))
        if not key:
            return False, "no-title"
        best = None
        for t, ts in recs:
            tt = re.sub(r"\s+", "", str(t or ""))
            if key not in tt:
                continue
            try:
                import datetime as _d
                dt = _d.datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
                if dt.timestamp() >= since_ts - 300:
                    if best is None or str(ts) > best:
                        best = str(ts)
            except Exception:
                continue
        return (True, best) if best else (False, "not-in-list")
    except Exception as exc:
        return False, str(exc)[:120]


def confirm_once_publish(chat_id):
    if chat_id in PUB_RUNNING:
        reply(chat_id, "\u6b63\u5728\u53d1\u5e03\u4e2d\uff0c\u4e0d\u8981\u91cd\u590d\u786e\u8ba4\u3002\u5b8c\u4e86\u4f1a\u56de\u62a5\u3002")
        return
    p = ONCE_PENDING.pop(chat_id, None)
    if not p:
        reply(chat_id, "\u6ca1\u6709\u5f85\u786e\u8ba4\u7684\u53d1\u5e03\u3002\u5148\u53d1\u300c\u53d1\u89c6\u9891\u300d\u3002")
        return
    items = p.get("items") or ([p] if p.get("video") else [])
    if not items:
        reply(chat_id, "\u6ca1\u6709\u5f85\u53d1\u5e03\u7684\u89c6\u9891\u3002")
        return
    reply(chat_id, "\u5f00\u59cb\u53d1\u5e03 %d \u6761\uff0c\u4e00\u6761\u4e00\u6761\u6765\u2026" % len(items))

    def _worker():
        PUB_RUNNING.add(chat_id)
        ok_n = 0
        fails = []
        for k, it in enumerate(items, 1):
            _t0 = time.time()
            try:
                d = _once_call([ONCE_PY, ONCE_SCRIPT,
                                PUB_INBOX + "\\" + it["video"],
                                "--title", it["title"],
                                "--tags", "|".join(it.get("tags") or ONCE_TAGS.split("|")),
                                ] + (["--task", (ONCE_TASK_OVERRIDE.get(chat_id) or ONCE_TASK)] if (ONCE_MODE.get(chat_id) or "\u661f\u56fe") == "\u661f\u56fe" else []) + ["--publish"], timeout=420)
                out = (d.get("stdout") or "").strip()
                ok = False
                try:
                    j = json.loads(out.splitlines()[-1])
                    ok = bool(j.get("ok"))
                except Exception:
                    ok = False
                if ok:
                    _once_mark(it["video"])
                    ok_n += 1
                    _v, _info = verify_published(it["title"], _t0)
                    if _v:
                        reply(chat_id, "\u7b2c %d \u6761\u5df2\u53d1\u5e76\u56de\u8bfb\u786e\u8ba4\uff1a%s\uff08%s\uff09" % (k, it["video"], _info))
                    else:
                        reply(chat_id, "\u7b2c %d \u6761\u811a\u672c\u62a5\u6210\u529f\uff0c\u4f46\u4f5c\u54c1\u5217\u8868\u6ca1\u67e5\u5230\uff0c\u6309\u672a\u786e\u8ba4\u5904\u7406\uff1a%s" % (k, it["video"]))
                else:
                    tail = out[-300:] or (d.get("stderr") or "")[-200:] or "\u65e0\u8fd4\u56de"
                    fails.append("%s\uff1a%s" % (it["video"], tail))
                    reply(chat_id, "\u7b2c %d \u6761\u6ca1\u53d1\u6210\uff1a%s" % (k, it["video"]))
            except Exception as exc:
                fails.append("%s\uff1a%s" % (it["video"], str(exc)[:120]))
                reply(chat_id, "\u7b2c %d \u6761\u62a5\u9519\uff1a%s" % (k, str(exc)[:120]))
            if k < len(items):
                try:
                    _g = _batch_gap_seconds({"gap_min": int(os.environ.get("BATCH_GAP_MIN") or 5), "gap_max": int(os.environ.get("BATCH_GAP_MAX") or 8)})
                except Exception:
                    _g = 300
                reply(chat_id, "\u7b2c %d \u6761\u5df2\u53d1\uff0c%d \u5206\u949f\u540e\u81ea\u52a8\u53d1\u4e0b\u4e00\u6761\u3002" % (k, _g // 60))
                _sleep_until(time.monotonic() + _g)
        if fails:
            reply(chat_id, "\u5168\u90e8\u8dd1\u5b8c\uff1a\u6210\u529f %d \u6761\uff0c\u5931\u8d25 %d \u6761\u3002\n%s"
                  % (ok_n, len(fails), "\n".join(fails)[:600]))
        else:
            reply(chat_id, "\u5168\u90e8\u53d1\u5b8c\uff1a\u6210\u529f %d \u6761\u3002" % ok_n)
        PUB_RUNNING.discard(chat_id)

    threading.Thread(target=_worker, daemon=True).start()



SCHED_FILE = os.environ.get("SCHED_FILE", "/tmp/publish_bot_schedule.json")


def _sched_load():
    try:
        with open(SCHED_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return []


def _sched_save(items):
    try:
        with open(SCHED_FILE, "w", encoding="utf-8") as fh:
            json.dump(items, fh, ensure_ascii=False)
    except Exception:
        pass


def schedule_jobs(chat_id, a):
    import datetime as _dt
    at = str(a.get("at") or "").strip()
    jobs = a.get("jobs") or []
    if not at or not jobs:
        reply(chat_id, "\u8981\u5b9a\u5230\u4ec0\u4e48\u65f6\u5019\u3001\u53d1\u54ea\u51e0\u6761\uff1f\u6bd4\u5982\u300c\u660e\u5929 9 \u70b9\u53d1\u7b2c 6 \u6761\u300d\u3002")
        return True
    try:
        when = _dt.datetime.fromisoformat(at.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
    except Exception:
        reply(chat_id, "\u65f6\u95f4\u6ca1\u770b\u61c2\uff0c\u8bf4\u6210\u300c2026-10-24 09:00\u300d\u8fd9\u6837\u3002")
        return True
    names, done = _once_queue()
    items = []
    for j in jobs:
        f = str(j.get("file") or "").strip()
        if f and f not in names:
            cand = [n for n in names if f in n or n in f]
            f = cand[0] if cand else ""
        if not f:
            continue
        items.append({"video": f,
                      "title": j.get("title") or ONCE_TITLE_OVERRIDE.get(chat_id) or ONCE_DEFAULT_TITLE,
                      "tags": j.get("tags") or ONCE_TAGS_OVERRIDE.get(chat_id) or ONCE_TAGS.split("|"),
                      "task": _norm_task(j.get("task") or "") if j.get("task") else (ONCE_TASK_OVERRIDE.get(chat_id) or ONCE_TASK),
                      "star": bool(j.get("task")) or (ONCE_MODE.get(chat_id) or "\u661f\u56fe") == "\u661f\u56fe"})
    if not items:
        reply(chat_id, "\u6ca1\u8ba4\u51fa\u8981\u53d1\u54ea\u51e0\u6761\u3002")
        return True
    data = _sched_load()
    data.append({"chat": chat_id, "at": when.isoformat(), "items": items})
    _sched_save(data)
    reply(chat_id, "\u5df2\u6392\u671f\uff1a%s\n%s"
          % (when.strftime("%Y-%m-%d %H:%M"),
             "\n".join("%d. %s%s" % (i, it["video"], (" | " + it["task"]) if it.get("star") else " | \u666e\u901a")
                        for i, it in enumerate(items, 1))))
    return True


def _sched_worker():
    import datetime as _dt
    while True:
        try:
            now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))
            data = _sched_load()
            keep = []
            for job in data:
                try:
                    when = _dt.datetime.fromisoformat(job.get("at"))
                except Exception:
                    continue
                if when > now:
                    keep.append(job)
                    continue
                _run_scheduled(job)
            if len(keep) != len(data):
                _sched_save(keep)
        except Exception as exc:
            print("[sched] %s" % str(exc)[:150], flush=True)
        time.sleep(20)


def _run_scheduled(job):
    chat_id = job.get("chat")
    for it in job.get("items") or []:
        args = [ONCE_PY, ONCE_SCRIPT, PUB_INBOX + "\\" + it["video"],
                "--title", it["title"], "--tags", "|".join(it.get("tags") or ONCE_TAGS.split("|"))]
        if it.get("star"):
            args += ["--task", it.get("task") or ONCE_TASK]
        args += ["--publish"]
        try:
            d = _once_call(args, timeout=420)
            out = (d.get("stdout") or "").strip()
            ok = False
            try:
                ok = bool(json.loads(out.splitlines()[-1]).get("ok"))
            except Exception:
                ok = False
            if ok:
                _once_mark(it["video"])
                reply(chat_id, "\u5b9a\u65f6\u53d1\u5e03\u5b8c\u6210\uff1a%s" % it["video"])
            else:
                reply(chat_id, "\u5b9a\u65f6\u53d1\u5e03\u5931\u8d25\uff1a%s\n%s" % (it["video"], out[-200:]))
        except Exception as exc:
            reply(chat_id, "\u5b9a\u65f6\u53d1\u5e03\u62a5\u9519\uff1a%s" % str(exc)[:150])



def request_delete(chat_id, a):
    title = str(a.get("title") or "").strip()
    if not title:
        reply(chat_id, a.get("say") or "\u5220\u54ea\u4e00\u6761\uff1f\u7ed9\u6211\u6807\u9898\u91cc\u7684\u51e0\u4e2a\u5b57\u5c31\u884c\u3002")
        return True
    ONCE_DELETE[chat_id] = title
    reply(chat_id, "\u786e\u8ba4\u5220\u9664\u6807\u9898\u5305\u542b\u300c%s\u300d\u7684\u6700\u65b0\u4e00\u6761\u4f5c\u54c1\uff1f\u56de\u300c\u786e\u8ba4\u5220\u9664\u300d\u3002" % title)
    return True


def confirm_delete(chat_id):
    title = ONCE_DELETE.pop(chat_id, None)
    if not title:
        reply(chat_id, "\u6ca1\u6709\u5f85\u786e\u8ba4\u7684\u5220\u9664\u3002")
        return
    reply(chat_id, "\u5f00\u59cb\u5220\u9664\uff1a%s \u2026" % title)

    def _worker():
        try:
            d = _once_call([ONCE_PY, DELETE_SCRIPT, title], timeout=180)
            out = (d.get("stdout") or "").strip()
            try:
                j = json.loads(out.splitlines()[-1])
            except Exception:
                j = {}
            if j.get("ok"):
                reply(chat_id, "\u5df2\u5220\u9664\uff1a%s" % (j.get("found", {}).get("row") or title))
            else:
                reply(chat_id, "\u6ca1\u5220\u6210\uff1a%s" % (out[-200:] or "\u6ca1\u627e\u5230\u8fd9\u6761\u4f5c\u54c1"))
        except Exception as exc:
            reply(chat_id, "\u5220\u9664\u62a5\u9519\uff1a%s" % str(exc)[:150])

    threading.Thread(target=_worker, daemon=True).start()



def llm_chat(user_text, system_text=""):
    import urllib.request
    msgs = []
    if system_text:
        msgs.append({"role": "system", "content": system_text})
    msgs.append({"role": "user", "content": user_text})
    body = json.dumps({"model": LLM_MODEL, "messages": msgs,
                       "temperature": 0.3, "max_tokens": 700}).encode()
    req = urllib.request.Request("https://api.deepseek.com/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + LLM_KEY})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=60).read())
        return scrub_fake_success((r["choices"][0]["message"].get("content") or "").strip())
    except Exception as exc:
        return "\u6570\u636e\u5206\u6790\u5931\u8d25\uff1a%s" % str(exc)[:120]


def _newest_data_json():
    code = ("import glob,os,json;"
            "base=r'%s';"
            "fs=glob.glob(os.path.join(base,'out','**','*.json'),recursive=True);"
            "fs=[f for f in fs if os.path.getsize(f)>500];"
            "fs.sort(key=os.path.getmtime,reverse=True);"
            "print(fs[0] if fs else '')") % WORKER_ROOT.replace("\\", "\\\\")
    d = _once_call([ONCE_PY, "-c", code], timeout=60)
    path = (d.get("stdout") or "").strip().splitlines()[-1] if (d.get("stdout") or "").strip() else ""
    if not path:
        return "", {}
    _bs = chr(92)
    code2 = ("import json;d=json.load(open(r'%s',encoding='utf-8'));"
             "print(json.dumps(d,ensure_ascii=False)[:400000])") % path.replace(_bs, _bs + _bs)
    d2 = _once_call([ONCE_PY, "-c", code2], timeout=90)
    raw = (d2.get("stdout") or "").strip()
    return path, raw


def spend_data(chat_id):
    def _worker():
        try:
            reply(chat_id, "\u53bb\u62c9\u661f\u56fe\u6d88\u8017\uff0c\u7a0d\u7b49\u2026")
            try:
                run_cli(["env-open", "--env", "env_1"], timeout=90)
            except Exception:
                pass
            r = run_cli(["collect-all", "--skip-douyin"], timeout=300)
            out = (r.get("stdout") or "").strip()
            try:
                j = json.loads(out)
            except Exception:
                j = {}
            if not (j.get("collected") or {}).get("xingtu"):
                reason = j.get("error") or (r.get("stderr") or "")[-160:] or "\u6ca1\u53d6\u5230\u661f\u56fe\u6570\u636e"
                reply(chat_id, "\u661f\u56fe\u6d88\u8017\u6ca1\u53d6\u5230\uff1a%s\n"
                      "\u5927\u6982\u7387\u662f\u661f\u56fe\u90a3\u4e2a\u53f7\u6ca1\u767b\u5f55\uff08\u73af\u5883 env_1 / 9224\uff09\uff0c\u5728\u9762\u677f\u767b\u4e00\u4e0b\u5c31\u884c\u3002" % str(reason)[:120])
                return
            path, raw = _newest_data_json()
            d = json.loads(raw) if raw else {}
            rows = d.get("records") or d.get("rows") or []
            if not rows:
                reply(chat_id, "\u661f\u56fe\u6570\u636e\u8bfb\u5230\u4e86\uff0c\u4f46\u91cc\u9762\u6ca1\u6709\u8bb0\u5f55\u3002")
                return
            def _n(v):
                try:
                    return float(str(v).replace(",", "").replace("%", "").replace("\u00a5", "") or 0)
                except Exception:
                    return 0.0
            lines = ["\u661f\u56fe\u6570\u636e\uff1a%d \u6761" % len(rows)]
            for key in ("\u6d88\u8017", "\u9884\u4f30\u6536\u76ca", "\u6709\u6548\u64ad\u653e", "\u5206\u6210"):
                tot = sum(_n(x.get(key)) for x in rows)
                if tot:
                    lines.append("%s\uff1a%s" % (key, ("%.2f" % tot).rstrip("0").rstrip(".")))
            reply(chat_id, "\n".join(lines))
        except Exception as exc:
            reply(chat_id, "\u661f\u56fe\u53d6\u6570\u62a5\u9519\uff1a%s" % str(exc)[:150])
    threading.Thread(target=_worker, daemon=True).start()


def analyze_data(chat_id):
    reply(chat_id, "\u5148\u53bb\u540e\u53f0\u53d6\u6570\uff0c\u7a0d\u7b49\u2026")

    def _worker():
        try:
            try:
                run_cli(["env-open", "--env", os.environ.get("BOT_DATA_ENV", "env_2")], timeout=60)
            except Exception:
                pass
            r = run_cli(["collect-all"], timeout=300)
            if not _exec_ok(r):
                _partial = False
                try:
                    _js = json.loads((r.get("stdout") or "").strip())
                    _partial = bool((_js.get("collected") or {}).get("douyin"))
                except Exception:
                    _partial = False
                if not _partial:
                    reply(chat_id, "\u53d6\u6570\u6ca1\u5b8c\u6210\uff1a%s"
                          % friendly_error(r.get("stderr") or r.get("stdout"), "\u8bf7\u7a0d\u540e\u518d\u8bd5\u3002"))
                    return
            path, raw = _newest_data_json()
            if not raw:
                reply(chat_id, "\u53d6\u6570\u5b8c\u4e86\uff0c\u4f46\u6ca1\u627e\u5230\u6570\u636e\u6587\u4ef6\u3002")
                return
            sys_p = ("\u4f60\u662f\u77ed\u89c6\u9891\u8fd0\u8425\u5206\u6790\u5e08\u3002\u4e0b\u9762\u662f\u6296\u97f3/\u661f\u56fe\u540e\u53f0\u5bfc\u51fa\u7684\u539f\u59cb JSON\u3002"
                     "\u7528\u4e2d\u6587\u7ed9\u51fa\uff1a1) \u8868\u73b0\u6700\u597d\u7684 3 \u6761\uff08\u6807\u9898+\u5173\u952e\u6570\u5b57\uff09 2) \u6570\u636e\u91cc\u770b\u5f97\u51fa\u7684\u95ee\u9898 3) \u4e0b\u4e00\u6b65\u5efa\u8bae\u3002"
                     "\u53ea\u7528\u6570\u636e\u91cc\u771f\u5b9e\u5b58\u5728\u7684\u6570\u5b57\uff0c\u4e0d\u51c6\u7f16\u3002\u4e0d\u8d85\u8fc7 300 \u5b57\u3002")
            _sum = ""
            _xl = ""
            try:
                _d = json.loads(raw)
                _recs = _d.get("records") or []
                def _num(v):
                    try:
                        return float(str(v).replace(",", "").replace("%", "") or 0)
                    except Exception:
                        return 0.0
                _name = _d.get("account_name") or ""
                _lines = ["\u8d26\u53f7\uff1a%s\uff08\u5171 %d \u6761\u4f5c\u54c1\uff09" % (_name, len(_recs))]
                _new = sorted(_recs, key=lambda x: str(x.get("\u53d1\u5e03\u65f6\u95f4") or ""), reverse=True)[:1]
                for _r0 in _new:
                    _lines.append("\u6700\u65b0\uff1a%s\uff5c%s\uff5c\u64ad\u653e %s\uff5c%s" % (_r0.get("\u53d1\u5e03\u65f6\u95f4",""), _r0.get("\u4f5c\u54c1\u540d\u79f0","")[:24], _r0.get("\u64ad\u653e\u91cf",""), _r0.get("\u5ba1\u6838\u72b6\u6001","")))
                _top = sorted(_recs, key=lambda x: _num(x.get("\u64ad\u653e\u91cf")), reverse=True)[:3]
                _lines.append("\u64ad\u653e\u524d3\uff1a")
                for _i, _r0 in enumerate(_top, 1):
                    _lines.append("%d. %s\uff5c\u64ad\u653e %s\uff5c\u70b9\u8d5e %s" % (_i, _r0.get("\u4f5c\u54c1\u540d\u79f0","")[:24], _r0.get("\u64ad\u653e\u91cf",""), _r0.get("\u70b9\u8d5e\u91cf","")))
                _lines.append("\u5408\u8ba1\uff1a\u64ad\u653e %d\uff0c\u70b9\u8d5e %d" % (int(sum(_num(x.get("\u64ad\u653e\u91cf")) for x in _recs)), int(sum(_num(x.get("\u70b9\u8d5e\u91cf")) for x in _recs))))
                _sum = "\n".join(_lines)
                _hdr = _d.get("header") or []
                if _hdr and _recs:
                    from openpyxl import Workbook
                    _wb = Workbook(); _ws = _wb.active
                    _ws.append(list(_hdr))
                    for _r1 in _recs:
                        _ws.append([_r1.get(_h, "") for _h in _hdr])
                    for _ci, _h in enumerate(_hdr, 1):
                        _ws.column_dimensions[_ws.cell(row=1, column=_ci).column_letter].width = 26 if _ci == 1 else 14
                    _ws.freeze_panes = "A2"
                    import time as _t
                    _xl = "/tmp/bot_table_%s.xlsx" % _t.strftime("%Y%m%d_%H%M%S")
                    _wb.save(_xl)
            except Exception:
                _sum = ""
            if _sum:
                reply(chat_id, _sum)
                if _xl:
                    try:
                        send_file(chat_id, _xl)
                    except Exception:
                        pass
            else:
                out = llm_chat(raw, sys_p)
                reply(chat_id, out or "\u6570\u636e\u91cc\u6ca1\u8bfb\u51fa\u6709\u6548\u4fe1\u606f\u3002")
        except Exception as exc:
            reply(chat_id, "\u5206\u6790\u62a5\u9519\uff1a%s" % str(exc)[:150])

    threading.Thread(target=_worker, daemon=True).start()



def request_edit(chat_id, a):
    match = str(a.get("match") or "").strip()
    new = str(a.get("title") or a.get("new_title") or "").strip()
    if not match or not new:
        reply(chat_id, a.get("say") or "\u8981\u6539\u54ea\u4e00\u6761\u3001\u6539\u6210\u4ec0\u4e48\uff1f\u6bd4\u5982\u300c\u628a\u5b88\u591c\u73ed\u90a3\u6761\u6807\u9898\u6539\u6210 XXX\u300d\u3002")
        return True
    ONCE_EDIT[chat_id] = {"match": match, "new": new}
    reply(chat_id, "\u786e\u8ba4\u4fee\u6539\uff1a\u6807\u9898\u542b\u300c%s\u300d\u7684\u4f5c\u54c1 \u2192 \u300c%s\u300d\n\u56de\u300c\u786e\u8ba4\u4fee\u6539\u300d\u3002" % (match, new))
    return True


def confirm_edit(chat_id):
    p = ONCE_EDIT.pop(chat_id, None)
    if not p:
        reply(chat_id, "\u6ca1\u6709\u5f85\u786e\u8ba4\u7684\u4fee\u6539\u3002")
        return
    reply(chat_id, "\u5f00\u59cb\u4fee\u6539\uff1a%s \u2026" % p["match"])

    def _worker():
        try:
            d = _once_call([ONCE_PY, EDIT_SCRIPT, p["match"], p["new"]], timeout=240)
            out = (d.get("stdout") or "").strip()
            try:
                j = json.loads(out.splitlines()[-1])
            except Exception:
                j = {}
            if j.get("ok"):
                reply(chat_id, "\u5df2\u63d0\u4ea4\u4fee\u6539\uff1a\u300c%s\u300d" % p["new"])
            else:
                reply(chat_id, "\u6ca1\u6539\u6210\uff1a%s" % (out[-250:] or "\u6ca1\u627e\u5230\u8fd9\u6761\u4f5c\u54c1"))
        except Exception as exc:
            reply(chat_id, "\u4fee\u6539\u62a5\u9519\uff1a%s" % str(exc)[:150])

    threading.Thread(target=_worker, daemon=True).start()


def _build_ctx(chat_id):
    bits = []
    try:
        import datetime as _dt2
        _now = _dt2.datetime.now(_dt2.timezone(_dt2.timedelta(hours=8)))
        bits.append("现在时间：%s（星期%s）" % (_now.strftime("%Y-%m-%d %H:%M"), "一二三四五六日"[_now.weekday()]))
    except Exception:
        pass
    _h = CHAT_HIST.get(chat_id) or []
    if _h:
        bits.append("最近对话（时间从旧到新，用来理解省略和指代）：")
        for _w, _t in _h[-10:]:
            bits.append("%s：%s" % (_w, _t))
    names = ONCE_LIST.get(chat_id) or []
    if not names:
        try:
            names, _done = _once_queue()
            ONCE_LIST[chat_id] = names
        except Exception:
            names = []
    if names:
        bits.append("\u5f85\u53d1\u5e03\u7d20\u6750 %d \u6761\uff1a%s"
                    % (len(names), "\u3001".join("%d.%s" % (i, n) for i, n in enumerate(names, 1))))
    mode = ONCE_MODE.get(chat_id) or _once_state_get(chat_id).get("mode")
    if mode:
        bits.append("\u53d1\u5e03\u65b9\u5f0f\uff1a%s" % mode)
    pend = ONCE_PENDING.get(chat_id)
    if pend and pend.get("items"):
        bits.append("\u5df2\u9009\uff1a" + "\u3001".join(
            "%d.%s%s" % (i, it["video"], ("(\u4efb\u52a1:%s)" % it.get("task") if it.get("task") else ""))
            for i, it in enumerate(pend["items"], 1)))
        bits.append("\u7b49\u5f85\u7528\u6237\u786e\u8ba4")
    bits.append("\u9ed8\u8ba4\u89c4\u5219\uff1a\u706b\u9f99\u6f2b\u5267 \u2014 \u6807\u9898\u300c\u706b\u9f99\u6f2b\u5267\u514d\u8d39\u770b\u300d\uff0c"
                "\u6807\u7b7e #\u706b\u9f99\u6f2b\u5267 #\u6f2b\u5267 #\u817e\u8baf\u6f2b\u5267\uff0c"
                "\u514d\u8d39\u7801=\u514d\u8d39-\u65b0\u7801\uff0c\u6fc0\u52b1\u7801=\u6fc0\u52b1")
    return "\n".join(bits)


def _dispatch_llm(chat_id, a, text):
    act = a.get("action")
    if act == "publish_start":
        start_once_publish(chat_id, text); return True
    if act == "select":
        names = ONCE_LIST.get(chat_id) or []
        if not names:
            try:
                names, _ = _once_queue(); ONCE_LIST[chat_id] = names
            except Exception:
                names = []
        idxs = [int(i) for i in (a.get("indexes") or []) if str(i).isdigit()]
        idxs = sorted(set(i for i in idxs if 1 <= i <= len(names)))
        if idxs:
            select_once_item(chat_id, idxs, text, a.get("items") or None); return True
        reply(chat_id, "\u8981\u53d1\u7b2c\u51e0\u6761\uff1f\u56de\u5e8f\u53f7\u5c31\u884c\u3002")
        return True
    if act == "list_files":
        _names, _done = _once_queue()
        if not _names:
            reply(chat_id, "\u5f85\u53d1\u5e03\u7d20\u6750\u91cc\u76ee\u524d\u6ca1\u6709\u89c6\u9891\u3002")
            return True
        _lines = ["\u5f85\u53d1\u5e03\u7d20\u6750\u5171 %d \u6761\uff1a" % len(_names)]
        for i, n in enumerate(_names, 1):
            _lines.append("%d. %s%s" % (i, n, "\uff08\u5df2\u53d1\uff09" if n in _done else ""))
        reply(chat_id, "\n".join(_lines))
        return True
    if act == "count_files":
        _names, _done = _once_queue()
        _new = len([n for n in _names if n not in _done])
        reply(chat_id, "\u5f85\u53d1\u5e03\u7d20\u6750\u5171 %d \u6761\u89c6\u9891\uff0c\u5176\u4e2d\u65b0\u7684 %d \u6761\u3002" % (len(_names), _new))
        return True
    if act in ("analyze", "insight"):
        analyze_data(chat_id); return True
    if act in ("report", "collect"):
        analyze_data(chat_id); return True
    if act in ("spend", "xingtu"):
        spend_data(chat_id); return True
    if act == "edit":
        return request_edit(chat_id, a)
    if act == "delete":
        return request_delete(chat_id, a)
    if act == "schedule":
        return schedule_jobs(chat_id, a)
    if act == "run_jobs":
        _jobs = a.get("jobs") or []
        if not _jobs:
            reply(chat_id, "\u8981\u53d1\u54ea\u51e0\u6761\uff1f"); return True
        _names = ONCE_LIST.get(chat_id) or []
        if not _names:
            try:
                _names, _ = _once_queue(); ONCE_LIST[chat_id] = _names
            except Exception:
                _names = []
        _done = _once_ledger()
        _items = []
        for j in _jobs:
            f = str(j.get("file") or "").strip()
            if f and f not in _names:
                _cand = [n for n in _names if f in n or n in f]
                f = _cand[0] if _cand else ""
            if not f:
                continue
            _items.append({"video": f,
                           "title": j.get("title") or ONCE_TITLE_OVERRIDE.get(chat_id) or ONCE_DEFAULT_TITLE,
                           "tags": j.get("tags") or ONCE_TAGS_OVERRIDE.get(chat_id) or ONCE_TAGS.split("|"),
                           "task": _norm_task(j.get("task") or "") if j.get("task") else (ONCE_TASK_OVERRIDE.get(chat_id) or ONCE_TASK),
                           "republish": f in _done})
        if not _items:
            reply(chat_id, "\u6ca1\u8ba4\u51fa\u8981\u53d1\u54ea\u51e0\u6761\uff0c\u8bf4\u5e8f\u53f7\u5427\u3002"); return True
        if any(not j.get("task") for j in _jobs):
            ONCE_MODE[chat_id] = "\u661f\u56fe"
        ONCE_PENDING[chat_id] = {"items": _items}
        _render_pending(chat_id)
        return True
    if act == "set_item_task":
        _its = a.get("items") or []
        if not (ONCE_PENDING.get(chat_id) or {}).get("items") and _its:
            _names = ONCE_LIST.get(chat_id) or []
            if not _names:
                try:
                    _names, _ = _once_queue(); ONCE_LIST[chat_id] = _names
                except Exception:
                    _names = []
            _idxs = sorted({int(i.get("index")) for i in _its
                            if str(i.get("index") or "").isdigit() and 1 <= int(i["index"]) <= len(_names)})
            if _idxs:
                _pos = {orig: pos for pos, orig in enumerate(_idxs, 1)}
                _remap = [{"index": _pos[int(i["index"])], "task": i.get("task")}
                          for i in _its if str(i.get("index") or "").isdigit() and int(i["index"]) in _pos]
                select_once_item(chat_id, _idxs, text, _remap); return True
        set_item_tasks(chat_id, _its); return True
    if act == "set_mode":
        m = "\u661f\u56fe" if "\u661f\u56fe" in (a.get("mode") or text) else "\u666e\u901a"
        set_once_mode(chat_id, m); return True
    if act == "set_task":
        set_once_task(chat_id, "\u4efb\u52a1 " + (a.get("task") or "")); return True
    if act == "set_title":
        set_once_title(chat_id, "\u6807\u9898 " + (a.get("title") or "")); return True
    if act == "confirm":
        confirm_once_publish(chat_id); return True
    if act == "cancel":
        ONCE_PENDING.pop(chat_id, None); reply(chat_id, "\u5df2\u53d6\u6d88\u3002"); return True
    if act in ACTIONS:
        handle(chat_id, ACTIONS[act]); return True
    return False


def handle(chat_id, text):
    _hist_add(chat_id, "用户", text)
    # Multitenant chats must never touch the legacy SSH tunnel below: that
    # tunnel points at a machine that has been offline since 2026-10-20 and
    # resets on connect, which killed the handler before any reply.
    if MULTITENANT:
        return handle_multitenant(chat_id, text)
    if _compact_text(text) in MT_CANCEL_WORDS:
        return cancel_batch(chat_id)
    pending, expired = take_pending_publish(chat_id)
    if expired:
        return reply(chat_id, "\u8bdd\u9898\u786e\u8ba4\u5df2\u8d85\u65f6\uff0c\u8bf7\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u3002")
    if pending:
        return handle_pending_reply(chat_id, text, pending)
    if text == "1" or str(text).startswith("\u53d1\u5e03"):
        return request_topics(chat_id, text)
    _once_cmd = re.sub(r"\s+", "", text or "")
    _pub_words = ("\u53d1\u5e03", "1", "\u53d1\u89c6\u9891", "\u6211\u8981\u53d1\u89c6\u9891",
                  "\u53d1\u4e00\u6761", "\u53d1\u65b0\u89c6\u9891", "\u4e0a\u65b0", "\u51fa\u7247")
    _raw = re.sub(r"\s+", " ", (text or "")).strip()
    _looks_pick = bool(re.fullmatch(r"[\d\u7b2c\u6761\u4ef6\u3001,\uff0c\-\s\u548c\u8ddf\u4e0e/\uff0f\u5168\u90e8\u90fd\u53d1]+", _raw))
    if _looks_pick and (re.search(r"\d", _raw) or _raw in ("\u5168\u90e8", "\u90fd\u53d1", "\u5168\u53d1")):
        if chat_id not in ONCE_LIST:
            try:
                _names, _ = _once_queue()
                ONCE_LIST[chat_id] = _names
            except Exception:
                ONCE_LIST[chat_id] = []
        _idxs = _parse_pick(_raw, len(ONCE_LIST.get(chat_id) or []))
        if _idxs:
            return select_once_item(chat_id, _idxs, text)
    if _once_cmd.startswith("\u6807\u9898"):
        return set_once_title(chat_id, text)
    if _once_cmd.startswith("\u4efb\u52a1") or _once_cmd.startswith("\u661f\u56fe\u4efb\u52a1"):
        return set_once_task(chat_id, text)
    if _once_cmd in ("\u666e\u901a", "\u666e\u901a\u53d1\u5e03", "\u661f\u56fe", "\u661f\u56fe\u53d1\u5e03"):
        ONCE_AWAIT_MODE.pop(chat_id, None)
        return set_once_mode(chat_id, text)
    if (ONCE_AWAIT_MODE.get(chat_id) or _once_state_get(chat_id).get("await_mode")) and _once_cmd in ("1", "2"):
        ONCE_AWAIT_MODE.pop(chat_id, None)
        return set_once_mode(chat_id, "\u661f\u56fe" if _once_cmd == "2" else "\u666e\u901a")
    if _once_cmd in _pub_words or (text or "").strip().startswith("\u53d1\u5e03"):
        return start_once_publish(chat_id, text)
    if _once_cmd in ("\u786e\u8ba4", "\u786e\u8ba4\u53d1\u5e03", "\u786e\u8ba4\u91cd\u53d1"):
        return confirm_once_publish(chat_id)
    if _once_cmd in ("\u786e\u8ba4\u4fee\u6539", "\u6539\u5427") and chat_id in ONCE_EDIT:
        return confirm_edit(chat_id)
    if _once_cmd in ("\u786e\u8ba4\u5220\u9664", "\u5220\u5427") and chat_id in ONCE_DELETE:
        return confirm_delete(chat_id)
    if _once_cmd in ("\u53d6\u6d88", "\u7b97\u4e86", "\u4e0d\u53d1\u4e86") and chat_id in ONCE_PENDING:
        ONCE_PENDING.pop(chat_id, None)
        reply(chat_id, "\u5df2\u53d6\u6d88\u3002")
        return

    if ("\u4e24\u4e2a\u53f7" in _once_cmd or "\u4e24\u4e2a\u8d26\u53f7" in _once_cmd) and ("\u6570\u636e" in _once_cmd or "\u8868" in _once_cmd):
        return analyze_data(chat_id)
    _spend_words = ("\u6d88\u8017", "\u6536\u76ca", "\u82b1\u4e86\u591a\u5c11", "\u661f\u56fe", "\u5206\u6210")
    if any(w in _once_cmd for w in _spend_words):
        return spend_data(chat_id)
    _table_words = ("\u8868\u683c", "excel", "Excel", "\u51fa\u8868", "\u62a5\u8868", "\u6570\u636e\u8868", "\u4e24\u4e2a\u53f7", "\u4e24\u4e2a\u8d26\u53f7")
    if any(w in _once_cmd for w in _table_words):
        threading.Thread(target=collect_and_report, args=(chat_id,), daemon=True).start()
        return
    _data_words = ("\u53d6\u6570", "\u62ff\u6570\u636e", "\u770b\u6570\u636e", "\u62c9\u6570\u636e")
    if any(w in _once_cmd for w in _data_words):
        return analyze_data(chat_id)
    _det_words = ("\u786e\u8ba4", "\u786e\u8ba4\u53d1\u5e03", "\u786e\u8ba4\u91cd\u53d1",
                  "\u53d6\u6d88", "\u7b97\u4e86", "\u666e\u901a", "\u661f\u56fe",
                  "\u83dc\u5355", "\u5e2e\u52a9", "help", "?")
    if _once_cmd not in _det_words and not (ONCE_AWAIT_MODE.get(chat_id) and _once_cmd in ("1", "2")):
        _a = llm_intent(text, _build_ctx(chat_id))
        if _a.get("action") and _a.get("action") != "chat":
            if _dispatch_llm(chat_id, _a, text):
                return
        elif _a.get("say"):
            reply(chat_id, _a["say"])
            return
    _txt = text or ""
    if "#" in _txt and any(k in _txt for k in ("\u53d1\u5e03", "\u4f5c\u54c1\u63cf\u8ff0", "\u661f\u56fe")):
        return start_once_publish(chat_id, _txt)
    republish, republish_expired = get_republish_candidate(chat_id)
    if republish_expired:
        clear_republish_candidate(chat_id)
    elif republish:
        if MULTITENANT and str(republish.get("mode") or "local") != "mt":
            clear_republish_candidate(chat_id)
            republish = None
    if republish:
        compact = re.sub(r"\s+", "", text or "")
        if compact == "\u786e\u8ba4\u91cd\u53d1":
            threading.Thread(target=do_republish, args=(chat_id,), daemon=True).start()
            return
        if compact in ("\u53d6\u6d88\u91cd\u53d1", "\u4e0d\u91cd\u53d1", "\u7b97\u4e86"):
            clear_republish_candidate(chat_id)
            reply(chat_id, "\u5df2\u53d6\u6d88\u8fd9\u6b21\u91cd\u53d1\u3002")
            return
    if re.sub(r"\s+", "", text or "") == "\u786e\u8ba4\u91cd\u53d1":
        reply(chat_id, "\u6ca1\u6709\u5f85\u786e\u8ba4\u7684\u91cd\u53d1\u3002\u5148\u53d1\u300c\u53d1\u5e03\u300d\u751f\u6210\u3002")
        return
    if MULTITENANT:
        pending, expired = None, False
    else:
        pending, expired = take_pending_publish(chat_id)
    if expired:
        reply(chat_id, "\u8bdd\u9898\u786e\u8ba4\u5df2\u8d85\u65f6\uff0c\u8bf7\u91cd\u65b0\u53d1\u300c\u53d1\u5e03\u300d\u3002")
        return
    if pending:
        handle_pending_reply(chat_id, text, pending)
        return
    if _is_cancel_publish_reply(text):
        latest = load_latest_batch(chat_id)
        if latest is not None or not MULTITENANT:
            cancel_batch(chat_id, latest.get("batch_id") if latest else None)
            return
    if MULTITENANT:
        return handle_multitenant(chat_id, text)
    if text in ("0", "\u83dc\u5355", "\u5e2e\u52a9", "help", "?"):
        reply(chat_id, HELP)
        return
    if text in ("6", "\u72b6\u6001", "status"):
        reply(chat_id, status_text())
        return
    if text in ("5", "\u51fa\u8868", "\u62a5\u8868", "\u6570\u636e\u8868"):
        reply(chat_id, "\u53bb\u5ba2\u6237\u673a\u53d6\u6570\u51fa\u8868\uff0c\u8dd1\u5b8c\u53d1\u4f60\u2026")
        threading.Thread(target=collect_and_report, args=(chat_id,), daemon=True).start()
        return
    if text == "4" or text.startswith("\u53d6\u6570"):
        reply(chat_id, "\u5f00\u59cb\u53d6\u6570\uff0c\u8dd1\u5b8c\u56de\u4f60\u2026")
        d = run_cli(["collect-all"], timeout=115)
        if d.get("exit_code") in (0, None, "0"):
            reply(chat_id, "\u53d6\u6570\u5df2\u5b8c\u6210\u3002")
        else:
            reply(chat_id, "\u53d6\u6570\u6ca1\u6709\u5b8c\u6210\uff1a%s"
                  % friendly_error(d.get("stderr") or d.get("stdout"),
                                   "\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002"))
        return
    if text in ("2", "3", "\u6539\u671f", "\u6682\u505c", "\u6392\u671f"):
        show_tasks(chat_id)
        return
    m = re.match(r"^\u6539\u671f\s*(\S+)\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})", text)
    if m:
        r = ops("time", m.group(1), m.group(2) + " " + m.group(3))
        if r.get("ok"):
            t = r.get("task") or {}
            reply(chat_id, "\u6539\u597d\u4e86\uff1a%s" % plan_text(t))
        else:
            reply(chat_id, "\u6539\u4e0d\u4e86\uff1a%s"
                  % friendly_error(r.get("message") or r.get("code") or str(r),
                                   "\u8bf7\u68c0\u67e5\u5e8f\u53f7\u548c\u65f6\u95f4\u3002"))
        return
    m = re.match(r"^\u6682\u505c\s*(\S+)", text)
    if m:
        r = ops("cancel", m.group(1))
        reply(chat_id, ("\u5df2\u6682\u505c\uff1a%s" % plan_text(r.get("task") or {})) if r.get("ok")
              else ("\u505c\u4e0d\u4e86\uff1a%s"
                    % friendly_error(r.get("message") or r.get("code") or str(r),
                                     "\u8bf7\u68c0\u67e5\u5e8f\u53f7\u3002")))
        return
    if text in ("\u786e\u8ba4\u53d1\u5e03", "\u786e\u8ba4"):
        latest = load_latest_batch(chat_id)
        if latest and latest.get("status") in ("pending", "confirmed", "running"):
            threading.Thread(target=do_confirm_batch,
                             args=(chat_id, latest.get("batch_id")),
                             daemon=True).start()
        else:
            threading.Thread(target=do_confirm, args=(chat_id,), daemon=True).start()
        return
    if text == "1" or text.startswith("\u53d1\u5e03"):
        request_topics(chat_id, text)
        return
    _a2 = llm_intent(text, _build_ctx(chat_id))
    if not _dispatch_llm(chat_id, _a2, text):
        reply(chat_id, _a2.get("say") or "\u6ca1\u592a\u542c\u61c2\uff0c\u80fd\u4e0d\u80fd\u518d\u8bf4\u4e00\u904d\uff1f")


MT_WORK_QUEUE = queue.Queue(maxsize=64)
MT_CANCEL_SIGNALS = set()
MT_CANCEL_CUTOFF = {}


def _claim_message(chat_id, message_id):
    if not message_id:
        return False
    path = os.path.join(MT_BATCH_DIR, 'dialog', 'message_claims.json')
    key = hashlib.sha256((str(chat_id) + ':' + str(message_id)).encode()).hexdigest()
    with MT_DIALOG_LOCK:
        try:
            with open(path, encoding='utf-8') as fh:
                state = json.load(fh)
        except FileNotFoundError:
            state = {}
        # Keep claims across restarts. Uncertain/crashed work is never auto-replayed.
        if key in state:
            return False
        state[key] = {'received_at': time.time()}
        _dialog_write(path, state)
        return True


def _submit_mt_work(chat_id, text, card=None):
    try:
        MT_WORK_QUEUE.put_nowait((chat_id, text, card, time.monotonic()))
    except queue.Full:
        reply(chat_id, '\u5904\u7406\u961f\u5217\u5df2\u6ee1\uff0c\u672a\u6267\u884c\uff0c\u8bf7\u7a0d\u540e\u53d1\u9001\u65b0\u6d88\u606f\u3002')


def _run_mt_work(work):
    chat_id, text, card, received = work
    try:
        if received <= MT_CANCEL_CUTOFF.get(chat_id, -1):
            return reply(chat_id, '\u6b64\u524d\u6392\u961f\u8bf7\u6c42\u5df2\u56e0\u53d6\u6d88\u800c\u5931\u6548\uff0c\u672a\u6267\u884c\u3002')
        if time.monotonic() - received > 30:
            return reply(chat_id, '\u8bf7\u6c42\u6392\u961f\u8d85\u65f6\uff0c\u672a\u6267\u884c\uff0c\u8bf7\u91cd\u65b0\u53d1\u9001\u3002')
        if card:
            return confirm_mt_batch(chat_id, card.get('batch_id'), card.get('digest'))
        return handle_multitenant(chat_id, text)
    except Exception as exc:
        print(json.dumps({'route': 'mt-worker', 'error': type(exc).__name__}), flush=True)
        return reply(chat_id, '\u5904\u7406\u5931\u8d25\uff0c\u672a\u786e\u8ba4\u5b8c\u6210\uff1b\u8bf7\u67e5\u8be2\u5f53\u524d\u8fdb\u5ea6\u3002')


def _mt_work_loop():
    while True:
        work = MT_WORK_QUEUE.get()
        try:
            _run_mt_work(work)
        finally:
            MT_WORK_QUEUE.task_done()


def on_message(data: P2ImMessageReceiveV1) -> None:
    try:
        ev = data.event
        msg = ev.message
        chat_id = msg.chat_id
        print("[msgtype] chat=%s type=%s" % (chat_id, msg.message_type), flush=True)
        try:
            with open(os.path.join(_HERE, "event_raw.log"), "a", encoding="utf-8") as _ef:
                _ef.write("%s chat=%s type=%s chat_type=%s content=%s\n" % (
                    time.strftime("%H:%M:%S"), chat_id, msg.message_type,
                    getattr(msg, "chat_type", ""), str(msg.content)[:200]))
        except Exception:
            pass
        text = ""
        if msg.message_type == "text":
            text = json.loads(msg.content).get("text", "")
        elif msg.message_type == "post":
            try:
                _c = json.loads(msg.content)
                _p = []
                for _l in (_c.get("content") or []):
                    for _g in _l:
                        if isinstance(_g, dict) and _g.get("text"):
                            _p.append(_g["text"])
                if not _p:
                    _q=[]
                    def _walk(o):
                        if isinstance(o,dict):
                            for k,v in o.items():
                                if k=="text" and isinstance(v,str): _q.append(v)
                                else: _walk(v)
                        elif isinstance(o,list):
                            for v in o: _walk(v)
                    _walk(_c)
                    _p=_q
                text = " ".join(_p)
            except Exception:
                text = ""
        if msg.message_type in ("image","file","audio","media","video","sticker","emotion"):
            reply(chat_id, "\u6536\u5230\uff08%s\uff09\u3002\u89c6\u9891\u8bf7\u653e\u8fdb\u684c\u9762\u300c\u5f85\u53d1\u5e03\u7d20\u6750\u300d\uff0c"
                           "\u7136\u540e\u53d1\u300c\u53d1\u89c6\u9891\u300d\uff1b\u56fe\u7247\u6211\u8fd9\u8fb9\u4e0d\u5904\u7406\u3002" % msg.message_type)
            return
        if msg.chat_type == "group":
            if "@_user_1" not in text and "mention" not in msg.content and not ev.mentions:
                return
        text = re.sub(r"@_user_\d+", "", text)
        text = text.replace("\u200b", "").replace("\ufeff", "").strip()
        text = re.sub(r"\s+", " ", text).strip()
        print(f"[msg] chat={chat_id} raw={text[:40]!r}", flush=True)
        if not text:
            reply(chat_id, "\u6ca1\u770b\u5230\u6587\u5b57\uff0c\u8bf4\u4e00\u53e5\u8981\u505a\u4ec0\u4e48\u5c31\u884c\uff08\u5982\u300c\u53d1\u89c6\u9891\u300d\uff09\u3002")
            return
        if MULTITENANT:
            if not _claim_message(chat_id, getattr(msg, "message_id", None)):
                return
            reply(chat_id, "\u6536\u5230\uff0c\u6b63\u5728\u6838\u5bf9\u3002")
            if _compact_text(text) in MT_CANCEL_WORDS:
                MT_CANCEL_SIGNALS.add(chat_id)
                cancel_mt_batch(chat_id)
            else:
                _submit_mt_work(chat_id, text)
        else:
            handle(chat_id, text)
    except Exception as e:
        request_id = uuid.uuid4().hex[:12]
        print(json.dumps({'request_id': request_id, 'route': 'on_message',
                          'error': type(e).__name__}), flush=True)
        if 'chat_id' in locals():
            try:
                reply(chat_id, '\u6d88\u606f\u5904\u7406\u5931\u8d25\uff0c\u672a\u786e\u8ba4\u5b8c\u6210\u3002\u7f16\u53f7\uff1a' + request_id)
            except Exception:
                pass


handler = None

if lark is not None:
    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message) \
        .register_p2_card_action_trigger(on_card_action).build()

def main():
    # Hold the OS lock for the process lifetime, including reconnects.
    import fcntl
    global _BOT_INSTANCE_LOCK
    _BOT_INSTANCE_LOCK = open(os.path.join(BASE, ".bot-instance.lock"), "a")
    try:
        fcntl.flock(_BOT_INSTANCE_LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("bot.py is already running")
    if lark is None:
        raise RuntimeError("lark_oapi is not installed")
    print(f"[boot] app={APP_ID}", flush=True)
    threading.Thread(target=_sched_worker, daemon=True).start()
    reconcile_interrupted_batches()
    reconcile_interrupted_mt_batches()
    if MULTITENANT:
        threading.Thread(target=_mt_work_loop, daemon=True, name="mt-dialog-worker").start()
        start_mt_result_relay()
        start_scheduled_dispatcher()
    ws = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler, log_level=lark.LogLevel.INFO)
    ws.start()

if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Compatibility aliases for deployment scripts / tests
# ---------------------------------------------------------------------------

resolve_customer = lookup_customer
customer_for_chat = lookup_customer
route_message = enqueue_for_chat
deliver_command = enqueue_for_chat
