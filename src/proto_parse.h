#pragma once
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include <ArduinoJson.h>
#include "config.h"
#include "logic/utf8_text_logic.h"
#include "session_table.h"

// Protocol v1+v2 parse core (PROTOCOL_V2.md). Header-only with file-static
// state: include from exactly one translation unit — data.h on the device,
// the test harness under BUDDY_NATIVE_TEST.
//
// Every device effect goes through the proto* hook functions declared below;
// data.h implements them against the real firmware (Serial/BLE, xfer.h,
// board::, stats, host registry) and test/test_proto implements them as
// recording stubs. That keeps the whole wire-protocol state machine
// natively unit-testable, including the v2 hard rule that matters most:
// pre-hello behavior is byte-identical to v1.
//
// v2 gating summary (bridge-track reconciled):
//   - hello is always answered (a v1 host never sends it).
//   - rxack: post-hello only, AND only if the host's hello caps included
//     "rxack". n = total bytes received on this transport (spec wording;
//     the host treats any rx ack as liveness and doesn't check the value).
//   - sessions[] / ask / ask_cancel / ntfy: ingested post-hello at effective
//     proto >= 2, tolerantly — NOT re-gated on the host's hello caps (the
//     host gates its sends on OUR ack caps; ntfy in particular is never in
//     the host's hello caps).
//   - prompt.sid/.qn: parsed whenever present (optional-field tolerance).
//   - prompt_cancel: post-hello it clears + acks; pre-hello it falls through
//     to the v1 unknown-cmd swallow (no ack, no effect) for purity.

// ---------------------------------------------------------------------------
// Wire-fed display state (v1 TamaState + v2 additions)
// ---------------------------------------------------------------------------
struct TamaState {
  uint8_t  sessionsTotal;
  uint8_t  sessionsRunning;
  uint8_t  sessionsWaiting;
  bool     recentlyCompleted;
  uint32_t tokensToday;
  uint32_t lastUpdated;
  char     msg[24];
  bool     connected;
  char     lines[8][92];
  uint8_t  nLines;
  uint16_t lineGen;          // bumps when lines change — UI uses to detect updates
  char     promptId[40];     // pending permission request ID; empty = no prompt
  char     promptTool[20];
  char     promptHint[44];
  // --- v2: prompt targeting ---
  char     promptSid[12];    // owning session id, "" when absent
  uint8_t  promptQn;         // prompts queued behind this one (host caps at 3)
  // --- v2: ask (read-only structured question; first question only) ---
  char     qAskId[16];       // "" = no ask pending
  char     qSid[12];
  char     qHeader[24];
  char     qText[200];
  char     qOpts[4][44];
  uint8_t  qNOpts;
  bool     qMulti;
};

inline void protoAskClear(TamaState* t) {
  t->qAskId[0] = 0; t->qSid[0] = 0; t->qHeader[0] = 0; t->qText[0] = 0;
  for (int i = 0; i < 4; i++) t->qOpts[i][0] = 0;
  t->qNOpts = 0; t->qMulti = false;
}

inline void protoPromptClear(TamaState* t) {
  t->promptId[0] = 0; t->promptTool[0] = 0; t->promptHint[0] = 0;
  t->promptSid[0] = 0; t->promptQn = 0;
}

// ---------------------------------------------------------------------------
// ntfy card ring (v2 `ntfy` capability) — stored now, rendered by the
// extras track; DISP_NORMAL shows a count badge.
// ---------------------------------------------------------------------------
struct NtfyCard {
  char     kind[8];
  char     title[36];
  char     body[72];
  uint32_t ts;
};

struct NtfyStore {
  NtfyCard card[4];
  uint8_t  head;    // next write slot
  uint8_t  count;   // cards held (<= 4)
  uint8_t  unread;  // arrived since the cards screen was last viewed (<= count)
  uint32_t total;   // lifetime cards seen
};

