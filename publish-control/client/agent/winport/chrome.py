"""Google Chrome resolution: detect the customer's real Chrome, open it with a
*separate* profile, and prove the debug port really came up.

Design freeze (source/extra-requirements-chrome.md "用真谷歌" +
source/brief-chrome-only-20261016.md "Google 的浏览器还是要装，其他东西不用装"):
  * the package must NOT bundle a *browser engine* (re-distribution licence);
  * **Google Chrome is the only browser we drive.**  If it is there we use it;
    if it is not, we STOP and say so -- we never silently substitute Microsoft
    Edge, Chromium or anything else.  The behaviour we verified online is real
    Google Chrome, so swapping in another engine would void that verification;
  * Edge may still be *detected*, but only to be reported as **diagnostics**
    ("本程序要求 Chrome；你机器上的 Edge 不参与运行").  ``find_edge`` is never
    on the launch path and ``ENGINE_TIERS`` no longer contains it;
  * a missing Chrome produces one plain-language sentence, Google's official
    download page, and the **official offline installer** we ship in the package
    (``chrome-offline/ChromeStandaloneSetup64.exe``, unmodified).  We never
    download or install it for the customer;
  * the browser starts with its OWN ``--user-data-dir`` and its OWN
    ``--remote-debugging-port``; the customer's day-to-day profile, bookmarks
    and logged-in accounts are never touched;
  * the minimum usable version is recorded and a major-version change is
    surfaced as a warning (selector drift risk);
  * an enterprise policy that forbids remote debugging is detected and reported
    in plain language -- never a silent failure.

Everything here is import-safe on the Linux build host: the Windows-only bits are
guarded and the pure helpers (version parsing, arg building, CDP probe, install
path list, Chrome-only resolution) are unit-tested there.
"""

from __future__ import annotations

import json
import os
import random
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .errors import ConfigError
from .jsonio import read_json, write_json
from .subprocess_text import hidden_window_options

#: official download page we point the customer at (never auto-downloaded)
CHROME_DOWNLOAD_URL = "https://www.google.cn/chrome/"

#: extra mirror of the same page (used in the English-facing text)
CHROME_DOWNLOAD_URL_GLOBAL = "https://www.google.com/chrome/"

#: ``?standalone=1`` is Google's own "download the offline installer" switch --
#: the customer stays offline-capable and installs nothing else.  We never fetch
#: or run it ourselves; the in-package copy (below) is a plain, unmodified
#: download of exactly what this page serves.
CHROME_OFFLINE_DOWNLOAD_URL = "https://www.google.com/chrome/?standalone=1"
CHROME_OFFLINE_DOWNLOAD_URL_CN = "https://www.google.cn/chrome/?standalone=1"

#: 1.2.2 (source/brief-chrome-only-20261016.md): the OFFICIAL Chrome offline
#: installer that ships next to the code, byte-for-byte as Google serves it
#: (served from Google's own host; we do not repack, patch or wrap it).  The
#: customer only runs it if Chrome turns out to be missing, and only ever
#: installs this one thing.
CHROME_BUNDLED_INSTALLER_REL = "chrome-offline/ChromeStandaloneSetup64.exe"
#: Windows-facing spelling of the same path (the customer copies this by hand)
CHROME_BUNDLED_INSTALLER_DISPLAY = r"chrome-offline\ChromeStandaloneSetup64.exe"
CHROME_BUNDLED_SOURCE_URL = "https://dl.google.com/chrome/install/ChromeStandaloneSetup64.exe"
CHROME_BUNDLED_SHA256_REL = "chrome-offline/ChromeStandaloneSetup64.exe.sha256"

#: minimum usable major version -- below this the CDP surface is too old to drive
MIN_CHROME_MAJOR = 100

CHROME_EXE = "chrome.exe"
#: msedge.exe is only ever *detected* (diagnostics) -- never launched by us.
EDGE_EXE = "msedge.exe"

#: 1.2.23 real-account fix: Chrome's native external-protocol prompt
#: ("Open xdg-open?") is outside the CDP page surface and silently eats page
#: interactions. The only reliable launch-level control is this feature flag.
DIALOG_GUARD_FEATURE = "ExternalProtocolDialog"
DIALOG_GUARD_MARKER_KEY = "dialog_guard"

#: engine ids (kept close to platforms.ENGINE_*; a plain string on purpose so
#: this module stays import-safe without the registry)
ENGINE_CHROME = "chrome"
ENGINE_EDGE = "edge"

#: 1.2.2: **Chrome is the only tier that can run.**  There is no fallback -- the
#: customer's real Google Chrome or nothing (and then we tell them so).  Edge
#: was demoted to diagnostics-only, so it is deliberately absent here.
ENGINE_TIERS = (ENGINE_CHROME,)

#: human-readable engine names for the panel / reports
ENGINE_LABELS = {ENGINE_CHROME: "Google Chrome", ENGINE_EDGE: "Microsoft Edge（系统自带）"}

#: registry keys that hold the install path (App Paths)
REGISTRY_APP_PATH_KEYS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
)

#: Edge's App Paths keys -- diagnostics only (never on the launch path).
#: Edge installs into Program Files (x86) by default.
EDGE_REGISTRY_APP_PATH_KEYS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
)

#: enterprise policy keys that can lock remote debugging
POLICY_KEYS = (
    r"SOFTWARE\Policies\Google\Chrome",
    r"SOFTWARE\Policies\Google\Chrome\RemoteDebuggingAllowed",
)

#: the exact origin the CDP websocket is allowed to come from
CDP_ALLOW_ORIGIN = "http://127.0.0.1"

# Windows process creation flags.  A browser launched from /exec must escape
# the command job object; otherwise KILL_ON_JOB_CLOSE terminates it the moment
# the HTTP request returns.
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_BREAKAWAY_FROM_JOB = 0x01000000



# --------------------------------------------------------------------- version
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?")


