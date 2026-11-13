"""Main entry for the resident agent (started hidden by the launcher).

1.2.5 (GPT review 1.2.4 item 8): every startup failure -- unwritable data area,
bad config, missing runtime -- is caught at the TOP level, logged to the real
log path, and turned into a NONZERO exit code with a Chinese remediation line.
"Failed but exited 0" is forbidden.  The startup chain is also integrity-checked
against MANIFEST.json (item 5).
"""

from __future__ import annotations

import argparse
import os
import json
import signal
import sys
import threading
import time
from pathlib import Path

from .errors import EXIT_CONFIG, EXIT_OK, EXIT_SKIP, WinPortError
from .instance import InstanceGuard
from .logutil import get_logger
from .paths import Layout, install_root
from .process_identity import marker_identity
from .settings import load_settings
from .version import APP_NAME_DISPLAY, VERSION


CONTROL_RECHECK_INTERVAL = 30.0
CONTROL_RECHECK_MAX_SECONDS = 24 * 60 * 60
_AGENTPOLL_RESTART_LOCK = threading.RLock()

#: Stable artifacts that only ever exist below a historical per-customer root.
#: They must not include ``<data>/inbox``: startup calls ``Layout.ensure()``
#: before this check, which always creates that directory at the shared root.
TENANT_LAYOUT_MARKERS = (
    "config",
    "run",
    "inbox",
    "out",
    "system",
    "accounts.json",
    "config/accounts.json",
)


def _tray_rows(rows, environments):
    """Tray menu rows grouped by environment (one instance per env)."""
    if not environments:
        return rows
    labels = {env.env_id: env.label for env in environments}
    out = []
    for row in rows or []:
        env_id = row.get("env_id")
        label = labels.get(env_id, env_id or "")
        out.append({**row,
                    "platform_name": f"{label}\u00b7{row.get('platform_name', '')}"
                    if label else row.get("platform_name", "")})
    return out


def _open_panel(url, logger):
    """Tray menu action: open the local control panel in the default browser."""
    if not url:
        logger.info("panel not running; nothing to open")
        return False
    from .panel import open_panel
    return open_panel(url)


def _valid_panel_url(url) -> bool:
    """True only for a real loopback control-panel URL.

    1.2.5 round-3 (GPT review item 8): the readiness marker must not count on a
    merely non-empty string (``httpnot-a-url``).  The control panel always binds
    loopback, so we require an http(s) URL on 127.0.0.1 / ::1 / localhost with a
    real port.
    """
    if not url or not isinstance(url, str):
        return False
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    if parts.hostname not in ("127.0.0.1", "::1", "localhost"):
        return False
    return port is not None and 0 < port <= 65535


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="winport")
    parser.add_argument("--data")
    parser.add_argument("--once", action="store_true",
                        help="one heartbeat round then exit (diagnostics)")
    args = parser.parse_args(argv)

    logger = None
    log_path = "(\u65e5\u5fd7\u5c1a\u672a\u521b\u5efa)"
    try:
        layout = Layout(data=args.data).ensure()
        logger = get_logger("winport.main", layout.logs)
        log_path = str(Path(layout.logs) / "agent.out")
        logger.info("%s v%s starting (data=%s)", APP_NAME_DISPLAY, VERSION,
                    layout.data)
        return _run_agent(layout, logger, args)
    except WinPortError as exc:
        _report_startup_failure(exc.message, exc.exit_code, log_path, logger)
        return exc.exit_code
    except Exception as exc:                       # never "failed but exited 0"
        _report_startup_failure(f"{type(exc).__name__}: {exc}", EXIT_CONFIG,
                                log_path, logger)
        return EXIT_CONFIG


