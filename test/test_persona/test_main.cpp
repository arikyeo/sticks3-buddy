// Unit tests for logic/persona_logic.h — the bridge->mood base-state
// mapping extracted from main.cpp. Assertion ideas ported from CodeBuddy's
// firmware/tests/persona_logic_test.cpp, adapted to THIS repo's semantics,
// which deliberately differ in two places:
//   - disconnected maps to P_IDLE, not P_SLEEP (sleep is owned by the
//     inactivity timer in main.cpp, not the bridge state), and
//   - P_BUSY needs 3+ running sessions, not 1+.
#include <unity.h>
#include "logic/persona_logic.h"

void setUp() {}
void tearDown() {}

static PersonaInputs in(bool conn, uint8_t run, uint8_t wait, bool done) {
  PersonaInputs i;
  i.connected = conn;
  i.sessionsRunning = run;
  i.sessionsWaiting = wait;
  i.recentlyCompleted = done;
  return i;
}

void test_disconnected_idles() {
  TEST_ASSERT_EQUAL_INT(P_IDLE, derivePersona(in(false, 0, 0, false)));
  // disconnect gate wins over everything else the bridge might claim
  TEST_ASSERT_EQUAL_INT(P_IDLE, derivePersona(in(false, 5, 2, true)));
}

void test_connected_quiet_is_idle() {
  TEST_ASSERT_EQUAL_INT(P_IDLE, derivePersona(in(true, 0, 0, false)));
}

void test_busy_needs_three_running() {
  TEST_ASSERT_EQUAL_INT(P_IDLE, derivePersona(in(true, 1, 0, false)));
  TEST_ASSERT_EQUAL_INT(P_IDLE, derivePersona(in(true, 2, 0, false)));
  TEST_ASSERT_EQUAL_INT(P_BUSY, derivePersona(in(true, 3, 0, false)));
  TEST_ASSERT_EQUAL_INT(P_BUSY, derivePersona(in(true, 9, 0, false)));
}

void test_completion_celebrates_over_busy() {
  TEST_ASSERT_EQUAL_INT(P_CELEBRATE, derivePersona(in(true, 0, 0, true)));
  TEST_ASSERT_EQUAL_INT(P_CELEBRATE, derivePersona(in(true, 3, 0, true)));
}

void test_waiting_wins_over_everything_connected() {
  TEST_ASSERT_EQUAL_INT(P_ATTENTION, derivePersona(in(true, 0, 1, false)));
  TEST_ASSERT_EQUAL_INT(P_ATTENTION, derivePersona(in(true, 3, 1, true)));
}

void test_state_names_track_enum_order() {
  TEST_ASSERT_EQUAL_STRING("sleep",     stateNames[P_SLEEP]);
  TEST_ASSERT_EQUAL_STRING("idle",      stateNames[P_IDLE]);
  TEST_ASSERT_EQUAL_STRING("busy",      stateNames[P_BUSY]);
  TEST_ASSERT_EQUAL_STRING("attention", stateNames[P_ATTENTION]);
  TEST_ASSERT_EQUAL_STRING("celebrate", stateNames[P_CELEBRATE]);
  TEST_ASSERT_EQUAL_STRING("dizzy",     stateNames[P_DIZZY]);
  TEST_ASSERT_EQUAL_STRING("heart",     stateNames[P_HEART]);
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_disconnected_idles);
  RUN_TEST(test_connected_quiet_is_idle);
  RUN_TEST(test_busy_needs_three_running);
  RUN_TEST(test_completion_celebrates_over_busy);
  RUN_TEST(test_waiting_wins_over_everything_connected);
  RUN_TEST(test_state_names_track_enum_order);
  return UNITY_END();
}