def parse_version(text):
    """'129.0.6668.29' -> (129, 0, 6668, 29).  None if unparseable."""
    if not text:
        return None
    match = _VERSION_RE.search(str(text))
    if not match:
        return None
    return tuple(int(g or 0) for g in match.groups())


def major_of(version):
    parsed = parse_version(version) if isinstance(version, str) else version
    return parsed[0] if parsed else None


def version_ok(version, *, minimum=MIN_CHROME_MAJOR):
    parsed = parse_version(version) if isinstance(version, str) else version
    if not parsed:
        return None  # unknown, not "bad"
    return parsed[0] >= int(minimum)


# --------------------------------------------------------------------- detect
def common_install_paths(env=None) -> list:
    """Candidate chrome.exe locations, most-likely first.

    Pure function of an environment mapping so the static host can test the
    ordering without touching a real Windows registry.
    """
    env = env if env is not None else os.environ
    out = []
    for key in ("LOCALAPPDATA",):
        base = env.get(key)
        if base:
            out.append(Path(base) / "Google" / "Chrome" / "Application" / CHROME_EXE)
    for key in ("PROGRAMFILES", "PROGRAMW6432", "PROGRAMFILES(X86)"):
        base = env.get(key)
        if base:
            out.append(Path(base) / "Google" / "Chrome" / "Application" / CHROME_EXE)
    return out


def common_edge_paths(env=None) -> list:
    """Candidate msedge.exe locations, most-likely first.

    **Diagnostics only** (1.2.2): Edge is never launched by us, this list only
    feeds the "你机器上的 Edge 不参与运行" note.  Edge is part of Windows 10/11;
    the default 64-bit install lives under ``Program Files (x86)``, so that
    comes first.  Pure function of an environment mapping, like
    ``common_install_paths``.
    """
    env = env if env is not None else os.environ
    out = []
    for key in ("PROGRAMFILES(X86)", "PROGRAMW6432", "PROGRAMFILES", "LOCALAPPDATA"):
        base = env.get(key)
        if base:
            out.append(Path(base) / "Microsoft" / "Edge" / "Application" / EDGE_EXE)
    return out


def registry_paths(*, keys=REGISTRY_APP_PATH_KEYS):
    """Install paths recorded in the Windows registry (App Paths).  [] elsewhere."""
    if os.name != "nt":
        return []
    import winreg  # noqa: WPS433 (Windows-only import on purpose)
    found = []
    for hive, hive_name in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"),
                            (winreg.HKEY_CURRENT_USER, "HKCU")):
        for key in keys:
            try:
                with winreg.OpenKey(hive, key) as handle:
                    value, _ = winreg.QueryValueEx(handle, None)
            except OSError:
                continue
            if value:
                found.append({"path": value, "source": f"registry:{hive_name}"})
    return found


def find_chrome(*, explicit=None, env=None, extra_paths=()) -> dict:
    """Locate the customer's real Chrome.  Never downloads, never installs.

    Returns {'found', 'path', 'source', 'checked': [...], 'reason'}.
    """
    checked = []
    if explicit:
        p = Path(explicit)
        checked.append(str(p))
        if p.exists():
            return {"found": True, "path": str(p), "source": "explicit",
                    "checked": checked, "reason": ""}
        return {"found": False, "path": None, "source": None, "checked": checked,
                "reason": f"指定的 Chrome 路径不存在：{p}"}

    for entry in registry_paths():
        checked.append(f"registry -> {entry['path']}")
        if Path(entry["path"]).exists():
            return {"found": True, "path": entry["path"], "source": entry["source"],
                    "checked": checked, "reason": ""}

    for p in list(extra_paths) + common_install_paths(env):
        checked.append(str(p))
        if Path(p).exists():
            return {"found": True, "path": str(p), "source": "common-path",
                    "checked": checked, "reason": ""}

    from shutil import which
    for name in (CHROME_EXE, "chrome"):
        hit = which(name)
        checked.append(f"PATH:{name}")
        if hit:
            return {"found": True, "path": hit, "source": "PATH",
                    "checked": checked, "reason": ""}

    if os.name != "nt":
        return {"found": False, "path": None, "source": None, "checked": checked,
                "reason": ("本机不是 Windows（构建机静态自检）：无法探测客户机 Chrome；"
                           "真机项见 VERIFY.md C7")}
    return {"found": False, "path": None, "source": None, "checked": checked,
            "reason": "这台电脑上没装 Google Chrome"}


def find_edge(*, explicit=None, env=None, extra_paths=()) -> dict:
    """Locate the Microsoft Edge that ships with Windows.  **Diagnostics only.**

    1.2.2 (source/brief-chrome-only-20261016.md): the three-tier fallback is
    gone -- Google Chrome is the only browser we drive.  This function survives
    solely so the panel can say "本程序要求 Chrome；你机器上的 Edge 不参与运行".
    It is NOT called on any execution path (``resolve_browser`` uses its result
    for the diagnostic note only).  Same return shape as ``find_chrome``.
    """
    checked = []
    if explicit:
        p = Path(explicit)
        checked.append(str(p))
        if p.exists():
            return {"found": True, "path": str(p), "source": "explicit",
                    "checked": checked, "reason": ""}
        return {"found": False, "path": None, "source": None, "checked": checked,
                "reason": f"指定的 Edge 路径不存在：{p}"}

    for entry in registry_paths(keys=EDGE_REGISTRY_APP_PATH_KEYS):
        checked.append(f"registry -> {entry['path']}")
        if Path(entry["path"]).exists():
            return {"found": True, "path": entry["path"], "source": entry["source"],
                    "checked": checked, "reason": ""}

    for p in list(extra_paths) + common_edge_paths(env):
        checked.append(str(p))
        if Path(p).exists():
            return {"found": True, "path": str(p), "source": "common-path",
                    "checked": checked, "reason": ""}

    from shutil import which
    for name in (EDGE_EXE, "msedge"):
        hit = which(name)
        checked.append(f"PATH:{name}")
        if hit:
            return {"found": True, "path": hit, "source": "PATH",
                    "checked": checked, "reason": ""}

    if os.name != "nt":
        return {"found": False, "path": None, "source": None, "checked": checked,
                "reason": ("本机不是 Windows（构建机静态自检）：无法探测客户机 Edge"
                           "（仅诊断用，不影响运行）")}
    return {"found": False, "path": None, "source": None, "checked": checked,
            "reason": "这台电脑上没找到系统自带的 Microsoft Edge"}


