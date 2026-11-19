"""Machine fingerprint.

Windows values (plan section 9.2): MachineGuid (HKLM\\SOFTWARE\\Microsoft\\
Cryptography) + system-volume serial number + motherboard/BIOS UUID (WMI
Win32_ComputerSystemProduct.UUID).  MAC address is deliberately excluded: it is
changeable and virtual NICs move it.

machine_id = sha256 of the joined present components (first 32 hex chars), so a
missing/blank component degrades gracefully instead of hashing an empty string.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

from .hashing import sha256_bytes
from .subprocess_text import hidden_window_options, text_mode_options

_ZERO_UUIDS = {"", "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF",
               "00000000-0000-0000-0000-000000000000"}


def _machine_guid() -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography") as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value)
    except Exception:
        return ""


def _volume_serial(drive: str = "C:\\") -> str:
    if os.name != "nt":
        return ""
    try:
        import ctypes
        root = drive.rstrip("\\") + "\\"
        volume_serial = ctypes.c_uint(0)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), None, 0,
            ctypes.byref(volume_serial), None, None, None, 0)
        return f"{volume_serial.value:08X}" if ok else ""
    except Exception:
        return ""


def _bios_uuid_smbios() -> str:
    """Read the system UUID from the raw SMBIOS table (no subprocess).

    360 Total Security blocks powershell.exe on some customer machines and
    Windows 11 24H2+ no longer ships wmic, so both external commands are
    unreliable sources for this value.
    """
    if os.name != "nt":
        return ""
    try:
        import ctypes
        import ctypes.wintypes as wintypes

        kernel32 = ctypes.windll.kernel32
        get_table = kernel32.GetSystemFirmwareTable
        get_table.argtypes = [wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                              wintypes.DWORD]
        get_table.restype = wintypes.DWORD
        signature = 0x52534D42  # 'RSMB'
        size = get_table(signature, 0, None, 0)
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        written = get_table(signature, 0, buffer, size)
        if not written:
            return ""
        # RawSMBIOSData: 4 header bytes + DWORD length, then the structure table.
        table = buffer.raw[:written][8:]
        index = 0
        while index + 4 <= len(table):
            structure_type = table[index]
            length = table[index + 1]
            if length < 4 or structure_type == 127:
                break
            if structure_type == 1 and length >= 0x19:
                raw = table[index + 4:index + length][4:20]
                if raw not in (b"\x00" * 16, b"\xff" * 16):
                    return "-".join((raw[0:4][::-1].hex().upper(),
                                     raw[4:6][::-1].hex().upper(),
                                     raw[6:8][::-1].hex().upper(),
                                     raw[8:10].hex().upper(),
                                     raw[10:16].hex().upper()))
            cursor = index + length
            while (cursor + 1 < len(table)
                   and not (table[cursor] == 0 and table[cursor + 1] == 0)):
                cursor += 1
            index = cursor + 2
    except Exception:
        return ""
    return ""


def _bios_uuid() -> str:
    if os.name != "nt":
        return ""
    value = _bios_uuid_smbios()
    if value:
        return value
    # Legacy fallback only: never spawn powershell here (360 blocks it and the
    # resulting empty UUID would change machine_id and invalidate the license).
    command = ["wmic", "csproduct", "get", "uuid"]
    try:
        out = subprocess.run(command, capture_output=True, timeout=15,
                             **hidden_window_options(command),
                             **text_mode_options())
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in (out.stdout or "").splitlines():
        candidate = line.strip()
        if candidate and candidate.upper() not in _ZERO_UUIDS and candidate.lower() != "uuid":
            return candidate.upper()
    return ""


def _posix_components() -> dict:
    """Static-test fallback so the combine/hash logic is exercised on Linux."""
    components = {}
    for name, path in (("machine_id_file", "/etc/machine-id"),
                       ("host_id_file", "/var/lib/dbus/machine-id")):
        p = Path(path)
        try:
            if p.exists():
                value = p.read_text(encoding="utf-8").strip()
                if value:
                    components[name] = value
        except OSError:
            pass
    # Minimal containers often ship an empty /etc/machine-id and no dbus id.
    # Keep the Linux-only test fallback bindable (two stable components) so a
    # missing host file cannot masquerade as a product trial failure.  Windows
    # fingerprinting above is unchanged.
    if not components:
        try:
            info = os.uname()
            if info.nodename:
                components["hostname"] = info.nodename
            if info.machine:
                components["machine"] = info.machine
        except (AttributeError, OSError):
            pass
    return components


def collect_components() -> dict:
    if os.name == "nt":
        return {
            "machine_guid": _machine_guid(),
            "volume_serial": _volume_serial(),
            "bios_uuid": _bios_uuid(),
        }
    return _posix_components()


def fingerprint(components: dict | None = None) -> dict:
    components = components if components is not None else collect_components()
    present = {k: v for k, v in components.items() if v}
    joined = "\x1f".join(f"{k}={present[k]}" for k in sorted(present))
    machine_id = sha256_bytes(joined.encode("utf-8"))[:32]
    return {
        "machine_id": machine_id,
        "components": components,
        "stable_component_count": len(present),
        "weak": len(present) < 2,
        "platform": "windows" if os.name == "nt" else sys.platform,
    }
