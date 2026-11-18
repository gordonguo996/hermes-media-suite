"""Named operations the Feishu bot calls instead of shipping Python (1.2.221).

Until 1.2.220 the bot sent ``python -c <script>`` to the customer machine for
everyday work (INBOX_SCAN, PLAN_MAKER, the plan-time edit, the ledger unblock,
_MT_RESCHED, file listing for reports).  Those scripts are now product code:

* they run only what this release ships, so remote arbitrary execution can be
  off by default (see remote_policy);
* every path they touch is checked to stay inside the data area or the
  customer's material folders;
* they use the client's own helpers (``material_fingerprint``, ``Layout``)
  instead of bot-side copies that drifted -- PLAN_MAKER called
  ``title_from_file`` which only existed in the bot, so it raised NameError on
  every customer machine.

Each command takes one ``--args-json`` (ASCII JSON) and prints exactly one
JSON line, the same shape the bot already parses.  Output is ASCII-escaped so
no console code page can garble it.
"""

from __future__ import annotations

import datetime as _dt
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

VIDEO_SUFFIXES = (".mp4", ".mov")
MATERIAL_FOLDER = "\u53d1\u5e03\u7d20\u6750"          # 发布素材
DEFAULT_TITLE = "\u706b\u9f99\u6f2b\u5267\u514d\u8d39\u770b"  # 火龙漫剧免费看
TITLE_LIMIT = 30
CN_TZ = _dt.timezone(_dt.timedelta(hours=8))
RESCHED_TAG = "MT_RESCHED_RESULT="


class NamedOpError(ValueError):
    pass


# ------------------------------------------------------------------ helpers
def emit(payload, prefix=""):
    print(prefix + json.dumps(payload, ensure_ascii=True, sort_keys=False))


def parse_args_json(text):
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise NamedOpError("--args-json is not valid JSON") from exc
    if not isinstance(value, dict):
        raise NamedOpError("--args-json must be an object")
    return value


def _inside(path, root) -> bool:
    try:
        Path(path).resolve(strict=False).relative_to(Path(root).resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def data_file(layout, value, *, suffix=".json", must_exist=True) -> Path:
    """An absolute path inside the data area with the expected suffix."""
    path = Path(str(value or ""))
    if not path.is_absolute():
        raise NamedOpError("absolute path required: %r" % str(value))
    if not _inside(path, layout.data):
        raise NamedOpError("path is outside the data area: %s" % path)
    if suffix and path.suffix.lower() != suffix:
        raise NamedOpError("unexpected file type: %s" % path)
    if path.is_symlink():
        raise NamedOpError("symlink denied: %s" % path)
    if must_exist and not path.is_file():
        raise NamedOpError("file not found: %s" % path)
    return path.resolve(strict=False)


def _atomic_json(path: Path, doc) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, ensure_ascii=False, indent=1))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def clamp_title(text, limit=TITLE_LIMIT):
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) > limit:
        value = value[:limit].rstrip(
            "\uff0c,\u3002.\u3001;\uff1b:\uff1a!\uff01?\uff1f~\uff5e-\u2014_ ")
    return value


def title_from_file(path, default=DEFAULT_TITLE):
    """Same rule as the bot: hashes, non-Chinese and over-long names -> default."""
    base = os.path.splitext(os.path.basename(str(path or "")))[0].strip()
    if (not base or re.fullmatch(r"[0-9a-fA-F]{16,64}", base)
            or not re.search(r"[\u4e00-\u9fff]", base) or len(base) > TITLE_LIMIT):
        return clamp_title(default) or DEFAULT_TITLE
    return clamp_title(base) or clamp_title(default) or DEFAULT_TITLE


# ---------------------------------------------------------------- material
def material_roots(layout):
    """Folders the bot may publish from: data inboxes + desktop 发布素材."""
    base = Path(layout.data)
    roots = []

    def add(p):
        p = str(p)
        if p and p not in roots and os.path.isdir(p):
            roots.append(p)

    for d in sorted(glob.glob(os.path.join(str(base), "customers", "*", "inbox"))):
        add(d)
    add(base / "inbox")
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    candidates = [os.path.join(home, "Desktop", MATERIAL_FOLDER)]
    for env in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        value = os.environ.get(env)
        if value:
            candidates.append(os.path.join(value, "Desktop", MATERIAL_FOLDER))
    if os.name == "nt":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
            desktop = os.path.expandvars(winreg.QueryValueEx(key, "Desktop")[0])
            candidates.append(os.path.join(desktop, MATERIAL_FOLDER))
        except OSError:
            pass
    for c in candidates:
        add(c)
    return roots


