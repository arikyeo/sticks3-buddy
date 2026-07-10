#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include "config.h"
#include "hal/hal.h"
#include "logic/utf8_text_logic.h"
#include "ble_bridge.h"
#include "host_registry.h"
#include "session_table.h"
#include "proto_parse.h"
#include "xfer.h"

// Device-side wiring for the protocol parse core (proto_parse.h): the two
// line feeders (USB serial + BLE ring), the demo/live/asleep mode logic,
// and the proto* hook implementations that bind the parse core to the real
// firmware (Serial/BLE emit, xfer commands, board clock, stats, registry).
//
// TamaState itself now lives in proto_parse.h so the parse core is natively
// unit-testable (test/test_proto) with stub hooks.

// ---------------------------------------------------------------------------
// Three modes, checked in priority order:
//   demo   → auto-cycle fake scenarios every 8s, ignore live data
//   live   → JSON arrived in the last 10s over USB or BT
//   asleep → no data, all zeros, "No Claude connected"
// ---------------------------------------------------------------------------

static uint32_t _lastLiveMs = 0;
static uint32_t _lastBtByteMs = 0;   // hasClient() lies; track actual BT traffic
static bool     _demoMode   = false;
static uint8_t  _demoIdx    = 0;
static uint32_t _demoNext   = 0;

struct _Fake { const char* n; uint8_t t,r,w; bool c; uint32_t tok; };
static const _Fake _FAKES[] = {
  {"asleep",0,0,0,false,0}, {"one idle",1,0,0,false,12000},
  {"busy",4,3,0,false,89000}, {"attention",2,1,1,false,45000},
  {"completed",1,0,0,true,142000},
};

inline void dataSetDemo(bool on) {
  _demoMode = on;
  if (on) { _demoIdx = 0; _demoNext = millis(); }
}
inline bool dataDemo() { return _demoMode; }

inline bool dataConnected() {
  return _lastLiveMs != 0 && (millis() - _lastLiveMs) <= 30000;
}

inline bool dataBtActive() {
  // Desktop's idle keepalive is ~10s; give it 1.5x headroom.
  return _lastBtByteMs != 0 && (millis() - _lastBtByteMs) <= 15000;
}

inline const char* dataScenarioName() {
  if (_demoMode) return _FAKES[_demoIdx].n;
  if (dataConnected()) return dataBtActive() ? "bt" : "usb";
  return "none";
}

// Set true once the bridge sends a time sync — until then the RTC may
// hold whatever was on the coin cell (or 2000-01-01 if it lost power).
static bool _rtcValid = false;
inline bool dataRtcValid() { return _rtcValid; }

// Per-transport protocol v2 state. BLE resets on its physical disconnect;
// USB has no connection signal, so it resets on the 30s liveness drop.
static ProtoState _usbProto = { false, 1, 0, PROTO_TRANSPORT_USB, 0 };
static ProtoState _btProto  = { false, 1, 0, PROTO_TRANSPORT_BT,  0 };

inline ProtoState& protoUsbState() { return _usbProto; }
inline ProtoState& protoBtState()  { return _btProto; }

// ---------------------------------------------------------------------------
// proto_parse.h hook implementations
// ---------------------------------------------------------------------------

// Replies go to both streams, matching the long-standing v1 ack convention
// (xfer.h _xAck): we don't track which transport delivered a command, and
// writes to the unused stream are dropped/ignored by the other side.
void protoEmit(const char* line) {
  Serial.println(line);
  size_t n = strlen(line);
  bleWrite((const uint8_t*)line, n);
  bleWrite((const uint8_t*)"\n", 1);
}

uint32_t protoMillis() { return millis(); }

bool protoXferCommand(JsonDocument& doc) { return xferCommand(doc); }

// Bridge sends {"time":[epoch_sec, tz_offset_sec]}; gmtime_r on the
// adjusted epoch yields local components including weekday. The board
// clock stores them — BM8563 hardware on the Plus/Plus2, millis-projected
// software clock on the S3 (which has no RTC). The tz offset is kept so
// other epoch-stamped data (ntfy card timestamps) can render local HH:MM.
static int32_t _tzOffsetSec = 0;
inline int32_t dataTzOffset() { return _tzOffsetSec; }

void protoSetClock(uint32_t epoch, int32_t tzOffsetSec) {
  time_t local = (time_t)epoch + tzOffsetSec;
  struct tm lt; gmtime_r(&local, &lt);
  board::setClock(&lt);
  _tzOffsetSec = tzOffsetSec;
  extern uint32_t _clkLastRead;
  _clkLastRead = 0;   // force clockRefreshRtc() to re-read immediately
  _rtcValid = true;
}

void protoTokens(uint32_t bridgeTotal) { statsOnBridgeTokens(bridgeTotal); }

bool protoHelloAccept(const ProtoHello& h) {
  return hostGlueOnHello(h.id, h.name, h.app, h.viaBle);
}

const char* protoBoardName()  { return board::name(); }
const char* protoDeviceName() { return petName(); }