def engine_for_path(path) -> str:
    """Which engine is this executable?  ``msedge.exe`` -> Edge, else Chrome.

    Diagnostics-only identity helper (see ``browser_engine_of``): knowing that a
    path is Edge is how we REFUSE it, not how we launch it.
    """
    name = Path(str(path or "")).name.lower()
    return ENGINE_EDGE if name == EDGE_EXE else ENGINE_CHROME


def browser_engine_of(cdp_browser: str):
    """Map a CDP ``Browser`` string to an engine id (None if unrecognised).

    Chrome reports ``Chrome/130.0.0.0``; Edge reports ``Edg/130.0.0.0``.  We can
    still *name* Edge (so a stray Edge on our debug port is reported honestly
    instead of mistaken for Chrome), but only ``Chrome/`` is accepted by the
    regression harness as "the browser we meant to drive".  Anything else
    (Chromium, HeadlessChrome) is not a browser we launched for the customer.
    """
    text = cdp_browser or ""
    if "HeadlessChrome" in text or "HeadlessEdg" in text:
        return None
    if "Edg/" in text or "Edg " in text:
        return ENGINE_EDGE
    if "Chrome/" in text:
        return ENGINE_CHROME
    return None


def bundled_installer(layout=None):
    """The in-package official Chrome offline installer, if it shipped.

    Returns a ``Path`` when the file exists (and, for convenience, when we have
    no layout we still return the intended relative path).  Never executed by
    us -- it is the file the customer double-clicks when Chrome is missing.
    """
    if layout is None:
        return Path(CHROME_BUNDLED_INSTALLER_REL)
    return Path(getattr(layout, "program", ".")) / CHROME_BUNDLED_INSTALLER_REL


def edge_note(edge: dict) -> str:
    """One plain sentence: Edge is on the machine but plays no part."""
    if edge.get("present"):
        return ("本程序要求 Google Chrome；你机器上的 Microsoft Edge（"
                f"{edge.get('path')}）不参与运行，也不会被自动使用。")
    return "系统里没检测到 Microsoft Edge（不影响运行）。"


def edge_diagnostic(*, explicit=None, env=None, extra_paths=()) -> dict:
    """Edge presence for the panel/report ONLY.  Never consulted to launch."""
    found = find_edge(explicit=explicit, env=env, extra_paths=extra_paths)
    diag = {"present": bool(found["found"]), "path": found["path"],
            "source": found["source"], "checked": found["checked"],
            "note": ""}
    diag["note"] = edge_note(diag)
    return diag


def chrome_required_message(*, edge=None, layout=None) -> str:
    """The plain-language "we need Google Chrome" sentence (no fallback).

    Mentions the in-package official installer first (offline-proof), then the
    official download page.  Never suggests Edge/Chromium, and never offers to
    download or install anything for the customer.
    """
    parts = [
        "本程序需要 Google Chrome（只认官方正版 Chrome，不用 Microsoft Edge、"
        "Chromium 或其它浏览器顶替）。这台电脑上没检测到 Google Chrome。",
        "怎么装（二选一）：① 用随包附带的官方离线安装包 "
        + CHROME_BUNDLED_INSTALLER_DISPLAY
        + "（双击即可，Google 官方原版、我们未做任何改动）；"
        + "② 到官网下载安装：" + CHROME_DOWNLOAD_URL
        + "（离线版：" + CHROME_OFFLINE_DOWNLOAD_URL
        + "，国内：" + CHROME_OFFLINE_DOWNLOAD_URL_CN + "）。",
        "装完这一个即可回控制面板点「重新检测」；除 Chrome 外什么都不用装。",
    ]
    if edge is not None:
        parts.append(("顺带说明：你机器上有 Microsoft Edge（"
                      f"{edge.get('path')}），它不参与运行。") if edge.get("present")
                     else "顺带说明：系统里也没检测到 Microsoft Edge（不影响运行）。")
    return "".join(parts)


def resolve_browser(*, explicit=None, edge_explicit=None, env=None,
                    extra_paths=(), chrome_path=None, edge_path=None) -> dict:
    """**Chrome-only** resolution.  There is no fallback browser.

    source/brief-chrome-only-20261016.md: "浏览器只认 Google Chrome ... 绝不静默
    用 Edge / Chromium / 其他浏览器顶替执行".  So this either returns the
    customer's real Chrome (``found=True``, ``engine='chrome'``, ``tier=1``) or
    reports ``found=False`` with a plain-language, actionable reason.  It never
    returns Edge, Chromium or anything else as a runnable browser.

    Returns a dict that is a superset of ``find_chrome``'s result::

        {'found', 'engine', 'tier', 'path', 'source', 'checked', 'reason',
         'tiers': [ {tier, engine, found, path, source, reason}, ... ],
         'requires', 'needs_chrome', 'fallback_disabled', 'edge': {...},
         'download_url', 'offline_download_url', 'bundled_installer'}

    ``edge_explicit`` / ``edge_path`` are still accepted (diagnostics input from
    ``--edge-path``); the Edge result goes into ``'edge'`` and is never used to
    choose a browser.
    """
    explicit = explicit if explicit is not None else chrome_path
    edge_explicit = edge_explicit if edge_explicit is not None else edge_path
    checked = []
    tiers = []
    found = find_chrome(explicit=explicit, env=env, extra_paths=extra_paths)
    checked.extend(found["checked"])
    tiers.append({"tier": 1, "engine": ENGINE_CHROME, "found": found["found"],
                  "path": found["path"], "source": found["source"],
                  "reason": found["reason"]})
    edge = edge_diagnostic(explicit=edge_explicit, env=env)
    common = {"requires": ENGINE_CHROME, "fallback_disabled": True,
              "edge": edge, "checked": checked, "tiers": tiers,
              "download_url": CHROME_DOWNLOAD_URL,
              "offline_download_url": CHROME_OFFLINE_DOWNLOAD_URL,
              "bundled_installer": CHROME_BUNDLED_INSTALLER_REL}
    if found["found"]:
        return {"found": True, "engine": ENGINE_CHROME, "tier": 1,
                "path": found["path"], "source": found["source"],
                "needs_chrome": False, "reason": "", **common}
    return {"found": False, "engine": None, "tier": None, "path": None,
            "source": None, "needs_chrome": True,
            "reason": chrome_required_message(edge=edge), **common}


