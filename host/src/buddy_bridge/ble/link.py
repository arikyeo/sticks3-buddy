# Portions vendored from SnowWarri0r/cc-buddy-bridge@5c5b6142c54488491d4a039be23ccc69dcf00146
# (MIT) — connect/serve/reconnect loop shape, UTF-8-safe chunking, backoff policy.
"""Persistent BLE link to the buddy over Nordic UART Service.

Scans for peripherals whose advertised name starts with the configured prefix
(or an explicitly pinned address), connects, subscribes to TX notifications,
and exposes ``send_line`` which writes newline-terminated JSON to RX in
UTF-8-safe chunks of (mtu-3) bytes, floor 20. Every GATT write is wrapped in
``asyncio.wait_for(..., SEND_TIMEOUT_SECS)`` so a wedged stack cannot hang the
daemon. Inbound notifications are reassembled into lines and parsed as JSON.

Reconnect uses exponential backoff 3s -> 60s; on Windows, 5 consecutive scan
misses trigger a rationed radio power-cycle (see winrt_radio).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import time
from typing import Any, Awaitable, Callable, Optional

from ..protocol import NUS_RX_UUID, NUS_TX_UUID
from .winrt_radio import RadioCycler

log = logging.getLogger(__name__)

SCAN_TIMEOUT_SECS = 3.0 if sys.platform == "win32" else 10.0
BACKOFF_BASE_SECS = 3.0
BACKOFF_MAX_SECS = 60.0
STABLE_CONNECTION_SECS = 30.0
SEND_TIMEOUT_SECS = 2.0
CHUNK_FLOOR = 20
_ASSEMBLY_CAP = 64 * 1024

LineHandler = Callable[[dict[str, Any]], Awaitable[None]]


class LineAssembler:
    """Reassemble notify fragments into newline-terminated JSON objects."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        self._buf.extend(data)
        out: list[dict[str, Any]] = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            raw = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                log.debug("ble: dropping unparseable line: %r", raw[:80])
                continue
            if isinstance(obj, dict):
                out.append(obj)
        if len(self._buf) > _ASSEMBLY_CAP:
            log.warning("ble: line-assembly buffer overflow, clearing")
            self._buf.clear()
        return out


def utf8_safe_chunks(data: bytes, max_size: int) -> list[bytes]:
    """Split UTF-8 bytes so no chunk boundary lands inside a codepoint."""
    if max_size <= 0:
        raise ValueError("max_size must be positive")
    chunks: list[bytes] = []
    offset = 0
    while offset < len(data):
        end = min(offset + max_size, len(data))
        if end < len(data):
            safe_end = end
            while safe_end > offset and (data[safe_end] & 0b1100_0000) == 0b1000_0000:
                safe_end -= 1
            end = safe_end if safe_end > offset else min(
                offset + _codepoint_size(data[offset]), len(data)
            )
        chunks.append(data[offset:end])
        offset = end
    return chunks


def _codepoint_size(lead: int) -> int:
    if (lead & 0b1000_0000) == 0:
        return 1
    if (lead & 0b1110_0000) == 0b1100_0000:
        return 2
    if (lead & 0b1111_0000) == 0b1110_0000:
        return 3
    if (lead & 0b1111_1000) == 0b1111_0000:
        return 4
    return 1