def _schedule_gaps(layout):
    paths = [Path(layout.data) / "config" / "settings.json",
             Path(layout.data) / "settings.json"]
    paths += [Path(p) for p in glob.glob(
        os.path.join(str(layout.data), "customers", "*", "config", "settings.json"))]
    settings = {}
    for sp in paths:
        try:
            doc = json.loads(sp.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict):
            settings = doc
            break
    g = settings.get("schedule") if isinstance(settings.get("schedule"), dict) else {}

    def _int(v, d):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d
    lo, hi = _int(g.get("gap_min"), 5), _int(g.get("gap_max"), 8)
    lo = lo if 1 <= lo <= 120 else 5
    hi = hi if 1 <= hi <= 120 else 8
    return (hi, lo) if lo > hi else (lo, hi)


def inbox_scan(layout, args=None):
    files, seen = [], set()
    for root in material_roots(layout):
        for f in glob.glob(os.path.join(root, "*")):
            try:
                if not os.path.isfile(f) or os.path.islink(f):
                    continue
                if not f.lower().endswith(VIDEO_SUFFIXES):
                    continue
                name = os.path.basename(f)
                if name.startswith(".") or name.startswith("~$"):
                    continue
                size = os.path.getsize(f)
                if size <= 0 or (name.lower(), size) in seen:
                    continue
                seen.add((name.lower(), size))
                files.append({"path": f, "name": name,
                              "mtime": os.path.getmtime(f), "size": size})
            except OSError:
                continue
    files.sort(key=lambda x: (x["mtime"], x["name"].lower()))
    gap_min, gap_max = _schedule_gaps(layout)
    return {"ok": True, "files": files, "gap_min": gap_min, "gap_max": gap_max}


def _owned_material(layout, value, roots):
    path = Path(str(value or ""))
    if not path.is_absolute() or path.is_symlink():
        raise NamedOpError("material must be an absolute, real file: %s" % value)
    if not any(_inside(path, r) for r in roots):
        raise NamedOpError("material is outside the material folders: %s" % value)
    if not path.is_file() or not str(path).lower().endswith(VIDEO_SUFFIXES):
        raise NamedOpError("material missing or not a video: %s" % value)
    return str(path)


def _topics(raw):
    out = []
    for t in str(raw or "").replace("\uff03", "#").replace("\uff0c", " ").replace(",", " ").split():
        t = t.strip().strip("#").strip()
        if t and "#" + t not in out:
            out.append("#" + t)
    return out