# --------------------------------------------------------------------- version
def file_version(path):
    """Windows file version of chrome.exe, without starting it.  None elsewhere.

    chrome.exe --version prints nothing useful on Windows (it would open the
    browser), so we read the PE version resource instead.
    """
    if os.name != "nt":
        return None
    path = str(path)
    try:
        import ctypes
        from ctypes import wintypes

        version_dll = ctypes.WinDLL("version", use_last_error=True)
        size = version_dll.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(path, 0, size, buffer):
            return None
        value = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version_dll.VerQueryValueW(
                buffer, "\\", ctypes.byref(value), ctypes.byref(length)):
            return None
        fixed = ctypes.cast(value, ctypes.POINTER(wintypes.DWORD * 4)).contents
        ms, ls = fixed[2], fixed[3]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return None


def policy_remote_debugging(registry=None, *, engine=None):
    """Is remote debugging forbidden by enterprise policy?

    Returns {'blocked': True/False/None, 'details': [...]}.  ``None`` means "we
    could not tell on this host" (Linux build host / no registry access) -- that
    is reported as unknown, never as a green light.

    1.2.2: only Google Chrome can run, so only Google Chrome's policy root is
    consulted.  The ``engine`` kwarg is accepted for call-site compatibility and
    ignored (Edge policy is irrelevant now that Edge never launches).
    """
    if os.name != "nt" and registry is None:
        return {"blocked": None, "details": ["非 Windows：策略检测跳过（静态自检）"]}
    import winreg  # noqa: WPS433
    # Chrome only: Edge is diagnostics-only, so its policy root is not read.
    roots = [(r"SOFTWARE\Policies\Google\Chrome", "Google\\Chrome")]
    details = []
    blocked = False
    for hive, hive_name in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"),
                            (winreg.HKEY_CURRENT_USER, "HKCU")):
        # Direct value under Policies\Google\Chrome
        for subkey, label in roots:
            for value_name, bad_values in (("RemoteDebuggingAllowed", (0,)),
                                           ("DeveloperToolsAvailability", (2,))):
                try:
                    with winreg.OpenKey(hive, subkey) as key:
                        value, _ = winreg.QueryValueEx(key, value_name)
                except OSError:
                    continue
                details.append(f"{hive_name}\\...\\{label} {value_name}={value}")
                if value in bad_values:
                    blocked = True
    if not details:
        details.append("未发现锁远程调试的组策略")
    return {"blocked": blocked, "details": details}


# --------------------------------------------------------------------- launch
def build_launch_args(browser_path, user_data_dir, port, url=None, *, extra=()):
    """The EXACT argv for one environment's Google Chrome (the only engine).

    ``--user-data-dir`` points at our own directory so the customer's default
    profile is never touched; ``--remote-debugging-port`` is the environment's
    own port; the window is separate from the customer's daily browser.

    1.2.2: this REFUSES a non-Chrome executable.  Building an argv for
    ``msedge.exe`` / ``chromium.exe`` is exactly the silent substitution
    source/brief-chrome-only-20261016.md forbids, so we raise ``ConfigError``
    instead of quietly driving the wrong engine.
    """
    exe_name = Path(str(browser_path or "")).name.lower()
    if exe_name != CHROME_EXE:
        raise ConfigError(
            "只允许启动 Google Chrome（chrome.exe）："
            f"收到的是 {exe_name or browser_path!r}。"
            "本程序不会用 Microsoft Edge / Chromium / 其它浏览器顶替执行。")
    args = [
        str(browser_path),
        f"--user-data-dir={user_data_dir}",
        f"--remote-debugging-port={int(port)}",
        "--no-first-run",
        "--no-default-browser-check",
        # keep the automation window visually separate from the daily browser
        "--new-window",
        f"--remote-allow-origins={CDP_ALLOW_ORIGIN}",
        # GPU 合成在部分集显/远程桌面/老驱动上会把窗口画成黑屏（客户反馈“电脑老黑屏”）。
        "--disable-gpu",
        "--disable-gpu-compositing",
        # 2026-10-29: the automation window is often occluded on the customer
        # laptop; Chrome then throttles rAF to a few frames per second and
        # Playwright's stability checks time out on a perfectly good button.
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--window-size=1440,900",
        "--window-position=80,60",
        "--disable-features=ExternalProtocolDialog,Translate,"
        "CalculateNativeWinOcclusion",
    ]
    args.extend(str(a) for a in extra)
    if url:
        args.append(url)
    return args


def cdp_version(port, *, host="127.0.0.1", timeout=1.5) -> dict:
    """Read http://host:port/json/version -- the CDP handshake.

    Returns {'ok', 'browser', 'debugger', 'error'}.  A refused connection here is
    exactly what an enterprise "remote debugging forbidden" policy looks like,
    so the caller pairs this with ``policy_remote_debugging``.
    """
    url = f"http://{host}:{int(port)}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "browser": None, "debugger": None,
                "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True,
            "browser": payload.get("Browser"),
            "debugger": payload.get("webSocketDebuggerUrl"),
            "error": ""}


