"""Control-plane publish preparation and confirmation on the customer machine.

The control plane never gets a shell.  A remote ``publish`` command is one of
two fixed operations:

``prepare``
    scan the customer's own inbox, build plans and dry-run previews, and return
    the plan paths plus confirmation tokens to the bot.

``confirm``
    write the bot-signed receipt into the customer's run directory and invoke
    the normal ``publish --mode approved_publish`` CLI with the normal gates.

No command text is evaluated.  File paths are confined to the customer data
area, and the existing receipt / license / freeze / canary checks stay in the
same CLI path used by the single-customer deployment.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import random
import re
import stat
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .idempotency import material_fingerprint
from .jsonio import read_json, write_json
from .secure_io import (assert_no_reparse_chain, copy_hashed_file,
                        is_within, sha256_file, secure_stat, secure_open)
from .subprocess_text import hidden_window_options

VIDEO_SUFFIXES = (".mp4", ".mov")
CN_TZ = timezone(timedelta(hours=8))


def _layout(config):
    return config.layout.ensure()


def _inside(path, root) -> bool:
    try:
        Path(path).resolve(strict=False).relative_to(
            Path(root).resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _safe_path(value, root, *, suffix=".json"):
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute() or not _inside(path, root):
        raise ValueError("path is outside the customer data area")
    path = path.resolve(strict=False)
    if suffix and path.suffix.lower() != suffix:
        raise ValueError("unexpected file type")
    return path


def _owned_drop_file(layout, value, root_dir, *, strict=False):
    """Own regular file inside one publish root (inbox or desktop folder)."""
    raw = Path(str(value or "")).expanduser()
    if not raw.is_absolute():
        return None
    base = Path(os.path.abspath(os.fspath(root_dir)))
    path = Path(os.path.abspath(os.fspath(raw)))
    if not is_within(path, base):
        return None
    inbox = Path(os.path.abspath(os.fspath(layout.inbox)))
    if is_within(path, inbox):
        return _owned_inbox_file(layout, path, strict=strict)
    try:
        assert_no_reparse_chain(path, root=base)
        st = secure_stat(path, root=base)
    except OSError as exc:
        if strict and exc.errno not in (errno.ELOOP, errno.EINVAL):
            raise
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
        return None
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        return None
    return path


def _owned_inbox_file(layout, value, *, strict=False):
    """Return one own regular inbox entry without following any link.

    The path is kept lexical.  Resolving it here would make every later
    reader follow a link that an attacker installs after this check.  The
    actual read path uses :func:`secure_io.secure_open` instead.
    """
    raw = Path(str(value or "")).expanduser()
    if not raw.is_absolute():
        return None
    layout.validate()
    root = Path(os.path.abspath(os.fspath(layout.root)))
    inbox = Path(os.path.abspath(os.fspath(layout.inbox)))
    path = Path(os.path.abspath(os.fspath(raw)))
    if not is_within(inbox, root) or not is_within(path, inbox):
        return None
    try:
        assert_no_reparse_chain(path, root=root)
        st = secure_stat(path, root=root)
    except OSError as exc:
        if strict and exc.errno not in (errno.ELOOP, errno.EINVAL):
            raise
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
        return None
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        return None
    return path


def _publish_roots(layout, *, strict=False):
    """Every folder a customer may drop publish material into.

    The desktop ``\u53d1\u5e03\u7d20\u6750`` folder is the folder customers actually use;
    the data inbox stays supported for existing installs.
    """
    roots = []
    seen = set()
    try:
        roots.append(Path(os.path.abspath(os.fspath(layout.inbox))))
    except OSError:
        if strict:
            raise
    try:
        from .desktop import materials_dir
        folder = materials_dir()
        if folder is not None and Path(folder).is_dir():
            roots.append(Path(os.path.abspath(os.fspath(folder))))
    except Exception:
        if strict:
            raise
    out = []
    for root in roots:
        key = os.path.normcase(str(root))
        if key in seen:
            continue
        seen.add(key)
        out.append(root)
    return out


def _scan_inbox(layout, *, strict=False):
    layout.validate()
    files = []
    seen = set()
    seen_keys = set()
    for inbox in _publish_roots(layout, strict=strict):
        try:
            entries = list(inbox.iterdir())
        except FileNotFoundError:
            continue
        except OSError:
            if strict:
                raise
            continue
        for path in entries:
            try:
                owned = _owned_drop_file(layout, path, inbox, strict=strict)
                if owned is None or owned in seen:
                    continue
                st = secure_stat(owned, root=inbox)
                if not stat.S_ISREG(st.st_mode):
                    continue
                if owned.name.startswith(".") or owned.name.startswith("~$"):
                    continue
                if st.st_size <= 0:
                    continue
                key = (owned.name.lower(), int(st.st_size))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                seen.add(owned)
                files.append((owned, st.st_mtime))
            except OSError:
                if strict:
                    raise
                continue
    files.sort(key=lambda item: (item[1], item[0].name.lower()))
    return [item[0] for item in files]


def _pick_files(layout, payload):
    layout.validate()
    wanted = payload.get("files")
    if not isinstance(wanted, list) or not wanted:
        return _scan_inbox(layout)
    roots = _publish_roots(layout)
    out = []
    for value in wanted:
        path = None
        root = None
        for candidate in roots:
            try:
                found = _owned_drop_file(layout, value, candidate)
            except (OSError, ValueError):
                found = None
            if found is not None:
                path, root = found, candidate
                break
        if path is None:
            continue
        try:
            st = secure_stat(path, root=root)
            if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
                continue
            out.append((path, st.st_mtime))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda item: (item[1], item[0].name.lower()))
    return [item[0] for item in out]


def _materials_digest(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True,
                     separators=(",", ":"), allow_nan=False).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _materials_stamp(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
            st.st_ctime_ns, st.st_mode)


def _account_rows(layout):
    """这台机器上的抖音号：环境 + 真昵称 + 是否真的登录过。

    environments.json 里声明的空环境（从没登录过、profile 目录是空的）
    不算账号——机器人以前按声明个数报「登录了 N 个号」，才会反复问客户。
    """
    from .account_names import load as load_names
    from .platforms import load_registry
    try:
        envs, _channels = load_registry(layout)
    except Exception:
        return []
    names = load_names(layout)
    rows = []
    for env in envs:
        if not getattr(env, "enabled", True):
            continue
        if "douyin" not in (getattr(env, "platforms", None) or []):
            continue
        # 登录态目录有两个口径：客户目录下的 profiles（remote_ops 拿到的
        # layout）和 agent 根目录下的 profiles（浏览器实际使用的那个）。
        # 只看前者会把已登录的号误判成没登录，所以两处都看，非空即算登过。
        logged_in = False
        _roots = [Path(layout.profiles)]
        _parent = Path(layout.profiles).parent
        if _parent.name.lower() == "customers" and _parent.parent.name:
            _roots.append(_parent.parent / "profiles")
        for _root in _roots:
            try:
                _dir = _root / env.profile_name
                if _dir.is_dir() and any(_dir.iterdir()):
                    logged_in = True
                    break
            except OSError:
                continue
        cached = names.get(env.env_id)
        cached = cached if isinstance(cached, dict) else {}
        nickname = str(cached.get("nickname") or "").strip()
        rows.append({
            "env_id": env.env_id,
            "channel_id": env.env_id + ":douyin",
            "account_id": env.env_id + ":douyin",
            "platform": "douyin",
            "label": env.label,
            "nickname": nickname or env.nickname or env.label,
            "nickname_verified": bool(nickname),
            "logged_in": bool(logged_in),
        })
    return rows


def materials(config, payload=None, command_id=None):
    """Read a complete, stable snapshot. Never ensure/create a data directory."""
    payload = payload if isinstance(payload, dict) else {}
    schema = "publish-materials/1"
    customer = str(getattr(config, "customer_id", "") or "")
    device = str(getattr(config, "device_id", "") or "")
    epoch = getattr(config, "binding_epoch", None)
    failure = {"ok": False, "schema": schema, "customer_id": customer,
               "device_id": device or None,
               "complete": False, "truncated": False}
    if payload.get("schema", schema) != schema:
        return dict(failure, error="unsupported_materials_schema")
    if not customer or payload.get("customer_id", customer) != customer:
        return dict(failure, error="materials_customer_mismatch")
    requested_device = str(payload.get("device_id") or "").strip()
    if requested_device and requested_device != device:
        return dict(failure, error="materials_device_mismatch")
    try:
        layout = config.layout
        if layout.customer_id != customer:
            raise ValueError("customer layout mismatch")
        layout.validate()
        roots = _publish_roots(layout, strict=True)
        for root in roots:
            assert_no_reparse_chain(root, root=root)
            # Missing roots are empty; inaccessible roots must not look empty.
            try:
                list(root.iterdir())
            except FileNotFoundError:
                pass
        started = time.time()
        files = _scan_inbox(layout, strict=True)
        rows, stamps = [], []
        for path in files:
            root = next(r for r in roots if is_within(path, r))
            before = secure_stat(path, root=root)
            digest = hashlib.sha256()
            size = 0
            with secure_open(path, root=root) as handle:
                if _materials_stamp(os.fstat(handle.fileno())) != _materials_stamp(before):
                    raise ValueError("material changed before hashing")
                while True:
                    block = handle.read(1 << 20)
                    if not block:
                        break
                    size += len(block)
                    digest.update(block)
                after = os.fstat(handle.fileno())
            stamp = _materials_stamp(before)
            if (stamp != _materials_stamp(after) or size != before.st_size
                    or stamp != _materials_stamp(secure_stat(path, root=root))):
                raise ValueError("material changed during hashing")
            name = "inbox" if is_within(path, layout.inbox) else "desktop"
            canonical = os.path.normcase(os.path.abspath(os.fspath(path)))
            sha = digest.hexdigest()
            identity = [customer, device, canonical, before.st_size,
                        before.st_mtime_ns, sha]
            rows.append({"material_id": _materials_digest(identity), "root": name,
                         "path": str(path), "name": path.name,
                         "size": size, "mtime": before.st_mtime, "sha256": sha,
                         "is_file": True, "is_symlink": False})
            stamps.append((path, root, stamp))
        if roots != _publish_roots(layout, strict=True) or files != _scan_inbox(layout, strict=True):
            raise ValueError("material set changed; refresh snapshot")
        for path, root, stamp in stamps:
            if stamp != _materials_stamp(secure_stat(path, root=root)):
                raise ValueError("material changed; refresh snapshot")
        if rows != sorted(rows, key=lambda r: (r["mtime"], r["name"].lower())):
            raise ValueError("material order changed; refresh snapshot")
        if time.time() >= started + 1800:
            raise ValueError("snapshot expired during hashing")
        root_map = {"inbox": str(roots[0]),
                    "desktop": str(roots[1]) if len(roots) > 1 else ""}
        # accounts 只出一个键：带真是否登录 + 真实昵称的那份（以前这里是
        # 声明清单，未登录的空环境也会被算成账号）。
        return {"ok": True, "schema": schema, "customer_id": customer,
                "accounts": _account_rows(layout),
                "device_id": device or None,
                "binding_epoch": epoch,
                "snapshot_id": _materials_digest(
                    [schema, customer, device, root_map, rows]),
                "expires_at": int(started) + 1800, "roots": root_map,
                "complete": True, "truncated": False, "next_cursor": None,
                "total": len(rows), "materials": rows}
    except (OSError, ValueError, StopIteration) as exc:
        return dict(failure, error="materials_snapshot_unavailable", message=str(exc))


def _topics(value):
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        topic = str(item or "").strip()
        if not topic:
            continue
        if not topic.startswith("#"):
            topic = "#" + topic.lstrip("#")
        if topic not in out:
            out.append(topic)
    return out


class NeedAccount(ValueError):
    """Raised when several douyin accounts exist and none was chosen."""

    def __init__(self, accounts):
        self.accounts = list(accounts or [])
        super().__init__("multiple_douyin_accounts")


def _target(envs, channels, payload):
    account_id = str(payload.get("account_id") or "").strip()
    channel_id = str(payload.get("channel_id") or "").strip()
    env_id = str(payload.get("env_id") or "").strip()
    channel = None
    if channel_id:
        channel = next((c for c in channels if c.channel_id == channel_id), None)
    if channel is None and account_id:
        channel = next((c for c in channels if c.account_id == account_id), None)
    if channel is None and env_id:
        channel = next((c for c in channels if c.env_id == env_id), None)
    if channel is None:
        candidates = [c for c in channels
                      if c.platform == "douyin" and c.enabled]
        if len(candidates) > 1:
            raise NeedAccount([
                {"env_id": c.env_id, "channel_id": c.channel_id,
                 "account_id": c.account_id, "nickname": c.nickname}
                for c in candidates])
        channel = candidates[0] if candidates else None
    if channel is None:
        raise ValueError("no enabled douyin channel is configured")
    env = next((e for e in envs if e.env_id == channel.env_id), None)
    if env is None:
        raise ValueError("channel environment is not configured")
    return env, channel


def _preview_for(layout, plan, settings, envs, channels, now):
    from .dryrun import build_preview
    from .cli import _licence_gate
    license_info = _licence_gate(layout, plan=plan)
    return build_preview(plan, envs=envs, channels=channels, settings=settings,
                         license_info=license_info, now=now)


DEFAULT_PUBLISH_TITLE = "\u706b\u9f99\u6f2b\u5267\u514d\u8d39\u770b"
TITLE_LIMIT = 30


def _clamp_title(value, limit=TITLE_LIMIT):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > limit:
        text = text[:limit].rstrip("\uff0c,\u3002.\u3001;\uff1b:\uff1a!\uff01?\uff1f~\uff5e-\u2014_ ")
    return text


def _title_from_path(path):
    """Derive a platform-legal title from a material file name.

    Material names are not titles: a content hash or a long descriptive name
    exceeds the 30-character limit and dry-run rejects the whole batch with
    TITLE_TOO_LONG.  Unreadable names fall back to the operator default;
    readable Chinese names are kept but clamped.
    """
    stem = str(getattr(path, "stem", path) or "").strip()
    if (not stem
            or re.fullmatch(r"[0-9a-fA-F]{16,64}", stem)
            or not re.search(r"[\u4e00-\u9fff]", stem)
            or len(stem) > TITLE_LIMIT):
        return DEFAULT_PUBLISH_TITLE
    return _clamp_title(stem) or DEFAULT_PUBLISH_TITLE


def _item_title(path, override):
    """One title for every item in a batch; the operator's word wins."""
    if override:
        return _clamp_title(override) or DEFAULT_PUBLISH_TITLE
    return _title_from_path(path)


