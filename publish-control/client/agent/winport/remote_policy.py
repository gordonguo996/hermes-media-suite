"""What an operator may do on this machine, and when (1.2.221).

Two operator channels reach a customer machine: the local relay
(``remote_channel``: exec / read / write / shot) and the control-plane poller
(``remote_agent``: exec / read / write / ls).  Until 1.2.220 both executed any
command and touched any file, all the time, and the Feishu bot depended on
that for everyday work.  Selling the product means that can no longer be the
default.

Default ("business mode", always on):
  * exec only of THIS product's own CLI with an allow-listed subcommand:
    ``<product python> -m agent.winport.cli <subcommand> ...`` (the bot's
    everyday work: inbox scan, plan make / set-time / reschedule, ledger
    unblock, dry-run, publish with its signed approval, collect, report...);
    the working directory is forced to the program directory.
  * read / ls of regular files inside the data area or the program directory.

Remote assist (customer turns it on in the panel; expires on its own,
default 4 h, max 24 h):
  * anything else: arbitrary argv / ``python -c`` / cmd / powershell, writes,
    files outside those two roots, desktop screenshots.

Every decision is appended to ``logs/remote-audit.jsonl`` (allowed or not), so
the customer can see what the vendor did on the machine.
"""

from __future__ import annotations

import json
import ntpath
import os
import sys
import threading
import time
from pathlib import Path

ASSIST_KEY = "remote_assist"
DEFAULT_ASSIST_HOURS = 4
MAX_ASSIST_HOURS = 24
AUDIT_NAME = "remote-audit.jsonl"
AUDIT_MAX_BYTES = 2 * 1024 * 1024

CLI_MODULES = frozenset({"agent.winport.cli", "winport.cli", "winport"})

#: Subcommands the vendor may run without remote assist.  Each is a product
#: function with its own validation; ``publish`` still needs the Ed25519
#: approval token.  Deliberately absent: ``bind`` (re-point the machine to
#: another control plane), ``publish-key``, ``license-import``, ``panel``,
#: ``autostart-plan``, ``evidence-prune`` -- those need the customer.
ALLOWED_CLI = frozenset({
    # 1.2.221 named operations (were bot-side "python -c" scripts)
    "inbox-scan", "plan-make", "plan-set-time", "plan-reschedule",
    "ledger-unblock", "export-files", "agent-health",
    # existing product commands the bot uses
    "dry-run", "publish", "publish-enqueue", "publish-hold-clear",
    "xingtu-tasks", "collect-all", "collect-offline", "report",
    "report-record", "report-export", "env-open", "env-probe",
    "environments", "channels", "state", "selftest", "instance-check",
    "integrity-check", "chrome-probe", "browser-probe", "chrome-regression",
    "login-check", "login-open", "evidence-list", "machine-code",
    "fingerprint", "license-check", "license-notices", "approval-status",
    "downlink-once",
})

_PYTHON_NAMES = frozenset({"python.exe", "pythonw.exe", "python", "python3",
                           "pythonw", "python3.exe"})
_AUDIT_LOCK = threading.Lock()


class RemoteAssistRequired(PermissionError):
    """Raised for an operation that needs the customer's remote-assist consent."""

    def __init__(self, what):
        super().__init__("remote assist is off on this machine: %s needs the "
                         "customer to turn on 远程协助 in the panel" % what)
        self.what = what


# --------------------------------------------------------------------- consent
def _settings_path(layout):
    return Path(layout.settings_file)