def _sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plan_make(layout, args):
    """PLAN_MAKER as product code (single plan or one plan per batch item)."""
    from .idempotency import material_fingerprint

    base = Path(layout.data)
    roots = material_roots(layout)
    wanted = args.get("files") or []
    if not isinstance(wanted, list):
        raise NamedOpError("files must be a list")
    if wanted:
        vids, missing = [], []
        for item in wanted:
            p = item.get("path") if isinstance(item, dict) else item
            try:
                vids.append(_owned_material(layout, p, roots))
            except NamedOpError:
                missing.append(str(p))
        if missing:
            return {"ok": False, "err": "\u7d20\u6750\u4e0d\u5b58\u5728\u6216\u4e0d\u662f\u89c6\u9891",
                    "missing": missing}
    else:
        inbox = base / "inbox"
        vids = [str(f) for f in inbox.glob("*")
                if f.is_file() and not f.is_symlink() and f.suffix.lower() in VIDEO_SUFFIXES]
    if not vids:
        return {"ok": False, "err": "inbox \u91cc\u6ca1\u6709\u89c6\u9891"}
    vids.sort(key=lambda p: (os.path.getmtime(p), os.path.basename(p).lower()))
    batch = len(vids) > 1
    batch_id = str(args.get("batch_id") or (
        _dt.datetime.now().strftime("%Y%m%d%H%M%S") + "-" + os.urandom(4).hex()))
    batch_id = "".join(c if (c.isalnum() or c in "-_") else "_" for c in batch_id)[:64]
    topics = _topics(args.get("topics"))
    item_topics = args.get("item_topics") or []
    if not isinstance(item_topics, list):
        item_topics = []
    item_topics = item_topics + [None] * max(0, len(vids) - len(item_topics))
    xt_raw = args.get("xingtu_task") if isinstance(args.get("xingtu_task"), dict) else {}
    xingtu_task = {}
    if str(xt_raw.get("order_id") or "").strip() or str(xt_raw.get("task_name") or "").strip():
        xingtu_task = {k: str(xt_raw.get(k) or "") for k in
                       ("order_id", "task_name", "demander", "task_type",
                        "remaining_quota", "task_id", "upload_url")}
    publish_mode = "xingtu" if xingtu_task else "normal"
    env_id = str(args.get("env_id") or "env_1")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", env_id):
        raise NamedOpError("bad env_id")
    acct = env_id + ":douyin"
    account_name = ""
    try:
        envs = json.loads((base / "config" / "environments.json").read_text(encoding="utf-8-sig"))
        rows = envs.get("environments") if isinstance(envs, dict) else envs
        for row in rows or []:
            if str(row.get("env_id") or "") == env_id:
                account_name = str(row.get("label") or "")
                break
    except (OSError, ValueError, AttributeError):
        account_name = ""
    default_title = str(args.get("default_title") or DEFAULT_TITLE)
    now = _dt.datetime.now(CN_TZ)
    items = []
    for i, f in enumerate(vids):
        use_topics = item_topics[i]
        use_topics = list(topics if use_topics is None else (use_topics or []))
        title = title_from_file(f, default_title)
        copy = ""
        if not batch:
            title = clamp_title(args.get("title") or title) or title
            copy = str(args.get("copy") or "")
        h = _sha256_file(f)
        piece = {"title": title, "copy_body": copy, "topics": use_topics,
                 "file_path": f, "file_name": os.path.basename(f), "sha256": h,
                 "covers": {"vertical": None, "horizontal": None}}
        ch = material_fingerprint(piece, account_id=acct)
        sched = (now + _dt.timedelta(seconds=-10)).replace(microsecond=0).isoformat()
        plan_id = "bot-" + batch_id + ("-%03d" % (i + 1) if batch else "")
        plan = {"plan_id": plan_id, "env_id": env_id, "channel_id": acct,
                "account_id": acct, "platform": "douyin", "schedule_at": sched,
                "account_name": account_name, "publish_mode": publish_mode,
                "piece": piece, "compliance": {"content_hash": ch}}
        if xingtu_task:
            if xingtu_task.get("order_id"):
                plan["order_id"] = xingtu_task["order_id"]
            plan["xingtu_task"] = dict(xingtu_task)
        if batch:
            d = base / "run" / "batches" / batch_id / ("item_%02d" % (i + 1))
            d.mkdir(parents=True, exist_ok=True)
            p, preview, preview_md = d / "plan.json", str(d / "preview.json"), str(d / "preview.md")
        else:
            (base / "run").mkdir(parents=True, exist_ok=True)
            p, preview, preview_md = base / "run" / "plan_bot.json", "", ""
        _atomic_json(p, plan)
        items.append({"index": i + 1, "plan": str(p), "plan_id": plan_id, "title": title,
                      "file": f, "name": os.path.basename(f), "at": sched,
                      "sha256": h[:12], "content_hash": ch, "topics": use_topics,
                      "preview": preview, "preview_md": preview_md,
                      "account": account_name, "publish_mode": publish_mode,
                      "xingtu_task": dict(xingtu_task) if xingtu_task else None})
    out = {"ok": True, "batch_id": batch_id, "batch": batch, "count": len(items),
           "items": items, "topics": topics}
    if not batch:
        it = items[0]
        out.update({"plan": it["plan"], "title": it["title"], "file": it["name"],
                    "at": it["at"], "sha256": it["sha256"], "topics": it["topics"],
                    "account": it["account"], "publish_mode": publish_mode,
                    "xingtu_task": it["xingtu_task"]})
    return out


