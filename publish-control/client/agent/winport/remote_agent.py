"""Operator remote-operations channel over the control plane (HTTPS only).

The customer machine polls the control plane for operator requests and posts
results back. No inbound port, no SSH keys, no extra configuration on the
customer side: the same CUSTOMER_TOKEN used by agentpoll authenticates it.

Supported operations: ping, exec, read, write, ls.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request

from .subprocess_text import hidden_window_options

MAX_RESULT_BYTES = 200000
MAX_FILE_BYTES = 4 * 1024 * 1024


def _post(url, token, payload, timeout=30):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8") or "{}")


def _trim(text):
    raw = text.encode("utf-8", "replace")
    if len(raw) <= MAX_RESULT_BYTES:
        return text
    return raw[:MAX_RESULT_BYTES].decode("utf-8", "replace") + "\n[truncated]"


def _identity(config):
    if not getattr(config, "device_id", ""):
        return {}
    epoch = getattr(config, "binding_epoch", None)
    if epoch is None:
        raise ValueError("binding_epoch is required when device_id is set")
    return {"device_id": config.device_id, "binding_epoch": int(epoch)}


def _op_exec(body):
    cmd = body.get("cmd")
    if isinstance(cmd, str) and cmd.strip():
        args = cmd
        shell = True
    elif isinstance(cmd, list) and cmd and all(isinstance(x, str) for x in cmd):
        args = cmd
        shell = False
    else:
        raise ValueError("exec requires cmd (string or string list)")
    timeout = body.get("timeout")
    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 600:
        timeout = 60
    cwd = body.get("cwd") or None
    # Remote operator commands must never show a console window on the
    # customer's desktop; this channel is separate from remote_channel.execute.
    proc = subprocess.run(args, shell=shell, cwd=cwd, timeout=timeout,
                          capture_output=True,
                          **hidden_window_options(args))
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": _trim(proc.stdout.decode("utf-8", "replace")),
        "stderr": _trim(proc.stderr.decode("utf-8", "replace")),
    }


def _op_read(body):
    path = os.path.expandvars(os.path.expanduser(str(body.get("path") or "")))
    if not path:
        raise ValueError("read requires path")
    with open(path, "rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("file exceeds 4 MiB limit")
    return {"ok": True, "bytes": len(data),
            "base64": base64.b64encode(data).decode("ascii")}


def _op_write(body):
    path = os.path.expandvars(os.path.expanduser(str(body.get("path") or "")))
    if not path:
        raise ValueError("write requires path")
    data = base64.b64decode(str(body.get("base64") or ""), validate=True)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("file exceeds 4 MiB limit")
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as stream:
        stream.write(data)
    return {"ok": True, "bytes": len(data)}


def _op_ls(body):
    path = os.path.expandvars(os.path.expanduser(str(body.get("path") or ".")))
    items = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        try:
            stat = os.stat(full)
        except OSError:
            continue
        items.append({"name": name,
                      "dir": os.path.isdir(full),
                      "size": stat.st_size,
                      "mtime": int(stat.st_mtime)})
    return {"ok": True, "path": path, "items": items[:500]}


def execute(op, body=None, config=None):
    if op == "materials":
        if config is None:
            return {"ok": False, "error": "materials_config_required"}
        from .remote_ops import materials
        return materials(config, body)

    if op == "ping":
        return {"ok": True, "host": socket.gethostname(),
                "user": os.environ.get("USERNAME") or os.environ.get("USER") or "",
                "cwd": os.getcwd()}
    if op == "exec":
        return _op_exec(body)
    if op == "read":
        return _op_read(body)
    if op == "write":
        return _op_write(body)
    if op == "ls":
        return _op_ls(body)
    raise ValueError("unsupported op: " + str(op))


def _loop(config, logger):
    poll_url = config.control_url + "/api/agent/remote-poll"
    result_url = config.control_url + "/api/agent/remote-result"
    while True:
        try:
            reply = _post(poll_url, config.token,
                          {"customer_id": config.customer_id,
                           **_identity(config)}, timeout=35)
            job = reply.get("op")
            if job:
                job_id = job.get("id")
                try:
                    result = execute(str(job.get("op") or ""),
                                     job.get("body") or {}, config=config)
                    ok = bool(result.get("ok", True))
                except Exception as exc:  # noqa: BLE001 - report, never crash
                    result = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)}
                    ok = False
                _post(result_url, config.token,
                      {"customer_id": config.customer_id, "id": job_id,
                       **_identity(config),
                       "ok": ok, "result": result}, timeout=35)
        except Exception as exc:  # noqa: BLE001 - keep polling
            if logger is not None:
                logger.debug("remote agent poll failed: %s", exc)
            # wait-ok: poll
            time.sleep(5)
            continue
        # wait-ok: poll
        time.sleep(2)


_THREAD = None
_THREAD_KEY = None
_THREAD_LOCK = threading.Lock()


def start_remote_agent(config, logger=None):
    """Start the operator channel once per binding.

    The panel restarts the poller after a successful re-provision; without this
    guard that path would either leave the operator channel dead (the old bug:
    every queued materials request timed out) or start duplicate threads.
    """
    global _THREAD, _THREAD_KEY
    key = (getattr(config, "control_url", ""), getattr(config, "customer_id", ""),
           getattr(config, "device_id", ""), getattr(config, "token", ""))
    with _THREAD_LOCK:
        if _THREAD is not None and _THREAD.is_alive() and _THREAD_KEY == key:
            return _THREAD
        thread = threading.Thread(target=_loop, args=(config, logger),
                                  name="remote-agent", daemon=True)
        _THREAD = thread
        _THREAD_KEY = key
        thread.start()
    if logger is not None:
        logger.info("remote agent started customer=%s device=%s",
                    config.customer_id, getattr(config, "device_id", "") or "-")
    return thread