inline void ntfyPush(NtfyStore* n, const char* kind, const char* title,
                     const char* body, uint32_t ts) {
  NtfyCard& c = n->card[n->head];
  utf8Sanitize(c.kind,  sizeof(c.kind),  kind  ? kind  : "");
  utf8Sanitize(c.title, sizeof(c.title), title ? title : "");
  utf8Sanitize(c.body,  sizeof(c.body),  body  ? body  : "");
  c.ts = ts;
  n->head = (uint8_t)((n->head + 1) % 4);
  if (n->count < 4) n->count++;
  if (n->unread < 4) n->unread++;   // badge caps at what the ring can hold
  n->total++;
}

// Card by view index, 0 = newest. NULL when out of range.
inline const NtfyCard* ntfyCardAt(const NtfyStore* n, uint8_t viewIdx) {
  if (viewIdx >= n->count) return (const NtfyCard*)0;
  return &n->card[(uint8_t)((n->head + 3 - viewIdx) % 4)];
}

// Remove one card by view index (0 = newest). Survivors are repacked
// oldest-first from slot 0, head follows, so push order stays intact.
// unread is clamped — a dismissal from the cards screen means "seen".
inline bool ntfyDismiss(NtfyStore* n, uint8_t viewIdx) {
  if (viewIdx >= n->count) return false;
  NtfyCard tmp[4];
  uint8_t k = 0;
  for (uint8_t i = 0; i < n->count; i++) {           // oldest -> newest
    uint8_t phys = (uint8_t)((n->head + 4 - n->count + i) % 4);
    uint8_t view = (uint8_t)(n->count - 1 - i);
    if (view == viewIdx) continue;
    tmp[k++] = n->card[phys];
  }
  for (uint8_t i = 0; i < k; i++) n->card[i] = tmp[i];
  n->count = k;
  n->head = k;                                        // k <= 3 after a removal
  if (n->unread > k) n->unread = k;
  return true;
}

// ---------------------------------------------------------------------------
// Per-connection protocol state. One instance per transport (USB, BLE); the
// feeder owns rxBytes (incremented per received byte, newline included) and
// resets the struct on its transport's connection boundary.
// ---------------------------------------------------------------------------
enum : uint8_t {
  PCAP_SESSIONS = 0x01,
  PCAP_ASK      = 0x02,
  PCAP_RXACK    = 0x04,
  PCAP_CANCEL   = 0x08,
  PCAP_NTFY     = 0x10,
};

enum : uint8_t { PROTO_TRANSPORT_USB = 0, PROTO_TRANSPORT_BT = 1 };

struct ProtoState {
  bool     helloSeen;
  uint8_t  effProto;    // min(host proto, BUDDY_PROTO_VER); 1 until hello
  uint8_t  hostCaps;    // PCAP_* bits the host claimed in hello
  uint8_t  transport;   // PROTO_TRANSPORT_*
  uint32_t rxBytes;     // feeder-maintained; reported in rxack
};

inline void protoResetConn(ProtoState* ps) {
  uint8_t tr = ps->transport;
  memset(ps, 0, sizeof(*ps));
  ps->transport = tr;
  ps->effProto = 1;
}

struct ProtoHello {
  char    id[9];
  char    name[24];
  char    app[12];
  char    ver[12];
  uint8_t proto;
  bool    viaBle;
};

// ---------------------------------------------------------------------------
// Hooks — device implementations live in data.h, native stubs in test_proto.
// ---------------------------------------------------------------------------
void        protoEmit(const char* line);            // send one reply line (no trailing \n)
uint32_t    protoMillis();
bool        protoXferCommand(JsonDocument& doc);    // v1 cmd path; true = consumed
void        protoSetClock(uint32_t epoch, int32_t tzOffsetSec);
void        protoTokens(uint32_t bridgeTotal);
bool        protoHelloAccept(const ProtoHello& h);  // registry store; returns data.sel
const char* protoBoardName();
const char* protoDeviceName();
#ifdef BUDDY_DEBUG
void        protoDebugState(const TamaState* t, const ProtoState* ps);
#endif

// Shared stores (single TU — same rule as the rest of this header).
static SessionTable _sessions;
static NtfyStore    _ntfy;