def cdp_browser_alive(port, *, host="127.0.0.1", timeout=8.0) -> dict:
    """Prove the browser behind ``port`` can actually be driven.

    1.2.216: ``cdp_version`` only reads the HTTP endpoint, and that endpoint
    keeps answering while the browser-level WebSocket is wedged.  A Chrome in
    that state passed every reuse check, and the very next publish died 30s
    later inside ``connect_over_cdp`` with "发布浏览器没有启动成功".  Only a real
    handshake tells a working browser from a frozen one.
    """
    if not port:
        return {"ok": False, "browser": None, "error": "no debug port"}
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        # No playwright in this interpreter: we cannot judge the browser, and a
        # false "hung" verdict would kill a perfectly good one, so stay neutral.
        return {"ok": True, "browser": None, "skipped": True,
                "error": f"{type(exc).__name__}: {exc}"}
    url = f"http://{host}:{int(port)}"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(
                url, timeout=max(int(timeout * 1000), 1000))
            try:
                version = str(browser.version or "")
            finally:
                # close() drops the CDP connection only; the browser lives on.
                browser.close()
    except Exception as exc:
        return {"ok": False, "browser": None,
                "error": f"{type(exc).__name__}: {exc}"[:300]}
    return {"ok": True, "browser": version, "error": ""}


def cdp_pages(port, *, host="127.0.0.1", timeout=1.5) -> list:
    """List open page targets via http://host:port/json/list."""
    url = f"http://{host}:{int(port)}/json/list"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    return [target for target in payload
            if isinstance(target, dict) and target.get("type") == "page"]


def _host_of(url):
    try:
        return (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def find_open_page(port, url, *, host="127.0.0.1", timeout=1.5):
    """Return an already open tab on the same site as ``url``.

    Launching Chrome a second time with a URL makes the running instance open
    yet another window; every scheduled publish therefore left one more tab
    behind.  Reusing an existing tab on the same host keeps one window per
    account instead of one per run.
    """
    target = _host_of(url)
    if not target:
        return None
    for target_info in cdp_pages(port, host=host, timeout=timeout):
        page_url = str(target_info.get("url") or "")
        if page_url.startswith(("devtools://", "chrome-extension://")):
            continue
        if _host_of(page_url) == target:
            return target_info
    return None


def port_open(port, *, host="127.0.0.1", timeout=0.6) -> bool:
    if not port:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, int(port))) == 0


def wait_for_cdp(port, *, timeout=20.0, interval=0.5, probe=cdp_version):
    deadline = time.monotonic() + max(timeout, 0)
    last = None
    while time.monotonic() < deadline:
        last = probe(port)
        if last.get("ok"):
            return last
        time.sleep(random.uniform(interval * 0.6, interval * 1.4))
    return last or {"ok": False, "error": "timeout", "browser": None,
                    "debugger": None}


def spawn(args, *, cwd=None, spawn=None, detach=False):
    """Start a child.  ``detach`` is used for the customer's Chrome only.

    On Windows the detached child requests CREATE_BREAKAWAY_FROM_JOB.  Our own
    ProcessTree job sets BREAKAWAY_OK, so the browser leaves the /exec job and
    survives the request.  If an outer job forbids breakaway, ShellExecuteEx is
    used as a fallback (the shell owns the new process, not this request).
    """
    if spawn is not None:
        return spawn(args)
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if detach:
            flags |= DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB
    command = [str(a) for a in args]
    options = hidden_window_options(command, popen=True, creationflags=flags)
    try:
        return subprocess.Popen(command, close_fds=True, cwd=cwd, **options)
    except OSError as exc:
        if not detach or os.name != "nt":
            raise
        winerror = int(getattr(exc, "winerror", 0) or 0)
        if winerror not in (0, 5, 87):
            raise
        return _shell_execute_spawn(command, cwd)


def _shell_execute_spawn(command, cwd=None):
    """Fallback launch through the shell so no request job owns the browser."""
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "open"
    info.lpFile = str(command[0])
    info.lpParameters = subprocess.list2cmdline([str(a) for a in command[1:]])
    info.lpDirectory = str(cwd) if cwd else None
    info.nShow = 1
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    pid = 0
    if info.hProcess:
        pid = int(kernel32.GetProcessId(info.hProcess) or 0)
        kernel32.CloseHandle(info.hProcess)
    return type("DetachedProcess", (), {"pid": pid})()


def _pid_alive(pid) -> bool:
    """Safe liveness check (delegates to proc.is_pid_alive).

    ``os.kill(pid, 0)`` must never be used on Windows: CPython maps it to
    TerminateProcess, so a health check would kill the browser.
    """
    from . import proc
    return proc.is_pid_alive(pid)


def clear_stale_profile_locks(profile_dir) -> list:
    """Remove Chromium singleton leftovers only when no process owns the profile."""
    profile = Path(str(profile_dir))
    if not profile.exists():
        return []
    try:
        from . import proc
        if proc.is_profile_in_use(profile):
            return []
    except Exception:
        # If ownership cannot be established, do not delete anything.
        return []
    removed = []
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket",
                 "lockfile", "DevToolsActivePort"):
        path = profile / name
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
                removed.append(name)
        except OSError:
            continue
    return removed