#ifdef BUDDY_DEBUG
// Harness introspection (tools/test_protocol.py): dump the session table,
// prompt/ask routing state, and the host registry as two JSON lines. Debug
// builds only — never present on a release wire, so the pre-hello purity
// rule holds by construction where it matters.
void protoDebugState(const TamaState* t, const ProtoState* ps) {
  char b[BUDDY_LINEBUF < 900 ? BUDDY_LINEBUF : 900];
  size_t o = 0;
  o += snprintf(b + o, sizeof(b) - o,
    "{\"dbg\":\"state\",\"hello\":%s,\"proto\":%u,\"caps\":%u,"
    "\"prompt\":{\"id\":\"%s\",\"sid\":\"%s\",\"qn\":%u},"
    "\"ask\":{\"id\":\"%s\",\"sid\":\"%s\",\"nOpts\":%u},\"sessions\":[",
    ps->helloSeen ? "true" : "false", ps->effProto, ps->hostCaps,
    t->promptId, t->promptSid, t->promptQn,
    t->qAskId, t->qSid, t->qNOpts);
  for (uint8_t i = 0; i < _sessions.count && o < sizeof(b) - 2; i++) {
    const Session& s = _sessions.s[i];
    o += snprintf(b + o, sizeof(b) - o,
      "%s{\"sid\":\"%s\",\"st\":%u,\"tok\":%lu,\"agent\":\"%s\"}",
      i ? "," : "", s.sid, s.state, (unsigned long)s.tok, s.agent);
  }
  if (o > sizeof(b) - 3) o = sizeof(b) - 3;
  snprintf(b + o, sizeof(b) - o, "]}");
  protoEmit(b);

  o = 0;
  o += snprintf(b + o, sizeof(b) - o,
    "{\"dbg\":\"hosts\",\"n\":%u,\"pin\":%d,\"conn\":%d,\"hosts\":[",
    hostGlueCount(), (int)hostGluePin(), hostGlueConnectedSlot());
  for (uint8_t i = 0; i < hostGlueCount() && o < sizeof(b) - 2; i++) {
    const HostRec* r = hostGlueGet(i);
    o += snprintf(b + o, sizeof(b) - o,
      "%s{\"name\":\"%s\",\"id\":\"%s\",\"seen\":%lu}",
      i ? "," : "", r->name, r->hostId, (unsigned long)r->lastSeen);
  }
  if (o > sizeof(b) - 3) o = sizeof(b) - 3;
  snprintf(b + o, sizeof(b) - o, "]}");
  protoEmit(b);
}
#endif

// ---------------------------------------------------------------------------
// Line feeders
// ---------------------------------------------------------------------------
template<size_t N>
struct _LineBuf {
  char buf[N];
  uint16_t len = 0;
  void feed(Stream& s, TamaState* out, ProtoState* ps) {
    while (s.available()) {
      char c = s.read();
      ps->rxBytes++;
      if (c == '\n' || c == '\r') {
        if (len > 0) {
          buf[len] = 0;
          if (buf[0] == '{' && protoApplyJson(buf, out, ps)) _lastLiveMs = millis();
          len = 0;
        }
      } else if (len < N-1) {
        buf[len++] = c;
      }
    }
  }
};

static _LineBuf<BUDDY_LINEBUF> _usbLine, _btLine;

inline void dataPoll(TamaState* out) {
  uint32_t now = millis();

  if (_demoMode) {
    if (now >= _demoNext) { _demoIdx = (_demoIdx + 1) % 5; _demoNext = now + 8000; }
    const _Fake& s = _FAKES[_demoIdx];
    out->sessionsTotal=s.t; out->sessionsRunning=s.r; out->sessionsWaiting=s.w;
    out->recentlyCompleted=s.c; out->tokensToday=s.tok; out->lastUpdated=now;
    out->connected = true;
    snprintf(out->msg, sizeof(out->msg), "demo: %s", s.n);
    return;
  }

#if !defined(ARDUINO_USB_MODE) || ARDUINO_USB_MODE == 0
  _usbLine.feed(Serial, out, &_usbProto);
#endif

  // BLE connection boundary: reset the per-connection protocol state, and
  // drop the per-host view (sessions, ask) if this link had negotiated it.
  static bool _btWasConn = false;
  bool btConn = bleConnected();
  if (_btWasConn && !btConn) {
    if (_btProto.helloSeen) {
      sessionTabClear(&_sessions);
      protoAskClear(out);
    }
    protoResetConn(&_btProto);
    _btLine.len = 0;   // half a line from the dead link must not prefix the next
  }
  _btWasConn = btConn;

  // BLE ring buffer is drained manually since it's not a Stream.
  while (bleAvailable()) {
    int c = bleRead();
    if (c < 0) break;
    _lastBtByteMs = millis();
    _btProto.rxBytes++;
    if (c == '\n' || c == '\r') {
      if (_btLine.len > 0) {
        _btLine.buf[_btLine.len] = 0;
        if (_btLine.buf[0] == '{' && protoApplyJson(_btLine.buf, out, &_btProto)) {
          _lastLiveMs = millis();
        }
        _btLine.len = 0;
      }
    } else if (_btLine.len < sizeof(_btLine.buf) - 1) {
      _btLine.buf[_btLine.len++] = (char)c;
    }
  }

  bool wasConnected = out->connected;
  out->connected = dataConnected();
  if (!out->connected) {
    if (wasConnected) {
      _rtcValid = false;  // force re-sync on next connection
      // 30s of silence is the v1 "connection dead" signal — reset the USB
      // protocol state (it has no physical disconnect) and the per-host view.
      protoResetConn(&_usbProto);
      sessionTabClear(&_sessions);
    }
    out->sessionsTotal=0; out->sessionsRunning=0; out->sessionsWaiting=0;
    out->recentlyCompleted=false; out->lastUpdated=now;
    out->nLines=0;
    protoPromptClear(out);
    protoAskClear(out);
    strncpy(out->msg, "No Claude connected", sizeof(out->msg)-1);
    out->msg[sizeof(out->msg)-1]=0;
  }
}
