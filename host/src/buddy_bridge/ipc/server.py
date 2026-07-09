"""Asyncio TCP IPC server bound to 127.0.0.1 on an ephemeral port.

Wire format: newline-delimited JSON objects. Auth is first-field token: every
inbound object must carry the shared secret in its ``token`` field (clients
serialize it as the first key). Any missing/bad token, non-object, or
unparseable line closes the connection without a reply.

A handler may return a dict; it is written back as one JSON line on the same
connection (used by blocking permission requests and status queries).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[Optional[dict[str, Any]]]]

MAX_LINE_BYTES = 256 * 1024


class IpcServer:
    def __init__(self, token: str, handler: Handler) -> None:
        self._token = token
        self._handler = handler
        self._server: asyncio.AbstractServer | None = None
        self._conn_tasks: set[asyncio.Task] = set()
        self.port: int = 0

    async def start(self, port: int = 0) -> int:
        self._server = await asyncio.start_server(
            self._on_connection, host="127.0.0.1", port=port, limit=MAX_LINE_BYTES
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for task in list(self._conn_tasks):
            task.cancel()
        for task in list(self._conn_tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._conn_tasks.clear()

    async def _on_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._conn_tasks.add(task)
        try:
            while True:
                try:
                    raw = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    break
                if not raw:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    break
                if not isinstance(msg, dict) or msg.pop("token", None) != self._token:
                    log.warning("ipc: rejected message (bad token)")
                    break
                try:
                    resp = await self._handler(msg)
                except Exception:
                    log.exception("ipc: handler crashed")
                    resp = None
                if resp is not None:
                    writer.write(
                        json.dumps(resp, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
                        + b"\n"
                    )
                    await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            if task is not None:
                self._conn_tasks.discard(task)
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
