#pragma once
#include <stdint.h>
#include <stddef.h>

// Nordic UART Service-compatible BLE bridge. Clients (browser Web
// Bluetooth, noble, etc.) subscribe to NUS to talk to the Stick exactly
// like a serial port.
//
// Service UUID  6e400001-b5a3-f393-e0a9-e50e24dcca9e
// RX char       6e400002-b5a3-f393-e0a9-e50e24dcca9e   (client → stick, WRITE)
// TX char       6e400003-b5a3-f393-e0a9-e50e24dcca9e   (stick → client, NOTIFY)
//
// Writes from the client are line-buffered and dispatched through the
// same protoApplyJson path that USB serial uses. Replies (acks, status
// snapshots) are written via bleWrite() and chunked to the negotiated MTU.

// pairMode: 0 = LE Secure Connections passkey (MITM, DisplayOnly — default),
// 1 = LE Secure Connections just-works (no passkey). Both bond and encrypt;
// the NUS characteristics stay encrypted-only either way. Persisted as NVS
// "s_pair" and applied here only, so changing it prompts a reboot.
void bleInit(const char* deviceName, uint8_t pairMode = 0);
bool bleConnected();
// True once LE Secure Connections bonding has completed for the current
// link. The NUS characteristics are encrypted-only, so in practice this
// is always true by the time any data flows; exposed so the status ack
// can report it to the desktop.
bool bleSecure();
// Non-zero while a 6-digit pairing passkey should be on screen. main.cpp
// renders it; cleared automatically on auth complete or disconnect.
uint32_t blePasskey();
// Remove only the bond for the currently connected peer. Called from the
// "unpair" JSON command so a desktop can unpair itself without affecting
// bonds held by other hosts.
void bleRemoveCurrentBond();
// Erase ALL stored bonds from NVS. Called only from factory reset and the
// "forget all hosts" menu action.
void bleClearBonds();

// --- multi-host (Track F P4) --------------------------------------------------
// Connection gate (decision table: logic/pairing_gate_logic.h).
// Normal mode — anti drive-by-bonding: a connection from a peer with no
// stored bond is dropped in onConnect, before pairing can start. Exception:
// a device with zero bonds stays open so the first out-of-box pairing needs
// no menu trip.
// Pairing window — exclusive courting of NEW devices: opening it evicts the
// currently connected host (bonded hosts auto-reconnect after the window)
// and, while it is open, the gate inverts — bonded peers are dropped on
// connect and only unbonded peers may proceed to SMP pairing. The window is
// RAM-only state: a reboot always lands in normal policy.
void bleAllowPairing(bool open, uint32_t windowMs);
uint32_t blePairingRemainingMs();   // 0 = window closed
// Pairing-window housekeeping; call once per main loop() pass. Handles the
// natural-expiry close and the post-eviction advertising fallback (start
// advertising 500ms after window-open eviction if the peer's disconnect
// event never landed).
void blePairingTick(uint32_t nowMs);
// BD address of the currently connected peer (identity address once the
// stack resolved it). False when nothing is connected.
bool blePeerBda(uint8_t out[6]);
// Drop the current link. Advertising restarts automatically.
void bleDisconnect();
#ifdef BUDDY_BLE_WHITELIST
// Optional (compile-flag, default OFF): restrict connects to one whitelisted
// address while pinned. RPA risk R3 — soft-pin is the guaranteed path.
void bleWhitelistPin(const uint8_t* bda);   // nullptr = open advertising
#endif

// Renegotiate the BLE connection interval (Track F P7 power). relaxed=true →
// long interval + slave latency (screen-off idle: the radio sleeps through
// most connection events, prompt delivery lags up to ~1s); relaxed=false →
// short interval, no latency (awake: snappy prompt + transcript delivery).
// The requested mode is latched, so a peer that connects while the device is
// already idle gets the relaxed parameters immediately. No-op on the wire
// until a peer is connected. Call on screen on/off transitions.
void bleConnParams(bool relaxed);

size_t bleAvailable();
int bleRead();
size_t bleWrite(const uint8_t* data, size_t len);