def survival_probe(pid, port, *, expected_exe=None, alive=None, probe=None,
                   wait=1.5) -> dict:
    """Confirm the browser still exists after the launching request returns."""
    if alive is None and os.name != "nt":
        return {"ok": True, "reason": "NON_WINDOWS_SKIP"}
    is_alive = alive or _pid_alive
    # Check immediately as well as after the settling delay.  A launcher that
    # exits before its first child exists must never be reported as OPENED.
    if not is_alive(pid):
        return {"ok": False, "reason": "BROWSER_EXITED",
                "message": "browser process was already gone after launch"}
    time.sleep(max(0.0, float(wait)))
    if not is_alive(pid):
        return {"ok": False, "reason": "BROWSER_EXITED",
                "message": "browser process exited immediately after launch"}
    if port:
        check = probe or cdp_version
        cdp = check(port)
        if not (cdp or {}).get("ok"):
            return {"ok": False, "reason": "CDP_LOST",
                    "message": "browser process is alive but the automation endpoint stopped responding",
                    "cdp": cdp}
    return {"ok": True, "reason": "ALIVE", "pid": pid}


def resolve_browser_pid(profile_dir, *, expected_exe=None, expected_port=None,
                        fallback_pid=None, processes=None):
    """Pick the long-lived browser process, not Chrome's short launcher helper.

    Chrome's initial process may hand the URL/profile to the singleton browser
    and exit.  The profile-bound process is the one whose lifetime matters; on
    a platform where the process table cannot be read, keep the spawned PID.
    """
    try:
        from . import proc
        hits = proc.profile_processes(
            profile_dir, processes, expected_exe=expected_exe,
            expected_port=expected_port)
    except Exception:
        hits = []
    pids = []
    for item in hits:
        try:
            pid = int(item.get("pid"))
        except (TypeError, ValueError):
            continue
        if pid > 0:
            pids.append(pid)
    if pids:
        # The main browser is normally the oldest/lowest PID; deterministic
        # selection also makes the marker stable across repeated launches.
        return min(pids), True
    return fallback_pid, False


# --------------------------------------------------------------------- report
def detect(layout=None, *, explicit=None, env=None, store=True, version_fn=None,
           edge_explicit=None):
    """Full detection report: engine, path, version, policy, drift vs. last run."""
    found = resolve_browser(explicit=explicit, edge_explicit=edge_explicit, env=env)
    report = dict(found)
    report["min_major"] = MIN_CHROME_MAJOR
    report["download_url"] = CHROME_DOWNLOAD_URL
    report["offline_download_url"] = CHROME_OFFLINE_DOWNLOAD_URL
    report["engine_label"] = ENGINE_LABELS.get(found.get("engine"), "")
    version = None
    if found["found"]:
        version = (version_fn or file_version)(found["path"])
    report["version"] = version
    report["major"] = major_of(version)
    report["version_ok"] = version_ok(version)
    report["policy"] = policy_remote_debugging(engine=found.get("engine"))
    report["previous_major"] = None
    report["version_changed"] = False
    if store and layout is not None:
        state = read_json(layout.chrome_state_file, default=None)
        state = state if isinstance(state, dict) else {}
        previous = state.get("major")
        previous_engine = state.get("engine")
        report["previous_major"] = previous
        if previous is not None and report["major"] is not None \
                and previous != report["major"]:
            report["version_changed"] = True
        # an engine switch is a bigger deal than a version bump: different
        # build -> selectors must be re-checked (report calls it out).
        report["engine_changed"] = bool(
            previous_engine and found.get("engine")
            and previous_engine != found.get("engine"))
        write_json(layout.chrome_state_file, {
            "engine": report.get("engine"),
            "path": report.get("path"),
            "version": report.get("version"),
            "major": report.get("major"),
            "min_major": MIN_CHROME_MAJOR,
            "policy": report["policy"],
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "version_changed": report["version_changed"],
            "engine_changed": report["engine_changed"],
            "previous_major": previous,
            "previous_engine": previous_engine,
        })
    else:
        report["engine_changed"] = False
    report["summary"] = summarize(report)
    return report


def summarize(report) -> str:
    if not report.get("found"):
        return (report.get("reason") or chrome_required_message())
    engine = report.get("engine") or engine_for_path(report.get("path"))
    label = ENGINE_LABELS.get(engine, engine)
    parts = [f"{label} {report.get('version') or '版本未知'}"
             f"（{report.get('path')}，来源 {report.get('source')}；"
             "唯一驱动引擎，无兜底替换）"]
    edge = report.get("edge") or {}
    if edge.get("present"):
        parts.append(f"检测到你机器上还有 Microsoft Edge（{edge.get('path')}），"
                     "它不参与运行")
    if report.get("version_ok") is False:
        parts.append(f"版本低于最低要求 {MIN_CHROME_MAJOR}，请升级浏览器")
    if report.get("engine_changed"):
        parts.append(f"注意：上次记录的浏览器内核是 {report.get('previous_engine')}，"
                     f"这次是 {engine}，选择器有漂移风险，建议重跑一次回归")
    if report.get("version_changed"):
        parts.append(f"注意：大版本从 {report.get('previous_major')} 升到 "
                     f"{report.get('major')}，选择器有漂移风险，建议重跑一次回归")
    policy = report.get("policy") or {}
    if policy.get("blocked"):
        parts.append("企业策略禁止远程调试（RemoteDebuggingAllowed=0 / "
                     "DeveloperToolsAvailability=2）："
                     "请联系我们或网管放开，否则无法自动填表")
    elif policy.get("blocked") is None:
        parts.append("企业策略检测：未知（静态自检）")
    return "；".join(parts)


