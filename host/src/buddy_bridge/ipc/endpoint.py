"""endpoint.json — discovery file for the daemon's loopback IPC listener.

Shape: ``{"port": int, "token": str, "pid": int}`` under the bridge home.
A stale endpoint (daemon crashed, machine rebooted, port re-used) is detected
by checking the recorded pid is alive AND something accepts on the port.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

ENDPOINT_FILENAME = "endpoint.json"


def endpoint_path(home: Path) -> Path:
    return home / ENDPOINT_FILENAME


def write_endpoint(home: Path, port: int, token: str, pid: int) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = endpoint_path(home)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"port": port, "token": token, "pid": pid}, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


def remove_endpoint(home: Path) -> None:
    try:
        endpoint_path(home).unlink()
    except OSError:
        pass


def read_endpoint(home: Path) -> dict | None:
    """Parse endpoint.json; None when absent/garbled/wrong shape."""
    try:
        data = json.loads(endpoint_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    port, token, pid = data.get("port"), data.get("token"), data.get("pid")
    if not isinstance(port, int) or isinstance(port, bool):
        return None
    if not isinstance(token, str) or not token:
        return None
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None
    return {"port": port, "token": token, "pid": pid}


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # NOTE: os.kill(pid, 0) on Windows would TerminateProcess — never use it.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def probe(port: int, timeout: float = 0.5) -> bool:
    """True when something accepts a TCP connect on 127.0.0.1:port."""
    if not (0 < port < 65536):
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def is_stale(ep: dict) -> bool:
    if not pid_alive(int(ep.get("pid", -1))):
        return True
    if not probe(int(ep.get("port", 0))):
        return True
    return False


def load_live_endpoint(home: Path) -> dict | None:
    """Endpoint dict only when it exists and looks alive; else None."""
    ep = read_endpoint(home)
    if ep is None or is_stale(ep):
        return None
    return ep
