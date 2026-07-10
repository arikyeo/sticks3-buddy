# Hardware Buddy BLE Protocol v2

This document covers the **v2 additions** on top of the v1 wire protocol.
v1 — the Nordic UART Service transport, heartbeat snapshot, permission
decisions, folder push, and pairing/security model — is unchanged and
documented in [REFERENCE.md](REFERENCE.md). Read that first; this file
doesn't repeat it.

v2 is an **optional-field superset** of v1: every v1 message still parses
the same way, and every v2 field is optional. A v1-only device that never
sends `hello` just keeps working exactly as REFERENCE.md describes.

## Hello handshake

A v2-capable host opens the connection by sending `hello` before anything
else:

```json
{
  "cmd": "hello",
  "proto": 2,
  "host": { "id": "...", "name": "...", "app": "...", "ver": "..." },
  "caps": ["sessions", "ask", "rxack", "cancel"]
}
```

The device acks with its own capability set:

```json
{
  "ack": "hello",
  "ok": true,
  "proto": 2,
  "data": {
    "fw": "...",
    "board": "...",
    "name": "...",
    "caps": ["sessions", "ask", "rxack"],
    "maxSessions": 6,
    "maxLine": 4608,
    "sel": true
  }
}
```

The **effective protocol** for the rest of the connection is
`min(host.proto, device.proto)`. Effective capabilities are the
intersection of `host.caps` and `data.caps` — a host shouldn't send a
`sessions` heartbeat if the device didn't list `sessions` in its ack, and
a device shouldn't expect `rxack` if the host didn't list it either.

### `sel` and host switching

`data.sel` (selected) tells the host whether the device wants this
connection to be the active one:

- **`sel: true`** — normal case, proceed.
- **`sel: false`** — the device is already paired to a different active
  host (soft-pin). The device disconnects this link after ~1 second. The
  losing host should back off and slow-retry (e.g. every 60s) rather than
  reconnect-spamming a device that's actively pinned elsewhere.

Host switching is entirely device-side: the device decides which host is
"selected" and disconnects the others. There's no separate handoff
message.

### No-ack fallback

If the host doesn't get a `hello` ack within **2 seconds**, treat the
device as v1-only: drop back to v1 framing and the v1 900-byte line cap
(see below). Don't retry `hello` on the same connection — a device that
didn't understand `hello` in 2s isn't going to understand a resend.

### Compatibility rules (hard requirements)

- **Nothing non-v1 may appear on the wire before `hello` completes.** A
  device seeing an unrecognized field pre-handshake should ignore it per
  the v1 "ignore unknown fields" rule, not choke on it — but a
  well-behaved host doesn't test that path on purpose.
- **Every v2 field is optional.** No v2 message adds a required field to
  an existing v1 message shape; v2 only adds new optional fields and new
  message types gated behind capability negotiation.
- **Line size.** v1 mode caps lines at ≤900 bytes. Once `hello` succeeds,
  the negotiated `maxLine` (device-advertised, e.g. 4608) applies instead.
- **Heartbeat timeout is 30 seconds.** This supersedes the v1 ~30s
  no-snapshot-means-dead guidance for v2 connections — same number, now
  explicit as a v2 contract rather than an implied one.

## Sessions (optional)

If both sides negotiated the `sessions` capability, the heartbeat can
carry a `sessions` array alongside (not instead of) the v1 aggregate
fields:

```json
{
  "total": 3,
  "running": 1,
  "waiting": 1,
  "sessions": [
    {
      "sid": "cli:1",
      "agent": "claude",
      "title": "...",
      "state": "wait",
      "tok": 18420,
      "last": "..."
    }
  ]
}
```

| Field   | Meaning                                        |
| ------- | ----------------------------------------------- |
| `sid`   | Session identifier, stable for the session's life |
| `agent` | `"claude"` or `"codex"`                          |
| `title` | Short human-readable label for the session       |
| `state` | `"run"` \| `"wait"` \| `"idle"` \| `"done"`      |
| `tok`   | Token count for this session                     |
| `last`  | Last activity line, for a small display          |

Ordering is **waiting-first**: sessions with `state: "wait"` sort ahead of
`run`/`idle`/`done` so a device with a small list slot always shows a
pending approval if one exists.

The device clamps the list to its advertised `maxSessions` — a host
shouldn't need to pre-truncate, but shouldn't assume the device rendered
everything it sent either.

