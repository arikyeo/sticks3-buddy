// Unit tests for logic/clock_display_logic.h — placeholder-hardened clock
// face formatting. Known-value assertions ported from CodeBuddy's
// firmware/tests/clock_display_logic_test.cpp, converted to Unity and
// extended with the weekday+date line and the label range boundaries.
#include <unity.h>
#include <string.h>
#include "logic/clock_display_logic.h"

void setUp() {}
void tearDown() {}

void test_format_hm_valid() {
  char b[16];
  clockFormatHm(b, sizeof(b), 9, 5);
  TEST_ASSERT_EQUAL_STRING("09:05", b);
  clockFormatHm(b, sizeof(b), 23, 59);
  TEST_ASSERT_EQUAL_STRING("23:59", b);
  clockFormatHm(b, sizeof(b), 0, 0);
  TEST_ASSERT_EQUAL_STRING("00:00", b);
}

void test_format_hm_out_of_range_renders_placeholder() {
  char b[16];
  clockFormatHm(b, sizeof(b), -1, 42);   // pre-sync garbage, not "4294967295"
  TEST_ASSERT_EQUAL_STRING("--:--", b);
  clockFormatHm(b, sizeof(b), 24, 0);
  TEST_ASSERT_EQUAL_STRING("--:--", b);
  clockFormatHm(b, sizeof(b), 12, 60);
  TEST_ASSERT_EQUAL_STRING("--:--", b);
}

void test_format_seconds() {
  char b[8];
  clockFormatSeconds(b, sizeof(b), 42);
  TEST_ASSERT_EQUAL_STRING(":42", b);
  clockFormatSeconds(b, sizeof(b), 0);
  TEST_ASSERT_EQUAL_STRING(":00", b);
  clockFormatSeconds(b, sizeof(b), -1);
  TEST_ASSERT_EQUAL_STRING(":--", b);
  clockFormatSeconds(b, sizeof(b), 60);
  TEST_ASSERT_EQUAL_STRING(":--", b);
}

void test_format_date_line() {
  char b[16];
  clockFormatDateLine(b, sizeof(b), 4, 20);
  TEST_ASSERT_EQUAL_STRING("Apr 20", b);
  clockFormatDateLine(b, sizeof(b), 12, 1);
  TEST_ASSERT_EQUAL_STRING("Dec 01", b);
  clockFormatDateLine(b, sizeof(b), -1, 20);
  TEST_ASSERT_EQUAL_STRING("--- --", b);
  clockFormatDateLine(b, sizeof(b), 13, 20);
  TEST_ASSERT_EQUAL_STRING("--- --", b);
  clockFormatDateLine(b, sizeof(b), 4, 0);
  TEST_ASSERT_EQUAL_STRING("--- --", b);
  clockFormatDateLine(b, sizeof(b), 4, 32);
  TEST_ASSERT_EQUAL_STRING("--- --", b);
}

void test_format_week_date_line() {
  char b[16];
  clockFormatWeekDateLine(b, sizeof(b), 0, 4, 20);
  TEST_ASSERT_EQUAL_STRING("Sun Apr 20", b);
  clockFormatWeekDateLine(b, sizeof(b), 6, 1, 3);
  TEST_ASSERT_EQUAL_STRING("Sat Jan 03", b);
}

void test_format_week_date_line_partial_garbage() {
  char b[16];
  // Bad weekday still renders the (valid) date; bad date keeps the weekday.
  clockFormatWeekDateLine(b, sizeof(b), 9, 4, 20);
  TEST_ASSERT_EQUAL_STRING("--- Apr 20", b);
  clockFormatWeekDateLine(b, sizeof(b), 2, 0, 20);
  TEST_ASSERT_EQUAL_STRING("Tue --- --", b);
}

void test_month_label_bounds() {
  TEST_ASSERT_EQUAL_STRING("Jan", clockMonthLabel(1));
  TEST_ASSERT_EQUAL_STRING("Dec", clockMonthLabel(12));
  TEST_ASSERT_EQUAL_STRING("---", clockMonthLabel(0));
  TEST_ASSERT_EQUAL_STRING("---", clockMonthLabel(13));
}

void test_weekday_label_bounds() {
  TEST_ASSERT_EQUAL_STRING("Sun", clockWeekdayLabel(0));
  TEST_ASSERT_EQUAL_STRING("Sat", clockWeekdayLabel(6));
  TEST_ASSERT_EQUAL_STRING("---", clockWeekdayLabel(-1));
  TEST_ASSERT_EQUAL_STRING("---", clockWeekdayLabel(7));
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_format_hm_valid);
  RUN_TEST(test_format_hm_out_of_range_renders_placeholder);
  RUN_TEST(test_format_seconds);
  RUN_TEST(test_format_date_line);
  RUN_TEST(test_format_week_date_line);
  RUN_TEST(test_format_week_date_line_partial_garbage);
  RUN_TEST(test_month_label_bounds);
  RUN_TEST(test_weekday_label_bounds);
  return UNITY_END();
}
