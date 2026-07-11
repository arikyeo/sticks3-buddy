#pragma once
#include <stdint.h>

// Pure UI layout math — pagination for fixed-height row lists (sessions
// screen), scroll-window placement (settings menu), and the dashboard
// ticker's marquee offset. No Arduino dependency — unit-tests on the
// native host (test/test_uilayout).

// --- pagination ---------------------------------------------------------------
// A list of `count` rows shown `perPage` at a time. Pages are derived from
// the selection index so the page always follows the cursor.
inline uint8_t pageOf(uint8_t idx, uint8_t perPage) {
  return perPage ? (uint8_t)(idx / perPage) : 0;
}

inline uint8_t pageCount(uint8_t count, uint8_t perPage) {
  if (!perPage || !count) return 1;   // an empty list still has one (empty) page
  return (uint8_t)((count + perPage - 1) / perPage);
}

inline uint8_t pageStart(uint8_t page, uint8_t perPage) {
  return (uint8_t)(page * perPage);
}

inline uint8_t rowsOnPage(uint8_t count, uint8_t page, uint8_t perPage) {
  uint8_t s = pageStart(page, perPage);
  if (s >= count) return 0;
  uint8_t left = (uint8_t)(count - s);
  return left < perPage ? left : perPage;
}

// --- scroll window --------------------------------------------------------------
// First visible row of a `vis`-row window over `count` rows such that `sel`
// stays visible. The window slides forward as the cursor advances and snaps
// back to the top when the cursor wraps to row 0.
inline uint8_t windowStart(uint8_t sel, uint8_t count, uint8_t vis) {
  if (count <= vis || vis == 0) return 0;
  if (sel < vis) return 0;
  uint8_t start = (uint8_t)(sel - vis + 1);
  uint8_t maxStart = (uint8_t)(count - vis);
  return start > maxStart ? maxStart : start;
}

// --- marquee ---------------------------------------------------------------------
// Character offset for a one-line horizontal ticker. `len` = text length,
// `window` = visible characters, `tick` = a monotonically increasing step
// counter (caller derives it from elapsed-ms / step-ms, restarting at 0
// when the content changes). Short text pins at 0; long text holds at the
// start, scrolls one char per tick to the end, holds there, and repeats —
// both ends stay readable.
inline uint16_t marqueeOffset(uint16_t len, uint16_t window, uint32_t tick) {
  if (window == 0 || len <= window) return 0;
  const uint16_t HOLD = 8;                  // ticks parked at each end
  uint16_t span = (uint16_t)(len - window); // maximum offset
  uint32_t cycle = (uint32_t)span + 2u * HOLD;
  uint32_t ph = tick % cycle;
  if (ph < HOLD) return 0;
  if (ph < (uint32_t)HOLD + span) return (uint16_t)(ph - HOLD);
  return span;
}
