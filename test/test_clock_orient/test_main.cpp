// Unit tests for logic/clock_orient_logic.h — the hysteresis orientation
// machine behind the clock face. Scenarios ported from CodeBuddy's
// firmware/tests/clock_orient_logic_test.cpp (axis-parameterized here:
// primary = the in-plane long axis, ax on our boards) and extended with
// enter/exit hysteresis, both lock modes and the direct 1<->3 fast flip.
#include <unity.h>
#include "logic/clock_orient_logic.h"

void setUp() {}
void tearDown() {}

struct Orient {
  uint8_t orient = 0;
  int8_t  frames = 0;
  int8_t  swap   = 0;
};

static void feed(Orient& o, float p, float s, float az, uint8_t lock, int n) {
  for (int i = 0; i < n; i++) {
    clockOrientUpdate(&o.orient, &o.frames, &o.swap, p, s, az, lock);
  }
}

void test_secondary_dominant_hold_stays_portrait() {
  Orient o;
  feed(o, 0.0f, 0.95f, 0.0f, 0, 16);   // gravity on the short axis: face up-ish
  TEST_ASSERT_EQUAL_UINT8(0, o.orient);
}

void test_primary_dominant_hold_enters_landscape_1() {
  Orient o;
  feed(o, 0.95f, 0.0f, 0.0f, 0, 16);   // on its long edge, BtnA side down
  TEST_ASSERT_EQUAL_UINT8(1, o.orient);
}

void test_negative_primary_enters_landscape_3() {
  Orient o;
  feed(o, -0.95f, 0.0f, 0.0f, 0, 16);  // other long edge: USB side down
  TEST_ASSERT_EQUAL_UINT8(3, o.orient);
}

void test_enter_needs_sustained_frames() {
  Orient o;
  feed(o, 0.95f, 0.0f, 0.0f, 0, 14);   // one frame short of the >=15 gate
  TEST_ASSERT_EQUAL_UINT8(0, o.orient);
  feed(o, 0.95f, 0.0f, 0.0f, 0, 1);
  TEST_ASSERT_EQUAL_UINT8(1, o.orient);
}

void test_tilted_entry_is_rejected_but_hold_tolerated() {
  Orient o;
  // |primary| at 0.6 is not "clearly sideways" — never enters landscape.
  feed(o, 0.6f, 0.0f, 0.0f, 0, 40);
  TEST_ASSERT_EQUAL_UINT8(0, o.orient);
  // But once in landscape, the same 0.6 counts as staying (loose threshold).
  Orient p;
  feed(p, 0.95f, 0.0f, 0.0f, 0, 20);
  TEST_ASSERT_EQUAL_UINT8(1, p.orient);
  feed(p, 0.6f, 0.0f, 0.0f, 0, 40);
  TEST_ASSERT_EQUAL_UINT8(1, p.orient);
}

void test_exit_hysteresis_back_to_portrait() {
  Orient o;
  feed(o, 0.95f, 0.0f, 0.0f, 0, 25);   // frames cap at 20
  TEST_ASSERT_EQUAL_UINT8(1, o.orient);
  feed(o, 0.0f, 0.0f, 0.95f, 0, 10);   // flat again — not enough yet
  TEST_ASSERT_EQUAL_UINT8(1, o.orient);
  feed(o, 0.0f, 0.0f, 0.95f, 0, 25);   // counter walks 20 -> -8
  TEST_ASSERT_EQUAL_UINT8(0, o.orient);
}

void test_fast_flip_swaps_1_to_3_directly() {
  Orient o;
  feed(o, 0.95f, 0.0f, 0.0f, 0, 20);
  TEST_ASSERT_EQUAL_UINT8(1, o.orient);
  // Flipped end-over-end: |primary| never drops, only the sign changes.
  feed(o, -0.95f, 0.0f, 0.0f, 0, 7);
  TEST_ASSERT_EQUAL_UINT8(1, o.orient);  // needs 8 agreeing frames
  feed(o, -0.95f, 0.0f, 0.0f, 0, 1);
  TEST_ASSERT_EQUAL_UINT8(3, o.orient);
}

void test_lock_portrait_forces_0() {
  Orient o;
  o.orient = 3;
  feed(o, 0.95f, 0.0f, 0.0f, 1, 1);
  TEST_ASSERT_EQUAL_UINT8(0, o.orient);
}

void test_lock_landscape_never_drops_to_portrait() {
  Orient o;
  feed(o, 0.0f, 0.0f, 0.95f, 2, 1);    // flat on a desk, locked landscape
  TEST_ASSERT_EQUAL_UINT8(1, o.orient); // primary >= 0 picks rotation 1
  feed(o, 0.0f, 0.0f, 0.95f, 2, 40);
  TEST_ASSERT_EQUAL_UINT8(1, o.orient); // holds; no exit path under lock
}

void test_lock_landscape_still_picks_side_from_gravity() {
  Orient o;
  feed(o, -0.9f, 0.0f, 0.0f, 2, 1);
  TEST_ASSERT_EQUAL_UINT8(3, o.orient);
  feed(o, 0.9f, 0.0f, 0.0f, 2, 1);     // strong opposite tilt swaps sides
  TEST_ASSERT_EQUAL_UINT8(1, o.orient);
  feed(o, -0.3f, 0.0f, 0.0f, 2, 40);   // weak tilt never swaps
  TEST_ASSERT_EQUAL_UINT8(1, o.orient);
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_secondary_dominant_hold_stays_portrait);
  RUN_TEST(test_primary_dominant_hold_enters_landscape_1);
  RUN_TEST(test_negative_primary_enters_landscape_3);
  RUN_TEST(test_enter_needs_sustained_frames);
  RUN_TEST(test_tilted_entry_is_rejected_but_hold_tolerated);
  RUN_TEST(test_exit_hysteresis_back_to_portrait);
  RUN_TEST(test_fast_flip_swaps_1_to_3_directly);
  RUN_TEST(test_lock_portrait_forces_0);
  RUN_TEST(test_lock_landscape_never_drops_to_portrait);
  RUN_TEST(test_lock_landscape_still_picks_side_from_gravity);
  return UNITY_END();
}