**The v1 aggregate fields (`total`, `running`, `waiting`) stay
authoritative** even when `sessions` is present. A device that doesn't
understand `sessions` (or ignores it) still gets a correct picture from
the aggregates alone. `sessions` is a richer view of the same state, not
a replacement source of truth.

## Prompts

A prompt (permission request) may now carry an optional `sid` (which
session it belongs to) and `qn` (question index, for multi-question
flows):

```json
{
  "id": "req_abc123",
  "sid": "cli:1",
  "qn": 0,
  "tool": "Bash",
  "hint": "rm -rf /tmp/foo"
}
```

`id` is unchanged from v1: a globally-unique wire id, ≤39 ASCII
characters. The decision reply format is **unchanged from v1**:

```json
{"cmd":"permission","id":"req_abc123","decision":"once"}
{"cmd":"permission","id":"req_abc123","decision":"deny"}
```

### Cancel (optional, `cancel` capability)

If a prompt becomes stale before the device answers (e.g. the CLI
timed out or the user answered from another surface), the host can
cancel it:

```json
{"cmd":"prompt_cancel","id":"req_abc123"}
```

The device should clear that prompt from its UI without expecting an ack
distinct from the usual `{"ack":"prompt_cancel","ok":true}`.

## Ask (optional, `ask` capability)

`ask` is a **read-only** structured question — the device displays it but
does not answer it on v2.0. It's for surfacing multi-choice questions
(e.g. "which of these 3 options?") in a richer form than a single
approve/deny prompt:

```json
{
  "evt": "ask",
  "id": "ask_xyz789",
  "sid": "cli:1",
  "multiSelect": false,
  "questions": [
    {
      "header": "Which approach?",
      "text": "Pick one:",
      "options": [
        { "label": "Option A", "desc": "..." },
        { "label": "Option B", "desc": "..." }
      ]
    }
  ]
}
```

`ask_cancel` works the same way as `prompt_cancel`:

```json
{"evt":"ask_cancel","id":"ask_xyz789"}
```

No on-device answering path exists in v2.0 — this is display-only. A
future protocol revision may add an answer message; don't build a device
that assumes silence means "answer coming later."

## rxack (optional, `rxack` capability)

Post-hello only. The device can periodically report how many bytes it's
received in total, so the host can detect a stalled link even when
nothing else is being sent:

```json
{"ack":"rx","n":<total>}
```

**Dead-link threshold:** if the host has sent bytes but gotten no
`rxack` (or any other traffic) for **>20 seconds**, treat the send side
as dead and reconnect. This is distinct from the 30s heartbeat timeout
above — heartbeat timeout covers "device stopped talking," this covers
"device stopped listening."

## Time sync

Unchanged in spirit from v1's one-shot-on-connect `time` message, but
v2 hosts should also **resend it once daily** (the device has no RTC, so
its clock drifts and a day boundary is exactly where `tokens_today`-style
resets would silently use the wrong day). Same format:

```json
{ "time": [1775731234, -25200] }
```

## Optional capabilities (extras)

These are additive, off by default, and only relevant if the device
advertised the matching capability in its `hello` ack.

### `play`

Reserved for device-side audio/haptic playback triggers. No fixed schema
yet — treat presence in `data.caps` as "ask before assuming," not as a
ready-to-use message type.

### `ntfy`

Push a notification card to the device for something outside the normal
session/prompt flow — a CI result, a GitHub event, a weather update:

```json
{
  "evt": "ntfy",
  "kind": "gh",
  "title": "PR #42 merged",
  "body": "sticks3-buddy: track-i landed",
  "ts": 1775731234
}
```

`kind` is a short tag (`"gh"`, `"ci"`, `"weather"`, ...) the device can
use to pick an icon or sound; treat unknown `kind` values as generic.

## Summary table

| Concept              | v1                              | v2 addition                                  |
| --------------------- | -------------------------------- | --------------------------------------------- |
| Handshake             | none — device just gets a stream | `hello` / `ack:"hello"`, capability negotiation |
| Line cap              | ≤900 bytes always                | negotiated `maxLine` post-hello               |
| Session detail        | aggregate counts only            | optional `sessions[]`, waiting-first          |
| Prompt targeting      | flat `id`                        | optional `sid`, `qn`                          |
| Prompt lifecycle      | fire-and-forget                  | optional `prompt_cancel`                      |
| Structured questions  | not supported                    | optional `ask` / `ask_cancel` (read-only)     |
| Link liveness         | 30s no-snapshot = dead           | + `rxack` for send-side dead detection (20s)  |
| Clock                 | one-shot `time` on connect       | + daily resync                                |
| Notifications         | not supported                    | optional `ntfy` cards                         |

