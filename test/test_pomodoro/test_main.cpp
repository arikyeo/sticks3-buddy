// Unit tests for logic/pomodoro_logic.h — the pure pomodoro state machine:
// 25/5/15 cadence, 4-completed-focus long-break cycle, pause freezing the
// clock, skip semantics (skipped focus doesn't count), millis-wrap safety.
#include <unity.h>
#include "logic/pomodoro_logic.h"

void setUp() {}
void tearDown() {}

static const uint32_t MIN_MS = 60UL * 1000UL;

void test_init_is_idle_with_zero_remaining() {
  PomoState s;
  pomoInit(s);
  TEST_ASSERT_EQUAL_UINT8(POMO_IDLE, s.phase);
  TEST_ASSERT_EQUAL_UINT32(0, pomoRemainMs(s, 12345));
  TEST_ASSERT_EQUAL_UINT8(POMO_EV_NONE, pomoTick(s, 99999));
  TEST_ASSERT_EQUAL_UINT8(POMO_IDLE, s.phase);   // tick never self-starts
}

void test_start_enters_focus_with_full_25min() {
  PomoState s;
  pomoInit(s);
  pomoStartPause(s, 1000);
  TEST_ASSERT_EQUAL_UINT8(POMO_FOCUS, s.phase);
  TEST_ASSERT_EQUAL_UINT32(25 * MIN_MS, pomoRemainMs(s, 1000));
  TEST_ASSERT_EQUAL_UINT32(25 * MIN_MS - 5000, pomoRemainMs(s, 6000));
}

void test_focus_completes_into_short_break() {
  PomoState s;
  pomoInit(s);
  pomoStartPause(s, 0);
  TEST_ASSERT_EQUAL_UINT8(POMO_EV_NONE, pomoTick(s, 25 * MIN_MS - 1));
  TEST_ASSERT_EQUAL_UINT8(POMO_EV_FOCUS_DONE, pomoTick(s, 25 * MIN_MS));
  TEST_ASSERT_EQUAL_UINT8(POMO_BREAK, s.phase);
  TEST_ASSERT_EQUAL_UINT8(1, s.focusDone);
  TEST_ASSERT_EQUAL_UINT32(5 * MIN_MS, pomoRemainMs(s, 25 * MIN_MS));
}

void test_break_completes_back_into_focus() {
  PomoState s;
  pomoInit(s);
  pomoStartPause(s, 0);
  pomoTick(s, 25 * MIN_MS);                       // -> break
  TEST_ASSERT_EQUAL_UINT8(POMO_EV_BREAK_DONE, pomoTick(s, 30 * MIN_MS));
  TEST_ASSERT_EQUAL_UINT8(POMO_FOCUS, s.phase);
  TEST_ASSERT_EQUAL_UINT8(1, s.focusDone);        // count survives the break
}

void test_fourth_focus_earns_long_break_and_resets_cycle() {
  PomoState s;
  pomoInit(s);
  pomoStartPause(s, 0);
  uint32_t t = 0;
  for (int i = 0; i < 3; i++) {
    t += 25 * MIN_MS;
    TEST_ASSERT_EQUAL_UINT8(POMO_EV_FOCUS_DONE, pomoTick(s, t));
    TEST_ASSERT_EQUAL_UINT8(POMO_BREAK, s.phase);
    t += 5 * MIN_MS;
    TEST_ASSERT_EQUAL_UINT8(POMO_EV_BREAK_DONE, pomoTick(s, t));
  }
  TEST_ASSERT_EQUAL_UINT8(3, s.focusDone);
  t += 25 * MIN_MS;
  TEST_ASSERT_EQUAL_UINT8(POMO_EV_FOCUS_DONE, pomoTick(s, t));
  TEST_ASSERT_EQUAL_UINT8(POMO_LONG_BREAK, s.phase);
  TEST_ASSERT_EQUAL_UINT8(0, s.focusDone);        // cycle resets
  TEST_ASSERT_EQUAL_UINT32(15 * MIN_MS, pomoRemainMs(s, t));
  t += 15 * MIN_MS;
  TEST_ASSERT_EQUAL_UINT8(POMO_EV_BREAK_DONE, pomoTick(s, t));
  TEST_ASSERT_EQUAL_UINT8(POMO_FOCUS, s.phase);
}

