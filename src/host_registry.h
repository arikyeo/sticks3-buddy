#pragma once
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include "config.h"
#include "logic/utf8_text_logic.h"

// Multi-host registry (Track F P4). The pure table half lives here so it
// unit-tests on the native host (test_hostreg); everything that touches NVS
// or the Bluedroid bond store lives in host_registry.cpp behind the
// hostGlue* API declared at the bottom.
//
// One record per bonded desktop host, keyed by BD address. The registry is
// reconciled against the Bluedroid bond store at boot: bonds we don't know
// get adopted as "Host-N", records whose bond vanished get dropped. The
// array is kept compact (entries 0..count-1 used); a pinned-slot reference
// is remapped through hostTabRemapPin() whenever a drop shifts indices.

static const uint8_t HOST_F_USED = 0x01;

struct HostRec {
  uint8_t  bda[6];
  char     name[24];   // hello host.name (sanitized) or "Host-N" placeholder
  char     app[12];    // hello host.app (sanitized)
  char     hostId[9];  // hello host.id, verbatim (8 chars + NUL)
  uint32_t lastSeen;   // monotonic HostTable::seq stamp — NOT wall time, so
                       // LRU ordering survives reboots without a valid clock
  uint8_t  flags;      // HOST_F_*
};

struct HostTable {
  HostRec  h[BUDDY_MAX_HOSTS];
  uint8_t  count;      // entries 0..count-1 are used (kept compact)
  uint32_t seq;        // monotonic LRU counter, persisted with the table
};

inline void hostTabInit(HostTable* t) { memset(t, 0, sizeof(*t)); }

inline int hostTabFindBda(const HostTable* t, const uint8_t bda[6]) {
  for (uint8_t i = 0; i < t->count; i++)
    if (memcmp(t->h[i].bda, bda, 6) == 0) return i;
  return -1;
}

inline void hostTabTouch(HostTable* t, int idx) {
  if (idx < 0 || idx >= (int)t->count) return;
  t->h[idx].lastSeen = ++t->seq;
}

// Adopt an unknown (but bonded) peer as "Host-N". Returns the slot, or -1
// when the table is full — the caller evicts via hostTabLru() first.
inline int hostTabAdopt(HostTable* t, const uint8_t bda[6]) {
  if (t->count >= BUDDY_MAX_HOSTS) return -1;
  int i = t->count++;
  HostRec& r = t->h[i];
  memset(&r, 0, sizeof(r));
  memcpy(r.bda, bda, 6);
  snprintf(r.name, sizeof(r.name), "Host-%d", i + 1);
  r.flags = HOST_F_USED;
  r.lastSeen = ++t->seq;
  return i;
}

// Fold a hello handshake into an existing record. Display strings are
// sanitized here (they come straight off the wire); hostId is an opaque
// token and copied verbatim. Empty fields leave the stored value alone.
inline void hostTabHello(HostTable* t, int idx, const char* hostId,
                         const char* name, const char* app) {
  if (idx < 0 || idx >= (int)t->count) return;
  HostRec& r = t->h[idx];
  if (hostId && hostId[0]) {
    strncpy(r.hostId, hostId, sizeof(r.hostId) - 1);
    r.hostId[sizeof(r.hostId) - 1] = 0;
  }
  if (name && name[0]) utf8Sanitize(r.name, sizeof(r.name), name);
  if (app && app[0])   utf8Sanitize(r.app, sizeof(r.app), app);
  r.lastSeen = ++t->seq;
}

// Route a hello to the record owning this link's bda. NEVER trust a cached
// slot index across link churn: during the pairing window the evicted
// host's gate-rejected reconnect retries can tear links down between polls,
// and a stale slot writes the NEW host's hello into the OLD host's record
// (live 2026-07-11: the hosts list showed only the newly paired Mac — its
// hello had renamed the Windows record in place). A hello only arrives on
// an encrypted link, so an unknown bda here is a bonded peer whose adopt
// was missed — adopt it now. Full table: no write, returns -1 (boot
// reconcile owns capacity truth; a hello never evicts).
inline int hostTabHelloRoute(HostTable* t, const uint8_t bda[6],
                             const char* hostId, const char* name,
                             const char* app) {
  int idx = hostTabFindBda(t, bda);
  if (idx < 0) idx = hostTabAdopt(t, bda);
  if (idx >= 0) hostTabHello(t, idx, hostId, name, app);
  return idx;
}

