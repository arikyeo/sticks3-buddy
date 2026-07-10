#pragma once
#include <stdint.h>
#include <string.h>
#include "config.h"

// Protocol v2 session table (PROTOCOL_V2.md "Sessions"). Pure logic — no
// Arduino dependency — so it unit-tests on the native host (test_session).
//
// The host sends the full session list in each heartbeat, waiting-first.
// We ingest it order-preserving, clamped to BUDDY_MAX_SESSIONS. Selection
// and focus-pin are stored by sid (not index) so they survive reorders:
// the host resorting the list must not silently move the cursor onto a
// different session.

enum : uint8_t { SES_IDLE = 0, SES_RUN = 1, SES_WAIT = 2, SES_DONE = 3 };

struct Session {
  char     sid[12];    // opaque session id, copied verbatim (like promptId)
  char     agent[8];   // "claude" / "codex"
  char     title[25];
  uint8_t  state;      // SES_*
  uint32_t tok;
  char     last[65];   // last activity line; host caps at 64 bytes sanitized
                       // (bridge-track decision), may be "" for codex sessions
};

struct SessionTable {
  Session s[BUDDY_MAX_SESSIONS];
  uint8_t count;
  char    selSid[12];  // UI selection cursor, by sid; "" = default (row 0)
  char    pinSid[12];  // focus pin, by sid; "" = unpinned. Retained even when
                       // the sid is momentarily absent (host clamp/restart) —
                       // lookups return -1 and the pin self-heals on return.
};

inline void sessionTabClear(SessionTable* t) {
  memset(t->s, 0, sizeof(t->s));
  t->count = 0;
  // selection + pin intentionally survive a clear (reconnect to same host)
}

inline void sessionTabReset(SessionTable* t) { memset(t, 0, sizeof(*t)); }

inline uint8_t sessionStateFromStr(const char* s) {
  if (!s) return SES_IDLE;
  if (strcmp(s, "run") == 0)  return SES_RUN;
  if (strcmp(s, "wait") == 0) return SES_WAIT;
  if (strcmp(s, "done") == 0) return SES_DONE;
  return SES_IDLE;
}

// Replace the table contents with the freshly parsed list (already clamped
// by the caller to BUDDY_MAX_SESSIONS; order preserved — spec sends
// waiting-first and we must not re-sort).
inline void sessionTabCommit(SessionTable* t, const Session* in, uint8_t n) {
  if (n > BUDDY_MAX_SESSIONS) n = BUDDY_MAX_SESSIONS;
  for (uint8_t i = 0; i < n; i++) t->s[i] = in[i];
  for (uint8_t i = n; i < BUDDY_MAX_SESSIONS; i++) memset(&t->s[i], 0, sizeof(Session));
  t->count = n;
}

inline int sessionTabFindSid(const SessionTable* t, const char* sid) {
  if (!sid || !sid[0]) return -1;
  for (uint8_t i = 0; i < t->count; i++)
    if (strcmp(t->s[i].sid, sid) == 0) return i;
  return -1;
}

inline const Session* sessionTabBySid(const SessionTable* t, const char* sid) {
  int i = sessionTabFindSid(t, sid);
  return i >= 0 ? &t->s[i] : (const Session*)0;
}

// Current selection as an index: sid match wins; empty/stale sid falls back
// to row 0 (or -1 when the table is empty).
inline int sessionTabSelIdx(const SessionTable* t) {
  if (t->count == 0) return -1;
  int i = sessionTabFindSid(t, t->selSid);
  return i >= 0 ? i : 0;
}

inline int sessionTabPinIdx(const SessionTable* t) {
  return sessionTabFindSid(t, t->pinSid);
}

inline void sessionTabSelectNext(SessionTable* t) {
  if (t->count == 0) return;
  int cur = sessionTabSelIdx(t);
  int next = (cur + 1) % t->count;
  strncpy(t->selSid, t->s[next].sid, sizeof(t->selSid) - 1);
  t->selSid[sizeof(t->selSid) - 1] = 0;
}

// Pin the selected session; pinning the already-pinned one unpins.
// Returns true when a pin is active after the call.
inline bool sessionTabTogglePin(SessionTable* t) {
  int cur = sessionTabSelIdx(t);
  if (cur < 0) { t->pinSid[0] = 0; return false; }
  if (t->pinSid[0] && strcmp(t->pinSid, t->s[cur].sid) == 0) {
    t->pinSid[0] = 0;
    return false;
  }
  strncpy(t->pinSid, t->s[cur].sid, sizeof(t->pinSid) - 1);
  t->pinSid[sizeof(t->pinSid) - 1] = 0;
  return true;
}
