# Deferred theoretical findings (KIV)

Findings judged theoretical / low-likelihood / design-residual during review.
Not fixed inline by policy — each entry records why it is inert and what would
make it real. Re-open only if the "what would make it real" column starts
happening.

## 2026-07-11 — codex adversarial review of fix/host-identity-bda (iter 2 sweep)

### Stray `onMtuChanged` can set the accepted link's `mtu` from a dying link
- **Where:** `src/ble_bridge.cpp` `ServerCallbacks::onMtuChanged` — writes the
  global `mtu` without attributing the event to the accepted link (the event
  carries a conn_id, not a peer address).
- **Why deferred:** pre-existing behavior, not introduced by the identity-bda
  diff. Requires a gate-rejected link to complete an MTU exchange in the
  stalled-teardown window while the accepted peer is connected; the accepted
  peer's own MTU exchange then overwrites the value moments later. Worst case
  is a transiently wrong notify chunk size (capped 20..180 either way).
- **What would make it real:** field logs showing truncated/oversized notify
  chunks right after a pairing-window eviction, or a peer that never performs
  its own MTU exchange.

### Candidate address arrays are guarded by volatile flags, not volatile themselves
- **Where:** `src/ble_bridge.cpp` — `pidAddr`/`authAddr` (and the pre-existing
  `peerAddr`) are plain arrays written on the BLE stack task and read from the
  main loop, gated by `volatile` bool flags written after the data.
- **Why deferred:** this is the codebase's established idiom (`peerAddr` +
  `hasPeer` shipped in every upstream fork). Volatile-qualifying the arrays
  changes nothing real — every access casts through `memcpy`, and `volatile`
  orders only volatile accesses among themselves. The writes land nanoseconds
  after the flag's producer-side sequence point; the consumer reads on a main
  loop pass milliseconds later, and ESP32 Xtensa cores retire stores to
  coherent memory in order. A genuine fix would be a portMUX critical section
  (rxBuf's pattern), which is heavier than the risk warrants here.
- **What would make it real:** field evidence of a torn candidate address
  (bond-key resolution logging an address that matches neither the link nor
  any bond entry), or a port to a core with weaker store ordering.

### Auth-failure disconnect races a same-instant connection swap
- **Where:** `src/ble_bridge.cpp` `onAuthenticationComplete` failure path —
  `server->disconnect(server->getConnId())` targets the newest connection; a
  connection swap between the (now bda-attributed) failure event and the
  disconnect call could drop a just-accepted successor link.
- **Why deferred:** needs the accepted link to fail auth AND be replaced by a
  new accepted link within the same few-ms callback window; a wrongly dropped
  bonded host auto-reconnects. The bda attribution added in this branch
  already removes the reachable variant (stale peer's failure dropping the
  live link).
- **What would make it real:** an API surface exposing per-event conn_id for
  auth events (then attribute the disconnect by conn_id instead), or field
  logs of hosts being dropped immediately after a successful swap.
