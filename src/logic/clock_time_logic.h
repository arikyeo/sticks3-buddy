#pragma once
#include <stdint.h>
#include <time.h>

// Pure software wall-clock math. No Arduino / newlib dependencies beyond
// <time.h> types, so it compiles and unit-tests on the native host exactly
// as it runs on device.
//
// The bridge pushes already-localized time; we store its epoch ("local time
// treated as UTC") and project it forward with the caller's millisecond
// tick. Both directions use pure proleptic-Gregorian integer math (Howard
// Hinnant's days-from-civil / civil-from-days), never the C library's
// timezone-aware conversions — so no offset is ever applied twice.

// Broken-down time -> epoch, fields treated as UTC (newlib lacks timegm()).
inline time_t clockTmToEpochUtc(const struct tm* t) {
  int y = t->tm_year + 1900;
  int m = t->tm_mon + 1;
  long d = t->tm_mday;
  y -= (m <= 2);
  long era = (y >= 0 ? y : y - 399) / 400;
  unsigned yoe = (unsigned)(y - era * 400);
  unsigned doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + (unsigned)d - 1;
  unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  long days = era * 146097L + (long)doe - 719468L;
  return (time_t)days * 86400 + t->tm_hour * 3600 + t->tm_min * 60 + t->tm_sec;
}

// Epoch -> broken-down time, UTC. Portable replacement for gmtime_r so the
// header behaves identically under newlib (device), glibc (CI) and MinGW.
inline void clockEpochToTmUtc(time_t epoch, struct tm* out) {
  long days = (long)(epoch / 86400);
  long rem  = (long)(epoch % 86400);
  if (rem < 0) { rem += 86400; days -= 1; }
  out->tm_hour = (int)(rem / 3600);
  out->tm_min  = (int)((rem % 3600) / 60);
  out->tm_sec  = (int)(rem % 60);
  int wday = (int)((days + 4) % 7);            // 1970-01-01 was a Thursday
  out->tm_wday = wday < 0 ? wday + 7 : wday;
  long z = days + 719468L;
  long era = (z >= 0 ? z : z - 146096L) / 146097L;
  unsigned long doe = (unsigned long)(z - era * 146097L);
  unsigned long yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
  long y = (long)yoe + era * 400;
  unsigned long doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
  unsigned long mp  = (5 * doy + 2) / 153;
  unsigned long dd  = doy - (153 * mp + 2) / 5 + 1;
  unsigned long mm  = mp + (mp < 10 ? 3 : -9);
  y += (mm <= 2);
  out->tm_year  = (int)(y - 1900);
  out->tm_mon   = (int)mm - 1;
  out->tm_mday  = (int)dd;
  out->tm_yday  = 0;   // unused by the firmware
  out->tm_isdst = 0;
}

// Millis-projected software clock. Seed on every bridge time sync; project
// with the current tick on read. Valid only after a seed (resets with the
// MCU). Unsigned subtraction keeps it correct across one millis() wrap
// (~49.7 days); like the hardware it replaces, it drifts by whole wrap
// periods only if more than one wrap passes with no re-sync — the bridge
// re-syncs on every connect, so that never accumulates in practice.
struct SoftClock {
  bool     set;
  time_t   base;     // local-time epoch at the last sync
  uint32_t baseMs;   // caller tick (millis) at the last sync
};

inline void softClockInit(SoftClock& c) { c.set = false; c.base = 0; c.baseMs = 0; }

inline void softClockSeed(SoftClock& c, const struct tm* lt, uint32_t nowMs) {
  c.base   = clockTmToEpochUtc(lt);
  c.baseMs = nowMs;
  c.set    = true;
}

inline bool softClockGet(const SoftClock& c, uint32_t nowMs, struct tm* out) {
  if (!c.set) return false;
  time_t now = c.base + (time_t)((uint32_t)(nowMs - c.baseMs) / 1000U);
  clockEpochToTmUtc(now, out);
  return true;
}