// Re-key a record to a new BD address, preserving every other field (name,
// hostId, app, LRU stamp). Needed because a privacy-enabled host's FIRST
// pairing connects under an unresolvable RPA: a record adopted by that
// connect-time address matches nothing once the SMP exchange establishes
// the identity address the bond store keys by — the boot reconcile would
// drop it (hello name lost), a same-session identity-resolved reconnect
// would duplicate it, and evict/forget would feed the bond store a key it
// never heard of. Refuses (returns false) when idx is invalid or a
// DIFFERENT record already owns newBda — a duplicate key would make
// hostTabFindBda ambiguous. Re-keying a record to its own address is a
// successful no-op.
inline bool hostTabRekey(HostTable* t, int idx, const uint8_t newBda[6]) {
  if (idx < 0 || idx >= (int)t->count) return false;
  int owner = hostTabFindBda(t, newBda);
  if (owner >= 0 && owner != idx) return false;
  memcpy(t->h[idx].bda, newBda, 6);
  return true;
}

// Least-recently-seen used slot; -1 when the table is empty.
inline int hostTabLru(const HostTable* t) {
  int best = -1;
  uint32_t bestSeen = 0xFFFFFFFFu;
  for (uint8_t i = 0; i < t->count; i++)
    if (t->h[i].lastSeen < bestSeen) { bestSeen = t->h[i].lastSeen; best = i; }
  return best;
}

// Drop a slot, compacting the array. Returns the new count.
inline int hostTabDrop(HostTable* t, int idx) {
  if (idx < 0 || idx >= (int)t->count) return t->count;
  for (int i = idx; i + 1 < (int)t->count; i++) t->h[i] = t->h[i + 1];
  t->count--;
  memset(&t->h[t->count], 0, sizeof(HostRec));
  return t->count;
}

// Slot indices shift on drop; keep a pinned-slot reference valid. Returns
// the new pin (-1 = auto when the pinned entry itself was dropped).
inline int8_t hostTabRemapPin(int8_t pin, int droppedIdx) {
  if (pin < 0) return -1;
  if (pin == droppedIdx) return -1;
  return pin > droppedIdx ? (int8_t)(pin - 1) : pin;
}

#if !defined(BUDDY_NATIVE_TEST)
// ---- device glue, implemented in host_registry.cpp --------------------------
// Owns the persistent registry (NVS namespace "hosts"), the bond-store
// reconcile, and the connection policy engine (soft-pin, pairing window,
// pinned-host fallback). main.cpp persists hostsel/hostfb in settings and
// mirrors them in via hostGlueInit()/hostGlueSetPin().
void        hostGlueInit(int8_t pinnedSlot, uint8_t fallbackAuto);
void        hostGluePoll(uint32_t nowMs);
// Registry update from a hello handshake; returns whether this link is the
// selected one (drives the hello ack's data.sel).
bool        hostGlueOnHello(const char* hostId, const char* name,
                            const char* app, bool viaBle);
void        hostGlueSetPin(int8_t slot);    // -1 = auto; kicks a mismatched link
int8_t      hostGluePin();
// 60s new-host pairing window: evicts the current link, rejects bonded
// peers while open (ble_bridge gate), first new bond ends it early.
void        hostGluePairWindow(bool open);
// One-shot poll for the "paired: <name>" toast; true = out was filled.
bool        hostGlueTakePaired(char* out, size_t cap);
void        hostGlueForget(uint8_t slot);   // remove bond + registry entry
void        hostGlueForgetAll();            // bonds + "hosts" namespace wipe
uint8_t     hostGlueCount();
const HostRec* hostGlueGet(uint8_t slot);
int         hostGlueConnectedSlot();        // -1 when not BLE-connected/known
const char* hostGlueActiveName();           // BLE registry/hello name, else USB hello name; ""
#endif  // !BUDDY_NATIVE_TEST
