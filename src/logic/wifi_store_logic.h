#pragma once
#include <stdint.h>
#include <string.h>
#include <stddef.h>

// Multi-network WiFi credential list — the pure in-memory model. No Arduino
// / NVS / filesystem dependency, so it unit-tests on the native host
// (test/test_wifi_store). Persistence (LittleFS /wifi.dat) and the join
// logic live in src/wifi_store.{h,cpp}; the protocol glue in proto_parse.h
// talks to the store through the protoWifi* hooks.
//
// Semantics:
//   - upsert dedups by exact ssid bytes: replace updates the pass IN PLACE
//     (position kept), a new ssid appends at the tail.
//   - at capacity a NEW ssid is rejected (WIFI_LIST_FULL) — networks a user
//     stored must never silently drop, so there is deliberately no LRU
//     eviction. Replacing an existing ssid still works at cap.
//   - remove compacts the tail down one slot; relative order is preserved.
//   - limits are the 802.11 maxima: ssid 1..32 bytes, WPA2-PSK pass 0..63
//     (empty = open network). Violations return WIFI_LIST_ERR untouched.
//
// The pass is a secret: nothing in this header ever formats it — the only
// output helper (wifiListFormatSsids) emits names alone, and callers keep
// that property (list acks, status, debug, logs).

// Soft cap, bounds RAM and scan-match time: 256 x 97 B ~= 25 KB, heap- or
// PSRAM-backed on the device (see wifi_store.cpp), trivial on the host.
#define WIFI_MAX 256

enum : int16_t {
  WIFI_LIST_ERR  = -1,   // bad input / absent ssid / store failure
  WIFI_LIST_FULL = -2,   // at capacity and ssid is new — nothing evicted
};

struct WifiCred {
  char ssid[33];   // 32 + NUL
  char pass[64];   // 63 + NUL; "" = open network
};

// Caller-provided backing array — the device store owns a WIFI_MAX-sized
// one; tests use whatever cap exercises the edge under test.
struct WifiList {
  WifiCred* c;
  uint16_t  n;
  uint16_t  cap;
};

inline int16_t wifiListFind(const WifiList* l, const char* ssid) {
  if (!l || !l->c || !ssid) return -1;
  for (uint16_t i = 0; i < l->n; i++)
    if (strcmp(l->c[i].ssid, ssid) == 0) return (int16_t)i;
  return -1;
}

// Returns the new count, WIFI_LIST_FULL at cap (new ssid), or WIFI_LIST_ERR
// on invalid input. NULL pass is accepted as "" (open network).
inline int16_t wifiListUpsert(WifiList* l, const char* ssid, const char* pass) {
  if (!l || !l->c || !ssid) return WIFI_LIST_ERR;
  size_t sl = strlen(ssid);
  if (sl < 1 || sl > 32) return WIFI_LIST_ERR;
  if (!pass) pass = "";
  if (strlen(pass) > 63) return WIFI_LIST_ERR;

  int16_t at = wifiListFind(l, ssid);
  if (at < 0) {
    if (l->n >= l->cap) return WIFI_LIST_FULL;
    at = (int16_t)l->n++;
  }
  // memset first so records are deterministic bytes end to end — the device
  // store writes whole 97-byte records to flash and must not embed heap
  // junk (or a prior occupant's pass) past the NULs.
  WifiCred& cr = l->c[at];
  memset(&cr, 0, sizeof(cr));
  memcpy(cr.ssid, ssid, sl);
  memcpy(cr.pass, pass, strlen(pass));
  return (int16_t)l->n;
}

// Returns the new count, or WIFI_LIST_ERR when the ssid isn't stored.
inline int16_t wifiListRemove(WifiList* l, const char* ssid) {
  int16_t at = wifiListFind(l, ssid);
  if (at < 0) return WIFI_LIST_ERR;
  for (uint16_t i = (uint16_t)at; i + 1 < l->n; i++) l->c[i] = l->c[i + 1];
  l->n--;
  memset(&l->c[l->n], 0, sizeof(WifiCred));   // scrub the vacated tail slot
  return (int16_t)l->n;
}

inline void wifiListClear(WifiList* l) {
  if (!l) return;
  if (l->c && l->n) memset(l->c, 0, (size_t)l->n * sizeof(WifiCred));
  l->n = 0;
}

// Formats stored NAMES as comma-separated JSON string literals: "a","b"
// (no enclosing brackets — the ack builder wraps). Emits as many whole
// ssids as fit outLen; returns how many were emitted so the caller can flag
// truncation against the true count. The pass is never touched. Quotes and
// backslashes are JSON-escaped; control bytes become '?' so the emitted
// line stays parseable.
inline uint16_t wifiListFormatSsids(const WifiList* l, char* out, size_t outLen) {
  if (!out || outLen == 0) return 0;
  out[0] = 0;
  if (!l || !l->c) return 0;
  size_t o = 0;
  uint16_t emitted = 0;
  for (uint16_t i = 0; i < l->n; i++) {
    char esc[68];   // 32 bytes worst-case escape to 64 + quotes headroom
    size_t e = 0;
    for (const char* p = l->c[i].ssid; *p && e < sizeof(esc) - 2; p++) {
      unsigned char ch = (unsigned char)*p;
      if (ch == '"' || ch == '\\') { esc[e++] = '\\'; esc[e++] = (char)ch; }
      else if (ch < 0x20)          { esc[e++] = '?'; }
      else                         { esc[e++] = (char)ch; }
    }
    esc[e] = 0;
    size_t need = e + 2 + (emitted ? 1 : 0);   // quotes + comma
    if (o + need >= outLen) break;             // whole names only
    if (emitted) out[o++] = ',';
    out[o++] = '"';
    memcpy(out + o, esc, e); o += e;
    out[o++] = '"';
    out[o] = 0;
    emitted++;
  }
  return emitted;
}
