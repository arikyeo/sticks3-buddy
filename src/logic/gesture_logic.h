#pragma once
#include <stdint.h>
#include <math.h>

// IMU gesture detector. Pure logic — no Arduino / M5 dependency — so it
// unit-tests on the native host (test/test_gesture).
//
// Fed accel samples (g units) + the caller's millis on the existing 50 ms
// IMU cadence; emits at most one event per feed:
//
//   GE_FLIP        z-axis inversion held 600 ms — one event per flip;
//                  re-arms only after z reads upright for 300 ms, so a
//                  device left face-down fires exactly once. main.cpp
//                  maps it to "pin next bonded host" (and additionally
//                  suppresses it in the flat face-down nap pose — the
//                  nap gesture owns that).
//   GE_DOUBLE_TAP  two z-impulse spikes 80..400 ms apart -> wake screen.
//                  Impacts ring for tens of ms, so 20 Hz sampling sees
//                  them; the min-gap keeps one physical tap (and its
//                  ring-down) from counting twice, a lockout keeps a
//                  drum-roll from firing repeatedly.
//
// Deliberately NO shake gesture here: shake->dizzy already lives in
// main.cpp, and shake-to-deny is forbidden — a misfire must never answer
// a consent prompt. This detector is wired nowhere near prompt actions.

enum GestureEvent : uint8_t { GE_NONE, GE_FLIP, GE_DOUBLE_TAP };

static const float    GE_Z_UP_G         = 0.5f;   // upright above this
static const float    GE_Z_DOWN_G       = -0.5f;  // inverted below this
static const uint32_t GE_FLIP_HOLD_MS   = 600;    // inversion hold to fire
static const uint32_t GE_FLIP_REARM_MS  = 300;    // upright hold to re-arm
static const float    GE_TAP_DELTA_G    = 0.45f;  // |az - ema| spike threshold
static const uint32_t GE_TAP_MIN_GAP_MS = 80;     // ring-down debounce
static const uint32_t GE_TAP_MAX_GAP_MS = 400;    // second-tap window
static const uint32_t GE_TAP_LOCKOUT_MS = 600;    // quiet time after a fire

struct GestureState {
  // flip
  bool     inverted;       // decisively below the down threshold
  bool     armed;          // upright long enough that a flip may fire
  bool     upSeen;         // counting toward the re-arm hold
  uint32_t invertedAtMs;
  uint32_t uprightAtMs;
  // double tap
  float    zEma;           // slow baseline; a tap is a fast excursion off it
  bool     emaSeeded;
  bool     tapPending;     // first tap seen, second-tap window open
  uint32_t tapAtMs;
  uint32_t lastSpikeMs;
  uint32_t lockoutUntilMs;
};

inline void gestureInit(GestureState& g) {
  g.inverted = false;
  g.armed = false;
  g.upSeen = false;
  g.invertedAtMs = 0;
  g.uprightAtMs = 0;
  g.zEma = 0.0f;
  g.emaSeeded = false;
  g.tapPending = false;
  g.tapAtMs = 0;
  g.lastSpikeMs = 0;
  g.lockoutUntilMs = 0;
}

inline GestureEvent gestureFeed(GestureState& g, float ax, float ay, float az,
                                uint32_t nowMs) {
  (void)ax; (void)ay;   // both gestures are z-axis affairs
  GestureEvent ev = GE_NONE;

  // ---- flip: sustained z inversion, edge-fired, upright re-arm ------------
  if (az > GE_Z_UP_G) {
    if (!g.upSeen) { g.upSeen = true; g.uprightAtMs = nowMs; }
    if (!g.armed && (nowMs - g.uprightAtMs) >= GE_FLIP_REARM_MS) g.armed = true;
    g.inverted = false;
  } else if (az < GE_Z_DOWN_G) {
    g.upSeen = false;
    if (!g.inverted) {
      g.inverted = true;
      g.invertedAtMs = nowMs;
    } else if (g.armed && (nowMs - g.invertedAtMs) >= GE_FLIP_HOLD_MS) {
      g.armed = false;   // consumed — next flip needs an upright spell first
      ev = GE_FLIP;
    }
  } else {
    // In-between (on edge / mid-turn): neither hold accumulates. A wobble
    // through the dead zone restarts the inversion clock on purpose.
    g.upSeen = false;
    g.inverted = false;
  }

  // ---- double tap: two isolated spikes off a slow baseline ----------------
  if (!g.emaSeeded) { g.zEma = az; g.emaSeeded = true; }
  float delta = fabsf(az - g.zEma);
  g.zEma = g.zEma * 0.7f + az * 0.3f;

  // Taps only pair while the flip detector is armed (device has been
  // upright ≥ 300 ms). That single gate kills the false pair a quick
  // flip-and-back would otherwise produce — two big z excursions 200 ms
  // apart are indistinguishable from taps by waveform alone.
  bool locked = (int32_t)(nowMs - g.lockoutUntilMs) < 0;
  if (!locked && g.armed && delta > GE_TAP_DELTA_G) {
    if ((nowMs - g.lastSpikeMs) >= GE_TAP_MIN_GAP_MS) {
      if (g.tapPending && (nowMs - g.tapAtMs) <= GE_TAP_MAX_GAP_MS) {
        g.tapPending = false;
        g.lockoutUntilMs = nowMs + GE_TAP_LOCKOUT_MS;
        if (ev == GE_NONE) ev = GE_DOUBLE_TAP;
      } else {
        g.tapPending = true;
        g.tapAtMs = nowMs;
      }
    }
    g.lastSpikeMs = nowMs;   // ring-down samples keep sliding the debounce
  }
  if (g.tapPending && (nowMs - g.tapAtMs) > GE_TAP_MAX_GAP_MS) {
    g.tapPending = false;    // window closed — that was a single tap
  }
  return ev;
}
