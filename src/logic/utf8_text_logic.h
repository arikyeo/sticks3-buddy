#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

// UTF-8 -> display-safe ASCII mapping for the transcript / prompt render
// path. Merged from MaikEight's _sanitize (accd745: typographic-punctuation
// mapping) and CodeBuddy's utf8_text_logic.h (codepoint helpers). Pure
// logic — no Arduino dependency — so it unit-tests on the native host.
//
// The on-device fonts (GLCD / FreeMono) only cover ASCII 0x20-0x7E, but
// desktop text is full of UTF-8 typographic punctuation (em dash, curly
// quotes, ellipsis) that would otherwise render as tofu boxes — and CJK
// text that can't render at all. Map the common punctuation to ASCII
// equivalents and collapse anything else to '?'.

inline bool utf8ContainsNonAscii(const char* text) {
  if (!text) return false;
  for (const unsigned char* p = (const unsigned char*)text; *p; ++p) {
    if (*p & 0x80) return true;
  }
  return false;
}

// Sequence length promised by a UTF-8 lead byte (1 for ASCII / stray bytes).
inline size_t utf8CodepointBytes(unsigned char lead) {
  if ((lead & 0x80) == 0x00) return 1;
  if ((lead & 0xE0) == 0xC0) return 2;
  if ((lead & 0xF0) == 0xE0) return 3;
  if ((lead & 0xF8) == 0xF0) return 4;
  return 1;
}

// Longest prefix of `text` that fits in maxBytes without splitting a
// codepoint — for safe truncating copies of raw (unsanitized) UTF-8.
inline size_t utf8SafePrefixBytes(const char* text, size_t maxBytes) {
  if (!text) return 0;
  size_t used = 0;
  while (text[used] && used < maxBytes) {
    size_t cp = utf8CodepointBytes((unsigned char)text[used]);
    if (cp > 1) {
      for (size_t i = 1; i < cp; ++i) {
        unsigned char c = (unsigned char)text[used + i];
        if (c == 0 || (c & 0xC0) != 0x80) { cp = 1; break; }   // truncated seq
      }
    }
    if (used + cp > maxBytes) break;
    used += cp;
  }
  return used;
}

// Decode UTF-8, map typographic punctuation to ASCII lookalikes, replace
// everything else non-printable with '?' (unknown glyph) or ' ' (control).
// Copies up to cap-1 chars + NUL; always terminates. dst != src.
inline void utf8Sanitize(char* dst, size_t cap, const char* src) {
  if (!dst || cap == 0) return;
  if (!src) { dst[0] = 0; return; }
  size_t o = 0;
  for (size_t i = 0; src[i] && o < cap - 1; ) {
    unsigned char c = (unsigned char)src[i];
    if (c < 0x80) {                                  // plain ASCII
      dst[o++] = (c >= 0x20 && c < 0x7F) ? (char)c : ' ';
      i++;
      continue;
    }
    uint32_t cp = 0; int len;                        // decode UTF-8 sequence
    if      ((c & 0xE0) == 0xC0) { cp = c & 0x1F; len = 2; }
    else if ((c & 0xF0) == 0xE0) { cp = c & 0x0F; len = 3; }
    else if ((c & 0xF8) == 0xF0) { cp = c & 0x07; len = 4; }
    else { i++; continue; }                          // stray byte -> skip
    for (int k = 1; k < len; k++) {
      unsigned char cc = (unsigned char)src[i + k];
      if ((cc & 0xC0) != 0x80) { len = k; break; }   // truncated sequence
      cp = (cp << 6) | (cc & 0x3F);
    }
    i += len;
    const char* rep;
    switch (cp) {
      case 0x2012: case 0x2013: case 0x2014: case 0x2015: rep = "-";   break; // dashes
      case 0x2018: case 0x2019: case 0x201B: case 0x2032: rep = "'";   break; // single quotes
      case 0x201C: case 0x201D: case 0x201F: case 0x2033: rep = "\"";  break; // double quotes
      case 0x2026: rep = "..."; break;                                        // ellipsis
      case 0x2022: case 0x00B7: case 0x2219: rep = "*";  break;               // bullet / middot
      case 0x2192: rep = "->"; break;                                         // right arrow
      case 0x00A0: rep = " ";  break;                                         // nbsp
      default:     rep = "?";  break;                                         // unknown glyph
    }
    for (const char* r = rep; *r && o < cap - 1; r++) dst[o++] = *r;
  }
  dst[o] = 0;
}

template <size_t N>
inline void utf8Sanitize(char (&dst)[N], const char* src) {
  utf8Sanitize(dst, N, src);
}