inline SessionTable& protoSessions() { return _sessions; }
inline NtfyStore&    protoNtfy()     { return _ntfy; }

// ---------------------------------------------------------------------------
// Internals
// ---------------------------------------------------------------------------

// rxack after each received line, post-hello, host-capability gated. The
// hello line itself is answered by the hello ack, not an rx ack.
inline void _protoMaybeRxAck(const ProtoState* ps) {
  if (!ps->helloSeen || ps->effProto < 2 || !(ps->hostCaps & PCAP_RXACK)) return;
  char b[48];
  snprintf(b, sizeof(b), "{\"ack\":\"rx\",\"n\":%lu}", (unsigned long)ps->rxBytes);
  protoEmit(b);
}

inline void _protoHello(JsonDocument& doc, TamaState* out, ProtoState* ps) {
  ProtoHello h;
  memset(&h, 0, sizeof(h));
  int hostProto = doc["proto"] | 1;
  if (hostProto < 1) hostProto = 1;
  if (hostProto > 255) hostProto = 255;
  JsonObject ho = doc["host"];
  const char* id = ho["id"];
  if (id) { strncpy(h.id, id, sizeof(h.id) - 1); h.id[sizeof(h.id) - 1] = 0; }
  utf8Sanitize(h.name, sizeof(h.name), ho["name"] | "");
  utf8Sanitize(h.app,  sizeof(h.app),  ho["app"]  | "");
  utf8Sanitize(h.ver,  sizeof(h.ver),  ho["ver"]  | "");
  h.proto = (uint8_t)hostProto;
  h.viaBle = ps->transport == PROTO_TRANSPORT_BT;

  uint8_t caps = 0;
  for (JsonVariant c : doc["caps"].as<JsonArray>()) {
    const char* cs = c.as<const char*>();
    if (!cs) continue;
    if      (strcmp(cs, "sessions") == 0) caps |= PCAP_SESSIONS;
    else if (strcmp(cs, "ask")      == 0) caps |= PCAP_ASK;
    else if (strcmp(cs, "rxack")    == 0) caps |= PCAP_RXACK;
    else if (strcmp(cs, "cancel")   == 0) caps |= PCAP_CANCEL;
    else if (strcmp(cs, "ntfy")     == 0) caps |= PCAP_NTFY;
  }

  bool sel = protoHelloAccept(h);
  ps->helloSeen = true;
  ps->hostCaps = caps;
  ps->effProto = (uint8_t)(hostProto < BUDDY_PROTO_VER ? hostProto : BUDDY_PROTO_VER);

  // Fresh handshake = fresh per-host view. ntfy cards survive (they're
  // device-side notifications, not host session state).
  sessionTabClear(&_sessions);
  protoAskClear(out);

  char b[288];
  snprintf(b, sizeof(b),
    "{\"ack\":\"hello\",\"ok\":true,\"proto\":%d,\"data\":{"
    "\"fw\":\"%s\",\"board\":\"%s\",\"name\":\"%s\","
    "\"caps\":[\"sessions\",\"ask\",\"rxack\",\"cancel\",\"ntfy\"],"
    "\"maxSessions\":%d,\"maxLine\":%d,\"sel\":%s}}",
    BUDDY_PROTO_VER, BUDDY_FW_VERSION, protoBoardName(), protoDeviceName(),
    BUDDY_MAX_SESSIONS, BUDDY_LINEBUF, sel ? "true" : "false");
  protoEmit(b);
}

inline void _protoIngestSessions(JsonArray sa) {
  Session tmp[BUDDY_MAX_SESSIONS];
  uint8_t n = 0;
  for (JsonVariant v : sa) {
    if (n >= BUDDY_MAX_SESSIONS) break;   // clamp to advertised maxSessions
    JsonObject o = v.as<JsonObject>();
    if (o.isNull()) continue;
    const char* sid = o["sid"];
    if (!sid || !sid[0]) continue;        // sid is the row identity — required
    Session& s = tmp[n];
    memset(&s, 0, sizeof(s));
    strncpy(s.sid, sid, sizeof(s.sid) - 1);
    utf8Sanitize(s.agent, sizeof(s.agent), o["agent"] | "");
    utf8Sanitize(s.title, sizeof(s.title), o["title"] | "");
    s.state = sessionStateFromStr(o["state"] | "");
    s.tok   = o["tok"] | (uint32_t)0;
    utf8Sanitize(s.last, sizeof(s.last), o["last"] | "");
    n++;
  }
  sessionTabCommit(&_sessions, tmp, n);
}