void test_pause_freezes_remaining_and_resume_rebases() {
  PomoState s;
  pomoInit(s);
  pomoStartPause(s, 0);                            // start
  pomoStartPause(s, 10 * MIN_MS);                  // pause at 15:00 left
  TEST_ASSERT_TRUE(s.paused);
  TEST_ASSERT_EQUAL_UINT32(15 * MIN_MS, pomoRemainMs(s, 10 * MIN_MS));
  TEST_ASSERT_EQUAL_UINT32(15 * MIN_MS, pomoRemainMs(s, 90 * MIN_MS));  // frozen
  TEST_ASSERT_EQUAL_UINT8(POMO_EV_NONE, pomoTick(s, 90 * MIN_MS));     // no fire
  pomoStartPause(s, 100 * MIN_MS);                 // resume
  TEST_ASSERT_FALSE(s.paused);
  TEST_ASSERT_EQUAL_UINT32(15 * MIN_MS, pomoRemainMs(s, 100 * MIN_MS));
  TEST_ASSERT_EQUAL_UINT8(POMO_EV_FOCUS_DONE, pomoTick(s, 115 * MIN_MS));
}

void test_skipped_focus_does_not_count_toward_long_break() {
  PomoState s;
  pomoInit(s);
  pomoStartPause(s, 0);
  for (int i = 0; i < 8; i++) {                    // skip focus+break 4x over
    pomoSkip(s, 1000 + i);                         // focus -> break -> focus...
  }
  TEST_ASSERT_EQUAL_UINT8(POMO_FOCUS, s.phase);
  TEST_ASSERT_EQUAL_UINT8(0, s.focusDone);         // no free long break
}

void test_skip_break_starts_focus_immediately() {
  PomoState s;
  pomoInit(s);
  pomoStartPause(s, 0);
  pomoTick(s, 25 * MIN_MS);                        // -> break
  pomoSkip(s, 26 * MIN_MS);
  TEST_ASSERT_EQUAL_UINT8(POMO_FOCUS, s.phase);
  TEST_ASSERT_EQUAL_UINT32(25 * MIN_MS, pomoRemainMs(s, 26 * MIN_MS));
  TEST_ASSERT_EQUAL_UINT8(1, s.focusDone);         // completed count kept
}

void test_skip_from_idle_is_a_noop() {
  PomoState s;
  pomoInit(s);
  pomoSkip(s, 5000);
  TEST_ASSERT_EQUAL_UINT8(POMO_IDLE, s.phase);
}

void test_stop_resets_everything() {
  PomoState s;
  pomoInit(s);
  pomoStartPause(s, 0);
  pomoTick(s, 25 * MIN_MS);
  pomoStop(s);
  TEST_ASSERT_EQUAL_UINT8(POMO_IDLE, s.phase);
  TEST_ASSERT_EQUAL_UINT8(0, s.focusDone);
  TEST_ASSERT_FALSE(s.paused);
}

void test_survives_millis_wrap() {
  PomoState s;
  pomoInit(s);
  // Start 10 minutes before the uint32 tick wraps; finish after it.
  uint32_t start = (uint32_t)0u - 10 * MIN_MS;
  pomoStartPause(s, start);
  // 15 min in (5 min past the wrap): 10:00 left of the 25:00 focus.
  TEST_ASSERT_EQUAL_UINT32(10 * MIN_MS, pomoRemainMs(s, 5 * MIN_MS));
  TEST_ASSERT_EQUAL_UINT8(POMO_EV_FOCUS_DONE, pomoTick(s, 15 * MIN_MS));
  TEST_ASSERT_EQUAL_UINT8(POMO_BREAK, s.phase);
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_init_is_idle_with_zero_remaining);
  RUN_TEST(test_start_enters_focus_with_full_25min);
  RUN_TEST(test_focus_completes_into_short_break);
  RUN_TEST(test_break_completes_back_into_focus);
  RUN_TEST(test_fourth_focus_earns_long_break_and_resets_cycle);
  RUN_TEST(test_pause_freezes_remaining_and_resume_rebases);
  RUN_TEST(test_skipped_focus_does_not_count_toward_long_break);
  RUN_TEST(test_skip_break_starts_focus_immediately);
  RUN_TEST(test_skip_from_idle_is_a_noop);
  RUN_TEST(test_stop_resets_everything);
  RUN_TEST(test_survives_millis_wrap);
  return UNITY_END();
}