class LinkManager:
    """Owns the BLE connection lifecycle. One instance per daemon.

    mode:
      exclusive — hold the connection open whenever the device is around.
      ondemand  — connect only while there is outbound traffic wanted
                  (``ensure_connected`` arms it), drop after IDLE_DISCONNECT_SECS.
      off       — never touch the radio; ``run()`` returns immediately.
    """

    IDLE_DISCONNECT_SECS = 120.0

    def __init__(
        self,
        on_line: LineHandler,
        *,
        name_prefix: str = "Claude",
        address: str = "",
        mode: str = "exclusive",
        on_connect: Optional[Callable[[], Awaitable[None]]] = None,
        on_disconnect: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self.on_line = on_line
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.name_prefix = name_prefix
        self.address = address.strip()
        self.mode = mode
        self._client = None  # BleakClient | None
        self._assembler = LineAssembler()
        self._connected_evt = asyncio.Event()
        self._want_evt = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._stop_evt = asyncio.Event()
        self._cycler = RadioCycler()
        self._last_activity = 0.0

    # ---- public API ----

    @property
    def connected(self) -> bool:
        client = self._client
        return client is not None and bool(client.is_connected)

    async def wait_connected(self) -> None:
        await self._connected_evt.wait()

    async def drop_connection(self) -> None:
        """Disconnect the current client (if any); the run() loop then walks
        its normal teardown + reconnect path. Used for dead-link recovery
        (rxack watch) and host-switch backoff."""
        client = self._client
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()

    async def ensure_connected(self, timeout: float) -> bool:
        """Arm an ondemand connection and wait for it. True when connected."""
        if self.mode == "off":
            return False
        self._want_evt.set()
        self._last_activity = time.monotonic()
        if self.connected:
            return True
        try:
            await asyncio.wait_for(self._connected_evt.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def send_line(self, line: str) -> bool:
        """Write one newline-terminated protocol line. True on success."""
        client = self._client
        if client is None or not client.is_connected:
            return False
        data = line.encode("utf-8") + b"\n"
        self._last_activity = time.monotonic()
        try:
            async with self._send_lock:
                chunk_size = max(CHUNK_FLOOR, int(getattr(client, "mtu_size", 23) or 23) - 3)
                for chunk in utf8_safe_chunks(data, chunk_size):
                    await asyncio.wait_for(
                        client.write_gatt_char(NUS_RX_UUID, chunk, response=False),
                        SEND_TIMEOUT_SECS,
                    )
                    # Yield so the host stack can drain write-without-response
                    # credits before the next chunk; prevents silent drops.
                    await asyncio.sleep(0)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("ble: send failed: %s", exc)
            return False

    async def run(self) -> None:
        """Connect/serve/reconnect until stop(). No-op when mode == 'off'."""
        if self.mode == "off":
            log.info("ble: mode=off, link disabled")
            return
        from bleak import BleakClient

        backoff = BACKOFF_BASE_SECS
        consecutive_misses = 0
        while not self._stop_evt.is_set():
            if self.mode == "ondemand" and not self._want_evt.is_set():
                await self._wait_want()
                continue
            connect_ts: float | None = None
            try:
                device = await self._find_device()
                if device is None:
                    consecutive_misses += 1
                    log.info(
                        "ble: no buddy found, retry in %.1fs (miss #%d)",
                        backoff,
                        consecutive_misses,
                    )
                    if await self._cycler.maybe_cycle(consecutive_misses):
                        backoff = BACKOFF_BASE_SECS  # cycle slept ~4s; fresh fast retry
                        continue
                    await self._sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX_SECS)
                    continue
                consecutive_misses = 0
                self._cycler.reset()
                log.info("ble: connecting to %s (%s)", device.name, device.address)
                async with BleakClient(device) as client:
                    self._client = client
                    self._assembler = LineAssembler()
                    await client.start_notify(NUS_TX_UUID, self._on_notify)
                    self._connected_evt.set()
                    connect_ts = time.monotonic()
                    self._last_activity = connect_ts
                    log.info("ble: connected, subscribed to TX notify")
                    if self.on_connect is not None:
                        await self._safe_cb(self.on_connect)
                    while client.is_connected and not self._stop_evt.is_set():
                        if self.mode == "ondemand" and (
                            time.monotonic() - self._last_activity > self.IDLE_DISCONNECT_SECS
                        ):
                            log.info("ble: ondemand idle, releasing connection")
                            self._want_evt.clear()
                            break
                        await asyncio.sleep(1.0)
                    # Clear immediately on observing the drop (teardown can lag).
                    self._connected_evt.clear()
                    log.info("ble: disconnected after %.1fs", time.monotonic() - connect_ts)
                self._cycler.reset()
            except Exception as exc:  # noqa: BLE001
                log.warning("ble: connection error: %s", exc)
            finally:
                self._client = None
                was_connected = self._connected_evt.is_set() or connect_ts is not None
                self._connected_evt.clear()
                if was_connected and self.on_disconnect is not None:
                    await self._safe_cb(self.on_disconnect)
            if not self._stop_evt.is_set():
                if connect_ts is not None and (
                    time.monotonic() - connect_ts >= STABLE_CONNECTION_SECS
                ):
                    backoff = BACKOFF_BASE_SECS
                await self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_SECS)

    async def stop(self) -> None:
        self._stop_evt.set()
        self._want_evt.set()  # unblock ondemand waiters
        client = self._client
        if client is not None and client.is_connected:
            with contextlib.suppress(Exception):
                await client.disconnect()

    # ---- internals ----

    async def _wait_want(self) -> None:
        want = asyncio.ensure_future(self._want_evt.wait())
        stop = asyncio.ensure_future(self._stop_evt.wait())
        try:
            await asyncio.wait({want, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for fut in (want, stop):
                if not fut.done():
                    fut.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await fut

    async def _sleep(self, secs: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop_evt.wait(), secs)

    async def _find_device(self):
        from bleak import BleakScanner

        if self.address:
            return await BleakScanner.find_device_by_address(
                self.address, timeout=SCAN_TIMEOUT_SECS
            )

        prefix = self.name_prefix

        def _match(device, adv) -> bool:
            name = (getattr(adv, "local_name", None) or device.name) or ""
            return name.startswith(prefix)

        return await BleakScanner.find_device_by_filter(_match, timeout=SCAN_TIMEOUT_SECS)

    def _on_notify(self, _handle: Any, data: bytearray) -> None:
        # bleak dispatches notify callbacks on the daemon's running loop.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for obj in self._assembler.feed(bytes(data)):
            loop.create_task(self._dispatch(obj))

    async def _dispatch(self, obj: dict[str, Any]) -> None:
        try:
            await self.on_line(obj)
        except Exception:  # noqa: BLE001
            log.exception("ble: on_line handler crashed")

    async def _safe_cb(self, cb: Callable[[], Awaitable[None]]) -> None:
        try:
            await cb()
        except Exception:  # noqa: BLE001
            log.exception("ble: lifecycle callback crashed")
