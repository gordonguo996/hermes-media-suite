"""User-session remote assistance. No services, elevation or system changes.

The bearer grants the operator the agent user's full command privileges; file
API containment is not a sandbox for exec. Never log tokens or request bodies.
"""
from __future__ import annotations

import base64
import codecs
import getpass
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .paths import install_root
from .process_identity import marker_identity
from .process_tree import ProcessTree
from . import remote_policy
from .settings import load_settings
from .subprocess_text import hidden_window_options
from .version import VERSION

LIMIT = 4 * 1024 * 1024
MAX_EXEC_TIMEOUT = 900

#: Every customer machine forwards a different loopback port on the relay
#: server.  The range is deliberately outside common service ports, and the
#: value is derived from the machine fingerprint so a restart or reinstall of
#: the app keeps the same port unless it is actually unavailable.
RELAY_PORT_MIN = 23000
RELAY_PORT_MAX = 24999
RELAY_PORT_COUNT = RELAY_PORT_MAX - RELAY_PORT_MIN + 1
LEGACY_RELAY_PORT = 2223

#: Local, operator-imported provisioning. None of these files is shipped in
#: the customer package: a per-customer key is copied into the app's private
#: data directory during deployment and is bound to a server-side account that
#: can only forward the assigned port.
RELAY_STATE_DIR = 'remote-channel'
RELAY_CONFIG_NAME = 'relay.json'
RELAY_KEY_NAME = 'client_key'
RELAY_KNOWN_HOSTS_NAME = 'known_hosts'
PROVISIONING_LIMIT = 64 * 1024


class RemoteSupportUnavailable(FileNotFoundError):
    """Local provisioning is absent or invalid; remote support stays off."""

    def __init__(self, code):
        super().__init__('remote support not enabled: ' + str(code))
        self.code = str(code)


def powershell():
    return str(Path(os.environ.get('SystemRoot', r'C:\Windows')) /
               'System32/WindowsPowerShell/v1.0/powershell.exe')


def hidden(argv, *, popen=False):
    return hidden_window_options(argv, popen=popen)


def _valid_port(port):
    return type(port) is int and 1 <= port <= 65535


def relay_port_path(layout):
    """The persisted relay port for this installation."""
    return Path(layout.data) / RELAY_STATE_DIR / 'relay-port'


def relay_state_dir(layout):
    return Path(layout.data) / RELAY_STATE_DIR


def relay_config_path(layout):
    return relay_state_dir(layout) / RELAY_CONFIG_NAME


def relay_key_path(layout):
    return relay_state_dir(layout) / RELAY_KEY_NAME


def relay_known_hosts_path(layout):
    return relay_state_dir(layout) / RELAY_KNOWN_HOSTS_NAME


def derive_relay_port(machine_id):
    """Map a stable machine id into RELAY_PORT_MIN..RELAY_PORT_MAX."""
    if not isinstance(machine_id, str) or not machine_id:
        raise ValueError('machine id required')
    digest = hashlib.sha256(('winport-relay-port\x00' + machine_id).encode('utf-8')).digest()
    return RELAY_PORT_MIN + int.from_bytes(digest[:4], 'big') % RELAY_PORT_COUNT


