"""LAN relay federation: several buddy-bridge machines, one stick.

The stick is a single-central BLE peripheral, so exactly one bridge holds it
at a time (the *holder*). The relay lets every other bridge surface its
sessions and pending prompts on the stick anyway, routes stick decisions
back to the machine that asked, and lets the holder yield the link early
when a peer needs attention while it sits idle.

  Discovery   UDP broadcast on the [relay] port every DISCOVERY_INTERVAL_SECS
              (and immediately when the holder state flips):
                {"bb":1,"v":2,"id","name","port","holder","ts"}
              With [relay] peers configured, the same signed datagram is
              also sent as unicast to each static peer (overlay networks —
              Tailscale/WireGuard — have no broadcast domain). Peers expire
              after PEER_TTL_SECS without a datagram; a successful TCP
              exchange also refreshes liveness (one-way-UDP overlays).
  Events      one newline-terminated JSON frame per short-lived TCP
              connection to the target's announced port (same framing as the
              loopback IPC, but LAN-bound):
                v1 (still spoken, and the fallback when the holder is v1):
                  prompt_pending  {host,name,agent,tool,hint,sid}
                  prompt_resolved {host,sid[,pid]}
                  attention       {host,name,n}
                v2 (federation):
                  state_sync      {host,name,sessions:[...],prompt?,ask?}
                                  non-holder -> holder, on-change + 5s
                                  keepalive; shapes match the PROTOCOL_V2
                                  heartbeat composer output
                  decision        {host,id,decision}  holder -> origin,
                                  retried; resolves the origin's pending
                                  gate exactly like a local stick button

Security model (trusted LAN / overlay, shared secret):
  * every datagram and TCP frame is HMAC-SHA256 signed with the [relay]
    token over its canonical JSON body; unsigned/garbled/wrong-token input
    is dropped without a reply;
  * frames carry a ``ts`` and anything outside FRESH_WINDOW_SECS is dropped,
    bounding replay;
  * v2 relays *decisions*: whoever holds the token can approve another
    machine's prompt — treat the token like an SSH key (long, random,
    never on a network segment you don't trust);
  * nothing else sensitive rides the relay: session titles, tool names and
    pre-truncated prompt hints only — never credentials or transcripts;
  * no socket is opened unless [relay] enabled = true AND the token is
    non-empty — the daemon does not even construct a RelayManager otherwise,
    and only then does anything bind 0.0.0.0.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import socket
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Sequence

log = logging.getLogger(__name__)

RELAY_PROTO = 2  # "v" in discovery datagrams; absent = 1 (pre-federation)
DISCOVERY_INTERVAL_SECS = 30.0
HOLDER_POLL_SECS = 1.0  # cadence of the announce loop's holder-flip check
PEER_TTL_SECS = 120.0
FRESH_WINDOW_SECS = 120.0  # max |now - ts| for any signed frame
SEND_TIMEOUT_SECS = 2.0
MAX_FRAME_BYTES = 16 * 1024
HINT_MAX_CHARS = 80
STATE_SYNC_KEEPALIVE_SECS = 5.0  # unchanged state is still resent this often
DECISION_SEND_ATTEMPTS = 3
DECISION_RETRY_DELAY_SECS = 2.0
RESOLVE_TTL_SECS = 300.0  # static-peer DNS answers cached this long

EV_PROMPT_PENDING = "prompt_pending"
EV_PROMPT_RESOLVED = "prompt_resolved"
EV_ATTENTION = "attention"
EV_STATE_SYNC = "state_sync"
EV_DECISION = "decision"

PeerEventHandler = Callable[[dict], Awaitable[None]]


# ---- signed frames -------------------------------------------------------


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _mac(payload: dict, token: str) -> str:
    return hmac.new(token.encode("utf-8"), _canonical(payload), hashlib.sha256).hexdigest()


def sign_frame(payload: dict, token: str) -> bytes:
    """One wire frame: payload + its HMAC, newline-terminated."""
    body = dict(payload)
    body["mac"] = _mac(payload, token)
    return json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("utf-8") + b"\n"


def parse_frame(
    raw: bytes, token: str, *, now: Optional[float] = None
) -> Optional[dict]:
    """Verify + parse one frame. None for anything unsigned, tampered,
    wrong-token, non-object, oversized, or outside the freshness window."""
    if not token or len(raw) > MAX_FRAME_BYTES:
        return None
    try:
        obj = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    mac = obj.pop("mac", None)
    if not isinstance(mac, str):
        return None
    if not hmac.compare_digest(_mac(obj, token), mac):
        return None
    ts = obj.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    if abs((now if now is not None else time.time()) - float(ts)) > FRESH_WINDOW_SECS:
        return None
    return obj


# ---- peer table ----------------------------------------------------------


@dataclass
class Peer:
    id: str
    name: str
    host: str  # IP the datagram came from
    port: int  # announced TCP event port
    holder: bool
    last_seen: float
    ver: int = 1  # discovery "v"; 1 = pre-federation (cards only)


class PeerTable:
    """Peers observed via discovery datagrams; expired after PEER_TTL_SECS.

    Liveness = max(last verified datagram, last successful TCP exchange):
    ``touch()`` lets TCP activity keep a peer alive on overlay networks
    where UDP only passes one way."""

    def __init__(self, own_id: str, now_fn: Callable[[], float] = time.time) -> None:
        self._own_id = own_id
        self._now = now_fn
        self._peers: dict[str, Peer] = {}

    def observe(self, payload: dict, addr: tuple) -> Optional[Peer]:
        """Fold one verified discovery payload in. None when it isn't a
        discovery frame or announces ourselves (broadcast echo)."""
        if payload.get("bb") != 1 or "ev" in payload:
            return None
        peer_id = payload.get("id")
        port = payload.get("port")
        if not isinstance(peer_id, str) or not peer_id or peer_id == self._own_id:
            return None
        if not isinstance(port, int) or isinstance(port, bool) or not (0 < port < 65536):
            return None
        name = payload.get("name")
        ver = payload.get("v")
        peer = Peer(
            id=peer_id,
            name=name if isinstance(name, str) and name else peer_id,
            host=str(addr[0]),
            port=port,
            holder=payload.get("holder") is True,
            last_seen=self._now(),
            ver=ver if isinstance(ver, int) and not isinstance(ver, bool) and ver > 0 else 1,
        )
        self._peers[peer_id] = peer
        return peer

    def touch(self, peer_id: str) -> None:
        """Refresh a known peer's liveness after a successful TCP exchange."""
        peer = self._peers.get(peer_id)
        if peer is not None:
            peer.last_seen = self._now()

    def get(self, peer_id: str) -> Optional[Peer]:
        self.prune()
        return self._peers.get(peer_id)

    def prune(self) -> None:
        cutoff = self._now() - PEER_TTL_SECS
        for key in [k for k, p in self._peers.items() if p.last_seen < cutoff]:
            del self._peers[key]

    def peers(self) -> list[Peer]:
        self.prune()
        return sorted(self._peers.values(), key=lambda p: p.id)

    def holder(self) -> Optional[Peer]:
        """The peer currently claiming the stick (freshest claim wins — BLE
        is single-central so overlapping claims only happen transiently)."""
        holders = [p for p in self.peers() if p.holder]
        return max(holders, key=lambda p: p.last_seen) if holders else None