# -------------------------------------------------------------------- plans
def _iso(value) -> str:
    text = str(value or "").strip()
    try:
        _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NamedOpError("schedule_at is not ISO-8601: %r" % text) from exc
    return text


def plan_set_time(layout, args):
    path = data_file(layout, args.get("plan"))
    sched = _iso(args.get("schedule_at"))
    doc = json.loads(path.read_text(encoding="utf-8-sig"))
    doc["schedule_at"] = sched
    _atomic_json(path, doc)
    return {"ok": True, "plan_id": doc.get("plan_id"), "schedule_at": doc.get("schedule_at"),
            "content_hash": (doc.get("compliance") or {}).get("content_hash")}


def _parse_obj(text):
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return {}
    return {}


def _publish_python(layout) -> str:
    """The interpreter that has playwright: the worker venv, else this one."""
    venv = Path(layout.data) / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    for name in (("python.exe",) if os.name == "nt" else ("python3", "python")):
        if (venv / name).is_file():
            return str(venv / name)
    return sys.executable


def plan_reschedule(layout, args, *, runner=subprocess.run):
    """_MT_RESCHED as product code: rewrite the plan, then a fresh dry-run.

    The content hash comes from the client's own ``material_fingerprint`` (the
    bot used to re-implement it), and the program directory is this install
    (the bot used to guess it from Desktop folder names)."""
    from .idempotency import material_fingerprint
    from .paths import install_root

    plan_path = data_file(layout, args.get("plan"))
    preview_path = data_file(layout, args.get("preview"), must_exist=False)
    sched = _iso(args["schedule_at"]) if args.get("schedule_at") else ""
    plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    piece = plan.get("piece") or {}
    piece["platform_account_id"] = ""
    if args.get("topics") is not None:
        topics = args.get("topics")
        if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
            raise NamedOpError("topics must be a list of strings")
        piece["topics"] = topics
    plan["piece"] = piece
    plan.setdefault("compliance", {})["content_hash"] = material_fingerprint(
        piece, account_id=plan.get("account_id") or "")
    if sched:
        plan["schedule_at"] = sched
    _atomic_json(plan_path, plan)
    try:
        preview_path.unlink()
    except FileNotFoundError:
        pass
    data_root = plan_path
    for _ in range(5):
        data_root = data_root.parent
    if not _inside(data_root, layout.data):
        data_root = Path(layout.data)
    program = install_root()
    env = dict(os.environ, PYTHONPATH=str(program), PYTHONUTF8="1")
    argv = [_publish_python(layout), "-m", "agent.winport.cli", "dry-run",
            "--data", str(data_root), "--plan", str(plan_path), "--out", str(preview_path)]
    rp = runner(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=str(program), env=env, timeout=300)
    parsed = _parse_obj(rp.stdout)
    confirmation = (parsed or {}).get("confirmation") or {}
    fresh = confirmation.get("fresh") or {}
    try:
        preview = json.loads(preview_path.read_text(encoding="utf-8-sig"))
        preview_error = ""
    except (OSError, ValueError) as exc:
        preview, preview_error = None, str(exc)
    plan_after = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    tail = {"dryrun_exit": rp.returncode, "stderr": (rp.stderr or "")[-2000:],
            "stdout": (rp.stdout or "")[-2000:]}
    if not preview:
        return {"ok": False, "error": "dry-run \u672a\u751f\u6210 preview\uff1a" + preview_error, **tail}
    token = preview.get("confirm_token") or fresh.get("confirm_token") or ""
    confirm_ref = preview.get("confirm_ref") or fresh.get("confirm_ref") or ""
    if not token or not confirm_ref:
        return {"ok": False, "error": "dry-run preview \u7f3a\u5c11 confirm_token/confirm_ref", **tail}
    return {"ok": True,
            "plan_id": plan_after.get("plan_id") or "",
            "schedule_at": plan_after.get("schedule_at") or "",
            "content_hash": (plan_after.get("compliance") or {}).get("content_hash") or "",
            "preview_plan_id": preview.get("plan_id") or "",
            "preview_fingerprint": preview.get("material_fingerprint") or "",
            "preview_schedule_at": preview.get("schedule_at") or "",
            "preview_generated_at": preview.get("generated_at") or "",
            "confirm_token": token, "confirm_ref": confirm_ref,
            "dryrun_exit": rp.returncode,
            "problems": fresh.get("problems") or [],
            "warnings": fresh.get("warnings") or [],
            "reason": confirmation.get("reason") or fresh.get("reason") or "",
            "stderr": tail["stderr"], "stdout_tail": (rp.stdout or "")[-1000:]}


