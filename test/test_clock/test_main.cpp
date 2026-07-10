// Unit tests for logic/clock_time_logic.h — pure proleptic-Gregorian
// epoch<->tm math plus the millis-projected SoftClock. Known-value
// assertions ported from CodeBuddy's firmware/tests/clock_time_logic_test.cpp
// (epoch 0 decodes to 1970-01-01 Thu 00:00:00; 12:34:56 decodes hh/mm/ss),
// adapted to this repo's struct tm API and extended with round-trips,
// a leap-day anchor and millis()-wrap projection.
#include <unity.h>
#include <string.h>
#include "logic/clock_time_logic.h"

void setUp() {}
void tearDown() {}

static struct tm mk(int y, int mo, int d, int h, int mi, int s) {
  struct tm t;
  memset(&t, 0, sizeof(t));
  t.tm_year = y - 1900;
  t.tm_mon  = mo - 1;
  t.tm_mday = d;
  t.tm_hour = h;
  t.tm_min  = mi;
  t.tm_sec  = s;
  return t;
}

void test_epoch_zero_decodes_to_1970_thursday() {
  struct tm out;
  clockEpochToTmUtc(0, &out);
  TEST_ASSERT_EQUAL_INT(70, out.tm_year);   // 1970
  TEST_ASSERT_EQUAL_INT(0,  out.tm_mon);    // January
  TEST_ASSERT_EQUAL_INT(1,  out.tm_mday);
  TEST_ASSERT_EQUAL_INT(4,  out.tm_wday);   // Thursday
  TEST_ASSERT_EQUAL_INT(0,  out.tm_hour);
  TEST_ASSERT_EQUAL_INT(0,  out.tm_min);
  TEST_ASSERT_EQUAL_INT(0,  out.tm_sec);
}

void test_hms_decode_within_first_day() {
  struct tm out;
  clockEpochToTmUtc(12 * 3600 + 34 * 60 + 56, &out);
  TEST_ASSERT_EQUAL_INT(12, out.tm_hour);
  TEST_ASSERT_EQUAL_INT(34, out.tm_min);
  TEST_ASSERT_EQUAL_INT(56, out.tm_sec);
  TEST_ASSERT_EQUAL_INT(1,  out.tm_mday);   // still Jan 1
}

void test_encode_epoch_zero() {
  struct tm t = mk(1970, 1, 1, 0, 0, 0);
  TEST_ASSERT_EQUAL_INT(0, (long)clockTmToEpochUtc(&t));
}

void test_leap_day_2024_known_epoch_and_weekday() {
  // 2024-02-29 00:00:00 UTC == 1709164800, a Thursday.
  struct tm t = mk(2024, 2, 29, 0, 0, 0);
  TEST_ASSERT_EQUAL_INT(1709164800L, (long)clockTmToEpochUtc(&t));
  struct tm out;
  clockEpochToTmUtc((time_t)1709164800L, &out);
  TEST_ASSERT_EQUAL_INT(124, out.tm_year);
  TEST_ASSERT_EQUAL_INT(1,   out.tm_mon);    // February
  TEST_ASSERT_EQUAL_INT(29,  out.tm_mday);
  TEST_ASSERT_EQUAL_INT(4,   out.tm_wday);   // Thursday
}

void test_round_trip_modern_datetime() {
  struct tm t = mk(2026, 7, 10, 15, 4, 5);
  time_t e = clockTmToEpochUtc(&t);
  struct tm out;
  clockEpochToTmUtc(e, &out);
  TEST_ASSERT_EQUAL_INT(t.tm_year, out.tm_year);
  TEST_ASSERT_EQUAL_INT(t.tm_mon,  out.tm_mon);
  TEST_ASSERT_EQUAL_INT(t.tm_mday, out.tm_mday);
  TEST_ASSERT_EQUAL_INT(t.tm_hour, out.tm_hour);
  TEST_ASSERT_EQUAL_INT(t.tm_min,  out.tm_min);
  TEST_ASSERT_EQUAL_INT(t.tm_sec,  out.tm_sec);
}

void test_soft_clock_invalid_until_seeded() {
  SoftClock c;
  softClockInit(c);
  struct tm out;
  TEST_ASSERT_FALSE(softClockGet(c, 12345, &out));
}

void test_soft_clock_projects_forward() {
  SoftClock c;
  softClockInit(c);
  struct tm seed = mk(2026, 7, 10, 12, 0, 0);
  softClockSeed(c, &seed, 1000);
  struct tm out;
  TEST_ASSERT_TRUE(softClockGet(c, 11000, &out));   // +10s of millis
  TEST_ASSERT_EQUAL_INT(12, out.tm_hour);
  TEST_ASSERT_EQUAL_INT(0,  out.tm_min);
  TEST_ASSERT_EQUAL_INT(10, out.tm_sec);
}

void test_soft_clock_survives_millis_wrap() {
  SoftClock c;
  softClockInit(c);
  struct tm seed = mk(2026, 7, 10, 12, 0, 0);
  // Seed 256ms before the uint32 tick wraps; read 1000ms after the wrap.
  softClockSeed(c, &seed, 0xFFFFFF00u);
  struct tm out;
  TEST_ASSERT_TRUE(softClockGet(c, 1000u, &out));   // elapsed = 1256ms -> +1s
  TEST_ASSERT_EQUAL_INT(12, out.tm_hour);
  TEST_ASSERT_EQUAL_INT(0,  out.tm_min);
  TEST_ASSERT_EQUAL_INT(1,  out.tm_sec);
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_epoch_zero_decodes_to_1970_thursday);
  RUN_TEST(test_hms_decode_within_first_day);
  RUN_TEST(test_encode_epoch_zero);
  RUN_TEST(test_leap_day_2024_known_epoch_and_weekday);
  RUN_TEST(test_round_trip_modern_datetime);
  RUN_TEST(test_soft_clock_invalid_until_seeded);
  RUN_TEST(test_soft_clock_projects_forward);
  RUN_TEST(test_soft_clock_survives_millis_wrap);
  return UNITY_END();
}
