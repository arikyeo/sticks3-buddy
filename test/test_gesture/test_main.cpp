// Unit tests for logic/gesture_logic.h — flip-and-return (inverted dip that
// fires on the RETURN to upright) and double-tap (two z spikes) detection,
// fed at the firmware's 50 ms IMU cadence. Pins the thresholds: inverted
// dwell 150..1500 ms, round trip <=2500 ms, 800 ms post-fire lockout;
// 80..400 ms tap pairing window, tap's own post-fire lockout.
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

// gesture_logic.h has no notion of prompts — that gate lives in main.cpp's
// call site (`ge == GE_FLIP && ... && !tama.promptId[0] && ...`), which
// can't be linked into this native/host test. Mirror it locally so the
// wiring contract is still pinned: the detector emits GE_FLIP from IMU
// state alone, and whether that turns into an action is the caller's call.
static bool wouldPinHost(GestureEvent ge, bool promptPending) {
  return ge == GE_FLIP && !promptPending;
}

// ---- flip: fires on the return to upright, not mid-hold --------------------

void test_flip_fires_on_return() {
  GestureState g;
  gestureInit(g);
  feedSpan(g, 1.0f, 0, 100);                                  // settle upright, arm
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, -0.9f, 150));  // dip inverted
  // Still nothing while it's down — the old design fired here; this one
  // waits for the return.
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, -0.9f, 300));
  TEST_ASSERT_EQUAL_UINT8(GE_FLIP, gestureFeed(g, 0, 0, 1.0f, 500));   // 350 ms dwell, fires
}

void test_flip_dwell_too_short_no_fire() {
  GestureState g;
  gestureInit(g);
  feedSpan(g, 1.0f, 0, 100);
  gestureFeed(g, 0, 0, -0.9f, 150);
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, 1.0f, 250));  // 100 ms < 150 min
}

void test_flip_dwell_too_long_no_fire() {
  GestureState g;
  gestureInit(g);
  feedSpan(g, 1.0f, 0, 100);
  gestureFeed(g, 0, 0, -0.9f, 150);
  // 1800 ms inverted: past the 1500 ms ceiling — set down, not flipped.
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, 1.0f, 1950));
}

void test_flip_roundtrip_too_slow_no_fire() {
  GestureState g;
  gestureInit(g);
  feedSpan(g, 1.0f, 0, 100);
  // Leaves upright into the dead zone and dawdles there a long time before
  // ever reaching the inverted threshold — a slow deliberate tilt, not a
  // flip. The eventual dip-and-return has a perfectly valid 200 ms dwell,
  // but the outer round trip (upright to upright) blows the 2500 ms cap,
  // so this must still not fire.
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, 0.0f, 300));    // dead zone, leaves baseline
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, -0.9f, 2700));  // finally inverted
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, 1.0f, 2900));   // 200 ms dwell, 2600 ms round trip
}

void test_flip_prompt_pending_suppressed() {
  GestureState g;
  gestureInit(g);
  feedSpan(g, 1.0f, 0, 100);
  gestureFeed(g, 0, 0, -0.9f, 150);
  GestureEvent ge = gestureFeed(g, 0, 0, 1.0f, 500);
  TEST_ASSERT_EQUAL_UINT8(GE_FLIP, ge);       // detector fires regardless of prompt state
  TEST_ASSERT_FALSE(wouldPinHost(ge, true));  // prompt pending -> call site suppresses
  TEST_ASSERT_TRUE(wouldPinHost(ge, false));  // no prompt -> call site would act
}

void test_flip_lockout_after_fire() {
  GestureState g;
  gestureInit(g);
  feedSpan(g, 1.0f, 0, 100);
  gestureFeed(g, 0, 0, -0.9f, 150);
  TEST_ASSERT_EQUAL_UINT8(GE_FLIP, gestureFeed(g, 0, 0, 1.0f, 500));   // fires, lockout until 1300

  // A second, otherwise-valid round trip landing inside the 800 ms
  // lockout (dwell 300 ms, done by 850) must not fire.
  gestureFeed(g, 0, 0, -0.9f, 550);
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, 1.0f, 850));

  // Once the lockout has expired, a fresh valid round trip fires again.
  gestureFeed(g, 0, 0, -0.9f, 900);
  TEST_ASSERT_EQUAL_UINT8(GE_FLIP, gestureFeed(g, 0, 0, 1.0f, 1350));
}

void test_flip_nap_scenario_sustained_facedown_never_fires() {
  GestureState g;
  gestureInit(g);
  feedSpan(g, 1.0f, 0, 100);
  gestureFeed(g, 0, 0, -0.95f, 150);   // goes face-down...
  // ...and stays there for 8 seconds (a real nap, not a flip). Nothing
  // fires while it's down — same as any other inverted span — and this
  // main.cpp-independent dwell cap is exactly why nap and flip-and-return
  // can't collide: by the time main.cpp's own sustained-face-down frame
  // counter would even consider starting a nap, this detector has long
  // since timed the excursion out.
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, feedSpan(g, -0.95f, 200, 8000));
  // Eventually picked back up: dwell (~7900 ms) blows the 1500 ms ceiling.
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, 1.0f, 8050));
}

void test_no_flip_when_booted_inverted() {
  GestureState g;
  gestureInit(g);
  // Never armed (no upright baseline seen yet) -> a dip-and-return right
  // from boot doesn't count, even with an otherwise-valid dwell.
  gestureFeed(g, 0, 0, -0.9f, 0);
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, 1.0f, 400));
  // It self-heals the moment it's seen upright once: the very next flip
  // fires normally.
  gestureFeed(g, 0, 0, -0.9f, 450);
  TEST_ASSERT_EQUAL_UINT8(GE_FLIP, gestureFeed(g, 0, 0, 1.0f, 750));
}

void test_flip_motion_does_not_double_tap() {
  GestureState g;
  gestureInit(g);
  feedSpan(g, 1.0f, 0, 100);
  // The dip is one big z excursion, but `armed` (the tap-pairing gate)
  // goes false the instant it leaves upright, so the dip can't open a tap
  // window; the only event out of a flip-and-return is the flip, on the
  // return sample.
  TEST_ASSERT_EQUAL_UINT8(GE_NONE, gestureFeed(g, 0, 0, -0.9f, 150));
  TEST_ASSERT_EQUAL_UINT8(GE_FLIP, gestureFeed(g, 0, 0, 1.0f, 500));
}

// ---- double tap: unchanged ------------------------------------------------

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

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_flip_fires_on_return);
  RUN_TEST(test_flip_dwell_too_short_no_fire);
  RUN_TEST(test_flip_dwell_too_long_no_fire);
  RUN_TEST(test_flip_roundtrip_too_slow_no_fire);
  RUN_TEST(test_flip_prompt_pending_suppressed);
  RUN_TEST(test_flip_lockout_after_fire);
  RUN_TEST(test_flip_nap_scenario_sustained_facedown_never_fires);
  RUN_TEST(test_no_flip_when_booted_inverted);
  RUN_TEST(test_flip_motion_does_not_double_tap);
  RUN_TEST(test_double_tap_fires_within_window);
  RUN_TEST(test_single_tap_and_slow_pair_do_not_fire);
  RUN_TEST(test_ring_down_debounce_is_not_a_second_tap);
  RUN_TEST(test_lockout_after_fire);
  return UNITY_END();
}