def ledger_unblock(layout, args=None):
    """Remove RESERVED rows (what the bot's auto-unblock removed), with backup."""
    from .stage2_ledger import RESERVED, Stage2Ledger

    path = Path(layout.stage2_ledger)
    if not path.exists():
        return {"ok": True, "path": str(path), "removed": {}, "backup": None}
    ledger = Stage2Ledger(str(path))
    with ledger.locked():
        entries = ledger.data.get("entries") or {}
        removed = {k: (e or {}).get("state") for k, e in entries.items()
                   if (e or {}).get("state") == RESERVED}
        backup = None
        if removed:
            backup = ledger.backup("bot auto unblock")
            for key in removed:
                entries.pop(key, None)
            ledger.save()
    return {"ok": True, "path": str(path), "removed": removed,
            "backup": str(backup) if backup else None}


# ------------------------------------------------------------------ exports
_EXPORT_DIRS = {"douyin": "douyin", "xingtu": "xingtu"}


def export_files(layout, args):
    """List today's exports the report needs (read them via the data-area read)."""
    kind = str(args.get("kind") or "")
    if kind not in _EXPORT_DIRS:
        raise NamedOpError("kind must be one of %s" % sorted(_EXPORT_DIRS))
    prefix = str(args.get("prefix") or "")
    if not prefix or any(c in prefix for c in "\\/*?[]") or ".." in prefix:
        raise NamedOpError("prefix must be a plain file-name prefix")
    folder = Path(layout.data) / "out" / _EXPORT_DIRS[kind]
    files = []
    if folder.is_dir():
        for f in sorted(folder.iterdir()):
            if (f.is_file() and not f.is_symlink() and f.name.startswith(prefix)
                    and f.suffix.lower() == ".json"):
                files.append({"name": f.name, "path": str(f), "size": f.stat().st_size})
    return {"ok": True, "folder": str(folder), "files": files}


def agent_health(layout, args=None):
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8795/health", timeout=5) as resp:
            body = resp.read(65536).decode("utf-8", "replace")
        try:
            return {"ok": True, "health": json.loads(body)}
        except ValueError:
            return {"ok": True, "health": body}
    except OSError as exc:
        return {"ok": False, "error": "agent health unreachable: %s" % exc}


OPERATIONS = {
    "inbox-scan": inbox_scan,
    "plan-make": plan_make,
    "plan-set-time": plan_set_time,
    "plan-reschedule": plan_reschedule,
    "ledger-unblock": ledger_unblock,
    "export-files": export_files,
    "agent-health": agent_health,
}


def dispatch(name, layout, args):
    """In-process entry for the relay / control-plane ``named`` operation."""
    if name not in OPERATIONS:
        return {"ok": False, "error": "unknown named operation: %s" % name,
                "err": "unknown named operation"}
    if not isinstance(args, dict):
        return {"ok": False, "error": "args must be an object", "err": "bad args"}
    try:
        return OPERATIONS[name](layout, args)
    except (NamedOpError, KeyError, OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "err": str(exc)}


def run(name, layout, args_json):
    """Entry used by the CLI; never raises, always prints one JSON line."""
    prefix = RESCHED_TAG if name == "plan-reschedule" else ""
    try:
        result = OPERATIONS[name](layout, parse_args_json(args_json))
    except (NamedOpError, KeyError, OSError, ValueError) as exc:
        result = {"ok": False, "error": str(exc), "err": str(exc)}
    emit(result, prefix)
    return 0 if result.get("ok") else 2