def _report_startup_failure(message, code, log_path, logger) -> None:
    text = (f"\u7a0b\u5e8f\u672a\u80fd\u542f\u52a8\uff1a{message}\n"
            f"\u8865\u6551\u6b65\u9aa4\uff1a1) \u5173\u95ed\u672c\u7a97\u53e3\uff1b2) \u786e\u8ba4\u78c1\u76d8\u6709\u7a7a\u95f4\u3001\u6740\u6bd2\u8f6f\u4ef6\u6ca1\u6709\u9694\u79bb\u672c\u7a0b\u5e8f\uff1b"
            f"3) \u53cc\u51fb\u300c\u6f2b\u5267\u53d1\u5e03\u52a9\u624b \u63a7\u5236\u9762\u677f\u300d\u91cd\u8bd5\uff1b\u82e5\u4ecd\u5931\u8d25\u8bf7\u628a\u8fd9\u4e00\u9875\u622a\u56fe\u53d1\u7ed9\u6211\u4eec\u3002\n"
            f"\u771f\u5b9e\u65e5\u5fd7\uff1a{log_path}")
    if logger is not None:
        logger.error("startup failed (exit %s): %s", code, message)
    print(text, file=sys.stderr)


def _clear_ready_marker(layout, logger) -> None:
    """1.2.5 round-11 (item 8-C): a stopping agent must not leave a readiness
    marker that a later launcher could still read as "this instance is up"."""
    path = Path(layout.run) / "agent-ready.json"
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        logger.info("could not remove readiness marker: %s", exc)
        try:
            from .jsonio import write_json
            write_json(path, {"pid": None, "ready": False, "panel_url": None,
                              "agent_pid": None, "pid_chain": [],
                              "panel_error": "agent stopped", "instance": None,
                              "ts": time.time(), "version": VERSION})
        except OSError:
            pass


