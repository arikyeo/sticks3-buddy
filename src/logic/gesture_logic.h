#pragma once
#include <stdint.h>
#include <math.h>

// IMU gesture detector. Pure logic — no Arduino / M5 dependency — so it
// unit-tests on the native host (test/test_gesture).
//
// Fed accel samples (g units) + the caller's millis on the existing 50 ms
// IMU cadence; emits at most one event per feed:
//
//   GE_FLIP        flip-and-return: az dips below the inverted threshold
//                  from a confirmed-upright baseline, then comes back up —
//                  fires on the RETURN (upright) edge, not while still
//                  inverted. Gated on two windows: the inverted dwell (how
//                  long it stayed flipped) must be 150..1500 ms — shorter
//                  is noise, longer means it was set down, not flipped —
//                  and the whole upright-to-upright round trip must land
//                  inside 2500 ms, which catches a slow deliberate tilt
//                  that only brushes the inverted threshold briefly but
//                  dawdles in transit. One-shot per excursion, plus an
//                  800 ms lockout after a fire so the settle wobble on the
//                  way back doesn't re-trigger. main.cpp maps it to "pin
//                  next bonded host".
//                  Firing on the return (instead of mid-hold, as an
//                  earlier revision did) is deliberate: it's what lets
//                  this live beside the flat face-down nap gesture with
//                  no shared suppression flag. Nap fires off a *sustained*
//                  face-down read (its own frame counter in main.cpp,
//                  untouched by this file); flip-and-return is over — the
//                  event has already fired — before that counter gets
//                  anywhere close. A device actually being set down to
//                  nap never returns upright at all, so it never reaches
//                  this detector's fire edge in the first place.
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

static const float    GE_Z_UP_G            = 0.4f;   // confirmed-upright baseline
static const float    GE_Z_DOWN_G          = -0.6f;  // inverted trigger
static const uint32_t GE_FLIP_DWELL_MIN_MS = 150;    // shorter inverted spell = noise
static const uint32_t GE_FLIP_DWELL_MAX_MS = 1500;   // longer = set down, not flipped
static const uint32_t GE_FLIP_ROUNDTRIP_MS = 2500;   // outer cap, upright to upright
static const uint32_t GE_FLIP_LOCKOUT_MS   = 800;    // quiet time after a fire
static const float    GE_TAP_DELTA_G       = 0.45f;  // |az - ema| spike threshold
static const uint32_t GE_TAP_MIN_GAP_MS    = 80;     // ring-down debounce
static const uint32_t GE_TAP_MAX_GAP_MS    = 400;    // second-tap window
static const uint32_t GE_TAP_LOCKOUT_MS    = 600;    // quiet time after a fire

struct GestureState {
  // flip (flip-and-return)
  bool     armed;             // confirmed upright right now; also the
                               // double-tap pairing gate below
  bool     inverted;          // this excursion has dipped below the inverted
                               // threshold at least once — latched through
                               // the return transit on purpose (see
                               // gestureFeed) so the return edge still sees it
  bool     excursionFromBaseline;  // this excursion legitimately left a
                               // confirmed-upright baseline (false for e.g. a
                               // device that boots already inverted, so it
                               // can never fire without ever having been
                               // seen upright first)
  uint32_t leftUprightAtMs;   // when this excursion left the baseline —
                               // anchors the round-trip cap
  uint32_t invertedAtMs;      // when this excursion first crossed inverted —
                               // anchors the dwell window
  uint32_t flipLockoutUntilMs;
  // double tap
  float    zEma;              // slow baseline; a tap is a fast excursion off it
  bool     emaSeeded;
  bool     tapPending;        // first tap seen, second-tap window open
  uint32_t tapAtMs;
  uint32_t lastSpikeMs;
  uint32_t lockoutUntilMs;
};

inline void gestureInit(GestureState& g) {
  g.armed = false;
  g.inverted = false;
  g.excursionFromBaseline = false;
  g.leftUprightAtMs = 0;
  g.invertedAtMs = 0;
  g.flipLockoutUntilMs = 0;
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
  bool flipLocked = (int32_t)(nowMs - g.flipLockoutUntilMs) < 0;
  bool wasArmed = g.armed;   // upright at entry — see the tap gate below

  // ---- flip: inverted dip that returns upright fires on the return ------
  if (az > GE_Z_UP_G) {
    // Back at (or freshly at) the upright baseline. If this excursion left
    // that baseline and dipped inverted, this is the fire edge — gate it
    // on the dwell + round-trip windows and the post-fire lockout, then
    // unconditionally close the excursion out either way.
    if (g.excursionFromBaseline && g.inverted && !flipLocked) {
      uint32_t dwell     = nowMs - g.invertedAtMs;
      uint32_t roundTrip = nowMs - g.leftUprightAtMs;
      if (dwell >= GE_FLIP_DWELL_MIN_MS && dwell <= GE_FLIP_DWELL_MAX_MS &&
          roundTrip <= GE_FLIP_ROUNDTRIP_MS) {
        ev = GE_FLIP;
        g.flipLockoutUntilMs = nowMs + GE_FLIP_LOCKOUT_MS;
      }
    }
    g.excursionFromBaseline = false;
    g.inverted = false;
    g.armed = true;
  } else if (az < GE_Z_DOWN_G) {
    // Inverted. Only latch the excursion's start if it left a confirmed
    // baseline — a device that boots (or wakes) already inverted has no
    // baseline to return to, so it can never produce a fire.
    if (g.armed) { g.excursionFromBaseline = true; g.leftUprightAtMs = nowMs; }
    g.armed = false;
    if (!g.inverted) {
      g.inverted = true;
      g.invertedAtMs = nowMs;
    }
  } else {
    // Dead zone (mid-turn / on edge). Deliberately does NOT clear the
    // `inverted` latch — unlike the old hold-while-inverted design, this
    // one fires on the return trip, which physically passes back through
    // this zone on every flip; clearing here would mean the detector never
    // fires. It only marks the excursion's start on the true leave-upright
    // edge, same as the inverted branch.
    if (g.armed) { g.excursionFromBaseline = true; g.leftUprightAtMs = nowMs; }
    g.armed = false;
  }

  // ---- double tap: two isolated spikes off a slow baseline ----------------
  if (!g.emaSeeded) { g.zEma = az; g.emaSeeded = true; }
  float delta = fabsf(az - g.zEma);
  g.zEma = g.zEma * 0.7f + az * 0.3f;

  // Taps only pair on STEADY-upright samples: armed both before this feed
  // (wasArmed) and after the flip section ran (g.armed). Gating on g.armed
  // alone is not enough — a flip's return edge re-arms in the same sample
  // it spikes, so two flip round trips ~350 ms apart would pair as a
  // double tap (caught by CI executing the native suite). Big z swings are
  // indistinguishable from taps by waveform alone; steadiness is the
  // discriminator.
  bool locked = (int32_t)(nowMs - g.lockoutUntilMs) < 0;
  if (!locked && wasArmed && g.armed && delta > GE_TAP_DELTA_G) {
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
