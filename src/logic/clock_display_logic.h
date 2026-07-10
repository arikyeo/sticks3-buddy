#pragma once
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

// Clock face formatting, ported from CodeBuddy's
// firmware/src/clock_display_logic.h (codebuddy/main) onto this repo's
// logic/ conventions. Pure logic — no Arduino / M5 dependency — so it
// unit-tests on the native host (test/test_clock_display).
//
// Every formatter range-checks its inputs and renders a placeholder
// ("--:--", "--- --") instead of printf'ing garbage: struct tm fields can
// hold anything before the first bridge time sync, and the old %02u casts
// turned a -1 into a 10-digit unsigned.

static const char CLOCK_MON[][4] = {
  "Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"
};

static const char CLOCK_DOW[][4] = {"Sun","Mon","Tue","Wed","Thu","Fri","Sat"};

inline bool clockValueInRange(int value, int minValue, int maxValue) {
  return value >= minValue && value <= maxValue;
}

// month is 1-based (tm_mon + 1), weekday 0-based Sunday-first (tm_wday).
inline const char* clockMonthLabel(int month) {
  return clockValueInRange(month, 1, 12) ? CLOCK_MON[month - 1] : "---";
}

inline const char* clockWeekdayLabel(int weekday) {
  return clockValueInRange(weekday, 0, 6) ? CLOCK_DOW[weekday] : "---";
}

inline void clockFormatHm(char* out, size_t size, int hours, int minutes) {
  if (!clockValueInRange(hours, 0, 23) || !clockValueInRange(minutes, 0, 59)) {
    snprintf(out, size, "--:--");
    return;
  }
  snprintf(out, size, "%02d:%02d", hours, minutes);
}

inline void clockFormatSeconds(char* out, size_t size, int seconds) {
  if (!clockValueInRange(seconds, 0, 59)) {
    snprintf(out, size, ":--");
    return;
  }
  snprintf(out, size, ":%02d", seconds);
}

inline void clockFormatDateLine(char* out, size_t size, int month, int date) {
  if (!clockValueInRange(month, 1, 12) || !clockValueInRange(date, 1, 31)) {
    snprintf(out, size, "--- --");
    return;
  }
  snprintf(out, size, "%s %02d", clockMonthLabel(month), date);
}

inline void clockFormatWeekDateLine(char* out, size_t size, int weekday,
                                    int month, int date) {
  if (!clockValueInRange(month, 1, 12) || !clockValueInRange(date, 1, 31)) {
    snprintf(out, size, "%s --- --", clockWeekdayLabel(weekday));
    return;
  }
  snprintf(out, size, "%s %s %02d", clockWeekdayLabel(weekday),
           clockMonthLabel(month), date);
}
