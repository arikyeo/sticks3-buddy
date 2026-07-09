#pragma once
#include <stdint.h>
#include <string.h>

// Greedy word-wrap into fixed-width rows (24-byte row storage). Continuation
// rows get a leading space. Returns the number of rows written. Pure logic —
// no Arduino dependency — so it unit-tests on the native host.
//
// Hardened against the wrapInto underflow (johnwong 3fa0884): without the
// `col < width` guard, `width - col` could underflow to ~255 and turn `take`
// into a giant memcpy past the 24-byte row — memory corruption fed straight
// from bridge-controlled transcript text (long no-space multibyte runs).
inline uint8_t wrapInto(const char* in, char out[][24], uint8_t maxRows, uint8_t width) {
  if (width > 23) width = 23;                  // row storage is 24 bytes incl. NUL
  uint8_t row = 0, col = 0;
  const char* p = in;
  while (*p && row < maxRows) {
    while (*p == ' ') p++;                     // skip leading spaces
    // measure next word
    const char* w = p;
    while (*p && *p != ' ') p++;
    uint8_t wlen = p - w;
    if (wlen == 0) break;
    uint8_t need = (col > 0 ? 1 : 0) + wlen;
    if (col + need > width) {
      out[row][col] = 0;
      if (++row >= maxRows) return row;
      out[row][0] = ' '; col = 1;              // continuation indent
    }
    if (col > 1 || (col == 1 && out[row][0] != ' ')) out[row][col++] = ' ';
    else if (col == 1 && row > 0) {}           // already have the indent space
    // hard-break words that still don't fit. Guard `col < width` so the
    // `width - col` below never underflows (uint8_t wraps to ~255), which
    // would turn `take` into a giant memcpy and corrupt memory past the
    // 24-byte row — a hang/crash fed straight from bridge transcript text.
    while (col < width && wlen > (uint8_t)(width - col)) {
      uint8_t take = width - col;
      if (take > wlen) take = wlen;            // never copy past the word
      memcpy(&out[row][col], w, take); col += take; w += take; wlen -= take;
      out[row][col] = 0;
      if (++row >= maxRows) return row;
      out[row][0] = ' '; col = 1;
    }
    memcpy(&out[row][col], w, wlen); col += wlen;
  }
  if (col > 0 && row < maxRows) { out[row][col] = 0; row++; }
  return row;
}
