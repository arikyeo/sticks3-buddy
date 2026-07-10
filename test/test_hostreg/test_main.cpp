// Unit tests for the pure half of src/host_registry.h — the multi-host
// table ops (adopt/find/touch/LRU/drop/pin-remap). The NVS + Bluedroid glue
// in host_registry.cpp is device-only and exercised by tools/test_protocol.py.
#include <unity.h>
#include <string.h>
#include "host_registry.h"

void setUp() {}
void tearDown() {}

static void bdaSet(uint8_t out[6], uint8_t seed) {
  for (int i = 0; i < 6; i++) out[i] = (uint8_t)(seed + i);
}

void test_adopt_names_host_n_and_finds_by_bda() {
  HostTable t;
  hostTabInit(&t);
  uint8_t a[6], b[6];
  bdaSet(a, 0x10);
  bdaSet(b, 0x20);
  TEST_ASSERT_EQUAL_INT(0, hostTabAdopt(&t, a));
  TEST_ASSERT_EQUAL_INT(1, hostTabAdopt(&t, b));
  TEST_ASSERT_EQUAL_UINT(2, t.count);
  TEST_ASSERT_EQUAL_STRING("Host-1", t.h[0].name);
  TEST_ASSERT_EQUAL_STRING("Host-2", t.h[1].name);
  TEST_ASSERT_EQUAL_INT(0, hostTabFindBda(&t, a));
  TEST_ASSERT_EQUAL_INT(1, hostTabFindBda(&t, b));
  uint8_t c[6];
  bdaSet(c, 0x30);
  TEST_ASSERT_EQUAL_INT(-1, hostTabFindBda(&t, c));
  TEST_ASSERT_EQUAL_UINT8(HOST_F_USED, t.h[0].flags & HOST_F_USED);
}

void test_adopt_full_returns_minus_one() {
  HostTable t;
  hostTabInit(&t);
  uint8_t a[6];
  for (uint8_t i = 0; i < BUDDY_MAX_HOSTS; i++) {
    bdaSet(a, (uint8_t)(i * 8));
    TEST_ASSERT_EQUAL_INT(i, hostTabAdopt(&t, a));
  }
  bdaSet(a, 0xF0);
  TEST_ASSERT_EQUAL_INT(-1, hostTabAdopt(&t, a));
  TEST_ASSERT_EQUAL_UINT(BUDDY_MAX_HOSTS, t.count);
}

void test_touch_moves_lru_victim() {
  HostTable t;
  hostTabInit(&t);
  uint8_t a[6], b[6], c[6];
  bdaSet(a, 1); bdaSet(b, 10); bdaSet(c, 20);
  hostTabAdopt(&t, a);   // seq 1
  hostTabAdopt(&t, b);   // seq 2
  hostTabAdopt(&t, c);   // seq 3
  TEST_ASSERT_EQUAL_INT(0, hostTabLru(&t));   // a is oldest
  hostTabTouch(&t, 0);                        // a freshened (seq 4)
  TEST_ASSERT_EQUAL_INT(1, hostTabLru(&t));   // now b is oldest
  hostTabTouch(&t, 1);
  TEST_ASSERT_EQUAL_INT(2, hostTabLru(&t));
}

void test_hello_updates_and_sanitizes() {
  HostTable t;
  hostTabInit(&t);
  uint8_t a[6];
  bdaSet(a, 3);
  int i = hostTabAdopt(&t, a);
  hostTabHello(&t, i, "a1b2c3d4", "Arik\xE2\x80\x99s Mac", "claude");
  TEST_ASSERT_EQUAL_STRING("a1b2c3d4", t.h[i].hostId);
  TEST_ASSERT_EQUAL_STRING("Arik's Mac", t.h[i].name);   // curly quote -> '
  TEST_ASSERT_EQUAL_STRING("claude", t.h[i].app);
  // empty fields leave stored values alone
  hostTabHello(&t, i, "", "", "");
  TEST_ASSERT_EQUAL_STRING("a1b2c3d4", t.h[i].hostId);
  TEST_ASSERT_EQUAL_STRING("Arik's Mac", t.h[i].name);
}

void test_hello_truncates_hostid_to_8_chars() {
  HostTable t;
  hostTabInit(&t);
  uint8_t a[6];
  bdaSet(a, 3);
  int i = hostTabAdopt(&t, a);
  hostTabHello(&t, i, "0123456789abcdef", "n", "a");
  TEST_ASSERT_EQUAL_STRING("01234567", t.h[i].hostId);
}

void test_drop_compacts_and_remaps_pin() {
  HostTable t;
  hostTabInit(&t);
  uint8_t a[6], b[6], c[6];
  bdaSet(a, 1); bdaSet(b, 10); bdaSet(c, 20);
  hostTabAdopt(&t, a);
  hostTabAdopt(&t, b);
  hostTabAdopt(&t, c);
  int8_t pin = 2;   // pinned to c
  hostTabDrop(&t, 1);   // drop b
  pin = hostTabRemapPin(pin, 1);
  TEST_ASSERT_EQUAL_UINT(2, t.count);
  TEST_ASSERT_EQUAL_INT(1, hostTabFindBda(&t, c));   // c shifted down
  TEST_ASSERT_EQUAL_INT8(1, pin);                    // pin followed it
  // dropping the pinned entry itself clears the pin
  pin = hostTabRemapPin(pin, 1);
  TEST_ASSERT_EQUAL_INT8(-1, pin);
  // pin below the dropped slot is untouched
  TEST_ASSERT_EQUAL_INT8(0, hostTabRemapPin(0, 1));
  // auto stays auto
  TEST_ASSERT_EQUAL_INT8(-1, hostTabRemapPin(-1, 0));
}

void test_drop_out_of_range_is_noop() {
  HostTable t;
  hostTabInit(&t);
  uint8_t a[6];
  bdaSet(a, 1);
  hostTabAdopt(&t, a);
  TEST_ASSERT_EQUAL_INT(1, hostTabDrop(&t, -1));
  TEST_ASSERT_EQUAL_INT(1, hostTabDrop(&t, 5));
  TEST_ASSERT_EQUAL_UINT(1, t.count);
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_adopt_names_host_n_and_finds_by_bda);
  RUN_TEST(test_adopt_full_returns_minus_one);
  RUN_TEST(test_touch_moves_lru_victim);
  RUN_TEST(test_hello_updates_and_sanitizes);
  RUN_TEST(test_hello_truncates_hostid_to_8_chars);
  RUN_TEST(test_drop_compacts_and_remaps_pin);
  RUN_TEST(test_drop_out_of_range_is_noop);
  return UNITY_END();
}