def _write_clean_exit_marker(layout, logger) -> None:
    """Tell the supervisor that this exit is orderly, not a crash.

    The supervisor clears this file before every launch and validates either
    the per-launch token (our own child) or the pid (an agent it took over).
    """
    try:
        from .jsonio import write_json
        payload = {"pid": os.getpid(), "ts": time.time(),
                   "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "reason": "orderly shutdown"}
        token = os.environ.get("WINPORT_SUPERVISOR_TOKEN", "").strip().lower()
        if token:
            payload["token"] = token
        write_json(Path(layout.run) / "agent-clean-exit.json", payload)
        logger.info("clean-exit marker written")
    except Exception as exc:  # never turn an orderly exit into a crash
        logger.warning("could not write clean-exit marker: %s", exc)


def _write_agentpoll_marker(layout, logger, **fields) -> None:
    """Best-effort status marker before/without a fully valid AgentConfig."""
    try:
        from .jsonio import write_json
        payload = {"updated": time.time()}
        payload.update(fields)
        write_json(Path(layout.run) / "agentpoll-status.json", payload)
    except Exception as exc:  # noqa: BLE001 - never block startup
        logger.warning("could not write agentpoll status: %s", exc)


def _write_control_status(layout, logger, state, reason="", *,
                          customer_id="", device_id="") -> None:
    """Best-effort customer-visible Feishu/control connection status."""
    try:
        from .control_status import write_status
        write_status(layout, state, reason, customer_id=customer_id,
                     device_id=device_id)
    except Exception as exc:  # noqa: BLE001 - never block the panel
        logger.warning("could not write control status: %s", exc)


def _single_root_for(layout, customer_id) -> bool:
    """Default to a dedicated single root; keep historical tenant layouts.

    The panel, accounts, inbox and profiles live in ``<data>`` on a dedicated
    customer machine.  Only an old multi-tenant tree still needs the scoped
    root.  Historical trees are recognised by real artifacts inside
    ``customers/<id>`` rather than by the absence of ``<data>/inbox``:
    ``Layout.ensure()`` has already created the shared inbox by the time this
    runs, so the old negative test could never match in production.
    """
    from .customer_paths import DEFAULT_CUSTOMER_ID, normalize_customer_id
    try:
        cid = normalize_customer_id(customer_id)
    except (TypeError, ValueError):
        return True
    if cid == DEFAULT_CUSTOMER_ID:
        return True
    tenant = Path(layout.data) / "customers" / cid
    if not tenant.is_dir():
        return True
    for marker in TENANT_LAYOUT_MARKERS:
        try:
            if (tenant / marker).exists():
                return False
        except OSError:
            continue
    return True


def _start_agentpoll(layout, logger, *, start_remote=True):
    """Start the control-plane poller as part of the normal startup chain.

    The poller is disabled when no CUSTOMER_TOKEN is configured (the legacy
    single-customer install).  Automatic startup also requires an explicit
    https CONTROL_URL; the old loopback default could hand the customer token
    to any local process that won the port race.  Every disabled/startup/error
    state is written to run/agentpoll-status.json so a silent non-running
    poller is observable.
    """
    try:
        from .agentpoll import AgentConfig, AgentPoller, write_status
    except Exception as exc:  # noqa: BLE001 - import must not kill the panel
        logger.error("agentpoll unavailable: %s", exc)
        _write_agentpoll_marker(layout, logger, state="import_error",
                                last_error=f"{type(exc).__name__}: {exc}")
        _write_control_status(layout, logger, "error",
                              f"接入组件不可用：{type(exc).__name__}: {exc}")
        return None
    overlay = {}
    control_data = None
    found = _valid_control_file(layout)
    if found is not None:
        candidate, control_data = found
        for key, env_name in (("control_url", "CONTROL_URL"),
                              ("customer_id", "CUSTOMER_ID"),
                              ("customer_token", "CUSTOMER_TOKEN"),
                              ("device_id", "DEVICE_ID"),
                              ("device_token", "DEVICE_TOKEN"),
                              ("binding_epoch", "DEVICE_BINDING_EPOCH")):
            if key in control_data and control_data[key] is not None:
                value = str(control_data[key]).strip()
                if value:
                    overlay[env_name] = value
        logger.info("agentpoll config loaded from %s", candidate)
    else:
        _write_control_status(layout, logger, "missing",
                              "尚未获取有效的接入配置")
    merged_env = dict(os.environ)
    merged_env.update(overlay)
    customer_id = (
        merged_env.get("CUSTOMER_ID")
        or merged_env.get("WINPORT_CUSTOMER_ID")
        or merged_env.get("AGENT_CUSTOMER_ID")
        or "default")
    single_root = _single_root_for(layout, customer_id)
    try:
        config = AgentConfig.from_env(
            env=merged_env,
            data_root=str(layout.data),
            system_root=str(layout.system),
            program_dir=str(layout.program),
            cli_root=str(layout.program),
            status_file=str(Path(layout.run) / "agentpoll-status.json"),
            single_root=single_root,
            require_explicit_control_url=True,
        )
    except ValueError as exc:
        logger.info("agentpoll disabled: %s", exc)
        _write_agentpoll_marker(layout, logger, state="disabled",
                                reason=str(exc))
        _write_control_status(layout, logger, "disabled", str(exc),
                              customer_id=customer_id)
        return None
    except Exception as exc:  # noqa: BLE001 - bad config is reported
        logger.error("agentpoll config failed: %s", exc)
        _write_agentpoll_marker(layout, logger, state="config_error",
                                last_error=f"{type(exc).__name__}: {exc}")
        _write_control_status(layout, logger, "error",
                              f"接入配置错误：{type(exc).__name__}: {exc}",
                              customer_id=customer_id)
        return None
    if not config.token:
        write_status(config, state="disabled",
                     reason="CUSTOMER_TOKEN is not configured")
        logger.info("agentpoll disabled: CUSTOMER_TOKEN is not configured")
        _write_control_status(layout, logger, "disabled",
                              "接入配置缺少 customer token",
                              customer_id=config.customer_id,
                              device_id=config.device_id)
        return None
    try:
        poller = AgentPoller(config, logger=logger)
        poller.start()
    except Exception as exc:  # noqa: BLE001 - panel still starts
        logger.error("agentpoll startup failed: %s", exc)
        write_status(config, state="startup_error",
                     last_error=f"{type(exc).__name__}: {exc}")
        _write_control_status(layout, logger, "error",
                              f"接入服务启动失败：{type(exc).__name__}: {exc}",
                              customer_id=config.customer_id,
                              device_id=config.device_id)
        return None
    logger.info("agentpoll started customer=%s control=%s",
                config.customer_id, config.control_url)
    # Do not report "connected" merely because the poller thread started or
    # control.json is valid.  The poller itself writes that status after its
    # first successful poll (and writes error/disabled when the control plane
    # rejects the credentials or cannot be reached).
    if start_remote:
        try:
            from .remote_agent import start_remote_agent
            start_remote_agent(config, logger=logger)
        except Exception as exc:  # noqa: BLE001 - panel keeps working
            logger.warning("remote agent unavailable: %s", exc)
    return poller


def _restart_agentpoll(layout, logger, poller_slot, start_poller=None):
    """Stop the active poller, then start a fresh one from control.json.

    The panel calls this after a successful re-provision.  The old poller keeps
    the old token in memory, so a file write alone cannot make the new token
    take effect.  Every failure is returned and written as a visible error.
    """
    def _default_start(layout_arg, logger_arg):
        # Always bring the operator channel up with the poller.  It used to be
        # skipped here to avoid duplicate threads, which left brand-new
        # installs unable to collect material lists after activation;
        # start_remote_agent is idempotent now, so pass start_remote=True.
        return _start_agentpoll(layout_arg, logger_arg, start_remote=True)

    with _AGENTPOLL_RESTART_LOCK:
        old = poller_slot.get("poller")
        if start_poller is None:
            # Keep an existing remote-agent thread instead of starting a
            # duplicate; when no poller existed yet, this is the first start.
            start_poller = (_default_start if old is not None
                            else _start_agentpoll)
        poller_slot["poller"] = None
        if old is not None:
            try:
                old.stop()
            except Exception as exc:  # noqa: BLE001 - report, never silent
                poller_slot["poller"] = old
                reason = f"停止旧接入服务失败：{type(exc).__name__}: {exc}"
                _write_control_status(layout, logger, "error", reason)
                return {"ok": False, "reason": reason}
            thread = getattr(old, "thread", None)
            if (thread is not None and thread.is_alive()
                    and thread is not threading.current_thread()):
                try:
                    thread.join(5.0)
                except Exception:  # noqa: BLE001 - restart must still proceed
                    pass
        new_poller = start_poller(layout, logger)
        if new_poller is None:
            marker = _agentpoll_marker(layout)
            detail = str(marker.get("reason") or marker.get("last_error")
                         or marker.get("state") or "未返回可用接入服务")
            reason = f"接入服务未能重新启动：{detail}"
            _write_control_status(layout, logger, "error", reason)
            return {"ok": False, "reason": reason}
        poller_slot["poller"] = new_poller
    logger.info("agentpoll restarted with the current control.json token")
    return {"ok": True, "reason": "接入服务已重启"}


def _agentpoll_marker(layout):
    """Return the last startup/poller marker written by the agentpoll chain."""
    try:
        from .jsonio import read_json
        data = read_json(Path(layout.run) / "agentpoll-status.json",
                         default=None)
    except Exception:  # noqa: BLE001 - a malformed marker must not matter
        return {}
    return data if isinstance(data, dict) else {}


def _agentpoll_marker_state(layout):
    """Return the last startup/poller state written by the agentpoll chain."""
    return str(_agentpoll_marker(layout).get("state") or "")


def _is_auth_failure(state, detail) -> bool:
    """Only an explicit control-plane rejection stops the recheck thread."""
    text = f"{state or ''} {detail or ''}".lower()
    if str(state or '').lower() in ("auth_error", "auth_failed",
                                    "unauthorized", "forbidden"):
        return True
    return any(token in text for token in (
        "401", "403", "authentication failed", "authentication rejected",
        "unauthorized", "forbidden", "鉴权", "认证失败"))


def _valid_control_file(layout):
    """Return (path, data) for the first usable control.json, else None."""
    from .activation import is_valid_control
    for candidate in (Path(layout.data) / "control.json",
                      Path(layout.program) / "control.json"):
        try:
            if not candidate.is_file():
                continue
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - unreadable file is not usable yet
            continue
        if is_valid_control(data):
            return candidate, data
    return None


def _control_recheck_once(layout, logger, poller_slot, stop_event=None):
    """Try agentpoll startup once when a valid control.json has appeared."""
    found = _valid_control_file(layout)
    if found is None:
        return "waiting"
    candidate, _data = found
    logger.info("control.json ready at %s; retrying agentpoll startup",
                candidate)
    with _AGENTPOLL_RESTART_LOCK:
        # A panel-triggered restart may have already installed a poller with
        # the new token.  Never create a second one from the recheck thread.
        if poller_slot.get("poller") is not None:
            return "started"
        poller = _start_agentpoll(layout, logger)
        if poller is not None:
            if stop_event is not None and stop_event.is_set():
                poller.stop()
                return "stopped"
            poller_slot["poller"] = poller
            return "started"
        marker = _agentpoll_marker(layout)
        state = str(marker.get("state") or "")
        detail = str(marker.get("last_error") or marker.get("reason") or "")
        if _is_auth_failure(state, detail):
            logger.error("agentpoll retry stopped after explicit auth failure: %s",
                         detail or state)
            return "auth_failed"
    logger.info("agentpoll retry will continue: %s", detail or state or "unknown")
    return "retry"


def _control_recheck_loop(layout, logger, stop_event, poller_slot,
                          interval=None, max_seconds=None):
    """Wait for a late control.json without a tight loop or blocking startup."""
    if interval is None:
        interval = CONTROL_RECHECK_INTERVAL
    if max_seconds is None:
        max_seconds = CONTROL_RECHECK_MAX_SECONDS
    deadline = time.monotonic() + max(0.0, float(max_seconds))
    while not stop_event.wait(interval):
        outcome = _control_recheck_once(layout, logger, poller_slot,
                                        stop_event=stop_event)
        if outcome in ("started", "stopped", "auth_failed"):
            return
        if time.monotonic() >= deadline:
            logger.warning("control recheck stopped after %.0f hour(s) of "
                           "transient failures", float(max_seconds) / 3600.0)
            return


def _integrity_gate(layout, logger) -> None:
    """Re-hash the startup chain; log loudly but never block \u6293\u6570."""
    try:
        from .integrity import verify_program
        from .jsonio import write_json
        from .paths import install_root
        verdict = verify_program(install_root())
        try:
            write_json(Path(layout.run) / "integrity.json", verdict)
        except OSError:
            pass
        if not verdict["ok"]:
            from .logutil import audit
            logger.error("integrity check FAILED: %s", verdict.get("reason"))
            audit(layout.logs, "integrity_failed",
                  modified=verdict.get("modified"),
                  missing=verdict.get("missing"))
        else:
            logger.info("integrity check ok (%d files verified)",
                        verdict.get("checked", 0))
    except Exception as exc:                       # a check fault must not be fatal
        logger.error("integrity check error (continuing): %s", exc)


def _run_agent(layout, logger, args) -> int:
    guard = InstanceGuard(layout, logger=logger)
    # A crash/power cut during the atomic commit can leave mixed files.  Only
    # touch them when no other agent owns this installation, and only before
    # VERSION was committed by the previous transaction.
    try:
        if not guard.already_running():
            from .updater import recover_interrupted
            recovered = recover_interrupted(layout, install_root())
            if recovered.get("recovered"):
                logger.info("rolled back an interrupted update to %s",
                            recovered.get("version"))
    except Exception as exc:
        logger.error("interrupted-update recovery failed: %s", exc)

    _integrity_gate(layout, logger)

    outcome = guard.start_or_focus()
    if not outcome["started"]:
        logger.info("already running: %s", outcome["reason"])
        return EXIT_SKIP

    from .daemon import Daemon, _default_downlink
    from .platforms import load_registry, panel_binding
    from .tray import TrayApp
    settings = load_settings(layout.settings_file)
    environments, channels = load_registry(layout)
    logger.info("registry: %d environment(s), %d login slot(s)",
                len(environments), len(channels))

    # The approval service is a local helper, not the agent itself.  Start it
    # with the agent and re-check every minute so a crashed helper comes back
    # without a manual restart.  ensure_running() never raises.
    approval_stop = threading.Event()

    def _approval_service_loop():
        from .approval_service import ensure_running
        while not approval_stop.is_set():
            try:
                verdict = ensure_running(layout, settings, logger=logger)
                if verdict.get("started"):
                    logger.info("approval service started")
                elif verdict.get("reason") not in ("already_running",):
                    logger.info("approval service not running: %s",
                                verdict.get("reason"))
            except Exception as exc:
                logger.error("approval service check failed: %s: %s",
                             type(exc).__name__, exc)
            approval_stop.wait(60)

    threading.Thread(target=_approval_service_loop, daemon=True,
                     name="approval-service").start()

    # Multitenant control-plane poller.  It is part of the normal startup
    # chain when CUSTOMER_TOKEN is configured; otherwise a disabled marker is
    # written so the missing layer is visible instead of silently absent.
    agentpoll = None
    agentpoll_slot = {"poller": None}
    control_recheck_stop = threading.Event()
    if not args.once:
        agentpoll = _start_agentpoll(layout, logger)
        agentpoll_slot["poller"] = agentpoll
        if agentpoll is None and _agentpoll_marker_state(layout) == "disabled":
            logger.info("agentpoll disabled at startup; rechecking control.json "
                        "every %.0fs", CONTROL_RECHECK_INTERVAL)
            threading.Thread(
                target=_control_recheck_loop,
                args=(layout, logger, control_recheck_stop, agentpoll_slot),
                name="control-recheck", daemon=True).start()

    def _stop_agentpoll():
        nonlocal agentpoll
        control_recheck_stop.set()
        with _AGENTPOLL_RESTART_LOCK:
            poller = agentpoll_slot.get("poller")
            agentpoll_slot["poller"] = None
            if poller is not None:
                poller.stop()
        agentpoll = None

    def _restart_for_panel():
        return _restart_agentpoll(layout, logger, agentpoll_slot)

    # Local control panel: the customer's "status lights" screen + login entry.
    # Loopback only.  1.2.5 (GPT review item 8): if it cannot bind we do NOT
    # pretend the start succeeded -- a readiness marker with no panel_url used
    # to make the launcher report success.  We record an honest not-ready marker
    # and fail with a nonzero exit + a plain-language remedy.
    panel_url = None
    panel_error = None
    panel_port = None
    remote = None
    if not args.once:
        try:
            from .remote_channel import RemoteChannel
            remote = RemoteChannel(layout)
        except Exception as exc:
            logger.error("remote channel setup failed: %s", exc)
    # 1.2.5 round-11 (GPT review item 8-C): a per-launch random instance token.
    # It is written into run\agent-ready.json and echoed by the panel's /healthz
    # endpoint, so the launcher proves the live loopback panel really belongs to
    # the agent instance it started -- not a recycled pid or a stranger HTTP
    # service that merely answers 200.
    # run_panel.py supplies this per-spawn token; the VBS entries leave it
    # unset and keep the historical random-token behaviour.
    injected_token = os.environ.get("WINPORT_INSTANCE_TOKEN", "").strip().lower()
    if len(injected_token) == 32 and all(ch in "0123456789abcdef"
                                         for ch in injected_token):
        instance_token = injected_token
    else:
        instance_token = os.urandom(16).hex()
    # A Windows venv pythonw is a redirector: the updater-spawned process and
    # the process serving /healthz have different pids.  Keep the legacy
    # supervisor-visible pid in "pid" and publish the live server pid as
    # "agent_pid" (see process_identity.py).
    identity = marker_identity()
    try:
        from .jsonio import read_json
        from .panel import serve_in_thread
        binding = panel_binding(read_json(layout.channels_file, default=None))
        panel_port = binding["port"]
        info = serve_in_thread(layout, channels, envs=environments,
                               host=binding["host"], port=binding["port"],
                               instance_token=instance_token,
                               remote_channel=remote,
                               restart_poller=_restart_for_panel)
        panel_url = info["url"]
        logger.info("control panel: %s", panel_url)
    except Exception as exc:
        panel_error = f"{type(exc).__name__}: {exc}"
        logger.error("control panel did not start: %s", panel_error)

    # 1.2.5 (GPT review item 8): only a REAL loopback panel URL counts as ready
    # (empty / null / "httpnot-a-url" all fail).
    panel_ok = _valid_panel_url(panel_url)

    daemon = Daemon(layout, downlink=_default_downlink(layout, logger),
                    logger=logger, settings=settings, channels=channels)
    tray = TrayApp(layout, on_check_now=daemon.run_round,
                   on_open_panel=(lambda: _open_panel(panel_url, logger)),
                   status_provider=lambda: _tray_rows(daemon.channel_rows,
                                                      environments),
                   logger=logger)
    daemon.notify = tray.notify
    tray.start()

    # 1.2.5: explicit readiness marker -> the launcher / panel do a real, timed
    # handshake instead of a blind sleep.  "ready" is TRUE only when the control
    # panel actually came up; a failed panel is never reported as ready.
    try:
        from .jsonio import write_json
        write_json(Path(layout.run) / "agent-ready.json",
                   {"pid": identity["pid"] if panel_ok else None,
                    "agent_pid": identity["agent_pid"] if panel_ok else None,
                    "pid_chain": list(identity["pid_chain"]) if panel_ok else [],
                    "ready": panel_ok, "panel_url": panel_url,
                    "panel_error": panel_error,
                    "instance": instance_token if panel_ok else None,
                    "ts": time.time(), "version": VERSION,
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        logger.info("readiness marker written (ready=%s panel=%s)",
                    panel_ok, panel_url)
    except OSError as exc:
        logger.error("could not write readiness marker: %s", exc)

    if not panel_ok:
        raise WinPortError(
            f"\u63a7\u5236\u9762\u677f\u672a\u80fd\u542f\u52a8\uff08{panel_error}\uff09\uff0c\u5df2\u6309\u5931\u8d25\u5904\u7406\uff08\u9000\u51fa\u7801\u975e 0\uff09\u3002"
            f"\u8865\u6551\uff1a1) \u786e\u8ba4\u63a7\u5236\u9762\u677f\u4f7f\u7528\u7684\u672c\u673a\u670d\u52a1\u6ca1\u6709\u88ab\u522b\u7684\u7a0b\u5e8f\u5360\u7528\uff1b"
            f"2) \u5173\u95ed\u5360\u7528\u7684\u7a0b\u5e8f\u540e\uff0c\u91cd\u65b0\u53cc\u51fb\u300c\u6f2b\u5267\u53d1\u5e03\u52a9\u624b \u63a7\u5236\u9762\u677f\u300d\uff1b"
            f"3) \u82e5\u4ecd\u5931\u8d25\uff0c\u8bf7\u628a\u8fd9\u4e00\u9875\u622a\u56fe\u53d1\u7ed9\u6211\u4eec\u3002\u771f\u5b9e\u65e5\u5fd7\uff1a"
            f"{Path(layout.logs) / 'agent.out'}",
            exit_code=EXIT_CONFIG)

    if remote is not None:
        try:
            remote.start()
        except Exception as exc:
            logger.error("remote channel startup failed: %s", exc)

    # Incremental update loop.  It only acts while there is no in-flight
    # publish and no publish lock, and it never runs in --once diagnostics.
    update_loop = None
    shutdown_event = threading.Event()
    if not args.once and panel_ok:
        try:
            from .updater import UpdateLoop
            update_loop = UpdateLoop(
                layout,
                health_url=panel_url.rstrip("/") + "/healthz",
                shutdown=shutdown_event.set,
                logger=logger,
            )
            update_loop.start()
        except Exception as exc:
            logger.error("update loop startup failed: %s", exc)

    clean_shutdown_requested = False

    def _stop(*_):
        nonlocal clean_shutdown_requested
        clean_shutdown_requested = True
        approval_stop.set()
        shutdown_event.set()
        if update_loop is not None:
            update_loop.stop()
        _stop_agentpoll()
        _clear_ready_marker(layout, logger)
        tray.stop()
        guard.release()
        raise SystemExit(EXIT_OK)

    try:
        signal.signal(signal.SIGTERM, _stop)
    except (ValueError, OSError):
        pass

    normal_return = False
    try:
        code = daemon.run_forever(
            max_rounds=1 if args.once else None,
            stop_event=shutdown_event if not args.once else None)
        normal_return = True
        return code
    finally:
        approval_stop.set()
        shutdown_event.set()
        if update_loop is not None:
            update_loop.stop()
        _stop_agentpoll()
        if remote is not None:
            remote.stop()
        _clear_ready_marker(layout, logger)
        tray.stop()
        guard.release()
        if normal_return or clean_shutdown_requested:
            _write_clean_exit_marker(layout, logger)


if __name__ == "__main__":
    raise SystemExit(run())
