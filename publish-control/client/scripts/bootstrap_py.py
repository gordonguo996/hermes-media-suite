#!/usr/bin/env python3
"""PowerShell-free installer for the Windows customer package.

360 Total Security blocks powershell.exe outright on some customer machines, so
the shipped installer must not depend on it.  This runs on the bundled runtime
python (runtime/python.exe) and performs the same essential steps:

  data area -> venv + offline wheels -> ffmpeg -> config seeds -> Chrome check
  -> desktop launcher -> self-test -> machine code

It writes install.log next to the data area and a copy on the Desktop so a
customer can send one file to support.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from winport.subprocess_text import process_encoding, text_mode_options  # noqa: E402

def read_owner_text(path, log=None):
    """Read historical FSO ANSI owners as well as UTF-8 owners."""
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "mbcs", "gbk", "latin-1"):
        try:
            text = raw.decode(encoding)
        except (UnicodeError, LookupError):
            continue
        if encoding != "utf-8-sig" and log is not None:
            log("owner encoding fallback=%s path=%s" % (encoding, path))
        return text


def supervisor_diagnostics(data):
    """Diagnostics must never replace the original failure."""
    try:
        return _supervisor_diagnostics(data)
    except Exception as exc:
        return "supervisor diagnostics unavailable: %s: %s" % (type(exc).__name__, exc)


def _supervisor_diagnostics(data):
    """Read-only failure evidence; never use diagnostic files as ownership proof."""
    run = Path(data) / "run"
    details = []

    def tail(path, limit=4096):
        try:
            with path.open("rb") as fh:
                bom = fh.read(2)
                fh.seek(0, 2)
                size = fh.tell()
                start = max(0, size - limit)
                if bom in (b"\xff\xfe", b"\xfe\xff"):
                    start -= start % 2
                fh.seek(start)
                raw = fh.read(limit + 2)
            encoding = ("utf-16-le" if bom == b"\xff\xfe" else
                        "utf-16-be" if bom == b"\xfe\xff" else "utf-8-sig")
            return "\n".join(raw.decode(encoding, errors="replace").lstrip("\ufeff").splitlines()[-12:])
        except Exception as exc:
            return "unreadable: %s: %s" % (type(exc).__name__, exc)

    owner = run / "supervisor.lock" / "owner.txt"
    try:
        raw_owner = read_owner_text(owner, details.append)
        details.append("owner %s: %s" % (owner, raw_owner))
        parsed = json.loads(raw_owner)
        details.append("owner parsed PID=%s version=%s program=%s" %
                       (parsed["pid"], parsed["version"], parsed["program"]))
    except Exception as exc:
        details.append("owner %s unreadable/invalid: %s: %s" %
                       (owner, type(exc).__name__, exc))
    paths = [run / "supervisor.lock", run / "supervisor.lock.guard"]
    try:
        staging = sorted(run.glob("supervisor.lock.new.*"))
        details.append("staging exists=%s" % bool(staging))
        paths.extend(staging)
    except OSError as exc:
        details.append("staging inventory unreadable: %s" % exc)
    for path in paths:
        try:
            stat = path.stat()
            created = stat.st_ctime if os.name == "nt" else stat.st_mtime
            details.append("%s exists=True age_seconds=%.1f" %
                           (path, max(0, time.time() - created)))
        except FileNotFoundError:
            details.append("%s exists=False" % path)
        except OSError as exc:
            details.append("%s unreadable: %s" % (path, exc))
    for path in (run / "supervisor-exit.json", Path(data) / "logs" / "install.log",
                 Path(data) / "logs" / "autostart-supervisor.log"):
        details.append("%s (tail): %s" % (path, tail(path)))
    return "\n".join(details)


DATA_SUBDIRS = [
    "profiles/env_1", "profiles/env_2", "profiles/env_wechat", "profiles/env_xhs",
    "run/envs", "profiles/douyin", "profiles/douyin-webkit", "profiles/xingtu",
    "profiles/shipinhao", "profiles/xiaohongshu", "profiles/gongzhonghao",
    "config", "run", "run/profiles", "logs", "evidence", "media", "inbox",
    "outbox", "out/douyin", "out/reports", "bin", "app",
]

CONFIG_SEEDS = [
    ("settings.json.tmpl", "settings.json"),
    ("accounts.json.tmpl", "accounts.json"),
    ("channels.json.tmpl", "channels.json"),
    ("environments.json.tmpl", "environments.json"),
]

CHROME_CANDIDATES = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
]


_LAUNCHER_NAME = "\u6f2b\u5267\u53d1\u5e03\u52a9\u624b"
_LEGACY_TASKS = ("HuolongAgent\\Agent", "HuolongAgent\\Tunnel",
                 "ManjuPublishAssistant",
                 "ManjuPublishAssistantSupervisor")
_LEGACY_RUN_VALUES = ("HuolongAgentAgent", "ManjuPublishAssistant")
# Directory names this product used across releases.  The name only selects a
# candidate; deletion still requires a valid MANIFEST version and bootstrap.
_PRODUCT_DIR_PREFIXES = ("manju-publish-assistant", "publish-assistant-",
                         "publish-worker-", "huolong-publish-")
RELEASE_TIMEOUT_SECONDS = 10.0


def _looks_like_product_dir(path) -> bool:
    try:
        name = Path(path).name.strip().lower()
    except (TypeError, ValueError):
        return False
    return any(name.startswith(prefix) for prefix in _PRODUCT_DIR_PREFIXES)


def _version_tuple(text):
    """Comparable numeric version, or None for an unparseable string.

    Only the leading dotted numbers count, so "1.2.174-r2" compares as
    (1, 2, 174).  Splitting on "." and int()-ing every part made such versions
    unparseable, which switched off the "newer than this install" guard.
    """
    match = re.match(r"\s*(\d+(?:\.\d+){0,3})", str(text or ""))
    if not match:
        return None
    return tuple(int(p) for p in match.group(1).split("."))


def _is_vendor_app_dir(path) -> bool:
    """``%LOCALAPPDATA%\\HuolongAgent\\app-1.2.x`` (older release layout)."""
    try:
        item = Path(path)
        return bool(re.match(r"app-\d", item.name.strip().lower())) \
            and item.parent.name.strip().lower() == "huolongagent"
    except (TypeError, ValueError):
        return False
RELEASE_POLL_SECONDS = 0.5


_FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"
_FOLDERID_DOWNLOADS = "{374DE290-123F-4565-9164-39C4925E467B}"


def _known_folder(guid_text: str):
    """Return a Windows Known Folder, or None when unavailable."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        class GUID(ctypes.Structure):
            _fields_ = [("data1", ctypes.c_ulong),
                        ("data2", ctypes.c_ushort),
                        ("data3", ctypes.c_ushort),
                        ("data4", ctypes.c_ubyte * 8)]

        folder = GUID()
        ctypes.memmove(ctypes.byref(folder),
                       uuid.UUID(guid_text).bytes_le, 16)
        result = ctypes.c_wchar_p()
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(GUID), ctypes.c_ulong, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p)]
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        if shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None,
                                        ctypes.byref(result)) == 0 and result.value:
            value = result.value
            try:
                ctypes.windll.ole32.CoTaskMemFree(result)
            except Exception:
                pass
            return Path(value)
    except Exception:
        pass
    return None


def desktop_dir() -> Path:
    """Windows known-folder Desktop, with ~/Desktop as the fallback."""
    return _known_folder(_FOLDERID_DESKTOP) or (
        Path(os.path.expanduser("~")) / "Desktop")


def downloads_dir() -> Path:
    """The REAL Downloads root, including a folder moved to another drive."""
    return _known_folder(_FOLDERID_DOWNLOADS) or (
        Path(os.path.expanduser("~")) / "Downloads")


def _is_winport_owner(pid, data, venv_py=None):
    root = Path(__file__).resolve().parent.parent / "agent"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from winport.port_owner import ownership, ownership_from_argv, process_cmdline

    argv = process_cmdline(pid)
    if argv is None and venv_py and Path(venv_py).exists():
        script = ("import json,psutil;"
                  "print(json.dumps(psutil.Process(%d).cmdline()))" % int(pid))
        try:
            result = subprocess.run([str(venv_py), "-c", script],
                                    capture_output=True, timeout=30,
                                    **text_mode_options())
            if result.returncode == 0:
                argv = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            argv = None
    if argv is not None:
        return ownership_from_argv(argv, data)
    return ownership(pid, data)


def _ascii_path(path) -> str:
    """Return an ASCII-only spelling of *path* (8.3 short name when needed)."""
    text = str(path)
    if text.isascii():
        return text
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetShortPathNameW(text, buf, 32768)
        if n and buf.value.isascii():
            return buf.value
    except Exception:
        pass
    return ""


def _cmdline_proves_product(pid) -> bool:
    """True when the process command line names this product's own module."""
    root = Path(__file__).resolve().parent.parent / "agent"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from winport.port_owner import process_cmdline

    cmd = " ".join(process_cmdline(pid) or []).lower()
    return ("winport" in cmd
            and ("manju-publish-assistant" in cmd or "huolongagent" in cmd))


def _healthz_probe():
    """Return ``(answered, is_winport)`` for the panel port.

    ``answered`` tells "nothing listening / hung" apart from "something else
    answered": a hung legacy agent is exactly the case the upgrade must still
    handle, while a foreign service on the port must never be killed.
    """
    try:
        import urllib.request as _u
        import json as _j

        with _u.urlopen("http://127.0.0.1:9223/healthz", timeout=3) as resp:
            data = _j.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return False, False
    if str(data.get("app", "")).lower() != "winport":
        return True, False
    # app name alone is a string anyone can serve; require a version too.
    # ``instance`` is optional: older builds predate it and must still be
    # recognised as ours, otherwise this check blocks the very upgrade it
    # exists to enable.
    return True, bool(str(data.get("version") or "").strip())


def _healthz_is_winport() -> bool:
    """True when the panel port answers as this product."""
    return _healthz_probe()[1]


