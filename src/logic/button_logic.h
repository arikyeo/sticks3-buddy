#pragma once
#include <stdint.h>

// Debounced button with an M5-Button-compatible query API. Pure logic —
// no Arduino dependency — so it unit-tests on the host (native env).
// Feed the raw level + timestamp once per loop via update().
//
// Ported verbatim from 23atomist's src/hw_button.h (8968f26). Not yet
// wired into main.cpp — main still reads M5.BtnA/BtnB directly; this
// header + its tests land first so the swap can be a separate, small diff.
class Button {
 public:
  static const uint32_t DEBOUNCE_MS = 15;

  void update(bool physDown, uint32_t nowMs) {
    prev_ = state_;
    if (physDown != raw_) { raw_ = physDown; lastEdgeMs_ = nowMs; }
    if (state_ != raw_ && (nowMs - lastEdgeMs_) >= DEBOUNCE_MS) {
      state_ = raw_;
      if (state_) downSinceMs_ = nowMs;
    }
    nowMs_ = nowMs;
  }
  bool isPressed()   const { return state_; }
  bool wasPressed()  const { return state_ && !prev_; }
  bool wasReleased() const { return !state_ && prev_; }
  bool pressedFor(uint32_t ms) const {
    return state_ && (nowMs_ - downSinceMs_) >= ms;
  }

 private:
  bool raw_ = false, state_ = false, prev_ = false;
  uint32_t lastEdgeMs_ = 0, downSinceMs_ = 0, nowMs_ = 0;
};

// A+B chord. While both buttons are (or were) down together the caller
// must suppress single-button actions (engaged()); holding both past
// holdMs makes update() return true exactly once per engagement.
class Chord {
 public:
  explicit Chord(uint32_t holdMs) : holdMs_(holdMs) {}

  bool update(bool aDown, bool bDown, uint32_t nowMs) {
    if (aDown && bDown) {
      if (!bothDown_) { bothDown_ = true; fired_ = false; bothSinceMs_ = nowMs; }
      engaged_ = true;
      if (!fired_ && (nowMs - bothSinceMs_) >= holdMs_) { fired_ = true; return true; }
    } else {
      bothDown_ = false;
      if (!aDown && !bDown) engaged_ = false;   // swallow until BOTH released
    }
    return false;
  }
  bool engaged() const { return engaged_; }

 private:
  uint32_t holdMs_, bothSinceMs_ = 0;
  bool bothDown_ = false, engaged_ = false, fired_ = false;
};