def port_available(port):
    """True when this machine can bind ``port`` on loopback."""
    if not _valid_port(port):
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == 'nt' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(('127.0.0.1', port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _probe(probe, port):
    try:
        return bool(probe(port))
    except (OSError, ValueError, TypeError):
        return False


def read_persisted_relay_port(layout):
    """Read data/remote-channel/relay-port; invalid contents mean ``None``."""
    try:
        value = relay_port_path(layout).read_text(encoding='ascii').strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not value.isdigit():
        return None
    port = int(value)
    return port if _valid_port(port) else None


def _persist_relay_port(layout, port):
    """Atomically persist the chosen port before SSH is launched."""
    path = relay_port_path(layout)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.relay-port-', dir=str(path.parent))
    temp_path = Path(temp)
    try:
        try:
            temp_path.chmod(0o600)
        except OSError:
            pass
        with open(fd, 'w', encoding='ascii') as stream:
            stream.write(str(port))
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def legacy_relay_port(layout):
    """Honour a fixed old relay port once when upgrading an existing install.

    Older packages hard-coded 2223 without writing it to settings.  If no
    relay-port file exists yet but remote-channel state (token/session.json)
    does, this is an upgrade: try the old port once, then fall back to the
    machine-derived port.  An explicit ``remote_channel.relay_port`` always
    wins over that inference.
    """
    value = {}
    try:
        value = load_settings(layout.settings_file).get('remote_channel', {})
    except Exception:
        value = {}
    if isinstance(value, dict):
        configured = value.get('relay_port')
        if _valid_port(configured):
            return configured
    if relay_port_path(layout).exists():
        return None
    state = Path(layout.data) / 'remote-channel'
    if (state / 'token').exists() or (state / 'session.json').exists():
        return LEGACY_RELAY_PORT
    return None


def select_relay_port(layout, machine_id, *, preferred=None, probe=None):
    """Return a stable, currently free relay port and persist the choice.

    A persisted port is reused across restarts.  If it is taken, ports are
    tried in ascending order (wrapping inside 23000..24999).  ``preferred`` is
    the one-shot legacy port; a failure there falls back to the hash port.
    """
    probe = probe or port_available
    persisted = read_persisted_relay_port(layout)
    tried = set()
    if persisted is not None:
        tried.add(persisted)
        if _probe(probe, persisted):
            return persisted
    if _valid_port(preferred) and preferred not in tried:
        tried.add(preferred)
        if _probe(probe, preferred):
            _persist_relay_port(layout, preferred)
            return preferred

    derived = derive_relay_port(machine_id)
    if persisted is not None and RELAY_PORT_MIN <= persisted <= RELAY_PORT_MAX:
        start = persisted + 1
    else:
        start = derived
    for offset in range(RELAY_PORT_COUNT):
        port = RELAY_PORT_MIN + ((start - RELAY_PORT_MIN + offset) % RELAY_PORT_COUNT)
        if port in tried:
            continue
        if _probe(probe, port):
            _persist_relay_port(layout, port)
            return port
    raise RuntimeError('no free relay port in 23000-24999')


def advance_relay_port(layout, current, machine_id, *, probe=None):
    """Move past a port whose remote bind failed, then persist the new one."""
    probe = probe or port_available
    if RELAY_PORT_MIN <= current <= RELAY_PORT_MAX:
        start = current + 1
    else:
        start = derive_relay_port(machine_id)
    for offset in range(RELAY_PORT_COUNT):
        port = RELAY_PORT_MIN + ((start - RELAY_PORT_MIN + offset) % RELAY_PORT_COUNT)
        if _probe(probe, port):
            _persist_relay_port(layout, port)
            return port
    raise RuntimeError('no free relay port in 23000-24999')


def display_relay_port(layout):
    """The port the panel should show before/while the channel is running."""
    persisted = read_persisted_relay_port(layout)
    if persisted is not None:
        return persisted
    preferred = legacy_relay_port(layout)
    if preferred is not None:
        return preferred
    try:
        from .fingerprint import fingerprint
        return derive_relay_port(fingerprint()['machine_id'])
    except Exception:
        return None


def private_directory(path):
    """Protect an application-owned directory BEFORE creating any secret."""
    path = Path(path)
    if path.is_symlink() or (path.exists() and getattr(path.stat(), 'st_file_attributes', 0) & 1024):
        raise ValueError('secret directory must not be a link')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != 'nt':
        path.chmod(0o700)
        return
    # 360 blocks powershell.exe on some customer machines, so the ACL is applied
    # with icacls (built into Windows) instead of a PowerShell script.
    user = os.environ.get("USERNAME") or getpass.getuser()
    for grant in ("%s:(OI)(CI)F" % user, "SYSTEM:(OI)(CI)F",
                  "Administrators:(OI)(CI)F"):
        try:
            command = ["icacls", str(path), "/inheritance:r", "/grant:r", grant]
            subprocess.run(command, check=False, capture_output=True, timeout=15,
                           **hidden(command))
        except (OSError, subprocess.SubprocessError):
            pass


def allowed_path(value, roots):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('absolute path required')
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('absolute path required')
    if os.name == 'nt':
        # Reject UNC/device namespaces, alternate data streams and DOS aliases.
        if value.startswith(('\\\\', '//')) or ':' in value[2:]:
            raise PermissionError('device, UNC or ADS path denied')
        reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {f'{p}{i}' for p in ('COM', 'LPT') for i in range(1, 10)}
        if any(part.rstrip(' .') != part or part.split('.')[0].upper() in reserved
               for part in path.parts[1:] if part not in ('.', '..')):
            raise PermissionError('ambiguous Windows path denied')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise PermissionError('symlink denied')
        if part.exists():
            st = part.stat()
            if getattr(st, 'st_file_attributes', 0) & 1024:
                raise PermissionError('reparse point denied')
            if part == path and stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                raise PermissionError('hard link denied')
    resolved = path.resolve()
    if not any(resolved.is_relative_to(Path(root).resolve()) for root in roots):
        raise PermissionError('path outside user and install directories')
    if resolved.exists() and not resolved.is_file():
        raise PermissionError('regular files only')
    return resolved


def terminate(proc):
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        command = [str(Path(os.environ.get('SystemRoot', r'C:\Windows')) /
                       'System32/taskkill.exe'),
                   '/PID', str(proc.pid), '/T', '/F']
        subprocess.run(command, capture_output=True, timeout=10,
                       **hidden(command))
    else:
        os.killpg(proc.pid, signal.SIGKILL)
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


def execute(body, cancel=None, lifecycle=None, processes=None, cwd=None):
    encoding = body.get('encoding', 'utf-8')
    if not isinstance(encoding, str):
        raise ValueError('encoding must be a codec name')
    try:
        codecs.lookup(encoding)
    except LookupError as exc:
        raise ValueError('unknown output encoding') from exc
    timeout = float(body.get('timeout', 30))
    if not math.isfinite(timeout) or not 0 < timeout <= MAX_EXEC_TIMEOUT:
        raise ValueError('timeout must be >0 and <=%d seconds' % MAX_EXEC_TIMEOUT)
    shell = body.get('shell')
    if shell is not None:
        command = body.get('command')
        if not isinstance(command, str) or not command:
            raise ValueError('explicit shell requires command text')
        if shell == 'cmd':
            argv = [str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/cmd.exe'), '/d', '/s', '/c', command]
        elif shell == 'powershell':
            argv = [powershell(), '-NoProfile', '-NonInteractive', '-Command', command]
        else:
            raise ValueError('shell must be cmd or powershell')
    else:
        argv = body.get('argv')
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and '\x00' not in a for a in argv):
            raise ValueError('argv must be a nonempty string list')
        if Path(argv[0]).suffix.lower() in ('.bat', '.cmd') or Path(argv[0]).name.lower() in ('cmd', 'cmd.exe', 'powershell', 'powershell.exe', 'pwsh', 'pwsh.exe'):
            raise ValueError('shell execution requires explicit shell field')
    lifecycle = lifecycle or threading.RLock()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        with lifecycle:
            if cancel is not None and cancel.is_set():
                return {'stdout': '', 'stderr': '', 'exit_code': None,
                        'timed_out': False, 'cancelled': True, 'truncated': False}
            tree = ProcessTree(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, shell=False,
                               **({'cwd': cwd} if cwd else {}))
            if processes is not None:
                processes.add(tree)
        proc = tree.proc
        deadline = time.monotonic() + timeout
        timed_out = False
        cancelled = False
        try:
            while proc.poll() is None:
                timed_out = time.monotonic() >= deadline
                cancelled = cancel is not None and cancel.is_set()
                if timed_out or cancelled:
                    break
                try:
                    proc.wait(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
        finally:
            tree.close()
            with lifecycle:
                if processes is not None:
                    processes.discard(tree)
        out.seek(0)
        err.seek(0)
        stdout, stderr = out.read(LIMIT + 1), err.read(LIMIT + 1)
    return {'stdout': stdout[:LIMIT].decode(encoding, 'replace'),
            'stderr': stderr[:LIMIT].decode(encoding, 'replace'),
            'exit_code': proc.returncode, 'timed_out': timed_out, 'cancelled': cancelled,
            'truncated': len(stdout) > LIMIT or len(stderr) > LIMIT}


class ControlService:
    def __init__(self, layout, logger):
        self.layout, self.logger = layout, logger
        self.token = self._stable_token(layout)
        self.roots = (Path.home(), layout.program)
        self.cancel = threading.Event()
        self.busy = threading.Lock()
        self.lifecycle = threading.RLock()
        self.processes = set()
        self.connections = set()
        self.server = None
        self.secret = layout.data / 'remote-channel' / 'session.json'


    @staticmethod
    def _stable_token(layout):
        """Reuse a persisted token so a restart does not lock out support."""
        path = layout.data / 'remote-channel' / 'token'
        try:
            token = path.read_text(encoding='utf-8').strip()
            if 20 <= len(token) <= 128:
                return token
        except Exception:
            pass
        try:
            legacy = json.loads((layout.data / 'remote-channel' / 'session.json').read_text(encoding='utf-8'))
            token = legacy.get('token')
            if isinstance(token, str) and 20 <= len(token) <= 128:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    stream.write(token)
                return token
        except Exception:
            pass
        token = secrets.token_urlsafe(32)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(token)
        except Exception:
            pass
        return token
    def dispatch(self, operation, body):
        with self.lifecycle:
            self.check_running()
        if operation == 'healthz':
            return {'ok': True, 'status': 'ok', 'version': VERSION,
                    'pid': os.getpid()}
        if operation == 'info':
            # The bot uses these fields to converge every /exec request onto
            # the runtime of the resident agent.  An old version directory that
            # is still on disk is never selected after an upgrade.
            return {'hostname': socket.gethostname(), 'username': getpass.getuser(),
                    'version': VERSION, 'pid': os.getpid(),
                    'program': str(install_root()),
                    'python': sys.executable,
                    # 1.2.221: the bot switches to named CLI operations when
                    # it sees this; arbitrary exec then needs remote assist.
                    'capabilities': ['named-ops-1'],
                    'remote_assist': remote_policy.assist_state(self.layout)}
        if operation == 'named':
            # 1.2.221: the bot's everyday work as product code (no -c scripts).
            from . import named_ops
            name = body.get('name')
            remote_policy.audit(self.layout, 'relay', 'named', name in named_ops.OPERATIONS,
                                {'name': str(name)[:40]})
            return named_ops.dispatch(name, self.layout, body.get('args') or {})
        if operation == 'exec':
            # The policy decides before anything starts: product CLI
            # subcommands always; anything else only under remote assist.
            cwd = remote_policy.authorize_exec(
                self.layout, body.get('argv'), channel='relay',
                shell=body.get('shell') is not None)
            return execute(body, self.cancel, self.lifecycle, self.processes, cwd=cwd)
        if operation in ('read', 'write'):
            path = allowed_path(body.get('path'), self.roots)
            remote_policy.authorize_path(self.layout, path, op=operation, channel='relay')
            if operation == 'read':
                with path.open('rb') as stream:
                    data = stream.read(LIMIT + 1)
                if len(data) > LIMIT:
                    raise ValueError('file exceeds 4 MiB limit')
                return {'base64': base64.b64encode(data).decode(), 'bytes': len(data)}
            data = base64.b64decode(body.get('base64', ''), validate=True)
            if len(data) > LIMIT:
                raise ValueError('file exceeds 4 MiB limit')
            # Recheck immediately before open; same-user concurrent filesystem
            # mutation is outside this API boundary (exec already has that UID).
            path = allowed_path(str(path), self.roots)
            # Staging is private and never exposes partial destination contents.
            # Stop and the atomic destination commit share one linearization lock.
            with self.lifecycle:
                self.check_running()
                fd, staging = tempfile.mkstemp(prefix='.remote-write-', dir=path.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data)
                with self.lifecycle:
                    self.check_running()
                    allowed_path(str(path), self.roots)
                    os.replace(staging, path)
            finally:
                Path(staging).unlink(missing_ok=True)
            return {'bytes': len(data)}
        if operation == 'shot':
            remote_policy.authorize_path(self.layout, None, op='shot', channel='relay')
            if os.name != 'nt':
                raise ValueError('screenshot requires a Windows interactive desktop')
            # Capture in-process: launching powershell.exe trips 360's
            # "PowerShell command execution" protection on customer machines.
            try:
                import io
                from PIL import ImageGrab
            except ImportError as exc:
                raise RuntimeError('screenshot requires Pillow') from exc
            image = ImageGrab.grab(all_screens=True)
            buffer = io.BytesIO()
            image.save(buffer, format='PNG')
            data = buffer.getvalue()
            if len(data) > LIMIT:
                raise RuntimeError('screenshot failed or exceeds response limit')
            return {'base64': base64.b64encode(data).decode(), 'mime': 'image/png'}
        raise ValueError('unknown operation')

    def check_running(self):
        if self.cancel.is_set():
            raise PermissionError('channel stopping')

    def start(self):
        owner = self

        class Server(ThreadingHTTPServer):
            daemon_threads = False
            block_on_close = True

            def process_request(self, request, address):
                with owner.lifecycle:
                    if owner.cancel.is_set():
                        self.shutdown_request(request)
                        return
                    owner.connections.add(request)
                    try:
                        super().process_request(request, address)
                    except BaseException:
                        owner.connections.discard(request)
                        raise

            def process_request_thread(self, request, address):
                try:
                    super().process_request_thread(request, address)
                finally:
                    with owner.lifecycle:
                        owner.connections.discard(request)


        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(10)

            def log_message(self, *_):
                pass

            def reply(self, code, data):
                if owner.cancel.is_set():
                    return
                raw = json.dumps(data).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request()

            def handle_request(self):
                op = self.path.lstrip('/')
                if not hmac.compare_digest(self.headers.get('Authorization', '').encode('utf-8'), ('Bearer ' + owner.token).encode('ascii')):
                    owner.logger.warning('authentication rejected')
                    self.reply(401, {'error': 'unauthorized'})
                    return
                if owner.cancel.is_set():
                    self.reply(503, {'error': 'channel stopping'})
                    return
                if self.command != 'POST' and op not in ('info', 'healthz'):
                    self.reply(405, {'error': 'POST required'})
                    return
                if not owner.busy.acquire(blocking=False):
                    self.reply(429, {'error': 'operation in progress'})
                    return
                try:
                    if self.headers.get('Transfer-Encoding'):
                        raise ValueError('chunked requests unsupported')
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 <= size <= 6 * 1024 * 1024:
                        raise ValueError('request too large')
                    body = json.loads(self.rfile.read(size)) if size else {}
                    if not isinstance(body, dict):
                        raise ValueError('JSON object required')
                    result = owner.dispatch(op, body)
                    owner.logger.info('operation %s completed', op if op in ('exec', 'read', 'write', 'shot', 'info', 'healthz', 'named') else 'unknown')
                    self.reply(200, result)
                except remote_policy.RemoteAssistRequired as exc:
                    owner.logger.warning('remote assist required for %s', op)
                    self.reply(403, {'error': str(exc), 'remote_assist_required': True})
                except PermissionError:
                    owner.logger.warning('file access denied')
                    self.reply(403, {'error': 'file access denied'})
                except (ValueError, TypeError) as exc:
                    owner.logger.warning('invalid request: %s', type(exc).__name__)
                    self.reply(400, {'error': str(exc)})
                except Exception as exc:
                    owner.logger.error('operation failed: %s', type(exc).__name__)
                    self.reply(500, {'error': 'operation failed; see remote-channel.log'})
                finally:
                    owner.busy.release()

        private_directory(self.secret.parent)
        # Remove old file, avoiding inherited permissions on an existing token.
        self.secret.unlink(missing_ok=True)
        self.server = Server(('127.0.0.1', 0), Handler)
        try:
            fd = os.open(self.secret, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                identity = marker_identity()
                json.dump({'token': self.token, 'port': self.server.server_port,
                           'pid': identity['pid'],
                           'agent_pid': identity['agent_pid'],
                           'pid_chain': identity['pid_chain']}, stream)
            threading.Thread(target=self.server.serve_forever, daemon=True,
                             name='remote-http').start()
        except Exception:
            self.server.server_close()
            self.server = None
            self.secret.unlink(missing_ok=True)
            raise
        self.logger.info('control service started on loopback port %s', self.server.server_port)
        return self.server.server_port

    def stop(self):
        with self.lifecycle:
            self.cancel.set()
            for connection in tuple(self.connections):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        with self.lifecycle:
            for tree in tuple(self.processes):
                tree.close()
                self.processes.remove(tree)
        self.secret.unlink(missing_ok=True)
        self.logger.info('control service stopped')


def ssh_config_path(path):
    # -o values are parsed again by OpenSSH, independently of argv quoting.
    value = Path(path).as_posix()
    if any(char in value for char in ('"', '\n', '\r', '\x00')):
        raise ValueError('unsupported SSH configuration path')
    return '"' + value.replace('\\', '\\\\') + '"'


def _read_local_provisioning_file(path, code):
    """Read an operator-imported file without following links or logging it."""
    path = Path(path)
    try:
        if path.is_symlink():
            raise RemoteSupportUnavailable(code + '_link')
        info = path.stat()
    except RemoteSupportUnavailable:
        raise
    except OSError as exc:
        raise RemoteSupportUnavailable(code + '_missing') from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > PROVISIONING_LIMIT:
        raise RemoteSupportUnavailable(code + '_invalid')
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RemoteSupportUnavailable(code + '_unreadable') from exc
    if b'\x00' in data:
        raise RemoteSupportUnavailable(code + '_invalid')
    return data


def _load_relay_config(layout):
    data = _read_local_provisioning_file(relay_config_path(layout), 'relay_config')
    try:
        value = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RemoteSupportUnavailable('relay_config_invalid') from exc
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise RemoteSupportUnavailable('relay_config_invalid')
    host = value.get('host')
    user = value.get('user')
    port = value.get('port', 22)
    if not isinstance(host, str) or not host or host.startswith('-') or not re.fullmatch(r'[A-Za-z0-9._:-]+', host):
        raise RemoteSupportUnavailable('relay_config_host_invalid')
    if not isinstance(user, str) or not re.fullmatch(r'[A-Za-z0-9._-]+', user):
        raise RemoteSupportUnavailable('relay_config_user_invalid')
    if type(port) is not int or not _valid_port(port):
        raise RemoteSupportUnavailable('relay_config_port_invalid')
    expected = value.get('key_sha256')
    if expected is not None and (not isinstance(expected, str) or
                                 not re.fullmatch(r'[0-9a-fA-F]{64}', expected)):
        raise RemoteSupportUnavailable('relay_config_key_hash_invalid')
    return {'host': host, 'user': user, 'port': port,
            'key_sha256': expected.lower() if expected else None}


def relay_provisioning(layout):
    """Return validated local paths; never returns or logs secret bytes."""
    key_path = relay_key_path(layout)
    known_path = relay_known_hosts_path(layout)
    key_bytes = _read_local_provisioning_file(key_path, 'client_key')
    known_bytes = _read_local_provisioning_file(known_path, 'known_hosts')
    config = _load_relay_config(layout)
    head = b'-----BEGIN ' + b'OPENSSH PRIVATE KEY-----'
    if not (key_bytes.startswith(head) or b'PRIVATE KEY-----' in key_bytes[:256]):
        raise RemoteSupportUnavailable('client_key_invalid')
    if not known_bytes.strip():
        raise RemoteSupportUnavailable('known_hosts_invalid')
    if config['key_sha256']:
        actual = hashlib.sha256(key_bytes).hexdigest()
        if not hmac.compare_digest(actual, config['key_sha256']):
            raise RemoteSupportUnavailable('client_key_hash_mismatch')
    return {'key': key_path, 'known_hosts': known_path, **config}


def relay_provisioning_status(layout):
    """A non-throwing provisioning verdict safe for the customer panel."""
    try:
        relay_provisioning(layout)
        return {'ok': True, 'code': 'provisioned'}
    except RemoteSupportUnavailable as exc:
        return {'ok': False, 'code': exc.code}
    except Exception:
        return {'ok': False, 'code': 'provisioning_error'}


def remote_support_view(layout):
    """Customer-facing status text. Never contains a host, port or local path."""
    try:
        settings = load_settings(layout.settings_file).get('remote_channel', {})
    except Exception:
        settings = {}
    enabled = not isinstance(settings, dict) or settings.get('enabled', True) is not False
    if not enabled:
        return {'enabled': False, 'provisioned': False,
                'message': '远程支持已关闭'}
    status = relay_provisioning_status(layout)
    if not status['ok']:
        return {'enabled': True, 'provisioned': False,
                'message': '远程支持未启用（本机尚未完成部署配置）'}
    token = Path(layout.data) / RELAY_STATE_DIR / 'token'
    try:
        ready = bool(token.read_text(encoding='utf-8').strip())
    except OSError:
        ready = False
    return {'enabled': True, 'provisioned': True,
            'message': '远程支持已启用' if ready else '远程支持正在准备'}


def relay_command(layout, local_port, relay_port=None):
    provisioning = relay_provisioning(layout)
    if not _valid_port(local_port):
        raise ValueError('local control port required')
    if relay_port is None:
        relay_port = display_relay_port(layout)
    if not _valid_port(relay_port):
        raise ValueError('relay port required')
    return [str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/OpenSSH/ssh.exe'),
            '-F', 'NUL', '-N', '-T', '-p', str(provisioning['port']),
            '-i', str(provisioning['key']),
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UserKnownHostsFile=' + ssh_config_path(provisioning['known_hosts']),
            '-o', 'GlobalKnownHostsFile=NUL', '-o', 'UpdateHostKeys=no',
            '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15',
            '-o', 'ServerAliveCountMax=3', '-o', 'ConnectTimeout=10',
            '-R', f'127.0.0.1:{relay_port}:127.0.0.1:{local_port}',
            f"{provisioning['user']}@{provisioning['host']}"]


def backoff(attempt):
    return min(60, 2 ** min(max(attempt, 0), 6))


class RemoteChannel:
    """One background supervisor; configuration is re-read each second."""
    def __init__(self, layout):
        self.layout = layout
        self.logger = logging.getLogger('winport.remote.' + str(layout.data))
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        if not self.logger.handlers:
            handler = RotatingFileHandler(layout.logs / 'remote-channel.log',
                                          maxBytes=2 * 1024 * 1024, backupCount=3, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
            self.logger.addHandler(handler)
        self.stopped = threading.Event()
        self.thread = None
        self.service = None
        self.proc = None
        self.machine_id = None
        self.relay_port = None
        self.port_is_legacy = False
        self.early_failures = 0
        self.status = 'starting'
        self._unprovisioned_notice = False
        self.lifecycle = threading.RLock()

    def _choose_relay_port(self):
        if self.relay_port is None:
            if self.machine_id is None:
                from .fingerprint import fingerprint
                self.machine_id = fingerprint()['machine_id']
            preferred = legacy_relay_port(self.layout)
            self.relay_port = select_relay_port(self.layout, self.machine_id,
                                                preferred=preferred)
            self.port_is_legacy = preferred is not None and self.relay_port == preferred
            self.logger.info('relay port selected: %s', self.relay_port)
        return self.relay_port

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name='remote-channel')
        self.thread.start()

    def enabled(self):
        value = load_settings(self.layout.settings_file).get('remote_channel', {})
        if not isinstance(value, dict) or type(value.get('enabled', True)) is not bool:
            raise ValueError('remote_channel.enabled must be boolean')
        return value.get('enabled', True)

    def _teardown(self, *, clear_secrets):
        failure = None
        with self.lifecycle:
            proc, self.proc = self.proc, None
        if proc is not None:
            try:
                terminate(proc)
            except Exception as exc:
                self.logger.exception('SSH cleanup failed: %s', exc)
                failure = exc
        with self.lifecycle:
            service = self.service
            self.service = None
            if service is not None:
                if clear_secrets:
                    service.token = ''
                try:
                    service.stop()
                except Exception as exc:
                    self.logger.exception('control service stop failed')
                    self.service = service
                    failure = exc
            if clear_secrets:
                for name in ('token', 'session.json'):
                    try:
                        (self.layout.data / 'remote-channel' / name).unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        self.logger.exception('could not remove %s', name)
                        failure = exc
        if failure is not None:
            raise failure

    def stop_now(self):
        """Disable assistance immediately: drop SSH, the HTTP API and secrets."""
        self._teardown(clear_secrets=True)

    def cleanup(self, *, clear_secrets=False):
        self._teardown(clear_secrets=clear_secrets)

    def run(self):
        attempt, retry_at, started = 0, 0, 0
        try:
            while not self.stopped.is_set():
                try:
                    if not self.enabled():
                        self.status = 'disabled'
                        self.cleanup(clear_secrets=True)
                        attempt, retry_at = 0, 0
                    elif os.name != 'nt':
                        self.status = 'unsupported'
                        self.logger.error('remote channel requires Windows; not starting SSH')
                        return
                    elif not relay_provisioning_status(self.layout)['ok']:
                        # No per-customer key in the package: keep all remote
                        # state off and let the rest of the app run normally.
                        self.status = 'unprovisioned'
                        self.cleanup(clear_secrets=True)
                        if not self._unprovisioned_notice:
                            self.logger.info('remote support not enabled: local provisioning missing')
                            self._unprovisioned_notice = True
                        self.stopped.wait(5)
                        continue
                    elif self.proc and self.proc.poll() is not None:
                        self.status = 'reconnecting'
                        self.logger.warning('SSH disconnected exit=%s', self.proc.returncode)
                        self.proc = None
                        early = time.monotonic() - started < 10
                        self.early_failures = self.early_failures + 1 if early else 0
                        if early and (self.port_is_legacy or self.early_failures >= 2):
                            old = self.relay_port
                            try:
                                self.relay_port = advance_relay_port(
                                    self.layout, old, self.machine_id)
                                self.port_is_legacy = False
                                self.early_failures = 0
                                self.logger.info('relay port %s unavailable; switched to %s',
                                                 old, self.relay_port)
                            except Exception as exc:
                                self.logger.error('could not switch relay port: %s: %s',
                                                  type(exc).__name__, exc)
                        if time.monotonic() - started >= 60:
                            attempt = 0
                        delay = backoff(attempt)
                        attempt += 1
                        retry_at = time.monotonic() + delay
                        self.logger.info('SSH reconnect in %ss', delay)
                    elif self.proc is None and time.monotonic() >= retry_at:
                        # Validate deployment before opening the command API.
                        self.status = 'connecting'
                        relay_port = self._choose_relay_port()
                        relay_command(self.layout, 1, relay_port=relay_port)
                        with self.lifecycle:
                            if self.service is None:
                                service = ControlService(self.layout, self.logger)
                                service.start()
                                self.service = service
                        command = relay_command(self.layout, self.service.server.server_port,
                                                relay_port=relay_port)
                        with (self.layout.logs / 'remote-channel.log').open('ab') as log:
                            self.proc = subprocess.Popen(
                                command, stdout=log, stderr=log,
                                **hidden(command, popen=True))
                        started = time.monotonic()
                        self.status = 'active'
                        self.logger.info('SSH launched; forwarding success pending (pid=%s)', self.proc.pid)
                except Exception as exc:
                    self.logger.error('remote channel failure: %s: %s', type(exc).__name__, exc)
                    self.cleanup()
                    delay = backoff(attempt)
                    attempt += 1
                    retry_at = time.monotonic() + delay
                    self.logger.info('remote channel retry in %ss', delay)
                    # Configuration/read failures must not become a busy loop.
                    self.stopped.wait(delay)
                self.stopped.wait(1)
        finally:
            self.cleanup()

    def stop(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=30)
            if self.thread.is_alive():
                raise RuntimeError('remote channel is still stopping')
        # Retry resources retained after a background cleanup failure.
        self.cleanup()
