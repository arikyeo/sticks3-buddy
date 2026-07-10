// Unit tests for src/session_table.h — the v2 per-session table: clamped
// order-preserving ingest, sid-keyed selection that survives reorders, and
// the focus pin.
#include <unity.h>
#include <string.h>
#include "session_table.h"

void setUp() {}
void tearDown() {}

static Session mk(const char* sid, const char* title, uint8_t state) {
  Session s;
  memset(&s, 0, sizeof(s));
  strncpy(s.sid, sid, sizeof(s.sid) - 1);
  strncpy(s.title, title, sizeof(s.title) - 1);
  strncpy(s.agent, "claude", sizeof(s.agent) - 1);
  s.state = state;
  return s;
}

void test_state_mapping() {
  TEST_ASSERT_EQUAL_UINT8(SES_RUN,  sessionStateFromStr("run"));
  TEST_ASSERT_EQUAL_UINT8(SES_WAIT, sessionStateFromStr("wait"));
  TEST_ASSERT_EQUAL_UINT8(SES_IDLE, sessionStateFromStr("idle"));
  TEST_ASSERT_EQUAL_UINT8(SES_DONE, sessionStateFromStr("done"));
  TEST_ASSERT_EQUAL_UINT8(SES_IDLE, sessionStateFromStr("weird"));
  TEST_ASSERT_EQUAL_UINT8(SES_IDLE, sessionStateFromStr(0));
}

void test_commit_preserves_order_and_clamps() {
  SessionTable t;
  sessionTabReset(&t);
  Session in[BUDDY_MAX_SESSIONS + 2];
  char sid[12];
  for (int i = 0; i < BUDDY_MAX_SESSIONS + 2; i++) {
    snprintf(sid, sizeof(sid), "cli:%d", i);
    in[i] = mk(sid, "t", (uint8_t)SES_RUN);
  }
  sessionTabCommit(&t, in, (uint8_t)(BUDDY_MAX_SESSIONS + 2));
  TEST_ASSERT_EQUAL_UINT(BUDDY_MAX_SESSIONS, t.count);
  TEST_ASSERT_EQUAL_STRING("cli:0", t.s[0].sid);   // order preserved, no re-sort
  TEST_ASSERT_EQUAL_STRING("cli:1", t.s[1].sid);
}

void test_selection_survives_reorder_by_sid() {
  SessionTable t;
  sessionTabReset(&t);
  Session in[3] = { mk("a", "A", SES_RUN), mk("b", "B", SES_IDLE), mk("c", "C", SES_IDLE) };
  sessionTabCommit(&t, in, 3);
  TEST_ASSERT_EQUAL_INT(0, sessionTabSelIdx(&t));   // default = row 0
  sessionTabSelectNext(&t);                         // select "b"
  TEST_ASSERT_EQUAL_INT(1, sessionTabSelIdx(&t));
  // host resorts waiting-first: b jumps to the front
  Session re[3] = { mk("b", "B", SES_WAIT), mk("a", "A", SES_RUN), mk("c", "C", SES_IDLE) };
  sessionTabCommit(&t, re, 3);
  TEST_ASSERT_EQUAL_INT(0, sessionTabSelIdx(&t));   // still on "b"
  TEST_ASSERT_EQUAL_STRING("b", t.s[sessionTabSelIdx(&t)].sid);
}

void test_selection_stale_sid_falls_back_to_zero() {
  SessionTable t;
  sessionTabReset(&t);
  Session in[2] = { mk("a", "A", SES_RUN), mk("b", "B", SES_IDLE) };
  sessionTabCommit(&t, in, 2);
  sessionTabSelectNext(&t);   // "b"
  Session re[1] = { mk("a", "A", SES_RUN) };
  sessionTabCommit(&t, re, 1);
  TEST_ASSERT_EQUAL_INT(0, sessionTabSelIdx(&t));
  sessionTabReset(&t);
  TEST_ASSERT_EQUAL_INT(-1, sessionTabSelIdx(&t));  // empty table
}

void test_select_next_wraps() {
  SessionTable t;
  sessionTabReset(&t);
  Session in[2] = { mk("a", "A", SES_RUN), mk("b", "B", SES_IDLE) };
  sessionTabCommit(&t, in, 2);
  sessionTabSelectNext(&t);
  sessionTabSelectNext(&t);
  TEST_ASSERT_EQUAL_INT(0, sessionTabSelIdx(&t));   // wrapped to "a"
}

void test_pin_toggle_and_persistence_across_commits() {
  SessionTable t;
  sessionTabReset(&t);
  Session in[2] = { mk("a", "A", SES_RUN), mk("b", "B", SES_WAIT) };
  sessionTabCommit(&t, in, 2);
  TEST_ASSERT_EQUAL_INT(-1, sessionTabPinIdx(&t));
  TEST_ASSERT_TRUE(sessionTabTogglePin(&t));        // pin "a"
  TEST_ASSERT_EQUAL_INT(0, sessionTabPinIdx(&t));
  // pin is retained when the sid momentarily vanishes (host clamp/restart)
  Session re[1] = { mk("b", "B", SES_WAIT) };
  sessionTabCommit(&t, re, 1);
  TEST_ASSERT_EQUAL_INT(-1, sessionTabPinIdx(&t));
  TEST_ASSERT_EQUAL_STRING("a", t.pinSid);
  Session back[2] = { mk("b", "B", SES_WAIT), mk("a", "A", SES_RUN) };
  sessionTabCommit(&t, back, 2);
  TEST_ASSERT_EQUAL_INT(1, sessionTabPinIdx(&t));   // self-healed
  // toggling on the pinned session unpins
  sessionTabSelectNext(&t);                         // b -> a? sel was "b"(stale->0)
  while (sessionTabSelIdx(&t) != sessionTabPinIdx(&t)) sessionTabSelectNext(&t);
  TEST_ASSERT_FALSE(sessionTabTogglePin(&t));
  TEST_ASSERT_EQUAL_INT(-1, sessionTabPinIdx(&t));
  TEST_ASSERT_EQUAL_STRING("", t.pinSid);
}

void test_by_sid_lookup() {
  SessionTable t;
  sessionTabReset(&t);
  Session in[2] = { mk("a", "Alpha", SES_RUN), mk("b", "Beta", SES_IDLE) };
  sessionTabCommit(&t, in, 2);
  const Session* s = sessionTabBySid(&t, "b");
  TEST_ASSERT_NOT_NULL(s);
  TEST_ASSERT_EQUAL_STRING("Beta", s->title);
  TEST_ASSERT_NULL(sessionTabBySid(&t, "zz"));
  TEST_ASSERT_NULL(sessionTabBySid(&t, ""));
  TEST_ASSERT_NULL(sessionTabBySid(&t, (const char*)0));
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_state_mapping);
  RUN_TEST(test_commit_preserves_order_and_clamps);
  RUN_TEST(test_selection_survives_reorder_by_sid);
  RUN_TEST(test_selection_stale_sid_falls_back_to_zero);
  RUN_TEST(test_select_next_wraps);
  RUN_TEST(test_pin_toggle_and_persistence_across_commits);
  RUN_TEST(test_by_sid_lookup);
  return UNITY_END();
}