## Clarifications (locked during first host+firmware implementation)

Resolved ambiguities — both sides are built to these; treat as normative:

1. **`prompt.qn`** = count of prompts queued *behind* the displayed one for the same session, capped at 3. `0`/absent when nothing queued. Devices render `+N queued`; it is **not** a question index.
2. **`ack.data.sel` absent** → hosts assume `true`. Devices should still send it explicitly.
3. **`ack.ok:false`** = handshake declined; the host stays in v1 mode for the connection.
4. **Ack value clamps**: hosts clamp `maxLine` to `[256,16384]` and `maxSessions` to `[1,32]`; if `data` is missing entirely, hosts fall back to 900-byte v1 budget. Devices must always populate `data.maxLine` and `data.maxSessions`.
5. **`sessions[].last`**: hosts cap at 64 sanitized bytes and may send `""`. Devices may truncate further on ingest.
6. **`ntfy` capability is device-advertised only**: hosts do not list `ntfy` in hello caps and gate card sends purely on the device ack caps. Devices must not require `ntfy` in the host's hello.
7. **Decision vocabulary**: devices send `"once"`/`"deny"` (v1 vocabulary). Hosts additionally accept `"allow"` as a synonym for `"once"`.
8. **`state:"done"`** is defined but not currently emitted by the reference host (sessions drop on end); devices must still tolerate it.
9. **`rxack.n`** is advisory; hosts treat any rx-ack as liveness and do not verify the count.
10. **`prompt.sid`/`prompt.qn` are proto-gated, not cap-gated**: hosts send them whenever negotiated proto ≥ 2, even if `sessions` wasn't negotiated. Standard unknown-field tolerance applies.

## Firmware implementation notes (reference device)

Decisions the reference firmware (this repo) made where the spec above is
silent or loose — pinned here so hosts don't have to reverse-engineer them.

- **`prompt.qn` semantics** (host+firmware agreed): the count of prompts
  *queued behind* the shown one for the same session, capped at 3; `0` or
  absent means it's the only one. It is not a question index. The device
  renders it as a `+Nq` badge.
- **rxack `n` counts bytes**, per the wording above: total bytes received
  on that transport (since connect for BLE, since boot for USB serial),
  reported after each received line. Hosts must not assert a specific
  meaning — any rx ack is proof of liveness.
- **Capability gating is asymmetric by design.** The device only *sends*
  v2 traffic (rx acks) if the host's `hello` caps asked for it, but it
  *accepts* v2 messages (`sessions[]`, `ask`, `ntfy`, `prompt_cancel`)
  post-hello without re-checking the host's claimed caps — the host gates
  its own sends on the device's ack caps. `ntfy` in particular never
  appears in a host's hello caps.
- **Pre-hello, unknown `cmd`s are swallowed silently** (no ack) — that is
  long-standing v1 firmware behavior and exactly what `prompt_cancel` or
  other v2 commands hit if sent before `hello`. `hello` itself is always
  answered.
- **`ask` / `ask_cancel` / `ntfy` are events, not commands: no acks.**
  "Works the same way as `prompt_cancel`" means clear-by-id, not the ack.
  Only the first entry of `questions[]` is displayed, first 4 options.
- **`sel:false` timing**: the losing link gets the `sel:false` ack first,
  then the disconnect ~0.3s later (well inside the ~1s the spec promises).
  The pinned-host decision is made per connection; a pairing window
  (explicit user action on the device) suspends it.
- **Pairing security**: outside a device-initiated 60s pairing window, a
  connection from an unbonded peer is dropped before pairing can start
  (drive-by-bonding guard). Exception: a device with zero stored bonds is
  open, preserving the out-of-box flow. NUS characteristics stay
  encrypted-only in both pairing modes (passkey and just-works).
- **Replies are dual-written** to USB serial and BLE, like every v1 ack —
  a host may see acks for the other transport's traffic and must ignore
  unknown/unexpected acks (the v1 tolerance rule, applied to acks).
