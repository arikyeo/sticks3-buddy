#pragma once
#include <math.h>
#include <stdint.h>

// Clock orientation state machine, extracted from main.cpp's
// clockUpdateOrient and reconciled with CodeBuddy's
// firmware/src/clock_orient_logic.h (same algorithm and thresholds; theirs
// hardcodes the StickS3 axis mapping, this port takes the axes as
// parameters). Pure logic — no Arduino / M5 dependency — so it unit-tests
// on the native host (test/test_clock_orient).
//
//   *orient: 0 = portrait (sprite path, pet sleeps underneath)
//            1 = landscape, BtnA-side down (M5.Lcd rotation 1)
//            3 = landscape, USB-side down (M5.Lcd rotation 3)
//   primary   = the in-plane axis that goes gravity-dominant when the stick
//               lies on its long edge (ax on every supported board — M5Unified
//               normalizes IMU axes per board)
//   secondary = the other in-plane axis (ay)
//   lock      = settings clockRot: 0 auto, 1 force portrait,
//               2 force landscape (still picks 1 vs 3 from gravity)
//
// Dual enter/stay thresholds plus signed frame counters give hysteresis on
// both transitions — same pattern as the face-down nap. swapFrames watches
// for a fast 1<->3 flip, where |primary| never drops below the stay
// threshold so the exit-via-portrait path can't fire. All three counters
// are caller-owned so the clock surface can reset them when it deactivates.
inline void clockOrientUpdate(uint8_t* orient, int8_t* orientFrames,
                              int8_t* swapFrames, float primary,
                              float secondary, float az, uint8_t lock) {
  if (lock == 1) { *orient = 0; return; }

  if (lock == 2) {
    // Locked landscape: never drop to 0, but still pick 1 vs 3 from gravity
    // so the cradle works either way up. Need a strong tilt for the 1<->3
    // swap so handling jitter doesn't flip it; otherwise hold whatever we
    // last had (or 1 from boot).
    if (*orient == 0) *orient = (primary >= 0) ? 1 : 3;
    if      (primary >  0.5f && *orient != 1) *orient = 1;
    else if (primary < -0.5f && *orient != 3) *orient = 3;
    return;
  }

  // Dual threshold: strict to enter (must be clearly sideways), loose to
  // stay (tolerate ~65 degrees of tilt). With one shared threshold a slight
  // lean while sitting on the long edge puts primary right at the boundary
  // and the counter ratchets down in ~half a second.
  bool side = (*orient == 0)
    ? fabsf(primary) > 0.7f && fabsf(secondary) < 0.5f && fabsf(az) < 0.5f
    : fabsf(primary) > 0.4f;
  if (side) { if (*orientFrames < 20) (*orientFrames)++; }
  else      { if (*orientFrames > -10) (*orientFrames)--; }

  if (*orient == 0 && *orientFrames >= 15) {
    *orient = (primary > 0) ? 1 : 3;
  } else if (*orient != 0 && *orientFrames <= -8) {
    *orient = 0;
  } else if (*orient != 0 && side) {
    // Direct 1<->3: a fast flip keeps |primary| > 0.7 (just changes sign),
    // so `side` never drops and the exit-via-0 path can't fire. Watch for
    // the primary sign disagreeing with the stored orientation.
    uint8_t want = (primary > 0) ? 1 : 3;
    if (want != *orient) {
      if (++(*swapFrames) >= 8) { *orient = want; *swapFrames = 0; }
    } else {
      *swapFrames = 0;
    }
  }
}
