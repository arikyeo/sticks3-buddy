// Unit tests for logic/ui_layout_logic.h — the pure layout math behind the
// dashboard ticker (marquee), the paginated sessions screen, and the
// settings scroll window.
#include <unity.h>
#include "logic/ui_layout_logic.h"

void setUp() {}
void tearDown() {}

// --- pagination ---------------------------------------------------------------

void test_page_of_follows_selection() {
  TEST_ASSERT_EQUAL_UINT8(0, pageOf(0, 4));
  TEST_ASSERT_EQUAL_UINT8(0, pageOf(3, 4));
  TEST_ASSERT_EQUAL_UINT8(1, pageOf(4, 4));
  TEST_ASSERT_EQUAL_UINT8(1, pageOf(5, 4));
  TEST_ASSERT_EQUAL_UINT8(0, pageOf(9, 0));   // perPage 0 never divides
}

void test_page_count_rounds_up() {
  TEST_ASSERT_EQUAL_UINT8(1, pageCount(0, 4));   // empty list = one empty page
  TEST_ASSERT_EQUAL_UINT8(1, pageCount(4, 4));
  TEST_ASSERT_EQUAL_UINT8(2, pageCount(5, 4));
  TEST_ASSERT_EQUAL_UINT8(2, pageCount(6, 4));   // BUDDY_MAX_SESSIONS case
  TEST_ASSERT_EQUAL_UINT8(1, pageCount(6, 0));
}

void test_rows_on_page_clamps_the_tail() {
  TEST_ASSERT_EQUAL_UINT8(4, rowsOnPage(6, 0, 4));
  TEST_ASSERT_EQUAL_UINT8(2, rowsOnPage(6, 1, 4));
  TEST_ASSERT_EQUAL_UINT8(0, rowsOnPage(6, 2, 4));   // past-the-end page draws nothing
  TEST_ASSERT_EQUAL_UINT8(0, rowsOnPage(0, 0, 4));
}

void test_page_start_matches_rows() {
  // every session index lands on exactly one page at the right offset
  for (uint8_t i = 0; i < 6; i++) {
    uint8_t pg = pageOf(i, 4);
    TEST_ASSERT_TRUE(i >= pageStart(pg, 4));
    TEST_ASSERT_TRUE(i < pageStart(pg, 4) + rowsOnPage(6, pg, 4));
  }
}

// --- scroll window --------------------------------------------------------------

void test_window_pins_top_until_sel_passes_it() {
  TEST_ASSERT_EQUAL_UINT8(0, windowStart(0, 14, 8));
  TEST_ASSERT_EQUAL_UINT8(0, windowStart(7, 14, 8));
  TEST_ASSERT_EQUAL_UINT8(1, windowStart(8, 14, 8));
  TEST_ASSERT_EQUAL_UINT8(6, windowStart(13, 14, 8));   // last row visible
}

void test_window_never_over_scrolls() {
  // start never exceeds count - vis, even for a (bogus) huge selection
  TEST_ASSERT_EQUAL_UINT8(6, windowStart(200, 14, 8));
  TEST_ASSERT_EQUAL_UINT8(0, windowStart(5, 6, 8));    // list shorter than window
  TEST_ASSERT_EQUAL_UINT8(0, windowStart(3, 8, 0));    // degenerate window
}

// --- marquee ---------------------------------------------------------------------

void test_marquee_short_text_never_scrolls() {
  for (uint32_t t = 0; t < 100; t++)
    TEST_ASSERT_EQUAL_UINT16(0, marqueeOffset(10, 21, t));
  TEST_ASSERT_EQUAL_UINT16(0, marqueeOffset(21, 21, 55));   // exactly fits
}

void test_marquee_holds_then_scrolls_then_holds() {
  // len 30, window 21 -> span 9; HOLD is 8 ticks at each end
  TEST_ASSERT_EQUAL_UINT16(0, marqueeOffset(30, 21, 0));
  TEST_ASSERT_EQUAL_UINT16(0, marqueeOffset(30, 21, 7));    // still holding
  TEST_ASSERT_EQUAL_UINT16(1, marqueeOffset(30, 21, 9));
  TEST_ASSERT_EQUAL_UINT16(9, marqueeOffset(30, 21, 17));   // reached the end
  TEST_ASSERT_EQUAL_UINT16(9, marqueeOffset(30, 21, 20));   // holding at the end
}

void test_marquee_cycles_back_to_start() {
  // cycle = span + 2*HOLD = 9 + 16 = 25
  TEST_ASSERT_EQUAL_UINT16(0, marqueeOffset(30, 21, 25));
  TEST_ASSERT_EQUAL_UINT16(marqueeOffset(30, 21, 3),
                           marqueeOffset(30, 21, 3 + 25));
}

void test_marquee_offset_never_exceeds_span() {
  // the printed tail (text + off) must always have >= window chars left
  for (uint32_t t = 0; t < 200; t++) {
    uint16_t off = marqueeOffset(64, 21, t);
    TEST_ASSERT_TRUE(off <= 64 - 21);
  }
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_page_of_follows_selection);
  RUN_TEST(test_page_count_rounds_up);
  RUN_TEST(test_rows_on_page_clamps_the_tail);
  RUN_TEST(test_page_start_matches_rows);
  RUN_TEST(test_window_pins_top_until_sel_passes_it);
  RUN_TEST(test_window_never_over_scrolls);
  RUN_TEST(test_marquee_short_text_never_scrolls);
  RUN_TEST(test_marquee_holds_then_scrolls_then_holds);
  RUN_TEST(test_marquee_cycles_back_to_start);
  RUN_TEST(test_marquee_offset_never_exceeds_span);
  return UNITY_END();
}