def reusable_instance_evidence(marker, profile, *, expected_exe, expected_port,
                               processes=None, netstat=None) -> dict:
    """Prove a launch marker still describes the live Chrome on this port.

    A JSON feature string is not enough: the marker must name this exact
    profile/port/PID, and the current OS process must still have the exact
    Chrome executable/path and the listening socket.  Any uncertainty is a
    refusal to reuse, never an optimistic match.
    """
    from . import proc
    if not isinstance(marker, dict):
        return {"ok": False, "reason": "MARKER_MISSING"}
    if marker.get(DIALOG_GUARD_MARKER_KEY) != DIALOG_GUARD_FEATURE:
        return {"ok": False, "reason": "DIALOG_GUARD_ABSENT"}
    if proc.normalize_path(marker.get("profile")) != proc.normalize_path(profile):
        return {"ok": False, "reason": "PROFILE_MISMATCH"}
    try:
        marker_port = int(marker.get("debug_port"))
        marker_pid = int(marker.get("pid"))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "MARKER_PID_OR_PORT_INVALID"}
    if marker_port != int(expected_port) or marker_pid <= 0:
        return {"ok": False, "reason": "MARKER_PORT_OR_PID_MISMATCH"}
    snapshot = processes if processes is not None else proc.list_processes()
    same_pid = [item for item in snapshot
                if str(item.get("pid")) == str(marker_pid)]
    if len(same_pid) != 1:
        return {"ok": False, "reason": "MARKER_PID_NOT_UNIQUE_OR_GONE"}
    hits = proc.profile_processes(
        profile, same_pid, expected_exe=expected_exe,
        expected_port=expected_port)
    if len(hits) != 1:
        return {"ok": False, "reason": "LIVE_ARGV_MISMATCH"}
    argv = proc.process_argv(hits[0])
    if not proc.has_disable_feature(argv, DIALOG_GUARD_FEATURE):
        return {"ok": False, "reason": "DIALOG_GUARD_ARGV_MISSING",
                "argv": argv, "pid": marker_pid}
    owners = proc.listening_pids(expected_port, netstat=netstat)
    if owners != {marker_pid}:
        return {"ok": False, "reason": "PORT_OWNER_MISMATCH",
                "owners": sorted(owners), "pid": marker_pid}
    return {"ok": True, "reason": "MARKER_AND_LIVE_PROCESS_MATCH",
            "pid": marker_pid, "port": marker_port,
            "argv": argv}