def prepare(config, payload=None, command_id=None):
    """Build plans + dry-run previews from this customer's inbox."""
    payload = payload if isinstance(payload, dict) else {}
    layout = _layout(config)
    from .platforms import load_registry
    from .settings import load_settings

    settings = load_settings(layout.settings_file)
    envs, channels = load_registry(layout)
    try:
        env, channel = _target(envs, channels, payload)
    except NeedAccount as need:
        names = ["%d) %s" % (i + 1, a.get("nickname") or a.get("account_id"))
                 for i, a in enumerate(need.accounts)]
        return {"ok": False, "stage": "prepare", "error": "need_account",
                "accounts": need.accounts,
                "message": "这台电脑登录了 %d 个抖音号，发哪个？%s"
                           % (len(need.accounts), "  ".join(names))}
    try:
        files = _pick_files(layout, payload)
    except (OSError, ValueError) as exc:
        return {"ok": False, "stage": "prepare", "error": "unsafe_inbox_path",
                "message": str(exc)}
    if not files:
        return {"ok": False, "stage": "prepare", "error": "inbox_empty",
                "message": "inbox has no publishable .mp4/.mov files"}
    if len(files) > int(payload.get("max_items") or 50):
        return {"ok": False, "stage": "prepare", "error": "too_many_items",
                "message": "too many inbox items for one batch"}

    batch_id = str(payload.get("batch_id") or "").strip()
    if not batch_id:
        batch_id = time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    batch_id = "".join(c if (c.isalnum() or c in "-_") else "_" for c in batch_id)[:64]
    title_override = _clamp_title(payload.get("title") or "")
    # Covers are the operator's own branded images, never platform candidates.
    # A relative value is resolved against the data root so the bot can send
    # one device-independent path ("covers/cover_v.jpg").  A requested cover
    # that cannot be found is a hard stop, never a silent fallback.
    cover_paths = {}
    cover_digests = {}
    covers_payload = payload.get("covers")
    if isinstance(covers_payload, dict):
        for kind in ("vertical", "horizontal"):
            value = str(covers_payload.get(kind) or "").strip()
            if not value:
                continue
            cover_path = Path(value)
            if not cover_path.is_absolute():
                cover_path = Path(layout.root) / value
            try:
                cover_path = cover_path.resolve()
            except OSError:
                pass
            if not cover_path.is_file():
                return {"ok": False, "stage": "prepare", "error": "cover_missing",
                        "message": "封面文件不存在（%s）：%s" % (kind, value)}
            cover_paths[kind] = str(cover_path)
            cover_digests[kind] = sha256_file(cover_path)
    topics = _topics(payload.get("topics"))
    item_topics = payload.get("item_topics")
    now = datetime.now(CN_TZ)
    sched = settings.get("schedule") or {}
    try:
        gap_min = max(1, int(sched.get("gap_min", 5)))
        gap_max = max(gap_min, int(sched.get("gap_max", 8)))
    except (TypeError, ValueError):
        gap_min, gap_max = 5, 8
    rng = random.SystemRandom()

    # 1.2.153: schedule into the next legal window instead of now+30s, which
    # always tripped SCHEDULE_OUT_OF_WINDOW when min_lead_minutes > 0.
    from .schedule import next_slot, pick_slots
    try:
        planned = pick_slots(len(files), now, settings=settings)
    except Exception:
        planned = []
    items = []
    for index, path in enumerate(files, 1):
        slot = planned[index - 1] if len(planned) >= index else None
        if slot is None:
            slot = next_slot(now, settings=settings)
        schedule_at = slot.replace(microsecond=0).isoformat()
        title = _item_title(path, title_override)
        item_topic = topics
        if isinstance(item_topics, dict):
            value = item_topics.get(str(index), item_topics.get(index))
            if isinstance(value, list):
                item_topic = _topics(value)
        elif isinstance(item_topics, list) and len(item_topics) >= index:
            value = item_topics[index - 1]
            if isinstance(value, list):
                item_topic = _topics(value)
        item_dir = layout.run / "remote" / batch_id / ("item_%02d" % index)
        source_dir = item_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        staged_source = source_dir / path.name
        allowed_root = layout.root
        try:
            from .desktop import materials_dir as _materials_dir
            _folder = _materials_dir()
            if _folder is not None and _inside(path, Path(_folder)):
                allowed_root = Path(_folder)
        except Exception:
            pass
        try:
            digest, _size = copy_hashed_file(
                path, staged_source, root=allowed_root)
        except OSError as exc:
            return {"ok": False, "stage": "prepare",
                    "error": "unsafe_inbox_path", "message": str(exc)}
        piece = {
            "title": title,
            "copy_body": title,
            "topics": item_topic,
            "file_path": str(staged_source),
            "file_name": path.name,
            "sha256": digest,
            "cover_sha256": dict(cover_digests),
            "covers": {"vertical": cover_paths.get("vertical"),
                       "horizontal": cover_paths.get("horizontal")},
            "platform_account_id": channel.account_id,
        }
        content_hash = material_fingerprint(piece, account_id=channel.account_id)
        plan = {
            "plan_id": "remote-%s-%03d" % (batch_id, index),
            "env_id": env.env_id,
            "channel_id": channel.channel_id,
            "account_id": channel.account_id,
            "platform": channel.platform,
            "schedule_at": schedule_at,
            "piece": piece,
            "compliance": {"content_hash": content_hash},
        }
        plan_path = item_dir / "plan.json"
        preview_path = item_dir / "preview.json"
        preview_md_path = item_dir / "preview.md"
        write_json(plan_path, plan)
        preview = _preview_for(layout, plan, settings, envs, channels, now)
        if preview.get("ok"):
            write_json(preview_path, preview)
            from .dryrun import render_markdown
            preview_md_path.write_text(render_markdown(preview), encoding="utf-8")
        items.append({
            "index": index,
            "name": path.name,
            "title": title,
            "topics": item_topic,
            "plan_path": str(plan_path),
            "preview_path": str(preview_path),
            "preview_md_path": str(preview_md_path),
            "confirm_token": preview.get("confirm_token") if preview.get("ok") else None,
            "plan_id": plan["plan_id"],
            "content_hash": content_hash,
            "schedule_at": schedule_at,
            "plan_data": plan,
            "preview_ok": bool(preview.get("ok")),
            "problems": preview.get("problems") or [],
        })
    failed = [item for item in items if not item["preview_ok"]]
    return {
        "ok": not failed,
        "stage": "prepared",
        "batch_id": batch_id,
        "count": len(items),
        "gap_min": gap_min,
        "gap_max": gap_max,
        "items": items,
        "problems": [{"item": item["index"], "problems": item["problems"]}
                     for item in failed],
        "command_id": command_id,
    }