inline void _protoAsk(JsonDocument& doc, TamaState* out) {
  const char* id = doc["id"];
  JsonArray qs = doc["questions"];
  JsonObject q0 = qs[0];
  if (!id || !id[0] || q0.isNull()) return;   // need an id to cancel + a question
  protoAskClear(out);
  strncpy(out->qAskId, id, sizeof(out->qAskId) - 1);
  out->qAskId[sizeof(out->qAskId) - 1] = 0;
  const char* sid = doc["sid"];
  if (sid) { strncpy(out->qSid, sid, sizeof(out->qSid) - 1); out->qSid[sizeof(out->qSid) - 1] = 0; }
  out->qMulti = doc["multiSelect"] | false;
  utf8Sanitize(out->qHeader, sizeof(out->qHeader), q0["header"] | "");
  utf8Sanitize(out->qText,   sizeof(out->qText),   q0["text"]   | "");
  uint8_t n = 0;
  for (JsonVariant v : q0["options"].as<JsonArray>()) {
    if (n >= 4) break;
    JsonObject o = v.as<JsonObject>();
    utf8Sanitize(out->qOpts[n], sizeof(out->qOpts[n]), o["label"] | "");
    n++;
  }
  out->qNOpts = n;
}

// ---------------------------------------------------------------------------
// Line entry point. Returns true when the line counted as live traffic
// (i.e. parsed as JSON) — the caller stamps its transport-liveness clock.
// ---------------------------------------------------------------------------
inline bool protoApplyJson(const char* line, TamaState* out, ProtoState* ps) {
  JsonDocument doc;
  if (deserializeJson(doc, line)) return false;

  const char* cmd = doc["cmd"];

  // --- v2 command intercepts (checked before the v1 xfer path, whose
  //     catch-all would otherwise swallow them silently). A v1 host never
  //     sends these, so pre-hello byte-identity is preserved.
  if (cmd && strcmp(cmd, "hello") == 0) {
    _protoHello(doc, out, ps);
    return true;
  }
  if (cmd && strcmp(cmd, "prompt_cancel") == 0 && ps->helloSeen && ps->effProto >= 2) {
    const char* id = doc["id"];
    bool ok = id && id[0] && out->promptId[0] && strcmp(id, out->promptId) == 0;
    if (ok) protoPromptClear(out);
    char b[56];
    snprintf(b, sizeof(b), "{\"ack\":\"prompt_cancel\",\"ok\":%s}", ok ? "true" : "false");
    protoEmit(b);
    _protoMaybeRxAck(ps);
    return true;
  }
#ifdef BUDDY_DEBUG
  if (cmd && strcmp(cmd, "debug_state") == 0) {
    protoDebugState(out, ps);
    return true;
  }
#endif

  // --- v1 command path (xfer, status, name, owner, unpair, species). Also
  //     swallows any other cmd without an ack — long-standing v1 behavior
  //     that pre-hello v2 cmds intentionally fall into.
  if (protoXferCommand(doc)) {
    _protoMaybeRxAck(ps);
    return true;
  }

  // --- v1 time sync {"time":[epoch_sec, tz_offset_sec]}
  JsonArray t = doc["time"];
  if (!t.isNull() && t.size() == 2) {
    protoSetClock(t[0].as<uint32_t>(), t[1].as<int32_t>());
    _protoMaybeRxAck(ps);
    return true;
  }

  // --- v2 events (post-hello only; pre-hello they fall through to the v1
  //     state parse below, exactly like any other unknown-field line).
  const char* evt = doc["evt"];
  if (evt && ps->helloSeen && ps->effProto >= 2) {
    if (strcmp(evt, "ask") == 0) {
      _protoAsk(doc, out);
      _protoMaybeRxAck(ps);
      return true;
    }
    if (strcmp(evt, "ask_cancel") == 0) {
      const char* id = doc["id"];
      if (id && id[0] && strcmp(id, out->qAskId) == 0) protoAskClear(out);
      _protoMaybeRxAck(ps);
      return true;
    }
    if (strcmp(evt, "ntfy") == 0) {
      ntfyPush(&_ntfy, doc["kind"] | "", doc["title"] | "", doc["body"] | "",
               doc["ts"] | (uint32_t)0);
      _protoMaybeRxAck(ps);
      return true;
    }
    // other evt kinds (e.g. v1 "turn") fall through, as in v1
  }

  // --- v1 heartbeat state parse (v2 optional fields folded in) ---
  out->sessionsTotal     = doc["total"]     | out->sessionsTotal;
  out->sessionsRunning   = doc["running"]   | out->sessionsRunning;
  out->sessionsWaiting   = doc["waiting"]   | out->sessionsWaiting;
  out->recentlyCompleted = doc["completed"] | false;
  uint32_t bridgeTokens = doc["tokens"] | 0;
  if (doc["tokens"].is<uint32_t>()) protoTokens(bridgeTokens);
  out->tokensToday = doc["tokens_today"] | out->tokensToday;
  // Display text is sanitized at ingestion (UTF-8 punctuation -> ASCII,
  // everything unrenderable -> '?') so stored buffers are always safe for
  // the ASCII-only fonts and the wrap logic.
  const char* m = doc["msg"];
  if (m) utf8Sanitize(out->msg, sizeof(out->msg), m);
  JsonArray la = doc["entries"];
  if (!la.isNull()) {
    // Entries are oldest-first; if more arrive than the buffer holds, skip the
    // oldest so we always keep the newest entries (the ones shown in the HUD).
    uint8_t total = (uint8_t)la.size();
    uint8_t skip = total > 8 ? total - 8 : 0;
    uint8_t n = 0;
    for (JsonVariant v : la) {
      if (skip > 0) { skip--; continue; }
      const char* s = v.as<const char*>();
      utf8Sanitize(out->lines[n], sizeof(out->lines[n]), s ? s : "");
      n++;
    }
    if (n != out->nLines || (n > 0 && strcmp(out->lines[n-1], out->msg) != 0)) {
      out->lineGen++;
    }
    out->nLines = n;
  }

  // v2: per-session detail alongside (not instead of) the v1 aggregates,
  // which stay authoritative. Ignored pre-hello (v1 unknown-field rule).
  if (ps->helloSeen && ps->effProto >= 2) {
    JsonArray sa = doc["sessions"];
    if (!sa.isNull()) _protoIngestSessions(sa);
  }

  JsonObject pr = doc["prompt"];
  if (!pr.isNull()) {
    const char* pid = pr["id"]; const char* pt = pr["tool"]; const char* ph = pr["hint"];
    // promptId is an opaque token echoed back in the permission response —
    // copy it verbatim. Tool/hint are display text -> sanitize.
    strncpy(out->promptId,   pid ? pid : "", sizeof(out->promptId)-1);   out->promptId[sizeof(out->promptId)-1]=0;
    utf8Sanitize(out->promptTool, sizeof(out->promptTool), pt ? pt : "");
    utf8Sanitize(out->promptHint, sizeof(out->promptHint), ph ? ph : "");
    // v2 optional targeting fields: sid (owning session) + qn (queued-behind
    // count, capped 3 host-side). Tolerated whenever present.
    const char* psid = pr["sid"];
    strncpy(out->promptSid, psid ? psid : "", sizeof(out->promptSid)-1);
    out->promptSid[sizeof(out->promptSid)-1] = 0;
    int qn = pr["qn"] | 0;
    out->promptQn = (uint8_t)(qn < 0 ? 0 : qn > 9 ? 9 : qn);
  } else {
    protoPromptClear(out);
  }
  out->lastUpdated = protoMillis();
  _protoMaybeRxAck(ps);
  return true;
}
