// Unit tests for logic/button_logic.h (debounced Button + A+B Chord).
// Ported from 23atomist's test/test_button/test_main.cpp (8968f26) with the
// include path adjusted to this repo's logic/ layout.
#include <unity.h>
#include "logic/button_logic.h"

void setUp() {}
void tearDown() {}

// Feed a button a constant level for a span of ms, 1ms steps.
static void feed(Button& b, bool level, uint32_t fromMs, uint32_t toMs) {
  for (uint32_t t = fromMs; t <= toMs; t++) b.update(level, t);
}

void test_glitch_is_debounced() {
  Button b;
  feed(b, false, 0, 10);
  feed(b, true, 11, 20);    // 9ms blip < DEBOUNCE_MS
  feed(b, false, 21, 60);
  TEST_ASSERT_FALSE(b.isPressed());
}

void test_press_edge_fires_once() {
  Button b;
  feed(b, false, 0, 10);
  feed(b, true, 11, 40);            // held well past DEBOUNCE_MS
  TEST_ASSERT_TRUE(b.isPressed());
  // wasPressed was true on exactly one update; by now it must be false
  TEST_ASSERT_FALSE(b.wasPressed());
  int edges = 0;
  Button c;
  for (uint32_t t = 0; t <= 100; t++) {
    c.update(t >= 20, t);
    if (c.wasPressed()) edges++;
  }
  TEST_ASSERT_EQUAL_INT(1, edges);
}

void test_release_edge_fires_once() {
  Button b;
  int edges = 0;
  for (uint32_t t = 0; t <= 200; t++) {
    b.update(t >= 20 && t <= 120, t);
    if (b.wasReleased()) edges++;
  }
  TEST_ASSERT_EQUAL_INT(1, edges);
  TEST_ASSERT_FALSE(b.isPressed());
}

void test_pressed_for_threshold() {
  Button b;
  feed(b, true, 0, 500);
  TEST_ASSERT_TRUE(b.pressedFor(400));
  TEST_ASSERT_FALSE(b.pressedFor(600));
  feed(b, false, 501, 560);
  TEST_ASSERT_FALSE(b.pressedFor(1));   // released — never true
}

void test_chord_fires_once_at_hold() {
  Chord ch(2000);
  int fires = 0;
  for (uint32_t t = 0; t <= 5000; t++) {
    if (ch.update(t >= 100, t >= 150, t)) fires++;
  }
  TEST_ASSERT_EQUAL_INT(1, fires);
}

void test_chord_short_hold_never_fires() {
  Chord ch(2000);
  int fires = 0;
  for (uint32_t t = 0; t <= 5000; t++) {
    bool both = t >= 100 && t <= 1500;   // 1.4s < 2s
    if (ch.update(both, both, t)) fires++;
  }
  TEST_ASSERT_EQUAL_INT(0, fires);
}

void test_chord_engaged_until_both_released() {
  Chord ch(2000);
  // A down at 100, B down at 200, B up at 300, A up at 400
  for (uint32_t t = 0; t <= 500; t++) {
    ch.update(t >= 100 && t < 400, t >= 200 && t < 300, t);
    if (t == 250) TEST_ASSERT_TRUE(ch.engaged());
    if (t == 350) TEST_ASSERT_TRUE(ch.engaged());   // A still down — keep swallowing
    if (t == 450) TEST_ASSERT_FALSE(ch.engaged());  // both released
  }
}

void test_chord_refires_on_second_engagement() {
  Chord ch(2000);
  int fires = 0;
  for (uint32_t t = 0; t <= 10000; t++) {
    bool both = (t >= 0 && t <= 2500) || (t >= 5000 && t <= 7500);
    if (ch.update(both, both, t)) fires++;
  }
  TEST_ASSERT_EQUAL_INT(2, fires);
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_glitch_is_debounced);
  RUN_TEST(test_press_edge_fires_once);
  RUN_TEST(test_release_edge_fires_once);
  RUN_TEST(test_pressed_for_threshold);
  RUN_TEST(test_chord_fires_once_at_hold);
  RUN_TEST(test_chord_short_hold_never_fires);
  RUN_TEST(test_chord_engaged_until_both_released);
  RUN_TEST(test_chord_refires_on_second_engagement);
  return UNITY_END();
}