def _run_cli(config, argv, timeout, runner=None):
    runner = runner or subprocess.run
    env = os.environ.copy()
    layout = _layout(config)
    env["WINPORT_DATA"] = str(layout.root)
    env["WINPORT_SYSTEM"] = str(layout.system)
    env["CUSTOMER_ID"] = config.customer_id
    if config.token:
        env["CUSTOMER_TOKEN"] = config.token
    if config.program_dir:
        env["WINPORT_PROGRAM"] = str(config.program_dir)
    return runner(argv, capture_output=True, text=True,
                  encoding="utf-8", errors="replace", timeout=timeout,
                  env=env, cwd=config.cli_root or None, shell=False,
                  **hidden_window_options(argv))


def _payload_flag(value) -> bool:
    """Accept only an explicit confirmation flag, never a truthy string."""
    if value is True:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def confirm(config, payload=None, command_id=None, runner=None):
    """Write the signed receipt and run the normal approved-publish CLI."""
    payload = payload if isinstance(payload, dict) else {}
    layout = _layout(config)
    if not payload.get("receipt"):
        return {"ok": False, "stage": "confirm", "error": "receipt_missing"}
    plan_path = _safe_path(payload.get("plan"), layout.root)
    preview_path = _safe_path(payload.get("preview"), layout.root)
    token = str(payload.get("confirm_token") or "")
    if not token:
        return {"ok": False, "stage": "confirm", "error": "confirm_token_missing"}
    plan_data = read_json(plan_path, default=None)
    if isinstance(plan_data, dict):
        requested_device = str(plan_data.get("device_id") or "").strip()
        own_device = str(getattr(config, "device_id", "") or "").strip()
        if requested_device and requested_device != own_device:
            return {"ok": False, "stage": "confirm",
                    "error": "plan_device_mismatch"}
        requested_epoch = plan_data.get("device_binding_epoch",
                                        plan_data.get("binding_epoch"))
        if requested_epoch is not None:
            try:
                if int(requested_epoch) != int(getattr(config, "binding_epoch", 0)):
                    return {"ok": False, "stage": "confirm",
                            "error": "plan_epoch_mismatch"}
            except (TypeError, ValueError):
                return {"ok": False, "stage": "confirm",
                        "error": "plan_epoch_invalid"}
    command_part = re.sub(r"[^A-Za-z0-9._-]", "_",
                          str(command_id or ""))[:80]
    if not command_part or command_part in (".", ".."):
        command_part = uuid.uuid4().hex
    receipt_dir = layout.run / "remote-confirm" / command_part
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_dir / "receipt.json"
    write_json(receipt_path, payload.get("receipt"))

    workspace = Path(config.cli_root) if config.cli_root else None
    argv = [config.cli_python, "-m", "agent.winport.cli", "publish",
            "--data", str(layout.root), "--mode", "approved_publish",
            "--plan", str(plan_path), "--preview", str(preview_path),
            "--confirm-token", token, "--receipt", str(receipt_path)]
    # The two real-publish switches are never implied by ``confirm``.  The
    # control plane must carry the explicit, already-verified confirmation
    # fields; otherwise the CLI stays on its normal preview/approval path.
    allow_submit = _payload_flag(payload.get("allow_submit"))
    canary_armed = _payload_flag(payload.get("canary_armed"))
    if allow_submit:
        argv.append("--allow-submit")
    if canary_armed:
        argv.append("--canary-armed")
    if workspace:
        argv += ["--program", str(workspace)]
    if payload.get("republish_confirmed"):
        argv.append("--republish-confirmed")
    timeout = payload.get("timeout", config.command_timeout)
    try:
        timeout = max(1.0, min(float(timeout), 3600.0))
    except (TypeError, ValueError):
        timeout = config.command_timeout
    try:
        proc = _run_cli(config, argv, timeout, runner=runner)
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "stage": "confirm", "error": "timeout",
                "argv": argv, "stdout": exc.stdout or "", "stderr": exc.stderr or ""}
    except Exception as exc:  # noqa: BLE001 - reported to the control plane
        return {"ok": False, "stage": "confirm", "error": str(exc), "argv": argv}

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    parsed = {}
    try:
        value = json.loads(stdout)
        if isinstance(value, dict):
            parsed = value
    except ValueError:
        pass
    for line in reversed(stdout.splitlines()) if not parsed else []:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            parsed = value
            break
    # Never infer a publish from exit code + switches.  A zero exit with lost
    # stdout, an empty object, or {"ok": false, "error": ...} is unconfirmed,
    # not success.  Only the worker's explicit boolean proves publication.
    reported_error = parsed.get("error")
    parsed_failed = parsed.get("ok") is False or bool(reported_error)
    published = (proc.returncode == 0 and not parsed_failed
                 and parsed.get("published") is True)
    if proc.returncode != 0 or parsed_failed:
        status = "failed"
    elif published:
        status = "done"
    else:
        status = "needs_reconcile"
    ok = status == "done"
    return {
        "ok": ok,
        "status": status,
        "stage": "confirm",
        "exit_code": proc.returncode,
        "published": published,
        "platform_work_id": parsed.get("platform_work_id"),
        "allow_submit": allow_submit,
        "canary_armed": canary_armed,
        "result": parsed,
        "stdout": stdout[-20000:],
        "stderr": stderr[-20000:],
        "argv": argv,
    }
