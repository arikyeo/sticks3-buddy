// Unit tests for logic/utf8_text_logic.h — codepoint helpers, safe
// truncation and the display sanitizer. Truncation assertions ported from
// CodeBuddy's firmware/tests/utf8_text_logic_test.cpp (their
// utf8CopyTruncate == our utf8SafePrefixBytes contract: never split a
// codepoint); sanitizer assertions cover this repo's punctuation mapping.
// All multibyte literals are spelled as escaped bytes so the tests are
// independent of the compiler's source-charset handling.
//   \xE4\xBD\xA0 = U+4F60 (ni3)   \xE5\xA5\xBD = U+597D (hao3)
#include <unity.h>
#include <string.h>
#include "logic/utf8_text_logic.h"

void setUp() {}
void tearDown() {}

void test_non_ascii_detection() {
  TEST_ASSERT_FALSE(utf8ContainsNonAscii("approve"));
  TEST_ASSERT_FALSE(utf8ContainsNonAscii(""));
  TEST_ASSERT_FALSE(utf8ContainsNonAscii(0));
  TEST_ASSERT_TRUE(utf8ContainsNonAscii("\xE6\x89\xB9\xE5\x87\x86"));   // CJK
  TEST_ASSERT_TRUE(utf8ContainsNonAscii("caf\xC3\xA9"));                // e-acute
}

void test_codepoint_bytes_from_lead() {
  TEST_ASSERT_EQUAL_UINT(1, utf8CodepointBytes('a'));
  TEST_ASSERT_EQUAL_UINT(2, utf8CodepointBytes(0xC3));
  TEST_ASSERT_EQUAL_UINT(3, utf8CodepointBytes(0xE4));
  TEST_ASSERT_EQUAL_UINT(4, utf8CodepointBytes(0xF0));
  TEST_ASSERT_EQUAL_UINT(1, utf8CodepointBytes(0x80));   // stray continuation
}

void test_safe_prefix_never_splits_codepoint() {
  // "ni hao" + "ab": 3+3+1+1 bytes. 4 bytes of room fits only the first CJK
  // glyph (3B) — taking a 4th byte would split the second glyph.
  const char* s = "\xE4\xBD\xA0\xE5\xA5\xBD" "ab";
  TEST_ASSERT_EQUAL_UINT(3, utf8SafePrefixBytes(s, 4));
  // "abc" + CJK(3B): 6 bytes fits "abc" + one whole glyph exactly.
  const char* s2 = "abc\xE4\xBD\xA0\xE5\xA5\xBD";
  TEST_ASSERT_EQUAL_UINT(6, utf8SafePrefixBytes(s2, 6));
  // 5 bytes of room: the glyph after "abc" would need 6 — stop at 3.
  TEST_ASSERT_EQUAL_UINT(3, utf8SafePrefixBytes(s2, 5));
  // plain ASCII passes through byte-for-byte
  TEST_ASSERT_EQUAL_UINT(3, utf8SafePrefixBytes("abcdef", 3));
  TEST_ASSERT_EQUAL_UINT(0, utf8SafePrefixBytes(0, 8));
}

void test_sanitize_maps_typographic_punctuation() {
  char out[32];
  utf8Sanitize(out, sizeof(out), "a\xE2\x80\x94" "b");        // em dash
  TEST_ASSERT_EQUAL_STRING("a-b", out);
  utf8Sanitize(out, sizeof(out), "it\xE2\x80\x99s");          // curly apostrophe
  TEST_ASSERT_EQUAL_STRING("it's", out);
  utf8Sanitize(out, sizeof(out), "\xE2\x80\x9Chi\xE2\x80\x9D");   // curly quotes
  TEST_ASSERT_EQUAL_STRING("\"hi\"", out);
  utf8Sanitize(out, sizeof(out), "wait\xE2\x80\xA6");         // ellipsis
  TEST_ASSERT_EQUAL_STRING("wait...", out);
  utf8Sanitize(out, sizeof(out), "a\xE2\x86\x92" "b");        // right arrow
  TEST_ASSERT_EQUAL_STRING("a->b", out);
  utf8Sanitize(out, sizeof(out), "\xE2\x80\xA2 item");        // bullet
  TEST_ASSERT_EQUAL_STRING("* item", out);
  utf8Sanitize(out, sizeof(out), "a\xC2\xA0" "b");            // nbsp
  TEST_ASSERT_EQUAL_STRING("a b", out);
}

void test_sanitize_unknown_glyphs_and_controls() {
  char out[32];
  utf8Sanitize(out, sizeof(out), "\xE4\xBD\xA0\xE5\xA5\xBD");   // CJK -> ?
  TEST_ASSERT_EQUAL_STRING("??", out);
  utf8Sanitize(out, sizeof(out), "a\tb\nc");                    // controls -> space
  TEST_ASSERT_EQUAL_STRING("a b c", out);
  utf8Sanitize(out, sizeof(out), "ok\x80\x80!");                // stray bytes skipped
  TEST_ASSERT_EQUAL_STRING("ok!", out);
}

void test_sanitize_respects_cap_and_terminates() {
  char out[4];
  memset(out, 'X', sizeof(out));
  utf8Sanitize(out, 3, "abcdef");          // cap 3 -> 2 chars + NUL
  TEST_ASSERT_EQUAL_STRING("ab", out);
  // multi-char replacement truncated mid-expansion still terminates in-cap
  utf8Sanitize(out, 3, "\xE2\x80\xA6");    // "..." but only 2 fit
  TEST_ASSERT_EQUAL_STRING("..", out);
  utf8Sanitize(out, 1, "abc");             // cap 1 -> empty string
  TEST_ASSERT_EQUAL_STRING("", out);
}

void test_sanitize_array_overload() {
  char out[6];
  utf8Sanitize(out, "a\xE2\x80\x93" "b longer than six");   // en dash
  TEST_ASSERT_EQUAL_STRING("a-b l", out);                   // 5 chars + NUL
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_non_ascii_detection);
  RUN_TEST(test_codepoint_bytes_from_lead);
  RUN_TEST(test_safe_prefix_never_splits_codepoint);
  RUN_TEST(test_sanitize_maps_typographic_punctuation);
  RUN_TEST(test_sanitize_unknown_glyphs_and_controls);
  RUN_TEST(test_sanitize_respects_cap_and_terminates);
  RUN_TEST(test_sanitize_array_overload);
  return UNITY_END();
}