def launch_environment(env, layout, *, url=None, probe=cdp_version,
                       timeout=20.0, spawn_fn=None, chrome_path=None,
                       edge_path=None, alive_probe=cdp_browser_alive) -> dict:
    """Start the environment's browser and confirm the CDP port is live.

    Chrome-only (1.2.2): the customer's real Google Chrome or a plain-language
    "we need Chrome" stop.  No Edge, no Chromium, no substitution.
    """
    result = {"env_id": getattr(env, "env_id", None), "opened": False,
              "pid": None, "command": [], "cdp": None, "reason": "",
              "message": "", "reused": False, "engine": None, "tier": None}
    detection = resolve_browser(explicit=chrome_path, edge_explicit=edge_path)
    if not detection["found"]:
        result.update(reason="BROWSER_MISSING", tier=detection["tier"],
                      message=summarize({**detection, "min_major": MIN_CHROME_MAJOR}))
        return result
    engine = detection["engine"]
    result.update(engine=engine, tier=detection["tier"])
    version = file_version(detection["path"])
    ok = version_ok(version)
    if ok is False:
        result.update(reason="BROWSER_TOO_OLD",
                      message=f"{ENGINE_LABELS.get(engine, engine)} 版本过低（{version}），"
                              f"请升级到 {MIN_CHROME_MAJOR} 及以上：{CHROME_DOWNLOAD_URL}")
        return result
    policy = policy_remote_debugging(engine=engine)
    if policy.get("blocked"):
        result.update(reason="POLICY_BLOCKS_DEBUG",
                      message="企业策略禁止浏览器远程调试（RemoteDebuggingAllowed=0）；"
                              "请网管放开后再试")
        return result

    port = env.debug_port
    profile = env.profile_dir(layout)
    profile.mkdir(parents=True, exist_ok=True)
    marker_path = layout.env_lease_dir / f"{env.env_id}.launch.json"
    marker = read_json(marker_path, default=None)
    reusing = False
    if port and port_open(port):
        reuse = reusable_instance_evidence(
            marker, profile, expected_exe=detection["path"],
            expected_port=port)
        result["reuse_evidence"] = reuse
        if reuse.get("ok"):
            # A listening port with a matching marker is still not proof the
            # browser can be driven (see cdp_browser_alive).  Probe the real
            # handshake and replace a frozen instance instead of handing it to
            # the next publish.
            alive = alive_probe(port) if (alive_probe and port) else {"ok": True}
            result["cdp_handshake"] = alive
            if alive_probe and port and not alive.get("ok"):
                from . import proc
                killed = proc.kill_profile_browsers(
                    profile, expected_exe=detection["path"], expected_port=port)
                if not killed:
                    result.update(
                        reason="BROWSER_HUNG_LOCKED",
                        message=(f"{env.label} 的浏览器调试通道无响应（端口 {port}），"
                                 "又无法确认该进程属于本程序；程序不会终止未确认归属的"
                                 "进程，请手动关闭那个浏览器窗口后重试"))
                    return result
                for _ in range(60):
                    if not port_open(port):
                        break
                    time.sleep(random.uniform(0.20, 0.30))
                if port_open(port):
                    result.update(
                        reason="BROWSER_HUNG_LOCKED",
                        message=(f"{env.label} 的浏览器调试通道无响应（端口 {port}），"
                                 "关闭后端口仍被占用，请手动处理后重试"))
                    return result
                result["hung_browser_restarted"] = True
                reusing = False
                existing = None
            else:
                reusing = True
                existing = find_open_page(port, url) if url else None
            if existing:
                result.update(
                    opened=True, reused=True, reason="TAB_REUSED", pid=reuse["pid"],
                    runtime_argv_verified=True,
                    matched_page=str(existing.get("url") or ""),
                    cdp=cdp_version(port, timeout=2.0),
                    message=(f"{env.label} 的浏览器已开着目标页面（端口 {port}），"
                             "直接复用该标签页，不再新开窗口"))
                return result
            elif not result.get("hung_browser_restarted"):
                result.update(reused=True, reason="ALREADY_RUNNING", pid=reuse["pid"],
                              runtime_argv_verified=True,
                              message=(f"{env.label} 的浏览器已经在运行（端口 {port}），"
                                       "已在该窗口打开新标签页"))
        else:
            # A pre-1.2.23 process was launched before this flag existed. Reusing
            # it would leave the native dialog silently blocking every click, so
            # restart only the profile-bound Chrome instance; never touch the
            # customer's daily browser (exact --user-data-dir is required).
            from . import proc
            killed = proc.kill_profile_browsers(
                profile, expected_exe=detection["path"], expected_port=port)
            if not killed:
                result.update(reason="STALE_BROWSER_LOCKED",
                              message=(f"无法安全确认占用端口 {port} 的进程属于"
                                       f"{env.label} 的 Chrome 配置目录；程序已停止，"
                                       "不会终止任何未确认归属的进程。请手动关闭该窗口后重试"))
                return result
            for _ in range(20):
                if not port_open(port):
                    break
                time.sleep(random.uniform(0.20, 0.30))
            if port_open(port):
                result.update(reason="STALE_BROWSER_LOCKED",
                              message=(f"{env.label} 的旧版浏览器窗口仍占用端口 {port}，"
                                       "请关闭后重试；原生弹窗效果待现场验证"))
                return result
            result["dialog_guard_restarted"] = True
    result["stale_profile_locks_removed"] = clear_stale_profile_locks(profile)
    args = build_launch_args(detection["path"], profile, port, url)
    result["command"] = [str(a) for a in args]
    child = spawn(args, cwd=str(layout.program), spawn=spawn_fn, detach=True)
    spawned_pid = getattr(child, "pid", None)
    if reusing:
        # Chrome's singleton forwards the URL to the existing browser and the
        # short-lived launcher may exit immediately.  The marker must keep the
        # verified long-lived browser PID, not the helper process.
        result["launch_helper_pid"] = spawned_pid
    else:
        result["pid"] = spawned_pid
    result["dialog_guard"] = {
        "configured": True,
        "feature": DIALOG_GUARD_FEATURE,
        "effect": "待现场验证",
    }
    cdp = wait_for_cdp(port, timeout=timeout, probe=probe) if port else None
    result["cdp"] = cdp
    actual_pid, resolved = resolve_browser_pid(
        profile, expected_exe=detection["path"], expected_port=port,
        fallback_pid=result.get("pid") or spawned_pid)
    result["resolved_profile_process"] = bool(resolved)
    if actual_pid and actual_pid != result.get("pid"):
        result["launch_helper_pid"] = result.get("pid")
        result["pid"] = actual_pid
    if port and not (cdp or {}).get("ok"):
        # If the launch helper is already gone and no profile-bound browser
        # exists, the honest diagnosis is exit, not a debugging-port problem.
        if not resolved and not _pid_alive(result.get("pid")):
            result.update(
                opened=False, reason="BROWSER_EXITED",
                message=(f"{env.label} 的浏览器启动后立即退出。"
                         "程序已改用独立进程方式启动；请重试。"))
            return result
        result.update(
            reason="CDP_NOT_UP",
            message=(f"浏览器进程仍在，但调试端口 {port} 没响应"
                     f"（{(cdp or {}).get('error')}）。多半是企业策略禁止远程调试，"
                     "请把这句话发给我们。"))
        return result
    if port and alive_probe:
        alive = alive_probe(port)
        if not alive.get("ok"):
            time.sleep(1.5)
            alive = alive_probe(port)
        result["cdp_handshake"] = alive
        if not alive.get("ok"):
            result.update(
                opened=False, reason="CDP_HUNG",
                message=(f"{env.label} 的浏览器已启动，但调试通道没有响应"
                         f"（{alive.get('error')}）。请再试一次；如果还是这句，"
                         "把这句话发给我们"))
            return result
    # honest engine confirmation: the CDP endpoint names itself
    cdp_browser = (cdp or {}).get("browser")
    report_engine = browser_engine_of(cdp_browser)
    result["cdp_engine"] = report_engine
    result["cdp_engine_matches"] = (report_engine == engine) if report_engine else None
    # Runtime evidence: the OS process table must still show the exact Chrome
    # executable, full profile path and debug port for this spawned PID.  This
    # proves the launch argv, not that Chrome honoured the native-dialog flag.
    from . import proc
    argv_evidence = proc.verify_launch_argv(
        result.get("pid"), profile, expected_exe=detection["path"],
        expected_port=port or None)
    result["runtime_argv_verified"] = bool(argv_evidence.get("verified"))
    result["runtime_argv_evidence"] = argv_evidence
    result["dialog_guard"].update(
        runtime_argv_verified=result["runtime_argv_verified"])
    survival = survival_probe(result.get("pid"), port,
                              expected_exe=detection["path"])
    result["survival"] = survival
    if not survival.get("ok"):
        result.update(opened=False, reason=survival.get("reason") or "BROWSER_EXITED",
                      message=(f"{env.label} 的浏览器启动后立即退出。"
                               "程序已改用独立进程方式启动；请重试，仍失败请把这句话发给我们。"))
        return result
    result.update(opened=True,
                  reason="REUSED" if result["reused"] else "OPENED",
                  message=(f"已用{ENGINE_LABELS.get(engine, engine)} 拉起 {env.label}"
                           "（独立登录态目录，已在独立进程中运行）"))
    if port:
        try:
            layout.env_lease_dir.mkdir(parents=True, exist_ok=True)
            write_json(marker_path, {
                "env_id": getattr(env, "env_id", None),
                "profile": str(profile),
                "pid": result.get("pid"),
                "debug_port": port,
                DIALOG_GUARD_MARKER_KEY: DIALOG_GUARD_FEATURE,
                "runtime_argv_verified": result["runtime_argv_verified"],
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            })
        except Exception:
            pass
    return result