def _read_settings(layout):
    try:
        doc = json.loads(_settings_path(layout).read_text(encoding="utf-8-sig"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def assist_state(layout, now=None) -> dict:
    """``{"active": bool, "until": epoch|None, "remaining_s": int}``.

    Absent, malformed, disabled or expired -> inactive.  There is no "always
    on" value: an ``until`` beyond MAX_ASSIST_HOURS from now is clamped."""
    now = time.time() if now is None else float(now)
    raw = _read_settings(layout).get(ASSIST_KEY)
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return {"active": False, "until": None, "remaining_s": 0}
    try:
        until = float(raw.get("until"))
    except (TypeError, ValueError):
        return {"active": False, "until": None, "remaining_s": 0}
    until = min(until, now + MAX_ASSIST_HOURS * 3600)
    if until <= now:
        return {"active": False, "until": until, "remaining_s": 0}
    return {"active": True, "until": until, "remaining_s": int(until - now)}


def set_assist(layout, enabled, hours=DEFAULT_ASSIST_HOURS, now=None, actor="panel"):
    """Turn remote assist on for ``hours`` (1..24) or off.  Returns the state."""
    from .jsonio import write_json
    from .settings import load_settings

    now = time.time() if now is None else float(now)
    if type(enabled) is not bool:
        raise ValueError("enabled must be boolean")
    settings = load_settings(layout.settings_file)
    if enabled:
        try:
            hours = float(hours)
        except (TypeError, ValueError) as exc:
            raise ValueError("hours must be a number") from exc
        if not 0 < hours <= MAX_ASSIST_HOURS:
            raise ValueError("hours must be within 1..%d" % MAX_ASSIST_HOURS)
        settings[ASSIST_KEY] = {"enabled": True, "until": now + hours * 3600,
                                "since": now}
    else:
        settings[ASSIST_KEY] = {"enabled": False}
    write_json(layout.settings_file, settings)
    state = assist_state(layout, now=now)
    audit(layout, "consent", "assist-on" if enabled else "assist-off", True,
          {"actor": actor, "until": state.get("until")})
    return state


# ----------------------------------------------------------------------- audit
def audit(layout, channel, op, allowed, detail=None):
    """Append one decision; never raises (an audit failure must not stop work)."""
    try:
        logs = Path(getattr(layout, "logs", Path(layout.data) / "logs"))
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / AUDIT_NAME
        line = json.dumps({"ts": round(time.time(), 3), "channel": channel,
                           "op": op, "allowed": bool(allowed),
                           "detail": detail or {}},
                          ensure_ascii=False, default=str)
        with _AUDIT_LOCK:
            if path.exists() and path.stat().st_size > AUDIT_MAX_BYTES:
                os.replace(path, path.with_suffix(".jsonl.1"))
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------- checks
def _norm(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _inside(path, root) -> bool:
    try:
        Path(path).resolve(strict=False).relative_to(Path(root).resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _roots(layout):
    return [Path(layout.data), Path(layout.program)]


def _product_python(exe, layout) -> bool:
    name = ntpath.basename(str(exe)).lower() if os.name == "nt" else Path(str(exe)).name.lower()
    if name not in _PYTHON_NAMES and not name.startswith("python3."):
        return False
    known = {_norm(sys.executable)}
    base = getattr(sys, "_base_executable", None)
    if base:
        known.add(_norm(base))
    if _norm(exe) in known:
        return True
    return any(_inside(exe, root) for root in _roots(layout))


def cli_subcommand(argv, layout):
    """The allow-listed subcommand ``argv`` runs, or None."""
    if not isinstance(argv, list) or len(argv) < 4:
        return None
    if not all(isinstance(a, str) and "\x00" not in a for a in argv):
        return None
    exe, flag, module, sub = argv[0], argv[1], argv[2], argv[3]
    if flag != "-m" or module not in CLI_MODULES:
        return None
    if sub not in ALLOWED_CLI:
        return None
    if not _product_python(exe, layout):
        return None
    if not _path_flags_ok(argv[4:], layout):
        return None
    return sub


def _path_flags_ok(rest, layout) -> bool:
    """``--program`` would point the CLI at another code tree; ``--data`` may
    only name a folder inside this machine's data area."""
    it = iter(range(len(rest)))
    for i in it:
        arg = rest[i]
        if arg == "--program" or arg.startswith("--program="):
            return False
        if arg == "--data" or arg.startswith("--data="):
            value = arg.split("=", 1)[1] if "=" in arg else (
                rest[i + 1] if i + 1 < len(rest) else "")
            if not value or not _inside(value, layout.data):
                return False
    return True


def authorize_exec(layout, argv, *, channel, shell=False):
    """Return the cwd to use (program dir) or raise RemoteAssistRequired."""
    sub = None if shell else cli_subcommand(argv, layout)
    if sub is not None:
        audit(layout, channel, "exec", True, {"cli": sub})
        return str(layout.program)
    if assist_state(layout)["active"]:
        audit(layout, channel, "exec", True,
              {"assist": True, "argv0": (argv or [""])[0] if isinstance(argv, list) else "",
               "shell": bool(shell)})
        return None
    audit(layout, channel, "exec", False,
          {"argv0": (argv or [""])[0] if isinstance(argv, list) else str(argv)[:80],
           "shell": bool(shell)})
    raise RemoteAssistRequired("running arbitrary commands")


def authorize_path(layout, path, *, op, channel):
    """Reads/listings inside data or program dir are business; the rest needs assist."""
    if op in ("read", "ls") and path and any(_inside(path, root) for root in _roots(layout)):
        audit(layout, channel, op, True, {"path": str(path)})
        return
    if assist_state(layout)["active"]:
        audit(layout, channel, op, True, {"assist": True, "path": str(path)})
        return
    audit(layout, channel, op, False, {"path": str(path)})
    what = {"write": "writing files", "shot": "capturing the desktop"}.get(
        op, "%s outside the data and program folders" % op)
    raise RemoteAssistRequired(what)