# ---- sockets (isolated so tests can stub them; nothing else binds) -------


def _local_ip() -> str:
    """Primary local IPv4 via a connected UDP socket (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1: never routed
            return probe.getsockname()[0]
    except OSError:
        return ""


def broadcast_addrs() -> list[str]:
    """Limited broadcast plus a /24 directed-broadcast guess of the primary
    interface (some networks filter 255.255.255.255)."""
    addrs = ["255.255.255.255"]
    ip = _local_ip()
    parts = ip.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        directed = ".".join(parts[:3] + ["255"])
        if directed not in addrs:
            addrs.append(directed)
    return addrs


def parse_static_peers(entries: Sequence[str], default_port: int) -> list[tuple[str, int]]:
    """[relay] peers entries -> (host, port) pairs. ``"host"`` or
    ``"host:port"``; host is an IPv4 literal or a DNS/MagicDNS name (no raw
    IPv6 — Tailscale MagicDNS names are the supported spelling there)."""
    out: list[tuple[str, int]] = []
    for entry in entries:
        entry = str(entry or "").strip()
        if not entry:
            continue
        host, sep, tail = entry.rpartition(":")
        if sep and host and tail.isdigit() and 0 < int(tail) < 65536:
            out.append((host, int(tail)))
        else:
            out.append((entry, default_port))
    return out


async def _resolve_host(host: str) -> Optional[str]:
    """One IPv4 address for a static-peer host, or None (quietly) when
    resolution fails — an offline MagicDNS peer must never break announce."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, family=socket.AF_INET, type=socket.SOCK_DGRAM
        )
    except (OSError, socket.gaierror):
        return None
    return infos[0][4][0] if infos else None


