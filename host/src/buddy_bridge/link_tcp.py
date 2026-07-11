"""WiFi/TCP device link: the stick connects over the LAN instead of BLE.

The bridge listens on ``[device_link] port`` (default 48902); the stick is
the TCP *client* and dials in whenever it is powered, on WiFi, and in range
of the provisioned host. The first line of every connection authenticates
it; after that the connection is a plain newline-JSON pipe with semantics
IDENTICAL to the BLE NUS link (bridge sends hello/heartbeats/one-shots,
stick sends decisions/acks), so the daemon's session pipeline stays
transport-blind.

  Auth      stick's first line (canonical relay signing scheme — see
            relay.py's ``sign_frame``/``parse_frame``; the signature field
            is ``mac_sig`` because ``mac`` is the device's BT MAC):
              {"hello_link":1,"mac":"<bt-mac>","ts":<epoch>,"mac_sig":"<hmac>"}
            mac_sig = HMAC-SHA256(token) over the canonical JSON minus
            mac_sig; |now - ts| must be within FRESH_WINDOW_SECS (±120 s).
            Verified -> {"ack":"hello_link","ok":true} and the connection
            becomes THE device pipe. Anything else -> closed immediately,
            no reply (no oracle), logged once, and further accepts from
            that IP are rate-limited (AUTH_FAIL_LIMIT per minute).
  One conn  exactly one authenticated device connection at a time: a second
            valid connect replaces the first (newest wins, old closed with
            a log) and the hello negotiation re-runs on the new pipe.

Security model (trusted LAN / overlay, shared secret — the relay's model):
  * the ``[device_link]`` token is the link authority: whoever holds it can
    BE the stick (receive prompts, answer them). Treat it like the [relay]
    token; it is deliberately a SEPARATE secret (separate blast radius).
  * the token itself only ever travels over the encrypted BLE pipe during
    provisioning — never over this TCP link, which is plaintext after its
    HMAC handshake. What rides here post-auth is exactly what the BLE link
    carries: session titles, hints, decisions. Never WiFi passwords.
  * no socket is opened unless [device_link] enabled = true AND the token
    is non-empty — the daemon does not even construct a DeviceLinkServer
    otherwise, and only then does anything bind 0.0.0.0.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from .relay import FRESH_WINDOW_SECS, _local_ip, _mac

log = logging.getLogger(__name__)

DEVICE_LINK_PORT = 48902
AUTH_TIMEOUT_SECS = 5.0  # max wait for the stick's hello_link line
SEND_TIMEOUT_SECS = 2.0
MAX_LINE_BYTES = 64 * 1024
AUTH_FAIL_LIMIT = 3  # failed auths per window before an IP is shunned
AUTH_FAIL_WINDOW_SECS = 60.0

_ACK_LINE = (
    json.dumps({"ack": "hello_link", "ok": True}, separators=(",", ":")).encode("utf-8") + b"\n"
)

LineHandler = Callable[[dict[str, Any]], Awaitable[None]]
UpDownHandler = Callable[[], Awaitable[None]]


def detect_host_ip() -> str:
    """This machine's primary LAN IPv4 for provisioning (the relay's
    connected-UDP-socket trick; no packet is ever sent). ``""`` when it
    cannot be determined — the caller must then require an explicit host."""
    return _local_ip()


def build_hello_link(mac: str, token: str, *, ts: Optional[float] = None) -> bytes:
    """The stick's auth line, newline-terminated (reference implementation
    for the firmware, and the tests' frame builder)."""
    payload: dict[str, Any] = {
        "hello_link": 1,
        "mac": mac,
        "ts": int(ts if ts is not None else time.time()),
    }
    payload["mac_sig"] = _mac(payload, token)
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8") + b"\n"


def parse_hello_link(raw: bytes, token: str, *, now: Optional[float] = None) -> Optional[dict]:
    """Verify + parse the stick's opening auth line. None for anything
    unsigned, tampered, wrong-token, non-object, oversized, MAC-less, or
    outside the freshness window — EXACTLY the relay signing scheme
    (canonical JSON + HMAC-SHA256 + ±FRESH_WINDOW_SECS), with the signature
    carried in ``mac_sig``."""
    if not token or len(raw) > MAX_LINE_BYTES:
        return None
    try:
        obj = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("hello_link") != 1:
        return None
    sig = obj.pop("mac_sig", None)
    if not isinstance(sig, str):
        return None
    if not hmac.compare_digest(_mac(obj, token), sig):
        return None
    ts = obj.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    if abs((now if now is not None else time.time()) - float(ts)) > FRESH_WINDOW_SECS:
        return None
    mac = obj.get("mac")
    if not isinstance(mac, str) or not mac:
        return None
    return obj


