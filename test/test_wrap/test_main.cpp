// Unit tests for logic/wrap_text_logic.h — greedy word-wrap into fixed
// 24-byte rows, including the underflow hardening from johnwong 3fa0884
// (bridge-controlled no-space runs must never memcpy past a row).
#include <unity.h>
#include <string.h>
#include "logic/wrap_text_logic.h"

void setUp() {}
void tearDown() {}

void test_single_row_fits() {
  char rows[4][24];
  memset(rows, 0, sizeof(rows));
  uint8_t n = wrapInto("hello world", rows, 4, 11);
  TEST_ASSERT_EQUAL_UINT(1, n);
  TEST_ASSERT_EQUAL_STRING("hello world", rows[0]);
}

void test_wrap_gets_continuation_indent() {
  char rows[4][24];
  memset(rows, 0, sizeof(rows));
  uint8_t n = wrapInto("aaaa bbbb", rows, 4, 5);
  TEST_ASSERT_EQUAL_UINT(2, n);
  TEST_ASSERT_EQUAL_STRING("aaaa", rows[0]);
  TEST_ASSERT_EQUAL_STRING(" bbbb", rows[1]);   // leading space = indent
}

void test_leading_and_double_spaces_collapse() {
  char rows[4][24];
  memset(rows, 0, sizeof(rows));
  uint8_t n = wrapInto("  a  b", rows, 4, 10);
  TEST_ASSERT_EQUAL_UINT(1, n);
  TEST_ASSERT_EQUAL_STRING("a b", rows[0]);
}

void test_oversized_word_hard_breaks() {
  char rows[6][24];
  memset(rows, 0, sizeof(rows));
  uint8_t n = wrapInto("abcdefghij", rows, 6, 5);
  // Current behavior: the wrap-to-new-row happens before the hard-break
  // loop, so an oversized FIRST word leaves an empty row 0 behind.
  TEST_ASSERT_EQUAL_UINT(4, n);
  TEST_ASSERT_EQUAL_STRING("",      rows[0]);
  TEST_ASSERT_EQUAL_STRING(" abcd", rows[1]);
  TEST_ASSERT_EQUAL_STRING(" efgh", rows[2]);
  TEST_ASSERT_EQUAL_STRING(" ij",   rows[3]);
}

void test_max_rows_is_respected() {
  char rows[2][24];
  memset(rows, 0, sizeof(rows));
  uint8_t n = wrapInto("one two three four five six", rows, 2, 4);
  TEST_ASSERT_EQUAL_UINT(2, n);
  TEST_ASSERT_EQUAL_STRING("one",  rows[0]);
  TEST_ASSERT_EQUAL_STRING(" two", rows[1]);
}

// Regression for the 3fa0884 underflow class: a long no-space run (exactly
// what hostile/CJK bridge transcript text looks like post-sanitize) with an
// oversized width must clamp to the 23-char row payload, never write past
// the requested row count, and NUL-terminate every produced row.
void test_hostile_run_clamps_and_stays_in_bounds() {
  char rows[9][24];                       // 8 usable + 1 sentinel row
  memset(rows, 0, sizeof(rows));
  memset(rows[8], 0x5A, 24);              // canary past maxRows
  char in[101];
  memset(in, 'A', 100);
  in[100] = 0;
  uint8_t n = wrapInto(in, rows, 8, 200);   // width clamps to 23 internally
  TEST_ASSERT_TRUE(n >= 1 && n <= 8);
  for (uint8_t i = 0; i < n; i++) {
    TEST_ASSERT_TRUE(strlen(rows[i]) <= 23);   // includes the NUL check
  }
  for (int k = 0; k < 24; k++) {
    TEST_ASSERT_EQUAL_HEX8(0x5A, (uint8_t)rows[8][k]);   // canary intact
  }
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_single_row_fits);
  RUN_TEST(test_wrap_gets_continuation_indent);
  RUN_TEST(test_leading_and_double_spaces_collapse);
  RUN_TEST(test_oversized_word_hard_breaks);
  RUN_TEST(test_max_rows_is_respected);
  RUN_TEST(test_hostile_run_clamps_and_stays_in_bounds);
  return UNITY_END();
}