def _image_path(pid):
    """Return the executable path of ``pid`` (no psutil required)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                                      int(pid))
        if not handle:
            return None
        try:
            size = wintypes.DWORD(2048)
            buf = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf,
                                                       ctypes.byref(size)):
                return None
            return buf.value
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


_PY_IMAGES = ("python.exe", "pythonw.exe", "python3.exe", "python3w.exe")


def _inside_roots(image: str, roots) -> bool:
    """True when ``image`` sits inside one of ``roots`` (no prefix tricks)."""
    if not image:
        return False
    target = os.path.normcase(os.path.abspath(image))
    for root in roots:
        if not root:
            continue
        base = os.path.normcase(os.path.abspath(str(root)))
        head = base.rstrip("\\/")
        if target == base or target.startswith(head + "\\") or target.startswith(head + "/"):
            return True
    return False


def _path_within(child, parent) -> bool:
    """Case-insensitive path containment (never a bare string prefix)."""
    try:
        child_n = os.path.normcase(os.path.abspath(str(child)))
        parent_n = os.path.normcase(os.path.abspath(str(parent)))
        return os.path.commonpath([child_n, parent_n]) == parent_n
    except (OSError, ValueError):
        return False


def _same_or_related(first, second) -> bool:
    return _path_within(first, second) or _path_within(second, first)


def _is_reparse_point(path) -> bool:
    """Detect symlinks and Windows junctions on Python 3.11 too."""
    try:
        info = os.lstat(str(path))
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & reparse)


def _valid_product_dir(path: Path):
    """Return ``(ok, reason, version)`` for a candidate old installation."""
    try:
        path = Path(path)
        if _is_reparse_point(path):
            return False, "symlink/reparse point refused", None
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return False, "junction refused", None
        if not path.is_dir():
            return False, "not a directory", None
        bootstrap = ((path / "bootstrap.bat").is_file()
                     or (path / "scripts" / "bootstrap_py.py").is_file())
        if not bootstrap:
            return False, "bootstrap entry missing", None
        manifest = path / "MANIFEST.json"
        version = None
        if manifest.is_file():
            data = json.loads(manifest.read_text(encoding="utf-8-sig"))
            version = data.get("version") if isinstance(data, dict) else None
        if version is None or str(version).strip() == "":
            # Very old builds predate MANIFEST.json.  The bootstrap entry above
            # is still a product signature; take the version from the folder
            # name (publish-assistant-1.2.49) so the upgrade can remove it.
            match = re.search(r"(\d+\.\d+\.\d+)", path.name)
            if not match:
                return False, "MANIFEST version missing", None
            version = match.group(1)
        return True, "", str(version)
    except (OSError, ValueError, UnicodeError) as exc:
        return False, "cannot validate: %s" % exc, None


def _read_launcher_text(path: Path) -> str:
    """Read old ASCII/UTF-8/UTF-16 launchers without ever raising."""
    try:
        raw = path.read_bytes()[:2 * 1024 * 1024]
    except OSError:
        return ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    for encoding in ("utf-8", "mbcs", "latin-1"):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return ""


def _program_from_launcher(command, *, data=None, log=None):
    r"""Resolve every historical Run-key launcher form to its product root.

    Handles the product's ``scripts\run-agent-task.vbs``, a desktop/start-menu
    wrapper that reads ``run\program-path.txt``, and wrappers that embed the
    program directory as a literal.  Parse failures are logged instead of
    silently reducing the old-version discovery set.
    """
    text = str(command or "").strip()

    def note(reason):
        if callable(log):
            log("  launcher parse failed: %s (%s)" % (reason, text[:240]))

    if not text:
        note("empty Run value")
        return None
    paths = re.findall(r'"([^"]+\.vbs)"', text, flags=re.IGNORECASE)
    if not paths:
        for token in text.split():
            token = token.strip().strip('"')
            if token.lower().endswith(".vbs"):
                paths.append(token)
    if not paths:
        note("no .vbs path in Run value")
        return None

    def product_at(path):
        try:
            path = Path(os.path.expandvars(str(path))).expanduser()
            if not path.is_absolute():
                return None
            if path.is_dir():
                valid, _reason, _version = _valid_product_dir(path)
                return path if valid else None
        except (OSError, ValueError):
            return None
        return None

    marker_paths = []
    if data is not None:
        marker_paths.append(Path(data) / "run" / "program-path.txt")
    marker_paths.append(
        Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        / "HuolongAgent" / "publish-worker" / "run" / "program-path.txt")

    for raw in paths:
        try:
            launcher = Path(os.path.expandvars(raw.strip().strip('"')))
        except (OSError, ValueError):
            continue
        if launcher.parent.name.lower() == "scripts":
            return launcher.parent.parent
        embedded = _read_launcher_text(launcher)
        candidates = []
        candidates.extend(re.findall(r'([A-Za-z]:[\\/][^"\'\r\n]+)',
                                    embedded))
        for quoted in re.findall(r'"([^"\r\n]+)"|\'([^\'\r\n]+)\'', embedded):
            value = quoted[0] or quoted[1]
            if value and os.path.isabs(os.path.expandvars(value)):
                candidates.append(value)
        for value in candidates:
            value = value.strip().rstrip("),;")
            if value.lower().endswith(".vbs") and \
                    Path(os.path.expandvars(value)).parent.name.lower() == "scripts":
                return Path(os.path.expandvars(value)).parent.parent
            direct = product_at(value)
            if direct is not None:
                return direct
        same_dir = product_at(launcher.parent)
        if same_dir is not None:
            return same_dir
        if "program-path.txt" in embedded:
            for marker in marker_paths:
                try:
                    recorded = marker.read_text(encoding="utf-8-sig").strip()
                except (OSError, UnicodeError):
                    continue
                recorded_dir = product_at(recorded)
                if recorded_dir is not None:
                    return recorded_dir
        note("launcher has no resolvable embedded or program-path target: %s"
             % launcher)
    return None


def _read_run_values():
    """Read the legacy HKCU Run launchers before they are replaced."""
    if os.name != "nt":
        return {}
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_READ)
    except (ImportError, OSError):
        return {}
    values = {}
    try:
        for name in _LEGACY_RUN_VALUES:
            try:
                value, _ = winreg.QueryValueEx(key, name)
            except OSError:
                continue
            values[name] = str(value)
    finally:
        try:
            winreg.CloseKey(key)
        except OSError:
            pass
    return values


def _discover_old_version_candidates(program, data, *, run_values=None,
                                     local_appdata=None, log=None):
    """Merge the four discovery sources into ``{absolute: {labels}}``.

    Paths stay lexical here.  Resolving a symlink/junction at discovery time
    would hide the reparse point from the final safety check and could turn a
    link into a different product directory before deletion.
    """
    program = Path(program).resolve()
    data = Path(data).resolve()
    found = {}

    def add(candidate, label):
        if candidate is None:
            return
        try:
            text = os.path.expandvars(str(candidate).strip())
            if not text or not os.path.isabs(text):
                return
            candidate = Path(os.path.abspath(text))
        except (OSError, ValueError):
            return
        if candidate == program:
            return
        found.setdefault(candidate, set()).add(label)

    try:
        siblings = list(program.parent.iterdir())
    except OSError:
        siblings = []
    for item in siblings:
        if _looks_like_product_dir(item):
            add(item, "sibling")

    marker = data / "run" / "program-path.txt"
    try:
        if marker.is_file():
            recorded = marker.read_text(encoding="utf-8-sig").strip()
            if recorded:
                add(Path(recorded), "program-path")
    except (OSError, UnicodeError):
        pass

    if run_values is None:
        run_values = _read_run_values()
    for name, command in (run_values or {}).items():
        add(_program_from_launcher(command, data=data, log=log),
            "HKCU Run:%s" % name)

    if local_appdata is None:
        local_appdata = (os.environ.get("LOCALAPPDATA")
                         or str(Path.home() / "AppData" / "Local"))
    vendor = Path(local_appdata) / "HuolongAgent"
    scan = [("LOCALAPPDATA app-*", vendor, ("app-",)),
            ("LOCALAPPDATA Programs", Path(local_appdata) / "Programs", ()),
            ("LOCALAPPDATA Programs/HuolongAgent",
             Path(local_appdata) / "Programs" / "HuolongAgent", ())]
    # Customers extract the zip wherever the browser put it (Downloads) or
    # double-click it on the Desktop.  Those copies are exactly what keeps
    # blocking an upgrade, so they are discovery sources too.
    for label, folder in (("Desktop", _known_folder(_FOLDERID_DESKTOP)),
                          ("Downloads", _known_folder(_FOLDERID_DOWNLOADS))):
        if folder is not None:
            scan.append((label, Path(folder), ()))
    for label, folder, extra_prefixes in scan:
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for item in entries:
            try:
                if not item.is_dir():
                    continue
            except OSError:
                continue
            low = item.name.strip().lower()
            if any(low.startswith(prefix) for prefix in extra_prefixes) \
                    or _looks_like_product_dir(item):
                add(item, label)
                if not _valid_product_dir(item)[0]:
                    # Old downloads were often unpacked into a container
                    # folder; the real version directory sits one level down.
                    try:
                        children = [c for c in item.iterdir() if c.is_dir()]
                    except OSError:
                        children = []
                    for child in children:
                        if _looks_like_product_dir(child):
                            add(child, label + "-container")
    return found


def _safe_old_version_dir(candidate, program, data, *, protected_roots=()):
    """Final allow-list check immediately before any kill/delete."""
    try:
        path = Path(candidate)
        if _is_reparse_point(path):
            return False, "symlink/reparse point refused"
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return False, "junction refused"
        resolved = path.resolve(strict=True)
    except OSError as exc:
        return False, "cannot resolve: %s" % exc
    if len(resolved.parts) <= 1:
        return False, "filesystem root refused"
    try:
        if resolved == Path(resolved.anchor):
            return False, "volume/drive root refused"
    except (OSError, ValueError):
        return False, "volume root is unreadable"
    if _same_or_related(resolved, program):
        return False, "inside or ancestor of the current program directory"
    if _same_or_related(resolved, data):
        return False, "inside or ancestor of the data directory"
    for root in protected_roots:
        try:
            root_path = Path(root).expanduser().resolve()
        except OSError:
            continue
        # Never delete the protected folder itself or anything containing it.
        if resolved == root_path or _path_within(root_path, resolved):
            return False, "is or contains a protected root: %s" % root
        # Inside Desktop/Downloads/the user profile only a directory that
        # already proves it is this product may be removed: a customer who
        # extracted the zip there must not have to uninstall by hand, while
        # their own files stay untouchable.  Product validation (MANIFEST
        # version + bootstrap entry) is still applied by the caller.
        if _path_within(resolved, root_path) \
                and not (_looks_like_product_dir(resolved)
                         or _is_vendor_app_dir(resolved)):
            return False, "not a product directory inside %s" % root
    return True, "ok"


def _tree_size(path) -> int:
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    pass
    except OSError:
        return total
    return total


def _format_size(value) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%.1f GB" % size


def _tasklist_pids():
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                                capture_output=True, timeout=30,
                                **text_mode_options())
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("tasklist inventory uncertain: %s" % exc) from exc
    if result.returncode != 0:
        raise RuntimeError("tasklist inventory uncertain: rc=%s" % result.returncode)
    pids = []
    for row in csv.reader((result.stdout or "").splitlines()):
        if len(row) < 2:
            continue
        try:
            pids.append(int(row[1]))
        except ValueError:
            continue
    return pids


def _stop_processes_in_dir(root, log) -> int:
    """Kill only processes whose executable image lives inside *root*."""
    stopped = 0
    for pid in _tasklist_pids():
        if pid == os.getpid():
            continue
        image = _image_path(pid) or ""
        if not image or not _path_within(image, root):
            continue
        log("  stopping process pid=%s image=%s" % (pid, image))
        try:
            result = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                    capture_output=True, timeout=20)
            log("    taskkill rc=%s" % result.returncode)
        except (OSError, subprocess.SubprocessError) as exc:
            log("    taskkill failed: %s" % exc)
            continue
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and _process_alive(pid):
            # wait-ok: poll
            time.sleep(0.25)
        if _process_alive(pid):
            log("    WARNING: pid %s did not exit" % pid)
        else:
            stopped += 1
    return stopped


def _rmtree_onerror(func, path, exc_info):
    """Clear the read-only bit once, then retry the failed removal call."""
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    func(path)


def _remove_tree_with_retry(path, log, *, attempts=5, delay=1.0):
    """Delete *path*; return ``(ok, last_error)`` after at most five tries."""
    last = None
    for attempt in range(1, attempts + 1):
        try:
            if not Path(path).exists():
                return True, None
            shutil.rmtree(str(path), onerror=_rmtree_onerror)
            if not Path(path).exists():
                return True, None
            last = OSError("directory still exists")
        except OSError as exc:
            last = exc
        if attempt < attempts:
            log("  delete attempt %d/%d failed: %s" % (attempt, attempts, last))
            time.sleep(delay)
    return False, last

def _is_elevated() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _healthz_ok() -> bool:
    try:
        import urllib.request as _u
        with _u.urlopen("http://127.0.0.1:9223/healthz", timeout=2) as resp:
            return b'"ok"' in resp.read(200)
    except Exception:
        return False


def _healthz_payload():
    """Return the /healthz JSON object, or None when it is not readable."""
    try:
        import urllib.request as _u
        with _u.urlopen("http://127.0.0.1:9223/healthz", timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _pid_on_port(port: int):
    try:
        result = subprocess.run(["netstat", "-ano"], capture_output=True,
                                timeout=15, **text_mode_options())
        if result.returncode != 0:
            raise RuntimeError("netstat query failed")
        out = result.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        if os.name != "nt":
            return None
        raise RuntimeError("port inventory unavailable") from exc
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper().startswith("TCP") \
                and parts[1].endswith(":%d" % port) and parts[3].upper() == "LISTENING":
            try:
                return int(parts[4])
            except ValueError as exc:
                raise RuntimeError("invalid listener PID") from exc
    return None


def _process_alive(pid):
    """Best-effort liveness check; uncertain results count as alive."""
    try:
        import psutil  # type: ignore
        return bool(psutil.pid_exists(int(pid)))
    except ImportError:
        pass
    except Exception:
        return True
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % int(pid), "/NH"],
                capture_output=True, timeout=15, **text_mode_options())
        except (OSError, subprocess.SubprocessError):
            return True
        if result.returncode != 0:
            return True
        return str(int(pid)) in (result.stdout or "").split()
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    except Exception:
        return True
    return True


def _wait_for_release(pid, *, timeout=None, poll=None):
    """Poll until ``pid`` is gone AND port 9223 has no listener."""
    if timeout is None:
        timeout = RELEASE_TIMEOUT_SECONDS
    if poll is None:
        poll = RELEASE_POLL_SECONDS
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        owner = _pid_on_port(9223)
        alive = _process_alive(pid)
        if owner is None and not alive:
            return True, owner, alive
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, owner, alive
        time.sleep(min(max(0.01, float(poll)), remaining))


def _read_back(path: Path) -> bytes:
    return path.read_bytes()


def _write_verified_bytes(path: Path, payload: bytes, label: str) -> None:
    """Write bytes, flush, then read back and require an exact match."""
    temporary = path.with_name(path.name + ".tmp-%d" % os.getpid())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(temporary, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
        actual = _read_back(path)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise RuntimeError("cannot write %s %s: %s" % (label, path, exc))
    if actual != payload:
        raise RuntimeError("%s read-back mismatch at %s (wrote %d bytes, read %d)"
                           % (label, path, len(payload), len(actual)))


def _update_run_value(reg, key, name, value, legacy_names, reg_type, log) -> None:
    """Overwrite the live autostart value, verify it, then drop duplicates.

    ``reg`` is the winreg module (or a double with the same module-level
    signatures: SetValueEx(key, name, reserved, type, value) ...).  Order
    matters: the old code deleted every known value first and only then wrote
    the new one, so a failure in between left HKCU Run empty while the previous
    program directory had already been removed -- the machine could never start
    the agent again.  The read-back is mandatory: an unverifiable value is a
    failure, never an assumed success.
    """
    reg.SetValueEx(key, name, 0, reg_type, value)
    try:
        stored = reg.QueryValueEx(key, name)[0]
    except OSError as exc:
        raise RuntimeError("cannot verify autostart value %s: %s"
                           % (name, exc)) from exc
    if stored != value:
        raise RuntimeError("autostart read-back mismatch: %r != %r"
                           % (stored, value))
    for legacy in legacy_names:
        if legacy.casefold() == name.casefold():
            continue
        try:
            reg.DeleteValue(key, legacy)
            log("legacy autostart value removed: %s" % legacy)
        except FileNotFoundError:
            continue
        except OSError as exc:
            # The live value is already correct; a leftover duplicate is worth
            # a warning, not a failed install, but it must not pass silently.
            log("WARNING: legacy autostart value not removed: %s (%s)"
                % (legacy, exc))


def _install_start_paths(*, supervisor, target, payload, autostart, log):
    """Register autostart and the desktop icon in the safe order.

    With a supervisor present the icon is cosmetic: write the Run value first
    and let a locked .vbs be skipped.  Without one the desktop launcher IS the
    start path, so it must exist before Run points at it and a failure is fatal.
    """
    if supervisor is not None and supervisor.is_file():
        autostart(supervisor)
        return _write_desktop_launcher(target, payload, log, allow_skip=True)
    entry = _write_desktop_launcher(target, payload, log, allow_skip=False)
    autostart(entry)
    return entry


def _write_desktop_launcher(target: Path, payload: bytes, log,
                            *, allow_skip: bool):
    """Write the desktop launcher without ever destroying the working one.

    Windows refuses to replace a .vbs that wscript.exe still has open
    (WinError 5).  Deleting the target first would remove the only entry the
    user has, so a blocked replace falls back to a second file name instead.
    ``allow_skip`` is only true when autostart already points at the program
    supervisor, which makes the desktop icon cosmetic rather than load-bearing.
    """
    try:
        _write_verified_bytes(target, payload, "desktop launcher")
        log("launcher created: %s" % target)
        return target
    except RuntimeError as exc:
        log("desktop launcher blocked: %s" % exc)
    alternate = target.with_name("%s-%s%s" % (target.stem, "new", target.suffix))
    try:
        _write_verified_bytes(alternate, payload, "desktop launcher (alternate)")
        log("launcher created under alternate name: %s" % alternate)
        return alternate
    except RuntimeError as exc:
        if allow_skip:
            log("WARNING: desktop launcher skipped: %s" % exc)
            return None
        raise

def _purge_leftover_dirs(roots, keep, current_version, log, protected=()) -> int:
    """Stop processes and delete verified old product directories.

    Split out of the installer step so the deletion rules can be exercised
    without Windows: the inputs are paths, a version string and a logger.
    """
    removed_count = 0
    seen = set()

    def purge_one(entry) -> bool:
        nonlocal removed_count
        low = entry.name.lower()
        try:
            if not entry.is_dir():
                return False
        except OSError:
            return False
        norm = os.path.normcase(os.path.abspath(str(entry)))
        if norm in keep or norm in seen:
            return False
        seen.add(norm)
        # A junction/symlink named like a product directory must never be
        # followed: rmtree would delete the link target instead.
        if _is_reparse_point(entry):
            log("purge: skip %s (symlink/reparse point)" % entry)
            return False
        is_junction = getattr(entry, "is_junction", None)
        if callable(is_junction) and is_junction():
            log("purge: skip %s (junction)" % entry)
            return False
        # Never remove the running installer, its data, or an ancestor of
        # either (a package nested one level deeper than usual).
        if any(_same_or_related(entry, item) for item in protected):
            log("purge: skip %s (current install or data)" % entry)
            return False
        valid, reason, version = _valid_product_dir(entry)
        if not valid:
            log("purge: skip %s (%s)" % (entry, reason))
            return False
        if version == current_version:
            log("purge: skip %s (same version as this install)" % entry)
            return False
        have, want = _version_tuple(version), _version_tuple(current_version)
        if have is None or want is None:
            # Cannot prove it is older: deleting is irreversible, keep it.
            log("purge: skip %s (version %r not comparable with %r)"
                % (entry, version, current_version))
            return False
        if have >= want:
            log("purge: skip %s (newer than this install)" % entry)
            return False
        stopped = 0
        try:
            stopped = _stop_processes_in_dir(entry, log)
        except Exception as exc:  # cleanup must never break the install
            log("purge: process stop in %s failed: %s" % (entry, exc))
        removed, last_error = _remove_tree_with_retry(entry, log)
        if not removed:
            # A supervisor may have relaunched the agent between the kill
            # and the delete; kill again and retry before giving up.
            try:
                _stop_processes_in_dir(entry, log)
            except Exception:
                pass
            removed, last_error = _remove_tree_with_retry(entry, log)
        if removed:
            removed_count += 1
            log("purge: removed old install dir %s (version=%s, "
                "processes stopped=%d)" % (entry, version, stopped))
            return True
        log("purge: could not remove %s yet (%s); the later "
            "cleanup pass will retry" % (entry, last_error))
        return False

    for root in roots:
        try:
            if not root.is_dir():
                continue
            entries = sorted(root.iterdir())
        except OSError as exc:
            log("purge: cannot list %s: %s" % (root, exc))
            continue
        vendor_root = "huolongagent" in str(root).lower()
        for entry in entries:
            low = entry.name.lower()
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            # Real leftovers seen on a customer machine:
            #   app-1.2.118 / app-1.2.119 (old program dirs)
            #   publish-worker-1.2.x, publish-assistant-1.2.x
            if not ((vendor_root and low.startswith("app-1."))
                    or low.startswith("publish-worker-")
                    or low.startswith("publish-assistant-")
                    or low.startswith("manju-publish-assistant-")
                    or low.startswith("huolong-publish-")
                    or low.startswith("publish-worker_old")):
                continue
            if purge_one(entry):
                continue
            # Old downloads were often unpacked into a container folder
            # ("manju-publish-assistant-1/<version>/"), so the version
            # directory is one level deeper and still blocks nothing but
            # wastes gigabytes.  Descend one level and clean those too.
            try:
                nested = [c for c in entry.iterdir()
                          if c.is_dir() and _looks_like_product_dir(c)]
            except OSError:
                nested = []
            for child in nested:
                purge_one(child)
            try:
                if entry.is_dir() and not any(entry.iterdir()):
                    entry.rmdir()
                    log("purge: removed empty container %s" % entry)
            except OSError:
                pass
    return removed_count

class Installer:
    def __init__(self, program: Path) -> None:
        self.program = program
        try:
            self.version = (program / "VERSION").read_text(
                encoding="utf-8-sig").strip()
        except OSError:
            self.version = ""
        self.data = Path(os.environ["LOCALAPPDATA"]) / "HuolongAgent" / "publish-worker"
        self.data_existed = self.data.exists()
        self.runtime_py = program / "runtime" / "python.exe"
        self.venv_py = self.data / ".venv" / "Scripts" / "python.exe"
        self.log_path = self.data / "logs" / "install.log"
        self.lines: list[str] = []
        self.problems: list[str] = []
        self.license_handover_ok: bool | None = None
        self.license_problem_reported = False

    # ---------- logging ----------
    def log(self, msg: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} {msg}"
        self.lines.append(line)
        try:
            print(msg, flush=True)
        except (OSError, UnicodeError):
            pass  # A detached/legacy console must not prevent file logging.
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def flush_desktop_copy(self) -> None:
        try:
            folder = desktop_dir()
            folder.mkdir(parents=True, exist_ok=True)
            desktop = folder / "install-log.txt"
            with open(desktop, "w", encoding="utf-8") as fh:
                fh.write("\n".join(self.lines[-400:]) + "\n")
        except OSError:
            pass

    # ---------- helpers ----------
    def _python_process_snapshot(self):
        """Python 进程清单（带解释器路径与命令行）。

        ``_process_snapshot`` 只收 wscript/cscript，用来判断"某个 vbs 是否在跑"。
        清理孤儿代理要找的是 python 进程，必须单独枚举，否则清理分支永远不会命中。
        """
        if os.name != "nt":
            return []
        if not self.venv_py.is_file():
            raise RuntimeError("process inventory interpreter missing")
        script = (
            "import json,psutil\n"
            "out=[]\n"
            "for p in psutil.process_iter(['pid','name','exe','cmdline']):\n"
            "    try:\n"
            "        name=(p.info.get('name') or '')\n"
            "        if not name.lower().startswith('python'): continue\n"
            "        argv=p.info.get('cmdline') or []\n"
            "        if not argv: continue\n"
            "        out.append({'pid':p.info['pid'],'name':name,"
            "'exe':p.info.get('exe') or '','cmdline':argv})\n"
            "    except psutil.NoSuchProcess: pass\n"
            "print(json.dumps(out))\n")
        result = subprocess.run([str(self.venv_py), "-c", script],
                                capture_output=True, timeout=30,
                                **text_mode_options())
        if result.returncode != 0:
            raise RuntimeError("python process inventory failed (rc=%s)" % result.returncode)
        try:
            value = json.loads(result.stdout)
            if not isinstance(value, list):
                raise ValueError("not a list")
        except (ValueError, TypeError) as exc:
            raise RuntimeError("invalid python process inventory") from exc
        return value

    def _process_snapshot(self):
        """An incomplete process inventory is an error, never an empty machine."""
        if os.name != "nt":
            return []
        if not self.venv_py.is_file():
            raise RuntimeError("process inventory interpreter missing")
        script = (
            "import json,psutil\n"
            "out=[]\n"
            "for p in psutil.process_iter():\n"
            "    try:\n"
            "        name=p.name()\n"
            "        if name.lower() not in ('wscript.exe','cscript.exe'): continue\n"
            "        argv=p.cmdline()\n"
            "        if not argv: raise RuntimeError('unreadable script command line')\n"
            "        out.append({'pid':p.pid,'name':name,'cmdline':argv})\n"
            "    except psutil.NoSuchProcess: pass\n"
            "print(json.dumps(out))\n")
        result = subprocess.run([str(self.venv_py), "-c", script],
                                capture_output=True, timeout=30,
                                **text_mode_options())
        if result.returncode != 0:
            raise RuntimeError("process inventory failed (rc=%s)" % result.returncode)
        try:
            value = json.loads(result.stdout)
            if not isinstance(value, list):
                raise ValueError("not a list")
            for item in value:
                if (not isinstance(item, dict) or int(item.get("pid", 0)) <= 0
                        or not isinstance(item.get("cmdline"), list)):
                    raise ValueError("invalid process entry")
        except (ValueError, TypeError) as exc:
            raise RuntimeError("invalid process inventory") from exc
        return value

    def _supervisor_ready_current(self):
        target = os.path.normcase(os.path.abspath(
            str(self.program / "scripts" / "run-agent-task.vbs")))
        supervisors = []
        for item in self._process_snapshot():
            for arg in item["cmdline"]:
                arg = str(arg).strip('"')
                if arg.lower().endswith("run-agent-task.vbs"):
                    supervisors.append((int(item["pid"]), os.path.normcase(os.path.abspath(arg))))
        try:
            owner = json.loads(read_owner_text(self.data / "run" / "supervisor.lock" /
                                               "owner.txt", self.log))
            expected = (int(owner["pid"]), target)
            return (supervisors == [expected] and expected[0] > 0
                    and owner.get("version") == self.version
                    and os.path.normcase(os.path.abspath(owner["program"])) ==
                    os.path.normcase(os.path.abspath(str(self.program))))
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _win_kill_data_processes(self, dry_run: bool = False) -> int:
        """Terminate every python/pythonw running from the product data dir.

        1.2.178: this must not use psutil.  A failed upgrade leaves the venv's
        psutil broken, and that is exactly the state this repair runs in --
        the old code asked the broken component for the process inventory and
        died with "process inventory failed" instead of repairing anything.
        Toolhelp32 + QueryFullProcessImageName come from kernel32, so this
        works before any dependency is installed, and it is Unicode-safe for
        Chinese user names.
        """
        if os.name != "nt":
            return 0
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        TH32CS_SNAPPROCESS = 0x00000002
        PROCESS_TERMINATE = 0x0001
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        # Handles are pointer-sized: without explicit argtypes ctypes would
        # truncate them to 32 bits and every TerminateProcess call would fail.
        k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.TerminateProcess.restype = wintypes.BOOL
        k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]

        def image_path(pid):
            handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return ""
            try:
                buf = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(32768)
                if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    return buf.value
                return ""
            finally:
                k32.CloseHandle(handle)

        def snapshot():
            snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snap == INVALID_HANDLE_VALUE:
                raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
            rows = []
            try:
                entry = PROCESSENTRY32W()
                entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                ok = k32.Process32FirstW(snap, ctypes.byref(entry))
                while ok:
                    rows.append((int(entry.th32ProcessID), entry.szExeFile))
                    ok = k32.Process32NextW(snap, ctypes.byref(entry))
            finally:
                k32.CloseHandle(snap)
            return rows

        mine = os.getpid()
        data_dir = os.path.normcase(os.path.abspath(str(self.data)))
        venv_dir = os.path.normcase(os.path.abspath(str(self.data / ".venv")))
        # The supervisor holds the agent open; kill it first so the agent is
        # not relaunched while we are terminating it.  owner.txt is the only
        # place its pid is recorded when the task was not used to start it.
        candidates = set()
        try:
            owner = json.loads(read_owner_text(
                self.data / "run" / "supervisor.lock" / "owner.txt", self.log))
            owner_pid = int(owner.get("pid") or 0)
            if owner_pid > 0:
                candidates.add(owner_pid)
        except Exception:
            pass
        killed = 0
        rows = snapshot()
        names = {pid: name.lower() for pid, name in rows}
        for pid in candidates:
            if pid == mine or names.get(pid) not in ("wscript.exe", "cscript.exe"):
                continue
            if dry_run:
                self.log("would stop old supervisor pid=%s" % pid)
                killed += 1
                continue
            handle = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if handle:
                k32.TerminateProcess(handle, 1)
                k32.CloseHandle(handle)
                killed += 1
                self.log("stopped old supervisor pid=%s" % pid)
        for pid, name in rows:
            if pid == mine or pid in candidates:
                continue
            if name.lower() not in ("python.exe", "pythonw.exe"):
                continue
            path = os.path.normcase(image_path(pid))
            if not path:
                continue
            if path.startswith(venv_dir) or path.startswith(data_dir + os.sep):
                if dry_run:
                    self.log("would stop product process pid=%s image=%s" % (pid, path))
                    killed += 1
                    continue
                handle = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
                if handle:
                    k32.TerminateProcess(handle, 1)
                    k32.CloseHandle(handle)
                    killed += 1
                    self.log("stopped product process pid=%s image=%s" % (pid, path))
        # Report what survived instead of silently assuming success.
        remaining = []
        for pid, name in snapshot():
            if pid == mine or name.lower() not in ("python.exe", "pythonw.exe"):
                continue
            path = os.path.normcase(image_path(pid))
            if path.startswith(venv_dir) or path.startswith(data_dir + os.sep):
                remaining.append("%s:%s" % (pid, path))
        if remaining:
            self.log("WARNING: product processes still running: %s"
                     % ", ".join(remaining))
        return killed

    def step_quiesce(self) -> None:
        r"""Stop old supervisors/agents BEFORE anything writes into the venv.

        1.2.178: this used to run only in step_stop_running(), i.e. after
        step_wheels().  pip then overwrote
        .venv\Lib\site-packages\psutil\_psutil_windows.pyd while an old
        pythonw.exe still had it mapped, and the upgrade died with
        "[WinError 5] Access is denied" before it ever reached the stop step.
        Every customer upgrading over a running install hit this.
        """
        if os.name != "nt":
            return
        for name in ("ManjuPublishAssistantSupervisor",):
            for argv in (["schtasks", "/End", "/TN", name],
                         ["schtasks", "/Change", "/TN", name, "/DISABLE"]):
                try:
                    subprocess.run(argv, capture_output=True, timeout=30)
                except (OSError, subprocess.SubprocessError) as exc:
                    self.log("quiesce: %s failed: %s" % (argv[1], exc))
        try:
            self._win_kill_data_processes()
        except Exception as exc:  # repair is best effort; pip retry follows
            self.log("WARNING: process cleanup failed: %s: %s"
                     % (type(exc).__name__, exc))

    def step_purge_old_leftovers(self) -> None:
        """Stop and delete what an older install left behind.

        Reported by the customer: the old package kept files, locks and a
        running supervisor around, so the new install could not take over and
        the customer had to uninstall by hand.  This runs right after quiesce,
        never touches the directory this install is using, and retries later
        in step_cleanup_old_versions if a process still pins the files.
        """
        if os.name != "nt":
            return
        keep = {os.path.normcase(os.path.abspath(str(self.data))),
                os.path.normcase(os.path.abspath(str(self.program)))}
        roots = []
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots += [Path(local) / "HuolongAgent",
                      Path(local) / "Programs",
                      Path(local) / "Programs" / "HuolongAgent"]
        for folder in (_known_folder(_FOLDERID_DESKTOP),
                       _known_folder(_FOLDERID_DOWNLOADS)):
            if folder is not None:
                roots.append(Path(folder))
        removed = _purge_leftover_dirs(roots, keep, self.version, self.log,
                                       protected=(self.program, self.data))
        if removed:
            self.log("purge: removed %d old install dir(s)" % removed)
        # Locks left by a crashed run make the new supervisor refuse to start.
        run = self.data / "run"
        for pattern in ("supervisor.lock", "supervisor.lock.guard"):
            target = run / pattern
            if not target.exists():
                continue
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
                self.log("purge: removed stale lock %s" % target)
            except Exception as exc:
                self.log("purge: lock %s still present: %s" % (target, exc))

    def _stop_legacy_supervisors(self):
        """Stop old supervisors, including same-path owners from an overwrite.

        Installation restarts even a current owner before killing its agent,
        so the agent stop cannot trigger the old supervisor's crash backoff.
        """
        target = os.path.normcase(os.path.abspath(
            str(self.program / "scripts" / "run-agent-task.vbs")))
        keep_current = False  # coordinated reinstall: stop supervisor before agent
        stopped = 0
        for item in self._process_snapshot():
            try:
                name = str(item.get("name") or "").lower()
                if name not in ("wscript.exe", "cscript.exe"):
                    continue
                script = ""
                for part in item.get("cmdline") or []:
                    text = str(part).strip().strip('"')
                    if text.lower().endswith("run-agent-task.vbs"):
                        script = text
                        break
                if not script:
                    continue
                if os.path.normcase(os.path.abspath(script)) == target and keep_current:
                    continue
                pid = int(item.get("pid") or 0)
                if pid <= 0:
                    continue
                try:
                    old_version = (Path(script).resolve().parent.parent /
                                   "VERSION").read_text(
                                       encoding="utf-8-sig").strip()
                except OSError:
                    old_version = "?"
                self.log("stopping old supervisor pid=%s version=%s script=%s"
                         % (pid, old_version, script))
                res = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                     capture_output=True, timeout=20)
                self.log("  taskkill rc=%s" % res.returncode)
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline and _process_alive(pid):
                    # wait-ok: poll
                    time.sleep(0.25)
                if _process_alive(pid):
                    raise RuntimeError(
                        "old supervisor pid %s did not exit" % pid)
                else:
                    stopped += 1
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise RuntimeError("old supervisor stop failed: %s" % exc) from exc
        remaining = []
        for item in self._process_snapshot():
            try:
                name = str(item.get("name") or "").lower()
                if name not in ("wscript.exe", "cscript.exe"):
                    continue
                for part in item.get("cmdline") or []:
                    text = str(part).strip().strip('"')
                    if (text.lower().endswith("run-agent-task.vbs")
                            and (os.path.normcase(os.path.abspath(text)) != target
                                 or not keep_current)):
                        remaining.append((int(item.get("pid") or 0), text))
                        break
            except (OSError, ValueError):
                continue
        # A supervisor can die while its agent keeps running, and an older
        # install may have left its own ``python -m winport`` behind.  Such an
        # orphan keeps answering publish commands from ITS root, so the freshly
        # installed agent is shadowed by stale code and stale licence state
        # (seen on a real upgrade: the old process still had the pre-upgrade
        # seat list and refused every publish with "该号未授权").  Kill every
        # winport process that does not live under THIS program directory.
        program_norm = os.path.normcase(os.path.abspath(str(self.program)))
        data_norm = os.path.normcase(os.path.abspath(str(self.data)))

        def _under(path, root):
            try:
                return os.path.normcase(os.path.abspath(str(path))).startswith(root)
            except (OSError, ValueError):
                return False

        for item in self._python_process_snapshot():
            try:
                name = str(item.get("name") or "").lower()
                if not name.startswith("python"):
                    continue
                cmdline = [str(part) for part in (item.get("cmdline") or [])]
                # 只认常驻代理：``-m winport``（模块名必须精确是 winport）。
                # ``-m winport.cli <subcommand>`` 是一次性任务，全部跳过。
                module = ""
                for idx, part in enumerate(cmdline[:-1]):
                    if part == "-m" or part == "--module":
                        module = cmdline[idx + 1].strip().lower()
                        break
                if module != "winport":
                    continue
                if "-m" in cmdline:
                    after = cmdline[cmdline.index("-m") + 2:]
                    positional = []
                    skip_next = False
                    for part in after:
                        if skip_next:
                            skip_next = False
                            continue
                        if part.startswith("--"):
                            # 选项后面跟的值（--data D:\x）不是位置参数
                            skip_next = "=" not in part
                            continue
                        if not part.startswith("-"):
                            positional.append(part)
                    if positional:
                        # -m winport 后面还有子命令，是一次性任务，不动
                        continue
                image = str(item.get("exe") or (cmdline[0] if cmdline else ""))
                image = image.strip().strip('"')
                if image and (_under(image, program_norm) or _under(image, data_norm)):
                    continue
                paths = [part.strip().strip('"') for part in cmdline
                         if part and not part.startswith("-")]
                # --data=<路径> / --program=<路径> 这种写法也要参与归属判断
                for part in cmdline:
                    if part.startswith("--") and "=" in part:
                        value = part.split("=", 1)[1].strip().strip('"')
                        if value:
                            paths.append(value)
                if any(_under(part, program_norm) or _under(part, data_norm)
                       for part in paths
                       if (os.path.sep in part or ":" in part)):
                    continue
                # 必须由**解释器所在目录**证明它是另一份安装包里的代理：
                # 只有 image 位于 ...\manju-publish-assistant-<ver>\ 之下、
                # 且不在本程序目录里，才认定为旧实例；参数里出现同名字样不算。
                image_dirs = [seg for seg in image.replace("/", "\\").split("\\") if seg]
                if not any(seg.lower().startswith("manju-publish-assistant")
                           for seg in image_dirs):
                    continue
                pid = int(item.get("pid") or 0)
                if pid <= 0 or pid == os.getpid():
                    continue
                self.log("stopping orphan agent pid=%s image=%s" % (pid, image))
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=20)
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
        return stopped, remaining

    def _agent_ready_current(self):
        """Prove the listener is OUR image, data directory and version."""
        pid = _pid_on_port(9223)
        if not pid:
            return False, "no listener on 9223; " + supervisor_diagnostics(self.data)
        image = _image_path(pid) or ""
        owned, reason = _is_winport_owner(pid, self.data, self.venv_py)
        if not owned:
            return False, "not this winport: %s" % reason
        if not image or not _inside_roots(image, [self.program, self.data]):
            return False, "image outside product/data: %s" % image
        payload = _healthz_payload()
        if not payload:
            return False, "healthz unreadable"
        version = str(payload.get("version") or "")
        if not self.version or version != self.version:
            return False, ("agent version %r != package %r"
                           % (version, self.version))
        try:
            marker = json.loads((self.data / "run" / "agent-ready.json").read_text(
                encoding="utf-8-sig"))
            marker_pid = int(marker.get("agent_pid") or marker.get("pid") or 0)
            ready = marker.get("ready") is True
        except (OSError, ValueError, TypeError, AttributeError):
            return False, "readiness marker unreadable"
        if (not ready or marker_pid != pid or not marker.get("instance")
                or marker.get("instance") != payload.get("instance")):
            return False, ("readiness marker pid %r != listener %r"
                           % (marker_pid, pid))
        if not self._supervisor_ready_current():
            return False, ("current supervisor ownership not established; expected version=%s; "
                           "check %s and supervisor.lock.guard (unreadable owner, live old PID "
                           "or abandoned guard prevents takeover)"
                           % (self.version, self.data / "run" / "supervisor.lock" / "owner.txt"))
        return True, "pid=%s image=%s version=%s" % (pid, image, version)

    def run(self, argv, *, timeout=1800, cwd=None, env=None) -> int:
        self.log("run: " + " ".join(str(a) for a in argv))
        try:
            proc = subprocess.Popen([str(a) for a in argv], cwd=str(cwd or self.program),
                                    env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    **text_mode_options())
        except OSError as exc:
            self.log(f"  launch failed: {exc} (cwd={cwd or self.program})")
            return 127
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip()
            if line:
                self.log("  " + line)
        return proc.wait(timeout=timeout)

    def python_env(self) -> dict:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.program / "agent")
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(self.program / "browsers")
        env["PYTHONIOENCODING"] = process_encoding()
        return env

    # ---------- steps ----------
    def step_data_area(self) -> None:
        for rel in DATA_SUBDIRS:
            (self.data / rel).mkdir(parents=True, exist_ok=True)
        self.log(f"data area ready: {self.data}")
        try:
            from winport import desktop as _desktop
            folder = _desktop.ensure(self.data, self.log)
            if folder:
                self.log("desktop materials folder ready: %s" % folder)
        except Exception as exc:  # never block install on a shortcut folder
            self.log("desktop materials folder skipped: %s" % exc)

    def venv_base_runtime(self):
        """Return the base runtime recorded by ``.venv/pyvenv.cfg`` (or None)."""
        cfg = self.data / ".venv" / "pyvenv.cfg"
        try:
            text = cfg.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        for line in text.splitlines():
            if line.strip().lower().startswith("home"):
                value = line.split("=", 1)[1].strip() if "=" in line else ""
                return Path(value) if value else None
        return None

    def venv_ok(self) -> bool:
        if not self.venv_py.exists():
            return False
        # A venv created by an older install keeps pointing at that install's
        # runtime; once that folder is gone (or moved) every later start dies.
        # Rebuild instead of silently depending on a foreign program area.
        base = self.venv_base_runtime()
        if base is not None:
            want = self.program / "runtime"
            try:
                same = os.path.normcase(str(base.resolve())) == \
                    os.path.normcase(str(want.resolve()))
            except OSError:
                same = False
            if not same:
                self.log("venv base runtime mismatch: %s (want %s)" % (base, want))
                return False
        try:
            out = subprocess.run([str(self.venv_py), "-c", "print('ok')"],
                                 capture_output=True, timeout=60,
                                 **text_mode_options())
        except (OSError, subprocess.TimeoutExpired):
            return False
        return out.returncode == 0 and "ok" in (out.stdout or "")

    def pip_ok(self) -> bool:
        """A venv whose python runs can still lack pip (created --without-pip
        and later reused); step_wheels would then die with 'No module named pip'."""
        try:
            out = subprocess.run([str(self.venv_py), "-c", "import pip; print(pip.__version__)"],
                                 capture_output=True, timeout=60,
                                 **text_mode_options())
        except (OSError, subprocess.TimeoutExpired):
            return False
        return out.returncode == 0

    def step_venv(self) -> None:
        if self.venv_ok():
            if self.pip_ok():
                self.log("venv already present")
                return
            self.log("venv present but pip missing - repairing with ensurepip")
            code = self.run([self.venv_py, "-u", "-m", "ensurepip", "--upgrade"])
            if code != 0 or not self.pip_ok():
                raise RuntimeError(f"venv pip repair failed (exit {code})")
            return
        venv_dir = self.data / ".venv"
        if venv_dir.exists():
            self.log("venv unusable (points at a removed runtime) - rebuilding")
            shutil.rmtree(venv_dir, ignore_errors=True)
        self.log("[1/6] preparing the runtime, please wait")
        code = self.run([self.runtime_py, "-u", "-m", "venv", "--without-pip", venv_dir])
        if code == 0:
            code = self.run([self.venv_py, "-u", "-m", "ensurepip", "--upgrade"])
        if code != 0 or not self.venv_py.exists():
            raise RuntimeError(f"venv/ensurepip failed (exit {code})")

    def _pip_install(self, req: Path) -> int:
        return self.run([self.venv_py, "-u", "-m", "pip", "install", "--no-index",
                         "--find-links", self.program / "wheels", "-r", req])

    def step_wheels(self) -> None:
        req = self.program / "requirements.txt"
        if not req.exists():
            raise RuntimeError("requirements.txt missing under %s - package incomplete"
                               % self.program)
        self.log("[2/6] installing offline dependencies")
        code = self._pip_install(req)
        if code != 0:
            # 1.2.178: a locked .pyd is the common upgrade failure.  Retry once
            # after killing whatever still holds the venv, instead of aborting
            # and leaving the customer with a panel that cannot start.
            self.log("dependency install failed (exit %s) - stopping leftover "
                     "agent processes and retrying once" % code)
            try:
                self._win_kill_data_processes()
            except Exception as exc:
                self.log("WARNING: process cleanup failed: %s: %s"
                         % (type(exc).__name__, exc))
            code = self._pip_install(req)
        if code != 0:
            raise RuntimeError(f"offline dependency install failed (exit {code})")

    def step_ffmpeg(self) -> None:
        src = self.program / "bin" / "ffmpeg.exe"
        dst = self.data / "bin" / "ffmpeg.exe"
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
            self.log(f"ffmpeg placed at {dst}")

    def step_configs(self) -> None:
        for tmpl, real in CONFIG_SEEDS:
            src = self.program / "config" / tmpl
            dst = self.data / "config" / real
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
                self.log(f"seeded config: {real}")

    def find_chrome(self):
        for raw in CHROME_CANDIDATES:
            path = Path(os.path.expandvars(raw))
            if path.exists():
                return path
        return None

    def step_runtime_dlls(self) -> None:
        """Restore VC runtime DLLs that ship renamed as ``*.dll.__rt``.

        360 Total Security blocks writing vcruntime140.dll / msvcp140*.dll into
        the install folder, which aborts the customer's own unzip half-way.
        The package therefore stores them under a harmless suffix and we copy
        them back here; when even that copy is blocked we fall back to the
        identical system copy.  A failure is logged, never fatal.
        """
        runtime = self.program
        if not runtime.is_dir():
            return
        restored = 0
        failures = []
        wanted = {"vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll",
                  "msvcp140_1.dll", "msvcp140_2.dll", "concrt140.dll"}
        for src in runtime.rglob("*.dll.__rt"):
            if src.name[: -len(".__rt")].lower() not in wanted:
                continue
            target = Path(str(src)[: -len(".__rt")])
            if target.exists() and target.stat().st_size == src.stat().st_size:
                continue
            try:
                shutil.copy2(str(src), str(target))
                restored += 1
                continue
            except OSError as exc:
                reason = exc
            system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / target.name
            try:
                if system32.exists():
                    shutil.copy2(str(system32), str(target))
                    restored += 1
                    continue
            except OSError as exc:
                reason = exc
            failures.append("%s (%s)" % (target.name, reason))
            self.log("  WARNING: runtime dll %s could not be restored: %s"
                     % (target.name, reason))
        self.log("  runtime dlls restored=%s failed=%s" % (restored, len(failures)))

    def step_chrome(self) -> None:
        found = self.find_chrome()
        if found:
            self.log(f"chrome found: {found}")
            return
        installer = self.program / "chrome-offline" / "ChromeStandaloneSetup64.exe"
        if not installer.exists():
            self.log("chrome missing and no bundled installer")
            return
        self.log("[3/6] installing Chrome silently")
        self.run([installer, "/silent", "/install"], timeout=600)
        for _ in range(30):
            if self.find_chrome():
                self.log("chrome installed")
                return
            # wait-ok: poll
            time.sleep(2)
        self.log("chrome still not detected after install")

    def launcher_path(self, folder: Path | None = None) -> Path:
        return (folder or desktop_dir()) / ("%s.vbs" % _LAUNCHER_NAME)

    def step_cleanup_legacy_tasks(self) -> None:
        if os.name != "nt":
            return
        import csv
        query = subprocess.run(["schtasks", "/Query", "/FO", "CSV", "/NH"],
                               capture_output=True, timeout=30, **text_mode_options())
        if query.returncode != 0:
            raise RuntimeError("cannot enumerate legacy scheduled tasks")
        names = {row[0].lstrip("\\").casefold()
                 for row in csv.reader((query.stdout or "").splitlines()) if row}
        for name in _LEGACY_TASKS:
            if name.casefold() not in names:
                continue
            deleted = subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"],
                                     capture_output=True, timeout=20)
            if deleted.returncode != 0:
                raise RuntimeError("cannot remove legacy scheduled task %s" % name)
            self.log("legacy scheduled task removed: %s" % name)

    def step_launcher(self) -> None:
        self.step_cleanup_legacy_tasks()
        # Old installs pointed the Desktop icon at panel.ps1, which 360 blocks.
        folder = desktop_dir()
        folder.mkdir(parents=True, exist_ok=True)
        target = self.launcher_path(folder)
        # The launcher stays pure ASCII and uses WScript.Shell's window style 0
        # so a desktop click never flashes a console.  It asks Windows for
        # %LOCALAPPDATA% at run time and lets Python read the program directory
        # from a UTF-8 data file instead of embedding that path in the VBS.
        program_file = self.data / "run" / "program-path.txt"
        _write_verified_bytes(program_file, str(self.program).encode("utf-8"),
                              "program path")
        body = (
            "Option Explicit\r\n"
            "Dim shell, fso, dataDir, pythonExe, command, rc, q\r\n"
            'Set shell = CreateObject("WScript.Shell")\r\n'
            'Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
            'dataDir = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & '
            '"\\HuolongAgent\\publish-worker"\r\n'
            'pythonExe = dataDir & "\\.venv\\Scripts\\python.exe"\r\n'
            "If Not fso.FileExists(pythonExe) Then\r\n"
            '  MsgBox "The assistant runtime is missing or incomplete. " & '
            '"Reinstall required.", 16, "Manju Publish Assistant"\r\n'
            "  WScript.Quit 5\r\n"
            "End If\r\n"
            "q = Chr(34)\r\n"
            'shell.Environment("PROCESS")("PYTHONUTF8") = "0"\r\n'
            'shell.Environment("PROCESS")("PYTHONIOENCODING") = "utf-8"\r\n'
            'shell.Environment("PROCESS")("PYTHONDONTWRITEBYTECODE") = "1"\r\n'
            "command = q & pythonExe & q & \" -B -c \" & q & "
            "\"import os,pathlib,runpy,sys; "
            "d=pathlib.Path(os.environ['LOCALAPPDATA'])/'HuolongAgent'/'publish-worker'; "
            "p=pathlib.Path((d/'run'/'program-path.txt').read_text(encoding='utf-8-sig').strip()); "
            "sys.path.insert(0,str(p)); sys.path.insert(0,str(p/'agent')); "
            "os.environ['PYTHONPATH']=str(p/'agent'); "
            "os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(p/'browsers'); "
            "sys.argv=[str(p/'run_panel.py')]; "
            "runpy.run_path(str(p/'run_panel.py'),run_name='__main__')\" & q\r\n"
            "rc = shell.Run(command, 0, True)\r\n"
            "WScript.Quit rc\r\n"
        )
        try:
            payload = body.encode("ascii")
        except UnicodeEncodeError as exc:
            raise RuntimeError("desktop launcher is not ASCII: %s" % exc)
        _install_start_paths(
            supervisor=self.program / "scripts" / "run-agent-task.vbs",
            target=target, payload=payload, autostart=self.step_autostart,
            log=self.log)
        legacy_cmd = folder / ("%s.cmd" % _LAUNCHER_NAME)
        if legacy_cmd.exists():
            try:
                legacy_cmd.unlink()
                self.log("removed legacy launcher: %s" % legacy_cmd.name)
            except OSError as exc:
                self.log("legacy launcher cleanup failed: %s: %s"
                         % (legacy_cmd.name, exc))
        for stale in folder.glob("%s*.lnk" % _LAUNCHER_NAME):
            try:
                stale.unlink()
                self.log("removed stale shortcut: %s" % stale.name)
            except OSError:
                pass

    def step_autostart(self, launcher: Path) -> None:
        """Start with Windows.  HKCU Run needs no admin and no PowerShell."""
        try:
            import winreg
        except ImportError:
            if os.name == "nt":
                raise RuntimeError("winreg unavailable")
            return
        try:
            key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Run",
                                 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE)
            _update_run_value(winreg, key, "ManjuPublishAssistant",
                              'wscript.exe //nologo "%s"' % launcher,
                              _LEGACY_RUN_VALUES, winreg.REG_SZ, self.log)
            winreg.CloseKey(key)
            self.log("autostart registered: %s" % launcher)
        except OSError as exc:
            raise RuntimeError("autostart registration failed: %s" % exc) from exc

    def step_stop_running(self) -> None:
        """Stop the process on 9223 only after proving it is a winport agent."""
        # Stop the old supervisor FIRST: killing the agent while it still runs
        # only makes it relaunch the old program from the old directory.
        stopped_supervisors, remaining = self._stop_legacy_supervisors()
        if stopped_supervisors:
            self.log("stopped %d old supervisor(s)" % stopped_supervisors)
        if remaining:
            detail = ", ".join("pid=%s %s" % item for item in remaining)
            self.log("WARNING: old supervisor(s) still running: %s" % detail)
            raise RuntimeError(
                "old supervisor still running; refusing to let it relaunch "
                "the old agent: %s" % detail)
        pid = _pid_on_port(9223)
        if not pid:
            return
        image = _image_path(pid) or ""
        # An unreadable image is not evidence against us: ownership below still
        # has to be proven by command line or by path + /healthz.
        is_python = (not image) or os.path.basename(image).lower() in _PY_IMAGES
        owned, reason = _is_winport_owner(pid, self.data, self.venv_py)
        # Every version of this product lives under the vendor folder
        # (%LOCALAPPDATA%\HuolongAgent\app-*), so an image outside it is never
        # ours -- no matter what its command line claims.
        roots = [self.program, self.data]
        # Only an explicit vendor folder may contain sibling versions; a plain
        # parent (Desktop, Downloads) must never widen the allow-list.
        for anc in (self.program.parent, self.program.parent.parent):
            if anc.name.lower() == "huolongagent":
                roots.append(anc)
        inside = _inside_roots(image, roots) if image else False
        if is_python and not owned:
            if inside and _healthz_is_winport():
                owned, reason = True, "winport answered on 9223"
        if not is_python:
            raise RuntimeError("port 9223 pid %s refused: image %s is not python; %s"
                               % (pid, image or "?", reason))
        if image and not inside:
            # A previous version of this same product may live anywhere the
            # customer chose (D:\..., USB, an old folder).  The command line is
            # what proves ownership there; the port is only consulted to detect
            # a genuine hijack.  A hung legacy agent often answers nothing at
            # all (that is why the customer is upgrading), and requiring a live
            # /healthz would block exactly the upgrade it exists to enable.
            answered, winport = _healthz_probe()
            if _cmdline_proves_product(pid) and (winport or not answered):
                self.log("port 9223 pid %s is a legacy winport install outside "
                         "product folders (%s); stopping it" % (pid, image))
                owned, reason = True, "legacy winport proven by command line"
            else:
                raise RuntimeError("port 9223 pid %s refused: image %s outside product "
                                   "folders (healthz answered=%s winport=%s); %s"
                                   % (pid, image, answered, winport, reason))
        if not owned:
            raise RuntimeError("port 9223 pid %s refused: %s (image=%s)"
                               % (pid, reason, image or "unavailable"))
        self.log("stopping running agent pid=%s (%s)" % (pid, image))
        if image and (_image_path(pid) or "") != image:
            raise RuntimeError("pid %s changed image before kill" % pid)
        try:
            res = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                 capture_output=True, timeout=20)
            self.log("  taskkill rc=%s" % res.returncode)
        except (OSError, subprocess.SubprocessError) as exc:
            self.log("  taskkill failed: %s" % exc)
            raise RuntimeError("taskkill failed for pid %s: %s" % (pid, exc))
        released, owner, alive = _wait_for_release(pid)
        if not released:
            raise RuntimeError(
                "taskkill failed: pid %s still alive=%s or port 9223 still "
                "owned by %s after %.1fs"
                % (pid, alive, owner, RELEASE_TIMEOUT_SECONDS))
        self.log("  stopped pid=%s; port 9223 released" % pid)

    def step_cleanup_old_versions(self) -> None:
        """Remove valid older product directories after stopping the agent.

        Discovery is deliberately conservative: directory names merely select
        candidates.  A candidate is removable only when it contains a bootstrap
        entry and a MANIFEST.json with a version.  The current program tree,
        the data tree, their ancestors, and everything inside the real Desktop
        / Downloads / user-profile roots are never candidates for deletion.
        """
        # Never substitute ~/Desktop or ~/Downloads when the real Known Folder
        # cannot be resolved: a moved Downloads root would then escape the
        # protection set and a package extracted there could be deleted.
        real_desktop = _known_folder(_FOLDERID_DESKTOP)
        real_downloads = _known_folder(_FOLDERID_DOWNLOADS)
        if os.name == "nt" and (real_desktop is None or real_downloads is None):
            missing = []
            if real_desktop is None:
                missing.append("Desktop")
            if real_downloads is None:
                missing.append("Downloads")
            self.log("old-version cleanup SKIPPED: cannot resolve real Known "
                     "Folder(s) %s; refusing to delete with default paths"
                     % ", ".join(missing))
            return
        try:
            candidates = _discover_old_version_candidates(
                self.program, self.data, run_values=_read_run_values(),
                log=self.log)
        except Exception as exc:  # cleanup must never break the install
            self.log("old-version cleanup skipped (discovery failed): %s" % exc)
            return
        if not candidates:
            self.log("old-version cleanup: no other product directories found")
            return

        protected = [real_desktop or desktop_dir(),
                     real_downloads or downloads_dir(), Path.home()]
        try:
            protected.append(Path(os.environ.get("USERPROFILE") or Path.home()))
        except (OSError, ValueError):
            pass
        local_appdata = (os.environ.get("LOCALAPPDATA")
                         or str(Path.home() / "AppData" / "Local"))
        protected.append(Path(local_appdata))
        self.log("old-version cleanup: %d candidate(s)" % len(candidates))
        self.log("old-version cleanup: protected roots=%s"
                 % ", ".join(str(item) for item in protected))
        for path, sources in sorted(candidates.items(), key=lambda item: str(item[0])):
            labels = ",".join(sorted(sources))
            self.log("  discovered %s [%s]" % (path, labels))
            safe, why = _safe_old_version_dir(
                path, self.program, self.data, protected_roots=protected)
            if not safe:
                self.log("  skip %s: %s" % (path, why))
                continue
            if not (_looks_like_product_dir(path) or _is_vendor_app_dir(path)):
                self.log("  skip %s: not a product directory name" % path)
                continue
            valid, reason, version = _valid_product_dir(path)
            if not valid:
                self.log("  skip %s: product validation failed (%s)" % (path, reason))
                continue
            self.log("  valid old product version=%s" % version)
            if version == self.version:
                self.log("  skip %s: same version as this install" % path)
                continue
            have, want = _version_tuple(version), _version_tuple(self.version)
            if have is None or want is None:
                self.log("  skip %s: version %r not comparable with %r"
                         % (path, version, self.version))
                continue
            if have >= want:
                self.log("  skip %s: newer than this install" % path)
                continue
            size = _tree_size(path)
            try:
                stopped = _stop_processes_in_dir(path, self.log)
            except RuntimeError as exc:
                self.log("  WARNING: cleanup uncertain; leaving %s untouched: %s" % (path, exc))
                continue
            self.log("  processes stopped=%d" % stopped)
            removed, last_error = _remove_tree_with_retry(path, self.log)
            if removed:
                self.log("  deleted %s; released %s" % (path, _format_size(size)))
                continue
            stamp = time.strftime("%Y%m%d-%H%M%S")
            tombstone = path.with_name(path.name + ".delete-" + stamp)
            suffix = 1
            while tombstone.exists():
                tombstone = path.with_name(
                    "%s.delete-%s-%d" % (path.name, stamp, suffix))
                suffix += 1
            try:
                path.rename(tombstone)
                self.log("  WARNING: delete failed (%s); renamed to %s"
                         % (last_error, tombstone))
            except OSError as exc:
                self.log("  WARNING: delete failed (%s); rename also failed: %s"
                         % (last_error, exc))

    def step_start_agent(self) -> None:
        """Update both persistent entries and prove the complete handoff."""
        if os.name != "nt":
            self.log("Windows supervisor start unavailable on this platform")
            return
        launcher = self.program / "scripts" / "run-agent-task.vbs"
        if not launcher.is_file():
            raise RuntimeError("supervisor missing")
        self.step_autostart(launcher)
        # Also replace an older elevated task on a non-elevated upgrade.
        # Permission failures must stop installation, not preserve the old entry.
        if not self._start_supervisor_as_user(launcher):
            # Some customer machines deny schtasks to a non-elevated user.  That
            # is only fatal when it leaves an OLD task behind: such a task would
            # relaunch the previous program at logon.  With no task at all the
            # HKCU Run entry above is the complete logon path, and the agent must
            # still come online NOW or the handover check fails the install.
            if self._supervisor_task_exists():
                raise RuntimeError("could not replace/start ONLOGON supervisor task; check task permissions")
            self.log("WARNING: logon task unavailable (no admin); starting supervisor directly")
            if not self._launch_supervisor_direct(launcher):
                raise RuntimeError("could not start supervisor directly")
        deadline = time.monotonic() + 75
        reason = "not checked"
        while time.monotonic() < deadline:
            ready, reason = self._agent_ready_current()
            if ready:
                self.log("agent and supervisor verified: %s" % reason)
                return
            # wait-ok: poll
            time.sleep(0.5)
        raise RuntimeError("agent handoff failed: %s\n%s" %
                           (reason, supervisor_diagnostics(self.data)))

    def _supervisor_task_exists(self) -> bool:
        """True when a supervisor logon task is present (any owner)."""
        try:
            out = subprocess.run(["schtasks", "/Query", "/TN", "ManjuPublishAssistantSupervisor"],
                                 capture_output=True, timeout=30, **text_mode_options())
        except (OSError, subprocess.SubprocessError):
            return True  # cannot prove it is absent -> treat as present, stay strict
        return out.returncode == 0

    def _launch_supervisor_direct(self, launcher: Path) -> bool:
        """Start the supervisor without Task Scheduler.

        Non-administrator accounts get "access denied" from schtasks.  HKCU Run
        covers the next logon, but the current install has to come online now,
        otherwise the handover check times out and the installer exits 5 --
        exactly what happened on the first non-admin customer machine.
        """
        if os.name != "nt":
            return False
        try:
            DETACHED_PROCESS = 0x00000008
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(["wscript.exe", "//nologo", str(launcher)],
                             close_fds=True,
                             creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW)
            self.log("supervisor started directly (schtasks unavailable)")
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            self.log("direct supervisor start failed: %s" % exc)
            return False

    def _start_supervisor_as_user(self, launcher: Path) -> bool:
        """Create/run a logon task with LUA so an elevated install self-recovers."""
        if os.name != "nt":
            return False
        # A top-level name avoids depending on Task Scheduler auto-creating a
        # nested folder on a locked-down customer machine.
        task_name = "ManjuPublishAssistantSupervisor"
        command = 'wscript.exe //nologo "%s"' % launcher
        user = (os.environ.get("USERDOMAIN") and "%s\\%s" % (
            os.environ.get("USERDOMAIN"), os.environ.get("USERNAME"))) \
            or os.environ.get("USERNAME", "")
        create = ["schtasks", "/Create", "/TN", task_name, "/TR", command,
                  "/SC", "ONLOGON", "/RL", "LIMITED", "/IT", "/F"]
        if user:
            create[create.index("/RL"):create.index("/RL")] = ["/RU", user]
        try:
            import csv
            query = subprocess.run(["schtasks", "/Query", "/FO", "CSV", "/NH"],
                                   capture_output=True, timeout=30, **text_mode_options())
            if query.returncode != 0:
                raise RuntimeError("cannot enumerate scheduled tasks")
            rows = list(csv.reader((query.stdout or "").splitlines()))
            if any(row and row[0].lstrip("\\").casefold() == task_name.casefold()
                   for row in rows):
                deleted = subprocess.run(["schtasks", "/Delete", "/TN", task_name, "/F"],
                                         capture_output=True, timeout=30)
                if deleted.returncode != 0:
                    raise RuntimeError("cannot delete old supervisor task (permission denied or task busy)")
            made = subprocess.run(create, capture_output=True, timeout=30,
                                  **text_mode_options())
            self.log("supervisor logon task create rc=%s" % made.returncode)
            if made.returncode != 0:
                detail = (made.stderr or made.stdout or "").strip()
                if detail:
                    self.log("  schtasks create: %s" % detail[:300])
                return self._launch_supervisor_direct(launcher)
            started = subprocess.run(
                ["schtasks", "/Run", "/TN", task_name],
                capture_output=True, timeout=30, **text_mode_options())
            self.log("supervisor logon task run rc=%s" % started.returncode)
            if started.returncode != 0:
                detail = (started.stderr or started.stdout or "").strip()
                if detail:
                    self.log("  schtasks run: %s" % detail[:300])
                return self._launch_supervisor_direct(launcher)
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            self.log("schtasks supervisor start failed: %s" % exc)
            return self._launch_supervisor_direct(launcher)

    def step_selfcheck_tool(self) -> None:
        """Drop a double-click self-check for the customer.

        The batch is pure ASCII: the copy inside the program folder locates
        itself with %~dp0, and the Desktop copy uses the 8.3 short path, so a
        Chinese install path never leaks into the file.
        """
        template = [
            "@echo off",
            "chcp 65001 >nul",
            'set "P={program}"',
            'set "D=%LOCALAPPDATA%\\HuolongAgent\\publish-worker"',
            'set "PY=%D%\\.venv\\Scripts\\python.exe"',
            'set "PYTHONPATH=%P%\\agent"',
            'set "PYTHONIOENCODING=utf-8"',
            'set "PYTHONDONTWRITEBYTECODE=1"',
            'set "OUT=%~dp0selfcheck.txt"',
            'echo === Manju Publish Assistant self-check === > "%OUT%"',
            'type "%P%\\VERSION" >> "%OUT%" 2>&1',
            'date /t >> "%OUT%" 2>&1',
            'time /t >> "%OUT%" 2>&1',
            'cd /d "%P%"',
            '"%PY%" -B -m winport.selftest --data "%D%" --program "%P%" >> "%OUT%" 2>&1',
            'tasklist /fi "imagename eq pythonw.exe" >> "%OUT%" 2>&1',
            'netstat -ano | findstr LISTENING >> "%OUT%" 2>&1',
            'echo === done === >> "%OUT%"',
            'start "" notepad "%OUT%"',
            "",
        ]

        def _write(target_dir, program_expr, label):
            if not program_expr:
                self.log("WARNING: self-check tool skipped for %s (path not ASCII)"
                         % target_dir)
                return
            out = target_dir / "selfcheck.bat"
            body = "\r\n".join(template).format(program=program_expr)
            try:
                out.write_text(body, encoding="ascii", newline="")
                self.log("  self-check tool (%s): %s" % (label, out))
            except (OSError, UnicodeError) as exc:
                self.log("WARNING: self-check tool not written to %s: %s"
                         % (target_dir, exc))

        # inside the product folder: %~dp0 keeps the path out of the file
        _write(self.program, "%~dp0", "program")

        # on the Desktop: needs an ASCII spelling of the install path
        desk = desktop_dir()
        if desk:
            _write(desk, _ascii_path(self.program), "desktop")

    def step_selftest(self) -> None:
        self.log("[4/6] self-test")
        code = self.run([self.venv_py, "-u", "-m", "winport.selftest",
                         "--data", self.data, "--program", self.program],
                        env=self.python_env())
        if code != 0:
            raise RuntimeError(f"self-test failed (exit {code})")

    def step_activation(self) -> None:
        """Redeem the package enrolment token once; never block the install.

        A machine that is offline here is still installable: the panel repeats
        the same call on every launch and shows the reason if it fails.
        """
        self.log("[4/6] auto activation")
        try:
            from winport.activation import activate
            from winport.paths import Layout
            result = activate(Layout(program=self.program, data=self.data).ensure())
            self.log("activation: %s" % (result.get("reason") or result))
        except Exception as exc:  # network / service / disk: panel retries
            self.log("activation deferred: %s" % exc)

    def step_trial(self) -> None:
        """Create the 14-day trial ONCE, then verify it is really usable.

        A trial that was not created (or an existing trial that expired / was
        tombstoned) must not be reported as a successful install: the customer
        would open the panel and find publishing blocked with no explanation.
        The explicit recovery text is written through ``self.log`` so it lands
        in both the console and ``install-log.txt``.
        """
        try:
            from winport.license import initialize_trial
            from winport.paths import Layout
            result = initialize_trial(
                Layout(program=self.program, data=self.data).ensure(),
                allow_create=not self.data_existed)
            self.log("trial: %s" % (result.get("reason") or result))
        except Exception as exc:
            result = {"created": False,
                      "reason": "trial initialization raised %s: %s"
                                % (type(exc).__name__, exc)}
            self.log("trial: %s" % result["reason"])
        self.step_license_handover(log_success=False)

    def step_license_handover(self, *, log_success: bool = True) -> bool:
        """Require a valid formal licence OR a genuinely valid trial."""
        from winport.licstate import unified_license_state
        from winport.paths import Layout

        try:
            state = unified_license_state(
                Layout(program=self.program, data=self.data).ensure())
        except Exception as exc:
            state = {"status": "ERROR", "publish_allowed": False,
                     "reasons": ["授权状态无法计算：%s: %s"
                                 % (type(exc).__name__, exc)]}
        status = state.get("status")
        if status in ("OK", "GRACE"):
            self.license_handover_ok = True
            if log_success:
                self.log("licence handover ok: formal licence %s" % status)
            return True
        if status == "TRIAL" and state.get("publish_allowed"):
            self.license_handover_ok = True
            if log_success:
                self.log("licence handover ok: trial active, days_left=%s"
                         % state.get("days_left"))
            return True

        self.license_handover_ok = False
        if not self.license_problem_reported:
            detail = ("；".join(state.get("reasons") or [])
                      or state.get("text") or "授权校验未通过")
            message = "试用未创建，请联系服务商。原因：%s" % detail
            self.log(message)
            self.log(
                "人工恢复办法：打开面板顶部「查看机器码」取得机器码"
                "（也保存在 %LOCALAPPDATA%\\HuolongAgent\\publish-worker\\logs\\机器码.txt），"
                "把机器码和桌面 install-log.txt 发给服务商；服务商可人工恢复试用，"
                "或签发 8 位激活码（填入面板「激活码」框后点「用激活码激活」），"
                "或签发 license.json（用包内 import-license.bat 导入）。"
                "不要自行删除或修改授权文件，也不要反复重装。")
            self.problems.append(message)
            self.license_problem_reported = True
        return False

    def step_machine_code(self) -> None:
        self.log("[5/6] machine code")
        code = self.run([self.venv_py, "-u", "-m", "winport", "machine-code",
                  "--data", self.data, "--program", self.program],
                   env=self.python_env())
        if code != 0:
            self.log("WARNING: machine-code failed (exit %s)" % code)

    def step_handover_check(self) -> None:
        """Final gate: listener image, version, port and supervisor must agree."""
        if os.name != "nt":
            return
        ready, reason = self._agent_ready_current()
        if not ready:
            raise RuntimeError("handover check failed: %s" % reason)
        self.log("[6/6] handover check ok: %s" % reason)

    def install(self) -> int:
        self.step_data_area()
        self.step_runtime_dlls()
        self.step_quiesce()
        self.step_purge_old_leftovers()
        self.step_venv()
        self.step_wheels()
        self.step_ffmpeg()
        self.step_configs()
        self.step_chrome()
        self.step_activation()
        self.step_trial()
        self.step_selftest()
        self.step_stop_running()
        self.step_cleanup_old_versions()
        self.step_launcher()
        self.step_machine_code()
        self.step_start_agent()
        self.step_selfcheck_tool()
        handover_ok = self.step_license_handover()
        self.step_handover_check()
        if not handover_ok or self.problems:
            self.log("[6/6] installation incomplete: %d authorization problem(s); "
                     "see the recovery instructions above" % len(self.problems))
            return 5
        self.log("[6/6] installation complete")
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--program", default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args(argv)
    # bootstrap.bat passes %~dp0, which always ends with a backslash; a trailing
    # backslash before the closing quote is read by Windows as an escaped quote,
    # so the path arrives with a stray quote and CreateProcess fails with
    # WinError 267 (invalid directory name).  Normalise it here as well.
    raw = args.program.strip().strip('"').rstrip('\\/')
    inst = None
    try:
        inst = Installer(Path(raw).resolve())
        code = inst.install()
    except Exception as exc:  # noqa: BLE001 - the customer must see a plain reason
        # One failure exit for every install step, including constructor errors.
        if inst is None:
            inst = Installer.__new__(Installer)
            inst.data = Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")) / "HuolongAgent" / "publish-worker"
            inst.log_path = inst.data / "logs" / "install.log"
            inst.lines = []
        failure = "FATAL: %s: %s\n%s" % (type(exc).__name__, exc,
                                               supervisor_diagnostics(inst.data))
        inst.log(failure)
        inst.flush_desktop_copy()
        print("Installation incomplete. Please send install-log.txt on your Desktop to support.")
        return 5
    inst.flush_desktop_copy()
    if code != 0:
        print("Installation incomplete. Please send install-log.txt on your "
              "Desktop to support.")
    else:
        print("Done.")
    return code


if __name__ == "__main__":
    sys.exit(main())
