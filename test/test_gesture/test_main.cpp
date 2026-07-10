// Unit tests for logic/gesture_logic.h — flip (z inversion held) and
// double-tap (two z spikes) detection, fed at the firmware's 50 ms IMU
// cadence. Pins the thresholds: 600 ms flip hold, 300 ms upright re-arm,
// 80..400 ms tap pairing window, post-fire lockout.
#include <unity.h>
#include "logic/gesture_logic.h"

void setUp() {}
void tearDown() {}

// Feed a constant az for a span at 50 ms cadence; return the first
// non-none event (and the tick it fired at, through *firedAt).
static GestureEvent feedSpan(GestureState& g, float az, uint32_t fromMs,
                             uint32_t toMs, uint32_t* firedAt = nullptr) {
  GestureEvent got = GE_NONE;
  for (uint32_t t = fromMs; t <= toMs; t += 50) {
    GestureEvent e = gestureFeed(g, 0.0f, 0.0f, az, t);
    if (e != GE_NONE && got == GE_NONE) {
      got = e;
      if (firedAt) *firedAt = t;
    }
  }
  return got;
}

void test_flip_fires_after_hold() {
  GestureState g;
  gestureInit(g);
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, feedSpan(g, 1.0f, 0, 500));      // arm upright
  uint32_t at = 0;
  TEST_ASSERT_EQUAL_UINT8(GE_FLIP, feedSpan(g, -1.0f, 550, 1400, &at));
  TEST_ASSERT_TRUE(at >= 550 + GE_FLIP_HOLD_MS);                    // not early
}

void test_flip_fires_once_until_rearmed() {
  GestureState g;
  gestureInit(g);
  feedSpan(g, 1.0f, 0, 500);
  TEST_ASSERT_EQUAL_UINT8(GE_FLIP, feedSpan(g, -1.0f, 550, 1400));
  // Still inverted for three more seconds: no repeat fire.
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, feedSpan(g, -1.0f, 1450, 4400));
  // Brief upright blip (< 300 ms) does not re-arm...
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, feedSpan(g, 1.0f, 4450, 4600));
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, feedSpan(g, -1.0f, 4650, 5600));
  // ...a real upright spell does.
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, feedSpan(g, 1.0f, 5650, 6100));
  TEST_ASSERT_EQUAL_UINT8(GE_FLIP, feedSpan(g, -1.0f, 6150, 7100));
}

void test_flip_needs_full_hold() {
  GestureState g;
  gestureInit(g);
  feedSpan(g, 1.0f, 0, 500);
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, feedSpan(g, -1.0f, 550, 1100));  // 550 ms < 600
}

void test_no_flip_when_booted_inverted() {
  GestureState g;
  gestureInit(g);
  // Never upright -> never armed -> face-down from boot stays silent.
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, feedSpan(g, -1.0f, 0, 3000));
}

void test_double_tap_fires_within_window() {
  GestureState g;
  gestureInit(g);
  feedSpan(g, 1.0f, 0, 950);                                        // settle EMA
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, 1.6f, 1000));   // tap 1
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, 1.0f, 1050));
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, 1.0f, 1100));
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, 1.0f, 1150));
  TEST_ASSERT_EQUAL_UINT8(GE_DOUBLE_TAP, gestureFeed(g, 0, 0, 1.6f, 1200));  // tap 2
}

void test_single_tap_and_slow_pair_do_not_fire() {
  GestureState g;
  gestureInit(g);
  feedSpan(g, 1.0f, 0, 950);
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, 1.6f, 1000));
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, feedSpan(g, 1.0f, 1050, 1900));  // window expires
  // A second tap 900 ms later starts a NEW pair instead of firing.
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, 1.6f, 1950));
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, feedSpan(g, 1.0f, 2000, 2600));
}

void test_ring_down_debounce_is_not_a_second_tap() {
  GestureState g;
  gestureInit(g);
  feedSpan(g, 1.0f, 0, 950);
  // One hard tap whose impact rings across consecutive 50 ms samples:
  // every follow-up spike is inside the 80 ms min gap, so no pair fires.
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, 2.0f, 1000));
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, 0.2f, 1050));
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, 1.8f, 1100));
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, 0.4f, 1150));
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, feedSpan(g, 1.0f, 1200, 1700));
}

void test_lockout_after_fire() {
  GestureState g;
  gestureInit(g);
  feedSpan(g, 1.0f, 0, 950);
  gestureFeed(g, 0, 0, 1.6f, 1000);
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, 1.0f, 1050));
  gestureFeed(g, 0, 0, 1.0f, 1100);
  gestureFeed(g, 0, 0, 1.0f, 1150);
  TEST_ASSERT_EQUAL_UINT8(GE_DOUBLE_TAP, gestureFeed(g, 0, 0, 1.6f, 1200));
  // Locked out: even big isolated spikes can't fire again yet.
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, 2.5f, 1350));
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, 2.5f, 1500));
}

void test_flip_motion_does_not_double_tap() {
  GestureState g;
  gestureInit(g);
  feedSpan(g, 1.0f, 0, 500);
  // The inversion itself is one big z excursion: its first sample opens a
  // tap window, the ring of converging samples is debounced, the window
  // expires — the only event out of a flip is the flip.
  uint32_t at = 0;
  GestureEvent e = feedSpan(g, -1.0f, 550, 1400, &at);
  TEST_ASSERT_EQUAL_UINT8(GE_FLIP, e);
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_flip_fires_after_hold);
  RUN_TEST(test_flip_fires_once_until_rearmed);
  RUN_TEST(test_flip_needs_full_hold);
  RUN_TEST(test_no_flip_when_booted_inverted);
  RUN_TEST(test_double_tap_fires_within_window);
  RUN_TEST(test_single_tap_and_slow_pair_do_not_fire);
  RUN_TEST(test_ring_down_debounce_is_not_a_second_tap);
  RUN_TEST(test_lockout_after_fire);
  RUN_TEST(test_flip_motion_does_not_double_tap);
  return UNITY_END();
}