class DeviceLinkServer:
    """Owns the WiFi/TCP device transport for one daemon.

    Exposes the same surface the daemon consumes from the BLE LinkManager —
    ``send_line``, an ``on_line`` inbound callback, a ``connected`` flag,
    ``drop_connection`` and ``stop`` — plus ``on_up``/``on_down`` lifecycle
    callbacks the daemon uses to arbitrate BLE pause/resume.
    """

    def __init__(
        self,
        *,
        port: int,
        token: str,
        on_line: LineHandler,
        on_up: Optional[UpDownHandler] = None,
        on_down: Optional[UpDownHandler] = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.port = int(port)
        self.token = token
        self.on_line = on_line
        self.on_up = on_up
        self.on_down = on_down
        self._now = now_fn
        self._server: asyncio.AbstractServer | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._gen = 0  # bumps per authenticated connection (newest wins)
        self._device_mac = ""
        self._auth_fails: dict[str, list[float]] = {}  # ip -> recent failure stamps
        self._send_lock = asyncio.Lock()
        self._stopping = False

    # ---- lifecycle ----

    async def start(self) -> None:
        """Bind the listener. The ONLY socket-touching entry point; the
        daemon never calls it unless [device_link] is active (enabled AND
        non-empty token)."""
        self._server = await asyncio.start_server(
            self._on_connection, host="0.0.0.0", port=self.port, limit=MAX_LINE_BYTES
        )
        log.info("devlink: listening on 0.0.0.0:%d (wifi device link)", self.port)

    async def stop(self) -> None:
        self._stopping = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await self.drop_connection()

    # ---- LinkManager-parity surface ----

    @property
    def connected(self) -> bool:
        return self._writer is not None

    async def send_line(self, line: str) -> bool:
        """Write one newline-terminated protocol line. True on success."""
        writer = self._writer
        if writer is None:
            return False
        try:
            async with self._send_lock:
                writer.write(line.encode("utf-8") + b"\n")
                await asyncio.wait_for(writer.drain(), SEND_TIMEOUT_SECS)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("devlink: send failed: %s", exc)
            # Nudge the dead transport shut; the read loop does the
            # bookkeeping (state clear + on_down) when it sees the close.
            with contextlib.suppress(Exception):
                writer.close()
            return False

    async def drop_connection(self) -> None:
        """Close the current authenticated connection (dead-link recovery —
        the stick's own retry loop reconnects). State clearing and the
        on_down callback run in the connection's read loop."""
        writer = self._writer
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    def status(self) -> dict[str, Any]:
        return {
            "active": True,
            "port": self.port,
            "connected": self.connected,
            "device_mac": self._device_mac,
        }

    # ---- accept path ----

    def _rate_limited(self, ip: str) -> bool:
        fails = self._auth_fails.get(ip)
        if not fails:
            return False
        cutoff = self._now() - AUTH_FAIL_WINDOW_SECS
        fails[:] = [t for t in fails if t > cutoff]
        if not fails:
            self._auth_fails.pop(ip, None)
            return False
        return len(fails) >= AUTH_FAIL_LIMIT

    def _record_auth_failure(self, ip: str) -> None:
        fails = self._auth_fails.setdefault(ip, [])
        fails.append(self._now())
        if len(fails) == 1:  # log once, not per probe
            log.warning(
                "devlink: rejected unauthenticated connection from %s "
                "(bad signature / stale ts); rate-limiting that address",
                ip or "?",
            )
        else:
            log.debug("devlink: rejected connection from %s (%d recent)", ip or "?", len(fails))

    async def _hang_up(self, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()

    async def _on_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        ip = str(peer[0]) if isinstance(peer, tuple) and peer else ""
        if self._rate_limited(ip):
            await self._hang_up(writer)
            return
        try:
            raw = await asyncio.wait_for(reader.readline(), AUTH_TIMEOUT_SECS)
        except (asyncio.TimeoutError, asyncio.LimitOverrunError, ValueError):
            await self._hang_up(writer)
            return
        hello = parse_hello_link(raw.strip(), self.token, now=self._now())
        if hello is None:
            self._record_auth_failure(ip)
            await self._hang_up(writer)
            return
        self._auth_fails.pop(ip, None)
        try:
            writer.write(_ACK_LINE)
            await asyncio.wait_for(writer.drain(), SEND_TIMEOUT_SECS)
        except (OSError, asyncio.TimeoutError):
            await self._hang_up(writer)
            return

        old = self._writer
        self._gen += 1
        gen = self._gen
        self._writer = writer
        self._device_mac = str(hello.get("mac") or "")
        if old is not None:
            log.info("devlink: new authenticated connection replaces the previous one")
            with contextlib.suppress(Exception):
                old.close()  # its read loop sees the close and exits quietly
        else:
            log.info("devlink: device authenticated from %s", ip or "?")
        if self.on_up is not None:
            await self._safe_cb(self.on_up)
        try:
            await self._read_loop(reader, gen)
        finally:
            still_current = self._gen == gen and self._writer is writer
            if still_current:
                self._writer = None
                self._device_mac = ""
            await self._hang_up(writer)
            if still_current and not self._stopping:
                log.info("devlink: device connection closed")
                if self.on_down is not None:
                    await self._safe_cb(self.on_down)

    async def _read_loop(self, reader: asyncio.StreamReader, gen: int) -> None:
        """Inbound newline-JSON pipe: parse + dispatch until EOF/replacement.
        Lines are dispatched in order (awaited), matching the wire order the
        BLE assembler preserves."""
        while not self._stopping and self._gen == gen:
            try:
                raw = await reader.readline()
            except (asyncio.LimitOverrunError, ValueError):
                break
            except (ConnectionError, OSError):
                break
            if not raw:
                break
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                log.debug("devlink: dropping unparseable line: %r", raw[:80])
                continue
            if isinstance(obj, dict):
                try:
                    await self.on_line(obj)
                except Exception:  # noqa: BLE001
                    log.exception("devlink: on_line handler crashed")

    async def _safe_cb(self, cb: UpDownHandler) -> None:
        try:
            await cb()
        except Exception:  # noqa: BLE001
            log.exception("devlink: lifecycle callback crashed")
