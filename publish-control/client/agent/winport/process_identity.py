"""Process identity shared by the readiness marker and remote session.

The Windows venv ``pythonw.exe`` is a redirector stub: the process the
updater spawns (the supervisor-visible pid) starts a second
``runtime\\pythonw.exe`` process, and that child is the process which serves
/healthz and writes ``run\\agent-ready.json``.  Version 1.2.191 supervisors
compare ``marker["pid"]`` with the pid they spawned, so the marker keeps that
legacy pid and adds ``agent_pid`` for the process that really owns the panel.

A valid ``WINPORT_INSTANCE_TOKEN`` means an updater supervisor launched this
process.  Only that launch mode rewrites ``pid`` to the launcher; a normal
desktop/VBS launch keeps the historical ``pid == os.getpid()`` semantics.
"""

from __future__ import annotations

import os

TOKEN_ENV = "WINPORT_INSTANCE_TOKEN"
LAUNCHER_PID_ENV = "WINPORT_LAUNCHER_PID"


def _positive_pid(value) -> int:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return 0
    return pid if pid > 0 else 0


def valid_instance_token(value) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 32:
        return ""
    if any(ch not in "0123456789abcdef" for ch in text):
        return ""
    return text


def update_supervised(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return bool(valid_instance_token(env.get(TOKEN_ENV)))


def marker_identity(*, environ=None, self_pid=None, parent_pid=None) -> dict:
    """Return the pid fields to write into agent-ready.json/session.json.

    ``pid`` is the process the updater supervisor spawned (legacy probe
    contract).  ``agent_pid`` is the process that really serves the panel.
    ``pid_chain`` makes the relationship explicit instead of letting the
    marker claim an unrelated pid.
    """
    env = os.environ if environ is None else environ
    pid = _positive_pid(self_pid if self_pid is not None else os.getpid())
    chain = [pid]
    if pid and update_supervised(env):
        launcher = _positive_pid(env.get(LAUNCHER_PID_ENV))
        if not launcher:
            try:
                launcher = _positive_pid(
                    parent_pid if parent_pid is not None else os.getppid())
            except OSError:
                launcher = 0
        if launcher and launcher != pid:
            chain = [launcher, pid]
    return {"pid": chain[0], "agent_pid": pid, "pid_chain": chain}


def _parent_pid(pid: int) -> int:
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class Entry(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD,
                                                           wintypes.DWORD]
            kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                                 ctypes.POINTER(Entry)]
            kernel32.Process32FirstW.restype = wintypes.BOOL
            kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                                ctypes.POINTER(Entry)]
            kernel32.Process32NextW.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
            if snapshot in (0, -1, ctypes.c_void_p(-1).value):
                return 0
            try:
                entry = Entry()
                entry.dwSize = ctypes.sizeof(Entry)
                more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
                while more:
                    if int(entry.th32ProcessID) == int(pid):
                        return int(entry.th32ParentProcessID)
                    more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            finally:
                kernel32.CloseHandle(snapshot)
        except Exception:
            return 0
        return 0
    try:
        text = open(f"/proc/{int(pid)}/stat", "r", encoding="utf-8").read()
        # The comm field can contain spaces/parentheses; ppid is field 4.
        fields = text.rsplit(")", 1)[1].split()
        return int(fields[1])
    except (OSError, ValueError, IndexError):
        return 0


def process_parents() -> dict[int, int]:
    """Snapshot every visible process and its parent pid.

    ``psutil`` is intentionally not used: the bundled runtime does not ship
    it.  A single snapshot is enough to walk a process tree before any child
    is terminated, which is what the updater needs to stop both the venv
    redirector and the ``runtime\\pythonw.exe`` child that owns port 9223.
    """
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class Entry(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD,
                                                           wintypes.DWORD]
            kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                                 ctypes.POINTER(Entry)]
            kernel32.Process32FirstW.restype = wintypes.BOOL
            kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                                ctypes.POINTER(Entry)]
            kernel32.Process32NextW.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
            if snapshot in (0, -1, ctypes.c_void_p(-1).value):
                return {}
            result = {}
            try:
                entry = Entry()
                entry.dwSize = ctypes.sizeof(Entry)
                more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
                while more:
                    pid = int(entry.th32ProcessID)
                    if pid > 0:
                        result[pid] = int(entry.th32ParentProcessID)
                    more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            finally:
                kernel32.CloseHandle(snapshot)
            return result
        except Exception:
            return {}
    result = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return result
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as stream:
                text = stream.read()
            fields = text.rsplit(")", 1)[1].split()
            result[pid] = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
    return result


def descendant_pids(roots, *, parents=None) -> list[int]:
    """Return ``roots`` plus every live descendant, parent before child.

    The parent map is a snapshot.  A child whose parent has already exited is
    still included on Windows because Toolhelp keeps the original parent pid;
    POSIX reparents orphans, so callers must snapshot before terminating the
    redirector.
    """
    if parents is None:
        parents = process_parents()
    roots = [pid for pid in (_positive_pid(item) for item in roots) if pid]
    children: dict[int, list[int]] = {}
    for pid, ppid in parents.items():
        pid = _positive_pid(pid)
        ppid = _positive_pid(ppid)
        if pid and ppid:
            children.setdefault(ppid, []).append(pid)
    seen = set()
    ordered = []
    stack = list(dict.fromkeys(roots))
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        ordered.append(pid)
        stack.extend(children.get(pid, ()))
    return ordered


def is_descendant(child, ancestor, *, parent_of=None, max_depth: int = 64) -> bool:
    """True when ``child`` is ``ancestor`` or a live descendant of it."""
    child = _positive_pid(child)
    ancestor = _positive_pid(ancestor)
    if not child or not ancestor:
        return False
    if child == ancestor:
        return True
    lookup = parent_of or _parent_pid
    seen = {child}
    for _ in range(max_depth):
        try:
            parent = _positive_pid(lookup(child))
        except Exception:
            return False
        if not parent:
            return False
        if parent == ancestor:
            return True
        if parent in seen:
            return False
        seen.add(parent)
        child = parent
    return False
