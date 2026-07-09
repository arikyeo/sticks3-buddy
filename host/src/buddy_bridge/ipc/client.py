"""Synchronous IPC client — STDLIB ONLY.

This is the single buddy_bridge module hook scripts are allowed to import
(plus the standard library). It must stay dependency-free and silent:
every failure path is a no-op so a dead daemon can never break the host CLI
the hook is running inside.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

ENV_HOME = "BUDDY_BRIDGE_HOME"
CONNECT_TIMEOUT = 0.5
_MAX_RESPONSE_BYTES = 1024 * 1024


def bridge_home() -> Path:
    env = os.environ.get(ENV_HOME, "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".buddy-bridge"


def _read_endpoint(home: Path | None) -> tuple[int, str] | None:
    base = home if home is not None else bridge_home()
    try:
        data = json.loads((base / "endpoint.json").read_text(encoding="utf-8"))
        port, token = data["port"], data["token"]
        if isinstance(port, int) and not isinstance(port, bool) and isinstance(token, str):
            return port, token
    except Exception:
        pass
    return None


def _encode(token: str, msg: dict) -> bytes:
    body: dict = {"token": token}  # token first on the wire
    body.update(msg)
    return (json.dumps(body, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def send_event(msg: dict, home: Path | None = None) -> bool:
    """Fire-and-forget one event line. True only if handed to the socket."""
    ep = _read_endpoint(home)
    if ep is None:
        return False
    port, token = ep
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=CONNECT_TIMEOUT) as sock:
            sock.sendall(_encode(token, msg))
        return True
    except Exception:
        return False


def request(msg: dict, timeout: float, home: Path | None = None) -> dict | None:
    """Send one request line, wait up to ``timeout`` for one JSON line back.

    None on any failure (no endpoint, refused, timeout, garbage) — callers
    treat None as "no decision" and fail open.
    """
    ep = _read_endpoint(home)
    if ep is None:
        return None
    port, token = ep
    deadline = time.monotonic() + max(timeout, 0.0)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=CONNECT_TIMEOUT) as sock:
            sock.sendall(_encode(token, msg))
            buf = b""
            while b"\n" not in buf:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or len(buf) > _MAX_RESPONSE_BYTES:
                    return None
                sock.settimeout(remaining)
                chunk = sock.recv(4096)
                if not chunk:
                    return None
                buf += chunk
            out = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
            return out if isinstance(out, dict) else None
    except Exception:
        return None