async def _open_udp(manager: "RelayManager", port: int):
    """Bind the discovery socket (rx broadcasts + tx). Returns the transport."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        sock.bind(("", port))
    except OSError:
        sock.close()
        raise
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _DiscoveryProtocol(manager), sock=sock
    )
    return transport


async def _open_tcp(manager: "RelayManager", port: int):
    """Bind the TCP event endpoint on the configured port, falling back to an
    ephemeral one (the real port is announced via discovery either way)."""
    try:
        return await asyncio.start_server(
            manager._on_tcp_connection, host="0.0.0.0", port=port, limit=MAX_FRAME_BYTES
        )
    except OSError:
        return await asyncio.start_server(
            manager._on_tcp_connection, host="0.0.0.0", port=0, limit=MAX_FRAME_BYTES
        )


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, manager: "RelayManager") -> None:
        self._manager = manager

    def datagram_received(self, data: bytes, addr) -> None:
        self._manager._on_datagram(data, addr)

    def error_received(self, exc) -> None:  # pragma: no cover - platform noise
        log.debug("relay: udp error: %s", exc)


# ---- manager -------------------------------------------------------------


class RelayManager:
    """Owns the relay sockets + peer view for one daemon.

    ``is_holder``       -> True while this bridge holds the BLE link.
    ``on_peer_event``   awaited with each verified inbound event payload.
    ``on_holder_view``  called (sync) when the peer holder-view changed —
                        the daemon uses it to fast-reconnect when the
                        holder released the stick and we have local demand.
    """

    def __init__(
        self,
        *,
        host_id: str,
        host_name: str,
        port: int,
        token: str,
        is_holder: Callable[[], bool],
        on_peer_event: PeerEventHandler,
        on_holder_view: Optional[Callable[[], None]] = None,
        static_peers: Sequence[str] = (),
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.host_id = host_id
        self.host_name = host_name
        self.port = port
        self.token = token
        self.tcp_port = port  # rebound by start() if the fixed port was taken
        self._is_holder = is_holder
        self._on_peer_event = on_peer_event
        self._on_holder_view = on_holder_view
        self._now = now_fn
        self.peers = PeerTable(host_id, now_fn)
        self.static_peers = parse_static_peers(static_peers, port)
        self._resolve_cache: dict[str, tuple[str, float]] = {}  # host -> (ip, ts)
        self._udp_transport = None
        self._tcp_server = None
        self._stop_evt = asyncio.Event()
        self._announced_holder: Optional[bool] = None
        # non-holder local state mirrored to the holder
        self._relayed_prompts: dict[str, dict] = {}  # sid -> {agent,tool,hint} (v1)
        self._last_attention_n = 0
        self._state_sig: Optional[str] = None  # last state_sync actually sent (v2)
        self._state_sent_at = 0.0

    # ---- lifecycle ----

    async def start(self) -> list[asyncio.Task]:
        """Bind sockets + start the announce loop. The ONLY socket-touching
        entry point; the daemon never calls it unless [relay] is active."""
        self._udp_transport = await _open_udp(self, self.port)
        self._tcp_server = await _open_tcp(self, self.port)
        self.tcp_port = self._tcp_server.sockets[0].getsockname()[1]
        log.info(
            "relay: up (udp discovery :%d, tcp events :%d, host %s %r)",
            self.port, self.tcp_port, self.host_id, self.host_name,
        )
        await self.announce()
        return [asyncio.create_task(self._announce_loop(), name="relay-announce")]

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._udp_transport is not None:
            self._udp_transport.close()
            self._udp_transport = None
        if self._tcp_server is not None:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
            self._tcp_server = None

    # ---- discovery ----

    def _discovery_payload(self) -> dict:
        return {
            "bb": 1,
            "v": RELAY_PROTO,
            "id": self.host_id,
            "name": self.host_name,
            "port": self.tcp_port,
            "holder": bool(self._is_holder()),
            "ts": int(self._now()),
        }

    async def announce(self) -> None:
        if self._udp_transport is None:
            return
        payload = self._discovery_payload()
        self._announced_holder = payload["holder"]
        frame = sign_frame(payload, self.token)
        for addr in broadcast_addrs():
            try:
                self._udp_transport.sendto(frame, (addr, self.port))
            except OSError as exc:
                log.debug("relay: broadcast to %s failed: %s", addr, exc)
        # Static peers (overlay networks): the same signed datagram, unicast.
        for host, port in self.static_peers:
            addr = await self._resolve(host)
            if addr is None:
                continue
            try:
                self._udp_transport.sendto(frame, (addr, port))
            except OSError as exc:
                self._resolve_cache.pop(host, None)  # stale answer? re-resolve next time
                log.debug("relay: unicast to %s (%s) failed: %s", host, addr, exc)

    async def _resolve(self, host: str) -> Optional[str]:
        """Resolve a static-peer host with a small TTL cache; failures are
        quiet (the peer may simply be offline) and never cached."""
        cached = self._resolve_cache.get(host)
        if cached is not None and self._now() - cached[1] < RESOLVE_TTL_SECS:
            return cached[0]
        addr = await _resolve_host(host)
        if addr is None:
            log.debug("relay: could not resolve static peer %r", host)
            return None
        self._resolve_cache[host] = (addr, self._now())
        return addr

    async def _announce_loop(self) -> None:
        last = self._now()
        while not self._stop_evt.is_set():
            await asyncio.sleep(HOLDER_POLL_SECS)
            try:
                self.peers.prune()
                holder = bool(self._is_holder())
                due = self._now() - last >= DISCOVERY_INTERVAL_SECS
                if due or holder != self._announced_holder:
                    await self.announce()  # holder flips announce immediately
                    last = self._now()
            except Exception:  # noqa: BLE001
                log.exception("relay: announce loop failed")

    def _on_datagram(self, data: bytes, addr) -> None:
        payload = parse_frame(data, self.token, now=self._now())
        if payload is None:
            log.debug("relay: dropped datagram from %s (bad signature/shape)", addr)
            return
        before = self.peers.holder()
        peer = self.peers.observe(payload, addr)
        if peer is None:
            return
        after = self.peers.holder()
        changed = (before.id if before else None, before.holder if before else None) != (
            after.id if after else None, after.holder if after else None,
        )
        if changed and self._on_holder_view is not None:
            self._on_holder_view()

    # ---- inbound events (we are the holder) ----

    async def _on_tcp_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            try:
                raw = await asyncio.wait_for(reader.readline(), SEND_TIMEOUT_SECS)
            except (asyncio.TimeoutError, asyncio.LimitOverrunError, ValueError):
                return
            payload = parse_frame(raw.strip(), self.token, now=self._now())
            if payload is None or payload.get("bb") != 1:
                log.debug("relay: rejected tcp frame (bad signature/shape)")
                return
            if not isinstance(payload.get("ev"), str) or payload.get("host") == self.host_id:
                return
            # A verified frame is a live peer, whatever UDP thinks (overlay
            # networks sometimes pass UDP only one way).
            self.peers.touch(str(payload.get("host") or ""))
            try:
                await self._on_peer_event(payload)
            except Exception:  # noqa: BLE001
                log.exception("relay: peer event handler crashed")
        except (ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    # ---- outbound events ----

    async def _send_frame(self, host: str, port: int, event: dict[str, Any]) -> bool:
        """Sign + deliver one event frame to a peer's TCP endpoint."""
        payload = {"bb": 1, "host": self.host_id, "ts": int(self._now()), **event}
        frame = sign_frame(payload, self.token)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), SEND_TIMEOUT_SECS
            )
        except (OSError, asyncio.TimeoutError):
            return False
        try:
            writer.write(frame)
            await asyncio.wait_for(writer.drain(), SEND_TIMEOUT_SECS)
            return True
        except (OSError, asyncio.TimeoutError):
            return False
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def send_to_peer(self, peer_id: str, event: dict[str, Any]) -> bool:
        """Deliver one event frame to a known peer (by id)."""
        peer = self.peers.get(peer_id)
        if peer is None:
            return False
        ok = await self._send_frame(peer.host, peer.port, event)
        if ok:
            self.peers.touch(peer_id)
        return ok

    async def send_to_holder(self, event: dict[str, Any]) -> bool:
        """Sign + deliver one event frame to the current holder peer."""
        holder = self.peers.holder()
        if holder is None:
            return False
        return await self.send_to_peer(holder.id, event)

    async def send_decision(self, peer_id: str, orig_id: str, decision: str) -> bool:
        """Relay a stick decision to the machine that owns the prompt.

        Retried up to DECISION_SEND_ATTEMPTS spaced DECISION_RETRY_DELAY_SECS
        apart — the origin keeps its own deadline and fails open to its
        native prompt regardless, so this only has to be best-effort. If
        every attempt fails, the origin's 5s state_sync keepalive re-offers
        the prompt and the user can simply answer again."""
        event = {"ev": EV_DECISION, "id": orig_id, "decision": decision}
        for attempt in range(DECISION_SEND_ATTEMPTS):
            if attempt:
                await asyncio.sleep(DECISION_RETRY_DELAY_SECS)
            if await self.send_to_peer(peer_id, event):
                return True
            log.info(
                "relay: decision %r for %s undelivered (attempt %d/%d)",
                decision, peer_id, attempt + 1, DECISION_SEND_ATTEMPTS,
            )
        return False

    # ---- state_sync (we are NOT the holder; the holder speaks v2) ----

    def holder_target(self) -> Optional[Peer]:
        """The current holder peer, but only when it can ingest state_sync."""
        holder = self.peers.holder()
        return holder if holder is not None and holder.ver >= 2 else None

    def reset_state_sync(self) -> None:
        """Forget the last-sent signature so the next sync pushes a full
        state (holder changed / we just released the stick)."""
        self._state_sig = None
        self._state_sent_at = 0.0

    async def sync_state(self, state: dict[str, Any], *, force: bool = False) -> bool:
        """Mirror the full local state to a v2 holder (federation).

        Sends on content change, at most-every-keepalive otherwise; a send
        failure clears the signature so the next tick retries. Returns True
        when the holder's view of us is fresh (sent now, or unchanged and
        recently sent)."""
        if self._is_holder():
            self.reset_state_sync()
            return True
        holder = self.holder_target()
        if holder is None:
            return False
        sig = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        now = self._now()
        if (
            not force
            and sig == self._state_sig
            and now - self._state_sent_at < STATE_SYNC_KEEPALIVE_SECS
        ):
            return True
        if await self.send_to_peer(holder.id, {"ev": EV_STATE_SYNC, **state}):
            self._state_sig = sig
            self._state_sent_at = now
            return True
        self.reset_state_sync()
        return False

    async def sync_local(self, prompts: dict[str, dict], waiting_count: int) -> None:
        """Mirror local pending prompts to the holder.

        ``prompts``: sid -> {"agent","tool","hint"} for every locally waiting
        session with a concrete prompt hint. Called periodically by the
        daemon; diffs against what was already relayed. While WE hold the
        stick nothing is relayed (prompts render locally) and the mirror
        state resets, so losing the stick re-relays anything still pending.
        """
        if self._is_holder():
            self._relayed_prompts = {}
            self._last_attention_n = waiting_count
            return
        for sid, info in prompts.items():
            if self._relayed_prompts.get(sid) == info:
                continue
            event = {
                "ev": EV_PROMPT_PENDING,
                "name": self.host_name,
                "agent": str(info.get("agent") or ""),
                "tool": str(info.get("tool") or "")[:32],
                "hint": str(info.get("hint") or "")[:HINT_MAX_CHARS],
                "sid": sid,
            }
            if await self.send_to_holder(event):
                self._relayed_prompts[sid] = dict(info)
        for sid in [s for s in self._relayed_prompts if s not in prompts]:
            # Resolved/vanished locally: tell the holder, then forget the sid
            # either way (an unreachable holder has likely changed, and the
            # new one never saw the pending).
            await self.send_to_holder({"ev": EV_PROMPT_RESOLVED, "sid": sid})
            del self._relayed_prompts[sid]
        if waiting_count != self._last_attention_n:
            if waiting_count > 0 and not prompts:
                # Coarse fallback: sessions wait but none carries a hint.
                await self.send_to_holder(
                    {"ev": EV_ATTENTION, "name": self.host_name, "n": int(waiting_count)}
                )
            self._last_attention_n = waiting_count

    # ---- introspection ----

    def status(self) -> dict[str, Any]:
        holder = self.peers.holder()
        return {
            "active": True,
            "proto": RELAY_PROTO,
            "port": self.port,
            "tcp_port": self.tcp_port,
            "holder": bool(self._is_holder()),
            "holder_peer": holder.name if holder else None,
            "holder_v2": self.holder_target() is not None,
            "peers": len(self.peers.peers()),
            "static_peers": len(self.static_peers),
        }
