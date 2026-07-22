# Connectivity — transports, federation, and reaching the buddy from anywhere

How a buddy device talks to the host bridge(s), how bridges federate across
machines and networks, and how a cellular buddy (T-Deck-class) reaches a
home broker without exposing the origin server's IP.

For the wire protocol itself see [PROTOCOL_V2.md](../PROTOCOL_V2.md); for the
T-Deck hardware port see [PORT_SPEC.md](PORT_SPEC.md) and
[TDECK_FEASIBILITY.md](TDECK_FEASIBILITY.md).

## Transports

The wire protocol is transport-agnostic newline-delimited JSON. A device speaks
it over whichever link is available:

| Transport | Where | Status |
| --- | --- | --- |
| **BLE NUS** | device ↔ a bridge (or the stock Claude Desktop app) on the same room | shipped |
| **WiFi/TCP device-link** | device ↔ a bridge on the same LAN; frees the BLE radio for the desktop app | host side shipped (`host/src/buddy_bridge/link_tcp.py`); firmware TCP client is the pending counterpart |
| **Cellular → MQTT/TLS** | device anywhere with signal ↔ a broker | planned (T-Deck phase 3, see PORT_SPEC §A) |

A device picks the best available link and fails over. Rough ladder:
USB-powered + LAN → WiFi/TCP; on battery + LAN → BLE; no LAN + cellular → MQTT.

## Federation across machines (bridges)

Each machine runs its own `buddy-bridge` daemon. One holds the physical device
link at a time (the "holder" / antenna); the others stream their sessions to
it over the LAN relay, so the buddy shows every machine's Claude/Codex sessions
in one list and decisions route back to the origin. See the daemon's relay /
federation modules and `host/README.md`.

The relay authenticates every datagram/frame with HMAC-SHA256 over a shared
token and rejects stale timestamps. On a flat LAN it self-discovers over UDP
broadcast (`peers = []`). **Across routed / overlay networks (Tailscale, VPN)
broadcast does not cross** — list the peers explicitly instead:

```toml
[relay]
enabled = true
token   = "…shared secret, same on every machine…"
peers   = ["desktop.your-tailnet.ts.net", "mbp.your-tailnet.ts.net"]
```

Tailscale is the supported way to federate bridges over the internet: the
machines are full OSes, run `tailscaled` natively, and reach each other by
MagicDNS name regardless of where they physically are.

## Reaching a cellular buddy from anywhere (and hiding the origin IP)

A T-Deck-class device on 4G is *away* from the LAN, so BLE and the WiFi/TCP
link do not apply. It reaches home over **cellular → MQTT-over-TLS**, using the
A7682E modem's on-chip MQTT client (`TinyGsmMqttA76xx`). Both the device and
every bridge connect *outbound* to a broker, so NAT never matters.

### Hiding the origin server IP — jump-box relay (recommended)

Goal: the device config and its cellular traffic must never reveal the real IP
of the origin server hosting the broker. The clean answer is **server-side**, a
disposable L4 relay in front of the origin — not a VPN on the device (a device
VPN still dials a known endpoint; it moves the exposure, it doesn't remove it).

```
T-Deck  --(cellular, MQTT/TLS to relay-host:8883)-->  cheap relay VPS
                                                        nginx stream{} / haproxy mode tcp
                                                        (pure L4 passthrough)
                                                            |
                                              (private link / server-to-server WireGuard)
                                                            v
                                                     origin broker (real IP, never exposed)
```

Why this shape:

- **Pure L4 TCP passthrough — do NOT terminate+re-encrypt at the relay.** The
  TLS session stays end-to-end device ↔ origin; the VPS forwards encrypted
  bytes blind and never holds a key. So the origin's cert CN/SAN is unchanged
  (no new cert), the modem's TLS validation is unchanged, and there is **zero
  firmware change** beyond the host the modem dials.
- **The origin IP never touches the device or the wire** — only the relay's
  disposable IP does.
- **Point the firmware at a hostname, not a hardcoded IP** (DDNS-style) so the
  relay VPS can be rotated/replaced without ever reflashing.

`nginx`:

```nginx
stream {
  server { listen 8883; proxy_pass ORIGIN_HOST:8883; }
}
```

Residual risks (accepted, documented):

- The relay's own IP becomes the new thing to protect — one hop removed. Keep
  the box a dumb pipe only (nothing else on it), rate-limit / fail2ban the
  stream port, and lean on **MQTT broker auth + a TLS client cert** rather than
  source-IP allowlisting (cellular CGNAT makes IP allowlists impractical).
- The relay is a new single point of failure and a box to patch. The L4-only
  surface keeps that minimal.

### What does NOT work (so future work doesn't re-investigate it)

- **WireGuard / any VPN client on the ESP32 device: blocked for this stack.**
  The A7682E modem via TinyGSM uses modem-native AT-command sockets that bypass
  the ESP32's lwIP stack entirely, so there is **no lwIP netif for a WireGuard
  tunnel to bind to**. Both maintained ports (`trombik/esp_wireguard`,
  `ciniml/WireGuard-ESP32-Arduino`) work only by riding an existing WiFi/Ethernet
  netif; over a TinyGSM cellular link they crash or no-op (confirmed by users on
  the same A76xx modem family). A working version would require dropping TinyGSM
  for `esp_modem` + PPPoS, moving to ESP-IDF 5.x / arduino-esp32 core 3.x (our
  pin is core 2.x / Bluedroid), and hand-patching an alpha library — a
  multi-week firmware R&D track, not a library add. Do server-to-server
  WireGuard on the *Linux* relay↔origin hop instead, where it is trivial.
- **Cloudflare Tunnel free tier: no.** Raw MQTT over TCP needs Spectrum (paid)
  plus a client agent that cannot run on ESP32 firmware. The MQTT-over-WebSockets
  workaround would move all logic off the modem's on-chip MQTT onto three stacked
  ESP32 libraries (TLS client + WS framing + MQTT) that no known project has
  combined over real cellular. Not worth it versus the jump-box relay.
- **shadowsocks / SNI domain fronting: not applicable.** No ESP32 shadowsocks
  port exists (it targets embedded Linux). Domain fronting is dead — the major
  CDNs blocked it years ago.

## Status summary

- Bridge-to-bridge federation over LAN and Tailscale: **shipped.**
- WiFi/TCP device-link (host side): **shipped**; firmware client: **pending.**
- Cellular MQTT transport + the jump-box relay topology: **designed here,
  implemented as part of the T-Deck cellular phase** (PORT_SPEC §A). The relay
  itself is standard Linux plumbing you can stand up independently of the
  firmware work.
