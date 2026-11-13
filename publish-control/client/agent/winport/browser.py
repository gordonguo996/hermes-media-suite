"""Start the login browser for one login slot / environment.

1.2.2 (source/brief-chrome-only-20261016.md): the browser is resolved through
``chrome.resolve_browser``, which now returns the customer's **real Google
Chrome or nothing**.  There is no Edge/Chromium fallback -- if Chrome is absent
the caller gets one plain sentence plus the official (in-package) installer.
``edge_path`` is still accepted, purely so the diagnostics can say "你机器上的
Edge 不参与运行".

Two layers stop two browsers from fighting over one login state:

  * a live-process scan for the exact ``--user-data-dir=<profile>`` argument
    (``proc.is_profile_in_use``) -- catches a browser started by the customer
    or by a previous run of the agent;
  * a launch marker (``run/envs/<env>.launch.json`` for an environment,
    ``run/profiles/<slot>.launch.json`` for a legacy per-platform slot) whose
    pid is checked for liveness -- closes the race where the browser process has
    not shown up yet in the process list.

The single-instance lock is PER ENVIRONMENT (source/env-design.md rule 1): all
platforms of one environment share one Chrome, so opening a second platform's
login while the environment is up only opens a new TAB in that same instance
(Chrome's own singleton), which is reported as ``REUSED_ENV_WINDOW`` -- never a
second instance.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from . import chrome
from .errors import WinPortError
from .jsonio import read_json, write_json
from .subprocess_text import hidden_window_options


class ProfileBusy(WinPortError):
    exit_code = 3  # EXIT_SKIP


def venv_python(layout, *, windowed=True):
    """The interpreter the launcher actually uses (venv first, then runtime).

    Kept in step with scripts/launcher.vbs -> scripts/run-agent.vbs; the G2 fix
    made the two agree, so a browser started from the panel runs in the same
    environment as the resident agent.
    """
    exe = "pythonw.exe" if (windowed and os.name == "nt") else "python.exe"
    candidates = [layout.data / ".venv" / "Scripts" / exe,
                  layout.data / ".venv" / "Scripts" / "python.exe",
                  layout.runtime / exe,
                  layout.runtime / "python.exe"]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def resolve_browser(layout, *, chrome_path=None):
    """The browser to drive: the customer's real Google Chrome, or nothing.

    Returns ``(path_or_None, report)`` where ``report`` is the resolution dict
    (``engine`` = chrome when found; ``found=False`` + ``needs_chrome=True``
    otherwise; ``reason`` is already a customer-readable sentence).  There is no
    fallback engine (1.2.2), so a ``None`` path always means "install Chrome".
    ``chrome_path`` is the explicit override used by diagnostics/tests.
    """
    report = chrome.resolve_browser(chrome_path=chrome_path)
    return (report["path"] if report["found"] else None), report


def marker_path(layout, channel):
    """Launch marker for this slot -- per environment when the slot has one."""
    env_id = getattr(channel, "env_id", None)
    if env_id:
        return layout.env_lease_dir / f"{env_id}.launch.json"
    return layout.run / "profiles" / f"{channel.profile_name}.launch.json"


# backwards-compatible name used by tests / older callers
def launch_marker(layout, channel):
    return marker_path(layout, channel)


def pid_alive(pid) -> bool:
    """Safe process liveness check (see proc.is_pid_alive)."""
    from . import proc
    return proc.is_pid_alive(pid)


def profile_in_use(layout, channel, processes=None) -> bool:
    from . import proc
    if proc.is_profile_in_use(channel.profile_dir(layout), processes):
        return True
    marker = read_json(marker_path(layout, channel), default=None)
    if isinstance(marker, dict) and marker.get("pid") and pid_alive(marker["pid"]):
        if marker.get("profile") == str(channel.profile_dir(layout)):
            return True
    return False


def plan_open(layout, channel, *, profile_dir=None, chrome_path=None,
              edge_path=None) -> dict:
    """Build the exact command line without running it (static-testable)."""
    profile_dir = profile_dir or channel.profile_dir(layout)
    plan = {"channel_id": channel.channel_id, "env_id": getattr(channel, "env_id", None),
            "engine": channel.engine, "profile": str(profile_dir),
            "login_url": channel.login_url, "debug_port": channel.debug_port,
            "browser": None, "browser_source": None, "browser_engine": None,
            "tier": None, "command": [], "error": None}
    if channel.engine in ("chromium", "chrome"):
        report = chrome.resolve_browser(chrome_path=chrome_path,
                                        edge_path=edge_path)
        exe = report["path"] if report["found"] else None
        if not exe:
            plan["error"] = chrome.summarize({**report, "min_major":
                                              chrome.MIN_CHROME_MAJOR})
            return plan
        plan["browser"] = exe
        plan["browser_source"] = report.get("source")
        plan["browser_engine"] = report.get("engine")
        plan["tier"] = report.get("tier")
        plan["command"] = chrome.build_launch_args(
            exe, profile_dir, channel.debug_port or 0, channel.login_url)
        return plan
    # Optional legacy WebKit engine: Playwright persistent context (no CDP port)
    plan["command"] = [str(venv_python(layout)), "-m", "winport.playwright_launch",
                       "--profile", str(profile_dir), "--url", channel.login_url,
                       "--data", str(layout.data)]
    return plan


def open_channel(layout, channel, *, processes=None, dry_run=False,
                 spawn=None, profile_dir=None, chrome_path=None,
                 edge_path=None) -> dict:
    """Public entry: start the login browser, and KEEP EVIDENCE on failure (B)."""
    result = _open_channel_impl(layout, channel, processes=processes,
                                dry_run=dry_run, spawn=spawn,
                                profile_dir=profile_dir, chrome_path=chrome_path,
                                edge_path=edge_path)
    _capture_step_failure(layout, "browser", result,
                          env_id=getattr(channel, "env_id", None),
                          channel_id=channel.channel_id)
    return result


def _open_channel_impl(layout, channel, *, processes=None, dry_run=False,
                       spawn=None, profile_dir=None, chrome_path=None,
                       edge_path=None) -> dict:
    """Start the login browser for one slot, or explain why it did not start."""
    result = {"channel_id": channel.channel_id, "env_id": getattr(channel, "env_id", None),
              "platform": channel.platform, "nickname": channel.nickname,
              "opened": False, "reused": False, "pid": None, "reason": "",
              "message": "", "command": [], "browser": None, "engine": None,
              "tier": None}
    if not channel.enabled:
        result.update(reason="CHANNEL_DISABLED",
                      message="该环境已停用（enabled=false），先启用再登录")
        return result

    in_use = profile_in_use(layout, channel, processes)
    env_channel = bool(getattr(channel, "env_id", None))
    if in_use:
        # One instance per environment: if the environment's Chrome is already
        # up, this request just opens a tab in that same window.  A legacy
        # per-platform slot keeps the old hard refusal (safer for its own login
        # state which is not shared).
        if env_channel and channel.debug_port and chrome.port_open(channel.debug_port):
            profile_dir = profile_dir or channel.profile_dir(layout)
            plan = plan_open(layout, channel, profile_dir=profile_dir,
                             chrome_path=chrome_path, edge_path=edge_path)
            result["command"] = [str(c) for c in plan["command"]]
            if plan["error"]:
                result.update(reason="BROWSER_MISSING", message=plan["error"])
                return result
            if dry_run:
                result.update(reason="DRY_RUN",
                              message="只演练、不启动（--dry-run）")
                return result
            marker = read_json(marker_path(layout, channel), default=None)
            reuse = chrome.reusable_instance_evidence(
                marker, profile_dir, expected_exe=plan.get("browser"),
                expected_port=channel.debug_port, processes=processes)
            result["reuse_evidence"] = reuse
            if not reuse.get("ok"):
                result.update(
                    reason="STALE_BROWSER_LOCKED",
                    message=("无法证明当前窗口的 Chrome 实际启动参数包含"
                             "外部协议弹窗禁用项；程序不会复用或强杀未确认实例。"
                             "请关闭该环境窗口后重试"))
                return result
            runner = spawn or _spawn
            child = runner(plan["command"], layout)
            result.update(opened=True, reused=True, pid=getattr(child, "pid", None),
                          browser=plan["browser"], reason="REUSED_ENV_WINDOW",
                          engine=plan.get("browser_engine"), tier=plan.get("tier"),
                          message=(f"{channel.nickname} 所属环境下浏览器已在运行，"
                                   "已在该窗口打开新标签页（不会开出第二个实例）"))
            return result
        result.update(reason="PROFILE_IN_USE",
                      message=("这个登录态目录已经有浏览器在跑了，不允许并行开第二个"
                               "（同一个 profile 并行会互相踩踏登录态）"))
        return result

    plan = plan_open(layout, channel, profile_dir=profile_dir,
                     chrome_path=chrome_path, edge_path=edge_path)
    result["command"] = [str(c) for c in plan["command"]]
    result["browser"] = plan["browser"]
    result["engine"] = plan.get("browser_engine")
    result["tier"] = plan.get("tier")
    if plan["error"]:
        result.update(reason="BROWSER_MISSING", message=plan["error"])
        return result
    if dry_run:
        result.update(reason="DRY_RUN", message="只演练、不启动（--dry-run）")
        return result

    profile_dir = profile_dir or channel.profile_dir(layout)
    profile_dir.mkdir(parents=True, exist_ok=True)
    runner = spawn or _spawn
    child = runner(plan["command"], layout)
    from . import proc
    pid = getattr(child, "pid", None)
    if os.name == "nt" and plan.get("browser") and channel.debug_port:
        pid, _resolved = chrome.resolve_browser_pid(
            profile_dir, expected_exe=plan["browser"],
            expected_port=channel.debug_port, fallback_pid=pid)
    survival = chrome.survival_probe(pid, channel.debug_port,
                                     expected_exe=plan.get("browser"))
    if not survival.get("ok"):
        result.update(
            opened=False, pid=pid, reason=survival.get("reason") or "BROWSER_EXITED",
            message=f"{channel.nickname} 的浏览器启动后立即退出，请重试。")
        return result
    runtime_argv = {"verified": False, "reason": "NOT_WINDOWS"}
    if plan.get("browser") and channel.debug_port and os.name == "nt":
        runtime_argv = proc.verify_launch_argv(
            pid, profile_dir,
            expected_exe=plan["browser"], expected_port=channel.debug_port)
    write_json(marker_path(layout, channel), {
        "channel_id": channel.channel_id, "env_id": getattr(channel, "env_id", None),
        "profile": str(profile_dir), "pid": pid,
        "engine": channel.engine, "browser_engine": plan.get("browser_engine"),
        "browser": plan.get("browser"), "debug_port": channel.debug_port,
        chrome.DIALOG_GUARD_MARKER_KEY: chrome.DIALOG_GUARD_FEATURE,
        "runtime_argv_verified": bool(runtime_argv.get("verified")),
        "runtime_argv_evidence": runtime_argv,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    result.update(opened=True, pid=pid, reason="OPENED",
                  runtime_argv_verified=bool(runtime_argv.get("verified")),
                  runtime_argv_evidence=runtime_argv,
                  message=(f"已用{chrome.ENGINE_LABELS.get(plan.get('browser_engine'), '浏览器')}"
                           f" 拉起 {channel.nickname}"
                           f"（{channel.platform_name}）登录窗口"))
    return result


def open_environment(layout, env, *, url=None, processes=None, dry_run=False,
                     spawn=None, probe=None, timeout=20.0, chrome_path=None,
                     edge_path=None):
    """Public entry: bring up an environment; keep evidence if it fails (B).

    1.2.214: this is the ONE place a browser may be launched, so the
    per-environment launch lock lives here and nowhere else.  The daemon
    supervisor, the panel button and the publish flow therefore cannot start the
    same account twice at the same moment; whoever loses the race gets a plain
    "already starting" answer instead of a second Chrome.
    """
    from .lock import FileLock
    lock = FileLock(layout.run / f"env-{env.env_id}.launch.lock")
    if not lock.acquire():
        return {"env_id": env.env_id, "label": env.label, "opened": False,
                "reused": False, "pid": None, "reason": "LAUNCH_IN_PROGRESS",
                "message": f"{env.label} 的浏览器正在启动中，请稍后再试",
                "command": [], "cdp": None, "engine": None, "tier": None}
    try:
        result = _open_environment_impl(layout, env, url=url, processes=processes,
                                        dry_run=dry_run, spawn=spawn, probe=probe,
                                        timeout=timeout, chrome_path=chrome_path,
                                        edge_path=edge_path)
    finally:
        lock.release()
    _capture_step_failure(layout, "browser", result, env_id=env.env_id,
                          channel_id=None)
    return result


def _open_environment_impl(layout, env, *, url=None, processes=None, dry_run=False,
                           spawn=None, probe=None, timeout=20.0, chrome_path=None,
                           edge_path=None):
    """Bring up / reuse one environment's Google Chrome instance (Chrome only)."""
    result = {"env_id": env.env_id, "label": env.label, "opened": False,
              "reused": False, "pid": None, "reason": "", "message": "",
              "command": [], "cdp": None, "engine": None, "tier": None}
    if not env.enabled:
        result.update(reason="ENV_DISABLED", message=f"{env.label} 已停用")
        return result
    if dry_run:
        detection = chrome.resolve_browser(chrome_path=chrome_path,
                                           edge_path=edge_path)
        args = chrome.build_launch_args(
            detection["path"] or "chrome.exe",
            env.profile_dir(layout), env.debug_port or 0, url or None)
        result.update(reason="DRY_RUN", command=[str(a) for a in args],
                      engine=detection.get("engine"), tier=detection.get("tier"),
                      message="只演练、不启动（--dry-run）")
        return result
    out = chrome.launch_environment(
        env, layout, url=url, timeout=timeout,
        probe=probe or chrome.cdp_version, spawn_fn=spawn, chrome_path=chrome_path,
        edge_path=edge_path)
    result.update(out)
    if out.get("opened"):
        marker = layout.env_lease_dir / f"{env.env_id}.launch.json"
        write_json(marker, {
            "env_id": env.env_id, "profile": str(env.profile_dir(layout)),
            "pid": out.get("pid"), "debug_port": env.debug_port,
            "cdp_browser": (out.get("cdp") or {}).get("browser"),
            chrome.DIALOG_GUARD_MARKER_KEY: chrome.DIALOG_GUARD_FEATURE,
            "runtime_argv_verified": out.get("runtime_argv_verified"),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    return result


def _spawn(command, layout):
    from .subprocess_text import is_visible_browser
    if is_visible_browser(command):
        # The browser must outlive the request/agent that launched it.
        return chrome.spawn(command, cwd=str(layout.program), detach=True)
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen([str(c) for c in command], close_fds=True,
                            cwd=str(layout.program),
                            **hidden_window_options(command, popen=True,
                                                    creationflags=flags))


def clear_marker(layout, channel) -> None:
    try:
        marker_path(layout, channel).unlink()
    except OSError:
        pass


def _capture_step_failure(layout, step, result, *, env_id=None, channel_id=None,
                          screenshot=None, html=None):
    """B: a failed browser step leaves a record in the evidence directory."""
    try:
        from .evidence import failure_evidence
        return failure_evidence(layout, step, result, env_id=env_id,
                                channel_id=channel_id, screenshot=screenshot,
                                html=html)
    except Exception:
        return None
